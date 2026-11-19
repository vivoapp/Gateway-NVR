#!/usr/bin/env bash
# Block C: install the Vivo Video API on a gateway that already runs the recorder (recorder/gateway/setup.sh).
#
# usage:
#   sudo gateway/setup.sh [--dry-run] [--only STEP[,STEP]]
#
# steps, in order:
#   preflight  root, Ubuntu, the recorder installed, Docker running, settings readable
#   settings   /etc/vivo/video-api.env (root, 600): a random Frigate password for the API's viewer
#              account, the go2rtc API login copied from secrets.env, limits, TURN (empty until set)
#   folders    data folder on the recording disk (clips, audit log), key and certificate folders
#   tls        a self-signed certificate for port 8443 (until Haris's M23 certificates)
#   frigate    Frigate's viewer account `video_api` (made or reset through Frigate's local API)
#   firewall   ufw: port 8443 from the LAN and admin networks only
#   units      vivo-video-api.service, vivo-live-gate.service, /run/vivo-live-gate (tmpfiles)
#   stack      install to /opt/vivo-video-api (owned by root), build the image, start, check /v1/health
#
# Safe to run again: every step checks what is already done. The Rails public keys
# (/etc/vivo/video-api-keys/rails.jwks.json) come from Vivo's backend; until they exist the API
# answers /v1/health and refuses every grant. For tests, tools/grant_issuer.py keygen makes test keys.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
INSTALL_DIR=/opt/vivo-video-api
RECORDER_DIR=/opt/vivo-gateway
ETC_DIR="${VIVO_ETC:-/etc/vivo}"       # VIVO_ETC: only for a dry run on a workstation
API_ENV="$ETC_DIR/video-api.env"
KEYS_DIR="$ETC_DIR/video-api-keys"
TLS_DIR="$ETC_DIR/video-api-tls"
API_UID=10001
PORT=8443
ALL_STEPS="preflight settings folders tls frigate firewall units stack"

DRY_RUN=0
ONLY=""

say()  { printf '\033[1m==> %s\033[0m\n' "$*"; }
ok()   { printf '    ok: %s\n' "$*"; }
warn() { printf '    WARN: %s\n' "$*" >&2; }
die()  { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
run()  { printf '    $ %s\n' "$*"; (( DRY_RUN )) || "$@"; }
# Write a file only if its content changed; prints what it does (never the content of a secret file).
put_file() { # <path> <mode> [secret] ; content on stdin
  local path="$1" mode="$2" secret="${3:-}" tmp
  tmp="$(mktemp)"; cat > "$tmp"
  if [[ -f "$path" ]] && cmp -s "$tmp" "$path"; then ok "$path unchanged"; rm -f "$tmp"; return 0; fi
  printf '    write %s (mode %s)\n' "$path" "$mode"
  if (( DRY_RUN )); then [[ -n "$secret" ]] || sed 's/^/      | /' "$tmp"; rm -f "$tmp"; return 0; fi
  install -D -m "$mode" "$tmp" "$path"; rm -f "$tmp"
}

while (( $# )); do
  case "$1" in
    --dry-run) DRY_RUN=1 ;;
    --only) ONLY="${2:?}"; shift ;;
    -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
    *) die "unknown option $1" ;;
  esac
  shift
done

load_settings() {
  set -a
  # shellcheck disable=SC1091
  . "$ETC_DIR/site.env"
  set +a
  : "${MEDIA_ROOT:?MEDIA_ROOT missing in $ETC_DIR/site.env}" "${GATEWAY_LAN_IP:?GATEWAY_LAN_IP missing}"
  : "${LAN_CIDRS:?LAN_CIDRS missing}" "${ADMIN_CIDRS:?ADMIN_CIDRS missing}"
  DATA_DIR="$MEDIA_ROOT/video-api"
}

env_value() { # <file> <NAME>: the value of NAME= in an env file (empty when the file or name is absent)
  [[ -f "$1" ]] || return 0
  sed -n "s/^$2=//p" "$1" | tail -n 1
}

# ---------------------------------------------------------------- preflight
step_preflight() {
  say "preflight"
  [[ $EUID -eq 0 ]] || (( DRY_RUN )) || die "run with sudo"
  # shellcheck disable=SC1091
  . /etc/os-release
  [[ "$ID" == ubuntu ]] || die "Ubuntu required (found $ID)"
  [[ -d "$RECORDER_DIR" && -f "$ETC_DIR/site.env" && -f "$ETC_DIR/secrets.env" ]] \
    || die "the recorder is not installed (run recorder/gateway/setup.sh first)"
  command -v docker >/dev/null && docker info >/dev/null 2>&1 || die "Docker is not running"
  load_settings
  [[ -f "$RECORDER_DIR/cameras.yaml" ]] || die "$RECORDER_DIR/cameras.yaml missing"
  ok "recorder at $RECORDER_DIR, MEDIA_ROOT=$MEDIA_ROOT, GATEWAY_LAN_IP=$GATEWAY_LAN_IP"
}

