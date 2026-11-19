"""Clip export (M17, contract section 8).

At most CLIP_WORKERS clips are prepared at once; up to CLIP_QUEUE_MAX more wait. Each clip is
written as <id>.mp4.part, checked to be a whole MP4, then renamed. Finished clips are deleted after
CLIP_KEEP seconds, and the oldest first when the folder passes its size cap.
"""
from __future__ import annotations

import asyncio
import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path

from . import mp4
from .errors import ApiError
from .recorders.base import NotRecorded, RecorderUnavailable, merge
from .upstream import UpstreamUnavailable


@dataclass
class ClipJob:
    cid: str
    camera: str
    start: float
    end: float
    sub: str
    site_id: str
    jti: str
    client_ip: str
    created: float
    status: str = "queued"                  # queued, running, ready, failed
    progress: int = 0
    size: int | None = None
    error: dict | None = None
    finished: float | None = None
    task: asyncio.Task | None = field(default=None, repr=False)


class ClipManager:
    def __init__(self, ctx):
        self.ctx = ctx
        self.dir = Path(ctx.settings.data_dir) / "clips"
        self.jobs: dict[str, ClipJob] = {}
        self.queue: asyncio.Queue | None = None
        self._tasks: list[asyncio.Task] = []
        self._stopping = False
        self._lock = asyncio.Lock()

    def path(self, job: ClipJob) -> Path:
        return self.dir / f"{job.cid}.mp4"

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> None:
        if self._tasks:
            return
        self.queue = asyncio.Queue()
        try:                                    # clips from before a restart are unknown now
            self.dir.mkdir(parents=True, exist_ok=True)
            for p in self.dir.glob("cl_*"):
                p.unlink(missing_ok=True)
        except OSError:
            pass
        self._tasks = [asyncio.create_task(self._worker()) for _ in range(self.ctx.settings.clip_workers)]
        self._tasks.append(asyncio.create_task(self._sweeper()))

    async def stop(self) -> None:
        self._stopping = True
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        self._tasks = []
        for job in self.jobs.values():
            if job.status in ("queued", "running"):
                self._fail(job, "upstream_unavailable", "The API stopped before the clip was ready.")

    # ------------------------------------------------------------------ requests
    def queued(self) -> list[ClipJob]:
        return [j for j in self.jobs.values() if j.status == "queued"]

    async def create(self, grant, camera: str, start: float, end: float, client_ip: str) -> ClipJob:
        # One request at a time from the queue check to the enqueue: the checks in between wait on
        # other services, and two requests must not both see the last free place.
        async with self._lock:
            return await self._create(grant, camera, start, end, client_ip)

    async def _create(self, grant, camera: str, start: float, end: float, client_ip: str) -> ClipJob:
        st = self.ctx.settings
        if self.queue is None:
            self.start()
        if len(self.queued()) >= st.clip_queue_max:
            self.ctx.audit.write("clip_request", "denied", "queue_full", grant=grant, camera_id=camera,
                                 client_ip=client_ip)
            raise ApiError(429, "limit_reached", "Too many clips are waiting. Try again later.", limit="clip_queue")
        try:
            if await self.ctx.health.disk_at_limit():
                self.ctx.audit.write("clip_request", "denied", "disk_near_limit", grant=grant, camera_id=camera,
                                     client_ip=client_ip)
                raise ApiError(503, "disk_near_limit")
        except UpstreamUnavailable:
            pass                                 # the health service is down: do not block clips on it
        try:
            segs = await self.ctx.recorder.segments(camera, start, end)
        except RecorderUnavailable:
            raise ApiError(502, "upstream_unavailable") from None
        recorded, _ = merge(segs, start, end, st.gap_threshold)
        if not recorded:
            raise ApiError(404, "no_recording")
        job = ClipJob(cid="cl_" + secrets.token_urlsafe(16), camera=camera, start=start, end=end, sub=grant.sub,
                      site_id=grant.site_id, jti=grant.jti, client_ip=client_ip, created=self.ctx.clock())
        self.jobs[job.cid] = job
        self.queue.put_nowait(job.cid)
        from .timeutil import fmt
        self.ctx.audit.write("clip_request", grant=grant, camera_id=camera, object_id=job.cid,
                             start=fmt(start), end=fmt(end), client_ip=client_ip)
        return job

    def position(self, job: ClipJob) -> int:
        if job.status != "queued":
            return 0
        waiting = sorted(self.queued(), key=lambda j: j.created)
        return waiting.index(job) + 1

    def get(self, cid: str) -> ClipJob:
        job = self.jobs.get(cid)
        if job is None:
            raise ApiError(404, "not_found")
        return job

    def expires(self, job: ClipJob) -> float | None:
        return job.finished + self.ctx.settings.clip_keep if job.finished else None

    async def delete(self, job: ClipJob) -> None:
        self.jobs.pop(job.cid, None)
        if job.task and not job.task.done():
            job.task.cancel()
        for p in (self.path(job), self.path(job).with_suffix(".mp4.part")):
            try:
                p.unlink(missing_ok=True)
            except OSError:
                pass

    # ------------------------------------------------------------------ work
    async def _worker(self) -> None:
        while True:
            cid = await self.queue.get()
            job = self.jobs.get(cid)
            if job is None or job.status != "queued":
                continue
            job.task = asyncio.create_task(self._run(job))
            try:
                await job.task
            except asyncio.CancelledError:
                if self._stopping:                # the worker itself is stopping
                    raise                         # (a deleted clip only cancels its own task)
            except Exception as exc:
                print(f"clip {cid}: {type(exc).__name__}: {exc}", flush=True)

    async def _run(self, job: ClipJob) -> None:
        job.status = "running"
        final = self.path(job)
        part = final.with_suffix(".mp4.part")
        written = 0
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            src = await self.ctx.recorder.clip(job.camera, job.start, job.end)
            with open(part, "wb") as f:
                async for chunk in src.body:
                    f.write(chunk)
                    written += len(chunk)
                    if src.expected_bytes:
                        job.progress = min(99, int(100 * written / src.expected_bytes))
                f.flush()
                os.fsync(f.fileno())
            bad = mp4.problem(part) if written else "empty"
            if bad:
                raise RecorderUnavailable(f"clip is not a whole MP4: {bad}")
            os.replace(part, final)
            job.size, job.progress, job.status = written, 100, "ready"
        except NotRecorded:
            self._fail(job, "no_recording", "Nothing is recorded in this range.")
        except RecorderUnavailable:
            self._fail(job, "upstream_unavailable", "The recorder did not deliver the clip.")
        except OSError:
            self._fail(job, "upstream_unavailable", "The clip could not be written.")
        finally:
            job.finished = self.ctx.clock()
            try:
                part.unlink(missing_ok=True)
            except OSError:
                pass

    def _fail(self, job: ClipJob, code: str, message: str) -> None:
        job.status, job.error = "failed", {"code": code, "message": message}

    async def sweep(self) -> None:
        now = self.ctx.clock()
        keep = self.ctx.settings.clip_keep
        for job in list(self.jobs.values()):
            if job.status in ("ready", "failed") and job.finished and now - job.finished > keep:
                await self.delete(job)
        ready = sorted((j for j in self.jobs.values() if j.status == "ready"), key=lambda j: j.finished or 0)
        total = sum(j.size or 0 for j in ready)
        while ready and total > self.ctx.settings.clip_store_max_bytes:
            oldest = ready.pop(0)
            total -= oldest.size or 0
            await self.delete(oldest)

    async def _sweeper(self) -> None:
        while True:
            await asyncio.sleep(30)
            try:
                await self.sweep()
            except Exception as exc:
                print(f"clip sweep: {type(exc).__name__}: {exc}", flush=True)
