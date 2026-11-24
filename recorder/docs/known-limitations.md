# Known limitations

What does not work as the scope describes, what was not tested, and what I would do next. Status
as of Milestone 4; each item points to where it is explained.

## What does not work, or works differently

| # | Limitation | Effect | Where |
|---|---|---|---|
| 1 | The reference cameras send H.265 on both streams, although the scope's default is H.264 substreams and ONVIF reports H.264 | Recording works (stream copy) and Quick Sync decodes it, but browser live view of H.265 is not universal, and the substream segments for the cloud uploader are H.265 | findings F5; Block C plans an H.264 substream, agreed with the uploader developer |
| 2 | The reference gateway's recording volume is 1.2 TB of LVM across the NVMe OS disk and the 512 GB SATA SSD, not a separate 2 TB SSD | If either disk fails, the recordings are lost (the OS stays on the NVMe and still boots) | findings F4 |
| 3 | How many days fit depends on the scenes: about 38 days of main stream on the reference site (M25 measured 29.8 GB a day of main stream and 8.9 GB of substream at 10 cameras), about a day if every camera hit its bitrate cap | The storage guard keeps the disk under its safe limit by deleting the oldest footage first, so busy scenes mean fewer days, never a full disk | findings F7, docs/m8-storage.md, docs/milestone-4-results.md |
| 4 | Frigate restarts a stalled recorder only after about 2 minutes (not configurable) | The health endpoint raises `recording_gap` after 30 s, but nothing restarts the recorder sooner | findings F2, docs/m22-health.md |
| 5 | A power cut loses the segment being written: up to about 25 s of main stream (Frigate's cache in RAM) and 65 s of substream (one `.part` file) | Unavoidable without a UPS. The gateway must be set in the BIOS to power on after a power cut; the reference N150 switched itself on after all 18 real power cuts (8 in Milestone 3, 10 in M26) | docs/m11-faults.md, docs/deploy.md step 2, docs/milestone-4-results.md |
| 6 | The camera clocks are at 1970 and cannot reach their NTP server | Recording times come from the gateway's clock, so recordings are correct; any time overlay drawn by the cameras is wrong until their NTP points at the gateway | findings F6 |
| 7 | All recording goes through go2rtc | If go2rtc restarts, every camera's recording pauses for about 10 to 30 s (Docker restarts it if it crashes) | docs/adr/0001-standalone-go2rtc.md |
| 8 | The health endpoint is read-only, without a login, and on the LAN only | It shows alerts but does not send them anywhere; the Edge Agent (M21) is to read it | docs/m22-health.md |
| 9 | When the safe limit has to delete substream segments younger than `substream_days`, the storage guard only logs it | The uploader is not told, so footage it had not yet uploaded can be gone; requested by the uploader developer on Oct 29 | docs/m9-retention.md |
| 10 | Substream retention is 3 days: a 2-day internet outage plus a day of margin | A longer outage loses substream footage the uploader has not sent; raise `substream_days` (more disk) if longer outages are agreed | cameras.yaml |
| 11 | Only the Ubuntu release, the kernel series, Docker Engine, containerd, Compose and the images are pinned | Other Ubuntu packages follow Ubuntu's security updates; Docker's packages are held | versions.env |
| 12 | Frigate's internal API (port 5000) has no login | It is bound to 127.0.0.1; any process on the gateway can delete recordings through it | docs/m2-frigate.md |
| 13 | When the health service's call to go2rtc's stream list fails (5 s timeout), it shows `camera_offline` for every camera at once | A false alert for one poll, while recording goes on: 7 polls in 27 hours of M25 runs, no gap behind any of them. Fixed on Nov 24, 2026: a failed call now shows the cameras as unknown (`online` null, no alert), and only a stream list that fails for 30 s or more shows them offline | docs/milestone-4-results.md |
| 14 | No smart plug was available, so the M26 power cuts were switched by hand. Until Nov 24, 2026 the M11 power test noted the cut time when it asked for the cut, before the power went off | An unattended run needs a smart plug (`--plug-off`, `--plug-on`). Fixed on Nov 24, 2026: with an operator the cut time is noted when the operator confirms the power is off, and the power-on time before the operator is asked, so both can only make the result look worse; the Nov 20 report stays rebuilt from Jorge's posts | docs/m26-handoff.md, docs/milestone-4-results.md |

## What was not tested

- Other hardware and cameras: only the reference N150 (11.5 GB RAM usable; production will have
  16 GB) with ten cameras of one model (`A_ONVIF_CAMERA N501`). Not tested: an N100, other camera
  models, more than ten cameras.
- `setup.sh --format-disk` on real hardware: the reference gateway used `--create-lv` and
  `--add-disk` (findings F4).
- A disk that really fails: the missing-disk test switches the recording volume off. A disk that
  fails while still mounted (findings F1) is caught by the health endpoint (filesystem errors,
  SMART, segment age), but no disk was pulled or broken on purpose.
- The SSD-health and high-temperature alerts are simulated: a real SSD cannot be made to fail, and
  heating the N150 on purpose is not safe.
- An internet outage with the cloud uploader running: the uploader is not on the gateway yet.
- Long runs: the longest is the 24-hour M25 run. The runs at 4 and 8 cameras were 1 hour each, at
  night on site. The 30-day field pilot is not in scope.
- Upgrading Frigate, go2rtc or Docker and rolling back: the steps are documented (`versions.env`,
  docs/deploy.md), but have not been run.
- A deployment from a blank machine by someone other than me: this is M26's acceptance test.

## What I would do next

1. Switch the substreams to H.264 (Block C), and agree the change with the uploader developer.
2. Put the recordings on their own 2 TB SSD, separate from the OS disk.
3. Add a UPS, or at least enough hold-up time for a clean shutdown, to lose nothing in a power cut.
4. Add a recorder watchdog: restart a camera's recorder when its newest segment is older than about
   60 s, instead of waiting for Frigate's 2 minutes.
5. Turn on the hardware watchdog (systemd `RuntimeWatchdogSec`), so a hung gateway reboots itself.
6. Send alerts to Vivo through the Edge Agent (M21).
7. Point the cameras' NTP at the gateway.
8. Tell the uploader when the safe limit deletes substream segments early.
9. Test an upgrade and a rollback of Frigate on a spare gateway before the fleet rollout (M27, M28).
10. Size the disks from the rate M25 measures on real community scenes, not the office test scenes.
11. Done on Nov 24, 2026: the health endpoint's false offline polls (limitation 13).
12. Done on Nov 24, 2026: the M11 power test with an operator notes the cut time when the operator
    confirms the power is off (limitation 14).
