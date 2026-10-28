"""Watch a directory, upload finished segments, survive outages and restarts."""
from __future__ import annotations

import base64
import hashlib
import logging
import os
import random
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from botocore.exceptions import ClientError, ConnectionError as BotoConnectionError, HTTPClientError

from . import store as st
from .config import Config
from .segment import parse_segment_name
from .storage import IntegrityError, S3Storage, remote_matches

log = logging.getLogger("segment_uploader")

CHUNK = 1024 * 1024


def hash_file(path: Path) -> tuple[int, str, str, str]:
    """One pass over the file: (size, sha256 hex, md5 hex, md5 base64)."""
    sha, md5, size = hashlib.sha256(), hashlib.md5(usedforsecurity=False), 0
    with open(path, "rb") as f:
        while chunk := f.read(CHUNK):
            sha.update(chunk)
            md5.update(chunk)
            size += len(chunk)
    return size, sha.hexdigest(), md5.hexdigest(), base64.b64encode(md5.digest()).decode()


def is_outage(exc: BaseException) -> bool:
    """Errors that mean 'the storage is unreachable or unhealthy', not 'this file is bad'."""
    if isinstance(exc, (BotoConnectionError, HTTPClientError)):
        return True
    if isinstance(exc, ClientError):
        status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0)
        return status >= 500 or exc.response.get("Error", {}).get("Code") in ("SlowDown", "RequestTimeout")
    return False


_DATE_DIR = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def iter_local_files(cfg: Config):
    """Every finished segment file (`*.mp4`) under the watch directory, at any depth.

    `.mp4.part` files (still being written) and anything else are not segments. This walks the
    whole tree, so it is used at start-up, for the hourly full scan and for `status`.
    """
    for root, dirs, files in os.walk(cfg.watch_dir):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for name in files:
            if not name.startswith(".") and name.endswith(".mp4"):
                yield Path(root) / name


def iter_recent_files(cfg: Config, today: datetime, dir_state: dict):
    """Relative paths ("<camera>/<date>/<name>.mp4") of finished segments in recent date folders.

    Layout: <watch_dir>/<camera_id>/<YYYY-MM-DD>/<name>.mp4. Date folders are UTC dates, but the
    window (`recent_days`) is wide enough that a recorder using local dates is still covered.

    A folder is listed again only if its modification time changed since it was last listed. The
    recorder adds a file by renaming it into the folder, which changes that time, so folders of
    earlier days cost one `stat` per scan instead of a listing of 1,440 files per camera.
    A time stamp within the last 2 s is not trusted (a rename right after the listing could
    leave it unchanged), so that folder is listed again on the next scan. The full scan is the
    safety net for anything missed.
    """
    cutoff = (today - timedelta(days=cfg.recent_days)).strftime("%Y-%m-%d")
    try:
        cameras = [e for e in os.scandir(cfg.watch_dir) if e.is_dir() and not e.name.startswith(".")]
    except FileNotFoundError:
        return
    for cam in cameras:
        try:
            dates = [e for e in os.scandir(cam.path)
                     if e.is_dir() and _DATE_DIR.match(e.name) and e.name >= cutoff]
        except FileNotFoundError:
            continue
        for date_dir in dates:
            try:
                mtime = date_dir.stat().st_mtime_ns
                if dir_state.get(date_dir.path) == mtime:
                    continue                      # nothing was added since the last listing
                names = [f.name for f in os.scandir(date_dir.path)
                         if f.name.endswith(".mp4") and not f.name.startswith(".")]
            except FileNotFoundError:
                continue
            for name in names:
                yield f"{cam.name}/{date_dir.name}/{name}"
            trusted = mtime < time.time_ns() - 2_000_000_000
            dir_state[date_dir.path] = mtime if trusted else None


