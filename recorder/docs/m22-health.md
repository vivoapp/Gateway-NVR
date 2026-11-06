# M22: health telemetry and diagnostics

## What it does

`tools/health.py` runs on the gateway as the systemd service `vivo-health` (installed and started
by `gateway/setup.sh`). Every 10 seconds it collects the state below and serves the latest snapshot:

| URL | Format |
|---|---|
| `http://<GATEWAY_LAN_IP>:9101/health` | JSON |
| `http://<GATEWAY_LAN_IP>:9101/metrics` | Prometheus text |

Both also answer on `127.0.0.1:9101`. The port is open to `LAN_CIDRS` only (ufw). The service only
reads; it changes nothing on the gateway. It runs as root because SMART, Docker and chrony need it,
which is why `/opt/vivo-gateway` is owned by root. The Edge Agent (M21) reads it; sending it to Vivo
is M21's job.

One snapshot from the command line: `sudo tools/health.py --once` (or `--once --alerts`, exit code 1
when an alert is active).

## What it reports

| Part | Fields |
|---|---|
| `cameras.<id>` | `online`, `main_silent_s` (seconds since the main stream last delivered data), `fps` (Frigate), `main_segment_age_s`, `substream_segment_age_s` (age of the newest recorded segment) |
| `recorder` | `frigate_up`, `recording_on` per camera, `recording_on_all` |
| `disk` | `path`, `mounted`, `percent_used`, `limit_percent`, `points_below_limit`, `free_bytes`, `fs_errors` (ext4 error count), `ssd` (SMART per disk: health, temperature, power-on hours, wear, media errors, reallocated and pending sectors, `warnings`), `early_deletions` (lines of `early-deletions.jsonl` from the last 24 hours, docs/m9-retention.md) |
| `containers.<name>` | `state`, `restarts` (Docker's count), `started` for go2rtc, frigate, substream, storage |
| `gateway` | `cpu_percent`, `memory` (`used_bytes`, `total_bytes`), `temperature_c` (CPU package), `uptime_s`, `clock` (`synced`, `offset_s`, `stratum`, `leap`, `source` from chrony) |
| `alerts` | the active alerts, each with `alert`, `target`, `level`, `detail`, `since` |
| `levels` | the trigger levels below |

SMART is read every 10 minutes (it is slow and changes slowly); everything else every 10 seconds.

## Alert conditions

| Alert | Target | Triggers when | Level |
|---|---|---|---|
| `camera_offline` | camera | its main stream delivered no data for 30 s | critical |
| `recording_gap` | camera/main, camera/substream | the newest main-stream segment is older than 30 s, or the newest substream segment older than 150 s, even if the stream looks healthy (the M12 segment-age monitor) | critical |
| `recorder_down` | frigate | Frigate's API does not answer | critical |
| `disk_missing` | recording disk | the recording disk is not mounted | critical |
| `disk_near_limit` | recording disk | within 5 points of the safe limit (warning), at or above it (critical) | warning, critical |
| `ssd_health` | disk | SMART overall health failed, media errors, reallocated or pending sectors, a critical NVMe warning, or ext4 errors on the recording filesystem | critical |
| `high_temperature` | cpu | CPU package at or above 80 °C in two samples in a row | warning |
| `clock_not_synced` | chrony | chrony not synchronized, not answering, or off by more than 1 s | warning |
| `container_down` | container | go2rtc, frigate, substream or storage not running | critical |
| `early_deletion` | camera/substream | substream segments were deleted before their retention ended, to keep the disk under its safe limit (listed in `early-deletions.jsonl`); shown for 24 hours after the deletion, `since` is the first one | warning |

With a 10-second collection, an alert shows at most its trigger level plus 10 s after the fault
(the 150 s substream level is longer than the agreed 60 s on purpose: a substream segment is 60 s
long, and the main-stream level catches a stopped recorder first). Agreed limit: within 60 s
(docs/acceptance-limits-proposal.md).

## Test

```bash
sudo tests/m22_check.py              # each alert triggered on purpose, shown within 60 s, then cleared
sudo tests/m22_check.py --only recorder_down   # stops Frigate: recording stops for about a minute
```

Camera offline (the camera is blocked in the firewall), recording gap (Frigate's recorder for one
camera is paused while its stream stays healthy), clock not synced (chrony stopped for a moment) and
container down (the storage container stopped) are real faults. Disk near the limit, an SSD warning,
a high temperature and an early deletion cannot be caused safely, so they go through a test-only override file,
`/run/vivo/health-overrides.json`, which the snapshot shows as `test_overrides` while it exists.
Recorder down (the Frigate container stopped, then started again) runs only when named in `--only`,
because recording stops while Frigate is down; post the planned window first.
The missing disk is checked by `tests/m11_faults.py missing-disk`; a real early-deletion list by
`tests/m9_pressure.py` when its cleanup reaches substream footage.

## Not included

Sending health data to Vivo's servers (M21, Edge Agent), dashboards in Vivo's apps, notifications.
