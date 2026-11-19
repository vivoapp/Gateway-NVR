# M14: Video API service and Frigate adapter

## What it does

- Runs the Video API on the gateway (port 8443, HTTPS) as its own Compose project, installed by
  `gateway/setup.sh`.
- Checks the grant on every request (`vivo_api/grants.py`, `vivo_api/context.py`): signature
  (ES256 only), issuer, gateway, expiry, lifetime, cameras and permissions. A camera outside the
  grant is refused whether or not it exists, so a grant cannot be used to discover cameras.
- Serves the routes of contract section 5: health, cameras, camera status and the timeline.
- Talks to Frigate only through `vivo_api/recorders/frigate.py`, as the viewer account
  `video_api` on Frigate's authenticated port. The viewer role cannot change configuration, users
  or recordings; admin calls such as the user list answer 403 to it.
- Reads stream status from go2rtc and recording status from the recorder's health service (M22),
  so "recording is healthy" means the same everywhere.
- Writes an audit line for every refused grant and every session or clip (contract 11.2); the
  grant itself is never stored.
- The recorder's health service now raises `video_api_down` when the API stops answering.

## Test

- `tests/unit/`: the grant checks (30 cases, including the "none" and HS256 tricks, a changed grant,
  an unknown key, claims of the wrong type), errors, the timeline, status, and that `openapi.json`
  matches the code.
- `tests/m14_check.py` against a running API: every route with the real recorder, the security set
  (no grant, expired, another gateway, algorithm none, HS256 with the public key, a changed grant,
  an unknown key, a grant over 120 s, a camera outside the grant, a missing permission, a changed
  playback link, another user's clip) and the audit log. Writes `reports/m14-check-<label>-<UTC>.md`.

## Not included

- TLS certificates from Haris's M23 (a self-signed certificate is used until then).
- Issuing grants (Rails); `tools/grant_issuer.py` stands in for tests.
