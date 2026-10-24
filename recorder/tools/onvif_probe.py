#!/usr/bin/env python3
"""M3: read each camera's identity and stream setup over ONVIF (read only).

For every camera in the inventory it asks the camera itself for: manufacturer,
model, firmware, its clock and NTP setting, and every media profile (codec,
resolution, frame rate, bitrate, keyframe interval, audio) with the RTSP path
of that profile. Nothing on the camera is changed.

Logins use the camera's own clock for the WS-Security timestamp, because many
cameras reject a login whose timestamp is far from their clock, and camera
clocks are often wrong before NTP is set up.

usage: sudo tools/onvif_probe.py                 (VIVO_INVENTORY=... for another camera list)
       sudo tools/onvif_probe.py --only cam01 --port 80 --json reports/onvif.json
"""
import argparse
import base64
import datetime as dt
import hashlib
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import vivo  # noqa: E402

NS_DEVICE = "http://www.onvif.org/ver10/device/wsdl"
NS_MEDIA = "http://www.onvif.org/ver10/media/wsdl"


class OnvifError(Exception):
    pass


def local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def find(el, *names):
    """First descendant whose local tag names follow the given path."""
    if el is None:
        return None
    cur = [el]
    for name in names:
        nxt = [c for e in cur for c in e.iter() if c is not e and local(c.tag) == name]
        if not nxt:
            return None
        cur = nxt
    return cur[0]


def text(el, *names, default=""):
    f = find(el, *names)
    return (f.text or "").strip() if f is not None and f.text else default


