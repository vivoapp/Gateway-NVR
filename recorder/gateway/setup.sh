#!/usr/bin/env bash
# M1: take a fresh Ubuntu Server 24.04 LTS install (x86_64) to a ready Vivo gateway.
#
# usage:
#   sudo gateway/setup.sh [--dry-run] [--only STEP[,STEP]] [--site FILE]
#                         [--format-disk /dev/XXX | --create-lv SIZE] [--add-disk /dev/XXX] [--yes]
#                         [--allow-password-lockout]
#
# steps, in order:
#   preflight  OS, architecture and settings files (--site installs a reviewed site.env)
#   packages   base packages (chrony, ufw, smartmontools, VAAPI tools, ...)
#   kernel     Ubuntu HWE kernel (N150 graphics need it); the first run asks for one reboot
#   time       UTC system clock, chrony NTP (also serves time to the cameras on LAN_CIDRS)
#   docker     Docker Engine + Compose at the versions pinned in versions.env
#   disk       recording disk mounted at MEDIA_ROOT (ext4, by UUID, nofail); --create-lv SIZE
#              makes it a new LVM volume next to the OS instead (no separate disk); --add-disk
#              /dev/XXX ERASES that disk and grows the LVM recording volume onto it
#   ssh        SSH by key only, password login off
#   firewall   deny inbound except LAN/admin networks, including Docker ports
#   stack      install to /opt/vivo-gateway, systemd unit, pull and start
#
# Safe to run again: every step checks what is already done.
# --format-disk ERASES the given disk; it asks for a typed confirmation unless --yes.
# The ssh step refuses to run while another account logs in by password and has no SSH key
# (it would lose SSH access); --allow-password-lockout overrides that after you have checked.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
INSTALL_DIR=/opt/vivo-gateway
ETC_DIR=/etc/vivo
ALL_STEPS="preflight packages kernel time docker disk ssh firewall stack"

DRY_RUN=0
ONLY=""
SITE_FILE=""
FORMAT_DISK=""
CREATE_LV=""
ADD_DISK=""
LV_NAME=vivo-media
ASSUME_YES=0
ALLOW_LOCKOUT=0
REBOOT_NEEDED=0

say()  { printf '\033[1m==> %s\033[0m\n' "$*"; }
ok()   { printf '    ok: %s\n' "$*"; }
warn() { printf '    WARN: %s\n' "$*" >&2; }
die()  { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
run()  { printf '    $ %s\n' "$*"; (( DRY_RUN )) || "$@"; }
# Write a file only if its content changed; prints what it does.
put_file() { # <path> <mode> ; content on stdin
  local path="$1" mode="$2" tmp
  tmp="$(mktemp)"; cat > "$tmp"
  if [[ -f "$path" ]] && cmp -s "$tmp" "$path"; then ok "$path unchanged"; rm -f "$tmp"; return 0; fi
  printf '    write %s (mode %s)\n' "$path" "$mode"
  if (( DRY_RUN )); then sed 's/^/      | /' "$tmp"; rm -f "$tmp"; return 0; fi
  install -D -m "$mode" "$tmp" "$path"; rm -f "$tmp"
}

while (( $# )); do
  case "$1" in
    --dry-run) DRY_RUN=1 ;;
    --only) ONLY="${2:?}"; shift ;;
    --site) SITE_FILE="${2:?}"; shift ;;
    --format-disk) FORMAT_DISK="${2:?}"; shift ;;
    --create-lv) CREATE_LV="${2:?}"; shift ;;
    --add-disk) ADD_DISK="${2:?}"; shift ;;
    --yes) ASSUME_YES=1 ;;
    --allow-password-lockout) ALLOW_LOCKOUT=1 ;;
    -h|--help) sed -n '2,25p' "$0"; exit 0 ;;
    *) die "unknown option $1" ;;
  esac
  shift
done

load_settings() {
  set -a
  # shellcheck disable=SC1091
  . "$REPO/versions.env"
  # shellcheck disable=SC1090
  . "${SITE_FILE:-$ETC_DIR/site.env}"   # --site: same content, and readable in a dry run too
  set +a
  : "${MEDIA_ROOT:?MEDIA_ROOT missing in $ETC_DIR/site.env}"
  : "${LAN_CIDRS:?LAN_CIDRS missing}" "${ADMIN_CIDRS:?ADMIN_CIDRS missing}" "${GATEWAY_LAN_IP:?GATEWAY_LAN_IP missing}"
}

