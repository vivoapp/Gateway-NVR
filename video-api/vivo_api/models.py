"""Request and response bodies. They are the machine-readable contract (openapi.json)."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ErrorBody(BaseModel):
    code: str
    message: str


class ErrorResponse(BaseModel):
    error: ErrorBody
    limit: str | None = Field(None, description="Which limit was reached (429 only)")


class Health(BaseModel):
    status: Literal["ok"]
    api_version: str


class StreamRole(BaseModel):
    role: Literal["main", "sub"]


class Camera(BaseModel):
    camera_id: str
    name: str
    streams: list[StreamRole]


class CameraList(BaseModel):
    cameras: list[Camera]


class StreamStatus(BaseModel):
    role: Literal["main", "sub"]
    receiving: bool


class RecordingStatus(BaseModel):
    ok: bool
    newest_segment_end: str | None
    age_seconds: int | None


class CameraStatus(BaseModel):
    camera_id: str
    checked_at: str
    streams: list[StreamStatus]
    recording: RecordingStatus


class Period(BaseModel):
    start: str
    end: str


class Timeline(BaseModel):
    camera_id: str
    start: str
    end: str
    recorded: list[Period]
    gaps: list[Period]


class TimeRange(Strict):
    start: str = Field(..., examples=["2026-11-08T09:00:00Z"])
    end: str = Field(..., examples=["2026-11-08T10:00:00Z"])


class LiveRequest(Strict):
    stream: Literal["main", "sub"] = "sub"


class IceServer(BaseModel):
    urls: list[str]
    username: str | None = None
    credential: str | None = None


class LiveSession(BaseModel):
    session_id: str
    camera_id: str
    stream: Literal["main", "sub"]
    remote: bool
    expires_at: str
    renew_after_seconds: int
    ice_servers: list[IceServer]


class Sdp(Strict):
    sdp: str = Field(..., max_length=65536)


class Expiry(BaseModel):
    expires_at: str


class Playback(BaseModel):
    playback_id: str
    camera_id: str
    start: str
    end: str
    url: str
    expires_at: str
    renew_after_seconds: int
    recorded: list[Period]


class ClipQueued(BaseModel):
    clip_id: str
    camera_id: str
    status: Literal["queued", "running", "ready", "failed"]
    queue_position: int


class Clip(BaseModel):
    clip_id: str
    camera_id: str
    start: str
    end: str
    status: Literal["queued", "running", "ready", "failed"]
    progress: int
    size_bytes: int | None
    expires_at: str | None
    error: ErrorBody | None
