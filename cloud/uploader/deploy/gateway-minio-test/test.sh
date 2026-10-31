#!/usr/bin/env bash
# TEMPORARY gateway test with a throw-away MinIO (until Backblaze B2 is ready).
#
#   ./test.sh start     build, make passwords, start MinIO + the uploader (ONE camera, read-only)
#   ./test.sh status    counts from the uploader and the bucket
#   ./test.sh logs      follow the uploader log
#   ./test.sh check     hardening + uploaded vs local files for the test camera
#   ./test.sh stop      stop everything, keep the data
#   ./test.sh clean     stop and DELETE everything the test wrote (containers, volumes, files, passwords)
#
# Options (environment):  TEST_CAMERA=cam01   TEST_RECORDINGS=/srv/vivo/recordings/substream
# All test data is in /var/lib/vivo-cloud/minio-test (OS disk), never under /srv/vivo.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")" || exit 1

export TEST_BASE="${TEST_BASE:-/var/lib/vivo-cloud/minio-test}"
export TEST_CAMERA="${TEST_CAMERA:-cam01}"
export TEST_RECORDINGS="${TEST_RECORDINGS:-/srv/vivo/recordings/substream}"
DC=(docker compose -f compose.yaml)
CFG=/etc/vivo-uploader/config.toml

make_env() {
  mkdir -p "$TEST_BASE/state" "$TEST_BASE/minio-data"
  [[ -f "$TEST_BASE/cloud.env" ]] && return 0
  local user pass
  user="test$(LC_ALL=C tr -dc 'a-z0-9' </dev/urandom | head -c 8)"
  pass="$(LC_ALL=C tr -dc 'A-Za-z0-9' </dev/urandom | head -c 28)"
  printf 'MINIO_ROOT_USER=%s\nMINIO_ROOT_PASSWORD=%s\n' "$user" "$pass" > "$TEST_BASE/minio.env"
  printf 'S3_ENDPOINT_URL=http://minio:9000\nS3_REGION=us-east-1\nS3_BUCKET=vivo-test\nS3_KEY_PREFIX=site-test\nS3_ACCESS_KEY=%s\nS3_SECRET_KEY=%s\n' \
    "$user" "$pass" > "$TEST_BASE/cloud.env"
  chmod 600 "$TEST_BASE"/*.env
}

case "${1:-}" in
  start)
    [[ -d "$TEST_RECORDINGS/$TEST_CAMERA" ]] || { echo "No folder $TEST_RECORDINGS/$TEST_CAMERA" >&2; exit 1; }
    make_env
    "${DC[@]}" --profile setup run --rm init || exit 1
    "${DC[@]}" up -d --build
    echo "Started (camera $TEST_CAMERA). Look at it with: ./test.sh status   ./test.sh logs   ./test.sh check"
    ;;
  status)
    "${DC[@]}" ps
    "${DC[@]}" exec -T uploader python -m segment_uploader status --remote --config "$CFG"
    ;;
  logs)
    "${DC[@]}" logs -f --tail 50 uploader
    ;;
  check)
    echo "== Hardening"
    cid="$("${DC[@]}" ps -q uploader)"
    [[ -n "$cid" ]] || { echo "uploader is not running"; exit 1; }
    docker inspect "$cid" --format 'user={{.Config.User}} readonly_rootfs={{.HostConfig.ReadonlyRootfs}} memory={{.HostConfig.Memory}} nano_cpus={{.HostConfig.NanoCpus}}'
    docker inspect "$cid" --format '{{range .Mounts}}{{.Destination}} rw={{.RW}}{{"\n"}}{{end}}'
    echo "== Segments"
    "${DC[@]}" exec -T uploader python -m segment_uploader status --remote --config "$CFG"
    "${DC[@]}" exec -T uploader python -m segment_uploader gaps --config "$CFG"
    echo "local files for $TEST_CAMERA: $(find "$TEST_RECORDINGS/$TEST_CAMERA" -name '*.mp4' | wc -l)"
    ;;
  stop)
    "${DC[@]}" down
    ;;
  clean)
    "${DC[@]}" down --remove-orphans
    "${DC[@]}" --profile clean run --rm cleaner
    rm -rf "$TEST_BASE"
    echo "Removed. Check: ls $TEST_BASE (should not exist)"
    ;;
  *)
    sed -n '2,12p' "$0"
    ;;
esac
