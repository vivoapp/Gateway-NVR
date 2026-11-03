#!/usr/bin/env python3
"""M7 acceptance check: main-stream recording plus substream segments for the cloud uploader.

usage:
  sudo tests/m7_check.py                          # last 15 minutes
  sudo tests/m7_check.py --hours 24 --samples 20  # the scope's 24-hour run
  sudo tests/m7_check.py --hours 24 --planned "2026-11-02T10:00Z/2026-11-02T10:05Z"

For every camera, over the window:
- the substream is saved as segments named and placed as in docs/SEGMENT_FORMAT.md, each
  name's length matches the MP4's own duration, and each segment ends where the next starts
  (gaps are listed; a gap inside a --planned window is explained)
- at most one .part file per camera (the segment being written)
- the main stream is recorded by Frigate over the same window (tools/continuity.py)
- random minutes: Frigate's API returns that minute of the main stream as a playable clip,
  and the substream segments for the same minute exist with the correct names
"""
import argparse
import datetime as dt
import random
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import continuity  # noqa: E402
import vivo  # noqa: E402
from substream_archiver import PART_RE, SEGMENT_RE, parse_stamp  # noqa: E402

rows: list[tuple[bool, str, str]] = []
IN_CONTAINER = Path("/recordings/substream")   # where the substream container sees the folder


