# Vivo Gateway NVR

Software for the Vivo CAM edge gateway (reference unit: Intel N150, Ubuntu Server 24.04 LTS).
Each part lives in its own folder, owned by one developer, and is deployed as its own Docker
Compose project on the gateway.

| Folder | What | Modules | Owner | Installed on the gateway at |
|---|---|---|---|---|
| [`recorder/`](recorder/) | Linux baseline, Frigate recording, go2rtc live streams, camera ingestion, storage, health | M1 to M12, M22, M25, M26 | Izzatullokh | `/opt/vivo-gateway` |

Other parts (cloud upload and agent, video API) get their own folder when their owners add them.

## Shared contracts

Agreements between the parts. Change them only by written agreement between the owners.

- [Substream segment format](recorder/docs/SEGMENT_FORMAT.md): the recorder writes one-minute
  substream segments that the cloud uploader reads (`/srv/vivo/recordings/substream`).

## Rules for everyone

- No passwords, keys or tokens in Git. Secrets live in `/etc/vivo/*.env` on the gateway (root, mode 600).
- Pin every image by version and digest, so a gateway can be rebuilt exactly.
- Tell the group before a reboot, a firewall change or an internet cut on the shared gateway.
