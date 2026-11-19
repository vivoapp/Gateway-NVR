"""Frigate adapter (M14): the only place the API talks to Frigate.

Frigate is reached on its authenticated port (8971) as the viewer user `video_api`, never on the
unauthenticated port 5000. The viewer role can read recordings, HLS playback (/vod) and clip.mp4,
and cannot change configuration, users or recordings. Frigate's certificate is self-signed and
remade when its container is recreated, so it is not checked; that is safe only because the API
reaches Frigate on 127.0.0.1 (the constructor refuses any other host for unverified TLS).
"""
from __future__ import annotations

import asyncio
import ipaddress
import math
from urllib.parse import urlsplit

import httpx

from .base import PIECE, ClipSource, Media, NotRecorded, RecorderUnavailable, Segment, content_type

CHUNK = 256 * 1024


def _loopback(url: str) -> bool:
    """True only for a loopback IP address. A name (even "localhost") is refused: it could resolve elsewhere."""
    host = urlsplit(url).hostname or ""
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class FrigateRecorder:
    name = "frigate"
    entry = "index.m3u8"

    def __init__(self, url: str, user: str, password: str, transport: httpx.AsyncBaseTransport | None = None):
        if url.startswith("https://") and not _loopback(url) and transport is None:
            raise ValueError("Frigate must be reached on 127.0.0.1 when its certificate is not checked")
        self.user, self.password = user, password
        self.client = httpx.AsyncClient(base_url=url.rstrip("/"), verify=False, transport=transport,
                                        timeout=httpx.Timeout(10.0, read=60.0))
        self._login_lock = asyncio.Lock()
        self._logged_in = False

    async def aclose(self) -> None:
        await self.client.aclose()

    # ------------------------------------------------------------------ HTTP with login
    async def _login(self) -> None:
        async with self._login_lock:
            try:
                r = await self.client.post("/api/login", json={"user": self.user, "password": self.password})
            except httpx.HTTPError as exc:
                raise RecorderUnavailable(f"login: {type(exc).__name__}") from None
            if r.status_code != 200:
                raise RecorderUnavailable(f"login refused ({r.status_code})")
            self._logged_in = True

    async def _send(self, method: str, path: str, stream: bool = False, timeout: float | None = None):
        if not self._logged_in:
            await self._login()
        for attempt in (1, 2):
            req = self.client.build_request(method, path, timeout=timeout or httpx.USE_CLIENT_DEFAULT)
            try:
                r = await self.client.send(req, stream=stream)
            except httpx.HTTPError as exc:
                raise RecorderUnavailable(f"{path.split('?')[0]}: {type(exc).__name__}") from None
            if r.status_code == 401 and attempt == 1:       # session expired: log in again once
                await r.aclose()
                self._logged_in = False
                await self._login()
                continue
            return r
        return r

    async def _json(self, path: str):
        r = await self._send("GET", path)
        if r.status_code != 200:
            raise RecorderUnavailable(f"{path.split('?')[0]}: HTTP {r.status_code}")
        try:
            return r.json()
        except ValueError:
            raise RecorderUnavailable(f"{path.split('?')[0]}: not JSON") from None

    # ------------------------------------------------------------------ adapter interface
    async def health(self) -> bool:
        try:
            r = await self._send("GET", "/api/version", timeout=5.0)
            return r.status_code == 200
        except RecorderUnavailable:
            return False

    async def cameras(self) -> list[str]:
        cfg = await self._json("/api/config")
        return sorted((cfg.get("cameras") or {}).keys()) if isinstance(cfg, dict) else []

    async def _recordings(self, camera: str, start: float, end: float) -> list[dict]:
        # a little either side, so a segment that started before the window is included
        rows = await self._json(f"/api/{camera}/recordings?after={math.floor(start) - 15}&before={math.ceil(end) + 1}")
        if not isinstance(rows, list):
            raise RecorderUnavailable("recordings: unexpected answer")
        return [r for r in rows if isinstance(r, dict) and "start_time" in r and "end_time" in r]

    async def segments(self, camera: str, start: float, end: float) -> list[Segment]:
        rows = await self._recordings(camera, start, end)
        segs = [Segment(float(r["start_time"]), float(r["end_time"])) for r in rows]
        return sorted((s for s in segs if s.end > start and s.start < end), key=lambda s: s.start)

    async def _stream(self, path: str) -> Media:
        r = await self._send("GET", path, stream=True, timeout=60.0)
        if r.status_code == 404:
            await r.aclose()
            raise NotRecorded(path)
        if r.status_code != 200:
            await r.aclose()
            raise RecorderUnavailable(f"HTTP {r.status_code}")

        async def body():
            try:
                async for chunk in r.aiter_bytes(CHUNK):
                    yield chunk
            except httpx.HTTPError:
                raise RecorderUnavailable("stream interrupted") from None
            finally:
                await r.aclose()

        length = r.headers.get("content-length")
        return Media(body(), r.headers.get("content-type") or "application/octet-stream",
                     int(length) if length and length.isdigit() else None)

    async def media(self, camera: str, start: float, end: float, piece: str) -> Media:
        if not PIECE.match(piece):
            raise NotRecorded(piece)
        m = await self._stream(f"/vod/{camera}/start/{math.floor(start)}/end/{math.ceil(end)}/{piece}")
        m.content_type = content_type(piece)
        return m

    async def clip(self, camera: str, start: float, end: float) -> ClipSource:
        rows = await self._recordings(camera, start, end)
        inside = [r for r in rows if r["end_time"] > start and r["start_time"] < end]
        if not inside:
            raise NotRecorded("clip")
        # segment_size is MiB for the whole segment; count the part inside the range
        expected = 0.0
        for r in inside:
            size, dur = float(r.get("segment_size") or 0), float(r["end_time"]) - float(r["start_time"])
            if dur > 0:
                share = (min(end, r["end_time"]) - max(start, r["start_time"])) / dur
                expected += size * 2 ** 20 * max(0.0, min(1.0, share))
        m = await self._stream(f"/api/{camera}/start/{math.floor(start)}/end/{math.ceil(end)}/clip.mp4")
        return ClipSource(m.body, int(expected) or None)
