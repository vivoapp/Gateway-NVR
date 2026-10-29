# Test report: M19 archive upload queue and M20 interrupted upload and recovery

Status on 29 October 2026. Everything below was run on a PC (Docker Desktop, MinIO as the storage, 10 simulated
cameras). **Not yet run: the 24-hour acceptance run on the real gateway with Backblaze B2.** That run needs the B2
bucket and the filled `/etc/vivo/cloud.env`; see "Still to do" at the end.

## 1. What each acceptance item is based on

| Acceptance item (scope document) | Evidence | State |
|---|---|---|
| M19: every segment exactly once, checksums match (24 h, 10 cameras) | `tools/m19_acceptance.py` run on a PC (section 3); 18 recovery tests (section 4) | PC: done. **Gateway 24 h run: to do** |
| M19: every segment has an index record | Same checker, "every uploaded segment has a correct index record" | PC: done |
| M19: second S3 store by configuration only | `./test.sh second` / `second-check` (section 3) | Done with a second MinIO. Real B2 as the second store: to do |
| M19: never deletes local files | Recordings are mounted read-only in the container; the checker proves every local file unchanged; no delete setting exists | Done |
| M19: configurable speed limit | `upload_limit_mbps`; test `test_speed_workers.py` (limit applied once, rate measured) | Done |
| M19: measured upload rate and CPU at 10 cameras | Section 3 (PC numbers). The real numbers come from the gateway run | PC: done. **Gateway: to do** |
| M20: checksum check after each upload | Every upload is read back (size, ETag = MD5, SHA-256 in metadata); `test_integrity.py`, `test_minio_integration.py` | Done |
| M20: gap report | `python -m segment_uploader gaps`; `test_gaps.py`; "gap report" check after each of the 18 tests | Done |
| M20: scripted tests: 5 cuts, 5 reboots, 5 uploader kills, 3 storage errors | `tools/m20_recovery.py`; section 4: **18 of 18 passed** | Done (on a PC) |
| M20: measured catch-up time | Section 4 (measured) and the table of calculated times for the agreed upload speed | Measured on a PC; **real value needs Vivo's upload speed** |
| M20: retention-days recommendation for M9 | Section 5 | Done for an outage of 2 days; **needs Vivo's agreed outage length** |

## 2. Automated tests

