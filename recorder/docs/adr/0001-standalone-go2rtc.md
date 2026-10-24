# ADR 0001: run go2rtc as its own container, not inside Frigate

Status: proposed (Oct 2026). Decided by: Izzatullokh. To confirm with Vivo at kickoff.

## Context

Frigate bundles go2rtc. Vivo also needs each camera's streams for live view (M5), the
substream archive (M7, read by the cloud uploader M19 and M20) and the Video API (M13 to M18).
Each camera must be pulled only once (M5 acceptance), because consumer cameras allow only
2 to 4 RTSP sessions.

## Decision

go2rtc 1.9.14 runs as a separate container (same version Frigate 0.18.0 bundles). It is the
only thing that connects to the cameras. Frigate records from the go2rtc restream, like any
other client.

## Why

- A Frigate restart, crash or upgrade does not drop live view or the substream archive.
  The M2 and M10 tests kill Frigate on purpose; with the bundled go2rtc every stream would
  drop each time.
- One place owns camera connections and credentials. Frigate never sees camera passwords.
- Other developers (uploader, Video API) read streams from go2rtc without depending on Frigate.
- go2rtc can be upgraded separately from Frigate.

## Trade-offs

- Frigate's own UI live view falls back to its built-in player instead of go2rtc's
  WebRTC/MSE player. Vivo's live view goes through go2rtc directly (M5), so this only
  affects the local admin UI.
- One more container to monitor (covered by M22).
