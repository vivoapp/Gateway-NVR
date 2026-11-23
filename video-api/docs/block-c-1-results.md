# Block C, milestone "M13, M14": results

Status: **M14 passes on the Vivo reference gateway (vivo-n150) against the real recordings**
(Nov 22, 2026). M13, the contract, is written and waits for Simon's approval.

Where it was tested:
- **Lab** (Nov 8): the recorder's own lab on the workstation (`recorder/tests/lab/`), Frigate 0.18.0
  and go2rtc 1.9.14, the gateway's pinned versions, with four fake cameras.
- **Gateway** (Nov 22 and 23): the reference N150 with the ten Vivo test cameras, recording as normal
  throughout. Since Nov 23 the gateway runs commit 289d162 (checked by comparing the installed files
  with the repository).

The reports are in [test-reports/block-c](test-reports/block-c/README.md).

## M13: Video API contract

| Done when (contract section 13) | Result | Evidence |
|---|---|---|
| The contract and `openapi.json` agree | Done: `openapi.json` is generated from the code and a unit test fails if they differ | `docs/api-contract.md`, `docs/openapi.json`, `tests/unit/test_cameras.py` |
| Simon approves the contract | Sent for review | |

## M14: Video API service and Frigate adapter

| Done when (contract section 13) | Lab (Nov 8) | Gateway (Nov 22) | Evidence |
|---|---|---|---|
| Every route in section 5 works against Frigate | Pass | **Pass**: health; camera list (only the granted camera); camera; status (both streams receiving, newest segment 15 s old); timeline of the last hour (one recorded period of 3,570 s, no gap); a timeline over 24 h refused (413) | `m14-check-gateway-20261122-150122` |
| The security set refuses every wrong camera, expired grant, wrong audience, wrong algorithm, bad signature, unknown key and changed link | Pass, 12 of 12 | **Pass, 12 of 12**: no grant, expired, another gateway, algorithm `none`, HS256 signed with the public key, changed grant, unknown key, grant over 120 s, camera not in the grant, missing permission, changed playback link, another user's clip | `m14-check-gateway-20261122-150122`, 30 grant unit tests |
| An audit line is written for each | Pass | **Pass**: every refusal is in the audit log, and the log never holds a grant | `m14-check-gateway-20261122-150122` |
| The adapter uses only Frigate's authenticated port as a viewer | Pass: the viewer account reads config and recordings, and Frigate's user list answers 403 | **Pass**: setup made the `video_api` viewer account; every gateway run (M14, M16, M17, M18) read Frigate through it on port 8971 | `m18-contract-20261123-123627` |
| Installs on a gateway with one script | Dry run of every step | **Pass**: installed on Nov 22 from 15:00 UTC with `gateway/setup.sh` (settings, folders, certificate, Frigate viewer account, firewall rule for 8443 from the LAN and the VPN only, units, stack). The recorder kept running (its containers stayed up) and the health endpoint showed no alerts. The fixes found that afternoon went in with `setup.sh --only stack` | group notice of Nov 22, checks after each step |

Overall on the gateway: **21 of 21 checks pass**. On the workstation, 110 unit and contract tests
pass; the 4 skipped there are the Frigate half of the contract suite, which runs on the gateway
(`m18-contract-20261123-123627`, 4 of 4).

## Changes and findings during this milestone

- go2rtc masks any text equal to one of its secrets in its API answers; the API therefore finds a
  live viewer by the random session id, not by a fixed name (found in the lab, findings F3).
- Two independent reviews (code and security) found no critical problems. Fixed: two clip requests
  at the same moment could both take the last place in the queue; a boolean `iat` claim was
  accepted; a too-long playlist kept its connection open; the MSE fallback now checks the page's
  origin; the test grant issuer refuses to run on a gateway; the Frigate certificate check is skipped
  only for a loopback address, never for a name.
- On the gateway, `CORS_ORIGINS` stays empty, so no web page from another site can call the API,
  until Vivo names the sites of any web client. The browser test on the workstation turns the
  browser's own check off in its throwaway test browser instead (`VIVO_TEST_NO_CORS=1` in
  `tests/lib/player.mjs`).
- The M14 check can read the audit log over SSH (`--audit-fetch`), so it runs from the workstation.

## Open points

- Simon's approval of the contract.
- Rails' public keys (`rails.jwks.json`); until then the gateway runs with a test key whose private
  half stays on the workstation.
- Haris's M23 certificates replace the self-signed one on port 8443.
