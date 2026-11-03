#!/usr/bin/env python3
"""M8 and M9: keep the recording disk under its safe limit and apply local retention.

Runs in the `storage` container (the pinned go2rtc image, for its Python), once a minute:

1. Substream retention (M9): segments whose start time is older than `substream_days` are
   deleted, and so are `.part` files the archiver left behind more than an hour ago.
2. Old exports (M8): Frigate exports older than `export_days` are deleted through Frigate's API.
3. Safe limit (M8, M9): when the recording disk is more than `disk_limit_percent` used, the
   oldest footage is deleted first, one clock hour at a time, until use is back down to
   `disk_target_percent`. A main-stream hour is deleted through Frigate's API (files and
   database rows together); substream segments are deleted directly. Footage that started
   within the last hour is never deleted. Substream segments deleted this way before their
   `substream_days` ended are listed in `recordings/substream/early-deletions.jsonl`, one line
   per camera and hour, so the cloud uploader knows they are gone (docs/m9-retention.md).

Main-stream retention by days is Frigate's own (record.continuous.days in frigate/config.yml).
The policy comes from services/config.json (generated from the site section of cameras.yaml).

usage:
  storage_guard.py                                  # run forever (the storage container)
  storage_guard.py --once [--dry-run]               # one pass; print what it does
  storage_guard.py --once --limit 40 --target 35    # disk-pressure test with a lower limit
"""
import argparse
import calendar
import json
import os
import re
import signal
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import vivo  # noqa: E402
from substream_archiver import PART_RE, SEGMENT_RE, parse_stamp  # noqa: E402

MEDIA_DIR = Path(os.environ.get("MEDIA_DIR", "/media/vivo"))
# Lab only (tests/lab/README.md): treat the recording folder as a disk of this many bytes, used by the
# files in it. A workstation's Docker reports the workstation's own disk for any folder.
LAB_DISK_BYTES = int(os.environ.get("VIVO_LAB_DISK_BYTES") or 0)
FRIGATE_API = os.environ.get("FRIGATE_API", "http://frigate:5000/api")
INTERVAL = 60
HOUR = 3600
KEEP_NEWEST = HOUR          # never delete footage that started within the last hour
PART_STALE = HOUR           # a .part this old is left over from a crash
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
HOUR_RE = re.compile(r"^\d{2}$")


def log(msg: str) -> None:
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime())} {msg}", flush=True)


class Layout:
    """Folders on the recording disk (docs/m8-storage.md)."""

    def __init__(self, media: Path):
        self.media = media
        self.frigate_media = media / "frigate" / "media"
        self.main = self.frigate_media / "recordings"     # Frigate: YYYY-MM-DD/HH/<camera>/MM.SS.mp4 (UTC)
        self.exports = self.frigate_media / "exports"
        self.state = media / "frigate" / "state"          # Frigate database and caches
        self.substream = media / "recordings" / "substream"
        self.early_deletions = self.substream / "early-deletions.jsonl"


ISO = "%Y-%m-%dT%H:%M:%SZ"


def iso(t: float) -> str:
    return time.strftime(ISO, time.gmtime(t))


def parse_iso(s: str) -> int:
    return calendar.timegm(time.strptime(s, ISO))


def early_records(segments: list[tuple[int, Path]], now: float) -> list[dict]:
    """One early-deletions.jsonl line per camera for substream segments deleted in one pass over an hour."""
    by_cam: dict[str, list[tuple[int, int]]] = {}
    for start, f in segments:
        if m := SEGMENT_RE.match(f.name):
            by_cam.setdefault(m["cam"], []).append((start, parse_stamp(m["end"])))
    return [{"camera": cam, "start": iso(min(s for s, _ in spans)), "end": iso(max(e for _, e in spans)),
             "deleted_at": iso(now), "segments": len(spans)} for cam, spans in sorted(by_cam.items())]


