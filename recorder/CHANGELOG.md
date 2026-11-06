# Changelog

## Unreleased

- Early-deletion notice (M9), as offered to Haris on Oct 29, on the reference gateway since Nov 3:
  the `storage` service lists substream segments deleted before `substream_days` ended (disk above the safe
  limit) in `recordings/substream/early-deletions.jsonl`, one line per camera and hour, and the health
  endpoint shows a tenth alert, `early_deletion` (warning, for 24 hours). `tests/m9_pressure.py` checks the
  list and `tests/m22_check.py` the alert (simulated). Segment format unchanged (docs/SEGMENT_FORMAT.md).
- Milestone 3 (Resilience and monitoring: M10, M11, M12, M22), on the reference gateway since Nov 4;
  results in docs/milestone-3-results.md:
  - M22: health endpoint `tools/health.py` as the host service `vivo-health` (port 9101, `/health` JSON and
    `/metrics` Prometheus): cameras, recorder, disk and SMART, containers, gateway, and nine alert conditions
    with trigger levels (docs/m22-health.md). It includes the M12 segment-age monitor. `tests/m22_check.py`
    triggers each alert on purpose; `recorder_down` (it stops Frigate) only when named in `--only`.
  - M11: `vivo-gateway.service` is bound to the recording disk's mount (it stops when the disk goes away);
    `vivo-disk-watch.timer` starts it again when the disk returns. `tests/m11_faults.py`: power cuts (by
    hand, with a smart plug, or as immediate reboots without saving with `--crash`), full disk, missing
    disk, corrupted segment.
  - M11 power loss, from the cuts on the reference gateway on Nov 6: new footage now reaches the recording
    disk within about 6 s instead of up to 35 s (data written out after 3 s, journal commit every 2 s; the
    `setup.sh` disk step); `bin/vivo-clean-empty` removes empty segments a cut left before the recorder
    starts; `vivo-cut-cleanup.service` (`tools/cut_cleanup.py`) takes segments a cut left broken out of
    Frigate's index, so clips and playback no longer stop there.
  - `tools/mp4box.py` checks that an MP4 is whole (its boxes add up to its size). ffprobe cannot see a
    file cut short when its index is at the start, as in Frigate's segments; the M11 corrupted-segment
    test, the M11 power report and M12 use it.
  - Substream archiver (M7): a segment gets its final name only once it is written to the disk and is a
    whole MP4, so a power cut can leave a `.part` file but no broken segment under a final name (asked
    by Haris after the power-cut tests; docs/SEGMENT_FORMAT.md change log).
  - M10: `tests/m10_recovery.py` (reboots from a workstation, Frigate kills, camera disconnects, gap log).
  - M12: `tests/m12_integrity.py` (missing intervals, unplayable segments with their cause, clock check,
    manual spot-check list).
  - `tests/testlib.py`: each gap goes to the latest event that started before it ended (events closer
    together than their limit keep their own gaps); `window_gaps`, `all_gaps` and `parse_result`.
    `tests/m10_recovery.py reboot` and `tests/m11_faults.py power` exit 1 when their report has a failed check.
    `tests/m10_recovery.py disconnect --block` simulates a short network drop (docs/findings.md F12);
    `tests/m12_integrity.py --start ... --streams main` scans a fixed period or one stream.
  - `setup.sh`: `/opt/vivo-gateway` is owned by root (the health service runs its files as root); the
    stack step installs the health, disk-watch and cut-cleanup units; the firewall opens 9101 to `LAN_CIDRS`.
  - `tools/vivo.py` no longer fails without root when it cannot read `/etc/vivo/secrets.env`.
