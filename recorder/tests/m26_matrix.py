#!/usr/bin/env python3
"""M26: the fault-simulation test matrix. M10, M11 and M12 in one repeatable run, with a results table.

Runs on a workstation with SSH key access to the gateway and passwordless sudo there (the same
access as tests/m10_recovery.py reboot), because two steps reboot or power-cycle the gateway.

usage:
  tests/m26_matrix.py --host vivo-n150 --dry-run       # the plan and the preflight checks; changes nothing
  tests/m26_matrix.py --host vivo-n150 \\
      --plug-off "curl -s http://PLUG/relay/0?turn=off" --plug-on "curl -s http://PLUG/relay/0?turn=on"
  tests/m26_matrix.py --host vivo-n150 --quick         # one event of each kind (about 1.5 hours): try it first
  tests/m26_matrix.py --host vivo-n150 --only m11-corrupt,m12-integrity
  tests/m26_matrix.py --host vivo-n150 --skip m10-disconnect,m11-power   # without PoE control or smart plug

Steps, in this order (the scope's counts; --quick runs one event of each kind). The two steps that
may need someone on site (camera cables, power) come first, so their part is one block of about
1 h 20 min at the start:

  m10-disconnect    5 camera disconnects through the PoE switch     gateway       about 15 min
  m11-power         10 power cuts with the smart plug               workstation   about 1 h
  m10-kill          Frigate killed 10 times                         gateway       about 20 min
  m10-reboot        10 gateway reboots                              workstation   about 1 h
  m11-full-disk     recording disk filled to its safe limit         gateway       about 15 min
  m11-missing-disk  recording disk taken away and given back        gateway       about 5 min
  m11-corrupt       one damaged segment, playback around it         gateway       about 5 min
  m12-integrity     every segment since the matrix began            gateway       about 15 min
  whole window      no recording gap outside the steps above        gateway       1 min

Each step is the module's own test script with its own report (reports/ on the gateway); the matrix
adds the results table and one check over the whole run: every gap in either stream of any camera
falls inside a step's window. Gateway steps run as transient systemd units on the gateway
(vivo-m26-*), so a dropped SSH session cannot stop one half-way (the missing-disk step switches the
recording disk off); the matrix follows their log. Without --plug-off/--plug-on the power step asks
an operator to switch the power; with POE_BACKEND=manual on the gateway the disconnect step asks
an operator on site to unplug each camera (through an interactive SSH session).

Before every step the gateway must be recording every camera, or the matrix stops. Recording stops
many times during a full run (about 4 hours): post the planned window to the group first. The
matrix asks for a typed "yes" unless --yes.
"""
import argparse
import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import testlib  # noqa: E402
import vivo  # noqa: E402

HERE = Path(__file__).resolve().parent
STEP_PAD = 60          # a gap this close to a step's window belongs to that step
FOLLOW_EVERY = 5


