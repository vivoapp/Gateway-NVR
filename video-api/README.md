# Vivo Video API (Block C: M13 to M18)

The gateway's one interface to cameras and recordings for Vivo Watch. Rails decides who may see
what and signs a short-lived grant; this service checks the grant on every request and serves the
camera list, status, timeline, live view (WebRTC, with TURN as fallback), playback (HLS) and clips.
It hides the recorder: Rails and Vivo Watch never talk to Frigate or go2rtc.

- **Contract (M13):** [docs/api-contract.md](docs/api-contract.md), and [docs/openapi.json](docs/openapi.json)
  generated from the code.
- **Owner:** Izzatullokh. Installed on the gateway at `/opt/vivo-video-api`, next to the recorder
  (`/opt/vivo-gateway`).

## How it fits on the gateway

| Part | Where | Notes |
|---|---|---|
| Video API | container `video-api`, host network, port 8443 (HTTPS) | non-root, read-only, no extra privileges, 1 CPU, 512 MB |
| Recorder it reads | Frigate on `https://127.0.0.1:8971` (viewer account), go2rtc on `127.0.0.1:1984`, health on `127.0.0.1:9101` | never Frigate's port 5000 |
| Live-view gate | `vivo-live-gate.service` (root, host) | cuts one viewer off when asked ([ADR 0002](docs/adr/0002-live-sessions.md)) |
| Settings and secrets | `/etc/vivo/video-api.env` (root, 600) | made by `gateway/setup.sh` from [video-api.env.example](video-api.env.example) |
| Rails public keys | `/etc/vivo/video-api-keys/rails.jwks.json` | from Vivo's backend; test keys from `tools/grant_issuer.py` |
| Data | `/srv/vivo/video-api/` | clips (deleted after 1 h) and the audit log (30 days) |

## Install on a gateway

The recorder must be installed first (`recorder/gateway/setup.sh`). Then:

```bash
sudo video-api/gateway/setup.sh --dry-run     # shows every change
sudo video-api/gateway/setup.sh
```

Steps: settings, folders, TLS certificate, Frigate viewer account, firewall (8443 from the LAN and
admin networks), units, then build and start. Safe to run again.

## Code

| Folder | What |
|---|---|
| `vivo_api/` | the service: grants, routes, recorder adapters (`recorders/frigate.py`, `recorders/mock.py`), live sessions (`live/`), clips, audit |
| `gateway/` | install script, systemd units, the live-view gate (`live_gate.py`) |
| `tools/` | `grant_issuer.py` (test stand-in for Rails), `make_mock.py` (mock recorder footage), `export_openapi.py` |
| `web/player.html` | test player: live view with renewals, playback, measurements |
| `tests/unit/` | unit tests (`python -m pytest tests/unit`) |
| `tests/contract/` | the same tests for every recorder adapter (M18) |
| `tests/m14_check.py`, `m15_live.mjs`, `m16_playback.mjs`, `m17_clips.py` | acceptance checks against a running API; each writes a report to `reports/` |
| `tests/lab/` | the API on a workstation against the recorder's lab |

## Tests

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt pytest
.venv/bin/python -m pytest tests/unit tests/contract        # the Frigate half of tests/contract needs FRIGATE_URL
.venv/bin/python tests/handoff_check.py                     # nothing secret, everything pinned, pages and tests present
```

On a gateway, after install (with a test key made by `tools/grant_issuer.py keygen`):

```bash
tests/m14_check.py --key KEYS/test-1.private.pem --aud vivo-gw:<GATEWAY_ID> --audit-dir /srv/vivo/video-api/audit --label gateway
tests/m17_clips.py --key KEYS/test-1.private.pem --aud vivo-gw:<GATEWAY_ID> --label gateway
```

## Module pages

[M13 contract](docs/m13-api-contract.md), [M14 Frigate adapter](docs/m14-frigate-adapter.md),
[M15 live view](docs/m15-live-view.md), [M16 playback](docs/m16-playback.md),
[M17 clips](docs/m17-clips.md), [M18 second recorder](docs/m18-portability.md).
Results: [block-c-1-results.md](docs/block-c-1-results.md) (M13, M14),
[block-c-2-results.md](docs/block-c-2-results.md) (M16, M17),
[block-c-3-results.md](docs/block-c-3-results.md) (M15, M18).
