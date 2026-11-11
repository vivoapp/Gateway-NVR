# Milestone 2 results: M6, M7, M8, M9

Status: **done on the reference gateway, Nov 2 to 4, 2026. Every "Done when" item passes; the one
finding to follow up is the short CPU peak at the top of each hour (M6). A 15-camera run was added
on Nov 9 (M6). Vivo checked the results independently on Nov 9 and 10 (see the end of this page).**
The lab (Oct 29) is the same stack with ten fake cameras (tests/lab/README.md); its numbers are not
the gateway's. Each "Result" below comes from the reference gateway's reports in
`/opt/vivo-gateway/reports`; copies of them are in
[test-reports/milestone-2](test-reports/milestone-2/README.md).

## M6 Multi-camera performance baseline

| Done when (scope) | Result | Evidence |
|---|---|---|
| Vivo can rerun the script and get results within a reasonable margin | Done: 3 rounds of 4, 8 and 10 cameras (Nov 3, 02:00 to 03:59 UTC). At 10 cameras the CPU average was 37.4, 44.1 and 44.4 %, RAM 3440 to 3543 MB. Rounds 2 and 3 agree within 0.3 points; round 1 was lower because the cameras sent less data then (4.1 against 5.8 Mbit/s), so CPU follows the cameras' bitrate | m6-benchmark-20261103-035937 |
| The 10-camera run stays under the CPU and temperature limits agreed before start (CPU average under 60 %, peak under 85 %, under 80 °C) | Pass in the benchmark rounds: CPU average 44.4 % at most, peak 48.0 %, CPU temperature 49 °C at most. About 4 % CPU per camera, so about 15 cameras would reach the 60 % average (measured on Nov 9, see "15 cameras" below). Once an hour the CPU is at 100 % for about 10 s (see below) | m6-benchmark-20261103-035937 |

Top of the hour: on each hour Frigate writes one short preview video per camera for its timeline,
and the CPU is busy while it does. No 10-camera benchmark round crossed an hour, so the CPU was
sampled every second from 04:59:40 to 05:01:10 UTC on Nov 3 with all 10 cameras: it was 85 % or
more for 10 s (05:00:01 to 05:00:11, 100 % at most), the same seconds the ten preview files were
written, and about 42 % before and after. The highest 1-minute average was 52.9 %. Neither stream
had a gap from 04:58 to 05:03. The same happened in round 2 with 8 cameras (two 5-second samples at
95 and 100 % at 03:00:06 and 03:00:11). The peak limit holds outside these seconds; spreading or
lightening this hourly work is noted for Milestone 3.

### 15 cameras (Nov 9)

Simon asked whether the gateway can record 15 cameras. The site has 10 cameras, so the run added 5
copies: copy11 to copy15 take the main stream and substream of cam01 to cam05 from go2rtc's own
restream. go2rtc, Frigate and the substream archiver do the same work for a copy as for a camera;
only the network input stays at the 10 real cameras (about 0.5 Mbit/s each). The script ran 3
rounds of 10 and 15 cameras (`sudo tests/m6_benchmark.py --counts 10,15 --copies`, Nov 9, 02:37 to
03:59 UTC), checked both streams of every camera after each run, then started the normal 10-camera
stack again and deleted the copies' substream folders. Frigate deletes the copies' main-stream
files (about 537 MB) with its normal 7-day cleanup.

| Measured | 10 cameras | 15 cameras |
|---|---|---|
| CPU average, rounds 1 to 3 | 39.0, 38.6, 39.6 % | 57.2, 54.1, 63.6 % |
| CPU peak, outside the top of the hour | 49.8 % | 67.3 % |
| CPU at the top of the hour | no round crossed an hour (85 to 100 % for about 10 s on Nov 3, see above) | 100 % for about 15 s (03:00:05 to 03:00:15), highest 1-minute average 68.0 % |
| RAM average | 3627 to 3717 MB | 4354 to 4463 MB (of 12 GB) |
| CPU temperature peak | 53 °C | 60 °C |
| Recording, both streams, each run's sampled minutes | 10 of 10 cameras, 100 % | 15 of 15 cameras, 99.37 % or more, no gap |

Result: recording at 15 cameras passes. The CPU does not stay under the agreed limits at 15
cameras: one of the three rounds averaged 63.6 % (limit 60 %), and the hourly preview work takes it
to 100 % for about 15 s (limit 85 %). Each camera adds about 3.8 % CPU. So 15 cameras is the most
this gateway records with the current settings, with no room left for the cloud uploader or live
view (neither ran during the test). CPU follows what the cameras send: the 10-camera averages were
lower than on Nov 3 (about 39 against 44 %), and round 3, with the most camera data (5.7 against
5.4 Mbit/s), had the highest CPU.
Evidence: m6-benchmark-20261109-035857 (.md and the raw samples in .csv).

