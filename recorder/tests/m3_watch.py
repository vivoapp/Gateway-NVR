#!/usr/bin/env python3
"""M3 acceptance, when someone on site switches the cameras off and on (no PoE control here).

Runs in the background and watches every camera once a second. Each time a camera stops
answering on its RTSP port, it records one power cycle:
  dropped           the camera stopped answering (power cut seen by the gateway)
  camera reachable  the camera answers again (it has booted)
  stream back       go2rtc receives video from the camera again
  recording back    Frigate's first new segment for that camera starts
Every finished cycle is appended to a CSV at once, so nothing is lost if the run is stopped.

The power-on moment is not visible from the gateway, so it is taken as `dropped + --off`
(the off time the operator was asked to use). PASS = stream back within --limit seconds of
that power-on. The report also shows "gateway reconnect": stream back minus camera reachable,
which is the part the gateway itself is responsible for.

usage: sudo tests/m3_watch.py [--hours 48] [--off 20] [--limit 120]      # record
       sudo tests/m3_watch.py --report reports/m3-watch-YYYYmmdd-HHMMSS.csv  # summarise
"""
import argparse
import csv
import socket
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import vivo  # noqa: E402

FIELDS = ["camera", "dropped_at", "camera_reachable_s", "stream_back_s", "recording_back_s",
          "gateway_reconnect_s", "est_power_on_to_stream_s", "pass"]


def reachable(cam: dict) -> bool:
    try:
        with socket.create_connection((cam["host"], int(cam.get("port", 554))), timeout=0.7):
            return True
    except OSError:
        return False


def main_bytes() -> dict:
    """Bytes received so far per main stream, and the producer id (it changes on reconnect)."""
    try:
        streams = vivo.go2rtc("/streams")
    except Exception:
        return {}
    if not isinstance(streams, dict):  # an error page or a half-written reply: skip this poll
        return {}
    out = {}
    for name, info in (streams or {}).items():
        prods = (info or {}).get("producers") or []
        out[name] = (prods[0].get("id") if prods else None, sum(int(p.get("bytes_recv") or 0) for p in prods))
    return out


def watch(args) -> int:
    cams = vivo.cameras()
    vivo.REPORTS.mkdir(exist_ok=True)
    path = vivo.REPORTS / f"m3-watch-{time.strftime('%Y%m%d-%H%M%S', time.gmtime())}.csv"
    with path.open("w", newline="") as f:
        csv.DictWriter(f, fieldnames=FIELDS).writeheader()
    print(f"watching {len(cams)} cameras for {args.hours} h, writing {path}", flush=True)

    state = {c["id"]: {"misses": 0, "event": None} for c in cams}
    prev = main_bytes()
    end = time.time() + args.hours * 3600
    with ThreadPoolExecutor(max_workers=len(cams)) as pool:
        while time.time() < end:
            t = time.time()
            up = dict(zip([c["id"] for c in cams], pool.map(reachable, cams)))
            now_bytes = main_bytes()
            if not now_bytes:  # go2rtc did not answer properly: keep the previous counters, try again
                time.sleep(1.0)
                continue
            for cam in cams:
                cid, st = cam["id"], state[cam["id"]]
                pid0, b0 = prev.get(f"{cid}_main", (None, 0))
                pid1, b1 = now_bytes.get(f"{cid}_main", (None, 0))
                flowing = pid1 is not None and (b1 > b0 or (pid1 != pid0 and b1 > 0))
                ev = st["event"]
                if ev is None:
                    st["misses"] = 0 if up[cid] else st["misses"] + 1
                    if st["misses"] >= 2:  # two misses in a row: a real drop, not one lost packet
                        st["event"] = {"camera": cid, "t_drop": t - 1.0}
                        print(f"{vivo.utc(t)} {cid}: dropped", flush=True)
                    continue
                if "t_tcp" not in ev and up[cid]:
                    ev["t_tcp"] = t
                if "t_tcp" in ev and "t_stream" not in ev and flowing:
                    ev["t_stream"] = t
                if "t_stream" in ev and "t_rec" not in ev and t - ev.get("t_rec_check", 0) >= 5:
                    ev["t_rec_check"] = t
                    rec = vivo.first_segment_start_after(cid, ev["t_tcp"])
                    if rec:
                        ev["t_rec"] = rec
                timed_out = t - ev["t_drop"] > args.giveup
                if "t_rec" in ev or timed_out:
                    rel = lambda k: round(ev[k] - ev["t_drop"], 1) if k in ev else None  # noqa: E731
                    est = round(ev["t_stream"] - (ev["t_drop"] + args.off), 1) if "t_stream" in ev else None
                    row = {"camera": cid, "dropped_at": vivo.utc(ev["t_drop"]),
                           "camera_reachable_s": rel("t_tcp"), "stream_back_s": rel("t_stream"),
                           "recording_back_s": rel("t_rec"),
                           "gateway_reconnect_s": round(ev["t_stream"] - ev["t_tcp"], 1) if "t_stream" in ev else None,
                           "est_power_on_to_stream_s": est,
                           "pass": est is not None and est <= args.limit}
                    with path.open("a", newline="") as f:
                        csv.DictWriter(f, fieldnames=FIELDS).writerow(row)
                    print(f"{vivo.utc(t)} {cid}: cycle done {row}", flush=True)
                    st["event"], st["misses"] = None, 0
            prev = now_bytes
            time.sleep(max(0.0, 1.0 - (time.time() - t)))
    print(f"done; summarise with: sudo tests/m3_watch.py --report {path}")
    return 0


