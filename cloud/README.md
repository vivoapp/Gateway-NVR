# cloud/ : Haris's modules

Installed on the gateway at `/opt/vivo-cloud`. One folder per module. Secrets are never in Git: they live in
`/etc/vivo/cloud.env` on the gateway (root-owned, mode 600). Data lives in `/var/lib/vivo-cloud`.

| Folder | Module | Status |
|---|---|---|
| `uploader/` | M19 archive upload queue + M20 interrupted upload and recovery | In progress (step 2 of 9 done) |
| `discovery/` | M4 ONVIF discovery | Not started |
| `agent/` | M21 Edge Agent, gateway identity | Not started |
| `hardening/` | M23 hardening | Not started |
| `support/` | M24 remote troubleshooting | Not started |

## Contracts this part depends on
- Reads segments read-only from `/srv/vivo/recordings/substream/<camera_id>/<YYYY-MM-DD>/<camera_id>_<YYYYMMDDTHHMMSS>Z.mp4` (recorder/ contract).
- Never deletes local files (retention M9 is the recorder's job).
- Uploads straight to Backblaze B2 over the S3-compatible API.

## Rules (from the repo README)
No passwords, keys or tokens in Git. Pin every image by version and digest. Tell the group before a reboot,
firewall change or internet cut.

## Known to fix before this is merged
The uploader still contains the evaluation demo (`docker-compose.yml`, `run.sh`, `minioadmin` test login,
local deletion after upload). These are for local testing only and are replaced in steps 3 and 6.
