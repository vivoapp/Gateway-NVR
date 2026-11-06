"""Unit tests for tools/health.py: readers, alert rules and the Prometheus output (no gateway needed).

usage: python3 -m unittest discover -s tests/unit -v
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
import health  # noqa: E402

CHRONY_OK = ("89BE0204,137.190.2.4,2,1793271355.166118045,0.000012136,-0.000018626,0.000028587,-3.000,-0.000,"
             "0.004,0.050348945,0.001208990,1042.2,Normal")
CHRONY_UNSYNCED = "00000000,,0,0.000000000,0.000000000,0.0,0.0,0.0,0.0,0.0,1.0,1.0,0.0,Not synchronised"


def healthy() -> dict:
    return {
        "cameras": {"cam01": {"online": True, "main_silent_s": 2.0, "fps": 25.0,
                              "main_segment_age_s": 12.0, "substream_segment_age_s": 40.0}},
        "recorder": {"frigate_up": True, "recording_on_all": True},
        "disk": {"path": "/srv/vivo", "mounted": True, "percent_used": 19.0, "limit_percent": 90,
                 "fs_errors": 0, "ssd": [{"device": "/dev/sda", "warnings": []}]},
        "gateway": {"temperature_c": 46.0, "clock": health.parse_chrony(CHRONY_OK)},
        "containers": {"frigate": {"state": "running", "restarts": 0}},
    }


def names(alerts: list[dict]) -> set[str]:
    return {f"{a['alert']}:{a['target']}" for a in alerts}


class Readers(unittest.TestCase):
    def test_chrony(self):
        ok = health.parse_chrony(CHRONY_OK)
        self.assertTrue(ok["synced"])
        self.assertAlmostEqual(ok["offset_s"], 0.000012136)
        self.assertEqual(ok["stratum"], 2)
        self.assertFalse(health.parse_chrony(CHRONY_UNSYNCED)["synced"])
        self.assertFalse(health.parse_chrony("")["synced"])

    def test_memory_and_cpu(self):
        m = health.meminfo("MemTotal: 1000 kB\nMemAvailable: 400 kB\n")
        self.assertEqual(m, {"total_bytes": 1024000, "used_bytes": 614400})
        self.assertEqual(health.cpu_ticks("cpu  10 0 5 80 5 0 0 0 0 0\n"), (85, 100))


class Alerts(unittest.TestCase):
    def test_healthy_gateway_has_no_alerts(self):
        self.assertEqual(health.evaluate(healthy(), {}, 1000.0), [])

    def test_camera_faults(self):
        s = healthy()
        s["cameras"]["cam01"].update(main_silent_s=35.0, main_segment_age_s=45.0, substream_segment_age_s=200.0)
        self.assertEqual(names(health.evaluate(s, {}, 1000.0)),
                         {"camera_offline:cam01", "recording_gap:cam01/main", "recording_gap:cam01/substream"})

    def test_segment_age_alone_is_a_recording_gap(self):
        # The 111-second case: the stream is fine, the recorder is not.
        s = healthy()
        s["cameras"]["cam01"]["main_segment_age_s"] = 111.0
        self.assertEqual(names(health.evaluate(s, {}, 1000.0)), {"recording_gap:cam01/main"})

    def test_disk(self):
        s = healthy()
        s["disk"]["percent_used"] = 86.0
        a = health.evaluate(s, {}, 1000.0)
        self.assertEqual([(x["alert"], x["level"]) for x in a], [("disk_near_limit", "warning")])
        s["disk"]["percent_used"] = 91.0
        self.assertEqual(health.evaluate(s, {}, 1000.0)[0]["level"], "critical")
        s["disk"] = {"path": "/srv/vivo", "mounted": False, "limit_percent": 90, "ssd": []}
        self.assertEqual(names(health.evaluate(s, {}, 1000.0)), {"disk_missing:/srv/vivo"})

    def test_ssd_and_filesystem_errors(self):
        s = healthy()
        s["disk"]["ssd"][0]["warnings"] = ["2 pending sectors"]
        s["disk"]["fs_errors"] = 3
        self.assertEqual(names(health.evaluate(s, {}, 1000.0)), {"ssd_health:/dev/sda", "ssd_health:/srv/vivo"})

    def test_temperature_needs_two_hot_samples(self):
        s, memory = healthy(), {}
        s["gateway"]["temperature_c"] = 85.0
        self.assertEqual(health.evaluate(s, memory, 1000.0), [])
        self.assertEqual(names(health.evaluate(s, memory, 1010.0)), {"high_temperature:cpu"})
        s["gateway"]["temperature_c"] = 70.0
        self.assertEqual(health.evaluate(s, memory, 1020.0), [])

    def test_clock_recorder_and_containers(self):
        s = healthy()
        s["gateway"]["clock"] = health.parse_chrony(CHRONY_UNSYNCED)
        s["recorder"]["frigate_up"] = False
        s["containers"]["frigate"]["state"] = "exited"
        self.assertEqual(names(health.evaluate(s, {}, 1000.0)),
                         {"clock_not_synced:chrony", "recorder_down:frigate", "container_down:frigate"})

    def test_early_deletion_is_a_warning_per_camera(self):
        s = healthy()
        s["disk"]["early_deletions"] = [
            {"camera": "cam01", "start": "2026-11-02T03:00:00Z", "end": "2026-11-02T04:00:12Z",
             "deleted_at": "2026-11-02T10:15:00Z", "segments": 60},
            {"camera": "cam01", "start": "2026-11-02T04:00:12Z", "end": "2026-11-02T05:00:05Z",
             "deleted_at": "2026-11-02T10:16:00Z", "segments": 60}]
        a = health.evaluate(s, {}, 1793700000.0)
        self.assertEqual([(x["alert"], x["target"], x["level"]) for x in a],
                         [("early_deletion", "cam01/substream", "warning")])
        self.assertIn("120 substream segments from 2026-11-02T03:00:00Z to 2026-11-02T05:00:05Z", a[0]["detail"])
        self.assertEqual(a[0]["since"], "2026-11-02 10:15:00 UTC")    # the first deletion, not when it was seen

    def test_since_is_kept_while_active_and_dropped_after(self):
        s, memory = healthy(), {}
        s["recorder"]["frigate_up"] = False
        first = health.evaluate(s, memory, 1000.0)[0]["since"]
        self.assertEqual(health.evaluate(s, memory, 1030.0)[0]["since"], first)
        s["recorder"]["frigate_up"] = True
        health.evaluate(s, memory, 1040.0)
        s["recorder"]["frigate_up"] = False
        self.assertNotEqual(health.evaluate(s, memory, 1050.0)[0]["since"], first)


class EarlyDeletions(unittest.TestCase):
    def test_last_day_only_and_read_again_when_the_file_changes(self):
        now = 1793700000.0
        with tempfile.TemporaryDirectory() as tmp:
            media = Path(tmp)
            path = health.sg.Layout(media).early_deletions
            collector = health.Collector()
            self.assertEqual(collector.early_deletions(media, now), [])          # no file yet
            path.parent.mkdir(parents=True)
            rec = {"camera": "cam01", "start": health.sg.iso(now - 7200), "end": health.sg.iso(now - 3600),
                   "deleted_at": health.sg.iso(now - 600), "segments": 60}
            path.write_text(json.dumps(dict(rec, deleted_at=health.sg.iso(now - 25 * 3600))) + "\n")
            self.assertEqual(collector.early_deletions(media, now), [])          # older than 24 h
            with open(path, "a") as f:
                f.write(json.dumps(rec) + "\n")
            self.assertEqual(collector.early_deletions(media, now), [rec])


class Prometheus(unittest.TestCase):
    def test_metrics(self):
        s = healthy()
        s["alerts"] = [{"alert": "recording_gap", "target": "cam01/main", "level": "critical"}]
        text = health.prometheus(s)
        self.assertIn('vivo_camera_online{camera="cam01"} 1', text)
        self.assertIn('vivo_segment_age_seconds{camera="cam01",stream="main"} 12', text)
        self.assertIn('vivo_alert{alert="recording_gap",target="cam01/main",level="critical"} 1', text)
        self.assertEqual(text.count("# HELP vivo_segment_age_seconds"), 1)


if __name__ == "__main__":
    unittest.main()