def read_early_deletions(path: Path, since: float = 0) -> list[dict]:
    """early-deletions.jsonl lines deleted at or after `since`, oldest first; lines that do not parse are skipped."""
    try:
        lines = path.read_text().splitlines()
    except (OSError, ValueError):
        return []
    out = []
    for line in lines:
        try:
            rec = json.loads(line)
            deleted = parse_iso(rec["deleted_at"])
            valid = isinstance(rec["camera"], str) and parse_iso(rec["start"]) <= parse_iso(rec["end"])
        except (ValueError, KeyError, TypeError):
            continue
        if valid and deleted >= since:
            out.append(rec)
    return out


def disk_usage(path: Path) -> tuple[int, int, float]:
    """(used bytes, bytes still free for writing, percent used) the way `df` counts them."""
    st = os.statvfs(path)
    used = (st.f_blocks - st.f_bfree) * st.f_frsize
    avail = st.f_bavail * st.f_frsize
    return used, avail, (100.0 * used / (used + avail) if used + avail else 0.0)


def recording_disk_usage(path: Path) -> tuple[int, int, float]:
    """disk_usage() of the recording disk, or of the lab's pretend disk when VIVO_LAB_DISK_BYTES is set."""
    if LAB_DISK_BYTES:
        used = size_of(tree_files(path))
        return used, max(0, LAB_DISK_BYTES - used), 100.0 * used / LAB_DISK_BYTES
    return disk_usage(path)


def entries(path: Path) -> list[Path]:
    """Folder contents; empty when the folder is gone (Frigate and the archiver add and remove folders)."""
    try:
        return list(path.iterdir())
    except OSError:
        return []


def mtime(path: Path) -> float | None:
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def subdirs(path: Path, pattern: re.Pattern | None = None) -> list[Path]:
    return sorted(p for p in entries(path) if p.is_dir() and (pattern is None or pattern.match(p.name)))


def tree_files(path: Path) -> list[Path]:
    return [Path(d) / f for d, _, files in os.walk(path) for f in files]


def size_of(files: list[Path]) -> int:
    total = 0
    for f in files:
        try:
            total += f.stat().st_size
        except OSError:
            pass
    return total


def day_start(name: str) -> int:
    return calendar.timegm(time.strptime(name, "%Y-%m-%d"))


def main_hours(layout: Layout) -> list[int]:
    """Start of every clock hour that still has main-stream recordings, oldest first."""
    hours = []
    for day in subdirs(layout.main, DATE_RE):
        for hour in subdirs(day, HOUR_RE):
            if any(entries(cam) for cam in subdirs(hour)):
                hours.append(day_start(day.name) + int(hour.name) * HOUR)
    return hours


def main_hour_dir(layout: Layout, start: int) -> Path:
    return layout.main / time.strftime("%Y-%m-%d/%H", time.gmtime(start))


def substream_segments(layout: Layout, before: float | None = None) -> list[tuple[int, Path]]:
    """(start, path) of finished substream segments, oldest first; only days before `before` if given."""
    found = []
    for cam in subdirs(layout.substream):
        for day in subdirs(cam, DATE_RE):
            if before is not None and day_start(day.name) >= before:
                continue
            for f in entries(day):
                if m := SEGMENT_RE.match(f.name):
                    found.append((parse_stamp(m["start"]), f))
    return sorted(found)


def remove_empty_dirs(top: Path, keep_from: float | None = None) -> None:
    """Remove empty folders below `top` (deepest first); date folders from `keep_from` on stay."""
    for d, _, _ in sorted(os.walk(top), key=lambda w: -len(w[0])):
        p = Path(d)
        if p == top:
            continue
        if keep_from is not None and DATE_RE.match(p.name) and day_start(p.name) >= keep_from:
            continue
        try:
            p.rmdir()
        except OSError:
            pass


def frigate_api(method: str, path: str, body: dict | None = None, timeout: float = 120.0):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"{FRIGATE_API}{path}", data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
    return json.loads(raw) if raw else {}


