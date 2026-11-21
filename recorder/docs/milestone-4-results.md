# Milestone 4 results: M25, M26

Status: **the test runs are done on the reference gateway, Nov 19 to 21, 2026, and every one passes.
Still open for M26: the handoff check and the push to the Vivo repository, the walkthrough call, and
a Vivo engineer's own deployment from the instructions (see "Open points").**
Each "Result" comes from the reference gateway's reports, copied to
[test-reports/milestone-4](test-reports/milestone-4/README.md). Times are UTC.

## M25 Load and resource acceptance

| Done when (scope) | Result | Evidence |
|---|---|---|
| A final run at 10 cameras with everything in scope on (both streams, retention, health endpoint), for the agreed length (24 h) | Pass. Nov 19 08:00 to Nov 20 08:00, 10 cameras, both streams, retention and the safe limit, health endpoint; the stack was measured as it ran (nothing restarted, its containers up since Nov 9); 17 of 17 checks | m25-load-20261120-080145 |
| Pass/fail against the agreed limits: CPU, RAM, temperature, disk headroom, unexplained recording gaps (target zero) | Pass on every limit. CPU average 42.8 % (limit 60 %), highest 1-minute average 61.0 % (limit 85 %). RAM highest 5.02 GB, 44 % of 11.5 GB (limit 70 %), growth +1.0 % (limit 10 %). CPU temperature highest 60 °C (limit 80 °C). Recording disk highest 20.3 % (safe limit 90 %); at the measured 29.8 GB a day of main stream and 8.90 GB of substream, 7 days plus 3 days take 235 GB of the 1,176 GB under the safe limit. OS disk highest 18.3 % (limit 80 %). 0 recording gaps on either stream of any camera; no container restarted; the health endpoint answered 2,825 of 2,825 polls | m25-load-20261120-080145 |
| The same report at 4 and 8 cameras for comparison | Pass. Nov 21 03:33 to 06:39, 1 h each at 4, 8 and 10 cameras, 16 of 16 checks in each run (table below) | m25-load-20261121-064117 |
| Vivo can rerun it from the script | `tests/m25_load.py`, docs/m25-load.md; commands below | |

### 4, 8 and 10 cameras

| Cameras | From (UTC) | CPU avg | CPU 1-min peak | RAM peak | Temp peak | Main GB/day | Substream GB/day | Gaps | Result |
|---|---|---|---|---|---|---|---|---|---|
| 4 | Nov 21 03:33 | 18.6 % | 25.1 % | 2.70 GB | 50 °C | 8.7 | 2.59 | 0 | PASS |
| 8 | Nov 21 04:36 | 33.2 % | 42.2 % | 3.31 GB | 56 °C | 15.9 | 4.75 | 0 | PASS |
| 10 | Nov 21 05:39 | 39.8 % | 52.2 % | 3.67 GB | 59 °C | 19.3 | 5.84 | 0 | PASS |
| 10, the 24 h run | Nov 19 08:00 | 42.8 % | 61.0 % | 5.02 GB | 60 °C | 29.8 | 8.90 | 0 | PASS |

- CPU grows about in step with the cameras: about 3.5 points of CPU per camera from 4 to 10 cameras.
- The 1-hour runs show less RAM and a lower recording rate than the 24-hour run, for two reasons.
  Frigate restarts at the start of each 1-hour run, so its memory starts fresh: Frigate used 2,378 MB on
  average at 10 cameras, against 4,018 MB after 10 days up. And the runs were at night on site (11:33 pm to
  2:39 am in Puerto Rico), when these scenes recorded less than their 24-hour average. Size disks and
  RAM from the 24-hour run.
- At the start of each 1-hour run the stack restarted with only the first 4 or 8 cameras of
  `cameras.yaml`, so the others were not recorded for that hour. The group was told before the run,
  and the windows are in `reports/planned-windows.txt` on the gateway.

To run them again on the gateway (docs/m25-load.md):

