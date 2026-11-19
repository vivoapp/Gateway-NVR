"""Rails grants: ES256 JWTs checked on every request (contract section 3).

The gateway holds only public keys (a JWKS file, reloaded when it changes). A grant names one
gateway (`aud`), the cameras (`cams`) and the permissions (`perms`), and lives at most
GRANT_MAX_LIFETIME seconds.
"""
from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import jwt
from jwt.algorithms import ECAlgorithm

CAMERA_ID = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
PERMS = frozenset({"live", "playback", "export"})
ALGORITHM = "ES256"


class GrantError(Exception):
    """A grant failed a check. `reason` goes to the audit log; the client sees 401."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class Grant:
    sub: str
    site_id: str
    cams: frozenset
    perms: frozenset
    jti: str
    iat: float
    exp: float

    def audit_fields(self) -> dict:
        return {"sub": self.sub, "site_id": self.site_id, "jti": self.jti}


class KeySet:
    """Rails public keys by `kid`, from a JWKS file. Reloaded when the file changes."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._stamp: tuple | None = None
        self._keys: dict = {}

    def _load(self) -> None:
        try:
            st = self.path.stat()
        except OSError:
            self._keys, self._stamp = {}, None
            return
        stamp = (st.st_mtime_ns, st.st_size)
        if stamp == self._stamp:
            return
        keys = {}
        try:
            data = json.loads(self.path.read_text())
            for jwk in data.get("keys", []):
                if jwk.get("kty") != "EC" or jwk.get("crv") != "P-256" or not jwk.get("kid"):
                    continue
                if "d" in jwk:                      # never accept a private key on the gateway
                    continue
                keys[jwk["kid"]] = ECAlgorithm.from_jwk(json.dumps(jwk))
        except (OSError, ValueError, jwt.InvalidKeyError):
            keys = {}
        self._keys, self._stamp = keys, stamp

    def get(self, kid: str):
        with self._lock:
            self._load()
            return self._keys.get(kid)

    def kids(self) -> list[str]:
        with self._lock:
            self._load()
            return sorted(self._keys)


class GrantChecker:
    def __init__(self, keys: KeySet, audience: str, issuer: str = "vivo-rails",
                 max_lifetime: int = 120, leeway: int = 5, clock=time.time):
        self.keys = keys
        self.audience = audience
        self.issuer = issuer
        self.max_lifetime = max_lifetime
        self.leeway = leeway
        self.clock = clock

    def verify(self, token: str) -> Grant:
        if not token or token.count(".") != 2:
            raise GrantError("malformed")
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError:
            raise GrantError("malformed") from None
        if header.get("alg") != ALGORITHM:
            raise GrantError("bad_algorithm")
        kid = header.get("kid")
        if not isinstance(kid, str) or not kid:
            raise GrantError("unknown_key")
        key = self.keys.get(kid)
        if key is None:
            raise GrantError("unknown_key")
        try:
            claims = jwt.decode(
                token, key, algorithms=[ALGORITHM], audience=self.audience, issuer=self.issuer,
                options={"verify_exp": False, "verify_iat": False, "verify_nbf": False,
                         "require": ["iss", "aud", "sub", "exp", "iat", "jti"]})
        except jwt.InvalidAudienceError:
            raise GrantError("bad_audience") from None
        except jwt.InvalidIssuerError:
            raise GrantError("bad_issuer") from None
        except jwt.InvalidSignatureError:
            raise GrantError("bad_signature") from None
        except jwt.MissingRequiredClaimError:
            raise GrantError("bad_claims") from None
        except jwt.PyJWTError:
            raise GrantError("malformed") from None

        now = self.clock()
        exp, iat = claims.get("exp"), claims.get("iat")
        if not isinstance(exp, (int, float)) or not isinstance(iat, (int, float)) \
                or isinstance(exp, bool) or isinstance(iat, bool):
            raise GrantError("bad_claims")
        if exp <= now - self.leeway:
            raise GrantError("expired")
        if iat > now + self.leeway:
            raise GrantError("not_yet_valid")
        if exp - iat > self.max_lifetime:
            raise GrantError("lifetime_too_long")

        sub, site, jti = claims.get("sub"), claims.get("site_id"), claims.get("jti")
        cams, perms = claims.get("cams"), claims.get("perms")
        if not (isinstance(sub, str) and sub and isinstance(jti, str) and jti and isinstance(site, str)):
            raise GrantError("bad_claims")
        if not isinstance(cams, list) or not all(isinstance(c, str) and CAMERA_ID.match(c) for c in cams):
            raise GrantError("bad_claims")
        if not isinstance(perms, list) or not perms or not all(p in PERMS for p in perms):
            raise GrantError("bad_claims")
        return Grant(sub=sub, site_id=site, cams=frozenset(cams), perms=frozenset(perms),
                     jti=jti, iat=float(iat), exp=float(exp))
