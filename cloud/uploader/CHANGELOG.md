# Change log

## Unreleased (M19 and M20 work, built on the technical evaluation uploader)

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
- Remove local deletion (`delete_after_upload`), ownership guard for remote deletes, error types
- Speed limit, worker pool, index record
- Docker and Compose for the gateway (user 10001, read-only mounts, pinned versions)
- M19 acceptance run, M20 gap report and the 18-test script, retention number
- README and test report update (the README still describes the evaluation demo)
