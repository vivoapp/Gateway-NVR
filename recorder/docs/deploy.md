# Deploy a gateway from a blank machine

These steps take a blank Intel N100 or N150 mini-PC to a working Vivo gateway: every camera's main
stream recorded by Frigate, its substream saved for the cloud uploader, retention and the safe disk
limit enforced, and the health endpoint running. Allow about 1.5 hours, plus the time to set up the
cameras. Everything runs from this folder (`recorder/` in the Vivo repository).

## 1. What you need

- **Gateway:** Intel N100 or N150 mini-PC, 16 GB RAM, an OS disk, and a recording SSD (2 TB for
  7 days at 10 cameras; the real need depends on the scenes, docs/findings.md F7). One disk also
  works: the recordings then go on an LVM volume next to the OS.
- **Network:** the gateway on the camera LAN, with a fixed address (static, or a DHCP reservation).
  Write down the gateway's address, the LAN (for example `192.168.30.0/24`) and the networks SSH
  may come from (the LAN and the Vivo VPN).
- **Cameras:** on the same LAN, with fixed addresses, RTSP on, and their login.
- **A workstation** with an SSH key and access to the Vivo repository.
- **Ubuntu Server 24.04 LTS** (x86_64) on a USB stick.

## 2. BIOS

- **Power on after a power cut:** set "Restore on AC power loss" (on some boards "State after G3")
  to **Power On**. Without it the gateway stays off after a power cut until someone presses the
  button. The M11 power test checks it.
- Boot from the USB stick. Secure Boot can stay on.

## 3. Install Ubuntu Server 24.04 LTS

- Install on the OS disk. If there is no separate recording SSD, choose LVM and leave free space in
  the volume group for the recordings (the reference gateway gave the OS a 100 GB root volume).
- Give the network interface the gateway's fixed address.
- Install the OpenSSH server and import your SSH public key. Nothing else is needed.
- After the first boot, check that `ssh <admin>@<gateway address>` works with your key.

## 4. Copy the repository to the gateway

From the workstation:

```bash
git clone https://github.com/vivoapp/Gateway-NVR.git
rsync -a Gateway-NVR/recorder/ <admin>@<gateway address>:recorder/
ssh <admin>@<gateway address>
cd ~/recorder
```

(Or `git clone` on the gateway itself, with a read-only deploy key.)

## 5. Site settings and passwords

```bash
sudo gateway/setup.sh --only preflight     # creates /etc/vivo/site.env and secrets.env from the examples, then stops
sudo nano /etc/vivo/site.env
sudo nano /etc/vivo/secrets.env
```

`/etc/vivo/site.env` (not secret):

| Setting | What |
|---|---|
| `GATEWAY_LAN_IP` | the gateway's fixed address on the camera LAN (also the WebRTC address for live view) |
| `LAN_CIDRS` | networks that may use live view, the Frigate UI, the RTSP restream and the health endpoint |
| `ADMIN_CIDRS` | networks SSH may come from: the LAN and the Vivo VPN. Must include the address you connect from now |
| `MEDIA_ROOT` | where the recording disk is mounted, `/srv/vivo` |
| `NTP_SERVERS` | empty for the Ubuntu pool, or your NTP servers |
| `POE_BACKEND` | optional, for the camera disconnect tests: `manual` (default) or `command` with `POE_OFF_CMD` and `POE_ON_CMD` (tools/poe.py) |

`/etc/vivo/secrets.env` (root only, mode 600; never commit it): the go2rtc web and API login, the
login Frigate uses for go2rtc's RTSP restream, and one `<NAME>_USER` / `<NAME>_PASSWORD` pair for
each `credentials` name in `cameras.yaml`. Use long random passwords made of letters and digits
(`openssl rand -hex 16`), so nothing needs URL encoding. `setup.sh` refuses to continue while any
value is still `change-me`.

