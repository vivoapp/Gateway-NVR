"""tools/onvif_encoder.py: reading and writing one encoder over ONVIF Media2, without a camera."""
import sys
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
import onvif_encoder as enc  # noqa: E402

TT = "http://www.onvif.org/ver10/schema"
# cam01's substream encoder as the camera reported it over Media2 (Nov 23, 2026)
CAM01_0002 = f"""<tr2:Configurations xmlns:tr2="{enc.NS_MEDIA2}" xmlns:tt="{TT}" token="0002" GovLength="100" Profile="Main">
  <tt:Name>0002</tt:Name><tt:UseCount>1</tt:UseCount><tt:Encoding>H265</tt:Encoding>
  <tt:Resolution><tt:Width>640</tt:Width><tt:Height>360</tt:Height></tt:Resolution>
  <tt:RateControl ConstantBitRate="false"><tt:FrameRateLimit>25</tt:FrameRateLimit><tt:BitrateLimit>500</tt:BitrateLimit></tt:RateControl>
  <tt:Multicast><tt:Address><tt:Type>IPv4</tt:Type><tt:IPv4Address>224.8.1.1</tt:IPv4Address></tt:Address>
    <tt:Port>60000</tt:Port><tt:TTL>60</tt:TTL><tt:AutoStart>true</tt:AutoStart></tt:Multicast>
  <tt:Quality>4</tt:Quality>
</tr2:Configurations>"""
AGREED = dict(encoding="H264", profile="Baseline", fps=15, gov=15, kbps=512)   # Oct 28: live view on phones


def parsed() -> dict:
    return enc.parse_config(ET.fromstring(CAM01_0002))


class Encoder(unittest.TestCase):
    def test_reads_the_cameras_answer(self):
        c = parsed()
        self.assertEqual((c["token"], c["encoding"], c["profile"], c["fps"], c["gov"], c["kbps"], c["cbr"]),
                         ("0002", "H265", "Main", "25", "100", "500", "false"))
        self.assertEqual(enc.summary(c), "0002: H265 Main 640x360 25 fps, keyframe every 100 frames, "
                                         "500 kbit/s VBR, quality 4")

    def test_the_agreed_substream_changes_only_those_fields(self):
        before, after = parsed(), enc.changed(parsed(), **AGREED)
        self.assertEqual({k for k in before if before[k] != after[k]},
                         {"encoding", "profile", "fps", "gov", "kbps"})

    def test_the_request_keeps_everything_else_as_the_camera_sent_it(self):
        body = enc.set_body(enc.changed(parsed(), **AGREED))
        root = ET.fromstring(f'<w xmlns:tt="{TT}">{body}</w>')
        conf = root[0][0]
        self.assertEqual((conf.get("token"), conf.get("GovLength"), conf.get("Profile")), ("0002", "15", "Baseline"))
        back = enc.parse_config(conf)
        self.assertEqual((back["encoding"], back["fps"], back["kbps"], back["width"], back["height"]),
                         ("H264", "15", "512", "640", "360"))
        self.assertEqual((back["mc_addr"], back["mc_port"], back["quality"], back["cbr"]), ("224.8.1.1", "60000", "4", "false"))

    def test_going_back_gives_the_original_request(self):
        original = parsed()
        back = enc.changed(enc.changed(original, **AGREED), encoding="H265", profile="Main", fps=25, gov=100, kbps=500)
        self.assertEqual(enc.set_body(back), enc.set_body(original))

    def test_out_of_range_values_are_refused(self):
        for bad in (dict(fps=0), dict(fps=31), dict(gov=129), dict(kbps=32), dict(kbps=4096),
                    dict(encoding="MJPEG"), dict(profile="Extended"), dict(encoding="H265", profile="Baseline")):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                enc.changed(parsed(), **bad)

    def test_text_is_escaped(self):
        c = enc.changed(parsed())
        c["name"] = "a<b&c"
        self.assertIn("<tt:Name>a&lt;b&amp;c</tt:Name>", enc.set_body(c))


if __name__ == "__main__":
    unittest.main()
