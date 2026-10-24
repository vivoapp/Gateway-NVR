# M5: protocol and codec limits for live view (go2rtc 1.9.14)

Status: checked on the reference gateway with Vivo's ten cameras on Oct 22, 2026 where marked
**verified**; the rest is from the go2rtc and browser documentation.

Vivo's test cameras (white-label N501) send **H.265** on both streams, although their ONVIF settings
say H.264 (findings F5), with G.711 A-law (PCMA) audio and a two-way audio back channel (PCMU) that
the gateway does not use.

## How a viewer gets a stream

go2rtc pulls each camera stream once and serves it to any number of clients on the LAN:

| Method | Where | Latency | Notes |
|---|---|---|---|
| WebRTC | browser, port 8555 + 1984 for signaling | lowest (under 1 s) | needs the gateway's LAN address as candidate (set from GATEWAY_LAN_IP) |
| MSE | browser, port 1984 (WebSocket) | about 1 to 2 s | most compatible in desktop browsers |
| HLS | browser, port 1984 | several seconds | fallback, mainly for iOS |
| RTSP | VLC, VMS, Frigate, uploader, port 8554 | low | password protected |

Test page on the LAN: `http://<gateway>:1984/stream.html?src=cam01_main` (go2rtc login).

## Video codecs

| Codec | WebRTC | MSE | HLS | Notes |
|---|---|---|---|---|
| H.264 | all browsers | all browsers | all browsers | the scope default; plays everywhere |
| H.265 | **verified** in Chrome on macOS (2880x1620, 25 fps, no dropped frames); Safari | **verified** in Chrome on macOS; Safari | Safari | Chrome and Edge decode H.265 only where the computer has a hardware H.265 decoder (most Macs and Windows PCs with a recent GPU, usually not Linux). Firefox has no H.265. For every browser, switch the cameras to H.264 (decision pending with Vivo) |

go2rtc does not transcode video in this setup, so the browser must support the camera's codec.

## Audio

| Camera audio | WebRTC | MSE / HLS | What to do |
|---|---|---|---|
| G.711 (PCMA/PCMU), common on IP cameras | plays as is | not supported (verified in the lab: go2rtc's fMP4 stream for a G.711 camera carries video only) | for audio in MSE, go2rtc can transcode to AAC or Opus per stream (`#audio=aac`), at a CPU cost per viewed stream |
| AAC | not supported (browsers' WebRTC audio is Opus or G.711) | plays as is | for WebRTC audio, transcode to Opus |
| none | video only | video only | |

Recording is separate: Frigate stores audio as AAC (cameras.yaml `audio: aac`, **verified** on the
reference gateway), because MP4 segments cannot hold G.711. The substream segments for the cloud
uploader (docs/SEGMENT_FORMAT.md) are video only unless Vivo asks for audio.

## Limits of this module (not included, per scope)

- No live view from outside the LAN: no NAT traversal, no TURN server, no viewer accounts (M15).
- No transcoding of video. One-off audio transcoding is possible but off by default.

## Live view checked in a browser (reference gateway, Oct 22, 2026)

`tests/m5_browser.mjs` opens go2rtc's player page in headless Chrome from an operator workstation
(over the Vivo VPN), lets each stream play, and passes only if the video has a size, playback time
moves forward and new frames are decoded. It answers go2rtc's login through the DevTools protocol
from the `GO2RTC_PASSWORD` environment variable, so the password is never typed or printed.

```bash
GO2RTC_PASSWORD="$(ssh <gateway> 'sudo sed -n "s/^GO2RTC_API_PASSWORD=//p" /etc/vivo/secrets.env')" \
  node tests/m5_browser.mjs --streams cam01_main,cam01_sub --modes webrtc,mse
```

Result on the ten Vivo cameras (H.265, 2880x1620): all 20 main-stream runs passed in Chrome on
macOS, WebRTC at about 25 fps with no dropped frames, MSE with about 6 to 7 s of an 8 s window
(start-up buffering). Chrome and Safari play H.265 through the platform decoder; Firefox does not
(see findings F5). Then `tests/m5_check.py --live-ok cam01,...,cam10` records the confirmation.
