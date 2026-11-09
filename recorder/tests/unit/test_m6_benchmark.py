"""Unit tests for tests/m6_benchmark.py: the /proc readers, the camera copies and the recording check.

usage: python3 -m unittest discover -s tests/unit -v
"""
import os
import signal
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import m6_benchmark as m6  # noqa: E402
import gen_config  # noqa: E402  (tools/ is on the path through m6_benchmark)

INVENTORY = Path(__file__).resolve().parents[2] / "cameras.yaml"

STAT = "cpu  100 0 50 800 50 0 0 0 0 0\ncpu0 25 0 12 200 12 0 0 0 0 0\n"
MEMINFO = "MemTotal:       12000000 kB\nMemFree:         1000000 kB\nMemAvailable:    8400000 kB\n"
DISKSTATS = """ 259       0 nvme0n1 100 0 2000 10 300 0 4000 20 0 30 30
 259       1 nvme0n1p1 1 0 20 1 3 0 40 2 0 3 3
   8       0 sda 10 0 200 1 30 0 1000 2 0 3 3
 253       1 dm-1 50 0 1000 5 200 0 3000 10 0 15 15
"""
NETDEV = """Inter-|   Receive                            |  Transmit
 face |bytes    packets errs drop fifo frame compressed multicast|bytes    packets errs drop fifo colls carrier compressed
    lo: 5000 10 0 0 0 0 0 0 5000 10 0 0 0 0 0 0
enp1s0: 987650 100 0 0 0 0 0 0 999 9 0 0 0 0 0 0
"""


class Parsers(unittest.TestCase):
    def test_cpu(self):
        self.assertEqual(m6.cpu_ticks(STAT), (850, 1000))

    def test_memory(self):
        self.assertAlmostEqual(m6.mem_used_mb(MEMINFO), 3600000 / 1024)

    def test_disk_writes(self):
        self.assertEqual(m6.disk_written(DISKSTATS, "dm-1"), 3000 * 512)
        self.assertEqual(m6.disk_written(DISKSTATS, None), (4000 + 1000) * 512)   # whole disks only

    def test_network(self):
        self.assertEqual(m6.net_received(NETDEV, "enp1s0"), 987650)
        self.assertEqual(m6.net_received(NETDEV, None), 987650)                    # all but lo


def cams(n):
    return [{"id": f"cam{i:02d}", "name": f"VIVO-CAM-{i:02d}", "host": f"192.168.30.{200 + i}", "port": 554,
             "main_path": "/media/live/1/1", "sub_path": "/media/live/1/2", "credentials": "VIVOCAM",
             "audio": "aac"} for i in range(1, n + 1)]


class Copies(unittest.TestCase):
    def test_up_to_the_real_count_no_copies(self):
        self.assertEqual([c["id"] for c in m6.with_copies(cams(10), 4)], ["cam01", "cam02", "cam03", "cam04"])
        self.assertEqual(m6.with_copies(cams(10), 10), cams(10))

    def test_copies_take_the_real_streams_from_go2rtc(self):
        out = m6.with_copies(cams(10), 15)
        self.assertEqual(out[:10], cams(10))
        self.assertEqual([c["id"] for c in out[10:]], ["copy11", "copy12", "copy13", "copy14", "copy15"])
        c = out[10]
        self.assertEqual((c["host"], c["port"], c["main_path"], c["sub_path"], c["credentials"]),
                         ("127.0.0.1", 8554, "/cam01_main", "/cam01_sub", "FRIGATE_GO2RTC_RTSP"))
        self.assertEqual(out[14]["main_path"], "/cam05_main")
        self.assertEqual(out[14]["audio"], "aac")          # same audio work as the camera it copies

    def test_more_copies_than_cameras_wrap_around(self):
        out = m6.with_copies(cams(2), 5)
        self.assertEqual([c["main_path"] for c in out[2:]], ["/cam01_main", "/cam02_main", "/cam01_main"])

    def test_refuses_ids_that_clash_and_an_empty_list(self):
        with self.assertRaises(ValueError):
            m6.with_copies(cams(2) + [{**cams(1)[0], "id": "copy11"}], 5)
        with self.assertRaises(ValueError):
            m6.with_copies([], 3)

    def test_gen_config_accepts_the_copies(self):
        inv = yaml.safe_load(INVENTORY.read_text())
        inv["cameras"] = m6.with_copies(inv["cameras"], len(inv["cameras"]) + 5)
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "cameras.yaml"
            p.write_text(yaml.safe_dump(inv, sort_keys=False))
            loaded = gen_config.load_inventory(p)
        go2rtc = gen_config.build_go2rtc(loaded)
        frigate = gen_config.build_frigate(loaded)
        n = len(yaml.safe_load(INVENTORY.read_text())["cameras"])
        first = f"copy{n + 1:02d}"
        self.assertEqual(go2rtc["streams"][f"{first}_main"],
                         ["rtsp://${FRIGATE_GO2RTC_RTSP_USER}:${FRIGATE_GO2RTC_RTSP_PASSWORD}@127.0.0.1:8554/cam01_main"])
        self.assertEqual(go2rtc["streams"][f"{first}_sub"][0].rsplit("/", 1)[1], "cam01_sub")
        self.assertIn(first, frigate["cameras"])
        self.assertEqual(len(frigate["cameras"]), n + 5)
        # the real cameras are unchanged
        self.assertEqual(go2rtc["streams"]["cam01_main"][0].split("@")[1], "192.168.30.201:554/media/live/1/1")


