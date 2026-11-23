"""Playback over more than an hour through Frigate (vivo_api/recorders/frigate.py).

Frigate's video module refuses one playlist of more than about 1,100 pieces (found on the reference
gateway, Nov 22 2026: a 4-hour playlist failed), so the adapter asks for one-hour parts and joins them.
The fake Frigate below refuses the same way.
"""
from __future__ import annotations

import asyncio
import re

import httpx
import pytest

from vivo_api.recorders import frigate as fr
from vivo_api.recorders.base import NotRecorded

T0 = 1_795_298_400                                 # 2026-11-22 03:00:00 UTC, on a clock hour
VOD = re.compile(r"^/vod/(\w+)/start/(\d+)/end/(\d+)/(.+)$")


class FakeFrigate:
    def __init__(self, empty_hours: set[int] = frozenset(), limit: int = 1100):
        self.empty, self.limit, self.asked = set(empty_hours), limit, []

    def handler(self, req: httpx.Request) -> httpx.Response:
        if req.url.path == "/api/login":
            return httpx.Response(200, json={})
        m = VOD.match(req.url.path)
        if not m:
            return httpx.Response(404)
        cam, a, b, name = m.group(1), int(m.group(2)), int(m.group(3)), m.group(4)
        self.asked.append((a, b, name))
        if (a - T0) // 3600 in self.empty:
            return httpx.Response(404, text="No recordings found.")
        if name != "index.m3u8":
            return httpx.Response(200, content=f"{cam}:{a}:{b}:{name}".encode())
        n = max(1, (b - a) // 10)
        if n > self.limit:
            return httpx.Response(503, text="<html>503 Service Temporarily Unavailable</html>")
        lines = ["#EXTM3U", "#EXT-X-TARGETDURATION:11", "#EXT-X-ALLOW-CACHE:YES", "#EXT-X-PLAYLIST-TYPE:VOD",
                 "#EXT-X-VERSION:6", "#EXT-X-MEDIA-SEQUENCE:1", '#EXT-X-MAP:URI="init-v1-a1.mp4"']
        for i in range(1, n + 1):
            lines += ["#EXTINF:10.007,", f"seg-{i}-v1-a1.m4s"]
        return httpx.Response(200, text="\n".join(lines + ["#EXT-X-ENDLIST"]) + "\n")


def recorder(fake: FakeFrigate) -> fr.FrigateRecorder:
    return fr.FrigateRecorder("http://frigate.test", "video_api", "pw", transport=httpx.MockTransport(fake.handler))


async def read(media) -> bytes:
    out = b""
    async for chunk in media.body:
        out += chunk
    return out


def test_one_hour_is_passed_through_as_before():
    fake = FakeFrigate()
    text = asyncio.run(read(asyncio.run(recorder(fake).media("cam01", T0, T0 + 3600, "index.m3u8")))).decode()
    assert "seg-1-v1-a1.m4s" in text and "c0-" not in text
    assert fake.asked == [(T0, T0 + 3600, "index.m3u8")]


def test_a_day_is_joined_from_hour_parts():
    fake = FakeFrigate()
    rec = recorder(fake)
    text = asyncio.run(read(asyncio.run(rec.media("cam01", T0, T0 + 86400, "index.m3u8")))).decode()
    lines = text.splitlines()
    assert lines[0] == "#EXTM3U" and lines[-1] == "#EXT-X-ENDLIST" and text.count("#EXT-X-ENDLIST") == 1
    assert text.count("#EXTINF") == 24 * 360
    assert text.count("#EXT-X-MAP") == 24 and text.count("#EXT-X-DISCONTINUITY") == 23
    assert 'URI="c0-init-v1-a1.mp4"' in text and "c23-seg-360-v1-a1.m4s" in text
    assert all(not ln or ln.startswith("#") or re.match(r"^c\d+-seg-\d+-v1-a1\.m4s$", ln) for ln in lines)
    assert sorted(a for a, _, n in fake.asked) == [T0 + 3600 * k for k in range(24)]
    # a piece goes to its own part's window
    body = asyncio.run(read(asyncio.run(rec.media("cam01", T0, T0 + 86400, "c5-seg-3-v1-a1.m4s"))))
    assert body == f"cam01:{T0 + 5 * 3600}:{T0 + 6 * 3600}:seg-3-v1-a1.m4s".encode()


def test_a_day_through_one_frigate_playlist_would_fail():
    assert FakeFrigate().handler(httpx.Request("GET", f"http://f/vod/cam01/start/{T0}/end/{T0 + 14400}/index.m3u8")).status_code == 503


def test_hours_without_recording_are_left_out():
    fake = FakeFrigate(empty_hours={0, 7, 8})
    text = asyncio.run(read(asyncio.run(recorder(fake).media("cam01", T0, T0 + 12 * 3600, "index.m3u8")))).decode()
    assert text.count("#EXT-X-MAP") == 9 and text.count("#EXT-X-DISCONTINUITY") == 8
    assert "c0-" not in text and "c7-" not in text and "c1-seg-1-v1-a1.m4s" in text
    assert text.splitlines()[5] == '#EXT-X-MAP:URI="c1-init-v1-a1.mp4"'      # no break before the first part


def test_nothing_recorded_and_bad_piece_names():
    rec = recorder(FakeFrigate(empty_hours=set(range(5))))
    with pytest.raises(NotRecorded):
        asyncio.run(rec.media("cam01", T0, T0 + 5 * 3600, "index.m3u8"))
    rec = recorder(FakeFrigate())
    for bad in ("c24-seg-1-v1-a1.m4s", "seg-1-v1-a1.m4s", "c1-../x.m4s"):
        with pytest.raises(NotRecorded):
            asyncio.run(rec.media("cam01", T0, T0 + 86400, bad))


def test_part_windows_are_cut_on_clock_hours():
    assert fr.vod_parts(T0, T0 + 3600) == [(T0, T0 + 3600)]
    assert fr.vod_parts(T0 + 0.5, T0 + 7200.2) == [(T0, T0 + 3600), (T0 + 3600, T0 + 7200), (T0 + 7200, T0 + 7201)]
    assert fr.vod_parts(T0 + 1800, T0 + 3 * 3600) == [(T0 + 1800, T0 + 3600), (T0 + 3600, T0 + 7200),
                                                      (T0 + 7200, T0 + 10800)]
    assert len(fr.vod_parts(T0, T0 + 86400)) == 24 and len(fr.vod_parts(T0 + 600, T0 + 87000)) == 25


def test_finished_hours_are_kept_and_the_current_one_is_not():
    fake = FakeFrigate(empty_hours={2})
    rec = recorder(fake)
    rec.clock = lambda: T0 + 6 * 3600 + 100            # 100 s into hour 6: hour 5 ended only 100 s ago
    for _ in range(2):
        asyncio.run(read(asyncio.run(rec.media("cam01", T0, T0 + 6 * 3600 + 90, "index.m3u8"))))
    asked = [a for a, _, n in fake.asked if n == "index.m3u8"]
    assert asked.count(T0) == 1 and asked.count(T0 + 2 * 3600) == 1     # kept, also the empty hour
    assert asked.count(T0 + 5 * 3600) == 2 and asked.count(T0 + 6 * 3600) == 2   # not settled yet
    rec.clock = lambda: T0 + 6 * 3600 + 100 + fr.VOD_KEEP + 1         # kept parts expire
    asyncio.run(read(asyncio.run(rec.media("cam01", T0, T0 + 6 * 3600 + 90, "index.m3u8"))))
    assert [a for a, _, n in fake.asked if n == "index.m3u8"].count(T0) == 2