class Camera:
    def __init__(self, host: str, port: int, user: str, password: str, timeout: float = 8):
        self.base = f"http://{host}:{port}"
        self.user, self.password, self.timeout = user, password, timeout
        self.offset = dt.timedelta(0)  # camera clock minus gateway clock

    def _security(self) -> str:
        if not self.user:
            return ""
        nonce = os.urandom(16)
        created = (dt.datetime.now(dt.timezone.utc) + self.offset).strftime("%Y-%m-%dT%H:%M:%SZ")
        digest = base64.b64encode(hashlib.sha1(nonce + created.encode() + self.password.encode()).digest()).decode()
        return (
            '<s:Header><Security s:mustUnderstand="1" xmlns="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd">'
            f"<UsernameToken><Username>{self.user}</Username>"
            '<Password Type="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-username-token-profile-1.0#PasswordDigest">'
            f"{digest}</Password>"
            '<Nonce EncodingType="http://docs.oasis-open.org/wss/2004/01/oasis-200401-soap-message-security-1.0#Base64Binary">'
            f"{base64.b64encode(nonce).decode()}</Nonce>"
            f'<Created xmlns="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd">{created}</Created>'
            "</UsernameToken></Security></s:Header>"
        )

    def call(self, url: str, body: str, auth: bool = True):
        env = ('<?xml version="1.0" encoding="UTF-8"?>'
               '<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope" '
               'xmlns:tt="http://www.onvif.org/ver10/schema">'
               f"{self._security() if auth else ''}<s:Body>{body}</s:Body></s:Envelope>")
        req = urllib.request.Request(url, data=env.encode(), headers={"Content-Type": "application/soap+xml; charset=utf-8"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return ET.fromstring(r.read())
        except urllib.error.HTTPError as e:
            reason = text(ET.fromstring(e.read() or b"<x/>"), "Text") if e.code in (400, 500) else ""
            raise OnvifError(f"HTTP {e.code} {reason}".strip()) from None
        except (urllib.error.URLError, TimeoutError, ET.ParseError) as e:
            raise OnvifError(str(getattr(e, "reason", e))) from None

    def device(self, op: str, inner: str = "", auth: bool = True):
        return self.call(f"{self.base}/onvif/device_service", f'<{op} xmlns="{NS_DEVICE}">{inner}</{op}>', auth)


def camera_clock(cam: Camera) -> dict:
    r = cam.device("GetSystemDateAndTime", auth=False)
    utc = find(r, "UTCDateTime")
    if utc is None:
        return {"clock": "no UTC time reported"}
    t = dt.datetime(int(text(utc, "Year")), int(text(utc, "Month")), int(text(utc, "Day")),
                    int(text(utc, "Hour")), int(text(utc, "Minute")), int(text(utc, "Second")), tzinfo=dt.timezone.utc)
    cam.offset = t - dt.datetime.now(dt.timezone.utc)
    return {"camera_utc": t.strftime("%Y-%m-%d %H:%M:%SZ"),
            "clock_offset_s": round(cam.offset.total_seconds()),
            "camera_tz": text(r, "TimeZone", "TZ"),
            "time_mode": text(r, "DateTimeType")}


def profiles(cam: Camera, media_url: str) -> list:
    out = []
    r = cam.call(media_url, f'<GetProfiles xmlns="{NS_MEDIA}"/>')
    for p in (e for e in r.iter() if local(e.tag) == "Profiles"):
        token = p.get("token", "")
        v = find(p, "VideoEncoderConfiguration")
        a = find(p, "AudioEncoderConfiguration")
        enc = text(v, "Encoding")
        gov = text(v, "H264", "GovLength") or text(v, "H265", "GovLength") or text(v, "GovLength")
        prof = {
            "token": token,
            "name": text(p, "Name"),
            "video": enc,
            "profile": text(v, "H264", "H264Profile") or text(v, "H265", "H265Profile"),
            "res": f"{text(v, 'Resolution', 'Width')}x{text(v, 'Resolution', 'Height')}" if v is not None else "",
            "fps": text(v, "RateControl", "FrameRateLimit"),
            "kbps": text(v, "RateControl", "BitrateLimit"),
            "gov": gov,
            "audio": f"{text(a, 'Encoding')} {text(a, 'SampleRate')}".strip() if a is not None else "",
        }
        try:
            s = cam.call(media_url, f'<GetStreamUri xmlns="{NS_MEDIA}"><StreamSetup>'
                                    "<tt:Stream>RTP-Unicast</tt:Stream><tt:Transport><tt:Protocol>RTSP</tt:Protocol></tt:Transport>"
                                    f"</StreamSetup><ProfileToken>{token}</ProfileToken></GetStreamUri>")
            u = urllib.parse.urlsplit(text(s, "Uri"))
            prof["rtsp_port"] = u.port or 554
            prof["path"] = u.path + (f"?{u.query}" if u.query else "")
        except OnvifError as e:
            prof["path_error"] = str(e)
        out.append(prof)
    return out


def probe_camera(c: dict, port: int) -> dict:
    cred = c["credentials"]
    cam = Camera(c["host"], port, vivo.SECRETS.get(f"{cred}_USER", ""), vivo.SECRETS.get(f"{cred}_PASSWORD", ""))
    res = {"id": c["id"], "host": c["host"]}
    try:
        res.update(camera_clock(cam))
        info = cam.device("GetDeviceInformation")
        res.update({k.lower(): text(info, k) for k in ("Manufacturer", "Model", "FirmwareVersion", "HardwareId")})
        try:
            ntp = cam.device("GetNTP")
            res["ntp"] = "from DHCP" if text(ntp, "FromDHCP") == "true" else \
                (", ".join(t.text for t in ntp.iter() if local(t.tag) in ("IPv4Address", "DNSname") and t.text) or "none")
        except OnvifError as e:
            res["ntp"] = f"unknown ({e})"
        caps = cam.device("GetCapabilities", "<Category>Media</Category>")
        media_url = text(caps, "Media", "XAddr") or f"{cam.base}/onvif/media_service"
        # Some cameras report an XAddr with a different host or port; keep their path on our address.
        media_url = cam.base + urllib.parse.urlsplit(media_url).path
        res["profiles"] = profiles(cam, media_url)
    except OnvifError as e:
        res["error"] = str(e)
    return res


def table(results: list) -> str:
    rows = ["| Camera | Host | Make / model | Firmware | Clock offset | NTP |", "|---|---|---|---|---|---|"]
    for r in results:
        if "error" in r:
            rows.append(f"| {r['id']} | {r['host']} | **error:** {vivo.redact(r['error'])} | | {r.get('clock_offset_s', '')} | |")
            continue
        rows.append(f"| {r['id']} | {r['host']} | {r.get('manufacturer', '')} {r.get('model', '')} | {r.get('firmwareversion', '')} "
                    f"| {r.get('clock_offset_s', '')} s | {r.get('ntp', '')} |")
    rows += ["", "| Camera | Profile | Codec | Resolution | fps | kbps | GOP | Audio | RTSP path |", "|---|---|---|---|---|---|---|---|---|"]
    for r in results:
        for p in r.get("profiles", []):
            rows.append(f"| {r['id']} | {p['name']} ({p['token']}) | {p['video']} {p['profile']} | {p['res']} | {p['fps']} | {p['kbps']} "
                        f"| {p['gov']} | {p['audio']} | {p.get('path') or p.get('path_error', '')} |")
    return "\n".join(rows)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--only", help="comma-separated camera ids")
    ap.add_argument("--port", type=int, default=80, help="ONVIF HTTP port (default 80)")
    ap.add_argument("--json", help="also write the raw results here")
    a = ap.parse_args()
    want = set(a.only.split(",")) if a.only else None
    results = [probe_camera(c, a.port) for c in vivo.cameras() if not want or c["id"] in want]
    print(table(results))
    if a.json:
        Path(a.json).parent.mkdir(parents=True, exist_ok=True)
        Path(a.json).write_text(vivo.redact(json.dumps(results, indent=2)))
    return 1 if any("error" in r for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