admin_user() { echo "${SUDO_USER:-$(logname 2>/dev/null || echo root)}"; }

# ---------------------------------------------------------------- preflight
step_preflight() {
  say "preflight"
  [[ $EUID -eq 0 ]] || (( DRY_RUN )) || die "run with sudo"
  # shellcheck disable=SC1091
  . /etc/os-release
  [[ "$ID" == ubuntu ]] || die "Ubuntu required (found $ID)"
  [[ "$VERSION_ID" == "$(grep -oP '^UBUNTU_VERSION_ID=\K.*' "$REPO/versions.env")" ]] \
    || die "Ubuntu $VERSION_ID found; this baseline is pinned to 24.04"
  [[ "$(uname -m)" == x86_64 ]] || die "x86_64 required (found $(uname -m))"
  ok "Ubuntu $VERSION_ID $(uname -m), kernel $(uname -r)"

  if [[ -n "$SITE_FILE" ]]; then
    [[ -f "$SITE_FILE" ]] || die "--site $SITE_FILE: no such file"
    run install -d -m 755 "$ETC_DIR"
    put_file "$ETC_DIR/site.env" 644 < "$SITE_FILE"
  fi
  if [[ ( ! -f "$ETC_DIR/site.env" && -z "$SITE_FILE" ) || ! -f "$ETC_DIR/secrets.env" ]]; then
    run install -d -m 755 "$ETC_DIR"
    [[ -f "$ETC_DIR/site.env" || -n "$SITE_FILE" ]] || run install -m 644 "$REPO/site.env.example" "$ETC_DIR/site.env"
    [[ -f "$ETC_DIR/secrets.env" ]] || run install -m 600 "$REPO/secrets.env.example" "$ETC_DIR/secrets.env"
    die "created $ETC_DIR/site.env and secrets.env from the examples; fill them in, then run again"
  fi
  [[ "$(stat -c %a "$ETC_DIR/secrets.env")" == 600 ]] || run chmod 600 "$ETC_DIR/secrets.env"
  grep -q 'change-me' "$ETC_DIR/secrets.env" && die "$ETC_DIR/secrets.env still has change-me values"
  load_settings
  ok "settings loaded (MEDIA_ROOT=$MEDIA_ROOT, GATEWAY_LAN_IP=$GATEWAY_LAN_IP)"
  if [[ -e /dev/dri/renderD128 ]]; then ok "Intel GPU render node present"; else warn "/dev/dri/renderD128 missing: no Quick Sync yet (the kernel step fixes this, see docs/findings.md F3)"; fi
}

# ---------------------------------------------------------------- packages
step_packages() {
  say "packages"
  run apt-get update -q
  run env DEBIAN_FRONTEND=noninteractive apt-get install -y -q --no-install-recommends \
    ca-certificates curl gnupg jq rsync python3-yaml gdisk \
    chrony ufw smartmontools nvme-cli lm-sensors \
    vainfo intel-media-va-driver intel-gpu-tools
}

# ---------------------------------------------------------------- kernel
# The 24.04 GA kernel (6.8) has no i915 ID for the N150 graphics (8086:46d4), so there is no
# /dev/dri/renderD128 and no Quick Sync. The HWE kernel has it. The old kernel stays installed
# and can be picked under "Advanced options" in the GRUB menu if the new one ever fails to boot.
newest_kernel() { find /boot -maxdepth 1 -name 'vmlinuz-*' -printf '%f\n' | sed 's/^vmlinuz-//' | sort -V | tail -1; }

step_kernel() {
  say "kernel ($KERNEL_PACKAGE)"
  if dpkg-query -W -f='${Status}' "$KERNEL_PACKAGE" 2>/dev/null | grep -q 'install ok installed'; then
    ok "$KERNEL_PACKAGE installed"
  else
    run apt-get update -q
    run env DEBIAN_FRONTEND=noninteractive apt-get install -y -q "$KERNEL_PACKAGE"
    if (( DRY_RUN )); then ok "after the install: reboot into the new kernel"; return 0; fi
  fi
  local newest; newest="$(newest_kernel)"
  if [[ "$(uname -r)" != "$newest" ]]; then
    REBOOT_NEEDED=1
    warn "running kernel $(uname -r), newest installed $newest: reboot needed"
  elif [[ -e /dev/dri/renderD128 ]]; then
    ok "kernel $(uname -r), Intel GPU render node present"
  else
    die "kernel $(uname -r) is the newest installed, but /dev/dri/renderD128 is still missing (see docs/findings.md F3)"
  fi
}

