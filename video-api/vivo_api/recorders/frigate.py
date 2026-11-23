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
import re
import time
from urllib.parse import urlsplit

import httpx

from .base import PIECE, ClipSource, Media, NotRecorded, RecorderUnavailable, Segment, content_type

CHUNK = 256 * 1024
# Frigate's video module refuses one playlist of more than about 1,100 pieces (about 3 hours of
# 10 s segments: "invalid number of elements in the durations array"). Longer playbacks are asked
# for in clock-hour parts and joined into one playlist (join_playlists). Frigate builds a part's
# playlist in about 90 ms, one at a time, so the parts of finished hours are kept for VOD_KEEP
# seconds: the same hour is the same window for every playback, so only new hours are asked for.
VOD_PART = 3600
VOD_PARALLEL = 6
VOD_KEEP = 3600
VOD_SETTLED = 180          # an hour's part is kept once it ended this long ago (its last segment is written)
VOD_KEEP_MAX = 2000        # parts kept at most (about 25 hours of 10 cameras, a few MB)
_PART_PIECE = re.compile(r"^c(\d{1,3})-(.+)$")
_URI_ATTR = re.compile(r'URI="([^"]*)"')
_SKIP = ("#EXT-X-PLAYLIST-TYPE", "#EXT-X-MEDIA-SEQUENCE", "#EXT-X-ALLOW-CACHE", "#EXT-X-INDEPENDENT-SEGMENTS",
         "#EXT-X-ENDLIST", "#EXTM3U")


def vod_parts(start: float, end: float) -> list[tuple[int, int]]:
    """The whole-second windows a playback is asked for in: cut on clock hours (UTC), so a full hour
    is the same window, and the same cached playlist, for every playback that covers it."""
    a, e, out = math.floor(start), math.ceil(end), []
    while a < e:
        b = min(e, (a // VOD_PART + 1) * VOD_PART)
        out.append((a, b))
        a = b
    return out


def join_playlists(texts: list[str | None]) -> str | None:
    """One VOD playlist from Frigate's playlists of consecutive parts (None: nothing recorded there).

    The pieces of part k are named c<k>-<name>; every part after the first starts with a
    discontinuity and its own EXT-X-MAP. None when no part has a piece.
    """
    target, version, body = 0, 6, []
    for k, text in enumerate(texts):
        part = []
        for line in (text or "").splitlines():
            s = line.strip()
            if not s or s.startswith(_SKIP):
                continue
            if s.startswith("#EXT-X-TARGETDURATION:"):
                target = max(target, int(float(s.split(":", 1)[1])))
                continue
            if s.startswith("#EXT-X-VERSION:"):
                version = max(version, int(s.split(":", 1)[1]))
                continue
            if s.startswith("#EXT-X-MAP:"):
                s = _URI_ATTR.sub(lambda m: f'URI="c{k}-{m.group(1).split("?", 1)[0].rsplit("/", 1)[-1]}"', s)
            elif not s.startswith("#"):
                s = f"c{k}-{s.split('?', 1)[0].rsplit('/', 1)[-1]}"
            part.append(s)
        if not any(not x.startswith("#") for x in part):
            continue
        if body:
            body.append("#EXT-X-DISCONTINUITY")
        body += part
    if not body:
        return None
    head = ["#EXTM3U", f"#EXT-X-VERSION:{version}", f"#EXT-X-TARGETDURATION:{target or 11}",
            "#EXT-X-PLAYLIST-TYPE:VOD", "#EXT-X-MEDIA-SEQUENCE:0"]
    return "\n".join(head + body + ["#EXT-X-ENDLIST"]) + "\n"


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
        self.clock = time.time
        self._vod_kept: dict[tuple[str, int, int], tuple[float, str | None]] = {}

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

    async def _part_playlist(self, camera: str, a: int, b: int) -> str | None:
        """Frigate's playlist for one part; None when nothing is recorded there. One retry.
        A part that ended VOD_SETTLED seconds ago is kept for VOD_KEEP seconds."""
        now = self.clock()
        kept = self._vod_kept.get((camera, a, b))
        if kept and now - kept[0] < VOD_KEEP:
            return kept[1]
        text: str | None = None
        for attempt in (1, 2):
            r = await self._send("GET", f"/vod/{camera}/start/{a}/end/{b}/index.m3u8", timeout=30.0)
            if r.status_code == 404:
                break
            if r.status_code == 200:
                text = r.text
                break
            if attempt == 2:
                raise RecorderUnavailable(f"vod part {a}-{b}: HTTP {r.status_code}")
        if b <= now - VOD_SETTLED:
            if len(self._vod_kept) >= VOD_KEEP_MAX:
                for key in sorted(self._vod_kept, key=lambda k: self._vod_kept[k][0])[:VOD_KEEP_MAX // 10]:
                    del self._vod_kept[key]
            self._vod_kept[(camera, a, b)] = (now, text)
        return text

    async def media(self, camera: str, start: float, end: float, piece: str) -> Media:
        if not PIECE.match(piece):
            raise NotRecorded(piece)
        parts = vod_parts(start, end)
        if len(parts) > 1:
            if piece == self.entry:
                gate = asyncio.Semaphore(VOD_PARALLEL)

                async def one(a: int, b: int):
                    async with gate:
                        return await self._part_playlist(camera, a, b)
                text = join_playlists(list(await asyncio.gather(*(one(a, b) for a, b in parts))))
                if text is None:
                    raise NotRecorded(piece)
                data = text.encode()

                async def body():
                    yield data
                return Media(body(), content_type(piece), len(data))
            m = _PART_PIECE.match(piece)
            if not m or int(m.group(1)) >= len(parts) or not PIECE.match(m.group(2)):
                raise NotRecorded(piece)
            a, b = parts[int(m.group(1))]
            piece_name = m.group(2)
            out = await self._stream(f"/vod/{camera}/start/{a}/end/{b}/{piece_name}")
            out.content_type = content_type(piece_name)
            return out
        out = await self._stream(f"/vod/{camera}/start/{math.floor(start)}/end/{math.ceil(end)}/{piece}")
        out.content_type = content_type(piece)
        return out

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
