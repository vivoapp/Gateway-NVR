"""M14 security set: every wrong camera, expired grant, wrong audience, wrong algorithm, bad
signature, unknown key and changed link is refused, and every refusal is in the audit log."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json

import jwt
import pytest
from cryptography.hazmat.primitives import serialization

import grant_issuer
from conftest import AUD


def b64(d: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()


def test_valid_grant_lists_only_granted_cameras(env):
    r = env.client().get("/v1/cameras", headers=env.auth(cams=("cam01", "cam03", "cam99")))
    assert r.status_code == 200
    assert [c["camera_id"] for c in r.json()["cameras"]] == ["cam01", "cam03"]
    assert r.json()["cameras"][0] == {"camera_id": "cam01", "name": "VIVO-CAM-01",
                                      "streams": [{"role": "main"}, {"role": "sub"}]}


def test_health_needs_no_grant(env):
    r = env.client().get("/v1/health")
    assert r.status_code == 200 and r.json()["status"] == "ok"


@pytest.mark.parametrize("header", [None, "", "Bearer", "Bearer ", "Basic dXNlcjpwYXNz", "Bearer abc"])
def test_missing_or_malformed_grant_is_401(env, header):
    h = {"Authorization": header} if header is not None else {}
    r = env.client().get("/v1/cameras", headers=h)
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "unauthenticated"
    assert r.headers["www-authenticate"] == "Bearer"


@pytest.mark.parametrize("change,reason", [
    (dict(ttl=60, now_shift=-70), "expired"),
    (dict(ttl=60, now_shift=30), "not_yet_valid"),
    (dict(ttl=300), "lifetime_too_long"),
    (dict(aud="vivo-gw:other-gw"), "bad_audience"),
    (dict(iss="someone-else"), "bad_issuer"),
])
def test_bad_claims_are_refused(env, change, reason):
    shift = change.pop("now_shift", 0)
    token = env.grant(now=env.clock.t + shift, **change)
    r = env.client().get("/v1/cameras", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 401
    assert env.audit()[-1]["reason"] == reason


def test_expiry_leeway_is_5_seconds(env):
    token = env.grant(ttl=60)
    env.clock.t += 64
    assert env.client().get("/v1/cameras", headers={"Authorization": f"Bearer {token}"}).status_code == 200
    env.clock.t += 2
    assert env.client().get("/v1/cameras", headers={"Authorization": f"Bearer {token}"}).status_code == 401


def test_alg_none_and_hs256_confusion_are_refused(env):
    """'none' and HS256 signed with the public key (the classic algorithm confusion) are refused."""
    claims = {"iss": "vivo-rails", "aud": AUD, "sub": "u", "site_id": "s", "cams": ["cam01"], "perms": ["live"],
              "iat": int(env.clock.t), "exp": int(env.clock.t) + 60, "jti": "j"}
    none_token = f"{b64({'alg': 'none', 'kid': 'test-1'})}.{b64(claims)}."
    pub_pem = env.key.public_key().public_bytes(serialization.Encoding.PEM,
                                                serialization.PublicFormat.SubjectPublicKeyInfo)
    head, body = b64({"alg": "HS256", "kid": "test-1", "typ": "JWT"}), b64(claims)
    mac = hmac.new(pub_pem, f"{head}.{body}".encode(), hashlib.sha256).digest()
    hs_token = f"{head}.{body}.{base64.urlsafe_b64encode(mac).rstrip(b'=').decode()}"
    for token in (none_token, hs_token):
        r = env.client().get("/v1/cameras", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 401
    assert [a["reason"] for a in env.audit()[-2:]] == ["bad_algorithm", "bad_algorithm"]


def test_tampered_grant_is_refused(env):
    token = env.grant(cams=("cam01",))
    head, body, sig = token.split(".")
    claims = json.loads(base64.urlsafe_b64decode(body + "=="))
    claims["cams"] = ["cam01", "cam02"]                  # a changed grant
    forged = f"{head}.{b64(claims)}.{sig}"
    r = env.client().get("/v1/cameras/cam02", headers={"Authorization": f"Bearer {forged}"})
    assert r.status_code == 401
    assert env.audit()[-1]["reason"] == "bad_signature"


def test_unknown_key_and_rotation(env, tmp_path):
    other = grant_issuer.keygen(tmp_path / "elsewhere", "test-2")
    token = grant_issuer.sign(grant_issuer.load_key(other), "test-2", AUD, ["cam01"], ["live"], now=env.clock.t)
    c = env.client()
    assert c.get("/v1/cameras", headers={"Authorization": f"Bearer {token}"}).status_code == 401
    assert env.audit()[-1]["reason"] == "unknown_key"
    # Rails adds key 2 to the set: both keys work, without a restart
    grant_issuer.keygen(env.tmp / "keys", "test-2")
    k2 = grant_issuer.load_key(env.tmp / "keys" / "test-2.private.pem")
    t2 = grant_issuer.sign(k2, "test-2", AUD, ["cam01"], ["live"], now=env.clock.t)
    assert c.get("/v1/cameras", headers={"Authorization": f"Bearer {t2}"}).status_code == 200
    assert c.get("/v1/cameras", headers=env.auth()).status_code == 200


def test_private_key_in_key_set_is_ignored(env):
    jwks_path = env.settings.keys_file
    jwks = json.loads(jwks_path.read_text())
    from jwt.algorithms import ECAlgorithm
    priv = json.loads(ECAlgorithm.to_jwk(env.key))
    priv.update({"kid": "test-1"})
    jwks_path.write_text(json.dumps({"keys": [priv]}))
    assert env.client().get("/v1/cameras", headers=env.auth()).status_code == 401


def test_camera_not_in_grant_is_403_even_if_it_does_not_exist(env):
    c = env.client()
    for cam in ("cam02", "cam77"):
        r = c.get(f"/v1/cameras/{cam}", headers=env.auth(cams=("cam01",)))
        assert r.status_code == 403 and r.json()["error"]["code"] == "forbidden"
    assert env.audit()[-1]["reason"] == "camera_not_in_grant"


def test_granted_but_unknown_camera_is_404(env):
    r = env.client().get("/v1/cameras/cam77", headers=env.auth(cams=("cam77",)))
    assert r.status_code == 404 and r.json()["error"]["code"] == "camera_not_found"


def test_malformed_camera_id_is_400(env):
    r = env.client().get("/v1/cameras/CAM-01", headers=env.auth())
    assert r.status_code == 400


def test_missing_permission_is_403(env):
    r = env.client().get("/v1/cameras/cam01/timeline?start=2026-11-08T04:00:00Z&end=2026-11-08T05:00:00Z",
                         headers=env.auth(perms=("live",)))
    assert r.status_code == 403
    assert env.audit()[-1]["reason"] == "missing_permission"


@pytest.mark.parametrize("change", [
    {"cams": "cam01"}, {"cams": ["CAM 1"]}, {"perms": ["admin"]}, {"perms": []}, {"site_id": 5},
    {"iat": True}, {"exp": True},
])
def test_bad_claim_types_are_refused(env, change):
    payload = {"iss": "vivo-rails", "aud": AUD, "sub": "u", "site_id": "s", "cams": ["cam01"], "perms": ["live"],
               "iat": int(env.clock.t), "exp": int(env.clock.t) + 60, "jti": "j"}
    payload.update(change)
    token = jwt.encode(payload, env.key, algorithm="ES256", headers={"kid": "test-1"})
    r = env.client().get("/v1/cameras", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 401
    assert env.audit()[-1]["reason"] == "bad_claims"


def test_audit_never_contains_the_grant(env):
    token = env.grant(aud="vivo-gw:other")
    env.client().get("/v1/cameras", headers={"Authorization": f"Bearer {token}"})
    text = "".join(p.read_text() for p in (env.settings.data_dir / "audit").glob("*.jsonl"))
    assert token not in text and token.split(".")[2] not in text
    rec = env.audit()[-1]
    assert rec["event"] == "grant_refused" and rec["client_ip"] == "192.168.30.50"


def test_frigate_tls_check_is_skipped_only_for_a_loopback_address():
    from vivo_api.recorders.frigate import FrigateRecorder, _loopback
    assert _loopback("https://127.0.0.1:8971") and _loopback("https://[::1]:8971")
    assert not _loopback("https://localhost:8971") and not _loopback("https://192.168.30.200:8971")
    with pytest.raises(ValueError):
        FrigateRecorder("https://localhost:8971", "u", "p")
