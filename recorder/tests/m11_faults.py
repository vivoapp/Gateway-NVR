#!/usr/bin/env python3
"""M11 acceptance: power loss, full disk, missing disk and a corrupted segment.

usage:
  tests/m11_faults.py power --host vivo-n150 --count 10 \\
      --plug-off "curl -s http://PLUG/relay/0?turn=off" --plug-on "curl -s http://PLUG/relay/0?turn=on"
                                                   # from a workstation; without --plug-*, asks an operator
  tests/m11_faults.py power --host vivo-n150 --count 5 --crash
                                                   # no plug or operator: reboots at once without saving (sysrq b)
  sudo tests/m11_faults.py full-disk              # on the gateway: tests/m9_pressure.py, then recovery checks
  sudo tests/m11_faults.py missing-disk --yes      # on the gateway: the recording disk goes away and comes back
  sudo tests/m11_faults.py corrupt --camera cam01  # on the gateway: one damaged segment, playback around it
  sudo tests/m11_faults.py report --events FILE    # power-cut report from recorded events (power runs it itself)

power         cuts the gateway's power with the smart plug, waits --off seconds, powers it on, and waits for
              recording. Passes when the gateway boots and records again within 180 s of power coming back,
              and at most the segment being written is lost: up to 25 s of main stream (one 10 s segment plus
              Frigate's cache) and 65 s of substream (one 60 s .part file). A file the cut left empty or
              without its index counts as lost, and the report lists it.
              --crash reboots at once without writing out what Linux still holds in memory, which is
              what a power cut does to unsaved writes; the SSD keeps power, so its own cache is not tested.
full-disk     the M9 cleanup protects the system (oldest footage goes first at the safe limit) and, after
              the filler file is removed, Frigate, both streams and the filesystem are healthy.
missing-disk  stops the recorder, unmounts the recording disk and switches its LVM volume off ("disk gone"):
              the recorder must refuse to start rather than write to the OS disk, the health endpoint must
              show the missing disk, and when the volume is switched on again vivo-disk-watch.timer must
              start everything without anyone logging in. Recording stops for a few minutes: announce it.
corrupt       truncates one main-stream segment from about 20 minutes ago, picked in a stretch without gaps
              (a copy is kept and put back at the end): the damaged file is reported broken (its MP4 boxes
              no longer add up to its size), and a clip and the HLS playlist across it still play.
"""
import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import continuity  # noqa: E402
import health  # noqa: E402
import testlib  # noqa: E402
import vivo  # noqa: E402
from substream_archiver import segment_name  # noqa: E402

LIMITS = {"power": 180}
MAIN_LOSS = 25
SUB_LOSS = 65
MEDIA = Path(vivo.SITE.get("MEDIA_ROOT", "/srv/vivo"))


def sh(cmd: str, timeout: float = 120) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)


# ------------------------------------------------------------------ power loss (from a workstation)

def plug(cmd: str | None, on: bool) -> None:
    if cmd:
        res = sh(cmd, 30)
        if res.returncode:
            raise RuntimeError(f"plug command failed: {res.stderr.strip()[-200:]}")
    else:
        input(f">>> switch the gateway's power {'ON' if on else 'OFF'} now, then press Enter ")


def crash(remote: testlib.Remote) -> None:
    """Reboot at once without writing anything out (sysrq b); the SSH session dies with it."""
    try:
        remote.run("sudo -n sh -c 'echo b > /proc/sysrq-trigger'", timeout=10)
    except subprocess.TimeoutExpired:
        pass


