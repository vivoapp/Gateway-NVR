"""Unit tests for the Milestone 3 test logic: gaps matched to events (tests/testlib.py) and the
segment classifier of tests/m12_integrity.py (no gateway needed).

usage: python3 -m unittest discover -s tests/unit -v
"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
import cut_cleanup  # noqa: E402
import m12_integrity as m12  # noqa: E402
import testlib  # noqa: E402

T = 1_800_000_000.0
LIMITS = {"kill": 60, "reboot": 180, "disconnect": 120, "power": 180}


def steady(a: float, b: float, step: float = 10.0, holes: tuple = ()) -> list[tuple[float, float]]:
    """Back-to-back segments from a to b, leaving out the (start, end) holes."""
    out, t = [], a
    while t < b:
        if not any(h0 <= t < h1 for h0, h1 in holes):
            out.append((t, t + step))
        t += step
    return out


class Gaps(unittest.TestCase):
    def test_gaps_from(self):
        self.assertEqual(testlib.gaps_from([(0, 10), (10, 20), (35, 45), (46, 50)], 3.0), [(20, 35)])
        self.assertEqual(testlib.gaps_from([(0, 10), (5, 30), (31, 40)], 3.0), [])


class Analyze(unittest.TestCase):
    def run_case(self, events, holes_by_cam, cams=("cam01", "cam02"), planned=()):
        def segs(cam, stream, a, b):
            return steady(T - 600, T + 1200, 10 if stream == "main" else 60, holes_by_cam.get((cam, stream), ()))
        return testlib.analyze(events, list(cams), LIMITS, list(planned), segments=segs, settle=-10_000_000)

    def test_kill_recovers_and_explains_its_gap(self):
        ev = [{"kind": "kill", "n": "1", "camera": "all", "t_event": T, "t_restore": None, "note": ""}]
        res = self.run_case(ev, {("cam01", "main"): ((T, T + 30),), ("cam02", "main"): ((T, T + 20),)})
        rec = {r["camera"]: r for r in res["events"]}
        self.assertEqual(rec["cam01"]["main_recovery_s"], 30.0)
        self.assertEqual(rec["cam02"]["main_recovery_s"], 20.0)
        self.assertTrue(all(r["pass"] for r in res["events"]))
        self.assertEqual({g["cause"] for g in res["gaps"]}, {"kill 1"})

    def test_slow_recovery_fails(self):
        ev = [{"kind": "kill", "n": "1", "camera": "all", "t_event": T, "t_restore": None, "note": ""}]
        res = self.run_case(ev, {("cam01", "main"): ((T, T + 90),)})
        self.assertFalse(next(r for r in res["events"] if r["camera"] == "cam01")["pass"])

    def test_gap_without_event_is_unexplained(self):
        ev = [{"kind": "kill", "n": "1", "camera": "all", "t_event": T, "t_restore": None, "note": ""}]
        res = self.run_case(ev, {("cam02", "main"): ((T + 600, T + 640),)})
        self.assertEqual([g["cause"] for g in res["gaps"]], ["UNEXPLAINED"])
        res = self.run_case(ev, {("cam02", "main"): ((T + 600, T + 640),)}, planned=[(T + 590, T + 700)])
        self.assertEqual([g["cause"] for g in res["gaps"]], ["planned window"])

    def test_close_events_each_keep_their_own_gap(self):
        # Power cuts 150 s apart, closer than the 180 s limit: each gap belongs to its own cut.
        ev = [{"kind": "power", "n": str(n), "camera": "all", "t_event": T + 150 * (n - 1),
               "t_restore": T + 150 * (n - 1) + 30, "note": ""} for n in (1, 2, 3)]
        holes = tuple((T + 150 * k - 40, T + 150 * k + 60) for k in range(3))
        res = self.run_case(ev, {("cam01", "main"): holes}, cams=("cam01",))
        self.assertEqual([g["cause"] for g in res["gaps"]], ["power 1", "power 2", "power 3"])
        self.assertEqual([r["main_recovery_s"] for r in res["events"]], [30.0, 30.0, 30.0])
        self.assertEqual([r["main_lost_before_s"] for r in res["events"]], [40.0, 40.0, 40.0])

    def test_damaged_file_counts_as_lost(self):
        # A segment that is listed but does not play (left out by the segments function) adds to the loss.
        ev = [{"kind": "power", "n": "1", "camera": "all", "t_event": T, "t_restore": T + 20, "note": ""}]
        res = self.run_case(ev, {("cam01", "substream"): ((T - 60, T + 60),)}, cams=("cam01",))
        self.assertEqual(res["events"][0]["substream_lost_before_s"], 60.0)

    def test_disconnect_counts_from_power_on_and_only_for_its_camera(self):
        ev = [{"kind": "disconnect", "n": "1", "camera": "cam02", "t_event": T, "t_restore": T + 20, "note": ""}]
        res = self.run_case(ev, {("cam02", "main"): ((T, T + 80),), ("cam01", "main"): ((T, T + 30),)})
        self.assertEqual([(r["camera"], r["main_recovery_s"]) for r in res["events"]], [("cam02", 60.0)])
        self.assertIn("UNEXPLAINED", {g["cause"] for g in res["gaps"] if g["camera"] == "cam01"})

    def test_power_cut_measures_what_was_lost_before_it(self):
        ev = [{"kind": "power", "n": "1", "camera": "all", "t_event": T + 5, "t_restore": T + 20, "note": ""}]
        res = self.run_case(ev, {("cam01", "main"): ((T - 10, T + 90),)}, cams=("cam01",))
        r = res["events"][0]
        self.assertEqual(r["main_lost_before_s"], 15.0)        # recording stopped at T - 10, cut at T + 5
        self.assertEqual(r["main_recovery_s"], 70.0)           # back at T + 90, power on at T + 20


class Integrity(unittest.TestCase):
    def test_frigate_file_name(self):
        self.assertEqual(m12.main_rel("cam01", 1793267995), "2026-10-29/09/cam01/59.55.mp4")

    def test_classify(self):
        self.assertIsNone(m12.classify(True, 100, "10.025,hevc", 10.025))
        self.assertEqual(m12.classify(False, 0, None, 10), "file missing")
        self.assertEqual(m12.classify(True, 0, None, 10), "empty file")
        self.assertEqual(m12.classify(True, 100, "ERR moov atom not found", 10), "moov atom not found")
        self.assertEqual(m12.classify(True, 100, "10.0,", 10), "no video stream")
        self.assertIn("recorder wrote down", m12.classify(True, 100, "4.0,hevc", 10.0))
        self.assertEqual(m12.classify(True, 100, "10.0,hevc,0", 10.0, deep_frames=0), "no decodable video frames")
        # A file cut short whose index is at the start: ffprobe still reads it, the box check does not.
        self.assertEqual(m12.classify(True, 60, "10.0,hevc", 10.0, layout="cut short: 60 of 100 bytes"),
                         "cut short: 60 of 100 bytes")

    def test_mp4_boxes(self):
        def box(kind: bytes, body: int, large: bool = False) -> bytes:
            if large:
                return (1).to_bytes(4, "big") + kind + (16 + body).to_bytes(8, "big") + bytes(body)
            return (8 + body).to_bytes(4, "big") + kind + bytes(body)

        frigate = box(b"ftyp", 20) + box(b"moov", 100) + box(b"free", 0) + box(b"mdat", 400)   # index at the start
        cases = {
            "whole": (frigate, None),
            "index at the end": (box(b"ftyp", 20) + box(b"mdat", 400) + box(b"moov", 100), None),
            "64-bit size": (box(b"ftyp", 20) + box(b"moov", 100) + box(b"mdat", 400, large=True), None),
            "last box to the end": (box(b"moov", 100) + (0).to_bytes(4, "big") + b"mdat" + bytes(50), None),
            "cut to 60%": (frigate[:int(len(frigate) * 0.6)], f"cut short: {int(len(frigate) * 0.6)} of {len(frigate)} bytes"),
            "cut before the video": (frigate[:len(frigate) - 408], "no mdat box"),
            "damaged header": (box(b"ftyp", 20) + bytes(16) + box(b"mdat", 10), "damaged box header at byte 28 of 62"),
        }
        with tempfile.TemporaryDirectory() as d:
            for name, (data, want) in cases.items():
                p = Path(d) / "seg.mp4"
                p.write_bytes(data)
                self.assertEqual(testlib.mp4_cut_short(p), want, name)

    def test_probe_parsing_ignores_decoder_warnings(self):
        self.assertEqual(m12.parse_probe("[hevc @ 0x7f] PPS id out of range: 0,hevc,60.000000,", False), "60.000000,hevc")
        self.assertEqual(m12.parse_probe("hevc,1500,60.000000,", True), "60.000000,hevc,1500")
        self.assertEqual(m12.parse_probe("[mov,mp4,m4a,3gp,3g2,mj2 @ 0x7fc8] moov atom not found,/x/y.mp4: Invalid data,", False),
                         "ERR moov atom not found Invalid data")



class CutCleanup(unittest.TestCase):
    """tools/cut_cleanup.py: only the broken segment leaves Frigate's index, never its neighbours."""

    def setUp(self):
        self.rows = [{"id": f"r{i}", "start_time": T + 10 * i, "end_time": T + 10 * (i + 1)} for i in range(3)]
        self.calls = []

    def fake_api(self, overlap_extra: int = 0):
        def call(method, path, timeout=60):
            self.calls.append((method, path))
            if method == "DELETE":
                return {"success": True}
            q = dict(p.split("=") for p in path.split("?", 1)[1].split("&"))
            a, b = float(q["after"]), float(q["before"])
            hits = [r for r in self.rows if r["end_time"] >= a and r["start_time"] <= b]   # Frigate's own rule
            return hits + self.rows[:overlap_extra]
        return call

    def broken_middle(self, path):
        return "file missing" if path == cut_cleanup.segment_file("cam01", T + 10) else None

    def test_deletes_only_the_broken_segment_inside_its_own_times(self):
        n = cut_cleanup.clean("cam01", T - 60, T + 60, False, call=self.fake_api(), damage_of=self.broken_middle)
        self.assertEqual(n, 1)
        deletes = [p for m, p in self.calls if m == "DELETE"]
        self.assertEqual(deletes, [f"/recordings/start/{T + 11:.3f}/end/{T + 19:.3f}?cameras=cam01"])

    def test_leaves_it_when_another_recording_overlaps_the_range(self):
        n = cut_cleanup.clean("cam01", T - 60, T + 60, False, call=self.fake_api(overlap_extra=1),
                              damage_of=self.broken_middle)
        self.assertEqual(n, 0)
        self.assertNotIn("DELETE", [m for m, _ in self.calls])

    def test_overlapping_broken_segments_go_together(self):
        self.rows[1]["end_time"] = T + 21.5            # overlaps the next segment by 1.5 s
        both = {cut_cleanup.segment_file("cam01", T + 10), cut_cleanup.segment_file("cam01", T + 20)}
        n = cut_cleanup.clean("cam01", T - 60, T + 60, False, call=self.fake_api(),
                              damage_of=lambda p: "file missing" if p in both else None)
        self.assertEqual(n, 2)
        self.assertEqual(len([m for m, _ in self.calls if m == "DELETE"]), 1)

    def test_overlap_with_a_healthy_segment_is_left(self):
        self.rows[1]["end_time"] = T + 21.5
        n = cut_cleanup.clean("cam01", T - 60, T + 60, False, call=self.fake_api(), damage_of=self.broken_middle)
        self.assertEqual(n, 0)
        self.assertNotIn("DELETE", [m for m, _ in self.calls])

    def test_api_accepts_a_plain_text_answer(self):
        # Frigate's /api/version answers "0.18.0-...", not JSON: that still means Frigate is up.
        import http.server
        import threading

        class Version(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"0.18.0-test")

            def log_message(self, *args):
                pass
        srv = http.server.HTTPServer(("127.0.0.1", 0), Version)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            old, cut_cleanup.vivo.FRIGATE_API = cut_cleanup.vivo.FRIGATE_API, f"http://127.0.0.1:{srv.server_port}/api"
            self.assertEqual(cut_cleanup.api("GET", "/version", 5), "0.18.0-test")
            self.assertTrue(cut_cleanup.wait_for_frigate(1))
        finally:
            cut_cleanup.vivo.FRIGATE_API = old
            srv.shutdown()

    def test_dry_run_deletes_nothing(self):
        n = cut_cleanup.clean("cam01", T - 60, T + 60, True, call=self.fake_api(), damage_of=self.broken_middle)
        self.assertEqual(n, 1)
        self.assertNotIn("DELETE", [m for m, _ in self.calls])


if __name__ == "__main__":
    unittest.main()
