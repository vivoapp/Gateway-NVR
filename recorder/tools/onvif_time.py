#!/usr/bin/env python3
"""M3/M12: point each camera's clock at an NTP server over ONVIF (the gateway, by default).

For every camera it: sets the clock to the gateway's UTC time right away (so the camera does
not wait for its first NTP poll), sets the NTP server, switches the clock mode to NTP, and sets
the timezone used for the time printed on the video (recordings stay in UTC). Then it reads the
camera clock back and reports the offset from the gateway.

The gateway serves NTP to the camera LAN (setup.sh time step, ufw 123/udp from LAN_CIDRS).

usage: sudo tools/onvif_time.py --ntp 192.168.30.200 --tz AST4 [--only cam01,cam02] [--dry-run]
       --tz is a POSIX TZ string: AST4 = Puerto Rico (UTC-4, no DST), UTC0 = UTC.
"""
import argparse
import datetime as dt
import ipaddress
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import vivo  # noqa: E402
from onvif_probe import Camera, OnvifError, camera_clock, text  # noqa: E402


def utc_block(now: dt.datetime) -> str:
    return ("<UTCDateTime><tt:Time>"
            f"<tt:Hour>{now.hour}</tt:Hour><tt:Minute>{now.minute}</tt:Minute><tt:Second>{now.second}</tt:Second>"
            f"</tt:Time><tt:Date><tt:Year>{now.year}</tt:Year><tt:Month>{now.month}</tt:Month><tt:Day>{now.day}</tt:Day>"
            "</tt:Date></UTCDateTime>")


def set_time(cam: Camera, mode: str, tz: str) -> None:
    body = (f"<DateTimeType>{mode}</DateTimeType><DaylightSavings>false</DaylightSavings>"
            f"<TimeZone><tt:TZ>{tz}</tt:TZ></TimeZone>")
    if mode == "Manual":
        body += utc_block(dt.datetime.now(dt.timezone.utc))
    cam.device("SetSystemDateAndTime", body)


def ntp_of(cam: Camera) -> str:
    r = cam.device("GetNTP")
    if text(r, "FromDHCP") == "true":
        return "from DHCP"
    return ", ".join(t.text for t in r.iter() if t.tag.rsplit("}", 1)[-1] in ("IPv4Address", "DNSname") and t.text) or "none"


def configure(c: dict, port: int, ntp: str, tz: str, dry: bool) -> dict:
    cred = c["credentials"]
    cam = Camera(c["host"], port, vivo.SECRETS.get(f"{cred}_USER", ""), vivo.SECRETS.get(f"{cred}_PASSWORD", ""))
    res = {"id": c["id"]}
    try:
        before = camera_clock(cam)
        res["before"] = f"{before['time_mode']}, {before['camera_tz']}, NTP {ntp_of(cam)}, offset {before['clock_offset_s']} s"
        if dry:
            res["after"] = "(dry run)"
            return res
        set_time(cam, "Manual", tz)          # correct now, without waiting for the first NTP poll
        camera_clock(cam)                    # the camera clock jumped: re-read it for the next logins
        cam.device("SetNTP", "<FromDHCP>false</FromDHCP><NTPManual><tt:Type>IPv4</tt:Type>"
                             f"<tt:IPv4Address>{ntp}</tt:IPv4Address></NTPManual>")
        set_time(cam, "NTP", tz)
        time.sleep(2)
        after = camera_clock(cam)
        res["after"] = f"{after['time_mode']}, {after['camera_tz']}, NTP {ntp_of(cam)}, offset {after['clock_offset_s']} s"
        res["ok"] = after["time_mode"] == "NTP" and abs(after["clock_offset_s"]) <= 2
    except OnvifError as e:
        res["error"] = vivo.redact(str(e))
    return res


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ntp", required=True, help="NTP server IPv4 address (the gateway's LAN address)")
    ap.add_argument("--tz", default="UTC0", help="POSIX TZ for the camera's on-screen time (default UTC0)")
    ap.add_argument("--only", help="comma-separated camera ids")
    ap.add_argument("--port", type=int, default=80, help="ONVIF HTTP port (default 80)")
    ap.add_argument("--dry-run", action="store_true", help="only show the current settings")
    a = ap.parse_args()
    ipaddress.IPv4Address(a.ntp)  # fail early on a typo
    want = set(a.only.split(",")) if a.only else None
    rows = [configure(c, a.port, a.ntp, a.tz, a.dry_run) for c in vivo.cameras() if not want or c["id"] in want]
    print("| Camera | Before | After | Result |\n|---|---|---|---|")
    for r in rows:
        result = r.get("error") or ("dry run" if a.dry_run else ("PASS" if r.get("ok") else "FAIL"))
        print(f"| {r['id']} | {r.get('before', '')} | {r.get('after', '')} | {result} |")
    return 0 if a.dry_run or all(r.get("ok") for r in rows) else 1


if __name__ == "__main__":
    sys.exit(main())
