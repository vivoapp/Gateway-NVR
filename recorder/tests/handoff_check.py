#!/usr/bin/env python3
"""M26 final source handoff check: nothing secret committed, every version pinned, nothing left out.

usage:
  tests/handoff_check.py                                     # in a clone of the Vivo repository
  sudo tests/handoff_check.py --secrets /etc/vivo/secrets.env   # on the gateway: also look for every
                                                             # real password in the files

Checks:
1. No settings or key file is in the repository (secrets.env, site.env, *.local.yaml, private keys).
2. No secret in any file: private keys, logins inside URLs, PASSWORD= lines with a real value, cloud
   and GitHub tokens. With --secrets, also none of that file's real passwords and tokens (the report
   names the file and the setting, never the value).
3. Every image in every compose file is pinned by version and digest from versions.env, with no
   "latest"; Docker Engine, containerd and Compose have exact package versions.
4. The generated configs match cameras.yaml (tools/gen_config.py --check).
5. Every module in scope has its page in docs/ and its test.
6. No camera footage or pictures in the repository, and no file over 5 MB.
7. The unit tests pass.
8. In a git checkout: nothing changed or left untracked (everything is committed).

On the gateway, /opt/vivo-gateway has no .git, so every file there is checked (reports/ excluded).
"""
import argparse
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import testlib  # noqa: E402
import vivo  # noqa: E402

ROOT = vivo.ROOT
# Modules in scope (docs/ page prefix, test or tool that proves it)
MODULES = {
    "M1": "tests/m1_check.sh", "M2": "tests/m2_check.py", "M3": "tests/m3_reconnect.py", "M5": "tests/m5_check.py",
    "M6": "tests/m6_benchmark.py", "M7": "tests/m7_check.py", "M8": "bin/vivo-disk", "M9": "tests/m9_pressure.py",
    "M10": "tests/m10_recovery.py", "M11": "tests/m11_faults.py", "M12": "tests/m12_integrity.py",
    "M22": "tests/m22_check.py", "M25": "tests/m25_load.py", "M26": "tests/m26_matrix.py",
}
SECRET_FILES = re.compile(r"(^|/)(secrets\.env|site\.env|[^/]*\.local\.yaml|cameras\.lab\.yaml|id_[a-z0-9]+|[^/]*\.pem|[^/]*\.key)$")
FOOTAGE = re.compile(r"\.(mp4|mkv|mov|ts|m3u8|h264|h265|hevc|jpe?g|png|webp|webm)$", re.I)
# Not a literal secret: a template or shell expansion ($..., {...}), a mask, or an example word
PLACEHOLDER = re.compile(r"^(\$.*|\{.*\}|\*+|<[^>]*>|change-me|\.\.\.|x+|pass(word)?|secret|user(name)?|)$", re.I)
TOKENS = re.compile(r"ghp_[A-Za-z0-9]{36}|github_pat_[A-Za-z0-9_]{30,}|AKIA[0-9A-Z]{16}|xox[abprs]-[A-Za-z0-9-]{10,}"
                    r"|sk-[A-Za-z0-9_-]{20,}|AIza[0-9A-Za-z_-]{35}")
PRIVATE_KEY = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")
URL_LOGIN = re.compile(r"[a-z][a-z0-9+.-]*://([^:/@\s]+):([^@/\s]+)@")
# env and shell style (NAME=value, no spaces, so Python assignments do not count) and YAML style
PASSWORD_LINE = re.compile(r"^\s*(?:export\s+)?([A-Z0-9_]*(?:PASSWORD|SECRET|TOKEN)[A-Z0-9_]*)=[\"']?([^\"'\s#]*)", re.M)
YAML_SECRET = re.compile(r"^\s*(password|secret|token)\s*:\s*[\"']?([^\"'\s#]*)", re.M | re.I)
IMAGE = re.compile(r"^\s*image:\s*(\S+)", re.M)
PINNED_IMAGE = re.compile(r"^\$\{(\w+)_IMAGE\}:\$\{\1_VERSION\}@\$\{\1_DIGEST\}$")
MAX_BYTES = 5_000_000


def repo_files(root: Path) -> tuple[list[Path], bool]:
    """Files to check: git's list in a checkout, else everything but reports/ and caches."""
    res = subprocess.run(["git", "-C", str(root), "ls-files", "-z"], capture_output=True)
    if res.returncode == 0:
        return [root / p for p in res.stdout.decode().split("\0") if p], True
    skip = {"reports", "__pycache__", ".git"}
    return [p for p in root.rglob("*") if p.is_file() and not skip & set(p.relative_to(root).parts)], False


def read_text(path: Path) -> str | None:
    try:
        data = path.read_bytes()
    except OSError:
        return None
    return None if b"\0" in data[:4096] else data.decode(errors="replace")


def secret_findings(rel: str, text: str) -> list[str]:
    """What looks like a real secret in one file's text (pure, unit tested)."""
    out = []
    if PRIVATE_KEY.search(text):
        out.append(f"{rel}: private key")
    for m in URL_LOGIN.finditer(text):
        if not PLACEHOLDER.match(m[2]):                  # a user name alone is not secret
            out.append(f"{rel}: login inside a URL ({m[1][:2]}...)")
    for m in (*PASSWORD_LINE.finditer(text), *YAML_SECRET.finditer(text)):
        if not PLACEHOLDER.match(m[2]):
            out.append(f"{rel}: {m[1]} has a value")
    for m in TOKENS.finditer(text):
        out.append(f"{rel}: token {m[0][:4]}...")
    return out


