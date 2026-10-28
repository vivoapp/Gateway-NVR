# M13 Video API contract (draft)

Status: **draft for review**. Revised 2026-10-26 (UTC). This file is the M13 deliverable. It specifies no running service. It does not change `recorder/`.

Reviewers: Simon (scope and client behavior) and Izzatullokh (recorder owner; check the integration constraints and the appendix against the gateway).

Nothing in the Proposed or Open question rows is agreed until the named reviewers accept it in writing. Shared contracts in this repository change only by written agreement between the owners.

## How to read the labels

| Label | Meaning |
|---|---|
| **Confirmed** | Stated by the repository, or by a written decision recorded in section 12. Simon’s 2026-10-26 decisions are **Confirmed** as his decisions. They are not repository facts unless the repository also states them. |
| **Proposed** | Suggested for this contract. Not implemented. Not agreed. |
| **Open question** | Neither the repository nor a written decision in section 12 decides this. |

A **Proposed** Video API route is not a Frigate route, a go2rtc route, or a route on any running process. Existing upstream calls are named in [Appendix A](#appendix-a-repository-evidence) and are labeled **Existing interface**.

No password, token, or secret value appears in this draft. Credential names are the names in `recorder/secrets.env.example`.

Illustrative JSON uses the reference camera id `cam01`. Timestamps in examples are either the camera-table measurement time (2026-10-22) or rounded illustrations. They are not new observations taken on the revision date.

---

## Decision summary

### Needed before implementation

These block coding against this contract. An answer belongs in the decision log before a server is built.

| ID | Decision | Who |
|---|---|---|
| D1 | Accept, edit, or reject the proposed `/v1` routes in section 6: camera list, camera detail, live description, timeline, and health. | Simon |
| D2 | Access policy is **Confirmed** (Simon, 2026-10-26): Rails decides tenant, site, camera, and role; the gateway accepts only a short-lived Rails-issued live or playback grant and denies every other request. Section 8.2 requires checks of signature, `iss`, `aud`, `exp`, and an algorithm allowlist. Still open: the algorithm, claims, maximum lifetime, audience value, key rotation, and the rest of the grant format. | Simon confirmed the policy. Backend approval of those parameters is still required. |
| D3 | Whether a live response may contain URLs or ports, and which of WebRTC, MSE, HLS, and RTSP the contract promises. | Simon |
| D7 | Who signs a later change to this contract. The deployed service owner and path are confirmed below. | Simon |

### Resolved

These are no longer open. The decision log has the written source.

| ID | Decision | Who |
|---|---|---|
| F12 | Starting a Frigate export job is the permitted exception to the read-only recorder API rule. Configuration, user, camera, and other recording mutations stay prohibited. | Izzatullokh |
| F13 | The Video API’s rootless Docker and Docker Compose environment is ready. On the host, `/etc/vivo/video-api.env` is non-empty and readable by Marlon through a read-only ACL. That is not a verified login from the running process (F16). `/opt/vivo-gateway/cameras.yaml` is readable. The agreed deployment mounts all of `/opt/vivo-gateway` read-only. | Jorge, Izzatullokh |

### Can be deferred

These stay open without blocking a review of the proposed routes. They must not be filled in as if they were decided.

| ID | Question | Who |
|---|---|---|
| D4 | Whether camera detail later adds optional `model`, `firmware`, and measured media. This blocks only those fields. The camera object in section 6.1 does not include them. | Simon |
| F1 | Video API behavior for historical playback, seeking, and clip export. No routes are defined. Frigate download and persistent-export constraints below are confirmed and do not approve an endpoint. | Simon |
| F2 | The H.264/H.265 product decision remains open. This contract promises neither codec and does not promise universal browser compatibility. Observed codec measurements stay in Appendix A. | Vivo, via Simon |
| F3 | Whether clients require audio, and on which path. | Simon, Vivo |
| F4 | Definitions of M14, M16, M17, and M18. This draft does not assign features to them. | Simon |
| F5 | Listen port of the Video API, and the numeric CPU and memory limits. Limits are required. The numbers are not set. TLS for this API follows Haris’s M21/M23 interface (F10). Leaving that interface unspecified does not approve a cleartext deployment. | Simon, Izzatullokh, Haris |
| F6 | Use of `/srv/vivo/recordings/substream` as an API source. The writer is not in this repository. | Izzatullokh, Haris, Simon |
| F7 | Retention the API may assume, the timeline gap threshold, and timestamp tolerance. Gap calculation, and acceptance of that calculation, wait on an agreed threshold. | Simon, Izzatullokh, Haris |
| F8 | Timeline page size. The proposed `cursor` / `next_cursor` pairing cannot be finalized until this is decided. This draft sets no default, maximum, or `limit`. | Simon |
| F9 | Mender (M27) OTA packaging and rollback for the Video API. Open pending Mohamed’s guidance. Simon accepts the separate folder and Compose file only with that agreement. The rootless runtime being ready (F13) does not close this. | Mohamed |
| F10 | Haris’s gateway identity and TLS interface (M21/M23). Alignment is required. The interface is not in this repository. | Haris |
| F11 | Whether audit logging covers every request and every denied attempt, beyond the view, export, and grant events Simon required. The section 8.4 split into Rails issuance, gateway acceptance, and gateway rejection is proposed. It is not approval of that wider logging. | Simon |
| F14 | How the Video API trusts Frigate’s self-signed certificate on port 8971 until M23. Open pending coordination with Haris. No trust method is chosen. | Haris, Izzatullokh |
| F15 | Whether a port published by the Video API’s rootless Docker daemon is exposed, and how it is firewalled. The recorder’s system-Docker `DOCKER-USER` rules do not answer this. | Jorge |
| F16 | Whether the running rootless Video API can read `/etc/vivo/video-api.env` and authenticate to go2rtc and Frigate. Host-level readability is confirmed. This runtime check is not. | Jorge, Izzatullokh |

---

## 1. Purpose and scope

**Confirmed.** The gateway software is split into folders. Each folder is one developer’s component and its own Docker Compose project. The README install table lists `recorder/` only: modules M1–M12, M22, M25, and M26, owner Izzatullokh, path `/opt/vivo-gateway`.

**Confirmed.** `recorder/docs/adr/0001-standalone-go2rtc.md` uses the name “the Video API” for M13 to M18 and says that API should read streams from standalone go2rtc. The ADR status is **proposed** (Izzatullokh, still to confirm with Vivo). It is not a signed API contract.

**Confirmed.** The repository names two items inside that range and does not define the rest:

- M14, “the Video API adapter,” is outside the Frigate milestone (`recorder/docs/m2-frigate.md`). The adapter is not described.
- M15 is the label M5 uses for live view from outside the LAN, NAT traversal, a TURN server, and viewer accounts. M5 does not include them.

**Confirmed.** M16, M17, and M18 do not appear in the repository.

**Proposed.** The M13 deliverable is this contract: ownership, conventions, the reviewable routes in section 6, error shape, and the integration constraints an implementer must keep. It is not a server.

**Proposed.** Candidate functionality for later work in M14–M18 is listed in section 7. This draft does not assign any candidate to M14, M16, M17, or M18. The only milestone labels used there are the ones the repository already uses (M14, M15).

**Open question.** F4. What M14 and M16–M18 contain, beyond the two labels above.

---

## 2. Component ownership and architectural boundaries

**Confirmed.** Recorder owner: Izzatullokh. Substream segment contract (M7 to cloud uploader M19 and M20): Izzatullokh and Haris, with changes flagged to Simon.

**Confirmed** (`recorder/docs/shared-gateway.md`). The Video API service owner is Marlon. It is installed at `/opt/vivo-video-api`. The source folder in this repository remains `video-api/`. Who signs a later revision of this contract is still D7.

**Confirmed.** `gateway/setup.sh` rsyncs the `recorder/` tree to `/opt/vivo-gateway`. That install does not include `video-api/`.

**Confirmed.** go2rtc 1.9.14 is a separate container and the only process intended to open camera RTSP. Frigate 0.18.0 records from the go2rtc restream. The ADR gives the reason: these cameras allow 2 to 4 RTSP sessions, and a Frigate restart must leave live view up. ADR status remains proposed.

**Confirmed.** `cameras.yaml` is the inventory. `tools/gen_config.py` generates `go2rtc/go2rtc.yaml` and `frigate/config.yml`. Both generated files are mounted read-only. `--check` fails if they drift.

**Confirmed** (Jorge and Izzatullokh, 2026-10-26). `/opt/vivo-gateway/cameras.yaml` is readable. The agreed deployment mounts the entire `/opt/vivo-gateway` directory read-only. The file has no secrets. On the reference gateway the ids `cam01` through `cam10` are the same ids in go2rtc stream names, Frigate camera keys, and recording folders.

**Confirmed** (Jorge, 2026-10-26). The Video API’s rootless Docker and Docker Compose environment is ready. The recorder still runs in system Docker. Docker DNS names `go2rtc` and `frigate` exist only on the recorder’s Compose network. Separate daemons share no network, so the Video API uses the gateway LAN address and does not join that network. This draft does not add a Compose file. Mender packaging and rollback stay open (F9).

**Confirmed** (Simon, 2026-10-26). A separate `video-api/` folder and its own Compose file are acceptable only if Mohamed agrees how that artifact is updated and rolled back with Mender (M27, F9). This draft does not add the Compose file.

**Confirmed** (Simon, 2026-10-26, and the repository image rule). Every Video API image is pinned by version and digest. `latest` is not allowed.

**Confirmed** (Simon, 2026-10-26, and `shared-gateway.md`). Frigate’s admin interface is not exposed directly to the public. The Video API does not publish it. Ports 1984, 8554, and 8971 stay on the LAN. Port 5000 stays on the host. Anything offered beyond the gateway goes through the Video API’s own authenticated interface. The Video API does not call `127.0.0.1:5000`.

**Confirmed** (`shared-gateway.md`). The Video API does not open RTSP or ONVIF to a camera. It does not change go2rtc or Frigate settings through their APIs or the Frigate UI. Recorder config changes go to the recorder owner. It does not publish Frigate’s port 5000.

**Confirmed** reference addresses for this service, from `shared-gateway.md`. Logins are named only. Values stay in `/etc/vivo/video-api.env`.

| Interface | Address | Login |
|---|---|---|
| Camera list | `/opt/vivo-gateway/cameras.yaml` | none |
| go2rtc API, MSE, and WebRTC | `http://192.168.30.200:1984` | go2rtc login, authenticated |
| go2rtc RTSP restream | `rtsp://192.168.30.200:8554/<camera>_main` or `<camera>_sub` | restream login, authenticated |
| Frigate API | `https://192.168.30.200:8971` | Frigate user `video_api`, role viewer |

**Confirmed.** The Frigate certificate on 8971 is self-signed until M23. How the Video API trusts it stays open pending coordination with Haris (F14). No trust method is chosen.

**Confirmed.** Frigate calls from this service stay in one adapter, because the pinned Frigate API can change between versions. That rule does not assign the adapter to milestone M14. M14 is still undefined (F4).

**Confirmed** (Izzatullokh, 2026-10-26). Starting a Frigate export job is the explicit exception to the read-only rule. Configuration changes, user changes, camera changes, and other recording mutations stay prohibited. The viewer account can also read recordings, stats, HLS playback (`/vod/`), and `clip.mp4`. Admin calls such as the user list return 403.

**Confirmed** (Izzatullokh, 2026-10-26). Downloads prefer Frigate’s `clip.mp4`, because that path does not retain a separate export copy. If a persistent export is required, the Video API queues it, names it with a `videoapi-` prefix, and coordinates cleanup with Izzatullokh until M8 automatic cleanup is available. These rules do not approve a Video API export endpoint. That scope remains F1, for Simon.

**Confirmed.** Exports and clips are queued. Many are not run at once. The Compose project sets CPU and memory limits. With ten cameras the recorder uses about 1.7 of 4 cores (finding F10). The Video API’s own numbers are F5.

**Confirmed** (Jorge, 2026-10-26). On the host, `/etc/vivo/video-api.env` is non-empty and readable by Marlon through a read-only ACL. It holds the `video_api` password and the go2rtc API and restream logins. This contract does not copy the file or any secret value. Recorder secrets in `/etc/vivo/secrets.env` stay mode 600. Host readability is not evidence that the running rootless process can read the file, and it is not a successful authentication to go2rtc or Frigate.

**Open question.** F16. That runtime read and those logins are pending verification. No secret value is recorded while that check is open.

**Confirmed.** A reboot, a firewall change, or an internet cut on the shared gateway is announced to the group first.

---

## 3. Implementation stack

**Confirmed** (Simon, 2026-10-26). Python, FastAPI, and Docker Compose are approved for the Video API. The repository itself does not mention these tools. Recorder host tools use Python 3 and PyYAML. `versions.env` does not pin a Python version.

**Confirmed** (Simon, 2026-10-26, and the repository rule). The Compose image is pinned by version and digest. `latest` is not allowed. No Compose file is added in this draft. The rootless runtime is ready (F13). Mender OTA packaging and rollback still wait on Mohamed (F9).

| Piece | Label | Role |
|---|---|---|
| Python 3 | **Confirmed** (Simon) | Language. Minor version is the pinned image, not `latest`. |
| FastAPI | **Confirmed** (Simon) | HTTP service for the proposed `/v1` routes. |
| Docker Compose | **Confirmed** runtime (Jorge). Packaging still conditional on F9 | Separate project. Source folder `video-api/`. Installed at `/opt/vivo-video-api`. Rootless Docker and Compose are ready. This draft adds no Compose file. |
| Pydantic | **Proposed** | Request and response models. Simon did not approve this library. |
| HTTPX | **Proposed** | Client for **existing** go2rtc and Frigate HTTP interfaces (Appendix A). Those calls stay internal. |
| pytest | **Proposed** | Automated tests of the Video API. Recorder acceptance scripts stay as they are. |

**Proposed.** This draft does not add dependencies, a Dockerfile, or a Compose file.

---

## 4. Proposed API conventions

**Proposed** throughout this section. No Video API is running.

**Versioning.** Metadata routes use the prefix `/v1`. A breaking change of a JSON field or an error `code` uses a new prefix. An added field that an old client can ignore stays on `/v1`.

**Content type.** Section 6 responses and errors use `application/json; charset=utf-8`. This contract sets no media `Content-Type`. Playback and export are unresolved (section 7).

**Identifiers.** **Confirmed** inventory rule: a camera id matches `^[a-z][a-z0-9_]{0,31}$`. Reference ids are `cam01`–`cam10`. go2rtc keys are `<camera_id>_main` and `<camera_id>_sub`. Frigate’s camera key is the same id. **Proposed:** `{camera_id}` in Video API paths is that id.

**Timestamps.** **Confirmed:** the gateway clock is UTC via NTP. Frigate tools read `start_time` and `end_time` as Unix epoch seconds. On-screen camera time is AST4; recordings stay on the gateway clock. **Proposed:** Video API JSON uses RFC 3339 UTC with a `Z` suffix. The service converts Frigate epoch seconds at the boundary.

**Pagination.** **Proposed:** `GET /v1/cameras` returns the full list. The reference site has ten cameras. **Proposed, not final:** the timeline requires `start` and `end`. A following request repeats them and sets `cursor` to the previous response’s `next_cursor`. **Open question** F8: page size is undecided, so that pairing cannot be finalized. This draft sets no default, no maximum, and no `limit`. The example `next_cursor` of `null` shows the field only.

**Names and secrets.** **Proposed:** JSON names are `snake_case`. An unknown query parameter is `400` / `invalid_argument`. Responses omit camera passwords, RTSP userinfo, and secret values.

---

## 5. Error responses

**Proposed.** Applies to the routes in section 6 once they exist.

```json
{
  "error": {
    "code": "camera_not_found",
    "message": "No camera with this id is in the inventory."
  }
}
```

`code` is stable `snake_case`. `message` is one short sentence. The body contains no secrets and no stack trace.

| HTTP | `code` | When |
|---|---|---|
| 400 | `invalid_argument` | Malformed id (not the inventory pattern), bad timestamp, `end` not after `start`, unknown query parameter. |
| 401 | `unauthenticated` | **Proposed** HTTP mapping of the **Confirmed** deny-by-default rule: the request has no accepted Rails grant. A missing grant is never success. |
| 403 | `forbidden` | **Proposed** HTTP mapping: a grant was accepted and does not contain the permission this route requires. Which permission each route requires is the matrix in section 8.3. Some cells there are unresolved. |
| 404 | `camera_not_found` | `{camera_id}` matches the id pattern and is absent from the inventory. See section 6.1. |
| 502 | `upstream_unavailable` | go2rtc or Frigate did not answer a call the route needs. |

**Confirmed.** None of these codes are implemented. go2rtc and Frigate answer on their own ports with their own status codes. Appendix A lists those ports. This table does not describe them.

**Proposed.** There is no `501` route and no placeholder body for playback or export. Those behaviors are absent until F1 is agreed.

---

## 6. Proposed Video API

**Confirmed.** The repository has no `/v1` server.

Simon’s approval (D1, and D3 where noted) is required before any route below is treated as in scope. D4 blocks only the optional model, firmware, and measured-media fields. Each route’s purpose is the contract behavior. Upstream calls an implementation would make are existing interfaces, not these paths.

### 6.1 Camera discovery

| Proposed Video API route | Purpose |
|---|---|
| `GET /v1/cameras` | List inventory ids, display names, and the two stream roles. |
| `GET /v1/cameras/{camera_id}` | One camera object for that id. |

**Proposed response of `GET /v1/cameras`.**

```json
{
  "cameras": [
    {
      "camera_id": "cam01",
      "name": "VIVO-CAM-01",
      "streams": [
        {"name": "cam01_main", "role": "main"},
        {"name": "cam01_sub", "role": "sub"}
      ]
    }
  ]
}
```

| Field | Label | Meaning |
|---|---|---|
| `camera_id` | **Confirmed** source: inventory `id` | **Proposed** as this JSON field. |
| `name` | **Confirmed** source: inventory `name` | Reference value `VIVO-CAM-01`. |
| `streams[].name` | **Confirmed** source: go2rtc stream key | `<id>_main` or `<id>_sub`. |
| `streams[].role` | **Proposed** | `main` or `sub`, from that key. |

**Proposed response of `GET /v1/cameras/{camera_id}`.** The body is one camera object, not the list wrapper.

```json
{
  "camera_id": "cam01",
  "name": "VIVO-CAM-01",
  "streams": [
    {"name": "cam01_main", "role": "main"},
    {"name": "cam01_sub", "role": "sub"}
  ]
}
```

**Proposed.** `404` / `camera_not_found` when `{camera_id}` matches `^[a-z][a-z0-9_]{0,31}$` and is not in the inventory. The body is the section 5 error object with that `code` and the message `No camera with this id is in the inventory.` A path segment that does not match the pattern is `400` / `invalid_argument`, not `404`. The same `404` applies to the other section 6 routes that include `{camera_id}`.

**Proposed.** Both success responses omit `host`, `port`, paths, and the `credentials` key.

**Open question.** D4, deferred. It blocks only optional `model`, `firmware`, and measured media. Those fields are absent from the object above. The 2026-10-22 measurements stay in Appendix A.

**Confirmed.** The inventory the API serves is `/opt/vivo-gateway/cameras.yaml`. The file is readable. The agreed deployment mounts the entire `/opt/vivo-gateway` directory read-only. Ids `cam01` through `cam10` match go2rtc, Frigate, and the recording folders.

### 6.2 Live description

| Proposed Video API route | Purpose |
|---|---|
| `GET /v1/cameras/{camera_id}/live` | Say whether each go2rtc stream is receiving media, and list protocols confirmed configured and available for that stream. |

**Proposed response.** URLs and ports are omitted until D3 is decided.

```json
{
  "camera_id": "cam01",
  "streams": [
    {
      "name": "cam01_main",
      "role": "main",
      "receiving": true,
      "protocols": ["webrtc", "mse", "rtsp"]
    },
    {
      "name": "cam01_sub",
      "role": "sub",
      "receiving": true,
      "protocols": ["webrtc", "mse", "rtsp"]
    }
  ]
}
```

| Field | Label |
|---|---|
| `receiving` | **Proposed.** True when a go2rtc producer’s `bytes_recv` increases. The recorder helper `stream_alive` waits 2 seconds. That window is the tool’s default, not an accepted API parameter. |
| `protocols` | **Proposed** list, limited to methods **Confirmed** configured and available for that stream. On the reference gateway those are WebRTC (port 8555 and signaling on 1984), MSE (`/api/stream.mp4` on 1984), and RTSP (port 8554). M5 names HLS on port 1984 and records no path, so `hls` is not listed and is not treated as verified. Listing a name here does not mean the Video API serves it. Codec and audio limits are in Appendix A. |
| URL, port, ICE host | **Open question** D3. Not in this schema. Confirmed listener facts are in Appendix A. |

**Confirmed** (Simon, 2026-10-26) applied to this **Proposed** route. `GET /v1/cameras/{camera_id}/live` reports stream status. It does not establish an authorized playback session, does not mint a live or playback grant, and does not return media. A playback session would be a different operation. No playback route is defined (F1).

**Proposed.** When a client does play, it plays through go2rtc. The Video API does not open the camera. A response never embeds a password in a URL.

**Open question.** D3. Simon decides whether the contract later adds URLs, and whether all four protocols are part of the promise. WebRTC candidates today are `GATEWAY_LAN_IP:8555` only (reference `192.168.30.200`). Remote viewers are the M15 topic M5 excludes, not a behavior this route defines.

### 6.3 Timeline

| Proposed Video API route | Purpose |
|---|---|
| `GET /v1/cameras/{camera_id}/timeline?start=&end=` | List recorded intervals and holes in a UTC window. Optional `cursor` repeats the previous `next_cursor`. That pairing is proposed and not final until F8. |

**Proposed.** This is a metadata list. It does not seek and it does not return bytes. Seeking and file download are section 7.

**Proposed query, not final until F8.** `start` and `end` are required, RFC 3339 UTC, with `end` after `start`. When `cursor` is sent, it is the previous response’s `next_cursor`, with the same `start` and `end`. **Open question** F8: page size is undecided, so this draft does not define `limit` or when paging stops.

**Proposed response.** Illustration only. The instants below are not a recorded outage.

```json
{
  "camera_id": "cam01",
  "stream": "main",
  "source": "frigate",
  "start": "2026-10-24T12:40:00Z",
  "end": "2026-10-24T12:46:00Z",
  "segments": [
    {"start": "2026-10-24T12:40:00Z", "end": "2026-10-24T12:40:48Z"},
    {"start": "2026-10-24T12:41:51Z", "end": "2026-10-24T12:45:58Z"}
  ],
  "gaps": [
    {"start": "2026-10-24T12:40:48Z", "end": "2026-10-24T12:41:51Z"}
  ],
  "next_cursor": null
}
```

| Field | Label |
|---|---|
| `segments[].start`, `segments[].end` | **Proposed** JSON. **Confirmed** source fields on Frigate’s list: `start_time`, `end_time` (epoch seconds). |
| `source` | **Proposed** value `frigate` for this route. Frigate’s record role is the main stream, so `stream` is `main`. |
| `gaps` | **Proposed** shape only. **Open question** F7. A gap would be a hole between consecutive segments larger than an agreed threshold. Calculation of `gaps`, and acceptance of that calculation, both depend on that F7 threshold. Tool defaults of 1.5 s and 3 s are not the threshold. Neighbouring Frigate segments can overlap; overlap is not a gap. The gap in the example is not an accepted result. |
| `next_cursor` | **Proposed** pair with request `cursor`: the client sends this value back with the same `start` and `end`. **Open question** F8. The pair cannot be finalized, and no page size is set, until F8 is decided. `null` in the example is only a sample value. |

**Existing interface** the Video API would call for this metadata, if D1 accepts the route: Frigate’s authenticated API at `https://192.168.30.200:8971`, as the viewer `video_api`. The recorder’s own tools call `GET http://127.0.0.1:5000/api/<camera>/recordings`. That host-local URL is not a Video API source. This route is not a Frigate URL and not a playback session.

**Open question.** D1. Simon accepts the route. The substream directory is not a source for this route (F6). Trust of the self-signed certificate is F14.

### 6.4 Health

| Proposed Video API route | Purpose |
|---|---|
| `GET /v1/health` | The Video API process answered. |
| `GET /v1/health/cameras` | Per camera, live receive and recording age as separate facts. |

**Proposed `GET /v1/health`.**

```json
{"status": "ok", "component": "video-api"}
```

**Proposed.** `ok` means this process answered. It does not mean cameras are live or recording. Who may call either health route is unresolved in the section 8.3 matrix. Deny-by-default applies until an exception is approved. No exception is approved.

**Proposed `GET /v1/health/cameras`.** Illustration only. `age_seconds` is `checked_at` minus `newest_segment_end`.

```json
{
  "checked_at": "2026-10-24T12:45:00Z",
  "cameras": [
    {
      "camera_id": "cam01",
      "live": {
        "cam01_main": {"receiving": true, "producers": 1},
        "cam01_sub": {"receiving": true, "producers": 1}
      },
      "recording": {
        "source": "frigate",
        "newest_segment_end": "2026-10-24T12:44:56Z",
        "age_seconds": 4
      }
    }
  ]
}
```

| Field | Label |
|---|---|
| `producers` | **Confirmed** concept on go2rtc: M5 checks one producer (the camera connection) while Frigate and another viewer are attached. **Proposed** as this field. |
| `newest_segment_end` | **Proposed** formatting of the newest Frigate `end_time`. |
| `receiving` | Same proposal as section 6.2. |

**Confirmed.** Frigate’s own health check has stayed healthy while the disk was unwritable (finding F1). `/api/stats` has shown a normal frame rate during a recording gap (finding F2). **Proposed.** This body reports each stream’s `receiving` status and `producers` count, plus `newest_segment_end` and `age_seconds`. It does not include go2rtc byte counters.

**Existing interfaces:** go2rtc `GET /api/streams` on `http://192.168.30.200:1984`, and Frigate recording metadata on `https://192.168.30.200:8971` for `recording`. Neither call uses port 5000. Details in Appendix A.

---

## 7. Candidate functionality (M14–M18)

**Proposed.** The items below are not M13 routes. The repository does not say which later module implements them. This draft does not assign them.

**Open question.** F1. Historical playback, seeking, and clip export as Video API operations. No path, query, status code, job id, or response body is defined. Simon’s grant policy names live and playback grants, and requires an audit event when an export happens. That does not approve a playback or export route. Agreement still needs a written decision that names what the client receives (description, URL, or bytes), seek granularity, export lifetime, and codec and audio. Until that decision, an implementation does not expose these operations.

**Confirmed constraints any later design still has to respect:** Frigate records the main stream. The Video API reads Frigate only at `https://192.168.30.200:8971` with the viewer account, through one adapter, and does not call port 5000. Downloads prefer `clip.mp4` because it does not keep a separate export copy. A persistent export, if Simon approves that scope, is queued, prefixed `videoapi-`, and cleaned up with Izzatullokh until M8 automatic cleanup exists. Starting that Frigate export job is allowed (F12). Changing configuration, users, cameras, or other recordings is not. The substream file contract belongs to the uploader. Video is copied, not transcoded, by the current go2rtc setup. CPU and memory limits are required (F5).

**Confirmed.** M15, as M5 uses the label, is live view from outside the LAN, NAT traversal, TURN, and viewer accounts. M5 does not include them. This contract does not add them and does not define their API.

**Confirmed.** M14 is an undefined name (“Video API adapter”). This contract does not describe that adapter.

---

## 8. Authentication and authorization

### 8.1 Policy confirmed by Simon (2026-10-26)

**Confirmed.** Rails is the source of truth for tenant, site, camera, and role authorization. The gateway does not make those business decisions.

**Confirmed.** The gateway accepts only a short-lived grant that Rails issued for live or playback. Every request without an accepted grant is denied. Ability to open a port is not acceptance. Anonymous access is not approved.

**Confirmed.** After the gateway accepts a grant, it enforces the permissions written in that grant. It does not independently decide which tenant, site, camera, or role Rails should have allowed.

**Confirmed.** View, export, and grant events are audit-logged (section 8.4). The raw grant, credentials, and private keys are never logged.

**Confirmed.** Frigate’s admin interface is not directly exposed to the public.

Recorder facts that this policy does not replace (Appendix A): go2rtc HTTP Basic on `http://192.168.30.200:1984`, go2rtc RTSP credentials on `rtsp://192.168.30.200:8554`, and Frigate’s viewer login on `https://192.168.30.200:8971`. Port 5000 stays on the host and is not a Video API client or upstream. Those upstream logins are not the Video API’s client authorization. Client authorization remains the Rails grant.

**Open question.** Reusing `GO2RTC_API_USER` / `GO2RTC_API_PASSWORD` inside the Video API process would share a recorder secret. That still needs Izzatullokh. It is not assumed. The client never receives those values.

### 8.2 Proposed grant format

**Proposed.** Awaiting backend approval. Simon required a short-lived Rails-issued grant. He did not specify the bytes on the wire. Nothing in this section is an approved token format.

**Proposed.** The client sends a bearer token. The gateway verifies it with a Rails public key. The matching private key stays on Rails. The gateway stores no Rails private key.

| Detail | Proposal awaiting backend approval |
|---|---|
| Format | Compact token: a JOSE header, a JSON payload, and a signature. Algorithm is not chosen. |
| Claims | `iss` (Rails), `aud` (this Video API), `sub`, `tenant_id`, `site_id`, `camera_ids`, `permissions`, `iat`, `exp`, `jti`. Names are proposals. |
| Expiry | `exp` is required and short. The maximum lifetime is not set. |
| Audience | `aud` must name this gateway’s Video API. The literal audience string is not set. |
| Key rotation | Public keys are selected by `kid`. More than one public key may be valid during rotation. The overlap duration is not set. A failed `kid` or signature is a denial. |
| Validation | Require a valid signature, `iss`, `aud`, and `exp` checked against the gateway clock. Require an explicit allowlist of approved signing algorithms, and reject every other algorithm. A missing, malformed, or mismatched `iss`, `aud`, or `exp` rejects the grant. Then enforce `permissions` and `camera_ids` from the payload. The allowlist contents, the `iss` value, the `aud` value, and the maximum lifetime are not set. Do not ask Rails to remake the authorization decision on each call. A revocation list is not specified. |

**Proposed** requirement of this draft: an implementation accepts a grant only after those signature, `iss`, `aud`, `exp`, and algorithm-allowlist checks. Unexpected algorithms are rejected. A missing, malformed, or mismatched issuer, audience, or expiry also rejects the grant. This does not choose the algorithm, the claim set, the maximum lifetime, the audience value, or the key-rotation rules.

**Open question.** D2 remainder. The Rails backend and Simon still have to approve the token format, claim names, maximum lifetime, audience value, algorithm allowlist, and key rotation. Until those parameters are approved, the gateway still denies by default. It does not treat this table as an accepted token.

### 8.3 Proposed authorization matrix

**Proposed.** This matrix is not approved. It applies the confirmed deny-by-default rule to the candidate routes in section 6. Playback and export have no rows because they have no routes (F1).

| Candidate route | What an accepted grant allows | Unresolved |
|---|---|---|
| `GET /v1/cameras` | Return only cameras the grant’s `camera_ids` allow. | Whether a grant with no cameras is `200` with an empty list or `403`. |
| `GET /v1/cameras/{camera_id}` | Return the object only when that id is allowed. | `403` versus `404` when the id exists and the grant does not allow it. |
| `GET /v1/cameras/{camera_id}/live` | Report status for an allowed camera. This is not a playback session. | Whether status is covered by a live grant or needs its own permission. |
| `GET /v1/cameras/{camera_id}/timeline` | Return metadata for an allowed camera. This does not return playback bytes. | Whether a playback grant covers the timeline. Playback media stays F1. |
| `GET /v1/health` | Not decided. | Whether process liveness is an exception to deny-by-default. No exception is approved. |
| `GET /v1/health/cameras` | Per-camera facts limited to cameras the grant allows. | Whether a grant with no cameras may see any of this body. |

**Proposed.** A missing or invalid grant is HTTP 401. An accepted grant that lacks the permission is HTTP 403. Simon required denial. He did not name these status codes.

### 8.4 Audit

**Confirmed.** The gateway writes an audit event for a view, an export, and a grant event. That is Simon’s requirement. It is unchanged.

**Proposed** distinction inside a grant event, not a further approval from Simon:

- Rails grant issuance: Rails creates the grant. This gateway does not issue it.
- Gateway grant acceptance: the gateway accepts the grant.
- Gateway grant rejection: the gateway refuses the grant.

**Confirmed.** The log never contains the raw grant, a credential, a private key, a camera password, or RTSP userinfo.

**Proposed** fields, not approved: UTC time, event type (`view`, `export`, or `grant`), result (`allowed` or `denied`), route name, `camera_id` when the event has one, `jti`, `tenant_id`, and `site_id`. `jti` identifies the grant. The token itself is not stored.

**Open question.** F11. Simon has not approved logging every HTTP request, or logging every denied attempt beyond the view, export, and grant events above. The proposed issuance, acceptance, and rejection split is not that approval. Both wider questions stay unresolved. An export event applies when an export operation exists. No export route exists yet (F1).

---

## 9. Media integration constraints

**Confirmed versions.** Frigate `0.18.0` and go2rtc `1.9.14`, image names and digests in `recorder/versions.env`.

**Confirmed** rules for an implementation of section 6, from `shared-gateway.md`:

- The camera list is `/opt/vivo-gateway/cameras.yaml`. The file is readable. The deployment mounts all of `/opt/vivo-gateway` read-only.
- Live state comes from go2rtc at `http://192.168.30.200:1984`, stream names `<id>_main` and `<id>_sub`. RTSP restream, when used, is `rtsp://192.168.30.200:8554/<id>_main` or `_sub`. The camera’s own RTSP URL is not a client or a server target.
- A timeline, and the `recording` object on `GET /v1/health/cameras`, use Frigate at `https://192.168.30.200:8971` as `video_api`. They do not use port 5000. Certificate trust is F14.
- Frigate calls sit in one adapter. The implementation does not change configuration, users, cameras, or recordings, except the permitted export-job exception in F12.
- A download uses `clip.mp4` when that is enough. A persistent export is queued, named with a `videoapi-` prefix, and cleaned up with Izzatullokh until M8. A Video API route for either one still needs Simon (F1). CPU and memory limits are set. The numbers are F5.
- The process is intended to run in the ready rootless Docker environment. On the host, Marlon can read `/etc/vivo/video-api.env` through a read-only ACL. That host fact does not show that the running process can read the file or authenticate to go2rtc and Frigate (F16). The file’s contents are not part of this contract.

**Open question.** F14 and F16. Trust of Frigate’s self-signed certificate is pending coordination with Haris. Runtime use of the secrets file, and a successful go2rtc or Frigate login from this process, are pending verification. This draft does not change `recorder/compose.yaml` and does not copy secrets.

**Confirmed.** The substream path in `SEGMENT_FORMAT.md` is the uploader’s contract (agreed 2026-10-19). No writer for it is in this repository. It is not a section 6 source (F6).

Codec, audio, gaps, and retention evidence used when implementing or when answering F1–F3 and F7 are in Appendix A. H.264 versus H.265 stays F2.

---

## 10. Operations and security

**Confirmed.** Recorder containers restart unless stopped. Docker JSON logs are capped at 5 × 20 MB. `vivo-gateway.service` starts the recorder on boot. For ports published by the recorder’s system Docker, the current `DOCKER-USER` rules allow `LAN_CIDRS` and `ADMIN_CIDRS` and drop other sources. Widening that set is a firewall change and has to be announced. The Video API uses a separate rootless Docker daemon. Those system-Docker rules do not automatically protect a port that rootless daemon publishes.

**Open question.** F15. Rootless port exposure, and verification of the firewall in front of it, are pending confirmation from Jorge. This draft does not treat `DOCKER-USER` as that confirmation.

**Confirmed** (Simon, 2026-10-26). Mender OTA packaging and rollback for this Compose project stay open pending Mohamed’s guidance (M27, F9). The repository names OTA as M27 and does not describe Mender or name Mohamed. Jorge’s ready rootless environment does not replace that agreement.

**Confirmed** (Simon, 2026-10-26). Gateway identity and TLS are a cross-milestone dependency on Haris’s M21 and M23 work (F10). `recorder/docs/m1-linux-baseline.md` names M21 as gateway identity and the edge agent, and M23 as security hardening, both outside M1. It does not define the interface. The Video API aligns with that interface. It does not invent a second identity or a second TLS design.

**Proposed.** A Video API restart does not open camera RTSP sessions and does not restart Frigate or go2rtc. When a Compose file is added, its image is pinned by version and digest, and it reads `/etc/vivo/video-api.env` without copying secrets into the repository. That Compose file is not part of this draft (F5, F9).

**Proposed.** Logs follow `vivo.redact`: URL userinfo and secret values are removed before they are stored. Audit rules in section 8.4 also forbid the raw grant and private keys.

**Confirmed.** M5’s integration check is one go2rtc producer per stream while Frigate and a viewer are both connected. An implementation that opened the cameras would fail that condition.

**Open question.** F5 and F10. The Video API’s listen port and its numeric CPU and memory limits are still open. Limits are required. Install path `/opt/vivo-video-api` and the rootless runtime are confirmed. TLS for clients of this API waits on Haris. Recorder tests open go2rtc’s web port over HTTP on the LAN. That fact does not approve serving this API in cleartext. Trust of Frigate’s certificate stays a separate question, pending coordination with Haris (F14).

---

## 11. Acceptance criteria for this draft

**Proposed.** These criteria accept the draft. They do not add code, tests, or Compose files.

Simon:

1. Every normative sentence remains labeled, and no Open question is marked Confirmed without a written decision or a repository change.
2. D1 is answered for each route in section 6. Deferred items stay in the deferred table.
3. Section 7 still defines no playback or export route, status code, or sample body.
4. Section 8.1 matches Simon’s 2026-10-26 policy: deny by default, Rails grants only, no public Frigate admin. Section 8.2 requires validation of the signature, `iss`, `aud`, and `exp`, and an explicit allowlist of signing algorithms that rejects any other algorithm. A missing, malformed, or mismatched `iss`, `aud`, or `exp` rejects the grant. The algorithm, claims, maximum lifetime, audience value, key rotation, and the other format parameters stay unapproved. Missing grants are not treated as allowed access.
5. Section 8.4 still requires audit events for a view, an export, and a grant. Issuance, acceptance, and rejection are labeled proposed. F11 stays open: every request and every denied attempt are not treated as approved logging. Raw grants, credentials, and private keys are never logged.
6. F2 stays open, or Vivo’s written codec choice is copied into the decision log. The contract does not promise that every browser can play the live stream.
7. The diff contains no secret values.

Izzatullokh:

1. Appendix A and section 2 match `recorder/docs/shared-gateway.md`: inventory path, LAN addresses, viewer account name, and no use of port 5000 by this service.
2. He agrees the API will not open camera RTSP or ONVIF, will not change recorder configuration, and will not call port 5000.
3. F12 and F13 match his and Jorge’s 2026-10-26 answers: export jobs are the only mutation allowed, the rootless environment is ready, the secrets file is readable on the host by a read-only ACL, and the whole recorder directory is mounted read-only. Host readability is not treated as a verified rootless login (F16). F14 stays open until he and Haris agree how to trust the certificate. F15 stays open until Jorge confirms rootless port exposure and its firewall.

**Confirmed.** Recorder checks for M1, M2, M3, and M5 stay the recorder’s.

---

## 12. Decision log

| Date | Item | Outcome | Label |
|---|---|---|---|
| 2026-10-19 | Substream segment format for the cloud uploader | Agreed by Izzatullokh and Haris. `recorder/docs/SEGMENT_FORMAT.md`. The Video API is not a party. | **Confirmed** |
| 2026-10-22 | Reference cameras measured as H.265 on both streams | `docs/camera-table.md` and finding F5. ONVIF reported H.264. Product choice of codec remains open (F2). | **Confirmed** measurement. **Open question** as a product decision |
| 2026-10 | Standalone go2rtc; Video API reads go2rtc | ADR 0001, status proposed, author Izzatullokh, still to confirm with Vivo. | **Confirmed** as a written proposal |
| 2026-10-25 | First draft of `/v1` text, including placeholder playback and export bodies and HTTP 501 | Superseded the same UTC day by the revision below. Those placeholders are withdrawn. | **Proposed** history |
| 2026-10-25 | Contract revision | M13 is this contract. Section 6 is the proposed reviewable API. Playback and export have no routes until F1 is agreed. Auth is unresolved. Stack in section 3 is a proposal. | **Proposed** |
| 2026-10-25 | Clarifications on the same UTC day | D6 includes Frigate for the timeline and for camera-recording health. Camera detail has its own example and a defined `404`. Timeline `next_cursor` matches the query. Unresolved auth and TLS do not approve an unauthenticated or cleartext deployment. | **Proposed** |
| 2026-10-26 | Simon’s access, stack, and deployment requirements | Python, FastAPI, and Docker Compose are approved. A separate `video-api/` folder and Compose file are acceptable only with Mohamed’s M27 Mender update and rollback agreement. Images are pinned by version and digest; `latest` is forbidden. Rails is the source of truth for tenant, site, camera, and role. The gateway accepts only a short-lived Rails-issued live or playback grant and denies by default. View, export, and grant events are audit-logged. Frigate’s admin interface is not exposed publicly. Gateway identity and TLS align with Haris’s M21/M23 work. The grant wire format, the section 8.3 matrix, playback and export routes, and F11 logging scope are not part of this approval. | **Confirmed** (Simon’s decision). Format and matrix remain **Proposed**. Playback and export routes remain **Open question** F1. |
| 2026-10-26 | Izzatullokh, `recorder/docs/shared-gateway.md` | Video API owner Marlon, install path `/opt/vivo-video-api`, secrets file `/etc/vivo/video-api.env` (no values copied). Inventory is a read-only mount of `/opt/vivo-gateway`, ids `cam01`–`cam10` shared. go2rtc `http://192.168.30.200:1984` and `rtsp://192.168.30.200:8554`, both authenticated. Frigate `https://192.168.30.200:8971` as viewer `video_api`; certificate self-signed until M23. Port 5000 is not used. No direct camera connections and no recorder configuration changes. Frigate calls stay in one adapter. Exports are queued. CPU and memory limits are required. F12, F13, and F14 were still open in this note. Rails grants are unchanged. | **Confirmed** for the sharing note. F12 and F13 are closed by the next row. F14 stays **Open question**. |
| 2026-10-26 | Izzatullokh and Jorge, follow-up to the sharing note | Rootless Docker and Docker Compose for the Video API are ready. `/etc/vivo/video-api.env` is non-empty and readable by Marlon through a read-only ACL; its contents are not recorded. `/opt/vivo-gateway/cameras.yaml` is readable, and the deployment mounts all of `/opt/vivo-gateway` read-only. Starting a Frigate export job is the permitted exception. Configuration, user, camera, and other recording mutations stay prohibited. Downloads prefer `clip.mp4` because it does not retain a separate export copy. A persistent export is queued, prefixed `videoapi-`, and cleaned up with Izzatullokh until M8 automatic cleanup. That does not approve a Video API export endpoint (F1). Certificate trust stays open pending Haris (F14). Mender OTA packaging and rollback stay open pending Mohamed (F9). | **Confirmed** for F12, F13, the `clip.mp4` preference, and the persistent-export rules. F1, F9, and F14 remain **Open question**. |
| 2026-10-26 | Draft corrections after that follow-up | Section 8.2 requires signature, `iss`, `aud`, `exp`, and an algorithm allowlist that rejects unexpected algorithms. Algorithm, claims, lifetime, audience value, and key rotation stay unapproved. Host readability of `/etc/vivo/video-api.env` is not a verified rootless login to go2rtc or Frigate (F16). System-Docker `DOCKER-USER` rules are not treated as protection for a rootless-published port (F15). Simon’s view, export, and grant audit requirement stands. Rails issuance, gateway acceptance, and gateway rejection are a proposed distinction. Logging every request and every denied attempt stays unresolved (F11). Raw grants, credentials, and private keys are never logged. | **Proposed** checks and distinctions. F11, F15, and F16 remain **Open question**. |

When a row in the decision summary is answered, add a line here and update the label on the affected section. A proposal becomes **Confirmed** only after that written decision, or because the repository already states it.

---

## Appendix A. Repository evidence

Implementation detail for reviewers. None of these paths are Video API routes.

### A.1 Reference site

**Confirmed.** Intel N150, Ubuntu Server 24.04 LTS, kernel 7.0.0-38 on the reference unit. Ten cameras in `recorder/cameras.yaml`: ids `cam01`–`cam10`, names `VIVO-CAM-01`–`VIVO-CAM-10`, hosts `192.168.30.201`–`192.168.30.210`, RTSP port 554. The inventory comment says Vivo assigned those addresses for this development environment. Paths: main `/media/live/1/1`, sub `/media/live/1/2`. Shared credential name `VIVOCAM` (`VIVOCAM_USER`, `VIVOCAM_PASSWORD` in `/etc/vivo/secrets.env`). Model `A_ONVIF_CAMERA N501`, firmware `V1.0.4.12-build:20260203164758`.

**Confirmed.** `GATEWAY_LAN_IP=192.168.30.200` in `recorder/gateway/sites/vivo-n150.site.env`. `MEDIA_ROOT=/srv/vivo`. `LAN_CIDRS=192.168.30.0/24`. `ADMIN_CIDRS=192.168.30.0/24,10.50.0.0/24`. VPN clients are NATed from `192.168.30.9`.

### A.2 Existing interfaces

**Confirmed** for the Video API (`recorder/docs/shared-gateway.md`, plus Jorge and Izzatullokh on 2026-10-26). It does not use Docker DNS names or port 5000. It uses `http://192.168.30.200:1984`, `rtsp://192.168.30.200:8554`, and `https://192.168.30.200:8971`. On the host, credentials are in `/etc/vivo/video-api.env`, which is non-empty and readable by Marlon through a read-only ACL. That host fact is not a verified read by the running rootless process, and it is not a successful authentication to go2rtc or Frigate (F16). The file is not copied here. The Frigate account name is `video_api`, role viewer. Its certificate is self-signed until M23 (F14). The deployment mounts all of `/opt/vivo-gateway` read-only. Downloads prefer `clip.mp4`. A persistent export uses the `videoapi-` prefix.

The calls below are what the recorder tools do on the gateway host. They are evidence of fields and behavior. They are not the Video API’s base URLs.

**Existing interface.** go2rtc, default base `http://127.0.0.1:1984/api`, HTTP Basic when `GO2RTC_API_USER` is set (`recorder/tools/vivo.py`):

| Call | What the recorder reads |
|---|---|
| `GET /streams` | Object keyed by stream name. `producers[].id`, `producers[].bytes_recv`. |
| `GET /streams?src=<stream>` | One object with `producers` and `consumers`. |
| `GET /stream.mp4?src=<stream>` | Called from inside the Frigate container as `http://<user>:<password>@go2rtc:1984/api/stream.mp4?src=<stream>` by `tests/m5_check.py`. fMP4 probe. |
| `http://<gateway>:1984/stream.html?src=<stream>` | Player page. Optional `&mode=webrtc` or `&mode=mse`. |

**Existing interface.** go2rtc RTSP: `rtsp://<FRIGATE_GO2RTC_RTSP_USER>:<password>@<host>:8554/<stream>`. Frigate inputs use the Docker DNS name `go2rtc`.

**Existing interface.** Listeners in generated `go2rtc.yaml`: API `:1984` (`GO2RTC_API_USER`, `GO2RTC_API_PASSWORD`), RTSP `:8554` (`FRIGATE_GO2RTC_RTSP_USER`, `FRIGATE_GO2RTC_RTSP_PASSWORD`), WebRTC `:8555` with candidate `${GATEWAY_LAN_IP}:8555`.

**Existing interface.** Frigate, default base `http://127.0.0.1:5000/api`, no auth in `vivo.frigate`:

| Call | What the recorder reads |
|---|---|
| `GET /version` | Version string compared with `FRIGATE_VERSION`. |
| `GET /config`, `GET /config/raw` | Running config equals `frigate/config.yml`. |
| `GET /stats` | GPU and CPU fields in the M2 check. |
| `GET /<camera>/recordings?after=<epoch>&before=<epoch>` | `start_time`, `end_time`. |

`tools/continuity.py` mentions `/api/recordings/unavailable` only to say it is coarser. Nothing calls it. No recording-download or VOD URL appears in the tree.

**Confirmed.** Compose publishes `8971` as the authenticated Frigate UI and API, and `127.0.0.1:5000:5000` as the internal API. Recorder tools call port 5000 and do not call port 8971. `shared-gateway.md` assigns port 8971 to other services, including this one, and keeps port 5000 on the host. `secrets.env.example` has no Frigate login variables. The Video API’s copy of those logins is `/etc/vivo/video-api.env`.

**Confirmed.** For the recorder’s system Docker, `DOCKER-USER` accepts established flows, `docker0`, and `br-*`, then `LAN_CIDRS` and `ADMIN_CIDRS`, then drops. Port 5000’s publish address is loopback, so other containers do not reach it by dialing their own `127.0.0.1`. These facts do not show that the same chain covers a port published by the Video API’s rootless daemon (F15).

### A.3 Streams, codec, and audio

**Confirmed.** go2rtc pulls each camera stream once. Video is not transcoded. M5 documents WebRTC (8555 plus signaling on 1984, latency under 1 s), MSE (1984, about 1–2 s), HLS (1984, several seconds; no path recorded), and RTSP (8554).

**Confirmed measurement, 2026-10-22 04:06:57 UTC**, through go2rtc (`docs/camera-table.md`): both streams H.265 Main, 25 fps, G.711 A-law 8000 Hz. Main 2880×1620, GOP about 5.02 s. Sub 640×360. ONVIF reported H.264 (finding F5). The scope notes call H.264 the default. The product switch is F2.

**Confirmed.** H.265 live view was verified in Chrome on macOS for the main stream, WebRTC and MSE, 20 of 20 runs (`docs/m5-go2rtc-notes.md`). Firefox has no H.265. Chrome and Edge need a hardware H.265 decoder. That note says Linux desktops usually lack it.

**Confirmed audio.**

| Path | What the repository records |
|---|---|
| Camera | G.711 A-law. A PCMU back channel exists and is unused. |
| WebRTC | G.711 can play as is. |
| MSE / fMP4 | Verified video only for these G.711 cameras. |
| Optional go2rtc `#audio=aac` or Opus | Described as possible, off by default, CPU cost per viewed stream. |
| Frigate main recording | `audio: aac` in `cameras.yaml` (also `copy` or `none`). AAC recording was verified. MP4 cannot hold G.711. Video is copied. |
| Substream contract files | Video only unless Vivo asks for audio. |

### A.4 Recordings

**Confirmed.** Frigate record input is `<id>_main`. Detect input is `<id>_sub`. Detection, snapshots, audio events, Birdseye, MQTT, and the AI features are off. Current hwaccel value is `preset-intel-qsv-h265`. Inventory comment: an H.264 camera change would use a different preset. That change belongs to the recorder.

**Confirmed.** `record.continuous.days` is 7 and `motion.days` is 0 in the current inventory. M9 tunes retention. The 7 is not a promise to this API (F7).

**Confirmed.** M2 looks for new `*.mp4` files under `${MEDIA_ROOT}/frigate/media/recordings`. The repository does not specify Frigate’s subdirectory layout. Reference mount: `/srv/vivo`.

**Confirmed.** Substream contract, agreed 2026-10-19: `/srv/vivo/recordings/substream/<camera_id>/<YYYY-MM-DD>/<camera_id>_<YYYYMMDDTHHMMSS>Z.mp4`. About 60 seconds, cut on keyframes, UTC from the first frame. Written as `.mp4.part` in the same directory, renamed when finished. A missing minute means nothing was recorded. Finished files are not modified again. Retention may delete them. Reference media in that contract: H.265 `hvc1`, 640×360, 25 fps, keyframe about every 5 s, video only. No writer for that directory is in this repository. Samples were noted at `/srv/vivo/samples/substream/` on the reference gateway.

**Confirmed.** Finding F9: about 60 main-stream segments per camera per 10 minutes during one interval. That is an observation, not a configured segment length. `continuity.py` says neighbouring segments can overlap slightly.

**Confirmed recovery measurements, not API timeouts.** Reboot on 2026-10-24 12:44 UTC: cameras recording again 61–62 seconds after the reboot command. Frigate kill: API back in 8 seconds, recording within 8 seconds. Camera power cuts: recording gaps 43–63 seconds. The M3 report used a 120 second limit. `docs/acceptance-limits-proposal.md` still lists 120 seconds as a kickoff proposal.

**Confirmed.** Finding F7: quiet main streams about 160–310 kbit/s each, against an ONVIF cap of 6000 kbit/s. Recording volume about 1.2 TB. The scope’s 2 TB disk is not in the unit.

### A.5 Load and failure signals

**Confirmed.** Idle record 2026-10-24, ten cameras, detection off, no viewer: CPU about 43% of four cores, RAM about 3.5 GB of 11.5 GB (`docs/m1-idle-record.md`). Finding F10: go2rtc about 18% of one core; Frigate recording CPU includes G.711-to-AAC. Haris’s uploader and agent are described as planned at about one core and 1 GB. No Video API budget is written down (F5).

**Confirmed.** F1: Frigate can look healthy while the recording disk returns I/O errors. F2: the record watchdog waits at least 120 seconds, and `/api/stats` can show a normal frame rate during a gap.
