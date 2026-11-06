#!/usr/bin/env python3
"""M22: the gateway's local health endpoint, with the M12 segment-age monitor.

Runs on the host as the systemd service vivo-health (root, to read SMART, Docker and chrony).
Every 10 s it collects the gateway's state and keeps the latest snapshot, served at

  http://<GATEWAY_LAN_IP>:9101/health    JSON, docs/m22-health.md
  http://<GATEWAY_LAN_IP>:9101/metrics   the same in Prometheus text format
  (and on 127.0.0.1:9101)

It only reads; it changes nothing on the gateway. Alert conditions and their trigger levels are
in ALERTS below and in docs/m22-health.md.

usage:
  sudo tools/health.py                  # serve (the vivo-health service)
  sudo tools/health.py --once           # one snapshot as JSON, then exit
  sudo tools/health.py --once --alerts  # only the active alerts
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import storage_guard as sg  # noqa: E402
import vivo  # noqa: E402
import vivo_disk  # noqa: E402
from substream_archiver import SEGMENT_RE, parse_stamp  # noqa: E402

PORT = int(os.environ.get("VIVO_HEALTH_PORT", "9101"))
INTERVAL = 10
SMART_EVERY = 600                       # SMART is slow and changes slowly
CONTAINERS = ("go2rtc", "frigate", "substream", "storage")
OVERRIDES = Path("/run/vivo/health-overrides.json")   # tests only (tests/m22_check.py), cleared at reboot

# Trigger levels (docs/m22-health.md). The fault-to-alert time is at most the level plus one
# collection interval (10 s); the agreed limit is 60 s (docs/acceptance-limits-proposal.md).
LEVELS = {
    "camera_offline_s": 30,             # main stream: no new bytes in go2rtc for this long
    "main_segment_age_s": 30,           # newest main-stream segment older than this
    "substream_segment_age_s": 150,     # newest substream segment (60 s each) older than this
    "disk_near_margin_percent": 5,      # warning this close to the safe limit; critical at the limit
    "temperature_c": 80,                # CPU package, two samples in a row
    "clock_offset_s": 1.0,              # chrony offset, or chrony not synchronized
    "early_deletion_shown_h": 24,       # an early substream deletion stays an alert this long
}
ALERTS = {
    "camera_offline": "a camera's main stream stopped delivering video",
    "recording_gap": "a camera's newest recorded segment is too old (main or substream), even if its stream looks healthy",
    "recorder_down": "Frigate's API does not answer",
    "disk_missing": "the recording disk is not mounted",
    "disk_near_limit": "the recording disk is close to (warning) or above (critical) its safe limit",
    "ssd_health": "an SSD reports a SMART problem, or the recording filesystem has logged errors",
    "high_temperature": "the CPU package is at or above the temperature limit",
    "clock_not_synced": "chrony is not synchronized, or the clock is off by more than the limit",
    "container_down": "a recorder container is not running",
    "early_deletion": "substream segments were deleted before their retention ended, to keep the disk under its safe limit",
}


def run(cmd: list[str], timeout: float = 15) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""


def read(path: str) -> str:
    try:
        return Path(path).read_text()
    except OSError:
        return ""


# ------------------------------------------------------------------ readers (pure where possible)

def parse_chrony(csv_line: str) -> dict:
    """`chronyc -c tracking` -> synced, offset (s), stratum, leap status."""
    f = csv_line.strip().split(",")
    if len(f) < 14:
        return {"synced": False, "offset_s": None, "stratum": None, "leap": "chronyc not available"}
    leap = f[13]
    stratum = int(f[2]) if f[2].isdigit() else None
    synced = leap != "Not synchronised" and stratum is not None and 0 < stratum < 16
    return {"synced": synced, "offset_s": float(f[4]), "stratum": stratum, "leap": leap,
            "source": f[1]}


def cpu_ticks(stat: str) -> tuple[int, int]:
    for line in stat.splitlines():
        if line.startswith("cpu "):
            v = [int(x) for x in line.split()[1:]]
            return v[3] + v[4], sum(v[:8])
    return 0, 0


def meminfo(text: str) -> dict:
    kv = {l.split(":")[0]: int(l.split()[1]) * 1024 for l in text.splitlines() if ":" in l and len(l.split()) >= 2}
    return {"total_bytes": kv.get("MemTotal", 0), "used_bytes": kv.get("MemTotal", 0) - kv.get("MemAvailable", 0)}


def cpu_temperature() -> float | None:
    """CPU package temperature, same sensor order as tests/idle_report.sh."""
    for h in sorted(Path("/sys/class/hwmon").glob("hwmon*")):
        if read(str(h / "name")).strip() == "coretemp":
            t = read(str(h / "temp1_input")).strip()
            if t.isdigit():
                return int(t) / 1000
    for want in ("x86_pkg_temp", "TCPU", "acpitz"):
        for z in sorted(Path("/sys/class/thermal").glob("thermal_zone*")):
            if read(str(z / "type")).strip() == want:
                t = read(str(z / "temp")).strip()
                if t.lstrip("-").isdigit():
                    return int(t) / 1000
    return None


def fs_errors(media: Path) -> int | None:
    """ext4's error counter for the recording filesystem (/sys/fs/ext4/<dev>/errors_count)."""
    try:
        dev = os.stat(media).st_dev
        name = Path(f"/sys/dev/block/{os.major(dev)}:{os.minor(dev)}").resolve().name
    except OSError:
        return None
    value = read(f"/sys/fs/ext4/{name}/errors_count").strip()
    return int(value) if value.isdigit() else None


