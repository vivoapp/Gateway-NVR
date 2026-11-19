"""Unit tests for the Milestone 4 logic: the M25 limit checks (tests/m25_load.py), the M26 test
matrix (tests/m26_matrix.py) and the handoff check (tests/handoff_check.py). No gateway needed.

usage: python3 -m unittest discover -s tests/unit -v
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
import handoff_check as hc  # noqa: E402
import m25_load as m25  # noqa: E402
import m26_matrix as m26  # noqa: E402
import testlib  # noqa: E402

T = 1_800_000_000.0
POLICY = {"record_days": 7, "substream_days": 3, "export_days": 7, "disk_limit_percent": 90, "disk_target_percent": 85}


def good_run(**kw) -> dict:
    """A 24-hour run that passes every limit; kw overrides fields."""
    n = 24 * 3600 // m25.STEP
    run = {"preconditions": [(True, "everything on", "")], "start": T, "end": T + 24 * 3600, "planned_s": 24 * 3600,
           "hours": 24.0, "cpu": [45.0] * n, "ram_mb": [4000.0] * n, "ram_total_mb": 16000.0, "temp": [60.0] * 100,
           "disk_percent": [40.0] * 100, "os_disk_percent": [20.0] * 100, "policy": POLICY,
           "projection": m25.projection(24 * 10**9, 2 * 10**9, 86400, POLICY, 1_200 * 10**9),
           "restarted": [], "health_polls": 2880, "health_answered": 2880, "gaps": []}
    run.update(kw)
    return run


def failed(rows) -> list[str]:
    return [name for ok, name, _ in rows if not ok]


class M25Helpers(unittest.TestCase):
    def test_rolling_peak(self):
        self.assertIsNone(m25.rolling_peak([], 12))
        self.assertAlmostEqual(m25.rolling_peak([10, 20], 12), 15)
        values = [10.0] * 100 + [90.0] * 12 + [10.0] * 100
        self.assertAlmostEqual(m25.rolling_peak(values, 12), 90.0)
        self.assertAlmostEqual(m25.rolling_peak([10.0] * 100 + [100.0] + [10.0] * 100, 12), 17.5)

    def test_growth(self):
        self.assertIsNone(m25.growth([1, 2, 3]))
        self.assertAlmostEqual(m25.growth([100.0] * 40), 0.0)
        self.assertAlmostEqual(m25.growth([100.0] * 20 + [120.0] * 10 + [130.0] * 10), 0.3)

    def test_alert_intervals_and_run_alerts(self):
        polls = [(T, ["camera_offline:cam02"]), (T + 30, ["camera_offline:cam02"]), (T + 60, []),
                 (T + 90, ["camera_offline:cam02", "container_down:storage"])]
        iv = m25.alert_intervals(polls, pad=0)
        self.assertEqual(iv["camera_offline:cam02"], [(T, T + 30), (T + 90, T + 90)])
        self.assertEqual(iv["container_down:storage"], [(T + 90, T + 90)])
        keys = ["camera_offline:cam09", "recording_gap:cam09/main", "recording_gap:cam01/main", "container_down:storage",
                "disk_near_limit:/srv/vivo"]
        self.assertEqual(m25.run_alerts(keys, ["cam01", "cam02"], [f"cam{i:02d}" for i in range(1, 11)]),
                         ["recording_gap:cam01/main", "container_down:storage", "disk_near_limit:/srv/vivo"])

    def test_classify_gaps(self):
        gaps = [{"camera": "cam01", "start": T + 100, "end": T + 130}, {"camera": "cam02", "start": T + 100, "end": T + 130},
                {"camera": "cam03", "start": T + 5000, "end": T + 5010}]
        offline = {"camera_offline:cam02": [(T + 110, T + 150)]}
        out = m25.classify_gaps(gaps, offline, planned=[(T + 4990, T + 5020)])
        self.assertEqual([g["cause"][:6] for g in out], ["UNEXPL", "camera", "planne"])

    def test_projection(self):
        p = m25.projection(24 * 10**9, 2 * 10**9, 86400, POLICY, 1_200 * 10**9)
        self.assertAlmostEqual(p["footprint_gb"], 24 * 7 + 2 * 3)
        self.assertTrue(p["fits"])
        self.assertAlmostEqual(p["main_days_fit"], (1080 - 6) / 24)
        self.assertFalse(m25.projection(200 * 10**9, 2 * 10**9, 86400, POLICY, 1_200 * 10**9)["fits"])


class M25Evaluate(unittest.TestCase):
    def test_good_run_passes(self):
        self.assertEqual(failed(m25.evaluate(good_run())), [])

    def test_each_limit_fails(self):
        n = len(good_run()["cpu"])
        cases = {
            "CPU average": {"cpu": [65.0] * n},
            "CPU highest 1-minute": {"cpu": [40.0] * (n - 12) + [95.0] * 12},
            "RAM highest": {"ram_mb": [12000.0] * n},
            "RAM: no steady growth": {"ram_mb": [4000.0] * (n // 2) + [5000.0] * (n - n // 2)},
            "CPU temperature": {"temp": [85.0]},
            "recording disk below": {"disk_percent": [40.0, 91.0]},
            "days of main stream": {"projection": m25.projection(200 * 10**9, 0, 86400, POLICY, 1_200 * 10**9)},
            "OS disk": {"os_disk_percent": [85.0]},
            "no container restarted": {"restarted": ["frigate (restart count 0 -> 1)"]},
            "health endpoint answered": {"health_answered": 2000},
            "no unexplained recording gap": {"gaps": [{"camera": "cam01", "stream": "main", "start": T, "end": T + 9,
                                                       "length": 9, "cause": "UNEXPLAINED"}]},
            "ran for the planned": {"end": T + 3600, "interrupted": True},
        }
        for prefix, override in cases.items():
            with self.subTest(prefix):
                names = failed(m25.evaluate(good_run(**override)))
                self.assertTrue(names and all(x.startswith(prefix) or prefix in x for x in names), names)

    def test_camera_side_gap_does_not_fail(self):
        gap = {"camera": "cam01", "stream": "main", "start": T, "end": T + 60, "length": 60,
               "cause": "camera stopped sending video (camera_offline alert)"}
        self.assertEqual(failed(m25.evaluate(good_run(gaps=[gap]))), [])

    def test_short_run_skips_growth_and_no_temperature_fails(self):
        rows = m25.evaluate(good_run(hours=1.0, planned_s=3600, end=T + 3600, temp=[],
                                     ram_mb=[4000.0] * 360 + [6000.0] * 360))
        self.assertFalse(any("growth" in name for _, name, _ in rows))
        self.assertEqual(failed(rows), ["CPU temperature under 80 °C"])

    def test_mem_mb(self):
        self.assertAlmostEqual(m25.mem_mb("512MiB"), 512)
        self.assertAlmostEqual(m25.mem_mb("1.5GiB "), 1536)
        self.assertAlmostEqual(m25.mem_mb("1024KiB"), 1)


class TestlibAdditions(unittest.TestCase):
    def test_window_gaps_include_the_edges(self):
        segs = [(T + 20, T + 30), (T + 30, T + 40), (T + 50, T + 80)]
        self.assertEqual(testlib.window_gaps(segs, T, T + 100, 3.0), [(T, T + 20), (T + 40, T + 50), (T + 80, T + 100)])
        self.assertEqual(testlib.window_gaps([(T - 5, T + 50), (T + 50, T + 105)], T, T + 100, 3.0), [])
        self.assertEqual(testlib.window_gaps([], T, T + 100, 3.0), [(T, T + 100)])

    def test_parse_result(self):
        self.assertEqual(testlib.parse_result("x\nResult: **5 passed, 1 failed**\n"), (5, 1))
        self.assertIsNone(testlib.parse_result("no result here"))


class Matrix(unittest.TestCase):
    def test_plan_counts(self):
        full = {s["name"]: s for s in m26.plan(False, None, None)}
        quick = {s["name"]: s for s in m26.plan(True, "off", "on")}
        self.assertEqual(list(full), ["m10-disconnect", "m11-power", "m10-kill", "m10-reboot", "m11-full-disk",
                                      "m11-missing-disk", "m11-corrupt", "m12-integrity"])
        self.assertEqual(full["m10-kill"]["argv"], ["kill", "--count", "10"])
        self.assertEqual(full["m10-reboot"]["argv"], ["reboot", "--count", "10"])
        self.assertEqual(full["m11-power"]["argv"], ["power", "--count", "10"])
        self.assertIn("operator", full["m11-power"]["what"])
        self.assertEqual(quick["m11-power"]["argv"], ["power", "--count", "1", "--plug-off", "off", "--plug-on", "on"])
        self.assertEqual(quick["m10-kill"]["what"], "Frigate killed once")
        self.assertIn("--yes", full["m11-missing-disk"]["argv"])

    def test_select(self):
        steps = m26.plan(False, None, None)
        self.assertEqual([s["name"] for s in m26.select(steps, "m11-corrupt,m10-kill", "")], ["m10-kill", "m11-corrupt"])
        self.assertNotIn("m11-power", [s["name"] for s in m26.select(steps, "", "m11-power")])
        with self.assertRaises(SystemExit):
            m26.select(steps, "m99-nothing", "")

    def test_classify_whole_window(self):
        windows = [("m10-kill", T, T + 1200), ("m10-reboot", T + 1500, T + 5000)]
        gaps = [{"start": T + 100, "end": T + 130}, {"start": T + 1450, "end": T + 1600},
                {"start": T + 1300, "end": T + 1310}, {"start": T + 9000, "end": T + 9010}]
        out = m26.classify(gaps, windows, planned=[(T + 8990, T + 9100)])
        self.assertEqual([g["cause"] for g in out], ["m10-kill", "m10-reboot", "UNEXPLAINED", "planned window"])

    def test_verdict(self):
        ok = "Result: **4 passed, 0 failed**"
        self.assertEqual(m26.verdict(0, ok), ("PASS", "4/4"))
        self.assertEqual(m26.verdict(1, "Result: **3 passed, 1 failed**"), ("FAIL", "3/4"))
        self.assertEqual(m26.verdict(2, ok)[0], "ERROR")
        self.assertEqual(m26.verdict(None, ""), ("ERROR", "no report"))

    def test_report_parsing(self):
        out = "PASS x\nreport: /opt/vivo-gateway/reports/a.md\nmore\nreport: /opt/vivo-gateway/reports/m10-recovery-1.md\n"
        self.assertEqual(m26.last_report_path(out), "/opt/vivo-gateway/reports/m10-recovery-1.md")
        md = "| Result | Check | Detail |\n|---|---|---|\n| PASS | a | |\n| **FAIL** | kill: recording back | slow |\n"
        self.assertEqual(m26.failed_checks(md), ["kill: recording back"])
        self.assertEqual(m26.iso(T), "2027-01-15T08:00:00Z")


class Handoff(unittest.TestCase):
    def test_placeholders_are_not_secrets(self):
        text = ("GO2RTC_API_PASSWORD=change-me\n  password: ${GO2RTC_API_PASSWORD}\n"
                "rtsp://{FRIGATE_GO2RTC_RTSP_USER}:{FRIGATE_GO2RTC_RTSP_PASSWORD}@go2rtc:8554/cam01_main\n"
                "rtsp://***:***@192.168.30.201:554/x\n"
                'return f"rtsp://${{{c}_USER}}:${{{c}_PASSWORD}}@{host}"\n'
                'PASSWORD_LINE = re.compile("x")\nGO2RTC_PASSWORD="$(ssh gw sudo cat x)"\n')
        self.assertEqual(hc.secret_findings("f", text), [])

    def test_real_secrets_are_found(self):
        text = ("VIVOCAM_PASSWORD=Hunter2xyz\n  password: s3cretvalue\nrtsp://admin:Hunter2xyz@10.0.0.5/x\n"
                "-----BEGIN OPENSSH PRIVATE KEY-----\ntoken ghp_" + "a" * 36 + "\n")
        found = hc.secret_findings("f", text)
        self.assertEqual(len(found), 5, found)
        self.assertFalse(any("Hunter2" in f or "s3cret" in f for f in found))   # never print the value

    def test_known_secret_values(self):
        secrets = {"VIVOCAM_PASSWORD": "p@ss:word99", "VIVOCAM_USER": "admin", "SHORT_PASSWORD": "abc"}
        self.assertEqual(len(hc.known_secret_findings("f", "url rtsp://admin:p%40ss%3Aword99@x", secrets)), 1)
        self.assertEqual(hc.known_secret_findings("f", "admin abc", secrets), [])
        self.assertNotIn("p@ss", hc.known_secret_findings("f", "p@ss:word99", secrets)[0])

    def test_pins(self):
        versions = {"GO2RTC_IMAGE": "alexxit/go2rtc", "GO2RTC_VERSION": "1.9.14", "GO2RTC_DIGEST": "sha256:" + "a" * 64,
                    "UBUNTU_VERSION_ID": "24.04", "DOCKER_CE_VERSION": "5:29.9.1", "CONTAINERD_VERSION": "2.4.2",
                    "COMPOSE_PLUGIN_VERSION": "5.6.0"}
        good = {"compose.yaml": "services:\n  a:\n    image: ${GO2RTC_IMAGE}:${GO2RTC_VERSION}@${GO2RTC_DIGEST}\n"}
        self.assertEqual(hc.pin_findings(good, versions), [])
        self.assertEqual(len(hc.pin_findings({"compose.yaml": "    image: nginx:latest\n"}, versions)), 1)
        self.assertEqual(len(hc.pin_findings(good, {**versions, "GO2RTC_VERSION": "latest"})), 1)
        self.assertEqual(len(hc.pin_findings(good, {**versions, "GO2RTC_DIGEST": "sha256:abc"})), 1)
        self.assertEqual(len(hc.pin_findings(good, {k: v for k, v in versions.items() if k != "DOCKER_CE_VERSION"})), 1)

    def test_file_rules(self):
        for bad in ("secrets.env", "gateway/site.env", "id_ed25519", "x/server.key", "cameras.lab.yaml"):
            self.assertTrue(hc.SECRET_FILES.search(bad), bad)
        for ok in ("secrets.env.example", "site.env.example", "gateway/sites/vivo-n150.site.env", "tools/keys.py"):
            self.assertFalse(hc.SECRET_FILES.search(ok), ok)
        self.assertTrue(hc.FOOTAGE.search("reports/cam01.png"))
        self.assertFalse(hc.FOOTAGE.search("docs/m25-load.md"))


if __name__ == "__main__":
    unittest.main()
