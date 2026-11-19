# Proposed "agreed before start" limits

The scope leaves several numbers to agree before each module starts. These are my proposals
for the kickoff. Vivo can change any of them; once agreed they go into this file and the tests.

| Module | Limit | Proposal | Why |
|---|---|---|---|
| M3 | Stream back after a camera is plugged in again | 120 s from power on | Most IP cameras need 40 to 90 s to boot; go2rtc then reconnects within seconds |
| M3 | 24-hour run | 0 unexplained stream drops | From the scope |
| M6 | CPU at 10 cameras | average under 60%, peak under 85% | Leaves headroom for the uploader and health service |
| M6 | CPU temperature at 10 cameras | under 80 °C sustained | Fanless N100/N150 throttle around 90 to 100 °C |
| M9 | Local substream retention | 3 days (input from the uploader developer) | Covers a weekend internet outage |
| M10 | Recording back after a reboot | 180 s from power on | Boot + Docker + first 10 s segment |
| M10 | Recording back after a Frigate kill | 60 s | Measured about 30 s in the assessment |
| M10 | Recording back after a camera's power returns | 120 s | Same as M3 |
| M11 | Footage lost in a power cut | main 25 s, substream 65 s | The segment being written: 10 s plus Frigate's cache; one 60 s substream segment |
| M11 | Recording back after power returns | 180 s | Same as a reboot (M10) |
| M12 | Segment timestamps vs UTC | within ±2 s | NTP keeps the gateway under 0.1 s; cameras' own clocks are not used |
| M22 | Alert shown after the fault | within 60 s | Segment-age check every 10 s with a 30 s threshold |
| M25 | Final acceptance run | 24 h at 10 cameras, on the stack as it runs (the optional 1 h runs at 4 and 8 cameras were not used on the reference gateway: the other cameras would not be recorded meanwhile) | Long enough to catch slow leaks and a full day and night of scenes; matches the 24 h runs elsewhere |
| M25 | CPU | average under 60%; highest 1-minute average under 85% | Same as M6; a 1-minute average, so one busy 5 s sample in 24 hours does not fail the run |
| M25 | RAM | highest under 70% of the total; runs of 6 h or more: last quarter's average within 10% of the second quarter's | Leaves room for the uploader and Video API; the growth check catches a slow leak |
| M25 | CPU temperature | under 80 °C | Same as M6 |
| M25 | Disk headroom | recording disk below its safe limit all the time; at the measured rate, 7 days of main stream plus 3 days of substream fit under it; OS disk under 80% | The retention agreed in M9 must fit on the disk with the real scenes |
| M25 | Recording gaps | zero unexplained, both streams, every camera | From the scope; a camera's own drop (shown by the health endpoint) is listed but not counted |
| M25 | Services | no container restart; health endpoint answers 99% of polls | Everything in scope stays on for the whole run |
