# Workstation lab: the recorder with ten fake cameras

Runs the whole recorder stack (go2rtc, Frigate, the substream archiver, the storage guard) in
Docker on a workstation, with ten fake cameras instead of real ones, to develop and test without
the gateway. A second go2rtc (`fakecam`) loops test clips over RTSP; the recorder's go2rtc pulls
them exactly as it pulls the real cameras. The clips copy what Vivo's test cameras send
(docs/findings.md F5): H.265 with a keyframe every 5 s and no B-frames, 2880x1620 main stream
with G.711 audio, 640x360 substream.

Numbers from the lab are not the gateway's: there is no Intel GPU, so Frigate decodes on the CPU.

On macOS, keep the lab short and small. Docker Desktop writes every recording through its file
sharing, and with ten cameras for more than an hour its backend reached about 11 cores on a
6-core, 12-thread MacBook (Oct 29, 2026); with `VIVO_LAB_DISK_BYTES` set, the storage guard also reads every
file's size each minute. Use `tests/lab/lab.sh up 4` for longer runs, and `lab.sh down` when done.

## Use

```bash
tests/lab/lab.sh up             # makes the clips (once, needs ffmpeg with libx265), settings, starts 10 cameras
tests/lab/lab.sh up 4           # only the first 4 cameras
eval "$(tests/lab/lab.sh env)"  # then the checks read the lab's settings and camera list
FRIGATE_API=http://127.0.0.1:5000/api tests/m7_check.py --hours 0.25
tests/lab/lab.sh compose logs -f substream
tests/lab/lab.sh down
```

`LAB_DIR` (default `$TMPDIR/vivo-lab`) holds everything: `etc/` (site.env and random lab
passwords), `media/` (the recording folder, MEDIA_ROOT), `project/` (a copy of this repository
with `tests/lab/fakecams.yaml` as the camera list) and `clips/`. `lab.sh up` copies the repository
again, so code changes reach the lab with `lab.sh up` or `lab.sh prepare` and a restart.

## Disk-pressure test

The storage guard reads the use of the disk the recordings are on. Docker on a workstation
reports the workstation's own disk for any folder (also for a mounted disk image), so the lab
pretends instead: with `VIVO_LAB_DISK_BYTES` set, the guard, `vivo-disk` and the pressure test
treat the recording folder as a disk of that size, used by the files in it. This tests the whole
cleanup (oldest first, Frigate's API, recording carrying on); the operating system's behavior on a
really full disk can only be tested on the gateway.

A new lab has no footage older than an hour, and the guard never deletes the last hour, so
`tests/lab/seed_footage.py` first puts copies of lab recordings into earlier hours:

```bash
export VIVO_LAB_DISK_BYTES=6000000000      # a 6 GB pretend disk
tests/lab/lab.sh up                        # the storage service gets the same setting
eval "$(tests/lab/lab.sh env)"
tests/lab/seed_footage.py --hours 6 --mb-per-hour 200
FRIGATE_API=http://127.0.0.1:5000/api tests/m9_pressure.py --max-filler-gb 3
```

The filler file is real data on the workstation's disk (about 2.5 GB here) and is deleted at the end.

## Benchmark smoke test

```bash
FRIGATE_API=http://127.0.0.1:5000/api LAB_CLIPS=$LAB_DIR/clips tests/m6_benchmark.py \
  --project "$LAB_DIR/project" --compose-file tests/lab/compose.lab.yaml \
  --compose-file tests/lab/compose.fakecam.yaml --sampler-container go2rtc \
  --counts 2,4 --rounds 1 --warmup 30 --minutes 1
```