`./run.sh test` (Docker, with the real MinIO): last run on the PC on 29 October **130 passed, none skipped**.
The final package has 131 tests (one more: the fake recorder's new file name); run `./run.sh test` once more
after replacing the folder. Without Docker 128 pass and 3 (the real-MinIO ones) are skipped.
The test files and what each covers are listed in `README.md`.

## 3. M19 acceptance run on the PC

Setup: `deploy/test.sh`, the gateway container settings (user 10001, recordings read-only, read-only
container, all Linux rights dropped, 1 CPU, 512 MB, log rotation), 10 cameras named `VIVO-CAM-01..10`, a new
segment per camera every 5 s (fast mode), MinIO as storage.

| Check | Result |
|---|---|
| Hardening (`./test.sh check`) | 8 of 8 ok |
| While running (`./test.sh accept`) | PASS: 190 of 200 uploaded, the 10 newest (last 30 s) reported as in flight; checksums, exactly-once, index and unchanged-files checks all ok |
| After stopping the cameras (`./test.sh accept --strict`) | **PASS: 220 of 220 uploaded, 220 objects in the bucket, none stuck, none uploaded twice, 220 index records, 170 local files unchanged** |
| Second store (`./test.sh second-check`) | PASS: 250 of 250 uploaded to the second MinIO with the same image and config file; only `S3_ENDPOINT_URL` differs |
| Measured (report of that run) | 250 segments, 262 MB; upload rate average 4.9 Mbps (busiest minute 16.8 Mbps); uploader CPU average 1.3 %, maximum 4.5 % of one core; memory maximum 57 MB; 2 workers, no speed limit |

These rates are from a simulated recorder running about 12 times faster than the real one (a segment every 5 s
instead of every 60 s) against a local MinIO. They show the uploader has plenty of room; they are **not** the
numbers for the acceptance report. The real recorder produces about 1.33 Mbps for 10 cameras.

## 4. M20 recovery tests (18 of 18 passed)

Run on the PC on 29 October with `./test.sh recovery` (about 45 minutes, MinIO). The real uploader and a
10-camera fake recorder ran as processes of the test script, a test proxy sat in front of the storage, and
the script killed processes with SIGKILL. After every test the script checked from the outside, without
trusting the uploader's own state:

1. everything that was waiting got uploaded without anyone touching it
2. every local segment uploaded; size, ETag and SHA-256 equal; the bucket holds exactly the uploaded segments;
   none stuck; none uploaded twice; every index record present and correct; local files unchanged (strict mode)
3. the gap report shows no lost segment

| # | Test | Waiting at end of fault | Catch-up | Result |
|---|---|---|---|---|
| C1 | Internet cut for 15 s | 28 segments | 1 s | PASS |
| C2 | Internet cut for 1 minute | 120 | 15 s | PASS |
| C3 | Five short cuts of 3 s, uploads cut in the middle | 48 | 3 s | PASS |
| C4 | Internet cut for 5 minutes | 600 | 72 s | PASS |
| C5 | Outage of the agreed length: 2 days of video (compressed) | 28,760 | 1,660 s | PASS |
| R1 | Power cut during an upload, 10 s down | 10 | 3 s | PASS |
| R2 | Reboot, 30 s down | 0 | 0 s | PASS |
| R3 | Two reboots within a minute | 0 | 0 s | PASS |
| R4 | Reboot while the internet is also down | 50 | 6 s | PASS |
| R5 | Reboot, 2 minutes down | 0 | 0 s | PASS |
| K1 | Uploader killed in the middle of an upload | 10 | 2 s | PASS |
| K2 | Killed after the storage stored the file, before the uploader noted it | 10 | 3 s | PASS |
| K3 | Killed with a long queue | 60 | 4 s | PASS |
| K4 | Killed three times in a row | 0 | 0 s | PASS |
| K5 | Killed and left off for 90 s | 180 | 11 s | PASS |
| S1 | Storage refuses the credentials (403) for 1 minute | 120 | 16 s | PASS |
| S2 | Storage is full (507) for 1 minute | 120 | 18 s | PASS |
| S3 | Storage server errors (500), then every second request failing | 160 | 12 s | PASS |

What the tests showed beyond "nothing was lost":

- **Cuts and errors:** the uploader noticed every outage and waited with growing delays instead of hammering the
  storage (for example 34 attempts in a 300 s cut, 6 in a 60 s credentials failure). Refused credentials and a full
  storage were logged as ERROR (`storage_refused_credentials`, `storage_full`) and paused the whole service.
- **Kills and reboots:** after a kill in the middle of an upload the restart recovered the interrupted upload
  (`recovered_interrupted`). After a kill right after the storage stored the file, the restart found the object
  already there (`already_present`) and did not upload it again.
- **C5:** 28,760 segments written during the outage were uploaded oldest first, camera by camera (0 out of
  order), in 1,660 s after the storage came back (19.5 segments per second; the first upload came 185 s after the
  storage was back, which is the uploader's wait before its next try, at most `backoff_max` = 300 s).
  C5 is compressed: the whole outage's files are written at once and the storage stays down for 10 minutes of
  real time, not 2 days.
- The harness itself was checked with deliberate damage: skipping the "is it already there?" check makes K2
  fail; not recovering interrupted uploads makes K1 fail.

### Bugs found by these tests and fixed

| Found by | Problem | Fix |
|---|---|---|
| Building the proxy | The storage client still made one hidden retry, against the design "retries are done by the service" | `total_max_attempts` 1 |
| Full test run | An older test assumed strict oldest-first across cameras with 2 workers | Test uses `workers = 1` |
| Long C5 run | The test script wrote a file over an earlier test's file (the uploader refused to overwrite and flagged 14 `conflict`) | Script never rewrites an existing segment |
| PC run | C1 failed once in `--quick` mode: the cut fell between two recorder rounds | Cuts last at least 1.5 rounds; C3 cuts while an upload is in flight |

## 5. Catch-up time and retention (for M9)

Calculation for 10 cameras at about 1 MB per minute (1.33 Mbps, 14.4 GB per day), `tools/m20_retention.py`.
Catch-up time = pile size / (upload speed - live rate), plus up to 5 minutes before the first retry.

| Upload speed | Catch-up after a 2-day outage (28.8 GB) | After a 3-day outage (43.2 GB) |
|---|---|---|
| 5 Mbps | 17.5 h | 26 h |
| 10 Mbps | 7.5 h | 11 h |
| 20 Mbps | 3.5 h | 5.2 h |
| 50 Mbps | 1.4 h | 2.1 h |
| 100 Mbps | 44 min | 1.1 h |
| 1.33 Mbps or less | never catches up | never catches up |

**Recommendation for M9: keep local substream segments for at least outage days + 1 day, that is 3 days for a
2-day outage (about 43 GB) and 4 days for a 3-day outage (about 58 GB).** The uploader sends the oldest
segment first, so the catch-up time does not make the oldest file older; what has to hold is
`retention >= outage + margin`. The 1-day margin is a choice (outage noticed late, gateway or uploader down at
the same time), not a measurement. The recorder team's current setting of 3 days fits a 2-day outage.

## 6. Still to do

- **24-hour acceptance run on the gateway** with 10 real cameras and Backblaze B2: checks, rate and CPU for the report.
  Needs: B2 endpoint, bucket and site prefix from Simon; `/etc/vivo/cloud.env` filled and readable by the `haris` user;
  recorder files readable by "other" (rootless Docker).
- **Vivo's agreed outage length and upload speed** (this report assumes 2 days; the table shows other speeds). With
  them the catch-up time and the retention days are fixed.
- **Index delivery:** how the index records reach Vivo's backend is not decided; the default file sink writes
  `index-<date>.jsonl` and a different sink can be added in `index.py`.
- Optional: an exact `duration_s` in the index record (read from the MP4 header), if Vivo wants it.
