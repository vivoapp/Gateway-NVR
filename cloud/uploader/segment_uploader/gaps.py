"""M20 gap report: which recorded segments were never uploaded and are now gone from the disk.

The rule is the one agreed for M20, and it is deliberately narrow:

    a segment is a GAP only if it was never confirmed uploaded AND its local file no longer exists.

* A segment that is uploaded is never a gap, even if the recorder (M9) has since removed the file.
* A segment that is still waiting and still on the disk is not a gap; it is just waiting.
  It is reported as "waiting" so that nobody mistakes a long queue for a loss.

Only segments the uploader has seen can be reported. A file that was written and removed between two
scans of the uploader (the uploader was stopped for longer than the recorder keeps files) leaves no
trace here. The retention recommendation (tools/m20_retention.py) exists to prevent exactly that.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .segment import parse_segment_name

REASONS = {
    "missing": "the file was removed before the uploader could upload it",
    "pending": "the file was removed while it was waiting to be uploaded",
    "uploading": "the file was removed while it was being uploaded",
    "conflict": "a different object already exists under this key, and the local file is gone",
}


def iso(ts: int | None) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if ts else "?"


def unfinished_rows(db_path: str | Path) -> list[sqlite3.Row]:
    """Every segment that is not confirmed uploaded, read without changing the state file."""
    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10)
    db.row_factory = sqlite3.Row
    try:
        return db.execute("SELECT * FROM segments WHERE state!='uploaded' ORDER BY camera_id, start_utc, key").fetchall()
    finally:
        db.close()


def find_gaps(cfg, rows) -> dict:
    """Split the not-uploaded segments into gaps (file gone) and waiting (file still there)."""
    gaps, waiting = [], []
    for r in rows:
        if r["state"] == "uploaded":
            continue
        if (cfg.watch_dir / r["rel_path"]).exists():
            waiting.append(r)
            continue
        start, end, camera = r["start_utc"], r["end_utc"], r["camera_id"]
        if start is None or camera is None:                 # a state file from the first version
            info = parse_segment_name(Path(r["rel_path"]).name, cfg.nominal_duration)
            if info:
                start, end, camera = info.start_utc, info.end_utc, info.camera_id
        gaps.append({"key": r["key"], "camera_id": camera or "?", "start_utc": start, "end_utc": end,
                     "start": iso(start), "end": iso(end), "state": r["state"],
                     "reason": REASONS.get(r["state"], "the file is gone"), "last_error": r["last_error"]})
    gaps.sort(key=lambda g: (g["camera_id"], g["start_utc"] or 0, g["key"]))

    ranges: list[dict] = []                                  # neighbouring segments of one camera, merged
    for g in gaps:
        last = ranges[-1] if ranges else None
        if (last and last["camera_id"] == g["camera_id"] and g["start_utc"] is not None
                and last["end_utc"] is not None and g["start_utc"] <= last["end_utc"] + 1):
            last["end_utc"] = max(last["end_utc"], g["end_utc"] or g["start_utc"])
            last["segments"] += 1
        else:
            ranges.append({"camera_id": g["camera_id"], "start_utc": g["start_utc"],
                           "end_utc": g["end_utc"] or g["start_utc"], "segments": 1})
    for rg in ranges:
        rg["start"], rg["end"] = iso(rg["start_utc"]), iso(rg["end_utc"])
        rg["seconds"] = (rg["end_utc"] or 0) - (rg["start_utc"] or 0)
    starts = [w["start_utc"] for w in waiting if w["start_utc"]]
    return {"gaps": gaps, "ranges": ranges, "gap_segments": len(gaps),
            "gap_seconds": sum(rg["seconds"] for rg in ranges),
            "waiting": len(waiting), "oldest_waiting": iso(min(starts)) if starts else None}


def format_text(result: dict) -> str:
    out = []
    if result["gap_segments"]:
        out.append(f"GAPS: {result['gap_segments']} segment(s) were never uploaded and are no longer on the disk "
                   f"({result['gap_seconds'] / 60:.0f} min of video in total).")
        for rg in result["ranges"]:
            out.append(f"  {rg['camera_id']}  {rg['start']} to {rg['end']}  ({rg['segments']} segment(s))")
        reasons = {}
        for g in result["gaps"]:
            reasons.setdefault(g["reason"], 0)
            reasons[g["reason"]] += 1
        for reason, n in reasons.items():
            out.append(f"  {n}: {reason}")
        out.append("  Files in the list:")
        for g in result["gaps"][:50]:
            out.append(f"    {g['key']}  [{g['state']}]")
        if len(result["gaps"]) > 50:
            out.append(f"    ... and {len(result['gaps']) - 50} more (use --json for the full list)")
    else:
        out.append("No gaps: every segment the uploader has seen is uploaded or still on the disk.")
    if result["waiting"]:
        out.append(f"{result['waiting']} segment(s) are still waiting to be uploaded (their files are on the disk; "
                   f"oldest {result['oldest_waiting']}). That is a queue, not a gap.")
    return "\n".join(out)
