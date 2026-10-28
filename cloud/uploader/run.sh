#!/usr/bin/env bash
# One command for the whole demo.
#
#   ./run.sh start    build and start MinIO, the uploader and a fake recorder
#   ./run.sh demo     scripted outage / crash / recovery run (good for a screen recording)
#   ./run.sh test     automated tests, including tests against the real MinIO
#   ./run.sh status   what has been uploaded, what is waiting, what is in the bucket
#   ./run.sh logs     follow the uploader and recorder logs
#   ./run.sh stop     stop everything (keeps the data)
#   ./run.sh clean    stop and delete all data
#
# Needs Docker (Docker Desktop on Windows) and a bash shell (Git Bash on Windows).

set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")" || exit 1

# Git Bash on Windows rewrites arguments that look like Unix paths; switch that off.
export MSYS_NO_PATHCONV=1 MSYS2_ARG_CONV_EXCL='*'

DC=(docker compose)

if [[ -t 1 ]]; then B=$'\033[1m'; G=$'\033[32m'; R=$'\033[31m'; Y=$'\033[33m'; N=$'\033[0m'; else B=""; G=""; R=""; Y=""; N=""; fi

say()    { printf '%s\n' "$*"; }
head1()  { printf '\n%s== %s%s\n' "$B" "$*" "$N"; }
good()   { printf '%s%s%s\n' "$G" "$*" "$N"; }
bad()    { printf '%s%s%s\n' "$R" "$*" "$N" >&2; }
note()   { printf '%s   %s%s\n' "$Y" "$*" "$N"; }

need_docker() {
  command -v docker >/dev/null 2>&1 || { bad "Docker is not installed."; exit 1; }
  docker info >/dev/null 2>&1 || { bad "Docker is not running. Start Docker Desktop, wait until it says it is running, and try again."; exit 1; }
  "${DC[@]}" version >/dev/null 2>&1 || { bad "The 'docker compose' plugin is missing (it ships with Docker Desktop)."; exit 1; }
}

# One status line from the uploader's state file and the bucket, e.g.
#   uploaded=12 pending=0 uploading=0 conflict=0 missing=0 local_files=0 bucket_objects=12
brief() { "${DC[@]}" run --rm -T cli 2>/dev/null | tail -n 1; }

# get "<status line>" <key>  ->  value
get() { echo " $1" | sed -n "s/.* $2=\([^ ]*\).*/\1/p"; }

show() {
  local line; line="$(brief)"
  if [[ -z "$line" ]]; then note "status: not available yet"; else note "status: $line"; fi
}

# Last few uploader log lines that match a pattern, without compose's "uploader-1 |" prefix.
log_excerpt() {
  "${DC[@]}" logs --no-color uploader 2>/dev/null | sed 's/^[^|]*| *//' \
    | grep -E "$1" | sed 's/^[0-9-]* //' | tail -n "${2:-4}" | sed 's/^/     /'
}

wait_for() {  # wait_for <seconds> <description> <command...>
  local limit="$1" what="$2"; shift 2
  local i
  for ((i = 0; i < limit; i++)); do "$@" && return 0; sleep 1; done
  bad "timed out after ${limit}s waiting for: $what"
  return 1
}

# ----------------------------------------------------------------------------- start
do_start() {
  need_docker
  head1 "Building the image"
  "${DC[@]}" build uploader || { bad "build failed"; exit 1; }
  head1 "Starting MinIO, the bucket, the uploader and the fake recorder"
  "${DC[@]}" up -d || { bad "could not start the containers"; exit 1; }

  uploaded_enough() {
    local n; n="$(get "$(brief)" uploaded)"
    [[ "$n" =~ ^[0-9]+$ ]] && (( n >= 4 ))
  }
  say "Waiting for the first segments to be uploaded..."
  wait_for 120 "4 uploaded segments" uploaded_enough || { "${DC[@]}" logs --tail 30; exit 1; }

  good "Running."
  show
  cat <<MSG

  See the files:   http://localhost:9001   (user minioadmin / password minioadmin) -> bucket vivo-segments
  Status:          ./run.sh status
  Logs:            ./run.sh logs
  Failure demo:    ./run.sh demo
  Tests:           ./run.sh test
MSG
}

