#!/usr/bin/env python3
"""M17 acceptance check against a running Video API (the lab or a gateway).

- a 5-minute clip is ready within 30 s, downloads as a whole MP4 and plays for about 5 minutes
- a clip over 30 minutes is refused (413)
- three clips at once: two are prepared, the third waits in the queue, all finish
- with --keep SECONDS (the API's CLIP_KEEP, short in the lab): the finished clip is deleted after it

Writes reports/m17-clips-<label>-<UTC>.md.

usage: tests/m17_clips.py --key KEYS/test-1.private.pem --aud vivo-gw:lab [--api https://127.0.0.1:8443]
                          [--camera cam01] [--keep 60] [--label lab] [--check-on vivo-n150]

--check-on HOST: on a gateway, the clip is downloaded and checked on the gateway itself over SSH, so no
footage leaves it (the scope's rule). Only the results come back. It needs passwordless sudo there,
and decodes the clip with the ffmpeg in the Frigate image already on the gateway.
"""
from __future__ import annotations

import argparse
import json
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT))
import grant_issuer  # noqa: E402
from m14_check import Report, utc  # noqa: E402
from vivo_api import mp4  # noqa: E402


def recorded_stretch(c, h, cam: str, need: float) -> float | None:
    """Start of the newest stretch of at least `need` seconds of recording in the last 6 hours."""
    now = time.time() - 30
    r = c.get(f"/v1/cameras/{cam}/timeline", params={"start": utc(now - 6 * 3600), "end": utc(now)}, headers=h)
    from datetime import datetime
    ts = lambda s: datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()  # noqa: E731
    for p in reversed(r.json().get("recorded", []) if r.status_code == 200 else []):
        if ts(p["end"]) - ts(p["start"]) >= need:
            return ts(p["end"]) - need
    return None


def wait(c, h, cid: str, timeout: float = 120) -> tuple[dict, float]:
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        b = c.get(f"/v1/clips/{cid}", headers=h).json()
        if b.get("status") in ("ready", "failed"):
            return b, time.monotonic() - t0
        time.sleep(0.25)
    return b, time.monotonic() - t0


REMOTE_CHECK = r"""
set -eu
d="$(mktemp -d /tmp/vivo-m17-XXXXXX)"; trap 'rm -rf "$d"' EXIT
umask 077; cat > "$d/header"
code="$(curl -sk -o "$d/clip.mp4" -w '%{http_code}' -H @"$d/header" "https://127.0.0.1:8443/v1/clips/$1/download")"
size="$(stat -c %s "$d/clip.mp4" 2>/dev/null || echo 0)"
box="$(python3 -c 'import sys; sys.path.insert(0, "/opt/vivo-gateway/tools"); import pathlib, mp4box; print(mp4box.cut_short(pathlib.Path(sys.argv[1])) or "ok")' "$d/clip.mp4" 2>&1 | tail -1)"
image="$(docker inspect frigate --format '{{.Config.Image}}')"
probe='p="$(command -v ffprobe || ls /usr/lib/ffmpeg/*/bin/ffprobe | sort -V | tail -1)"; f="$(dirname "$p")/ffmpeg";
  "$p" -v error -show_entries format=duration:stream=codec_name -of compact=p=0:nk=1 /x/clip.mp4 | tr "\n" " "; echo;
  "$f" -v error -i /x/clip.mp4 -f null - 2>&1 | wc -l'
out="$(docker run --rm --network none --cpus 2 -v "$d:/x:ro" --entrypoint sh "$image" -c "$probe" 2>&1 || true)"
printf 'http=%s\nsize=%s\nbox=%s\nprobe=%s\nerrors=%s\n' "$code" "$size" "$box" "$(echo "$out" | head -1)" "$(echo "$out" | tail -1)"
"""


