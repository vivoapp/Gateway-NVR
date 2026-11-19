"""Audit log: one JSON line per event, one file per UTC day, kept AUDIT_DAYS (contract 11.2).

Never stores the grant itself, a password or a URL with credentials.
"""
from __future__ import annotations

import json
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from .timeutil import fmt


class Audit:
    def __init__(self, folder: Path, days: int = 30, clock=time.time):
        self.folder = Path(folder)
        self.days = days
        self.clock = clock
        self._lock = threading.Lock()
        self._pruned_day: str | None = None

    def write(self, event: str, result: str = "allowed", reason: str | None = None,
              grant=None, **fields) -> dict:
        now = self.clock()
        rec = {"time": fmt(now), "event": event, "result": result}
        if reason:
            rec["reason"] = reason
        if grant is not None:
            rec.update(grant.audit_fields())
        for k, v in fields.items():
            if v is not None:
                rec[k] = v
        day = datetime.fromtimestamp(now, tz=timezone.utc).strftime("%Y-%m-%d")
        line = json.dumps(rec, separators=(",", ":")) + "\n"
        with self._lock:
            try:
                self.folder.mkdir(parents=True, exist_ok=True)
                with open(self.folder / f"{day}.jsonl", "a", encoding="utf-8") as f:
                    f.write(line)
                if self._pruned_day != day:
                    self._prune(now)
                    self._pruned_day = day
            except OSError as exc:                       # the request must not fail on the log
                print(f"audit: cannot write: {exc}", file=sys.stderr, flush=True)
        return rec

    def _prune(self, now: float) -> None:
        cutoff = datetime.fromtimestamp(now - self.days * 86400, tz=timezone.utc).strftime("%Y-%m-%d")
        for p in self.folder.glob("*.jsonl"):
            if p.stem < cutoff:
                try:
                    p.unlink()
                except OSError:
                    pass