def run_power(host: str, count: int, off: float, pause: float, plug_off: str | None, plug_on: str | None,
              crash_only: bool = False) -> list[dict]:
    remote = testlib.Remote(host)
    if not remote.up():
        raise RuntimeError(f"cannot reach {host} over SSH")
    events = []
    for n in range(1, count + 1):
        t_cut, offset = remote.clock()
        if crash_only:
            crash(remote)
            remote.wait_down(30)
            t_on = t_cut                                 # it starts again at once
        else:
            plug(plug_off, on=False)
            remote.wait_down(30)
            time.sleep(off)
            plug(plug_on, on=True)
            t_on = time.time() + offset                  # on the gateway's clock
        up = remote.wait_up(600)
        print(f"power cut {n}/{count} at {vivo.utc(t_cut)}, power on {vivo.utc(t_on)}, "
              f"{'SSH back after %.0f s' % (up + offset - t_on) if up else 'SSH NOT back'}", flush=True)
        how = "reboot without saving (sysrq b)" if crash_only else f"{off:g} s off"
        events.append({"kind": "power", "n": n, "camera": "all", "t_event": f"{t_cut:.3f}", "t_restore": f"{t_on:.3f}",
                       "note": how + ("" if up else "; SSH did not come back")})
        if not up:
            break
        remote.wait_recording(t_cut, LIMITS["power"] + 180)
        time.sleep(pause)
    return events


def damage(path: Path) -> str | None:
    if not path.exists():
        return "file missing"
    return "empty file" if path.stat().st_size == 0 else testlib.mp4_cut_short(path)


def segment_files(cam: str, stream: str, a: float, b: float) -> list[tuple[float, float, Path]]:
    if stream == "main":                                 # Frigate's index
        return [(s, e, MEDIA / "frigate" / "media" / "recordings" / seg_rel(cam, s)) for s, e in testlib.main_segments(cam, a, b)]
    root = testlib.substream_root()                      # file names
    return [(s, e, root / cam / time.strftime("%Y-%m-%d", time.gmtime(s)) / segment_name(cam, s, e))
            for s, e in testlib.substream_segments(cam, a, b, root)]


def playable_segments(cam: str, stream: str, a: float, b: float) -> list[tuple[float, float]]:
    """Segments that play. A file a cut left empty or without its index (written but not yet on the
    disk) is in the index or has its final name, but its video is lost, so it counts as a gap."""
    return [(s, e) for s, e, p in segment_files(cam, stream, a, b) if not damage(p)]


def power_report(events: list[dict], planned: list[str]) -> int:
    cams = vivo.services_config()["cameras"]
    res = testlib.analyze(events, cams, LIMITS, [testlib.parse_window(w) for w in planned], segments=playable_segments)
    damaged = sorted({(cam, stream, str(p), why) for e in events for cam in cams for stream in ("main", "substream")
                      for s, _, p in segment_files(cam, stream, e["t_event"] - 120, e["t_event"] + 5)
                      if (why := damage(p))})
    r = testlib.Report()
    rows = res["events"]
    r.check(f"boots and records again within {LIMITS['power']} s of power coming back, every time",
            all(x["main_recovery_s"] <= LIMITS["power"] for x in rows),
            f"slowest main stream {max((x['main_recovery_s'] for x in rows), default=0):.0f} s, substream "
            f"{max((x['substream_recovery_s'] for x in rows), default=0):.0f} s after power on")
    worst_main = max((x["main_lost_before_s"] for x in rows), default=0)
    worst_sub = max((x["substream_lost_before_s"] for x in rows), default=0)
    r.check(f"at most the segment being written is lost (main {MAIN_LOSS} s, substream {SUB_LOSS} s)",
            worst_main <= MAIN_LOSS and worst_sub <= SUB_LOSS, f"lost before the cut: main up to {worst_main:.0f} s, substream up to {worst_sub:.0f} s")
    # No partial file survives as a finished segment: nothing may span a cut.
    spans = []
    for e in events:
        for cam in cams:
            spans += [f"{cam} {vivo.utc(s)}" for s, en in testlib.substream_segments(cam, e["t_event"] - 120, e["t_event"] + 120)
                      if s < e["t_event"] - 2 and en > e["t_event"] + 2]
    r.check("no substream segment was finished across a power cut", not spans, ", ".join(spans[:5]))
    errors = health.fs_errors(MEDIA)
    r.check("recording filesystem: no errors logged by ext4", errors == 0, f"errors_count {errors}")
    unexplained = [g for g in res["gaps"] if g["cause"] == "UNEXPLAINED"]
    r.check("no unexplained gap", not unexplained, f"{len(unexplained)} unexplained of {len(res['gaps'])}")
    indexed = [d for d in damaged if d[1] == "main"]
    r.check("Frigate's index lists no broken file near a cut (a clip or playback would stop there)", not indexed,
            f"{len(indexed)} listed" + (f", e.g. {indexed[0][2]} ({indexed[0][3]})" if indexed else ""))
    how = "; ".join(sorted({e["note"] for e in events}))
    md = ["# M11 power-loss test", "", f"{len(events)} power cuts ({how}).", ""] + r.markdown()
    md += ["", "## Per cut and camera", "", "| Cut | Camera | Cut at (UTC) | Main back after power on | Substream back | "
           "Main lost before the cut | Substream lost | Result |", "|---|---|---|---|---|---|---|---|"]
    md += [f"| {x['event']} | {x['camera']} | {vivo.utc(x['at'])} | {x['main_recovery_s']:.0f} s | {x['substream_recovery_s']:.0f} s | "
           f"{x['main_lost_before_s']:.0f} s | {x['substream_lost_before_s']:.0f} s | "
           f"{'PASS' if x['main_recovery_s'] <= LIMITS['power'] and x['main_lost_before_s'] <= MAIN_LOSS and x['substream_lost_before_s'] <= SUB_LOSS else '**FAIL**'} |"
           for x in rows]
    md += ["", "## Damaged files near a cut", "",
           "Files from the 2 minutes before each cut that are listed but do not play; they count as lost above.", ""]
    md += ["| Camera | Stream | File | Problem |", "|---|---|---|---|"] + [
        f"| {cam} | {stream} | {p} | {why} |" for cam, stream, p, why in damaged] if damaged else ["None."]
    md += ["", "## Gap log", ""] + testlib.gap_table(res["gaps"])
    print("report:", vivo.write_report("m11-power", "\n".join(md) + "\n"))
    return 0 if r.passed else 1


