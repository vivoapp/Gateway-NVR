#!/usr/bin/env python3
"""M14 acceptance check against a running Video API (the lab or a gateway).

Checks every route in contract section 5 against the real recorder, the security set (wrong
camera, expired grant, wrong audience, wrong algorithm, bad signature, unknown key, changed
link, missing permission), and that each refusal is in the audit log. Writes
reports/m14-check-<UTC>.md.

usage: tests/m14_check.py --key KEYS/test-1.private.pem --aud vivo-gw:lab [--api https://127.0.0.1:8443]
                          [--camera cam01] [--audit-dir lab/data/audit] [--label lab]
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import sys
import tempfile
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import grant_issuer  # noqa: E402


class Report:
    def __init__(self):
        self.rows: list[tuple[str, bool, str]] = []

    def check(self, name: str, ok: bool, detail: str = "") -> bool:
        self.rows.append((name, bool(ok), detail))
        print(f"{'PASS' if ok else 'FAIL'}  {name}  {detail}", flush=True)
        return ok

    def markdown(self, title: str, intro: str) -> str:
        p = sum(ok for _, ok, _ in self.rows)
        lines = [f"# {title}", "", intro, "", f"Result: **{p} passed, {len(self.rows) - p} failed**", "",
                 "| Check | Result | Detail |", "|---|---|---|"]
        lines += [f"| {n} | {'PASS' if ok else 'FAIL'} | {d} |" for n, ok, d in self.rows]
        return "\n".join(lines) + "\n"


def b64(d: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()


def utc(t: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--api", default="https://127.0.0.1:8443")
    ap.add_argument("--key", required=True, type=Path)
    ap.add_argument("--aud", required=True)
    ap.add_argument("--camera", default="cam01")
    ap.add_argument("--audit-dir", type=Path)
    ap.add_argument("--label", default="lab")
    a = ap.parse_args()

    key, kid, cam = grant_issuer.load_key(a.key), grant_issuer.kid_of(a.key), a.camera
    sign = lambda **kw: grant_issuer.sign(key, kid, kw.pop("aud", a.aud), kw.pop("cams", [cam]),  # noqa: E731
                                          kw.pop("perms", ["live", "playback", "export"]), **kw)
    hdr = lambda tok: {"Authorization": f"Bearer {tok}"}                                          # noqa: E731
    c = httpx.Client(base_url=a.api, verify=False, timeout=30)       # self-signed until M23
    rep = Report()
    started = time.time()
    now = time.time()

    # ---- routes against the real recorder
    r = c.get("/v1/health")
    rep.check("GET /v1/health answers without a grant", r.status_code == 200 and r.json().get("status") == "ok", f"HTTP {r.status_code}")
    r = c.get("/v1/cameras", headers=hdr(sign(cams=[cam, "zz_none"])))
    ids = [x["camera_id"] for x in r.json().get("cameras", [])] if r.status_code == 200 else []
    rep.check("GET /v1/cameras lists only granted cameras that exist", ids == [cam], f"{ids}")
    r = c.get(f"/v1/cameras/{cam}", headers=hdr(sign()))
    rep.check("GET /v1/cameras/{id}", r.status_code == 200 and r.json().get("camera_id") == cam, f"HTTP {r.status_code}")
    r = c.get(f"/v1/cameras/{cam}/status", headers=hdr(sign()))
    st = r.json() if r.status_code == 200 else {}
    rx = {s["role"]: s["receiving"] for s in st.get("streams", [])}
    rep.check("status: both streams receiving", rx == {"main": True, "sub": True}, json.dumps(rx))
    rec = st.get("recording", {})
    rep.check("status: recording ok, newest segment fresh", rec.get("ok") is True and (rec.get("age_seconds") or 99) <= 30,
              f"age {rec.get('age_seconds')} s")
    r = c.get(f"/v1/cameras/{cam}/timeline", params={"start": utc(now - 3600), "end": utc(now - 30)}, headers=hdr(sign()))
    tl = r.json() if r.status_code == 200 else {}
    covered = sum(_span(p) for p in tl.get("recorded", []))
    rep.check("timeline of the last hour", r.status_code == 200 and covered > 0,
              f"{len(tl.get('recorded', []))} recorded periods, {covered:.0f} s, {len(tl.get('gaps', []))} gaps")
    r = c.get(f"/v1/cameras/{cam}/timeline", params={"start": utc(now - 90000), "end": utc(now)}, headers=hdr(sign()))
    rep.check("timeline over 24 h is refused", r.status_code == 413, f"HTTP {r.status_code}")

    # ---- security set
    claims = {"iss": "vivo-rails", "aud": a.aud, "sub": "u", "site_id": "s", "cams": [cam], "perms": ["live"],
              "iat": int(now), "exp": int(now) + 60, "jti": "m14"}
    good = sign()
    head, body, sig = good.split(".")
    forged_body = json.loads(base64.urlsafe_b64decode(body + "=="))
    forged_body["cams"] = [cam, "cam02", "cam03"]
    pub = key.public_key().public_bytes(*_pem())
    hs_head = b64({"alg": "HS256", "kid": kid, "typ": "JWT"})
    hs_sig = base64.urlsafe_b64encode(hmac.new(pub, f"{hs_head}.{b64(claims)}".encode(), hashlib.sha256).digest()).rstrip(b"=").decode()
    other_key = grant_issuer.load_key(grant_issuer.keygen(Path(tempfile.mkdtemp(prefix="m14-")), "unknown-kid"))
    cases = [
        ("no grant", {}, 401, "missing"),
        ("expired grant", hdr(sign(now=now - 120)), 401, "expired"),
        ("grant for another gateway", hdr(sign(aud="vivo-gw:someone-else")), 401, "bad_audience"),
        ("algorithm none", hdr(f"{b64({'alg': 'none', 'kid': kid})}.{b64(claims)}."), 401, "bad_algorithm"),
        ("HS256 signed with the public key", hdr(f"{hs_head}.{b64(claims)}.{hs_sig}"), 401, "bad_algorithm"),
        ("changed grant (cameras added)", hdr(f"{head}.{b64(forged_body)}.{sig}"), 401, "bad_signature"),
        ("key not in the gateway's key set", hdr(grant_issuer.sign(other_key, "unknown-kid", a.aud, [cam], ["live"])), 401, "unknown_key"),
        ("grant lives longer than 120 s", hdr(sign(ttl=600)), 401, "lifetime_too_long"),
    ]
    for name, h, want, _ in cases:
        r = c.get(f"/v1/cameras/{cam}", headers=h)
        rep.check(f"refused: {name}", r.status_code == want and r.json()["error"]["code"] == "unauthenticated", f"HTTP {r.status_code}")
    r = c.get("/v1/cameras/cam77", headers=hdr(sign(cams=[cam])))
    rep.check("refused: camera not in the grant (even if it does not exist)", r.status_code == 403, f"HTTP {r.status_code}")
    r = c.get(f"/v1/cameras/{cam}/timeline", params={"start": utc(now - 600), "end": utc(now)}, headers=hdr(sign(perms=["live"])))
    rep.check("refused: permission missing (timeline with a live-only grant)", r.status_code == 403, f"HTTP {r.status_code}")
    # changed links: a playback id with one character changed, and another user's clip
    pb = c.post(f"/v1/cameras/{cam}/playback", json={"start": utc(now - 300), "end": utc(now - 60)}, headers=hdr(sign()))
    if pb.status_code == 201:
        url = pb.json()["url"]
        pid = pb.json()["playback_id"]
        bad = url.replace(pid, pid[:-1] + ("A" if pid[-1] != "A" else "B"))
        rep.check("refused: changed playback link", c.get(bad).status_code == 404 and c.get(url).status_code == 200,
                  "changed id 404, original 200")
    else:
        rep.check("refused: changed playback link", False, f"could not start playback: HTTP {pb.status_code}")
    cl = c.post(f"/v1/cameras/{cam}/clips", json={"start": utc(now - 120), "end": utc(now - 60)}, headers=hdr(sign(sub="alice")))
    if cl.status_code == 202:
        cid = cl.json()["clip_id"]
        r = c.get(f"/v1/clips/{cid}", headers=hdr(sign(sub="mallory")))
        rep.check("refused: another user's clip", r.status_code == 403, f"HTTP {r.status_code}")
        c.delete(f"/v1/clips/{cid}", headers=hdr(sign(sub="alice")))
    else:
        rep.check("refused: another user's clip", False, f"could not ask for a clip: HTTP {cl.status_code}")

    # ---- audit log
    if a.audit_dir:
        time.sleep(0.5)
        lines = []
        for p in sorted(a.audit_dir.glob("*.jsonl")):
            lines += [json.loads(x) for x in p.read_text().splitlines() if x.strip()]
        mine = [x for x in lines if x.get("time", "") >= utc(started)[:-1]]
        reasons = {x.get("reason") for x in mine if x.get("event") == "grant_refused"}
        want = {"missing", "expired", "bad_audience", "bad_algorithm", "bad_signature", "unknown_key",
                "lifetime_too_long", "camera_not_in_grant", "missing_permission", "other_user"}
        rep.check("every refusal is in the audit log", want <= reasons, f"missing: {sorted(want - reasons) or 'none'}")
        text = "".join(p.read_text() for p in a.audit_dir.glob("*.jsonl"))
        rep.check("the audit log never holds a grant", good not in text and sig not in text, "")

    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    out = ROOT / "reports" / f"m14-check-{a.label}-{stamp}.md"
    out.parent.mkdir(exist_ok=True)
    out.write_text(rep.markdown(f"M14 check: {a.label}",
                                f"Run {utc(started)} against {a.api}, camera {cam}, audience {a.aud}."))
    print(f"report: {out}")
    return 0 if all(ok for _, ok, _ in rep.rows) else 1


def _span(p: dict) -> float:
    from datetime import datetime
    f = lambda s: datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()  # noqa: E731
    return f(p["end"]) - f(p["start"])


def _pem():
    from cryptography.hazmat.primitives import serialization
    return serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo


if __name__ == "__main__":
    sys.exit(main())
