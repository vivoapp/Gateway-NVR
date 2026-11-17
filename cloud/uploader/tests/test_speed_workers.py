"""Upload speed limit and the small worker pool."""
import os
import threading
import time

from conftest import seg_key, seg_rel, step, write_segment
from helpers import CountingStorage
from segment_uploader.storage import RateLimiter


def test_rate_limiter_spreads_bytes_over_time_across_all_callers():
    now = [0.0]
    slept = []

    def sleep(s):
        slept.append(s)
        now[0] += s

    lim = RateLimiter(0.008, clock=lambda: now[0], sleep=sleep)      # 1000 bytes per second
    for _ in range(3):
        lim.wait(1000)
    assert round(now[0], 6) == 2.0           # the first 1000 bytes go at once, the next two wait 1 s each


def test_zero_means_no_limit():
    lim = RateLimiter(0, sleep=lambda s: (_ for _ in ()).throw(AssertionError("slept")))
    lim.wait(10**9)


def timed_upload(cfg, size):
    from conftest import FakeClock
    from helpers import CountingStorage  # noqa: F401
    from segment_uploader.service import Service
    from segment_uploader.storage import S3Storage
    write_segment(cfg, seg_rel("cam01", 1), os.urandom(size))
    svc = Service(cfg, storage=S3Storage(cfg), clock=FakeClock(), jitter=lambda: 1.0)
    try:
        svc.scan_once()
        t0 = time.monotonic()
        svc.upload_due()
        return time.monotonic() - t0, svc.store.counts()
    finally:
        svc.store.close()


def test_upload_limit_is_applied_once_to_the_bytes_sent(cfg, s3):
    cfg.upload_limit_mbps = 6                # 0.75 MB per second
    took, counts = timed_upload(cfg, 1_500_000)
    assert counts == {"uploaded": 1}
    # ~2 s. About 4 s would mean the body is read (and slowed) twice, e.g. a signing pass.
    assert 1.5 <= took <= 3.2, took


def test_extra_signing_pass_over_the_body_does_not_slow_the_upload_twice(cfg, s3):
    """Some botocore versions read the body once more to sign it over plain http (seen in Docker)."""
    from segment_uploader.service import Service
    from segment_uploader.storage import S3Storage
    from conftest import FakeClock

    def signing_pass(request, **_):
        request.body.read()
        request.body.seek(0)

    cfg.upload_limit_mbps = 6
    write_segment(cfg, seg_rel("cam01", 1), os.urandom(1_500_000))
    storage = S3Storage(cfg)
    storage.client.meta.events.register("before-sign.s3.PutObject", signing_pass)
    svc = Service(cfg, storage=storage, clock=FakeClock(), jitter=lambda: 1.0)
    try:
        svc.scan_once()
        t0 = time.monotonic()
        svc.upload_due()
        took = time.monotonic() - t0
        assert svc.store.counts() == {"uploaded": 1}
    finally:
        svc.store.close()
    assert 1.5 <= took <= 3.2, took


def test_no_limit_is_fast(cfg, s3):
    took, counts = timed_upload(cfg, 1_500_000)
    assert counts == {"uploaded": 1} and took < 1.5


class SlowStorage(CountingStorage):
    def __init__(self, cfg, delay=0.2):
        super().__init__(cfg)
        self.delay, self.log, self.active, self.peak = delay, [], 0, 0
        self._lock = threading.Lock()

    def put(self, key, *a, **k):
        with self._lock:
            self.active += 1
            self.peak = max(self.peak, self.active)
            self.log.append(key)
        time.sleep(self.delay)
        try:
            return super().put(key, *a, **k)
        finally:
            with self._lock:
                self.active -= 1


def test_cameras_upload_in_parallel_but_each_camera_in_order(cfg, s3, new_service):
    cfg.workers = 4
    cams = ["cam01", "cam02", "cam03", "cam04"]
    for c in cams:
        for n in (1, 2, 3):
            write_segment(cfg, seg_rel(c, n), os.urandom(1000))
    storage = SlowStorage(cfg)
    svc = new_service(storage)
    svc.scan_once()
    t0 = time.monotonic()
    stats = svc.upload_due()
    took = time.monotonic() - t0

    assert stats["uploaded"] == 12 and svc.store.counts() == {"uploaded": 12}
    assert 2 <= storage.peak <= 4                          # parallel, and never more than `workers`
    assert took < 12 * 0.2 * 0.7                           # clearly faster than one at a time
    for c in cams:                                         # segments of one camera: oldest first
        mine = [k for k in storage.log if f"/{c}/" in k]
        assert mine == [seg_key(c, n) for n in (1, 2, 3)]


def test_one_worker_uploads_strictly_oldest_first(cfg, s3, new_service):
    cfg.workers = 1
    for c in ("cam01", "cam02"):
        for n in (1, 2):
            write_segment(cfg, seg_rel(c, n), os.urandom(1000))
    storage = SlowStorage(cfg, delay=0)
    svc = new_service(storage)
    step(svc)
    starts = [k.rsplit("_", 1)[1] for k in storage.log]
    assert starts == sorted(starts) and storage.peak == 1


def test_an_outage_stops_all_workers_quickly(cfg, s3, new_service):
    cfg.workers = 2                                        # fewer workers than cameras: some wait their turn
    for c in ("cam01", "cam02", "cam03", "cam04"):
        for n in (1, 2, 3):
            write_segment(cfg, seg_rel(c, n), os.urandom(1000))
    svc = new_service()
    s3.stop()
    svc.scan_once()
    svc.upload_due()
    attempts = [svc.store.get(seg_key(c, n))["attempts"] for c in
                ("cam01", "cam02", "cam03", "cam04") for n in (1, 2, 3)]
    assert max(attempts) <= 1 and sum(attempts) <= 2       # one try per worker; waiting cameras never start
    assert svc._pause_until > svc.clock()
