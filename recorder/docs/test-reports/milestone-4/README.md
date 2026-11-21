# Milestone 4 test reports (M25, M26)

The reports named in [milestone-4-results.md](../../milestone-4-results.md), copied from
`/opt/vivo-gateway/reports` on the Vivo reference gateway (vivo-n150) or written by the matrix on the
workstation. All times are UTC. They hold measurements and file names only, no footage or pictures.

## M25 load and resource acceptance

| File | What it is |
|---|---|
| m25-load-20261120-080145.md | the acceptance run: 24 h at 10 cameras, Nov 19 08:00 to Nov 20 08:00, 17 of 17 |
| m25-load-20261120-080145.csv | the raw samples behind it (gateway every 5 s, health endpoint every 30 s, containers every 2 minutes) |
| m25-load-20261121-064117.md | the comparison: 1 h each at 4, 8 and 10 cameras, Nov 21 03:33 to 06:39, 16 of 16 in each run |
| m25-load-20261121-064117.csv | the raw samples behind it |

The heading of the 24-hour run reads "2026-11-19 08:00:03 UTC to 08:00:07 UTC" because the script
printed the end time without its date; the run ended on Nov 20. Fixed in the script for later reports.

## M26 fault-simulation matrix

| File | What it is |
|---|---|
| m26-matrix-20261120-154645.md | the results table of the full run, Nov 20 13:01 to 15:46, with the preflight, every gap of the whole window and the step windows; updated on Nov 21 after the power report was rebuilt (see the note at its top) |
| m26-run-20261120.log | the matrix's console log of that run |
| m10-recovery-20261120-131503.md | step 1: 5 camera disconnects |
| m11-power-20261121-002629.md | step 2: 10 power cuts, rebuilt from the same recordings with the cut and power-on times from the group chat, 6 of 6 |
| m11-events-power-20261120-corrected.csv | the cut and power-on times used for that rebuild |
| m11-power-20261120-140737.md | step 2 as the matrix first wrote it, with the runner's early cut times, 5 of 6; kept as the raw record |
| m10-recovery-20261120-142755.md | step 3: Frigate killed 10 times |
| m10-recovery-20261120-151222.md | step 4: 10 gateway reboots |
| m10-events-reboot-20261120-143328.csv | the reboot times and how long SSH was down |
| m11-full-disk-20261120-152105.md | step 5: the recording disk filled to its safe limit |
| m9-pressure-20261120-152041.md | the disk-pressure test step 5 ran (what the storage guard deleted) |
| m11-missing-disk-20261120-152917.md | step 6: the recording disk taken away and given back |
| m11-corrupt-20261120-153445.md | step 7: one damaged segment, playback around it |
| m12-integrity-20261120-154444.md | step 8: every segment recorded during the matrix; its per-segment `.csv` stays on the gateway |
