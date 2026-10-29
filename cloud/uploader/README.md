# Segment Uploader (Vivo CAM modules M19 and M20)

A small Linux service for the Vivo CAM gateway. It watches the recorder's substream folder for finished
one-minute segments and uploads each one to S3-compatible storage (Backblaze B2 in production, MinIO in the
tests). It keeps the status of every segment in a local SQLite file, so it carries on after a network failure,
a crash or a reboot. After an outage it catches up by itself, oldest segment first.

It only reads the segment files. **It never deletes or changes them**: removing old segments is the
recorder's retention (M9).

- **M19, archive upload queue:** detect finished segments, upload, verify, no duplicates, status that survives
  restarts, index record per segment, speed limit, second S3 store by configuration only.
- **M20, interrupted upload and recovery:** checksum check after every upload, gap report, 18 scripted
  recovery tests, measured catch-up time, retention recommendation for M9.

Test results are in [`TEST_REPORT.md`](TEST_REPORT.md). The version history is in [`CHANGELOG.md`](CHANGELOG.md).

## How it works

```
 recorder (M7)                         gateway                                       cloud
 ┌──────────────────┐   finished   ┌──────────────────────────────┐   PUT + verify   ┌────────────────────┐
 │ <cam>_<start>Z_  │   *.mp4      │ scan ─► state file (SQLite)  │ ───────────────► │ Backblaze B2       │
 │ <end>Z.mp4.part  │ ───────────► │          │                   │  (S3 API)        │ (or MinIO in tests)│
 │ renamed to .mp4  │  read-only   │          ▼                   │                  └────────────────────┘
 └──────────────────┘              │ upload oldest first, 2 cams  │
        ▲                          │ at a time, read back size    │   index record (one JSON line per segment)
        │ M9 deletes old files     │ and checksum                 │ ───────────────► index/index-<date>.jsonl
        └──────────────────────────┴──────────────────────────────┘
```

1. **Scan** every `scan_interval` seconds. A file ending in `.mp4` is finished (the recorder renames
   `<name>.mp4.part` only when the segment is complete). New segments are added to the state file as `pending`.
2. **Upload** the oldest pending segments (different cameras in parallel, each camera in order): hash the file,
   ask the bucket whether the key already exists, upload with a `Content-MD5` header if not, then read back size
   and ETag and compare them with the local file.
3. **Finish:** mark the segment `uploaded` and hand one index record to the index sink. The local file is left alone.
4. **On any error:** write it down, schedule the next try, keep the local file. The state is saved before each step
   that can fail, so a crash costs at most one re-check.

### The segment file name

```
<camera_id>/<YYYY-MM-DD>/<camera_id>_<start>Z_<end>Z.mp4       start and end are UTC, YYYYMMDDTHHMMSS
```

Example: `VIVO-CAM-01/2026-10-29/VIVO-CAM-01_20261029T061824Z_20261029T061924Z.mp4`. The date folder is the date
of the start time, so a segment that crosses midnight stays in the folder of the day it started. The older name
without the end time (`<camera_id>_<start>Z.mp4`) is also accepted; the end is then start + `nominal_duration`.
A name outside this format is ignored and reported once (`event=ignored_name`). The object key in the bucket is
`<key_prefix>/<the same relative path>`.

## Run it on the gateway

Everything for the gateway is in [`deploy/`](deploy/README.md): `compose.yaml`, `config.gateway.toml` and
`cloud.env.example`. Short version:

```bash
# one time
sudo mkdir -p /var/lib/vivo-cloud
cd /opt/vivo-cloud/uploader/deploy
docker compose --profile setup run --rm init-data          # gives the state folder to the container user
# secrets (endpoint, bucket, key prefix, keys) go in /etc/vivo/cloud.env, never in Git
docker compose up -d --build                               # start (restart policy: unless-stopped)

docker compose ps                                          # is it running?
docker compose logs -f uploader                            # follow the log
docker compose exec uploader python -m segment_uploader status --remote --config /etc/vivo-uploader/config.toml
docker compose exec uploader python -m segment_uploader gaps   --config /etc/vivo-uploader/config.toml
docker compose down                                        # stop
```

The container runs as user 10001, with the recordings mounted read-only, a read-only container filesystem, all
Linux capabilities dropped, 1 CPU, 512 MB and 200 processes, and log rotation (20 MB x 5). The base image is pinned
by digest and the Python packages are pinned.

### The `status` and `gaps` commands

`status` shows the counts per state, how many local files are waiting, and with `--remote` the objects in the
bucket. `gaps` is the M20 gap report: **a gap is a segment that was never confirmed uploaded AND whose local file
no longer exists.** Uploaded segments are never gaps (even after the recorder removed the file); segments still
waiting with their file present are reported as "waiting", not as lost. Exit code 1 if there is any gap
(`--json` for the full list).

## Try it on a PC (Docker Desktop + Git Bash)

Two sets of scripts, both only for testing:

| Folder | Purpose |
|---|---|
| `deploy/test.sh` | The gateway setup on a PC: same container settings, MinIO as storage, 10 simulated cameras. Used for the M19 acceptance and the M20 recovery tests. |
| `run.sh` | A small demo (fake recorder, MinIO, scripted outage and crash) and the automated tests. |