class Guard:
    def __init__(self, layout: Layout, policy: dict, dry_run: bool = False):
        self.layout, self.policy, self.dry_run = layout, policy, dry_run

    def delete(self, files: list[Path]) -> None:
        if self.dry_run:
            return
        for f in files:
            f.unlink(missing_ok=True)

    # 1. substream retention
    def substream_retention(self, now: float) -> int:
        cutoff = now - self.policy["substream_days"] * 86400
        old = [f for start, f in substream_segments(self.layout, before=cutoff + 86400) if start < cutoff]
        stale = [p for cam in subdirs(self.layout.substream) for day in subdirs(cam, DATE_RE) for p in entries(day)
                 if PART_RE.match(p.name) and now - (mtime(p) or now) > PART_STALE]
        if old or stale:
            log(f"substream retention ({self.policy['substream_days']} days): deleting {len(old)} segments "
                f"older than {vivo.utc(cutoff)}" + (f" and {len(stale)} leftover .part files" if stale else ""))
            self.delete(old + stale)
        if not self.dry_run:
            today = day_start(time.strftime("%Y-%m-%d", time.gmtime(now)))
            remove_empty_dirs(self.layout.substream, keep_from=today)  # the archiver makes today's and tomorrow's
        return len(old)

    # 2. old exports
    def export_cleanup(self, now: float) -> int:
        cutoff = now - self.policy["export_days"] * 86400
        try:
            exports = frigate_api("GET", "/exports", timeout=10)
        except (OSError, ValueError) as e:
            log(f"exports: Frigate API not available ({e}); trying again next pass")
            return 0
        old = [e["id"] for e in exports if not e.get("in_progress") and export_time(e) < cutoff]
        if old:
            log(f"exports ({self.policy['export_days']} days): deleting {len(old)} exports older than {vivo.utc(cutoff)}")
            if not self.dry_run:
                frigate_api("POST", "/exports/delete", {"ids": old})
        return len(old)

    # 3. safe limit
    def relieve_pressure(self, now: float, limit: float, target: float) -> int:
        used, avail, pct = recording_disk_usage(self.layout.media)
        if pct < limit:
            return 0
        log(f"recording disk {pct:.1f}% used, above the safe limit of {limit:g}%: deleting the oldest footage "
            f"down to {target:g}%")
        freed_total, hours, gone = 0, 0, set()
        while pct > target:
            main = [h for h in main_hours(self.layout) if h not in gone]
            sub = substream_segments(self.layout)
            sub = [(s, f) for s, f in sub if s // HOUR * HOUR not in gone]
            candidates = ([main[0]] if main else []) + ([sub[0][0] // HOUR * HOUR] if sub else [])
            if not candidates:
                log("nothing left to delete")
                break
            hour = min(candidates)
            if hour + HOUR > now - KEEP_NEWEST:
                log(f"still {pct:.1f}% used, but everything left is from the last {KEEP_NEWEST // 60} minutes; "
                    "not deleting it")
                break
            freed = 0
            if main and main[0] == hour:
                freed += self.delete_main_hour(hour)
            sub_hour = [(s, f) for s, f in sub if hour <= s < hour + HOUR]
            sub_files = [f for _, f in sub_hour]
            if sub_files:
                freed += size_of(sub_files)
                self.delete(sub_files)
            gone.add(hour)
            hours += 1
            freed_total += freed
            log(f"deleted the hour from {vivo.utc(hour)}: {freed / 1e9:.2f} GB "
                f"({'main stream' if main and main[0] == hour else ''}{' and ' if main and main[0] == hour and sub_files else ''}"
                f"{'substream' if sub_files else ''})")
            self.note_early(sub_hour, now)
            if self.dry_run:
                used -= freed
                pct = 100.0 * used / (used + avail + freed_total) if used + avail else 0.0
            else:
                used, avail, pct = recording_disk_usage(self.layout.media)
            if hours > 24 * 400:
                break
        log(f"recording disk now {pct:.1f}% used after deleting {hours} hours ({freed_total / 1e9:.2f} GB)")
        return hours

    def note_early(self, segments: list[tuple[int, Path]], now: float) -> None:
        """List substream segments deleted before their retention ended in early-deletions.jsonl."""
        cutoff = now - self.policy["substream_days"] * 86400
        records = early_records([(s, f) for s, f in segments if s >= cutoff], now)
        if not records:
            return
        log(f"substream deleted early for {', '.join(r['camera'] for r in records)}: "
            + ("not listed (dry run)" if self.dry_run else f"listed in {self.layout.early_deletions.name}"))
        if self.dry_run:
            return
        try:   # after the delete on purpose: on a full disk, the deletion frees the space for the line
            with open(self.layout.early_deletions, "a") as out:
                out.write("".join(json.dumps(r) + "\n" for r in records))
        except OSError as e:   # the cleanup must carry on; the storage log above still shows what went
            log(f"could not write {self.layout.early_deletions.name}: {e}")

    def delete_main_hour(self, hour: int) -> int:
        d = main_hour_dir(self.layout, hour)
        files = tree_files(d)
        size = size_of(files)
        if self.dry_run:
            return size
        try:
            # Frigate deletes every recording that overlaps the range, files and database rows.
            frigate_api("DELETE", f"/recordings/start/{hour}/end/{hour + HOUR - 1}?cameras=all")
        except (OSError, ValueError) as e:
            log(f"Frigate API not available ({e}); deleting {d} directly (Frigate drops the missing "
                "files from its database at its next recordings sync)")
        self.delete(tree_files(d))  # what Frigate did not delete (or everything, without the API)
        remove_empty_dirs(d.parent)
        try:
            d.parent.rmdir()        # the date folder, once its last hour is gone
        except OSError:
            pass                    # not empty, or Frigate removed it already
        return size

    def run_once(self, limit: float | None = None, target: float | None = None) -> None:
        now = time.time()
        self.substream_retention(now)
        self.export_cleanup(now)
        self.relieve_pressure(now, limit if limit is not None else self.policy["disk_limit_percent"],
                              target if target is not None else self.policy["disk_target_percent"])


def export_time(export: dict) -> float:
    """Frigate's export date as epoch seconds (a number, or an ISO time in older versions)."""
    d = export.get("date")
    if isinstance(d, (int, float)):
        return float(d)
    try:
        return calendar.timegm(time.strptime(str(d)[:19], "%Y-%m-%dT%H:%M:%S"))
    except ValueError:
        return time.time()  # unknown: keep it


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--once", action="store_true", help="one pass, then exit")
    ap.add_argument("--dry-run", action="store_true", help="only print what would be deleted")
    ap.add_argument("--limit", type=float, help="override disk_limit_percent (tests)")
    ap.add_argument("--target", type=float, help="override disk_target_percent (tests)")
    args = ap.parse_args()

    layout = Layout(MEDIA_DIR)
    if not layout.media.is_dir() or not layout.frigate_media.is_dir():
        print(f"ERROR: {layout.frigate_media} missing (recording disk not mounted?)", file=sys.stderr)
        return 1
    policy = vivo.services_config()
    guard = Guard(layout, policy, dry_run=args.dry_run)
    if args.once or args.dry_run:
        guard.run_once(args.limit, args.target)
        return 0
    # As the container's main process, Python gets no default SIGTERM handling: without this, every
    # `docker stop` waits 10 s and then kills it. A pass cut short is simply finished by the next one.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    log(f"storage guard: {MEDIA_DIR}, safe limit {policy['disk_limit_percent']}% (down to "
        f"{policy['disk_target_percent']}%), substream {policy['substream_days']} days, "
        f"exports {policy['export_days']} days, every {INTERVAL} s")
    while True:
        try:
            guard.run_once()
        except Exception as e:  # keep guarding; one bad pass must not stop the service
            log(f"pass failed: {e!r}")
        time.sleep(INTERVAL)


if __name__ == "__main__":
    sys.exit(main())
