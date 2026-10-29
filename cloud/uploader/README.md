# Segment Uploader

A small Linux service for the Vivo CAM gateway (modules M19 and M20). It watches the recorder's substream
folder for finished one-minute segments and uploads them to S3-compatible storage (Backblaze B2 in
production, MinIO for local tests). It keeps the status of every segment in a local SQLite file, so it can
retry after a network failure and carry on after a crash or restart.

It reads the segment files and **never deletes them**: the recorder's retention (M9) does that. Status:
built on the technical evaluation uploader, steps 1 to 6 of 9 done. See `CHANGELOG.md`. The Docker
files in this folder (`docker-compose.yml`, `run.sh`) are the local demo. The gateway setup is in `deploy/`
(see `deploy/README.md`).

Segment layout it expects (agreed with the recorder):
`<camera_id>/<YYYY-MM-DD>/<camera_id>_<YYYYMMDDTHHMMSS>Z.mp4` (UTC start time). The recorder writes
`<name>.mp4.part` and renames it when the segment is complete.

## Quick start (Docker)

You need Docker (Docker Desktop on Windows) and a bash shell (Git Bash on Windows). Nothing else.

```bash
./run.sh start     # builds the image, starts MinIO + uploader + a fake recorder (first run: a few minutes)
./run.sh demo      # scripted outage, crash and recovery in one terminal
./run.sh test      # automated tests, including tests against the real MinIO
```

Other commands: `./run.sh status`, `./run.sh logs`, `./run.sh stop` (keeps data), `./run.sh clean` (deletes data).

### What is running

```
 Your machine (Docker)
 ┌──────────────────────────────────────────────────────────────────────┐
 │  recorder ──► [recordings volume] ──► uploader ──► MinIO (S3 API)    │──► http://localhost:9001
 │  (fake cameras:     finished             │          bucket:          │    (web console)
 │   1 MB segment      segments             │          vivo-segments    │
 │   per camera        (*.mp4)              ▼                           │
 │   every 2 s)                      [state volume]                     │
 │                                   SQLite: status of every segment    │
 └──────────────────────────────────────────────────────────────────────┘
```

| Container | Role |
|---|---|
| `minio` | S3-compatible storage. API on `localhost:9000`, web console on `localhost:9001` |
| `create-bucket` | Creates the bucket `vivo-segments`, then exits |
| `uploader` | The service. Watches `/data/recordings`, uploads to MinIO, keeps its state in `/data/state` |
| `recorder` | Simulated cameras. Writes each segment as `.part`, then renames it when complete, like a real recorder |

Everything is stored in Docker volumes (`recordings`, `state`, `minio-data`), so stopping and restarting keeps the data.

### Where the uploaded files are, and how to look at them

- **Web console:** open <http://localhost:9001>, log in with `minioadmin` / `minioadmin`, open the bucket
  `vivo-segments`, folder `site-001/`. There is one folder per camera.
- **Command line:** `./run.sh status` prints the counts, how many local files are still waiting, and the number
  of objects in the bucket.
- **Logs:** `./run.sh logs`. Each upload is one line:
  `event=uploaded key=site-001/cam1/... size=... sha256=... attempts=1`.

### What `./run.sh demo` shows

1. **Normal operation.** Segments arrive in the bucket. Local files stay where they are.
2. **Storage outage.** MinIO is stopped. The log shows `upload_failed` with growing `retry_in` delays and no
   file is lost.
3. **Crash.** The uploader is killed with SIGKILL while storage is still down. The recorder keeps writing.
4. **Recovery.** MinIO and the uploader are started again. The uploader reads its state file, finds the unfinished
   work and catches up.
5. **Check.** The recorder is stopped, the backlog drains, and the script compares the numbers: segments recorded
   as uploaded, objects in the bucket, segments uploaded twice (must be 0), conflicts and missing files.
   It prints `RESULT: PASS` only if everything matches.

