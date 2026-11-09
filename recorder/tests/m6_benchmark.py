#!/usr/bin/env python3
"""M6: multi-camera performance baseline at 4, 8 and 10 cameras.

usage:
  sudo tests/m6_benchmark.py --dry-run            # print the plan, change nothing
  sudo tests/m6_benchmark.py                      # 4, 8, 10 cameras, 3 rounds (about 2 hours)
  sudo tests/m6_benchmark.py --counts 10 --rounds 1 --minutes 5
  sudo tests/m6_benchmark.py --counts 10,15 --copies   # more cameras than the site has (see below)

For each camera count the stack is started with only the first N cameras of cameras.yaml: each
records its main stream continuously, and its substream feeds Frigate and the substream archiver,
as in normal running. After a warm-up the gateway is sampled every 5 s: CPU busy (all cores), RAM
used, write rate to the recording disk, network input on the camera LAN, CPU package temperature;
average and peak, plus each container's share. Then every camera of the run is checked for
recording over the sampled minutes: both streams recorded, no gap over 10 s. The rounds repeat
the whole sequence, so the spread between rounds shows how repeatable the numbers are. At the
end, and if anything fails, the normal stack with every camera is started again.

--copies allows counts above the number of cameras in cameras.yaml. The extra cameras (copy11,
copy12, ...) are copies of the first cameras' two streams, taken from go2rtc's own RTSP restream,
so go2rtc, Frigate and the substream archiver do the same work for each copy as for a real
camera. No extra connection goes to the cameras, so the camera network carries only the real
cameras. After the run the copies' substream folders are deleted; their main-stream files are
deleted by Frigate after the retention days (Frigate removes recordings of cameras no longer in
its config). The cloud uploader, if it runs, sees the copies' substream segments during the run.

Each start restarts the recorder, a short gap on every camera, and cameras left out of a run are
not recorded during it: post the planned window to the group first. The windows are added to
reports/planned-windows.txt.

Limits agreed before start (docs/acceptance-limits-proposal.md), checked on the 10-camera runs:
CPU average under 60 %, peak under 85 %, CPU temperature under 80 °C.

Lab: --project, --compose-file and --sampler-container run the same steps on tests/lab/lab.sh
(see tests/lab/README.md); there the numbers are the workstation's, not the gateway's.
"""
import argparse
import csv
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import continuity  # noqa: E402
import m7_check  # noqa: E402
import vivo  # noqa: E402

LIMITS = {"cpu_avg": 60.0, "cpu_peak": 85.0, "temp": 80.0}
STEP = 5
MAX_GAP = 10.0            # longest recording gap allowed inside a sampled window, seconds (as M7)
MIN_RECORDED = 98.0       # share of the checked window each stream must cover, percent (as M7)
UNWRITTEN = 120           # the newest two minutes may still be in a .part file (as M7)
MIN_CHECKED = 60          # shortest window worth a recording check, seconds (needs --minutes 3 or more)
COPY_CREDENTIALS = "FRIGATE_GO2RTC_RTSP"   # go2rtc's restream login, -> FRIGATE_GO2RTC_RTSP_USER/_PASSWORD
COPY_RE = re.compile(r"^copy\d{2,3}$")


def with_copies(cams: list[dict], n: int) -> list[dict]:
    """The first n cameras; past the end of the list, copies of the real cameras' streams.

    A copy takes its camera's two streams from go2rtc's own RTSP restream on 127.0.0.1:8554, so it
    costs go2rtc, Frigate and the substream archiver what one more camera costs, without another
    connection to the camera.
    """
    if not cams:
        raise ValueError("no cameras to copy")
    if clash := [c["id"] for c in cams if COPY_RE.match(c["id"])]:
        raise ValueError(f"camera ids {clash} look like benchmark copies; rename them first")
    out = [dict(c) for c in cams[:n]]
    for k in range(max(0, n - len(cams))):
        src = cams[k % len(cams)]
        out.append({**src, "id": f"copy{len(cams) + k + 1:02d}", "name": f"COPY-OF-{src['name']}",
                    "host": "127.0.0.1", "port": 8554, "main_path": f"/{src['id']}_main",
                    "sub_path": f"/{src['id']}_sub", "credentials": COPY_CREDENTIALS,
                    "notes": f"M6 benchmark copy of {src['id']}, not a camera"})
    return out


