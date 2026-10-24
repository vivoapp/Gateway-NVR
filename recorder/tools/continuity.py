#!/usr/bin/env python3
"""Recording continuity per camera, from Frigate's segment list.

Lists every gap between consecutive segments. More exact than
/api/recordings/unavailable, which rounds to its scale bucket.

usage: tools/continuity.py [--hours 24 | --after EPOCH --before EPOCH] [--min-gap 1.5] [--camera cam01 ...]
"""
import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import vivo  # noqa: E402


def segments(cam: str, after: float, before: float) -> list:
    segs = vivo.frigate(f"/{cam}/recordings?after={int(after)}&before={int(before)}", timeout=30)
    return sorted(segs if isinstance(segs, list) else [], key=lambda s: s["start_time"])


def gaps(cam: str, after: float, before: float, min_gap: float = 1.5) -> dict:
    segs = segments(cam, after, before)
    out = {"camera": cam, "segments": len(segs), "gaps": [], "covered": 0.0, "span": 0.0}
    if not segs:
        return out
    # Union of segment intervals: neighbouring segments overlap slightly, so a plain sum overcounts.
    covered, cur_s, cur_e = 0.0, segs[0]["start_time"], segs[0]["end_time"]
    for s in segs[1:]:
        if s["start_time"] <= cur_e:
            cur_e = max(cur_e, s["end_time"])
        else:
            covered += cur_e - cur_s
            cur_s, cur_e = s["start_time"], s["end_time"]
    out["covered"] = covered + (cur_e - cur_s)
    out["span"] = segs[-1]["end_time"] - segs[0]["start_time"]
    out["first"], out["last"] = segs[0]["start_time"], segs[-1]["end_time"]
    out["gaps"] = [(a["end_time"], b["start_time"]) for a, b in zip(segs, segs[1:])
                   if b["start_time"] - a["end_time"] > min_gap]
    return out


def markdown(results: list) -> str:
    lines = ["| Camera | Segments | Recorded | Gaps | Longest gap |", "|---|---|---|---|---|"]
    for r in results:
        pct = f"{100 * r['covered'] / r['span']:.2f}%" if r["span"] else "n/a"
        longest = max((b - a for a, b in r["gaps"]), default=0)
        lines.append(f"| {r['camera']} | {r['segments']} | {pct} | {len(r['gaps'])} | {longest:.1f} s |")
    for r in results:
        if r["gaps"]:
            lines += ["", f"Gaps on {r['camera']}:", "", "| From | To | Length |", "|---|---|---|"]
            lines += [f"| {vivo.utc(a)} | {vivo.utc(b)} | {b - a:.1f} s |" for a, b in r["gaps"]]
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--hours", type=float, default=24)
    ap.add_argument("--after", type=float)
    ap.add_argument("--before", type=float)
    ap.add_argument("--min-gap", type=float, default=1.5)
    ap.add_argument("--camera", action="append")
    args = ap.parse_args()
    before = args.before or time.time() - 30
    after = args.after or before - args.hours * 3600
    cams = args.camera or [c["id"] for c in vivo.cameras()]
    print(markdown([gaps(c, after, before, args.min_gap) for c in cams]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
