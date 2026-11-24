"""Per-camera substream decoding in tools/gen_config.py (Block C, M15: one camera's substream in H.264)."""
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))
import gen_config  # noqa: E402


def inventory(**cam01) -> dict:
    cams = [{"id": f"cam0{i}", "name": f"CAM-0{i}", "host": f"192.0.2.{i}", "main_path": "/main",
             "sub_path": "/sub", "credentials": "VIVOCAM"} for i in (1, 2)]
    cams[0].update(cam01)
    return {"site": {"hwaccel": "preset-intel-qsv-h265"}, "cameras": cams}


def load(data: dict) -> dict:
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "cameras.yaml"
        path.write_text(yaml.safe_dump(data))
        return gen_config.load_inventory(path)


def inputs(frigate: dict, cam: str) -> dict:
    return {tuple(i["roles"]): i for i in frigate["cameras"][cam]["ffmpeg"]["inputs"]}


class PerCameraHwaccel(unittest.TestCase):
    def test_only_that_cameras_substream_input_gets_it(self):
        frigate = gen_config.build_frigate(load(inventory(hwaccel="preset-intel-qsv-h264")))
        self.assertEqual(frigate["ffmpeg"]["hwaccel_args"], "preset-intel-qsv-h265")       # site default
        cam01, cam02 = inputs(frigate, "cam01"), inputs(frigate, "cam02")
        self.assertEqual(cam01[("detect",)]["hwaccel_args"], "preset-intel-qsv-h264")
        self.assertNotIn("hwaccel_args", cam01[("record",)])        # the main stream is copied, not decoded
        self.assertNotIn("hwaccel_args", cam02[("detect",)])

    def test_without_it_the_config_is_as_before(self):
        frigate = gen_config.build_frigate(load(inventory()))
        for cam in ("cam01", "cam02"):
            self.assertTrue(all("hwaccel_args" not in i for i in inputs(frigate, cam).values()))

    def test_bad_values_are_refused(self):
        for bad in ("-hwaccel qsv", "", 5, "preset-x; rm -rf /"):
            with self.subTest(bad=bad), self.assertRaises(gen_config.InventoryError):
                load(inventory(hwaccel=bad))

    def test_the_reference_inventory_still_loads(self):
        inv = gen_config.load_inventory(ROOT / "cameras.yaml")
        self.assertEqual(len(inv["cameras"]), 10)


if __name__ == "__main__":
    unittest.main()