# ---------------------------------------------------------------- time
step_time() {
  say "time (UTC + chrony)"
  run timedatectl set-timezone Etc/UTC
  local servers=""
  for s in ${NTP_SERVERS:-}; do servers+="server $s iburst"$'\n'; done
  if [[ -n "$servers" ]]; then
    put_file /etc/chrony/sources.d/vivo.sources 644 <<<"${servers%$'\n'}"
  fi
  # Serve time to the cameras: they often cannot reach the internet, and recordings from
  # cameras with wrong clocks are hard to line up (M12). Allowed through ufw in the firewall step.
  local allow="" c
  for c in ${LAN_CIDRS//,/ }; do allow+="allow $c"$'\n'; done
  put_file /etc/chrony/conf.d/vivo-server.conf 644 <<<"# Vivo: NTP server for the cameras on the LAN (managed by gateway/setup.sh)
${allow%$'\n'}"
  run systemctl enable chrony
  run systemctl restart chrony
  (( DRY_RUN )) || chronyc -a 'burst 4/4' >/dev/null 2>&1 || true
  ok "clock in UTC; check sync with: chronyc tracking"
}

# ---------------------------------------------------------------- docker
step_docker() {
  say "docker (pinned)"
  if [[ ! -f /etc/apt/keyrings/docker.asc ]]; then
    run install -d -m 755 /etc/apt/keyrings
    run curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
    run chmod a+r /etc/apt/keyrings/docker.asc
  fi
  put_file /etc/apt/sources.list.d/docker.list 644 <<EOF
deb [arch=amd64 signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu noble stable
EOF
  run apt-get update -q
  run sh -c 'apt-mark unhold docker-ce docker-ce-cli containerd.io docker-compose-plugin >/dev/null 2>&1 || true'
  run env DEBIAN_FRONTEND=noninteractive apt-get install -y -q --allow-downgrades \
    "docker-ce=$DOCKER_CE_VERSION" "docker-ce-cli=$DOCKER_CE_VERSION" \
    "containerd.io=$CONTAINERD_VERSION" "docker-compose-plugin=$COMPOSE_PLUGIN_VERSION"
  run apt-mark hold docker-ce docker-ce-cli containerd.io docker-compose-plugin
  put_file /etc/docker/daemon.json 644 < "$REPO/gateway/files/daemon.json"

  # Start Docker after the recording disk is mounted (but still start without it,
  # so go2rtc live view keeps working and Frigate reports the missing disk).
  local mount_unit; mount_unit="$(systemd-escape -p --suffix=mount "$MEDIA_ROOT")"
  put_file /etc/systemd/system/docker.service.d/10-vivo-media.conf 644 <<EOF
[Unit]
After=$mount_unit
EOF
  run systemctl daemon-reload
  run systemctl enable --now docker
  (( DRY_RUN )) || ok "$(docker --version); $(docker compose version)"
}

# ---------------------------------------------------------------- disk
step_disk() {
  say "recording disk at $MEDIA_ROOT"
  local rootdev fresh=0; rootdev="$(findmnt -no SOURCE /)"
  # --create-lv: a new logical volume in the OS volume group, for gateways without a separate
  # recording disk. It erases nothing that exists, so it needs no typed confirmation.
  if [[ -n "$CREATE_LV" ]]; then
    [[ -z "$FORMAT_DISK" ]] || die "--create-lv and --format-disk cannot be combined"
    local vg; vg="$(lvs --noheadings -o vg_name "$rootdev" 2>/dev/null | tr -d ' ' || true)"
    [[ -n "$vg" ]] || die "--create-lv needs the OS on LVM (/ is on $rootdev)"
    lvs "$vg/$LV_NAME" >/dev/null 2>&1 && die "$vg/$LV_NAME already exists; run without --create-lv to keep its recordings"
    ok "volume group $vg has $(vgs --noheadings --units g -o vg_free "$vg" | tr -d ' ') free"
    run lvcreate --yes --wipesignatures y -n "$LV_NAME" -L "$CREATE_LV" "$vg"
    FORMAT_DISK="/dev/$vg/$LV_NAME"; fresh=1
  fi
  if [[ -n "$FORMAT_DISK" ]]; then
    if [[ -b "$FORMAT_DISK" ]]; then
      lsblk -no PATH "$FORMAT_DISK" | grep -qxF "$rootdev" && die "$FORMAT_DISK holds the OS; refusing"
      lsblk -no MOUNTPOINT "$FORMAT_DISK" | grep -q . && die "$FORMAT_DISK has mounted partitions; unmount first"
      lsblk -o NAME,SIZE,MODEL,FSTYPE "$FORMAT_DISK"
    elif (( ! (fresh && DRY_RUN) )); then
      die "$FORMAT_DISK is not a block device"
    fi
    if (( ! ASSUME_YES && ! fresh && ! DRY_RUN )); then
      read -r -p "Type ERASE $FORMAT_DISK to wipe it: " answer
      [[ "$answer" == "ERASE $FORMAT_DISK" ]] || die "not confirmed"
    fi
    run wipefs -a "$FORMAT_DISK"
    # ext4: journaled, mature fsck tooling, safe after power loss. No reserved blocks.
    run mkfs.ext4 -F -L vivo-media -m 0 "$FORMAT_DISK"
  fi

  local uuid line
  if [[ -n "$FORMAT_DISK" ]]; then
    uuid="$( (( DRY_RUN )) && echo DRYRUN-UUID || blkid -s UUID -o value "$FORMAT_DISK")"
  else
    uuid="$(awk -v m="$MEDIA_ROOT" '$2==m && $1 ~ /^UUID=/ {sub("UUID=","",$1); print $1}' /etc/fstab)"
    [[ -n "$uuid" ]] || die "no fstab entry for $MEDIA_ROOT; run with --format-disk /dev/XXX (or add one by UUID)"
  fi
  line="UUID=$uuid $MEDIA_ROOT ext4 defaults,noatime,nofail,x-systemd.device-timeout=15s 0 2"
  if ! grep -qxF "$line" /etc/fstab; then
    # Replace any older entry for MEDIA_ROOT (e.g. a replaced or re-formatted disk), then add ours.
    printf '    fstab: %s\n' "$line"
    if (( ! DRY_RUN )); then
      cp -a /etc/fstab "/etc/fstab.vivo-backup.$(date +%s)"
      awk -v m="$MEDIA_ROOT" '$2!=m' /etc/fstab > /etc/fstab.vivo-new
      echo "$line" >> /etc/fstab.vivo-new
      mv /etc/fstab.vivo-new /etc/fstab
    fi
  fi
  ok "fstab entry uses UUID=$uuid with nofail"

  # The mount point itself is made immutable while nothing is mounted on it, so if
  # the disk is ever missing, nothing (Frigate included) can write to the OS disk.
  if ! mountpoint -q "$MEDIA_ROOT"; then
    run mkdir -p "$MEDIA_ROOT"
    if [[ -z "$(ls -A "$MEDIA_ROOT" 2>/dev/null)" ]]; then run chattr +i "$MEDIA_ROOT"; fi
    run systemctl daemon-reload
    run mount "$MEDIA_ROOT"
  fi
  (( DRY_RUN )) || mountpoint -q "$MEDIA_ROOT" || die "$MEDIA_ROOT is not mounted"
  run mkdir -p "$MEDIA_ROOT/frigate/state" "$MEDIA_ROOT/frigate/media"

  # --add-disk: more recording space on the same path. The disk joins the recording volume's
  # LVM group and the volume (and its ext4) grows onto that disk only, while mounted. If either
  # disk fails, the recordings on the volume are lost; the OS keeps running.
  if [[ -n "$ADD_DISK" ]]; then
    [[ -b "$ADD_DISK" ]] || die "$ADD_DISK is not a block device"
    local src vg lv free
    src="$(findmnt -no SOURCE "$MEDIA_ROOT" 2>/dev/null || true)"
    vg="$(lvs --noheadings -o vg_name "$src" 2>/dev/null | tr -d ' ' || true)"
    lv="$(lvs --noheadings -o lv_name "$src" 2>/dev/null | tr -d ' ' || true)"
    [[ -n "$vg" && -n "$lv" ]] || die "--add-disk needs the recording volume on LVM ($MEDIA_ROOT is on ${src:-nothing})"
    if pvs --noheadings -o vg_name "$ADD_DISK" 2>/dev/null | grep -qw "$vg"; then
      ok "$ADD_DISK is already in $vg"
    else
      lsblk -no PATH "$ADD_DISK" | grep -qxF "$rootdev" && die "$ADD_DISK holds the OS; refusing"
      lsblk -no MOUNTPOINT "$ADD_DISK" | grep -q . && die "$ADD_DISK has mounted partitions; unmount first"
      lsblk -o NAME,SIZE,MODEL,FSTYPE "$ADD_DISK"
      if (( ! ASSUME_YES && ! DRY_RUN )); then
        read -r -p "Type ERASE $ADD_DISK to wipe it: " answer
        [[ "$answer" == "ERASE $ADD_DISK" ]] || die "not confirmed"
      fi
      run sgdisk --zap-all "$ADD_DISK"
      run wipefs -a "$ADD_DISK"
      run pvcreate --yes "$ADD_DISK"
      run vgextend "$vg" "$ADD_DISK"
    fi
    free="$(pvs --noheadings -o pv_free --units b --nosuffix "$ADD_DISK" 2>/dev/null | tr -d ' ' || true)"
    if (( DRY_RUN )) || [[ "${free:-0}" -gt 0 ]]; then
      run lvextend --resizefs -l +100%PVS "$vg/$lv" "$ADD_DISK"
    else
      ok "$vg/$lv already uses all of $ADD_DISK"
    fi
  fi
  (( DRY_RUN )) || ok "$(findmnt -no SOURCE,FSTYPE,SIZE,OPTIONS "$MEDIA_ROOT")"
}

# ---------------------------------------------------------------- ssh
step_ssh() {
  say "ssh (key only)"
  local user home keys
  user="$(admin_user)"; home="$(getent passwd "$user" | cut -d: -f6)"; keys="$home/.ssh/authorized_keys"
  [[ -s "$keys" ]] || die "$keys is empty: add your public key first, or you would be locked out"
  ok "$user has $(grep -c . "$keys") authorized key(s)"
  # Other people's accounts: anyone who logs in by password and has no key would lose SSH access.
  local u uid h shell st stranded=""
  while IFS=: read -r u _ uid _ _ h shell; do
    (( uid >= 1000 && uid < 65000 )) || continue
    [[ "$shell" == */nologin || "$shell" == */false ]] && continue
    st="$(passwd -S "$u" 2>/dev/null | awk '{print $2}')"
    [[ "$st" == P && ! -s "$h/.ssh/authorized_keys" ]] && stranded+=" $u"
  done < <(getent passwd)
  if [[ -n "$stranded" ]]; then
    (( ALLOW_LOCKOUT )) || die "these accounts log in by password and have no SSH key:$stranded. They would lose SSH access (the local console still works). Add a key for each, or run again with --allow-password-lockout"
    warn "password-only accounts lose SSH access:$stranded (local console still works)"
  fi
  put_file /etc/ssh/sshd_config.d/10-vivo.conf 644 < "$REPO/gateway/files/sshd-vivo.conf"
  run sshd -t
  run systemctl try-reload-or-restart ssh
  (( DRY_RUN )) || sshd -T | grep -E '^(passwordauthentication|kbdinteractiveauthentication|permitrootlogin|pubkeyauthentication) ' | sed 's/^/    /'
}

# ---------------------------------------------------------------- firewall
cidr_contains() { # <ip> <comma-separated cidrs>
  python3 - "$1" "$2" <<'PY'
import ipaddress, sys
ip = ipaddress.ip_address(sys.argv[1])
sys.exit(0 if any(ip in ipaddress.ip_network(c.strip(), strict=False) for c in sys.argv[2].split(",") if c.strip()) else 1)
PY
}

step_firewall() {
  say "firewall (ufw + Docker ports)"
  # Never cut an SSH session that is open now, ours included. sudo drops SSH_CONNECTION, so the
  # open connections on port 22 are read from the kernel as well.
  local client clients
  clients="$( { [[ -n "${SSH_CONNECTION:-}" ]] && echo "${SSH_CONNECTION%% *}"
               ss -Htn state established '( sport = :22 )' 2>/dev/null | awk '{print $4}' \
                 | sed -E 's/:[0-9]+$//; s/^\[//; s/\]$//; s/^::ffff://'; } | sort -u)"
  for client in $clients; do
    cidr_contains "$client" "$ADMIN_CIDRS" \
      || die "SSH client $client (connected now) is not inside ADMIN_CIDRS=$ADMIN_CIDRS; fix site.env first"
  done
  ok "open SSH sessions inside ADMIN_CIDRS: ${clients//$'\n'/ }"
  run ufw default deny incoming
  run ufw default allow outgoing
  local c
  for c in ${ADMIN_CIDRS//,/ }; do run ufw allow from "$c" to any port 22 proto tcp comment 'vivo ssh'; done
  for c in ${LAN_CIDRS//,/ }; do run ufw allow from "$c" to any port 123 proto udp comment 'vivo ntp for cameras'; done

  # Docker-published ports skip ufw's INPUT rules, so they are filtered in DOCKER-USER.
  local rules="" nets
  nets="$(printf '%s,%s' "$LAN_CIDRS" "$ADMIN_CIDRS" | tr ',' '\n' | sed '/^$/d' | sort -u)"
  for c in $nets; do rules+="-A DOCKER-USER -s $c -j RETURN"$'\n'; done
  local block
  block="$(sed -e "/@ALLOWED_NETWORKS@/{r /dev/stdin" -e 'd}' "$REPO/gateway/files/ufw-docker-user.rules" <<<"${rules%$'\n'}")"
  local after=/etc/ufw/after.rules new
  new="$(sed '/^# BEGIN VIVO DOCKER-USER$/,/^# END VIVO DOCKER-USER$/d' "$after" 2>/dev/null || true)"
  printf '%s\n%s\n' "$new" "$block" | put_file "$after" 640
  run ufw --force enable
  run ufw reload
  (( DRY_RUN )) || ufw status verbose | sed 's/^/    /'
}

# ---------------------------------------------------------------- stack
step_stack() {
  say "stack (go2rtc + Frigate under systemd)"
  # compose.yaml passes /dev/dri/renderD128 to Frigate, so it cannot start without it.
  if [[ ! -e /dev/dri/renderD128 ]]; then
    if (( REBOOT_NEEDED )); then warn "skipped until after the reboot (no /dev/dri/renderD128 yet)"; return 0; fi
    (( DRY_RUN )) || die "/dev/dri/renderD128 missing: Frigate cannot start (run the kernel step, reboot, see docs/findings.md F3)"
    warn "/dev/dri/renderD128 missing: the real run stops here until the kernel step and a reboot"
  fi
  run python3 "$REPO/tools/gen_config.py" --check
  run install -d -m 755 "$INSTALL_DIR"
  run rsync -a --delete --exclude .git --exclude reports "$REPO/" "$INSTALL_DIR/"
  run ln -sf "$INSTALL_DIR/bin/vivo-compose" /usr/local/bin/vivo-compose
  local mount_unit; mount_unit="$(systemd-escape -p --suffix=mount "$MEDIA_ROOT")"
  sed -e "s#@INSTALL_DIR@#$INSTALL_DIR#g" -e "s#@MEDIA_MOUNT@#$mount_unit#g" \
    "$REPO/gateway/files/vivo-gateway.service" | put_file /etc/systemd/system/vivo-gateway.service 644
  run systemctl daemon-reload
  run systemctl enable vivo-gateway.service
  run "$INSTALL_DIR/bin/vivo-compose" pull
  run systemctl restart vivo-gateway.service
  (( DRY_RUN )) || "$INSTALL_DIR/bin/vivo-compose" ps
}

# ---------------------------------------------------------------- main
steps="${ONLY//,/ }"; steps="${steps:-$ALL_STEPS}"
for s in $steps; do [[ " $ALL_STEPS " == *" $s "* ]] || die "unknown step '$s'"; done
(( DRY_RUN )) && say "DRY RUN: nothing will be changed"
[[ " $steps " == *" preflight "* ]] || load_settings
for s in $steps; do "step_$s"; done
say "done: $steps"
if (( REBOOT_NEEDED )); then
  say "REBOOT NEEDED: run 'sudo reboot', then the same setup.sh command again to finish"
fi
