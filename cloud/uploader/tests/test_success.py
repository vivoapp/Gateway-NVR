"""Normal operation: finished segments are uploaded, verified, and left alone on disk."""
import os
from datetime import timedelta

import pytest

from conftest import BASE, seg_key, seg_rel, sha, step, write_segment


def test_segments_are_uploaded_with_matching_content(cfg, s3, new_service):
    a = os.urandom(300_000)
    b = os.urandom(150_000)
    write_segment(cfg, seg_rel("cam01", 1), a)
    write_segment(cfg, seg_rel("cam02", 1), b)
    svc = new_service()

    step(svc)

    objects = s3.objects()
    assert objects == {seg_key("cam01", 1): a, seg_key("cam02", 1): b}
    assert svc.store.counts() == {"uploaded": 2}
    row = svc.store.get(seg_key("cam01", 1))
    assert row["sha256"] == sha(a) and row["attempts"] == 1


def test_a_finished_segment_is_picked_up_in_the_first_scan(cfg, s3, new_service):
    """The recorder renames .mp4.part to .mp4 when done, so no waiting for the size to settle."""
    write_segment(cfg, seg_rel("cam01", 1), os.urandom(1000))
    svc = new_service()
    assert svc.scan_once() == 1
    assert svc.store.counts() == {"pending": 1}


def test_local_files_are_never_deleted(cfg, s3, new_service):
    path = write_segment(cfg, seg_rel("cam01", 1), os.urandom(1000))
    svc = new_service()

    svc.scan_once()
    assert path.exists() and svc.store.counts() == {"pending": 1}   # registered, not uploaded yet

    step(svc, 3)
    assert path.exists()                                            # the recorder (M9) owns cleanup
    assert svc.store.counts() == {"uploaded": 1}
    assert len(s3.objects()) == 1


def test_the_old_delete_after_upload_setting_is_rejected(tmp_path):
    from segment_uploader.config import load_config
    f = tmp_path / "c.toml"
    f.write_text('[service]\nwatch_dir = "x"\ndelete_after_upload = true\n[s3]\nbucket = "b"\n')
    with pytest.raises(ValueError, match="delete_after_upload"):
        load_config(f)


def test_part_files_and_other_files_are_never_uploaded(cfg, s3, new_service):
    part = write_segment(cfg, seg_rel("cam01", 2) + ".part", os.urandom(1000))   # still being written
    write_segment(cfg, "cam01/notes.txt", b"hello")
    write_segment(cfg, seg_rel("cam01", 3).replace(".mp4", ".ts"), b"x" * 100)
    svc = new_service()

    step(svc, 3)
    assert svc.store.counts() == {} and s3.objects() == {}

    final = part.with_name(part.name[: -len(".part")])
    part.rename(final)                                  # the recorder finishes the segment
    step(svc)
    assert list(s3.objects()) == [seg_key("cam01", 2)]


def test_mp4_with_a_name_outside_the_agreed_format_is_ignored_and_reported_once(cfg, s3, new_service, caplog):
    write_segment(cfg, "cam01/2026-10-20/seg_0001.mp4", os.urandom(500))
    write_segment(cfg, seg_rel("cam01", 4), os.urandom(500))
    svc = new_service()
    with caplog.at_level("WARNING", logger="segment_uploader"):
        step(svc, 3)
        svc.scan_once(full=True)
    assert list(s3.objects()) == [seg_key("cam01", 4)]
    warnings = [r for r in caplog.records if "ignored_name" in r.getMessage()]
    assert len(warnings) == 1 and "seg_0001.mp4" in warnings[0].getMessage()


def test_camera_and_utc_times_come_from_the_file_name(cfg, s3, new_service):
    write_segment(cfg, seg_rel("cam07", 5), os.urandom(500))
    svc = new_service()
    svc.scan_once()
    row = svc.store.get(seg_key("cam07", 5))
    start = int((BASE + timedelta(minutes=5)).timestamp())
    assert row["camera_id"] == "cam07"
    assert row["start_utc"] == start
    assert row["end_utc"] == start + 60               # nominal length until the name carries an end time


def test_end_time_in_the_file_name_is_used_when_present(cfg, s3, new_service):
    start = BASE + timedelta(minutes=1)
    end = start + timedelta(seconds=58)               # a short segment
    rel = f"cam01/{start:%Y-%m-%d}/cam01_{start:%Y%m%dT%H%M%S}Z_{end:%Y%m%dT%H%M%S}Z.mp4"
    write_segment(cfg, rel, os.urandom(500))
    svc = new_service()
    svc.scan_once()
    row = svc.store.get(f"site-1/{rel}")
    assert row["end_utc"] - row["start_utc"] == 58


def test_upload_order_follows_segment_start_time_not_file_time_or_listing_order(cfg, s3, monkeypatch):
    """Oldest segment first, across cameras, whatever the file system or file times say."""
    import itertools
    import segment_uploader.service as service_module
    from segment_uploader.service import Service
    from segment_uploader.storage import S3Storage

    real_iter = service_module.iter_local_files
    monkeypatch.setattr(service_module, "iter_local_files",
                        lambda c: iter(sorted(real_iter(c), reverse=True)))   # listing: newest name first

    class RecordingStorage(S3Storage):
        def __init__(self, c):
            super().__init__(c)
            self.order = []

        def put(self, key, *args, **kwargs):
            self.order.append(key)
            return super().put(key, *args, **kwargs)

    wanted = [seg_key("cam02", 1), seg_key("cam01", 2), seg_key("cam02", 3), seg_key("cam01", 4)]
    paths = {k: write_segment(cfg, k.split("/", 1)[1], os.urandom(2000)) for k in wanted}
    for i, key in enumerate(reversed(wanted)):         # file times in the opposite order
        os.utime(paths[key], ns=((1 + i) * 10**12, (1 + i) * 10**12))
    ticks = itertools.count(1_000_000)
    storage = RecordingStorage(cfg)
    svc = Service(cfg, storage=storage, clock=lambda: float(next(ticks)), jitter=lambda: 1.0)
    try:
        step(svc)
    finally:
        svc.store.close()
    assert storage.order == wanted
