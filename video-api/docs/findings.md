# Findings: Video API (Block C)

What testing showed that the documentation did not, and what was done about it.

## F1: go2rtc aliases share one stream (go2rtc 1.9.14, Nov 8, 2026)

A stream added at run time whose source is go2rtc's own RTSP path of another stream
(`PATCH /api/streams?name=vs_1&src=rtsp://127.0.0.1:8554/cam01_sub`) becomes an alias of the same
stream object: same producer, same viewers. Deleting it does not stop anyone's video, so it cannot be
used to cut one viewer off. ADR 0002 uses F2 instead.

## F2: go2rtc closes a viewer by itself once its packets stop (go2rtc 1.9.14, Nov 8, 2026)

When one viewer's packets are dropped on go2rtc's WebRTC port, its video stops at once and go2rtc
removes that viewer after about 5 to 7 s (its ICE connection goes to disconnected). The camera, the
recording and other viewers are not affected. This is how a refused renewal cuts the video
(ADR 0002): measured 0.46 s from the refusal to the last frame.

## F3: go2rtc masks its secrets in its API answers (lab, Nov 8, 2026)

go2rtc replaces any text equal to one of its secrets with `***` in `/api/streams`. In the lab the go2rtc
API user is called "vivo", so the API's tag `vivo-video-api/<session>` came back as
`***-video-api/<session>` and the first cut found no viewer. The API now matches viewers on the random
session id only.

## F4: TURN relay ports are reused (lab, Nov 8, 2026)

A viewer through TURN reaches go2rtc from the TURN server's address and one of its relay ports, and
the TURN server hands the same ports out again. A drop that lasted 2 minutes after a session ended
blocked the next viewer given the same relay port (in the lab, with a 41-port range, after a few
sessions no relayed viewer got video). Fixed: the API lifts the drop as soon as go2rtc has removed the
viewer (5 to 10 s), and the helper lifts it after 60 s in any case. The production TURN server uses
coturn's full port range (turn-runbook.md).

## F5: TURN over TCP through Docker Desktop is too slow to measure (lab only, Nov 8, 2026)

On a Mac, a TURN server in Docker reached through Docker Desktop's port forwarding took 5 to 18 s to
the first frame over TCP, against 0.5 to 1.5 s over UDP. This is the workstation's port forwarder,
not TURN; the lab uses UDP, and the gateway test uses a real server.

## F6: the first playback in a fresh browser starts slower (lab, Nov 8, 2026)

The first HLS start in a new browser took 2.3 s, later starts 0.2 to 0.5 s: the browser starts its
H.265 decoder once. The M16 measure is the median of 5 starts.

## F7: a clip covers whole recording segments (lab, Nov 8, 2026)

Frigate's `clip.mp4` starts and ends on whole segments (about 10 s each), so a 5-minute request gave
303.9 s. The asked range is always covered; a clip can be a few seconds longer at each end.
