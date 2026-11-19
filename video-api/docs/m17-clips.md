# M17: Clip export and download

## What it does

- `POST /v1/cameras/{id}/clips` asks for a clip of at most 30 minutes. It is queued: at most 2 are
  prepared at once (`CLIP_WORKERS`), up to 6 more wait (`CLIP_QUEUE_MAX`), and beyond that the
  answer is `429 limit_reached` (`clip_queue`). A clip is refused with `503 disk_near_limit` when the
  recording disk is at its safe limit, and with `404 no_recording` when nothing is recorded.
- Each clip is pulled from Frigate's `clip.mp4` (built on request, nothing kept in Frigate) into
  `/srv/vivo/video-api/clips/`, with progress from 0 to 100, checked to be a whole MP4, and only
  then made available. A broken clip fails with a clear error instead of a broken download.
- Only the user who asked for a clip can see or download it. Finished clips are deleted 1 hour after
  they are ready, and the oldest first when the folder passes its size cap (5 GB). Clips left from
  before an API restart are deleted at start.
- Every request and download is in the audit log.

## Test

- `tests/unit/test_clips.py`: the flow, a broken clip, the limits (also with five requests at the
  same moment), cancel, cleanup by age and by size.
- `tests/m17_clips.py` against a running API: a 5-minute clip ready within 30 s, a whole MP4 that
  plays for 5 minutes with every frame decoding, over 30 minutes refused, three at once (two
  prepared, one waiting, all finish), deletion, and with `--keep` the automatic cleanup.

## Not included

- Clips longer than 30 minutes, and clips joined from several cameras.
- Uploading clips to the cloud (Haris's M19/M20 handles the substream uploads).
