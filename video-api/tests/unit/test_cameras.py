"""M14: timeline merging, the timeline route, camera status, and the OpenAPI drift check."""
from __future__ import annotations

import subprocess
import sys

from conftest import ROOT, T0
from vivo_api.recorders.base import Segment, merge
from vivo_api.timeutil import fmt

W = "start=2026-11-08T04:00:00Z&end=2026-11-08T05:00:00Z"
S, E = 1794110400.0, 1794114000.0               # 04:00 and 05:00 UTC


def test_merge_joins_close_segments_and_reports_gaps():
    segs = [Segment(0, 10), Segment(10.5, 20), Segment(19, 30), Segment(40, 50), Segment(52, 60)]
    rec, gaps = merge(segs, 0, 60, 3.0)
    assert rec == [(0, 30), (40, 60)]
    assert gaps == [(30, 40)]


def test_merge_edges_and_empty():
    assert merge([], 0, 100, 3.0) == ([], [(0, 100)])
    rec, gaps = merge([Segment(1, 50), Segment(60, 98)], 0, 100, 3.0)
    assert rec == [(0, 50), (60, 100)] and gaps == [(50, 60)]
    rec, gaps = merge([Segment(10, 50)], 0, 100, 3.0)
    assert rec == [(10, 50)] and gaps == [(0, 10), (50, 100)]
    # pieces outside the window are ignored, pieces across its edges are clipped
    assert merge([Segment(-20, -5), Segment(-5, 30), Segment(120, 130)], 0, 100, 3.0)[0] == [(0, 30)]


def test_timeline_route(env):
    env.recorder.record("cam01", S - 5, S + 1200)
    env.recorder.record("cam01", S + 1300, E + 30)
    r = env.client().get(f"/v1/cameras/cam01/timeline?{W}", headers=env.auth(perms=("playback",)))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["recorded"] == [{"start": "2026-11-08T04:00:00Z", "end": fmt(S + 1200)},
                                {"start": fmt(S + 1300), "end": "2026-11-08T05:00:00Z"}]
    assert body["gaps"] == [{"start": fmt(S + 1200), "end": fmt(S + 1300)}]


def test_timeline_arguments(env):
    c, h = env.client(), env.auth(perms=("playback",))
    assert c.get("/v1/cameras/cam01/timeline?start=2026-11-08T04:00:00&end=2026-11-08T05:00:00Z", headers=h).status_code == 400
    assert c.get("/v1/cameras/cam01/timeline?start=2026-11-08T05:00:00Z&end=2026-11-08T04:00:00Z", headers=h).status_code == 400
    assert c.get("/v1/cameras/cam01/timeline?start=2026-11-08T04:00:00Z&end=2026-11-09T04:00:01Z", headers=h).status_code == 413
    assert c.get(f"/v1/cameras/cam01/timeline?{W}&limit=5", headers=h).status_code == 400
    assert c.get("/v1/cameras/cam01/timeline?start=2026-11-08T04:00:00Z", headers=h).status_code == 400
    env.recorder.down = True
    r = c.get(f"/v1/cameras/cam01/timeline?{W}", headers=h)
    assert r.status_code == 502 and r.json()["error"]["code"] == "upstream_unavailable"


def test_status_route(env):
    import asyncio
    clock = {"t": 100.0}
    watch = env.ctx.watch
    watch.clock = lambda: clock["t"]
    p = lambda b, i="a": {"producers": [{"id": i, "bytes_recv": b}]}     # noqa: E731
    watch.update({"cam01_main": p(1000), "cam01_sub": p(500)}, clock["t"])
    clock["t"] += 3
    watch.update({"cam01_main": p(2000), "cam01_sub": p(500)}, clock["t"])     # sub did not grow
    env.health.snap["cameras"]["cam01"] = {"main_segment_age_s": 7.0}
    r = env.client().get("/v1/cameras/cam01/status", headers=env.auth(perms=("live",)))
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["streams"] == [{"role": "main", "receiving": True}, {"role": "sub", "receiving": False}]
    assert b["recording"]["ok"] is True and b["recording"]["newest_segment_end"] == "2026-11-08T05:19:53Z"
    # a producer that reconnected (new id, bytes from 0) counts as receiving
    clock["t"] += 3
    watch.update({"cam01_main": p(10, "b"), "cam01_sub": p(500)}, clock["t"])
    assert watch.receiving("cam01_main")
    # a recording alert from the health service makes recording not ok
    env.health.snap["alerts"] = [{"alert": "recording_gap", "target": "cam01/main", "level": "critical"}]
    env.health._cache = None
    assert env.client().get("/v1/cameras/cam01/status", headers=env.auth()).json()["recording"]["ok"] is False
    env.health.down = True
    assert env.client().get("/v1/cameras/cam01/status", headers=env.auth()).status_code == 502
    asyncio.run(env.ctx.go2rtc.aclose())


def test_openapi_file_is_up_to_date():
    r = subprocess.run([sys.executable, str(ROOT / "tools" / "export_openapi.py"), "--check"],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
