# M11: power-loss and disk fault testing

## What it does

`tests/m11_faults.py` runs the four faults from the scope and writes a report for each:

| Fault | How | Passes when |
|---|---|---|
| Power loss | cuts with Vivo's smart plug (`--plug-off`, `--plug-on` commands) or by hand (an operator), 15 s off; or `--crash`, immediate reboots that skip writing out memory (sysrq b), the same for unsaved data but the SSD keeps power | the gateway boots and records again within 180 s of power coming back; at most the segment being written is lost (25 s of main stream: one 10 s segment plus Frigate's cache; 65 s of substream: one 60 s `.part`), counting a file the cut left empty or broken as lost (the report lists them); no partial substream file was kept as finished; Frigate's index lists no broken file near a cut; no ext4 errors |
| Full disk | `tests/m9_pressure.py`, then checks | the M9 cleanup protects the system at the safe limit, and after the filler file is removed Frigate answers, both streams record, the filesystem has no errors and the OS disk is unaffected |
| Missing disk | the recorder is stopped, the recording disk unmounted and its LVM volume switched off, then on again | the recorder refuses to start without its disk (nothing is written to the OS disk), the health endpoint shows `disk_missing`, and when the volume is back `vivo-disk-watch.timer` mounts it and starts the recorder without anyone logging in |
| Corrupted file | one main-stream segment from about 20 minutes ago, in a stretch without gaps, is cut to 60 % of its size (a copy is put back at the end) | the damaged file is reported broken (its MP4 boxes no longer add up to its size, `tools/mp4box.py`), a clip and the HLS playlist across it still play, and the segments just before and after it play |

## How a power cut is handled

Found with the power cuts on the reference gateway on Nov 6 (docs/milestone-3-results.md):

- Linux keeps written data in memory for up to 30 s by default, so a cut lost up to about 50 s of
  footage that Frigate had already moved to the disk. The `setup.sh` disk step now writes data out
  after 3 s (`/etc/sysctl.d/90-vivo-recording.conf`) and commits the recording disk's journal every
  2 s (`commit=2`): new footage is on the disk within about 6 s.
- A segment saved in those last seconds can still come back empty or damaged. Before the recorder
  starts, `bin/vivo-clean-empty` removes empty segments from before the boot. Once Frigate is up,
  `vivo-cut-cleanup.service` (`tools/cut_cleanup.py`) takes the main-stream segments from the 10
  minutes before the boot whose files are missing, empty or not whole out of Frigate's index,
  through Frigate's own API; otherwise a clip across one stops there and the HLS playlist lists a
  part that answers 404. Each delete stays 1 s inside the segment and only goes ahead when no healthy
  recording overlaps it.
- A substream segment finished in those seconds can lose its index too (ffprobe: "moov atom not
  found"). It is left in place; M12 lists it, and the report of the power test counts it as lost.

## How a missing disk is handled

- `vivo-gateway.service` is bound to the recording disk's mount (`BindsTo`): when the disk goes away,
  systemd stops the stack, and the stack cannot start without it, so nothing is ever written to the
  OS disk (the empty mount point is also immutable, M1).
- `vivo-disk-watch.timer` runs `bin/vivo-disk-watch` every 30 s. It notices the missing disk and,
  once the disk's device is back, starts the stack again; systemd mounts the disk on the way. It
  only restarts a stack it saw lose its disk, so a planned `systemctl stop vivo-gateway` is not undone.
- For planned disk work, create `/etc/vivo/maintenance` (the watch then does nothing) or stop the timer.
- The health endpoint (M22) reports `disk_missing` while the disk is away.

## Test

```bash
tests/m11_faults.py power --host vivo-n150 --count 10 --plug-off "..." --plug-on "..."   # workstation
tests/m11_faults.py power --host vivo-n150 --count 5 --crash                # no plug or person needed
sudo tests/m11_faults.py report --events FILE                              # rebuild a power report
sudo tests/m11_faults.py full-disk
sudo tests/m11_faults.py missing-disk --yes
sudo tests/m11_faults.py corrupt --camera cam01
```

The power test needs the smart plug (Vivo provides it) and someone who can reach the gateway if it
does not come back. The missing-disk test stops recording for a few minutes and needs the recording
disk on LVM, as on the reference gateway; for a plain disk, unplug it by hand and check the same
points. Post the planned windows first.

## Not included

Replacing a failed SSD in the field, data recovery from a dead disk.
