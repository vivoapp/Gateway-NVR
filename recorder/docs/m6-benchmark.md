# M6: multi-camera performance baseline

## What it does

`tests/m6_benchmark.py` runs the recorder at 4, 8 and 10 cameras: each camera records its main
stream continuously, and its substream feeds Frigate and the substream archiver (M7), as in
normal running. For each run, after a 3-minute warm-up, it samples every 5 seconds for 10 minutes:

- CPU busy, all cores (`/proc/stat`)
- RAM used (MemTotal minus MemAvailable)
- write rate to the recording disk (`/proc/diskstats` for the recording volume)
- network input on the camera LAN interface
- CPU package temperature (coretemp, as in the M1 idle record)

and each container's CPU share every 2 minutes. Three rounds of the whole sequence show how much
the numbers vary between runs. The report has average and peak per run, the spread between
rounds, the check against the limits, and the per-camera CPU cost with an estimate of how many
cameras fit.

## Limits (agreed before start)

At 10 cameras: CPU average under 60 %, peak under 85 %; CPU temperature under 80 °C
(docs/acceptance-limits-proposal.md).

## Run

```bash
sudo tests/m6_benchmark.py --dry-run     # the plan and how long it takes (about 2 hours)
sudo tests/m6_benchmark.py               # 3 rounds of 4, 8 and 10 cameras
sudo tests/m6_benchmark.py --counts 10,15 --copies   # more cameras than the site has
```

During a run with fewer cameras, the cameras left out are not recorded. Post the planned window
to the group first; the script adds its windows to `reports/planned-windows.txt` and starts the
normal stack with every camera at the end, also after an error or `systemctl stop` (the report
then covers the runs done so far).

`--copies` allows counts above the number of cameras: the extra cameras (copy11, copy12, ...) take
the main stream and substream of the real cameras in turn from go2rtc's own restream, so the
recorder does the same work for them and no extra connection goes to the cameras. After each run
the script checks that every camera, copies included, recorded both streams (98 % or more of the
sampled minutes, no gap over 10 s); at the end it deletes the copies' substream folders. Frigate
deletes the copies' main-stream files with its normal retention cleanup.

To compare with a rerun, use the same options and the same time of day (the cameras' bitrate
depends on what they see, docs/findings.md F7).

## Not included

The 72-hour soak test, AI or detection load, testing on Jetson or other hardware.
