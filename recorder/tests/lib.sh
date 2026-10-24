# Shared helpers for the shell check scripts. Source it: . "$(dirname "$0")/lib.sh"
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPORTS="$ROOT/reports"
ETC_DIR="${VIVO_ETC:-/etc/vivo}"
API="${FRIGATE_API:-http://127.0.0.1:5000/api}"

set -a
# shellcheck disable=SC1091
. "$ROOT/versions.env"
# shellcheck disable=SC1091
[[ -r "$ETC_DIR/site.env" ]] && . "$ETC_DIR/site.env"
set +a

ROWS=""
NPASS=0
NFAIL=0

# check <name> <command...>   records PASS/FAIL and the command's output as detail
check() {
  local name="$1"; shift
  local out rc
  out="$("$@" 2>&1)" && rc=0 || rc=$?
  out="$(printf '%s' "$out" | tr '\n' ' ' | sed 's/|/\\|/g' | cut -c1-160)"
  if (( rc == 0 )); then NPASS=$((NPASS + 1)); ROWS+="| PASS | $name | $out |"$'\n'
  else NFAIL=$((NFAIL + 1)); ROWS+="| **FAIL** | $name | $out |"$'\n'; fi
  printf '%-6s %s  %s\n' "$( ((rc==0)) && echo PASS || echo FAIL)" "$name" "$out"
}

# write_report <name> <title> [extra markdown]
write_report() {
  mkdir -p "$REPORTS"
  local file="$REPORTS/$1-$(date -u +%Y%m%d-%H%M%S).md"
  {
    echo "# $2"
    echo
    echo "Run on $(hostname) at $(date -u '+%Y-%m-%d %H:%M:%S UTC'), boot $(cat /proc/sys/kernel/random/boot_id 2>/dev/null | cut -c1-8), up $(uptime -p 2>/dev/null)."
    echo
    echo "Result: **$NPASS passed, $NFAIL failed**"
    echo
    echo "| Result | Check | Detail |"
    echo "|---|---|---|"
    printf '%s' "$ROWS"
    [[ $# -ge 3 ]] && printf '\n%s\n' "$3"
  } > "$file"
  echo "report: $file"
}

need_root() { [[ $EUID -eq 0 ]] || { echo "run with sudo" >&2; exit 1; }; }