# ------------------------------------------------------------------------------ demo
do_demo() {
  need_docker
  local line
  line="$(brief)"
  if [[ -z "$line" ]] || [[ "$(get "$line" bucket_objects)" == "unreachable" ]]; then do_start; fi
  "${DC[@]}" start minio uploader recorder >/dev/null 2>&1   # make sure all three are running

  # The bucket may already hold objects from earlier runs (its volume survives restarts, the
  # state file may not). Remember how many, so the final comparison only counts this run.
  local base_up base_bucket offset=0
  line="$(brief)"; base_up="$(get "$line" uploaded)"; base_bucket="$(get "$line" bucket_objects)"
  if [[ "$base_up" =~ ^[0-9]+$ && "$base_bucket" =~ ^[0-9]+$ ]]; then offset=$(( base_bucket - base_up )); fi
  (( offset < 0 )) && offset=0

  head1 "Step 1 of 5: normal operation"
  say "   The recorder writes segments and the uploader sends them to MinIO. Local files are never deleted by the uploader."
  sleep 8
  show
  log_excerpt 'event=uploaded' 4

  head1 "Step 2 of 5: storage outage (MinIO is stopped)"
  "${DC[@]}" stop minio >/dev/null 2>&1
  sleep 12
  show
  note "bucket_objects=unreachable is expected. local_files keeps growing and nothing is lost."
  log_excerpt 'event=upload_failed' 3

  head1 "Step 3 of 5: the uploader process is killed (SIGKILL)"
  "${DC[@]}" kill uploader >/dev/null 2>&1
  sleep 4
  show
  note "The recorder keeps writing. The state file survives in its own volume."

  head1 "Step 4 of 5: storage is back, uploader restarted"
  "${DC[@]}" start minio >/dev/null 2>&1
  minio_up() { [[ "$(get "$(brief)" bucket_objects)" =~ ^[0-9]+$ ]]; }
  wait_for 60 "MinIO to answer" minio_up
  "${DC[@]}" start uploader >/dev/null 2>&1
  sleep 15
  show
  note "On start-up the uploader found the unfinished work in its state file:"
  log_excerpt 'event=startup' 1
  log_excerpt 'event=(already_present|uploaded)' 3

  head1 "Step 5 of 5: stop the recorder, let the backlog drain, compare"
  "${DC[@]}" stop recorder >/dev/null 2>&1
  drained() {
    local l; l="$(brief)"
    [[ "$(get "$l" pending)" == 0 && "$(get "$l" uploading)" == 0 ]]
  }
  wait_for 120 "the backlog to drain" drained
  line="$(brief)"
  local uploaded bucket conflict missing local_files dups
  uploaded="$(get "$line" uploaded)"; bucket="$(get "$line" bucket_objects)"
  conflict="$(get "$line" conflict)"; missing="$(get "$line" missing)"; local_files="$(get "$line" local_files)"
  dups="$("${DC[@]}" logs --no-color uploader 2>/dev/null | grep 'event=uploaded' \
          | sed 's/.* key=\([^ ]*\).*/\1/' | sort | uniq -d | wc -l | tr -d ' ')"

  say ""
  say "   segments recorded as uploaded : $uploaded"
  local expected_bucket=""
  [[ "$bucket" =~ ^[0-9]+$ ]] && expected_bucket=$(( bucket - offset ))
  say "   objects in the MinIO bucket   : $bucket"
  (( offset > 0 )) && say "   (of which $offset are older objects from earlier runs; not counted below)"
  say "   segments uploaded twice       : $dups"
  say "   conflicts / missing           : $conflict / $missing"
  say "   local files on disk           : $local_files  (the uploader never deletes them)"
  if [[ -n "$uploaded" && "$uploaded" == "$expected_bucket" && "$dups" == 0 && "$conflict" == 0 && "$missing" == 0 ]]; then
    good "RESULT: PASS - every segment is in the bucket exactly once, nothing was lost."
  else
    bad "RESULT: CHECK THE NUMBERS ABOVE and ./run.sh logs"
    return 1
  fi
  note "The recorder is stopped. Run ./run.sh start to start it again."
}

# ----------------------------------------------------------------------------- other
do_test() {
  need_docker
  mkdir -p logs
  head1 "Running the automated tests (stand-in S3 server and the real MinIO)"
  "${DC[@]}" --profile test build tests || { bad "build failed"; exit 1; }
  { echo "Command: ./run.sh test   ($(date -u +%Y-%m-%dT%H:%M:%SZ))"; echo; } > logs/docker-test-results.txt
  "${DC[@]}" --profile test run --rm -T tests 2>&1 | tee -a logs/docker-test-results.txt
  local rc="${PIPESTATUS[0]}"
  say ""
  if [[ "$rc" == 0 ]]; then good "Tests passed. Log: logs/docker-test-results.txt"; else bad "Tests FAILED. Log: logs/docker-test-results.txt"; fi
  return "$rc"
}

do_status() {
  need_docker
  "${DC[@]}" run --rm -T cli status --remote 2>/dev/null
}

case "${1:-start}" in
  start)  do_start ;;
  demo)   do_demo ;;
  test)   do_test ;;
  status) do_status ;;
  logs)   need_docker; "${DC[@]}" logs -f --tail 30 uploader recorder ;;
  stop)   need_docker; "${DC[@]}" --profile test --profile tools down ;;
  clean)  need_docker; "${DC[@]}" --profile test --profile tools down -v ;;
  *)      echo "usage: ./run.sh [start|demo|test|status|logs|stop|clean]" >&2; exit 2 ;;
esac