def check(name: str, ok: bool, detail: str = "") -> bool:
    rows.append((ok, name, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}  {detail}")
    return ok


def parse_window(s: str) -> tuple[float, float]:
    a, b = s.split("/")
    f = lambda x: dt.datetime.fromisoformat(x.replace("Z", "+00:00")).timestamp()  # noqa: E731
    return f(a), f(b)


def durations(paths: list[Path], root: Path) -> dict[Path, float]:
    """MP4 duration of each segment, read with ffprobe inside the substream container."""
    out: dict[Path, float] = {}
    script = 'for f; do printf "%s|" "$f"; ffprobe -v error -show_entries format=duration -of csv=p=0 "$f" || echo; done'
    for i in range(0, len(paths), 300):
        batch = paths[i:i + 300]
        inside = [str(IN_CONTAINER / p.relative_to(root)) for p in batch]
        res = subprocess.run(["docker", "exec", "substream", "sh", "-c", script, "sh", *inside],
                             capture_output=True, text=True, timeout=600)
        by_inside = dict(zip(inside, batch))
        for line in res.stdout.splitlines():
            name, _, value = line.partition("|")
            try:
                out[by_inside[name]] = float(value)
            except (KeyError, ValueError):
                pass
    return out


def substream(root: Path, cam: str, after: float, before: float) -> dict:
    segs, bad_names, parts = [], [], []
    cam_dir = root / cam
    for day in sorted(cam_dir.iterdir()) if cam_dir.is_dir() else []:
        for f in sorted(day.iterdir()) if day.is_dir() else []:
            if PART_RE.match(f.name):
                parts.append(f)
                continue
            m = SEGMENT_RE.match(f.name)
            if not m or m["cam"] != cam:
                bad_names.append(f)
                continue
            start, end = parse_stamp(m["start"]), parse_stamp(m["end"])
            if end < after or start > before:
                continue
            if day.name != time.strftime("%Y-%m-%d", time.gmtime(start)):
                bad_names.append(f)
            segs.append((start, end, f))
    segs.sort()
    gaps = [(a[1], b[0]) for a, b in zip(segs, segs[1:]) if b[0] > a[1]]
    overlaps = [(b[0], a[1]) for a, b in zip(segs, segs[1:]) if b[0] < a[1]]
    covered = sum(max(0, min(e, before) - max(s, after)) for s, e, _ in segs)
    return {"segs": segs, "bad_names": bad_names, "parts": parts, "gaps": gaps, "overlaps": overlaps,
            "covered": covered}


def clip_plays(cam: str, t: int) -> tuple[bool, str]:
    """Main stream for one minute from Frigate's own API, probed inside the Frigate container.

    Frigate streams clip.mp4 as fragmented MP4, whose header only knows the first fragment's
    length, so the length is read from the video frames themselves.
    """
    url = f"http://127.0.0.1:5000/api/{cam}/start/{t}/end/{t + 60}/clip.mp4"
    try:
        out = vivo.ffprobe(["-v", "error", "-select_streams", "v:0", "-show_entries",
                            "stream=codec_name:packet=pts_time", "-of", "csv=p=0", url], timeout=180)
    except Exception as e:
        return False, str(e)[:120]
    times, codec = [], "?"
    for line in out.split():
        value = line.strip(",")
        try:
            times.append(float(value))
        except ValueError:
            codec = value or codec
    length = max(times) - min(times) if len(times) > 1 else 0.0
    return length >= 50, f"{length:.1f} s of {codec} video ({len(times)} frames)"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--hours", type=float, default=0.25)
    ap.add_argument("--samples", type=int, default=5, help="random minutes to check on both streams")
    ap.add_argument("--max-gap", type=float, default=10, help="longest unexplained gap allowed, seconds")
    ap.add_argument("--planned", action="append", default=[], help="UTC window of a deliberate test, ISO a/b")
    ap.add_argument("--seed", type=int, help="random seed (to repeat the same minutes)")
    args = ap.parse_args()

    root = Path(vivo.SITE.get("MEDIA_ROOT", "/srv/vivo")) / "recordings" / "substream"
    before = time.time() - 120          # the newest minute may still be in a .part file
    after = before - args.hours * 3600
    planned = [parse_window(w) for w in args.planned]
    is_planned = lambda a, b: any(p0 - 60 <= a and b <= p1 + 60 for p0, p1 in planned)  # noqa: E731
    cams = vivo.services_config()["cameras"]
    span = before - after

    table = ["| Camera | Substream segments | Substream recorded | Gaps (longest) | Name/length problems | "
             "Main stream recorded | Main gaps (longest) |", "|---|---|---|---|---|---|---|"]
    gap_rows = []
    all_segs = []
    for cam in cams:
        s = substream(root, cam, after, before)
        all_segs += [(cam, *x) for x in s["segs"]]
        lengths = durations([f for _, _, f in s["segs"]], root)
        mismatched = [f for st, en, f in s["segs"] if f not in lengths or abs((en - st) - lengths[f]) > 1.5]
        unexplained = [(a, b) for a, b in s["gaps"] if b - a > args.max_gap and not is_planned(a, b)]
        main = continuity.gaps(cam, after, before)
        main_unexplained = [(a, b) for a, b in main["gaps"] if b - a > args.max_gap and not is_planned(a, b)]
        # Frigate's first and last segments can reach past the window, so its share is capped at 100%.
        sub_pct, main_pct = 100 * s["covered"] / span, min(100.0, 100 * main["covered"] / span)
        longest = max((b - a for a, b in s["gaps"]), default=0)
        main_longest = max((b - a for a, b in main["gaps"]), default=0)
        problems = len(s["bad_names"]) + len(mismatched) + len(s["overlaps"])
        table.append(f"| {cam} | {len(s['segs'])} | {sub_pct:.2f}% | {len(s['gaps'])} ({longest:.0f} s) | {problems} | "
                     f"{main_pct:.2f}% | {len(main['gaps'])} ({main_longest:.0f} s) |")
        check(f"{cam}: substream segments over the window", sub_pct >= 98 and not unexplained,
              f"{len(s['segs'])} segments, {sub_pct:.2f}% recorded, {len(unexplained)} unexplained gaps over {args.max_gap:g} s")
        check(f"{cam}: names, folders and lengths as in SEGMENT_FORMAT.md", problems == 0,
              f"{len(s['bad_names'])} bad names, {len(mismatched)} length mismatches, {len(s['overlaps'])} overlaps"
              + (f" (first: {(s['bad_names'] + mismatched)[0].name})" if s["bad_names"] or mismatched else ""))
        check(f"{cam}: at most one .part file", len(s["parts"]) <= 1, f"{len(s['parts'])} .part")
        check(f"{cam}: main stream recorded by Frigate", main_pct >= 98 and not main_unexplained,
              f"{main['segments']} segments, {main_pct:.2f}% recorded, {len(main_unexplained)} unexplained gaps")
        for a, b in s["gaps"]:
            gap_rows.append(f"| {cam} | substream | {vivo.utc(a)} | {b - a:.0f} s | {'yes' if is_planned(a, b) else 'no'} |")
        for a, b in main["gaps"]:
            gap_rows.append(f"| {cam} | main | {vivo.utc(a)} | {b - a:.1f} s | {'yes' if is_planned(a, b) else 'no'} |")

    rng = random.Random(args.seed)
    sample_rows = ["| Camera | Minute (UTC) | Main stream from Frigate's API | Substream segments for the minute |",
                   "|---|---|---|---|"]
    passed = 0
    first, last = -(-int(after) // 60) * 60, (int(before) - 60) // 60 * 60   # whole minutes inside the window
    for _ in range(args.samples):
        cam = rng.choice(cams)
        t = rng.randrange(first, last + 1, 60) if last >= first else first
        main_ok, main_detail = clip_plays(cam, t)
        cover = sorted((st, en, f) for c, st, en, f in all_segs if c == cam and en > t and st < t + 60)
        joined = all(b[0] <= a[1] for a, b in zip(cover, cover[1:]))
        sub_ok = bool(cover) and cover[0][0] <= t + 2 and cover[-1][1] >= t + 58 and joined
        passed += main_ok and sub_ok
        sample_rows.append(f"| {cam} | {vivo.utc(t)[11:16]} | {'ok' if main_ok else '**FAIL**'} {main_detail} | "
                           f"{'ok' if sub_ok else '**FAIL**'} {'<br>'.join(f.name for _, _, f in cover) or 'none'} |")
    check("random minutes play from the main stream and have their substream segments",
          passed == args.samples, f"{passed} of {args.samples}")

    npass = sum(ok for ok, _, _ in rows)
    md = [f"# M7 recording check: main stream and substream segments", "",
          f"Window: {vivo.utc(after)} to {vivo.utc(before)} ({args.hours:g} h), {len(cams)} cameras. "
          f"Substream folder: `{root}`.", "",
          f"Result: **{npass} passed, {len(rows) - npass} failed**", "",
          "| Result | Check | Detail |", "|---|---|---|"]
    md += [f"| {'PASS' if ok else '**FAIL**'} | {n} | {d} |" for ok, n, d in rows]
    md += ["", "## Per camera", ""] + table
    md += ["", "## Random minutes", ""] + sample_rows
    if gap_rows:
        md += ["", "## Gaps", "", "| Camera | Stream | Start | Length | Planned test? |", "|---|---|---|---|---|"] + gap_rows
    print("report:", vivo.write_report("m7-check", "\n".join(md) + "\n"))
    return 0 if npass == len(rows) else 1


if __name__ == "__main__":
    sys.exit(main())
