# Sharing the gateway with other services

The recorder (this folder) runs go2rtc and Frigate in the system Docker. Other parts of Vivo Gateway
NVR run as separate Compose projects in their owners' rootless Docker, set up by Vivo (Jorge). They
reach the recorder over the gateway's LAN address, because separate Docker daemons share no network.

| Service | Owner | Folder | Secrets file (root, readable only by the owner) | Reads from the recorder |
|---|---|---|---|---|
| Cloud uploader and agent (M19 to M21) | Haris | `/opt/vivo-cloud`, data in `/var/lib/vivo-cloud` | `/etc/vivo/cloud.env` | substream segments, read-only (docs/SEGMENT_FORMAT.md) |
| Video API (M13 to M18) | open (Marlon until Oct 27, 2026) | `/opt/vivo-video-api` | `/etc/vivo/video-api.env` | camera list, go2rtc, Frigate API |

## What the recorder offers (reference gateway, 192.168.30.200)

| Interface | Address | Login |
|---|---|---|
| Camera list | `/opt/vivo-gateway/cameras.yaml` (mount the folder read-only) | none, no secrets in it |
| go2rtc API, MSE and WebRTC live view | `http://192.168.30.200:1984` | go2rtc login |
| go2rtc RTSP restream | `rtsp://192.168.30.200:8554/<camera>_main` or `_sub` | restream login |
| Frigate API | `https://192.168.30.200:8971` (Frigate's self-signed certificate until M23) | per-service Frigate user |
| Health (M22), for the Edge Agent | `http://192.168.30.200:9101/health` (JSON) and `/metrics` (Prometheus), docs/m22-health.md | none, read only |
| Substream segments | `/srv/vivo/recordings/substream`, written by the `substream` service, kept 3 days (docs/m7-recording.md, m9-retention.md); early deletions listed in `early-deletions.jsonl` there | read-only mount |

`127.0.0.1:5000` (Frigate without login) stays local to the host on purpose.

## Accounts made for other services

- Frigate user `video_api`, role **viewer**, created Oct 26, 2026 for the Video API. Checked: it can log in on
  port 8971 and read recordings, stats, HLS playback (`/vod/`) and `clip.mp4`; it can also start export jobs
  (`POST /api/export/...`) but cannot delete them (`POST /api/exports/delete` needs the admin role); admin-only
  calls such as the user list are refused (403). Deleted Oct 27, 2026 when Marlon left the project; at the same
  time the go2rtc API and restream passwords were changed and `/etc/vivo/video-api.env` was emptied. A new
  account with the same role is made for the next Video API owner.
- go2rtc has one API login and one restream login, shared with services that need them. Changing them means
  updating `/etc/vivo/secrets.env` and every service file that copies them.

## Rules for every service

- Never connect to the cameras directly (RTSP or ONVIF). Read video through go2rtc, so each camera keeps one
  connection per stream.
- Use the recorder's APIs read only. Do not change go2rtc or Frigate settings through their APIs or the Frigate
  UI; the repository config is the only source of truth. Ask the recorder owner for changes.
- The one allowed exception: the Video API may start Frigate export jobs. Prefer `clip.mp4` for downloads (built
  on request, nothing kept on disk). Name kept exports with a `videoapi-` prefix. Exports use the recording disk
  and a viewer cannot delete them, so the recorder deletes exports older than 7 days (`export_days` in
  `cameras.yaml`, the `storage` service, docs/m8-storage.md).
- Exports and clips cost CPU and disk: queue them instead of running many at once.
- Keep ports 1984, 8554, 8971 and 5000 on the LAN. Anything offered outside goes through the service's own
  authenticated interface.
- Frigate is pinned (versions.env) and its API can change between versions: keep Frigate calls in one adapter.
- Set CPU and memory limits in your Compose file. With ten cameras the recorder uses about 1.7 of the 4 cores
  (docs/findings.md F10).
- Tell the group before a reboot, a firewall change or an internet cut.
