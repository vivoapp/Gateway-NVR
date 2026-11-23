#!/usr/bin/env python3
"""M16 measured on the gateway itself: first picture and seeks, with the footage never leaving it.

The workstation signs the test grants (the private key stays here) and sends each one over SSH input.
On the gateway, a throwaway container from the Frigate image already there (its ffmpeg, 1 CPU core)
plays the playback the Video API returns, through https://127.0.0.1:8443, and reports only times:

- first picture: from asking for the playback (POST) to the first decoded video frame, --starts times
- seeks: --seeks random points across the playback, done the way a player does it once it has the
  playlist: fetch the piece that holds the point and its header (init), and decode its first frame.
  The piece goes into a private temporary folder on the gateway, deleted at the end

This is the building-network figure (the gateway's own loopback, plus decoding); tests/m16_playback.mjs
measures a real browser over whatever network the workstation is on.

usage: tests/m16_probe.py --key KEYS/test-1.private.pem --aud vivo-gw:<GATEWAY_ID> --host vivo-n150
                          [--cam cam01] [--hours 24] [--starts 5] [--seeks 20] [--label gateway]
"""
from __future__ import annotations

import argparse
import json
import shlex
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import grant_issuer  # noqa: E402

PROBE = "vivo-m16-probe"
WORK = "/tmp/vivo-m16-probe"         # on the gateway, root only, removed at the end

# Runs on the gateway with the grant on stdin. Prints one JSON line with times only.
REMOTE = r"""
import json, random, ssl, subprocess, sys, time, urllib.request
cam, hours, seeks, probe, work = sys.argv[1], float(sys.argv[2]), int(sys.argv[3]), sys.argv[4], sys.argv[5]
tok = sys.stdin.readline().strip()
ctx, base = ssl._create_unverified_context(), "https://127.0.0.1:8443"
utc = lambda t: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))
def fetch(url):
    with urllib.request.urlopen(url, context=ctx, timeout=60) as r:
        return r.read()
def ffmpeg(*args):
    cmd = ["docker", "exec", probe, "sh", "-c",
           'f="$(command -v ffmpeg || ls /usr/lib/ffmpeg/*/bin/ffmpeg | sort -V | tail -1)"; exec "$f" "$@"', "ffmpeg",
           "-v", "error", "-nostdin", *args, "-map", "0:v:0", "-frames:v", "1", "-f", "null", "-"]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    return r.returncode == 0 and not r.stderr.strip(), r.stderr.strip()[-200:]
now = time.time()
req = urllib.request.Request(base + f"/v1/cameras/{cam}/playback", method="POST",
                             data=json.dumps({"start": utc(now - hours * 3600), "end": utc(now - 60)}).encode(),
                             headers={"Authorization": "Bearer " + tok, "Content-Type": "application/json"})
t0 = time.monotonic()
with urllib.request.urlopen(req, context=ctx, timeout=60) as r:
    pb = json.loads(r.read())
created = time.monotonic() - t0
url = base + pb["url"]
ok, err = ffmpeg("-i", url)
out = {"create": round(created, 3), "first": round(time.monotonic() - t0, 3), "first_ok": ok, "error": "" if ok else err}
if seeks:
    entries, off, init, dur = [], 0.0, None, 0.0
    for line in fetch(url).decode().splitlines():
        if line.startswith("#EXT-X-MAP:"):
            init = line.split('URI="', 1)[1].split('"', 1)[0]
        elif line.startswith("#EXTINF:"):
            dur = float(line.split(":", 1)[1].split(",", 1)[0])
        elif line and not line.startswith("#"):
            entries.append((off, line, init))
            off += dur
    folder = url.rsplit("/", 1)[0]
    res = []
    for p in sorted(random.uniform(0, max(0.0, off - 15)) for _ in range(seeks)):
        at, piece, head = max((e for e in entries if e[0] <= p), key=lambda e: e[0])
        t = time.monotonic()
        with open(f"{work}/seek.mp4", "wb") as fh:
            fh.write(fetch(f"{folder}/{head}") + fetch(f"{folder}/{piece}"))
        ok, err = ffmpeg("-i", "/x/seek.mp4")
        res.append([round(p, 1), round(time.monotonic() - t, 3), ok])
    out["seeks"], out["playlist_seconds"] = res, round(off, 1)
print(json.dumps(out))
"""


def ssh(host: str, cmd: str, stdin: str = "", timeout: float = 900) -> subprocess.CompletedProcess:
    return subprocess.run(["ssh", "-o", "BatchMode=yes", host, cmd], input=stdin, capture_output=True, text=True,
                          timeout=timeout)