def newest_substream_end(root: Path, cam: str, now: float) -> float | None:
    newest = None
    for off in (0, -86400):
        d = root / cam / time.strftime("%Y-%m-%d", time.gmtime(now + off))
        for f in sg.entries(d):
            if (m := SEGMENT_RE.match(f.name)) and m["cam"] == cam:
                end = parse_stamp(m["end"])
                newest = end if newest is None or end > newest else newest
        if newest is not None:
            break
    return newest


# ------------------------------------------------------------------ alerts (pure, unit tested)

def evaluate(snap: dict, memory: dict, now: float, levels: dict = LEVELS) -> list[dict]:
    """Active alerts for a snapshot. `memory` carries what one sample cannot show (since when a
    stream has been silent, how many hot samples in a row) and is updated in place."""
    alerts = []

    def add(alert: str, target: str, level: str, detail: str, since: float | None = None):
        key = f"{alert}:{target}"
        since = memory.setdefault("since", {}).setdefault(key, now if since is None else since)
        alerts.append({"alert": alert, "target": target, "level": level, "detail": detail,
                       "since": vivo.utc(since)})

    for cam, c in snap.get("cameras", {}).items():
        silent = c.get("main_silent_s")
        if silent is not None and silent >= levels["camera_offline_s"]:
            add("camera_offline", cam, "critical", f"no video from the main stream for {silent:.0f} s")
        if c.get("main_segment_age_s") is None or c["main_segment_age_s"] > levels["main_segment_age_s"]:
            age = c.get("main_segment_age_s")
            add("recording_gap", f"{cam}/main", "critical",
                f"newest main-stream segment is {age:.0f} s old" if age is not None else "no main-stream segment in the last 15 min")
        if c.get("substream_segment_age_s") is None or c["substream_segment_age_s"] > levels["substream_segment_age_s"]:
            age = c.get("substream_segment_age_s")
            add("recording_gap", f"{cam}/substream", "critical",
                f"newest substream segment is {age:.0f} s old" if age is not None else "no substream segment today")
    rec = snap.get("recorder", {})
    if not rec.get("frigate_up"):
        add("recorder_down", "frigate", "critical", "Frigate API not answering")
    disk = snap.get("disk", {})
    if not disk.get("mounted", True):
        add("disk_missing", disk.get("path", "recording disk"), "critical", "recording disk not mounted")
    elif disk.get("percent_used") is not None:
        used, limit = disk["percent_used"], disk["limit_percent"]
        if used >= limit:
            add("disk_near_limit", disk.get("path", ""), "critical", f"{used:.1f}% used, at or above the {limit}% safe limit")
        elif used >= limit - levels["disk_near_margin_percent"]:
            add("disk_near_limit", disk.get("path", ""), "warning", f"{used:.1f}% used, {limit - used:.1f} points from the safe limit")
    for s in disk.get("ssd", []):
        if s.get("warnings"):
            add("ssd_health", s.get("device", "?"), "critical", "; ".join(s["warnings"]))
    if disk.get("fs_errors"):
        add("ssd_health", disk.get("path", ""), "critical", f"{disk['fs_errors']} filesystem errors logged by ext4")
    early: dict[str, list[dict]] = {}
    for entry in disk.get("early_deletions", []):
        early.setdefault(entry.get("camera", "?"), []).append(entry)
    for cam, recs in early.items():
        add("early_deletion", f"{cam}/substream", "warning",
            f"{sum(r.get('segments', 0) for r in recs)} substream segments from {min(r['start'] for r in recs)} to "
            f"{max(r['end'] for r in recs)} deleted before their retention ended, to keep the disk under its "
            f"safe limit; newest deletion at "
            f"{max(r['deleted_at'] for r in recs)} (early-deletions.jsonl)",
            since=min(sg.parse_iso(r["deleted_at"]) for r in recs))
    temp = snap.get("gateway", {}).get("temperature_c")
    hot = temp is not None and temp >= levels["temperature_c"]
    memory["hot_samples"] = memory.get("hot_samples", 0) + 1 if hot else 0
    if memory["hot_samples"] >= 2:
        add("high_temperature", "cpu", "warning", f"CPU package {temp:.0f} °C (limit {levels['temperature_c']} °C)")
    clock = snap.get("gateway", {}).get("clock", {})
    off = clock.get("offset_s")
    if not clock.get("synced") or off is None or abs(off) > levels["clock_offset_s"]:
        add("clock_not_synced", "chrony", "warning",
            f"{clock.get('leap', '?')}, offset {off:+.3f} s" if off is not None else str(clock.get("leap", "chronyc not available")))
    for name, c in snap.get("containers", {}).items():
        if c.get("state") != "running":
            add("container_down", name, "critical", f"state {c.get('state', 'missing')}")
    active = {f"{a['alert']}:{a['target']}" for a in alerts}
    memory["since"] = {k: v for k, v in memory.get("since", {}).items() if k in active}
    return alerts


