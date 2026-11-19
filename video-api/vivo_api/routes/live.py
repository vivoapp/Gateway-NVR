"""M15: live view (contract section 6)."""
from __future__ import annotations

import asyncio
import base64

from fastapi import APIRouter, Request, Response, WebSocket

from ..context import ctx
from ..errors import ApiError
from ..live.sessions import USER_AGENT
from ..models import Expiry, LiveRequest, LiveSession, Sdp
from ..timeutil import fmt
from . import ERRORS

router = APIRouter(prefix="/v1", tags=["live"])


@router.post("/cameras/{camera_id}/live", status_code=201, response_model=LiveSession, responses=ERRORS,
             summary="Start a live session (phones: ask for sub; main only in full screen)")
async def start(request: Request, camera_id: str, body: LiveRequest | None = None):
    c = ctx(request)
    g = c.authorize(request, camera_id, ("live",))
    stream = (body or LiveRequest()).stream
    s = await c.live.create(g, camera_id, stream, c.is_remote(request), c.client_ip(request))
    return {"session_id": s.sid, "camera_id": camera_id, "stream": stream, "remote": s.remote,
            "expires_at": fmt(s.expires), "renew_after_seconds": c.settings.live_renew_after,
            "ice_servers": c.live.ice(s)}


@router.post("/live/{session_id}/offer", response_model=Sdp, responses=ERRORS,
             summary="Send the WebRTC offer; get the gateway's complete answer")
async def offer(request: Request, session_id: str, body: Sdp):
    c = ctx(request)
    s = c.live.get(session_id)
    c.authorize(request, s.camera, ("live",), owner=s.sub)
    return {"sdp": await c.live.offer(s, body.sdp)}


@router.post("/live/{session_id}/renew", response_model=Expiry, responses=ERRORS,
             summary="Keep the session open with a fresh grant; a refused renewal closes it")
async def renew(request: Request, session_id: str):
    c = ctx(request)
    s = c.live.get(session_id)
    try:
        g = c.authorize(request, s.camera, ("live",), owner=s.sub)
    except ApiError:
        await c.live.close(s, "renewal_refused")
        raise
    return {"expires_at": fmt(c.live.renew(s, g))}


@router.delete("/live/{session_id}", status_code=204, responses=ERRORS, summary="Close the session")
async def close(request: Request, session_id: str):
    c = ctx(request)
    s = c.live.sessions.get(session_id)
    if s is None or s.state == "closed":
        return Response(status_code=204)
    c.authorize(request, s.camera, ("live",), owner=s.sub)
    await c.live.close(s, "closed_by_client")
    return Response(status_code=204)


@router.websocket("/live/{session_id}/mse")
async def mse(ws: WebSocket, session_id: str):
    """Fallback on the building network: relays go2rtc's MSE stream. The session id is the credential."""
    c = ws.app.state.ctx
    s = c.live.sessions.get(session_id)
    if s is None or s.state != "new" or s.remote or s.expires <= c.clock():
        await ws.close(code=4004)
        return
    origin = ws.headers.get("origin")
    allowed = c.settings.cors_origins
    if origin and "*" not in allowed and origin not in allowed:   # a page from another site; apps send none
        await ws.close(code=4003)
        return
    from websockets.asyncio.client import connect

    s.state = "mse"
    await ws.accept()
    s.ws = ws
    st = c.settings
    url = st.go2rtc_url.replace("http://", "ws://", 1).rstrip("/") + f"/api/ws?src={s.src}"
    headers = {}
    if st.go2rtc_user:
        token = base64.b64encode(f"{st.go2rtc_user}:{st.go2rtc_password}".encode()).decode()
        headers["Authorization"] = f"Basic {token}"
    try:
        async with connect(url, additional_headers=headers, user_agent_header=USER_AGENT.format(sid=s.sid),
                           max_size=None, open_timeout=10) as up:
            async def downstream():
                async for msg in up:
                    if isinstance(msg, bytes):
                        await ws.send_bytes(msg)
                    else:
                        await ws.send_text(msg)

            async def upstream():
                while True:
                    m = await ws.receive()
                    if m["type"] == "websocket.disconnect":
                        return
                    if m.get("text") is not None:
                        await up.send(m["text"])

            async def expiry():                 # the sweeper also closes it; this stops it on time by itself
                while s.expires > c.clock() and s.state != "closed":
                    await asyncio.sleep(min(1.0, max(0.05, s.expires - c.clock())))

            tasks = [asyncio.create_task(downstream()), asyncio.create_task(upstream()), asyncio.create_task(expiry())]
            _, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for t in pending:
                t.cancel()
    except Exception:
        pass
    finally:
        s.ws = None
        if s.state != "closed":
            await c.live.close(s, "closed_by_client")
        try:
            await ws.close()
        except Exception:
            pass
