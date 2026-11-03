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
```

During a run with fewer cameras, the cameras left out are not recorded. Post the planned window
to the group first; the script adds its windows to `reports/planned-windows.txt` and starts the
normal stack with every camera at the end, also after an error.

To compare with a rerun, use the same options and the same time of day (the cameras' bitrate
depends on what they see, docs/findings.md F7).

## Not included

The 72-hour soak test, AI or detection load, testing on Jetson or other hardware.
