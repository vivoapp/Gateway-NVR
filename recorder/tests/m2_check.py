#!/usr/bin/env python3
"""M2 acceptance check: Frigate installation and configuration.

usage:
  sudo tests/m2_check.py               # config, recording, hardware decode, ports
  sudo tests/m2_check.py --kill-test   # also kill Frigate's main process and time the recovery

Run again after `sudo reboot` to prove Frigate comes back on its own.
"""
import argparse
import hashlib
import os
import subprocess
import sys
import time
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import vivo  # noqa: E402

rows: list[tuple[bool, str, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    rows.append((ok, name, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}  {detail}")
    return ok


def pct(v) -> float:
    try:
        return float(str(v).rstrip("%"))
    except (TypeError, ValueError):
        return 0.0


def sh(*cmd: str) -> str:
    return subprocess.run(cmd, capture_output=True, text=True).stdout.strip()


def wait_api(timeout: float = 300) -> float | None:
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            vivo.frigate("/version", timeout=2)
            return time.time() - t0
        except Exception:
            time.sleep(2)
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--kill-test", action="store_true")
    args = ap.parse_args()

    version = vivo.frigate("/version")
    want = vivo.read_env(vivo.ROOT / "versions.env")["FRIGATE_VERSION"]
    check("Frigate version", str(version).startswith(want), str(version))

    # The running config must be exactly the repository file.
    raw = vivo.frigate("/config/raw")
    repo = (vivo.ROOT / "frigate" / "config.yml").read_text()
    same = yaml.safe_load(str(raw)) == yaml.safe_load(repo)
    digest = hashlib.sha256(repo.encode()).hexdigest()[:12]
    check("running config is the repository file", same,
          f"same content as frigate/config.yml ({digest})" if same else "differs from frigate/config.yml")

    cfg = vivo.frigate("/config")
    cams = cfg.get("cameras", {})
    inv_ids = [c["id"] for c in vivo.cameras()]
    check("all inventory cameras configured", sorted(cams) == sorted(inv_ids), ", ".join(sorted(cams)))
    check("recording on for every camera", all(c["record"]["enabled"] for c in cams.values()))
    check("detection off for every camera", not any(c["detect"]["enabled"] for c in cams.values()))
    ai_off = {k: (cfg.get(k) or {}).get("enabled") for k in ("semantic_search", "face_recognition", "lpr", "audio", "birdseye")}
    check("AI and extra features off", not any(ai_off.values()), str(ai_off))
    hw = {n: c["ffmpeg"]["hwaccel_args"] for n, c in cams.items()}
    check("hardware decoding configured (Intel VAAPI/QSV)", all(("vaapi" in str(v) or "qsv" in str(v)) for v in hw.values()),
          str(set(map(str, hw.values()))))

    # Hardware decode really used: Frigate's own GPU stats, and low ffmpeg CPU.
    stats = vivo.frigate("/stats")
    gpus = stats.get("gpu_usages", {})
    check("Intel GPU busy (Frigate GPU stats)", any(pct(g.get("gpu")) > 0 for g in gpus.values()),
          str({k: g.get("gpu") for k, g in gpus.items()}))
    cpu = stats.get("cpu_usages", {})
    ff = {n: cpu.get(str(c.get("ffmpeg_pid")), {}).get("cpu") for n, c in stats.get("cameras", {}).items()}
    check("detect-stream ffmpeg CPU per camera (low = hardware decode)", True, str(ff))
    # A decoder that keeps crashing still shows fps between restarts, so count the restarts.
    out = subprocess.run(["docker", "logs", "--since", "10m", "frigate"], capture_output=True, text=True)
    logs = out.stdout + out.stderr
    crashes = [l for l in logs.splitlines() if "process crashed unexpectedly" in l]
    by_cam = sorted({l.split("for ")[-1].strip(" .") for l in crashes})
    check("no ffmpeg crashes in the last 10 minutes", not crashes, f"{len(crashes)} crash(es): {', '.join(by_cam)}" if crashes else "")

    # Recordings land on the recording disk.
    media = Path(vivo.SITE.get("MEDIA_ROOT", "/srv/vivo")) / "frigate" / "media" / "recordings"
    newest = sh("sh", "-c", f"find '{media}' -name '*.mp4' -mmin -3 | head -1")
    check("new segments on the recording SSD", bool(newest), newest.replace(str(media), "recordings"))

    # Ports: UI on the LAN, unauthenticated API on localhost only.
    listen = sh("ss", "-ltnH")
    check("port 5000 bound to 127.0.0.1 only", "127.0.0.1:5000" in listen and "0.0.0.0:5000" not in listen)
    check("port 8971 listening (LAN, filtered by DOCKER-USER)", ":8971" in listen)

    restarts_before = sh("docker", "inspect", "-f", "{{.RestartCount}}", "frigate")
    if args.kill_test:
        pid = sh("docker", "inspect", "-f", "{{.State.Pid}}", "frigate")
        t_kill = time.time()
        subprocess.run(["kill", "-9", pid], check=True)  # host-side kill: Docker treats it as a crash
        back = wait_api()
        restarts_after = sh("docker", "inspect", "-f", "{{.RestartCount}}", "frigate")
        check("Frigate restarts on its own after its process is killed",
              back is not None and restarts_after != restarts_before,
              f"API back after {back:.0f} s, restart count {restarts_before} -> {restarts_after}" if back else "API not back in 300 s")
        # Recording resumes on every camera.
        time.sleep(30)
        lines = []
        for cam in inv_ids:
            first = vivo.first_segment_start_after(cam, t_kill)
            lines.append(f"{cam}: {('+%.0f s' % (first - t_kill)) if first else 'none'}")
        check("recording resumed on every camera", all("none" not in s for s in lines), "; ".join(lines))

    md = ["# M2 Frigate installation and configuration: check", "",
          f"Run at {vivo.utc(time.time())} on {os.uname().nodename}.", "",
          f"Result: **{sum(r[0] for r in rows)} passed, {sum(not r[0] for r in rows)} failed**", "",
          "| Result | Check | Detail |", "|---|---|---|"]
    md += [f"| {'PASS' if ok else '**FAIL**'} | {n} | {d.replace('|', '/')} |" for ok, n, d in rows]
    md += ["", "Reboot proof: `sudo reboot`, then run this check again without any manual step."]
    print("report:", vivo.write_report("m2-check", "\n".join(md) + "\n"))
    return 0 if all(r[0] for r in rows) else 1


if __name__ == "__main__":
    sys.exit(main())
