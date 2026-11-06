#!/usr/bin/env python3
"""Shared helpers for the Milestone 3 tests (M10, M11, M12, M22).

- Report: PASS/FAIL rows and their Markdown table.
- Recording gaps for both streams: the main stream from Frigate's segment list, the substream
  from the segment names on disk (docs/SEGMENT_FORMAT.md).
- analyze(): match every gap to the test event that caused it (reboot, Frigate kill, camera
  disconnect, power cut), measure each event's recovery, and list any gap left unexplained.
- Remote: runs commands on the gateway over SSH, for the tests that reboot or power-cycle it and
  so cannot run on the gateway itself. Event times come from the gateway's own clock.

On the gateway, `python3 tests/testlib.py recording-after EPOCH` prints, as JSON, each camera's
first main-stream and substream segment start after that time (used by Remote), and
`python3 tests/testlib.py gaps EPOCH EPOCH` every gap in that window (used by tests/m26_matrix.py).
"""
import csv
import json
import re
import shlex
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import continuity  # noqa: E402
import vivo  # noqa: E402
from mp4box import cut_short as mp4_cut_short  # noqa: E402,F401  (used by m11_faults and m12_integrity)
from substream_archiver import SEGMENT_RE, parse_stamp  # noqa: E402

GATEWAY_ROOT = "/opt/vivo-gateway"
MAIN_MIN_GAP = 3.0       # smaller holes between Frigate's 10 s segments are not gaps
SUB_MIN_GAP = 2.0        # substream names are to the second


class Report:
    def __init__(self):
        self.rows: list[tuple[bool, str, str]] = []

    def check(self, name: str, ok: bool, detail: str = "") -> bool:
        self.rows.append((bool(ok), name, detail))
        print(f"{'PASS' if ok else 'FAIL'}  {name}  {detail}", flush=True)
        return bool(ok)

    @property
    def passed(self) -> bool:
        return all(ok for ok, _, _ in self.rows)

    def markdown(self) -> list[str]:
        n = sum(ok for ok, _, _ in self.rows)
        return [f"Result: **{n} passed, {len(self.rows) - n} failed**", "", "| Result | Check | Detail |", "|---|---|---|"] + [
            f"| {'PASS' if ok else '**FAIL**'} | {name} | {str(detail).replace('|', '/')} |" for ok, name, detail in self.rows]


def wait_until(pred, timeout: float, poll: float = 1.0) -> float | None:
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            if pred():
                return time.time()
        except Exception:
            pass
        time.sleep(poll)
    return None


def substream_root() -> Path:
    return Path(vivo.SITE.get("MEDIA_ROOT", "/srv/vivo")) / "recordings" / "substream"


def substream_segments(cam: str, after: float, before: float, root: Path | None = None) -> list[tuple[int, int]]:
    """(start, end) of the camera's finished substream segments that overlap the window, in order."""
    root = root or substream_root()
    out = []
    day = int(after) // 86400 * 86400
    while day <= before:
        d = root / cam / time.strftime("%Y-%m-%d", time.gmtime(day))
        if d.is_dir():
            for f in d.iterdir():
                m = SEGMENT_RE.match(f.name)
                if m and m["cam"] == cam:
                    s, e = parse_stamp(m["start"]), parse_stamp(m["end"])
                    if e >= after and s <= before:
                        out.append((s, e))
        day += 86400
    return sorted(out)


def gaps_from(segs: list[tuple[float, float]], min_gap: float) -> list[tuple[float, float]]:
    out, end = [], None
    for s, e in segs:
        if end is not None and s - end > min_gap:
            out.append((end, s))
        end = e if end is None else max(end, e)
    return out


def window_gaps(segs: list[tuple[float, float]], a: float, b: float, min_gap: float) -> list[tuple[float, float]]:
    """Gaps inside [a, b], including a late first segment and an early last one."""
    inside = [(s, e) for s, e in segs if e >= a and s <= b]
    return gaps_from([(a, a)] + inside + [(b, b)], min_gap)


def all_gaps(a: float, b: float, cams: list[str] | None = None) -> list[dict]:
    """Every gap in [a, b], both streams, every camera (b must be at least 90 s in the past)."""
    out = []
    for cam in cams or vivo.services_config()["cameras"]:
        for stream, min_gap, segs in (("main", MAIN_MIN_GAP, main_segments(cam, a - 30, b + 30)),
                                      ("substream", SUB_MIN_GAP, substream_segments(cam, a - 90, b + 90))):
            out += [{"camera": cam, "stream": stream, "start": g0, "end": g1, "length": g1 - g0}
                    for g0, g1 in window_gaps(segs, a, b, min_gap)]
    return out


