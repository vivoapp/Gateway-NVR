"""ICE servers for a live session, with TURN credentials valid for that session only.

coturn's `use-auth-secret` scheme (the "TURN REST API"): username = "<expiry>:<session>",
password = base64(HMAC-SHA1(shared secret, username)). coturn checks both without a database, and
refuses the username after its expiry.
"""
from __future__ import annotations

import base64
import hashlib
import hmac


def credentials(secret: str, session_id: str, ttl: int, now: float) -> tuple[str, str]:
    username = f"{int(now) + ttl}:{session_id}"
    digest = hmac.new(secret.encode(), username.encode(), hashlib.sha1).digest()
    return username, base64.b64encode(digest).decode()


def ice_servers(stun_urls, turn_urls, secret: str, session_id: str, ttl: int, now: float) -> list[dict]:
    out = []
    if stun_urls:
        out.append({"urls": list(stun_urls)})
    if turn_urls and secret:
        user, cred = credentials(secret, session_id, ttl, now)
        out.append({"urls": list(turn_urls), "username": user, "credential": cred})
    return out
