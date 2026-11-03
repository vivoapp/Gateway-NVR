# Milestone 2 results: M6, M7, M8, M9

Status: **built and tested in the workstation lab (Oct 29, 2026); the gateway runs are pending.**
The lab is the same stack with ten fake cameras (tests/lab/README.md); its numbers are not the
gateway's. Each "Result" below is filled in from the reference gateway's reports.

## M6 Multi-camera performance baseline

| Done when (scope) | Result | Evidence |
|---|---|---|
| Vivo can rerun the script and get results within a reasonable margin | pending: 3 rounds of 4, 8 and 10 cameras, spread between rounds in the report | m6-benchmark |
| The 10-camera run stays under the CPU and temperature limits agreed before start (CPU average under 60 %, peak under 85 %, under 80 °C) | pending | m6-benchmark |

Lab: the script ran a 2- and 4-camera round, wrote the report and the raw samples, logged its
planned windows and started the normal stack again.

## M7 Continuous recording, main stream and substream

| Done when (scope) | Result | Evidence |
|---|---|---|
| A 24-hour run at 10 cameras records both streams for every camera | pending | m7-check (`--hours 24`) |
| Any minute picked at random plays back from the main stream, and the matching substream segment exists with the correct name | pending | m7-check (`--samples 20`) |
| Extra CPU and disk cost of both streams at 10 cameras | pending | m6-benchmark (substream container share, disk write rate) |
| Footage retrieved by camera and time range through Frigate's own API | pending | m7-check (clip.mp4 per random minute) |

Lab: 41 of 41 checks, 10 of 10 random minutes on both streams; a graceful restart finishes the
current segment, a killed ffmpeg leaves no `.part` behind and restarts within 5 s. The lab found
the timestamp jump on a go2rtc reconnect (docs/findings.md F11), now handled.

## M8 Storage layout and disk management

| Done when (scope) | Result | Evidence |
|---|---|---|
| The layout matches the document on a fresh install | pending | docs/m8-storage.md, `setup.sh --only disk,stack` |
| When recordings reach the safe limit, the OS, Docker and Frigate keep running | pending | m9-pressure, then m1-check and m2-check |
| Disk use and SSD health can be read with one documented command | pending | `sudo vivo-disk` |

Lab: `vivo-disk` reports use per folder, the safe limit, days stored and the estimate; SMART needs
the gateway (no smartctl in the lab).

## M9 Retention and overwrite policies

| Done when (scope) | Result | Evidence |
|---|---|---|
| A disk-pressure test shows the oldest footage deleted first while new recording continues with no gap | pending | m9-pressure |
| The report states the actual number of days stored at 10 cameras | pending | `sudo vivo-disk` |

Lab (6 GB pretend disk, six seeded old hours): the filler took use from 45.4 % to 91.1 %; the guard
deleted the two oldest hours first, in order, down to 83.8 % (target 85 %), kept the newest hour,
and recording carried on with no gap (main stream 0 s, substream 1 s at most, names being to the
second); Frigate no longer listed the deleted hours. 7 of 7 checks.

Agreed before start: local substream retention 3 days (proposal, Haris to confirm).
