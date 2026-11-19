#!/usr/bin/env python3
"""Which addresses the live video flowed between (M15 remote test), from a packet capture.

Reads `tcpdump -nn -tt -r CAPTURE` output (standard library only, so it runs on the gateway) and
sums packets and bytes per direction. A flow with the TURN server at one end is the relayed path;
otherwise the video went directly between the gateway and the viewer.

usage: tcpdump -nn -tt -r m15-direct.pcap | tests/pcap_summary.py [--turn 203.0.113.10] [--title "direct"]
"""
from __future__ import annotations

import argparse
import re
import sys
from collections import defaultdict

LINE = re.compile(r"^(?P<t>\d+\.\d+)\s+(?:\S+\s+(?:In|Out|M|P)\s+)?IP6?\s+(?P<src>\S+)\s+>\s+(?P<dst>[^:]+):"
                  r".*?(?:length|UDP, length)\s+(?P<len>\d+)")


def split(addr: str) -> tuple[str, str]:
    host, _, port = addr.rpartition(".")
    return host, port


def flows(lines) -> dict[tuple[str, str], dict]:
    out: dict[tuple[str, str], dict] = defaultdict(lambda: {"packets": 0, "bytes": 0, "first": None, "last": None})
    for line in lines:
        m = LINE.match(line.strip())
        if not m:
            continue
        f = out[(m["src"], m["dst"])]
        f["packets"] += 1
        f["bytes"] += int(m["len"])
        t = float(m["t"])
        f["first"] = t if f["first"] is None else f["first"]
        f["last"] = t
    return dict(out)


def markdown(fl: dict, turn: str | None, title: str) -> str:
    rows = sorted(fl.items(), key=lambda kv: -kv[1]["bytes"])
    lines = [f"### Packet capture: {title}", "",
             "| From | To | Packets | Bytes | Seconds | Path |", "|---|---|---|---|---|---|"]
    for (src, dst), f in rows[:12]:
        relay = turn and turn in (split(src)[0], split(dst)[0])
        lines.append(f"| {src} | {dst} | {f['packets']} | {f['bytes']:,} | {f['last'] - f['first']:.1f} | "
                     f"{'through TURN' if relay else 'direct'} |")
    if rows:
        (src, dst), f = rows[0]
        relay = turn and turn in (split(src)[0], split(dst)[0])
        lines += ["", f"The video ({f['bytes']:,} bytes) went from {src} to {dst}: "
                      f"**{'through the TURN server' if relay else 'directly, no relay'}**."]
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--turn", help="the TURN server's public address")
    ap.add_argument("--title", default="live view")
    a = ap.parse_args()
    print(markdown(flows(sys.stdin), a.turn, a.title))
    return 0


if __name__ == "__main__":
    sys.exit(main())