def median(xs: list[float]) -> float | None:
    s = sorted(xs)
    if not s:
        return None
    return s[len(s) // 2] if len(s) % 2 else (s[len(s) // 2 - 1] + s[len(s) // 2]) / 2


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--key", required=True, type=Path)
    ap.add_argument("--aud", required=True)
    ap.add_argument("--host", required=True)
    ap.add_argument("--cam", default="cam01")
    ap.add_argument("--hours", type=float, default=24)
    ap.add_argument("--starts", type=int, default=5)
    ap.add_argument("--seeks", type=int, default=20)
    ap.add_argument("--label", default="gateway")
    a = ap.parse_args()
    key, kid = grant_issuer.load_key(a.key), grant_issuer.kid_of(a.key)

    start = ("image=$(docker inspect frigate --format '{{.Config.Image}}') && "
             f"docker rm -f {PROBE} >/dev/null 2>&1; rm -rf {WORK} && install -d -m 700 {WORK} && "
             f"docker run -d --rm --name {PROBE} --network host --cpus 1 -v {WORK}:/x:ro "
             "--entrypoint sleep \"$image\" 1800 >/dev/null && echo started")
    r = ssh(a.host, f"sudo -n bash -c {shlex.quote(start)}", timeout=120)
    if "started" not in r.stdout:
        print(f"could not start the probe container: {r.stderr.strip()[-200:]}")
        return 1
    started, firsts, creates, seeks, errors = time.time(), [], [], [], []
    try:
        for i in range(a.starts):
            tok = grant_issuer.sign(key, kid, a.aud, [a.cam], ["playback"], ttl=60)
            n = a.seeks if i == a.starts - 1 else 0
            cmd = f"sudo -n python3 -c {shlex.quote(REMOTE)} {shlex.quote(a.cam)} {a.hours} {n} {PROBE} {WORK}"
            r = ssh(a.host, cmd, stdin=tok + "\n")
            try:
                res = json.loads(r.stdout.strip().splitlines()[-1])
            except (ValueError, IndexError):
                errors.append((r.stderr.strip() or r.stdout.strip())[-200:])
                continue
            creates.append(res["create"])
            if res["first_ok"]:
                firsts.append(res["first"])
            else:
                errors.append(res["error"])
            seeks += res.get("seeks", [])
            print(f"start {i + 1}: create {res['create']:.2f} s, first picture {res['first']:.2f} s"
                  f"{'' if res['first_ok'] else ' FAILED'}", flush=True)
    finally:
        ssh(a.host, f"sudo -n docker rm -f {PROBE} >/dev/null 2>&1; sudo -n rm -rf {WORK}", timeout=60)

    ok_seeks = [s[1] for s in seeks if s[2] is True]
    first_ok = bool(firsts) and median(firsts) <= 3.0 and len(firsts) == a.starts
    seek_ok = bool(seeks) and len(ok_seeks) == len(seeks) and max(ok_seeks) <= 2.0
    lines = [f"# M16 probe on the gateway: {a.label}", "",
             f"Run {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(started))} on {a.host}, camera {a.cam}, "
             f"playback of the last {a.hours:g} h. Measured on the gateway (loopback to the Video API, ffmpeg from the "
             "Frigate image on 1 CPU core); the footage did not leave it.", "",
             "| Result | Check | Detail |", "|---|---|---|",
             f"| {'PASS' if first_ok else 'FAIL'} | first picture within 3 s (median of {a.starts}) | "
             f"median {median(firsts) or 0:.2f} s ({', '.join(f'{x:.2f}' for x in firsts)} s); creating the playback "
             f"median {median(creates) or 0:.2f} s |",
             f"| {'PASS' if seek_ok else 'FAIL'} | a seek anywhere in the playback plays within 2 s | "
             f"{len(ok_seeks)} of {len(seeks)} played, median {median(ok_seeks) or 0:.2f} s, slowest "
             f"{max(ok_seeks) if ok_seeks else 0:.2f} s (fetching the piece and its header, decoding its first frame) |"]
    if errors:
        lines += ["", "Errors:", ""] + [f"- {e}" for e in errors]
    out = ROOT / "reports" / f"m16-probe-{a.label}-{time.strftime('%Y%m%d-%H%M%S', time.gmtime())}.md"
    out.parent.mkdir(exist_ok=True)
    out.write_text("\n".join(lines) + "\n")
    print("\n".join(lines[6:8]))
    print(f"report: {out}")
    return 0 if first_ok and seek_ok else 1


if __name__ == "__main__":
    sys.exit(main())
