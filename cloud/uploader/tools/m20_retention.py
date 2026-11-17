#!/usr/bin/env python3
"""M20: how long does the catch-up take after an outage, and how many days must the gateway keep files?

    python tools/m20_retention.py --outage-days 2 --speed-mbps 20
    python tools/m20_retention.py --outage-days 3                    (table for several speeds)

The numbers behind it (change them with the options if Vivo's real figures differ):

  * one camera records about 1 MB per minute (substream), 10 cameras: about 1.33 Mbps all the time;
  * while the storage is not reachable, that data piles up on the gateway disk;
  * when it is reachable again, the uploader sends the pile (oldest first) AND the new segments, so
    only "speed minus the live rate" is available for the pile:

        catch-up time = pile size / (upload speed - live rate)

How many days of files must the recorder keep (M9 retention)?

  The uploader sends the OLDEST segment first. After an outage of D days, the oldest waiting segment
  is D days old; it goes first, and everything behind it is younger. So the catch-up time itself does
  not add to the age of the oldest segment; what matters is that the outage plus some reaction time
  fits inside the retention:

        retention days  >=  outage days  +  margin

  The margin (default 1 day) covers what has no fixed length: the outage being noticed late, the
  gateway or the uploader being down at the same time, and the first retry (up to the longest wait,
  5 minutes). It is a choice, not a measurement. The disk must hold the retention days of video.
"""
from __future__ import annotations

import argparse
import math

DEFAULT_SPEEDS = (5, 10, 20, 50, 100)


def live_mbps(cameras: int, mb_per_min: float) -> float:
    return cameras * mb_per_min * 8 / 60


def pile_gb(outage_days: float, cameras: int, mb_per_min: float) -> float:
    return outage_days * 1440 * cameras * mb_per_min / 1000


def catchup_hours(outage_days: float, speed_mbps: float, cameras: int = 10, mb_per_min: float = 1.0,
                  wait_min: float = 5.0) -> float | None:
    """Hours from "the storage is back" until the pile is gone. None = never (speed <= live rate)."""
    spare = speed_mbps - live_mbps(cameras, mb_per_min)
    if spare <= 0:
        return None
    seconds = pile_gb(outage_days, cameras, mb_per_min) * 8000 / spare
    return seconds / 3600 + wait_min / 60


def retention_days(outage_days: float, margin_days: float = 1.0) -> int:
    return math.ceil(outage_days + margin_days)


def disk_gb(days: float, cameras: int = 10, mb_per_min: float = 1.0) -> float:
    return days * 1440 * cameras * mb_per_min / 1000


def fmt_hours(h: float | None) -> str:
    if h is None:
        return "never (speed is below the live rate)"
    return f"{h * 60:.0f} min" if h < 1 else f"{h:.1f} h"


def table(outage_days: float, speeds=DEFAULT_SPEEDS, cameras: int = 10, mb_per_min: float = 1.0,
          wait_min: float = 5.0) -> str:
    rows = ["| Upload speed | Catch-up after a %g-day outage |" % outage_days, "|---|---|"]
    for s in speeds:
        rows.append(f"| {s:g} Mbps | {fmt_hours(catchup_hours(outage_days, s, cameras, mb_per_min, wait_min))} |")
    return "\n".join(rows)


def recommendation(outage_days: float, speed_mbps: float | None = None, cameras: int = 10,
                   mb_per_min: float = 1.0, margin_days: float = 1.0) -> str:
    keep = retention_days(outage_days, margin_days)
    live = live_mbps(cameras, mb_per_min)
    out = [f"- Live data: {cameras} cameras x {mb_per_min:g} MB/min = {live:.2f} Mbps, "
           f"{disk_gb(1, cameras, mb_per_min):.1f} GB per day.",
           f"- A {outage_days:g}-day outage piles up {pile_gb(outage_days, cameras, mb_per_min):.1f} GB.",
           f"- **Recommended retention for M9: at least {keep} days** "
           f"({outage_days:g} days of outage + {margin_days:g} day margin), "
           f"which needs about {disk_gb(keep, cameras, mb_per_min):.0f} GB of recording disk."]
    if speed_mbps:
        h = catchup_hours(outage_days, speed_mbps, cameras, mb_per_min)
        out.append(f"- At {speed_mbps:g} Mbps the pile is gone {fmt_hours(h)} after the storage is back"
                   + (" (up to 5 minutes of that is the uploader's longest wait before its next try)." if h else "."))
    return "\n".join(out)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--outage-days", type=float, default=2.0, help="the agreed outage length (default 2)")
    ap.add_argument("--speed-mbps", type=float, help="the agreed upload speed; leave out for a table")
    ap.add_argument("--cameras", type=int, default=10)
    ap.add_argument("--mb-per-min", type=float, default=1.0, help="MB per camera per minute (default 1)")
    ap.add_argument("--margin-days", type=float, default=1.0)
    a = ap.parse_args()
    print(recommendation(a.outage_days, a.speed_mbps, a.cameras, a.mb_per_min, a.margin_days))
    print()
    print(table(a.outage_days, (a.speed_mbps,) if a.speed_mbps else DEFAULT_SPEEDS, a.cameras, a.mb_per_min))


if __name__ == "__main__":
    main()
