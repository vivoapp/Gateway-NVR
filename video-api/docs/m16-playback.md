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
- Through Frigate, a range longer than one hour is fetched as one part per clock hour (UTC) and joined
  into one playlist (`vivo_api/recorders/frigate.py`): Frigate's video module refuses one playlist of
  more than about 1,100 pieces, about 3 hours (findings F8). Each part after the first starts with a
  discontinuity marker, and piece names carry the part number (`c3-seg-12-v1-a1.m4s`). Parts of
  finished hours are kept for an hour, so the next playback over the same day is quick. Playlists
  over 1 KB go out gzipped when the player accepts it.

## Test

- `tests/unit/test_playback.py`: the flow, piece names and path tricks refused, expiry and renewal,
  a refused renewal closing the playback, playlist rewriting.
- `tests/unit/test_frigate_vod.py`: a fake Frigate with the same piece limit: one hour passed
  through as before, a day joined from 24 parts, empty hours left out, bad piece names refused,
  finished hours kept.
- `tests/m16_playback.mjs`: hls.js in headless Chrome: the first picture over 5 starts (median, with
  the time of each step), then 20 random seeks across the range. It measures the network the
  workstation is on.
- `tests/m16_probe.py`: the same two measures on the gateway itself (the building-network figure),
  with ffmpeg from the Frigate image; the footage does not leave the gateway.

## Not included

- Faster or slower playback speeds and frame-by-frame stepping: these are player features.
- Playback of the substream (it belongs to the cloud uploader).
