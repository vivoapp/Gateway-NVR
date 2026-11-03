"""Unit tests for the /proc readers in tests/m6_benchmark.py.

usage: python3 -m unittest discover -s tests/unit -v
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import m6_benchmark as m6  # noqa: E402

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


if __name__ == "__main__":
    unittest.main()
