#!/usr/bin/env python3
"""M12 acceptance: recording integrity and timeline QA.

usage:
  sudo tests/m12_integrity.py                       # last 24 hours, every camera, both streams
  sudo tests/m12_integrity.py --hours 1 --spot 10   # a short scan, with 10 segments for a manual spot-check
  sudo tests/m12_integrity.py --deep                # also read every frame (slower; finds damage inside files)
  sudo tests/m12_integrity.py --start "2026-11-03 15:17" --streams main   # a fixed 24 hours, main stream only

For each camera and stream over the period it lists:
- every missing interval (a gap between segments longer than 3 s for the main stream, 2 s for the
  substream; gaps inside --planned windows are marked as planned)
- every segment that will not play, with its cause: file missing, empty file, cut short (its MP4
  boxes do not add up to its size), ffprobe's error, no video, or a length that differs from what
  the recorder wrote down (Frigate's database for the main stream, the file name for the substream)
and a clock check: chrony's offset from UTC, and each recent segment's time against when it was
written to disk, within the tolerance agreed before start (2 s for chrony, docs/acceptance-limits-proposal.md).

--spot N picks N random segments with their times and paths, for a person to open in the Frigate
UI or a player and tick off; the M12 "Done when" compares those with the script's verdicts.

The live segment-age monitor (a camera whose newest segment is too old although its stream looks
healthy) is the recording_gap alert of the health endpoint, tools/health.py (M22).
"""
import argparse
import calendar
import csv
import random
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import continuity  # noqa: E402
import health  # noqa: E402
import testlib  # noqa: E402
import vivo  # noqa: E402
from substream_archiver import SEGMENT_RE, parse_stamp, segment_name  # noqa: E402

MEDIA = Path(vivo.SITE.get("MEDIA_ROOT", "/srv/vivo"))
MAIN_HOST = MEDIA / "frigate" / "media" / "recordings"
MAIN_CTR = "/media/frigate/recordings"          # inside the frigate container
SUB_CTR = "/recordings/substream"               # inside the substream container
CLOCK_TOLERANCE = 2.0
LENGTH_TOLERANCE = 2.0


def main_rel(cam: str, start: float) -> str:
    """Frigate's file for a segment: recordings/YYYY-MM-DD/HH/<camera>/MM.SS.mp4 (UTC start time)."""
    return time.strftime(f"%Y-%m-%d/%H/{cam}/%M.%S.mp4", time.gmtime(start))


def classify(exists: bool, size: int, probe: str | None, expected: float, deep_frames: int | None = None,
             layout: str | None = None) -> str | None:
    """None if the segment plays, else the cause. probe is ffprobe's 'duration,codec' line or its error;
    layout is testlib.mp4_cut_short's finding (a file cut short whose index still looks whole)."""
    if not exists:
        return "file missing"
    if size == 0:
        return "empty file"
    if layout:
        return layout
    if probe is None or probe.startswith("ERR "):
        return (probe or "ERR no output")[4:].strip()[:160] or "ffprobe failed"
    fields = probe.split(",")
    try:
        dur = float(fields[0])
    except (ValueError, IndexError):
        return f"no duration ({probe[:60]})"
    if len(fields) < 2 or not fields[1].strip():
        return "no video stream"
    if dur <= 0:
        return "duration 0"
    if expected and abs(dur - expected) > LENGTH_TOLERANCE:
        return f"{dur:.1f} s long, the recorder wrote down {expected:.1f} s"
    if deep_frames is not None and deep_frames == 0:
        return "no decodable video frames"
    return None


def parse_probe(value: str, deep: bool) -> str:
    """ffprobe's csv output for one file -> 'duration,codec[,frames]', or 'ERR message'.

    The reference cameras' H.265 makes ffprobe print warnings for files that play fine (docs/findings.md
    F9), so messages alone do not make a segment broken: only a missing length or video stream does.
    """
    parts = [x.strip() for x in value.split(",") if x.strip()]
    nums = [x for x in parts if re.fullmatch(r"\d+(\.\d+)?", x)]
    codec = next((x for x in parts if re.fullmatch(r"[a-z][a-z0-9_]*", x)), "")
    if not nums:
        text = " ".join(parts)
        text = re.sub(r"\[[^\]]*@ 0x[0-9a-f]+\]\s*", "", text)      # ffprobe's "[mov,mp4,... @ 0x...]" prefix
        text = re.sub(r"\s/\S+", "", text)                           # the file's own path
        return "ERR " + text.strip()[:200]
    frames = nums[0] if deep and len(nums) > 1 else ""
    return f"{nums[-1]},{codec}" + (f",{frames}" if deep else "")


