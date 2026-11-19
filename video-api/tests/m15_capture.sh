#!/usr/bin/env bash
# M15 remote test, on the gateway: capture packet headers of live view for SECONDS while a viewer
# watches from outside (a phone on cellular), then say which path the video took.
#
# usage: sudo tests/m15_capture.sh LABEL SECONDS [TURN_IP]
#   LABEL    direct | turn (part of the file names)
#   TURN_IP  the TURN server's public address (also captured, and marks the relayed flows)
#
# Only the first 96 bytes of each packet are kept (addresses and lengths, no video), into
# reports/m15-<LABEL>-<UTC>.pcap; the summary goes to reports/m15-<LABEL>-<UTC>-paths.md.
# The captures hold the viewer's public address: they go in the milestone ZIP, never in Git.
set -euo pipefail
LABEL="${1:?usage: m15_capture.sh LABEL SECONDS [TURN_IP]}"
SECONDS_="${2:?seconds}"
TURN="${3:-}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUT="$HERE/../reports"
mkdir -p "$OUT"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
IFACE="$(ip -o route get 1.1.1.1 | sed -n 's/.* dev \([^ ]*\).*/\1/p')"
FILTER="udp port 8555"
[[ -n "$TURN" ]] && FILTER="($FILTER) or (udp and host $TURN)"
PCAP="$OUT/m15-$LABEL-$STAMP.pcap"
echo "capturing on $IFACE for $SECONDS_ s: $FILTER"
timeout "$SECONDS_" tcpdump -i "$IFACE" -nn -s 96 -w "$PCAP" "$FILTER" || true
tcpdump -nn -tt -r "$PCAP" 2>/dev/null | python3 "$HERE/pcap_summary.py" ${TURN:+--turn "$TURN"} --title "$LABEL" \
  | tee "$OUT/m15-$LABEL-$STAMP-paths.md"
echo "capture: $PCAP"
