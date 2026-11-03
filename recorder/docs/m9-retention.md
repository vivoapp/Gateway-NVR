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

## Test

```bash
sudo tests/m9_pressure.py            # disk-pressure test
sudo vivo-disk                       # actual days stored at 10 cameras
```

The pressure test writes a filler file on the recording disk until it is just above the safe
limit, runs one storage-guard pass, and checks that the deleted hours are the oldest ones in
order, that Frigate no longer lists them, that use is back at the target, and that the main
stream and the substream kept recording with no gap. The filler file is then deleted. On a nearly
empty disk, reaching 90 % would mean writing about a terabyte, so the filler is capped at 20 GB
and the test uses a lower limit just under the filled level, with a target that frees about the
two oldest hours; the report says which limit was used.

## Not included

Cloud retention tiers, legal holds on incident clips, retention chosen per customer.