```bash
# the acceptance run, 24 h at 10 cameras, on the stack as it runs (no restart)
sudo systemd-run --unit vivo-m25 --collect -p TimeoutStopSec=10min /opt/vivo-gateway/tests/m25_load.py --counts 10 --hours 24
# the comparison, 1 h each at 4, 8 and 10 cameras (the stack restarts at each step)
sudo systemd-run --unit vivo-m25-cmp --collect -p TimeoutStopSec=10min /opt/vivo-gateway/tests/m25_load.py --counts 4,8,10 --hours 1 --compare-hours 1
journalctl -fu vivo-m25-cmp
```

## M26 Fault simulation and handoff

| Done when (scope) | Result | Evidence |
|---|---|---|
| One test runner combining the M10, M11 and M12 tests into a single repeatable test matrix, with a results table | Pass. Full run with the scope's counts, Nov 20 13:01 to 15:46: 9 of 9 (8 steps and the whole-window gap check). The power step's report was rebuilt with the right cut times (see "Power cuts" below) | m26-matrix-20261120-154645 |
| Deployment instructions that take a blank N100/N150 to a working gateway | docs/deploy.md, which is also the runbook the scope asks for: from the BIOS settings to a recording gateway, then the checks, the acceptance tests, day-to-day tasks, what to do when something is wrong, and how it recovers from each fault (with the times measured in M26) | |
| Final source handoff: everything in the Vivo repository, cleaned up, no secrets committed, every version pinned | pending: `tests/handoff_check.py` runs before the push to the Vivo repository | handoff-check |
| Known-limitations list | docs/known-limitations.md, updated with this milestone's results | |
| Walkthrough call (up to 1 hour) | agenda in docs/m26-handoff.md; to be scheduled | |
| A Vivo engineer deploys a gateway from the instructions alone and runs the test matrix with the same results | pending: Vivo's test after the handoff; the results to compare with are below | |

### The matrix run, step by step

Jorge was on site for steps 1 and 2: he unplugged each camera and switched the gateway's power when
the matrix asked.

| # | Step | What happened | Result | Report |
|---|---|---|---|---|
| 1 | m10-disconnect | cam01 to cam05 unplugged on site, one after another. Main stream back within 20 s of the camera's power coming back, substream within 38 s (limit 120 s). 11 gaps, 0 unexplained | PASS, 2 of 2 | m10-recovery-20261120-131503 |
| 2 | m11-power | 10 power cuts by hand, 15 s off each. The gateway switched itself on every time; main stream back within 34 s of power on, substream within 24 s (limit 180 s). Lost before a cut: main stream up to 25 s, substream up to 57 s (limits 25 s and 65 s). No substream segment finished across a cut; no ext4 errors; Frigate's index listed no broken file; 200 gaps, 0 unexplained | PASS, 6 of 6 | m11-power-20261121-002629 |
| 3 | m10-kill | Frigate killed 10 times. Main stream back within 8 s (limit 60 s), lost before a kill up to 6 s; substream not interrupted. 100 gaps, 0 unexplained | PASS, 2 of 2 | m10-recovery-20261120-142755 |
| 4 | m10-reboot | 10 reboots. SSH back 46 to 48 s after each; main stream back within 61 s, substream within 56 s (limit 180 s); lost before a reboot up to 8 s. 200 gaps, 0 unexplained | PASS, 2 of 2 | m10-recovery-20261120-151222 |
| 5 | m11-full-disk | A filler pushed the disk over a test limit of 21.1 % (the real 90 % would need 932 GB of filler). The storage guard deleted the two oldest hours of main stream, 3.85 GB, in order, with no recording gap. After the filler was removed: Frigate answered, every camera recorded both streams, no ext4 errors, OS disk at 18 % | PASS, 5 of 5 (the disk-pressure test 8 of 8) | m11-full-disk-20261120-152105, m9-pressure-20261120-152041 |
| 6 | m11-missing-disk | Recording volume off from 15:26:57 to 15:28:27. The recorder refused to start ("Dependency failed for srv-vivo.mount"), nothing was written to the OS disk, the health endpoint showed `disk_missing`, and recording resumed on its own 25 s after the volume came back | PASS, 6 of 6 | m11-missing-disk-20261120-152917 |
| 7 | m11-corrupt | cam01's segment from 15:12:04 cut to 60 % and caught ("cut short: 165053 of 275089 bytes"). A 128 s clip across it still gave 128 s of video; the HLS playlist loads; both neighbouring segments play; the original was put back | PASS, 7 of 7 | m11-corrupt-20261120-153445 |
| 8 | m12-integrity | Every segment from 12:43 to 15:37, both streams, 10 cameras: 8,818 main-stream and 1,662 substream segments, none broken, nothing missing outside the steps' windows. Gateway clock 0.000015 s from UTC; no segment written before its own end time | PASS, 22 of 22 | m12-integrity-20261120-154444 |
| 9 | whole window | 531 gaps on either stream of any camera from 13:01 to 15:46, every one inside a step's window | PASS | m26-matrix-20261120-154645 |

