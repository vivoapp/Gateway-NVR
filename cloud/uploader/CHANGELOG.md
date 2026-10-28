# Change log

## Unreleased (M19 and M20 work, built on the technical evaluation uploader)

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
