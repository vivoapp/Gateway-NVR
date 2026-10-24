#!/usr/bin/env python3
"""M3 acceptance: after a 24-hour run, show that no camera had an unexplained stream drop.

For each camera it lists every recording gap (from Frigate's segment list) next to
the go2rtc and Frigate log lines for that camera in the same minute. A gap is
"explained" if it falls inside a planned test window passed with --planned.

usage: sudo tests/m3_soak_report.py [--hours 24] [--planned "2026-10-21T10:00Z/2026-10-21T10:30Z" ...]
"""
import argparse
import datetime as dt
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import continuity  # noqa: E402
import vivo  # noqa: E402


def parse_window(s: str) -> tuple[float, float]:
    a, b = s.split("/")
    f = lambda x: dt.datetime.fromisoformat(x.replace("Z", "+00:00")).timestamp()  # noqa: E731
    return f(a), f(b)


def log_lines(container: str, since: float) -> list[str]:
    out = subprocess.run(["docker", "logs", "--timestamps", "--since", str(int(since)), container],
                         capture_output=True, text=True)
    return (out.stdout + out.stderr).splitlines()


def near(lines: list[str], cam: str, t: float, window: float = 90) -> list[str]:
    hits = []
    for line in lines:
        if cam not in line:
            continue
        try:
            ts = dt.datetime.fromisoformat(line.split(" ", 1)[0][:26] + "+00:00").timestamp()
        except ValueError:
            continue
        if abs(ts - t) <= window:
            hits.append(vivo.redact(line.split(" ", 1)[1][:160]))
    return hits[:3]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--hours", type=float, default=24)
    ap.add_argument("--planned", action="append", default=[], help="UTC window of a deliberate test, ISO a/b")
    args = ap.parse_args()
    before = time.time() - 60
    after = before - args.hours * 3600
    planned = [parse_window(w) for w in args.planned]
    go2rtc_log, frigate_log = log_lines("go2rtc", after), log_lines("frigate", after)

    results = [continuity.gaps(c["id"], after, before) for c in vivo.cameras()]
    unexplained = 0
    md = ["# M3 24-hour stream run", "", f"Window: {vivo.utc(after)} to {vivo.utc(before)}.", "",
          continuity.markdown(results), "", "## Each gap with its cause", "",
          "| Camera | Gap start | Length | Planned test? | Logs around the gap |", "|---|---|---|---|---|"]
    for r in results:
        for a, b in r["gaps"]:
            is_planned = any(p0 - 60 <= a <= p1 + 60 for p0, p1 in planned)
            unexplained += not is_planned
            logs = near(go2rtc_log, r["camera"], a) + near(frigate_log, r["camera"], a)
            md.append(f"| {r['camera']} | {vivo.utc(a)} | {b - a:.1f} s | {'yes' if is_planned else '**no**'} | "
                      f"{'<br>'.join(l.replace('|', '/') for l in logs) or 'none'} |")
    md += ["", f"Unexplained gaps: **{unexplained}** (target: 0)."]
    print("report:", vivo.write_report("m3-soak", "\n".join(md) + "\n"))
    return 0 if unexplained == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
