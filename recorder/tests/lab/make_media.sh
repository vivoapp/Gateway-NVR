#!/usr/bin/env bash
# Make the looping test clips that the lab's fake cameras send (tests/lab/README.md).
# They copy what Vivo's test cameras send (docs/findings.md F5): H.265 with a keyframe every
# 5 s at 25 fps and no B-frames, main stream with G.711 A-law audio at 8 kHz, substream video only.
# usage: tests/lab/make_media.sh OUT_DIR     (needs ffmpeg with libx265; a few minutes once)
set -euo pipefail
OUT="${1:?usage: make_media.sh OUT_DIR}"
mkdir -p "$OUT"
x265="-c:v libx265 -preset ultrafast -pix_fmt yuv420p -g 125 -x265-params keyint=125:min-keyint=125:scenecut=0:bframes=0:log-level=error"

if [[ ! -s "$OUT/main.mkv" ]]; then
  echo "making main.mkv (2880x1620, 60 s)"
  # shellcheck disable=SC2086
  ffmpeg -hide_banner -loglevel error -y \
    -f lavfi -i "testsrc2=size=2880x1620:rate=25:duration=60" \
    -f lavfi -i "sine=frequency=440:sample_rate=8000:duration=60" \
    $x265 -b:v 400k -c:a pcm_alaw -ac 1 -ar 8000 "$OUT/main.mkv.tmp.mkv"
  mv "$OUT/main.mkv.tmp.mkv" "$OUT/main.mkv"
fi
if [[ ! -s "$OUT/sub.mkv" ]]; then
  echo "making sub.mkv (640x360, 60 s)"
  # shellcheck disable=SC2086
  ffmpeg -hide_banner -loglevel error -y \
    -f lavfi -i "testsrc2=size=640x360:rate=25:duration=60" \
    $x265 -b:v 150k -an "$OUT/sub.mkv.tmp.mkv"
  mv "$OUT/sub.mkv.tmp.mkv" "$OUT/sub.mkv"
fi
ls -l "$OUT"
