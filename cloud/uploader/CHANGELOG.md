# Change log

## Unreleased (M19 and M20 work, built on the technical evaluation uploader)

### Step 9: documentation and final touches
- `README.md` rewritten: what it does, how it works (with a diagram), the segment file name (start and end), how to
  run it on the gateway and on a PC, every setting, guarantees, error kinds, the index record, the log lines,
  tests, folder layout, open items.
- `TEST_REPORT.md`: the M19 PC acceptance run, the 18 M20 recovery tests (18 of 18 passed on the PC), the bugs the tests
  found, the catch-up times and the retention recommendation (3 days for a 2-day outage), and what is still to do.
- Commented configs: every setting explained in `deploy/config.gateway.toml`, `config.example.toml` and
  `deploy/cloud.env.example`.
- File names: the recorder (M7) now writes `<camera_id>_<start>Z_<end>Z.mp4` (agreed with the recorder developer).
  The uploader already read both forms. `tools/fake_recorder.py --end-in-name` writes the new name (the `.part`
  file keeps the short name, as the real recorder does); the 10-camera test setup and the M20 recovery tests use it.
- The M20 report no longer prints an empty line for tests without extra expectations.

### Step 8: M20 gap report, recovery tests, catch-up and retention
- `python -m segment_uploader gaps --config ...` (`--json` for the full list; `./test.sh gaps` in the test
  setup): the gap report. A gap is a segment that was never confirmed uploaded AND whose local file no longer
  exists; neighbouring segments of one camera are merged into stretches. Uploaded segments are never gaps
  (even if the recorder removed the file later); segments still waiting with their file present are counted
  as "waiting", not as lost. Exit code 1 if there is any gap.
- `tools/fault_proxy.py`: a small proxy between the uploader and the storage for the tests. It passes requests on
  unchanged and can cut the line (also in the middle of an upload), refuse the credentials (403), answer
  "full" (507), fail with server errors (500 / every second request), and hold an upload before it is sent on
  or hold the answer after the storage has stored it (so the uploader can be killed at an exact moment).
- `tools/m20_recovery.py`: the 18 scripted tests (5 internet cuts, 5 gateway reboots, 5 uploader kills,
  3 storage errors). The real uploader and a 10-camera fake recorder run as processes of the script, so they
  can be killed with SIGKILL. After every test: the M19 checks in strict mode, the gap report, the measured
  catch-up time, and test-specific expectations (for example: after a kill in the middle of an upload the
  restart recovered the interrupted upload; after a kill after the storage stored the file the restart found it
  `already_present` and did not upload it again; wrong credentials and a full storage are logged loudly and
  do not make the uploader hammer the storage). C5 is an outage of the agreed length (`--outage-days`,
  default 2), compressed: the segments of the whole outage are written at once and the storage stays down for
  `--outage-minutes` of real time. Report: `m20-recovery-report.md`.
- `tools/m20_retention.py`: catch-up time and the retention recommendation for M9. Catch-up time = pile / (upload
  speed - live rate). The uploader sends the oldest segment first, so the catch-up time does not make the oldest
  file older: retention must be at least outage days + a margin (default 1 day).
- `./test.sh recovery [--quick]`, `recovery-report`, `gaps`; a `recovery` service in `compose.test.yaml`.
- Fixed: the storage client still made one hidden retry (`max_attempts` 1 allows one retry; it is now
  `total_max_attempts` 1), which contradicted "retries are done by the service, persisted, with backoff" and
  added a second connect timeout to every failed request during an outage.
- Fixed: `test_upload_order_follows_segment_start_time...` was flaky (it asserted strict oldest-first across
  cameras with 2 workers, which run cameras in parallel). It now sets `workers = 1`, the setting for which the
  guarantee holds.
- Found by a full-length run on the test machine (not an uploader bug): the C5 pile wrote a segment over the file
  of an earlier test that had the same name (same camera, same second). The uploader behaved correctly: it saw a
  changed file, found a different object in the bucket, did not overwrite it, and marked 14 segments `conflict`,
  and the M19 checks reported it. The pile now never rewrites an existing segment.
- Found on the PC run: C1 failed once in `--quick` mode ("0 failed attempts logged as outage"): the cut (3.75 s)
  fell between two recorder rounds, so the uploader made no request during it. Cuts are now at least 1.5
  recorder rounds long, and the C3 flaps wait for an upload to be in flight before cutting the line. The proxy
  no longer prints a traceback when a killed uploader resets its connection.
- The harness was checked against deliberate damage: skipping the "is it already there?" check makes K2 fail,
  and not recovering interrupted uploads makes K1 fail.

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
- New check "no segment is stuck (pending, conflict or missing)", and the bucket check now says which state an
  unexpected object has in the state file. (On a PC, restarting the containers started a second simulator in
  the same second as the first; it wrote the same 10 file names again with different content. The uploader
  correctly refused to overwrite the stored objects and marked the 10 segments `conflict`; the checker had
  called them "not in state file".) The simulator no longer rewrites an existing segment.
- Found on the PC run: a segment that was already uploaded and verified could end as `missing` ("local file
  vanished before upload"): its file had been rewritten on disk (the doubled simulator), so it was queued for
  a re-check, and the recorder's retention removed it before the re-check ran. Now such a segment goes back
  to `uploaded`, because the verified copy is in the bucket and nothing was lost. A segment that was never
  uploaded and vanishes is still `missing` (it is the gap report's input).
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
