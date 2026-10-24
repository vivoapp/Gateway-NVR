# Milestone 1 results: M1, M2, M3, M5 on the reference gateway

Reference gateway: Intel N150, Ubuntu Server 24.04.3 LTS, kernel 7.0.0-38 (HWE), Vivo office rack,
ten Vivo test cameras (white-label N501, 2880x1620 main stream and 640x360 substream, H.265).
All checks are in `tests/` and write a report to `reports/` on the gateway; the report names are listed.

## M1 Linux gateway baseline

| Done when (scope) | Result | Evidence |
|---|---|---|
| Ready gateway from the script and the instructions alone | `gateway/setup.sh` took the fresh Ubuntu install to a ready gateway (README "Install on a blank gateway") | m1-check-20261022-033851 |
| Every service comes back after a reboot, untouched | Reboot on Oct 24 12:44 UTC: SSH back in 58 s, all ten cameras recording again 61 to 62 s after the reboot command; M1 16/16 and M2 13/13 on the new boot | m1-check-20261024-124547, m2-check-20261024-124549 |
| Clock synchronized | chrony synchronized, UTC; the gateway also serves time to the cameras | m1-check-20261024-124547 |
| Quick Sync proven inside the container | hardware decoding used 13 times less CPU than software decoding | qsv-proof-20261022-033937 |
| Idle record | CPU 43 % average with ten cameras, RAM 3.5 GB, CPU 46 °C | docs/m1-idle-record.md |

Recording volume: 1.2 TB ext4 on LVM (NVMe plus the 512 GB SSD Vivo released), mounted at `/srv/vivo`
by UUID with `nofail` (docs/findings.md F4). The scope's 2 TB disk is not in the unit yet.

## M2 Frigate

| Done when (scope) | Result | Evidence |
|---|---|---|
| Fresh install from the repository config, no manual steps | running config is byte-for-byte `frigate/config.yml` from the repository | m2-check-20261024-124549 |
| Survives a reboot; restarts on its own after its process is killed | reboot: see M1; kill: back in 8 s, every camera recording again within 8 s | m2-check-20261022-075439 |
| Detection off; CPU confirms hardware decoding | detection and all AI features off; Intel QSV decoding (VAAPI crashed on these cameras' H.265, findings F9) | m2-check-20261024-124549 |

## M3 RTSP camera ingestion

| Done when (scope) | Result | Evidence |
|---|---|---|
| All test cameras deliver both streams at the expected settings | ten cameras, main and substream, through go2rtc; camera table | docs/camera-table.md |
| Each camera unplugged and plugged back in 5 times, back on its own within the limit | Vivo power-cycled all ten cameras 6 times on the PoE switch; every camera recorded again 43 to 63 s after each cut (limit 120 s) | m3-reconnect-20261024-031503 |
| 24-hour run, no unexplained stream drops | 0 unexplained gaps (only the planned power-cycle test) | m3-soak-20261024-031528 |

## M5 go2rtc

| Done when (scope) | Result | Evidence |
|---|---|---|
| Every camera plays live on the local network | all ten cameras played in Chrome over WebRTC and MSE (20 of 20) | m5-browser-20261022T073622 |
| One connection per stream while Frigate records and a viewer watches | 70 of 70 checks | m5-check-20261022-075038 |
| Written note on formats, H.265 and audio | docs/m5-go2rtc-notes.md | |

## Open decisions for Vivo

- H.264 or H.265 on the cameras (findings F5): H.265 plays in Chrome and Safari but not Firefox.
- Acceptance limits for later modules (docs/acceptance-limits-proposal.md).
