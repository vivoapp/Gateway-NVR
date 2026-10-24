# Findings that shape the design

Real events observed while building and testing. Each one points at the module that handles it.

## F1: a recording disk that disappears while running is invisible to Frigate's health check (lab, Oct 2026)

On a lab gateway (Jetson, NVMe recording disk, Frigate 0.18.0), the NVMe controller stopped
responding and the kernel removed the device:

```
nvme nvme0: I/O 100 QID 4 timeout, reset controller
nvme nvme0: Device not ready; aborting reset, CSTS=0x1
nvme nvme0: Removing after probe failure status: -19
EXT4-fs error (device nvme0n1p1): ... reading directory lblock 0
```

The filesystem stayed "mounted" (read-only) but every access returned `Input/output error`.
For more than two days:

- Frigate's Docker health check reported **healthy** the whole time.
- No recordings were written. The only trace was `OSError: [Errno 5] Input/output error`
  in Frigate's log once an hour.

What this means for Vivo:

- **M1** protects the case where the disk is missing **at boot** (immutable mount point, and
  Docker refuses to create the missing path), but not a disk that fails **while mounted**.
- **M22** must therefore alert on: newest segment too old per camera, the recording mount not
  writable (a periodic write test), kernel I/O errors for the recording device, and SMART warnings.
  Frigate's own health check is not enough.
- **M11** "missing disk" should test both cases: disk absent at boot, and disk removed or
  unmounted while recording.

## F2: Frigate restarts a stalled recorder only after 120 s (assessment, Oct 2026)

Frigate's record watchdog waits at least 120 s (`frigate/video/ffmpeg.py`, not configurable)
before restarting a recorder that stopped producing segments, and `/api/stats` kept showing a
normal frame rate during a 111 s recording gap. The segment-age check in M12 and M22 exists for this.

## F3: the Ubuntu 24.04 GA kernel does not drive the N150 graphics (reference gateway, Oct 20, 2026)

Read-only check on the reference N150 (Ubuntu Server 24.04.3, kernel `6.8.0-147-generic`):

- The GPU is `Intel Corporation Alder Lake-N [Intel Graphics] [8086:46d4]`.
- The 6.8 `i915` module has no alias for `46d4`, so it never loads. Only `simpledrm` runs, and
  `/dev/dri` has `card0` but **no `renderD128`**.
- Without `renderD128` there is no VAAPI / Quick Sync, and the Frigate service (which maps
  `/dev/dri/renderD128`) cannot start.

Rechecked: `lspci -k` shows no driver in use; the 6.8 `i915` knows only ADL-N IDs `46D0`,
`46D1`, `46D2`; `xe` has no `46D4` either. Upstream `INTEL_ADLN_IDS` lists `46D3` and `46D4`
by v6.12, so a newer kernel drives it.

Fix: Ubuntu's hardware enablement kernel `linux-generic-hwe-24.04` (candidate on Oct 20:
`7.0.0-38.38~24.04.4`) and one reboot. **M1** setup installs it (`kernel` step) and checks for
`renderD128` after the reboot. Its `i915` has the `46D4` alias. On the host, Ubuntu's
`intel-media-va-driver 24.1.0+dfsg1-1ubuntu0.2` adds the same IDs ("TWL support", LP: #2138258).
Installed on the reference gateway Oct 21, 2026 with Vivo's approval. After the reboot (back in 43 s):
`i915` in use, `renderD128` present, DMC/GuC/HuC firmware loaded, and `vainfo` (iHD 24.1.0) lists
hardware decode for H.264, HEVC Main/Main10, VP9, AV1 and JPEG. `renderD128` is group `render`, so
non-root users need `sudo` for `vainfo`; Frigate's container runs as root.

## F4: reference gateway layout as delivered (Oct 20, 2026)

- OS disk: Kingston SNV3S1000G (1 TB NVMe), LVM, root LV 100 GB of 928 GB (rest free in `ubuntu-vg`).
  Recordings now use a 760 GiB LV `ubuntu-vg/vivo-media` on it (`setup.sh --create-lv 760G`, Oct 22),
  because the SATA SSD's Windows partition cannot be shrunk from Linux (`ntfsresize` reports
  "404 clusters are referenced multiple times" and wants `chkdsk /f` in Windows first) and Vivo
  asked to keep Windows. One disk now holds OS and recordings: a disk failure loses both.
  Oct 23: Vivo released the SATA SSD ("we won't be using Windows"). It was healthy and nearly new
  (SMART PASSED, 74 power-on hours, no reallocated or pending sectors). `setup.sh --add-disk /dev/sda`
  added it to `ubuntu-vg` and grew `vivo-media` onto it online: 1.21 TiB (ext4 1.2 TB), no recording
  gap. The volume now spans both disks: if either fails, the recordings are lost; the root LV stays
  on the NVMe only, so the OS still boots and `nofail` plus the immutable mount point keep Frigate
  from writing to the OS disk.
- Recording disk: SATA SSD reporting only `SSD 512GB` (rev 3A0). It still holds a Windows
  install (EFI `SYSTEM`, `Recovery`, `Windows` partitions, NTFS clean, about 40 GB used, so a
  factory install). The Windows key is in firmware (ACPI `MSDM` present), and `Windows Boot
  Manager` is second in the UEFI boot order. Formatting it erases Windows, so Vivo must confirm first. 512 GB is a quarter of the 2 TB the scope sizes for 7 days (M9).
- Network: `enp1s0` static `192.168.30.200/24`, router `192.168.30.1`. VPN clients reach the
  gateway NATed from `192.168.30.9`, so SSH allow rules only need `192.168.30.0/24`.
