"""gateway/setup.sh, docker step: /etc/docker/daemon.json gets our log limits and live-restore merged
into it, and keeps every other key. Mohamed's Mender image sets "data-root": "/data/docker" there
(checked on the test N150 on Nov 25 2026); writing our file over it would move Docker's data back to
the A/B root partitions."""
import json
import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

RECORDER = Path(__file__).resolve().parents[2]
SETUP = RECORDER / "gateway" / "setup.sh"
OURS = RECORDER / "gateway" / "files" / "daemon.json"
# What we asked Mohamed to put in the image's file (group message, Nov 25 2026).
IMAGE_SUGGESTED = ('{"data-root": "/data/docker", "log-driver": "json-file", '
                   '"log-opts": {"max-size": "20m", "max-file": "5"}, "live-restore": false}\n')


def merge(existing: "str | None"):
    """Run setup.sh's docker_daemon_json; existing None means there is no file yet.
    Returns (exit code, stdout, stderr)."""
    func = re.search(r"^docker_daemon_json\(\) \{.*?^\}", SETUP.read_text(), re.S | re.M)
    assert func, "docker_daemon_json not found in gateway/setup.sh"
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "daemon.json"
        if existing is not None:
            path.write_text(existing)
        out = subprocess.run(["bash", "-c", "set -euo pipefail\n" + func.group(0) + '\ndocker_daemon_json "$1" "$2"',
                              "test", str(path), str(OURS)], capture_output=True, text=True)
    return out.returncode, out.stdout, out.stderr


class DockerDaemonJson(unittest.TestCase):
    def test_no_file_writes_ours_as_is(self):
        self.assertEqual(merge(None), (0, OURS.read_text(), ""))

    def test_same_as_ours_is_left_unchanged(self):
        # the reference gateway today: put_file must report "unchanged"
        self.assertEqual(merge(OURS.read_text())[:2], (0, OURS.read_text()))

    def test_mender_image_keeps_data_root(self):
        rc, out, err = merge('{\n  "data-root": "/data/docker"\n}\n')
        self.assertEqual(rc, 0, err)
        self.assertEqual(json.loads(out), {"data-root": "/data/docker", **json.loads(OURS.read_text())})
        self.assertIn("data-root", err)                 # says what it kept
        self.assertTrue(out.endswith("\n"))

    def test_image_with_our_settings_is_left_unchanged(self):
        # once the image carries data-root and our keys (any formatting), nothing is rewritten
        self.assertEqual(merge(IMAGE_SUGGESTED)[:2], (0, IMAGE_SUGGESTED))

    def test_our_values_win_and_other_keys_stay(self):
        rc, out, err = merge('{"data-root": "/data/docker", "log-opts": {"max-size": "100m"}, '
                             '"live-restore": true, "registry-mirrors": ["https://m.example"]}')
        self.assertEqual(rc, 0, err)
        got = json.loads(out)
        self.assertEqual(got["log-opts"], {"max-size": "20m", "max-file": "5"})
        self.assertIs(got["live-restore"], False)
        self.assertEqual(got["data-root"], "/data/docker")
        self.assertEqual(got["registry-mirrors"], ["https://m.example"])

    def test_empty_file_counts_as_no_settings(self):
        self.assertEqual(merge("")[:2], (0, OURS.read_text()))

    def test_bad_file_stops_and_prints_nothing(self):
        for bad in ('{"data-root": "/data/docker",', "[]", '"text"'):
            with self.subTest(bad=bad):
                rc, out, err = merge(bad)
                self.assertNotEqual(rc, 0)
                self.assertEqual(out, "")
                self.assertIn("fix or remove it", err)

    def test_docker_step_merges_before_writing(self):
        # the step must not write gateway/files/daemon.json straight over the file any more, and must
        # stop before put_file when the merge fails (a pipe into put_file would write an empty file)
        text = SETUP.read_text()
        step = re.search(r"^step_docker\(\) \{.*?^\}", text, re.S | re.M).group(0)
        self.assertNotIn('put_file /etc/docker/daemon.json 644 < "$REPO/gateway/files/daemon.json"', step)
        self.assertRegex(step, r'daemon="\$\(docker_daemon_json /etc/docker/daemon\.json "\$REPO/gateway/files/daemon\.json"\)" \\\n\s+\|\| die')
        self.assertIn('printf \'%s\\n\' "$daemon" | put_file /etc/docker/daemon.json 644', step)


if __name__ == "__main__":
    unittest.main()
