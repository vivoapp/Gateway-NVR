# Block C, milestone "M13, M14": results

Status: **built and tested on the workstation lab; the gateway run follows when the milestone is
funded.** The lab is the recorder's own lab (`recorder/tests/lab/`): Frigate 0.18.0 and go2rtc
1.9.14, the same pinned versions as the gateway, with four fake cameras sending what Vivo's test
cameras send. The rows marked "gateway" are run on the reference N150 after funding, with the same
scripts, and this page is updated with their reports.

## M13: Video API contract

| Done when (contract section 13) | Result | Evidence |
|---|---|---|
| The contract and `openapi.json` agree | Done: `openapi.json` is generated from the code and a unit test fails if they differ | `docs/api-contract.md`, `docs/openapi.json`, `tests/unit/test_cameras.py` |
| Simon approves the contract | Sent for review | |

## M14: Video API service and Frigate adapter

| Done when (contract section 13) | Lab result (Nov 8, 2026) | Evidence | Gateway |
|---|---|---|---|
| Every route in section 5 works against Frigate | Pass: health, camera list (only granted cameras), camera, status (both streams receiving, newest segment 12 s old), timeline of the last hour, over 24 h refused | `m14-check-lab-20261108-061624` | after funding |
| The security set refuses every wrong camera, expired grant, wrong audience, wrong algorithm, bad signature, unknown key and changed link | Pass, 12 of 12: also a grant over 120 s, a missing permission, a changed playback link and another user's clip | `m14-check-lab-20261108-061624`, 30 grant unit tests | after funding |
| An audit line is written for each | Pass: every refusal is in the audit log, and the log never holds a grant | `m14-check-lab-20261108-061624` | after funding |
| The adapter uses only Frigate's authenticated port as a viewer | Pass: the viewer account logs in on 8971, reads config and recordings, and the user list answers 403 | lab check, Nov 8 | after funding |
| Installs on a gateway with one script | Script written; a dry run of every step works | `gateway/setup.sh --dry-run` | after funding |

Overall in the lab: **21 of 21 checks pass** (`m14-check-lab`), **86 unit tests pass**, and the
contract test suite passes against Frigate and against the mock recorder (8 of 8).

## Changes and findings during this milestone

- go2rtc masks any text equal to one of its secrets in its API answers; the API therefore finds a
  live viewer by the random session id, not by a fixed name (found in the lab, ADR 0002).
- Two independent reviews (code and security) found no critical problems. Fixed: two clip requests
  at the same moment could both take the last place in the queue; a boolean `iat` claim was
  accepted; a too-long playlist kept its connection open; the MSE fallback now checks the page's
  origin; the test grant issuer refuses to run on a gateway; the Frigate certificate check is skipped
  only for a loopback address, never for a name.

## Open points

- Simon's approval of the contract.
- The gateway run of `tests/m14_check.py` and `gateway/setup.sh`, after funding.
- Rails' public keys (`rails.jwks.json`); until then the gateway runs with a test key.
