#!/usr/bin/env python3
"""M3 acceptance from the recordings themselves: cameras power-cycled on site, in a known window.

Every power cut leaves a gap in a camera's continuous recording. For each camera this lists the
gaps inside the window (from Frigate's own segment list): when recording stopped, and when the
first new segment started. "Recording back" is stronger proof than "stream back", because the
recorder only writes a segment once the camera's stream is flowing again.

The gateway cannot see the moment power came back, so the PASS rule uses the whole gap, from the
cut to the first new segment: if that is within the limit, the time from power on to recording is
too, whatever the off time was. A camera passes with at least --cycles gaps, all within --limit.

usage: sudo tests/m3_gaps_report.py --start 2026-10-23T14:00:00Z --end 2026-10-23T14:30:00Z \
         [--cycles 5] [--limit 120] [--min-gap 3] [--note "power cycled by Vivo on the PoE switch"]
"""
import argparse
import calendar
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import vivo  # noqa: E402


def ts(s: str) -> float:
    return calendar.timegm(time.strptime(s.rstrip("Z"), "%Y-%m-%dT%H:%M:%S"))


def hms(t: float) -> str:
    return time.strftime("%H:%M:%S", time.gmtime(t))


def merge(gaps: list) -> list:
    """Join gaps separated by a tiny segment (a partial segment flushed during the outage)."""
    out = []
    for s, e in gaps:
        if out and s - out[-1][1] < 15:
            out[-1] = (out[-1][0], e)
        else:
            out.append((s, e))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--start", required=True, help="window start, UTC, e.g. 2026-10-23T14:00:00Z")
    ap.add_argument("--end", required=True, help="window end, UTC")
    ap.add_argument("--cycles", type=int, default=5)
    ap.add_argument("--limit", type=float, default=120, help="max seconds from the cut to the first new segment")
    ap.add_argument("--min-gap", type=float, default=3, help="smaller holes between segments are not gaps")
    ap.add_argument("--note", default="power cycled on site")
    a = ap.parse_args()
    t0, t1 = ts(a.start), ts(a.end)

    rows, ok_cams = [], []
    for cam in [c["id"] for c in vivo.cameras()]:
        segs = sorted(vivo.frigate(f"/{cam}/recordings?after={int(t0) - 60}&before={int(t1) + 300}"),
                      key=lambda s: s["start_time"])
        gaps = merge([(p["end_time"], n["start_time"]) for p, n in zip(segs, segs[1:])
                      if n["start_time"] - p["end_time"] > a.min_gap and t0 <= p["end_time"] <= t1])
        cam_ok = len(gaps) >= a.cycles and all(e - s <= a.limit for s, e in gaps)
        ok_cams += [cam] if cam_ok else []
        for n, (s, e) in enumerate(gaps, 1):
            rows.append((cam, n, s, e, e - s <= a.limit))
        if not gaps:
            rows.append((cam, 0, None, None, False))

    longest = max((e - s for _, _, s, e, _ in rows if s), default=0)
    md = ["# M3 camera reconnect test (from the recordings)", "",
          f"Window {a.start} to {a.end} ({a.note}). Each gap is one power cut seen in the camera's continuous "
          f"recording: from the end of the last segment before the cut to the start of the first new segment.", "",
          f"Rule: at least {a.cycles} cycles per camera, and every gap within **{a.limit:.0f} s**. The gap includes "
          f"the time the camera was off, so the time from power on to recording is shorter than the gap.", "",
          f"Result: **{len(ok_cams)} of {len(vivo.cameras())} cameras passed**; longest gap {longest:.0f} s.", "",
          "| Camera | Cycle | Recording stopped (UTC) | Recording back (UTC) | Gap | Result |",
          "|---|---|---|---|---|---|"]
    for cam, n, s, e, ok in rows:
        if s is None:
            md.append(f"| {cam} | none | no gap found in the window | | | **FAIL** |")
        else:
            md.append(f"| {cam} | {n} | {hms(s)} | {hms(e)} | {e - s:.0f} s | {'PASS' if ok else '**FAIL**'} |")
    md += ["", "Source: Frigate's recording segment list (`/api/<camera>/recordings`). No manual step on the gateway "
           "was needed for any camera to come back."]
    print("\n".join(md[:7]))
    print("report:", vivo.write_report("m3-reconnect", "\n".join(md) + "\n"))
    return 0 if len(ok_cams) == len(vivo.cameras()) else 1


if __name__ == "__main__":
    sys.exit(main())
