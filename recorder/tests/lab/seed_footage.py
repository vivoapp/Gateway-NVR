#!/usr/bin/env python3
"""Lab only: put copies of real lab recordings into earlier hours, so the disk-pressure test
(tests/m9_pressure.py) has old footage to delete. A lab that just started has none, and the
storage guard never deletes the last hour.

The copies go where the real files would be: main stream in Frigate's folders
(recordings/YYYY-MM-DD/HH/<camera>/MM.SS.mp4) and substream segments under their normal names.
Frigate's database does not list the main-stream copies, so the guard's API call finds nothing
to delete for those hours and the guard deletes the files directly (its fallback path).

usage: tests/lab/seed_footage.py --hours 6 [--mb-per-hour 200]   (after: eval "$(tests/lab/lab.sh env)")
"""
import argparse
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
import storage_guard as sg  # noqa: E402
import substream_archiver as sa  # noqa: E402
import vivo  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--hours", type=int, default=6, help="hours of old footage, ending 2 hours ago")
    ap.add_argument("--mb-per-hour", type=float, default=200, help="main stream per hour, all cameras together")
    args = ap.parse_args()
    layout = sg.Layout(Path(vivo.SITE["MEDIA_ROOT"]))
    cams = vivo.services_config()["cameras"]
    main_src = next(iter(sorted(sg.tree_files(layout.main))), None)
    sub_src = next(iter(f for _, f in sg.substream_segments(layout)), None)
    if not main_src or not sub_src:
        print("ERROR: no lab recordings yet to copy; let the lab run a few minutes first", file=sys.stderr)
        return 1
    copies = max(1, int(args.mb_per_hour * 1e6 / len(cams) / max(main_src.stat().st_size, 1)))
    now_hour = int(time.time()) // 3600 * 3600
    for n in range(args.hours, 0, -1):
        hour = now_hour - (n + 1) * 3600
        for cam in cams:
            d = sg.main_hour_dir(layout, hour) / cam
            d.mkdir(parents=True, exist_ok=True)
            for i in range(copies):
                shutil.copyfile(main_src, d / f"{(i * 10) // 60 % 60:02d}.{(i * 10) % 60:02d}.mp4")
            for m in range(0, 60, 10):                   # a few substream segments per camera and hour
                start = hour + m * 60
                day = sa.day_dir(layout.substream, cam, start)
                day.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(sub_src, day / sa.segment_name(cam, start, start + 60))
        print(f"seeded {vivo.utc(hour)}: {copies} main files and 6 substream segments per camera")
    return 0


if __name__ == "__main__":
    sys.exit(main())
