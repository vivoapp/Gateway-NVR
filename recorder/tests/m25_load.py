#!/usr/bin/env python3
"""M25 acceptance: load and resource run with everything in scope switched on.

usage:
  sudo tests/m25_load.py --dry-run                 # print the plan and the limits, change nothing
  sudo tests/m25_load.py                           # 1 h at 4 and at 8 cameras, then 24 h at all of them
  sudo tests/m25_load.py --counts 10 --hours 24    # only the acceptance run (the stack is not restarted)
  sudo systemd-run --unit vivo-m25 --collect -p TimeoutStopSec=10min /opt/vivo-gateway/tests/m25_load.py
                                                   # keeps running when the SSH session ends;
                                                   # follow it with: journalctl -fu vivo-m25

Everything in scope runs as in production: Frigate records every camera's main stream, the
substream archiver saves its substream, the storage guard applies retention and the safe limit,
and the health endpoint (vivo-health) runs. The run at the highest camera count is the acceptance
run; the shorter runs at fewer cameras are for comparison. For a run at fewer cameras the stack is
started with only the first N cameras of cameras.yaml, as in M6: the others are not recorded during
it, so post the planned window first (it is added to reports/planned-windows.txt). The normal stack
with every camera is running again before the acceptance run starts.

Each run checks that everything is switched on, then samples the gateway every 5 s (CPU, RAM,
temperature), the health endpoint every 30 s (alerts, recording disk use), the OS disk every minute
and each container's CPU and memory every 2 minutes; the samples go to a CSV as they are taken. At
the end it measures what was recorded (the gap log for both streams, and the bytes per hour) and
checks the limits agreed before start (docs/acceptance-limits-proposal.md):

  CPU          average under 60 %; highest 1-minute average under 85 %
  RAM          highest under 70 % of the total; for runs of 6 h or more, no steady growth (the last
               quarter's average within 10 % of the second quarter's)
  temperature  CPU package under 80 °C
  disk         recording disk below its safe limit all the time; at this run's recording rate,
               record_days of main stream plus substream_days of substream fit under the safe
               limit; OS disk under 80 %
  gaps         no unexplained gap in either stream of any camera (target zero); a gap while the
               health endpoint showed that camera offline is the camera's, and is listed as such
  services     no container restarted; the health endpoint answered 99 % of the polls

Lab: --project, --compose-file and --sampler-container as in tests/m6_benchmark.py; --health-url ""
skips the health endpoint (there is none in the lab). The lab's numbers are the workstation's.
"""
import argparse
import csv
import json
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import m6_benchmark as m6  # noqa: E402
import storage_guard as sg  # noqa: E402
import testlib  # noqa: E402
import vivo  # noqa: E402
from substream_archiver import segment_name  # noqa: E402

LIMITS = {
    "cpu_avg": 60.0,          # percent of all cores, whole run
    "cpu_peak_1min": 85.0,    # highest 1-minute average
    "ram_peak_percent": 70.0,
    "ram_growth": 0.10,       # last quarter's average over the second quarter's
    "temp": 80.0,
    "os_disk_percent": 80.0,
    "health_answered": 0.99,
}
GROWTH_MIN_HOURS = 6          # shorter runs are dominated by warm-up and caches
STEP = 5
HEALTH_EVERY = 30
DISK_EVERY = 60
CONTAINERS_EVERY = 120
OFFLINE_PAD = 60              # a gap this close to a camera_offline alert is the camera's
READY_WAIT = 300              # after a restart, wait this long at most for every camera to record
END_MARGIN = 70               # the last seconds of a run are not gap-checked: a restart right after
                              # it drops the segment being written (60 s substream + slack)
FIELDS = ["run", "utc", "kind", "name", "cpu", "ram_mb", "temp", "value"]
CONTAINERS = ("go2rtc", "frigate", "substream", "storage")


# ------------------------------------------------------------------ pure helpers (unit tested)

