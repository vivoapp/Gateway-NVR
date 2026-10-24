#!/usr/bin/env bash
# M1 acceptance check: Linux gateway baseline.
# Run after setup.sh, and again after a reboot to prove everything comes back alone.
# usage: sudo tests/m1_check.sh
. "$(dirname "$0")/lib.sh"
need_root

os_ok() { . /etc/os-release; [[ "$ID $VERSION_ID $(uname -m)" == "ubuntu $UBUNTU_VERSION_ID x86_64" ]] && echo "$PRETTY_NAME $(uname -m) kernel $(uname -r)"; }
pkg_is() { local v; v="$(dpkg-query -W -f='${Version}' "$1" 2>/dev/null)"; echo "$1 $v"; [[ "$v" == "$2" ]]; }
kernel_ok() {
  dpkg-query -W -f='${Status}' "$KERNEL_PACKAGE" 2>/dev/null | grep -q 'install ok installed' || { echo "$KERNEL_PACKAGE not installed"; return 1; }
  local newest; newest="$(find /boot -maxdepth 1 -name 'vmlinuz-*' -printf '%f\n' | sed 's/^vmlinuz-//' | sort -V | tail -1)"
  echo "$KERNEL_PACKAGE installed, running $(uname -r), newest $newest"; [[ "$(uname -r)" == "$newest" ]]
}
held() { local h; h="$(apt-mark showhold)"; for p in docker-ce docker-ce-cli containerd.io docker-compose-plugin; do grep -qx "$p" <<<"$h" || { echo "$p not held"; return 1; }; done; echo "all held"; }
unit_ok() { systemctl is-enabled "$1" >/dev/null && systemctl is-active "$1" >/dev/null && echo "$1 enabled and active"; }
mount_ok() {
  mountpoint -q "$MEDIA_ROOT" || { echo "$MEDIA_ROOT not mounted"; return 1; }
  local line; line="$(awk -v m="$MEDIA_ROOT" '$2==m' /etc/fstab)"
  [[ "$line" == UUID=* && "$line" == *nofail* ]] || { echo "fstab: $line"; return 1; }
  echo "$(findmnt -no SOURCE,FSTYPE,SIZE "$MEDIA_ROOT"), fstab by UUID with nofail"
}
mountpoint_immutable() {
  # Look under the mount: the bare mount point on the OS disk must be immutable.
  local d; d="$(mktemp -d)"; mount --bind -o ro / "$d"
  local attrs; attrs="$(lsattr -d "$d$MEDIA_ROOT" 2>&1 | cut -d' ' -f1)"; umount "$d"; rmdir "$d"
  echo "underlying $MEDIA_ROOT attrs: $attrs"; [[ "$attrs" == *i* ]]
}
gpu_ok() {
  [[ -e /dev/dri/renderD128 ]] || { echo "no /dev/dri/renderD128"; return 1; }
  local p; p="$(vainfo --display drm --device /dev/dri/renderD128 2>&1 | grep -E 'VAProfileH264(Main|High).*VAEntrypointVLD' | wc -l)"
  echo "renderD128 present, VAAPI H.264 decode profiles: $p"; (( p > 0 ))
}
time_ok() {
  local tz sync; tz="$(timedatectl show -p Timezone --value)"; sync="$(timedatectl show -p NTPSynchronized --value)"
  local off; off="$(chronyc tracking | awk -F': ' '/^System time/ {print $2}')"
  echo "timezone $tz, synchronized $sync, offset $off"; [[ "$tz" == Etc/UTC || "$tz" == UTC ]] && [[ "$sync" == yes ]]
}
ssh_ok() {
  local t; t="$(sshd -T 2>/dev/null)"
  for kv in "passwordauthentication no" "kbdinteractiveauthentication no" "permitrootlogin no" "pubkeyauthentication yes"; do
    grep -qx "$kv" <<<"$t" || { echo "missing: $kv"; return 1; }
  done; echo "key only, no passwords, no root login"
}
fw_ok() {
  ufw status verbose | grep -q '^Status: active' || { echo "ufw inactive"; return 1; }
  ufw status verbose | grep -q 'deny (incoming)' || { echo "default incoming not deny"; return 1; }
  local last; last="$(iptables -S DOCKER-USER | tail -1)"
  [[ "$last" == "-A DOCKER-USER -j DROP" ]] || { echo "DOCKER-USER last rule: $last"; return 1; }
  echo "ufw active, deny incoming, DOCKER-USER ends in DROP ($(iptables -S DOCKER-USER | grep -c RETURN) allow rules)"
}
containers_ok() {
  local c s r
  for c in go2rtc frigate; do
    s="$(docker inspect -f '{{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{end}}' "$c" 2>/dev/null)" || { echo "$c missing"; return 1; }
    r="$(docker inspect -f '{{.HostConfig.RestartPolicy.Name}}' "$c")"
    echo -n "$c: $s, restart=$r; "
    [[ "$s" == running* && "$r" == unless-stopped ]] || return 1
  done
}
images_pinned() {
  local c want got
  for c in go2rtc:GO2RTC frigate:FRIGATE; do
    want="$(eval echo "\${${c#*:}_DIGEST}")"
    got="$(docker inspect -f '{{.Config.Image}}' "${c%%:*}")"
    [[ "$got" == *"@$want" ]] || { echo "${c%%:*} runs $got"; return 1; }
  done; echo "both images match versions.env digests"
}

check "OS is Ubuntu $UBUNTU_VERSION_ID x86_64"          os_ok
check "HWE kernel installed and running"                 kernel_ok
check "docker-ce pinned"                                 pkg_is docker-ce "$DOCKER_CE_VERSION"
check "containerd pinned"                                pkg_is containerd.io "$CONTAINERD_VERSION"
check "compose plugin pinned"                            pkg_is docker-compose-plugin "$COMPOSE_PLUGIN_VERSION"
check "Docker packages held"                             held
check "docker.service starts on boot"                    unit_ok docker
check "vivo-gateway.service starts on boot"              unit_ok vivo-gateway
check "recording SSD mounted"                            mount_ok
check "bare mount point is immutable"                    mountpoint_immutable
check "Intel GPU decode available (host)"                gpu_ok
check "clock in UTC and synchronized"                    time_ok
check "SSH key only"                                     ssh_ok
check "firewall (ufw + Docker ports)"                    fw_ok
check "containers running, restart policy"               containers_ok
check "images match pinned digests"                      images_pinned

write_report m1-check "M1 Linux gateway baseline: check" \
"Quick Sync inside the container is proven separately by \`tests/qsv_proof.sh\`, and the idle record by \`tests/idle_report.sh\`.
To prove services come back after a reboot: \`sudo reboot\`, wait, then run this check again without touching anything else."
exit $(( NFAIL > 0 ))