# ------------------------------------------------------------------ collector

class Collector:
    def __init__(self):
        self.lock = threading.Lock()
        self.snapshot: dict = {}
        self.memory: dict = {}
        self.cpu_prev = None
        self.bytes_prev: dict[str, tuple[int, float]] = {}   # stream -> (bytes, last time they grew)
        self.smart_cache: tuple[float, list] = (0.0, [])
        self.config_cache: tuple[float, dict] = (0.0, {})
        self.early_cache: tuple[tuple | None, list] = (None, [])

    def overrides(self) -> dict:
        try:
            return json.loads(OVERRIDES.read_text())
        except (OSError, ValueError):
            return {}

    def gateway(self) -> dict:
        idle, total = cpu_ticks(read("/proc/stat"))
        cpu = None
        if self.cpu_prev and total > self.cpu_prev[1]:
            cpu = round(100 * (1 - (idle - self.cpu_prev[0]) / (total - self.cpu_prev[1])), 1)
        self.cpu_prev = (idle, total)
        uptime = read("/proc/uptime").split()
        return {"cpu_percent": cpu, "memory": meminfo(read("/proc/meminfo")), "temperature_c": cpu_temperature(),
                "uptime_s": round(float(uptime[0])) if uptime else None,
                "clock": parse_chrony(run(["chronyc", "-c", "tracking"]))}

    def cameras(self, cams: list[str], media: Path, now: float) -> dict:
        try:
            streams = vivo.go2rtc("/streams", timeout=5)
        except Exception:
            streams = {}
        try:
            stats = vivo.frigate("/stats", timeout=5).get("cameras", {})
        except Exception:
            stats = {}
        out = {}
        for cam in cams:
            info = (streams or {}).get(f"{cam}_main") if isinstance(streams, dict) else None
            producers = (info or {}).get("producers") or []
            b = sum(int(p.get("bytes_recv") or 0) for p in producers)
            prev = self.bytes_prev.get(cam)
            last_growth = now if prev is None or b > prev[0] else prev[1]
            self.bytes_prev[cam] = (b, last_growth)
            try:
                segs = vivo.frigate(f"/{cam}/recordings?after={int(now) - 900}&before={int(now) + 60}&nocache={time.time_ns()}", timeout=5)
                newest_main = max((s["end_time"] for s in segs), default=None) if isinstance(segs, list) else None
            except Exception:
                newest_main = None
            newest_sub = newest_substream_end(media / "recordings" / "substream", cam, now)
            s = stats.get(cam, {})
            out[cam] = {
                "online": bool(producers) and now - last_growth < LEVELS["camera_offline_s"],
                "main_silent_s": round(now - last_growth, 1) if producers else LEVELS["camera_offline_s"] * 2,
                "fps": s.get("camera_fps"),
                "main_segment_age_s": round(now - newest_main, 1) if newest_main else None,
                "substream_segment_age_s": round(now - newest_sub, 1) if newest_sub else None,
            }
        return out

    def recorder(self, now: float) -> dict:
        up = True
        try:
            vivo.frigate("/version", timeout=5)
        except Exception:
            up = False
        cached_at, cfg = self.config_cache
        if up and now - cached_at > 60:
            try:
                cfg = vivo.frigate("/config", timeout=10)
                self.config_cache = (now, cfg)
            except Exception:
                pass
        rec_on = {n: bool(c.get("record", {}).get("enabled")) for n, c in (cfg.get("cameras") or {}).items()}
        return {"frigate_up": up, "recording_on": rec_on, "recording_on_all": bool(rec_on) and all(rec_on.values())}

    def containers(self) -> dict:
        out = {name: {"state": "missing", "restarts": None, "started": None} for name in CONTAINERS}
        if not shutil.which("docker"):
            return out
        lines = run(["docker", "inspect", "-f", "{{.Name}}|{{.State.Status}}|{{.RestartCount}}|{{.State.StartedAt}}",
                     *CONTAINERS]).splitlines()
        for line in lines:
            parts = line.split("|")
            if len(parts) == 4:
                out[parts[0].lstrip("/")] = {"state": parts[1], "restarts": int(parts[2]), "started": parts[3][:19] + "Z"}
        return out

    def disk(self, media: Path, policy: dict, now: float) -> dict:
        mounted = os.path.ismount(media)
        d = {"path": str(media), "mounted": mounted, "limit_percent": policy["disk_limit_percent"],
             "target_percent": policy["disk_target_percent"]}
        if mounted:
            used, avail, pct = sg.disk_usage(media)
            d.update({"percent_used": round(pct, 2), "free_bytes": avail,
                      "points_below_limit": round(policy["disk_limit_percent"] - pct, 2), "fs_errors": fs_errors(media)})
        cached_at, ssd = self.smart_cache
        if now - cached_at > SMART_EVERY and shutil.which("smartctl"):
            source = vivo_disk.mount_source(media)[0] if mounted else ""
            disks = []
            for src in dict.fromkeys([source, vivo_disk.mount_source(Path("/"))[0]]):
                disks += [x for x in vivo_disk.disks_under(src) if x not in disks] if src and src != "?" else []
            ssd = [vivo_disk.smart(x) for x in disks]
            self.smart_cache = (now, ssd)
        d["ssd"] = ssd
        d["early_deletions"] = self.early_deletions(media, now)
        return d

    def early_deletions(self, media: Path, now: float) -> list[dict]:
        """early-deletions.jsonl lines from the last early_deletion_shown_h hours; read again only when the file changes."""
        path = sg.Layout(media).early_deletions
        try:
            st = os.stat(path)
            stamp = (st.st_mtime_ns, st.st_size)
        except OSError:
            stamp = None
        if stamp != self.early_cache[0]:
            self.early_cache = (stamp, sg.read_early_deletions(path) if stamp else [])
        since = now - LEVELS["early_deletion_shown_h"] * 3600
        return [r for r in self.early_cache[1] if sg.parse_iso(r["deleted_at"]) >= since]

    def collect(self) -> dict:
        now = time.time()
        policy = vivo.services_config()
        media = Path(vivo.SITE.get("MEDIA_ROOT", "/srv/vivo"))
        snap = {"time": vivo.utc(now), "gateway": self.gateway(), "cameras": self.cameras(policy["cameras"], media, now),
                "recorder": self.recorder(now), "disk": self.disk(media, policy, now), "containers": self.containers()}
        ov = self.overrides()
        if ov:   # tests only: shown in the snapshot so nobody mistakes it for the real state
            snap["test_overrides"] = ov
            if "temperature_c" in ov:
                snap["gateway"]["temperature_c"] = ov["temperature_c"]
            if "disk_percent_used" in ov and snap["disk"].get("mounted"):
                snap["disk"]["percent_used"] = ov["disk_percent_used"]
            if ov.get("ssd_warning"):
                snap["disk"]["ssd"] = snap["disk"]["ssd"] + [{"device": "test", "warnings": ["simulated by the M22 test"]}]
            if ov.get("early_deletion"):   # the real early-deletions.jsonl is not touched
                snap["disk"]["early_deletions"] = snap["disk"]["early_deletions"] + [
                    {"camera": "test", "start": sg.iso(now - 2 * 3600), "end": sg.iso(now - 3600),
                     "deleted_at": sg.iso(now), "segments": 60}]
        snap["alerts"] = evaluate(snap, self.memory, now)
        snap["levels"] = LEVELS
        with self.lock:
            self.snapshot = snap
        return snap

    def loop(self) -> None:
        while True:
            try:
                self.collect()
            except Exception as e:   # keep serving the last snapshot
                print(f"{vivo.utc(time.time())} collect failed: {e!r}", flush=True)
            time.sleep(INTERVAL)