# ------------------------------------------------------------------ full disk

def recording_fresh(r: testlib.Report, label: str) -> None:
    t = time.time() - 90
    after = testlib.wait_until(lambda: all(v["main"] and v["substream"] for v in testlib.recording_after(t).values()), 240, 10)
    state = testlib.recording_after(t)
    missing = [c for c, v in state.items() if not (v["main"] and v["substream"])]
    r.check(f"{label}: every camera records both streams", after is not None, f"not recording: {', '.join(missing)}" if missing else "")


def run_full_disk() -> int:
    r = testlib.Report()
    pressure = subprocess.run([sys.executable, str(Path(__file__).parent / "m9_pressure.py")], capture_output=True, text=True)
    print(pressure.stdout[-3000:])
    path = next((l.split("report:", 1)[1].strip() for l in pressure.stdout.splitlines() if l.startswith("report:")), "")
    r.check("M9 cleanup protects the system at the safe limit (tests/m9_pressure.py)", pressure.returncode == 0,
            Path(path).name if path else pressure.stderr.strip()[-200:])
    try:
        vivo.frigate("/version")
        up = True
    except Exception:
        up = False
    r.check("after the filler is removed: Frigate answers", up)
    recording_fresh(r, "after the filler is removed")
    r.check("recording filesystem: no errors logged by ext4", health.fs_errors(MEDIA) == 0, f"errors_count {health.fs_errors(MEDIA)}")
    root_used = shutil.disk_usage("/")
    r.check("OS disk not affected", root_used.used / root_used.total < 0.9, f"{100 * root_used.used / root_used.total:.0f}% used")
    md = ["# M11 full-disk test", "", "The disk-pressure test fills the recording disk to just above its safe limit; the "
          "storage guard deletes the oldest footage first; the filler is then removed and the system checked.", ""] + r.markdown()
    print("report:", vivo.write_report("m11-full-disk", "\n".join(md) + "\n"))
    return 0 if r.passed else 1


# ------------------------------------------------------------------ missing disk

def health_alerts() -> list[str]:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{health.PORT}/health", timeout=5) as f:
            return [f"{a['alert']}:{a['target']}" for a in json.load(f).get("alerts", [])]
    except Exception as e:
        return [f"health endpoint not reachable ({e})"]


