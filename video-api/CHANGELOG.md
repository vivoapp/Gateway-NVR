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
- Live gate fix (Nov 24, 2026, findings F11): the request folder belongs to root with the API's
  group allowed to write, so `vivo-live-gate`, which runs with network rights only, can read it; it
  now says once when it cannot. Found by the first M15 run on the gateway (the cut did not happen).
- Gateway test fixes (Nov 22, 2026, found on the reference gateway with real recordings):
  - Playback over more than about 3 hours failed with `upstream_unavailable`: Frigate's video module
    refuses one playlist of more than about 1,100 pieces. The Frigate adapter now asks for one part
    per clock hour (UTC), 6 at a time, and joins them into one playlist (a discontinuity between
    parts, piece names prefixed `c<k>-`). Finished hours are kept for an hour. On the gateway, the
    first picture of a 24-hour playback now takes 0.94 s (median of 5).
  - Playlists over 1 KB go out gzipped when the player accepts it (a 24-hour playlist is 8,640
    pieces).
  - The mock recorder's clips kept the stored pieces' absolute timestamps, so a 5-minute clip read
    as about 30 minutes long. Clips now start at 0, without the per-piece `styp` and `sidx` boxes.
  - Test tools: `tests/m16_probe.py` measures first picture and seeks on the gateway itself;
    `tests/m17_clips.py --check-on HOST` checks the clip on the gateway, so footage stays there;
    `tests/m14_check.py --audit-fetch` reads the audit log over SSH.