def known_secret_findings(rel: str, text: str, secrets: dict[str, str]) -> list[str]:
    from urllib.parse import quote
    out = []
    for key, value in secrets.items():
        if len(value) >= 6 and any(w in key for w in ("PASSWORD", "SECRET", "TOKEN", "KEY")):
            if value in text or quote(value, safe="") in text:
                out.append(f"{rel}: the value of {key} from the secrets file ({len(value)} characters)")
    return out


def pin_findings(compose_texts: dict[str, str], versions: dict[str, str]) -> list[str]:
    """Images not pinned by version and digest, and loose versions (pure, unit tested)."""
    out = []
    for rel, text in compose_texts.items():
        for ref in IMAGE.findall(text):
            m = PINNED_IMAGE.match(ref.strip("\"'"))
            if not m:
                out.append(f"{rel}: image {ref} is not ${{X_IMAGE}}:${{X_VERSION}}@${{X_DIGEST}}")
                continue
            name = m[1]
            ver, digest = versions.get(f"{name}_VERSION", ""), versions.get(f"{name}_DIGEST", "")
            if not versions.get(f"{name}_IMAGE"):
                out.append(f"versions.env: {name}_IMAGE missing")
            if not re.search(r"\d", ver) or "latest" in ver.lower():
                out.append(f"versions.env: {name}_VERSION is {ver or 'missing'}")
            if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
                out.append(f"versions.env: {name}_DIGEST is not a sha256 digest")
    for key in ("UBUNTU_VERSION_ID", "DOCKER_CE_VERSION", "CONTAINERD_VERSION", "COMPOSE_PLUGIN_VERSION"):
        if not re.search(r"\d", versions.get(key, "")):
            out.append(f"versions.env: {key} is {versions.get(key) or 'missing'}")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--secrets", help="the real secrets file, to look for its values (on the gateway, with sudo)")
    ap.add_argument("--skip-unit-tests", action="store_true")
    a = ap.parse_args()

    files, in_git = repo_files(ROOT)
    rels = {p: p.relative_to(ROOT).as_posix() for p in files}
    texts = {rels[p]: t for p in files if (t := read_text(p)) is not None}
    r = testlib.Report()

    bad_files = [rel for rel in rels.values() if SECRET_FILES.search(rel)]
    r.check("no settings or key file in the repository", not bad_files, ", ".join(bad_files))
    found = [f for rel, t in texts.items() for f in secret_findings(rel, t)]
    r.check("no secret in any file (keys, URL logins, passwords, tokens)", not found, "; ".join(found[:10]))
    if a.secrets:
        secrets = vivo.read_env(Path(a.secrets))
        known = [f for rel, t in texts.items() for f in known_secret_findings(rel, t, secrets)]
        r.check(f"none of the real passwords in {a.secrets} appears in any file", bool(secrets) and not known,
                "; ".join(known[:10]) if secrets else f"{a.secrets} could not be read (sudo?)")

    versions = vivo.read_env(ROOT / "versions.env")
    compose = {rel: t for rel, t in texts.items() if re.search(r"(^|/)compose[^/]*\.ya?ml$", rel)}
    pins = pin_findings(compose, versions)
    r.check(f"every image pinned by version and digest ({sum(len(IMAGE.findall(t)) for t in compose.values())} in "
            f"{len(compose)} compose files), exact Docker versions", not pins, "; ".join(pins[:10]))

    gen = subprocess.run([sys.executable, str(ROOT / "tools" / "gen_config.py"), "--check"], capture_output=True, text=True)
    r.check("generated configs match cameras.yaml", gen.returncode == 0, (gen.stdout + gen.stderr).strip()[-200:])

    missing = []
    for module, test in MODULES.items():
        n = module[1:]
        if not any(rel.startswith(f"docs/m{n}-") and rel.endswith(".md") for rel in rels.values()):
            missing.append(f"{module} page in docs/")
        if test not in rels.values():
            missing.append(f"{module} {test}")
    r.check(f"every module in scope has its page and its test ({len(MODULES)} modules)", not missing, ", ".join(missing))

    footage = [rel for rel in rels.values() if FOOTAGE.search(rel)]
    big = [f"{rel} ({p.stat().st_size / 1e6:.1f} MB)" for p, rel in rels.items() if p.stat().st_size > MAX_BYTES]
    r.check("no camera footage or pictures, no file over 5 MB", not footage and not big, ", ".join(footage + big)[:300])

    if not a.skip_unit_tests:
        ut = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", str(ROOT / "tests" / "unit")],
                            capture_output=True, text=True, cwd=ROOT)
        last = (ut.stderr.strip().splitlines() or [""])[-1]
        ran = next((l for l in ut.stderr.splitlines() if l.startswith("Ran ")), "")
        r.check("unit tests pass", ut.returncode == 0, f"{ran}, {last}")

    if in_git:
        st = subprocess.run(["git", "-C", str(ROOT), "status", "--porcelain", "--untracked-files=all", "--", "."],
                            capture_output=True, text=True).stdout.splitlines()
        r.check("everything committed (nothing changed or untracked)", not st,
                f"{len(st)} files, e.g. {', '.join(l[3:] for l in st[:5])}" if st else "")

    md = ["# M26 handoff check", "", f"{vivo.utc(time.time())}. {len(files)} files in "
          f"{'the git checkout' if in_git else ROOT}.", ""] + r.markdown()
    path = vivo.write_report("handoff-check", "\n".join(md) + "\n")
    print("report:", path)
    return 0 if r.passed else 1


if __name__ == "__main__":
    sys.exit(main())
