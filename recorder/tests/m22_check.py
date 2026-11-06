#!/usr/bin/env python3
"""M22 acceptance: each alert condition is triggered on purpose and the health endpoint shows it in time.

usage:
  sudo tests/m22_check.py                    # every alert, one camera (cam02) used for the camera faults
  sudo tests/m22_check.py --only clock_not_synced,container_down --camera cam05

For each alert: cause the fault, poll http://127.0.0.1:9101/health every 2 s until the alert shows
(PASS if within --limit, 60 s agreed before start), undo the fault, and check that the alert clears.

  camera_offline     firewall rule (DOCKER-USER) blocks the camera's address: its stream stops
  recording_gap      the camera's main-stream recorder (Frigate's ffmpeg) is paused while its stream
                     stays healthy: the 111-second case from the assessment (M12 segment-age monitor)
  clock_not_synced   chrony is stopped for a moment
  container_down     the storage container is stopped (recording is not affected)
  disk_near_limit    simulated: a test override reports the disk 1 point over its safe limit
  ssd_health         simulated: a test override adds a SMART warning (a real SSD cannot be made to fail)
  high_temperature   simulated: a test override reports 95 °C (heating the N150 on purpose is not safe)
  early_deletion     simulated: a test override adds an early substream deletion (the real
                     early-deletions.jsonl is not touched)
  recorder_down      only when named in --only: the Frigate container is stopped, so recording stops
                     for about a minute (post the planned window first), then started again
The disk-missing alert is checked by tests/m11_faults.py missing-disk.
The simulated faults use /run/vivo/health-overrides.json, which the snapshot shows as `test_overrides`.
Everything is undone at the end, also after an error. The camera faults leave a short recording gap
on that camera: post the planned window first. If this script itself is killed (kill -9) while the
recorder is paused, Frigate's watchdog restarts that ffmpeg within about 2 minutes (docs/findings.md
F2); to resume it at once: `sudo pkill -CONT -f "go2rtc:8554/<camera>_main"`. A leftover firewall rule:
`sudo iptables -D DOCKER-USER -d <camera ip> -j DROP`; a stopped chrony: `sudo systemctl start chrony`.
"""
import argparse
import json
import os
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import health  # noqa: E402
import testlib  # noqa: E402
import vivo  # noqa: E402

ALL = ["camera_offline", "recording_gap", "clock_not_synced", "container_down", "disk_near_limit", "ssd_health", "high_temperature",
       "early_deletion"]


def sh(*cmd: str, check: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, check=check)


def alerts() -> list[dict]:
    with urllib.request.urlopen(f"http://127.0.0.1:{health.PORT}/health", timeout=5) as f:
        return json.load(f).get("alerts", [])


def shows(alert: str, target: str | None) -> bool:
    return any(a["alert"] == alert and (target is None or a["target"].startswith(target)) for a in alerts())


def set_override(values: dict | None) -> None:
    if values:
        health.OVERRIDES.parent.mkdir(parents=True, exist_ok=True)
        health.OVERRIDES.write_text(json.dumps(values))
    else:
        health.OVERRIDES.unlink(missing_ok=True)


