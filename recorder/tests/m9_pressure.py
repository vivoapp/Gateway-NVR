#!/usr/bin/env python3
"""M9 acceptance: disk-pressure test. The oldest footage goes first while recording carries on.

usage:
  sudo tests/m9_pressure.py                        # fill the recording disk to just above its safe limit
  sudo tests/m9_pressure.py --max-filler-gb 20     # the default cap on the filler file (see below)

Steps:
1. Wait until `frigate` and `substream` have run for --warmup seconds, so a restart just before
   the test (the M6 benchmark's, for example) is not counted as a recording gap.
2. Note the recording disk use and which main-stream hours and substream segments exist.
3. Write a filler file on the recording disk until use is just above the safe limit from
   cameras.yaml. If that needs more than --max-filler-gb (a nearly empty 1.2 TB disk would need
   about 1 TB of writes), the filler is capped and the test uses a lower limit just under the
   filled level instead, with a target that frees about the two oldest hours (so the test does
   not delete days of real footage); the report says which limit was used.
4. Run one pass of the storage guard in the `storage` container with that limit.
5. Check: the deleted hours are the oldest ones, in order; Frigate no longer lists them; use is
   at or below the target; substream segments deleted before their retention ended are listed
   in early-deletions.jsonl; main stream and substream kept recording through the test with no
   gap over --max-gap seconds.
6. Delete the filler file (also when a step fails).
"""
import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import continuity  # noqa: E402
import m7_check  # noqa: E402
import storage_guard as sg  # noqa: E402
import vivo  # noqa: E402

rows: list[tuple[bool, str, str]] = []
CHUNK = 64 * 1024 * 1024
RECORDERS = ("frigate", "substream")


