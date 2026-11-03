"""Unit tests for tools/storage_guard.py on a fake recording disk in a temporary folder.

usage: python3 -m unittest discover -s tests/unit -v
"""
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
import storage_guard as sg  # noqa: E402
import substream_archiver as sa  # noqa: E402

POLICY = {"substream_days": 3, "export_days": 7, "disk_limit_percent": 90, "disk_target_percent": 85,
          "record_days": 7, "segment_seconds": 60, "cameras": ["cam01", "cam02"]}
HOUR = 3600
NOW = time.time() // HOUR * HOUR + 1800          # half past an hour
MB = 1_000_000


class FakeDisk:
    """A recording disk of `total` bytes whose use is the size of the files under `root`."""

    def __init__(self, root: Path, total: int):
        self.root, self.total = root, total

    def usage(self, _path):
        used = sg.size_of(sg.tree_files(self.root))
        return used, self.total - used, 100.0 * used / self.total


class GuardTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.layout = sg.Layout(self.root)
        self.layout.frigate_media.mkdir(parents=True)
        self.api_calls = []

    def tearDown(self):
        self.tmp.cleanup()

    def main_hour(self, start: float, size: int = MB) -> None:
        for cam in POLICY["cameras"]:
            d = sg.main_hour_dir(self.layout, int(start)) / cam
            d.mkdir(parents=True, exist_ok=True)
            (d / "00.00.mp4").write_bytes(b"m" * size)

    def sub_segment(self, cam: str, start: float, size: int = MB // 10) -> Path:
        d = sa.day_dir(self.layout.substream, cam, start)
        d.mkdir(parents=True, exist_ok=True)
        f = d / sa.segment_name(cam, int(start), int(start) + 60)
        f.write_bytes(b"s" * size)
        return f

    def fake_api(self, method, path, body=None, timeout=0):
        """Frigate: DELETE recordings removes the files of that hour; exports list and delete."""
        self.api_calls.append((method, path, body))
        if method == "DELETE" and path.startswith("/recordings/start/"):
            start = int(path.split("/")[3])
            for f in sg.tree_files(sg.main_hour_dir(self.layout, start)):
                f.unlink()
            return {"success": True}
        if method == "GET" and path == "/exports":
            return [{"id": "old", "date": NOW - 8 * 86400, "in_progress": False},
                    {"id": "busy", "date": NOW - 9 * 86400, "in_progress": True},
                    {"id": "new", "date": NOW - 86400, "in_progress": False}]
        return {"success": True}

    def guard(self, dry_run=False):
        return sg.Guard(self.layout, POLICY, dry_run=dry_run)

    def test_substream_retention_by_start_time(self):
        just_old = self.sub_segment("cam01", NOW - 3 * 86400 - 120)
        kept = self.sub_segment("cam01", NOW - 3 * 86400 + 120)
        old = self.sub_segment("cam01", NOW - 5 * 86400)
        newest = self.sub_segment("cam02", NOW - 60)
        stale = sa.day_dir(self.layout.substream, "cam01", NOW) / "cam01_20261029T000000Z.mp4.part"
        stale.parent.mkdir(parents=True, exist_ok=True)
        stale.write_bytes(b"x")
        os.utime(stale, (NOW - 2 * HOUR, NOW - 2 * HOUR))
        fresh = sa.day_dir(self.layout.substream, "cam02", NOW) / "cam02_20261029T000000Z.mp4.part"
        fresh.write_bytes(b"x")
        self.assertEqual(self.guard().substream_retention(NOW), 2)
        self.assertFalse(old.exists() or just_old.exists())
        self.assertFalse(old.parent.exists())          # its empty date folder is gone too
        self.assertTrue(kept.exists() and newest.exists() and fresh.exists())
        self.assertFalse(stale.exists())

    def test_dry_run_deletes_nothing(self):
        old = self.sub_segment("cam01", NOW - 5 * 86400)
        self.guard(dry_run=True).substream_retention(NOW)
        self.assertTrue(old.exists())

    def test_oldest_footage_goes_first_and_the_last_hour_stays(self):
        hours = [NOW - h * HOUR for h in (6, 5, 4, 3, 2, 1, 0)]
        for h in hours:
            self.main_hour(h // HOUR * HOUR, size=5 * MB)
        sub_old = self.sub_segment("cam01", hours[1])        # same hour as the second main hour
        disk = FakeDisk(self.root, total=80 * MB)            # 7 h x 2 cams x 5 MB = 70 MB: 87.6% used
        with mock.patch.object(sg, "disk_usage", disk.usage), mock.patch.object(sg, "frigate_api", self.fake_api):
            deleted = self.guard().relieve_pressure(NOW, limit=85, target=60)
        self.assertGreaterEqual(deleted, 3)
        remaining = sg.main_hours(self.layout)
        self.assertEqual(remaining[0], (hours[0] // HOUR * HOUR) + deleted * HOUR)   # oldest first, in order
        self.assertIn(hours[-1] // HOUR * HOUR, remaining)   # the current hour is never touched
        self.assertFalse(sub_old.exists())
        deletes = [p for m, p, _ in self.api_calls if m == "DELETE"]
        self.assertEqual(deletes[0], f"/recordings/start/{int(hours[0] // HOUR * HOUR)}/end/{int(hours[0] // HOUR * HOUR) + HOUR - 1}?cameras=all")

    def test_substream_only_hours_are_deleted_oldest_first(self):
        # Substream segments in hours with no main-stream footage (e.g. Frigate was down).
        old = [self.sub_segment("cam01", (NOW - h * HOUR) // HOUR * HOUR, size=10 * MB) for h in (5, 4)]
        self.main_hour((NOW - 3 * HOUR) // HOUR * HOUR, size=5 * MB)
        newest = self.sub_segment("cam02", NOW - 60, size=10 * MB)
        disk = FakeDisk(self.root, total=50 * MB)                          # 40 MB used: 80 %
        with mock.patch.object(sg, "disk_usage", disk.usage), mock.patch.object(sg, "frigate_api", self.fake_api):
            self.assertEqual(self.guard().relieve_pressure(NOW, limit=75, target=50), 2)   # 40 -> 30 -> 20 MB
        self.assertFalse(old[0].exists() or old[1].exists())
        self.assertEqual(len(sg.main_hours(self.layout)), 1)              # the newer main hour stays
        self.assertTrue(newest.exists())

    def test_early_substream_deletions_are_listed(self):
        start = (NOW - 5 * HOUR) // HOUR * HOUR
        self.main_hour(start, size=5 * MB)
        for cam in POLICY["cameras"]:
            for minute in (0, 1, 2):
                self.sub_segment(cam, start + minute * 60)
        self.main_hour(NOW // HOUR * HOUR, size=5 * MB)
        disk = FakeDisk(self.root, total=25 * MB)                          # 20.6 MB used: 82 %
        with mock.patch.object(sg, "disk_usage", disk.usage), mock.patch.object(sg, "frigate_api", self.fake_api):
            self.assertEqual(self.guard().relieve_pressure(NOW, limit=80, target=50), 1)
        recs = sg.read_early_deletions(self.layout.early_deletions)
        self.assertEqual([r["camera"] for r in recs], ["cam01", "cam02"])  # one line per camera and hour
        self.assertEqual(recs[0], {"camera": "cam01", "start": sg.iso(start), "end": sg.iso(start + 180),
                                   "deleted_at": sg.iso(NOW), "segments": 3})

    def test_deletions_after_the_retention_are_not_early(self):
        old = (NOW - 4 * 86400) // HOUR * HOUR                             # past the 3 days already
        self.sub_segment("cam01", old, size=10 * MB)
        self.sub_segment("cam02", NOW - 60, size=10 * MB)
        disk = FakeDisk(self.root, total=25 * MB)
        with mock.patch.object(sg, "disk_usage", disk.usage), mock.patch.object(sg, "frigate_api", self.fake_api):
            self.assertEqual(self.guard().relieve_pressure(NOW, limit=75, target=50), 1)
        self.assertFalse(self.layout.early_deletions.exists())

    def test_cleanup_carries_on_when_the_list_cannot_be_written(self):
        start = (NOW - 5 * HOUR) // HOUR * HOUR
        seg = self.sub_segment("cam01", start, size=10 * MB)
        self.sub_segment("cam02", NOW - 60, size=10 * MB)
        self.layout.early_deletions.mkdir()                                # a folder: opening it fails
        disk = FakeDisk(self.root, total=25 * MB)
        with mock.patch.object(sg, "disk_usage", disk.usage), mock.patch.object(sg, "frigate_api", self.fake_api):
            self.assertEqual(self.guard().relieve_pressure(NOW, limit=75, target=50), 1)
        self.assertFalse(seg.exists())

    def test_dry_run_lists_nothing(self):
        start = (NOW - 5 * HOUR) // HOUR * HOUR
        self.sub_segment("cam01", start, size=10 * MB)
        self.sub_segment("cam02", NOW - 60, size=10 * MB)
        disk = FakeDisk(self.root, total=25 * MB)
        with mock.patch.object(sg, "disk_usage", disk.usage), mock.patch.object(sg, "frigate_api", self.fake_api):
            self.guard(dry_run=True).relieve_pressure(NOW, limit=75, target=50)
        self.assertFalse(self.layout.early_deletions.exists())

    def test_reading_the_list_skips_bad_lines_and_old_ones(self):
        self.layout.substream.mkdir(parents=True)
        good = {"camera": "cam01", "start": sg.iso(NOW - 2 * HOUR), "end": sg.iso(NOW - HOUR),
                "deleted_at": sg.iso(NOW), "segments": 60}
        old = dict(good, deleted_at=sg.iso(NOW - 2 * 86400))
        self.layout.early_deletions.write_text("\n".join([
            json.dumps(old), "not json", json.dumps({"camera": "cam01"}), "[1, 2]",
            json.dumps(dict(good, start="yesterday")), json.dumps(good)]) + "\n")
        self.assertEqual(sg.read_early_deletions(self.layout.early_deletions), [old, good])
        self.assertEqual(sg.read_early_deletions(self.layout.early_deletions, since=NOW - 86400), [good])
        self.assertEqual(sg.read_early_deletions(self.root / "missing.jsonl"), [])

    def test_folders_that_vanish_are_not_an_error(self):
        missing = self.root / "gone"
        self.assertEqual(sg.entries(missing), [])
        self.assertEqual(sg.subdirs(missing), [])
        self.assertIsNone(sg.mtime(missing / "x.mp4.part"))

    def test_lab_disk_size(self):
        self.main_hour(NOW - 5 * HOUR, size=10 * MB)                       # 2 cameras: 20 MB
        with mock.patch.object(sg, "LAB_DISK_BYTES", 40 * MB):
            used, avail, pct = sg.recording_disk_usage(self.root)
        self.assertEqual((used, avail, pct), (20 * MB, 20 * MB, 50.0))

    def test_stops_before_the_newest_hour(self):
        self.main_hour(NOW // HOUR * HOUR, size=40 * MB)     # only the current hour, and the disk is full
        disk = FakeDisk(self.root, total=81 * MB)
        with mock.patch.object(sg, "disk_usage", disk.usage), mock.patch.object(sg, "frigate_api", self.fake_api):
            self.assertEqual(self.guard().relieve_pressure(NOW, limit=90, target=85), 0)
        self.assertEqual(len(sg.main_hours(self.layout)), 1)

    def test_below_the_limit_nothing_happens(self):
        self.main_hour(NOW - 5 * HOUR)
        disk = FakeDisk(self.root, total=100 * MB)
        with mock.patch.object(sg, "disk_usage", disk.usage), mock.patch.object(sg, "frigate_api", self.fake_api):
            self.assertEqual(self.guard().relieve_pressure(NOW, limit=90, target=85), 0)
        self.assertEqual(self.api_calls, [])

    def test_without_frigate_the_hour_is_deleted_directly(self):
        start = (NOW - 5 * HOUR) // HOUR * HOUR
        self.main_hour(start)

        def down(*_a, **_k):
            raise OSError("connection refused")
        with mock.patch.object(sg, "frigate_api", down):
            self.guard().delete_main_hour(int(start))
        self.assertEqual(sg.main_hours(self.layout), [])

    def test_old_exports_are_deleted_through_the_api(self):
        with mock.patch.object(sg, "frigate_api", self.fake_api):
            self.assertEqual(self.guard().export_cleanup(NOW), 1)
        self.assertIn(("POST", "/exports/delete", {"ids": ["old"]}), self.api_calls)

    def test_export_time_formats(self):
        self.assertEqual(sg.export_time({"date": 1761700000.5}), 1761700000.5)
        self.assertEqual(sg.export_time({"date": "2026-10-29T08:00:00"}), 1793260800)


if __name__ == "__main__":
    unittest.main()
