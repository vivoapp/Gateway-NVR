"""M18 contract suite: the same tests for every recorder adapter.

Runs against the mock recorder always (footage made with ffmpeg in a temporary folder), and
against Frigate when FRIGATE_URL, FRIGATE_USER and FRIGATE_PASSWORD point at one (the lab or the
gateway), with CONTRACT_CAMERA naming a camera that has been recording for at least 10 minutes.

  python -m pytest tests/contract                       # mock only
  FRIGATE_URL=https://127.0.0.1:8971 FRIGATE_USER=video_api FRIGATE_PASSWORD=... \\
    CONTRACT_CAMERA=cam01 python -m pytest tests/contract
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests" / "unit"))
sys.path.insert(0, str(ROOT / "tools"))

from conftest import Env  # noqa: E402
from vivo_api import mp4  # noqa: E402
from vivo_api.timeutil import fmt  # noqa: E402

GAP_OFFSET, GAP_LEN = 300, 60                       # the mock has a 60 s hole 5 minutes in


_FOOTAGE: dict = {}


def _mock(tmp: Path):
    """Ten minutes of mock footage, made once per test run."""
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg is needed to make mock footage")
    if not _FOOTAGE:
        import tempfile
        import make_mock
        folder = Path(tempfile.mkdtemp(prefix="vivo-mock-"))
        start = (int(time.time()) // 600 - 2) * 600
        make_mock.make(folder, "cam01", start, 10, [(GAP_OFFSET, GAP_LEN)])
        _FOOTAGE.update(folder=folder, start=start)
    from vivo_api.recorders.mock import MockRecorder
    start = _FOOTAGE["start"]
    return (MockRecorder(_FOOTAGE["folder"]), "cam01", start, start + 600,
            (start + GAP_OFFSET, start + GAP_OFFSET + GAP_LEN))


def _frigate(tmp: Path):
    url = os.environ.get("FRIGATE_URL")
    if not url:
        pytest.skip("FRIGATE_URL not set")
    import asyncio
    from vivo_api.recorders.base import merge
    from vivo_api.recorders.frigate import FrigateRecorder
    cam = os.environ.get("CONTRACT_CAMERA", "cam01")
    end = int(time.time()) - 30
    start = end - 1800

    async def recorded():
        r = FrigateRecorder(url, os.environ.get("FRIGATE_USER", "video_api"), os.environ.get("FRIGATE_PASSWORD", ""))
        try:
            return merge(await r.segments(cam, start, end), start, end, 3.0)[0]
        finally:
            await r.aclose()
    periods = [p for p in asyncio.run(recorded()) if p[1] - p[0] >= 150]
    if not periods:
        pytest.skip(f"{cam} has no 150 s of continuous recording in the last 30 minutes yet")
    return (FrigateRecorder(url, os.environ.get("FRIGATE_USER", "video_api"), os.environ.get("FRIGATE_PASSWORD", "")),
            cam, int(periods[-1][1]) - 150, end, None)


@pytest.fixture(params=["mock", "frigate"])
def rig(request, tmp_path):
    recorder, cam, start, end, gap = (_mock if request.param == "mock" else _frigate)(tmp_path)
    env = Env(tmp_path)
    from vivo_api.main import create_app
    env.app = create_app(env.settings, recorder=recorder, go2rtc=env.go2rtc.client(), health=env.health,
                         clock=time.time, start_background=False)
    env.ctx = env.app.state.ctx
    env.clock.t = time.time()
    if cam not in ("cam01", "cam02", "cam03"):
        (tmp_path / "cameras.yaml").write_text(f"cameras:\n  - {{id: {cam}, name: {cam}}}\n")
    with env.client() as client:
        yield {"env": env, "client": client, "cam": cam, "start": start, "end": end, "gap": gap,
               "h": env.auth(cams=(cam,), perms=("playback", "export"))}


def test_timeline(rig):
    c, cam = rig["client"], rig["cam"]
    r = c.get(f"/v1/cameras/{cam}/timeline", params={"start": fmt(rig["start"]), "end": fmt(rig["end"])},
              headers=rig["h"])
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["recorded"], "nothing recorded in the window"
    if rig["gap"]:
        assert b["gaps"] == [{"start": fmt(rig["gap"][0]), "end": fmt(rig["gap"][1])}]


def test_playback_playlist_and_pieces(rig):
    c, cam = rig["client"], rig["cam"]
    body = {"start": fmt(rig["start"]), "end": fmt(rig["start"] + 120)}
    r = c.post(f"/v1/cameras/{cam}/playback", json=body, headers=rig["h"])
    assert r.status_code == 201, r.text
    url = r.json()["url"]
    base = url.rsplit("/", 1)[0]
    pl = c.get(url)
    assert pl.status_code == 200 and pl.text.startswith("#EXTM3U")
    names = [l for l in pl.text.splitlines() if l and not l.startswith("#")]
    names += re.findall(r'URI="([^"]+)"', pl.text)
    assert names and all("/" not in n for n in names)
    # a master playlist leads to a media playlist: follow one level
    media = [n for n in names if n.endswith(".m3u8")]
    if media:
        pl = c.get(f"{base}/{media[0]}")
        names = [l for l in pl.text.splitlines() if l and not l.startswith("#")] + re.findall(r'URI="([^"]+)"', pl.text)
    for name in names[:3]:
        piece = c.get(f"{base}/{name}")
        assert piece.status_code == 200 and len(piece.content) > 100, name


def test_clip(rig, tmp_path):
    c, cam = rig["client"], rig["cam"]
    # the mock's ten minutes: 7 minutes in, after the hole, so a clip that kept its pieces' times would
    # read 480 s long; Frigate's stretch is 150 s, so its clip starts 10 s in
    at = rig["start"] + (420 if rig["end"] - rig["start"] >= 600 else 10)
    body = {"start": fmt(at), "end": fmt(at + 60)}
    r = c.post(f"/v1/cameras/{cam}/clips", json=body, headers=rig["h"])
    assert r.status_code == 202, r.text
    cid = r.json()["clip_id"]
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        b = c.get(f"/v1/clips/{cid}", headers=rig["h"]).json()
        if b["status"] in ("ready", "failed"):
            break
        time.sleep(0.2)
    assert b["status"] == "ready", b
    d = c.get(f"/v1/clips/{cid}/download", headers=rig["h"])
    out = tmp_path / "clip.mp4"
    out.write_bytes(d.content)
    assert mp4.problem(out) is None
    if shutil.which("ffprobe"):
        dur = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                                    str(out)], capture_output=True, text=True).stdout.strip() or 0)
        assert 50 <= dur <= 80, dur


def test_empty_range_is_no_recording(rig):
    c, cam = rig["client"], rig["cam"]
    future = {"start": fmt(time.time() + 3600), "end": fmt(time.time() + 3700)}
    r = c.post(f"/v1/cameras/{cam}/playback", json=future, headers=rig["h"])
    assert r.status_code == 404 and r.json()["error"]["code"] == "no_recording"
