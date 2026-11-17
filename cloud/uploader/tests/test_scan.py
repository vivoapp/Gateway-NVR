"""Scanning: file name parsing, recent-folder scans, and the full scan at start-up and hourly."""
import os
from datetime import datetime, timedelta, timezone

import pytest

from conftest import BASE, seg_key, seg_rel, write_segment
from segment_uploader.segment import parse_segment_name


@pytest.mark.parametrize("name, camera, start, end_from_name", [
    ("cam01_20261020T100000Z.mp4", "cam01", "2026-10-20T10:00:00Z", False),
    ("VIVO-CAM-10_20261231T235959Z.mp4", "VIVO-CAM-10", "2026-12-31T23:59:59Z", False),
    ("cam_01_20261020T100000Z.mp4", "cam_01", "2026-10-20T10:00:00Z", False),
    ("cam01_20261020T100000Z_20261020T100100Z.mp4", "cam01", "2026-10-20T10:00:00Z", True),
])
def test_valid_names(name, camera, start, end_from_name):
    info = parse_segment_name(name)
    assert (info.camera_id, info.start_iso, info.end_from_name) == (camera, start, end_from_name)


def test_end_time_defaults_to_start_plus_nominal_length():
    info = parse_segment_name("cam01_20261020T100000Z.mp4", nominal_seconds=60)
    assert info.end_iso == "2026-10-20T10:01:00Z"
    assert parse_segment_name("cam01_20261020T100000Z.mp4", nominal_seconds=30).end_iso == "2026-10-20T10:00:30Z"


@pytest.mark.parametrize("name", [
    "cam01_20261020T100000Z.mp4.part",       # still being written
    "cam01_20261020T100000.mp4",             # no Z: time zone not stated
    "cam01_20261320T100000Z.mp4",            # month 13
    "cam01_20261020T250000Z.mp4",            # hour 25
    "20261020T100000Z.mp4",                  # no camera
    "cam01_20261020T100000Z.ts",
    "cam01_20261020T100100Z_20261020T100000Z.mp4",   # end before start
    "seg_0001.mp4",
])
def test_invalid_names(name):
    assert parse_segment_name(name) is None


def make_clock_at(dt: datetime):
    t = [dt.timestamp()]

    def clock():
        return t[0]

    clock.advance = lambda s: t.__setitem__(0, t[0] + s)
    return clock


def test_first_scan_is_full_and_registers_the_whole_backlog(cfg, s3, new_service, clock):
    old = BASE - timedelta(days=30)                    # far outside the recent window
    for n in range(3):
        write_segment(cfg, seg_rel("cam01", n, old), os.urandom(200))
    svc = new_service()
    assert svc.scan_once() == 3
    assert svc.store.counts() == {"pending": 3}


def test_normal_scans_only_look_at_recent_date_folders(cfg, s3, new_service):
    clock = make_clock_at(BASE + timedelta(hours=1))   # "now" is 2026-10-20 11:00 UTC
    svc = new_service()
    svc.clock = clock
    svc.scan_once()                                    # first scan (full), nothing there yet

    recent = write_segment(cfg, seg_rel("cam01", 0), os.urandom(200))               # today
    yesterday = write_segment(cfg, seg_rel("cam01", 0, BASE - timedelta(days=1)), os.urandom(200))
    old = write_segment(cfg, seg_rel("cam01", 0, BASE - timedelta(days=10)), os.urandom(200))

    assert svc.scan_once() == 2                        # today and yesterday, not 10 days ago
    keys = {r["key"] for r in svc.store.unfinished()}
    assert seg_key("cam01", 0) in keys and seg_key("cam01", 0, base=BASE - timedelta(days=1)) in keys
    assert seg_key("cam01", 0, base=BASE - timedelta(days=10)) not in keys
    assert recent.exists() and yesterday.exists() and old.exists()


def test_full_scan_runs_again_after_the_full_scan_interval(cfg, s3, new_service):
    clock = make_clock_at(BASE + timedelta(hours=1))
    svc = new_service()
    svc.clock = clock
    svc.scan_once()
    write_segment(cfg, seg_rel("cam01", 0, BASE - timedelta(days=10)), os.urandom(200))

    assert svc.scan_once() == 0                        # a normal scan does not look there
    clock.advance(cfg.full_scan_interval - 1)
    assert svc.scan_once() == 0
    clock.advance(2)                                   # the hour is over: full scan
    assert svc.scan_once() == 1


def test_known_files_are_not_checked_again_by_normal_scans(cfg, s3, new_service, monkeypatch):
    path = write_segment(cfg, seg_rel("cam01", 0), os.urandom(200))
    svc = new_service()
    svc.scan_once()
    stats = []
    real_stat = type(path).stat
    monkeypatch.setattr(type(path), "stat", lambda self, *a, **k: (stats.append(self), real_stat(self, *a, **k))[1])
    for _ in range(5):
        svc.scan_once()
    assert stats == []                                 # no stat call per file per scan


def test_known_keys_are_loaded_from_the_state_file_after_a_restart(cfg, s3, new_service):
    write_segment(cfg, seg_rel("cam01", 0), os.urandom(200))
    svc = new_service()
    svc.scan_once()
    svc.store.close()
    svc2 = new_service()
    svc2.recover()
    assert svc2.scan_once() == 0                       # full scan, but the file is already known
    assert svc2.store.counts() == {"pending": 1}


