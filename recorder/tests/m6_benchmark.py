#!/usr/bin/env python3
"""M6: multi-camera performance baseline at 4, 8 and 10 cameras.

usage:
  sudo tests/m6_benchmark.py --dry-run            # print the plan, change nothing
  sudo tests/m6_benchmark.py                      # 4, 8, 10 cameras, 3 rounds (about 2 hours)
  sudo tests/m6_benchmark.py --counts 10 --rounds 1 --minutes 5

For each camera count the stack is started with only the first N cameras of cameras.yaml: each
records its main stream continuously, and its substream feeds Frigate and the substream archiver,
as in normal running. After a warm-up the gateway is sampled every 5 s: CPU busy (all cores), RAM
used, write rate to the recording disk, network input on the camera LAN, CPU package temperature;
average and peak, plus each container's share. The rounds repeat the whole sequence, so the spread
between rounds shows how repeatable the numbers are. At the end, and if anything fails, the normal
stack with every camera is started again.

Cameras left out of a run are not recorded during it: post the planned window to the group first.
The windows are added to reports/planned-windows.txt.

Limits agreed before start (docs/acceptance-limits-proposal.md), checked on the 10-camera runs:
CPU average under 60 %, peak under 85 %, CPU temperature under 80 °C.

Lab: --project, --compose-file and --sampler-container run the same steps on tests/lab/lab.sh
(see tests/lab/README.md); there the numbers are the workstation's, not the gateway's.
"""
import argparse
import csv
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import vivo  # noqa: E402

LIMITS = {"cpu_avg": 60.0, "cpu_peak": 85.0, "temp": 80.0}
STEP = 5


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
        inv["cameras"] = inv["cameras"][:n]
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
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--project", default=str(vivo.ROOT), help="folder of the running stack (default: this repository)")
    ap.add_argument("--compose-file", action="append", default=[], help="extra compose file, relative to --project (lab)")
    ap.add_argument("--sampler-container", help="read /proc inside this container (lab on a workstation)")
    args = ap.parse_args()

    counts = [int(x) for x in args.counts.split(",")]
    inventory = vivo.INVENTORY
    total_cams = len(vivo.cameras(inventory))
    counts = [n for n in counts if n <= total_cams]
    per_run = args.warmup + args.minutes * 60
    plan = (f"{args.rounds} round(s) of {counts} cameras, {args.warmup} s warm-up + {args.minutes:g} min sampling each: "
            f"about {len(counts) * args.rounds * per_run / 60:.0f} minutes. Cameras beyond N are not recorded during a run.")
    print(plan)
    if args.dry_run:
        return 0

    reader = Reader(args.sampler_container)
    media = Path(vivo.SITE.get("MEDIA_ROOT", "/srv/vivo"))
    device = None if args.sampler_container else media_device(media)
    iface = None if args.sampler_container else lan_iface(vivo.SITE.get("GATEWAY_LAN_IP", ""))
    stack = Stack(Path(args.project), args.compose_file, vivo.ETC)
    csv_rows: list[dict] = []
    results: list[tuple[int, int, dict]] = []
    windows: list[str] = []
    work = Path(tempfile.mkdtemp(prefix="vivo-m6-"))
    try:
        for rnd in range(1, args.rounds + 1):
            for n in counts:
                w0 = time.time()
                print(f"round {rnd}: starting {n} cameras")
                stack.start_subset(n, inventory, work)
                time.sleep(args.warmup)
                print(f"round {rnd}: sampling {n} cameras for {args.minutes:g} min")
                results.append((rnd, n, sample(reader, int(args.minutes * 60), device, iface, csv_rows, f"r{rnd}-{n}")))
                windows.append(f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(w0))}/"
                               f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} M6 benchmark, {n} of {total_cams} cameras")
    finally:
        print("starting the normal stack again")
        stack.restore()
        shutil.rmtree(work, ignore_errors=True)
        if windows:
            vivo.REPORTS.mkdir(exist_ok=True)
            with open(vivo.REPORTS / "planned-windows.txt", "a") as f:
                f.write("\n".join(windows) + "\n")

    md = ["# M6 multi-camera performance baseline", "",
          f"{time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime())}. {plan}",
          f"Sampled every {STEP} s. Recording disk device: {device or 'all disks'}; camera network: {iface or 'all interfaces'}."
          + (f" Read inside container `{args.sampler_container}` (lab, not the gateway)." if args.sampler_container else ""),
          "", "| Round | Cameras | CPU avg | CPU peak | RAM avg | RAM peak | Disk write avg | Network in avg | Temp avg | Temp peak |",
          "|---|---|---|---|---|---|---|---|---|---|"]
    for rnd, n, s in results:
        md.append(f"| {rnd} | {n} | {fmt(s['cpu'][0], ' %')} | {fmt(s['cpu'][1], ' %')} | {fmt(s['ram'][0], ' MB', 0)} | "
                  f"{fmt(s['ram'][1], ' MB', 0)} | {fmt(s['write'][0], ' MB/s', 2)} | {fmt(s['net'][0], ' Mbit/s')} | "
                  f"{fmt(s['temp'][0], ' °C')} | {fmt(s['temp'][1], ' °C')} |")
    md += ["", "## Spread between rounds (repeatability)", "", "| Cameras | CPU avg, lowest to highest | RAM avg | Disk write avg |",
           "|---|---|---|---|"]
    for n in counts:
        rs = [s for _, c, s in results if c == n]
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
