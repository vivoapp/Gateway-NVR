"""go2rtc and the recorder's health service (M22), as the API uses them.

go2rtc is the only process that opens a camera; the API reads stream status from it and passes
WebRTC offers to it. The health service is the gateway's one definition of "recording is healthy".
"""
from __future__ import annotations

import asyncio
import calendar
import time

import httpx


class UpstreamUnavailable(Exception):
    pass


def parse_health_time(value: str | None) -> float | None:
    """'2026-11-08 10:15:00 UTC' (the health service's format) -> Unix seconds."""
    if not value:
        return None
    try:
        return float(calendar.timegm(time.strptime(value, "%Y-%m-%d %H:%M:%S UTC")))
    except (TypeError, ValueError):
        return None


class HealthSource:
    def __init__(self, url: str, transport: httpx.AsyncBaseTransport | None = None, ttl: float = 5.0,
                 clock=time.monotonic):
        self.url = url
        self.client = httpx.AsyncClient(timeout=5.0, transport=transport)
        self.ttl = ttl
        self.clock = clock
        self._cache: tuple[float, dict] | None = None

    async def aclose(self) -> None:
        await self.client.aclose()

    async def snapshot(self) -> dict:
        now = self.clock()
        if self._cache and now - self._cache[0] < self.ttl:
            return self._cache[1]
        try:
            r = await self.client.get(self.url)
            data = r.json() if r.status_code == 200 else None
        except (httpx.HTTPError, ValueError):
            data = None
        if not isinstance(data, dict):
            raise UpstreamUnavailable("health")
        self._cache = (now, data)
        return data

    async def recording(self, camera: str) -> dict:
        snap = await self.snapshot()
        cam = (snap.get("cameras") or {}).get(camera) or {}
        taken = parse_health_time(snap.get("time")) or time.time()
        age = cam.get("main_segment_age_s")
        bad = {f"{camera}/main", camera}
        alerts = [a for a in snap.get("alerts") or []
                  if a.get("alert") in ("recording_gap", "camera_offline") and a.get("target") in bad]
        if (snap.get("recorder") or {}).get("frigate_up") is False:
            alerts.append({"alert": "recorder_down"})
        out = {"ok": not alerts and age is not None, "newest_segment_end": None, "age_seconds": None}
        if age is not None:
            out["newest_segment_end"] = taken - float(age)
            out["age_seconds"] = round(float(age) + max(0.0, time.time() - taken))
        return out

    async def disk_at_limit(self) -> bool:
        snap = await self.snapshot()
        return any(a.get("alert") in ("disk_near_limit", "disk_missing") and a.get("level") == "critical"
                   for a in snap.get("alerts") or [])


class Go2rtc:
    def __init__(self, url: str, user: str = "", password: str = "",
                 transport: httpx.AsyncBaseTransport | None = None):
        auth = (user, password) if user else None
        self.client = httpx.AsyncClient(base_url=url.rstrip("/"), auth=auth, transport=transport,
                                        timeout=httpx.Timeout(5.0, read=15.0))

    async def aclose(self) -> None:
        await self.client.aclose()

    async def _call(self, method: str, path: str, **kw) -> httpx.Response:
        try:
            return await self.client.request(method, path, **kw)
        except httpx.HTTPError as exc:
            raise UpstreamUnavailable(f"go2rtc {path.split('?')[0]}: {type(exc).__name__}") from None

    async def streams(self) -> dict:
        r = await self._call("GET", "/api/streams")
        if r.status_code != 200:
            raise UpstreamUnavailable(f"go2rtc streams: HTTP {r.status_code}")
        try:
            data = r.json()
        except ValueError:                      # go2rtc has answered with plain text before
            raise UpstreamUnavailable("go2rtc streams: not JSON") from None
        return data if isinstance(data, dict) else {}

    async def stream(self, name: str) -> dict | None:
        r = await self._call("GET", "/api/streams", params={"src": name})
        if r.status_code == 404:
            return None
        if r.status_code != 200:
            raise UpstreamUnavailable(f"go2rtc stream: HTTP {r.status_code}")
        try:
            data = r.json()
        except ValueError:
            return None
        return data if isinstance(data, dict) else None

    async def webrtc(self, src: str, sdp: str) -> httpx.Response:
        """POST the offer; go2rtc answers with the complete SDP (all its candidates included)."""
        return await self._call("POST", "/api/webrtc", params={"src": src},
                                json={"type": "offer", "sdp": sdp}, timeout=15.0)

    async def patch_stream(self, name: str, src: str) -> None:
        """Create or change a stream in memory only (PATCH does not write go2rtc.yaml)."""
        r = await self._call("PATCH", "/api/streams", params={"name": name, "src": src})
        if r.status_code >= 300:
            raise UpstreamUnavailable(f"go2rtc patch: HTTP {r.status_code}")

    async def delete_stream(self, name: str) -> None:
        r = await self._call("DELETE", "/api/streams", params={"src": name})
        if r.status_code >= 300 and r.status_code != 404:
            raise UpstreamUnavailable(f"go2rtc delete: HTTP {r.status_code}")


class StreamWatch:
    """Polls go2rtc every few seconds; a stream is receiving when its bytes grew recently.

    go2rtc's byte counter restarts at 0 when a producer reconnects (and its id changes), so a
    drop in bytes or a new producer id counts as growth, not as a stall.
    """

    def __init__(self, go2rtc: Go2rtc, interval: float = 3.0, fresh: float = 10.0, clock=time.monotonic):
        self.go2rtc = go2rtc
        self.interval = interval
        self.fresh = fresh
        self.clock = clock
        self.state: dict[str, tuple] = {}          # stream -> (ids, bytes, last growth)
        self.polled = 0
        self.last_ok: float | None = None
        self._task: asyncio.Task | None = None

    def update(self, streams: dict, now: float) -> None:
        for name, info in streams.items():
            producers = (info or {}).get("producers") or [] if isinstance(info, dict) else []
            ids = tuple(sorted(str(p.get("id", p.get("url", ""))) for p in producers if isinstance(p, dict)))
            total = sum(int(p.get("bytes_recv") or 0) for p in producers if isinstance(p, dict))
            prev = self.state.get(name)
            grew = prev is not None and total > 0 and (ids != prev[0] or total != prev[1])
            last = now if grew else (prev[2] if prev else None)
            self.state[name] = (ids, total, last)
        self.polled += 1
        self.last_ok = now

    async def poll_once(self) -> None:
        self.update(await self.go2rtc.streams(), self.clock())

    async def _run(self) -> None:
        while True:
            try:
                await self.poll_once()
            except UpstreamUnavailable:
                pass
            await asyncio.sleep(self.interval)

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    def ready(self) -> bool:
        return self.polled >= 2 and self.last_ok is not None and self.clock() - self.last_ok < self.fresh

    def receiving(self, stream: str) -> bool:
        st = self.state.get(stream)
        return bool(st and st[2] is not None and self.clock() - st[2] < self.fresh)