def prometheus(snap: dict) -> str:
    def esc(v: str) -> str:
        return str(v).replace("\\", "\\\\").replace('"', '\\"')

    lines = []

    def m(name: str, value, labels: dict | None = None, help_: str = ""):
        if value is None:
            return
        if help_ and not any(l.startswith(f"# HELP {name} ") for l in lines):
            lines.append(f"# HELP {name} {help_}")
        lab = "{" + ",".join(f'{k}="{esc(v)}"' for k, v in (labels or {}).items()) + "}" if labels else ""
        lines.append(f"{name}{lab} {float(value):g}")

    for cam, c in snap.get("cameras", {}).items():
        m("vivo_camera_online", int(c["online"]), {"camera": cam}, "1 if the camera's main stream delivers video")
        m("vivo_camera_fps", c.get("fps"), {"camera": cam}, "frames per second Frigate receives")
        m("vivo_segment_age_seconds", c.get("main_segment_age_s"), {"camera": cam, "stream": "main"}, "age of the newest recorded segment")
        m("vivo_segment_age_seconds", c.get("substream_segment_age_s"), {"camera": cam, "stream": "substream"})
    rec = snap.get("recorder", {})
    m("vivo_recorder_up", int(bool(rec.get("frigate_up"))), None, "1 if Frigate's API answers")
    m("vivo_recording_on", int(bool(rec.get("recording_on_all"))), None, "1 if recording is on for every camera")
    d = snap.get("disk", {})
    m("vivo_disk_mounted", int(bool(d.get("mounted"))), None, "1 if the recording disk is mounted")
    m("vivo_disk_used_percent", d.get("percent_used"), None, "recording disk use")
    m("vivo_disk_limit_percent", d.get("limit_percent"), None, "safe limit on the recording disk")
    m("vivo_fs_errors", d.get("fs_errors"), None, "errors logged by ext4 on the recording filesystem")
    for s in d.get("ssd", []):
        if "error" not in s:
            m("vivo_ssd_healthy", int(not s.get("warnings")), {"device": s.get("device", "?")}, "1 if SMART reports no problem")
    for name, c in snap.get("containers", {}).items():
        m("vivo_container_running", int(c.get("state") == "running"), {"container": name}, "1 if the container runs")
        m("vivo_container_restarts", c.get("restarts"), {"container": name}, "restarts counted by Docker")
    g = snap.get("gateway", {})
    m("vivo_cpu_percent", g.get("cpu_percent"), None, "CPU busy, all cores")
    m("vivo_memory_used_bytes", g.get("memory", {}).get("used_bytes"), None, "RAM used")
    m("vivo_temperature_celsius", g.get("temperature_c"), None, "CPU package temperature")
    m("vivo_uptime_seconds", g.get("uptime_s"), None, "time since boot")
    m("vivo_clock_synced", int(bool(g.get("clock", {}).get("synced"))), None, "1 if chrony is synchronized")
    m("vivo_clock_offset_seconds", g.get("clock", {}).get("offset_s"), None, "chrony's estimate of the clock error")
    for a in snap.get("alerts", []):
        m("vivo_alert", 1, {"alert": a["alert"], "target": a["target"], "level": a["level"]}, "1 for each active alert")
    return "\n".join(lines) + "\n"


