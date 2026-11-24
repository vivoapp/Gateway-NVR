#!/usr/bin/env python3
"""Show or change one camera encoder over ONVIF Media2 (Block C, M15: the H.264 substream).

The Vivo test cameras answer the older ONVIF media service with "H264" whatever they send
(docs/findings.md F5), but the Media2 service reports and sets the real codec. This tool reads one
encoder configuration through Media2 and, with --yes, writes it back with only the given fields
changed; everything else (resolution, multicast, quality) is sent back as the camera reported it.

It runs on the gateway, where the camera login is (/etc/vivo/secrets.env); nothing secret is printed.
Without --yes, `set` only prints what it would send.

Change an encoder only while the recorder is stopped (`sudo systemctl stop vivo-gateway`, then
set, then start it again): a camera whose stream changes while it records loses its unfinished
substream segment (docs/findings.md F13).

usage:
  sudo tools/onvif_encoder.py show --cam cam01 [--config 0002]
  sudo tools/onvif_encoder.py set  --cam cam01 --config 0002 --encoding H264 --profile Baseline \\
       --fps 15 --gov 15 --kbps 512 [--cbr | --vbr] [--yes]
  # back to what the camera sent before (the values `show` printed):
  sudo tools/onvif_encoder.py set  --cam cam01 --config 0002 --encoding H265 --profile Main \\
       --fps 25 --gov 100 --kbps 500 --vbr --yes
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from xml.sax.saxutils import escape

sys.path.insert(0, str(Path(__file__).resolve().parent))
import onvif_probe as op  # noqa: E402

NS_MEDIA2 = "http://www.onvif.org/ver20/media/wsdl"
ENCODINGS = ("H264", "H265")
PROFILES = ("Baseline", "Main", "High")


def parse_config(el) -> dict:
    """One Media2 VideoEncoder2Configuration as a plain dict."""
    rc = op.find(el, "RateControl")
    mc = op.find(el, "Multicast")
    return {
        "token": el.get("token", ""),
        "gov": el.get("GovLength", ""),
        "profile": el.get("Profile", ""),
        "name": op.text(el, "Name"),
        "use_count": op.text(el, "UseCount"),
        "encoding": op.text(el, "Encoding"),
        "width": op.text(el, "Resolution", "Width"),
        "height": op.text(el, "Resolution", "Height"),
        "cbr": (rc.get("ConstantBitRate", "false") if rc is not None else "false"),
        "fps": op.text(el, "RateControl", "FrameRateLimit"),
        "kbps": op.text(el, "RateControl", "BitrateLimit"),
        "mc_type": op.text(mc, "Address", "Type") if mc is not None else "",
        "mc_addr": op.text(mc, "Address", "IPv4Address") if mc is not None else "",
        "mc_port": op.text(mc, "Port") if mc is not None else "",
        "mc_ttl": op.text(mc, "TTL") if mc is not None else "",
        "mc_auto": op.text(mc, "AutoStart") if mc is not None else "",
        "quality": op.text(el, "Quality"),
    }


def summary(c: dict) -> str:
    rate = "CBR" if c["cbr"] == "true" else "VBR"
    return (f"{c['token']}: {c['encoding']} {c['profile']} {c['width']}x{c['height']} {c['fps']} fps, "
            f"keyframe every {c['gov']} frames, {c['kbps']} kbit/s {rate}, quality {c['quality']}")


def changed(c: dict, **new) -> dict:
    out = dict(c)
    for key, value in new.items():
        if value is not None:
            out[key] = str(value)
    if out["encoding"] not in ENCODINGS:
        raise ValueError(f"encoding must be one of {', '.join(ENCODINGS)}")
    if out["profile"] not in PROFILES:
        raise ValueError(f"profile must be one of {', '.join(PROFILES)}")
    if out["encoding"] == "H265" and out["profile"] != "Main":
        raise ValueError("these cameras offer only the Main profile for H265")
    for key, low, high in (("fps", 1, 30), ("gov", 1, 128), ("kbps", 64, 2048)):
        if not out[key].isdigit() or not low <= int(out[key]) <= high:
            raise ValueError(f"{key} must be a whole number from {low} to {high}")
    return out


def set_body(c: dict) -> str:
    e = {k: escape(str(v)) for k, v in c.items()}
    multicast = ""
    if c["mc_addr"]:
        multicast = (f"<tt:Multicast><tt:Address><tt:Type>{e['mc_type']}</tt:Type>"
                     f"<tt:IPv4Address>{e['mc_addr']}</tt:IPv4Address></tt:Address>"
                     f"<tt:Port>{e['mc_port']}</tt:Port><tt:TTL>{e['mc_ttl']}</tt:TTL>"
                     f"<tt:AutoStart>{e['mc_auto']}</tt:AutoStart></tt:Multicast>")
    return (f'<SetVideoEncoderConfiguration xmlns="{NS_MEDIA2}">'
            f'<Configuration token="{e["token"]}" GovLength="{e["gov"]}" Profile="{e["profile"]}">'
            f"<tt:Name>{e['name']}</tt:Name><tt:UseCount>{e['use_count']}</tt:UseCount>"
            f"<tt:Encoding>{e['encoding']}</tt:Encoding>"
            f"<tt:Resolution><tt:Width>{e['width']}</tt:Width><tt:Height>{e['height']}</tt:Height></tt:Resolution>"
            f'<tt:RateControl ConstantBitRate="{e["cbr"]}"><tt:FrameRateLimit>{e["fps"]}</tt:FrameRateLimit>'
            f"<tt:BitrateLimit>{e['kbps']}</tt:BitrateLimit></tt:RateControl>"
            f"{multicast}<tt:Quality>{e['quality']}</tt:Quality>"
            "</Configuration></SetVideoEncoderConfiguration>")


def connect(cam_id: str):
    import vivo  # reads /etc/vivo/secrets.env; imported here so the unit tests need no gateway
    cams = [c for c in vivo.cameras() if c["id"] == cam_id]
    if not cams:
        raise SystemExit(f"no camera {cam_id} in the inventory")
    c = cams[0]
    cam = op.Camera(c["host"], 80, vivo.SECRETS.get(f"{c['credentials']}_USER", ""),
                    vivo.SECRETS.get(f"{c['credentials']}_PASSWORD", ""))
    op.camera_clock(cam)          # logins are signed with the camera's own clock
    return cam, cam.base + "/onvif/media2_service"


def read(cam, url: str, token: str) -> dict:
    r = cam.call(url, f'<GetVideoEncoderConfigurations xmlns="{NS_MEDIA2}">'
                      f"<ConfigurationToken>{escape(token)}</ConfigurationToken></GetVideoEncoderConfigurations>")
    for el in r.iter():
        if op.local(el.tag) == "Configurations" and el.get("token") == token:
            return parse_config(el)
    raise SystemExit(f"the camera has no encoder configuration {token}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("action", choices=("show", "set"))
    ap.add_argument("--cam", required=True)
    ap.add_argument("--config", default="0002", help="encoder configuration token (0002: the substream)")
    ap.add_argument("--encoding", choices=ENCODINGS)
    ap.add_argument("--profile", choices=PROFILES)
    ap.add_argument("--fps", type=int)
    ap.add_argument("--gov", type=int, help="keyframe interval in frames")
    ap.add_argument("--kbps", type=int)
    rate = ap.add_mutually_exclusive_group()
    rate.add_argument("--cbr", dest="cbr", action="store_const", const="true")
    rate.add_argument("--vbr", dest="cbr", action="store_const", const="false")
    ap.add_argument("--yes", action="store_true", help="really write it to the camera")
    a = ap.parse_args()

    cam, url = connect(a.cam)
    now = read(cam, url, a.config)
    print(f"{a.cam} now   {summary(now)}")
    if a.action == "show":
        return 0
    try:
        want = changed(now, encoding=a.encoding, profile=a.profile, fps=a.fps, gov=a.gov, kbps=a.kbps, cbr=a.cbr)
    except ValueError as e:
        print(f"refused: {e}")
        return 2
    print(f"{a.cam} new   {summary(want)}")
    if not a.yes:
        print("not written (add --yes)")
        return 0
    cam.call(url, set_body(want))
    after = read(cam, url, a.config)
    print(f"{a.cam} after {summary(after)}")
    same = all(after[k] == want[k] for k in ("encoding", "profile", "fps", "gov", "kbps", "cbr"))
    print("the camera took the new settings" if same else "the camera reports different settings")
    return 0 if same else 1


if __name__ == "__main__":
    sys.exit(main())
