#!/usr/bin/env python3
"""Make footage for the mock recorder (M18) with ffmpeg: test pictures cut into 10 s fragmented-MP4
pieces, named by their Unix start time, with optional gaps.

usage: tools/make_mock.py --dir DIR [--cameras cam01,cam02] [--start EPOCH] [--minutes 30]
                          [--gap 600:60 ...]     # remove 60 s starting 600 s after --start
Default start: the last full hour before now. Nothing here is committed (media files are not allowed
in the repository); tests make it in a temporary folder.
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

SEG = 10


def make(folder: Path, camera: str, start: int, minutes: float, gaps: list[tuple[int, int]],
         size: str = "640x360", fps: int = 15) -> int:
    out = folder / camera
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    with tempfile.TemporaryDirectory() as tmp:
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
               "-i", f"testsrc2=size={size}:rate={fps}", "-t", str(int(minutes * 60)),
               "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p", "-g", str(fps),
               "-keyint_min", str(fps), "-sc_threshold", "0",
               "-f", "hls", "-hls_time", str(SEG), "-hls_segment_type", "fmp4", "-hls_playlist_type", "vod",
               "-hls_fmp4_init_filename", "init.mp4", "-hls_segment_filename", f"{tmp}/p%05d.m4s",
               f"{tmp}/x.m3u8"]
        subprocess.run(cmd, check=True)
        shutil.move(f"{tmp}/init.mp4", out / "init.mp4")
        n = 0
        for i, p in enumerate(sorted(Path(tmp).glob("p*.m4s"))):
            t = start + i * SEG
            if any(g0 <= t - start < g0 + glen for g0, glen in gaps):
                continue
            shutil.move(str(p), out / f"{t}.m4s")
            n += 1
    (out / "meta.json").write_text(json.dumps({"segment_seconds": SEG}) + "\n")
    return n


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dir", required=True, type=Path)
    ap.add_argument("--cameras", default="cam01")
    ap.add_argument("--start", type=int)
    ap.add_argument("--minutes", type=float, default=30)
    ap.add_argument("--gap", action="append", default=[], help="OFFSET:SECONDS, both multiples of 10")
    a = ap.parse_args()
    start = a.start or (int(time.time()) // 3600 - 1) * 3600
    gaps = [tuple(int(x) for x in g.split(":")) for g in a.gap]
    for cam in a.cameras.split(","):
        n = make(a.dir, cam, start, a.minutes, gaps)
        print(f"{cam}: {n} pieces from {start} in {a.dir / cam}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
