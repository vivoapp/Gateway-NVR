#!/usr/bin/env python3
"""Video API handoff check (the recorder's tests/handoff_check.py rules, for video-api/).

usage: tests/handoff_check.py [--committed]     # --committed: also require everything committed

Checks:
1. No settings, key or certificate file in the folder (*.env other than examples, *.pem, *.key,
   a key set holding a private key).
2. No secret in any file: private keys, logins inside URLs, PASSWORD/SECRET/TOKEN lines with a real value.
3. The base image is pinned by version and digest; every Python package by version and hash.
4. docs/openapi.json matches the code.
5. Every module M13 to M18 has its page in docs/ and its test or tool.
6. No footage or pictures, and no file over 5 MB.
7. The unit tests pass.
8. With --committed, in a git checkout: nothing in video-api/ changed or untracked.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULES = {"m13": "tools/export_openapi.py", "m14": "tests/m14_check.py", "m15": "tests/m15_live.mjs",
           "m16": "tests/m16_playback.mjs", "m17": "tests/m17_clips.py", "m18": "tests/contract/test_contract.py"}
SKIP = {".venv", "lab", "reports", "__pycache__", ".pytest_cache", ".git"}
SECRET_FILE = re.compile(r"(^|/)([^/]+\.env|[^/]+\.pem|[^/]+\.key|id_[a-z0-9]+)$")
FOOTAGE = re.compile(r"\.(mp4|mkv|mov|ts|m4s|m3u8|h264|h265|hevc|jpe?g|png|webp|webm|pcap)$", re.I)
PRIVATE_KEY = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")
URL_LOGIN = re.compile(r"[a-z][a-z0-9+.-]*://([^:/@\s\"'$]+):([^@/\s\"'$]+)@")
SECRET_LINE = re.compile(r"^\s*(?:export\s+)?([A-Z0-9_]*(?:PASSWORD|SECRET|TOKEN)[A-Z0-9_]*)=[\"']?([^\"'\s#]*)", re.M)
PLACEHOLDER = re.compile(r"^(\$.*|\{.*\}|\*+|<[^>]*>|change-me|\.\.\.|)$")
PINNED_BASE = re.compile(r"^ARG PYTHON_IMAGE=python:\d+\.\d+\.\d+[\w.-]*@sha256:[0-9a-f]{64}$", re.M)
MAX_BYTES = 5_000_000


def files() -> list[Path]:
    r = subprocess.run(["git", "-C", str(ROOT), "ls-files", "-co", "--exclude-standard", "-z", "."], capture_output=True)
    if r.returncode == 0:
        return [ROOT / p for p in r.stdout.decode().split("\0") if p and (ROOT / p).is_file()]
    return [p for p in ROOT.rglob("*") if p.is_file() and not SKIP & set(p.relative_to(ROOT).parts)]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--committed", action="store_true")
    a = ap.parse_args()
    results: list[tuple[str, bool, str]] = []
    check = lambda name, ok, detail="": results.append((name, bool(ok), detail))  # noqa: E731
    fs = files()
    rel = lambda p: str(p.relative_to(ROOT))  # noqa: E731

    bad_files = [rel(p) for p in fs if SECRET_FILE.search(rel(p)) and not rel(p).endswith(".example")]
    for p in fs:
        if p.suffix == ".json" and "jwks" in p.name:
            try:
                if any("d" in k for k in json.loads(p.read_text()).get("keys", [])):
                    bad_files.append(rel(p))
            except (OSError, ValueError):
                pass
    check("1. no settings, key or certificate files", not bad_files, ", ".join(bad_files))

    leaks = []
    for p in fs:
        if p.stat().st_size > MAX_BYTES or FOOTAGE.search(p.name):
            continue
        try:
            text = p.read_text()
        except (OSError, UnicodeDecodeError):
            continue
        if PRIVATE_KEY.search(text):
            leaks.append(f"{rel(p)}: private key")
        for m in URL_LOGIN.finditer(text):
            if not PLACEHOLDER.match(m.group(2)):
                leaks.append(f"{rel(p)}: login in a URL")
        for m in SECRET_LINE.finditer(text):
            if not PLACEHOLDER.match(m.group(2)):
                leaks.append(f"{rel(p)}: {m.group(1)} has a value")
    check("2. no secrets in files", not leaks, "; ".join(leaks[:5]))

    dockerfile = (ROOT / "Dockerfile").read_text()
    req = (ROOT / "requirements.txt").read_text()
    pins = re.findall(r"^([A-Za-z0-9_.-]+)==[\w.]+ \\\n((?:\s+--hash=sha256:[0-9a-f]{64}(?: \\)?\n)+)", req, re.M)
    names = re.findall(r"^([A-Za-z0-9_.-]+)==", req, re.M)
    check("3. base image and packages pinned", PINNED_BASE.search(dockerfile) and names and len(pins) == len(names),
          f"{len(pins)} of {len(names)} packages with hashes")

    r = subprocess.run([sys.executable, str(ROOT / "tools" / "export_openapi.py"), "--check"], capture_output=True, text=True)
    check("4. openapi.json matches the code", r.returncode == 0, (r.stdout + r.stderr).strip())

    missing = []
    for m, test in MODULES.items():
        if not list((ROOT / "docs").glob(f"{m}-*.md")):
            missing.append(f"docs/{m}-*.md")
        if not (ROOT / test).exists():
            missing.append(test)
    check("5. every module has its page and its test", not missing, ", ".join(missing))

    media = [rel(p) for p in fs if FOOTAGE.search(p.name) or p.stat().st_size > MAX_BYTES]
    check("6. no footage and no file over 5 MB", not media, ", ".join(media[:5]))

    r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:warnings", "-p", "no:cacheprovider", "tests/unit"],
                       cwd=ROOT, capture_output=True, text=True)
    check("7. unit tests pass", r.returncode == 0, (r.stdout.strip().splitlines() or [""])[-1])

    if a.committed:
        r = subprocess.run(["git", "-C", str(ROOT), "status", "--porcelain", "--", "."], capture_output=True, text=True)
        check("8. everything committed", r.returncode == 0 and not r.stdout.strip(), f"{len(r.stdout.splitlines())} paths not committed")

    for name, ok, detail in results:
        print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if detail else ""))
    return 0 if all(ok for _, ok, _ in results) else 1


if __name__ == "__main__":
    sys.exit(main())
