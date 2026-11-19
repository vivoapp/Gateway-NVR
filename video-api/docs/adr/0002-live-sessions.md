# ADR 0002: Live sessions and how the gateway cuts a viewer off

Status: **accepted** (Izzatullokh, 2026-11-08). Evidence from a test on go2rtc 1.9.14, the pinned
version, below.

## Context

The contract (section 6) and Simon's Block C terms require: grants of about 1 minute, renewed while
watching, and "when a renewal is refused, the gateway closes that viewer's session". Live video is
direct WebRTC from go2rtc, so the video never passes through the API. The API needs a way to stop
the video to **one** viewer without touching the camera, the recording or other viewers.

go2rtc 1.9.14 has no API to close one consumer (checked in its source: `internal/streams/api.go`
has no consumer route; closing the signaling WebSocket does not close the peer connection).

## What was tested (2026-11-08, go2rtc 1.9.14 in Docker, a headless Chrome viewer)

1. **A per-session alias stream** (`PATCH /api/streams?name=vs_1&src=rtsp://127.0.0.1:8554/cam01_sub`).
   go2rtc turns an RTSP source whose path names an existing stream into an alias of the **same**
   stream object (same producer, same consumers). Deleting or changing the alias does not affect
   the viewer. **Rejected.**
2. **Dropping that viewer's packets.** go2rtc lists each WebRTC consumer with `remote_addr` (the
   viewer's selected ICE address, for example `192.168.65.1:36284 prflx`) and `user_agent` (from
   the HTTP request that carried the offer). After an `iptables DROP` for that address and port,
   in both directions, on the go2rtc side:
   - the viewer's frame counter stopped at once (96 frames, then no more);
   - go2rtc removed the consumer by itself **7 s** later (its ICE connection went to
     disconnected and go2rtc closes the peer then);
   - the camera stream and the recording were not affected.
   **Accepted.**

## Decision

- The API sends each offer to go2rtc with `User-Agent: vivo-video-api/<session_id>`, so the
  session's consumer can be found exactly in `/api/streams?src=<camera>_<stream>`. It matches on the
  random session id only: go2rtc masks any text equal to one of its secrets in its API output (found
  in the lab, where the go2rtc user is called "vivo").
- To cut a session (renewal refused, session expired, or closed by the client while its peer is
  still open), the API reads the consumer's `remote_addr` and asks the host helper
  `vivo-live-gate` to drop that address and port. As soon as go2rtc has removed that viewer
  (about 5 to 10 s later), the API asks the helper to lift the drop again; the helper lifts it after
  60 s in any case. Found in the lab: a TURN server reuses its relay ports, so a drop that outlived
  the viewer blocked the next viewer who was given the same relay port.
- `vivo-live-gate` is a small root service on the host (`gateway/live_gate.py`). The API, which
  runs as a non-root user with no privileges, only writes a request file into
  `/run/vivo-live-gate/requests/`. The helper checks each request strictly (an IP address, a port
  from 1 to 65535, at most 10 minutes, at most 64 viewers at once) and adds rules to `DOCKER-USER`, tagged
  `vivo-live-gate`, that match **only go2rtc's WebRTC port 8555**:
  - `-p udp|tcp -s <ip> --sport <port> --dport 8555 -j DROP` (viewer to go2rtc)
  - `-p udp|tcp -d <ip> --dport <port> --sport 8555 -j DROP` (go2rtc to viewer)
  It removes them when the API releases them or they expire, and removes any left over when it starts.
- A viewer through TURN has the relay's address and allocation port as `remote_addr`, which is
  unique to that viewer, so the same cut works for relayed sessions.
- Limits (per site, per camera) count the API's own sessions, not go2rtc consumers.

## Why

- It stops the video within about a second and frees go2rtc's resources within seconds, with no
  change to go2rtc, no extra video process per viewer, and no effect on other viewers.
- The API keeps no privileges; the only privileged part is a 150-line helper that can drop
  traffic on one port of one container, for a limited time.

## Trade-offs

- It depends on two go2rtc consumer fields (`remote_addr`, `user_agent`). A go2rtc upgrade must
  re-run the M15 revocation test (it is part of `tests/m15_live.mjs`).
- A viewer that changes its network address during a session (for example from Wi-Fi to cellular)
  gets a new ICE address; the cut reads the address at the moment of cutting, so it still works,
  but a later ICE restart would need a new offer, which the API refuses (one offer per session).
- The MSE fallback does not need this: its video passes through the API's WebSocket, which the API
  closes.
