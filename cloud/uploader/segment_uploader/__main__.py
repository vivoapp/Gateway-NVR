"""Command line: `python -m segment_uploader run|status --config config.toml`."""
from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import threading

from .config import load_config
from .service import Service


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="segment_uploader")
    ap.add_argument("command", choices=["run", "status"])
    ap.add_argument("--config", default=os.environ.get("SEGMENT_UPLOADER_CONFIG", "config.toml"))
    ap.add_argument("--log-level", default="INFO")
    ap.add_argument("--remote", action="store_true", help="status: also count objects in the bucket")
    ap.add_argument("--brief", action="store_true", help="status: one line of key=value for scripts")
    args = ap.parse_args(argv)
    logging.basicConfig(level=args.log_level.upper(), stream=sys.stdout,
                        format="%(asctime)s %(levelname)s %(message)s")
    cfg = load_config(args.config)

    if args.command == "status":
        from .service import iter_local_files
        from .storage import S3Storage
        from .store import Store
        store = Store(cfg.state_db)
        counts = store.counts()
        local_files = sum(1 for _ in iter_local_files(cfg))
        remote = "n/a"
        if args.remote or args.brief:
            try:
                remote = S3Storage(cfg).count(cfg.object_key(""))
            except Exception:
                remote = "unreachable"
        try:
            if args.brief:
                keys = ("uploaded", "pending", "uploading", "conflict", "missing")
                print(" ".join(f"{k}={counts.get(k, 0)}" for k in keys),
                      f"local_files={local_files} bucket_objects={remote}")
            else:
                print("counts:", counts or "{}")
                print(f"local files waiting in {cfg.watch_dir}: {local_files}")
                if remote != "n/a":
                    print(f"objects in bucket {cfg.bucket}: {remote}")
                for r in store.unfinished():
                    print(f"  {r['state']:9} attempts={r['attempts']} {r['key']}  {r['last_error'] or ''}")
        except BrokenPipeError:          # e.g. `status | head`
            pass
        return 0

    svc = Service(cfg)
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    svc.run_forever(stop)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
