"""SQLite state store: one row per logical segment (identified by its object key)."""
from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

PENDING, UPLOADING, UPLOADED, CONFLICT, MISSING = (
    "pending", "uploading", "uploaded", "conflict", "missing")

SCHEMA = """
CREATE TABLE IF NOT EXISTS segments (
    key             TEXT PRIMARY KEY,   -- object key = logical identity of the segment
    rel_path        TEXT NOT NULL,
    camera_id       TEXT,               -- from the file name
    start_utc       INTEGER,            -- segment start, seconds since the epoch (UTC)
    end_utc         INTEGER,            -- segment end (start + nominal length unless the name has it)
    size            INTEGER NOT NULL,
    mtime_ns        INTEGER NOT NULL,
    sha256          TEXT,
    md5             TEXT,
    state           TEXT NOT NULL,
    attempts        INTEGER NOT NULL DEFAULT 0,
    next_attempt_at REAL NOT NULL DEFAULT 0,
    last_error      TEXT,
    etag            TEXT,
    first_seen      REAL NOT NULL,
    uploaded_at     REAL,
    local_deleted   INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_segments_due ON segments (state, next_attempt_at);
"""

# Columns added after the first version. A state file made by an older version gets them on start.
_ADDED_COLUMNS = {"camera_id": "TEXT", "start_utc": "INTEGER", "end_utc": "INTEGER"}


class Store:
    def __init__(self, path: str | Path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._in_bulk = False
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=FULL")   # a committed state survives a crash
        self._db.executescript(SCHEMA)
        have = {r["name"] for r in self._db.execute("PRAGMA table_info(segments)")}
        for column, kind in _ADDED_COLUMNS.items():
            if column not in have:
                self._db.execute(f"ALTER TABLE segments ADD COLUMN {column} {kind}")
        self._db.commit()

    def close(self) -> None:
        self._db.close()

    def _exec(self, sql: str, args: tuple = ()):
        if self._in_bulk:            # inside bulk(): committed once, when the block ends
            with self._lock:
                return self._db.execute(sql, args)
        with self._lock, self._db:   # `with db` = one transaction, committed on success
            return self._db.execute(sql, args)

    @contextmanager
    def bulk(self):
        """Group many writes into one transaction (used when a scan registers many files).

        Use only for discovery writes that can be repeated. Upload results are always committed
        one by one, so a confirmed upload is never lost.
        """
        self._in_bulk = True
        try:
            yield
            with self._lock:
                self._db.commit()
        except BaseException:
            with self._lock:
                self._db.rollback()
            raise
        finally:
            self._in_bulk = False

    def get(self, key: str):
        with self._lock:
            return self._db.execute("SELECT * FROM segments WHERE key=?", (key,)).fetchone()

    def all_keys(self) -> set[str]:
        with self._lock:
            return {r["key"] for r in self._db.execute("SELECT key FROM segments")}

    def register(self, key: str, rel_path: str, size: int, mtime_ns: int, now: float,
                 camera_id: str | None = None, start_utc: int | None = None,
                 end_utc: int | None = None) -> str:
        """Record a finished local file. Returns 'new', 'changed' or 'known'."""
        row = self.get(key)
        if row is None:
            self._exec(
                "INSERT INTO segments (key, rel_path, camera_id, start_utc, end_utc, size, mtime_ns,"
                " state, first_seen) VALUES (?,?,?,?,?,?,?,?,?)",
                (key, rel_path, camera_id, start_utc, end_utc, size, mtime_ns, PENDING, now))
            return "new"
        if (row["size"], row["mtime_ns"]) == (size, mtime_ns):
            return "known"
        # The local file is not what we recorded. If it is still waiting, just refresh the
        # numbers. Otherwise re-check it: the upload step compares hashes with the remote
        # object, so identical content is not uploaded twice and different content is
        # reported as a conflict instead of silently overwriting.
        if row["state"] in (PENDING, UPLOADING):
            self._exec("UPDATE segments SET size=?, mtime_ns=? WHERE key=?", (size, mtime_ns, key))
            return "known"
        self._exec(
            "UPDATE segments SET size=?, mtime_ns=?, state=?, attempts=0, next_attempt_at=0,"
            " last_error=NULL WHERE key=?", (size, mtime_ns, PENDING, key))
        return "changed"

    def due(self, now: float, limit: int):
        with self._lock:
            return self._db.execute(
                "SELECT * FROM segments WHERE state=? AND next_attempt_at<=?"
                " ORDER BY start_utc, key LIMIT ?", (PENDING, now, limit)).fetchall()

    def mark_uploading(self, key: str) -> int:
        self._exec("UPDATE segments SET state=?, attempts=attempts+1 WHERE key=?", (UPLOADING, key))
        return self.get(key)["attempts"]

    def set_hash(self, key: str, size: int, sha256: str, md5: str) -> None:
        self._exec("UPDATE segments SET size=?, sha256=?, md5=? WHERE key=?", (size, sha256, md5, key))

    def mark_uploaded(self, key: str, etag: str, now: float) -> None:
        self._exec("UPDATE segments SET state=?, etag=?, uploaded_at=?, last_error=NULL WHERE key=?",
                   (UPLOADED, etag, now, key))

    def mark_retry(self, key: str, error: str, next_at: float) -> None:
        self._exec("UPDATE segments SET state=?, last_error=?, next_attempt_at=? WHERE key=?",
                   (PENDING, error, next_at, key))

    def requeue(self, key: str, next_at: float, note: str) -> None:
        self._exec("UPDATE segments SET state=?, attempts=MAX(attempts-1,0), last_error=?,"
                   " next_attempt_at=? WHERE key=?", (PENDING, note, next_at, key))

    def set_state(self, key: str, state: str, error: str | None = None) -> None:
        self._exec("UPDATE segments SET state=?, last_error=? WHERE key=?", (state, error, key))

    def mark_local_deleted(self, key: str) -> None:
        self._exec("UPDATE segments SET local_deleted=1 WHERE key=?", (key,))

    def reset_interrupted(self) -> int:
        """After a crash or restart, anything left 'uploading' must be checked again."""
        cur = self._exec("UPDATE segments SET state=?, next_attempt_at=0 WHERE state=?",
                         (PENDING, UPLOADING))
        return cur.rowcount

    def counts(self) -> dict[str, int]:
        with self._lock:
            rows = self._db.execute("SELECT state, COUNT(*) n FROM segments GROUP BY state").fetchall()
        return {r["state"]: r["n"] for r in rows}

    def unfinished(self):
        with self._lock:
            return self._db.execute(
                "SELECT * FROM segments WHERE state!=? ORDER BY start_utc, key", (UPLOADED,)).fetchall()