def record_ffmpeg_pid(cam: str) -> int | None:
    """Host pid of Frigate's recording ffmpeg for the camera (the one that writes segments)."""
    out = sh("pgrep", "-f", f"go2rtc:8554/{cam}_main").stdout.split()
    for pid in out:
        cmd = Path(f"/proc/{pid}/cmdline").read_bytes().decode(errors="replace").replace("\0", " ")
        if "segment" in cmd and "ffmpeg" in cmd:
            return int(pid)
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--only", help="comma-separated alerts (default: all)")
    ap.add_argument("--camera", default="cam02")
    ap.add_argument("--limit", type=float, default=60, help="seconds from fault to alert")
    a = ap.parse_args()
    if os.geteuid() != 0:
        print("run with sudo", file=sys.stderr)
        return 2
    wanted = a.only.split(",") if a.only else ALL
    cam = next((c for c in vivo.cameras() if c["id"] == a.camera), None)
    if cam is None:
        print(f"ERROR: {a.camera} not in cameras.yaml", file=sys.stderr)
        return 2
    policy = vivo.services_config()
    r = testlib.Report()
    rows = []
    try:
        base = alerts()
    except Exception as e:
        print(f"ERROR: health endpoint not reachable: {e} (is vivo-health running?)", file=sys.stderr)
        return 2
    r.check("health endpoint answers, /health and /metrics", True,
            f"{len(base)} alerts active before the test: {', '.join(x['alert'] + ':' + x['target'] for x in base) or 'none'}")

    undo: list = []

    seen_with: dict[str, list] = {}

    def trial(alert: str, target: str | None, cause, restore, note: str):
        t0 = time.time()
        try:
            cause()
        except Exception as e:
            r.check(f"{alert}: fault caused", False, f"{note}: {e}")
            return
        undo.append(restore)

        def visible():
            current = alerts()
            if any(x["alert"] == alert and (target is None or x["target"].startswith(target)) for x in current):
                seen_with[alert] = current
                return True
            return False
        seen = testlib.wait_until(visible, a.limit + 60, 2)
        try:
            restore()
        except Exception as e:    # e.g. Frigate already restarted the paused ffmpeg: nothing left to undo
            print(f"undo for {alert}: {e}", flush=True)
        undo.remove(restore)
        cleared = testlib.wait_until(lambda: not shows(alert, target), 240, 5) if seen else None
        delay = seen - t0 if seen else None
        rows.append((alert, note, delay, cleared - t0 if cleared else None))
        r.check(f"{alert}: shown within {a.limit:g} s of the fault, and cleared after it",
                delay is not None and delay <= a.limit and cleared is not None,
                f"shown after {delay:.0f} s, cleared {cleared - seen:.0f} s after the fault was undone" if delay and cleared
                else ("shown after %.0f s, NOT cleared" % delay if delay else "not shown"))

    try:
        if "camera_offline" in wanted:
            rule = ["DOCKER-USER", "-d", cam["host"], "-j", "DROP"]
            trial("camera_offline", a.camera, lambda: sh("iptables", "-I", *rule, check=True),
                  lambda: sh("iptables", "-D", *rule), f"{a.camera} ({cam['host']}) blocked in DOCKER-USER")
        if "recording_gap" in wanted:
            pid = record_ffmpeg_pid(a.camera)
            if pid is None:
                r.check("recording_gap: Frigate's recording ffmpeg found", False, a.camera)
            else:
                trial("recording_gap", f"{a.camera}/main", lambda: os.kill(pid, signal.SIGSTOP),
                      lambda: os.kill(pid, signal.SIGCONT), f"Frigate's recording ffmpeg for {a.camera} paused (pid {pid}); stream untouched")
                r.check("recording_gap was raised without camera_offline (the stream stayed healthy)",
                        "recording_gap" in seen_with and not any(x["alert"] == "camera_offline" and x["target"] == a.camera
                                                                 for x in seen_with["recording_gap"]), "")
        if "clock_not_synced" in wanted:
            trial("clock_not_synced", None, lambda: sh("systemctl", "stop", "chrony", check=True),
                  lambda: sh("systemctl", "start", "chrony"), "chrony stopped")
        if "container_down" in wanted:
            trial("container_down", "storage", lambda: sh("docker", "stop", "storage", check=True),
                  lambda: sh("docker", "start", "storage"), "storage container stopped")
        if "recorder_down" in wanted:     # never in ALL: recording stops while Frigate is down
            trial("recorder_down", "frigate", lambda: sh("docker", "stop", "frigate", check=True),
                  lambda: sh("docker", "start", "frigate"), "Frigate container stopped (recording stops until it is back)")
        sims = {
            "disk_near_limit": ({"disk_percent_used": policy["disk_limit_percent"] + 1}, None, "simulated: disk 1 point over the safe limit"),
            "ssd_health": ({"ssd_warning": True}, None, "simulated SMART warning"),
            "high_temperature": ({"temperature_c": 95}, None, "simulated 95 °C"),
            # its own target, so a real early deletion from the last 24 h does not hide the simulated one
            "early_deletion": ({"early_deletion": True}, "test/substream", "simulated early substream deletion"),
        }
        for alert, (values, target, note) in sims.items():
            if alert in wanted:
                trial(alert, target, lambda v=values: set_override(v), lambda: set_override(None), note)
    finally:
        for restore in list(undo):
            try:
                restore()
            except Exception as e:
                print(f"undo failed: {e}", file=sys.stderr)
        set_override(None)

    md = ["# M22 health endpoint: alerts triggered on purpose", "",
          f"Endpoint http://127.0.0.1:{health.PORT}/health, polled every 2 s. Limit: alert shown within {a.limit:g} s.", ""]
    md += r.markdown() + ["", "| Alert | Fault | Shown after | Cleared after (from the fault) |", "|---|---|---|---|"]
    md += [f"| {al} | {note} | {('%.0f s' % d) if d is not None else 'not shown'} | {('%.0f s' % c) if c is not None else '-'} |"
           for al, note, d, c in rows]
    md += ["", "Trigger levels: docs/m22-health.md. Simulated faults go through the test override file and are marked "
           "`test_overrides` in the snapshot while active."]
    print("report:", vivo.write_report("m22-check", "\n".join(md) + "\n"))
    return 0 if r.passed else 1


if __name__ == "__main__":
    sys.exit(main())
