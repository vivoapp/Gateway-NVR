"""Unit tests for tools/substream_archiver.py (no cameras or Docker needed).

usage: python3 -m unittest discover -s tests/unit -v
"""
import struct
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
import substream_archiver as sa  # noqa: E402


class Names(unittest.TestCase):
    def test_stamp_round_trip(self):
        t = sa.parse_stamp("20261023T051710")
        self.assertEqual(sa.stamp(t), "20261023T051710")
        self.assertEqual(time.gmtime(t).tm_hour, 5)

    def test_segment_name_is_the_contract(self):
        start = sa.parse_stamp("20261023T051710")
        name = sa.segment_name("cam01", start, start + 60)
        self.assertEqual(name, "cam01_20261023T051710Z_20261023T051810Z.mp4")
        m = sa.SEGMENT_RE.match(name)
        self.assertEqual((m["cam"], m["start"], m["end"]), ("cam01", "20261023T051710", "20261023T051810"))

    def test_part_name(self):
        m = sa.PART_RE.match("cam_10_20261023T235959Z.mp4.part")
        self.assertEqual((m["cam"], m["start"]), ("cam_10", "20261023T235959"))
        self.assertIsNone(sa.PART_RE.match("cam01_20261023T051710Z_20261023T051810Z.mp4"))

    def test_day_folder_is_the_start_date(self):
        start = sa.parse_stamp("20261023T235930")  # crosses midnight
        self.assertEqual(sa.day_dir(Path("/r"), "cam01", start), Path("/r/cam01/2026-10-23"))


class SegmentTimes(unittest.TestCase):
    start = sa.parse_stamp("20261023T051710")

    def test_ends_where_the_next_segment_starts(self):
        s = self.start
        self.assertEqual(sa.segment_times(s, 59.974, s + 60), (s, s + 60))
        self.assertEqual(sa.segment_times(s, 59.2, s + 60), (s, s + 60))      # under 1 s is rounding

    def test_first_segment_starts_at_its_first_keyframe(self):
        # Opened on connect at :10, video from the keyframe 4 s later: start is end - duration.
        s = self.start
        self.assertEqual(sa.segment_times(s, 55.0, s + 59), (s + 4, s + 59))
        self.assertEqual(sa.segment_times(s, 35.0, s + 37), (s + 2, s + 37))   # cam10 in the lab, Oct 29

    def test_last_segment_ends_at_start_plus_duration(self):
        s = self.start
        self.assertEqual(sa.segment_times(s, 3.274, None), (s, s + 3))
        self.assertEqual(sa.segment_times(s, 3.6, None), (s, s + 4))

    def test_never_ends_in_the_future(self):
        # Timestamps that claim four hours are capped at the time the segment finished.
        s = self.start
        self.assertEqual(sa.segment_times(s, 14641.8, None, now=s + 57), (s, s + 57))
        self.assertEqual(sa.segment_times(s, 5.0, None, now=s - 10), (s, s))


class Mp4Duration(unittest.TestCase):
    @staticmethod
    def box(kind: bytes, body: bytes) -> bytes:
        return struct.pack(">I4s", 8 + len(body), kind) + body

    def write(self, data: bytes) -> Path:
        f = Path(self.tmp.name) / "x.mp4"
        f.write_bytes(data)
        return f

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def test_version_0_and_1(self):
        mvhd0 = self.box(b"mvhd", b"\0\0\0\0" + struct.pack(">IIII", 0, 0, 1000, 59974) + b"\0" * 80)
        mvhd1 = self.box(b"mvhd", b"\1\0\0\0" + struct.pack(">QQIQ", 0, 0, 90000, 5400540) + b"\0" * 80)
        ftyp = self.box(b"ftyp", b"isom\0\0\2\0")
        self.assertAlmostEqual(sa.mp4_duration(self.write(ftyp + self.box(b"moov", mvhd0))), 59.974)
        self.assertAlmostEqual(sa.mp4_duration(self.write(ftyp + self.box(b"moov", mvhd1))), 60.006)

    def test_no_header(self):
        self.assertIsNone(sa.mp4_duration(self.write(self.box(b"ftyp", b"isom") + self.box(b"mdat", b"x" * 100))))
        self.assertIsNone(sa.mp4_duration(Path(self.tmp.name) / "missing.mp4"))