def probe_batch(container: str, paths: list[str], deep: bool) -> dict[str, str]:
    """ffprobe each file inside a container: 'duration,codec[,frames]' or 'ERR message'."""
    out: dict[str, str] = {}
    entries = "stream=codec_name" + (",nb_read_frames" if deep else "") + ":format=duration"
    extra = "-count_frames " if deep else ""
    script = ('p="$(command -v ffprobe || ls /usr/lib/ffmpeg/*/bin/ffprobe | sort -V | tail -1)"; '
              f'for f; do r=$("$p" -v error {extra}-select_streams v:0 -show_entries {entries} -of csv=p=0 "$f" 2>&1 | tr "\\n" ","); '
              'printf "%s|%s\\n" "$f" "$r"; done')
    for i in range(0, len(paths), 400):
        batch = paths[i:i + 400]
        res = subprocess.run(["docker", "exec", container, "sh", "-c", script, "sh", *batch],
                             capture_output=True, text=True, timeout=3600)
        for line in res.stdout.splitlines():
            name, _, value = line.partition("|")
            out[name] = parse_probe(value, deep)
        print(f"  probed {min(i + 400, len(paths))} of {len(paths)} in {container}", flush=True)
    return out


def scan_main(cam: str, after: float, before: float, deep: bool) -> tuple[list, list]:
    segs = continuity.segments(cam, after, before)
    files = []
    for s in segs:
        rel = main_rel(cam, s["start_time"])
        host = MAIN_HOST / rel
        size = host.stat().st_size if host.exists() else -1
        files.append((s, rel, size))
    probes = probe_batch("frigate", [f"{MAIN_CTR}/{rel}" for _, rel, size in files if size > 0], deep)
    findings = []
    for s, rel, size in files:
        p = probes.get(f"{MAIN_CTR}/{rel}")
        frames = int(p.split(",")[2]) if deep and p and not p.startswith("ERR") and len(p.split(",")) > 2 and p.split(",")[2].isdigit() else None
        layout = testlib.mp4_cut_short(MAIN_HOST / rel) if size > 0 else None
        cause = classify(size >= 0, max(size, 0), p, s.get("duration") or (s["end_time"] - s["start_time"]), frames, layout)
        findings.append({"camera": cam, "stream": "main", "start": s["start_time"], "end": s["end_time"], "file": rel,
                         "cause": cause})
    gaps = testlib.gaps_from([(s["start_time"], s["end_time"]) for s in segs], testlib.MAIN_MIN_GAP)
    return findings, gaps


def scan_substream(cam: str, after: float, before: float, deep: bool) -> tuple[list, list]:
    root = testlib.substream_root()
    segs = testlib.substream_segments(cam, after, before, root)
    files = []
    for s, e in segs:
        rel = f"{cam}/{time.strftime('%Y-%m-%d', time.gmtime(s))}/{segment_name(cam, s, e)}"
        host = root / rel
        files.append((s, e, rel, host.stat().st_size if host.exists() else -1))
    probes = probe_batch("substream", [f"{SUB_CTR}/{rel}" for *_, rel, size in files if size > 0], deep)
    findings = []
    for s, e, rel, size in files:
        p = probes.get(f"{SUB_CTR}/{rel}")
        frames = int(p.split(",")[2]) if deep and p and not p.startswith("ERR") and len(p.split(",")) > 2 and p.split(",")[2].isdigit() else None
        layout = testlib.mp4_cut_short(root / rel) if size > 0 else None
        findings.append({"camera": cam, "stream": "substream", "start": s, "end": e, "file": rel,
                         "cause": classify(size >= 0, max(size, 0), p, e - s, frames, layout)})
    return findings, testlib.gaps_from(segs, testlib.SUB_MIN_GAP)