For a gateway that already has a reviewed site file in `gateway/sites/`, use it instead of editing
site.env: `sudo gateway/setup.sh --only preflight --site gateway/sites/<name>.site.env`.

## 6. Packages and kernel, then one reboot

```bash
sudo gateway/setup.sh --only packages,kernel
sudo reboot
```

The N100/N150 graphics need Ubuntu's HWE kernel for Intel Quick Sync (docs/findings.md F3).

## 7. Cameras

`cameras.yaml` is the only camera file people edit. For each camera: `id` (lowercase, for example
`cam01`), `name`, `host`, `port`, `main_path`, `sub_path`, `credentials` (the name of its login in
secrets.env), `audio`, and optionally `poe_port` for the disconnect tests. The `site` section holds
retention (`record_days`, `substream_days`, `export_days`), the safe limit (`disk_limit_percent`,
`disk_target_percent`) and the decoder (`hwaccel`).

If you do not know the stream paths, read them from the cameras over ONVIF (it needs the cameras'
login in secrets.env): `sudo tools/onvif_probe.py`. ONVIF can be wrong about the codec
(docs/findings.md F5); step 9 measures the real one. The packages step installed PyYAML, which
`tools/gen_config.py` needs.

```bash
cd ~/recorder
nano cameras.yaml
tools/gen_config.py        # writes go2rtc/go2rtc.yaml, frigate/config.yml and services/config.json
```

Commit the site's `cameras.yaml` and the three generated files to the Vivo repository (for example
on a branch named after the site), so the gateway's configuration is never only on the gateway.

## 8. Recording disk and everything else

Find the recording SSD (`lsblk -o NAME,SIZE,MODEL,SERIAL,MOUNTPOINT`), look at what setup.sh will do,
then run it:

```bash
cd ~/recorder
sudo gateway/setup.sh --dry-run --format-disk /dev/sdX      # changes nothing
sudo gateway/setup.sh --format-disk /dev/sdX                # ERASES /dev/sdX; asks you to type ERASE
```

