# M12: recording integrity and timeline QA

## What it does

- **Integrity scan**, `tests/m12_integrity.py`: for a period (default the last 24 hours), every camera,
  both streams, it lists every missing interval (a gap of more than 3 s between main-stream segments,
  2 s between substream segments) and every segment that will not play, with its cause: file missing,
  empty file, ffprobe's error (for example "moov atom not found"), no video stream, or a length that
  differs by more than 2 s from what the recorder wrote down (Frigate's database for the main stream,
  the file name for the substream). `--deep` also reads every frame. Every segment and its verdict
  goes into a CSV next to the report.
- **Segment-age monitor**: the health endpoint's `recording_gap` alert (docs/m22-health.md) flags a
  camera whose newest segment is older than 30 s (main) or 150 s (substream), even while its stream
  looks healthy: the 111-second case from the assessment (docs/findings.md F2).
- **Clock check**: chrony's offset from UTC within the agreed 2 s, and each recent segment written to
  disk after its own end time (so the timestamps follow the gateway's clock). The cameras' own clocks
  are not used for recording times.
- **Manual spot-check**: the report lists random segments (and every broken one) with time and file,
  for a person to open in the Frigate UI or a player and tick off.

These cameras' H.265 makes ffprobe print warnings for files that play fine (docs/findings.md F9), so a
warning alone never marks a segment broken; a missing length or video stream does.

## Test

```bash
sudo tests/m12_integrity.py --hours 24 --spot 10       # after a 24-hour run at 10 cameras
sudo tests/m12_integrity.py --hours 24 --deep           # the same, reading every frame (slower)
```

Done when the findings match the manual spot-check and every segment in the period plays or is
listed as broken with its cause. The M11 corrupted-file test leaves one known broken segment the scan
must find, if it is run over the same period before the file is restored.

## Not included

The timeline in Vivo Watch, cloud-side gap reporting.
