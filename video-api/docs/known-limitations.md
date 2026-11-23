# Known limitations: Video API (Block C)

| # | Limitation | Effect | Where |
|---|---|---|---|
| 1 | The main stream is H.265 | Over WebRTC and HLS it plays only where the browser or phone has H.265 support (Chrome 136 and later, Safari 18 and later, with hardware decoding); others get `codec_unsupported` and use the substream | M15, M16 |
| 2 | The substream on the gateway is still H.265 until the camera switch | Live view on phones needs the H.264 substream; the switch is measured on one camera first and agreed with Simon and Haris (its upload cost) | M15 |
| 3 | Sessions are kept in memory | An API restart ends open live views and playbacks; clients start new ones (the player retries) | M15, M16 |
| 4 | The cut uses two fields of go2rtc's viewer list (`remote_addr`, `user_agent`) | A go2rtc upgrade must re-run the revocation test (`tests/m15_live.mjs`) | M15, ADR 0002 |
| 5 | The cut needs Linux iptables | It runs on the gateway only; on a workstation it is unit tested | M15 |
| 6 | A self-signed certificate on port 8443 | Clients must trust it by its fingerprint until Haris's M23 certificates | M14 |
| 7 | Remote access waits on Haris's M21 connection | Until then remote tests use a test tunnel to Vivo's server | M15 |
| 8 | The TURN server is Vivo's to host | Without it, viewers on networks that block direct paths cannot watch | M15 |
| 9 | The MSE fallback is for the building network only | Remote viewers use WebRTC | M15 |
| 10 | Clips cover whole recording segments | A clip can be a few seconds longer than asked at each end (findings F7) | M17 |
| 11 | Each remote viewer is a separate upload from the building | `LIVE_SITE_REMOTE_MAX` should follow the site's upload speed (about 0.5 Mbit/s per substream viewer, up to 6 Mbit/s per main-stream viewer) | M15 |
| 12 | The audit log stays on the gateway for 30 days | Sending it to Vivo's backend belongs to M21/M24 | M14 |
| 13 | Playback far from the site starts slower | Each start and seek is a few requests in a row, so a viewer with a long round trip waits longer: over a 340 ms link, about 3 s to the first picture and up to 2.6 s per seek (findings F10) | M16 |
| 14 | A playback over more than an hour has a break marker at each clock hour | The player must support HLS discontinuities; hls.js does (tested), as do the native players on Apple devices | M16 |
| 15 | The first 24-hour playback after an hour changes builds its new parts | About 2 s more for the first viewer; later ones use the kept parts (findings F8) | M16 |

## What was not tested on the gateway yet

M15 live view (the building-network test, the 10-camera load test and the remote tests), the H.264
substream switch, and the 72-hour test; see block-c-3-results.md. M14, M16, M17 and M18 passed on the
reference N150 on Nov 22 and 23.
