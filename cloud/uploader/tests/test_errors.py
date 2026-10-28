"""What kind of failure is it? Storage-wide problems pause everything; file problems delay one file."""
import os

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError

from conftest import seg_key, seg_rel, step, write_segment
from helpers import CountingStorage
from segment_uploader.storage import S3Storage, classify


def client_error(code, status, op="PutObject"):
    return ClientError({"Error": {"Code": code, "Message": "x"},
                        "ResponseMetadata": {"HTTPStatusCode": status}}, op)


@pytest.mark.parametrize("exc, kind", [
    (EndpointConnectionError(endpoint_url="http://x"), "outage"),
    (client_error("InternalError", 500), "outage"),
    (client_error("SlowDown", 503), "outage"),
    (client_error("InvalidAccessKeyId", 403), "auth"),
    (client_error("SignatureDoesNotMatch", 403), "auth"),
    (client_error("NoSuchBucket", 404), "auth"),
    (client_error("bad_auth_token", 401), "auth"),
    (client_error("storage_cap_exceeded", 403), "full"),       # a 403 that means "full", not "denied"
    (client_error("XMinioStorageFull", 507), "full"),
    (client_error("QuotaExceeded", 400), "full"),
    (client_error("BadDigest", 400), "file"),
    (client_error("EntityTooLarge", 400), "file"),
    (ValueError("anything else"), "file"),
])
def test_classify(exc, kind):
    assert classify(exc) == kind


class FailingStorage(CountingStorage):
    def __init__(self, cfg, error):
        super().__init__(cfg)
        self.error, self.heads = error, 0

    def head(self, key):
        self.heads += 1
        if self.error:
            raise self.error
        return super().head(key)


@pytest.mark.parametrize("error, event", [
    (client_error("InvalidAccessKeyId", 403, "HeadObject"), "storage_refused_credentials"),
    (client_error("storage_cap_exceeded", 403, "HeadObject"), "storage_full"),
])
def test_wrong_credentials_or_full_storage_pause_the_whole_service(cfg, s3, new_service, clock, caplog, error, event):
    cfg.workers = 1
    data = {c: os.urandom(3000) for c in ("cam01", "cam02", "cam03")}
    paths = {c: write_segment(cfg, seg_rel(c, 1), d) for c, d in data.items()}
    storage = FailingStorage(cfg, error)
    svc = new_service(storage)

    with caplog.at_level("INFO"):
        step(svc)
    assert storage.heads == 1                              # first failure stopped everything
    assert any(event in r.getMessage() for r in caplog.records if r.levelname == "ERROR")
    assert svc.store.counts() == {"pending": 3}
    assert all(p.exists() for p in paths.values())
    assert svc._pause_until - clock() >= cfg.backoff_max * 0.5   # a long pause, not a 1 s blip

    step(svc, 3)                                           # still paused: nothing is tried again
    assert storage.heads == 1

    storage.error = None                                   # credentials fixed / space freed
    clock.advance(cfg.backoff_max + 1)
    step(svc)
    assert svc.store.counts() == {"uploaded": 3}
    assert s3.objects() == {seg_key(c, 1): d for c, d in data.items()}


def test_a_problem_with_one_file_delays_only_that_file(cfg, s3, new_service):
    class OneBadFile(CountingStorage):
        def put(self, key, *a, **k):
            if "cam02" in key:
                raise client_error("BadDigest", 400)
            return super().put(key, *a, **k)

    for c in ("cam01", "cam02", "cam03"):
        write_segment(cfg, seg_rel(c, 1), os.urandom(2000))
    svc = new_service(OneBadFile(cfg))
    step(svc)
    assert svc.store.counts() == {"uploaded": 2, "pending": 1}
    assert svc._pause_until == 0


def test_remote_delete_is_refused_outside_our_own_prefix(cfg, s3):
    storage = S3Storage(cfg)
    s3.client.put_object(Bucket=s3.bucket, Key="someone-else/file.mp4", Body=b"keep me")
    with pytest.raises(ValueError, match="outside the prefix"):
        storage.delete("someone-else/file.mp4")
    assert "someone-else/file.mp4" in s3.objects()
    storage.delete(seg_key("cam01", 1))                    # our own prefix is allowed (no error)