```bash
./run.sh test                      # automated tests in Docker (incl. real MinIO)
./run.sh demo                      # scripted outage / crash / recovery

cd deploy
./test.sh start fast               # build and start (a segment per camera every 5 s)
./test.sh check                    # hardening and counts
./test.sh snapshot && ./test.sh accept            # M19 checks while running
./test.sh pause && ./test.sh accept --strict      # the final M19 check
./test.sh second && ./test.sh second-check        # second S3 store, configuration only
./test.sh report > m19-report.md
./test.sh recovery --quick         # M20: the 18 tests in about 6 minutes
./test.sh recovery                 # M20: the real run, about 45 minutes
./test.sh recovery-report > m20-report.md
./test.sh clean                    # delete all test data
```

## Configuration

Settings come from a TOML file (`deploy/config.gateway.toml` on the gateway, `config.example.toml` for a local
run). An unknown setting is an error. Site settings and secrets come from the environment and win over the file;
on the gateway they are in `/etc/vivo/cloud.env` (template: `deploy/cloud.env.example`).

| Setting | Default | Meaning |
|---|---|---|
| `[service] watch_dir` | required | Folder with `<camera>/<date>/<file>.mp4` (the recorder's substream folder) |
| `state_db` | `state.db` | SQLite file with the status of every segment (rebuildable) |
| `nominal_duration` | 60 | Seconds; segment end = start + this, only for names without an end time |
| `scan_interval` | 2 | Seconds between scans |
| `recent_days` | 2 | Normal scans look at the date folders of the last N UTC days |
| `full_scan_interval` | 3600 | A scan of every folder at start-up and then this often (seconds) |
| `workers` | 2 | Cameras uploaded at the same time; one camera's segments always go in order. `1` = strictly oldest first |
| `upload_limit_mbps` | 0 | Upload speed limit for all workers together; 0 = no limit |
| `backoff_initial`, `backoff_max` | 2, 300 | Retry delay doubles from the first to the second value (seconds) |
| `batch_size` | 50 | Segments taken from the queue at a time |
| `connect_timeout`, `read_timeout` | 5, 60 | Seconds |
| `stats_interval` | 60 | Seconds between `event=stats` lines (0 = off) |
| `[index] sink`, `dir` | `jsonl`, next to the state file | Where index records go (`none` = off) |
| `[s3] endpoint_url`, `bucket`, `region`, `key_prefix`, `access_key`, `secret_key` | | Environment: `S3_ENDPOINT_URL`, `S3_BUCKET`, `S3_REGION`, `S3_KEY_PREFIX`, `S3_ACCESS_KEY`, `S3_SECRET_KEY` |

To use a **second S3 store**, change only `S3_ENDPOINT_URL` (and the keys, bucket if needed). The same image and the
same config file work (`./test.sh second-check` proves it).

## What it guarantees

| Requirement | How it is done |
|---|---|
| Detect finished files | Only `*.mp4` in the agreed name format. `.part` files and everything else are ignored. |
| Upload | One `PUT` per file with `Content-MD5`, so the server rejects a damaged body. |
| Order | Oldest segment (UTC start in the name) first; cameras in parallel, each camera in order. |
| Remember status | SQLite table `segments`: `pending`, `uploading`, `uploaded`, `conflict`, `missing`. Committed with `synchronous=FULL`. |
| Retry | Delay doubles up to `backoff_max`. Storage problems pause the whole service; a problem with one file delays only that file. |
| Resume after restart | Anything left in `uploading` goes back to `pending` at start-up. |
| No duplicate uploads | The object key is the identity. An identical object already in the bucket is accepted as done; a different object under the same key is a `conflict` and is never overwritten. |
| Verify | Size and ETag are read back and compared with the local MD5 and size; a bad copy is deleted (only inside `key_prefix`) and the upload is repeated. SHA-256 is stored as object metadata. |
| Never touch local files | The uploader only reads them. There is no delete setting; the recordings are mounted read-only. |
| Speed limit | `upload_limit_mbps`, shared by all workers. |
| Index record | One JSON line per uploaded segment, delivered at least once (see below). |

### Error kinds

| Kind | Examples | What happens |
|---|---|---|
| `outage` | cannot connect, timeout, HTTP 5xx, SlowDown | whole service pauses with growing backoff |
| `auth` | wrong or expired key, access denied, bucket missing | whole service pauses for `backoff_max`; ERROR `storage_refused_credentials` |
| `full` | quota reached, HTTP 507 | whole service pauses for `backoff_max`; ERROR `storage_full` |
| `file` | bad digest, anything else about one file | only that file waits and is retried |

### Index record

```json
{"schema":1,"camera_id":"VIVO-CAM-01","start_utc":"2026-10-29T06:18:24Z","end_utc":"2026-10-29T06:19:24Z",
 "bucket":"vivo-segments","key":"site-001/VIVO-CAM-01/2026-10-29/VIVO-CAM-01_20261029T061824Z_20261029T061924Z.mp4",
 "size":1048576,"sha256":"...","etag":"...","uploaded_at":"2026-10-29T06:19:30Z"}
```

The default sink appends to `index/index-<date>.jsonl`. How the records reach Vivo's backend is not decided yet; a
different sink can be added in `index.py` without touching the uploader. A failing sink never blocks uploads, and a
record may repeat after a crash, so readers should treat `key` as unique. `end_utc` is exact to the second (it comes
from the file name); the real duration is in the MP4 header.

### Log lines

One line per event, `key=value` style. The ones to know:

| Event | Meaning |
|---|---|
| `detected` | a new finished segment was found |
| `uploaded` / `already_present` | uploaded and verified / the identical object was already in the bucket |
| `upload_failed` (`kind=`, `retry_in=`) | a try failed; the kind and the wait until the next try |
| `storage_refused_credentials`, `storage_full` | ERROR: the whole service is paused, a person must act |
| `conflict`, `missing` | ERROR: a different object under the key / the file vanished before it was uploaded |
| `startup` (`recovered_interrupted=`) | start-up; how many interrupted uploads were taken up again |
| `stats` | uploads, bytes, Mbps, `cpu_pct`, `rss_mb`, pending, workers, limit |
| `ignored_name`, `index_failed`, `loop_error` | a name outside the format / the index sink failed / an unexpected error in the loop |

## Tests

```bash
./run.sh test                                      # in Docker, with the real MinIO (all tests)
pip install -r requirements-dev.txt && pytest      # without Docker (MinIO tests skipped)
```

Without Docker the tests start a local S3-compatible server (moto) on a random port. With `./run.sh test` three
more tests run against the real MinIO. A real run is saved in `logs/docker-test-results.txt`.

| File | Covers |
|---|---|
| `test_success.py` | Normal upload, local files never deleted, `.part` and other files ignored, upload order, names |
| `test_scan.py` | File-name format, scan windows, full and quick scans, state upgrade, fake recorder names |
| `test_outage_restart.py` | Real connection failure, growing backoff, restart while down, crash after upload but before saving state |
| `test_duplicates.py` | One upload per segment across rescans and restarts, identical object adopted, conflicts never overwrite |
| `test_integrity.py` | Damaged upload rejected, mismatch found after upload, file changed during upload, vanished file |
| `test_errors.py` | Error kinds, pause on wrong credentials or full storage, one bad file delays only itself, delete guard |
| `test_speed_workers.py` | Speed limit applied once, parallel cameras, order per camera, outage stops all workers |
| `test_index.py` | Index record contents, sent once, failing sink, default file sink |
| `test_status_and_config.py` | The `status` command, environment overrides |
| `test_acceptance.py` | The M19 checker passes a good run and fails on each kind of damage |
| `test_gaps.py` | M20 gap report: only "never uploaded and file gone" is a gap |
| `test_fault_proxy.py` | The M20 test proxy: intact pass-through (also with a strict signature check), cut line, refused credentials, full, errors |
| `test_m20.py` | Retention and catch-up calculation; a short end-to-end run of the recovery script |
| `test_minio_integration.py` | Real MinIO: round trip with matching ETag, wrong `Content-MD5` rejected, duplicate and conflict |

## Folder layout

```
segment_uploader/   the product (about 1,200 lines):
                    config.py  segment.py (file names)  store.py (SQLite)  storage.py (S3, speed limit, error kinds)
                    index.py (index record + sinks)  gaps.py (M20 gap report)  service.py (the logic)  __main__.py (CLI)
deploy/             the gateway setup (compose.yaml, config.gateway.toml, cloud.env.example) and the PC test setup
                    (compose.test.yaml, test.sh); see deploy/README.md
tools/              test tools, not run on the gateway: m19_acceptance.py, m20_recovery.py, m20_retention.py,
                    fault_proxy.py, fake_recorder.py
tests/              automated tests
Dockerfile          image for the uploader (stage "runtime") and the tests (stage "test")
run.sh, docker-compose.yml, docker/   the small local demo
config.example.toml   settings for a local run without Docker
```

## Open items and limits

- **Not done yet:** the 24-hour acceptance run with 10 cameras on the real gateway with Backblaze B2 (needs the B2 bucket
  and the filled `/etc/vivo/cloud.env`), and the CPU and upload rate measured there.
- **Waiting for Vivo:** the agreed outage length and upload speed (the report assumes 2 days and shows 5 to 100 Mbps),
  and how the index records reach the backend.
- **Gateway checks:** Docker must start at boot for the restart policy to work after a reboot; recorder files must be readable by "other" (rootless Docker), and `/etc/vivo/cloud.env` must be
  readable by the `haris` user; see `deploy/README.md`.
- One `PUT` per file (limit 5 GiB), no multipart; objects are not re-checked later; a `conflict` or `missing` segment needs a
  person to decide (there are no alerts besides the log); a segment the recorder removed before the uploader ever saw it
  cannot be reported (the retention recommendation prevents that).
- `ENGINEERING_NOTE.md` is the note from the technical evaluation and describes the first version.
