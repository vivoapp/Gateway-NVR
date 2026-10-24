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
| M12 | Segment timestamps vs UTC | within ±2 s | NTP keeps the gateway under 0.1 s; cameras' own clocks are not used |
| M22 | Alert shown after the fault | within 60 s | Segment-age check every 10 s with a 30 s threshold |
| M25 | Final acceptance run | 24 h at 10 cameras | Long enough to catch slow leaks; matches the 24 h runs elsewhere |
