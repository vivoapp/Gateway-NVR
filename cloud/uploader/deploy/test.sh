#!/usr/bin/env bash
# Try the gateway setup on a PC: same container settings as the gateway, MinIO as fake storage,
# 10 simulated cameras. Needs Docker Desktop and Git Bash.
#
#   ./test.sh start [fast]   build and start (fast = a new segment every 5 s instead of 60 s)
#   ./test.sh check          prove the hardening and compare recorded / uploaded / stored
#   ./test.sh status         counts from the uploader
#   ./test.sh snapshot       remember the local files now (so `accept` can prove they were not changed)
#   ./test.sh accept         M19 acceptance checks (re-reads files and storage, does not trust the uploader).
#                            While running, segments from the last 30 s count as "in flight" (and are reported).
#   ./test.sh pause          stop the simulated cameras, so the queue can drain
#   ./test.sh accept --strict   the final check, after `pause` and a short wait: no grace, any difference fails
#   ./test.sh resume         start the simulated cameras again
#   ./test.sh report         the same checks as a markdown report with upload rate and CPU:  ./test.sh report > m19-report.md
#   ./test.sh second         also start a SECOND store (second MinIO) with the same uploader, one variable changed
#   ./test.sh second-check   both stores hold the same segments
#   ./test.sh logs           follow the uploader log
#   ./test.sh stop           stop everything (keeps the data)
#   ./test.sh clean          stop and delete all test data and the generated test passwords
#
# Test passwords are generated here at the first start (folder .test/, not in Git).

set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")" || exit 1
export MSYS_NO_PATHCONV=1 MSYS2_ARG_CONV_EXCL='*'

TEST_DIR=.test
export TEST_DIR CLOUD_ENV_FILE="$TEST_DIR/cloud.env"
DC=(docker compose -f compose.yaml -f compose.test.yaml)

if [[ -t 1 ]]; then B=$'\033[1m'; G=$'\033[32m'; R=$'\033[31m'; N=$'\033[0m'; else B=""; G=""; R=""; N=""; fi
ok()   { printf '%s  ok   %s%s\n' "$G" "$*" "$N"; }
fail() { printf '%s  FAIL %s%s\n' "$R" "$*" "$N"; FAILED=1; }

need_docker() {
  command -v docker >/dev/null 2>&1 || { echo "Docker is not installed." >&2; exit 1; }
  docker info >/dev/null 2>&1 || { echo "Docker is not running. Start Docker Desktop and try again." >&2; exit 1; }
}

make_env() {
  [[ -f "$TEST_DIR/cloud.env" ]] && return 0
  mkdir -p "$TEST_DIR"
  local user pass
  user="test$(LC_ALL=C tr -dc 'a-z0-9' </dev/urandom | head -c 8)"
  pass="$(LC_ALL=C tr -dc 'A-Za-z0-9' </dev/urandom | head -c 28)"
  printf 'MINIO_ROOT_USER=%s\nMINIO_ROOT_PASSWORD=%s\n' "$user" "$pass" > "$TEST_DIR/minio.env"
  printf 'S3_ENDPOINT_URL=http://minio:9000\nS3_REGION=us-east-1\nS3_BUCKET=vivo-test\nS3_KEY_PREFIX=site-test\nS3_ACCESS_KEY=%s\nS3_SECRET_KEY=%s\n' \
    "$user" "$pass" > "$TEST_DIR/cloud.env"
}

status_line() { "${DC[@]}" exec -T uploader python -m segment_uploader status --brief --config /etc/vivo-uploader/config.toml 2>/dev/null | tail -n 1; }
get() { echo " $1" | sed -n "s/.* $2=\([^ ]*\).*/\1/p"; }

do_start() {
  need_docker; make_env
  export SIM_INTERVAL=60; [[ "${1:-}" == "fast" ]] && SIM_INTERVAL=5
  "${DC[@]}" build uploader || exit 1
  "${DC[@]}" up -d || exit 1
  echo "Started. Look at it with: ./test.sh status   ./test.sh logs   ./test.sh check"
}

