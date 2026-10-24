#!/usr/bin/env python3
"""M3: probe every camera stream through go2rtc and write the camera table.

Probes go through the go2rtc restream, so they never open extra sessions on
the cameras. Model and firmware come from cameras.yaml (filled in by hand from
each camera's web page); everything else is measured.

usage: sudo tools/probe_cameras.py   (VIVO_INVENTORY=... to use another camera list)
       sudo tools/probe_cameras.py [--seconds 30] [--out docs/camera-table.md]
"""
import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import vivo  # noqa: E402


def container_url(stream: str) -> str:
    # ffprobe runs inside the frigate container, which reaches go2rtc by name.
    return vivo.restream_url(stream).replace("127.0.0.1", "go2rtc")


def probe(stream: str) -> dict:
    out = json.loads(vivo.ffprobe(["-v", "error", "-rtsp_transport", "tcp", "-i", container_url(stream),
                                   "-show_entries", "stream=codec_type,codec_name,profile,width,height,avg_frame_rate,sample_rate,channels",
                                   "-of", "json"]))
    info = {}
    for s in out.get("streams", []):
        if s.get("codec_type") == "video" and "video" not in info:
            num, _, den = str(s.get("avg_frame_rate", "0/1")).partition("/")
            fps = float(num) / float(den or 1) if float(den or 1) else 0
            info["video"] = f"{s.get('codec_name')} {s.get('profile', '')}".strip()
            info["res"] = f"{s.get('width')}x{s.get('height')}"
            info["fps"] = round(fps, 1)
        elif s.get("codec_type") == "audio" and "audio" not in info:
            info["audio"] = f"{s.get('codec_name')} {s.get('sample_rate', '')} Hz".strip()
    return info


def gop_seconds(stream: str) -> float | None:
    pk = vivo.ffprobe(["-v", "error", "-rtsp_transport", "tcp", "-read_intervals", "%+12",
                       "-select_streams", "v:0", "-show_entries", "packet=pts_time,flags", "-of", "csv=p=0",
                       container_url(stream)], timeout=40)
    keys = [float(l.split(",")[0]) for l in pk.splitlines() if "K" in l.split(",")[-1] and l.split(",")[0] not in ("", "N/A")]
    return round((keys[-1] - keys[0]) / (len(keys) - 1), 2) if len(keys) >= 2 else None


def problems(cam: dict, main: dict, sub: dict) -> list:
    p = []
    for label, s in (("main", main), ("sub", sub)):
        if "hevc" in s.get("video", "") or "h265" in s.get("video", ""):
            p.append(f"{label} is H.265 (limited browser support; scope default is H.264)")
        if s.get("fps") and s["fps"] < 10:
            p.append(f"{label} at {s['fps']} fps")
    a = main.get("audio", "")
    if any(c in a for c in ("pcm_alaw", "pcm_mulaw")) and cam.get("audio") != "aac":
        p.append("G.711 audio: set audio: aac in cameras.yaml")
    if "aac" in a and cam.get("audio") == "aac":
        p.append("camera sends AAC: audio: copy would save CPU")
    if main.get("gop") and main["gop"] > 4:
        p.append(f"long keyframe interval ({main['gop']} s) delays segment cuts")
    return p


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--seconds", type=int, default=30, help="bitrate measuring window")
    ap.add_argument("--out", default=str(vivo.ROOT / "docs" / "camera-table.md"))
    args = ap.parse_args()

    cams = vivo.cameras()
    streams = [f"{c['id']}_{k}" for c in cams for k in ("main", "sub")]
    b0 = {s: vivo.stream_bytes(s)[1] for s in streams}
    t0 = time.time()

    rows = []
    for cam in cams:
        print(f"probing {cam['id']}...")
        main = probe(f"{cam['id']}_main")
        sub = probe(f"{cam['id']}_sub")
        main["gop"] = gop_seconds(f"{cam['id']}_main")
        rows.append((cam, main, sub))

    time.sleep(max(0, args.seconds - (time.time() - t0)))
    dt = time.time() - t0
    # go2rtc's byte counter restarts when a stream reconnects, so a drop means "no valid measurement".
    b1 = {s: vivo.stream_bytes(s)[1] for s in streams}
    kbps = {s: (b1[s] - b0[s]) * 8 / dt / 1000 if b1[s] >= b0[s] else None for s in streams}

    md = ["# Camera table", "",
          f"Measured {vivo.utc(time.time())} through go2rtc on the reference gateway. "
          f"Bitrate averaged over {dt:.0f} s. Model and firmware from cameras.yaml.", "",
          "| ID | Name | Model | Firmware | Stream URL pattern | Main stream | Sub stream | Audio | Problems |",
          "|---|---|---|---|---|---|---|---|---|"]
    for cam, main, sub in rows:
        rate = lambda k: "n/a (reconnected)" if k is None else f"{k:.0f} kbit/s"  # noqa: E731
        mk, sk = rate(kbps[f"{cam['id']}_main"]), rate(kbps[f"{cam['id']}_sub"])
        url = f"rtsp://USER:PASS@{cam['host']}:{cam.get('port', 554)}{cam['main_path']} (sub {cam['sub_path']})"
        m = f"{main.get('video')} {main.get('res')} {main.get('fps')} fps, {mk}, GOP {main.get('gop')} s"
        s = f"{sub.get('video')} {sub.get('res')} {sub.get('fps')} fps, {sk}"
        md.append(f"| {cam['id']} | {cam['name']} | {cam.get('model') or '?'} | {cam.get('firmware') or '?'} | {url} | {m} | {s} | "
                  f"{main.get('audio', 'none')} | {'; '.join(problems(cam, main, sub)) or 'none'} |")
    text = vivo.redact("\n".join(md) + "\n")
    Path(args.out).write_text(text)
    print(text)
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
