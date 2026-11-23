# M15: Live-view delivery

## What it does

- **WebRTC from go2rtc**, the agreed design: the API only handles the setup and the rules; the video
  goes straight between the gateway and the viewer, or through a TURN relay when the networks do
  not allow a direct path (for example a phone behind a carrier's NAT). go2rtc stays the only
  process that opens a camera, so viewers add no camera connections.
- **Sessions** (`vivo_api/live/sessions.py`): `POST /v1/cameras/{id}/live` checks the grant and the
  limits, and returns the ICE servers with TURN credentials valid for that session only
  (`live/turn.py`, coturn's shared-secret scheme). One offer per session; the offer goes to go2rtc
  and its complete answer comes back.
- **Renewal and revocation**: clients renew every 30 s with a fresh grant. A session that is not
  renewed in time, or whose renewal is refused, is closed and its video is cut: the API asks the
  host helper `vivo-live-gate` to drop that viewer's packets on go2rtc's WebRTC port, go2rtc then
  closes the viewer itself, and the drop is lifted as soon as it has
  ([ADR 0002](adr/0002-live-sessions.md)).
- **Limits**: remote live streams per site (`LIVE_SITE_REMOTE_MAX`, default 6) and viewers per camera
  (`LIVE_CAMERA_MAX`, default 5); over either, `429 limit_reached` names the limit. Phones ask for
  the substream and switch to the main stream only in full screen.
- **MSE fallback** on the building network (`GET /v1/live/{id}/mse`, a WebSocket relayed through the
  API), for browsers where WebRTC is blocked; it checks the page's origin and ends with the session.
- **Recorder changes**: go2rtc also advertises its public address (`stun:8555`); `WEBRTC_PUBLIC=1` in
  `site.env` opens UDP 8555 to everyone, rate limited per source, for the direct remote path.

## Test

- `tests/unit/test_live.py`, `test_live_gate.py`: limits, one offer, renewal, expiry, the cut and its
  release, TURN credentials, the helper's request checks and rules, the MSE origin check.
- `tests/m15_live.mjs`: the test player in headless Chrome: first frame over N attempts (median and
  the ICE path of each), then a refused renewal and the time until the video stops. `--relay` forces
  TURN.
- `tests/m15_load.mjs`: 10 cameras recording plus 10 viewers (5 on one camera): every viewer plays,
  one camera connection per stream, no recording gap, CPU peak, and the 6th viewer refused.
- `tests/m15_capture.sh` with `tests/pcap_summary.py`: packet headers on the gateway during the remote
  test, and which path the video took (direct or through TURN).

## Remote test (with Vivo's server)

1. Vivo provides a small Linux server with a public IP; coturn and the test tunnel are set up on it
   following [turn-runbook.md](turn-runbook.md).
2. Jorge forwards UDP 8555 to the gateway; `WEBRTC_PUBLIC=1` (announced, a firewall change).
3. A phone on cellular opens the test player through the tunnel: 10 attempts direct, 10 forced through
   TURN, each with a packet capture; then the revocation test through TURN.

## Not included

- The production TURN server and its cost (Vivo), Haris's M21 connection (a test tunnel stands in),
  the Vivo Watch player (the test player shows the flow).
- Converting the H.265 main stream for browsers without H.265 support (heavy for the N150): phones use
  the substream.
