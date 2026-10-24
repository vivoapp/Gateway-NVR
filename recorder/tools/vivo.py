"""Shared helpers for the gateway tools and tests (standard library + PyYAML only).

Reads the camera inventory, the site and secret settings, and talks to the
Frigate and go2rtc APIs on the gateway. Nothing here ever prints a secret:
use redact() on any text that may contain a camera URL.
"""
import base64
import json
import os
import re
import shutil
import subprocess
import time
import urllib.request
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
ETC = Path(os.environ.get("VIVO_ETC", "/etc/vivo"))
FRIGATE_API = os.environ.get("FRIGATE_API", "http://127.0.0.1:5000/api")
GO2RTC_API = os.environ.get("GO2RTC_API", "http://127.0.0.1:1984/api")
GO2RTC_RTSP = os.environ.get("GO2RTC_RTSP", "rtsp://127.0.0.1:8554")
REPORTS = ROOT / "reports"


def read_env(path: Path) -> dict:
    env = {}
    if not path.exists():
        return env
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip().strip('"').strip("'")
    return env


SITE = read_env(ETC / "site.env")
SECRETS = read_env(ETC / "secrets.env")


INVENTORY = Path(os.environ.get("VIVO_INVENTORY", ROOT / "cameras.yaml"))


def inventory(path: Path | None = None) -> dict:
    return yaml.safe_load((path or INVENTORY).read_text())


def cameras(path: Path | None = None) -> list:
    return inventory(path)["cameras"]


_URL_CRED = re.compile(r"([a-z]+://)[^:/@\s]+:[^@/\s]+@")


def redact(text: str) -> str:
    text = _URL_CRED.sub(r"\1***:***@", str(text))
    for k, v in SECRETS.items():
        if v and ("PASSWORD" in k or "USER" in k) and len(v) >= 3:
            text = text.replace(v, "***")
    return text


def _get(url: str, auth: tuple | None = None, timeout: float = 5.0):
    req = urllib.request.Request(url)
    if auth:
        token = base64.b64encode(f"{auth[0]}:{auth[1]}".encode()).decode()
        req.add_header("Authorization", f"Basic {token}")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = r.read()
    try:
        return json.loads(body)
    except ValueError:
        return body.decode(errors="replace")


def frigate(path: str, timeout: float = 5.0):
    return _get(f"{FRIGATE_API}{path}", timeout=timeout)


def go2rtc(path: str, timeout: float = 5.0):
    auth = (SECRETS.get("GO2RTC_API_USER", ""), SECRETS.get("GO2RTC_API_PASSWORD", ""))
    return _get(f"{GO2RTC_API}{path}", auth=auth if auth[0] else None, timeout=timeout)


def restream_url(stream: str) -> str:
    u = SECRETS.get("FRIGATE_GO2RTC_RTSP_USER", "")
    p = SECRETS.get("FRIGATE_GO2RTC_RTSP_PASSWORD", "")
    cred = f"{u}:{p}@" if u else ""
    return GO2RTC_RTSP.replace("rtsp://", f"rtsp://{cred}") + f"/{stream}"


def stream_bytes(stream: str) -> tuple[int, int]:
    """(number of producers, total bytes received) for a go2rtc stream."""
    info = go2rtc(f"/streams?src={stream}")
    producers = (info.get("producers") or []) if isinstance(info, dict) else []
    return len(producers), sum(int(p.get("bytes_recv", 0)) for p in producers)


def stream_alive(stream: str, window: float = 2.0) -> bool:
    """True if go2rtc received new bytes from the camera within `window` seconds."""
    try:
        _, b0 = stream_bytes(stream)
        time.sleep(window)
        _, b1 = stream_bytes(stream)
        return b1 > b0
    except Exception:
        return False


def newest_segment_end(cam: str, after: float) -> float | None:
    segs = frigate(f"/{cam}/recordings?after={int(after)}&before={int(time.time()) + 60}")
    ends = [s["end_time"] for s in segs] if isinstance(segs, list) else []
    return max(ends) if ends else None


def first_segment_start_after(cam: str, t: float) -> float | None:
    segs = frigate(f"/{cam}/recordings?after={int(t) - 30}&before={int(time.time()) + 60}")
    starts = [s["start_time"] for s in segs if s["start_time"] >= t] if isinstance(segs, list) else []
    return min(starts) if starts else None


def ffprobe(args: list, timeout: float = 30.0) -> str:
    """Run ffprobe inside the Frigate container (no extra tools on the host)."""
    if not shutil.which("docker"):
        raise RuntimeError("docker not found")
    script = 'p="$(command -v ffprobe || ls /usr/lib/ffmpeg/*/bin/ffprobe | sort -V | tail -1)"; exec "$p" "$@"'
    out = subprocess.run(["docker", "exec", "frigate", "sh", "-c", script, "ffprobe", *args],
                         capture_output=True, text=True, timeout=timeout)
    if out.returncode != 0:
        raise RuntimeError(redact(out.stderr.strip()[-400:]))
    return out.stdout


def utc(t: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(t))


def write_report(name: str, text: str) -> Path:
    REPORTS.mkdir(exist_ok=True)
    path = REPORTS / f"{name}-{time.strftime('%Y%m%d-%H%M%S', time.gmtime())}.md"
    path.write_text(redact(text))
    return path
