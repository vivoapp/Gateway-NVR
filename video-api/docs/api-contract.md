# Vivo Video API contract, version 1 (M13)

Status: **version 1, for Simon's review**. Written 2026-11-14 (UTC) by Izzatullokh, owner of the
Video API and the recorder. It starts from Marlon's draft (PR #1, `feature/video-api-m13`) and keeps
its conventions; every question that draft left open is decided in [section 12](#12-decisions).

The Video API is the only way Vivo Watch reaches cameras and recordings on a gateway. Rails decides
who may see what and says so in a short-lived signed grant; the gateway checks the grant on every
request and never makes that business decision itself. The API hides the recorder: Rails and Vivo
Watch never talk to Frigate or go2rtc, so the recorder can be replaced by writing one new adapter
([section 10](#10-recorder-adapter)).

Contents:
1. [Where it runs](#1-where-it-runs)
2. [Conventions](#2-conventions)
3. [Grants](#3-grants)
4. [Routes at a glance](#4-routes-at-a-glance)
5. [Cameras, status and timeline](#5-cameras-status-and-timeline)
6. [Live view](#6-live-view)
7. [Playback and seeking](#7-playback-and-seeking)
8. [Clip export](#8-clip-export)
9. [Errors](#9-errors)
10. [Recorder adapter](#10-recorder-adapter)
11. [Limits, audit and operations](#11-limits-audit-and-operations)
12. [Decisions](#12-decisions)
13. [Acceptance targets](#13-acceptance-targets)
14. [What others provide](#14-what-others-provide)
15. [Change log](#15-change-log)

---

## 1. Where it runs

| Item | Value |
|---|---|
| Source folder | `video-api/` in vivoapp/Gateway-NVR |
| Installed at | `/opt/vivo-video-api`, its own Docker Compose project in system Docker |
| Listens on | port **8443**, HTTPS, on the gateway's LAN address and on 127.0.0.1 |
| Reached by | clients on the building network directly; clients outside it through the gateway's one outbound connection (Haris's M21; a test tunnel stands in until then) |
| Secrets | `/etc/vivo/video-api.env` (root, mode 600); Rails public keys in `/etc/vivo/video-api-keys/` |
| Data | `/srv/vivo/video-api/` (clips, audit log) on the recording disk |
| TLS | a self-signed certificate made by the setup script, replaced by Haris's M23 certificates |

The API is the only service that clients reach. It never exposes go2rtc (port 1984), Frigate
(8971, 5000) or the RTSP restream (8554). Inside the gateway it uses:

- **Frigate** at `https://127.0.0.1:8971` as the viewer user `video_api`. Never port 5000, which has
  no login. The viewer role cannot change configuration, users or recordings.
- **go2rtc** at `http://127.0.0.1:1984` for stream status and WebRTC signaling. go2rtc stays the only
  process that opens a camera, so live viewers add no camera connections.
- **The recorder's health service** (M22) at `http://127.0.0.1:9101/health`, so "recording is
  healthy" has one definition on the gateway.
- **The camera list** from the recorder's `/opt/vivo-gateway/cameras.yaml` (ids and names, no secrets),
  read again whenever it changes.

Video is never re-encoded by the API: live view, playback and clips copy the recorded or streamed
video.

## 2. Conventions

- **Version.** All routes start with `/v1`. A breaking change gets `/v2`. A new field that old
  clients can ignore stays on `/v1`.
- **Format.** JSON, `application/json; charset=utf-8`, field names in `snake_case`, except media
  routes (HLS playlists, video segments, MP4 downloads).
- **Camera ids.** The inventory id, matching `^[a-z][a-z0-9_]{0,31}$` (reference site: `cam01` to
  `cam10`). An id that does not match is `400 invalid_argument`.
- **Stream roles.** `main` (full resolution, recorded) and `sub` (low resolution, for phones and
  the cloud uploader).
- **Times.** RFC 3339 in UTC with a `Z`: `2026-11-08T10:15:00Z` or `2026-11-08T10:15:00.250Z`.
  Responses use up to 3 decimals; requests may send up to 6 (`+00:00` is also accepted). Times
  without a zone are `400 invalid_argument`.
- **Ids made by the API.** Random, 128 bits, with a prefix: `ls_` live session, `pb_` playback,
  `cl_` clip. They are unguessable and work as capabilities where noted below.
- **Unknown query parameters or body fields** are `400 invalid_argument`.
- **No secrets** in any response: no camera address, password, RTSP URL, recorder port or path.

## 3. Grants

Every route except `GET /v1/health` needs a grant: a JSON Web Token signed by Rails, sent as

```
Authorization: Bearer <grant>
```

### 3.1 Format

| Part | Value |
|---|---|
| Algorithm | **ES256** (ECDSA P-256 with SHA-256). Every other algorithm, including `none` and `HS256`, is refused. |
| Header | `alg: ES256`, `kid`: the id of the Rails signing key, `typ: JWT` |
| `iss` | `vivo-rails` |
| `aud` | `vivo-gw:<gateway_id>`, for example `vivo-gw:vivo-n150`. A grant works on one gateway only. |
| `sub` | the Vivo user id (a string) |
| `site_id` | the site id (a string), recorded in the audit log |
| `cams` | the camera ids this grant covers, for example `["cam01","cam03"]` |
| `perms` | any of `live`, `playback`, `export` |
| `iat`, `exp` | issued at and expiry, Unix seconds. `exp - iat` is at most **120 s**; Rails should issue **60 s**. |
| `jti` | a unique id for this grant, recorded in the audit log |

Example payload:

```json
{
  "iss": "vivo-rails", "aud": "vivo-gw:vivo-n150",
  "sub": "user-4821", "site_id": "site-17",
  "cams": ["cam01", "cam03"], "perms": ["live", "playback"],
  "iat": 1794120000, "exp": 1794120060, "jti": "7c1f0d4e-2b9a-4d0f-9a41-0f3c5e2d8b11"
}
```

### 3.2 Checks on every request

1. The token is well formed, `alg` is ES256, and `kid` names a key in the gateway's key set.
2. The signature is valid for that key.
3. `iss` is `vivo-rails` and `aud` is this gateway's audience.
4. `exp` is in the future and `iat` is not in the future (5 s of leeway for clock differences),
   and `exp - iat` is at most 120 s.
5. `cams`, `perms`, `sub` and `jti` are present and have the right types.
6. The camera in the request is in `cams`, and the route's permission is in `perms`.

A failure in 1 to 5 is `401 unauthenticated`. A failure in 6 is `403 forbidden`. A camera that is
not in the grant is `403` whether or not it exists, so a grant cannot be used to discover other
cameras. Every refusal is written to the audit log with its reason; the token itself never is.

### 3.3 Keys and rotation

The gateway keeps only Rails **public** keys, as a JSON Web Key Set in
`/etc/vivo/video-api-keys/rails.jwks.json`. Several keys may be valid at once: Rails adds the new
key to the set, starts signing with it, and removes the old one after the longest grant lifetime
has passed. The API reads the file again when it changes, without a restart.

### 3.4 Renewal and revocation

Grants are short on purpose. A live view or playback that lasts longer than one grant is kept open
by **renewing** it with a fresh grant (sections 6 and 7). If Rails stops issuing grants to a user
(access revoked), the next renewal fails or does not happen, and the gateway closes that user's
session. A revoked user loses access within one grant lifetime, at most about 1 minute with 60 s
grants.

## 4. Routes at a glance

| Route | Permission | Module | Purpose |
|---|---|---|---|
| `GET /v1/health` | none | M14 | The API process is running |
| `GET /v1/cameras` | any | M14 | Cameras the grant covers |
| `GET /v1/cameras/{camera_id}` | any | M14 | One camera |
| `GET /v1/cameras/{camera_id}/status` | `live` or `playback` | M14 | Is the camera streaming and recording |
| `GET /v1/cameras/{camera_id}/timeline` | `playback` | M14 | Recorded periods and gaps in a time window |
| `POST /v1/cameras/{camera_id}/live` | `live` | M15 | Start a live session |
| `POST /v1/live/{session_id}/offer` | `live` | M15 | WebRTC offer in, answer out |
| `POST /v1/live/{session_id}/renew` | `live` | M15 | Keep a live session open |
| `DELETE /v1/live/{session_id}` | `live` | M15 | Close a live session |
| `GET /v1/live/{session_id}/mse` (WebSocket) | session id | M15 | Fallback player on the building network |
| `POST /v1/cameras/{camera_id}/playback` | `playback` | M16 | Start playback of a time range |
| `GET /v1/playback/{playback_id}/...` | playback id | M16 | HLS playlist and video pieces |
| `POST /v1/playback/{playback_id}/renew` | `playback` | M16 | Keep a playback open |
| `DELETE /v1/playback/{playback_id}` | `playback` | M16 | Close a playback |
| `POST /v1/cameras/{camera_id}/clips` | `export` | M17 | Ask for a clip |
| `GET /v1/clips/{clip_id}` | `export` | M17 | Clip status and progress |
| `GET /v1/clips/{clip_id}/download` | `export` | M17 | Download the finished clip |
| `DELETE /v1/clips/{clip_id}` | `export` | M17 | Cancel or delete a clip |

"Any" means at least one of `live`, `playback`, `export`. For routes on a session, playback or clip,
the grant must also cover that object's camera and have the same `sub` as the grant that created it.

The full machine-readable description is [`openapi.json`](openapi.json), generated from the code; a
test fails if the two differ.

## 5. Cameras, status and timeline

### `GET /v1/health`

No grant. Answers `200` while the API process runs.

```json
{"status": "ok", "api_version": "1.0"}
```

### `GET /v1/cameras`

Returns the cameras that are both in the inventory and in the grant's `cams`, in inventory order.
A grant whose cameras are all unknown gets an empty list.

```json
{
  "cameras": [
    {"camera_id": "cam01", "name": "VIVO-CAM-01", "streams": [{"role": "main"}, {"role": "sub"}]},
    {"camera_id": "cam03", "name": "VIVO-CAM-03", "streams": [{"role": "main"}, {"role": "sub"}]}
  ]
}
```

### `GET /v1/cameras/{camera_id}`

One camera object, as in the list. `404 camera_not_found` when the grant covers the id but the
gateway has no such camera.

### `GET /v1/cameras/{camera_id}/status`

```json
{
  "camera_id": "cam01",
  "checked_at": "2026-11-08T10:15:03Z",
  "streams": [
    {"role": "main", "receiving": true},
    {"role": "sub", "receiving": true}
  ],
  "recording": {"ok": true, "newest_segment_end": "2026-11-08T10:14:55Z", "age_seconds": 8}
}
```

- `receiving`: the stream is arriving from the camera now.
- `recording.ok`: the recorder's health service has no recording alert for this camera. A newest
  segment 1 to 15 s old is normal, because segments are about 10 s long and are moved into place
  once finished.

### `GET /v1/cameras/{camera_id}/timeline?start=...&end=...`

Recorded periods and gaps of the main stream between `start` and `end`. `end` must be after
`start`, and the window is at most **24 hours** (`413 range_too_long` otherwise). Neighbouring
recording pieces closer than **3 s** count as one period; a hole of 3 s or more is a gap.

```json
{
  "camera_id": "cam01",
  "start": "2026-11-08T09:00:00Z",
  "end": "2026-11-08T10:00:00Z",
  "recorded": [
    {"start": "2026-11-08T09:00:00Z", "end": "2026-11-08T09:41:12Z"},
    {"start": "2026-11-08T09:42:05Z", "end": "2026-11-08T10:00:00Z"}
  ],
  "gaps": [
    {"start": "2026-11-08T09:41:12Z", "end": "2026-11-08T09:42:05Z"}
  ]
}
```

A window with no recording returns empty `recorded` and one gap covering the whole window; it is
not an error. Footage older than the retention period is simply not in `recorded`.

## 6. Live view

Live view uses **WebRTC** from go2rtc on the gateway. The video goes straight between the gateway
and the viewer when the networks allow it, or through a **TURN** relay when they do not (for
example a phone behind a carrier's NAT). The API only handles the setup messages and the rules;
the video itself never passes through the API.

### 6.1 Flow

1. `POST /v1/cameras/{camera_id}/live` with `{"stream": "sub"}`. Phones should ask for `sub` and
   switch to `main` only in full screen. The answer gives a session id and the ICE servers (STUN
   and TURN, with TURN credentials valid for this session only).
2. The client creates its WebRTC peer connection with those ICE servers, makes an offer and sends
   it to `POST /v1/live/{session_id}/offer`. The answer comes back complete (all gateway
   candidates included), so no further candidate messages are needed.
3. While watching, the client calls `POST /v1/live/{session_id}/renew` with a **fresh grant**
   every `renew_after_seconds` (30 s).
4. When done, `DELETE /v1/live/{session_id}`.

If a session is not renewed before `expires_at`, or a renewal is refused, the gateway **cuts the
video to that viewer** and closes the session. How the cut is done on go2rtc is recorded in
[ADR 0002](adr/0002-live-sessions.md).

### 6.2 `POST /v1/cameras/{camera_id}/live`

Request: `{"stream": "sub"}` (`sub` or `main`, default `sub`).

Response `201`:

```json
{
  "session_id": "ls_Jx3kQ9b2Vd7Lr0pW1sNcYa",
  "camera_id": "cam01",
  "stream": "sub",
  "remote": true,
  "expires_at": "2026-11-08T10:16:00Z",
  "renew_after_seconds": 30,
  "ice_servers": [
    {"urls": ["stun:turn.vivo.example:3478"]},
    {"urls": ["turn:turn.vivo.example:3478?transport=udp", "turn:turn.vivo.example:3478?transport=tcp"],
     "username": "1794120900:ls_Jx3kQ9b2Vd7Lr0pW1sNcYa", "credential": "q8m4…"}
  ]
}
```

- `remote`: true unless the request came from the building network. Remote sessions count toward
  the site limit.
- `expires_at`: the grant's expiry. The session cannot outlive the grant that opened or last
  renewed it.
- Limits: `429 limit_reached` with `"limit": "site_remote"` when the site already has its maximum
  of remote live streams, or `"limit": "camera_viewers"` when the camera already has its maximum
  of viewers (section 11).

### 6.3 `POST /v1/live/{session_id}/offer`

Request: `{"sdp": "<the client's SDP offer>"}`. Response `200`: `{"sdp": "<the gateway's answer>"}`.

- `422 codec_unsupported` when the client cannot play the stream's codec. The main stream is H.265;
  WebRTC plays it only on browsers and phones with H.265 support (Chrome 136 and later, Safari 18
  and later, with hardware decoding). The client then asks for `sub`, which is H.264.
- One offer per session. A second offer is `409 conflict`; start a new session instead.

### 6.4 `POST /v1/live/{session_id}/renew`

Send a fresh grant in the `Authorization` header and no body. Response `200`:
`{"expires_at": "2026-11-08T10:16:30Z"}`. A grant that fails the checks, no longer covers the
camera, no longer has `live` or has a different `sub` gets `401` or `403` **and the session is
closed at once**.

### 6.5 `DELETE /v1/live/{session_id}`

Closes the session. `204`. Closing an unknown or already closed session is also `204`.

### 6.6 `GET /v1/live/{session_id}/mse` (WebSocket)

A fallback for browsers on the building network where WebRTC is blocked: the API relays go2rtc's
MSE stream (fragmented MP4 over a WebSocket). The session id is the credential, because browsers
cannot send headers on a WebSocket. Only for sessions with `remote: false`; the same renewal rules
apply, and the socket closes when the session ends.

## 7. Playback and seeking

Playback uses **HLS**: the player downloads a playlist and then small video pieces (about 10 s
each) only as it needs them, so nobody downloads a large file to watch, and seeking jumps straight
to the piece for that moment. Pieces are served from the recordings without re-encoding.

### 7.1 `POST /v1/cameras/{camera_id}/playback`

Request: `{"start": "2026-11-08T09:00:00Z", "end": "2026-11-08T10:00:00Z"}`. At most **24 hours**
(`413 range_too_long`).

Response `201`:

```json
{
  "playback_id": "pb_4fK2nT8sVq1Lm7xR0cZpWe",
  "camera_id": "cam01",
  "start": "2026-11-08T09:00:00Z",
  "end": "2026-11-08T10:00:00Z",
  "url": "/v1/playback/pb_4fK2nT8sVq1Lm7xR0cZpWe/index.m3u8",
  "expires_at": "2026-11-08T10:16:00Z",
  "renew_after_seconds": 30,
  "recorded": [{"start": "2026-11-08T09:00:00Z", "end": "2026-11-08T10:00:00Z"}]
}
```

- `404 no_recording` when there is no recording at all in the range.
- `recorded` is the same list as the timeline, so the app can mark gaps on its scrub bar. Gaps
  are skipped in the playlist: the player plays what exists.

### 7.2 `GET /v1/playback/{playback_id}/...`

The playlist (`index.m3u8`) and the pieces it lists. The playback id in the path is the credential
(HLS players cannot add headers), so these requests need no grant. They work until `expires_at`;
after that, `404 not_found`.

### 7.3 `POST /v1/playback/{playback_id}/renew` and `DELETE /v1/playback/{playback_id}`

Same rules as live renewal and closing (sections 6.4 and 6.5).

## 8. Clip export

A clip is a bounded piece of recording, prepared on the gateway as one MP4 file and downloaded once
ready.

### 8.1 `POST /v1/cameras/{camera_id}/clips`

Request: `{"start": "2026-11-08T09:40:00Z", "end": "2026-11-08T09:45:00Z"}`. At most **30
minutes** (`413 range_too_long`).

Response `202`:

```json
{"clip_id": "cl_8sJd2Lq0Xn5Vw3Rb7KtYfA", "camera_id": "cam01", "status": "queued", "queue_position": 1}
```

- At most **2** clips are prepared at a time on a gateway; up to **6** more wait in the queue.
  Beyond that, `429 limit_reached` with `"limit": "clip_queue"`.
- `503 disk_near_limit` when the recording disk is at its safe limit.
- `404 no_recording` when there is no recording at all in the range.

### 8.2 `GET /v1/clips/{clip_id}`

```json
{
  "clip_id": "cl_8sJd2Lq0Xn5Vw3Rb7KtYfA",
  "camera_id": "cam01",
  "start": "2026-11-08T09:40:00Z",
  "end": "2026-11-08T09:45:00Z",
  "status": "ready",
  "progress": 100,
  "size_bytes": 81264512,
  "expires_at": "2026-11-08T10:46:10Z",
  "error": null
}
```

`status` is `queued`, `running`, `ready` or `failed`. `progress` goes from 0 to 100. A failed clip
has `error` with a `code` and `message` from section 9 (for example `upstream_unavailable`). A clip
that covers a gap contains only the recorded parts, in order.

### 8.3 `GET /v1/clips/{clip_id}/download`

`200`, `Content-Type: video/mp4`, `Content-Disposition: attachment; filename="cam01_20261108T094000Z_20261108T094500Z.mp4"`.
`409 conflict` if the clip is not ready yet.

### 8.4 `DELETE /v1/clips/{clip_id}`

Cancels a queued or running clip, or deletes a finished one. `204`.

Finished clips are deleted automatically **1 hour** after they are ready, and the oldest are
deleted first if the clip folder passes its size cap. Only the user (`sub`) who asked for a clip can
see or download it.

## 9. Errors

Every error has the same shape:

```json
{"error": {"code": "forbidden", "message": "The grant does not cover this camera."}}
```

`code` is stable and safe to program against; `message` is one short sentence for people. No error
contains a secret, a stack trace or the grant.

| HTTP | `code` | When |
|---|---|---|
| 400 | `invalid_argument` | malformed id, time, body or query; `end` not after `start` |
| 401 | `unauthenticated` | no grant, or the grant fails a check in section 3.2 (1 to 5) |
| 403 | `forbidden` | the grant does not cover the camera, lacks the permission, or has another `sub` |
| 404 | `camera_not_found` | the grant covers the id but the gateway has no such camera |
| 404 | `not_found` | unknown or expired session, playback or clip |
| 404 | `no_recording` | nothing recorded in the requested range |
| 409 | `conflict` | a second offer on one session; downloading a clip that is not ready |
| 413 | `range_too_long` | timeline or playback over 24 h, clip over 30 min |
| 422 | `codec_unsupported` | the client cannot play this stream's codec |
| 429 | `limit_reached` | a limit in section 11 is reached; the body adds `"limit"` |
| 502 | `upstream_unavailable` | the recorder or go2rtc did not answer |
| 503 | `disk_near_limit` | a clip was refused because the recording disk is full |

## 10. Recorder adapter

All recorder calls go through one adapter with this interface (`vivo_api/recorders/base.py`). Frigate
is the first adapter (M14); a mock recorder is the second (M18). Replacing Frigate means writing a
new adapter that passes the same contract tests; the routes, Rails and Vivo Watch do not change.

| Method | Returns |
|---|---|
| `cameras()` | the camera ids the recorder knows |
| `segments(camera, start, end)` | recorded pieces `(start, end)` overlapping the window, sorted |
| `playlist(camera, start, end)` | an HLS playlist for the range, with piece names the adapter can fetch |
| `fetch(camera, piece)` | the bytes of one playlist piece, streamed |
| `clip(camera, start, end)` | one MP4 of the range, streamed, plus its expected size when known |
| `health()` | whether the recorder answers |

Live view does not use the adapter: it goes through go2rtc, which works the same whatever records.

## 11. Limits, audit and operations

### 11.1 Limits

All are settings in `/etc/vivo/video-api.env`, so dealers can tune them per site.

| Setting | Default | Meaning |
|---|---|---|
| `LIVE_SITE_REMOTE_MAX` | 6 | live streams to viewers outside the building at the same time |
| `LIVE_CAMERA_MAX` | 5 | live viewers of one camera at the same time, inside and outside |
| `LIVE_RENEW_AFTER` | 30 s | how often clients renew live and playback sessions |
| `GRANT_MAX_LIFETIME` | 120 s | longest grant accepted |
| `TIMELINE_MAX_WINDOW` | 24 h | longest timeline or playback range |
| `CLIP_MAX_SECONDS` | 30 min | longest clip |
| `CLIP_WORKERS` / `CLIP_QUEUE_MAX` | 2 / 6 | clips prepared at once / waiting |
| `CLIP_KEEP` / `CLIP_STORE_MAX_GB` | 1 h / 5 GB | how long finished clips stay / clip folder cap |
| `LOCAL_CIDRS` | the site's LAN | addresses counted as the building network |

Each remote viewer of the main stream can use up to about 6 Mbit/s of the building's upload, and of
the substream up to 0.5 Mbit/s, so `LIVE_SITE_REMOTE_MAX` should follow the site's upload speed.

### 11.2 Audit log

One JSON line per event in `/srv/vivo/video-api/audit/YYYY-MM-DD.jsonl`, kept 30 days.

| Field | Example |
|---|---|
| `time` | `2026-11-08T10:15:00.120Z` |
| `event` | `grant_refused`, `live_start`, `live_end`, `playback_start`, `playback_end`, `clip_request`, `clip_download` |
| `result` / `reason` | `allowed` or `denied`; for example `expired`, `camera_not_in_grant`, `renewal_refused`, `closed_by_client` |
| `sub`, `site_id`, `jti` | from the grant (`jti` identifies the grant; the grant itself is never stored) |
| `camera_id`, `object_id` | the camera and the session, playback or clip id |
| `client_ip`, `remote` | where the request came from |
| `start`, `end` | for playback and clips |

### 11.3 Operations

- The service restarts by itself (`restart: unless-stopped`) and starts at boot after the recorder.
  A restart of the API does not touch go2rtc, Frigate or the cameras; open sessions end and clients
  start new ones.
- It runs as a non-root user in a read-only container with no extra privileges, limited to 1 CPU
  and 512 MB, with its image pinned by version and digest.
- The recorder's health service reports `video_api_down` when `GET /v1/health` stops answering.

## 12. Decisions

Each open item from Marlon's draft, and how this version settles it. Proposed by Izzatullokh on
2026-11-14 for Simon's approval.

| Item | Decision |
|---|---|
| D1 Routes | The routes in section 4. |
| D2 Grant format | ES256 JWT with the claims and checks in section 3; at most 120 s; `aud` = `vivo-gw:<gateway_id>`; keys by `kid` from a key set file. |
| D3 Live protocols and URLs | WebRTC for everyone, with TURN as fallback; MSE only as a building-network fallback. Responses carry no stream URLs or ports; the SDP answer has the gateway's ICE candidates. |
| D4 Model and firmware fields | Not in version 1. |
| D7 Who changes the contract | The owner (Izzatullokh) with Simon's written approval; changes go in section 15. |
| F1 Playback, seeking, export | Defined in sections 7 and 8. |
| F2 Codec | Main stream stays H.265 (recording); substream H.264 640x360, 15 fps, keyframe every second, for live view on phones. |
| F3 Audio | Version 1 promises video. Audio recorded with the main stream plays in playback and clips where the player supports it; live view may carry it but does not promise it. |
| F4 What M14 to M18 contain | Section 4, column "Module", and section 13. |
| F5 Port and limits | Port 8443; 1 CPU and 512 MB. |
| F6 Substream folder as a source | No. The API reads recordings only through the recorder adapter; the substream folder belongs to the cloud uploader. |
| F7 Gap threshold | 3 s, as in the recorder's own checks. |
| F8 Timeline paging | None: periods are merged and the window is at most 24 h, so a response stays small. |
| F9 OTA packaging | The Compose project and its install steps are ready for Mohamed's Mender packaging (M27). |
| F10 TLS and identity | Self-signed TLS on 8443 until Haris's M23; remote access goes through Haris's M21 connection, with a test tunnel standing in until then. |
| F11 Audit scope | Section 11.2: every refused grant, every session start and end, every clip. |
| F12 Frigate exports | Not used. Clips use Frigate's `clip.mp4`, so no extra copy is kept in Frigate. |
| F13, F16 Access to Frigate and go2rtc | The setup script creates the `video_api` viewer user and writes `/etc/vivo/video-api.env`; checked by the M14 test. |
| F14 Frigate's certificate | The API reaches Frigate only on 127.0.0.1, so it does not check that certificate; nothing else on the network can answer on that address. |
| F15 Ports and firewall | System Docker with host networking; one ufw rule allows 8443 from the site LAN and admin networks. |

## 13. Acceptance targets

Measured on the reference gateway (Intel N150, 10 test cameras recording) and written up in the
results pages with the report behind each number.

| Module | Done when |
|---|---|
| M13 | This contract and `openapi.json` agree, and Simon approves the contract. |
| M14 | Every route in section 5 works against Frigate on the gateway. An automated security set refuses every wrong camera, expired grant, wrong audience, wrong algorithm, bad signature, unknown key and changed link. Audit lines are written for each. |
| M15 | First frame on the building network in 2 s or less, live delay under 1 s. From outside (a phone on cellular): 3 s or less direct, 5 s or less through TURN. All medians of 10. Packet captures show which path the video took. A refused renewal cuts the video within the grant lifetime. Limits answer `429`. 10 cameras recording plus 10 viewers (5 on one camera): still one camera connection per stream, no recording gap, CPU peak under 85%. Live view comes back on its own within 2 minutes after a camera or service restart. |
| M16 | First picture within 3 s; a seek anywhere in the last 24 hours plays within 2 s. Gaps are skipped; an empty range is `404 no_recording`. |
| M17 | A 5-minute clip is ready within 30 s. Clips over 30 minutes are refused; at most 2 are prepared at once and the rest queue. Finished clips are deleted after 1 hour. |
| M18 | The same contract test suite passes against Frigate and against a mock recorder, switched by one setting, with no change to routes or clients. |
| 72-hour test | 10 cameras recording while a script keeps opening live views, playbacks and clips and restarts services on a schedule (the API every 6 h, go2rtc and Frigate every 12 h, one reboot). Pass: no recording gap over 10 s outside planned restarts; recording back within 60 s and features within 2 minutes after each restart; at least 99% of scripted actions succeed; each service's memory in the last 12 h within 10% of hours 12 to 24; CPU under 85% (5-minute average). |

## 14. What others provide

The gateway side is finished and tested with stand-ins; these parts connect when ready.

| Who | What | Stand-in until then |
|---|---|---|
| Rails team | Sign grants as in section 3 and publish the public key set | `tools/grant_issuer.py` |
| Haris (M21, M23) | The outbound connection that carries remote requests to port 8443; certificates | a test SSH tunnel to Vivo's test server |
| Vivo | A STUN/TURN server for production | coturn on Vivo's test server, with a runbook |
| Mohamed (M27) | Mender packaging of the Compose project | the setup script |
| Vivo Watch team | The player: WebRTC with renewals; HLS for playback | the test player in `web/player.html` |

## 15. Change log

| Version | Date | Change | By |
|---|---|---|---|
| draft | 2026-10-26 | First draft, PR #1 | Marlon |
| 1.0 | 2026-11-14 | All open items decided; live view, playback and clips added | Izzatullokh |
