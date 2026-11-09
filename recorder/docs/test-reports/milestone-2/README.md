# Milestone 2 test reports (M6 to M9)

The reports named in [milestone-2-results.md](../../milestone-2-results.md), copied from
`/opt/vivo-gateway/reports` on the Vivo reference gateway (vivocontrol-n150). All times are UTC.
The same files, without the Nov 9 run, were in the Milestone 2 ZIP.

## M6 performance baseline

| File | What it is |
|---|---|
| m6-benchmark-20261103-035937.md | 3 rounds of 4, 8 and 10 cameras, Nov 3 02:00 to 03:59 |
| m6-benchmark-20261103-035937.csv | the raw samples behind it, every 5 s (numbers rounded to 4 decimals) |
| m6-run-20261103.log | the benchmark's progress log |
| planned-windows-20261103.txt | the planned stops during that benchmark (cameras beyond N not recorded) |
| m6-top-of-hour-20261103-0500.txt | CPU about once a second around 05:00 on Nov 3 (the hourly Frigate preview peak) |
| m6-benchmark-20261109-035857.md | 3 rounds of 10 and 15 cameras (10 real + 5 copies), Nov 9 02:37 to 03:59, with the recording check of every camera |
| m6-benchmark-20261109-035857.csv | the raw samples behind it, every 5 s (numbers rounded to 4 decimals) |
| m6-run-20261109.log | the benchmark's progress log |
| planned-windows-20261109.txt | the planned stops during that benchmark |

## M7 continuous recording

| File | What it is |
|---|---|
| m7-check-20261104-044203.md | the clean 24-hour check, Nov 3 04:28 to Nov 4 04:28, 41 of 41 |
| m7-check-20261102-103614.md | the first 24-hour check, with the Nov 2 cable disconnect |

## M8 storage layout and disk management

| File | What it is |
|---|---|
| vivo-disk-20261104-063910.txt | output of `sudo vivo-disk` on Nov 4 06:39; the results quote the same command on Nov 3 04:19, so the numbers differ a little |
| m1-check-20261103-042010.md | OS and Docker checks after the first disk-pressure test |
| m2-check-20261103-042010.md | Frigate checks after the first disk-pressure test |

## M9 retention and overwrite

| File | What it is |
|---|---|
| m9-pressure-20261104-044555.md | the disk-pressure test on a settled stack, 8 of 8 |
| m9-pressure-20261103-040233.md | the first run, 6 of 7: the gap check caught the benchmark's restart 12 s before the test, not the cleanup |

To make new ones on the gateway, see "Acceptance checks" in [recorder/README.md](../../../README.md).
