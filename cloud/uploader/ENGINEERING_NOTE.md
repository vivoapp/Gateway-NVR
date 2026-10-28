> Historical note from the technical evaluation. It describes the first version, not the current M19/M20 code (see README.md and CHANGELOG.md).

# Engineering note: Segment Uploader

Candidate: Haris H
Date: 12-10-2026

## What I built

A small Linux service that watches a folder for finished video segments (.mp4, .ts, .mkv) and uploads them to S3-compatible storage. I used MinIO for the demo. It keeps the status of every segment in a local SQLite file, so it can retry after a network failure and carry on after a restart.

## Architecture choices

- Python 3.11+, boto3 and SQLite. No framework. SQLite gives durable status without another service to run.
- The object key (prefix + relative path) is the identity of a segment. Seeing the same file again, or restarting, cannot create a second upload.
- Before uploading, the service asks the bucket if the key already exists. If the same object is there (for example the process died after the upload but before saving its state), it is accepted and not uploaded again. A different object under the same key is marked as a conflict and never overwritten.
- Two integrity checks: Content-MD5 on upload, so the server can reject a damaged body, and then a read-back of size and ETag. The SHA-256 is stored as object metadata.
- State is written to the database before each step that can fail, and the local file is deleted last, and only after the check passes. A crash can only cause a re-check, never lost data.
- One failure policy. If the storage is unreachable, the whole service pauses and backs off (the delay doubles up to a limit). If only one file has a problem, only that file backs off.

## How it is run and tested

At first I ran everything by hand: MinIO in one terminal, the uploader in another, and a fake recorder in a third. That worked but was slow and hard to repeat, so I automated it.

- `./run.sh start` builds the image and starts MinIO, the bucket, the uploader and a fake recorder in Docker.
- `./run.sh demo` runs the whole story by itself: normal uploads, MinIO stopped, the uploader killed with SIGKILL, everything restarted, then a comparison. It prints PASS only if every segment is in the bucket exactly once and no local files are left.
- `./run.sh test` runs the automated tests in Docker, including three tests against the real MinIO.
- `./run.sh status`, `logs`, `stop` and `clean` do what their names say.

Tests (pytest) cover success, outage and restart, duplicate prevention and integrity. Most use a local stand-in S3 server (moto). Three use the real MinIO, including one that checks that MinIO rejects a body that does not match its Content-MD5.

## Problems I hit and how I fixed them

1. MinIO image not found. `docker compose up` failed with "pull access denied for minio/minio". This was a Docker image problem, not a bug in my code: the official minio/minio and minio/mc images are no longer on Docker Hub. I now use the cgr.dev/chainguard/minio image, which is a third-party build, and the image name can be changed with the MINIO_IMAGE variable. Because mc is gone too, the bucket is now created by a small boto3 script in the app image.
2. A test failed on Windows. One outage test looked for the Linux wording "Could not connect" in the error. On Windows the failed connection shows up as a timeout instead, so the test failed even though the service behaved correctly. I changed the test to accept both.
3. A bug found by the Docker test run. When several new segments were found in the same scan, they were registered in the order the file system listed them, and that order differs between systems. During an outage only the first segment is retried, so a newer segment could be retried while an older one waited. The scan now looks at files oldest first (by modified time, then name), so segments upload in the order they were recorded. I added a test for this that fails without the fix. I reproduced the failure on Linux by forcing the directory listing into reverse order.
4. The demo's final check compared the uploaded count with everything in the bucket, so old objects from earlier runs made it report a failure. It now ignores objects that were there before the demo started.

## AI tools used

I used Claude (Anthropic) to write the first version of the code, tests and Docker setup from the assignment text, and to help find and fix the problems above.
I ran the code myself and reviewed the changes.

## Work I personally verified

- Ran the tests on my Windows machine. They showed the failure in point 2, which I fixed.
- Ran MinIO in Docker and the service against it, by hand and then with `./run.sh`: normal uploads, MinIO stopped and started again, and the uploader killed and restarted.
- Ran `./run.sh demo` on my machine. The uploads, the outage retries and the recovery after the kill worked, and nothing was uploaded twice. The first run reported a failure only because of old objects in the bucket (point 4).
- Ran `./run.sh test` in Docker. 26 of 27 tests passed and the failure led to the fix in point 3. [After the fix, `./run.sh test` result: write the real result here.]
- Confirmed in the MinIO console that the files in the bucket match what the log says was uploaded.

## Another version

My colleague Sarin Sukumar built a separate version of this task. It has its own storage server and a video player, and it uploads only the files made by its own recorder, not to S3. It does not match this brief (S3-compatible storage, watching a normal folder), so I built this one instead. I took one idea from his work, the one-command `run.sh`. No code was copied. I can send his version too if you want it.

## Limitations

- Uploads go one at a time from one worker, so throughput is limited to one connection.
- One PUT per file (S3 limit 5 GiB). No multipart upload and no resume of a half-sent large file.
- Uploaded objects are not checked again later. If one is lost on the server afterwards, the service will not notice.
- Finished-file detection is a check on size and age, so the recorder should write a .part file and rename it when done.
- A conflict or a missing file needs a person to decide. There are no alerts.
- The MinIO image comes from a third party now, and the official images are gone. Check the image before using this anywhere real.

## Actual time spent

Total: about 4 hours 30 minutes.

- 30 min: reviewed a teammate's (Sarin's) version of the project and decided not to use it, because it went beyond what the brief asked for.
- 1 hour: built my own version, with help from Claude.
- 2 hours: testing and bug fixing (Windows test failure, MinIO image problem, file-order bug, demo check).
- 1 hour: final testing, the screen recording and updating the documents.

Note: these hours are approximate. I worked on it between about 10 AM and 7 PM with breaks in between, and the hours above are the time I actually spent working on it.