| Recording disk | Option |
|---|---|
| a separate SSD (the scope's layout) | `--format-disk /dev/sdX` erases it and mounts it at `MEDIA_ROOT` |
| no separate SSD (Ubuntu installed with LVM and free space) | `--create-lv 760G` makes an LVM volume of that size |
| later, more space | `--add-disk /dev/sdY` grows the LVM volume onto another disk while recording (erases that disk) |

setup.sh then sets the clock to UTC with chrony (also serving time to the cameras), installs the
pinned Docker, mounts the recording disk by UUID with `nofail` (the empty mount point is made
immutable, so nothing is ever written to the OS disk by mistake), switches SSH to keys only, sets
the firewall (LAN only, Docker ports included), installs the stack in `/opt/vivo-gateway` with its
systemd units (`vivo-gateway`, `vivo-health`, `vivo-disk-watch.timer`), pulls the pinned images and
starts everything. Every step is safe to run again; later runs need no disk option.

Two safety stops: the SSH step refuses to run while another account logs in by password and has no
SSH key (it would lose SSH access), and the firewall step refuses if your own SSH address is outside
`ADMIN_CIDRS`. Fix the cause and run the same command again.

## 9. Check it

Run the checks from the installed copy, so all reports stay in `/opt/vivo-gateway/reports`:

```bash
cd /opt/vivo-gateway
sudo tests/m1_check.sh                   # packages, disk mount, clock, SSH, firewall, services
sudo tests/m2_check.py                   # Frigate runs the repository config, hardware decoding, ports
sudo tools/probe_cameras.py              # each camera's real codec, resolution, frame rate and bitrate
                                         # (writes docs/camera-table.md: commit it with the site's config)
sudo tests/m5_check.py                   # one camera connection per stream, live view works
sudo vivo-disk                           # recording disk, safe limit, SSD health
curl -s http://127.0.0.1:9101/health | python3 -m json.tool | less    # no "alerts" once recording runs
sudo tests/m7_check.py                   # after 15 minutes: both streams recorded for every camera
```

If the cameras send H.264 rather than H.265, set `hwaccel: preset-intel-qsv-h264` in `cameras.yaml`,
then run `tools/gen_config.py` and `sudo gateway/setup.sh --only stack` again.

Then `sudo reboot`, and run `tests/m1_check.sh` and `tests/m2_check.py` again without touching
anything else: everything must come back on its own.

- **Frigate UI:** `https://<gateway address>:8971`. At its first start Frigate creates the user
  `admin` with a random password and writes it to its log (`sudo vivo-compose logs frigate | grep -i password`).
  Sign in and change it.
- **Live view:** `http://<gateway address>:1984` with the go2rtc login from secrets.env.

## 10. Acceptance tests

| Test | Command | Time |
|---|---|---|
| Load and resources (M25) | `sudo systemd-run --unit vivo-m25 --collect -p TimeoutStopSec=10min /opt/vivo-gateway/tests/m25_load.py` | about 26 h |
| Fault matrix (M10, M11, M12) | `tests/m26_matrix.py --host <gateway> --plug-off ... --plug-on ...`, from the workstation | about 4 h |
| Handoff check | `sudo /opt/vivo-gateway/tests/handoff_check.py --secrets /etc/vivo/secrets.env` | 1 min |

docs/m25-load.md and docs/m26-handoff.md explain each one, and README.md lists every module's check.

## 11. Day to day

| Task | How |
|---|---|
| Add or change a camera | edit `cameras.yaml`, `tools/gen_config.py`, commit, `sudo gateway/setup.sh --only stack` (recording stops for about a minute) |
| Change retention or the safe limit | the `site` section of `cameras.yaml`, then the same three steps |
| See the state | `curl http://<gateway>:9101/health` (JSON) or `/metrics` (Prometheus), docs/m22-health.md; `sudo vivo-disk` |
| Logs | `journalctl -u vivo-gateway -u vivo-health -u vivo-disk-watch`; `sudo vivo-compose logs -f frigate` (or `go2rtc`, `substream`, `storage`) |
| Restart the recorder | `sudo systemctl restart vivo-gateway` |
| Planned work on the recording disk | `sudo touch /etc/vivo/maintenance` first (the disk watch then leaves the recorder stopped), remove it afterwards |
| Change a pinned version | edit `versions.env` in a reviewed commit, note it in CHANGELOG.md, then `sudo gateway/setup.sh --only docker,stack` |

## 12. When something is wrong

| What you see | Why | What to do |
|---|---|---|
| `secrets.env still has change-me values` | a password was not filled in | edit `/etc/vivo/secrets.env`, run again |
| `/dev/dri/renderD128 missing` | the HWE kernel is not running yet | `sudo gateway/setup.sh --only kernel`, reboot (docs/findings.md F3) |
| The SSH step stops: accounts log in by password | they would lose SSH access | add an SSH key for each, or `--allow-password-lockout` after asking their owners |
| The firewall step stops: client outside `ADMIN_CIDRS` | you would be locked out | add your network to `ADMIN_CIDRS` in site.env |
| A camera is not recorded | wrong login, path or address; camera off | `http://<gateway>:1984` shows each stream; `sudo tools/probe_cameras.py` measures them all |
| Health shows `disk_missing`, recorder stopped | the recording disk is gone | check the disk and its cable; when it is back, `vivo-disk-watch` starts the recorder within 30 s |
| Health shows `disk_near_limit` | the scenes record more than planned | the storage guard deletes the oldest footage; plan a bigger disk or shorter retention (docs/m8-storage.md) |
| Health shows `recording_gap` for one camera | that camera's recorder stalled | Frigate restarts it within about 2 minutes (docs/findings.md F2); if not, `sudo systemctl restart vivo-gateway` |