def test_a_file_removed_between_listing_and_stat_is_skipped(cfg, s3, new_service, monkeypatch):
    path = write_segment(cfg, seg_rel("cam01", 0), os.urandom(200))
    svc = new_service()
    real_iter = svc._iter_files
    monkeypatch.setattr(svc, "_iter_files", lambda: (p for p in [*real_iter(), path.parent / "gone.mp4"]))
    assert svc.scan_once() == 1                        # the listed-but-missing path causes no error


def test_old_state_file_without_the_new_columns_is_upgraded(tmp_path):
    import sqlite3
    from segment_uploader.store import Store
    db = tmp_path / "old.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE segments (key TEXT PRIMARY KEY, rel_path TEXT NOT NULL, size INTEGER NOT NULL,"
                " mtime_ns INTEGER NOT NULL, sha256 TEXT, md5 TEXT, state TEXT NOT NULL,"
                " attempts INTEGER NOT NULL DEFAULT 0, next_attempt_at REAL NOT NULL DEFAULT 0,"
                " last_error TEXT, etag TEXT, first_seen REAL NOT NULL, uploaded_at REAL,"
                " local_deleted INTEGER NOT NULL DEFAULT 0)")
    con.execute("INSERT INTO segments (key, rel_path, size, mtime_ns, state, first_seen)"
                " VALUES ('k', 'r', 1, 1, 'uploaded', 1)")
    con.commit()
    con.close()
    store = Store(db)
    assert store.get("k")["camera_id"] is None
    assert store.register("k2", "r2", 1, 1, 1.0, "cam01", 100, 160) == "new"
    store.close()


def _count_listings(monkeypatch, folder):
    """Count how often a given folder is listed with os.scandir."""
    calls = []
    real = os.scandir

    def counting(path="."):
        if os.fspath(path) == os.fspath(folder):
            calls.append(path)
        return real(path)

    monkeypatch.setattr(os, "scandir", counting)
    return calls


def test_an_unchanged_date_folder_is_not_listed_again_but_a_changed_one_is(cfg, s3, new_service, monkeypatch):
    first = write_segment(cfg, seg_rel("cam01", 0), os.urandom(200))
    folder = first.parent
    old = (BASE - timedelta(hours=5)).timestamp() * 1e9
    os.utime(folder, ns=(int(old), int(old)))          # the folder has been quiet for hours
    svc = new_service()
    svc.clock = make_clock_at(BASE + timedelta(hours=1))
    svc.scan_once()                                    # first scan: full
    listings = _count_listings(monkeypatch, folder)

    svc.scan_once()                                    # normal scan: lists it once, notes its time
    assert len(listings) == 1
    svc.scan_once()
    svc.scan_once()
    assert len(listings) == 1                          # unchanged folder: only a stat per scan

    write_segment(cfg, seg_rel("cam01", 1), os.urandom(200))     # recorder adds a segment
    assert svc.scan_once() == 1                        # folder time changed: listed, file found
    assert len(listings) == 2


def test_a_folder_changed_in_the_last_two_seconds_is_listed_again_next_scan(cfg, s3, new_service, monkeypatch):
    first = write_segment(cfg, seg_rel("cam01", 0), os.urandom(200))     # folder time = now
    svc = new_service()
    svc.clock = make_clock_at(BASE + timedelta(hours=1))
    svc.scan_once()
    listings = _count_listings(monkeypatch, first.parent)
    svc.scan_once()
    svc.scan_once()
    svc.scan_once()
    assert len(listings) == 3                          # too fresh to trust: listed every time


def test_a_scan_interrupted_half_way_leaves_nothing_half_registered(cfg, s3, new_service, monkeypatch):
    for n in range(5):
        write_segment(cfg, seg_rel("cam01", n), os.urandom(200))
    svc = new_service()
    real = svc.store.register
    calls = []

    def flaky(*a, **k):
        calls.append(1)
        if len(calls) == 3:
            raise RuntimeError("process killed")
        return real(*a, **k)

    monkeypatch.setattr(svc.store, "register", flaky)
    with pytest.raises(RuntimeError):
        svc.scan_once()
    assert svc.store.counts() == {}                    # rolled back as one unit

    monkeypatch.setattr(svc.store, "register", real)
    assert svc.scan_once() == 5                        # the next scan finds everything again
    assert svc.store.counts() == {"pending": 5}


def test_fake_recorder_can_name_files_with_start_and_end_time(tmp_path):
    import subprocess
    import sys
    from pathlib import Path
    from segment_uploader.segment import parse_segment_name
    tool = Path(__file__).resolve().parent.parent / "tools" / "fake_recorder.py"
    subprocess.run([sys.executable, str(tool), "--dir", str(tmp_path), "--cameras", "1", "--count", "1",
                    "--size-mb", "0.001", "--end-in-name", "--name-format", "VIVO-CAM-{:02d}"], check=True,
                   capture_output=True)
    files = list(tmp_path.glob("*/*/*.mp4"))
    assert len(files) == 1 and not list(tmp_path.glob("*/*/*.part"))
    info = parse_segment_name(files[0].name)
    assert info.end_from_name and info.end_utc - info.start_utc == 60