class Service:
    def __init__(self, cfg: Config, storage=None, store: st.Store | None = None,
                 clock=time.time, jitter=random.random):
        self.cfg = cfg
        self.storage = storage or S3Storage(cfg)
        self.store = store or st.Store(cfg.state_db)
        self.clock = clock
        self.jitter = jitter
        self._known: set[str] = set()   # keys already in the state file: skipped by normal scans
        self._dir_state: dict[str, int | None] = {}   # date folder -> modification time at last listing
        self._last_full_scan: float | None = None
        self._warned_names: set[str] = set()
        self._pause_until = 0.0         # whole-service pause while storage is unreachable
        self._outage_streak = 0

    # ---- start-up -------------------------------------------------------------------
    def recover(self) -> None:
        n = self.store.reset_interrupted()
        self._known = self.store.all_keys()
        self._dir_state = {}
        self._last_full_scan = None            # the first scan after a start looks at everything
        log.info("event=startup recovered_interrupted=%d counts=%s", n, self.store.counts())

    # ---- scanning -------------------------------------------------------------------
    def scan_once(self, full: bool | None = None) -> int:
        """Register finished segments (`*.mp4`). Returns how many were new or changed.

        The recorder writes `<name>.mp4.part` and renames it when the segment is complete, so a
        `.mp4` file is finished and needs no "is it still growing?" check.

        A full scan walks every folder: at start-up, then every `full_scan_interval` seconds, or
        when `full=True`. Other scans look only at recent date folders and skip files already in
        the state file, which keeps them cheap even with days of segments on the disk.
        """
        now = self.clock()
        if full is None:
            full = (self._last_full_scan is None
                    or now - self._last_full_scan >= self.cfg.full_scan_interval)
        previous_full_scan = self._last_full_scan
        if full:
            self._last_full_scan = now
            rels = (p.relative_to(self.cfg.watch_dir).as_posix() for p in self._iter_files())
        else:
            rels = iter_recent_files(self.cfg, datetime.fromtimestamp(now, timezone.utc),
                                     self._dir_state)
        added: list[str] = []                 # keys this scan put in self._known
        # One transaction for the whole scan: a first scan can register tens of thousands of
        # files, and a commit (fsync) per file would take minutes. If the scan fails half way it
        # is rolled back as a unit and the next scan finds the files again.
        try:
            with self.store.bulk():
                return self._register_all(rels, full, now, added)
        except BaseException:
            self._known.difference_update(added)     # forget what the rolled-back scan did
            self._dir_state = {}
            self._last_full_scan = previous_full_scan
            raise

    def _register_all(self, rels, full: bool, now: float, added: list[str]) -> int:
        found = 0
        for rel in rels:
            key = self.cfg.object_key(rel)
            if not full and key in self._known:
                continue                          # plain string test: no Path, no stat
            path = self.cfg.watch_dir / rel
            info = parse_segment_name(path.name, self.cfg.nominal_duration)
            if info is None:
                if key not in self._warned_names:
                    self._warned_names.add(key)
                    log.warning("event=ignored_name rel=%s reason=not_in_agreed_format", rel)
                continue
            try:
                stat = path.stat()
            except FileNotFoundError:
                continue                          # removed by retention between listing and now
            outcome = self.store.register(key, rel, stat.st_size, stat.st_mtime_ns, now,
                                          info.camera_id, info.start_utc, info.end_utc)
            if key not in self._known:
                self._known.add(key)
                added.append(key)
            if outcome != "known":
                found += 1
                log.info("event=detected key=%s camera=%s start=%s size=%d outcome=%s", key,
                         info.camera_id, info.start_iso, stat.st_size, outcome)
        return found

    def _iter_files(self):
        return iter_local_files(self.cfg)

    # ---- uploading ------------------------------------------------------------------
    def _backoff(self, attempts: int) -> float:
        base = min(self.cfg.backoff_max, self.cfg.backoff_initial * 2 ** max(attempts - 1, 0))
        return base * (0.5 + 0.5 * self.jitter())

    def upload_due(self, should_stop=lambda: False) -> dict[str, int]:
        stats = {"uploaded": 0, "retry": 0, "outage": 0}
        now = self.clock()
        if now < self._pause_until:
            return stats
        for row in self.store.due(now, self.cfg.batch_size):
            if should_stop():
                break
            result = self._upload_one(row)
            if result in stats:
                stats[result] += 1
            if result == "outage":
                break                         # do not hammer an unreachable server file by file
        return stats

    def _upload_one(self, row) -> str:
        key = row["key"]
        path = self.cfg.watch_dir / row["rel_path"]
        t0 = time.monotonic()
        attempts = self.store.mark_uploading(key)
        try:
            try:
                before = path.stat()
            except FileNotFoundError:
                self.store.set_state(key, st.MISSING, "local file vanished before upload")
                log.error("event=missing key=%s", key)
                return "skipped"
            size, sha, md5_hex, md5_b64 = hash_file(path)
            after = path.stat()
            if (after.st_size, after.st_mtime_ns) != (before.st_size, before.st_mtime_ns):
                self.store.requeue(key, self.clock() + max(self.cfg.scan_interval, 1.0),
                                   "file changed while hashing")
                log.info("event=still_changing key=%s", key)
                return "skipped"
            self.store.set_hash(key, size, sha, md5_hex)

            remote = self.storage.head(key)
            if remote is None:
                self.storage.put(key, path, md5_b64, sha, size)
                remote = self.storage.head(key)
                if remote is None or not remote_matches(remote, size, md5_hex, sha):
                    try:                      # do not leave a bad object behind
                        self.storage.delete(key)
                    except Exception:
                        log.warning("event=cleanup_failed key=%s", key)
                    raise IntegrityError("stored object does not match local file")
                action = "uploaded"
            elif remote_matches(remote, size, md5_hex, sha):
                action = "already_present"    # e.g. we crashed after upload, before saving state
            else:
                self.store.set_state(key, st.CONFLICT,
                                     "a different object already exists under this key; not overwritten")
                log.error("event=conflict key=%s local_size=%d remote_size=%d", key, size, remote.size)
                return "skipped"

            now_stat = path.stat()
            if (now_stat.st_size, now_stat.st_mtime_ns) != (before.st_size, before.st_mtime_ns):
                self.store.set_state(key, st.CONFLICT, "local file changed during upload")
                log.error("event=conflict key=%s reason=changed_during_upload", key)
                return "skipped"

            self.store.mark_uploaded(key, remote.etag, self.clock())
            self._outage_streak = 0
            log.info("event=%s key=%s size=%d sha256=%s attempts=%d ms=%d", action, key, size, sha,
                     attempts, (time.monotonic() - t0) * 1000)
            if self.cfg.delete_after_upload:
                path.unlink()                 # only reached after verified upload
                self.store.mark_local_deleted(key)
                log.info("event=local_deleted key=%s", key)
            return "uploaded"
        except Exception as exc:              # not BaseException: a crash must leave 'uploading'
            return self._schedule_retry(key, attempts, exc)

    def _schedule_retry(self, key: str, attempts: int, exc: Exception) -> str:
        outage = is_outage(exc)
        if outage:
            self._outage_streak += 1
            delay = self._backoff(self._outage_streak)
            self._pause_until = self.clock() + delay
        else:
            delay = self._backoff(attempts)
        self.store.mark_retry(key, f"{type(exc).__name__}: {exc}", self.clock() + delay)
        log.warning("event=upload_failed key=%s attempts=%d kind=%s retry_in=%.1fs error=%s",
                    key, attempts, "outage" if outage else "file", delay, f"{type(exc).__name__}: {exc}")
        return "outage" if outage else "retry"

    # ---- main loop ------------------------------------------------------------------
    def run_once(self) -> dict[str, int]:
        self.scan_once()
        return self.upload_due()

    def run_forever(self, stop: threading.Event) -> None:
        self.recover()
        log.info("event=running watch_dir=%s bucket=%s", self.cfg.watch_dir, self.cfg.bucket)
        while not stop.is_set():
            try:
                self.scan_once()
                self.upload_due(should_stop=stop.is_set)
            except Exception:
                log.exception("event=loop_error")
            stop.wait(self.cfg.scan_interval)
        log.info("event=stopped counts=%s", self.store.counts())