def rolling_peak(values: list[float], window: int) -> float | None:
    """Highest average of `window` consecutive values (all of them if there are fewer)."""
    if not values:
        return None
    if len(values) <= window:
        return sum(values) / len(values)
    s = sum(values[:window])
    best = s
    for i in range(window, len(values)):
        s += values[i] - values[i - window]
        best = max(best, s)
    return best / window


def growth(values: list[float]) -> float | None:
    """Last quarter's average over the second quarter's, minus 1 (None with too few samples)."""
    q = len(values) // 4
    if q < 2:
        return None
    second = sum(values[q:2 * q]) / q
    last = sum(values[-q:]) / q
    return last / second - 1 if second else None


def alert_intervals(polls: list[tuple[float, list[str]]], pad: float = HEALTH_EVERY) -> dict[str, list[tuple[float, float]]]:
    """From (time, active alert keys) polls: each key's active periods, widened by one poll interval."""
    out: dict[str, list[tuple[float, float]]] = {}
    open_: dict[str, list[float]] = {}
    for t, keys in polls:
        for k in keys:
            if k in open_:
                open_[k][1] = t
            else:
                open_[k] = [t, t]
        for k in [k for k in open_ if k not in keys]:
            a, b = open_.pop(k)
            out.setdefault(k, []).append((a - pad, b + pad))
    for k, (a, b) in open_.items():
        out.setdefault(k, []).append((a - pad, b + pad))
    return out


def run_alerts(keys: list[str], cams: list[str], all_cams: list[str]) -> list[str]:
    """Alerts that belong to this run: not the ones for cameras left out of it."""
    left_out = set(all_cams) - set(cams)
    return [k for k in keys if k.split(":", 1)[1].split("/")[0] not in left_out]


def classify_gaps(gaps: list[dict], offline: dict[str, list[tuple[float, float]]],
                  planned: list[tuple[float, float]]) -> list[dict]:
    for g in gaps:
        if any(p0 - 60 <= g["start"] and g["end"] <= p1 + 60 for p0, p1 in planned):
            g["cause"] = "planned window"
        elif any(o0 - OFFLINE_PAD <= g["end"] and g["start"] <= o1 + OFFLINE_PAD
                 for o0, o1 in offline.get(f"camera_offline:{g['camera']}", [])):
            g["cause"] = "camera stopped sending video (camera_offline alert)"
        else:
            g["cause"] = "UNEXPLAINED"
    return gaps


def projection(main_bytes: int, sub_bytes: int, seconds: float, policy: dict, disk_bytes: int) -> dict:
    """Footage at this run's rate after record_days of main stream and substream_days of substream."""
    per_day_main = main_bytes / seconds * 86400 if seconds > 0 else 0.0
    per_day_sub = sub_bytes / seconds * 86400 if seconds > 0 else 0.0
    footprint = per_day_main * policy["record_days"] + per_day_sub * policy["substream_days"]
    allowed = disk_bytes * policy["disk_limit_percent"] / 100
    days_fit = (allowed - per_day_sub * policy["substream_days"]) / per_day_main if per_day_main else None
    return {"main_gb_day": per_day_main / 1e9, "sub_gb_day": per_day_sub / 1e9, "footprint_gb": footprint / 1e9,
            "allowed_gb": allowed / 1e9, "fits": footprint <= allowed, "main_days_fit": days_fit}


