"""Network outage, retry with backoff, and recovery after a service restart."""
import os
import threading
import time

import pytest

from datetime import datetime, timezone

from conftest import BASE, seg_key, seg_rel, sha, step, write_segment
from helpers import Crash, CountingStorage, CrashAfterPutStorage
from segment_uploader.service import Service



def test_outage_keeps_files_and_retries_with_growing_backoff(cfg, s3, new_service, clock):
    svc = new_service()
    first = write_segment(cfg, seg_rel("cam01", 1), os.urandom(5000))
    step(svc)
    assert not first.exists()                      # uploaded and removed while the server was up

    s3.stop()                                      # --- network outage ---
    data = {n: os.urandom(5000) for n in (2, 3)}
    paths = {n: write_segment(cfg, seg_rel("cam01", n), d) for n, d in data.items()}
    step(svc)

    assert svc.store.counts() == {"uploaded": 1, "pending": 2}
    assert all(p.exists() for p in paths.values())           # nothing deleted without confirmation
    row = svc.store.get(seg_key("cam01", 2))
    assert row["attempts"] == 1
    assert "Could not connect" in row["last_error"]          # a real connection failure

    # While the pause lasts, no new attempts are made (no hammering of a dead server).
    svc.run_once()
    assert svc.store.get(seg_key("cam01", 2))["attempts"] == 1

    # Backoff doubles: 1s, 2s, 4s, 8s, then stays at the 8s cap.
    delays = []
    for _ in range(5):
        clock.advance(10)                                    # past any pause
        svc.run_once()
        row = svc.store.get(seg_key("cam01", 2))
        delays.append(row["next_attempt_at"] - clock())
    assert delays == [2.0, 4.0, 8.0, 8.0, 8.0]

    s3.start()                                     # --- network is back ---
    clock.advance(20)
    step(svc)

    assert svc.store.counts() == {"uploaded": 3}
    objects = s3.objects()
    assert objects[seg_key("cam01", 2)] == data[2] and objects[seg_key("cam01", 3)] == data[3]
    assert not any(p.exists() for p in paths.values())       # removed only after verified upload


def test_restart_during_outage_resumes_pending_files(cfg, s3, new_service, clock):
    s3.stop()
    data = os.urandom(8000)
    path = write_segment(cfg, seg_rel("cam01", 1), data)
    svc1 = new_service()
    step(svc1)
    assert svc1.store.counts() == {"pending": 1}
    svc1.store.close()                             # service is stopped / machine reboots

    s3.start()
    svc2 = new_service()                           # fresh process, same state file
    svc2.recover()
    clock.advance(60)
    step(svc2)

    assert s3.objects() == {seg_key("cam01", 1): data}
    assert svc2.store.counts() == {"uploaded": 1} and not path.exists()


def test_crash_after_upload_before_state_saved_does_not_upload_twice(cfg, s3, new_service):
    data = os.urandom(8000)
    path = write_segment(cfg, seg_rel("cam01", 1), data)
    svc1 = new_service(CrashAfterPutStorage(cfg))
    svc1.scan_once()
    with pytest.raises(Crash):
        svc1.run_once()                            # process "killed" right after the upload

    assert svc1.store.get(seg_key("cam01", 1))["state"] == "uploading"   # state file says: unfinished
    assert s3.objects() == {seg_key("cam01", 1): data}                    # but the object is there
    assert path.exists()                                            # and the local copy is intact
    svc1.store.close()

    storage2 = CountingStorage(cfg)
    svc2 = new_service(storage2)                   # restart
    svc2.recover()
    assert svc2.store.get(seg_key("cam01", 1))["state"] == "pending"
    step(svc2)

    assert storage2.puts == 0                      # found the verified object, did not re-upload
    assert svc2.store.get(seg_key("cam01", 1))["state"] == "uploaded"
    assert not path.exists()


def test_run_forever_end_to_end_with_outage(cfg, s3):
    """The real loop in a thread, real clock: upload, outage, recovery, clean stop."""
    cfg.scan_interval = 0.05
    cfg.backoff_initial, cfg.backoff_max = 0.1, 0.4
    now = datetime.now(timezone.utc)      # recent date folders: the loop only scans recent days
    svc = Service(cfg)
    stop = threading.Event()
    worker = threading.Thread(target=svc.run_forever, args=(stop,))
    worker.start()

    def wait_for(predicate, timeout=20):
        end = time.time() + timeout
        while time.time() < end:
            if predicate():
                return
            time.sleep(0.05)
        raise AssertionError(f"timed out; counts={svc.store.counts()}")

    try:
        files = {}
        for n in (1, 2):
            files[n] = os.urandom(20_000)
            write_segment(cfg, seg_rel("cam01", n, now), files[n])
        wait_for(lambda: svc.store.counts().get("uploaded") == 2)

        s3.stop()
        for n in (3, 4):
            files[n] = os.urandom(20_000)
            write_segment(cfg, seg_rel("cam01", n, now), files[n])
        wait_for(lambda: svc.store.counts().get("pending") == 2
                 and svc.store.get(seg_key("cam01", 3, base=now))["last_error"])
        assert (cfg.watch_dir / seg_rel("cam01", 3, now)).exists()

        s3.start()
        wait_for(lambda: svc.store.counts() == {"uploaded": 4})
    finally:
        stop.set()
        worker.join(timeout=10)
    assert not worker.is_alive()
    assert s3.objects() == {seg_key("cam01", n, base=now): d for n, d in files.items()}
