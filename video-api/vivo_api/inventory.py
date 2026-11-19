"""The camera list, from the recorder's cameras.yaml (read-only; reloaded when it changes)."""
from __future__ import annotations

import threading
from pathlib import Path

import yaml

from .grants import CAMERA_ID


class Inventory:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._stamp: tuple | None = None
        self._cams: dict[str, dict] = {}

    def _load(self) -> None:
        try:
            st = self.path.stat()
        except OSError:
            self._cams, self._stamp = {}, None
            return
        stamp = (st.st_mtime_ns, st.st_size)
        if stamp == self._stamp:
            return
        cams: dict[str, dict] = {}
        try:
            data = yaml.safe_load(self.path.read_text()) or {}
            for c in data.get("cameras") or []:
                cid = str(c.get("id", ""))
                if CAMERA_ID.match(cid):
                    cams[cid] = {"camera_id": cid, "name": str(c.get("name") or cid)}
        except (OSError, yaml.YAMLError):
            return                                   # keep the last good list
        self._cams, self._stamp = cams, stamp

    def ids(self) -> list[str]:
        with self._lock:
            self._load()
            return list(self._cams)

    def get(self, camera_id: str) -> dict | None:
        with self._lock:
            self._load()
            c = self._cams.get(camera_id)
            return dict(c) if c else None

    @staticmethod
    def public(cam: dict) -> dict:
        return {"camera_id": cam["camera_id"], "name": cam["name"],
                "streams": [{"role": "main"}, {"role": "sub"}]}