The demo's fake recorder deletes its own segments after 15 minutes. That stands in for the real recorder's
retention (M9) and keeps the Docker volume small. The uploader itself never deletes anything.

## If the MinIO image cannot be pulled

The official `minio/minio` and `minio/mc` images were removed from Docker Hub in 2026
("pull access denied"). `docker-compose.yml` therefore uses `cgr.dev/chainguard/minio:latest`, and the
bucket is created with the app image and boto3 instead of `mc`. To use a different S3-compatible image:

```bash
MINIO_IMAGE=<other-image> ./run.sh start
```

## What it guarantees

| Requirement | How it is done |
|---|---|
| Detect finished files | A `.mp4` file is finished: the recorder renames `<name>.mp4.part` to `<name>.mp4` only when the segment is complete. Everything else is ignored. An `.mp4` with a name outside the agreed format is ignored and reported once. |
| Upload to S3 storage | One `PUT` per file with a `Content-MD5` header, so the server can reject a damaged upload. |
| Order | Oldest segment (by UTC start time in the file name) first. Cameras upload in parallel (`workers`), each camera in order. |
| Remember status | SQLite table `segments` (state: `pending`, `uploading`, `uploaded`, `conflict`, `missing`). Committed with `synchronous=FULL`. It can be rebuilt: the bucket is the source of truth. |
| Retry after failure | Retry delay doubles (`backoff_initial` up to `backoff_max`). Storage problems (unreachable, wrong credentials, full) pause the whole service; a problem with one file delays only that file. |
| Resume after restart | On start, anything left in `uploading` goes back to `pending`. Pending files are picked up from the database. |
| No duplicate uploads | The object key (`prefix/relative/path`) is the identity of a segment. Before uploading, the service checks the bucket: an identical object is accepted as done, a different object under the same key is a `conflict` and is never overwritten. |
| Verify integrity | After upload, the service reads the object's size and ETag back and compares them to the local MD5 and size. A bad copy is deleted from the bucket (only inside our own `key_prefix`) and the upload is retried. The SHA-256 is also stored as object metadata. |
| Never touch local files | The uploader only reads them. There is no delete setting. |
| Speed limit | `upload_limit_mbps` caps the upload speed for all workers together. 0 = no limit. |
| Index record | One JSON note per uploaded segment goes to a replaceable sink (`index.py`); see below. |
| Readable logs | One line per event, `key=value` style. |

### Error kinds

| Kind | Examples | What happens |
|---|---|---|
| `outage` | cannot connect, timeout, HTTP 5xx, SlowDown | whole service pauses with growing backoff |
| `auth` | wrong or expired key, access denied, bucket missing | whole service pauses for `backoff_max`, ERROR `storage_refused_credentials` |
| `full` | quota or storage cap reached, HTTP 507 | whole service pauses for `backoff_max`, ERROR `storage_full` |
| `file` | bad digest, anything else about one file | only that file waits and is retried |

### Index record

After an upload is confirmed, one record is handed to the index sink:

```json
{"schema":1,"camera_id":"VIVO-CAM-01","start_utc":"2026-10-28T06:18:24Z","end_utc":"2026-10-28T06:19:24Z",
 "bucket":"vivo-segments","key":"site-001/VIVO-CAM-01/2026-10-28/VIVO-CAM-01_20261028T061824Z.mp4",
 "size":1048576,"sha256":"...","etag":"...","uploaded_at":"2026-10-28T06:19:30Z"}
```

The default sink (`jsonl`) appends one line per record to `index/index-<date>.jsonl`. How the records reach
Vivo's backend is not decided yet, so another sink can be added in `index.py` without touching the uploader.
Delivery is "at least once": a failing sink never blocks uploads, and a record may repeat after a crash, so
readers should treat `key` as unique.

## Tests

```bash
./run.sh test                       # in Docker, with the real MinIO (54+ tests)
pip install -r requirements-dev.txt && pytest     # without Docker (80 tests, 3 skipped)
```

