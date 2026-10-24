# M1: Linux gateway baseline

## What it does

`gateway/setup.sh` takes a fresh Ubuntu Server 24.04 LTS install to a ready gateway:

| Step | Result |
|---|---|
| packages | chrony, ufw, smartmontools, nvme-cli, VAAPI tools, Python YAML |
| kernel | Ubuntu's HWE kernel (`KERNEL_PACKAGE` in `versions.env`); the N150 graphics need it for Quick Sync. One reboot on first install |
| time | system clock in UTC, chrony NTP (servers from `NTP_SERVERS`, else the Ubuntu pool); also serves NTP to `LAN_CIDRS` so cameras without internet keep correct time |
| docker | Docker Engine and Compose at the versions in `versions.env`, held so apt cannot upgrade them; log rotation 5 x 20 MB |
| disk | recording SSD as ext4, mounted at `MEDIA_ROOT` by UUID with `nofail`; or, with `--create-lv SIZE`, a new LVM volume `vivo-media` next to the OS (used on the reference N150, see findings F4); `--add-disk /dev/XXX` erases another disk and grows that volume onto it online |
| ssh | key only: password and keyboard-interactive login off, no root login |
| firewall | deny all inbound except SSH from `ADMIN_CIDRS`; Docker ports allowed only from `LAN_CIDRS`/`ADMIN_CIDRS` |
| stack | installed to `/opt/vivo-gateway`, `vivo-gateway.service` starts it on boot |

## Choices

- **ext4** for the recording disk: journaled, mature repair tools, behaves well after power loss. Formatted with no reserved blocks (`-m 0`).
- **Mounted by UUID with `nofail`**: the gateway still boots if the disk is missing.
- **Immutable mount point**: the empty `MEDIA_ROOT` directory on the OS disk gets `chattr +i`, so nothing can write recordings to the OS disk when the SSD is missing. Frigate then fails to start with a clear "path does not exist" error instead of filling the OS disk (also the basis for M11).
- **Docker ports and ufw**: Docker-published ports skip ufw's normal rules, so they are filtered in Docker's `DOCKER-USER` chain (managed block in `/etc/ufw/after.rules`).
- **HWE kernel**: the 24.04 GA kernel (6.8) has no graphics driver ID for the N150 (`8086:46d4`), so there is no `/dev/dri/renderD128` and Frigate cannot start (docs/findings.md F3). The HWE kernel series adds it. The old kernel stays installed as a fallback under "Advanced options" in the GRUB menu. Until the reboot, the stack step is skipped instead of failing.
- **Lock-out protection**: the SSH step refuses to run without an authorized key for you, and also while any other account logs in by password and has no key (it would lose SSH access; `--allow-password-lockout` overrides after checking with its owner). The firewall step refuses to run if your own SSH address is outside `ADMIN_CIDRS`.
- **Site file**: `--site FILE` installs a reviewed `site.env` from the repository (`gateway/sites/`), so the reference gateway's network settings are versioned instead of typed by hand.

## Install and test

See the README. Acceptance:

```bash
sudo tests/m1_check.sh          # all checks PASS
sudo tests/qsv_proof.sh         # Quick Sync decoding inside the container
tests/idle_report.sh 10         # idle CPU, RAM, temperature
sudo reboot                     # then, without touching anything:
sudo tests/m1_check.sh          # still all PASS, new boot id in the report
```

Done when Vivo runs `setup.sh` on a blank machine from these instructions alone, every service
returns after a reboot, and the clock shows synchronized.

## Not included

Gateway identity and Edge Agent (M21), full security hardening (M23), remote support access (M24), OTA (M27).

## Known limitations

- The OS install itself (Ubuntu from USB/KVM) is manual; the script starts from a fresh install.
- Needs internet access during setup for apt and image downloads.
