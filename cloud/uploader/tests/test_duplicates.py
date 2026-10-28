"""A logical segment (identified by its key) is uploaded once, never twice, never overwritten."""
import os
import time

from conftest import seg_key, seg_rel, step, write_segment
from helpers import CountingStorage
from segment_uploader.storage import S3Storage

KEY = seg_key("cam01", 1)


def test_same_segment_uploaded_once_across_rescans_and_restarts(cfg, s3, new_service):
    data = os.urandom(10_000)
    write_segment(cfg, seg_rel("cam01", 1), data)

    storage = CountingStorage(cfg)
    svc = new_service(storage)
    step(svc, 6)
    svc.store.close()

    svc2 = new_service(storage)                    # restart with the same state file
    svc2.recover()
    step(svc2, 6)

    assert storage.puts == 1
    assert s3.objects() == {KEY: data}


def test_existing_identical_object_is_adopted_not_uploaded_again(cfg, s3, new_service):
    """E.g. the state file was lost, but the object is already in the bucket."""
    data = os.urandom(10_000)
    path = write_segment(cfg, seg_rel("cam01", 1), data)
    s3.client.put_object(Bucket=s3.bucket, Key=KEY, Body=data)

    storage = CountingStorage(cfg)
    svc = new_service(storage)
    step(svc)

    assert storage.puts == 0
    assert svc.store.get(KEY)["state"] == "uploaded"
    assert path.exists()                           # the local file is never touched


def test_touching_a_file_does_not_cause_a_second_upload(cfg, s3, new_service):
    path = write_segment(cfg, seg_rel("cam01", 1), os.urandom(10_000))
    storage = CountingStorage(cfg)
    svc = new_service(storage)
    step(svc)

    later = time.time() + 100
    os.utime(path, (later, later))                 # new mtime, same content
    svc.scan_once(full=True)                       # a full scan looks at every file again
    step(svc, 3)

    assert storage.puts == 1
    assert svc.store.get(KEY)["state"] == "uploaded"


def test_different_content_under_same_key_is_flagged_and_never_overwrites(cfg, s3, new_service):
    original = os.urandom(10_000)
    path = write_segment(cfg, seg_rel("cam01", 1), original)
    storage = CountingStorage(cfg)
    svc = new_service(storage)
    step(svc)

    replacement = os.urandom(12_000)
    path.write_bytes(replacement)                  # recorder reused the file name
    svc.scan_once(full=True)                       # the hourly full scan notices the change
    step(svc, 3)

    assert storage.puts == 1
    assert s3.objects() == {KEY: original}         # remote data untouched
    row = svc.store.get(KEY)
    assert row["state"] == "conflict" and "not overwritten" in row["last_error"]
    assert path.read_bytes() == replacement        # local data untouched too


def test_identical_content_in_two_files_are_two_segments(cfg, s3, new_service):
    data = os.urandom(5_000)
    write_segment(cfg, seg_rel("cam01", 1), data)
    write_segment(cfg, seg_rel("cam02", 1), data)
    svc = new_service()
    step(svc)
    assert sorted(s3.objects()) == [seg_key("cam01", 1), seg_key("cam02", 1)]