- Accounts: `dev` (Vivo), `izzatullokh`, `haris`. sshd allows password login only because of
  Ubuntu's `50-cloud-init.conf` (`PasswordAuthentication yes`); our `10-vivo.conf` sorts first, and
  sshd keeps the first value, so ours wins. `dev` has a password, no `authorized_keys`, and logs
  in by password, so the M1 key-only hardening would lock it out of SSH (Vivo to add a key first).
  `izzatullokh`, `haris` and `root` have locked passwords.

## F5: the test cameras send H.265 although ONVIF reports H.264 (Oct 22, 2026)

All ten Vivo test cameras (white-label `A_ONVIF_CAMERA N501`, firmware `V1.0.4.12-build:20260203164758`)
report `H264 Main` for both ONVIF profiles (`tools/onvif_probe.py`), but the RTSP streams they
actually send are **H.265** on both `/media/live/1/1` (2880x1620) and `/media/live/1/2` (640x360),
with G.711 A-law audio (go2rtc producer medias, and the fMP4 that go2rtc serves).

- ONVIF encoder settings cannot be trusted on these cameras. The camera table (M3) is measured
  from the streams themselves (`tools/probe_cameras.py`), not taken from ONVIF.
- H.265 recording works (stream copy) and Quick Sync decodes it, but browser live view of H.265
  is not universal, the scope's default codec is H.264, and `docs/SEGMENT_FORMAT.md` promises
  H.264 substream segments to the cloud uploader. Needs a decision: switch the cameras to H.264,
  or keep H.265 and update the segment contract with Haris.

## F6: the camera clocks are at 1970 and cannot reach their NTP server (Oct 22, 2026)

The cameras' UTC clocks read 1970-01-01 (offset about -56.8 years), and their NTP server is
`pool.ntp.org`, which they cannot reach. ONVIF logins only work because `onvif_probe.py` signs
with the camera's own clock. The gateway now serves NTP to the LAN (`time` step, ufw 123/udp
from `LAN_CIDRS`); next, point each camera's NTP at 192.168.30.200 (M3/M12).

## F7: storage depends on scene activity: about a month now, about a day at the bitrate cap (Oct 22, 2026)

ONVIF reports a 6000 kbit/s limit for the main stream, but the cameras use variable bitrate. Measured
from 10 minutes of recorded main-stream segments on the reference gateway (quiet test scenes), the
ten cameras average 160 to 310 kbit/s each, about 2.2 Mbit/s in total: about 24 GB per day, so the
747 GB recording volume would hold about a month. If every camera hit its 6000 kbit/s cap (busy or
noisy night scenes), the same volume would hold about one day. The scope's 7 days on 2 TB assumed
about 2.5 Mbit/s per camera. M9 must therefore enforce retention by days and keep Frigate's
low-space cleanup as the safety net, and the real figure has to be measured on Vivo's live scenes.

## F8: one VAAPI decode error on the first start (Oct 22, 2026)

At 03:38:34 UTC, three minutes after the first start, cam04's detect ffmpeg exited with
`Failed to sync surface ... Input/output error` (`hwdownload` failed). Frigate's watchdog
restarted it and it kept running. Watching for repeats in the 24 h run (M3) before acting.

## F9: VAAPI decoding of these cameras' H.265 crashes; Intel QSV decodes it cleanly (Oct 22, 2026)

With Frigate's `preset-vaapi`, the detect decoders crashed again and again (`Failed to sync surface`,
`hwdownload ... Input/output error`, 1759 error lines in 13 minutes, cam07 at 0 fps), so go2rtc kept
reopening the substreams. Recording was not affected (stream copy, 60 segments per camera per
10 minutes). The kernel showed no GPU hang.

One-minute side-by-side test on `cam07_sub` through go2rtc, inside the Frigate container (ffmpeg 8.0):

| Decoder | Frames out of 300 | Decode errors |
|---|---|---|
| VAAPI (Frigate's exact detect command) | 268 | "Error constructing the frame RPS", about 2 per second |
| CPU (ffmpeg hevc) | 300 | same RPS errors, but no frames lost |
| Intel QSV (`hevc_qsv`) | 300 | none |

The cameras' H.265 has non-conforming reference picture sets. ffmpeg's own HEVC parser (used by
the CPU and VAAPI paths) complains about them, and on VAAPI the bad references end in a failed
surface sync. Intel's QSV decoder handles the stream. Fix: `site.hwaccel: preset-intel-qsv-h265`
in `cameras.yaml`. `tests/m2_check.py` now also fails on any ffmpeg crash in the last 10 minutes.

## F10: where the CPU goes with ten cameras (Oct 23, 2026, before M6 tuning)

The whole gateway averages about 42 % of its four cores (about 1.7 cores) with all ten cameras
recording and detection off; RAM about 3.5 GB of 11.5 GB. From Frigate's per-process stats
(percent of one core):

| Part | CPU | Why |
|---|---|---|
| Detect-stream decoders (10 ffmpeg, Intel QSV) | about 53 % | the cameras send 25 fps on the substream; Frigate uses 5 fps but decodes all 25 |
| One Frigate main process | about 43 % | not yet identified |
| Recording ffmpeg (10) | about 30 % | G.711 audio transcoded to AAC for MP4; video is copied |
| Per-camera processes (10) | about 22 % | frame handling at 5 fps |
| go2rtc | about 18 % | ten cameras, two streams each, plus restream clients |

M6 candidates: lower the substream to 5 to 10 fps on the cameras (also changes the cloud segments,
so Vivo decides), record without audio if Vivo does not need it, and identify the busy Frigate
process. Haris's uploader and agent are planned at about one core and 1 GB, with limits.
