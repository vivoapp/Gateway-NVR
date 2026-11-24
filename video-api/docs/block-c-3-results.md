# Block C, milestone "M15, M18": results

Status: **M18 passes, against the real Frigate on the Vivo reference gateway and against the mock
recorder** (Nov 23, 2026). **M15 is built and passes in the lab; its gateway and remote tests are
still to run**, and the 72-hour test follows them.

Where it was tested:
- **Lab** (Nov 8): Frigate 0.18.0 and go2rtc 1.9.14 with four fake cameras; the substream was the
  H.264 one planned for live view (640x360, 15 fps, a keyframe every second).
- **Gateway** (Nov 23): the reference N150 (vivo-n150) with its real recordings.

The reports are in [test-reports/block-c](test-reports/block-c/README.md).

## M15: Live-view delivery

| Done when (contract section 13) | Lab (Nov 8) | Evidence | Gateway / remote |
|---|---|---|---|
| First frame on the building network within 2 s (median of 10) | Pass: median 0.53 s, 10 of 10 played | `m15-live-lab-20261108T063719` | Over the VPN (Nov 24, cam01 H.264 substream, about 340 ms round trip): median 2.00 s, 10 of 10 played (`m15-live-gateway-vpn-20261124T123921`); the building-network run is still to do |
| A refused renewal cuts the video | Pass: renewal refused (403), video stopped 0.46 s later, go2rtc dropped the viewer within 4 s | `m15-live-lab-20261108T063719` | **Pass** over the VPN (Nov 24): renewal refused, video stopped 363 ms later, drop rule removed 6 s after (`m15-live-gateway-vpn-20261124T123921`), after fixing F11 (the first run failed) |
| Through TURN within 5 s (median of 10) | Relayed path works in the lab: 0.5 to 1.5 s over 5 attempts (coturn, UDP). The 10-attempt run then failed because of findings F4; fixed, re-run pending | lab check, Nov 8; `m15-live-lab-turn-20261108T064311` (before the fix) | remote test with Vivo's server |
| From outside the site within 3 s direct; packet captures show the path | Needs a public server and a phone on cellular | `tests/m15_capture.sh` ready | remote test |
| Limits answer 429 | Pass (unit tests: per-site remote limit and per-camera viewer limit) | `tests/unit/test_live.py` | in `m15_load` |
| 10 cameras plus 10 viewers: one camera connection per stream, no gap, CPU peak under 85% | Script ready (the lab has 4 cameras) | `tests/m15_load.mjs` | to run |
| Live view comes back on its own within 2 minutes after a restart | Covered by the 72-hour test | `tests/soak72.py` | to run |

What the gateway run needs:
1. The go2rtc WebRTC settings on the gateway (`recorder/tools/gen_config.py`). go2rtc restarts, so
   recording pauses for about 30 s on all cameras, in a window announced to the group.
2. The H.264 substream for phones: one camera first, measured for 24 hours, and the rest only after
   Simon and Haris agree (it changes the upload size).
3. For the remote rows: Vivo's public server for TURN and the test tunnel, and UDP 8555 forwarded from
   the site's router to the gateway.

## M18: Second-adapter mock validation

| Done when | Lab (Nov 8) | Gateway and workstation (Nov 23) | Evidence |
|---|---|---|---|
| The same contract test suite passes against Frigate and against a mock recorder, switched by one setting | Pass, 8 of 8 | **Pass, 8 of 8**: 4 of 4 against the real Frigate on the gateway (timeline, playback playlist and pieces, a clip, an empty range), 4 of 4 against the mock on the workstation, at commit 289d162 | `m18-contract-20261123-123627` |

The same M16 and M17 scripts that ran on the gateway also passed through the mock recorder (block-c-2,
"Against the mock recorder"), so more than the contract suite works the same on both.

## 72-hour test

The script, its pass/fail rules (unit tested) and the run instructions are ready
(`tests/soak72.py`). It runs on the gateway after M15's gateway tests.

## Findings

See [findings.md](findings.md): go2rtc's alias streams cannot separate viewers (F1), the cut by
dropping packets (F2), go2rtc masking its secrets in its API answers (F3), and TURN relay ports being
reused (F4, fixed).

## Open points

- Vivo: the public server for TURN and the test tunnel; Jorge: UDP 8555 forwarded to the gateway.
- The go2rtc settings window on the gateway, then the M15 building-network and load tests.
- The H.264 substream on one camera, then the rest after Simon and Haris agree.
- The TURN re-run after the F4 fix, the remote test with packet captures, and the 72-hour test.