class SaveAndRename(unittest.TestCase):
    """A segment gets its final name only when it is on the disk and whole (M11 power cuts)."""

    def test_whole_file_is_renamed(self):
        with tempfile.TemporaryDirectory() as d:
            part, final = Path(d) / "cam01_x.mp4.part", Path(d) / "cam01_x_y.mp4"
            part.write_bytes(Mp4Duration.box(b"ftyp", b"isom") + Mp4Duration.box(b"moov", b"\0" * 20)
                             + Mp4Duration.box(b"mdat", b"x" * 100))
            self.assertIsNone(sa.save_and_rename(part, final))
            self.assertTrue(final.exists() and not part.exists())

    def test_broken_file_keeps_its_part_name(self):
        with tempfile.TemporaryDirectory() as d:
            part, final = Path(d) / "cam01_x.mp4.part", Path(d) / "cam01_x_y.mp4"
            part.write_bytes(Mp4Duration.box(b"ftyp", b"isom") + struct.pack(">I4s", 0, b"free")
                             + Mp4Duration.box(b"mdat", b"x" * 100))     # the index never arrived
            self.assertIn("no moov", sa.save_and_rename(part, final) or "")
            self.assertTrue(part.exists() and not final.exists())


class SegmentList(unittest.TestCase):
    def test_csv_line(self):
        name, duration = sa.parse_list_line("cam01_20261023T051710Z.mp4.part,12.000000,71.974000\n")
        self.assertEqual(name, "cam01_20261023T051710Z.mp4.part")
        self.assertAlmostEqual(duration, 59.974, places=6)

    def test_bad_lines(self):
        self.assertIsNone(sa.parse_list_line(""))
        self.assertIsNone(sa.parse_list_line("a,b,c"))


class Folders(unittest.TestCase):
    def test_part_files_and_day_dirs(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            sa.ensure_day_dirs(root, "cam01")
            today = sa.day_dir(root, "cam01", time.time())
            tomorrow = sa.day_dir(root, "cam01", time.time() + 86400)
            self.assertTrue(today.is_dir() and tomorrow.is_dir())
            (today / "cam01_20261023T051710Z.mp4.part").write_bytes(b"x")
            (today / "cam01_20261023T051610Z.mp4.part").write_bytes(b"x")
            (today / "cam02_20261023T051710Z.mp4.part").write_bytes(b"x")       # other camera
            (today / "cam01_20261023T051510Z_20261023T051610Z.mp4").write_bytes(b"x")  # finished
            names = [p.name for p in sa.part_files(root, "cam01")]
            self.assertEqual(names, ["cam01_20261023T051610Z.mp4.part", "cam01_20261023T051710Z.mp4.part"])


class Command(unittest.TestCase):
    def test_ffmpeg_command(self):
        cmd = sa.ffmpeg_cmd(Path("/recordings/substream"), "cam01", "rtsp://u:p@go2rtc:8554/cam01_sub", "hevc")
        self.assertIn("-c", cmd)
        self.assertEqual(cmd[cmd.index("-c") + 1], "copy")
        self.assertEqual(cmd[cmd.index("-tag:v") + 1], "hvc1")
        self.assertEqual(cmd[-1], "/recordings/substream/cam01/%Y-%m-%d/cam01_%Y%m%dT%H%M%SZ.mp4.part")
        self.assertNotIn("-tag:v", sa.ffmpeg_cmd(Path("/r"), "cam01", "rtsp://x", "h264"))

    def test_arrival_clock_only_without_b_frames(self):
        cmd = sa.ffmpeg_cmd(Path("/r"), "cam01", "rtsp://x", "hevc")
        self.assertLess(cmd.index("-use_wallclock_as_timestamps"), cmd.index("-i"))
        self.assertNotIn("-use_wallclock_as_timestamps", sa.ffmpeg_cmd(Path("/r"), "cam01", "rtsp://x", "hevc", b_frames=True))


if __name__ == "__main__":
    unittest.main()
