"""M17: clip export and download (contract section 8)."""
from __future__ import annotations

from fastapi import APIRouter, Request, Response
from fastapi.responses import FileResponse

from ..context import ctx
from ..errors import ApiError
from ..models import Clip, ClipQueued, TimeRange
from ..timeutil import compact, fmt, window
from . import ERRORS

router = APIRouter(prefix="/v1", tags=["clips"])


def _view(c, job) -> dict:
    exp = c.clips.expires(job)
    return {"clip_id": job.cid, "camera_id": job.camera, "start": fmt(job.start), "end": fmt(job.end),
            "status": job.status, "progress": job.progress, "size_bytes": job.size,
            "expires_at": fmt(exp) if exp else None, "error": job.error}


@router.post("/cameras/{camera_id}/clips", status_code=202, response_model=ClipQueued, responses=ERRORS,
             summary="Ask for a clip (at most 30 minutes)")
async def create(request: Request, camera_id: str, body: TimeRange):
    c = ctx(request)
    g = c.authorize(request, camera_id, ("export",))
    s, e = window(body.start, body.end, c.settings.clip_max_seconds)
    job = await c.clips.create(g, camera_id, s, e, c.client_ip(request))
    return {"clip_id": job.cid, "camera_id": camera_id, "status": job.status,
            "queue_position": c.clips.position(job)}


@router.get("/clips/{clip_id}", response_model=Clip, responses=ERRORS, summary="Clip status and progress")
async def status(request: Request, clip_id: str):
    c = ctx(request)
    job = c.clips.get(clip_id)
    c.authorize(request, job.camera, ("export",), owner=job.sub)
    return _view(c, job)


@router.get("/clips/{clip_id}/download", responses={**ERRORS, 200: {"content": {"video/mp4": {}}}},
            summary="Download the finished clip")
async def download(request: Request, clip_id: str):
    c = ctx(request)
    job = c.clips.get(clip_id)
    g = c.authorize(request, job.camera, ("export",), owner=job.sub)
    if job.status != "ready":
        raise ApiError(409, "conflict", f"The clip is {job.status}.")
    path = c.clips.path(job)
    if not path.exists():
        raise ApiError(404, "not_found")
    c.audit.write("clip_download", grant=g, camera_id=job.camera, object_id=job.cid,
                  client_ip=c.client_ip(request), bytes=job.size)
    name = f"{job.camera}_{compact(job.start)}_{compact(job.end)}.mp4"
    return FileResponse(path, media_type="video/mp4", filename=name)


@router.delete("/clips/{clip_id}", status_code=204, responses=ERRORS, summary="Cancel or delete a clip")
async def delete(request: Request, clip_id: str):
    c = ctx(request)
    job = c.clips.jobs.get(clip_id)
    if job is None:
        return Response(status_code=204)
    c.authorize(request, job.camera, ("export",), owner=job.sub)
    await c.clips.delete(job)
    return Response(status_code=204)
