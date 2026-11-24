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
303.9 s (303.6 s on the gateway, Nov 22). The asked range is always covered; a clip can be a few
seconds longer at each end.

## F8: Frigate refuses one playlist of more than about 1,100 pieces (gateway, Nov 22, 2026)

Frigate builds a playback playlist with its nginx video module, which refuses a list of more than
about 1,100 clips ("invalid number of elements in the durations array"). With 10 s segments that is
about 3 hours, so on the gateway a 4-hour and a 24-hour playback failed with `502
upstream_unavailable`. The lab could not show it: it had 14 minutes of footage.

Fixed in the Frigate adapter: a range over one hour is asked for one part per clock hour (UTC), 6 at
a time, and the parts are joined into one playlist, with a discontinuity marker before each part
after the first and the part number in each piece name. Frigate builds each new part in about 90 ms
and one after another, so a cold 24-hour playlist takes about 2 s; parts of finished hours are kept
for an hour, after which a 24-hour playback starts with the first picture in 0.94 s (median, on the
gateway). `tests/unit/test_frigate_vod.py` uses a fake Frigate with the same limit.

## F9: the mock recorder's clips kept their stored times (workstation, Nov 22, 2026)

The mock joined its stored pieces as they were, and each piece carries its time from the start of
the recording, so a 5-minute clip read as 1,790 s long. The contract suite missed it because its clip
started 10 s into the footage, where the error is only 10 s. Fixed: the mock moves every piece's time so
the clip starts at 0 and leaves out the per-piece `styp` and `sidx` boxes (`vivo_api/mp4.py`), and
the contract suite now takes its clip 7 minutes in. The Frigate clip was never affected: Frigate
makes the MP4 itself.

## F10: remote playback time is round trips (gateway over the VPN, Nov 22, 2026)

The same 24-hour playback that starts in 0.94 s on the gateway took a median of 3.07 s in a browser
on the workstation in Korea, over the VPN with about 340 ms per round trip, and seeks took a median
of 1.61 s (slowest 2.62 s). A start is several requests one after another (the playback, the
playlist, the header, the first pieces), so the distance to the site adds up. On the building
network, or near the site, the figures are close to the gateway's. Playlists are gzipped (a 24-hour
playlist lists 8,640 pieces), which keeps the playlist step short.

## F11: the live gate could not read its request folder (gateway, Nov 24, 2026)

The first M15 run on the gateway (over the VPN, cam01's new H.264 substream) played 10 of 10, but
the revocation check failed: the renewal was refused (403) and the video kept playing. The Video API
had written every cut request, and `vivo-live-gate` never read one. The helper runs as root with only
`CAP_NET_ADMIN` and `CAP_NET_RAW`, and the request folder belonged to the API's user with mode 0700.
Root without `CAP_DAC_OVERRIDE` cannot open another user's private folder, and `Path.glob` returns
nothing on "Permission denied", so the helper stayed silent. The lab did not show it because there
the helper runs inside go2rtc's container as full root (`--lab`).

Fixed: `requests/` now belongs to root with group 10001 (the API) allowed to write (mode 0770,
`gateway/files/vivo-live-gate.tmpfiles`), so the helper reads it as its owner and keeps its narrow
rights. The helper lists the folder with `iterdir` and says once when it cannot read it. Two unit
tests cover both. Applied on the gateway at 12:38 UTC (tmpfiles, helper restarted; recording and the
API untouched); the re-run passed: renewal refused, video stopped 363 ms later, the drop rule removed
6 s after the cut (reports `m15-live-gateway-vpn-20261124T123425.md` before, `...T123921.md` after).