def iso(t: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


def plan(quick: bool, plug_off: str | None, plug_on: str | None) -> list[dict]:
    """The matrix steps in order. `argv` is the test script's arguments; m12 gets its period at run time."""
    n = (lambda full: 1) if quick else (lambda full: full)

    def count(k: int, one: str, many: str) -> str:
        return f"{n(k)} {one if n(k) == 1 else many}"
    power = ["--plug-off", plug_off, "--plug-on", plug_on] if plug_off and plug_on else []
    return [
        {"name": "m10-disconnect", "module": "M10", "what": f"{count(5, 'camera disconnect', 'camera disconnects')} (PoE)", "where": "gateway",
         "script": "m10_recovery.py", "argv": ["disconnect", "--count", str(n(5))], "minutes": 3 * n(5)},
        {"name": "m11-power", "module": "M11", "what": count(10, "power cut", "power cuts") + ("" if power else " (operator switches the power)"),
         "where": "workstation", "script": "m11_faults.py", "argv": ["power", "--count", str(n(10))] + power,
         "minutes": 6 * n(10)},
        {"name": "m10-kill", "module": "M10", "what": f"Frigate killed {'once' if n(10) == 1 else f'{n(10)} times'}", "where": "gateway",
         "script": "m10_recovery.py", "argv": ["kill", "--count", str(n(10))], "minutes": 2 * n(10)},
        {"name": "m10-reboot", "module": "M10", "what": count(10, "gateway reboot", "gateway reboots"), "where": "workstation",
         "script": "m10_recovery.py", "argv": ["reboot", "--count", str(n(10))], "minutes": 6 * n(10)},
        {"name": "m11-full-disk", "module": "M11", "what": "recording disk filled to its safe limit", "where": "gateway",
         "script": "m11_faults.py", "argv": ["full-disk"], "minutes": 15},
        {"name": "m11-missing-disk", "module": "M11", "what": "recording disk taken away and given back", "where": "gateway",
         "script": "m11_faults.py", "argv": ["missing-disk", "--yes"], "minutes": 5},
        {"name": "m11-corrupt", "module": "M11", "what": "one damaged segment, playback around it", "where": "gateway",
         "script": "m11_faults.py", "argv": ["corrupt"], "minutes": 5},
        {"name": "m12-integrity", "module": "M12", "what": "every segment since the matrix began", "where": "gateway",
         "script": "m12_integrity.py", "argv": ["--spot", "10"], "minutes": 15},
    ]


def select(steps: list[dict], only: str, skip: str) -> list[dict]:
    names = {s["name"] for s in steps}
    for opt in (only, skip):
        bad = [x for x in opt.split(",") if x and x not in names]
        if bad:
            raise SystemExit(f"unknown step {', '.join(bad)}; steps: {', '.join(s['name'] for s in steps)}")
    keep = [s for s in steps if (not only or s["name"] in only.split(",")) and s["name"] not in skip.split(",")]
    return keep


def classify(gaps: list[dict], windows: list[tuple[str, float, float]], planned: list[tuple[float, float]]) -> list[dict]:
    """Each gap's cause: the step whose window holds it, a --planned window, or UNEXPLAINED."""
    for g in gaps:
        step = next((name for name, a, b in windows if a - STEP_PAD <= g["start"] and g["end"] <= b + STEP_PAD), None)
        if step is None and any(a - STEP_PAD <= g["start"] and g["end"] <= b + STEP_PAD for a, b in planned):
            step = "planned window"
        g["cause"] = step or "UNEXPLAINED"
    return gaps


def verdict(exit_code: int | None, report_text: str) -> tuple[str, str]:
    """(PASS / FAIL / ERROR, checks) from a step's exit code and its report."""
    result = testlib.parse_result(report_text)
    if result is None:
        return "ERROR", "no report" if not report_text else "report without a result line"
    passed, failed = result
    if failed == 0 and exit_code == 0:
        return "PASS", f"{passed}/{passed + failed}"
    return ("FAIL" if failed or exit_code == 1 else "ERROR"), f"{passed}/{passed + failed}"


def failed_checks(report_text: str) -> list[str]:
    return [line.split("|")[2].strip() for line in report_text.splitlines() if line.startswith("| **FAIL** |")]


def last_report_path(output: str) -> str:
    return next((l.split("report:", 1)[1].strip() for l in reversed(output.splitlines()) if l.startswith("report:")), "")


# ------------------------------------------------------------------ running things

def stream(cmd: list[str]) -> tuple[int, str]:
    """Run a command with the terminal's stdin (operator prompts work), echo and keep its output."""
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, cwd=HERE.parent)
    chunks = []
    while True:
        data = os.read(proc.stdout.fileno(), 4096)
        if not data:
            break
        sys.stdout.write(data.decode(errors="replace"))
        sys.stdout.flush()
        chunks.append(data)
    return proc.wait(), b"".join(chunks).decode(errors="replace")