Where each camera's CPU goes, from Frigate's own stats (`/api/stats`) and `top` at 10 cameras,
Nov 9 13:30 UTC, in percent of one core: Frigate decoding the substream and checking it for motion
at 5 frames a second 6.3 (decoder 3.5, frame capture 0.8, motion check 2.0), Frigate writing the
main stream to disk 2.6, go2rtc 1.6, the substream archiver about 0.5, and the camera's share of
Frigate's main processes and previews about 3. That is about 14 % of one core, or 3.5 % of the
gateway's 4 cores, per camera. The video decoding itself runs on the iGPU (7 % busy); the CPU cost
is in the steps after it. So almost half of each camera's cost is Frigate's motion check, which
runs even though object detection is off and recording is continuous. Checking 2 instead of 5
frames a second should lower it; this is not tested yet.

Lab: the script ran a 2- and 4-camera round, wrote the report and the raw samples, logged its
planned windows and started the normal stack again.

## M7 Continuous recording, main stream and substream

| Done when (scope) | Result | Evidence |
|---|---|---|
| A 24-hour run at 10 cameras records both streams for every camera | Pass (Nov 3 04:28 to Nov 4 04:28 UTC, 41 of 41 checks): the substream recorded 100.00 % on every camera with no gap, and the main stream had no gap on any camera; the longest space between two main-stream files was 1.03 s (see below). The first check (Nov 1 10:22 to Nov 2 10:22 UTC) had a main-stream gap of 100 to 109 s on every camera from a cable disconnect (see below) | m7-check-20261104-044203, m7-check-20261102-103614 |
| Any minute picked at random plays back from the main stream, and the matching substream segment exists with the correct name | Pass (Nov 4 check): 20 of 20 random minutes, both streams | m7-check-20261104-044203 |
| Extra CPU and disk cost of both streams at 10 cameras | The substream container used 8.2 % of one core (about 2 % of the gateway's 4 cores); go2rtc, which serves both streams from one camera connection each, 20.9 to 22.3 % of one core. On disk the substream adds 7.8 GB a day to the main stream's 30.9 GB | m6-benchmark-20261103-035937 (rounds 2 and 3), `sudo vivo-disk` |
| Footage retrieved by camera and time range through Frigate's own API | Pass (Nov 4 check): each random minute fetched as `clip.mp4` by camera and time range, 20 of 20, 60.6 to 66.4 s of H.265 video | m7-check-20261104-044203 |

Main-stream percentage: the check reports 99.52 to 99.64 % of the 24 hours, counted from the start
and end time Frigate stores for each file. Frigate stores the start to the whole second but not the
length (8.007 s, for example), so next to a space of about 1 s between two files there is an overlap
of about 1 s. The file lengths add up to 99.89 to 99.91 % of the 24 hours, and no space between two
files was longer than 1.03 s on any camera.

Cable disconnect, Nov 2: the gateway's network link was down from 02:16:10 to 02:16:16 UTC when the
server was pulled from the rack. The substream had no gap. The main-stream recording stalled and
Frigate's own watchdog restarted it after about 100 s, so every camera has a main-stream gap of 100
to 109 s. A faster main-stream recovery after a link drop is noted for Milestone 3.

Lab: 41 of 41 checks, 10 of 10 random minutes on both streams; a graceful restart finishes the
current segment, a killed ffmpeg leaves no `.part` behind and restarts within 5 s. The lab found
the timestamp jump on a go2rtc reconnect (docs/findings.md F11), now handled.

## M8 Storage layout and disk management

| Done when (scope) | Result | Evidence |
|---|---|---|
| The layout matches the document on a fresh install | Matches (Nov 3), with one folder the document did not list: Frigate's timeline previews in `frigate/media/clips/previews/` (6.3 GB, about 0.9 GB a day), now added to docs/m8-storage.md | docs/m8-storage.md, `find /srv/vivo -maxdepth 3` |
| When recordings reach the safe limit, the OS, Docker and Frigate keep running | Pass: no container restarted during or after either pressure test (Nov 3 and Nov 4); m1-check 16 of 16 and m2-check 13 of 13 at 04:20 UTC Nov 3. The tests used a lower limit (20.47 % and 20.80 %): the real 90 % would need about 940 GB of filler | m9-pressure-20261104-044555, m9-pressure-20261103-040233, m1-check-20261103-042010, m2-check-20261103-042010 |
| Disk use and SSD health can be read with one documented command | Pass: `sudo vivo-disk` shows 18.9 % used (247.0 GB of 1,306.1 GB), use per folder, the safe limit, days stored, and SMART PASSED on both SSDs | `sudo vivo-disk`, Nov 3 04:19 UTC |

Lab: `vivo-disk` reports use per folder, the safe limit, days stored and the estimate; SMART needs
the gateway (no smartctl in the lab).

## M9 Retention and overwrite policies

| Done when (scope) | Result | Evidence |
|---|---|---|
| A disk-pressure test shows the oldest footage deleted first while new recording continues with no gap | Pass, 8 of 8 checks (Nov 4 04:43 UTC): the two oldest hours (Oct 28 04:00 and 05:00, 1.92 GB) went first, in order, down to the target in 15 s; the newest hour was kept; Frigate no longer lists them; no substream was deleted early; no gap on either stream (0.0 s main, 0 s substream). The first run (Nov 3, 6 of 7) failed only the gap check, because of the test order, not the cleanup (see below) | m9-pressure-20261104-044555, m9-pressure-20261103-040233 |
| The report states the actual number of days stored at 10 cameras | 6.97 days of main stream (kept 7) and 3.0 days of substream (kept 3), 38.7 GB a day. About 30 days would fit under the 90 % limit, so the 7-day setting, not the disk, sets the days stored | `sudo vivo-disk`, Nov 3 04:19 UTC |

Gap check, Nov 3: the benchmark restarted the stack from 03:59:25 to 03:59:36 UTC, 12 s before the
pressure test started, and the gap check looks from 60 s before the cleanup, so it caught that
restart (main stream 24 to 26 s, substream 13 to 15 s, every camera, all ending by 03:59:47). The
cleanup itself (03:59:46 to 03:59:59) and the minutes after it, up to 04:02, had no gap on any
camera. The test now first waits until `frigate` and `substream` have run for 3 minutes
(`--warmup`, default 180 s); the Nov 4 rerun, on a stack that had run for 25 hours, passed.

Lab (6 GB pretend disk, six seeded old hours): the filler took use from 45.4 % to 91.1 %; the guard
deleted the two oldest hours first, in order, down to 83.8 % (target 85 %), kept the newest hour,
and recording carried on with no gap (main stream 0 s, substream 1 s at most, names being to the
second); Frigate no longer listed the deleted hours. 7 of 7 checks.

Early deletion (offered to Haris on Oct 29, unit tested, on the gateway since Nov 3 04:19 UTC):
substream deleted before its 3 days ended is listed in `early-deletions.jsonl` (docs/m9-retention.md);
the matching health alert comes with the health endpoint in Milestone 3. The pressure test checks
the list. On a normal
disk the oldest hours are 7 days old and have no substream left, so the pressure test reports none
deleted early; the unit tests cover the listing.

Agreed before start: local substream retention 3 days (proposal, Haris to confirm).

## Vivo's independent verification (Nov 9 and 10)

Jorge (Vivo) ran the acceptance checks on the gateway himself, from a computer on the site
network, with the commands in "Acceptance checks" in recorder/README.md.

| Check | Result | Evidence |
|---|---|---|
| 24-hour recording, both streams, 10 cameras (Nov 9 13:13 to Nov 10 13:13 UTC) | Pass, 41 of 41: the substream recorded 100.00 % on every camera, the main stream 99.55 to 99.73 % (see "Main-stream percentage" under M7), no gap on either stream; 20 of 20 random minutes play from the main stream and have their substream segments | m7-check-20261110-132657 |
| 1-hour recording check (Nov 9 13:55 to 14:55 UTC) | Pass, 41 of 41, 10 of 10 random minutes | m7-check-20261109-145823 |
| Disk-pressure test (Nov 9 14:46 UTC) | Pass, 8 of 8: the two oldest hours (Nov 2 14:00 and 15:00, 2.89 GB) went first, in order, with no gap on either stream | m9-pressure-20261109-144909 |
| Storage, `sudo vivo-disk` (Nov 9) | About 27.6 days fit under the 90 % limit. The estimate follows the footage actually stored: 42.8 GB a day on Nov 9 against 38.7 GB on Nov 3, as the cameras sent about 10 % more data that week | `sudo vivo-disk` |

The 24-hour window starts after the 15-camera run and includes the disk-pressure test and the
1-hour check. Vivo's conclusion (Nov 10): every check passed, no acceptance-blocking defect, and
Vivo is comfortable closing Milestone 2.
