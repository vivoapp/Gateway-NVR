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

## M20 recovery (on your PC, and later the same way on the gateway)

```bash
./test.sh gaps                # gap report of the running uploader: never uploaded AND the file is gone
./test.sh recovery --quick    # about 6 minutes: all 18 tests with short faults, to see that it works
./test.sh recovery            # the real run, about 45 minutes (leave the window open)
./test.sh recovery --outage-days 3      # if the agreed outage length is 3 days (default 2)
./test.sh recovery-report > m20-report.md
```

The 18 tests are 5 internet cuts (C1-C5, C5 is an outage of the agreed length), 5 gateway reboots (R1-R5),
5 uploader kills (K1-K5) and 3 storage errors (S1-S3). `./test.sh recovery --list` prints them. Each one is
checked from the outside afterwards (all M19 checks in strict mode plus the gap report), and the time the
uploader needed to catch up is measured. The report ends with the calculated catch-up times and the
retention recommendation for M9. On the gateway the same script can run inside the uploader image (it needs the same `S3_*` variables from
`/etc/vivo/cloud.env`, a writable work folder, and it uses its own key prefix `m20-<time>`, so it never touches the
real segments). This has not been tried on the gateway yet; the exact command is agreed when the B2 bucket exists.
Do not run it on a gateway that is recording, and delete the `m20-*` objects afterwards.

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
