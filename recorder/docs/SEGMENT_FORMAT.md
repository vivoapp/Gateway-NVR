# Substream segment format (M7 → cloud uploader M19/M20)

The contract between the gateway recorder (M7, Izzatullokh) and the cloud archive uploader
(M19 and M20, Haris). Change it only by written agreement between both developers, recorded in
the change log below and flagged to Simon the same day.

Status: **agreed** on Oct 19, 2026 in the Vivo Gateway/Recorder chat.

## Agreed

| Item | Rule |
|---|---|
| Folder | `/recordings/substream/<camera_id>/<YYYY-MM-DD>/` on the recording SSD. On the reference gateway the SSD is mounted at `MEDIA_ROOT` (`/srv/vivo`), so the full path is `/srv/vivo/recordings/substream/<camera_id>/<YYYY-MM-DD>/` |
| Camera ID | The camera's `id` in `cameras.yaml`: the same ID as in the M3 camera table and in Frigate (lowercase letters, digits and `_`) |
| File name | `<camera_id>_<YYYYMMDDTHHMMSS>Z.mp4`, the UTC start time of the segment. Example: `cam01_20261019T103000Z.mp4` |
| Segment length | 60 seconds |
| Finished marker | Each segment is written as `<name>.mp4.part` **in the same folder** and renamed to `<name>.mp4` only when complete. The rename is instant because both names are on the same disk. The uploader only picks up files ending in `.mp4` |
| Deletion | The gateway deletes segments after the local retention period (M9). The uploader only reads |
| Local retention | To be set by Haris (preliminary number by Oct 21), once Vivo gives the expected outage length and site upload speed |

## Details (confirmed by Haris on Oct 19, 2026)

- **Clock:** dates and times in folder and file names come from the gateway's UTC clock (NTP synchronized, M1), taken from the first frame of the segment. The date folder is the UTC date of the segment start.
- **Length tolerance:** segments are cut on keyframes, so one can be a couple of seconds longer or shorter than 60 s. The last segment before a camera outage can be short. A missing minute means nothing was recorded; there are no placeholder files.
- **Leftover `.part` files:** if the gateway restarts in the middle of a segment, the gateway cleans up the leftover `.part` file. It is never renamed later, so the uploader can always ignore `.part` files.
- **Reading safely:** a finished `.mp4` is never modified again. It can disappear only through retention, so the uploader should handle a file deleted between listing and reading.
- **Media:** the camera's substream video copied without transcoding, in a standard MP4. On the reference gateway that is H.265 (tagged `hvc1`, 640x360, 25 fps, keyframe every 5 s), because Vivo's test cameras send H.265; if Vivo switches the cameras to H.264, the segments become H.264 with no other change. Video only: the cameras' G.711 audio cannot go in MP4 (AAC on request). The uploader's container mounts the substream folder read-only. Samples: `/srv/vivo/samples/substream/` on the reference gateway.

## Change log

| Date | Change | Agreed by |
|---|---|---|
| 2026-10-19 | First version. Haris asked that `camera_id` match the M3 camera table and that `.part` files stay in the same folder; both adopted | Izzatullokh, Haris |
| 2026-10-19 | Haris confirmed the details (clock, length tolerance, leftover `.part` files, reading safely, media). His uploader ignores `.part` files and handles files deleted by retention | Izzatullokh, Haris |
| 2026-10-23 | Media: H.265 on the reference cameras (they send H.265, findings F5), video only; same folders, names and rules. Told Haris in the group chat, with sample segments | Izzatullokh |
