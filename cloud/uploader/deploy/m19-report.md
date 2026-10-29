# M19 acceptance report

Generated 2026-10-29 02:24:22Z. Bucket `vivo-test`, prefix `site-test`, endpoint `http://minio:9000`.

## Checks

| Check | Result | Detail |
|---|---|---|
| every finished local segment is uploaded | PASS | 250 of 250 uploaded |
| every uploaded checksum matches the local file | PASS | 250 of 250 checked by size, ETag and SHA-256 |
| the bucket holds exactly the uploaded segments | PASS | 250 objects, 250 uploaded |
| no segment is stuck (pending, conflict or missing) | PASS | none |
| no segment was uploaded twice (log) | PASS | 250 segments with an upload line |
| every uploaded segment has a correct index record | PASS | 250 of 250 have one |
| local files are unchanged | PASS | 170 unchanged, 0 removed by the recorder's retention |

**Overall: PASS**

## Measurements

- Run: 2026-10-29 02:16:48Z to 2026-10-29 02:23:54Z (0.12 h), 250 segments from 10 cameras, 262.1 MB.
- Upload rate: average 4.920 Mbps over the run; busiest minute 16.777 Mbps.
- From the uploader's own `event=stats` lines (7): CPU average 1.3% and maximum 4.5% of one core; memory maximum 57 MB; rate per minute average 4.683 Mbps, peak 17.764 Mbps; workers 2, speed limit 0 Mbps (0 = none).

