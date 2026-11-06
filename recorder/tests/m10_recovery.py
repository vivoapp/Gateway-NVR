#!/usr/bin/env python3
"""M10 acceptance: recording comes back on its own after reboots, Frigate crashes and camera disconnects.

usage:
  sudo tests/m10_recovery.py kill --count 10                 # on the gateway
  sudo tests/m10_recovery.py disconnect --count 5            # on the gateway; PoE backend in site.env
  sudo tests/m10_recovery.py disconnect --block --camera cam02 --count 3 --off 6   # a short network drop
  tests/m10_recovery.py reboot --host vivo-n150 --count 10   # from a workstation (SSH key, sudo on the gateway)
  sudo tests/m10_recovery.py report --events FILE.csv [--events ...]   # gap log for recorded events

kill        kills Frigate's main process (Docker counts it as a crash, as in tests/m2_check.py)
disconnect  switches one camera's PoE port off and on (tools/poe.py), a different camera each time;
            with --block the camera's address is blocked in the firewall (DOCKER-USER) instead: the
            camera keeps power and only its connection drops, like a short network drop
reboot      reboots the gateway; event times come from the gateway's own clock

Every event is written to a CSV at once. The report lists, per event and camera, how long recording
of each stream took to come back, and a gap log with every gap in the main stream and the
substream over the test window and its cause; a gap that no event or --planned window explains
fails the test. Limits (agreed before start, docs/acceptance-limits-proposal.md): recording back
within 180 s of a reboot command, 60 s of a Frigate kill, 120 s of a camera's power coming back.
The substream may need up to 70 s more: its segments start at the camera's next keyframe after
the archiver reconnects.
"""
import argparse
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import testlib  # noqa: E402
import vivo  # noqa: E402

LIMITS = {"reboot": 180, "kill": 60, "disconnect": 120}


def events_file(kind: str) -> Path:
    vivo.REPORTS.mkdir(exist_ok=True)
    return vivo.REPORTS / f"m10-events-{kind}-{time.strftime('%Y%m%d-%H%M%S', time.gmtime())}.csv"


def sh(*cmd: str) -> str:
    return subprocess.run(cmd, capture_output=True, text=True).stdout.strip()


def run_kills(count: int, pause: float) -> list[dict]:
    events, path = [], events_file("kill")
    for n in range(1, count + 1):
        pid = sh("docker", "inspect", "-f", "{{.State.Pid}}", "frigate")
        if not pid or pid == "0":
            raise RuntimeError("Frigate is not running")
        t = time.time()
        subprocess.run(["kill", "-9", pid], check=True)
        print(f"kill {n}/{count} at {vivo.utc(t)}", flush=True)
        events.append({"kind": "kill", "n": n, "camera": "all", "t_event": f"{t:.3f}", "t_restore": "", "note": f"pid {pid}"})
        testlib.write_events(path, events)
        testlib.wait_until(lambda: all(v["main"] for v in testlib.recording_after(t).values()), LIMITS["kill"] + 120, 3)
        time.sleep(pause)
    return events


def block(cam: dict, on: bool) -> None:
    """Block (on=False) or unblock the camera's address for the containers (go2rtc)."""
    rule = ["DOCKER-USER", "-d", cam["host"], "-j", "DROP"]
    subprocess.run(["iptables", "-D" if on else "-I", *rule], check=on is False, capture_output=True)


def run_disconnects(count: int, off: float, pause: float, only: list[str] | None, firewall: bool = False) -> list[dict]:
    import poe
    switch = block if firewall else poe.set_power
    cams = [c for c in vivo.cameras() if not only or c["id"] in only]
    events, path = [], events_file("disconnect")
    for n in range(1, count + 1):
        cam = cams[(n - 1) % len(cams)]
        t_off = time.time()
        switch(cam, on=False)
        try:
            time.sleep(off)
        finally:                    # the camera always comes back, also if the test is interrupted
            t_on = time.time()
            switch(cam, on=True)
        print(f"disconnect {n}/{count}: {cam['id']} off {vivo.utc(t_off)}, on {vivo.utc(t_on)}", flush=True)
        events.append({"kind": "disconnect", "n": n, "camera": cam["id"], "t_event": f"{t_off:.3f}",
                       "t_restore": f"{t_on:.3f}", "note": f"{off:g} s off, " + ("firewall block" if firewall else f"PoE {vivo.SITE.get('POE_BACKEND', 'manual')}")})
        testlib.write_events(path, events)
        testlib.wait_until(lambda: testlib.recording_after(t_on, [cam["id"]])[cam["id"]]["main"], LIMITS["disconnect"] + 180, 3)
        time.sleep(pause)
    return events


