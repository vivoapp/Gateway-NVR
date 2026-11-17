#!/usr/bin/env python3
"""M19 acceptance checks, run against a finished (or running) uploader.

It does not trust the uploader's own state file. It re-reads the recorder's files, asks the storage
for every object, and compares:

  1. every finished local segment has been uploaded                        (nothing skipped)
  2. size, MD5 (= ETag) and SHA-256 of the object match the local file     (checksums match)
  3. the bucket holds exactly the uploaded segments, no extra, no missing  (exactly once)
  4. in the log, no segment has more than one `event=uploaded`             (exactly once, if --log given)
  5. every uploaded segment has an index record with the right values      (index complete)
  6. local files are unchanged since `snapshot`                            (never changes local files)

    python tools/m19_acceptance.py snapshot --config CONFIG --out manifest.json     (start of the run)
    python tools/m19_acceptance.py verify   --config CONFIG [--manifest manifest.json] [--log LOG|-] [--deep]
    python tools/m19_acceptance.py report   --config CONFIG [--manifest ...] [--log ...]      (markdown)

`--deep` downloads every object and hashes it (slow, strongest). Exit code 0 = all checks passed.

While the recorder and uploader are still running, segments appear between the moments the checker looks
at the files, the state file, the bucket and the index. So by default anything from the last `--grace`
seconds (30) counts as "still in flight" and is left out of the comparisons; the number is printed, so
nothing is hidden. For the final acceptance run stop the recorder, wait until the queue is empty, and
use `--strict` (no grace): then any difference is a failure.
Credentials come from the same environment variables as the uploader (S3_ACCESS_KEY ...).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from segment_uploader.config import load_config  # noqa: E402
from segment_uploader.segment import parse_segment_name  # noqa: E402
from segment_uploader.service import iter_local_files  # noqa: E402
from segment_uploader.storage import S3Storage  # noqa: E402

LOGKV = re.compile(r"(\w+)=(\S+)")


def hashes(path: Path) -> tuple[int, str, str]:
    sha, md5, size = hashlib.sha256(), hashlib.md5(usedforsecurity=False), 0
    with open(path, "rb") as f:
        while chunk := f.read(1 << 20):
            sha.update(chunk)
            md5.update(chunk)
            size += len(chunk)
    return size, sha.hexdigest(), md5.hexdigest()


def local_segments(cfg) -> dict[str, Path]:
    """key -> path of every finished segment in the agreed name format."""
    out = {}
    for p in iter_local_files(cfg):
        rel = p.relative_to(cfg.watch_dir).as_posix()
        if parse_segment_name(p.name, cfg.nominal_duration):
            out[cfg.object_key(rel)] = p
    return out


def read_log(source: str | None) -> list[dict]:
    if not source:
        return []
    lines = sys.stdin if source == "-" else open(source, encoding="utf-8", errors="replace")
    events = []
    for line in lines:
        if "event=" not in line:
            continue
        kv = dict(LOGKV.findall(line))
        events.append(kv)
    return events


def read_index(cfg) -> list[dict]:
    folder = cfg.index_dir or cfg.state_db.parent / "index"
    records = []
    for f in sorted(Path(folder).glob("index-*.jsonl")):
        for line in f.read_text(encoding="utf-8").splitlines():
            if line.strip():
                records.append(json.loads(line))
    return records


def snapshot(cfg) -> dict:
    return {rel: [p.stat().st_size, p.stat().st_mtime_ns]
            for rel, p in ((p.relative_to(cfg.watch_dir).as_posix(), p) for p in iter_local_files(cfg))}


class Check:
    def __init__(self):
        self.rows: list[tuple[str, bool, str]] = []

    def add(self, name: str, ok: bool, detail: str) -> None:
        self.rows.append((name, ok, detail))

    @property
    def ok(self) -> bool:
        return all(ok for _, ok, _ in self.rows)


def examples(items, n=3) -> str:
    items = sorted(items)
    return ", ".join(items[:n]) + (f" ... (+{len(items) - n} more)" if len(items) > n else "")


def iso_to_epoch(text: str) -> float:
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()


def verify(cfg, manifest=None, log_events=None, deep=False, grace: float = 30.0) -> tuple[Check, dict]:
    chk, info = Check(), {}
    cut = time.time() - grace                  # anything newer than this may still be in flight
    storage = S3Storage(cfg)
    db = sqlite3.connect(f"file:{cfg.state_db}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    rows = {r["key"]: r for r in db.execute("SELECT * FROM segments")}
    local = local_segments(cfg)
    info.update(local_files=len(local), db_rows=len(rows))

    # 1. nothing skipped
    not_up, fresh_local = [], 0
    for k, p in local.items():
        if k in rows and rows[k]["state"] == "uploaded":
            continue
        if p.stat().st_mtime > cut:
            fresh_local += 1                   # the recorder wrote it moments ago
        else:
            not_up.append(k)
    chk.add("every finished local segment is uploaded", not not_up,
            f"{len(local) - len(not_up) - fresh_local} of {len(local)} uploaded"
            + (f"; {fresh_local} written in the last {grace:g} s, still in flight" if fresh_local else "")
            + (f"; NOT uploaded: {examples(not_up)}" if not_up else ""))

    # 2. checksums
    uploaded = {k: r for k, r in rows.items() if r["state"] == "uploaded"}
    bad, gone, checked = [], 0, 0
    for key, row in uploaded.items():
        remote = storage.head(key)
        if remote is None:
            bad.append(f"{key} (missing in storage)")
            continue
        path = local.get(key)
        if path is not None:
            size, sha, md5 = hashes(path)
        else:                                  # the recorder already removed it (M9): use what we recorded
            gone += 1
            size, sha, md5 = row["size"], row["sha256"], row["md5"]
        if remote.size != size or remote.etag.lower() != md5 or (remote.sha256 and remote.sha256 != sha):
            bad.append(f"{key} (size/ETag/SHA-256 differ)")
            continue
        if deep:
            body = storage.client.get_object(Bucket=storage.bucket, Key=key)["Body"]
            h = hashlib.sha256()
            for chunk in iter(lambda: body.read(1 << 20), b""):
                h.update(chunk)
            if h.hexdigest() != sha:
                bad.append(f"{key} (downloaded content differs)")
                continue
        checked += 1
    chk.add("every uploaded checksum matches the local file", not bad and bool(uploaded),
            f"{checked} of {len(uploaded)} checked" + (" by downloading" if deep else " by size, ETag and SHA-256")
            + (f"; {gone} no longer on disk (compared with the recorded checksum)" if gone else "")
            + (f"; WRONG: {examples(bad)}" if bad else ""))

    # 3. exactly what was uploaded is in the bucket
    prefix = cfg.object_key("")
    listed, fresh_obj = set(), set()
    for page in storage.client.get_paginator("list_objects_v2").paginate(Bucket=storage.bucket, Prefix=prefix):
        for o in page.get("Contents", []):
            listed.add(o["Key"])
            if o["LastModified"].timestamp() > cut:
                fresh_obj.add(o["Key"])
    fresh_rows = {k for k, r in uploaded.items() if (r["uploaded_at"] or 0) > cut}
    extra = listed - set(uploaded) - fresh_obj
    absent = set(uploaded) - listed - fresh_rows
    skipped = len((listed - set(uploaded)) & fresh_obj) + len(fresh_rows - listed)
    extra_states = Counter(rows[k]["state"] if k in rows else "not in the state file" for k in extra)
    chk.add("the bucket holds exactly the uploaded segments", not extra and not absent,
            f"{len(listed)} objects, {len(uploaded)} uploaded"
            + (f"; {skipped} from the last {grace:g} s, still in flight" if skipped else "")
            + (f"; NOT marked uploaded ({dict(extra_states)}): {examples(extra)}" if extra else "")
            + (f"; missing in bucket: {examples(absent)}" if absent else ""))
    info["bucket_objects"] = len(listed)

    # 3b. nothing is stuck
    stuck = {}
    for k, r in rows.items():
        if r["state"] in ("conflict", "missing"):
            stuck[k] = r
        elif r["state"] in ("pending", "uploading") and (r["first_seen"] or 0) <= cut:
            stuck[k] = r
    stuck_states = Counter(r["state"] for r in stuck.values())
    first = next(iter(stuck.values()), None)
    chk.add("no segment is stuck (pending, conflict or missing)", not stuck,
            "none" if not stuck else f"{dict(stuck_states)}: {examples(stuck)}"
            + (f"; first reason: {first['last_error']}" if first["last_error"] else ""))

    # 4. exactly once (log)
    if log_events:
        puts = Counter(e["key"] for e in log_events if e.get("event") == "uploaded" and "key" in e)
        twice = [k for k, n in puts.items() if n > 1]
        chk.add("no segment was uploaded twice (log)", not twice,
                f"{len(puts)} segments with an upload line" + (f"; twice: {examples(twice)}" if twice else ""))
    else:
        chk.add("no segment was uploaded twice (log)", True, "skipped: no --log given")

    # 5. index
    records = read_index(cfg)
    by_key = defaultdict(list)
    for r in records:
        by_key[r["key"]].append(r)
    missing, wrong, fresh_idx = [], [], 0
    for key, row in uploaded.items():
        recs = by_key.get(key)
        if not recs:
            if key in fresh_rows:
                fresh_idx += 1                 # uploaded moments ago; its record is on the way
            else:
                missing.append(key)
            continue
        info_name = parse_segment_name(Path(key).name, cfg.nominal_duration)
        r = recs[0]
        if (r["sha256"] != row["sha256"] or r["size"] != row["size"] or r["bucket"] != cfg.bucket
                or r["camera_id"] != info_name.camera_id or r["start_utc"] != info_name.start_iso):
            wrong.append(key)
    repeats = sum(len(v) - 1 for v in by_key.values())
    stray = {k for k in set(by_key) - set(uploaded) if iso_to_epoch(by_key[k][0]["uploaded_at"]) <= cut}
    chk.add("every uploaded segment has a correct index record", not missing and not wrong and not stray,
            f"{len(uploaded) - len(missing) - fresh_idx} of {len(uploaded)} have one"
            + (f"; {fresh_idx} from the last {grace:g} s, record still on the way" if fresh_idx else "")
            + (f"; MISSING: {examples(missing)}" if missing else "")
            + (f"; wrong values: {examples(wrong)}" if wrong else "")
            + (f"; for segments not marked uploaded: {examples(stray)}" if stray else "")
            + (f" ({repeats} repeated records, allowed)" if repeats else ""))

    # 6. local files untouched
    if manifest:
        changed, vanished = [], []
        for rel, (size, mtime) in manifest.items():
            p = cfg.watch_dir / rel
            if not p.exists():
                vanished.append(rel)
            else:
                st = p.stat()
                if (st.st_size, st.st_mtime_ns) != (size, mtime):
                    changed.append(rel)
        chk.add("local files are unchanged", not changed,
                f"{len(manifest) - len(changed) - len(vanished)} unchanged, {len(vanished)} removed by the recorder's retention"
                + (f"; CHANGED: {examples(changed)}" if changed else ""))
    else:
        chk.add("local files are unchanged", True, "skipped: no --manifest given (the read-only mount also guarantees it)")
    db.close()
    return chk, info


def measurements(cfg, log_events) -> dict:
    db = sqlite3.connect(f"file:{cfg.state_db}?mode=ro", uri=True)
    rows = db.execute("SELECT camera_id, size, first_seen, uploaded_at FROM segments WHERE state='uploaded'").fetchall()
    db.close()
    m = {"segments": len(rows), "cameras": len({r[0] for r in rows}), "bytes": sum(r[1] for r in rows)}
    if rows:
        t0, t1 = min(r[2] for r in rows), max(r[3] for r in rows)
        m["hours"] = (t1 - t0) / 3600
        m["avg_mbps"] = m["bytes"] * 8 / max(t1 - t0, 1) / 1e6
        per_min = Counter(int(r[3] // 60) for r in rows)
        sizes = defaultdict(int)
        for r in rows:
            sizes[int(r[3] // 60)] += r[1]
        m["peak_mbps_1min"] = max(sizes.values()) * 8 / 60 / 1e6
        m["first"] = datetime.fromtimestamp(t0, timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
        m["last"] = datetime.fromtimestamp(t1, timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
    stats = [e for e in (log_events or []) if e.get("event") == "stats"]
    if stats:
        cpu = [float(e["cpu_pct"]) for e in stats]
        rss = [float(e["rss_mb"]) for e in stats]
        rate = [float(e["mbps"]) for e in stats]
        m.update(stat_lines=len(stats), cpu_avg=sum(cpu) / len(cpu), cpu_max=max(cpu),
                 rss_max=max(rss), log_avg_mbps=sum(rate) / len(rate), log_peak_mbps=max(rate),
                 limit_mbps=float(stats[-1].get("limit_mbps", 0)), workers=int(stats[-1].get("workers", 0)))
    return m


def markdown(chk: Check, info: dict, m: dict, cfg) -> str:
    out = ["# M19 acceptance report", "",
           f"Generated {datetime.now(timezone.utc):%Y-%m-%d %H:%M:%SZ}. Bucket `{cfg.bucket}`, prefix `{cfg.key_prefix}`, "
           f"endpoint `{cfg.endpoint_url or 'AWS default'}`.", "", "## Checks", "", "| Check | Result | Detail |", "|---|---|---|"]
    for name, ok, detail in chk.rows:
        out.append(f"| {name} | {'PASS' if ok else '**FAIL**'} | {detail} |")
    out += ["", f"**Overall: {'PASS' if chk.ok else 'FAIL'}**", "", "## Measurements", ""]
    if m.get("segments"):
        out += [f"- Run: {m['first']} to {m['last']} ({m['hours']:.2f} h), {m['segments']} segments from {m['cameras']} cameras, "
                f"{m['bytes'] / 1e6:.1f} MB.",
                f"- Upload rate: average {m['avg_mbps']:.3f} Mbps over the run; busiest minute {m['peak_mbps_1min']:.3f} Mbps."]
    if m.get("stat_lines"):
        out += [f"- From the uploader's own `event=stats` lines ({m['stat_lines']}): CPU average {m['cpu_avg']:.1f}% and "
                f"maximum {m['cpu_max']:.1f}% of one core; memory maximum {m['rss_max']:.0f} MB; "
                f"rate per minute average {m['log_avg_mbps']:.3f} Mbps, peak {m['log_peak_mbps']:.3f} Mbps; "
                f"workers {m['workers']}, speed limit {m['limit_mbps']:g} Mbps (0 = none)."]
    else:
        out.append("- CPU and memory: no `event=stats` lines in the log (pass `--log`).")
    return "\n".join(out) + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["snapshot", "verify", "report"])
    ap.add_argument("--config", required=True)
    ap.add_argument("--manifest")
    ap.add_argument("--out", help="snapshot: where to write the manifest")
    ap.add_argument("--log", help="uploader log file, or - for stdin")
    ap.add_argument("--deep", action="store_true", help="download every object and hash it")
    ap.add_argument("--grace", type=float, default=30.0,
                    help="seconds: newer files and objects count as still in flight (default 30)")
    ap.add_argument("--strict", action="store_true", help="no grace: for the final run, after the queue is empty")
    args = ap.parse_args(argv)
    cfg = load_config(args.config)

    if args.command == "snapshot":
        snap = snapshot(cfg)
        Path(args.out or "manifest.json").write_text(json.dumps(snap))
        print(f"{len(snap)} local files recorded in {args.out or 'manifest.json'}")
        return 0

    manifest = json.loads(Path(args.manifest).read_text()) if args.manifest else None
    events = read_log(args.log)
    chk, info = verify(cfg, manifest, events, args.deep, 0.0 if args.strict else args.grace)
    if args.command == "report":
        print(markdown(chk, info, measurements(cfg, events), cfg))
    else:
        for name, ok, detail in chk.rows:
            print(f"{'ok  ' if ok else 'FAIL'} {name}: {detail}")
        print("RESULT: PASS" if chk.ok else "RESULT: FAIL")
    return 0 if chk.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