def report(csv_path: Path, off: float, limit: float) -> int:
    rows = list(csv.DictReader(csv_path.open()))
    if not rows:
        print("no power cycles recorded yet")
        return 1
    per_cam = {}
    for r in rows:
        per_cam.setdefault(r["camera"], []).append(r["pass"] == "True")
    ok_cams = [c for c, v in per_cam.items() if len(v) >= 5 and all(v)]
    md = ["# M3 camera reconnect test (power cycled on site)", "",
          f"Recorded by `tests/m3_watch.py` on the gateway. Power was switched by Vivo on site, so the power-on "
          f"moment is estimated as drop + {off:.0f} s (the off time asked for). Limit: stream back within "
          f"**{limit:.0f} s** of power on.", "",
          f"Result: **{sum(all(v) and len(v) >= 5 for v in per_cam.values())} of {len(vivo.cameras())} cameras** "
          f"passed 5 or more cycles; {sum(sum(v) for v in per_cam.values())} of {len(rows)} cycles passed.", "",
          "| Camera | Dropped (UTC) | Camera reachable | Stream back | Recording back | Gateway reconnect | Est. power on to stream | Result |",
          "|---|---|---|---|---|---|---|---|"]
    md += [f"| {r['camera']} | {r['dropped_at']} | {r['camera_reachable_s']} s | {r['stream_back_s']} s | "
           f"{r['recording_back_s']} s | {r['gateway_reconnect_s']} s | {r['est_power_on_to_stream_s']} s | "
           f"{'PASS' if r['pass'] == 'True' else '**FAIL**'} |" for r in rows]
    md += ["", "Columns 3 to 5 are seconds after the drop. Gateway reconnect = stream back minus camera reachable.",
           f"Cameras with 5 or more passing cycles: {', '.join(sorted(ok_cams)) or 'none'}. Raw data: {csv_path.name}"]
    print("report:", vivo.write_report("m3-reconnect-onsite", "\n".join(md) + "\n"))
    return 0 if len(ok_cams) == len(vivo.cameras()) else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--hours", type=float, default=48)
    ap.add_argument("--off", type=float, default=20, help="seconds the operator keeps the cameras off")
    ap.add_argument("--limit", type=float, default=120, help="max seconds from power on to stream back")
    ap.add_argument("--giveup", type=float, default=900, help="seconds after a drop to record it as failed")
    ap.add_argument("--report", type=Path, help="summarise a recorded CSV instead of watching")
    a = ap.parse_args()
    return report(a.report, a.off, a.limit) if a.report else watch(a)


if __name__ == "__main__":
    sys.exit(main())