def recording_check(cams: list[str], after: float, before: float, root: Path) -> list[dict]:
    """Per camera over [after, before]: share of the window each stream recorded, longest gap."""
    span = max(before - after, 1.0)
    rows = []
    for cam in cams:
        try:
            sub = m7_check.substream(root, cam, after, before)
            main = continuity.gaps(cam, after, before)
        except Exception as e:   # one camera's error must not lose the benchmark numbers
            rows.append({"camera": cam, "error": vivo.redact(str(e))[:120]})
            continue
        # Frigate's first and last segments can reach past the window, so its share is capped at 100%.
        rows.append({"camera": cam,
                     "main_pct": min(100.0, 100 * main["covered"] / span),
                     "main_gap": max((b - a for a, b in main["gaps"]), default=0.0),
                     "sub_pct": min(100.0, 100 * sub["covered"] / span),
                     "sub_gap": max((b - a for a, b in sub["gaps"]), default=0.0)})
    return rows


def recording_ok(row: dict) -> bool:
    return ("error" not in row and row["main_pct"] >= MIN_RECORDED and row["sub_pct"] >= MIN_RECORDED
            and row["main_gap"] <= MAX_GAP and row["sub_gap"] <= MAX_GAP)


def remove_copy_folders(root: Path, ids: list[str]) -> list[str]:
    """Delete the copies' substream folders (only names made by with_copies, only below root)."""
    removed = []
    for cam in ids:
        d = root / cam
        if COPY_RE.match(cam) and d.is_dir() and d.resolve().parent == root.resolve():
            shutil.rmtree(d, ignore_errors=True)
            removed.append(cam)
    return removed


class Reader:
    """Reads /proc and /sys on the gateway, or inside a container (lab on a workstation)."""

    def __init__(self, container: str | None):
        self.container = container

    def read(self, *paths: str) -> list[str]:
        if not self.container:
            out = []
            for p in paths:
                try:
                    out.append(Path(p).read_text())
                except OSError:
                    out.append("")
            return out
        script = 'for f; do cat "$f" 2>/dev/null; echo "@@END@@"; done'
        res = subprocess.run(["docker", "exec", self.container, "sh", "-c", script, "sh", *paths],
                             capture_output=True, text=True, timeout=30)
        parts = res.stdout.split("@@END@@\n")
        return (parts + [""] * len(paths))[:len(paths)]


def cpu_ticks(stat: str) -> tuple[int, int]:
    """(idle, total) jiffies from /proc/stat."""
    for line in stat.splitlines():
        if line.startswith("cpu "):
            v = [int(x) for x in line.split()[1:]]
            return v[3] + v[4], sum(v[:8])
    return 0, 0


def mem_used_mb(meminfo: str) -> float:
    kv = {l.split(":")[0]: int(l.split()[1]) for l in meminfo.splitlines() if ":" in l and len(l.split()) >= 2}
    return (kv.get("MemTotal", 0) - kv.get("MemAvailable", 0)) / 1024


def disk_written(diskstats: str, device: str | None) -> int:
    """Bytes written to `device` (e.g. dm-1), or to all whole disks when it is not known."""
    total = 0
    for line in diskstats.splitlines():
        f = line.split()
        if len(f) < 10:
            continue
        name = f[2]
        whole = name.startswith(("sd", "vd", "nvme", "mmcblk")) and not name[-1].isdigit() or (
            name.startswith("nvme") and "p" not in name[4:])
        if (device and name == device) or (not device and whole):
            total += int(f[9]) * 512
    return total


def net_received(netdev: str, iface: str | None) -> int:
    total = 0
    for line in netdev.splitlines()[2:]:
        name, _, rest = line.partition(":")
        name = name.strip()
        if (iface and name == iface) or (not iface and name != "lo"):
            total += int(rest.split()[0])
    return total


def temperature(reader: Reader) -> float | None:
    """CPU package temperature, the same sensor order as tests/idle_report.sh."""
    script = ('for h in /sys/class/hwmon/hwmon*; do [ "$(cat $h/name 2>/dev/null)" = coretemp ] && '
              'cat $h/temp1_input 2>/dev/null && exit; done; for want in x86_pkg_temp TCPU acpitz; do '
              'for z in /sys/class/thermal/thermal_zone*; do [ "$(cat $z/type 2>/dev/null)" = $want ] && '
              'cat $z/temp 2>/dev/null && exit; done; done')
    cmd = ["sh", "-c", script] if not reader.container else ["docker", "exec", reader.container, "sh", "-c", script]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=10).stdout.split()
    try:
        return int(out[0]) / 1000
    except (IndexError, ValueError):
        return None