class RecordingCheck(unittest.TestCase):
    def run_check(self, main_gaps, sub_gaps, main_cov=600.0, sub_cov=600.0):
        main = {"covered": main_cov, "gaps": main_gaps}
        sub = {"covered": sub_cov, "gaps": sub_gaps}
        with mock.patch.object(m6.continuity, "gaps", return_value=main), \
                mock.patch.object(m6.m7_check, "substream", return_value=sub):
            return m6.recording_check(["cam01"], 1000.0, 1600.0, Path("/nowhere"))[0]

    def test_clean_window_passes(self):
        row = self.run_check([], [(1200.0, 1201.0)])
        self.assertTrue(m6.recording_ok(row))
        self.assertEqual((row["main_pct"], row["sub_gap"]), (100.0, 1.0))

    def test_long_gap_or_low_share_fails(self):
        self.assertFalse(m6.recording_ok(self.run_check([(1200.0, 1211.0)], [])))       # 11 s main gap
        self.assertFalse(m6.recording_ok(self.run_check([], [], sub_cov=580.0)))          # 96.7 % substream
        self.assertTrue(m6.recording_ok(self.run_check([(1200.0, 1210.0)], [], sub_cov=590.0)))   # at the limits

    def test_frigate_share_is_capped(self):
        self.assertEqual(self.run_check([], [], main_cov=640.0)["main_pct"], 100.0)

    def test_an_error_is_a_failed_camera_not_a_crash(self):
        with mock.patch.object(m6.continuity, "gaps", side_effect=RuntimeError("frigate down")), \
                mock.patch.object(m6.m7_check, "substream", return_value={"covered": 0, "gaps": []}):
            row = m6.recording_check(["cam01"], 0, 600, Path("/nowhere"))[0]
        self.assertFalse(m6.recording_ok(row))
        self.assertIn("frigate down", row["error"])


class CopyFolders(unittest.TestCase):
    def test_only_copy_folders_below_the_root_go(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "substream"
            for name in ("cam01", "copy11", "copy12"):
                (root / name / "2026-11-09").mkdir(parents=True)
            outside = Path(d) / "copy13"
            outside.mkdir()
            removed = m6.remove_copy_folders(root, ["copy11", "copy12", "cam01", "../copy13", "copy14"])
            self.assertEqual(removed, ["copy11", "copy12"])
            self.assertEqual(sorted(p.name for p in root.iterdir()), ["cam01"])
            self.assertTrue(outside.is_dir())


class StopMidRun(unittest.TestCase):
    """systemctl stop (SIGTERM) in the middle of a run: the normal stack comes back and a report is written."""

    def setUp(self):
        self.old_handler = signal.getsignal(signal.SIGTERM)

    def tearDown(self):
        signal.signal(signal.SIGTERM, self.old_handler)

    def test_sigterm_restores_the_stack_and_reports(self):
        stats = {"cpu": (40.0, 45.0), "ram": (3500.0, 3600.0), "write": (5.0, 6.0), "net": (6.0, 7.0),
                 "temp": (50.0, 52.0), "containers": {}}
        calls = []

        def start(n, *_):
            calls.append(n)
            if len(calls) == 2:                       # stop arrives while the 15-camera run starts
                os.kill(os.getpid(), signal.SIGTERM)

        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(m6.Stack, "start_subset", side_effect=start), \
                mock.patch.object(m6.Stack, "restore") as restore, \
                mock.patch.object(m6, "sample", return_value=stats), \
                mock.patch.object(m6, "container_cpu", return_value={}), \
                mock.patch.object(m6.time, "sleep"), \
                mock.patch.object(m6.vivo, "REPORTS", Path(d)), \
                mock.patch.object(sys, "argv", ["m6", "--counts", "10,15", "--copies", "--rounds", "1",
                                                "--warmup", "0", "--minutes", "3"]):
            self.assertEqual(m6.main(), 0)
            restore.assert_called_once()
            self.assertEqual(calls, [10, 15])
            report = next(Path(d).glob("m6-benchmark-*.md")).read_text()
            self.assertIn("Stopped early", report)
            self.assertIn("| 1 | 10 |", report)
            windows = (Path(d) / "planned-windows.txt").read_text()
            self.assertIn("10 of 10 cameras", windows)
            self.assertIn("stopped early", windows)


if __name__ == "__main__":
    unittest.main()
