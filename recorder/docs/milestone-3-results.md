# Milestone 3 results: M10, M11, M12, M22

Status: **done on the reference gateway, Nov 3 to 6, 2026. Every "Done when" item passes. The power-loss
item passes after a fix made during this milestone, checked with 8 immediate reboots and 3 real power
cuts (see "Power loss" below). Open points are listed at the end.**
Each "Result" comes from the reference gateway's reports in `/opt/vivo-gateway/reports`. Times are UTC.

## M10 Recording recovery after restart

| Done when (scope) | Result | Evidence |
|---|---|---|
| Recording resumes on its own after every event (10 reboots, 10 Frigate kills, 5 camera disconnects), within the limit agreed before start | Pass. 10 Frigate kills (Nov 6 02:00 to 02:14): main stream back within 12 s every time (limit 60 s), substream not interrupted. 10 reboots (02:30 to 03:04): SSH back 45 to 49 s after each reboot, main stream within 54 s and substream within 50 s (limit 180 s). 5 camera disconnects (13:03 to 13:11, cam01 to cam05, cable pulled on site for 28 to 63 s): main stream back within 16 s of the cable going back in, substream within 36 s (limit 120 s); only the unplugged camera dropped | m10-recovery-20261106-021603 (kills), m10-recovery-20261106-030725 (reboots), m10-recovery-20261106-131212 (disconnects) |
| No gap is left unexplained in the log | Pass. Kills: 100 gaps, 0 unexplained. Reboots: 200 gaps, 0 unexplained. Disconnects: 10 gaps, 0 unexplained | same reports (gap log) |

The disconnect times come from a ping monitor on the gateway (the second the camera dropped) and from
Jorge's "on" message or the camera answering again, whichever came first (events
m10-events-disconnect-20261106-130323, ping log m10-disconnect-ping.log).

### Short network drop

A drop shorter than 10 s recovers more slowly than a cable pull. Checked on Nov 6, 16:10 to 16:19, by
blocking VIVO-CAM-02 in the firewall for 6 s, 3 times (`tests/m10_recovery.py disconnect --block`):
the main stream came back after 108 s, 52 s and 102 s (limit 120 s, so a pass), the substream at once,
losing only the 6 s. Cause: Frigate's recorder gives up on a silent connection after 10 s, so after a
shorter drop it keeps a connection that no longer delivers segments, until Frigate's own watchdog
restarts it at about 2 minutes (not configurable, docs/findings.md F2 and F12). This is what happened
on Nov 2, when a 6 s drop of the gateway's network link left a 111 s gap. It is listed under "Open
points" with the planned fix (m10-recovery-20261106-161909).

## M11 Power-loss and disk fault testing

| Done when (scope) | Result | Evidence |
|---|---|---|
| Power loss: boots, recording resumes, at most the last partial segment lost | Pass after a fix (details below). Boots and records again: Pass, in 8 real power cuts the gateway switched itself on every time and all 10 cameras recorded again within 31 s of power coming back (limit 180 s). Loss before a cut: Fail in the 5 real cuts before the fix (main stream up to 57 s, limit 25 s); Pass after the fix, in 3 real power cuts (main stream up to 18 s, substream up to 33 s, limits 25 s and 65 s) and 8 immediate reboots without saving (main up to 17 s, substream up to 64 s) | m11-power-20261106-142808 (before the fix), m11-power-20261106-163630 (real cuts after the fix), m11-power-20261106-142816 and m11-power-20261106-145927 (reboots) |
| Full disk: the M9 cleanup protects the system, and it recovers once space is freed | Pass (Nov 6 02:23 to 02:29): a 20 GB filler pushed the disk over a test limit; the cleanup deleted the two oldest hours in order with no recording gap; after the filler was removed Frigate and both streams were healthy, ext4 logged no errors, the OS disk stayed at 18 % | m11-full-disk-20261106-022907, m9-pressure-20261106-022832 |
| Missing disk: a clear error instead of crashing silently, recording resumes when the disk returns | Pass (volume off 02:20:45 to 02:22:15): the recorder refused to start ("Dependency failed for srv-vivo.mount") and nothing was written to the OS disk; the health endpoint showed disk_missing; recording resumed on its own 15 s after the volume came back | m11-missing-disk-20261106-022303 |
| Corrupted file: a damaged segment does not stop playback of the segments around it | Pass, 7 of 7 (Nov 6 14:29): cam01 14:06:36 cut to 60 % and caught as "cut short: 168625 of 281043 bytes"; a 128 s clip across it still gave 133 s of video, as before the damage; the HLS playlist loads; both neighbouring segments play; the original was put back | m11-corrupt-20261106-142908 |
| The gateway recovers without anyone logging in | Pass: missing disk, full disk, Frigate kills, reboots, camera disconnects and power cuts | reports above |

