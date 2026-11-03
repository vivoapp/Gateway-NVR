#!/usr/bin/env python3
"""M8: disk use and SSD health of the gateway in one command.

usage:
  sudo vivo-disk             # recording disk per folder, safe limit, days stored, OS disk, SSD health
  sudo vivo-disk --fast      # without the folder sizes (they read every file's size)
  sudo vivo-disk --json      # the same as JSON

Exit code 0 when all is well, 1 when the recording disk is at or above its safe limit or an
SSD reports a SMART problem. SMART needs root (smartctl from smartmontools, installed by
gateway/setup.sh).
"""
import argparse
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import storage_guard as sg  # noqa: E402
import vivo  # noqa: E402

# SATA attributes worth showing: id -> label. Any non-zero raw value of the first three is a warning.
ATA_WARN = {5: "reallocated sectors", 197: "pending sectors", 198: "uncorrectable sectors"}
ATA_WEAR = {169: "life left %", 177: "wear leveling", 202: "life used %", 231: "life left %", 233: "media wearout"}


def run(cmd: list[str]) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=30).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""


def gb(n: float) -> str:
    return f"{n / 1e9:,.1f} GB"


def mount_source(path: Path) -> tuple[str, str]:
    out = run(["findmnt", "-n", "-o", "SOURCE,FSTYPE", "--target", str(path)]).split()
    return (out[0], out[1]) if len(out) >= 2 else ("?", "?")


def disks_under(source: str) -> list[str]:
    """Physical disks behind a mounted device, through LVM and partitions (lsblk --inverse)."""
    out = run(["lsblk", "-s", "-n", "-p", "-o", "NAME,TYPE", source])
    disks = []
    for line in out.splitlines():
        parts = line.replace("└─", " ").replace("├─", " ").replace("│", " ").split()
        if len(parts) == 2 and parts[1] == "disk" and parts[0] not in disks:
            disks.append(parts[0])
    return disks


def smart(dev: str) -> dict:
    """Health summary of one disk from `smartctl --json` (needs root)."""
    raw = run(["smartctl", "--json=c", "-H", "-A", "-i", dev])
    try:
        data = json.loads(raw)
    except ValueError:
        return {"device": dev, "error": "smartctl not available"}
    info = {"device": dev, "model": data.get("model_name", "?"),
            "passed": (data.get("smart_status") or {}).get("passed"),
            "temperature_c": (data.get("temperature") or {}).get("current"),
            "power_on_hours": (data.get("power_on_time") or {}).get("hours"), "warnings": []}
    messages = [m.get("string", "") for m in (data.get("smartctl") or {}).get("messages", [])
                if m.get("severity") == "error"]
    if info["passed"] is None and messages:
        info["error"] = "; ".join(messages)[:160]
    nvme = data.get("nvme_smart_health_information_log")
    if nvme:
        info["percentage_used"] = nvme.get("percentage_used")
        info["media_errors"] = nvme.get("media_errors")
        info["available_spare"] = nvme.get("available_spare")
        if nvme.get("media_errors"):
            info["warnings"].append(f"{nvme['media_errors']} media errors")
        if nvme.get("critical_warning"):
            info["warnings"].append(f"critical warning {nvme['critical_warning']}")
    for attr in (data.get("ata_smart_attributes") or {}).get("table", []):
        raw_value = (attr.get("raw") or {}).get("value")
        if attr.get("id") in ATA_WARN:
            info[ATA_WARN[attr["id"]].replace(" ", "_")] = raw_value
            if raw_value:
                info["warnings"].append(f"{raw_value} {ATA_WARN[attr['id']]}")
        elif attr.get("id") in ATA_WEAR:
            info["wear"] = f"{ATA_WEAR[attr['id']]} {attr.get('value')}"
    if info["passed"] is False:
        info["warnings"].append("SMART overall health FAILED")
    return info


def folder_sizes(layout: sg.Layout) -> dict[str, int]:
    sizes = {name: sg.size_of(sg.tree_files(path)) for name, path in (
        ("main stream (Frigate)", layout.main), ("substream segments", layout.substream),
        ("exports (Frigate)", layout.exports), ("Frigate database and state", layout.state))}
    return sizes