do_check() {
  need_docker; FAILED=0
  local cid; cid="$("${DC[@]}" ps -q uploader)"
  [[ -n "$cid" ]] || { echo "The uploader is not running. Run ./test.sh start first." >&2; exit 1; }
  printf '%s== Hardening%s\n' "$B" "$N"
  local uid; uid="$("${DC[@]}" exec -T uploader python -c 'import os; print(os.getuid())' 2>/dev/null | tr -d '\r')"
  [[ "$uid" == 10001 ]] && ok "runs as user 10001, not root" || fail "user is '$uid', expected 10001"
  "${DC[@]}" exec -T uploader python -c 'open("/data/recordings/_probe","w")' >/dev/null 2>&1 \
    && fail "the recordings folder can be written" || ok "the recordings folder is read-only"
  "${DC[@]}" exec -T uploader python -c 'open("/app/_probe","w")' >/dev/null 2>&1 \
    && fail "the container's own files can be written" || ok "the container's own files are read-only"
  "${DC[@]}" exec -T uploader python -c 'open("/data/state/_probe","w").close(); import os; os.remove("/data/state/_probe")' >/dev/null 2>&1 \
    && ok "the state folder is writable (needed)" || fail "the state folder is not writable"
  local caps mem cpus
  caps="$(docker inspect -f '{{.HostConfig.CapDrop}}' "$cid")"
  [[ "$caps" == *ALL* ]] && ok "all extra Linux rights dropped" || fail "capabilities not dropped: $caps"
  mem="$(docker inspect -f '{{.HostConfig.Memory}}' "$cid")"
  cpus="$(docker inspect -f '{{.HostConfig.NanoCpus}}' "$cid")"
  [[ "$mem" == 536870912 ]] && ok "memory limit 512 MB" || fail "memory limit is $mem"
  [[ "$cpus" == 1000000000 ]] && ok "CPU limit 1 core" || fail "CPU limit is $cpus"
  [[ "$(docker inspect -f '{{.HostConfig.LogConfig.Config}}' "$cid")" == *max-size:20m* ]] \
    && ok "log rotation 20 MB x 5" || fail "log rotation not set"

  printf '%s== Segments%s\n' "$B" "$N"
  local line up pend recorded bucket
  line="$(status_line)"; echo "  $line"
  up="$(get "$line" uploaded)"; pend="$(get "$line" pending)"; recorded="$(get "$line" local_files)"; bucket="$(get "$line" bucket_objects)"
  [[ "$up" =~ ^[0-9]+$ && "$up" -gt 0 ]] && ok "segments are being uploaded ($up)" || fail "nothing uploaded yet (wait a minute, or use ./test.sh start fast)"
  [[ "$bucket" =~ ^[0-9]+$ && "$bucket" -ge "$up" ]] && ok "objects in the bucket: $bucket" || fail "bucket objects: $bucket"
  [[ "$(get "$line" conflict)" == 0 && "$(get "$line" missing)" == 0 ]] && ok "no conflicts, nothing missing" || fail "conflicts or missing files"
  local cams; cams="$("${DC[@]}" exec -T uploader python - <<'PY' 2>/dev/null
import os
print(len([d for d in os.listdir("/data/recordings") if d.startswith("VIVO-CAM-")]))
PY
)"
  [[ "$(echo "$cams" | tr -d '\r')" == 10 ]] && ok "10 cameras seen" || fail "cameras seen: $cams"
  "${DC[@]}" exec -T uploader sh -c 'ls /data/state/index/*.jsonl >/dev/null 2>&1' >/dev/null 2>&1 \
    && ok "index records are being written" || fail "no index file yet"
  [[ "$FAILED" == 0 ]] && printf '%sRESULT: PASS%s\n' "$G" "$N" || { printf '%sRESULT: CHECK THE LINES ABOVE%s\n' "$R" "$N"; exit 1; }
}

DCP=("${DC[@]}" --profile second)     # same project, with the optional second store visible to exec / logs
ACC=(python tools/m19_acceptance.py)
CONF=(--config /etc/vivo-uploader/config.toml)

# run an acceptance command in the uploader container, feeding it the uploader's log
acc_run() {  # acc_run <service> <command> [extra args]
  local svc="$1" cmd="$2"; shift 2
  local manifest=()
  "${DCP[@]}" exec -T "$svc" test -f /data/state/manifest.json >/dev/null 2>&1 && manifest=(--manifest /data/state/manifest.json)
  "${DCP[@]}" logs --no-color "$svc" 2>/dev/null | "${DCP[@]}" exec -T "$svc" "${ACC[@]}" "$cmd" "${CONF[@]}" "${manifest[@]}" --log - "$@"
}

do_second() {
  need_docker; make_env
  "${DC[@]}" build uploader || exit 1
  "${DC[@]}" up -d || exit 1
  "${DC[@]}" --profile second up -d || exit 1
  echo "Second store started. Wait a minute, then: ./test.sh second-check"
}

do_second_check() {
  need_docker; FAILED=0
  local a b
  a="$("${DC[@]}" exec -T uploader python -m segment_uploader status --brief "${CONF[@]}" 2>/dev/null | tail -n 1)"
  b="$("${DCP[@]}" exec -T uploader2 python -m segment_uploader status --brief "${CONF[@]}" 2>/dev/null | tail -n 1)"
  echo "  store 1: $a"; echo "  store 2: $b"
  [[ "$(get "$a" bucket_objects)" =~ ^[0-9]+$ && "$(get "$b" bucket_objects)" =~ ^[0-9]+$ ]] || { fail "a store does not answer"; exit 1; }
  [[ "$(get "$b" uploaded)" -gt 0 ]] && ok "the second store receives segments with the same image and config file" || fail "nothing in the second store"
  local d; d=$(( $(get "$a" bucket_objects) - $(get "$b" bucket_objects) )); d=${d#-}
  [[ "$d" -le 20 ]] && ok "both stores hold the same segments (difference $d = segments still in flight)" || fail "the stores differ by $d segments"
  echo "  full check of the second store:"
  acc_run uploader2 verify | sed 's/^/    /'
  [[ "$FAILED" == 0 ]] || exit 1
}

case "${1:-}" in
  start)  do_start "${2:-}" ;;
  check)  do_check ;;
  status) need_docker; "${DC[@]}" exec -T uploader python -m segment_uploader status --remote --config /etc/vivo-uploader/config.toml ;;
  snapshot)     need_docker; "${DC[@]}" exec -T uploader "${ACC[@]}" snapshot "${CONF[@]}" --out /data/state/manifest.json ;;
  accept)       need_docker; acc_run uploader verify "${@:2}" ;;
  report)       need_docker; acc_run uploader report "${@:2}" ;;
  pause)        need_docker; "${DC[@]}" stop simulator ;;
  resume)       need_docker; "${DC[@]}" start simulator ;;
  second)       do_second ;;
  second-check) do_second_check ;;
  logs)   need_docker; "${DC[@]}" logs -f --tail 30 uploader simulator ;;
  stop)   need_docker; "${DC[@]}" --profile second down ;;
  clean)  need_docker; "${DC[@]}" --profile second down -v; rm -rf "$TEST_DIR" ;;
  *)      echo "usage: ./test.sh [start [fast]|check|snapshot|accept|pause|resume|report|second|second-check|status|logs|stop|clean]" >&2; exit 2 ;;
esac
