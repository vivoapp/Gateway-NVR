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
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import store as st
from .config import Config
from .index import IndexSink, build_record, make_sink
from .segment import parse_segment_name
from .storage import PAUSING_KINDS, IntegrityError, S3Storage, classify, remote_matches

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
                 clock=time.time, jitter=random.random, sink: IndexSink | None = None):
        self.cfg = cfg
        self.storage = storage or S3Storage(cfg)
        self.store = store or st.Store(cfg.state_db)
        self.sink = sink or make_sink(cfg)
        self._lock = threading.Lock()   # guards the pause state below (workers share it)
        self._index_pause_until = 0.0
        self._win = {"count": 0, "bytes": 0}      # what was uploaded since the last stats line
        self._last_stats = (time.monotonic(), time.process_time())
        self.clock = clock
        self.jitter = jitter
        self._known: set[str] = set()   # keys already in the state file: skipped by normal scans
        self._dir_state: dict[str, int | None] = {}   # date folder -> modification time at last listing
        self._last_full_scan: float | None = None
        self._warned_names: set[str] = set()
        self._pause_until = 0.0         # whole-service pause: storage unreachable, refusing us, or full
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
        """Upload what is due. Cameras go in parallel (`workers`); one camera's segments go in order.

        A file-level problem delays only that file. Storage-level problems (unreachable, wrong
        credentials, no space) stop every worker and pause the whole service.
        """
        stats = {"uploaded": 0, "retry": 0, "outage": 0}
        if self.clock() < self._pause_until:
            return stats
        rows = self.store.due(self.clock(), self.cfg.batch_size)
        by_camera: dict[str, list] = {}
        for row in rows:                      # rows are oldest first, so cameras start oldest first
            by_camera.setdefault(row["camera_id"] or "", []).append(row)
        halt = threading.Event()

        def run_camera(items) -> dict[str, int]:
            local = {"uploaded": 0, "retry": 0, "outage": 0}
            for row in items:
                if halt.is_set() or should_stop():
                    break
                result = self._upload_one(row)
                if result in local:
                    local[result] += 1
                if result == "outage":
                    halt.set()                # do not hammer a storage that is refusing us
                    break
            return local

        groups = list(by_camera.values())
        workers = min(self.cfg.workers, len(groups))
        if workers <= 1:
            results = [run_camera(rows)] if rows else []      # one line: strictly oldest first
        else:
            with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="upload") as pool:
                results = list(pool.map(run_camera, groups))
        for r in results:
            for k, v in r.items():
                stats[k] += v
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
            with self._lock:
                self._outage_streak = 0
                if action == "uploaded":
                    self._win["count"] += 1
                    self._win["bytes"] += size
            log.info("event=%s key=%s size=%d sha256=%s attempts=%d ms=%d", action, key, size, sha,
                     attempts, (time.monotonic() - t0) * 1000)
            return "uploaded"                 # the local file is never touched: the recorder owns it
        except Exception as exc:              # not BaseException: a crash must leave 'uploading'
            return self._schedule_retry(key, attempts, exc)

    def _schedule_retry(self, key: str, attempts: int, exc: Exception) -> str:
        kind = classify(exc)
        error = f"{type(exc).__name__}: {exc}"
        if kind in PAUSING_KINDS:
            with self._lock:
                now = self.clock()
                if now >= self._pause_until:          # first failure of this pause (workers share it)
                    if kind == "outage":
                        self._outage_streak += 1
                        delay = self._backoff(self._outage_streak)
                    else:                              # wrong credentials / no space: not a blip
                        delay = self.cfg.backoff_max * (0.5 + 0.5 * self.jitter())
                    self._pause_until = now + delay
                    if kind == "auth":
                        log.error("event=storage_refused_credentials service_paused_for=%.0fs error=%s",
                                  delay, error)
                    elif kind == "full":
                        log.error("event=storage_full service_paused_for=%.0fs error=%s", delay, error)
                delay = self._pause_until - now
        else:
            delay = self._backoff(attempts)
        self.store.mark_retry(key, error, self.clock() + delay)
        log.warning("event=upload_failed key=%s attempts=%d kind=%s retry_in=%.1fs error=%s",
                    key, attempts, kind, delay, error)
        return "outage" if kind in PAUSING_KINDS else "retry"

    # ---- index records ----------------------------------------------------------------
    def deliver_index(self, limit: int = 200) -> int:
        """Hand the index record of every confirmed upload to the sink. Returns how many were sent.

        A failing sink never blocks uploading: the records stay in the state file and are sent
        later (with a pause, so a broken sink is not retried every 2 seconds).
        """
        if self.clock() < self._index_pause_until:
            return 0
        sent = 0
        for row in self.store.unsent_index(limit):
            try:
                self.sink.emit(build_record(row, self.cfg.bucket))
            except Exception as exc:
                self._index_pause_until = self.clock() + max(self.cfg.backoff_initial, 5.0)
                log.warning("event=index_failed key=%s error=%s: %s", row["key"], type(exc).__name__, exc)
                break
            self.store.mark_index_sent(row["key"])
            sent += 1
        return sent

    # ---- measurements -----------------------------------------------------------------
    def log_stats(self) -> None:
        """One line with the upload rate and the CPU and memory used by this process.

        Used for the M19 report ("measured upload rate and CPU use at 10 cameras"). cpu_pct is the
        share of one CPU core used since the previous line.
        """
        wall0, cpu0 = self._last_stats
        wall, cpu = time.monotonic(), time.process_time()
        self._last_stats = (wall, cpu)
        with self._lock:
            count, nbytes = self._win["count"], self._win["bytes"]
            self._win = {"count": 0, "bytes": 0}
        window = max(wall - wall0, 1e-9)
        rss_mb = 0.0
        try:
            with open("/proc/self/status") as f:
                for line in f:
                    if line.startswith("VmRSS:"):
                        rss_mb = int(line.split()[1]) / 1024
        except OSError:
            pass
        pending = self.store.counts().get(st.PENDING, 0)
        log.info("event=stats window_s=%.0f uploaded=%d bytes=%d mbps=%.3f cpu_pct=%.1f rss_mb=%.0f "
                 "pending=%d workers=%d limit_mbps=%g", window, count, nbytes, nbytes * 8 / window / 1e6,
                 (cpu - cpu0) / window * 100, rss_mb, pending, self.cfg.workers, self.cfg.upload_limit_mbps)

    # ---- main loop ------------------------------------------------------------------
    def run_once(self) -> dict[str, int]:
        self.scan_once()
        stats = self.upload_due()
        self.deliver_index()
        return stats

    def run_forever(self, stop: threading.Event) -> None:
        self.recover()
        log.info("event=running watch_dir=%s bucket=%s", self.cfg.watch_dir, self.cfg.bucket)
        next_stats = time.monotonic() + self.cfg.stats_interval
        self._last_stats = (time.monotonic(), time.process_time())
        while not stop.is_set():
            try:
                self.scan_once()
                self.upload_due(should_stop=stop.is_set)
                self.deliver_index()
                if self.cfg.stats_interval > 0 and time.monotonic() >= next_stats:
                    self.log_stats()
                    next_stats = time.monotonic() + self.cfg.stats_interval
            except Exception:
                log.exception("event=loop_error")
            stop.wait(self.cfg.scan_interval)
        log.info("event=stopped counts=%s", self.store.counts())