Without Docker the tests start a local S3-compatible server (moto) on a random port, so they need no network.
With `./run.sh test` three more tests run against the real MinIO. They are skipped unless `MINIO_ENDPOINT` is set.
A real run is saved in `logs/docker-test-results.txt` (`./run.sh test`).

| File | Covers |
|---|---|
| `test_success.py` | Normal upload, local files never deleted, `.part` and other files ignored, upload order, names |
| `test_scan.py` | File-name format, scan windows, full and quick scans, state upgrade |
| `test_outage_restart.py` | Real connection failure, growing backoff, restart while down, crash after upload but before saving state, full loop in a thread |
| `test_duplicates.py` | One upload per segment across rescans and restarts, identical existing object adopted, conflicts never overwrite |
| `test_integrity.py` | Damaged upload rejected, mismatch found after upload, file changed during upload, vanished file |
| `test_errors.py` | Error kinds, pause on wrong credentials or full storage, one bad file delays only itself, remote delete guard |
| `test_speed_workers.py` | Speed limit applied once, parallel cameras, order per camera, outage stops all workers |
| `test_index.py` | Index record contents, sent once, failing sink, default file sink, sink setting |
| `test_status_and_config.py` | The `status` command used by `run.sh`, environment overrides |
| `test_gaps.py` | M20 gap report: only "never uploaded and file gone" is a gap; stretches, cameras, exit code, JSON |
| `test_fault_proxy.py` | The M20 test proxy: passes requests through intact; cut line, refused credentials, full, server errors; held answers |
| `test_m20.py` | Retention and catch-up calculation; a short end-to-end run of the 18-test recovery script |
| `test_minio_integration.py` | Real MinIO: round trip with matching ETag, server rejects a wrong `Content-MD5`, duplicate and conflict |

## Running without Docker

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
cp config.example.toml config.toml            # points at MinIO on localhost:9000
python -m segment_uploader run --config config.toml
python tools/fake_recorder.py --dir recordings --cameras 2 --interval 3 --size-mb 2
python -m segment_uploader status --config config.toml --remote
```

You then need an S3-compatible server on `localhost:9000` (for example `docker compose up -d minio create-bucket`).

## Configuration

See `config.example.toml` (local runs) and `docker/config.docker.toml` (containers). Unknown settings are an
error. Environment variables override the file: `S3_ACCESS_KEY`, `S3_SECRET_KEY`, `S3_ENDPOINT_URL`.
On the gateway the keys come from `/etc/vivo/cloud.env`, never from Git.
The MinIO login in `docker-compose.yml` is the default demo login. Never use it for anything real.

## How it works

1. **Scan** every `scan_interval` seconds. Finished segments are added to the database as `pending`.
2. **Upload** the oldest pending segments (cameras in parallel): hash the file (SHA-256 and MD5 in one pass),
   ask the bucket whether the key exists, upload if not, then read back size and ETag and compare.
3. **Finish**: mark `uploaded`, then hand the index record to the sink. The local file is left alone.
4. **On any error**: record it, schedule the next try, keep the local file.

The order matters: the state is written to disk before each step that can fail. If the process dies at any
point, the worst case is that one file is checked again after the restart.

## Layout

```
run.sh                one command for start / demo / test / status / logs / stop / clean
Dockerfile            image for the uploader and recorder; second stage adds the tests
docker-compose.yml    MinIO, bucket setup, uploader, recorder, status tool, tests
docker/               config used inside the containers
segment_uploader/     config.py  segment.py (file names)  store.py (SQLite)  storage.py (S3, speed limit, error kinds)
                      index.py (index record + sinks)  service.py (logic)  __main__.py (CLI)
tests/                automated tests and helpers
tools/                fake_recorder.py
```

## Known limitations

See `ENGINEERING_NOTE.md`.
