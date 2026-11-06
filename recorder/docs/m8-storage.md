# M8: storage layout and disk management

## Folder layout

Recording disk, mounted at `MEDIA_ROOT` (`/srv/vivo` on the reference gateway; ext4, by UUID,
`nofail`; the empty mount point is immutable, so nothing is written to the OS disk when the
recording disk is missing, docs/findings.md F1):

| Folder | What | Written by | Kept |
|---|---|---|---|
| `frigate/media/recordings/YYYY-MM-DD/HH/<camera>/MM.SS.mp4` | main stream, 10 s files, UTC | Frigate | `record_days` (7) |
| `frigate/media/exports/` | clips exported through Frigate's API | Frigate | `export_days` (7) |
| `frigate/media/clips/previews/<camera>/` | Frigate's low-resolution timeline previews, one file per camera and hour (about 0.9 GB a day at 10 cameras); the other `clips/` folders stay empty with detection off | Frigate | with the main stream, deleted by Frigate |
| `frigate/state/` | Frigate database, caches, its own config backups | Frigate | always |
| `recordings/substream/<camera>/YYYY-MM-DD/` | substream segments for the uploader | `substream` | `substream_days` (3) |
| `recordings/substream/early-deletions.jsonl` | substream deleted before `substream_days` ended (docs/m9-retention.md) | `storage` | always |
| `samples/` | sample files for other developers, made by hand | people | until removed |

OS disk:

| Folder | What |
|---|---|
| `/opt/vivo-gateway` | this repository as installed by `gateway/setup.sh` (compose files, generated configs, tools) |
| `/opt/vivo-gateway/reports` | test reports |
| `/etc/vivo` | `site.env` and `secrets.env` (root, 600), the other services' secret files |
| `/var/lib/docker` | images and container logs (each container's log capped at 5 x 20 MB) |

`gateway/setup.sh` makes the recording-disk folders; `tests/m1_check.sh` checks the mount.

If the recording disk goes away while running, the recorder stops (nothing is written to the OS disk) and starts again on its own when the disk returns (M11, docs/m11-faults.md).

## Safe limit

Recordings live on their own filesystem, so filling it cannot fill the OS disk, but Frigate's
database is on it too, and a full disk stops Frigate. The `storage` service
(`tools/storage_guard.py`, once a minute) keeps it below the safe limit:

- Above `disk_limit_percent` (90 %) used, it deletes the oldest footage first, one clock hour
  at a time, down to `disk_target_percent` (85 %): main-stream hours through Frigate's API
  (`DELETE /api/recordings/start/<s>/end/<e>?cameras=all`, which removes the files and the database
  rows together) and substream segments directly. Footage from the last hour is never deleted.
  Substream deleted this way before its retention ended is listed in
  `recordings/substream/early-deletions.jsonl` (docs/m9-retention.md).
- Frigate's own low-space cleanup (it starts when less than about one hour of recording fits)
  stays as a second safety net.
- Old exports are deleted through Frigate's API (`POST /api/exports/delete`) after `export_days`.
- The numbers are in the `site` section of `cameras.yaml` and reach the service through the
  generated `services/config.json`.

## Disk use and SSD health

```bash
sudo vivo-disk            # recording disk per folder, safe limit, days stored, OS disk, SMART
sudo vivo-disk --fast     # without the folder sizes
sudo vivo-disk --json     # for M22
```

The exit code is 1 when the recording disk is at or above its safe limit or an SSD reports a
SMART problem (overall health, media errors, reallocated or pending sectors). SMART comes from
`smartctl` (smartmontools) for every disk under the recording volume and the OS volume, through
LVM.

## Test

```bash
sudo vivo-disk                       # the documented command works, layout as above
sudo tests/m9_pressure.py            # at the limit: OS, Docker and Frigate keep running (M9)
sudo tests/m1_check.sh && sudo tests/m2_check.py
```

## Not included

Sending disk alerts to Vivo (M22), choosing or buying the SSD.