def media_device(media: Path) -> str | None:
    """Kernel name of the block device holding the recording disk (dm-N for LVM), Linux only."""
    try:
        dev = os.stat(media).st_dev
        link = Path(f"/sys/dev/block/{os.major(dev)}:{os.minor(dev)}").resolve()
        return link.name
    except OSError:
        return None


def lan_iface(ip: str) -> str | None:
    out = subprocess.run(["ip", "-o", "-4", "addr", "show"], capture_output=True, text=True).stdout \
        if shutil.which("ip") else ""
    for line in out.splitlines():
        f = line.split()
        if len(f) > 3 and f[3].split("/")[0] == ip:
            return f[1]
    return None


def container_cpu() -> dict[str, float]:
    out = subprocess.run(["docker", "stats", "--no-stream", "--format", "{{json .}}"],
                         capture_output=True, text=True, timeout=60).stdout
    shares = {}
    for line in out.splitlines():
        try:
            d = json.loads(line)
            shares[d["Name"]] = float(d["CPUPerc"].rstrip("%"))
        except (ValueError, KeyError):
            pass
    return shares


class Stack:
    def __init__(self, project: Path, extra: list[str], etc: Path):
        self.project, self.extra, self.etc = project, extra, etc

    def compose(self, project: Path, *args: str) -> None:
        files = ["-f", str(project / "compose.yaml")] + [x for f in self.extra for x in ("-f", str(project / f))]
        cmd = ["docker", "compose", "--project-directory", str(project), *files,
               "--env-file", str(project / "versions.env"), "--env-file", str(self.etc / "site.env"), *args]
        res = subprocess.run(cmd, capture_output=True, text=True, env={**os.environ, "VIVO_ETC": str(self.etc)})
        if res.returncode:
            raise RuntimeError(vivo.redact(res.stderr.strip()[-400:]))

    def start_subset(self, n: int, inventory: Path, work: Path) -> None:
        """Copy the project, keep the first n cameras, regenerate the configs, start that stack."""
        import yaml
        d = work / f"cams{n}"
        if d.exists():
            shutil.rmtree(d)
        shutil.copytree(self.project, d, ignore=shutil.ignore_patterns(".git", "reports", "__pycache__"))
        inv = yaml.safe_load(inventory.read_text())
        inv["cameras"] = with_copies(inv["cameras"], n)
        (d / "cameras.yaml").write_text(yaml.safe_dump(inv, sort_keys=False))
        subprocess.run([sys.executable, str(d / "tools" / "gen_config.py"), "--inventory", str(d / "cameras.yaml"),
                        "--out", str(d)], check=True, capture_output=True)
        self.compose(d, "up", "-d", "--force-recreate", "--remove-orphans")

    def restore(self) -> None:
        self.compose(self.project, "up", "-d", "--force-recreate", "--remove-orphans")


def sample(reader: Reader, seconds: int, device: str | None, iface: str | None, csv_rows: list, label: str) -> dict:
    files = ("/proc/stat", "/proc/meminfo", "/proc/diskstats", "/proc/net/dev")
    stat, mem, disk, net = reader.read(*files)
    i0, t0 = cpu_ticks(stat)
    w0, r0, clock0 = disk_written(disk, device), net_received(net, iface), time.time()
    cols = {"cpu": [], "ram": [], "write": [], "net": [], "temp": []}
    shares: dict[str, list[float]] = {}
    end = time.time() + seconds
    n = 0
    while time.time() < end:
        time.sleep(STEP)
        stat, mem, disk, net = reader.read(*files)
        i1, t1 = cpu_ticks(stat)
        w1, r1, clock1 = disk_written(disk, device), net_received(net, iface), time.time()
        dt = max(clock1 - clock0, 0.1)
        row = {"cpu": 100 * (1 - (i1 - i0) / max(t1 - t0, 1)), "ram": mem_used_mb(mem),
               "write": (w1 - w0) / dt / 1e6, "net": (r1 - r0) * 8 / dt / 1e6, "temp": temperature(reader)}
        for k, v in row.items():
            if v is not None:
                cols[k].append(v)
        csv_rows.append({"run": label, "utc": time.strftime("%H:%M:%S", time.gmtime()), **row})
        i0, t0, w0, r0, clock0 = i1, t1, w1, r1, clock1
        n += 1
        if n % 24 == 0:   # every 2 minutes, each container's CPU share
            for name, v in container_cpu().items():
                shares.setdefault(name, []).append(v)
    for name, v in container_cpu().items():   # and once at the end, so short runs have one too
        shares.setdefault(name, []).append(v)
    stats = {k: (sum(v) / len(v), max(v)) if v else (None, None) for k, v in cols.items()}
    stats["containers"] = {k: sum(v) / len(v) for k, v in shares.items()}
    return stats