def run_missing_disk(missing_s: float) -> int:
    r = testlib.Report()
    if not os.path.ismount(MEDIA):
        print(f"ERROR: {MEDIA} is not mounted now", file=sys.stderr)
        return 2
    source = sh(f"findmnt -n -o SOURCE --mountpoint {shlex.quote(str(MEDIA))}").stdout.strip()
    lv = sh(f"lvs --noheadings -o vg_name,lv_name {shlex.quote(source)}").stdout.split()
    if len(lv) != 2:
        print(f"ERROR: this test switches the recording disk's LVM volume off and on; {source} is not on LVM. "
              "For a plain disk, unplug it by hand and follow docs/m11-faults.md.", file=sys.stderr)
        return 2
    vg_lv = f"{lv[0]}/{lv[1]}"
    maint = vivo.ETC / "maintenance"
    log: list[str] = []

    def step(cmd: str) -> subprocess.CompletedProcess:
        res = sh(cmd)
        log.append(f"$ {cmd}  -> {res.returncode} {(res.stdout + res.stderr).strip()[-160:]}")
        print(log[-1], flush=True)
        return res

    lv_off = False
    t_gone = t_back = time.time()
    err = ""
    try:
        try:
            maint.write_text("m11 missing-disk test: taking the disk away\n")   # the watcher waits while we set up
            step("systemctl stop vivo-gateway.service")
            step(f"umount {shlex.quote(str(MEDIA))}")
            gone = step(f"lvchange -an {shlex.quote(vg_lv)}")
            lv_off = gone.returncode == 0
            r.check("recording disk gone (volume off)", lv_off and not os.path.ismount(MEDIA), source)
        finally:
            maint.unlink(missing_ok=True)
        t_gone = time.time()
        start = step("systemctl start vivo-gateway.service")
        mount_unit = sh(f"systemd-escape -p --suffix=mount {shlex.quote(str(MEDIA))}").stdout.strip()
        err = sh(f"journalctl -u vivo-gateway.service -u {shlex.quote(mount_unit)} --since @{int(t_gone)} --no-pager -o cat "
                 "| tail -5").stdout.strip()
        running = sh("docker ps --format '{{.Names}}'").stdout.split()
        r.check("the recorder refuses to start without its disk", start.returncode != 0 and "frigate" not in running,
                err.replace("\n", " / ")[-220:] or "no error text")
        written = list(MEDIA.iterdir()) if MEDIA.is_dir() else []
        r.check("nothing written to the OS disk under the mount point", not written, ", ".join(p.name for p in written[:5]))
        time.sleep(40)                                   # health collects every 10 s
        alerts = health_alerts()
        r.check("health endpoint shows the missing disk", any(a.startswith("disk_missing") for a in alerts), ", ".join(alerts[:6]))
        time.sleep(max(0.0, missing_s - (time.time() - t_gone)))
        t_back = time.time()
        back = step(f"lvchange -ay {shlex.quote(vg_lv)}")
        lv_off = back.returncode != 0
        print("volume back on; waiting for vivo-disk-watch.timer to mount it and start the recorder", flush=True)
        active = testlib.wait_until(lambda: sh("systemctl is-active vivo-gateway.service").stdout.strip() == "active"
                                    and os.path.ismount(MEDIA), 180, 5)
        r.check("recorder back on its own after the disk returns (no login)", active is not None,
                f"{active - t_back:.0f} s after the volume came back" if active else "not back within 180 s")
        if active:
            recording_fresh(r, "after the disk returned")
    finally:
        # Whatever happened above (an error, Ctrl-C, a lost SSH session): never leave the disk off or
        # the recorder stopped. These steps are a safety net; the checks above are what the test measures.
        maint.unlink(missing_ok=True)
        if lv_off:
            step(f"lvchange -ay {shlex.quote(vg_lv)}")
        if sh("systemctl is-active vivo-gateway.service").stdout.strip() != "active":
            step("systemctl start vivo-gateway.service")
            log.append("(safety net: the recorder was started by the test, not by vivo-disk-watch)")
    md = ["# M11 missing-disk test", "", f"Recording disk `{MEDIA}` ({source}, LVM volume {vg_lv}) taken away at "
          f"{vivo.utc(t_gone)} and given back at {vivo.utc(t_back)}.", ""] + r.markdown()
    md += ["", "## Steps", "", "```"] + log + ["```", "", f"Recorder log while the disk was missing: `{err[-400:]}`"]
    print("report:", vivo.write_report("m11-missing-disk", "\n".join(md) + "\n"))
    return 0 if r.passed else 1


