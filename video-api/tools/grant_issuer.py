#!/usr/bin/env python3
"""Test grant issuer: a stand-in for Rails (contract section 3), for tests and demos only.

Keys are made at test time and never committed. The gateway gets only the public key set.

usage:
  tools/grant_issuer.py keygen --dir DIR [--kid test-1]
      writes DIR/<kid>.private.pem (mode 600) and adds the public key to DIR/rails.jwks.json
  tools/grant_issuer.py sign --key DIR/test-1.private.pem --aud vivo-gw:vivo-n150 \\
      --cams cam01,cam03 --perms live,playback [--sub user-1] [--site site-test] [--ttl 60]
      prints one grant
  tools/grant_issuer.py serve --key DIR/test-1.private.pem --aud vivo-gw:vivo-n150 [--port 8099]
      GET http://127.0.0.1:8099/grant?cams=cam01&perms=live&ttl=60 -> a grant (for the test player)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from jwt.algorithms import ECAlgorithm


def keygen(folder: Path, kid: str = "test-1") -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    key = ec.generate_private_key(ec.SECP256R1())
    priv = folder / f"{kid}.private.pem"
    priv.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                       serialization.NoEncryption()))
    os.chmod(priv, 0o600)
    jwk = json.loads(ECAlgorithm.to_jwk(key.public_key()))
    jwk.update({"kid": kid, "use": "sig", "alg": "ES256"})
    jwks_path = folder / "rails.jwks.json"
    try:
        jwks = json.loads(jwks_path.read_text())
    except (OSError, ValueError):
        jwks = {"keys": []}
    jwks["keys"] = [k for k in jwks.get("keys", []) if k.get("kid") != kid] + [jwk]
    jwks_path.write_text(json.dumps(jwks, indent=2) + "\n")
    return priv


def load_key(path: Path):
    return serialization.load_pem_private_key(Path(path).read_bytes(), password=None)


def sign(key, kid: str, aud: str, cams, perms, sub: str = "test-user", site: str = "site-test",
         ttl: int = 60, iss: str = "vivo-rails", now: float | None = None, **extra) -> str:
    now = int(time.time() if now is None else now)
    claims = {"iss": iss, "aud": aud, "sub": sub, "site_id": site, "cams": list(cams), "perms": list(perms),
              "iat": now, "exp": now + int(ttl), "jti": str(uuid.uuid4())}
    claims.update(extra)
    return jwt.encode(claims, key, algorithm="ES256", headers={"kid": kid, "typ": "JWT"})


def kid_of(path: Path) -> str:
    return Path(path).name.split(".private.pem")[0]


LOCAL_ORIGIN = ("http://127.0.0.1", "http://localhost", "https://127.0.0.1", "https://localhost")


def serve(key_path: Path, aud: str, port: int) -> None:
    """Test stand-in for Rails, on 127.0.0.1 only. It hands a grant to anyone who asks, so it refuses
    to start on a gateway (where /etc/vivo/video-api.env exists)."""
    if Path("/etc/vivo/video-api.env").exists():
        raise SystemExit("refused: this looks like a gateway (/etc/vivo/video-api.env exists); "
                         "use 'sign' for single test grants there")
    key, kid = load_key(key_path), kid_of(key_path)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            u = urlsplit(self.path)
            if u.path != "/grant":
                self.send_error(404)
                return
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            token = sign(key, kid, aud, q.get("cams", "").split(","), q.get("perms", "live").split(","),
                         sub=q.get("sub", "test-user"), ttl=int(q.get("ttl", "60")))
            body = token.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            origin = self.headers.get("Origin", "")
            if origin.startswith(LOCAL_ORIGIN):          # the test player, served from this computer
                self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_):
            pass

    print(f"test grant issuer on http://127.0.0.1:{port}/grant (kid {kid}, aud {aud})", flush=True)
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    k = sub.add_parser("keygen")
    k.add_argument("--dir", required=True, type=Path)
    k.add_argument("--kid", default="test-1")
    s = sub.add_parser("sign")
    s.add_argument("--key", required=True, type=Path)
    s.add_argument("--aud", required=True)
    s.add_argument("--cams", required=True)
    s.add_argument("--perms", default="live")
    s.add_argument("--sub", default="test-user")
    s.add_argument("--site", default="site-test")
    s.add_argument("--ttl", type=int, default=60)
    v = sub.add_parser("serve")
    v.add_argument("--key", required=True, type=Path)
    v.add_argument("--aud", required=True)
    v.add_argument("--port", type=int, default=8099)
    a = ap.parse_args()
    if a.cmd == "keygen":
        print(keygen(a.dir, a.kid))
    elif a.cmd == "sign":
        print(sign(load_key(a.key), kid_of(a.key), a.aud, a.cams.split(","), a.perms.split(","),
                   sub=a.sub, site=a.site, ttl=a.ttl))
    else:
        serve(a.key, a.aud, a.port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