class Gateway(testlib.Remote):
    offset = 0.0                   # gateway clock minus this workstation's, from the last answer

    def now(self) -> float:
        """The gateway's clock; this workstation's plus the last known offset while SSH is down."""
        try:
            t, self.offset = self.clock()
            return t
        except (ValueError, subprocess.TimeoutExpired):
            return time.time() + self.offset

    def raw(self, cmd: str, timeout: float = 30) -> tuple[int, bytes]:
        res = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", "-o", "ServerAliveInterval=5",
                              self.host, cmd], capture_output=True, timeout=timeout)
        return res.returncode, res.stdout

    def sudo(self, cmd: str, timeout: float = 60) -> subprocess.CompletedProcess:
        return self.run(f"sudo -n {cmd}", timeout=timeout)

    def text(self, cmd: str, timeout: float = 60) -> str:
        try:
            return self.run(cmd, timeout=timeout).stdout
        except subprocess.TimeoutExpired:
            return ""

    def site(self, key: str) -> str:
        return self.text(f"sed -n 's/^{key}=//p' /etc/vivo/site.env | tail -1").strip()

    def unit_step(self, unit: str, script: str, argv: list[str], log: str, limit_s: float) -> tuple[int | None, str]:
        """Run a test script as a transient unit on the gateway and follow its log until it exits."""
        inner = f"cd {shlex.quote(self.root)} && python3 -u tests/{script} {' '.join(shlex.quote(a) for a in argv)}; echo m26-exit=$?"
        start = self.sudo(f"systemd-run --unit {unit} --collect --quiet -p TimeoutStopSec=15min "
                          f"-p StandardOutput=append:{log} -p StandardError=append:{log} /bin/sh -c {shlex.quote(inner)}")
        if start.returncode:
            return None, f"could not start {unit}: {start.stderr.strip()[-300:]}"
        seen, offset, quiet_since = [], 0, None
        deadline = time.time() + limit_s
        while True:
            time.sleep(FOLLOW_EVERY)
            try:
                code, data = self.raw(f"sudo -n tail -c +{offset + 1} {log}")
            except subprocess.TimeoutExpired:
                continue                                   # SSH dropped: the unit keeps running; try again
            if code == 0 and data:
                sys.stdout.write(data.decode(errors="replace"))
                sys.stdout.flush()
                offset += len(data)
                seen.append(data)
            text = b"".join(seen).decode(errors="replace")
            if "m26-exit=" in text:
                code = text.rsplit("m26-exit=", 1)[1].split()[0]
                return (int(code) if code.isdigit() else None), text
            state = self.text(f"systemctl is-active {unit}", timeout=15).strip()
            if state in ("inactive", "failed", "unknown", ""):
                quiet_since = quiet_since or time.time()
                if time.time() - quiet_since > 30:
                    return None, text + f"\n{unit} stopped without finishing (state {state or 'unknown'})"
            else:
                quiet_since = None
            if time.time() > deadline:
                return None, text + f"\n{unit} still running after {limit_s / 60:.0f} min; stop it with: sudo systemctl stop {unit}"

    def interactive_step(self, script: str, argv: list[str]) -> tuple[int, str]:
        """For steps that ask an operator (POE_BACKEND=manual): a normal SSH session with a terminal."""
        inner = f"cd {shlex.quote(self.root)} && sudo -n python3 -u tests/{script} {' '.join(shlex.quote(a) for a in argv)}"
        return stream(["ssh", "-t", "-o", "ServerAliveInterval=5", self.host, inner])

    def all_recording(self) -> tuple[bool, str]:
        state = self.recording_after(self.now() - 180)
        missing = [f"{c} {s}" for c, v in state.items() for s in ("main", "substream") if not v[s]]
        return bool(state) and not missing, ", ".join(missing) if state else "no answer from the gateway"


