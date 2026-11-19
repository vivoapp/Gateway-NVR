# M13: Vivo Video API contract

## What it is

The agreement between the gateway and its clients (Rails and Vivo Watch): every route, request,
response, error, the grant format, the limits and the audit log. It is written so a client can be
built from it alone, and so the recorder behind it can change without the clients noticing.

- The contract: [api-contract.md](api-contract.md) (version 1.0).
- The machine-readable half: [openapi.json](openapi.json), generated from the code by
  `tools/export_openapi.py`. A unit test fails if the file and the code differ, so they cannot drift.
- How live sessions are cut off on revocation: [adr/0002-live-sessions.md](adr/0002-live-sessions.md).

It starts from Marlon's draft (PR #1) and keeps its conventions. Every question that draft left open
is decided in section 12 of the contract.

## Main decisions

| Topic | Decision |
|---|---|
| Grants | ES256 JWT signed by Rails, at most 120 s, one gateway (`aud`), cameras (`cams`) and permissions (`perms`); the gateway holds public keys only, several during a key change |
| Errors | one shape, `{"error": {"code", "message"}}`, stable codes (contract section 9) |
| Live view | WebRTC from go2rtc, TURN as fallback, sessions renewed with fresh grants; a refused renewal cuts the video |
| Playback | HLS through the API; the playback id is the credential and expires with the grant |
| Clips | queued (2 at a time), at most 30 minutes, deleted 1 hour after they are ready |
| Recorder | one adapter interface (contract section 10); Frigate is one adapter, a mock the second |

## Test

- `tools/export_openapi.py --check` (also run by the unit tests): the contract file matches the code.
- The M14 to M18 checks test the routes the contract describes.

## Not included

- Rails issuing grants and publishing its keys, the Vivo Watch player, and Haris's M21 connection.
  The contract says what each must do (section 14); the gateway side has a stand-in for each.
