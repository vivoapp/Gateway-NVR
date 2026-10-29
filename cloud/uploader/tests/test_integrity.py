"""Integrity: damaged or mismatching uploads are never accepted and never cause local deletion."""
import hashlib
import os

from conftest import seg_key, seg_rel, sha, step, write_segment
from helpers import LyingHeadStorage, ModifyLocalDuringPutStorage, TamperingStorage
from segment_uploader.storage import S3Storage

KEY = seg_key("cam01", 1)


def test_checksums_recorded_on_the_server(cfg, s3, new_service):
    data = os.urandom(50_000)
    write_segment(cfg, seg_rel("cam01", 1), data)
    step(new_service())

    head = s3.client.head_object(Bucket=s3.bucket, Key=KEY)
    assert head["ETag"].strip('"') == hashlib.md5(data).hexdigest()
    assert head["Metadata"]["sha256"] == sha(data)


def test_damaged_upload_is_rejected_then_succeeds_when_fixed(cfg, s3, new_service, clock):
    data = os.urandom(50_000)
    path = write_segment(cfg, seg_rel("cam01", 1), data)
    svc = new_service(TamperingStorage(cfg))

    step(svc)

    row = svc.store.get(KEY)
    assert row["state"] == "pending" and row["last_error"]    # failed, will be retried
    assert path.exists()                                       # local copy kept
    assert KEY not in s3.objects()                             # no bad object left behind

    svc.storage = S3Storage(cfg)                               # the problem goes away
    clock.advance(60)
    step(svc)

    assert s3.objects() == {KEY: data}
    assert svc.store.get(KEY)["state"] == "uploaded" and path.exists()


def test_mismatch_found_after_upload_removes_object_and_keeps_local_file(cfg, s3, new_service, clock):
    data = os.urandom(50_000)
    path = write_segment(cfg, seg_rel("cam01", 1), data)
    storage = LyingHeadStorage(cfg)
    svc = new_service(storage)

    step(svc)

    row = svc.store.get(KEY)
    assert row["state"] == "pending" and "IntegrityError" in row["last_error"]
    assert path.exists()
    assert KEY not in s3.objects()                             # bad object cleaned up


def test_file_changed_during_upload_is_not_deleted(cfg, s3, new_service):
    data = os.urandom(50_000)
    path = write_segment(cfg, seg_rel("cam01", 1), data)
    svc = new_service(ModifyLocalDuringPutStorage(cfg))

    step(svc)

    row = svc.store.get(KEY)
    assert row["state"] == "conflict" and "changed during upload" in row["last_error"]
    assert path.exists() and path.read_bytes().startswith(data)    # nothing lost locally


def test_file_that_disappears_before_upload_is_reported_not_crashed(cfg, s3, new_service):
    path = write_segment(cfg, seg_rel("cam01", 1), os.urandom(1000))
    svc = new_service()
    svc.scan_once()
    svc.scan_once()                                            # registered, upload pass not run yet
    path.unlink()                                              # file vanishes (disk cleanup, bug, ...)

    svc.upload_due()

    row = svc.store.get(KEY)
    assert row["state"] == "missing" and "vanished" in row["last_error"]


def test_server_rejecting_a_bad_checksum_is_a_file_error_not_an_outage(cfg, s3, new_service):
    """Real S3/MinIO answer 400 BadDigest when the body does not match Content-MD5."""
    from botocore.exceptions import ClientError

    class BadDigestStorage(S3Storage):
        def put(self, *a, **k):
            raise ClientError({"Error": {"Code": "BadDigest", "Message": "checksum mismatch"},
                               "ResponseMetadata": {"HTTPStatusCode": 400}}, "PutObject")

    path = write_segment(cfg, seg_rel("cam01", 1), os.urandom(2000))
    svc = new_service(BadDigestStorage(cfg))
    svc.run_once()
    svc.run_once()
    row = svc.store.get(KEY)
    assert row["state"] == "pending" and "BadDigest" in row["last_error"]
    assert svc._pause_until == 0                      # service did not pause: other files can proceed
    assert path.exists()


def test_a_segment_already_uploaded_is_not_reported_missing_when_retention_removes_it_during_a_recheck(
        cfg, s3, new_service):
    """Seen on a PC: the file changed on disk after its upload, so it was queued for a re-check; the
    recorder's retention deleted it before the re-check ran. The verified copy is in the bucket: no gap."""
    path = write_segment(cfg, seg_rel("cam01", 1), os.urandom(3000))
    svc = new_service()
    step(svc, 2)
    assert svc.store.get(KEY)["state"] == "uploaded"

    path.write_bytes(os.urandom(4000))                     # same name, rewritten
    svc.scan_once(full=True)                               # noticed: queued for a re-check
    assert svc.store.get(KEY)["state"] == "pending"
    path.unlink()                                          # retention removes it first
    svc.upload_due()

    assert svc.store.get(KEY)["state"] == "uploaded"       # not "missing"
    assert len(s3.objects()) == 1