def report(media: Path, fast: bool) -> dict:
    layout = sg.Layout(media)
    policy = vivo.services_config()
    used, avail, pct = sg.recording_disk_usage(media)
    source, fstype = mount_source(media)
    now = time.time()
    main = sg.main_hours(layout)
    sub = sg.substream_segments(layout)
    r = {"time": vivo.utc(now), "recording_disk": {
        "path": str(media), "device": source, "fstype": fstype, "size": used + avail, "used": used,
        "free": avail, "percent_used": round(pct, 1), "limit_percent": policy["disk_limit_percent"],
        "target_percent": policy["disk_target_percent"], "at_or_above_limit": pct >= policy["disk_limit_percent"]},
        "retention": {"main_days_setting": policy["record_days"], "substream_days_setting": policy["substream_days"],
                      "export_days_setting": policy["export_days"],
                      "main_oldest": vivo.utc(main[0]) if main else None,
                      "main_days_stored": round((now - main[0]) / 86400, 2) if main else 0,
                      "substream_oldest": vivo.utc(sub[0][0]) if sub else None,
                      "substream_days_stored": round((now - sub[0][0]) / 86400, 2) if sub else 0}}
    if not fast:
        sizes = folder_sizes(layout)
        r["folders"] = sizes
        stored = r["retention"]["main_days_stored"]
        footage = sizes["main stream (Frigate)"] + sizes["substream segments"]
        if stored >= 1 / 24 and footage:
            # Days of footage that fit under the safe limit at the rate recorded so far.
            per_day = sizes["main stream (Frigate)"] / stored + (
                sizes["substream segments"] / max(r["retention"]["substream_days_stored"], 1 / 24))
            room = (used + avail) * policy["disk_limit_percent"] / 100 - (used - footage)
            r["estimate"] = {"footage_per_day": round(per_day), "days_that_fit_under_limit": round(room / per_day, 1)}
    root_used, root_avail, root_pct = sg.disk_usage(Path("/"))
    r["os_disk"] = {"path": "/", "device": mount_source(Path("/"))[0], "size": root_used + root_avail,
                    "free": root_avail, "percent_used": round(root_pct, 1)}
    disks = []
    for src in dict.fromkeys([source, r["os_disk"]["device"]]):
        disks += [d for d in disks_under(src) if d not in disks]
    if not shutil.which("smartctl") or not disks:
        r["ssd_health"] = [{"error": "smartctl or lsblk not available on this system"}]
    else:
        r["ssd_health"] = [smart(d) for d in disks]
    return r


def text(r: dict) -> str:
    d, ret = r["recording_disk"], r["retention"]
    out = [f"Recording disk {d['path']} ({d['device']}, {d['fstype']}), {r['time']}",
           f"  {d['percent_used']}% used: {gb(d['used'])} of {gb(d['size'])}, {gb(d['free'])} free",
           f"  safe limit {d['limit_percent']}%: above it the oldest footage is deleted down to {d['target_percent']}%"
           + ("   ** AT OR ABOVE THE LIMIT **" if d["at_or_above_limit"] else "")]
    if "folders" in r:
        out += [f"    {name:<28} {gb(size):>12}" for name, size in r["folders"].items()]
    out += [f"  main stream: {ret['main_days_stored']} days stored (oldest {ret['main_oldest'] or 'none'}), "
            f"kept {ret['main_days_setting']} days",
            f"  substream:   {ret['substream_days_stored']} days stored (oldest {ret['substream_oldest'] or 'none'}), "
            f"kept {ret['substream_days_setting']} days; exports kept {ret['export_days_setting']} days"]
    if "estimate" in r:
        e = r["estimate"]
        out.append(f"  at the rate so far ({gb(e['footage_per_day'])} a day), about {e['days_that_fit_under_limit']} "
                   "days of footage fit under the safe limit")
    o = r["os_disk"]
    out += [f"OS disk / ({o['device']}): {o['percent_used']}% used, {gb(o['free'])} free", "SSD health (SMART):"]
    for s in r["ssd_health"]:
        if "error" in s:
            out.append(f"  {s.get('device', '')} {s['error']}".rstrip())
            continue
        health = {True: "PASSED", False: "FAILED", None: "unknown"}[s["passed"]]
        extra = [f"{s['temperature_c']} °C" if s.get("temperature_c") is not None else "",
                 f"{s['power_on_hours']} h on" if s.get("power_on_hours") is not None else "",
                 f"{s['percentage_used']}% worn" if s.get("percentage_used") is not None else s.get("wear", ""),
                 f"{s.get('media_errors', 0)} media errors" if "media_errors" in s else "",
                 f"reallocated {s['reallocated_sectors']}" if "reallocated_sectors" in s else "",
                 f"pending {s['pending_sectors']}" if "pending_sectors" in s else ""]
        out.append(f"  {s['device']} {s['model']}: {health}, " + ", ".join(x for x in extra if x)
                   + (f"   ** {'; '.join(s['warnings'])} **" if s["warnings"] else ""))
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--fast", action="store_true", help="skip the folder sizes")
    ap.add_argument("--media", default=vivo.SITE.get("MEDIA_ROOT", "/srv/vivo"), help="recording disk mount point")
    args = ap.parse_args()
    media = Path(args.media)
    if not media.is_dir():
        print(f"ERROR: {media} not found", file=sys.stderr)
        return 2
    r = report(media, args.fast)
    print(json.dumps(r, indent=2) if args.json else text(r))
    bad = r["recording_disk"]["at_or_above_limit"] or any(s.get("warnings") for s in r["ssd_health"])
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
