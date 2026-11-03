# M9: retention and overwrite policies

## What it does

| Footage | Kept | Done by |
|---|---|---|
| Main stream | 7 days (`record_days`) | Frigate (`record.continuous.days`) |
| Substream segments | 3 days (`substream_days`), long enough for the uploader to catch up after a weekend outage | `storage` service: segments whose start time is older |
| Exports | 7 days (`export_days`) | `storage` service, through Frigate's API |

When the disk reaches the safe limit before that (busy scenes, a smaller disk), the oldest footage
is deleted first while recording carries on (docs/m8-storage.md). The substream number is a
proposal; Haris confirms it once Vivo gives the expected outage length and upload speed.

How many days fit depends on what the cameras see (variable bitrate, docs/findings.md F7).
`sudo vivo-disk` shows the days stored and, from the rate so far, how many days fit under the
safe limit.

## Early deletion

With the main stream kept 7 days and the substream 3 days, the oldest-first rule cuts the main
stream back to the last 3 days before it touches any substream segment, so the substream is
deleted early only when the disk is much fuller than planned. If it happens, the cloud uploader
may not have those segments yet, so the `storage` service lists each one:

- `/srv/vivo/recordings/substream/early-deletions.jsonl`, one JSON line per camera and clock hour,
  appended when the segments are deleted and never rewritten. The uploader can read it through its
  read-only mount of the substream folder; its name does not end in `.mp4`, so it is not a segment.

  ```json
  {"camera": "cam01", "start": "2026-11-02T03:00:00Z", "end": "2026-11-02T04:00:12Z", "deleted_at": "2026-11-02T10:15:00Z", "segments": 60}
  ```

  `start` is the first deleted segment's start and `end` the last one's end (UTC, to the second, as
  in the file names), `deleted_at` when they were deleted, `segments` how many.
- The health endpoint (M22, Milestone 3) will show the alert `early_deletion` (warning) for that
  camera for 24 hours.

Segments deleted after `substream_days`, the normal retention, are not listed.

## Test

```bash
sudo tests/m9_pressure.py            # disk-pressure test
sudo vivo-disk                       # actual days stored at 10 cameras
```

The pressure test writes a filler file on the recording disk until it is just above the safe
limit, runs one storage-guard pass, and checks that the deleted hours are the oldest ones in
order, that Frigate no longer lists them, that use is back at the target, and that the main
stream and the substream kept recording with no gap, and that any substream segment deleted before
its retention ended is listed in `early-deletions.jsonl`. The filler file is then deleted. On a nearly
empty disk, reaching 90 % would mean writing about a terabyte, so the filler is capped at 20 GB
and the test uses a lower limit just under the filled level, with a target that frees about the
two oldest hours; the report says which limit was used.

## Not included

Cloud retention tiers, legal holds on incident clips, retention chosen per customer.
