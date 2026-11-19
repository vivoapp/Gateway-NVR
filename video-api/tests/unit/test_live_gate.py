"""The host helper vivo-live-gate: strict request checks and exactly the agreed rules."""
from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("live_gate", ROOT / "gateway" / "live_gate.py")
live_gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(live_gate)

NOW = 1000.0


@pytest.mark.parametrize("req,ok", [
    ({"ip": "203.0.113.7", "port": 51234, "until": 1120}, True),
    ({"ip": "2001:db8::1", "port": 5000, "until": 1100}, True),
    ({"ip": "203.0.113.7", "port": 51234, "until": 1601}, False),     # longer than 10 minutes
    ({"ip": "203.0.113.7", "port": 51234, "until": 999}, False),      # already over
    ({"ip": "203.0.113.7/24", "port": 1, "until": 1100}, False),
    ({"ip": "203.0.113.7 -j ACCEPT", "port": 1, "until": 1100}, False),
    ({"ip": "203.0.113.7", "port": "51234", "until": 1100}, False),
    ({"ip": "203.0.113.7", "port": 0, "until": 1100}, False),
    ({"ip": "203.0.113.7", "port": 70000, "until": 1100}, False),
    ({"ip": "203.0.113.7", "port": True, "until": 1100}, False),
    ({"port": 1, "until": 1100}, False),
    ({"ip": "203.0.113.7", "port": 51234, "release": True}, True),
    ({"ip": "203.0.113.7", "port": 51234, "release": True, "until": 9}, False),
    ({"ip": "203.0.113.7", "port": 51234, "release": 1}, False),
])
def test_parse_request(req, ok):
    assert (live_gate.parse_request(json.dumps(req), NOW) is not None) is ok
    assert live_gate.parse_request("not json", NOW) is None


def test_rules_only_touch_the_webrtc_port():
    rs = live_gate.rules("203.0.113.7", 51234, 8555)
    assert len(rs) == 4 and {c for c, _ in rs} == {"DOCKER-USER"}
    assert {c for c, _ in live_gate.rules("203.0.113.7", 1, 8555, ("INPUT", "OUTPUT"))} == {"INPUT", "OUTPUT"}
    for _, r in rs:
        assert r[-2:] == ["-j", "DROP"] and "vivo-live-gate" in r and "8555" in r and "51234" in r


class FakeIptables:
    def __init__(self):
        self.rules: list[tuple] = []
        self.calls: list[list[str]] = []

    def __call__(self, cmd, capture_output=True, text=True):
        self.calls.append(cmd)
        binary, args = cmd[0], cmd[3:]          # skip "-w 5"
        if args[0] == "-I":
            self.rules.insert(0, (binary, tuple(args[3:])))      # (chain DOCKER-USER, position 1)
            return subprocess.CompletedProcess(cmd, 0, "", "")
        if args[0] == "-D":
            key = (binary, tuple(args[2:]))
            if key in self.rules:
                self.rules.remove(key)
                return subprocess.CompletedProcess(cmd, 0, "", "")
            return subprocess.CompletedProcess(cmd, 1, "", "no rule")
        if args[0] == "-S":
            lines = [f"-A DOCKER-USER {' '.join(spec)}" for b, spec in self.rules if b == binary]
            return subprocess.CompletedProcess(cmd, 0, "\n".join(["-N DOCKER-USER", *lines]) + "\n", "")
        return subprocess.CompletedProcess(cmd, 1, "", "")


def test_gate_adds_then_releases(tmp_path):
    fake, clock = FakeIptables(), {"t": NOW}
    g = live_gate.Gate(tmp_path, 8555, run=fake, clock=lambda: clock["t"])
    (tmp_path / "requests").mkdir()
    (tmp_path / "requests" / "a.json").write_text(json.dumps({"ip": "203.0.113.7", "port": 51234, "until": 1120}))
    (tmp_path / "requests" / "b.json").write_text(json.dumps({"ip": "1.2.3.4; rm -rf /", "port": 1, "until": 1100}))
    g.step()
    assert len(fake.rules) == 4 and not list((tmp_path / "requests").iterdir())
    assert all(b == "iptables" for b, _ in fake.rules)
    clock["t"] = 1121
    g.step()
    assert fake.rules == [] and g.active == {}


def test_leftovers_are_removed_at_start(tmp_path):
    fake = FakeIptables()
    for _, r in live_gate.rules("198.51.100.2", 4000, 8555):
        fake.rules.append(("iptables", tuple(r)))
    fake.rules.append(("iptables", ("-s", "192.168.30.0/24", "-j", "RETURN")))      # not ours: kept
    g = live_gate.Gate(tmp_path, 8555, run=fake)
    assert g.flush_leftovers() == 4
    assert fake.rules == [("iptables", ("-s", "192.168.30.0/24", "-j", "RETURN"))]


def test_release_lifts_the_drop_early(tmp_path):
    fake, clock = FakeIptables(), {"t": NOW}
    g = live_gate.Gate(tmp_path, 8555, run=fake, clock=lambda: clock["t"])
    (tmp_path / "requests").mkdir()
    (tmp_path / "requests" / "1.json").write_text(json.dumps({"ip": "198.51.100.9", "port": 49170, "until": 1060}))
    g.step()
    assert len(fake.rules) == 4
    clock["t"] = 1007
    (tmp_path / "requests" / "2.json").write_text(json.dumps({"ip": "198.51.100.9", "port": 49170, "release": True}))
    g.step()
    assert fake.rules == [] and g.active == {}
