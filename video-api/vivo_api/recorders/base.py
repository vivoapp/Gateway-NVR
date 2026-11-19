"""The recorder adapter interface (contract section 10).

Every call the API makes to a recorder goes through one of these. Frigate is the first adapter
(frigate.py, M14), a mock recorder the second (mock.py, M18). A new recorder needs a new adapter
that passes tests/contract; routes, Rails and Vivo Watch stay unchanged.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import AsyncIterator, Protocol

# Names of HLS pieces a recorder may hand out: plain file names, never a path.
PIECE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}\.(m3u8|mp4|m4s|ts|vtt)$")

CONTENT_TYPES = {
    "m3u8": "application/vnd.apple.mpegurl",
    "mp4": "video/mp4",
    "m4s": "video/iso.segment",
    "ts": "video/mp2t",
    "vtt": "text/vtt",
}


class RecorderUnavailable(Exception):
    """The recorder did not answer (the API maps this to 502 upstream_unavailable)."""


class NotRecorded(Exception):
    """Nothing to serve for this range or piece (404)."""


@dataclass(frozen=True)
class Segment:
    start: float
    end: float


@dataclass
class Media:
    body: AsyncIterator[bytes]
    content_type: str
    length: int | None = None


@dataclass
class ClipSource:
    body: AsyncIterator[bytes]
    expected_bytes: int | None


class Recorder(Protocol):
    name: str
    entry: str                       # the playlist name a playback starts from, e.g. "index.m3u8"

    async def cameras(self) -> list[str]: ...

    async def segments(self, camera: str, start: float, end: float) -> list[Segment]: ...

    async def media(self, camera: str, start: float, end: float, piece: str) -> Media: ...

    async def clip(self, camera: str, start: float, end: float) -> ClipSource: ...

    async def health(self) -> bool: ...

    async def aclose(self) -> None: ...


def content_type(piece: str) -> str:
    return CONTENT_TYPES.get(piece.rsplit(".", 1)[-1], "application/octet-stream")


def merge(segments: list[Segment], start: float, end: float, gap: float) -> tuple[list, list]:
    """Recorded periods and gaps of [start, end].

    Segments are clipped to the window and merged when the hole between them is under `gap`
    seconds (neighbouring recorder segments touch or overlap slightly). A hole of `gap` or more,
    including at either edge of the window, is a gap.
    """
    periods: list[list[float]] = []
    for s in sorted(segments, key=lambda x: x.start):
        a, b = max(s.start, start), min(s.end, end)
        if b <= a:
            continue
        if periods and a - periods[-1][1] < gap:
            periods[-1][1] = max(periods[-1][1], b)
        else:
            periods.append([a, b])
    # edges: a recording that starts or ends less than `gap` inside the window covers the edge
    if periods and periods[0][0] - start < gap:
        periods[0][0] = start
    if periods and end - periods[-1][1] < gap:
        periods[-1][1] = end
    gaps, cursor = [], start
    for a, b in periods:
        if a > cursor:
            gaps.append((cursor, a))
        cursor = b
    if cursor < end:
        gaps.append((cursor, end))
    return [(a, b) for a, b in periods], gaps
