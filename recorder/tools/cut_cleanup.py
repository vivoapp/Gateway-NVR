#!/usr/bin/env python3
"""M11: after a power cut, take the recordings it left broken out of Frigate's index.

Segments Frigate saved in the seconds before a power cut can come back empty (bin/vivo-clean-empty
removes those before the recorder starts) or damaged, while Frigate's index still lists them. A
clip across such a segment then stops there, and the HLS playlist lists a part that answers 404
(docs/milestone-3-results.md, M11). Run by vivo-cut-cleanup.service once per boot, after the
recorder has started: it checks the main-stream segments Frigate indexed in the 10 minutes before
this boot, and deletes through Frigate's API each one whose file is missing, empty or not a whole
MP4 (tools/mp4box.py).

Frigate deletes every recording that touches the range it is given, ends included, so each delete
uses the segment's own times 1 s in from both ends, and only after Frigate lists for that range no
recording other than broken ones (segments can overlap by a second or more). The substream's damaged files are left alone (the cloud uploader's
early-deletions list covers only what the storage guard deletes); M12 lists them.

usage:
  cut_cleanup.py                                   # as run by vivo-cut-cleanup.service
  cut_cleanup.py --dry-run                         # print what it would delete
  cut_cleanup.py --before EPOCH --minutes 45       # another window (default: 10 min before this boot)
"""
import argparse
import json
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mp4box  # noqa: E402
import vivo  # noqa: E402

MEDIA = Path(vivo.SITE.get("MEDIA_ROOT", "/srv/vivo"))
MAIN_HOST = MEDIA / "frigate" / "media" / "recordings"
DONE = Path("/run/vivo/cut-cleanup-done")      # once per boot (/run is emptied at boot)
INSET = 1.0                                      # seconds in from each end of a segment


def log(msg: str) -> None:
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime())} {msg}", flush=True)


def boot_time() -> float:
    for line in Path("/proc/stat").read_text().splitlines():
        if line.startswith("btime "):
            return float(line.split()[1])
    raise RuntimeError("no btime in /proc/stat")


def segment_file(cam: str, start: float) -> Path:
    """Frigate's file for a segment: recordings/YYYY-MM-DD/HH/<camera>/MM.SS.mp4 (UTC start time)."""
    return MAIN_HOST / time.strftime(f"%Y-%m-%d/%H/{cam}/%M.%S.mp4", time.gmtime(start))


def damage(path: Path) -> str | None:
    if not path.exists():
        return "file missing"
    return "empty file" if path.stat().st_size == 0 else mp4box.cut_short(path)


def api(method: str, path: str, timeout: float = 60.0):
    req = urllib.request.Request(f"{vivo.FRIGATE_API}{path}", method=method)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
    try:
        return json.loads(raw) if raw else {}
    except ValueError:                  # e.g. /version answers in plain text
        return raw.decode(errors="replace")


def wait_for_frigate(timeout: float, call=api) -> bool:
    end = time.time() + timeout
    while True:
        try:
            call("GET", "/version", 5)
            return True
        except (OSError, ValueError):
            if time.time() >= end:
                return False
            time.sleep(5)


def clean(cam: str, after: float, before: float, dry_run: bool, call=api, damage_of=damage) -> int:
    """Delete the camera's broken segments indexed in [after, before]; returns how many."""
    n, gone = 0, set()
    rows = call("GET", f"/{cam}/recordings?after={after:.3f}&before={before:.3f}")
    if not isinstance(rows, list):
        log(f"{cam}: unexpected answer from Frigate, nothing checked: {str(rows)[:200]}")
        return 0
    for row in rows:
        if row["id"] in gone:
            continue
        why = damage_of(segment_file(cam, row["start_time"]))
        if not why:
            continue
        what = f"{cam} {vivo.utc(row['start_time'])} ({row['end_time'] - row['start_time']:.0f} s, {why})"
        a, b = row["start_time"] + INSET, row["end_time"] - INSET
        hits = call("GET", f"/{cam}/recordings?after={a:.3f}&before={b:.3f}") if a < b else []
        # Segments can overlap by a second or more; the delete then also takes the overlapping ones,
        # which is only allowed when they are broken too.
        others = [h for h in hits if h["id"] != row["id"]]
        healthy = [h for h in others if not damage_of(segment_file(cam, h["start_time"]))]
        if row["id"] not in {h["id"] for h in hits} or healthy:
            log(f"{what}: left in the index, {len(healthy)} healthy recordings overlap its range")
            continue
        if others:
            what += f" with {len(others)} broken overlapping recordings"
        if not dry_run:
            try:
                call("DELETE", f"/recordings/start/{a:.3f}/end/{b:.3f}?cameras={cam}")
            except (OSError, ValueError) as e:
                log(f"{what}: left in the index, the delete failed ({e})")
                continue
        log(f"{what}: {'would be taken' if dry_run else 'taken'} out of Frigate's index")
        gone |= {h["id"] for h in hits}
        n += len(hits)
    return n


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--before", type=float, help="end of the window, epoch seconds (default: this boot)")
    ap.add_argument("--minutes", type=float, default=10, help="length of the window")
    ap.add_argument("--wait", type=float, default=300, help="seconds to wait for Frigate's API")
    a = ap.parse_args()
    per_boot = a.before is None and not a.dry_run
    if per_boot and DONE.exists():
        return 0
    before = a.before if a.before is not None else boot_time()
    after = before - a.minutes * 60
    try:
        cams = vivo.services_config()["cameras"]
    except (OSError, ValueError, KeyError) as e:
        log(f"cannot read the camera list from services/config.json ({e}); nothing checked")
        return 1
    if not wait_for_frigate(a.wait):
        log(f"Frigate's API did not answer within {a.wait:.0f} s; nothing checked")
        return 1
    n = 0
    for cam in cams:                    # one camera's error never stops the others
        try:
            n += clean(cam, after, before, a.dry_run)
        except (OSError, ValueError, KeyError, TypeError) as e:
            log(f"{cam}: skipped ({e})")
    log(f"{n} broken recordings {'found' if a.dry_run else 'taken out of the index'}, "
        f"window {vivo.utc(after)} to {vivo.utc(before)}")
    if per_boot:
        DONE.parent.mkdir(parents=True, exist_ok=True)
        DONE.touch()
    return 0


if __name__ == "__main__":
    sys.exit(main())