def parse_result(text: str) -> tuple[int, int] | None:
    """(passed, failed) from a report's "Result: **N passed, M failed**" line, or None."""
    m = re.search(r"Result: \*\*(\d+) passed, (\d+) failed\*\*", text)
    return (int(m[1]), int(m[2])) if m else None


def main_segments(cam: str, after: float, before: float) -> list[tuple[float, float]]:
    return [(s["start_time"], s["end_time"]) for s in continuity.segments(cam, after, before)]


def first_start_after(segs: list[tuple[float, float]], t: float) -> float | None:
    return min((s for s, _ in segs if s >= t), default=None)


def recording_after(t: float, cams: list[str] | None = None) -> dict:
    """Each camera's first main and substream segment start at or after t (None if not yet)."""
    cams = cams or vivo.services_config()["cameras"]
    now = time.time() + 60
    return {cam: {"main": first_start_after(main_segments(cam, t - 30, now), t),
                  "substream": first_start_after(substream_segments(cam, t - 90, now), t)} for cam in cams}


# ------------------------------------------------------------------ events and gaps

EVENT_FIELDS = ["kind", "n", "camera", "t_event", "t_restore", "note"]


def write_events(path: Path, events: list[dict]) -> None:
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=EVENT_FIELDS)
        w.writeheader()
        w.writerows(events)


def read_events(paths: list[str]) -> list[dict]:
    events = []
    for p in paths:
        with open(p) as f:
            for r in csv.DictReader(f):
                r["t_event"] = float(r["t_event"])
                r["t_restore"] = float(r["t_restore"]) if r.get("t_restore") else None
                events.append(r)
    return sorted(events, key=lambda e: e["t_event"])


def analyze(events: list[dict], cams: list[str], limits: dict, planned: list[tuple[float, float]],
            segments=None, settle: float = 30) -> dict:
    """Recovery per event and camera, and the gap log with each gap's cause.

    An event's reference time is when the fault ends (power back, camera back on) or, for a
    reboot or kill, when it was started. Its recovery is the end of the gaps it caused minus that
    time (0 if recording never stopped). A gap is explained by an event that overlaps it, or by a
    --planned window; any other gap is unexplained. When events are closer together than their
    limit, a gap goes to the latest event that started before it ended, not to the one before it.
    `segments(cam, stream, after, before)` can be replaced in tests.
    """
    segments = segments or (lambda cam, stream, a, b: main_segments(cam, a, b) if stream == "main"
                            else substream_segments(cam, a, b))
    if not events:
        return {"events": [], "gaps": [], "window": None}
    t0 = min(e["t_event"] for e in events) - 120
    t1 = max((e["t_restore"] or e["t_event"]) + limits.get(e["kind"], 180) for e in events) + 120
    t1 = min(t1, time.time() - settle)
    latest_first = sorted(events, key=lambda e: e["t_event"], reverse=True)
    gap_log, per_event = [], []
    for cam in cams:
        for stream, min_gap in (("main", MAIN_MIN_GAP), ("substream", SUB_MIN_GAP)):
            gaps = gaps_from(segments(cam, stream, t0, t1), min_gap)
            for a, b in gaps:
                cause = next((f"{e['kind']} {e['n']}" for e in latest_first
                              if (e["camera"] in ("all", cam)) and a <= (e["t_restore"] or e["t_event"]) + limits.get(e["kind"], 180)
                              and b >= e["t_event"] - 15), None)
                if cause is None and any(p0 - 60 <= a and b <= p1 + 60 for p0, p1 in planned):
                    cause = "planned window"
                gap_log.append({"camera": cam, "stream": stream, "start": a, "end": b, "length": b - a,
                                "cause": cause or "UNEXPLAINED"})
    for e in events:
        ref = e["t_restore"] or e["t_event"]
        limit = limits.get(e["kind"], 180)
        for cam in cams if e["camera"] == "all" else [e["camera"]]:
            row = {"event": f"{e['kind']} {e['n']}", "camera": cam, "at": e["t_event"]}
            for stream in ("main", "substream"):
                mine = [g for g in gap_log if g["camera"] == cam and g["stream"] == stream and g["cause"] == row["event"]]
                row[f"{stream}_recovery_s"] = round(max(g["end"] for g in mine) - ref, 1) if mine else 0.0
                row[f"{stream}_lost_before_s"] = round(max(0.0, e["t_event"] - min(g["start"] for g in mine)), 1) if mine else 0.0
            row["pass"] = row["main_recovery_s"] <= limit and row["substream_recovery_s"] <= limit + 70
            per_event.append(row)
    return {"events": per_event, "gaps": gap_log, "window": (t0, t1)}