This run had 10 real power cuts, the count in the scope's plan. Milestone 3 had 8 (open point there).

### Power cuts

- The scope's ground rules say Vivo provides a smart plug. There was none on site, so Jorge switched
  the power by hand and posted "Off" and "On" in the group chat for each cut. With a plug, the matrix
  runs this step unattended (`--plug-off`, `--plug-on`, docs/m26-handoff.md).
- The matrix first reported this step as 5 of 6 (m11-power-20261120-140737). The failed check was "no
  substream segment was finished across a power cut", and the cause was the test's timing, not the
  recording. With an operator, the runner noted the cut time when it asked for the cut, 22 to 41 s
  before Jorge's "Off" post (131 s for cut 1, which Jorge made while the test was paused). It noted
  the power-on time when its prompt was answered, 8 to 34 s after his "On" post (379 s for cut 1).
  With the early cut times, normal substream files that had finished before the power went off looked
  as if they crossed the cut.
- The report was rebuilt from the same recordings with `tests/m11_faults.py report --events`, using
  the times of Jorge's posts (m11-events-power-20261120-corrected.csv): 6 of 6. Jorge posted "Off"
  after the power was off, so "lost before a cut" is measured from a moment a little after the real cut,
  which can only make it look larger. The first report is kept as the raw record.
- Planned change to the script: with an operator, note the cut time when the operator confirms the
  power is off, and list every file that crosses a cut instead of the first 5.

### Health endpoint: false offline polls

In the 24-hour M25 run, 6 single polls (Nov 19 11:34:50, 11:41:57, 14:39:53, 21:59:57, 23:15:54 and
Nov 20 07:01:28) showed `camera_offline` for all 10 cameras at once, and one poll on Nov 21 at
05:36:13 did the same for the 8 cameras of the 8-camera run. Recording did not stop: no gap on either
stream of any camera, and the next poll 30 s later was clear. The health service marks every camera
offline at once only when its call to go2rtc's stream list fails (5 s timeout): it then reads as if no
camera sends video. Planned fix: when that call fails, report the cameras as unknown, not offline. The
M25 limits use alerts only to explain gaps, so these polls changed no result.

## Changes made during this milestone

- `tests/m25_load.py` (M25), `tests/m26_matrix.py` and `tests/handoff_check.py` (M26), with docs/m25-load.md,
  docs/m26-handoff.md, docs/deploy.md and docs/known-limitations.md.
- The matrix runs the two steps that need someone on site first, so that person is needed for one
  block at the start.
- Reports: the end of a time span now shows its date when it falls on the next day (`vivo.utc_end`).
- The handoff check's unit tests split their fake keys and URL logins, so the check's own secret scan
  no longer flags them.

Unit tests: `python3 -m unittest discover -s tests/unit` (102 pass).

## Open points

- Handoff: run `tests/handoff_check.py`, then push this branch to the Vivo repository.
- The walkthrough call: to be scheduled.
- Vivo's own test: a Vivo engineer deploys from docs/deploy.md and runs the matrix.
- Health endpoint: the false offline polls (planned fix above).
- M11 power test with an operator: the cut-time change above.
- The gateway still runs the test scripts from before the date fix; they are updated with the next
  install.