def check_on_gateway(host: str, cid: str, auth: str) -> dict:
    """Download clip cid on the gateway and check it there; the grant goes over stdin, never on a command line."""
    cmd = f"sudo -n bash -c {shlex.quote(REMOTE_CHECK)} m17 {shlex.quote(cid)}"
    try:
        out = subprocess.run(["ssh", "-o", "BatchMode=yes", host, cmd], input=f"Authorization: {auth}\n",
                             capture_output=True, text=True, timeout=600)
    except subprocess.TimeoutExpired:
        return {"error": "the check on the gateway took over 10 minutes"}
    kv = dict(line.split("=", 1) for line in out.stdout.splitlines() if "=" in line)
    if out.returncode != 0 or "http" not in kv:
        return {"error": (out.stderr.strip().splitlines() or ["ssh failed"])[-1][:160]}
    parts = kv.get("probe", "").split()
    codecs = ",".join(p for p in parts if not p.replace(".", "", 1).isdigit())
    duration = next((p for p in parts if p.replace(".", "", 1).isdigit()), "0")
    errors = kv.get("errors", "").strip()
    return {"http": kv["http"].strip(), "size": int(kv.get("size", "0") or 0), "box": kv.get("box", "").strip(),
            "duration": duration, "codecs": codecs or "?", "decode_errors": int(errors) if errors.isdigit() else -1}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--api", default="https://127.0.0.1:8443")
    ap.add_argument("--key", required=True, type=Path)
    ap.add_argument("--aud", required=True)
    ap.add_argument("--camera", default="cam01")
    ap.add_argument("--keep", type=int, help="the API's CLIP_KEEP; checks that the clip is deleted after it")
    ap.add_argument("--label", default="lab")
    ap.add_argument("--check-on", help="SSH host: download and check the clip there, not here")
    a = ap.parse_args()
    key, kid, cam = grant_issuer.load_key(a.key), grant_issuer.kid_of(a.key), a.camera
    h = lambda: {"Authorization": "Bearer " + grant_issuer.sign(key, kid, a.aud, [cam], ["export", "playback"], ttl=60)}  # noqa: E731
    c = httpx.Client(base_url=a.api, verify=False, timeout=120)
    rep = Report()
    started = time.time()

    s = recorded_stretch(c, h(), cam, 330)
    if s is None:
        rep.check("5.5 minutes of continuous recording to cut from", False, "none in the last 6 hours")
        return finish(rep, a, started)
    body = {"start": utc(s + 15), "end": utc(s + 315)}
    t0 = time.monotonic()
    r = c.post(f"/v1/cameras/{cam}/clips", json=body, headers=h())
    if not rep.check("5-minute clip accepted", r.status_code == 202, f"HTTP {r.status_code}"):
        return finish(rep, a, started)
    cid = r.json()["clip_id"]
    b, _ = wait(c, h(), cid)
    took = time.monotonic() - t0
    rep.check("5-minute clip ready within 30 s", b.get("status") == "ready" and took <= 30,
              f"{b.get('status')} after {took:.1f} s, {b.get('size_bytes') or 0:,} bytes")
    if a.check_on:
        res = check_on_gateway(a.check_on, cid, h()["Authorization"])
        rep.check("download is a whole MP4", res.get("http") == "200" and res.get("box") == "ok"
                  and res.get("size") == b.get("size_bytes"),
                  res.get("error") or f"{res.get('size', 0):,} bytes, checked on {a.check_on}, box check {res.get('box')}")
        dur, errors = float(res.get("duration") or 0), int(res.get("decode_errors", -1))
        rep.check("clip plays for the asked 5 minutes, every frame decodes", 290 <= dur <= 315 and errors == 0,
                  f"{dur:.1f} s, {res.get('codecs', '?')}, {errors} decode errors (on {a.check_on}; the clip never left it)")
    else:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "clip.mp4"
            d = c.get(f"/v1/clips/{cid}/download", headers=h())
            out.write_bytes(d.content)
            bad = mp4.problem(out)
            rep.check("download is a whole MP4", d.status_code == 200 and bad is None and len(d.content) == b.get("size_bytes"),
                      bad or f"{len(d.content):,} bytes, {d.headers.get('content-disposition', '')}")
            if shutil.which("ffprobe"):
                info = json.loads(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration:stream=codec_name",
                                                  "-of", "json", str(out)], capture_output=True, text=True).stdout or "{}")
                dur = float(info.get("format", {}).get("duration", 0))
                codecs = ",".join(x.get("codec_name", "?") for x in info.get("streams", []))
                dec = subprocess.run(["ffmpeg", "-v", "error", "-i", str(out), "-f", "null", "-"], capture_output=True, text=True)
                rep.check("clip plays for the asked 5 minutes, every frame decodes", 290 <= dur <= 310 and not dec.stderr.strip(),
                          f"{dur:.1f} s, {codecs}, {len(dec.stderr.strip().splitlines())} decode errors")

    long = {"start": utc(s), "end": utc(s + 1801)}
    r = c.post(f"/v1/cameras/{cam}/clips", json=long, headers=h())
    rep.check("clip over 30 minutes refused", r.status_code == 413, f"HTTP {r.status_code}")

    short = {"start": utc(s + 15), "end": utc(s + 135)}
    ids, positions = [], []
    for _ in range(3):
        r = c.post(f"/v1/cameras/{cam}/clips", json=short, headers=h())
        if r.status_code == 202:
            ids.append(r.json()["clip_id"])
            positions.append(r.json()["queue_position"])
    states = [c.get(f"/v1/clips/{i}", headers=h()).json().get("status") for i in ids]
    rep.check("three at once: at most two prepared, the rest queued", len(ids) == 3 and states.count("running") <= 2,
              f"states right after asking: {states}, queue positions {positions}")
    done = [wait(c, h(), i)[0].get("status") for i in ids]
    rep.check("all three finish", done == ["ready"] * 3, f"{done}")
    for i in ids:
        c.delete(f"/v1/clips/{i}", headers=h())
    rep.check("a deleted clip is gone", all(c.get(f"/v1/clips/{i}", headers=h()).status_code == 404 for i in ids), "")

    if a.keep:
        time.sleep(a.keep + 35)                      # the sweeper runs every 30 s
        r = c.get(f"/v1/clips/{cid}", headers=h())
        rep.check(f"finished clip deleted after CLIP_KEEP ({a.keep} s)", r.status_code == 404, f"HTTP {r.status_code}")
    return finish(rep, a, started)


def finish(rep: Report, a, started: float) -> int:
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    out = ROOT / "reports" / f"m17-clips-{a.label}-{stamp}.md"
    out.parent.mkdir(exist_ok=True)
    out.write_text(rep.markdown(f"M17 clips: {a.label}", f"Run {utc(started)} against {a.api}, camera {a.camera}."))
    print(f"report: {out}")
    return 0 if rep.rows and all(ok for _, ok, _ in rep.rows) else 1


if __name__ == "__main__":
    sys.exit(main())
