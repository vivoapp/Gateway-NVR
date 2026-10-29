# Change log

## Unreleased (M19 and M20 work, built on the technical evaluation uploader)

### Step 7: M19 acceptance tools
- `tools/m19_acceptance.py` (`snapshot`, `verify`, `report`): checks a run without trusting the uploader's state
  file. It re-reads the recorder's files and asks the storage for every object, then checks: every finished
  segment uploaded; size, MD5/ETag and SHA-256 equal to the local file (`--deep` downloads and hashes every
  object); the bucket holds exactly the uploaded segments; no segment logged as uploaded twice; every
  uploaded segment has a correct index record; local files unchanged since `snapshot`. `report` adds the
  measured upload rate and CPU as markdown. 12 tests prove it passes on a good run and fails on each kind of damage.
- The uploader logs `event=stats` every `stats_interval` seconds (60): uploads, bytes, Mbps, CPU % of one core,
  memory, pending count.
- The checker works on a live system: segments from the last `--grace` seconds (30) count as in flight and
  are reported, not hidden. `--strict` (no grace) is for the final run after the cameras stop and the queue is
  empty. (A first live run on a PC showed 10 objects "not in state file": the recorder and uploader had moved
  on between the moments the checker read the files, the state file, the bucket and the index.)
- `deploy/test.sh`: `snapshot`, `accept`, `report`, and `second` / `second-check` (a second MinIO and a second
  uploader that differs only in `S3_ENDPOINT_URL`).
- `tools/fake_recorder.py`: `--name-format`, fractional `--size-mb`, `--start-hours-ago` + `--time-step` to
  produce a day of segments in minutes.

### Step 6: gateway Docker and Compose (`deploy/`)
- `deploy/compose.yaml`: user 10001, `/srv/vivo/recordings/substream` read-only, read-only container, all
  Linux rights dropped, 1 CPU / 512 MB / 200 processes, logs 20 MB x 5, secrets from `/etc/vivo/cloud.env`,
  one-time `init-data` job that gives the state folder to the container user.
- `deploy/config.gateway.toml`; site settings now also come from the environment (`S3_BUCKET`,
  `S3_REGION`, `S3_KEY_PREFIX`, as well as the endpoint and keys), so every gateway uses the same file.
- `deploy/compose.test.yaml` + `deploy/test.sh`: the same setup on a PC with MinIO and 10 simulated cameras
  (`VIVO-CAM-01` to `10`), generated test passwords (none in Git), and `./test.sh check` which proves the
  hardening settings and compares the counts.
- Base image pinned by digest, Python packages pinned, container user is now 10001 everywhere.
- The evaluation demo files are marked local-only.

### Steps 3 to 5: no local deletion, error types, speed limit, workers, index record
- The uploader never deletes local files. `delete_after_upload` and `DELETE_AFTER_UPLOAD` are gone
  (the old setting in a config file is an error). The state column `local_deleted` stays, unused,
  so older state files still open.
- Errors are sorted by kind. `outage` (unreachable, 5xx), `auth` (wrong or expired credentials,
  missing bucket) and `full` (quota, storage cap, 507) pause the whole service; `auth` and `full`
  pause for `backoff_max` and log an ERROR (`storage_refused_credentials`, `storage_full`).
  Anything else (`file`) delays only that file.
- A remote delete (only used to remove a copy that failed the read-back check) is refused for any
  key outside `key_prefix`.
- `upload_limit_mbps` limits the upload speed for all workers together (0 = no limit).
- `workers` (default 2): different cameras upload at the same time, one camera's segments always
  go oldest first. With `workers = 1` everything is strictly oldest first. An outage stops all
  workers.
- Index record: one JSON note per uploaded segment (`camera_id`, `start_utc`, `end_utc`, `bucket`,
  `key`, `size`, `sha256`, `etag`, `uploaded_at`) goes to a replaceable sink (`index.py`). The default
  sink appends to `index/index-<date>.jsonl`. A failing sink never blocks uploads; records are
  sent again later (at least once, so readers ignore repeated keys). How the records reach Vivo's
  backend is not decided yet.
- The Docker demo's fake recorder deletes its own old files after 15 minutes (stands in for M9).
- Tests: 80 passed, 3 need Docker (error kinds, speed limit, parallel workers, index record).

### Step 2: agreed segment format and cheaper scan
- Only finished segments are uploaded: `<camera_id>/<YYYY-MM-DD>/<camera_id>_<YYYYMMDDTHHMMSS>Z.mp4`.
  The recorder renames `.mp4.part` to `.mp4` when a segment is complete, so the "size and age are
  stable" check is gone. `.part` and every other file type are ignored. An `.mp4` whose name is
  not in the agreed format is ignored and reported once (`event=ignored_name`).
- Camera and UTC start/end time are read from the file name and stored per segment. End time =
  start + `nominal_duration` (60 s) until the name carries an end time; a second timestamp
  (`..._<start>Z_<end>Z.mp4`) is used automatically when present.
- Segments upload oldest first by segment start time (not file time, not listing order).
- Scans: a full scan at start-up and every `full_scan_interval` (1 h); in between, only date
  folders of the last `recent_days` (2) UTC days, and a folder is listed again only when its
  modification time changed. With 57,600 files on disk (10 cameras, 4 days): quiet scan 0 ms,
  active scan 16 ms, first full scan 5 s (was 27 s with one commit per file).
- A scan is one database transaction. If it fails half way it is rolled back and forgotten.
- Settings removed: `patterns`, `ignore_suffixes`, `settle_seconds`. Unknown settings in the config
  file are now an error instead of being ignored.
- State files from the evaluation version are upgraded automatically (new columns).
- `tools/fake_recorder.py` writes the agreed layout and names.

### Step 1
- Imported the evaluation uploader as the baseline (25 tests passed, 3 MinIO tests need Docker).

### Still to do (see the plan)
- Docker and Compose for the gateway (user 10001, read-only mounts, pinned versions)
- M19 acceptance run, M20 gap report and the 18-test script, retention number
- README and test report update (the README still describes the evaluation demo)