def check(name: str, ok: bool, detail: str = "") -> bool:
    rows.append((ok, name, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}  {detail}")
    return ok


def fill(path: Path, size: int) -> None:
    """Allocate `size` bytes: fallocate where the filesystem has it (ext4: instant, no SSD wear)."""
    with open(path, "wb") as f:
        if hasattr(os, "posix_fallocate"):
            try:
                os.posix_fallocate(f.fileno(), 0, size)
                return
            except OSError:
                pass
        zeros = b"\0" * CHUNK
        left = size
        while left > 0:
            f.write(zeros[:min(CHUNK, left)])
            left -= CHUNK
        f.flush()
        os.fsync(f.fileno())


def wait_until_settled(warmup: int) -> bool:
    """Wait until the recorders have run `warmup` seconds; False when one is not running."""
    out = subprocess.run(["docker", "inspect", "-f", "{{.State.Running}} {{.State.StartedAt}}", *RECORDERS],
                         capture_output=True, text=True).stdout.split()
    if out[0::2] != ["true"] * len(RECORDERS):
        return False
    started = max(sg.parse_iso(s[:19] + "Z") for s in out[1::2])
    wait = started + warmup - time.time()
    if wait > 0:
        print(f"the recorders started at {vivo.utc(started)[11:19]}; waiting {wait:.0f} s before the test")
        time.sleep(wait)
    return True


def frigate_rows(cam: str, start: int, end: int) -> int:
    # The cache-busting parameter: Frigate's nginx briefly caches API answers.
    segs = vivo.frigate(f"/{cam}/recordings?after={start}&before={end}&nocache={time.time_ns()}", timeout=30)
    return len(segs) if isinstance(segs, list) else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--max-filler-gb", type=float, default=20)
    ap.add_argument("--max-gap", type=float, default=10)
    ap.add_argument("--settle", type=int, default=150, help="seconds to keep recording after the cleanup")
    ap.add_argument("--warmup", type=int, default=180, help="seconds frigate and substream must have run first")
    args = ap.parse_args()

    if not wait_until_settled(args.warmup):
        print(f"ERROR: {' and '.join(RECORDERS)} must be running", file=sys.stderr)
        return 2

    media = Path(vivo.SITE.get("MEDIA_ROOT", "/srv/vivo"))
    layout = sg.Layout(media)
    policy = vivo.services_config()
    cams = policy["cameras"]
    filler = media / "m9-pressure-filler.bin"
    t0 = time.time()
    used0, avail0, pct0 = sg.recording_disk_usage(media)
    total = used0 + avail0
    hours0 = sg.main_hours(layout)
    sub0 = sg.substream_segments(layout)
    print(f"recording disk {pct0:.1f}% used; {len(hours0)} main-stream hours, {len(sub0)} substream segments")
    if len(hours0) < 2:
        print("ERROR: need at least two hours of main-stream recordings (the newest hour is never deleted)",
              file=sys.stderr)
        return 2

    limit, target = float(policy["disk_limit_percent"]), float(policy["disk_target_percent"])
    need = int(total * (limit + 1) / 100 - used0)
    lowered = need > args.max_filler_gb * 1e9
    size = int(min(max(need, 0), args.max_filler_gb * 1e9))
    if size and lowered:
        size = min(size, int(avail0 * 0.9))
    md_mode = (f"the safe limit from cameras.yaml ({limit:g}%, down to {target:g}%)" if not lowered else "")
    try:
        if size:
            print(f"writing a {size / 1e9:.1f} GB filler file")
            fill(filler, size)
        used1, _, pct1 = sg.recording_disk_usage(media)
        if lowered:
            # Limit just under the filled level, target about two of the oldest hours lower.
            two_hours = sg.size_of([f for h in hours0[:2] for f in sg.tree_files(sg.main_hour_dir(layout, h))])
            limit = round(pct1 - 0.02, 3)
            target = round(min(limit - 0.01, 100 * (used1 - 0.8 * two_hours) / total), 3)
            md_mode = (f"a lower limit for this test ({limit:g}%, down to {target:g}%): reaching the real "
                       f"{policy['disk_limit_percent']}% would need {need / 1e9:.0f} GB of filler, over the "
                       f"{args.max_filler_gb:g} GB cap")
        print(f"disk now {pct1:.1f}% used; running the storage guard with limit {limit:g}% and target {target:g}%")
        t_clean = time.time()
        out = subprocess.run(["docker", "exec", "storage", "python3", "/opt/vivo/tools/storage_guard.py", "--once",
                              "--limit", str(limit), "--target", str(target)],
                             capture_output=True, text=True, timeout=1800)
        guard_log = (out.stdout + out.stderr).strip()
        print(guard_log)
        _, _, pct2 = sg.recording_disk_usage(media)
    finally:
        filler.unlink(missing_ok=True)
    check("filler pushed the disk over the limit", pct1 >= limit, f"{pct0:.3f}% -> {pct1:.3f}% (limit {limit:g}%)")
    check("guard brought use down to the target", pct2 <= target + 0.01, f"{pct2:.3f}% after the pass (target {target:g}%)")

    hours1 = sg.main_hours(layout)
    gone = [h for h in hours0 if h not in hours1]
    check("main stream: the oldest hours were deleted, in order", bool(gone) and gone == hours0[:len(gone)],
          f"deleted {len(gone)} hours: {', '.join(vivo.utc(h)[5:16] for h in gone) or 'none'}")
    newest_kept = max(hours0)
    check("main stream: the newest hour was kept", newest_kept in hours1, vivo.utc(newest_kept)[:16])
    left_rows = sum(frigate_rows(c, h, h + 3599) for h in gone for c in cams)
    check("Frigate no longer lists the deleted hours", left_rows == 0, f"{left_rows} rows left")
    sub1 = {f for _, f in sg.substream_segments(layout)}
    sub_gone = [s for s, f in sub0 if f not in sub1]
    sub_left = [s for s, f in sub0 if f in sub1]
    check("substream: only segments older than the remaining ones were deleted",
          not sub_gone or not sub_left or max(sub_gone) <= min(sub_left) + 3600,
          f"{len(sub_gone)} deleted, oldest left {vivo.utc(min(sub_left)) if sub_left else 'none'}")
    # Segments younger than substream_days were deleted early: each must be in early-deletions.jsonl.
    cut = time.time() - policy["substream_days"] * 86400
    early = sg.read_early_deletions(layout.early_deletions, since=t_clean - 1)
    early_gone = [(s, f) for s, f in sub0 if f not in sub1 and s >= cut]
    unlisted = [f.name for s, f in early_gone
                if not any(r["camera"] == sg.SEGMENT_RE.match(f.name)["cam"]
                           and sg.parse_iso(r["start"]) <= s <= sg.parse_iso(r["end"]) for r in early)]
    if early_gone:
        detail = f"{len(early_gone)} segments deleted early, {len(early)} lines written"
        detail += f"; not listed: {', '.join(unlisted[:3])}" if unlisted else ""
    else:
        detail = "no substream deleted early (the deleted hours had no substream)"
    check("substream: segments deleted before their retention ended are listed in early-deletions.jsonl",
          not unlisted, detail)

    print(f"recording for {args.settle} s more to check there was no gap")
    time.sleep(args.settle)
    before = time.time() - 120
    after = t_clean - 60
    root = layout.substream
    worst_main, worst_sub = 0.0, 0.0
    for cam in cams:
        g = continuity.gaps(cam, after, before)
        s = m7_check.substream(root, cam, after, before)
        worst_main = max([worst_main] + [b - a for a, b in g["gaps"]])
        worst_sub = max([worst_sub] + [b - a for a, b in s["gaps"]])
        if not g["segments"] or not s["segs"]:
            check(f"{cam}: recording carried on", False, f"{g['segments']} main segments, {len(s['segs'])} substream")
    check("recording carried on through the cleanup with no gap", worst_main <= args.max_gap and worst_sub <= args.max_gap,
          f"longest gap {worst_main:.1f} s main, {worst_sub:.0f} s substream ({vivo.utc(after)[11:19]} to {vivo.utc(before)[11:19]})")

    npass = sum(ok for ok, _, _ in rows)
    md = ["# M9 disk-pressure test", "",
          f"Recording disk `{media}`, {total / 1e9:,.1f} GB. Started {vivo.utc(t0)}. The test used {md_mode}.", "",
          f"Result: **{npass} passed, {len(rows) - npass} failed**", "",
          "| Result | Check | Detail |", "|---|---|---|"]
    md += [f"| {'PASS' if ok else '**FAIL**'} | {n} | {d} |" for ok, n, d in rows]
    md += ["", "## Storage guard output", "", "```", vivo.redact(guard_log), "```"]
    print("report:", vivo.write_report("m9-pressure", "\n".join(md) + "\n"))
    return 0 if npass == len(rows) else 1


if __name__ == "__main__":
    sys.exit(main())
