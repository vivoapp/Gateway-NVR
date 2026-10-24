# M2: Frigate installation and configuration

## What it does

- Frigate 0.18.0 in Docker Compose, pinned by tag and digest (`versions.env`).
- One config file, `frigate/config.yml`, generated from `cameras.yaml` and mounted **read-only**:
  nothing can be changed by hand in the Frigate UI. (Frigate logs one harmless line at start,
  "Config file is read-only, unable to migrate config file"; the file is already in the 0.18 format.)
- Continuous recording of each camera's main stream; detection, snapshots, audio events,
  Birdseye and all AI features off. Version check (outbound call to GitHub) off.
- Decoding through Intel VAAPI (`preset-vaapi`, the Frigate recommendation for N100/N150 graphics).
- Recordings and the Frigate database on the recording SSD (`MEDIA_ROOT/frigate`).
- UI and API on the LAN only: port 8971 (Frigate login), port 5000 bound to 127.0.0.1.

## Test

```bash
sudo tests/m2_check.py --kill-test   # config, recording, GPU decode, ports, self-restart
sudo reboot                          # then run tests/m2_check.py again
```

The kill test kills Frigate's main process from the host. Docker treats that as a crash and
restarts it (a plain `docker kill` would count as a manual stop and is not used).

## Not included

Frigate+ and AI models, the Video API adapter (M14), remote access to the Frigate UI.
