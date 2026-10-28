"""Shared test helpers: a real S3-compatible HTTP server (moto) that can be switched off."""
from __future__ import annotations

import hashlib
import socket
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import boto3
import pytest
from botocore.config import Config as BotoConfig
from moto.server import ThreadedMotoServer

from segment_uploader.config import Config
from segment_uploader.service import Service
from segment_uploader.storage import S3Storage


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class S3Server:
    """A local S3 server. stop() makes connections fail, like a real network outage."""

    def __init__(self):
        self.port = free_port()
        # moto keeps its data for the whole test run, so every test gets its own bucket
        self.bucket = f"test-{uuid.uuid4().hex[:12]}"
        self.url = f"http://127.0.0.1:{self.port}"
        self._srv = None
        self.start()
        self.client = boto3.client(
            "s3", endpoint_url=self.url, region_name="us-east-1",
            aws_access_key_id="test", aws_secret_access_key="test",
            config=BotoConfig(s3={"addressing_style": "path"}))
        self.client.create_bucket(Bucket=self.bucket)

    def start(self):
        self._srv = ThreadedMotoServer(ip_address="127.0.0.1", port=self.port, verbose=False)
        self._srv.start()

    def stop(self):
        self._srv.stop()
        self._srv._server.server_close()      # really close the port -> "connection refused"
        self._srv = None

    def objects(self) -> dict[str, bytes]:
        out = {}
        for page in self.client.get_paginator("list_objects_v2").paginate(Bucket=self.bucket):
            for o in page.get("Contents", []):
                out[o["Key"]] = self.client.get_object(Bucket=self.bucket, Key=o["Key"])["Body"].read()
        return out


@pytest.fixture
def s3():
    server = S3Server()
    yield server
    if server._srv:
        server.stop()


@pytest.fixture
def cfg(tmp_path, s3) -> Config:
    watch = tmp_path / "recordings"
    watch.mkdir()
    return Config(
        watch_dir=watch, state_db=tmp_path / "state.db", bucket=s3.bucket, endpoint_url=s3.url,
        access_key="test", secret_key="test", key_prefix="site-1",
        backoff_initial=1.0, backoff_max=8.0,
        connect_timeout=2, read_timeout=5)


class FakeClock:
    """Manual clock so retry/backoff tests do not need to sleep."""

    def __init__(self, t: float = 1_000_000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


def make_service(cfg, clock, storage=None) -> Service:
    # jitter=1.0 -> backoff delay is exactly the computed value (deterministic tests)
    return Service(cfg, storage=storage or S3Storage(cfg), clock=clock, jitter=lambda: 1.0)


@pytest.fixture
def new_service(cfg, clock):
    created = []

    def factory(storage=None):
        svc = make_service(cfg, clock, storage)
        created.append(svc)
        return svc

    yield factory
    for svc in created:
        svc.store.close()


def write_segment(cfg: Config, rel: str, data: bytes) -> Path:
    path = cfg.watch_dir / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# Segment names follow the agreed format: <camera_id>/<YYYY-MM-DD>/<camera_id>_<YYYYMMDDTHHMMSS>Z.mp4
BASE = datetime(2026, 10, 20, 10, 0, 0, tzinfo=timezone.utc)


def seg_rel(camera: str = "cam01", n: int = 0, base: datetime = BASE) -> str:
    """Relative path of segment number n (one per minute, UTC) of a camera."""
    start = base + timedelta(minutes=n)
    return f"{camera}/{start:%Y-%m-%d}/{camera}_{start:%Y%m%dT%H%M%S}Z.mp4"


def seg_key(camera: str = "cam01", n: int = 0, prefix: str = "site-1", base: datetime = BASE) -> str:
    return f"{prefix}/{seg_rel(camera, n, base)}"


def step(svc, n: int = 1):
    """Run the service a few passes (scan, then upload what is due)."""
    last = {}
    for _ in range(n):
        last = svc.run_once()
    return last