### Power loss

- **Real power cuts, Nov 6 13:13 to 13:24**: 5 cuts by hand on site, 15 s off (5 agreed in the group on
  Nov 5 because of SSD wear; the scope's plan said 10). The BIOS switched the gateway on every time.
  Footage lost before each cut: main stream up to 57 s, substream up to 76 s for one cut. Cause:
  Linux kept written data in memory for up to 30 s, so segments Frigate had already moved to the disk
  came back empty (157 files) or without their index; some stayed in Frigate's index, so a clip
  across them stopped there and the HLS playlist listed parts that answered 404. ext4 logged no
  errors; SSD health unchanged (NVMe 0 media errors, its unsafe-shutdown count 4 to 9; SATA 0
  reallocated sectors).
- **Fix, deployed Nov 6 13:34 to 14:52** (docs/m11-faults.md, "How a power cut is handled"): new
  footage reaches the disk within about 6 s (data written out after 3 s, journal commit every 2 s);
  empty segments from before the boot are removed before the recorder starts (`bin/vivo-clean-empty`);
  segments a cut left broken are taken out of Frigate's index once Frigate is up
  (`vivo-cut-cleanup.service`). On the leftovers of the real cuts and the first test reboot it
  removed 47 entries; the clip across cut 2 on cam01 then gave 89 s instead of 60 s, with every HLS
  part answering. From 15:45, the substream archiver writes each segment to the disk and checks it
  before giving it its final name, so a cut can leave only a `.part` file, which it deletes at start
  (asked by Haris for the cloud uploader).
- **Checked after the fix**: 8 immediate reboots that skip writing out memory (`tests/m11_faults.py
  power --crash`, sysrq b), the same for unsaved data as a power cut, though the SSD keeps power.
  5 reboots 13:40 to 13:50: main stream lost up to 17 s, substream up to 64 s. 3 reboots 14:52 to
  14:59, with the index cleanup: main up to 9 s, substream up to 36 s, and Frigate's index listed no
  broken file near a cut. Recording back within 35 s every time; 0 unexplained gaps; no ext4 errors.
- **Real power cuts after the fix, Nov 6 16:29 to 16:34**: 3 cuts by hand on site, 15 s off. Main
  stream lost up to 18 s, substream up to 33 s; recording back within 31 s of power on; no broken
  file anywhere (Frigate's index clean after each start, no broken substream segment, the unfinished
  `.part` files deleted at start); no ext4 errors; SSD health unchanged (NVMe 0 media errors, its
  unsafe-shutdown count 9 to 12; SATA 0 reallocated sectors).
- The loss counts files a cut left empty or broken as lost, and each gap is matched to its own cut
  (both added to the report during this milestone; the reports were rebuilt with them).

### Corrupted file

The first run (Nov 6 02:29, m11-corrupt-20261106-022944) was not valid: cutting a file to 60 % did not
look broken to a check that reads only the file's header, because Frigate stores the index at the
start, and the chosen minute fell inside the Frigate kill test. The test now picks a minute without
gaps, uses the segments just before and after as neighbours, and checks that the file's MP4 boxes add
up to its size (`tools/mp4box.py`). On an hour of the gateway's recordings that check passed all 3,249
main-stream files; the only files it has flagged are ones the power cuts broke, each confirmed
unplayable by ffprobe.

## M12 Recording integrity and timeline QA

| Done when (scope) | Result | Evidence |
|---|---|---|
| The script runs over a 24-hour period at 10 cameras and its findings match a manual spot-check | Pass. Script run Nov 3 15:17 to Nov 4 15:17, 10 cameras, 22 of 22 checks. Manual spot-check of its 10 listed segments (Nov 6 15:05): 10 of 10 match the script; each file whole, every frame decoded with 0 errors, length within 0.15 s of Frigate's index, a clear picture in a still from each, the camera's on-screen clock within 1 s of the segment time | m12-integrity-20261104-160238, m12-spotcheck-20261106 |
| Every segment in that period plays, or is listed as broken with its cause | Pass: 86,412 main-stream and 14,410 substream segments, none broken, no interval missing on either stream; gateway clock 0.0006 s from UTC and no segment written before its own end time. Rerun of the same 24 hours on the main stream with the MP4 completeness check (Nov 6 15:21 to 15:52): 86,412 segments, 0 broken, 0 missing, 12 of 12 | m12-integrity-20261104-160238, m12-integrity-20261106-155254 |

The first run read file headers only; a file cut short with its index at the start looks whole that
way, so the same 24 hours were scanned again with the MP4 completeness check (see "Corrupted file").
The substream was not rescanned: its first hours had already passed the 3-day retention, and its
files keep their index at the end, where a header-only read already notices a cut.

## M22 Health telemetry and diagnostics

| Done when (scope) | Result | Evidence |
|---|---|---|
| Each alert condition is triggered on purpose and the endpoint shows it correctly within the agreed time (60 s) | Pass, all 10 alerts, each shown in time and cleared after the fault was undone: camera_offline 42 s, recording_gap 0 s (raised without camera_offline), clock_not_synced 8 s, container_down 12 s, disk_near_limit 10 s, ssd_health 8 s, high_temperature 22 s, early_deletion 12 s (the last four simulated with a test override; Nov 6 02:16 to 02:20); disk_missing within 40 s (missing-disk test); recorder_down 17 s, cleared 10 s after Frigate was back (Nov 6 14:45) | m22-check-20261106-021955, m11-missing-disk-20261106-022303, m22-check-20261106-144530 |

## Changes made during this milestone

- Power loss: the write-cache settings, `bin/vivo-clean-empty` and `vivo-cut-cleanup.service`
  (`tools/cut_cleanup.py`, reviewed before it was deployed), all installed by `gateway/setup.sh`; the
  substream archiver gives a segment its final name only once it is on the disk and whole.
- `tools/mp4box.py`, the MP4 completeness check, used by the M11 tests and M12.
- Tests: `tests/m11_faults.py power --crash`; the power report counts damaged files as lost and checks
  Frigate's index; each gap goes to its own event when events are close together;
  `tests/m22_check.py --only recorder_down`; `tests/m10_recovery.py disconnect --block` (a short
  network drop); `tests/m12_integrity.py --start ... --streams main` (a fixed period, one stream).

Unit tests: `python3 -m unittest discover -s tests/unit` (68 pass).

## Hourly CPU peak (follow-up from Milestone 2, M6)

Measured again on Nov 6 around 16:00 with a sample about every 2 s: from 16:00:01 to 16:00:17 the CPU
was at 98 to 100 % while 4 to 8 preview encoders ran, then back to about 51 % (the sampler itself adds a
few points); no gap on either stream of any camera from 15:58 to 16:01. Cause: at the top of each hour
Frigate encodes each camera's timeline preview for the hour (a 180-pixel-high video) with libx264 on the
CPU, one thread per camera, all at once; its code encodes previews on the CPU only by design, and no
setting spreads them out or moves them to the graphics. The quality setting changes the bitrate more
than the CPU time. So this stays as a known, harmless peak of about 15 s an hour at 10 cameras, longer
with more cameras (m6-top-of-hour-20261106-1600.txt).

## Open points

- Power cuts: the scope's plan said 10. 8 real cuts were made (5 before the fix, 3 after), plus 8
  immediate reboots; 5 were agreed in the group on Nov 5 because of SSD wear, waiting for Simon to
  confirm.
- Short network drop (see M10): after a drop shorter than 10 s a camera's main stream can take up to
  about 2 minutes to record again (within the 120 s limit). Planned fix, outside this milestone:
  the health service restarts that camera's recorder when its newest segment is about 30 s old while
  its stream is up, which should bring this to well under a minute.
- One broken entry stays in Frigate's index on purpose (cam09, Nov 6 13:12:33 to 13:12:43), because a
  healthy segment overlaps it and removing it would remove that segment too.
- Substream segments the power cuts before the fix broke (23 from Nov 6 13:12 to 13:24, and one from
  the immediate reboots) stay in place and are listed by M12; they leave the disk with the 3-day
  retention. Since 15:45 on Nov 6 the archiver no longer produces them (see "Power loss"); Haris was
  told.
