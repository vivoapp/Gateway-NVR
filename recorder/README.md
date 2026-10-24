# Vivo gateway: recorder foundation

Local recording gateway for Vivo CAM: IP cameras (RTSP) → go2rtc → Frigate → recording SSD,
on an Intel N100/N150 mini-PC running Ubuntu Server 24.04 LTS.

This repository covers Milestone 1 (Gateway foundation): M1 Linux baseline, M2 Frigate,
M3 RTSP camera ingestion and M5 go2rtc stream integration. Later milestones build on it.

## Layout

```
versions.env          every pinned version (Docker, Frigate, go2rtc), with digests
cameras.yaml          camera inventory: the only camera file people edit (no secrets)
site.env.example      per-site settings  -> /etc/vivo/site.env
secrets.env.example   passwords          -> /etc/vivo/secrets.env (root, 600)
compose.yaml          go2rtc + Frigate
bin/vivo-compose      docker compose with versions.env and site.env loaded
go2rtc/go2rtc.yaml    GENERATED from cameras.yaml
frigate/config.yml    GENERATED from cameras.yaml (mounted read-only)
gateway/setup.sh      M1: blank Ubuntu install -> ready gateway
gateway/files/        sshd, firewall, Docker and systemd files used by setup.sh
gateway/sites/        reviewed site.env per known gateway (setup.sh --site)
tools/                config generator, camera probe, continuity report, PoE control
tests/                one acceptance check per module; reports go to reports/
docs/                 one page per module, design decisions, camera table
```

## Install on a blank gateway

1. Install Ubuntu Server 24.04 LTS (x86_64) on the gateway's OS disk. Add your SSH public key
   for the admin user during install.
2. Copy this folder (`recorder/` in the Vivo repository) to the gateway, for example with
   `git clone`, then run from it:

   ```bash
   # a) settings: creates /etc/vivo/site.env and secrets.env from the examples, then fill them in
   sudo gateway/setup.sh --only preflight
   sudo nano /etc/vivo/site.env /etc/vivo/secrets.env
   #    (Vivo's reference N150: add --site gateway/sites/vivo-n150.site.env instead of editing site.env)

   # b) packages and kernel (the N150 needs the HWE kernel for Quick Sync), then one reboot
   sudo gateway/setup.sh --only packages,kernel
   sudo reboot

   # c) everything else: look first (changes nothing), then run
   sudo gateway/setup.sh --dry-run --format-disk /dev/sdX
   sudo gateway/setup.sh --format-disk /dev/sdX     # ERASES the recording SSD; asks you to type ERASE
   ```

   Recording disk, pick one:
   - `--format-disk /dev/sdX`: a separate recording SSD (the scope's layout). Erases it.
   - `--create-lv SIZE`: no separate disk; a new LVM volume next to the OS (Ubuntu installed with LVM).
   - Later, `--add-disk /dev/sdX` grows that LVM volume onto another disk, while recording. Erases that disk.

   The reference N150 was installed with `--site gateway/sites/vivo-n150.site.env --create-lv 760G`,
   then `--add-disk /dev/sda` (docs/findings.md F4). Later runs need no disk option; every step is
   safe to repeat. If you skip the reboot, setup.sh installs everything else, skips starting the
   stack, and asks for the reboot at the end.
3. Check it with the acceptance checks below, then `sudo reboot` and run `tests/m1_check.sh` and
   `tests/m2_check.py` again without touching anything else.

## Change cameras

Read a new camera's model, firmware and stream paths over ONVIF, and point its clock at the gateway:

```bash
sudo tools/onvif_probe.py --only cam11                       # read only
sudo tools/onvif_time.py --ntp 192.168.30.200 --tz AST4 --only cam11
```

ONVIF can be wrong about the codec (findings F5); `tools/probe_cameras.py` measures the real one.
Edit `cameras.yaml`, add the login to `/etc/vivo/secrets.env` as `<CREDENTIALS>_USER` /
`<CREDENTIALS>_PASSWORD` (the reference cameras share `VIVOCAM`), then:

```bash
tools/gen_config.py            # regenerate go2rtc and Frigate configs
git commit -am "Add camera cam03"
sudo gateway/setup.sh --only stack
```

`tools/gen_config.py --check` fails if someone edited a generated file by hand.

## Acceptance checks

| Module | Command | Proves |
|---|---|---|
| M1 | `sudo tests/m1_check.sh` | pinned packages, SSD mount, UTC + NTP, SSH key only, firewall, services on boot |
| M1 | `sudo tests/qsv_proof.sh` | Quick Sync decoding works inside the Frigate container |
| M1 | `tests/idle_report.sh 10` | idle CPU, RAM and temperature record |
| M2 | `sudo tests/m2_check.py --kill-test` | repo config is what runs, detection off, GPU decode, LAN-only ports, self-restart |
| M3 | `sudo tools/probe_cameras.py` | camera table (codec, resolution, fps, bitrate, problems) |
| M3 | `sudo tests/m3_reconnect.py` | 5 unplug/replug cycles per camera through the PoE switch, stream back within the limit |
| M3 | `sudo tests/m3_gaps_report.py --start ... --end ...` | same, when someone power-cycles the cameras on site: every gap in the recordings and when recording came back |
| M3 | `sudo tests/m3_soak_report.py --hours 24` | 24-hour run with no unexplained drops |
| M5 | `node tests/m5_browser.mjs` (workstation) | each stream really plays in Chrome (WebRTC and MSE), with screenshots |
| M5 | `sudo tests/m5_check.py --live-ok cam01,...` | one camera connection per stream, live view works |

Every check writes a Markdown report to `reports/`. Reports never contain passwords.

## Ports (all LAN only)

| Port | Service | Notes |
|---|---|---|
| 22 | SSH | key only, from ADMIN_CIDRS |
| 1984 | go2rtc web/API, MSE and HLS live view | password protected |
| 8554 | go2rtc RTSP restream | password protected |
| 8555 tcp/udp | go2rtc WebRTC | |
| 8971 | Frigate UI and API | Frigate login |
| 5000 | Frigate internal API | 127.0.0.1 only |