def run_reboots(host: str, count: int, pause: float) -> list[dict]:
    remote = testlib.Remote(host)
    if not remote.up():
        raise RuntimeError(f"cannot reach {host} over SSH")
    events = []
    local_path = Path(f"m10-events-reboot-{time.strftime('%Y%m%d-%H%M%S', time.gmtime())}.csv")
    for n in range(1, count + 1):
        t, offset = remote.clock()                      # gateway clock
        remote.run("sudo -n systemctl reboot", timeout=15)
        print(f"reboot {n}/{count} at {vivo.utc(t)} (gateway clock, {offset:+.2f} s from this machine)", flush=True)
        down = remote.wait_down(90)
        up = remote.wait_up(600)
        events.append({"kind": "reboot", "n": n, "camera": "all", "t_event": f"{t:.3f}", "t_restore": "",
                       "note": f"SSH down after {down - t + offset:.0f} s, back after {up - t + offset:.0f} s" if down and up
                       else "SSH did not come back within 10 min"})
        testlib.write_events(local_path, events)
        if not up:
            break
        remote.wait_recording(t, LIMITS["reboot"] + 180)
        time.sleep(pause)
    print(f"events: {local_path}")
    return events


def report(events: list[dict], planned: list[str], title: str = "M10 recording recovery") -> int:
    cams = vivo.services_config()["cameras"]
    res = testlib.analyze(events, cams, LIMITS, [testlib.parse_window(w) for w in planned])
    r = testlib.Report()
    kinds = sorted({e["kind"] for e in events})
    for kind in kinds:
        rows = [x for x in res["events"] if x["event"].startswith(kind)]
        worst = max((x["main_recovery_s"] for x in rows), default=0)
        worst_sub = max((x["substream_recovery_s"] for x in rows), default=0)
        n = len({x["event"] for x in rows})
        r.check(f"{kind}: recording back on its own after every one of {n} events (limit {LIMITS[kind]} s)",
                all(x["pass"] for x in rows),
                f"slowest main stream {worst:.0f} s, substream {worst_sub:.0f} s")
    unexplained = [g for g in res["gaps"] if g["cause"] == "UNEXPLAINED"]
    r.check("no unexplained gap in the log", not unexplained, f"{len(res['gaps'])} gaps, {len(unexplained)} unexplained")
    t0, t1 = res["window"] or (0, 0)
    md = [f"# {title}", "", f"Window {vivo.utc(t0)} to {vivo.utc(t1)}; {len(events)} events, {len(cams)} cameras. "
          "Recovery is measured from the reboot command, the kill, or the moment a camera's power came back, to the end "
          "of the gap it caused (0 when recording did not stop).", ""] + r.markdown()
    md += ["", "## Recovery per event and camera", "",
           "| Event | Camera | At (UTC) | Main stream back | Substream back | Lost before the event (main) | Result |",
           "|---|---|---|---|---|---|---|"]
    md += [f"| {x['event']} | {x['camera']} | {vivo.utc(x['at'])} | {x['main_recovery_s']:.0f} s | {x['substream_recovery_s']:.0f} s | "
           f"{x['main_lost_before_s']:.0f} s | {'PASS' if x['pass'] else '**FAIL**'} |" for x in res["events"]]
    md += ["", "## Gap log", ""] + testlib.gap_table(res["gaps"])
    md += ["", "## Events", "", "| Event | Camera | At (UTC) | Restored (UTC) | Note |", "|---|---|---|---|---|"]
    md += [f"| {e['kind']} {e['n']} | {e['camera']} | {vivo.utc(e['t_event'])} | {vivo.utc(e['t_restore']) if e['t_restore'] else ''} | "
           f"{e['note']} |" for e in events]
    print("report:", vivo.write_report("m10-recovery", "\n".join(md) + "\n"))
    return 0 if r.passed else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="mode", required=True)
    k = sub.add_parser("kill")
    k.add_argument("--count", type=int, default=10)
    k.add_argument("--pause", type=float, default=60, help="seconds between kills, after recording is back")
    d = sub.add_parser("disconnect")
    d.add_argument("--count", type=int, default=5)
    d.add_argument("--off", type=float, default=20)
    d.add_argument("--pause", type=float, default=60)
    d.add_argument("--camera", action="append")
    d.add_argument("--block", action="store_true", help="block the camera in the firewall instead of its PoE port")
    b = sub.add_parser("reboot")
    b.add_argument("--host", required=True, help="SSH name of the gateway, e.g. vivo-n150")
    b.add_argument("--count", type=int, default=10)
    b.add_argument("--pause", type=float, default=120)
    rp = sub.add_parser("report")
    rp.add_argument("--events", action="append", required=True)
    for p in (k, d, b, rp):
        p.add_argument("--planned", action="append", default=[], help="UTC window of another planned test, ISO a/b")
    a = ap.parse_args()

    if a.mode == "reboot":
        events = run_reboots(a.host, a.count, a.pause)
        text = testlib.Remote(a.host).report("m10_recovery.py", events, " ".join(f"--planned {w}" for w in a.planned))
        print(text)
        result = testlib.parse_result(text)
        return 0 if result and result[1] == 0 else 1
    if a.mode == "report":
        return report(testlib.read_events(a.events), a.planned)
    events = run_kills(a.count, a.pause) if a.mode == "kill" else run_disconnects(a.count, a.off, a.pause, a.camera, a.block)
    time.sleep(60)   # let the last segments land before the gap log is built
    for e in events:
        e["t_event"] = float(e["t_event"])
        e["t_restore"] = float(e["t_restore"]) if e["t_restore"] else None
    return report(events, a.planned)


if __name__ == "__main__":
    sys.exit(main())
