# Segment Uploader

A small Linux service that watches a folder for finished video segments (`.mp4`, `.ts`, `.mkv`) and
uploads them to S3-compatible storage (AWS S3, MinIO). It keeps the status of every segment in a local
SQLite file, so it can retry after a network failure and carry on after a crash or restart.

Built for the Vivo CAM technical evaluation. It does not touch Frigate, Rails or React.

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

1. **Normal operation.** Segments arrive in the bucket and the local copies are removed after verification.
2. **Storage outage.** MinIO is stopped. The log shows `upload_failed` with growing `retry_in` delays. Local
   files pile up and none are deleted.
3. **Crash.** The uploader is killed with SIGKILL while storage is still down. The recorder keeps writing.
4. **Recovery.** MinIO and the uploader are started again. The uploader reads its state file, finds the unfinished
   work and catches up.
5. **Check.** The recorder is stopped, the backlog drains, and the script compares the numbers: segments recorded
   as uploaded, objects in the bucket, segments uploaded twice (must be 0), conflicts, local files left.
   It prints `RESULT: PASS` only if everything matches.

For the demo the uploader deletes local files after a verified upload (`DELETE_AFTER_UPLOAD=true` in
`docker-compose.yml`). The default in the service itself is `false`.

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
| Detect finished files | A file counts as finished when its size and modified time are unchanged between two scans and it is at least `settle_seconds` old. Names ending in `.part`, `.tmp`, `.partial` are always skipped. |
| Upload to S3 storage | One `PUT` per file with a `Content-MD5` header, so the server can reject a damaged upload. |
| Remember status | SQLite table `segments` (state: `pending`, `uploading`, `uploaded`, `conflict`, `missing`). Committed with `synchronous=FULL`. |
| Retry after failure | Retry delay doubles (`backoff_initial` up to `backoff_max`). If the server is unreachable the whole service pauses instead of trying every file. |
| Resume after restart | On start, anything left in `uploading` goes back to `pending`. Pending files are picked up from the database. |
| No duplicate uploads | The object key (`prefix/relative/path`) is the identity of a segment. Before uploading, the service checks the bucket: an identical object is accepted as done, a different object under the same key is a `conflict` and is never overwritten. |
| Verify integrity | After upload, the service reads the object's size and ETag back and compares them to the local MD5 and size. A bad object is deleted and the upload is retried. The SHA-256 is also stored as object metadata. |
| Never delete early | With `delete_after_upload = true` the local file is removed only after the check above passes and the file is confirmed unchanged. |
| Readable logs | One line per event, `key=value` style. |

## Tests

```bash
./run.sh test                       # in Docker, with the real MinIO (28 tests)
pip install -r requirements-dev.txt && pytest     # without Docker (25 tests, 3 skipped)
```

Without Docker the tests start a local S3-compatible server (moto) on a random port, so they need no network.
With `./run.sh test` three more tests run against the real MinIO. They are skipped unless `MINIO_ENDPOINT` is set.
A real run is saved in `logs/test-results.txt` (without Docker) and `logs/docker-test-results.txt` (`./run.sh test`).

| File | Covers |
|---|---|
| `test_success.py` | Normal upload, local delete only after upload, in-progress and temp files ignored |
| `test_outage_restart.py` | Real connection failure, growing backoff, restart while down, crash after upload but before saving state, full loop in a thread |
| `test_duplicates.py` | One upload per segment across rescans and restarts, identical existing object adopted, conflicts never overwrite |
| `test_integrity.py` | Damaged upload rejected, mismatch found after upload, file changed during upload, vanished file |
| `test_status_and_config.py` | The `status` command used by `run.sh`, environment overrides |
| `test_minio_integration.py` | Real MinIO: round trip with 6 MB file and matching ETag, server rejects a wrong `Content-MD5`, duplicate and conflict |

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

See `config.example.toml` (local runs) and `docker/config.docker.toml` (containers). Environment variables
override the file: `S3_ACCESS_KEY`, `S3_SECRET_KEY`, `S3_ENDPOINT_URL`, `DELETE_AFTER_UPLOAD`.
The MinIO login in `docker-compose.yml` is the default demo login. Never use it for anything real.

## How it works

1. **Scan** every `scan_interval` seconds. Finished files are added to the database as `pending`.
2. **Upload** one pending file at a time: hash it (SHA-256 and MD5 in one pass), ask the bucket whether
   the key exists, upload if not, then read back size and ETag and compare.
3. **Finish**: mark `uploaded`, then optionally delete the local file.
4. **On any error**: record it, schedule the next try, keep the local file.

The order matters: the state is written to disk before each step that can fail, and a file is
deleted last. If the process dies at any point, the worst case is that one file is checked again after the restart.

## Layout

```
run.sh                one command for start / demo / test / status / logs / stop / clean
Dockerfile            image for the uploader and recorder; second stage adds the tests
docker-compose.yml    MinIO, bucket setup, uploader, recorder, status tool, tests
docker/               config used inside the containers
segment_uploader/     config.py  store.py (SQLite)  storage.py (S3)  service.py (logic)  __main__.py (CLI)
tests/                automated tests and helpers
tools/                fake_recorder.py
```

## Known limitations

See `ENGINEERING_NOTE.md`.
