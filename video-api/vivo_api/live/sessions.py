"""Live sessions: limits, one offer each, renewal with fresh grants, and closing (contract 6)."""
from __future__ import annotations

import asyncio
import secrets
import time
from dataclasses import dataclass, field

from ..errors import ApiError
from ..upstream import UpstreamUnavailable
from .gate import Gate, parse_remote_addr
from .turn import ice_servers

USER_AGENT = "vivo-video-api/{sid}"


@dataclass
class LiveSession:
    sid: str
    camera: str
    stream: str
    sub: str
    site_id: str
    jti: str
    remote: bool
    client_ip: str
    created: float
    expires: float
    state: str = "new"                      # new, offered, mse, closed
    ws: object = field(default=None, repr=False)

    @property
    def src(self) -> str:
        return f"{self.camera}_{self.stream}"


class LiveManager:
    def __init__(self, ctx, gate: Gate | None = None):
        self.ctx = ctx
        self.gate = gate or Gate(ctx.settings.live_gate_dir, clock=ctx.clock)
        self.sessions: dict[str, LiveSession] = {}
        self._task: asyncio.Task | None = None
        self._releases: set[asyncio.Task] = set()
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------ bookkeeping
    def open_sessions(self) -> list[LiveSession]:
        return [s for s in self.sessions.values() if s.state != "closed"]

    def get(self, sid: str) -> LiveSession:
        s = self.sessions.get(sid)
        if s is None or s.state == "closed" or s.expires <= self.ctx.clock():
            raise ApiError(404, "not_found")
        return s

    async def create(self, grant, camera: str, stream: str, remote: bool, client_ip: str) -> LiveSession:
        st = self.ctx.settings
        async with self._lock:
            open_ = self.open_sessions()
            if remote and sum(1 for s in open_ if s.remote) >= st.live_site_remote_max:
                self.ctx.audit.write("live_start", "denied", "site_remote_limit", grant=grant, camera_id=camera,
                                     client_ip=client_ip, remote=remote)
                raise ApiError(429, "limit_reached", "The site's limit of remote live streams is reached.",
                               limit="site_remote")
            if sum(1 for s in open_ if s.camera == camera) >= st.live_camera_max:
                self.ctx.audit.write("live_start", "denied", "camera_viewer_limit", grant=grant, camera_id=camera,
                                     client_ip=client_ip, remote=remote)
                raise ApiError(429, "limit_reached", "This camera's limit of viewers is reached.",
                               limit="camera_viewers")
            now = self.ctx.clock()
            s = LiveSession(sid="ls_" + secrets.token_urlsafe(16), camera=camera, stream=stream, sub=grant.sub,
                            site_id=grant.site_id, jti=grant.jti, remote=remote, client_ip=client_ip,
                            created=now, expires=grant.exp)
            self.sessions[s.sid] = s
        self.ctx.audit.write("live_start", grant=grant, camera_id=camera, object_id=s.sid, stream=stream,
                             client_ip=client_ip, remote=remote)
        return s

    def ice(self, s: LiveSession) -> list[dict]:
        st = self.ctx.settings
        return ice_servers(st.stun_urls, st.turn_urls, st.turn_secret, s.sid, st.turn_ttl, self.ctx.clock())

    async def offer(self, s: LiveSession, sdp: str) -> str:
        if s.state != "new":
            raise ApiError(409, "conflict", "This session already has an offer. Start a new session.")
        s.state = "offered"
        try:
            r = await self.ctx.go2rtc.client.post(
                "/api/webrtc", params={"src": s.src}, json={"type": "offer", "sdp": sdp},
                headers={"User-Agent": USER_AGENT.format(sid=s.sid)}, timeout=15.0)
        except Exception:
            s.state = "new"
            raise ApiError(502, "upstream_unavailable") from None
        if r.status_code not in (200, 201):
            text = r.text.lower()
            s.state = "new"
            if "codec" in text:
                raise ApiError(422, "codec_unsupported")
            raise ApiError(502, "upstream_unavailable")
        try:
            body = r.json()
            answer = body.get("sdp") if isinstance(body, dict) else None
        except ValueError:
            answer = r.text
        if not answer or "v=0" not in answer:
            s.state = "new"
            raise ApiError(502, "upstream_unavailable")
        return answer

    def renew(self, s: LiveSession, grant) -> float:
        s.expires = max(s.expires, grant.exp)
        return s.expires

    async def cut(self, s: LiveSession) -> str:
        """Stop go2rtc's video to this session's viewer. Returns what was done, for the audit log."""
        if s.ws is not None:
            try:
                await s.ws.close(code=4001, reason="session closed")
            except Exception:
                pass
            return "mse_closed"
        if s.state != "offered":
            return "no_peer"
        try:
            info = await self.ctx.go2rtc.stream(s.src)
        except UpstreamUnavailable:
            return "go2rtc_unavailable"
        # Match on the session id only: go2rtc masks any text equal to one of its secrets in its API
        # output (a go2rtc user called "vivo" turns "vivo-video-api/..." into "***-video-api/..."),
        # and the random session id cannot be one of them.
        for c in (info or {}).get("consumers") or []:
            if isinstance(c, dict) and s.sid in str(c.get("user_agent") or ""):
                addr = parse_remote_addr(c.get("remote_addr"))
                if addr and self.gate.cut(*addr):
                    self._releases.add(asyncio.create_task(self._release_when_gone(s, addr)))
                    return "dropped"
                return "no_address"
        return "already_gone"

    async def _release_when_gone(self, s: LiveSession, addr: tuple[str, int], every: float = 1.0) -> None:
        """Lift the drop as soon as go2rtc has closed the viewer (about 5 to 10 s); the helper's
        own time limit lifts it in any case."""
        try:
            for _ in range(int(self.gate.hold / every)):
                await asyncio.sleep(every)
                try:
                    info = await self.ctx.go2rtc.stream(s.src)
                except UpstreamUnavailable:
                    continue
                except Exception as exc:                  # the helper's time limit still lifts the drop
                    print(f"live release {s.sid}: {type(exc).__name__}: {exc}", flush=True)
                    return
                if not any(isinstance(c, dict) and s.sid in str(c.get("user_agent") or "")
                           for c in (info or {}).get("consumers") or []):
                    self.gate.release(*addr)
                    return
        finally:
            self._releases.discard(asyncio.current_task())

    async def close(self, s: LiveSession, reason: str) -> None:
        if s.state == "closed":
            return
        how = await self.cut(s)
        s.state = "closed"
        self.ctx.audit.write("live_end", "allowed", reason, camera_id=s.camera, object_id=s.sid, sub=s.sub,
                             site_id=s.site_id, jti=s.jti, client_ip=s.client_ip, remote=s.remote, cut=how,
                             seconds=round(self.ctx.clock() - s.created))

    # ------------------------------------------------------------------ expiry
    async def sweep(self) -> None:
        now = self.ctx.clock()
        for s in list(self.sessions.values()):
            if s.state != "closed" and s.expires <= now:
                await self.close(s, "expired")
        # forget closed sessions after a while
        for sid, s in list(self.sessions.items()):
            if s.state == "closed" and now - s.expires > 600:
                del self.sessions[sid]

    async def _run(self) -> None:
        while True:
            try:
                await self.sweep()
            except Exception as exc:                      # keep sweeping whatever happens
                print(f"live sweep: {type(exc).__name__}: {exc}", flush=True)
            await asyncio.sleep(1.0)

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
        for s in self.open_sessions():
            await self.close(s, "api_stopping")
        for task in list(self._releases):
            task.cancel()
