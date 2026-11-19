"""Mock second recorder (M18): proves the API does not depend on Frigate.

It serves recordings from a folder of fragmented-MP4 pieces, the way a different recorder might
store them:

    <mock_dir>/<camera>/init.mp4            movie header shared by all pieces
    <mock_dir>/<camera>/<start>.m4s         one piece of `segment_seconds` from Unix time <start>
    <mock_dir>/<camera>/meta.json           {"segment_seconds": 10}

tools/make_mock.py makes such a folder with ffmpeg (test pictures, with gaps where asked).
Playback is an HLS playlist of the pieces; a clip is the header plus the pieces (a fragmented MP4).
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from .base import ClipSource, Media, NotRecorded, RecorderUnavailable, Segment, content_type

CHUNK = 256 * 1024
_PIECE = re.compile(r"^(\d{9,11})\.m4s$")


async def _read(paths: list[Path]):
    for p in paths:
        try:
            with open(p, "rb") as f:
                while chunk := f.read(CHUNK):
                    yield chunk
        except OSError:
            raise RecorderUnavailable(f"cannot read {p.name}") from None


class MockRecorder:
    name = "mock"
    entry = "index.m3u8"

    def __init__(self, folder: Path):
        self.folder = Path(folder)

    async def aclose(self) -> None:
        pass

    def _cam(self, camera: str) -> Path:
        d = self.folder / camera
        if not (d / "init.mp4").is_file():
            raise NotRecorded(camera)
        return d

    def _seconds(self, d: Path) -> float:
        try:
            return float(json.loads((d / "meta.json").read_text()).get("segment_seconds", 10))
        except (OSError, ValueError):
            return 10.0

    def _pieces(self, camera: str, start: float, end: float) -> list[tuple[int, Path, float]]:
        d = self._cam(camera)
        dur = self._seconds(d)
        out = []
        for p in d.iterdir():
            m = _PIECE.match(p.name)
            if m:
                t = int(m.group(1))
                if t + dur > start and t < end:
                    out.append((t, p, dur))
        return sorted(out)

    async def health(self) -> bool:
        return self.folder.is_dir()

    async def cameras(self) -> list[str]:
        if not self.folder.is_dir():
            raise RecorderUnavailable("mock folder missing")
        return sorted(p.name for p in self.folder.iterdir() if (p / "init.mp4").is_file())

    async def segments(self, camera: str, start: float, end: float) -> list[Segment]:
        try:
            return [Segment(float(t), float(t) + dur) for t, _, dur in self._pieces(camera, start, end)]
        except NotRecorded:
            return []

    async def media(self, camera: str, start: float, end: float, piece: str) -> Media:
        d = self._cam(camera)
        if piece == self.entry:
            pieces = self._pieces(camera, start, end)
            if not pieces:
                raise NotRecorded(piece)
            dur = pieces[0][2]
            lines = ["#EXTM3U", "#EXT-X-VERSION:7", f"#EXT-X-TARGETDURATION:{int(dur + 0.999)}",
                     "#EXT-X-PLAYLIST-TYPE:VOD", "#EXT-X-MEDIA-SEQUENCE:0", '#EXT-X-MAP:URI="init.mp4"']
            prev_end = None
            for t, p, d_ in pieces:
                if prev_end is not None and t - prev_end >= 1:
                    lines.append("#EXT-X-DISCONTINUITY")
                lines += [f"#EXTINF:{d_:.3f},", p.name]
                prev_end = t + d_
            lines.append("#EXT-X-ENDLIST")
            text = ("\n".join(lines) + "\n").encode()

            async def body():
                yield text
            return Media(body(), content_type(piece), len(text))
        if piece == "init.mp4":
            path = d / "init.mp4"
        else:
            m = _PIECE.match(piece)
            if not m:
                raise NotRecorded(piece)
            t = int(m.group(1))
            dur = self._seconds(d)
            path = d / piece
            if not path.is_file() or not (t + dur > start and t < end):
                raise NotRecorded(piece)
        return Media(_read([path]), content_type(piece), path.stat().st_size)

    async def clip(self, camera: str, start: float, end: float) -> ClipSource:
        pieces = self._pieces(camera, start, end)
        if not pieces:
            raise NotRecorded("clip")
        d = self._cam(camera)
        paths = [d / "init.mp4"] + [p for _, p, _ in pieces]
        return ClipSource(_read(paths), sum(p.stat().st_size for p in paths))
