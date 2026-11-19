"""Playback sessions (M16, contract section 7).

A playback id is a capability for one camera and one time range, valid until the grant that
opened (or last renewed) it expires. The HLS playlist and pieces are fetched through the
recorder adapter; playlists are rewritten so every piece is a plain name under the playback URL.
"""
from __future__ import annotations

import re
import secrets
from dataclasses import dataclass

from .errors import ApiError
from .recorders.base import PIECE
from .timeutil import fmt

_URI_ATTR = re.compile(r'URI="([^"]*)"')


@dataclass
class PlaybackSession:
    pid: str
    camera: str
    start: float
    end: float
    sub: str
    site_id: str
    jti: str
    client_ip: str
    remote: bool
    created: float
    expires: float
    closed: bool = False


def _piece_name(uri: str) -> str | None:
    """Last path part of a playlist URI, if it is a plain piece name we can serve."""
    name = uri.split("?", 1)[0].rstrip("/").rsplit("/", 1)[-1]
    return name if PIECE.match(name) else None


def rewrite_playlist(text: str) -> str:
    """Keep only plain piece names in a playlist, so the player asks the API for every piece.

    Lines that are URIs and URI="..." attributes (EXT-X-MAP, EXT-X-MEDIA) are reduced to their last
    path part; a URI that is not a plain piece name is dropped together with its EXTINF line.
    """
    out: list[str] = []
    for line in text.splitlines():
        s = line.strip()
        if not s:
            continue
        if s.startswith("#"):
            bad = False

            def sub(m):
                nonlocal bad
                name = _piece_name(m.group(1))
                if name is None:
                    bad = True
                    return m.group(0)
                return f'URI="{name}"'

            s = _URI_ATTR.sub(sub, s)
            if not bad:
                out.append(s)
            continue
        name = _piece_name(s)
        if name is None:
            if out and out[-1].startswith("#EXTINF"):
                out.pop()
            continue
        out.append(name)
    return "\n".join(out) + "\n"


class PlaybackManager:
    def __init__(self, ctx):
        self.ctx = ctx
        self.sessions: dict[str, PlaybackSession] = {}

    def _purge(self) -> None:
        now = self.ctx.clock()
        for pid, p in list(self.sessions.items()):
            if not p.closed and p.expires <= now:
                p.closed = True
                self.ctx.audit.write("playback_end", "allowed", "expired", camera_id=p.camera, object_id=pid,
                                     sub=p.sub, site_id=p.site_id, jti=p.jti, client_ip=p.client_ip)
            if p.closed and now - p.expires > 600:
                del self.sessions[pid]

    def create(self, grant, camera: str, start: float, end: float, client_ip: str, remote: bool) -> PlaybackSession:
        self._purge()
        p = PlaybackSession(pid="pb_" + secrets.token_urlsafe(16), camera=camera, start=start, end=end,
                            sub=grant.sub, site_id=grant.site_id, jti=grant.jti, client_ip=client_ip,
                            remote=remote, created=self.ctx.clock(), expires=grant.exp)
        self.sessions[p.pid] = p
        self.ctx.audit.write("playback_start", grant=grant, camera_id=camera, object_id=p.pid,
                             start=fmt(start), end=fmt(end), client_ip=client_ip, remote=remote)
        return p

    def get(self, pid: str) -> PlaybackSession:
        self._purge()
        p = self.sessions.get(pid)
        if p is None or p.closed:
            raise ApiError(404, "not_found")
        return p

    def renew(self, p: PlaybackSession, grant) -> float:
        p.expires = max(p.expires, grant.exp)
        return p.expires

    def close(self, p: PlaybackSession, reason: str) -> None:
        if p.closed:
            return
        p.closed = True
        self.ctx.audit.write("playback_end", "allowed", reason, camera_id=p.camera, object_id=p.pid, sub=p.sub,
                             site_id=p.site_id, jti=p.jti, client_ip=p.client_ip)

