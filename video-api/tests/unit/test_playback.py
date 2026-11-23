"""M16: playback sessions, the HLS proxy and playlist rewriting."""
from __future__ import annotations

from vivo_api.playback import rewrite_playlist

S = 1794110400.0                                    # 2026-11-08 04:00 UTC
BODY = {"start": "2026-11-08T04:00:00Z", "end": "2026-11-08T05:00:00Z"}


def test_rewrite_playlist_keeps_only_plain_piece_names():
    text = "\n".join([
        "#EXTM3U", "#EXT-X-VERSION:6", '#EXT-X-MAP:URI="/vod/cam01/start/1/end/2/init-v1-a1.mp4"',
        "#EXTINF:10.0,", "/vod/cam01/start/1/end/2/seg-1-v1-a1.m4s",
        "#EXTINF:10.0,", "seg-2-v1-a1.m4s?x=1",
        "#EXTINF:10.0,", "http://evil.example/../../api/config",
        "#EXTINF:10.0,", "seg-4-v1-a1.m4s",
        '#EXT-X-MEDIA:TYPE=AUDIO,URI="../../secret"', "#EXT-X-ENDLIST", ""])
    out = rewrite_playlist(text).splitlines()
    assert out == ["#EXTM3U", "#EXT-X-VERSION:6", '#EXT-X-MAP:URI="init-v1-a1.mp4"',
                   "#EXTINF:10.0,", "seg-1-v1-a1.m4s", "#EXTINF:10.0,", "seg-2-v1-a1.m4s",
                   "#EXTINF:10.0,", "seg-4-v1-a1.m4s", "#EXT-X-ENDLIST"]


def test_playback_flow(env):
    env.recorder.record("cam01", S, S + 1800)
    env.recorder.pieces = {"index.m3u8": b"#EXTM3U\n#EXTINF:10.0,\n/vod/x/seg-1-v1.m4s\n#EXT-X-ENDLIST\n",
                           "seg-1-v1.m4s": b"\x00" * 50}
    c = env.client()
    r = c.post("/v1/cameras/cam01/playback", json=BODY, headers=env.auth(perms=("playback",)))
    assert r.status_code == 201, r.text
    b = r.json()
    assert b["url"] == f"/v1/playback/{b['playback_id']}/index.m3u8"
    assert b["recorded"] == [{"start": "2026-11-08T04:00:00Z", "end": "2026-11-08T04:30:00Z"}]
    pl = c.get(b["url"])                            # no grant: the playback id is the credential
    assert pl.status_code == 200 and pl.headers["content-type"].startswith("application/vnd.apple.mpegurl")
    assert "seg-1-v1.m4s" in pl.text and "/vod/" not in pl.text
    seg = c.get(f"/v1/playback/{b['playback_id']}/seg-1-v1.m4s")
    assert seg.status_code == 200 and seg.content == b"\x00" * 50
    assert c.get(f"/v1/playback/{b['playback_id']}/missing.m4s").status_code == 404
    assert c.get(f"/v1/playback/{b['playback_id']}/..%2Fapi.m4s").status_code == 404
    assert c.get("/v1/playback/pb_unknown/index.m3u8").status_code == 404
    assert env.audit()[-1]["event"] == "playback_start"


def test_long_playlists_go_gzipped_when_the_client_accepts_it(env):
    env.recorder.record("cam01", S, S + 1800)
    long = "#EXTM3U\n" + "".join(f"#EXTINF:10.0,\nseg-{i}-v1.m4s\n" for i in range(1, 181)) + "#EXT-X-ENDLIST\n"
    env.recorder.pieces = {"index.m3u8": long.encode()}
    c = env.client()
    b = c.post("/v1/cameras/cam01/playback", json=BODY, headers=env.auth(perms=("playback",))).json()
    zipped = c.get(b["url"], headers={"Accept-Encoding": "gzip"})
    assert zipped.headers.get("content-encoding") == "gzip" and zipped.text == long      # httpx unpacks it
    plain = c.get(b["url"], headers={"Accept-Encoding": "identity"})
    assert "content-encoding" not in plain.headers and plain.text == long


def test_playback_errors(env):
    c, h = env.client(), env.auth(perms=("playback",))
    r = c.post("/v1/cameras/cam01/playback", json=BODY, headers=h)
    assert r.status_code == 404 and r.json()["error"]["code"] == "no_recording"
    long = {"start": "2026-11-07T04:00:00Z", "end": "2026-11-08T04:00:01Z"}
    assert c.post("/v1/cameras/cam01/playback", json=long, headers=h).status_code == 413
    assert c.post("/v1/cameras/cam01/playback", json={**BODY, "speed": 2}, headers=h).status_code == 400
    assert c.post("/v1/cameras/cam01/playback", json=BODY, headers=env.auth(perms=("live",))).status_code == 403


def test_playback_expires_with_its_grant_and_renews(env):
    env.recorder.record("cam01", S, S + 600)
    env.recorder.pieces = {"index.m3u8": b"#EXTM3U\n"}
    c = env.client()
    b = c.post("/v1/cameras/cam01/playback", json=BODY, headers=env.auth(ttl=60)).json()
    env.clock.t += 50
    assert c.post(f"/v1/playback/{b['playback_id']}/renew", headers=env.auth(ttl=60)).status_code == 200
    env.clock.t += 50
    assert c.get(b["url"]).status_code == 200            # still inside the renewed grant
    env.clock.t += 20
    assert c.get(b["url"]).status_code == 404
    ends = [a for a in env.audit() if a["event"] == "playback_end"]
    assert ends and ends[-1]["reason"] == "expired"


def test_refused_playback_renewal_closes_it(env):
    env.recorder.record("cam01", S, S + 600)
    env.recorder.pieces = {"index.m3u8": b"#EXTM3U\n"}
    c = env.client()
    b = c.post("/v1/cameras/cam01/playback", json=BODY, headers=env.auth(sub="alice")).json()
    assert c.post(f"/v1/playback/{b['playback_id']}/renew", headers=env.auth(sub="bob")).status_code == 403
    assert c.get(b["url"]).status_code == 404