def clock_check(r: testlib.Report, cams: list[str]) -> list[str]:
    chrony = health.parse_chrony(subprocess.run(["chronyc", "-c", "tracking"], capture_output=True, text=True).stdout)
    off = chrony.get("offset_s")
    r.check(f"gateway clock synchronized to UTC within {CLOCK_TOLERANCE:g} s (chrony)",
            chrony["synced"] and off is not None and abs(off) <= CLOCK_TOLERANCE,
            f"{chrony.get('leap')}, offset {off:+.6f} s, source {chrony.get('source')}" if off is not None else str(chrony))
    # A segment is written when it ends: its file time minus its end time shows the timestamps follow the clock.
    lines, worst = [], 0.0
    now = time.time()
    for cam in cams:
        segs = continuity.segments(cam, now - 900, now)[-3:]
        for s in segs:
            p = MAIN_HOST / main_rel(cam, s["start_time"])
            if p.exists():
                d = p.stat().st_mtime - s["end_time"]
                worst = max(worst, abs(d) if d < 0 else 0)
                lines.append(f"{cam} main {vivo.utc(s['end_time'])[11:]}: written {d:+.1f} s after its end")
        for st, en in testlib.substream_segments(cam, now - 600, now)[-2:]:
            f = testlib.substream_root() / cam / time.strftime("%Y-%m-%d", time.gmtime(st)) / segment_name(cam, st, en)
            if f.exists():
                d = f.stat().st_mtime - en
                worst = max(worst, abs(d) if d < -CLOCK_TOLERANCE else 0)
                lines.append(f"{cam} substream {vivo.utc(en)[11:]}: written {d:+.1f} s after its end")
    r.check("no segment is written before its own end time (timestamps follow the clock)",
            worst <= CLOCK_TOLERANCE, f"worst {worst:.1f} s early" if worst else "")
    return lines


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--hours", type=float, default=24)
    ap.add_argument("--start", help="start of the period, UTC \"YYYY-MM-DD HH:MM\" (default: --hours before now)")
    ap.add_argument("--streams", default="main,substream", help="main, substream or both (default)")
    ap.add_argument("--camera", action="append")
    ap.add_argument("--deep", action="store_true")
    ap.add_argument("--spot", type=int, default=10)
    ap.add_argument("--planned", action="append", default=[])
    ap.add_argument("--seed", type=int)
    a = ap.parse_args()
    if a.start:
        after = float(calendar.timegm(time.strptime(a.start, "%Y-%m-%d %H:%M")))
        before = min(after + a.hours * 3600, time.time() - 180)
    else:
        before = time.time() - 180
        after = before - a.hours * 3600
    streams = [x.strip() for x in a.streams.split(",") if x.strip()]
    if not streams or set(streams) - {"main", "substream"}:
        print("--streams takes main, substream or both", file=sys.stderr)
        return 2
    cams = a.camera or vivo.services_config()["cameras"]
    planned = [testlib.parse_window(w) for w in a.planned]
    r = testlib.Report()
    findings, gap_rows = [], []
    for cam in cams:
        print(f"{cam}: scanning", flush=True)
        for scan in [s for name, s in (("main", scan_main), ("substream", scan_substream)) if name in streams]:
            f, gaps = scan(cam, after, before, a.deep)
            findings += f
            stream = f[0]["stream"] if f else ("main" if scan is scan_main else "substream")
            gap_rows += [{"camera": cam, "stream": stream, "start": g0, "end": g1, "length": g1 - g0,
                          "cause": "planned window" if any(p0 - 60 <= g0 and g1 <= p1 + 60 for p0, p1 in planned) else "missing"}
                         for g0, g1 in gaps]
    broken = [x for x in findings if x["cause"]]
    for cam in cams:
        for stream in streams:
            n = sum(1 for x in findings if x["camera"] == cam and x["stream"] == stream)
            b = sum(1 for x in broken if x["camera"] == cam and x["stream"] == stream)
            missing = sum(g["length"] for g in gap_rows if g["camera"] == cam and g["stream"] == stream and g["cause"] == "missing")
            r.check(f"{cam} {stream}: every segment plays or is listed with its cause", n > 0,
                    f"{n} segments, {b} broken, {missing:.0f} s missing in {sum(1 for g in gap_rows if g['camera'] == cam and g['stream'] == stream and g['cause'] == 'missing')} intervals")
    clock_lines = clock_check(r, cams)

    rng = random.Random(a.seed)
    spot = rng.sample(findings, min(a.spot, len(findings))) + broken[:5]
    vivo.REPORTS.mkdir(exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    csv_path = vivo.REPORTS / f"m12-integrity-{stamp}.csv"
    with csv_path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["camera", "stream", "start", "end", "file", "cause"])
        w.writeheader()
        w.writerows({**x, "start": vivo.utc(x["start"]), "end": vivo.utc(x["end"]), "cause": x["cause"] or "plays"} for x in findings)

    md = ["# M12 recording integrity and timeline", "",
          f"Period {vivo.utc(after)} to {vivo.utc(before)} ({(before - after) / 3600:g} h), {len(cams)} cameras, "
          f"{' and '.join(streams)}, "
          f"{'every frame read (--deep)' if a.deep else 'file headers and MP4 boxes read'}. Every segment: {csv_path.name}.", ""] + r.markdown()
    md += ["", "## Segments that will not play", ""]
    md += (["| Camera | Stream | Start (UTC) | File | Cause |", "|---|---|---|---|---|"] +
           [f"| {x['camera']} | {x['stream']} | {vivo.utc(x['start'])} | `{x['file']}` | {x['cause']} |" for x in broken]
           if broken else ["None."])
    md += ["", "## Missing intervals", ""]
    missing = [g for g in gap_rows]
    md += (["| Camera | Stream | From (UTC) | To | Length | |", "|---|---|---|---|---|---|"] +
           [f"| {g['camera']} | {g['stream']} | {vivo.utc(g['start'])} | {vivo.utc(g['end'])[11:]} | {g['length']:.0f} s | {g['cause']} |"
            for g in missing] if missing else ["None."])
    md += ["", "## Clock", ""] + [f"- {l}" for l in clock_lines[:30]]
    md += ["", "## Manual spot-check", "", "Open each segment (Frigate UI at that time, or the file in a player) and tick "
           "whether it plays as stated.", "",
           "| # | Camera | Stream | Start (UTC) | File | Script says | Checked by hand |", "|---|---|---|---|---|---|---|"]
    md += [f"| {i} | {x['camera']} | {x['stream']} | {vivo.utc(x['start'])} | `{x['file']}` | {x['cause'] or 'plays'} | |"
           for i, x in enumerate(spot, 1)]
    print("report:", vivo.write_report("m12-integrity", "\n".join(md) + "\n"))
    return 0 if r.passed else 1


if __name__ == "__main__":
    sys.exit(main())