- Milestone 2 groundwork, built and tested in the new workstation lab (`tests/lab/`, ten fake cameras),
  not yet on the reference gateway:
  - M7: `substream` service (`tools/substream_archiver.py`) saves each camera's substream as 60 s MP4
    segments in the agreed layout (docs/SEGMENT_FORMAT.md), with `.part` files renamed when complete.
    Frame times from the arrival clock, so a camera reconnect cannot leave a segment claiming hours
    (docs/findings.md F11). New `tests/m7_check.py` (names, lengths, gaps, random minutes on both streams).
  - M8 and M9: `storage` service (`tools/storage_guard.py`): substream retention (3 days), old exports
    deleted through Frigate's API (7 days), and a safe limit on the recording disk: above 90 % the oldest
    footage goes first, down to 85 %. New `vivo-disk` command (disk use, days stored, SMART) and
    `tests/m9_pressure.py` (disk-pressure test).
  - M6: `tests/m6_benchmark.py` (4, 8 and 10 cameras, 3 rounds).
  - Storage policy in the `site` section of `cameras.yaml`; `tools/gen_config.py` also writes
    `services/config.json` for the two services, which run in the pinned go2rtc image (Python and ffmpeg,
    no new image). PyYAML is now only needed by the tools that read `cameras.yaml`.
  - `setup.sh`: the disk step makes `recordings/substream`; the stack step installs `vivo-disk`.
    On an installed gateway: `sudo gateway/setup.sh --only disk,stack`.
  - Unit tests in `tests/unit/`.
- Milestone 1 groundwork: setup script (M1), Frigate and go2rtc compose and generated configs (M2, M5),
  camera probe, reconnect and 24 h run tools (M3), acceptance checks and module docs.
- Pinned: Docker 29.9.1, containerd 2.4.2, Compose 5.6.0, Frigate 0.18.0, go2rtc 1.9.14.
- Reference N150 (Oct 20-21, 2026): new `kernel` step installs the Ubuntu HWE kernel
  (`KERNEL_PACKAGE=linux-generic-hwe-24.04`, 7.0.0-38 on the gateway) because the 6.8 kernel has no
  driver ID for the N150 graphics (docs/findings.md F3). The stack step waits for the reboot.
- `--site` installs a reviewed site.env from `gateway/sites/` (`vivo-n150.site.env` for the reference gateway).
- The ssh step now refuses to run while another account logs in by password without a key
  (`--allow-password-lockout` overrides).
- Applied on the reference gateway with Vivo's approval: HWE kernel 7.0.0-38 running (Quick Sync verified with vainfo),
  SSH key only (password login off), host VAAPI tools.
- Reference N150 (Oct 22, 2026): full M1 applied (`--create-lv 760G` recording volume on the NVMe,
  chrony also serving NTP to the camera LAN, firewall, stack). Ten Vivo cameras in `cameras.yaml`
  with one shared credential name `VIVOCAM`. New `tools/onvif_probe.py` (read-only identity, clock,
  profiles and stream paths). Firewall step now reads open SSH sessions from the kernel (sudo drops
  SSH_CONNECTION). `tests/qsv_proof.sh` fixed for ffmpeg 8 (benchmark line is info level).
  Checks on the gateway: M1 16/16, Quick Sync proof (13x less CPU), M2 12/12, M5 all automated checks.
- Detect decoding switched to `preset-intel-qsv-h265` (findings F9: VAAPI crashed on these cameras'
  H.265); `tests/m2_check.py` now fails on ffmpeg crashes in the last 10 minutes; camera table bitrate
  no longer goes negative when a stream reconnects. New `tests/m5_browser.mjs` (live view checked in
  headless Chrome): 20/20 passed on the reference gateway.
- New `tools/onvif_time.py`: sets each camera's NTP server, NTP clock mode and on-screen timezone over
  ONVIF. Applied on the reference gateway: all ten cameras now sync from the gateway (were at 1970,
  Manual mode, China timezone); on-screen time is Puerto Rico (AST4). M2 kill test passed: Frigate
  back in 8 s, every camera recording again within 8 s (15/15).
- `setup.sh --add-disk /dev/XXX`: erases a disk and grows the LVM recording volume onto it while
  mounted. Applied on the reference gateway (Oct 23): the freed 512 GB SATA SSD took `vivo-media` from
  760 GiB to 1.21 TiB with no recording gap. Dry runs no longer stop at the ERASE prompts or on LVM
  lookups of disks that are not volumes yet. New `tests/m3_watch.py`: records camera power cycles done
  by hand on site (drop, camera back, stream back, recording back), tested with a simulated cam10
  outage (gateway reconnect 1 s); running for 48 h for Vivo's M3 test.
- Reboot proof on the reference gateway (Oct 24, 12:44 UTC): SSH back in 58 s, all ten cameras
  recording again 61 to 62 s after the reboot command, M1 16/16 and M2 13/13 on the new boot.
  M3 reconnect proven from the recordings (`tests/m3_gaps_report.py`): 10 of 10 cameras, gaps 43 to
  63 s. All Milestone 1 results on one page: docs/milestone-1-results.md.
