# deploy/ : the uploader on the gateway

`compose.yaml` runs the uploader (M19 + M20) on the gateway. `compose.test.yaml` and `test.sh` try the same
setup on a PC with fake storage. The older `docker-compose.yml` and `run.sh` one folder up are the evaluation
demo (they contain a throw-away MinIO login) and are for local testing only.

## What the gateway setup does

| Setting | Why |
|---|---|
| runs as user 10001, not root | a bug or attack cannot act as root |
| `/srv/vivo/recordings/substream` mounted read-only | the uploader can never change or delete the recorder's files |
| container filesystem read-only, all Linux rights dropped, `no-new-privileges` | nothing to modify or escalate |
| 1 CPU, 512 MB, 200 processes | cannot starve Frigate and go2rtc |
| logs rotate at 20 MB, 5 files | cannot fill the OS disk |
| secrets only from `/etc/vivo/cloud.env` | nothing secret in Git |
| base image pinned by version and digest, Python packages pinned | the gateway can be rebuilt exactly |

## Try it on your PC first

```bash
cd cloud/uploader/deploy
./test.sh start fast      # builds the image, starts MinIO + 10 simulated cameras (a segment every 5 s)
./test.sh check           # after about a minute: proves the settings above and compares the counts
./test.sh logs
./test.sh clean           # removes everything, including the generated test passwords
```

## M19 acceptance (on your PC, and later the same way on the gateway)

```bash
./test.sh start               # a segment per camera per minute (use "fast" for a quick look)
./test.sh snapshot            # remember the local files, to prove later they were not changed
# ... leave it running (24 hours for the real acceptance run) ...
./test.sh accept              # checksums, exactly once, index records, local files unchanged
                              # (while running, segments from the last 30 s count as "in flight" and are reported)
./test.sh pause               # stop the simulated cameras; wait about 10 seconds for the queue to empty
./test.sh accept --strict     # the final check: no grace, any difference fails
./test.sh resume
./test.sh report > m19-report.md      # the same, as a report with upload rate and CPU
./test.sh second              # also start a second store (second MinIO), same image and config file
./test.sh second-check        # both stores hold the same segments
```

On the gateway the same checker runs inside the container (it has the config, the secrets and the
read-only recordings): `docker compose exec -T uploader python tools/m19_acceptance.py report
--config /etc/vivo-uploader/config.toml --log - < <(docker compose logs --no-color uploader)`.
Add `--deep` to download and hash every object (slow, strongest).

## Install on the gateway (first time)

1. Get the code to `/opt/vivo-cloud` (the repo's `cloud/` folder).
2. `/etc/vivo/cloud.env` must be filled by Jorge or Izzatullokh (template: `cloud.env.example`) with the
   B2 endpoint, bucket, region, site prefix and the application key. Check that your user can read it:
   `head -c 0 /etc/vivo/cloud.env && echo readable`.
3. `mkdir -p /var/lib/vivo-cloud` if it does not exist, then give it to the container user once:
   `cd /opt/vivo-cloud/uploader/deploy && docker compose --profile setup run --rm init-data`
4. `docker compose up -d --build`, then `docker compose ps` and `docker compose logs -f uploader`.
5. In the first minutes you should see `event=detected` and `event=uploaded` lines.

## Things to confirm on the gateway (not testable on a PC)

- **Who can read the segments.** In rootless Docker the container user is not the owner of the recorder's files,
  so they count as "other". Folders need `r-x` and files `r--` for everyone (usually the default, 755 and 644).
  Check with `ls -ln /srv/vivo/recordings/substream | head`. If not, ask Izzatullokh.
- **`/etc/vivo/cloud.env` permissions.** The repo rule says root, mode 600, but then the `haris` user cannot read
  it. It needs to be readable by `haris` (for example group `haris`, mode 640). Ask Jorge.
- **Resource limits** need cgroup v2 delegation for rootless Docker. Jorge confirmed they work; `./test.sh check`
  style checks can be repeated with `docker inspect`.
- **B2 endpoint and bucket path** (Simon): `S3_ENDPOINT_URL`, `S3_BUCKET`, and what `S3_KEY_PREFIX` (site id) should be.

## Updating

Change the image tag (`vivo-cloud-uploader:0.1.0` in `compose.yaml` and `compose.test.yaml`) when the code changes,
then `docker compose up -d --build`. Update the base image tag and digest together in `Dockerfile`.