def serve(collector: Collector, addresses: list[str]) -> None:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            with collector.lock:
                snap = dict(collector.snapshot)
            if self.path.split("?")[0] == "/health":
                body, ctype = json.dumps(snap, indent=2).encode(), "application/json"
            elif self.path.split("?")[0] == "/metrics":
                body, ctype = prometheus(snap).encode(), "text/plain; version=0.0.4"
            else:
                self.send_error(404, "use /health or /metrics")
                return
            self.send_response(200 if snap else 503)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_):
            pass

    for addr in addresses:
        httpd = ThreadingHTTPServer((addr, PORT), Handler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        print(f"vivo health: http://{addr}:{PORT}/health and /metrics", flush=True)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--alerts", action="store_true", help="with --once: only the active alerts")
    args = ap.parse_args()
    collector = Collector()
    if args.once:
        collector.collect()
        time.sleep(3)              # a second sample, for CPU use and stream activity
        snap = collector.collect()
        print(json.dumps(snap["alerts"] if args.alerts else snap, indent=2))
        return 1 if snap["alerts"] else 0
    lan = vivo.SITE.get("GATEWAY_LAN_IP", "")
    serve(collector, ["127.0.0.1"] + ([lan] if re.match(r"^\d+\.\d+\.\d+\.\d+$", lan) and lan != "127.0.0.1" else []))
    collector.loop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
