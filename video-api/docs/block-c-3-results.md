# Block C, milestone "M15, M18": results

Status: **built and tested on the workstation lab; the remote test needs Vivo's public server, and
the gateway runs follow when the milestone is funded.** Lab: Frigate 0.18.0 and go2rtc 1.9.14 with
four fake cameras; the substream was the H.264 one planned for live view (640x360, 15 fps, a
keyframe every second).

## M15: Live-view delivery

| Done when (contract section 13) | Lab result (Nov 8, 2026) | Evidence | Gateway / remote |
|---|---|---|---|
| First frame on the building network within 2 s (median of 10) | Pass: median 0.53 s, 10 of 10 played | `m15-live-lab-20261108T063719` | after funding |
| A refused renewal cuts the video | Pass: renewal refused (403), video stopped 0.46 s later, go2rtc dropped the viewer within 4 s | `m15-live-lab-20261108T063719` | after funding |
| Through TURN within 5 s (median of 10) | Relayed path works in the lab: 0.5 to 1.5 s over 5 attempts (coturn, UDP). The 10-attempt run then failed because of the relay-port finding below; fixed, re-run pending | lab check, Nov 8; `m15-live-lab-turn-20261108T064311` (before the fix) | remote test with Vivo's server |
| From outside the site within 3 s direct; packet captures show the path | Needs a public server and a phone on cellular | `tests/m15_capture.sh` ready | remote test |
| Limits answer 429 | Pass (unit tests: per-site remote limit and per-camera viewer limit) | `tests/unit/test_live.py` | in `m15_load` |
| 10 cameras plus 10 viewers: one camera connection per stream, no gap, CPU peak under 85% | Script ready (the lab has 4 cameras) | `tests/m15_load.mjs` | after funding |
| Live view comes back on its own within 2 minutes after a restart | Covered by the 72-hour test | `tests/soak72.py` | after funding |

## M18: Second-adapter mock validation

| Done when | Lab result (Nov 8, 2026) | Evidence |
|---|---|---|
| The same contract test suite passes against Frigate and against a mock recorder, switched by one setting | Pass, 8 of 8 (4 against Frigate 0.18, 4 against the mock); the mock half also passes without Frigate | `tests/contract/test_contract.py` |

## 72-hour test

The script, its pass/fail rules (unit tested) and the run instructions are ready
(`tests/soak72.py`). It runs on the gateway after all three Block C milestones are in place.

## Findings

See [findings.md](findings.md): go2rtc's alias streams cannot separate viewers (F1), the cut by
dropping packets (F2), go2rtc masking its secrets in its API answers (F3), and TURN relay ports being
reused (F4, fixed).

## Open points

- Vivo: the public server for TURN and the test tunnel; Jorge: UDP 8555 forwarded to the gateway.
- The TURN re-run after the F4 fix, the remote test with packet captures, the load test and the
  72-hour test, after funding.
