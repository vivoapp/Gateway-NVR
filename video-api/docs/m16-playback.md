# M16: Historical playback and seeking

## What it does

- `POST /v1/cameras/{id}/playback` with a time range (at most 24 hours) checks the grant and that
  something is recorded in the range (`404 no_recording` otherwise), and returns a playback URL plus
  the recorded periods, so the app can mark the gaps on its scrub bar.
- The player uses **HLS**: it downloads a playlist and then pieces of about 10 seconds only as it
  needs them, so nobody downloads a large file to watch, and a seek jumps straight to the piece for
  that moment. Pieces come from the recordings without re-encoding (Frigate's `/vod`).
- The playback id in the URL is the credential (HLS players cannot add headers). It is random, tied
  to one camera, one range and the grant that opened it, and stops working when that grant expires
  unless the client renews it with a fresh grant.
- Every playlist passes through `rewrite_playlist` (`vivo_api/playback.py`): only plain piece names
  reach the player, and every piece is fetched through the API, so the player never learns a
  recorder address and cannot reach any other recorder route.

## Test

- `tests/unit/test_playback.py`: the flow, piece names and path tricks refused, expiry and renewal,
  a refused renewal closing the playback, playlist rewriting.
- `tests/m16_playback.mjs`: hls.js in headless Chrome: the first picture over 5 starts (median, with
  the time of each step), then 20 random seeks across the range.

## Not included

- Faster or slower playback speeds and frame-by-frame stepping: these are player features.
- Playback of the substream (it belongs to the cloud uploader).
