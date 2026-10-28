"""The index record: one small note per uploaded segment, so Vivo's backend can find it.

How the notes reach the backend is not decided yet. Everything that writes them goes through a
*sink*, so the output can be replaced without touching the uploader:

    class MySink(IndexSink):
        def emit(self, record: dict) -> None: ...      # raise on failure: it is retried later

and register it in SINKS. The default sink appends one JSON line per segment to a daily file.

A record is emitted after the upload is confirmed. If the sink fails, or the process stops
between writing and saving that fact, the record is emitted again later. Whoever reads the
records must therefore treat `key` as unique and ignore repeats ("at least once" delivery).
"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

from .config import Config
from .segment import iso_utc

SCHEMA_VERSION = 1


def build_record(row, bucket: str) -> dict:
    """The index record for one uploaded segment (`row` is a row of the state file)."""
    return {
        "schema": SCHEMA_VERSION,
        "camera_id": row["camera_id"],
        "start_utc": iso_utc(row["start_utc"]),
        "end_utc": iso_utc(row["end_utc"]),
        "bucket": bucket,
        "key": row["key"],
        "size": row["size"],
        "sha256": row["sha256"],
        "etag": row["etag"],
        "uploaded_at": iso_utc(int(row["uploaded_at"])),
    }


class IndexSink:
    def emit(self, record: dict) -> None:
        raise NotImplementedError

    def close(self) -> None:
        pass


class JsonLinesSink(IndexSink):
    """Appends the record as one line to `<dir>/index-<YYYY-MM-DD>.jsonl` (day of the segment start)."""

    def __init__(self, directory: Path):
        self.dir = Path(directory)
        self._lock = threading.Lock()

    def emit(self, record: dict) -> None:
        day = record["start_utc"][:10]
        line = json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n"
        with self._lock:
            self.dir.mkdir(parents=True, exist_ok=True)
            with open(self.dir / f"index-{day}.jsonl", "a", encoding="utf-8") as f:
                f.write(line)
                f.flush()
                os.fsync(f.fileno())


class NullSink(IndexSink):
    """Discards records (for tests, or until the delivery is decided)."""

    def emit(self, record: dict) -> None:
        pass


SINKS = {"jsonl": lambda cfg: JsonLinesSink(cfg.index_dir or cfg.state_db.parent / "index"),
         "none": lambda cfg: NullSink()}


def make_sink(cfg: Config) -> IndexSink:
    try:
        return SINKS[cfg.index_sink](cfg)
    except KeyError:
        raise ValueError(f"unknown index sink {cfg.index_sink!r}; choose one of {sorted(SINKS)}") from None
