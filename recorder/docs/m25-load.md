# M25: load and resource acceptance

## What it does

`tests/m25_load.py` runs the gateway with everything in scope switched on, as in production, and
checks it against limits agreed before start:

- Frigate records every camera's main stream (M7), the substream archiver saves every substream (M7),
  the storage guard applies retention and the safe limit (M8, M9), and the health endpoint runs (M22).
- First it checks that all of that is really on: the four containers run, every camera records both
  streams, Frigate has recording on for every camera, and the health endpoint answers.
- Then it samples for the agreed run length: CPU, RAM and CPU temperature every 5 s, the health
  endpoint every 30 s (its alerts and the recording disk use), the OS disk every minute, and each
  container's CPU and memory every 2 minutes. Every sample goes to a CSV as it is taken.
- At the end it builds the gap log for both streams of every camera over the whole run, and measures
  how many bytes each stream recorded, to check that the agreed retention fits on the disk.

The acceptance run is at 10 cameras. The same report is repeated at 4 and 8 cameras for comparison:
for those runs the stack starts with only the first 4 or 8 cameras of `cameras.yaml` (as in M6), so
the other cameras are not recorded meanwhile, and the normal stack is back before the 10-camera run.

## Limits (agreed before start)

From docs/acceptance-limits-proposal.md; once Vivo agrees, the numbers are in `LIMITS` in the script.

| Limit | Value |
|---|---|
| Run length | 24 h at 10 cameras; 1 h each at 4 and 8 cameras |
| CPU | average under 60 %; highest 1-minute average under 85 % |
| RAM | highest under 70 % of the total; in runs of 6 h or more, the last quarter's average within 10 % of the second quarter's (no steady growth) |
| CPU temperature | under 80 °C |
| Disk headroom | recording disk below its safe limit (90 %) all the time; at the run's measured recording rate, 7 days of main stream plus 3 days of substream fit under the safe limit; OS disk under 80 % |
| Recording gaps | zero unexplained gaps, both streams, every camera |
| Services | no container restarted; the health endpoint answered at least 99 % of the polls |

A gap while the health endpoint showed that camera offline (`camera_offline`) is listed as the
camera's own drop, not as unexplained. A gap inside a `--planned` window is listed as planned.
After a stack restart, sampling starts once every camera records both streams (at most 5 minutes
after the warm-up; a camera still missing then fails the run). The last 70 s of each run are left
out of the gap check, because a restart right after a run drops the segment being written.

## Run it

The default plan takes about 26 hours: 1 h at 4 cameras, 1 h at 8 cameras, then 24 h at all 10.
Run it as a transient systemd unit, so it keeps running when the SSH session ends:

```bash
sudo /opt/vivo-gateway/tests/m25_load.py --dry-run          # the plan and the limits
sudo systemd-run --unit vivo-m25 --collect -p TimeoutStopSec=10min /opt/vivo-gateway/tests/m25_load.py
journalctl -fu vivo-m25                                     # follow it; progress every 10 minutes
sudo systemctl stop vivo-m25                                # stop early: it still writes the report
```

Only the acceptance run, on the stack as it runs (nothing is restarted, no recording stops):

```bash
sudo systemd-run --unit vivo-m25 --collect -p TimeoutStopSec=10min \
  /opt/vivo-gateway/tests/m25_load.py --counts 10 --hours 24
```

Post the planned window to the group before the 4 and 8 camera runs. The script adds them to
`reports/planned-windows.txt`. Avoid other tests, reboots and camera changes during the 24 hours,
or pass their windows with `--planned 2026-11-20T10:00Z/2026-11-20T10:05Z`.

Report: `reports/m25-load-<time>.md` with a summary table (one row per camera count), the checks per
run, container CPU and memory, the health alerts seen, the gap log, and the raw samples in the
`.csv` with the same name. The exit code is 0 when the 10-camera run passes every limit.

## Lab

```bash
export LAB_DIR=$TMPDIR/vivo-lab LAB_CLIPS=$TMPDIR/vivo-lab/clips VIVO_LAB_DISK_BYTES=20000000000
tests/lab/lab.sh up 4 && eval "$(tests/lab/lab.sh env)"
FRIGATE_API=http://127.0.0.1:5000/api tests/m25_load.py --counts 2,4 --hours 0.08 --compare-hours 0.05 \
  --warmup 120 --health-url "" --project "$LAB_DIR/project" --compose-file tests/lab/compose.lab.yaml \
  --compose-file tests/lab/compose.fakecam.yaml --sampler-container go2rtc
```

`LAB_CLIPS` must be set: the stack restarts for the 2-camera run, and the fake cameras need their
clips. The lab has no health endpoint, no temperature sensor in the container and no Intel GPU, so
the temperature check fails there, the OS disk check reads the workstation's disk, and the numbers
are the workstation's.

## Not included

Live-view latency (M15), cloud upload load (M19, M20), the 30-day field pilot.
