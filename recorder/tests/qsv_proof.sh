#!/usr/bin/env bash
# M1/M2: prove Intel Quick Sync (VAAPI) decoding works INSIDE the Frigate container.
# Encodes a 20 s 1080p H.264 test clip, then decodes it twice: on the CPU and with
# VAAPI. Hardware decoding must succeed and use far less CPU time.
# usage: sudo tests/qsv_proof.sh
. "$(dirname "$0")/lib.sh"

out="$(docker exec frigate sh -c '
set -e
F="$(ls /usr/lib/ffmpeg/*/bin/ffmpeg | sort -V | tail -1)"
T=/tmp/cache/qsv_proof.mp4
"$F" -v error -y -f lavfi -i testsrc2=size=1920x1080:rate=25 -t 20 -c:v libx264 -preset veryfast -pix_fmt yuv420p "$T"
echo "ffmpeg: $F"
# The "bench:" line is logged at info level (ffmpeg 7+), so -v error would hide it.
echo -n "cpu   "; "$F" -hide_banner -nostats -v info -benchmark -i "$T" -f null - 2>&1 | grep bench:
echo -n "vaapi "; "$F" -hide_banner -nostats -v info -benchmark -hwaccel vaapi -hwaccel_device /dev/dri/renderD128 -hwaccel_output_format vaapi -i "$T" -f null - 2>&1 | grep bench:
rm -f "$T"
' 2>&1)" || { echo "$out"; echo "FAIL: decode test did not run"; exit 1; }
echo "$out"

utime() { sed -n "s/^$1 *bench: utime=\([0-9.]*\)s.*/\1/p" <<<"$out"; }
cpu="$(utime cpu)"; hw="$(utime vaapi)"
[[ -n "$cpu" && -n "$hw" ]] || { echo "FAIL: could not read benchmark times"; exit 1; }
ratio="$(python3 -c "print(round($cpu/max($hw,0.001),1))")"
check "VAAPI decode runs in the container"  test -n "$hw"
check "VAAPI uses much less CPU than software decode (cpu ${cpu}s vs vaapi ${hw}s, ${ratio}x)" \
  python3 -c "import sys; sys.exit(0 if $hw * 3 < $cpu else 1)"
write_report qsv-proof "Quick Sync (VAAPI) decoding inside the Frigate container" \
"Same 20 s 1080p25 H.264 clip decoded twice inside the \`frigate\` container.

\`\`\`
$out
\`\`\`
utime is CPU time used by the decoder. Hardware decoding should use a small fraction of the software figure."
exit $(( NFAIL > 0 ))
