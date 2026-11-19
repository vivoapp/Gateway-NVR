#!/usr/bin/env python3
"""vivo-live-gate: cuts one live viewer off when the Video API asks (ADR 0002).

The Video API runs without privileges and writes request files into
/run/vivo-live-gate/requests/:  {"ip": "203.0.113.7", "port": 51234, "until": <unix time>}
and, once go2rtc has closed that viewer:  {"ip": "203.0.113.7", "port": 51234, "release": true}
This service (root) checks each request strictly and adds two DROP rules to DOCKER-USER that
match only that address and port on go2rtc's WebRTC port, removes them at `until`, and removes
any of its rules left over when it starts. Nothing else on the gateway is touched.

usage: live_gate.py [--dir /run/vivo-live-gate] [--port 8555] [--once] [--lab]
"""
from __future__ import annotations

import argparse
import ipaddress
import json
import os
import subprocess
import sys
import time
from pathlib import Path

TAG = "vivo-live-gate"
MAX_HOLD = 600            # seconds; a request asking for longer is refused
MAX_ACTIVE = 64           # at most this many viewers cut off at once


def parse_request(text: str, now: float) -> tuple[str, int, int | None] | None:
    """(ip, port, until) from a request file; until is None for a release. None when anything is off."""
    try:
        req = json.loads(text)
        ip = str(ipaddress.ip_address(req["ip"]))
        port = req["port"]
    except (ValueError, KeyError, TypeError):
        return None
    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
        return None
    if req.get("release") is True and set(req) == {"ip", "port", "release"}:
        return ip, port, None
    until = req.get("until")
    if not isinstance(until, int) or isinstance(until, bool) or until <= now or until > now + MAX_HOLD:
        return None
    return ip, port, until


def rules(ip: str, port: int, webrtc_port: int, chains: tuple[str, str] = ("DOCKER-USER", "DOCKER-USER")
          ) -> list[tuple[str, list[str]]]:
    """(chain, rule) pairs that drop one viewer: both directions, UDP and TCP, only on go2rtc's WebRTC
    port. On the gateway both go in DOCKER-USER (go2rtc's traffic is forwarded to its container);
    the lab runs this inside go2rtc's own network namespace and uses INPUT and OUTPUT."""
    inbound, outbound = chains
    out = []
    for proto in ("udp", "tcp"):
        out.append((inbound, ["-p", proto, "-s", ip, "--sport", str(port), "--dport", str(webrtc_port),
                              "-m", "comment", "--comment", TAG, "-j", "DROP"]))
        out.append((outbound, ["-p", proto, "-d", ip, "--dport", str(port), "--sport", str(webrtc_port),
                               "-m", "comment", "--comment", TAG, "-j", "DROP"]))
    return out


def tool(ip: str) -> str:
    return "ip6tables" if ipaddress.ip_address(ip).version == 6 else "iptables"


class Gate:
    def __init__(self, folder: Path, webrtc_port: int = 8555, run=subprocess.run, clock=time.time,
                 chains: tuple[str, str] = ("DOCKER-USER", "DOCKER-USER")):
        self.requests = Path(folder) / "requests"
        self.webrtc_port = webrtc_port
        self.chains = chains
        self.run = run
        self.clock = clock
        self.active: dict[tuple[str, int], int] = {}

    def _ipt(self, binary: str, args: list[str]) -> bool:
        r = self.run([binary, "-w", "5", *args], capture_output=True, text=True)
        return r.returncode == 0

    def flush_leftovers(self) -> int:
        removed = 0
        for binary in ("iptables", "ip6tables"):
            for chain in dict.fromkeys(self.chains):
                r = self.run([binary, "-w", "5", "-S", chain], capture_output=True, text=True)
                if r.returncode != 0:
                    continue
                for line in r.stdout.splitlines():
                    if f"--comment {TAG}" in line and line.startswith(f"-A {chain} "):
                        spec = line.split()[2:]
                        if self._ipt(binary, ["-D", chain, *spec]):
                            removed += 1
        return removed

    def add(self, ip: str, port: int, until: int) -> bool:
        key = (ip, port)
        if key in self.active:
            self.active[key] = max(self.active[key], until)
            return True
        if len(self.active) >= MAX_ACTIVE:
            return False
        ok = all(self._ipt(tool(ip), ["-I", chain, "1", *r])
                 for chain, r in rules(ip, port, self.webrtc_port, self.chains))
        if ok:
            self.active[key] = until
        else:
            self.remove(ip, port)
        return ok

    def remove(self, ip: str, port: int) -> None:
        for chain, r in rules(ip, port, self.webrtc_port, self.chains):
            while self._ipt(tool(ip), ["-D", chain, *r]):
                pass
        self.active.pop((ip, port), None)

    def step(self) -> None:
        now = self.clock()
        if self.requests.is_dir():
            for p in sorted(self.requests.glob("*.json")):
                try:
                    text = p.read_text()
                    p.unlink()
                except OSError:
                    continue
                req = parse_request(text, now)
                if req is None:
                    print(f"{TAG}: refused request {p.name}", flush=True)
                    continue
                ip, port, until = req
                if until is None:
                    if (ip, port) in self.active:
                        self.remove(ip, port)
                        print(f"{TAG}: released {ip}:{port} (viewer gone)", flush=True)
                    continue
                ok = self.add(ip, port, until)
                print(f"{TAG}: {'dropping' if ok else 'could not drop'} {ip}:{port} until {until}", flush=True)
        for (ip, port), until in list(self.active.items()):
            if until <= now:
                self.remove(ip, port)
                print(f"{TAG}: released {ip}:{port}", flush=True)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dir", type=Path, default=Path(os.environ.get("LIVE_GATE_DIR", "/run/vivo-live-gate")))
    ap.add_argument("--port", type=int, default=int(os.environ.get("WEBRTC_PORT", "8555")))
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--lab", action="store_true", help="inside go2rtc's network namespace: INPUT and OUTPUT chains")
    a = ap.parse_args()
    gate = Gate(a.dir, a.port, chains=("INPUT", "OUTPUT") if a.lab else ("DOCKER-USER", "DOCKER-USER"))
    print(f"{TAG}: removed {gate.flush_leftovers()} old rules; watching {gate.requests}", flush=True)
    while True:
        gate.step()
        if a.once:
            return 0
        time.sleep(0.5)


if __name__ == "__main__":
    sys.exit(main())
