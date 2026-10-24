#!/usr/bin/env python3
"""M3 acceptance: each camera is unplugged and plugged back in N times; the
stream must come back on its own within the agreed limit every time.

For each cycle it records, from the moment power is restored:
  camera reachable  the camera's RTSP port answers (camera boot time)
  stream back       go2rtc receives video from the camera again
  recording back    Frigate's first new segment for that camera starts
The pass/fail limit applies to "stream back" (agreed before start; default 120 s).

usage: sudo tests/m3_reconnect.py [--cycles 5] [--off 20] [--limit 120] [--camera cam01 ...]
"""
import argparse
import csv
import socket
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import poe  # noqa: E402
import vivo  # noqa: E402


def reachable(cam: dict) -> bool:
    try:
        with socket.create_connection((cam["host"], int(cam.get("port", 554))), timeout=1.5):
            return True
    except OSError:
        return False


def wait_until(pred, timeout: float, poll: float = 1.0) -> float | None:
    t0 = time.time()
    while time.time() - t0 < timeout:
        if pred():
            return time.time()
        time.sleep(poll)
    return None


def cycle(cam: dict, off_s: float) -> dict:
    main = f"{cam['id']}_main"
    if not wait_until(lambda: vivo.stream_alive(main), 180):
        raise RuntimeError(f"{cam['id']}: stream not alive before the test")
    t_off = time.time()
    poe.set_power(cam, on=False)
    t_drop = wait_until(lambda: not reachable(cam), 30, 0.5)
    time.sleep(off_s)
    t_on = time.time()
    poe.set_power(cam, on=True)
    t_tcp = wait_until(lambda: reachable(cam), 300, 0.5)
    t_stream = wait_until(lambda: vivo.stream_alive(main, 1.0), 300, 0.5)
    t_rec = wait_until(lambda: vivo.first_segment_start_after(cam["id"], t_on) is not None, 300, 3)
    rec = vivo.first_segment_start_after(cam["id"], t_on) if t_rec else None
    rel = lambda t: round(t - t_on, 1) if t else None  # noqa: E731
    return {"camera": cam["id"], "off_at": vivo.utc(t_off), "drop_detected_s": round(t_drop - t_off, 1) if t_drop else None,
            "on_at": vivo.utc(t_on), "camera_reachable_s": rel(t_tcp), "stream_back_s": rel(t_stream),
            "recording_back_s": rel(rec)}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cycles", type=int, default=5)
    ap.add_argument("--off", type=float, default=20, help="seconds the camera stays off")
    ap.add_argument("--limit", type=float, default=120, help="max seconds from power on to stream back")
    ap.add_argument("--camera", action="append")
    args = ap.parse_args()

    cams = [c for c in vivo.cameras() if not args.camera or c["id"] in args.camera]
    results = []
    for cam in cams:
        for n in range(1, args.cycles + 1):
            print(f"{cam['id']} cycle {n}/{args.cycles}")
            r = cycle(cam, args.off)
            r["cycle"] = n
            r["pass"] = r["stream_back_s"] is not None and r["stream_back_s"] <= args.limit
            print("   ", r)
            results.append(r)

    if not results:
        print("no cycles ran: check --camera ids against cameras.yaml")
        return 1
    vivo.REPORTS.mkdir(exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    csv_path = vivo.REPORTS / f"m3-reconnect-{stamp}.csv"
    with csv_path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(results[0]))
        w.writeheader()
        w.writerows(results)

    md = ["# M3 camera reconnect test", "",
          f"{args.cycles} power cycles per camera, {args.off:.0f} s off each time, PoE backend "
          f"`{vivo.SITE.get('POE_BACKEND', 'manual')}`. Limit: stream back within **{args.limit:.0f} s** of power on.", "",
          f"Result: **{sum(r['pass'] for r in results)} of {len(results)} cycles passed**", "",
          "| Camera | Cycle | Power on (UTC) | Camera reachable | Stream back | Recording back | Result |",
          "|---|---|---|---|---|---|---|"]
    md += [f"| {r['camera']} | {r['cycle']} | {r['on_at']} | {r['camera_reachable_s']} s | {r['stream_back_s']} s | "
           f"{r['recording_back_s']} s | {'PASS' if r['pass'] else '**FAIL**'} |" for r in results]
    md += ["", "Times are seconds after power was restored. Camera reachable mostly reflects the camera's own boot time.",
           f"Raw data: {csv_path.name}"]
    print("report:", vivo.write_report("m3-reconnect", "\n".join(md) + "\n"))
    return 0 if all(r["pass"] for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
