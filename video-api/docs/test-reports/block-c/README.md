# Block C test reports (M14 to M18)

The reports named in the Block C results pages ([block-c-1](../../block-c-1-results.md),
[block-c-2](../../block-c-2-results.md), [block-c-3](../../block-c-3-results.md)), copied from
`video-api/reports` on the workstation. All times are UTC. They hold measurements only, no footage,
pictures or grants.

## On the Vivo reference gateway (vivo-n150, real Frigate 0.18.0 recordings)

| File | What it is |
|---|---|
| m14-check-gateway-20261122-150122.md | M14: every route, the security set and the audit log, 21 of 21 |
| m16-probe-gateway-20261122-151825.md | M16 on the gateway itself (the building-network figure): first picture and 20 seeks over 24 hours |
| m16-playback-gateway-vpn-20261122T151938.md | M16 in a real browser (hls.js, headless Chrome) on the workstation in Korea, over the VPN (about 340 ms round trip) |
| m16-playback-gateway-vpn-20261122T151314.md | the same, 6 minutes earlier; kept as the raw record |
| m17-clips-gateway-20261122-152051.md | M17: clips, 8 of 8; the clip was checked on the gateway and never left it |
| m18-contract-20261123-123627.md | M18: the contract suite against Frigate on the gateway (4 of 4) and against the mock on the workstation (4 of 4) |

## On the workstation lab (Nov 8): Frigate 0.18.0 and go2rtc 1.9.14 with four fake cameras

| File | What it is |
|---|---|
| m14-check-lab-20261108-061624.md | M14, 21 of 21 |
| m15-live-lab-20261108T063719.md | M15 on the building network: first frame and the cut on a refused renewal |
| m15-live-lab-turn-20261108T064311.md | M15 through TURN, 0 of 10: the run that showed findings F4 (relay ports reused); fixed, re-run pending with Vivo's server |
| m16-playback-lab-20261108T061936.md | M16, 14 minutes of footage |
| m17-clips-lab-20261108-062240.md | M17, with the keep time set to 60 s |

## On the workstation against the mock recorder (Nov 22)

| File | What it is |
|---|---|
| m16-playback-mock-mac-20261122T141617.md | M16 through the mock recorder, 2 of 2 |
| m17-clips-mock-mac-20261122-141805.md | M17 through the mock, 8 of 9: the clip read 1,790 s long (findings F9); kept as the raw record |
| m17-clips-mock-mac-20261122-142353.md | the same after the fix, 9 of 9 |
