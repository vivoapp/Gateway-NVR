# Changelog: Vivo Video API

## Unreleased

- **M13 contract**: `docs/api-contract.md` version 1.0 (from Marlon's draft, every open item
  decided), `docs/openapi.json` generated from the code, ADR 0002 on cutting live viewers off.
- **M14 service and Frigate adapter**: FastAPI service on port 8443; ES256 grants with key rotation;
  camera list, status and timeline; Frigate adapter on the authenticated port as a viewer; audit
  log; install script, units and Compose project; the recorder's health service watches the API.
- **M15 live view**: sessions with limits (per site for remote viewers, per camera), one offer each,
  renewal with fresh grants, TURN credentials per session, the `vivo-live-gate` helper that cuts a
  viewer off, an MSE fallback on the building network; recorder: go2rtc also advertises its public
  address (`stun:8555`) and `WEBRTC_PUBLIC=1` opens UDP 8555 with a per-source rate limit.
- **M16 playback**: HLS playback through the API with a capability id that expires with its grant;
  playlists rewritten so only plain piece names reach the player.
- **M17 clips**: queued clip jobs (2 at a time, 6 waiting), progress, MP4 check, cleanup after 1 hour
  and by size, refusal when the disk is at its limit.
- **M18 second recorder**: the mock recorder and one contract test suite for every adapter.
- Review fixes (code and security reviews, Nov 8, 2026): clip queue limit under concurrent
  requests, boolean time claims refused, playlist streams closed on error, MSE origin check, test
  issuer refuses to run on a gateway, loopback-only TLS exception, relay drops lifted as soon as
  go2rtc drops the viewer.
- Install fix (Nov 16, 2026): when `/etc/vivo/video-api.env` already exists, `setup.sh` keeps its
  settings and adds the ones it lacks from `video-api.env.example`. Before, a file left with only a
  comment (the reference gateway's, emptied on Oct 27) kept the API on its built-in defaults,
  including the gateway id `vivo-gw` instead of the host name.