# ------------------------------------------------------------------ corrupted segment

def probe(path: str) -> tuple[bool, str]:
    try:
        out = vivo.ffprobe(["-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path], timeout=60)
        dur = float(out.strip() or 0)
        return dur > 0, f"plays, {dur:.1f} s"
    except Exception as e:
        return False, str(e).strip().splitlines()[-1][:160] if str(e).strip() else "ffprobe failed"


def clip_length(cam: str, a: float, b: float) -> tuple[float, str]:
    url = f"http://127.0.0.1:5000/api/{cam}/start/{int(a)}/end/{int(b)}/clip.mp4"
    try:
        out = vivo.ffprobe(["-v", "error", "-select_streams", "v:0", "-show_entries", "packet=pts_time", "-of", "csv=p=0", url],
                           timeout=180)
    except Exception as e:
        return 0.0, str(e)[:160]
    times = [float(x.strip(",")) for x in out.split() if x.strip(",").replace(".", "", 1).replace("-", "", 1).isdigit()]
    return (max(times) - min(times) if len(times) > 1 else 0.0), f"{len(times)} frames"


def seg_rel(cam: str, start: float) -> str:
    return time.strftime(f"%Y-%m-%d/%H/{cam}/%M.%S.mp4", time.gmtime(start))


def playable(rel: str) -> tuple[bool, str]:
    """A segment plays: ffprobe reads it and its MP4 boxes add up to its size."""
    host = MEDIA / "frigate" / "media" / "recordings" / rel
    if not host.exists() or host.stat().st_size == 0:
        return False, "file missing" if not host.exists() else "empty file"
    layout = testlib.mp4_cut_short(host)
    ok, detail = probe(f"/media/frigate/recordings/{rel}")
    return ok and not layout, layout or detail


def clean_segment(cam: str, segs: list[dict]) -> int | None:
    """Index of a segment, nearest the middle, whose two minutes around it have no gap and whose
    file and neighbours play, so the test damages one good file in a clean stretch."""
    middle = len(segs) // 2
    for i in sorted(range(1, len(segs) - 1), key=lambda i: abs(i - middle)):
        seg = segs[i]
        a, b = seg["start_time"] - 60, seg["end_time"] + 60
        around = [(s["start_time"], s["end_time"]) for s in continuity.segments(cam, a - 15, b + 15)]
        if testlib.window_gaps(around, a, b, testlib.MAIN_MIN_GAP):
            continue
        if all(playable(seg_rel(cam, x["start_time"]))[0] for x in segs[i - 1:i + 2]):
            return i
    return None


def run_corrupt(cam: str, minutes_ago: float) -> int:
    r = testlib.Report()
    now = time.time()
    segs = continuity.segments(cam, now - (minutes_ago + 5) * 60, now - minutes_ago * 60)
    i = clean_segment(cam, segs) if len(segs) >= 3 else None
    if i is None:
        print(f"ERROR: no {cam} segment in a stretch without gaps around {minutes_ago:g} min ago", file=sys.stderr)
        return 2
    seg = segs[i]
    rel = seg_rel(cam, seg["start_time"])
    host_path = MEDIA / "frigate" / "media" / "recordings" / rel
    backup = Path("/root/m11-corrupt-backup") / rel.replace("/", "_")
    backup.parent.mkdir(mode=0o700, exist_ok=True)
    shutil.copy2(host_path, backup)
    size = host_path.stat().st_size
    a, b = seg["start_time"] - 60, seg["end_time"] + 60
    try:
        before_len, _ = clip_length(cam, a, b)
        r.check("before the damage, a clip across the segment plays in full", before_len >= (b - a) - 5,
                f"{before_len:.0f} s of video for {b - a:.0f} s asked")
        os.truncate(host_path, int(size * 0.6))         # its end is gone, as after a write cut short
        ok, detail = playable(rel)
        r.check("the damaged segment is detected as broken", not ok, f"{rel}: {detail}")
        length, frames = clip_length(cam, a, b)
        r.check("a clip across the damaged segment still plays the segments around it",
                length >= (b - a) - (seg["end_time"] - seg["start_time"]) - 15,
                f"{length:.0f} s of video ({frames}) for {b - a:.0f} s asked; {before_len:.0f} s before the damage")
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:5000/vod/{cam}/start/{int(a)}/end/{int(b)}/index.m3u8", timeout=30) as f:
                hls = f.read().decode(errors="replace")
            r.check("the HLS playlist across it still loads", f.status == 200 and "#EXTM3U" in hls, f"{hls.count('#EXTINF')} entries")
        except Exception as e:
            r.check("the HLS playlist across it still loads", False, str(e)[:160])
        for x in (segs[i - 1], segs[i + 1]):
            nrel = seg_rel(cam, x["start_time"])
            nok, ndetail = playable(nrel)
            r.check(f"neighbouring segment {nrel} plays", nok, ndetail)
    finally:
        shutil.copy2(backup, host_path)
        restored = host_path.stat().st_size == size and not testlib.mp4_cut_short(host_path)
        print(f"restored {host_path} ({'ok' if restored else 'SIZE DIFFERS'})", flush=True)
        if restored:
            backup.unlink()
    r.check("the original segment was put back", restored, rel)
    md = ["# M11 corrupted-segment test", "", f"Camera {cam}: segment `{rel}` (from {vivo.utc(seg['start_time'])}) cut to 60% "
          "of its size, then restored from a copy.", ""] + r.markdown()
    print("report:", vivo.write_report("m11-corrupt", "\n".join(md) + "\n"))
    return 0 if r.passed else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="mode", required=True)
    p = sub.add_parser("power")
    p.add_argument("--host", required=True)
    p.add_argument("--count", type=int, default=10)
    p.add_argument("--off", type=float, default=15, help="seconds without power")
    p.add_argument("--pause", type=float, default=120)
    p.add_argument("--plug-off", help="shell command that switches the smart plug off")
    p.add_argument("--plug-on", help="shell command that switches it on")
    p.add_argument("--crash", action="store_true", help="no power cut: reboot at once without saving (sysrq b)")
    p.add_argument("--planned", action="append", default=[], help="UTC window of another planned test, ISO a/b")
    sub.add_parser("full-disk")
    m = sub.add_parser("missing-disk")
    m.add_argument("--yes", action="store_true", help="confirm: recording stops for a few minutes")
    m.add_argument("--missing", type=float, default=90, help="seconds the disk stays away")
    c = sub.add_parser("corrupt")
    c.add_argument("--camera", default="cam01")
    c.add_argument("--minutes-ago", type=float, default=20)
    rp = sub.add_parser("report")
    rp.add_argument("--events", action="append", required=True)
    rp.add_argument("--planned", action="append", default=[], help="UTC window of another planned test, ISO a/b")
    a = ap.parse_args()

    if a.mode == "power":
        events = run_power(a.host, a.count, a.off, a.pause, a.plug_off, a.plug_on, a.crash)
        text = testlib.Remote(a.host).report("m11_faults.py", events, " ".join(f"--planned {w}" for w in a.planned))
        print(text)
        result = testlib.parse_result(text)
        return 0 if result and result[1] == 0 else 1
    if os.geteuid() != 0:
        print("run with sudo", file=sys.stderr)
        return 2
    if a.mode == "report":
        return power_report(testlib.read_events(a.events), a.planned)
    if a.mode == "full-disk":
        return run_full_disk()
    if a.mode == "missing-disk":
        if not a.yes:
            print("This stops recording for a few minutes. Announce it, then run again with --yes.", file=sys.stderr)
            return 2
        return run_missing_disk(a.missing)
    return run_corrupt(a.camera, a.minutes_ago)


if __name__ == "__main__":
    sys.exit(main())
