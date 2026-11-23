"""gateway/setup.sh, disk step: the recording disk's fstab entry may be by UUID (what setup.sh
writes itself) or by LABEL (Mender images, where one image serves disks with different UUIDs)."""
import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

SETUP = Path(__file__).resolve().parents[2] / "gateway" / "setup.sh"
OPTIONS = "ext4 defaults,noatime,commit=2,nofail,x-systemd.device-timeout=15s 0 2"


def fstab_source(fstab: str, mount: str = "/srv/vivo") -> str:
    """Run setup.sh's media_fstab_source on an fstab text."""
    func = re.search(r"^media_fstab_source\(\) \{.*?^\}", SETUP.read_text(), re.S | re.M)
    assert func, "media_fstab_source not found in gateway/setup.sh"
    with tempfile.NamedTemporaryFile("w", suffix=".fstab", delete=False) as f:
        f.write(fstab)
    try:
        out = subprocess.run(["bash", "-c", func.group(0) + '\nmedia_fstab_source "$1" "$2"', "test", f.name, mount],
                             capture_output=True, text=True, check=True)
    finally:
        os.unlink(f.name)
    return out.stdout.strip()


class MediaFstabSource(unittest.TestCase):
    ROOT = "/dev/disk/by-id/dm-uuid-LVM-x / ext4 defaults 0 1\n"

    def test_uuid_entry(self):
        self.assertEqual(fstab_source(self.ROOT + f"UUID=66949717-aa /srv/vivo {OPTIONS}\n"), "UUID=66949717-aa")

    def test_label_entry(self):
        self.assertEqual(fstab_source(self.ROOT + "LABEL=vivo-media /srv/vivo ext4 noatime,commit=2,nofail 0 2\n"),
                         "LABEL=vivo-media")

    def test_no_entry(self):
        self.assertEqual(fstab_source(self.ROOT), "")

    def test_ignores_comments_other_mounts_and_device_paths(self):
        fstab = (self.ROOT
                 + f"# LABEL=old /srv/vivo {OPTIONS}\n"
                 + f"LABEL=vivo-data /data {OPTIONS}\n"
                 + f"UUID=1234 /srv/vivo2 {OPTIONS}\n"
                 + f"/dev/sdb1 /srv/vivo {OPTIONS}\n")
        self.assertEqual(fstab_source(fstab), "")

    def test_mount_point_must_match_exactly(self):
        self.assertEqual(fstab_source(f"LABEL=vivo-media /srv/vivo/ {OPTIONS}\n"), "")

    def test_label_disk_count_under_strict_mode(self):
        # setup.sh runs with set -euo pipefail; blkid exits 2 when no disk has the label.
        func = re.search(r"^label_disk_count\(\) \{.*?^\}", SETUP.read_text(), re.S | re.M).group(0)
        with tempfile.TemporaryDirectory() as bindir:
            stub = Path(bindir) / "blkid"
            for devices, want in (("", "0"), ("/dev/sdb1\n", "1"), ("/dev/sdb1\n/dev/sdc1\n", "2")):
                code = 0 if devices else 2
                stub.write_text(f"#!/bin/sh\nprintf '{devices}'\nexit {code}\n")
                stub.chmod(0o755)
                out = subprocess.run(["bash", "-c", "set -euo pipefail\n" + func + '\nn="$(label_disk_count LABEL=vivo-media)"\necho "n=$n"'],
                                     capture_output=True, text=True, env={**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}"})
                self.assertEqual((out.returncode, out.stdout.strip()), (0, f"n={want}"), out.stderr)

    def test_line_keeps_the_agreed_options(self):
        # Mohamed's Mender image carries this exact line with LABEL=vivo-media (agreed Nov 19, 2026);
        # if the options here change, the image's line must change too.
        self.assertIn(f'line="$src $MEDIA_ROOT {OPTIONS}"', SETUP.read_text())


if __name__ == "__main__":
    unittest.main()