def gap_table(gaps: list[dict]) -> list[str]:
    lines = ["| Camera | Stream | Gap start (UTC) | End | Length | Cause |", "|---|---|---|---|---|---|"]
    lines += [f"| {g['camera']} | {g['stream']} | {vivo.utc(g['start'])} | {vivo.utc(g['end'])[11:]} | "
              f"{g['length']:.1f} s | {g['cause'] if g['cause'] != 'UNEXPLAINED' else '**unexplained**'} |" for g in gaps]
    return lines if gaps else ["No gaps."]


def parse_window(s: str) -> tuple[float, float]:
    import datetime as dt
    a, b = s.split("/")
    f = lambda x: dt.datetime.fromisoformat(x.replace("Z", "+00:00")).timestamp()  # noqa: E731
    return f(a), f(b)


# ------------------------------------------------------------------ SSH to the gateway

class Remote:
    """The gateway over SSH (key login, passwordless sudo, as for the other checks)."""

    def __init__(self, host: str, root: str = GATEWAY_ROOT):
        self.host, self.root = host, root

    def run(self, cmd: str, timeout: float = 60) -> subprocess.CompletedProcess:
        return subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", "-o", "ServerAliveInterval=5",
                               self.host, cmd], capture_output=True, text=True, timeout=timeout)

    def up(self) -> bool:
        try:
            return self.run("true", timeout=12).returncode == 0
        except subprocess.TimeoutExpired:
            return False

    def clock(self) -> tuple[float, float]:
        """(gateway time, gateway minus local clock) from one round trip."""
        t0 = time.time()
        out = self.run("date +%s.%N", timeout=15)
        t1 = time.time()
        g = float(out.stdout.strip())
        return g, g - (t0 + t1) / 2

    def cameras(self) -> list[str]:
        return json.loads(self.run(f"cat {self.root}/services/config.json").stdout)["cameras"]

    def recording_after(self, t: float) -> dict:
        out = self.run(f"python3 {self.root}/tests/testlib.py recording-after {t:.3f}", timeout=90)
        return json.loads(out.stdout) if out.returncode == 0 and out.stdout.strip() else {}

    def wait_down(self, timeout: float) -> float | None:
        return wait_until(lambda: not self.up(), timeout, 2)

    def wait_up(self, timeout: float) -> float | None:
        return wait_until(self.up, timeout, 3)

    def wait_recording(self, t: float, timeout: float) -> dict:
        """Wait until every camera recorded both streams after t; return the last answer."""
        last: dict = {}

        def done():
            nonlocal last
            last = self.recording_after(t)
            return bool(last) and all(v["main"] and v["substream"] for v in last.values())
        wait_until(done, timeout, 10)
        return last

    def report(self, script: str, events: list[dict], extra: str = "") -> str:
        """Copy the events to the gateway, build the report there, and return its text."""
        name = f"/tmp/{Path(script).stem}-events-{time.strftime('%Y%m%d-%H%M%S', time.gmtime())}.csv"
        local = Path(tempfile.gettempdir()) / Path(name).name
        write_events(local, events)
        subprocess.run(["scp", "-q", str(local), f"{self.host}:{name}"], check=True)
        out = self.run(f"sudo -n python3 {self.root}/tests/{script} report --events {shlex.quote(name)} {extra}", timeout=900)
        print(out.stdout + out.stderr)
        path = next((l.split("report:", 1)[1].strip() for l in out.stdout.splitlines() if l.startswith("report:")), "")
        return self.run(f"sudo -n cat {shlex.quote(path)}").stdout if path else ""


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "recording-after":
        print(json.dumps(recording_after(float(sys.argv[2]))))
    elif len(sys.argv) == 4 and sys.argv[1] == "gaps":
        print(json.dumps(all_gaps(float(sys.argv[2]), float(sys.argv[3]))))
    else:
        print(__doc__)
        sys.exit(2)
