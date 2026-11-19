#!/usr/bin/env python3
"""Write docs/openapi.json from the code (the machine-readable half of the M13 contract).

usage: tools/export_openapi.py [--check]     # --check: fail if docs/openapi.json is out of date
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

OUT = ROOT / "docs" / "openapi.json"


def spec() -> str:
    from vivo_api.config import Settings
    from vivo_api.main import create_app
    from vivo_api.recorders.mock import MockRecorder

    app = create_app(Settings(), recorder=MockRecorder(Path("/nonexistent")), start_background=False)
    return json.dumps(app.openapi(), indent=2, sort_keys=True) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args()
    text = spec()
    if a.check:
        if not OUT.exists() or OUT.read_text() != text:
            print("docs/openapi.json is out of date: run tools/export_openapi.py", file=sys.stderr)
            return 1
        print("docs/openapi.json is up to date")
        return 0
    OUT.write_text(text)
    print(f"wrote {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
