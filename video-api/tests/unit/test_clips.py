"""M17: clip jobs, limits, progress, download, cleanup."""
from __future__ import annotations

import asyncio
import time

import pytest

from conftest import Env, tiny_mp4

S = 1794110400.0
FIVE_MIN = {"start": "2026-11-08T04:00:00Z", "end": "2026-11-08T04:05:00Z"}


@pytest.fixture
def env(tmp_path):
    e = Env(tmp_path, clip_queue_max=2, clip_workers=1, clip_keep=3600, clip_store_max_bytes=5000)
    e.recorder.record("cam01", S, S + 3600)
    return e


def wait_status(c, h, cid, want, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        b = c.get(f"/v1/clips/{cid}", headers=h).json()
        if b["status"] in want:
            return b
        time.sleep(0.02)
    raise AssertionError(f"clip stayed {b['status']}")


def test_clip_is_prepared_checked_and_downloaded(env):
    h = env.auth(perms=("export",))
    with env.client() as c:
        r = c.post("/v1/cameras/cam01/clips", json=FIVE_MIN, headers=h)
        assert r.status_code == 202, r.text
        assert r.json()["queue_position"] in (0, 1)
        cid = r.json()["clip_id"]
        b = wait_status(c, h, cid, {"ready", "failed"})
        assert b["status"] == "ready" and b["progress"] == 100 and b["size_bytes"] == len(tiny_mp4())
        assert b["expires_at"] is not None and b["error"] is None
        d = c.get(f"/v1/clips/{cid}/download", headers=h)
        assert d.status_code == 200 and d.content == tiny_mp4()
        assert d.headers["content-disposition"] == 'attachment; filename="cam01_20261108T040000Z_20261108T040500Z.mp4"'
        assert c.get(f"/v1/clips/{cid}/download", headers=env.auth(perms=("export",), sub="other")).status_code == 403
    events = [a["event"] for a in env.audit()]
    assert "clip_request" in events and "clip_download" in events


def test_broken_clip_fails(env):
    env.recorder.clip_data = b"\x00\x00\x00\x10ftypisom" + b"junk"
    h = env.auth(perms=("export",))
    with env.client() as c:
        cid = c.post("/v1/cameras/cam01/clips", json=FIVE_MIN, headers=h).json()["clip_id"]
        b = wait_status(c, h, cid, {"ready", "failed"})
        assert b["status"] == "failed" and b["error"]["code"] == "upstream_unavailable"
        assert c.get(f"/v1/clips/{cid}/download", headers=h).status_code == 409
    assert not list((env.settings.data_dir / "clips").glob("*.part"))


def test_limits_and_errors(env):
    h = env.auth(perms=("export",))
    with env.client() as c:
        long = {"start": "2026-11-08T04:00:00Z", "end": "2026-11-08T04:30:01Z"}
        assert c.post("/v1/cameras/cam01/clips", json=long, headers=h).status_code == 413
        empty = {"start": "2026-11-08T06:00:00Z", "end": "2026-11-08T06:05:00Z"}
        assert c.post("/v1/cameras/cam01/clips", json=empty, headers=h).json()["error"]["code"] == "no_recording"
        env.health.snap["alerts"] = [{"alert": "disk_near_limit", "level": "critical", "target": "/srv/vivo"}]
        r = c.post("/v1/cameras/cam01/clips", json=FIVE_MIN, headers=h)
        assert r.status_code == 503 and r.json()["error"]["code"] == "disk_near_limit"
        env.health.snap["alerts"] = []
        # one worker held busy, two queued, the next is refused
        env.recorder.clip_gate = asyncio.Event()
        ids = [c.post("/v1/cameras/cam01/clips", json=FIVE_MIN, headers=h).json()["clip_id"] for _ in range(3)]
        wait_status(c, h, ids[0], {"running"})
        r = c.post("/v1/cameras/cam01/clips", json=FIVE_MIN, headers=h)
        assert r.status_code == 429 and r.json()["limit"] == "clip_queue"
        assert c.get(f"/v1/clips/{ids[2]}", headers=h).json()["status"] == "queued"
        # cancelling the running clip lets the queue move on
        assert c.delete(f"/v1/clips/{ids[0]}", headers=h).status_code == 204
        assert c.get(f"/v1/clips/{ids[0]}", headers=h).status_code == 404
        c.portal.call(env.recorder.clip_gate.set)
        assert wait_status(c, h, ids[2], {"ready", "failed"})["status"] == "ready"


def test_cleanup_by_age_and_size(env):
    h = env.auth(perms=("export",))
    with env.client() as c:
        ids = []
        for _ in range(3):
            cid = c.post("/v1/cameras/cam01/clips", json=FIVE_MIN, headers=h).json()["clip_id"]
            wait_status(c, h, cid, {"ready"})
            ids.append(cid)
            env.clock.t += 1
        # cap 5000 bytes, each clip ~1050: all three fit
        c.portal.call(env.ctx.clips.sweep)
        assert all(c.get(f"/v1/clips/{i}", headers=h).status_code == 200 for i in ids)
        object.__setattr__(env.ctx.settings, "clip_store_max_bytes", 2200)
        c.portal.call(env.ctx.clips.sweep)
        assert c.get(f"/v1/clips/{ids[0]}", headers=h).status_code == 404      # oldest went first
        assert c.get(f"/v1/clips/{ids[2]}", headers=h).status_code == 200
        env.clock.t += 3601
        c.portal.call(env.ctx.clips.sweep)
        assert all(c.get(f"/v1/clips/{i}", headers=h).status_code == 404 for i in ids)
    assert not list((env.settings.data_dir / "clips").glob("cl_*"))


def test_queue_limit_holds_under_concurrent_requests(tmp_path):
    """Five requests at the same moment, room for two: exactly two are queued, three refused."""
    e = Env(tmp_path, clip_queue_max=2)
    e.recorder.record("cam01", S, S + 3600)
    slow = e.recorder.segments

    async def segments(*a):
        await asyncio.sleep(0.05)              # the checks before enqueue wait on the recorder
        return await slow(*a)
    e.recorder.segments = segments

    async def run():
        e.ctx.clips.queue = asyncio.Queue()    # no workers: nothing leaves the queue during the test
        from vivo_api.grants import Grant
        g = Grant(sub="u", site_id="s", cams=frozenset({"cam01"}), perms=frozenset({"export"}), jti="j",
                  iat=e.clock.t, exp=e.clock.t + 60)
        return await asyncio.gather(*[e.ctx.clips.create(g, "cam01", S, S + 300, "192.168.30.50") for _ in range(5)],
                                    return_exceptions=True)
    results = asyncio.run(run())
    from vivo_api.errors import ApiError
    assert sum(not isinstance(r, Exception) for r in results) == 2
    assert [r.extra.get("limit") for r in results if isinstance(r, ApiError)] == ["clip_queue"] * 3
