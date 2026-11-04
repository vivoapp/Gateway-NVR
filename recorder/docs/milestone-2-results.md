# Milestone 2 results: M6, M7, M8, M9

Status: **done on the reference gateway, Nov 2 to 4, 2026. Every "Done when" item passes; the one
finding to follow up is the short CPU peak at the top of each hour (M6).**
The lab (Oct 29) is the same stack with ten fake cameras (tests/lab/README.md); its numbers are not
the gateway's. Each "Result" below comes from the reference gateway's reports in
`/opt/vivo-gateway/reports`.

## M6 Multi-camera performance baseline

| Done when (scope) | Result | Evidence |
|---|---|---|
| Vivo can rerun the script and get results within a reasonable margin | Done: 3 rounds of 4, 8 and 10 cameras (Nov 3, 02:00 to 03:59 UTC). At 10 cameras the CPU average was 37.4, 44.1 and 44.4 %, RAM 3440 to 3543 MB. Rounds 2 and 3 agree within 0.3 points; round 1 was lower because the cameras sent less data then (4.1 against 5.8 Mbit/s), so CPU follows the cameras' bitrate | m6-benchmark-20261103-035937 |
| The 10-camera run stays under the CPU and temperature limits agreed before start (CPU average under 60 %, peak under 85 %, under 80 °C) | Pass in the benchmark rounds: CPU average 44.4 % at most, peak 48.0 %, CPU temperature 49 °C at most. About 4 % CPU per camera, so about 15 cameras would reach the 60 % average. Once an hour the CPU is at 100 % for about 10 s (see below) | m6-benchmark-20261103-035937 |

Top of the hour: on each hour Frigate writes one short preview video per camera for its timeline,
and the CPU is busy while it does. No 10-camera benchmark round crossed an hour, so the CPU was
sampled every second from 04:59:40 to 05:01:10 UTC on Nov 3 with all 10 cameras: it was 85 % or
more for 10 s (05:00:01 to 05:00:11, 100 % at most), the same seconds the ten preview files were
written, and about 42 % before and after. The highest 1-minute average was 52.9 %. Neither stream
had a gap from 04:58 to 05:03. The same happened in round 2 with 8 cameras (two 5-second samples at
95 and 100 % at 03:00:06 and 03:00:11). The peak limit holds outside these seconds; spreading or
lightening this hourly work is noted for Milestone 3.

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