def evaluate(run: dict, limits: dict = LIMITS) -> list[tuple[bool, str, str]]:
    """The run's checks against the limits, as (passed, check, detail)."""
    rows = list(run["preconditions"])
    ran = run["end"] - run["start"]
    rows.append((ran >= run["planned_s"] - 60, f"ran for the planned {run['planned_s'] / 3600:g} h",
                 f"{ran / 3600:.2f} h" + (" (stopped early)" if run.get("interrupted") else "")))
    cpu, ram, temps = run["cpu"], run["ram_mb"], run["temp"]
    avg = sum(cpu) / len(cpu) if cpu else None
    peak = rolling_peak(cpu, 60 // STEP)
    rows.append((avg is not None and avg < limits["cpu_avg"], f"CPU average under {limits['cpu_avg']:g} %",
                 m6.fmt(avg, " %")))
    rows.append((peak is not None and peak < limits["cpu_peak_1min"],
                 f"CPU highest 1-minute average under {limits['cpu_peak_1min']:g} %", m6.fmt(peak, " %")))
    total = run["ram_total_mb"]
    ram_peak = max(ram) if ram else None
    rows.append((ram_peak is not None and total > 0 and 100 * ram_peak / total < limits["ram_peak_percent"],
                 f"RAM highest under {limits['ram_peak_percent']:g} % of {total / 1024:.1f} GB",
                 f"{ram_peak / 1024:.2f} GB ({100 * ram_peak / total:.0f} %)" if ram_peak and total else "n/a"))
    if run["hours"] >= GROWTH_MIN_HOURS:
        g = growth(ram)
        rows.append((g is not None and g <= limits["ram_growth"],
                     f"RAM: no steady growth (last quarter within {100 * limits['ram_growth']:g} % of the second)",
                     f"{100 * g:+.1f} %" if g is not None else "too few samples"))
    t_peak = max(temps) if temps else None
    rows.append((t_peak is not None and t_peak < limits["temp"], f"CPU temperature under {limits['temp']:g} °C",
                 f"highest {m6.fmt(t_peak, ' °C')}" if temps else "not measured"))
    disk = run["disk_percent"]
    d_peak = max(disk) if disk else None
    rows.append((d_peak is not None and d_peak < run["policy"]["disk_limit_percent"],
                 f"recording disk below its {run['policy']['disk_limit_percent']} % safe limit all the time",
                 f"highest {m6.fmt(d_peak, ' %')}"))
    p = run["projection"]
    rows.append((p["fits"], f"{run['policy']['record_days']} days of main stream and {run['policy']['substream_days']} of "
                 "substream fit under the safe limit at this run's rate",
                 f"{p['main_gb_day']:.1f} GB/day main + {p['sub_gb_day']:.2f} GB/day substream: {p['footprint_gb']:.0f} GB "
                 f"of {p['allowed_gb']:.0f} GB" + (f"; about {p['main_days_fit']:.1f} days of main stream fit"
                                                    if p["main_days_fit"] else "")))
    os_peak = max(run["os_disk_percent"]) if run["os_disk_percent"] else None
    rows.append((os_peak is not None and os_peak < limits["os_disk_percent"], f"OS disk under {limits['os_disk_percent']:g} %",
                 f"highest {m6.fmt(os_peak, ' %')}"))
    restarted = run["restarted"]
    rows.append((not restarted, "no container restarted during the run", ", ".join(restarted)))
    if run["health_polls"]:
        share = run["health_answered"] / run["health_polls"]
        rows.append((share >= limits["health_answered"], f"health endpoint answered {100 * limits['health_answered']:g} % of polls",
                     f"{run['health_answered']} of {run['health_polls']}"))
    unexplained = [g for g in run["gaps"] if g["cause"] == "UNEXPLAINED"]
    camera_side = [g for g in run["gaps"] if g["cause"].startswith("camera stopped")]
    rows.append((not unexplained, "no unexplained recording gap, main stream and substream, every camera",
                 f"{len(run['gaps'])} gaps: {len(unexplained)} unexplained, {len(camera_side)} while a camera was offline"))
    return rows


# ------------------------------------------------------------------ on the gateway

def containers() -> dict[str, tuple[str, int, str]]:
    """name -> (state, restart count, started at) for the recorder's containers."""
    out = subprocess.run(["docker", "inspect", "-f", "{{.Name}}|{{.State.Status}}|{{.RestartCount}}|{{.State.StartedAt}}",
                          *CONTAINERS], capture_output=True, text=True).stdout
    res = {}
    for line in out.splitlines():
        p = line.split("|")
        if len(p) == 4:
            res[p[0].lstrip("/")] = (p[1], int(p[2]), p[3])
    return res


def container_stats() -> dict[str, tuple[float, float]]:
    """name -> (CPU percent of one core, memory MB) from docker stats."""
    out = subprocess.run(["docker", "stats", "--no-stream", "--format", "{{json .}}"],
                         capture_output=True, text=True, timeout=60).stdout
    res = {}
    for line in out.splitlines():
        try:
            d = json.loads(line)
            res[d["Name"]] = (float(d["CPUPerc"].rstrip("%")), mem_mb(d["MemUsage"].split("/")[0]))
        except (ValueError, KeyError):
            pass
    return res


def mem_mb(text: str) -> float:
    units = {"B": 1 / 2**20, "KiB": 1 / 1024, "MiB": 1, "GiB": 1024, "TiB": 2**20, "kB": 1e3 / 2**20, "MB": 1e6 / 2**20,
             "GB": 1e9 / 2**20}
    text = text.strip()
    for u in sorted(units, key=len, reverse=True):
        if text.endswith(u):
            return float(text[:-len(u)]) * units[u]
    return float(text)


def health(url: str) -> dict | None:
    try:
        with urllib.request.urlopen(url, timeout=5) as f:
            return json.load(f)
    except Exception:
        return None


def preconditions(cams: list[str], health_url: str, t_from: float) -> list[tuple[bool, str, str]]:
    rows = []
    states = containers()
    down = [n for n in CONTAINERS if states.get(n, ("missing",))[0] != "running"]
    rows.append((not down, "go2rtc, Frigate, substream archiver and storage guard running", ", ".join(down)))
    rec = testlib.recording_after(t_from, cams)
    missing = [f"{c} {s}" for c, v in rec.items() for s in ("main", "substream") if not v[s]]
    rows.append((not missing, f"every camera records both streams ({len(cams)} cameras)", ", ".join(missing)))
    if health_url:
        snap = health(health_url)
        on = (snap or {}).get("recorder", {}).get("recording_on", {})
        off = [c for c in cams if not on.get(c)]
        rows.append((snap is not None, "health endpoint answers", health_url))
        rows.append((snap is not None and not off, "Frigate has recording switched on for every camera", ", ".join(off)))
    policy = vivo.services_config()
    rows.append((True, "retention and safe limit in force (storage guard)",
                 f"main {policy['record_days']} days, substream {policy['substream_days']} days, "
                 f"safe limit {policy['disk_limit_percent']} %"))
    return rows


def recorded_bytes(cams: list[str], a: float, b: float, media: Path) -> tuple[int, int]:
    """Bytes of main-stream and substream segments recorded entirely inside [a, b]."""
    main_root = media / "frigate" / "media" / "recordings"
    sub_root = testlib.substream_root()
    main = sub = 0
    for cam in cams:
        for s, e in testlib.main_segments(cam, a, b):
            if s >= a and e <= b:
                main += sg.size_of([main_root / time.strftime(f"%Y-%m-%d/%H/{cam}/%M.%S.mp4", time.gmtime(s))])
        for s, e in testlib.substream_segments(cam, a, b):
            if s >= a and e <= b:
                sub += sg.size_of([sub_root / cam / time.strftime("%Y-%m-%d", time.gmtime(s)) / segment_name(cam, s, e)])
    return main, sub


def measure(label: str, n: int, cams: list[str], all_cams: list[str], seconds: float, reader: m6.Reader,
            health_url: str, media: Path, writer: csv.DictWriter, out) -> dict:
    run = {"label": label, "cameras": n, "hours": seconds / 3600, "planned_s": seconds, "cpu": [], "ram_mb": [],
           "temp": [], "disk_percent": [], "os_disk_percent": [], "containers": {}, "health_polls": 0,
           "health_answered": 0, "polls": [], "policy": vivo.services_config()}
    ready = testlib.wait_until(lambda: all(v["main"] and v["substream"] for v in
                                           testlib.recording_after(time.time() - 180, cams).values()), READY_WAIT, 10)
    if ready is None:
        print(f"{label}: not every camera records both streams after {READY_WAIT} s; measuring anyway", flush=True)
    run["preconditions"] = preconditions(cams, health_url, time.time() - 180)
    for ok, name, detail in run["preconditions"]:
        print(f"{'PASS' if ok else 'FAIL'}  {name}  {detail}", flush=True)
    before = containers()
    meminfo = reader.read("/proc/meminfo")[0]
    run["ram_total_mb"] = next((int(l.split()[1]) / 1024 for l in meminfo.splitlines() if l.startswith("MemTotal:")), 0)

    def row(kind: str, name: str = "", **kw) -> None:
        writer.writerow({"run": label, "utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "kind": kind, "name": name, **kw})

    i0, t0 = m6.cpu_ticks(reader.read("/proc/stat")[0])
    start = time.time()
    end = start + seconds
    next_health = next_disk = next_ctr = last_print = start
    try:
        while time.time() < end:
            time.sleep(STEP)
            now = time.time()
            stat, mem = reader.read("/proc/stat", "/proc/meminfo")
            i1, t1 = m6.cpu_ticks(stat)
            cpu = 100 * (1 - (i1 - i0) / max(t1 - t0, 1))
            i0, t0 = i1, t1
            ram = m6.mem_used_mb(mem)
            temp = m6.temperature(reader)
            run["cpu"].append(cpu)
            run["ram_mb"].append(ram)
            if temp is not None:
                run["temp"].append(temp)
            row("gateway", cpu=f"{cpu:.1f}", ram_mb=f"{ram:.0f}", temp="" if temp is None else f"{temp:.1f}")
            if health_url and now >= next_health:
                next_health = now + HEALTH_EVERY
                run["health_polls"] += 1
                snap = health(health_url)
                keys = []
                if snap is not None:
                    run["health_answered"] += 1
                    keys = run_alerts([f"{a['alert']}:{a['target']}" for a in snap.get("alerts", [])], cams, all_cams)
                    pct = snap.get("disk", {}).get("percent_used")
                    if pct is not None and "test_overrides" not in snap:
                        run["disk_percent"].append(pct)
                run["polls"].append((now, keys))
                row("health", "answered" if snap is not None else "no answer", value=" ".join(keys))
            if now >= next_disk:
                next_disk = now + DISK_EVERY
                if not health_url:
                    run["disk_percent"].append(sg.recording_disk_usage(media)[2])
                root = shutil.disk_usage("/")
                run["os_disk_percent"].append(100 * root.used / root.total)
                row("disk", "recording", value=f"{run['disk_percent'][-1]:.2f}" if run["disk_percent"] else "")
            if now >= next_ctr:
                next_ctr = now + CONTAINERS_EVERY
                for name, (c, m) in container_stats().items():
                    run["containers"].setdefault(name, []).append((c, m))
                    row("container", name, cpu=f"{c:.1f}", ram_mb=f"{m:.0f}")
            if now - last_print >= 600:
                last_print = now
                out.flush()
                print(f"{label}: {(now - start) / 3600:.1f} of {seconds / 3600:g} h, CPU now {cpu:.0f} %, "
                      f"RAM {ram / 1024:.1f} GB", flush=True)
    except KeyboardInterrupt:      # Ctrl-C, or `systemctl stop vivo-m25`: report what was measured so far
        run["interrupted"] = True
        print(f"{label}: stopped early; reporting what was measured", flush=True)
    out.flush()
    run["start"], run["end"] = start, time.time()
    after = containers()
    run["restarted"] = [f"{n} (restart count {before[n][1]} -> {count}, started {started[:19]})"
                        for n, (_, count, started) in after.items()
                        if n in before and (count, started) != before[n][1:]]
    run["restarted"] += [f"{n} (gone)" for n in before if n not in after]
    return run


def finish(run: dict, cams: list[str], media: Path, planned: list[tuple[float, float]]) -> dict:
    """Gap log and recorded bytes, once the last segments of the window have landed."""
    a, b = run["start"], run["end"]
    run["gaps"] = classify_gaps(testlib.all_gaps(a, b - END_MARGIN, cams), alert_intervals(run["polls"]), planned)
    main_b, sub_b = recorded_bytes(cams, a, b, media)
    used, avail, _ = sg.recording_disk_usage(media)
    run["projection"] = projection(main_b, sub_b, b - a, run["policy"], used + avail)
    run["checks"] = evaluate(run)
    return run


# ------------------------------------------------------------------ report

def report_md(runs: list[dict], plan: str, device_note: str) -> tuple[str, bool]:
    top = max(runs, key=lambda r: r["cameras"]) if runs else None
    passed = bool(top) and all(ok for ok, _, _ in top["checks"])
    md = ["# M25 load and resource acceptance", "", f"{time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime())}. {plan}",
          device_note, "",
          f"Result: **{'PASS' if passed else 'FAIL'}** (the run at {top['cameras'] if top else '?'} cameras is the acceptance run; "
          "the others are for comparison)", "",
          "| Run | Cameras | Hours | From (UTC) | CPU avg | CPU 1-min peak | RAM avg | RAM peak | Temp peak | Recording disk peak | "
          "Main GB/day | Substream GB/day | Gaps (unexplained) | Result |",
          "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in runs:
        cpu, ram = r["cpu"], r["ram_mb"]
        ok = all(c for c, _, _ in r["checks"])
        md.append(f"| {r['label']} | {r['cameras']} | {(r['end'] - r['start']) / 3600:.1f} | {vivo.utc(r['start'])[:16]} | "
                  f"{m6.fmt(sum(cpu) / len(cpu) if cpu else None, ' %')} | {m6.fmt(rolling_peak(cpu, 60 // STEP), ' %')} | "
                  f"{m6.fmt(sum(ram) / len(ram) / 1024 if ram else None, ' GB', 2)} | {m6.fmt(max(ram) / 1024 if ram else None, ' GB', 2)} | "
                  f"{m6.fmt(max(r['temp']) if r['temp'] else None, ' °C')} | {m6.fmt(max(r['disk_percent']) if r['disk_percent'] else None, ' %')} | "
                  f"{r['projection']['main_gb_day']:.1f} | {r['projection']['sub_gb_day']:.2f} | "
                  f"{len(r['gaps'])} ({sum(g['cause'] == 'UNEXPLAINED' for g in r['gaps'])}) | {'PASS' if ok else '**FAIL**'} |")
    for r in runs:
        rep = testlib.Report()
        rep.rows = [(ok, name, detail) for ok, name, detail in r["checks"]]
        md += ["", f"## {r['label']}: {r['cameras']} cameras, {vivo.utc(r['start'])} to {vivo.utc(r['end'])[11:]}", ""]
        md += rep.markdown()
        names = sorted(r["containers"])
        if names:
            md += ["", "Containers (average and highest of the 2-minute samples):", "",
                   "| Container | CPU avg (% of one core) | CPU peak | Memory avg | Memory peak | Memory first to last |",
                   "|---|---|---|---|---|---|"]
            for n in names:
                v = r["containers"][n]
                c, m = [x for x, _ in v], [y for _, y in v]
                md.append(f"| {n} | {sum(c) / len(c):.1f} | {max(c):.1f} | {sum(m) / len(m):.0f} MB | {max(m):.0f} MB | "
                          f"{m[0]:.0f} to {m[-1]:.0f} MB |")
        seen = alert_intervals(r["polls"], pad=0)
        md += ["", "Health alerts seen during the run:", ""]
        md += (["| Alert | From (UTC) | To |", "|---|---|---|"] +
               [f"| {k} | {vivo.utc(a)} | {vivo.utc(b)[11:]} |" for k, iv in sorted(seen.items()) for a, b in iv]
               if seen else ["None."])
        md += ["", "Gap log:", ""] + testlib.gap_table(r["gaps"])
    md += ["", "Raw samples: the .csv file with the same name. Limits: docs/acceptance-limits-proposal.md (M25)."]
    return "\n".join(md) + "\n", passed


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--counts", default="4,8,10", help="camera counts; the highest is the acceptance run")
    ap.add_argument("--hours", type=float, default=24, help="length of the acceptance run")
    ap.add_argument("--compare-hours", type=float, default=1, help="length of each run at fewer cameras")
    ap.add_argument("--warmup", type=int, default=180, help="seconds after a stack restart before sampling")
    ap.add_argument("--planned", action="append", default=[], help="UTC window of another planned test, ISO a/b")
    ap.add_argument("--health-url", default="http://127.0.0.1:9101/health", help='"" to skip (lab)')
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--project", default=str(vivo.ROOT), help="folder of the running stack (default: this repository)")
    ap.add_argument("--compose-file", action="append", default=[], help="extra compose file, relative to --project (lab)")
    ap.add_argument("--sampler-container", help="read /proc inside this container (lab on a workstation)")
    args = ap.parse_args()

    all_cams = vivo.services_config()["cameras"]
    counts = sorted({min(int(x), len(all_cams)) for x in args.counts.split(",")})
    top = max(counts)
    lengths = {n: (args.hours if n == top else args.compare_hours) for n in counts}
    restarts = [n for n in counts if n < len(all_cams)]
    total_h = sum(lengths.values()) + (len(restarts) + (1 if restarts and top == len(all_cams) else 0)) * args.warmup / 3600
    plan = (", ".join(f"{lengths[n]:g} h at {n} cameras" for n in counts) + f": about {total_h:.1f} hours. "
            + (f"Cameras beyond N are not recorded during the runs at {', '.join(map(str, restarts))} cameras."
               if restarts else "The running stack is measured as it is; nothing is restarted."))
    print(plan)
    print("limits: " + ", ".join(f"{k} {v:g}" for k, v in LIMITS.items()))
    if args.dry_run:
        return 0

    def stop(*_):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, stop)   # systemctl stop: end the run and report, like Ctrl-C

    reader = m6.Reader(args.sampler_container)
    media = Path(vivo.SITE.get("MEDIA_ROOT", "/srv/vivo"))
    stack = m6.Stack(Path(args.project), args.compose_file, vivo.ETC)
    planned = [testlib.parse_window(w) for w in args.planned]
    vivo.REPORTS.mkdir(exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    csv_path = vivo.REPORTS / f"m25-load-{stamp}.csv"
    runs, windows = [], []
    work = Path(tempfile.mkdtemp(prefix="vivo-m25-"))
    changed = False
    try:
        with csv_path.open("w", newline="") as out:
            writer = csv.DictWriter(out, fieldnames=FIELDS)
            writer.writeheader()
            for n in counts:
                w0 = time.time()
                if n < len(all_cams):
                    print(f"starting {n} cameras", flush=True)
                    stack.start_subset(n, vivo.INVENTORY, work)
                    changed = True
                    time.sleep(args.warmup)
                elif changed:
                    print("starting the normal stack with every camera", flush=True)
                    stack.restore()
                    changed = False
                    time.sleep(args.warmup)
                cams = all_cams[:n]
                label = f"{n} cameras"
                print(f"{label}: measuring for {lengths[n]:g} h", flush=True)
                run = measure(label, n, cams, all_cams, lengths[n] * 3600, reader, args.health_url, media, writer, out)
                if n < len(all_cams):
                    windows.append(f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(w0))}/"
                                   f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} M25 load run, {n} of {len(all_cams)} cameras")
                runs.append(run)
                if run.get("interrupted"):
                    break
    finally:
        if changed:
            print("starting the normal stack again", flush=True)
            stack.restore()
        shutil.rmtree(work, ignore_errors=True)
        if windows:
            with open(vivo.REPORTS / "planned-windows.txt", "a") as f:
                f.write("\n".join(windows) + "\n")
    print("waiting 90 s for the last segments of the window to be written", flush=True)
    time.sleep(90)
    for run in runs:
        finish(run, all_cams[:run["cameras"]], media, planned)
    device = media.resolve()
    note = (f"Gateway sampled every {STEP} s, health endpoint every {HEALTH_EVERY} s ({args.health_url or 'not used'}), "
            f"containers every {CONTAINERS_EVERY} s. Recording disk {device}."
            + (f" Read inside container `{args.sampler_container}` (lab, not the gateway)." if args.sampler_container else ""))
    text, passed = report_md(runs, plan, note)
    path = vivo.write_report("m25-load", text)
    csv_path.rename(path.with_suffix(".csv"))
    print(text)
    print("report:", path)
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
