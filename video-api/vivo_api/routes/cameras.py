"""M14: health, cameras, status and timeline (contract section 5)."""
from __future__ import annotations

from fastapi import APIRouter, Query, Request

from .. import API_VERSION
from ..context import ctx
from ..errors import ApiError
from ..models import CameraList, Camera, CameraStatus, Health, Timeline
from ..recorders.base import RecorderUnavailable, merge
from ..timeutil import fmt, window
from ..upstream import UpstreamUnavailable
from . import ERRORS

router = APIRouter(prefix="/v1")


@router.get("/health", response_model=Health, tags=["health"],
            summary="The API process is running (no grant needed)")
async def health():
    return {"status": "ok", "api_version": API_VERSION}


@router.get("/cameras", response_model=CameraList, tags=["cameras"], responses=ERRORS,
            summary="Cameras the grant covers")
async def cameras(request: Request):
    c = ctx(request)
    g = c.authorize(request, None, ("live", "playback", "export"))
    out = []
    for cid in c.inventory.ids():
        if cid in g.cams:
            out.append(c.inventory.public(c.inventory.get(cid)))
    return {"cameras": out}


@router.get("/cameras/{camera_id}", response_model=Camera, tags=["cameras"], responses=ERRORS,
            summary="One camera")
async def camera(request: Request, camera_id: str):
    c = ctx(request)
    c.authorize(request, camera_id, ("live", "playback", "export"))
    return c.inventory.public(c.inventory.get(camera_id))


@router.get("/cameras/{camera_id}/status", response_model=CameraStatus, tags=["cameras"], responses=ERRORS,
            summary="Whether the camera is streaming and recording")
async def status(request: Request, camera_id: str):
    c = ctx(request)
    c.authorize(request, camera_id, ("live", "playback"))
    if not c.watch.ready():
        try:                                        # first request after start: sample now
            await c.watch.poll_once()
        except UpstreamUnavailable:
            raise ApiError(502, "upstream_unavailable") from None
    try:
        rec = await c.health.recording(camera_id)
    except UpstreamUnavailable:
        raise ApiError(502, "upstream_unavailable") from None
    return {
        "camera_id": camera_id,
        "checked_at": fmt(c.clock()),
        "streams": [{"role": r, "receiving": c.watch.receiving(f"{camera_id}_{r}")} for r in ("main", "sub")],
        "recording": {"ok": rec["ok"],
                      "newest_segment_end": fmt(rec["newest_segment_end"]) if rec["newest_segment_end"] else None,
                      "age_seconds": rec["age_seconds"]},
    }


@router.get("/cameras/{camera_id}/timeline", response_model=Timeline, tags=["cameras"], responses=ERRORS,
            summary="Recorded periods and gaps in a time window (at most 24 h)")
async def timeline(request: Request, camera_id: str,
                   start: str = Query(..., examples=["2026-11-08T09:00:00Z"]),
                   end: str = Query(..., examples=["2026-11-08T10:00:00Z"])):
    c = ctx(request)
    extra = set(request.query_params) - {"start", "end"}
    if extra:
        raise ApiError(400, "invalid_argument", f"Unknown parameter {sorted(extra)[0]}.")
    c.authorize(request, camera_id, ("playback",))
    s, e = window(start, end, c.settings.timeline_max_window)
    try:
        segs = await c.recorder.segments(camera_id, s, e)
    except RecorderUnavailable:
        raise ApiError(502, "upstream_unavailable") from None
    recorded, gaps = merge(segs, s, e, c.settings.gap_threshold)
    return {"camera_id": camera_id, "start": fmt(s), "end": fmt(e),
            "recorded": [{"start": fmt(a), "end": fmt(b)} for a, b in recorded],
            "gaps": [{"start": fmt(a), "end": fmt(b)} for a, b in gaps]}
