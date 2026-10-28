"""The segment file name agreed with the gateway developer.

    <MEDIA_ROOT>/recordings/substream/<camera_id>/<YYYY-MM-DD>/<camera_id>_<YYYYMMDDTHHMMSS>Z.mp4

The time in the name is the UTC start of the segment. A recorder writes `<name>.mp4.part` and renames
it to `<name>.mp4` only when the segment is complete, so every `.mp4` file is finished.

The end time is not in the name yet. Until it is, end = start + the nominal segment length
(60 s). If the name later carries the end time as a second timestamp
(`<camera_id>_<start>Z_<end>Z.mp4`) it is used instead, so no code change is needed.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone

_NAME = re.compile(
    r"^(?P<camera>[A-Za-z0-9][A-Za-z0-9_-]*?)"
    r"_(?P<start>\d{8}T\d{6})Z"
    r"(?:_(?P<end>\d{8}T\d{6})Z)?"
    r"\.mp4$")
_STAMP = "%Y%m%dT%H%M%S"


def _epoch(stamp: str) -> int | None:
    try:
        return int(datetime.strptime(stamp, _STAMP).replace(tzinfo=timezone.utc).timestamp())
    except ValueError:                      # e.g. month 13
        return None


def iso_utc(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class SegmentInfo:
    camera_id: str
    start_utc: int          # seconds since the epoch, UTC
    end_utc: int
    end_from_name: bool     # False = end time is start + nominal length (an estimate)

    @property
    def start_iso(self) -> str:
        return iso_utc(self.start_utc)

    @property
    def end_iso(self) -> str:
        return iso_utc(self.end_utc)


def parse_segment_name(name: str, nominal_seconds: float = 60) -> SegmentInfo | None:
    """Return the segment's camera and UTC times, or None if the name is not in the agreed format."""
    m = _NAME.match(name)
    if not m:
        return None
    start = _epoch(m["start"])
    if start is None:
        return None
    if m["end"]:
        end = _epoch(m["end"])
        if end is None or end <= start:
            return None
        return SegmentInfo(m["camera"], start, end, True)
    return SegmentInfo(m["camera"], start, start + int(round(nominal_seconds)), False)
