#!/usr/bin/env python3
"""M5 acceptance: go2rtc stream integration.

Checks, for every camera stream (main and sub):
  1. go2rtc holds exactly one connection to the camera (one producer)
  2. that stays one while Frigate records AND an extra viewer is watching
  3. the stream is receiving video
Live playback in a browser is confirmed by the operator and passed with --live-ok.

usage:
  sudo tests/m5_check.py
  sudo tests/m5_check.py --live-ok cam01,cam02     # after watching each camera at
       http://<gateway>:1984/stream.html?src=cam01_main  (go2rtc web, LAN only)
"""
import argparse
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import vivo  # noqa: E402

rows: list[tuple[bool, str, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    rows.append((ok, name, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}  {detail}")


def counts(stream: str) -> tuple[int, int]:
    info = vivo.go2rtc(f"/streams?src={stream}")
    return len(info.get("producers") or []), len(info.get("consumers") or [])


def start_viewer(stream: str, seconds: int = 25) -> subprocess.Popen:
    # An extra RTSP client on the go2rtc restream, like a VMS or a second viewer.
    url = vivo.restream_url(stream).replace("127.0.0.1", "go2rtc")
    script = 'f="$(ls /usr/lib/ffmpeg/*/bin/ffmpeg | sort -V | tail -1)"; exec "$f" -v error -rtsp_transport tcp -i "$1" -t "$2" -c copy -f null -'
    return subprocess.Popen(["docker", "exec", "frigate", "sh", "-c", script, "viewer", url, str(seconds)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def serves_mse(stream: str) -> tuple[bool, str]:
    """go2rtc serves the stream as fragmented MP4, the format browsers play through MSE."""
    u, pw = vivo.SECRETS.get("GO2RTC_API_USER", ""), vivo.SECRETS.get("GO2RTC_API_PASSWORD", "")
    url = f"http://{u}:{pw}@go2rtc:1984/api/stream.mp4?src={stream}"
    try:
        out = vivo.ffprobe(["-v", "error", "-show_entries", "stream=codec_type,codec_name", "-of", "csv=p=0", "-i", url], timeout=30)
    except Exception as e:  # noqa: BLE001
        return False, vivo.redact(str(e))[:120]
    codecs = ", ".join(l.replace(",", " ") for l in out.split())
    return "video" in out, codecs


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--live-ok", default="", help="comma-separated camera ids the operator watched live in a browser")
    args = ap.parse_args()
    live_ok = {c for c in args.live_ok.split(",") if c}

    for cam in vivo.cameras():
        for kind in ("main", "sub"):
            s = f"{cam['id']}_{kind}"
            p0, c0 = counts(s)
            viewer = start_viewer(s)
            time.sleep(8)
            p1, c1 = counts(s)
            alive = vivo.stream_alive(s)
            viewer.wait(timeout=60)
            check(f"{s}: one camera connection, shared", p0 == 1 and p1 == 1,
                  f"producers {p0} -> {p1} while consumers {c0} -> {c1}")
            check(f"{s}: receiving video", alive)
            ok, codecs = serves_mse(s)
            check(f"{s}: served as fMP4 for browser (MSE) playback", ok, codecs)
        check(f"{cam['id']}: plays live in a browser (operator)", cam["id"] in live_ok,
              "confirmed" if cam["id"] in live_ok else "not confirmed: watch it, then rerun with --live-ok")

    md = ["# M5 go2rtc stream integration: check", "", f"Run at {vivo.utc(time.time())}.", "",
          f"Result: **{sum(r[0] for r in rows)} passed, {sum(not r[0] for r in rows)} failed**", "",
          "| Result | Check | Detail |", "|---|---|---|"]
    md += [f"| {'PASS' if ok else '**FAIL**'} | {n} | {d} |" for ok, n, d in rows]
    md += ["", "Producers are go2rtc's connections to the camera; consumers are Frigate, viewers and other clients.",
           "During the check an extra RTSP viewer was attached to each stream for 25 s."]
    print("report:", vivo.write_report("m5-check", "\n".join(md) + "\n"))
    return 0 if all(r[0] for r in rows) else 1


if __name__ == "__main__":
    sys.exit(main())