# ---------------------------------------------------------------- settings
step_settings() {
  say "settings ($API_ENV)"
  local frigate_pw go_user go_pw new defaults missing line
  local secrets_re='^(FRIGATE_PASSWORD|GO2RTC_API_USER|GO2RTC_API_PASSWORD)='
  frigate_pw="$(env_value "$API_ENV" FRIGATE_PASSWORD)"
  [[ -n "$frigate_pw" ]] || frigate_pw="$(openssl rand -hex 16)"
  # The API uses the recorder's go2rtc API login (shared-gateway.md: rotated together), so it is
  # copied again on every run.
  go_user="$(env_value "$ETC_DIR/secrets.env" GO2RTC_API_USER)"
  go_pw="$(env_value "$ETC_DIR/secrets.env" GO2RTC_API_PASSWORD)"
  [[ -n "$go_user" && -n "$go_pw" ]] || die "GO2RTC_API_USER/PASSWORD missing in $ETC_DIR/secrets.env"
  # The example file with this gateway's host name as GATEWAY_ID and without the secrets.
  defaults="$(sed -e "s/^GATEWAY_ID=.*/GATEWAY_ID=$(hostname -s)/" "$REPO/video-api.env.example" \
              | grep -v -E "$secrets_re" || true)"
  if [[ -f "$API_ENV" ]]; then
    # Keep every setting already in the file, and add the ones it lacks from the example. A file
    # left with only a comment (as when an earlier developer's access was closed) gets them all.
    new="$(grep -v -E "$secrets_re" "$API_ENV" || true)"
    missing=""
    while IFS= read -r line; do
      grep -q -E "^${line%%=*}=" "$API_ENV" || missing+="$line"$'\n'
    done < <(grep -E '^[A-Z0-9_]+=' <<<"$defaults")
    if [[ -n "$missing" ]]; then
      new+=$'\n'"# added by setup.sh from video-api.env.example"$'\n'"${missing%$'\n'}"
      ok "added missing settings: $(cut -d= -f1 <<<"${missing%$'\n'}" | paste -sd' ' -)"
    fi
  else
    new="$defaults"
  fi
  printf '%s\nFRIGATE_PASSWORD=%s\nGO2RTC_API_USER=%s\nGO2RTC_API_PASSWORD=%s\n' \
    "$new" "$frigate_pw" "$go_user" "$go_pw" | put_file "$API_ENV" 600 secret
  (( DRY_RUN )) || chown root:root "$API_ENV"
  ok "GATEWAY_ID=$(env_value "$API_ENV" GATEWAY_ID || true) (grants must name aud vivo-gw:<GATEWAY_ID>)"
}

# ---------------------------------------------------------------- folders
step_folders() {
  say "folders"
  run install -d -m 750 -o "$API_UID" -g "$API_UID" "$DATA_DIR" "$DATA_DIR/clips" "$DATA_DIR/audit"
  run install -d -m 755 -o root -g root "$KEYS_DIR"
  run install -d -m 750 -o root -g "$API_UID" "$TLS_DIR"
  if [[ -f "$KEYS_DIR/rails.jwks.json" ]]; then ok "Rails public keys present"
  else warn "$KEYS_DIR/rails.jwks.json missing: every grant is refused until Vivo's backend provides it"; fi
}

# ---------------------------------------------------------------- tls
step_tls() {
  say "tls (port $PORT)"
  if [[ -s "$TLS_DIR/cert.pem" && -s "$TLS_DIR/key.pem" ]] \
     && openssl x509 -in "$TLS_DIR/cert.pem" -noout -checkend $((30 * 86400)) >/dev/null 2>&1 \
     && openssl x509 -in "$TLS_DIR/cert.pem" -noout -ext subjectAltName 2>/dev/null | grep -q "IP Address:$GATEWAY_LAN_IP"; then
    ok "certificate valid for $GATEWAY_LAN_IP for at least 30 more days"
  else
    run openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes -days 825 \
      -subj "/CN=vivo-video-api $(hostname -s)" -addext "subjectAltName=IP:$GATEWAY_LAN_IP,IP:127.0.0.1" \
      -keyout "$TLS_DIR/key.pem" -out "$TLS_DIR/cert.pem"
  fi
  run chown root:"$API_UID" "$TLS_DIR/key.pem" "$TLS_DIR/cert.pem"
  run chmod 640 "$TLS_DIR/key.pem"
  run chmod 644 "$TLS_DIR/cert.pem"
  (( DRY_RUN )) || ok "fingerprint $(openssl x509 -in "$TLS_DIR/cert.pem" -noout -fingerprint -sha256 | cut -d= -f2)"
}

