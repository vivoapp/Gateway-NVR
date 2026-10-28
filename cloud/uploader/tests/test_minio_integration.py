"""Runs against a REAL MinIO (or any S3 server) when MINIO_ENDPOINT is set; skipped otherwise.

    ./run.sh test        # starts MinIO in Docker and runs everything, including this file

Why: the other tests use a stand-in server (moto) that does not check Content-MD5. These
tests show the real thing behaves as the service assumes.
"""
import base64
import hashlib
import os
import uuid

import pytest
from botocore.exceptions import ClientError

from conftest import FakeClock, make_service, seg_rel, step, write_segment
from segment_uploader.config import Config
from segment_uploader.storage import S3Storage

ENDPOINT = os.environ.get("MINIO_ENDPOINT")
pytestmark = pytest.mark.skipif(not ENDPOINT, reason="set MINIO_ENDPOINT to test against a real MinIO")


@pytest.fixture
def real_cfg(tmp_path):
    watch = tmp_path / "recordings"
    watch.mkdir()
    cfg = Config(
        watch_dir=watch, state_db=tmp_path / "state.db", endpoint_url=ENDPOINT,
        bucket=os.environ.get("MINIO_BUCKET", "vivo-segments"),
        access_key=os.environ.get("MINIO_USER", "minioadmin"),
        secret_key=os.environ.get("MINIO_PASSWORD", "minioadmin"),
        key_prefix=f"it-{uuid.uuid4().hex[:10]}",          # every test gets its own folder
        delete_after_upload=True, backoff_initial=1.0, backoff_max=8.0)
    yield cfg
    client = S3Storage(cfg).client                            # clean up what the test stored
    for page in client.get_paginator("list_objects_v2").paginate(Bucket=cfg.bucket, Prefix=cfg.key_prefix):
        for obj in page.get("Contents", []):
            client.delete_object(Bucket=cfg.bucket, Key=obj["Key"])


def remote_bytes(cfg, key):
    return S3Storage(cfg).client.get_object(Bucket=cfg.bucket, Key=key)["Body"].read()


def test_upload_round_trip_with_matching_etag(real_cfg):
    small, big = os.urandom(10_000), os.urandom(6 * 1024 * 1024)
    write_segment(real_cfg, seg_rel("cam01", 1), small)
    write_segment(real_cfg, seg_rel("cam01", 2), big)
    svc = make_service(real_cfg, FakeClock())

    step(svc)

    prefix = real_cfg.key_prefix
    assert remote_bytes(real_cfg, f"{prefix}/{seg_rel('cam01', 1)}") == small
    assert remote_bytes(real_cfg, f"{prefix}/{seg_rel('cam01', 2)}") == big
    head = S3Storage(real_cfg).head(f"{prefix}/{seg_rel('cam01', 2)}")
    assert head.etag == hashlib.md5(big).hexdigest()
    assert svc.store.counts() == {"uploaded": 2}
    assert not any(real_cfg.watch_dir.rglob("*.mp4"))         # removed only after verification
    svc.store.close()


def test_real_server_rejects_a_body_that_does_not_match_content_md5(real_cfg):
    storage = S3Storage(real_cfg)
    body = os.urandom(5000)
    wrong_md5 = base64.b64encode(hashlib.md5(b"something else").digest()).decode()
    path = real_cfg.watch_dir / "x.bin"
    path.write_bytes(body)
    key = f"{real_cfg.key_prefix}/{seg_rel('cam01', 9)}"

    with pytest.raises(ClientError) as err:
        storage.put(key, path, wrong_md5, hashlib.sha256(body).hexdigest(), len(body))

    assert err.value.response["Error"]["Code"] in ("BadDigest", "InvalidDigest")
    assert storage.head(key) is None                           # nothing was stored


def test_duplicate_and_conflict_against_real_server(real_cfg):
    real_cfg.delete_after_upload = False
    original = os.urandom(20_000)
    path = write_segment(real_cfg, seg_rel("cam01", 1), original)
    svc = make_service(real_cfg, FakeClock())
    step(svc, 4)                                               # rescans do not upload again
    key = f"{real_cfg.key_prefix}/{seg_rel('cam01', 1)}"
    first_etag = S3Storage(real_cfg).head(key).etag

    path.write_bytes(os.urandom(21_000))                       # same name, different bytes
    svc.scan_once(full=True)                                   # the hourly full scan notices it
    step(svc, 3)

    assert svc.store.get(key)["state"] == "conflict"
    assert remote_bytes(real_cfg, key) == original             # never overwritten
    assert S3Storage(real_cfg).head(key).etag == first_etag
    svc.store.close()