def fmt(v, unit="", digits=1) -> str:
    return "n/a" if v is None else f"{v:.{digits}f}{unit}"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--counts", default="4,8,10")
    ap.add_argument("--rounds", type=int, default=3)
    ap.add_argument("--warmup", type=int, default=180, help="seconds after each start before sampling")
    ap.add_argument("--minutes", type=float, default=10, help="sampling time per run")
    ap.add_argument("--copies", action="store_true",
                    help="allow counts above the cameras in cameras.yaml, filled with copies of their streams")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--project", default=str(vivo.ROOT), help="folder of the running stack (default: this repository)")
    ap.add_argument("--compose-file", action="append", default=[], help="extra compose file, relative to --project (lab)")
    ap.add_argument("--sampler-container", help="read /proc inside this container (lab on a workstation)")
    args = ap.parse_args()

    counts = [int(x) for x in args.counts.split(",")]
    inventory = vivo.INVENTORY
    real = vivo.cameras(inventory)
    total_cams = len(real)
    counts = [n for n in counts if 0 < n <= total_cams or (args.copies and n > 0)]
    if not counts:
        print("no camera count to run (counts above the cameras in cameras.yaml need --copies)")
        return 2
    with_copies(real, max(counts))   # refuses ids that clash with the copies' names before anything starts
    per_run = args.warmup + args.minutes * 60
    copies = max(counts) - total_cams
    plan = (f"{args.rounds} round(s) of {counts} cameras, {args.warmup} s warm-up + {args.minutes:g} min sampling each: "
            f"about {len(counts) * args.rounds * per_run / 60:.0f} minutes. Cameras beyond N are not recorded during a run."
            + (f" Above {total_cams} cameras the extra ones are copies of real cameras' streams through go2rtc "
               "(no extra camera connections)." if copies > 0 else ""))
    print(plan)
    if args.dry_run:
        for n in counts:
            ids = [c["id"] for c in with_copies(real, n)]
            print(f"  {n} cameras: {', '.join(ids)}")
        return 0
    substream_root = Path(vivo.SITE.get("MEDIA_ROOT", "/srv/vivo")) / "recordings" / "substream"

    reader = Reader(args.sampler_container)
    media = Path(vivo.SITE.get("MEDIA_ROOT", "/srv/vivo"))
    device = None if args.sampler_container else media_device(media)
    iface = None if args.sampler_container else lan_iface(vivo.SITE.get("GATEWAY_LAN_IP", ""))
    stack = Stack(Path(args.project), args.compose_file, vivo.ETC)
    csv_rows: list[dict] = []
    results: list[tuple[int, int, dict]] = []
    recordings: list[tuple[int, int, float, float, list[dict]]] = []
    windows: list[str] = []
    removed: list[str] = []
    stopped = False

    def stop(*_):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, stop)   # systemctl stop: end the run, restore the stack, report, like Ctrl-C

    work = Path(tempfile.mkdtemp(prefix="vivo-m6-"))
    w0 = None
    try:
        for rnd in range(1, args.rounds + 1):
            for n in counts:
                w0 = time.time()
                print(f"round {rnd}: starting {n} cameras")
                stack.start_subset(n, inventory, work)
                time.sleep(args.warmup)
                print(f"round {rnd}: sampling {n} cameras for {args.minutes:g} min")
                s0 = time.time()
                results.append((rnd, n, sample(reader, int(args.minutes * 60), device, iface, csv_rows, f"r{rnd}-{n}")))
                s1 = time.time() - UNWRITTEN
                if s1 - s0 >= MIN_CHECKED:
                    time.sleep(15)   # let Frigate list the last finished segments
                    ids = [c["id"] for c in with_copies(real, n)]
                    rows = recording_check(ids, s0, s1, substream_root)
                    recordings.append((rnd, n, s0, s1, rows))
                    bad = [r["camera"] for r in rows if not recording_ok(r)]
                    print(f"round {rnd}: recording at {n} cameras: "
                          + (f"problem on {', '.join(bad)}" if bad else "every camera OK"))
                else:
                    print(f"round {rnd}: recording not checked (sampling shorter than {(UNWRITTEN + MIN_CHECKED) // 60} min)")
                label = f"{n} of {total_cams} cameras" if n <= total_cams else f"{n} cameras ({total_cams} + {n - total_cams} copies)"
                windows.append(f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(w0))}/"
                               f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} M6 benchmark, {label}")
                w0 = None
    except KeyboardInterrupt:
        stopped = True
        print("stopped: ending the run early")
    finally:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)   # a second stop must not interrupt the restore
        if w0 is not None:   # the run that was cut short is a planned window too
            windows.append(f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(w0))}/"
                           f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} M6 benchmark, stopped early")
        print("starting the normal stack again")
        try:
            stack.restore()
        finally:   # the log and the clean-up happen even when the restore fails (that error still ends the run)
            shutil.rmtree(work, ignore_errors=True)
            if copies > 0:
                time.sleep(10)   # the restarted archiver no longer writes the copies' folders
                removed = remove_copy_folders(substream_root,
                                              [c["id"] for c in with_copies(real, max(counts))[total_cams:]])
            if windows:
                vivo.REPORTS.mkdir(exist_ok=True)
                with open(vivo.REPORTS / "planned-windows.txt", "a") as f:
                    f.write("\n".join(windows) + "\n")

    md = ["# M6 multi-camera performance baseline", "",
          f"{time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime())}. {plan}",
          f"Sampled every {STEP} s. Recording disk device: {device or 'all disks'}; camera network: {iface or 'all interfaces'}."
          + (f" Read inside container `{args.sampler_container}` (lab, not the gateway)." if args.sampler_container else "")]
    if copies > 0:
        extra = with_copies(real, max(counts))[total_cams:]
        pairs = ", ".join(f"{x['id']} of {x['main_path'].strip('/').rsplit('_', 1)[0]}" for x in extra)
        md += ["", f"Above {total_cams} cameras the extra cameras are copies: {pairs}. "
               "Each copy takes its camera's main stream and substream from go2rtc's own RTSP restream, so go2rtc, "
               "Frigate (recording the main stream, decoding the substream) and the substream archiver do the same "
               "work for it as for a real camera. No extra connection goes to the cameras, so the network input "
               f"shows only the {total_cams} real cameras. "
               + (f"After the run the copies' substream folders were deleted ({', '.join(removed)}); "
                  if removed else "")
               + "Frigate deletes the copies' main-stream files after the retention days."]
    if stopped:
        md += ["", f"**Stopped early** (systemctl stop or Ctrl-C) after {len(results)} of {len(counts) * args.rounds} runs; "
               "the normal stack was started again. The numbers below are the runs that finished."]
    md += ["", "| Round | Cameras | CPU avg | CPU peak | RAM avg | RAM peak | Disk write avg | Network in avg | Temp avg | Temp peak |",
          "|---|---|---|---|---|---|---|---|---|---|"]
    for rnd, n, s in results:
        md.append(f"| {rnd} | {n} | {fmt(s['cpu'][0], ' %')} | {fmt(s['cpu'][1], ' %')} | {fmt(s['ram'][0], ' MB', 0)} | "
                  f"{fmt(s['ram'][1], ' MB', 0)} | {fmt(s['write'][0], ' MB/s', 2)} | {fmt(s['net'][0], ' Mbit/s')} | "
                  f"{fmt(s['temp'][0], ' °C')} | {fmt(s['temp'][1], ' °C')} |")
    md += ["", "## Spread between rounds (repeatability)", "", "| Cameras | CPU avg, lowest to highest | RAM avg | Disk write avg |",
           "|---|---|---|---|"]
    for n in counts:
        rs = [s for _, c, s in results if c == n]
        if not rs:   # a run stopped early may not have reached this count
            continue
        lo_hi = lambda k, d=1: f"{min(s[k][0] for s in rs):.{d}f} to {max(s[k][0] for s in rs):.{d}f}"  # noqa: E731
        md.append(f"| {n} | {lo_hi('cpu')} % | {lo_hi('ram', 0)} MB | {lo_hi('write', 2)} MB/s |")
    md += ["", "## CPU by container (average of the samples, percent of one core)", ""]
    names = sorted({k for _, _, s in results for k in s["containers"]})
    md += ["| Round | Cameras | " + " | ".join(names) + " |", "|---|---|" + "---|" * len(names)]
    for rnd, n, s in results:
        md.append(f"| {rnd} | {n} | " + " | ".join(fmt(s["containers"].get(k)) for k in names) + " |")

    top = [s for _, n, s in results if n == max(counts)]
    verdict = []
    if top:
        cpu_avg = max(s["cpu"][0] for s in top)
        cpu_peak = max(s["cpu"][1] for s in top)
        temps = [s["temp"][1] for s in top if s["temp"][1] is not None]
        ok_cpu = cpu_avg < LIMITS["cpu_avg"] and cpu_peak < LIMITS["cpu_peak"]
        ok_temp = bool(temps) and max(temps) < LIMITS["temp"]
        verdict += [f"- CPU at {max(counts)} cameras: average {cpu_avg:.1f} % (limit {LIMITS['cpu_avg']:g} %), "
                    f"peak {cpu_peak:.1f} % (limit {LIMITS['cpu_peak']:g} %): **{'PASS' if ok_cpu else 'FAIL'}**",
                    f"- CPU temperature at {max(counts)} cameras: peak {fmt(max(temps) if temps else None, ' °C')} "
                    f"(limit {LIMITS['temp']:g} °C): **{'PASS' if ok_temp else 'FAIL' if temps else 'not measured'}**"]
        low = [s for _, n, s in results if n == min(counts)]
        if low and max(counts) > min(counts):
            per_cam = (sum(s["cpu"][0] for s in top) / len(top) - sum(s["cpu"][0] for s in low) / len(low)) / (
                max(counts) - min(counts))
            avg_top = sum(s["cpu"][0] for s in top) / len(top)
            if per_cam > 0:
                fit = max(counts) + (LIMITS["cpu_avg"] - avg_top) / per_cam
                verdict.append(f"- Each camera adds about {per_cam:.1f} % CPU (from {min(counts)} to {max(counts)} cameras), "
                               f"so about {fit:.0f} cameras would reach the {LIMITS['cpu_avg']:g} % average limit with "
                               "this load (the cloud uploader and health service are not included).")
    md += ["", "## Recording during the sampled minutes (both streams, every camera of the run)", "",
           f"Each run's sampled minutes, without the last {UNWRITTEN // 60} (still being written). A camera passes with "
           f"{MIN_RECORDED:g} % or more of the window recorded on both streams and no gap over {MAX_GAP:g} s.", "",
           "| Round | Cameras | Window (UTC) | Cameras OK | Lowest main stream | Lowest substream | Longest gap | Problems |",
           "|---|---|---|---|---|---|---|---|"]
    for rnd, n, s0, s1, rows in recordings:
        good = [r for r in rows if "error" not in r]
        low_main = min(good, key=lambda r: r["main_pct"], default=None)
        low_sub = min(good, key=lambda r: r["sub_pct"], default=None)
        gap = max(good, key=lambda r: max(r["main_gap"], r["sub_gap"]), default=None)
        problems = [r["camera"] + (f" ({r['error']})" if "error" in r else "") for r in rows if not recording_ok(r)]
        md.append(f"| {rnd} | {n} | {vivo.utc(s0)[11:19]} to {vivo.utc(s1)[11:19]} | "
                  f"{sum(recording_ok(r) for r in rows)} of {len(rows)} | "
                  + (f"{low_main['main_pct']:.2f} % ({low_main['camera']})" if low_main else "n/a") + " | "
                  + (f"{low_sub['sub_pct']:.2f} % ({low_sub['camera']})" if low_sub else "n/a") + " | "
                  + (f"{max(gap['main_gap'], gap['sub_gap']):.1f} s ({gap['camera']})" if gap else "n/a") + " | "
                  + (", ".join(problems) or "none") + " |")
    if not recordings:
        md.append(f"| all | | not checked: sampling shorter than {(UNWRITTEN + MIN_CHECKED) // 60} min | | | | | |")
    top_rec = [r for _, n, _, _, rows in recordings if n == max(counts) for r in rows]
    if top_rec:
        all_ok = all(recording_ok(r) for r in top_rec)
        verdict.append(f"- Recording at {max(counts)} cameras: every camera recorded both streams, {MIN_RECORDED:g} % or "
                       f"more of each sampled window, no gap over {MAX_GAP:g} s: **{'PASS' if all_ok else 'FAIL'}**")
    else:
        verdict.append(f"- Recording at {max(counts)} cameras: not checked")
    md += ["", "## Against the limits", ""] + (verdict or ["- no run at the highest camera count"])
    md += ["", "Raw samples: the .csv file with the same name."]
    path = vivo.write_report("m6-benchmark", "\n".join(md) + "\n")
    with open(path.with_suffix(".csv"), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["run", "utc", "cpu", "ram", "write", "net", "temp"])
        w.writeheader()
        w.writerows(csv_rows)
    print("report:", path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
