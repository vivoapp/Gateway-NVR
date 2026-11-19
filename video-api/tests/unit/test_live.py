"""M15: live sessions, limits, one offer each, renewal, revocation and the cut (ADR 0002)."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json

import pytest

from conftest import LAN_CLIENT, REMOTE_CLIENT, T0, Env
from vivo_api.timeutil import fmt
from vivo_api.live.gate import Gate, parse_remote_addr
from vivo_api.live.turn import credentials


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path, live_site_remote_max=2, live_camera_max=3, turn_secret="s3cret",
               stun_urls=("stun:turn.test:3478",), turn_urls=("turn:turn.test:3478?transport=udp",))


def start(env, client, cam="cam01", stream="sub", **grant):
    grant.setdefault("cams", (cam,))
    return client.post(f"/v1/cameras/{cam}/live", json={"stream": stream}, headers=env.auth(**grant))


def gate_requests(env) -> list[dict]:
    d = env.settings.live_gate_dir / "requests"
    return [json.loads(p.read_text()) for p in sorted(d.glob("*.json"))] if d.exists() else []


def test_session_has_turn_credentials_for_this_session(env):
    r = start(env, env.client())
    assert r.status_code == 201, r.text
    b = r.json()
    assert b["session_id"].startswith("ls_") and len(b["session_id"]) == 25
    assert b["remote"] is False and b["stream"] == "sub" and b["renew_after_seconds"] == 30
    stun, turn = b["ice_servers"]
    assert stun == {"urls": ["stun:turn.test:3478"], "username": None, "credential": None}
    assert turn["username"] == f"{int(env.clock.t) + 600}:{b['session_id']}"
    expect = base64.b64encode(hmac.new(b"s3cret", turn["username"].encode(), hashlib.sha1).digest()).decode()
    assert turn["credential"] == expect
    assert env.audit()[-1]["event"] == "live_start"


def test_default_stream_is_sub(env):
    r = env.client().post("/v1/cameras/cam01/live", headers=env.auth())
    assert r.status_code == 201 and r.json()["stream"] == "sub"


def test_limits(env):
    remote, lan = env.client(REMOTE_CLIENT), env.client(LAN_CLIENT)
    assert start(env, remote).json()["remote"] is True
    assert start(env, remote, cam="cam02").status_code == 201
    r = start(env, remote, cam="cam03")
    assert r.status_code == 429 and r.json()["limit"] == "site_remote"
    # building network viewers do not count toward the site's remote limit, but toward the camera's
    assert start(env, lan).status_code == 201
    assert start(env, lan).status_code == 201
    r = start(env, lan)
    assert r.status_code == 429 and r.json()["limit"] == "camera_viewers"
    assert env.audit()[-1]["reason"] == "camera_viewer_limit"


def test_offer_goes_to_go2rtc_with_the_session_tag(env):
    c = env.client()
    sid = start(env, c).json()["session_id"]
    r = c.post(f"/v1/live/{sid}/offer", json={"sdp": "v=0 offer"}, headers=env.auth())
    assert r.status_code == 200 and r.json()["sdp"].startswith("v=0")
    req = env.go2rtc.requests[-1]
    assert req.url.path == "/api/webrtc" and req.url.params["src"] == "cam01_sub"
    assert req.headers["user-agent"] == f"vivo-video-api/{sid}"
    assert json.loads(req.content) == {"type": "offer", "sdp": "v=0 offer"}
    assert req.headers["authorization"].startswith("Basic ")
    # one offer per session
    assert c.post(f"/v1/live/{sid}/offer", json={"sdp": "v=0"}, headers=env.auth()).status_code == 409


def test_offer_errors(env):
    c = env.client()
    sid = start(env, c, stream="main").json()["session_id"]
    env.go2rtc.answer = {"status": 500, "text": "streams: codecs not matched: H265"}
    r = c.post(f"/v1/live/{sid}/offer", json={"sdp": "v=0"}, headers=env.auth())
    assert r.status_code == 422 and r.json()["error"]["code"] == "codec_unsupported"
    env.go2rtc.answer = {"status": 500, "text": "boom"}
    assert c.post(f"/v1/live/{sid}/offer", json={"sdp": "v=0"}, headers=env.auth()).status_code == 502
    assert c.post(f"/v1/live/{sid}/offer", json={"sdp": "v=0", "x": 1}, headers=env.auth()).status_code == 400


def test_other_user_cannot_use_a_session(env):
    c = env.client()
    sid = start(env, c, sub="alice").json()["session_id"]
    r = c.post(f"/v1/live/{sid}/offer", json={"sdp": "v=0"}, headers=env.auth(sub="mallory"))
    assert r.status_code == 403
    assert c.post("/v1/live/ls_doesnotexist/offer", json={"sdp": "v=0"}, headers=env.auth()).status_code == 404


def test_renewal_extends_and_expiry_closes(env):
    c = env.client()
    sid = start(env, c, ttl=60).json()["session_id"]
    env.clock.t += 30
    r = c.post(f"/v1/live/{sid}/renew", headers=env.auth(ttl=60))
    assert r.status_code == 200 and r.json()["expires_at"] == fmt(T0 + 90)
    env.clock.t += 61
    asyncio.run(env.ctx.live.sweep())
    assert c.post(f"/v1/live/{sid}/renew", headers=env.auth()).status_code == 404
    assert env.audit()[-1]["reason"] == "expired"


def test_refused_renewal_closes_and_cuts_the_viewer(env):
    c = env.client()
    sid = start(env, c).json()["session_id"]
    c.post(f"/v1/live/{sid}/offer", json={"sdp": "v=0"}, headers=env.auth())
    env.go2rtc.streams["cam01_sub"] = {"consumers": [
        {"id": 3, "user_agent": "Mozilla/5.0", "remote_addr": "192.168.30.9:4000 host"},
        {"id": 6, "user_agent": f"***-video-api/{sid}", "remote_addr": "203.0.113.7:51234 prflx"}]}   # go2rtc masks secrets
    r = c.post(f"/v1/live/{sid}/renew", headers=env.auth(cams=("cam02",)))      # access to cam01 revoked
    assert r.status_code == 403
    assert gate_requests(env) == [{"ip": "203.0.113.7", "port": 51234, "until": int(env.clock.t) + 60}]
    end = [a for a in env.audit() if a["event"] == "live_end"][-1]
    assert end["reason"] == "renewal_refused" and end["cut"] == "dropped"
    assert c.post(f"/v1/live/{sid}/renew", headers=env.auth()).status_code == 404


def test_close_by_client(env):
    c = env.client()
    sid = start(env, c).json()["session_id"]
    assert c.delete(f"/v1/live/{sid}", headers=env.auth()).status_code == 204
    assert c.delete(f"/v1/live/{sid}", headers=env.auth()).status_code == 204
    assert c.delete("/v1/live/ls_unknown", headers=env.auth()).status_code == 204
    end = [a for a in env.audit() if a["event"] == "live_end"][-1]
    assert end["reason"] == "closed_by_client" and end["cut"] == "no_peer"
    # a closed session frees the camera's viewer slot
    for _ in range(3):
        assert start(env, c).status_code == 201


@pytest.mark.parametrize("value,expected", [
    ("203.0.113.7:51234 prflx", ("203.0.113.7", 51234)),
    ("192.168.65.1:37295 host", ("192.168.65.1", 37295)),
    ("[2001:db8::1]:5000 relay", ("2001:db8::1", 5000)),
    ("", None), (None, None), ("garbage", None), ("1.2.3.4:0 host", None), ("1.2.3.4:99999", None),
])
def test_parse_remote_addr(value, expected):
    assert parse_remote_addr(value) == expected


def test_gate_request_file(tmp_path):
    g = Gate(tmp_path, hold=120, clock=lambda: 1000.0)
    assert g.cut("203.0.113.7", 51234)
    files = list((tmp_path / "requests").glob("*.json"))
    assert len(files) == 1 and json.loads(files[0].read_text()) == {"ip": "203.0.113.7", "port": 51234, "until": 1120}
    assert g.release("203.0.113.7", 51234)
    assert {"ip": "203.0.113.7", "port": 51234, "release": True} in [json.loads(f.read_text()) for f in (tmp_path / "requests").glob("*.json")]
    assert not list((tmp_path / "requests").glob(".*"))


def test_turn_credentials_format():
    user, cred = credentials("k", "ls_x", 600, 1000.0)
    assert user == "1600:ls_x"
    assert cred == base64.b64encode(hmac.new(b"k", b"1600:ls_x", hashlib.sha1).digest()).decode()


def test_drop_is_released_once_go2rtc_dropped_the_viewer(env):
    s = asyncio.run(_offered_session(env))
    env.go2rtc.streams["cam01_sub"] = {"consumers": [{"user_agent": f"x/{s.sid}", "remote_addr": "198.51.100.9:49170 relay"}]}

    async def run():
        assert await env.ctx.live.cut(s) == "dropped"
        env.go2rtc.streams["cam01_sub"] = {"consumers": []}         # go2rtc closed the peer
        task = next(iter(env.ctx.live._releases))
        await asyncio.wait_for(task, 5)
    import vivo_api.live.sessions as sessions_mod
    orig = sessions_mod.LiveManager._release_when_gone

    async def fast(self, s, addr, every=0.01):
        return await orig(self, s, addr, every)
    sessions_mod.LiveManager._release_when_gone = fast
    try:
        asyncio.run(run())
    finally:
        sessions_mod.LiveManager._release_when_gone = orig
    reqs = gate_requests(env)
    assert {"ip": "198.51.100.9", "port": 49170, "until": int(env.clock.t) + 60} in reqs
    assert {"ip": "198.51.100.9", "port": 49170, "release": True} in reqs


async def _offered_session(env):
    from vivo_api.grants import Grant
    g = Grant(sub="u", site_id="s", cams=frozenset({"cam01"}), perms=frozenset({"live"}), jti="j",
              iat=env.clock.t, exp=env.clock.t + 60)
    s = await env.ctx.live.create(g, "cam01", "sub", False, "192.168.30.50")
    s.state = "offered"
    return s


def test_mse_refuses_a_page_from_another_site(env):
    from starlette.websockets import WebSocketDisconnect
    c = env.client(LAN_CLIENT)
    sid = start(env, c).json()["session_id"]
    with pytest.raises(WebSocketDisconnect) as e:
        with c.websocket_connect(f"/v1/live/{sid}/mse", headers={"Origin": "https://evil.example"}) as ws:
            ws.receive_text()
    assert e.value.code == 4003
    remote = env.client(REMOTE_CLIENT)
    rsid = start(env, remote, cam="cam02").json()["session_id"]
    with pytest.raises(WebSocketDisconnect) as e:              # MSE is for the building network only
        with remote.websocket_connect(f"/v1/live/{rsid}/mse") as ws:
            ws.receive_text()
    assert e.value.code == 4004