def preflight(gw: Gateway, steps: list[dict]) -> list[tuple[bool, str, str]]:
    rows = []
    up = gw.up()
    rows.append((up, f"SSH to {gw.host} with a key", ""))
    if not up:
        return rows
    sudo = gw.sudo("true", timeout=15).returncode == 0
    rows.append((sudo, "passwordless sudo on the gateway", ""))
    rows.append((gw.text(f"test -x {gw.root}/tests/m10_recovery.py && echo yes").strip() == "yes",
                 f"test scripts installed in {gw.root}/tests", ""))
    units = gw.text("systemctl is-active vivo-gateway.service vivo-health.service vivo-disk-watch.timer").split()
    rows.append((units == ["active"] * 3, "vivo-gateway, vivo-health and vivo-disk-watch.timer active", " ".join(units)))
    ok, missing = gw.all_recording()
    rows.append((ok, "every camera records both streams", missing))
    gw.now()
    rows.append((True, "clocks", f"gateway {gw.offset:+.1f} s from this workstation (event times use the gateway's clock)"))
    names = {s["name"] for s in steps}
    if "m10-disconnect" in names:
        backend = gw.site("POE_BACKEND") or "manual"
        rows.append((True, "PoE control for m10-disconnect", f"POE_BACKEND={backend}" +
                     ("" if backend == "command" else ": an operator on site unplugs each camera when asked")))
    if "m11-missing-disk" in names:
        media = gw.site("MEDIA_ROOT") or "/srv/vivo"
        lv = gw.text(f"sudo -n lvs --noheadings -o lv_path $(findmnt -n -o SOURCE --mountpoint {shlex.quote(media)}) "
                     "2>/dev/null").strip()
        rows.append((bool(lv), "recording disk on LVM (m11-missing-disk switches its volume off)",
                     lv or "not LVM: run the missing-disk test by hand (docs/m11-faults.md), and --skip m11-missing-disk"))
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", required=True, help="SSH name of the gateway, e.g. vivo-n150")
    ap.add_argument("--quick", action="store_true", help="one event of each kind, to try the matrix")
    ap.add_argument("--only", default="", help="comma-separated step names")
    ap.add_argument("--skip", default="", help="comma-separated step names")
    ap.add_argument("--plug-off", help="shell command that switches the gateway's smart plug off (m11-power)")
    ap.add_argument("--plug-on", help="shell command that switches it on")
    ap.add_argument("--settle", type=float, default=300, help="seconds between steps")
    ap.add_argument("--planned", action="append", default=[], help="UTC window of another planned test, ISO a/b")
    ap.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    steps = select(plan(a.quick, a.plug_off, a.plug_on), a.only, a.skip)
    gw = Gateway(a.host)
    minutes = sum(s["minutes"] for s in steps) + len(steps) * a.settle / 60
    print(f"Test matrix on {a.host}: {len(steps)} steps, about {minutes / 60:.1f} hours. Recording stops during most of them.")
    for i, s in enumerate(steps, 1):
        print(f"  {i}. {s['name']:<17} {s['module']}  {s['what']:<48} {s['where']}")
    print("\npreflight:")
    pre = preflight(gw, steps)
    for ok, name, detail in pre:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}  {detail}")
    if a.dry_run:
        return 0 if all(ok for ok, _, _ in pre) else 1
    if not all(ok for ok, _, _ in pre):
        print("preflight failed: fix the above first", file=sys.stderr)
        return 2
    if not a.yes and input('\nRecording stops many times. Post the planned window first. Type "yes" to start: ').strip() != "yes":
        return 2

    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    logdir = f"{gw.root}/reports/m26-{stamp}"
    gw.sudo(f"install -d -m 755 {logdir}")
    manual_poe = (gw.site("POE_BACKEND") or "manual") != "command"
    planned = [testlib.parse_window(w) for w in a.planned]
    t_begin = gw.now()
    results, windows = [], []
    stopped = ""
    try:
        for i, s in enumerate(steps):
            if i:
                print(f"\nsettling {a.settle:.0f} s before the next step", flush=True)
                time.sleep(a.settle)
            ok, missing = gw.all_recording()
            if not ok:
                stopped = f"before {s['name']}: the gateway was not recording every camera ({missing})"
                print(f"STOP: {stopped}", flush=True)
                break
            print(f"\n=== {s['name']}: {s['what']} ({s['where']})", flush=True)
            argv = list(s["argv"])
            if s["name"] == "m12-integrity":
                hours = (gw.now() - t_begin) / 3600 + 0.25
                argv = ["--hours", f"{hours:.2f}"] + argv
                for _, w0, w1 in windows:
                    argv += ["--planned", f"{iso(w0 - STEP_PAD)}/{iso(w1 + STEP_PAD)}"]
            if s["script"] != "m11_faults.py" or s["name"] == "m11-power":   # the other M11 modes have no gap log
                argv += [x for w in a.planned for x in ("--planned", w)]
            w0 = gw.now()
            if s["where"] == "workstation":
                code, out = stream([sys.executable, "-u", str(HERE / s["script"]), argv[0], "--host", a.host, *argv[1:]])
            elif s["name"] == "m10-disconnect" and manual_poe:
                code, out = gw.interactive_step(s["script"], argv)
            else:
                code, out = gw.unit_step(f"vivo-m26-{s['name']}-{stamp}", s["script"], argv, f"{logdir}/{s['name']}.log",
                                         limit_s=(4 * s["minutes"] + 30) * 60)
            gw.wait_up(600)
            w1 = gw.now()
            windows.append((s["name"], w0, w1))
            line = shlex.quote(f"{iso(w0)}/{iso(w1)} M26 matrix {s['name']}")
            gw.sudo(f"sh -c {shlex.quote(f'echo {line} >> {gw.root}/reports/planned-windows.txt')}")
            path = last_report_path(out)
            text = gw.sudo(f"cat {shlex.quote(path)}").stdout if path else ""
            result, checks = verdict(code, text)
            results.append({**s, "start": w0, "end": w1, "exit": code, "result": result, "checks": checks,
                            "report": Path(path).name if path else "", "failed": failed_checks(text)})
            print(f"=== {s['name']}: {result} {checks}", flush=True)
    except KeyboardInterrupt:
        stopped = "stopped by the operator (Ctrl-C)"
        running = gw.text("systemctl list-units --plain --no-legend 'vivo-m26-*'").strip()
        if running:
            print(f"\nStill running on the gateway: {running}\nIt finishes and cleans up on its own; to stop it: "
                  f"ssh {a.host} sudo systemctl stop <unit>", flush=True)

    whole = None
    if results:
        print("\nchecking the whole window for gaps outside the steps", flush=True)
        time.sleep(max(0.0, 100 - (gw.now() - results[-1]["end"])))
        t_end = gw.now() - 90
        out = gw.text(f"python3 {shlex.quote(gw.root)}/tests/testlib.py gaps {t_begin:.3f} {t_end:.3f}", timeout=600)
        try:
            gaps = classify(json.loads(out), windows, planned)
            unexplained = [g for g in gaps if g["cause"] == "UNEXPLAINED"]
            whole = {"gaps": gaps, "unexplained": unexplained, "from": t_begin, "to": t_end}
        except ValueError:
            whole = {"gaps": [], "unexplained": [], "error": out.strip()[-300:] or "no answer", "from": t_begin, "to": t_end}

    md = report(a, results, whole, stopped, pre)
    path = vivo.write_report("m26-matrix", md)
    path.with_suffix(".json").write_text(json.dumps({"results": results, "whole": whole, "stopped": stopped}, indent=2, default=str))
    subprocess.run(["scp", "-q", str(path), f"{a.host}:/tmp/{path.name}"], check=False)
    gw.sudo(f"install -m 644 /tmp/{path.name} {gw.root}/reports/{path.name}")
    print(md)
    print("report:", path)
    ok = (not stopped and results and all(r["result"] == "PASS" for r in results) and whole and not whole["unexplained"]
          and "error" not in whole)
    return 0 if ok else 1


