# M7: continuous recording, main stream and substream

## What it does

- **Main stream:** Frigate records every camera's main stream 24/7 to the recording disk, copied
  without transcoding, in 10-second files (M2). No event or motion recording.
- **Substream for the cloud uploader:** the `substream` service saves every camera's substream as
  60-second MP4 segments in the layout agreed with Haris, `docs/SEGMENT_FORMAT.md`:
  `MEDIA_ROOT/recordings/substream/<camera_id>/<YYYY-MM-DD>/<camera_id>_<start>Z_<end>Z.mp4`.
- One ffmpeg per camera (`tools/substream_archiver.py`) reads `<camera>_sub` from go2rtc, so each
  camera still has one connection per stream. Video is copied, not transcoded; audio is left out.
- Segments are cut at the first keyframe after each full minute (UTC). ffmpeg writes
  `<camera_id>_<start>Z.mp4.part`; when the segment is complete, the service renames it to the
  full name. A `.part` that ffmpeg could not finish (killed, crashed) is deleted, never renamed.
- File names come from the gateway's clock (UTC, NTP). The end time is where the next segment
  starts, so a gap-free run has `end` equal to the next `start`; the start is the segment's first
  frame. Each file's own duration (MP4 header) matches its name to the second.
- Frame times come from the arrival clock, not the camera's, as in Frigate's camera preset: when
  go2rtc reconnects to a camera, the camera's timestamps restart from a new base, and a copied
  segment would otherwise claim hours of video (docs/findings.md F11). This needs a camera stream
  without B-frames, which the reference cameras are; with B-frames the service keeps the camera's
  timestamps and logs a warning.
- A stopped or restarted service finishes the segment it is writing (short last segment). A
  stalled stream (no finished segment for 3.5 minutes) restarts that camera's ffmpeg; go2rtc
  missing or a camera offline is retried, every 2 to 30 seconds.
- Retention of the segments is M9 (`tools/storage_guard.py`).

## Test

```bash
sudo tests/m7_check.py                                   # last 15 minutes
sudo tests/m7_check.py --hours 24 --samples 20           # the 24-hour run (Done when)
sudo tests/m7_check.py --hours 24 --planned "2026-11-02T10:00Z/2026-11-02T10:05Z"
```

Per camera: segment names, date folders and lengths as in the format document, gaps between
segments, at most one `.part`, and the main stream's coverage from Frigate's segment list.
Random minutes: Frigate's API returns that minute of the main stream as a playable clip
(`/api/<camera>/start/<t>/end/<t+60>/clip.mp4`, checked with ffprobe), and the substream
segments for the same minute exist with the correct names.

The extra CPU and disk cost of the second stream at 10 cameras comes from the M6 benchmark: the
`substream` container's CPU share and the disk write rate.

## Not included

Event or AI-triggered recording, uploading to the cloud (M19, M20), the Video API (M13, M14).
