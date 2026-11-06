# M10: recording recovery after restart

## What it does

The recorder already comes back on its own: Docker restarts a crashed container
(`restart: unless-stopped`), systemd starts the stack on every boot (`vivo-gateway.service`), go2rtc
reconnects to a camera that comes back, Frigate's watchdog restarts its recorders, and the substream
archiver restarts its ffmpeg (M7). M10 proves it with a repeatable test and a gap log.

`tests/m10_recovery.py` runs the three kinds of events from the scope and writes every event to a
CSV as it happens:

| Events | How | Run from | Limit (recording back) |
|---|---|---|---|
| 10 gateway reboots | `systemctl reboot` | a workstation with SSH key access and sudo on the gateway | 180 s after the reboot command |
| 10 Frigate process kills | `kill -9` of Frigate's main process (Docker sees a crash) | the gateway | 60 s after the kill |
| 5 camera disconnects | the camera's PoE port off and on (`tools/poe.py`, or an operator when `POE_BACKEND=manual`), a different camera each time | the gateway | 120 s after its power comes back |

The substream may take up to 70 s longer: its segments start at the camera's next keyframe once the
archiver has reconnected.

The report lists, for every event and camera, when each stream recorded again, and a gap log with
every gap in the main stream and the substream over the test window and its cause (the event that
caused it, or a planned window). A gap nothing explains fails the test.

## Test

```bash
sudo tests/m10_recovery.py kill --count 10
sudo tests/m10_recovery.py disconnect --count 5 --off 20
tests/m10_recovery.py reboot --host vivo-n150 --count 10        # on the workstation
```

Each run writes its own report; `report --events FILE --events FILE` combines event files into one
gap log. Reboot times are read from the gateway's own clock, so the workstation's clock does not
matter. The whole run takes about 1.5 hours for the reboots, 20 minutes for the kills and 15 minutes
for the disconnects. Recording stops during each event: post the planned window first.

## Not included

Power cuts and disk failures (M11), restarting the gateway through Vivo's systems.
