"""Shared fixtures: a temporary gateway (keys, camera list, data folder), fakes for the recorder,
go2rtc and the health service, and a clock the tests move by hand."""
from __future__ import annotations

import json
import struct
import sys
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import grant_issuer  # noqa: E402
from vivo_api.config import Settings  # noqa: E402
from vivo_api.main import create_app  # noqa: E402
from vivo_api.recorders.base import ClipSource, Media, NotRecorded, RecorderUnavailable, Segment  # noqa: E402
from vivo_api.upstream import Go2rtc  # noqa: E402

T0 = 1794120000.0            # 2026-11-08 06:40:00 UTC
AUD = "vivo-gw:test-gw"
LAN_CLIENT = ("192.168.30.50", 50000)
REMOTE_CLIENT = ("127.0.0.1", 50000)      # remote requests arrive on loopback through the tunnel


class Clock:
    def __init__(self, t: float = T0):
        self.t = t

    def __call__(self) -> float:
        return self.t


def box(kind: bytes, payload: bytes = b"") -> bytes:
    return struct.pack(">I4s", 8 + len(payload), kind) + payload


def tiny_mp4(n: int = 1000) -> bytes:
    return box(b"ftyp", b"isom\x00\x00\x02\x00") + box(b"moov", b"\x00" * 16) + box(b"mdat", b"\x01" * n)


class FakeRecorder:
    name = "fake"
    entry = "index.m3u8"

    def __init__(self):
        self.segs: dict[str, list[Segment]] = {}
        self.pieces: dict[str, bytes] = {}
        self.clip_data = tiny_mp4()
        self.clip_expected: int | None = None
        self.down = False
        self.clip_gate = None                   # an asyncio.Event a test can hold clips on

    def record(self, cam: str, start: float, end: float, step: float = 10.0):
        t = start
        while t < end:
            self.segs.setdefault(cam, []).append(Segment(t, min(t + step, end)))
            t += step

    async def aclose(self):
        pass

    async def health(self):
        return not self.down

    async def cameras(self):
        return sorted(self.segs)

    async def segments(self, cam, start, end):
        if self.down:
            raise RecorderUnavailable("down")
        return [s for s in self.segs.get(cam, []) if s.end > start and s.start < end]

    async def media(self, cam, start, end, piece):
        if self.down:
            raise RecorderUnavailable("down")
        if piece not in self.pieces:
            raise NotRecorded(piece)
        data = self.pieces[piece]

        async def body():
            yield data
        ctype = "application/vnd.apple.mpegurl" if piece.endswith(".m3u8") else "video/iso.segment"
        return Media(body(), ctype, len(data))

    async def clip(self, cam, start, end):
        if self.down:
            raise RecorderUnavailable("down")
        if not [s for s in self.segs.get(cam, []) if s.end > start and s.start < end]:
            raise NotRecorded("clip")
        data, gate = self.clip_data, self.clip_gate

        async def body():
            if gate is not None:
                await gate.wait()
            for i in range(0, len(data), 100):
                yield data[i:i + 100]
        return ClipSource(body(), self.clip_expected or len(data))


class FakeHealth:
    def __init__(self):
        self.snap = {"time": "2026-11-08 05:20:00 UTC", "cameras": {}, "alerts": [], "recorder": {"frigate_up": True}}
        self.down = False

    async def aclose(self):
        pass

    async def snapshot(self):
        from vivo_api.upstream import UpstreamUnavailable
        if self.down:
            raise UpstreamUnavailable("health")
        return self.snap

    async def recording(self, camera):
        from vivo_api.upstream import HealthSource
        return await HealthSource.recording(self, camera)

    async def disk_at_limit(self):
        from vivo_api.upstream import HealthSource
        return await HealthSource.disk_at_limit(self)


class FakeGo2rtc:
    """go2rtc's HTTP API as an httpx mock transport."""

    def __init__(self):
        self.streams: dict = {}
        self.requests: list[httpx.Request] = []
        self.answer = {"status": 200, "json": {"type": "answer", "sdp": "v=0\r\no=- 1 1 IN IP4 0.0.0.0\r\n"}}

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path == "/api/streams" and request.method == "GET":
            src = request.url.params.get("src")
            if src:
                return httpx.Response(200, json=self.streams.get(src)) if src in self.streams else httpx.Response(404)
            return httpx.Response(200, json=self.streams)
        if request.url.path == "/api/webrtc":
            a = self.answer
            if "json" in a:
                return httpx.Response(a["status"], json=a["json"])
            return httpx.Response(a["status"], text=a.get("text", ""))
        return httpx.Response(404)

    def client(self) -> Go2rtc:
        return Go2rtc("http://go2rtc.test", "u", "p", transport=httpx.MockTransport(self.handler))


CAMERAS_YAML = """
site: {}
cameras:
  - {id: cam01, name: VIVO-CAM-01, host: 192.168.30.201}
  - {id: cam02, name: VIVO-CAM-02, host: 192.168.30.202}
  - {id: cam03, name: VIVO-CAM-03, host: 192.168.30.203}
"""


class Env:
    """One test gateway: app, client, keys, fakes and clock."""

    def __init__(self, tmp: Path, **overrides):
        self.tmp = tmp
        self.clock = Clock()
        keys = tmp / "keys"
        self.key_path = grant_issuer.keygen(keys, "test-1")
        self.key = grant_issuer.load_key(self.key_path)
        inv = tmp / "cameras.yaml"
        inv.write_text(CAMERAS_YAML)
        base = dict(gateway_id="test-gw", keys_file=keys / "rails.jwks.json", inventory_file=inv,
                    data_dir=tmp / "data", live_gate_dir=tmp / "gate")
        base.update(overrides)
        from vivo_api.config import _networks
        self.settings = Settings(local_cidrs=_networks("192.168.30.0/24"), **base)
        self.recorder = FakeRecorder()
        self.go2rtc = FakeGo2rtc()
        self.health = FakeHealth()
        self.app = create_app(self.settings, recorder=self.recorder, go2rtc=self.go2rtc.client(),
                              health=self.health, clock=self.clock, start_background=False)
        self.ctx = self.app.state.ctx

    def client(self, addr=LAN_CLIENT) -> TestClient:
        return TestClient(self.app, client=addr)

    def grant(self, cams=("cam01",), perms=("live", "playback", "export"), **kw) -> str:
        kw.setdefault("now", self.clock.t)
        return grant_issuer.sign(self.key, "test-1", kw.pop("aud", AUD), cams, perms, **kw)

    def auth(self, *a, **kw) -> dict:
        return {"Authorization": f"Bearer {self.grant(*a, **kw)}"}

    def audit(self) -> list[dict]:
        out = []
        for p in sorted((self.settings.data_dir / "audit").glob("*.jsonl")):
            out += [json.loads(line) for line in p.read_text().splitlines()]
        return out


@pytest.fixture
def env(tmp_path) -> Env:
    return Env(tmp_path)
