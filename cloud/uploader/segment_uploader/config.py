"""Configuration loading (TOML)."""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Config:
    watch_dir: Path
    state_db: Path
    bucket: str
    endpoint_url: str | None = None
    region: str = "us-east-1"
    key_prefix: str = ""
    access_key: str | None = None
    secret_key: str | None = None
    # A file ending in .mp4 is finished: the recorder renames <name>.mp4.part to <name>.mp4
    # only when the segment is complete. Nothing else is ever uploaded.
    # Length used for a segment's end time while the file name does not carry one.
    nominal_duration: float = 60.0
    scan_interval: float = 2.0
    # One `event=stats` log line this often (seconds): upload rate, CPU and memory of the uploader.
    stats_interval: float = 60.0
    # Normal scans look only at date folders from the last `recent_days` days (UTC today and
    # back). A full scan of every folder runs at start-up and then every `full_scan_interval`
    # seconds. This keeps the scan cheap when days of segments are on the disk.
    recent_days: int = 2
    full_scan_interval: float = 3600.0
    # The uploader never deletes local files. Removing old segments is the recorder's job (M9).
    # Upload limit in megabits per second for all workers together. 0 = no limit.
    upload_limit_mbps: float = 0.0
    # How many cameras are uploaded at the same time. Segments of one camera always go in order.
    workers: int = 2
    # Where the index records (one per uploaded segment) are written. See index.py.
    index_sink: str = "jsonl"
    index_dir: Path | None = None            # default: an "index" folder next to the state file
    backoff_initial: float = 2.0
    backoff_max: float = 300.0
    batch_size: int = 50
    connect_timeout: float = 5.0
    read_timeout: float = 60.0

    def object_key(self, rel_path: str) -> str:
        prefix = self.key_prefix.strip("/")
        return f"{prefix}/{rel_path}" if prefix else rel_path


SERVICE_KEYS = {"watch_dir", "state_db", "nominal_duration", "scan_interval", "recent_days",
                "full_scan_interval", "stats_interval", "backoff_initial", "backoff_max", "batch_size",
                "connect_timeout", "read_timeout", "upload_limit_mbps", "workers"}
INDEX_KEYS = {"sink", "dir"}
S3_KEYS = {"endpoint_url", "bucket", "region", "key_prefix", "access_key", "secret_key"}


def load_config(path: str | os.PathLike) -> Config:
    with open(path, "rb") as f:
        raw = tomllib.load(f)
    svc = raw.get("service", {})
    s3 = raw.get("s3", {})
    idx = raw.get("index", {})
    # A misspelled or removed setting must not be silently ignored.
    unknown = ([f"service.{k}" for k in svc if k not in SERVICE_KEYS]
               + [f"s3.{k}" for k in s3 if k not in S3_KEYS]
               + [f"index.{k}" for k in idx if k not in INDEX_KEYS]
               + [k for k in raw if k not in ("service", "s3", "index")])
    if unknown:
        raise ValueError(f"unknown setting(s) in {path}: {', '.join(sorted(unknown))}")
    base = Path(path).resolve().parent

    def p(value: str) -> Path:
        q = Path(value)
        return q if q.is_absolute() else base / q

    cfg = Config(
        watch_dir=p(svc["watch_dir"]),
        state_db=p(svc.get("state_db", "state.db")),
        # Site settings: environment wins over the file, so one image and one config file work on
        # every gateway and only /etc/vivo/cloud.env differs.
        bucket=os.environ.get("S3_BUCKET") or s3.get("bucket", ""),
        endpoint_url=s3.get("endpoint_url"),
        region=os.environ.get("S3_REGION") or s3.get("region", "us-east-1"),
        key_prefix=os.environ.get("S3_KEY_PREFIX") if os.environ.get("S3_KEY_PREFIX") is not None
        else s3.get("key_prefix", ""),
        # Credentials: environment wins over the file, so secrets need not be committed.
        access_key=os.environ.get("S3_ACCESS_KEY") or s3.get("access_key"),
        secret_key=os.environ.get("S3_SECRET_KEY") or s3.get("secret_key"),
    )
    # Environment overrides, handy in containers: they win over the file.
    if os.environ.get("S3_ENDPOINT_URL"):
        cfg.endpoint_url = os.environ["S3_ENDPOINT_URL"]
    for name in ("nominal_duration", "scan_interval", "recent_days", "full_scan_interval", "stats_interval",
                 "backoff_initial", "backoff_max", "connect_timeout", "read_timeout",
                 "batch_size", "upload_limit_mbps", "workers"):
        if name in svc:
            setattr(cfg, name, svc[name])
    cfg.index_sink = idx.get("sink", cfg.index_sink)
    if idx.get("dir"):
        cfg.index_dir = p(idx["dir"])
    if not cfg.bucket:
        raise ValueError("no bucket: set s3.bucket in the config file or S3_BUCKET in the environment")
    if cfg.workers < 1:
        raise ValueError("service.workers must be at least 1")
    if cfg.upload_limit_mbps < 0:
        raise ValueError("service.upload_limit_mbps must be 0 (no limit) or more")
    return cfg
