#!/usr/bin/env python3
"""Simulates cameras recording video segments, for demos and tests.

Same layout and names as the gateway recorder:

    <dir>/<camera_id>/<YYYY-MM-DD>/<camera_id>_<YYYYMMDDTHHMMSS>Z.mp4     (UTC start time)

Like the real recorder it writes each segment as `<name>.mp4.part` and renames it to `.mp4` when
finished, so the uploader never sees half-written files.

    python tools/fake_recorder.py --dir recordings --cameras 2 --interval 3 --size-mb 2
"""
import argparse
import os
import time
from datetime import datetime, timezone
from pathlib import Path


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default="recordings")
    ap.add_argument("--cameras", type=int, default=2)
    ap.add_argument("--interval", type=float, default=3.0, help="seconds between segments")
    ap.add_argument("--name-format", default="cam{:02d}",
                    help='camera id pattern, e.g. "VIVO-CAM-{:02d}" for the real names')
    ap.add_argument("--size-mb", type=float, default=2.0)
    ap.add_argument("--count", type=int, default=0, help="segments per camera (0 = run forever)")
    ap.add_argument("--retain-minutes", type=float, default=0,
                    help="delete own segments older than this, like the recorder's retention (0 = keep all)")
    args = ap.parse_args()

    root = Path(args.dir)
    n = 0
    try:
        while args.count == 0 or n < args.count:
            n += 1
            now = datetime.now(timezone.utc)
            for cam in range(1, args.cameras + 1):
                camera_id = args.name_format.format(cam)
                cam_dir = root / camera_id / now.strftime("%Y-%m-%d")
                cam_dir.mkdir(parents=True, exist_ok=True)
                final = cam_dir / f"{camera_id}_{now.strftime('%Y%m%dT%H%M%S')}Z.mp4"
                part = final.with_name(final.name + ".part")
                with open(part, "wb") as f:
                    for _ in range(max(int(args.size_mb), 1)):
                        f.write(os.urandom(1024 * 1024))
                os.replace(part, final)                 # atomic: file appears complete
                print(f"recorded {final} ({final.stat().st_size} bytes)", flush=True)
            if args.retain_minutes > 0:
                cutoff = time.time() - args.retain_minutes * 60
                for old in root.glob("*/*/*.mp4"):
                    if old.stat().st_mtime < cutoff:
                        old.unlink()
            time.sleep(args.interval)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