# ---------------------------------------------------------------- frigate
frigate_login() { # <user> <password>: true when that account logs in on Frigate's authenticated port.
  # The password goes to curl on stdin, so it never shows in the process list.
  printf '{"user":"%s","password":"%s"}' "$1" "$2" \
    | curl -sk -o /dev/null -w '%{http_code}' -X POST https://127.0.0.1:8971/api/login \
        -H 'Content-Type: application/json' --data-binary @- | grep -q '^200$'
}

step_frigate() {
  say "frigate (viewer account video_api)"
  local user pw users
  user="$(env_value "$API_ENV" FRIGATE_USER)"; user="${user:-video_api}"
  pw="$(env_value "$API_ENV" FRIGATE_PASSWORD)"
  [[ -n "$pw" ]] || (( DRY_RUN )) || die "run the settings step first"
  if (( DRY_RUN )); then printf '    (would make or reset Frigate user %s, role viewer)\n' "$user"; return 0; fi
  # Already working? Log in on the authenticated port as that user.
  if frigate_login "$user" "$pw"; then
    ok "Frigate user $user logs in"
    return 0
  fi
  # Frigate's local port (127.0.0.1:5000, no login) manages users; the viewer role cannot.
  users="$(curl -sf http://127.0.0.1:5000/api/users)" || die "Frigate's local API is not answering"
  if grep -q "\"username\":\"$user\"" <<<"$users"; then
    curl -sf -X DELETE "http://127.0.0.1:5000/api/users/$user" >/dev/null || die "could not reset Frigate user $user"
  fi
  printf '{"username":"%s","password":"%s","role":"viewer"}' "$user" "$pw" \
    | curl -sf -X POST http://127.0.0.1:5000/api/users -H 'Content-Type: application/json' --data-binary @- >/dev/null \
    || die "could not make Frigate user $user"
  sleep 2
  frigate_login "$user" "$pw" || die "Frigate user $user was made but cannot log in"
  ok "Frigate user $user (viewer) made and logs in"
}

# ---------------------------------------------------------------- firewall
step_firewall() {
  say "firewall (port $PORT from the LAN and admin networks)"
  local c
  for c in $(printf '%s,%s' "$LAN_CIDRS" "$ADMIN_CIDRS" | tr ',' '\n' | sed '/^$/d' | sort -u); do
    run ufw allow from "$c" to any port "$PORT" proto tcp comment 'vivo video api (Block C)'
  done
}

# ---------------------------------------------------------------- units
step_units() {
  say "units"
  local unit
  for unit in vivo-video-api.service vivo-live-gate.service; do
    sed -e "s#@INSTALL_DIR@#$INSTALL_DIR#g" -e "s#@MEDIA_ROOT@#$MEDIA_ROOT#g" \
      "$REPO/gateway/files/$unit" | put_file "/etc/systemd/system/$unit" 644
  done
  put_file /etc/tmpfiles.d/vivo-live-gate.conf 644 < "$REPO/gateway/files/vivo-live-gate.tmpfiles"
  run systemd-tmpfiles --create /etc/tmpfiles.d/vivo-live-gate.conf
  run systemctl daemon-reload
  run systemctl enable vivo-live-gate.service vivo-video-api.service
}

# ---------------------------------------------------------------- stack
step_stack() {
  say "stack ($INSTALL_DIR)"
  run install -d -m 755 "$INSTALL_DIR"
  # Owned by root: vivo-live-gate runs gateway/live_gate.py from here as root.
  run rsync -a --delete --chown=root:root --chmod=go-w --exclude .git --exclude .venv --exclude lab \
    --exclude reports --exclude __pycache__ --exclude .pytest_cache "$REPO/" "$INSTALL_DIR/"
  run docker compose --project-directory "$INSTALL_DIR" --env-file "$ETC_DIR/site.env" build
  run systemctl restart vivo-live-gate.service
  run systemctl restart vivo-video-api.service
  (( DRY_RUN )) && return 0
  local i
  for i in $(seq 1 30); do
    if curl -sk "https://127.0.0.1:$PORT/v1/health" | grep -q '"ok"'; then ok "https://$GATEWAY_LAN_IP:$PORT/v1/health answers"; return 0; fi
    sleep 1
  done
  docker compose --project-directory "$INSTALL_DIR" logs --tail 30 video-api || true
  die "the Video API did not answer on port $PORT"
}

# ---------------------------------------------------------------- main
steps="${ONLY//,/ }"; steps="${steps:-$ALL_STEPS}"
for s in $steps; do [[ " $ALL_STEPS " == *" $s "* ]] || die "unknown step '$s'"; done
(( DRY_RUN )) && say "DRY RUN: nothing will be changed"
[[ " $steps " == *" preflight "* ]] || load_settings
for s in $steps; do "step_$s"; done
say "done: $steps"
