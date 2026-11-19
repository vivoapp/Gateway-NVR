"""Everything a request needs, and the grant check every protected route starts with."""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from fastapi import Request

from .audit import Audit
from .config import Settings
from .errors import ApiError
from .grants import CAMERA_ID, Grant, GrantChecker, GrantError
from .inventory import Inventory


@dataclass
class Context:
    settings: Settings
    checker: GrantChecker
    inventory: Inventory
    audit: Audit
    recorder: object
    go2rtc: object = None
    health: object = None
    watch: object = None
    live: object = None
    playbacks: object = None
    clips: object = None
    clock: object = field(default=time.time)

    def client_ip(self, request: Request) -> str:
        peer = request.client.host if request.client else ""
        # Remote requests arrive on loopback through the outbound connection; the far end of that
        # connection adds X-Forwarded-For. Trusted only from loopback, and only for the audit log.
        if peer in ("127.0.0.1", "::1"):
            fwd = request.headers.get("x-forwarded-for", "").split(",")[0].strip()
            if fwd:
                return fwd
        return peer

    def is_remote(self, request: Request) -> bool:
        peer = request.client.host if request.client else ""
        return not self.settings.is_local(peer)

    def grant(self, request: Request) -> Grant:
        auth = request.headers.get("authorization", "")
        scheme, _, token = auth.partition(" ")
        if scheme.lower() != "bearer" or not token.strip():
            self.audit.write("grant_refused", "denied", "missing", client_ip=self.client_ip(request),
                             route=request.url.path)
            raise ApiError(401, "unauthenticated")
        try:
            return self.checker.verify(token.strip())
        except GrantError as exc:
            self.audit.write("grant_refused", "denied", exc.reason, client_ip=self.client_ip(request),
                             route=request.url.path)
            raise ApiError(401, "unauthenticated") from None

    def authorize(self, request: Request, camera_id: str | None, any_of: tuple[str, ...],
                  owner: str | None = None) -> Grant:
        """Check the grant, the permission, the camera and (for an existing object) its owner."""
        if camera_id is not None and not CAMERA_ID.match(camera_id):
            raise ApiError(400, "invalid_argument", "camera_id is not a valid camera id.")
        g = self.grant(request)
        reason = None
        if not g.perms & set(any_of):
            reason = "missing_permission"
        elif camera_id is not None and camera_id not in g.cams:
            reason = "camera_not_in_grant"
        elif owner is not None and owner != g.sub:
            reason = "other_user"
        if reason:
            self.audit.write("grant_refused", "denied", reason, grant=g, camera_id=camera_id,
                             client_ip=self.client_ip(request), route=request.url.path)
            raise ApiError(403, "forbidden")
        if camera_id is not None and self.inventory.get(camera_id) is None:
            raise ApiError(404, "camera_not_found")
        return g


def ctx(request: Request) -> Context:
    return request.app.state.ctx
