"""M16: playback and seeking over HLS (contract section 7)."""
from __future__ import annotations

import gzip

from fastapi import APIRouter, Request, Response
from fastapi.responses import StreamingResponse

from ..context import ctx
from ..errors import ApiError
from ..models import Expiry, Playback, TimeRange
from ..playback import rewrite_playlist
from ..recorders.base import PIECE, NotRecorded, RecorderUnavailable, merge
from ..timeutil import fmt, window
from . import ERRORS

router = APIRouter(prefix="/v1", tags=["playback"])

MAX_PLAYLIST = 8 * 1024 * 1024
GZIP_FROM = 1024             # bytes; smaller playlists go as they are


@router.post("/cameras/{camera_id}/playback", status_code=201, response_model=Playback, responses=ERRORS,
             summary="Start playback of a time range (at most 24 h)")
async def start(request: Request, camera_id: str, body: TimeRange):
    c = ctx(request)
    g = c.authorize(request, camera_id, ("playback",))
    s, e = window(body.start, body.end, c.settings.timeline_max_window)
    try:
        segs = await c.recorder.segments(camera_id, s, e)
    except RecorderUnavailable:
        raise ApiError(502, "upstream_unavailable") from None
    recorded, _ = merge(segs, s, e, c.settings.gap_threshold)
    if not recorded:
        raise ApiError(404, "no_recording")
    p = c.playbacks.create(g, camera_id, s, e, c.client_ip(request), c.is_remote(request))
    return {"playback_id": p.pid, "camera_id": camera_id, "start": fmt(s), "end": fmt(e),
            "url": f"/v1/playback/{p.pid}/{c.recorder.entry}", "expires_at": fmt(p.expires),
            "renew_after_seconds": c.settings.live_renew_after,
            "recorded": [{"start": fmt(a), "end": fmt(b)} for a, b in recorded]}


@router.get("/playback/{playback_id}/{piece}", responses=ERRORS,
            summary="HLS playlist and pieces; the playback id is the credential")
async def piece(request: Request, playback_id: str, piece: str):
    c = ctx(request)
    p = c.playbacks.get(playback_id)
    if not PIECE.match(piece):
        raise ApiError(404, "not_found")
    try:
        media = await c.recorder.media(p.camera, p.start, p.end, piece)
    except NotRecorded:
        raise ApiError(404, "not_found") from None
    except RecorderUnavailable:
        raise ApiError(502, "upstream_unavailable") from None
    if piece.endswith(".m3u8"):
        data = bytearray()
        try:
            async for chunk in media.body:
                data += chunk
                if len(data) > MAX_PLAYLIST:
                    raise ApiError(502, "upstream_unavailable")
        finally:
            await media.body.aclose()           # closes the recorder's HTTP stream now, not at garbage collection
        text = rewrite_playlist(data.decode("utf-8", errors="replace")).encode()
        headers = {"Cache-Control": "no-store", "Vary": "Accept-Encoding"}
        # a day's playlist is about 300 KB of text and gzip makes it about a tenth: on a far or slow
        # link (a phone, the VPN) that saves several round trips before the first picture
        if len(text) > GZIP_FROM and "gzip" in request.headers.get("accept-encoding", "").lower():
            text = gzip.compress(text, compresslevel=6)
            headers["Content-Encoding"] = "gzip"
        return Response(text, media_type="application/vnd.apple.mpegurl", headers=headers)
    headers = {"Cache-Control": "private, max-age=3600"}
    if media.length is not None:
        headers["Content-Length"] = str(media.length)
    return StreamingResponse(media.body, media_type=media.content_type, headers=headers)


@router.post("/playback/{playback_id}/renew", response_model=Expiry, responses=ERRORS,
             summary="Keep the playback open with a fresh grant; a refused renewal closes it")
async def renew(request: Request, playback_id: str):
    c = ctx(request)
    p = c.playbacks.get(playback_id)
    try:
        g = c.authorize(request, p.camera, ("playback",), owner=p.sub)
    except ApiError:
        c.playbacks.close(p, "renewal_refused")
        raise
    return {"expires_at": fmt(c.playbacks.renew(p, g))}


@router.delete("/playback/{playback_id}", status_code=204, responses=ERRORS, summary="Close the playback")
async def close(request: Request, playback_id: str):
    c = ctx(request)
    p = c.playbacks.sessions.get(playback_id)
    if p is None or p.closed:
        return Response(status_code=204)
    c.authorize(request, p.camera, ("playback",), owner=p.sub)
    c.playbacks.close(p, "closed_by_client")
    return Response(status_code=204)