def report(a, results: list[dict], whole: dict | None, stopped: str, pre: list) -> str:
    passed = sum(r["result"] == "PASS" for r in results)
    whole_ok = bool(whole) and not whole["unexplained"] and "error" not in whole
    rows = len(results) + (1 if whole else 0)
    md = ["# M26 fault-simulation test matrix (M10, M11, M12)", "",
          f"Gateway `{a.host}`, {'quick run (one event of each kind)' if a.quick else 'full counts from the scope'}, "
          f"started {vivo.utc(results[0]['start']) if results else 'not started'}.", "",
          f"Result: **{passed + (1 if whole_ok else 0)} passed, {rows - passed - (1 if whole_ok else 0)} failed**"
          + (f". Stopped early: {stopped}" if stopped else ""), "",
          "| # | Step | Module | What | Ran on | Started (UTC) | Minutes | Result | Checks | Report | Failed checks |",
          "|---|---|---|---|---|---|---|---|---|---|---|"]
    for i, r in enumerate(results, 1):
        md.append(f"| {i} | {r['name']} | {r['module']} | {r['what']} | {r['where']} | {vivo.utc(r['start'])[:19]} | "
                  f"{(r['end'] - r['start']) / 60:.0f} | {r['result'] if r['result'] == 'PASS' else '**' + r['result'] + '**'} | "
                  f"{r['checks']} | {r['report'] or '-'} | {'; '.join(r['failed']).replace('|', '/') or ''} |")
    if whole:
        n_un = len(whole["unexplained"])
        md.append(f"| {len(results) + 1} | whole window | M10-M12 | no gap outside the steps' windows | gateway | "
                  f"{vivo.utc(whole['from'])[:19]} | {(whole['to'] - whole['from']) / 60:.0f} | "
                  f"{'PASS' if whole_ok else '**FAIL**'} | {len(whole['gaps'])} gaps, {n_un} unexplained | this report | "
                  f"{whole.get('error', '')} |")
    md += ["", "## Preflight", "", "| Result | Check | Detail |", "|---|---|---|"]
    md += [f"| {'PASS' if ok else '**FAIL**'} | {name} | {detail} |" for ok, name, detail in pre]
    if whole:
        md += ["", "## Gaps in the whole window", "", "Every gap in either stream of any camera from the start of the "
               "first step to the end of the last; each belongs to the step whose window holds it.", ""]
        md += testlib.gap_table(whole["gaps"])
    md += ["", "## Step windows (gateway clock)", "", "| Step | From (UTC) | To | Exit code |", "|---|---|---|---|"]
    md += [f"| {r['name']} | {vivo.utc(r['start'])} | {vivo.utc(r['end'])[11:]} | {r['exit']} |" for r in results]
    md += ["", "Each step's own report (reports/ on the gateway) has its details and gap log. Step logs: "
           "reports/m26-<time>/ on the gateway."]
    return "\n".join(md) + "\n"


if __name__ == "__main__":
    sys.exit(main())
