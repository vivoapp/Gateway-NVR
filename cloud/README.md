# cloud/ : Haris's modules

Installed on the gateway at `/opt/vivo-cloud`. One folder per module. Secrets are never in Git: they live in
`/etc/vivo/cloud.env` on the gateway (root-owned, mode 640, plus read access for the haris user). Data lives in `/var/lib/vivo-cloud`.

| Folder | Module | Status |
|---|---|---|
| `uploader/` | M19 archive upload queue + M20 interrupted upload and recovery | Code done and tested (steps 1 to 10). Waiting for the 24-hour run on real B2 |
| `discovery/` | M4 ONVIF discovery | Not started |
| `agent/` | M21 Edge Agent, gateway identity | Not started |
| `hardening/` | M23 hardening | Not started |
| `support/` | M24 remote troubleshooting | Not started |

## Contracts this part depends on
- Reads segments read-only from `/srv/vivo/recordings/substream/<camera_id>/<YYYY-MM-DD>/<camera_id>_<YYYYMMDDTHHMMSS>Z_<YYYYMMDDTHHMMSS>Z.mp4` (recorder/ contract).
- Never deletes local files (retention M9 is the recorder's job).
- Uploads straight to Backblaze B2 over the S3-compatible API.

## Rules (from the repo README)
No passwords, keys or tokens in Git. Pin every image by version and digest. Tell the group before a reboot,
firewall change or internet cut.

## Known to fix before this is merged
The uploader still contains the evaluation demo Docker files (`docker-compose.yml`, `run.sh`, a `minioadmin` test login).
These are local-only (the gateway setup is `uploader/deploy/`). Local deletion is gone.
