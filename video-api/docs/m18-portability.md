# M18: Second-adapter mock validation

## What it shows

The Video API does not depend on Frigate. Every recorder call goes through one interface
(`vivo_api/recorders/base.py`, contract section 10), and two recorders implement it:

| Adapter | Where recordings come from |
|---|---|
| `recorders/frigate.py` | Frigate 0.18's API on its authenticated port, as a viewer |
| `recorders/mock.py` | a folder of fragmented-MP4 pieces named by their start time, the way another recorder might keep them (made by `tools/make_mock.py` with ffmpeg) |

One setting switches between them (`RECORDER=frigate` or `RECORDER=mock` in `video-api.env`). The
routes, the grants, the errors, Rails and Vivo Watch stay exactly the same.

## Test

`tests/contract/test_contract.py` is one test suite run against every adapter: the timeline (with a
known gap in the mock footage), playback (playlist and pieces through the API), a clip (a whole MP4
of the right length) and an empty range (`404 no_recording`). It runs against the mock always, and
against Frigate when `FRIGATE_URL` points at one.

## What changes to replace Frigate

| Part | Changes? |
|---|---|
| A new adapter implementing the six methods of `recorders/base.py` | yes, the only code change |
| `RECORDER=` in `video-api.env` | yes, one setting |
| The recorder's own checks (recording health in M22, the recorder tests) | yes, they belong to the recorder |
| Routes, grants, errors, limits, audit log, live view (go2rtc) | no |
| Rails, Vivo Watch, the cloud uploader | no |

## Not included

- An adapter for a real second recorder or a vendor NVR (out of scope by agreement).
