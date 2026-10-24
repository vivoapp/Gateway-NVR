# M1 idle record (reference gateway)

Measured with `tests/idle_report.sh 10` on Oct 24, 2026, 03:57 to 04:07 UTC: Intel N150, 11.5 GB RAM,
kernel 7.0.0-38, the recording stack running (go2rtc and Frigate, all ten cameras recording, detection
off), no test or viewer running. One sample every 10 s for 10 minutes.

| Metric | Average | Peak |
|---|---|---|
| CPU busy (all 4 cores) | 43.1 % | 95.5 % (one 10 s sample, see below) |
| RAM used | 3.46 GB | 3.57 GB of 11.5 GB |
| CPU temperature (coretemp, package) | 45.6 °C | 57.0 °C |

- Apart from one sample, CPU stayed between 41 % and 43 %.
- The one high sample was at 04:00:00 UTC, on the hour. Only `sysstat-collect` (tiny) ran in the system
  journal then, and Frigate logged nothing unusual. The likely cause is Frigate's hourly recording
  cleanup (`record.expire_interval`, 60 minutes by default). To be confirmed per process in M6.
- Where the CPU goes and how to lower it (detect streams decoded at 25 fps for 5 fps use, G.711 to AAC
  audio for recording) is M6 work; see the CPU breakdown in docs/findings.md and the M6 limits in
  docs/acceptance-limits-proposal.md.
- Temperature is read from the CPU package sensor. The board sensor (`acpitz`) stays at about 27 °C
  whatever the load, so it is not used.

Raw data: `reports/m1-idle-20261024-040731.{md,csv}` on the gateway.
