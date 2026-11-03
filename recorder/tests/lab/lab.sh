#!/usr/bin/env bash
# Run the whole recorder stack on a workstation with ten fake cameras (tests/lab/README.md).
#
# usage:
#   tests/lab/lab.sh up [CAMERAS]     build the lab and start it (default 10 cameras)
#   tests/lab/lab.sh prepare [CAMERAS] build the lab without starting it
#   tests/lab/lab.sh down             stop and remove the lab containers (keeps recordings)
#   tests/lab/lab.sh env              print the settings to run the checks against the lab
#   tests/lab/lab.sh compose ARGS...  docker compose on the lab project (ps, logs -f, ...)
#
# LAB_DIR (default $TMPDIR/vivo-lab) holds everything: settings with random lab passwords,
# the recording folder (MEDIA_ROOT), a copy of the repository with the lab camera list,
# and the test clips. Needs Docker and ffmpeg with libx265 (only to make the clips once).
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
LAB_DIR="${LAB_DIR:-${TMPDIR:-/tmp}/vivo-lab}"
PROJECT="$LAB_DIR/project"
export VIVO_ETC="$LAB_DIR/etc"
export LAB_CLIPS="$LAB_DIR/clips"

compose() {
  docker compose --project-directory "$PROJECT" \
    -f "$PROJECT/compose.yaml" -f "$PROJECT/tests/lab/compose.lab.yaml" -f "$PROJECT/tests/lab/compose.fakecam.yaml" \
    --env-file "$PROJECT/versions.env" --env-file "$VIVO_ETC/site.env" "$@"
}

settings() {
  install -d -m 700 "$VIVO_ETC"
  local media="${LAB_MEDIA_ROOT:-$LAB_DIR/media}"
  cat > "$VIVO_ETC/site.env" <<EOF
# Lab settings made by tests/lab/lab.sh
GATEWAY_LAN_IP=127.0.0.1
LAN_CIDRS=127.0.0.0/8
ADMIN_CIDRS=127.0.0.0/8
MEDIA_ROOT=$media
NTP_SERVERS=
EOF
  if [[ ! -s "$VIVO_ETC/secrets.env" ]]; then
    ( umask 077
      { echo "# Lab passwords made by tests/lab/lab.sh (random, lab only)"
        echo "GO2RTC_API_USER=vivo"; echo "GO2RTC_API_PASSWORD=$(openssl rand -hex 12)"
        echo "FRIGATE_GO2RTC_RTSP_USER=vivo"; echo "FRIGATE_GO2RTC_RTSP_PASSWORD=$(openssl rand -hex 12)"
        echo "LABCAM_USER=lab"; echo "LABCAM_PASSWORD=$(openssl rand -hex 12)"
      } > "$VIVO_ETC/secrets.env" )
  fi
  # The same folders gateway/setup.sh makes on the recording disk (docs/m8-storage.md).
  mkdir -p "$media/frigate/state" "$media/frigate/media" "$media/recordings/substream"
}

project() {
  local n="$1"
  mkdir -p "$PROJECT"
  rsync -a --delete --exclude .git --exclude reports --exclude __pycache__ "$REPO/" "$PROJECT/"
  python3 - "$REPO/tests/lab/fakecams.yaml" "$PROJECT/cameras.yaml" "$n" <<'PY'
import sys, yaml
src, dst, n = sys.argv[1], sys.argv[2], int(sys.argv[3])
inv = yaml.safe_load(open(src))
inv["cameras"] = inv["cameras"][:n]
open(dst, "w").write("# Lab inventory: first %d cameras of tests/lab/fakecams.yaml\n" % n + yaml.safe_dump(inv, sort_keys=False))
PY
  python3 "$PROJECT/tools/gen_config.py" --inventory "$PROJECT/cameras.yaml" --out "$PROJECT" >/dev/null
}

prepare() {
  "$REPO/tests/lab/make_media.sh" "$LAB_CLIPS" >/dev/null
  settings
  project "$1"
}

case "${1:-}" in
  prepare) prepare "${2:-10}" ;;
  up)
    prepare "${2:-10}"
    compose up -d --remove-orphans
    echo "lab up: ${2:-10} cameras, MEDIA_ROOT $(grep ^MEDIA_ROOT "$VIVO_ETC/site.env" | cut -d= -f2)"
    ;;
  down) compose down ;;
  env)
    echo "export VIVO_ETC=$VIVO_ETC"
    echo "export VIVO_INVENTORY=$PROJECT/cameras.yaml"
    echo "export VIVO_SERVICES_CONFIG=$PROJECT/services/config.json"
    echo "export GO2RTC_RTSP=rtsp://127.0.0.1:8554"
    ;;
  compose) shift; compose "$@" ;;
  *) sed -n '2,14p' "$0"; exit 2 ;;
esac
