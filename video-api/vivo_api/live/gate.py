"""Cutting one live viewer off (ADR 0002).

The API runs without privileges. To stop go2rtc's video to one viewer it writes a small request
file; the host helper `vivo-live-gate` (gateway/live_gate.py, root) drops that viewer's address and
port on go2rtc's WebRTC port for a while, and go2rtc then closes the peer by itself.
"""
from __future__ import annotations

import ipaddress
import json
import os
import secrets
import time
from pathlib import Path


def parse_remote_addr(value: str | None) -> tuple[str, int] | None:
    """go2rtc's consumer `remote_addr`, e.g. '203.0.113.7:51234 prflx' or '[2001:db8::1]:5000 host'."""
    if not value:
        return None
    addr = value.split()[0]
    host, _, port = addr.rpartition(":")
    host = host.strip("[]")
    try:
        ip = ipaddress.ip_address(host)
        p = int(port)
    except ValueError:
        return None
    if not 1 <= p <= 65535:
        return None
    return str(ip), p


class Gate:
    def __init__(self, folder: Path, hold: int = 60, clock=time.time):
        self.requests = Path(folder) / "requests"
        self.hold = hold
        self.clock = clock

    def cut(self, ip: str, port: int) -> bool:
        """Ask the helper to drop this viewer, for at most `hold` seconds. False when it cannot be asked."""
        return self._write({"ip": ip, "port": int(port), "until": int(self.clock()) + self.hold})

    def release(self, ip: str, port: int) -> bool:
        """go2rtc has closed the viewer: lift the drop now. A TURN server reuses its relay ports, so a
        drop that outlives the viewer could block the next viewer who gets the same port."""
        return self._write({"ip": ip, "port": int(port), "release": True})

    def _write(self, req: dict) -> bool:
        try:
            self.requests.mkdir(parents=True, exist_ok=True)
            name = f"{int(self.clock() * 1000)}-{secrets.token_hex(4)}"
            tmp = self.requests / f".{name}.tmp"
            tmp.write_text(json.dumps(req))
            os.replace(tmp, self.requests / f"{name}.json")
            return True
        except OSError:
            return False
