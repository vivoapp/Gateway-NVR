"""The packet-capture summary of the M15 remote test (tests/pcap_summary.py)."""
from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("pcap_summary", ROOT / "tests" / "pcap_summary.py")
ps = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ps)

DIRECT = """\
1794120000.100000 IP 192.168.30.200.8555 > 203.0.113.7.51234: UDP, length 1200
1794120000.200000 IP 192.168.30.200.8555 > 203.0.113.7.51234: UDP, length 1100
1794120000.300000 IP 203.0.113.7.51234 > 192.168.30.200.8555: UDP, length 120
reading from file x.pcap, link-type EN10MB
"""
RELAY = """\
1794120000.100000 enp1s0 Out IP 192.168.30.200.8555 > 198.51.100.10.49170: UDP, length 1200
1794120001.100000 enp1s0 Out IP 192.168.30.200.8555 > 198.51.100.10.49170: UDP, length 1200
1794120001.200000 enp1s0 In  IP 198.51.100.10.49170 > 192.168.30.200.8555: UDP, length 100
"""


def test_direct_path():
    fl = ps.flows(DIRECT.splitlines())
    assert fl[("192.168.30.200.8555", "203.0.113.7.51234")] == {"packets": 2, "bytes": 2300, "first": 1794120000.1, "last": 1794120000.2}
    md = ps.markdown(fl, "198.51.100.10", "direct")
    assert "directly, no relay" in md and "203.0.113.7.51234" in md


def test_relayed_path_with_interface_prefix():
    fl = ps.flows(RELAY.splitlines())
    assert fl[("192.168.30.200.8555", "198.51.100.10.49170")]["bytes"] == 2400
    assert "through the TURN server" in ps.markdown(fl, "198.51.100.10", "turn")
