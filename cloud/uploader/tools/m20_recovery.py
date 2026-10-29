#!/usr/bin/env python3
"""M20 recovery tests: 18 scripted faults against the real uploader, each one checked from the outside.

    5 internet cuts      (C1-C5; C5 is an outage of the agreed length)
    5 gateway reboots    (R1-R5)
    5 uploader kills     (K1-K5)
    3 storage errors     (S1-S3)

How it works. One uploader process (the real `python -m segment_uploader run`) and a fake recorder with 10
cameras run against a real S3 server (MinIO on the test PC, Backblaze B2 on the gateway). A small proxy
(tools/fault_proxy.py) sits between the uploader and the storage and can cut the line, refuse the
credentials, say "full", or fail at an exact moment. The uploader and the recorder are processes of this
script, so they can be killed with SIGKILL (no clean-up), like a power cut.

After every test the recorder is stopped, the queue is allowed to empty, and then everything is checked
again from the outside, without trusting the uploader's own state:

    * the M19 checks in strict mode: every segment uploaded, size / ETag / SHA-256 equal, bucket holds
      exactly the uploaded segments, none stuck, none uploaded twice, index records complete,
      local files unchanged
    * the M20 gap report: no gap (a segment never uploaded whose file is gone)
    * the test's own expectations (for example: after a kill in the middle of an upload, the restart
      noticed the interrupted upload)

The measured catch-up time of each test is the time from "the fault is over" until every segment that was
waiting at that moment is in the bucket.

Needs the same environment variables as the uploader: S3_ENDPOINT_URL, S3_BUCKET, S3_ACCESS_KEY,
S3_SECRET_KEY (and optionally S3_REGION). A fresh key prefix is used for every run.

    python tools/m20_recovery.py --work /data/recovery --out-dir /data/recovery/report
    python tools/m20_recovery.py --quick                    (a short smoke run, minutes instead of an hour)
    python tools/m20_recovery.py --only C1,K2               (selected tests)
    python tools/m20_recovery.py --outage-days 3            (the agreed outage length, default 2)
"""
from __future__ import annotations

import argparse
import atexit
import json
import math
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import m19_acceptance as acc  # noqa: E402
import m20_retention as retention  # noqa: E402
from fault_proxy import FaultProxy  # noqa: E402
from segment_uploader import gaps as gap_report  # noqa: E402
from segment_uploader.config import load_config  # noqa: E402

CAMERAS = 10
NAME_FORMAT = "VIVO-CAM-{:02d}"


def say(msg: str) -> None:
    print(f"{datetime.now():%H:%M:%S} {msg}", flush=True)


# ============================================================================================
# The rig: storage proxy, uploader process, recorder process
# ============================================================================================
class Rig:
    def __init__(self, a: argparse.Namespace):
        self.a = a
        self.upstream = os.environ["S3_ENDPOINT_URL"]
        self.prefix = f"m20-{datetime.now(timezone.utc):%Y%m%dT%H%M%S}"
        os.environ["S3_KEY_PREFIX"] = self.prefix            # the checks and the uploader use the same prefix
        self.work = Path(a.work).resolve()
        self.rec, self.state = self.work / "recordings", self.work / "state"
        self.log_path, self.conf = self.work / "uploader.log", self.work / "config.toml"
        for p in (self.rec, self.state):
            shutil.rmtree(p, ignore_errors=True)
            p.mkdir(parents=True, exist_ok=True)
        self.log_path.unlink(missing_ok=True)
        self.proxy = FaultProxy(self.upstream).start()
        self.env = dict(os.environ, S3_ENDPOINT_URL=self.proxy.url)     # the uploader talks to the proxy
        self.up: subprocess.Popen | None = None
        self.recp: subprocess.Popen | None = None
        self.conf_key = None
        self.cfg = None
        self.manifest: dict = {}
        self.log_offset = 0
        self.t_end = 0.0
        self.backlog: set[str] = set()
        self.pile_keys: set[str] = set()
        self.notes: dict = {}
        self.ensure_bucket()
        atexit.register(self.shutdown)

    # ---- helpers ----
    def T(self, seconds: float) -> float:
        return max(1.0, seconds * self.a.scale)

    def ensure_bucket(self) -> None:
        from segment_uploader.storage import S3Storage
        c = load_config(self._write_config(self.a.backoff_max, 2)) if not self.conf.exists() else load_config(self.conf)
        c.endpoint_url = self.upstream
        client = S3Storage(c).client
        try:
            client.head_bucket(Bucket=c.bucket)
        except Exception:
            client.create_bucket(Bucket=c.bucket)

    def _write_config(self, backoff_max: float, recent_days: int) -> Path:
        self.conf.write_text(f"""# written by tools/m20_recovery.py
[service]
watch_dir = "{self.rec.as_posix()}"
state_db = "{(self.state / 'state.db').as_posix()}"
nominal_duration = 60
scan_interval = 1
workers = 2
recent_days = {recent_days}
full_scan_interval = 3600
backoff_initial = 2
backoff_max = {backoff_max:g}
stats_interval = 30
connect_timeout = 3
read_timeout = 20

[index]
sink = "jsonl"
dir = "{(self.state / 'index').as_posix()}"
""", encoding="utf-8")
        return self.conf

    def configure(self, backoff_max: float, recent_days: int = 2) -> None:
        """(Re)write the config. A running uploader is stopped (cleanly) and started again with it."""
        key = (backoff_max, recent_days)
        if key == self.conf_key and self.alive(self.up):
            return
        if self.alive(self.up):
            self.stop_uploader()
        self._write_config(backoff_max, recent_days)
        self.cfg = load_config(self.conf)                   # checks talk to the real storage, not the proxy
        self.cfg.endpoint_url = self.upstream
        self.conf_key = key
        self.start_uploader()

    @staticmethod
    def alive(p: subprocess.Popen | None) -> bool:
        return p is not None and p.poll() is None

    # ---- processes ----
    def start_uploader(self) -> None:
        if self.alive(self.up):
            return
        logf = open(self.log_path, "ab", buffering=0)
        self.up = subprocess.Popen([sys.executable, "-m", "segment_uploader", "run", "--config", str(self.conf)],
                                   cwd=ROOT, env=self.env, stdout=logf, stderr=subprocess.STDOUT)
        logf.close()

    def kill_uploader(self) -> None:
        if self.alive(self.up):
            self.up.kill()
            self.up.wait()

    def stop_uploader(self) -> None:
        if self.alive(self.up):
            self.up.terminate()
            try:
                self.up.wait(20)
            except subprocess.TimeoutExpired:
                self.up.kill()
                self.up.wait()

    def start_recorder(self) -> None:
        if self.alive(self.recp):
            return
        self.recp = subprocess.Popen(
            [sys.executable, str(ROOT / "tools" / "fake_recorder.py"), "--dir", str(self.rec),
             "--cameras", str(CAMERAS), "--name-format", NAME_FORMAT, "--end-in-name", "--interval", str(self.a.interval),
             "--size-mb", str(self.a.size_mb)], cwd=ROOT, stdout=subprocess.DEVNULL)

    def stop_recorder(self) -> None:
        if self.alive(self.recp):
            self.recp.kill()
            self.recp.wait()

    def reboot(self, down_s: float) -> None:
        """Power cut: recorder and uploader die at the same moment without any clean-up, then boot again."""
        self.stop_recorder()
        self.kill_uploader()
        time.sleep(down_s)
        self.start_uploader()
        self.start_recorder()

    def shutdown(self) -> None:
        self.stop_recorder()
        self.stop_uploader()
        try:
            self.proxy.stop()
        except Exception:
            pass

    # ---- looking at the state ----
    def query(self, sql: str, args=()) -> list:
        db = sqlite3.connect(f"file:{self.state / 'state.db'}?mode=ro", uri=True, timeout=10)
        try:
            return db.execute(sql, args).fetchall()
        finally:
            db.close()

    def uploaded_keys(self) -> set[str]:
        return {r[0] for r in self.query("SELECT key FROM segments WHERE state='uploaded'")}

    def count_state(self, state: str) -> int:
        return self.query("SELECT COUNT(*) FROM segments WHERE state=?", (state,))[0][0]

    def local_keys(self) -> set[str]:
        return set(acc.local_segments(self.cfg))

    def wait(self, pred: Callable[[], bool], timeout: float, every: float = 0.3) -> bool:
        end = time.time() + timeout
        while time.time() < end:
            try:
                if pred():
                    return True
            except sqlite3.Error:
                pass
            time.sleep(every)
        return False

    def events_since(self, offset: int) -> list[dict]:
        if not self.log_path.exists():
            return []
        with open(self.log_path, "rb") as f:
            f.seek(offset)
            text = f.read().decode("utf-8", "replace")
        return [dict(acc.LOGKV.findall(line)) for line in text.splitlines() if "event=" in line]

    # ---- test frame ----
    def begin(self, live: bool = True, backoff_max: float | None = None, recent_days: int = 2) -> None:
        self.proxy.set_mode("ok")
        self.proxy.delay_before_put = self.proxy.delay_after_put = 0.0
        self.proxy.requests.clear()
        self.configure(backoff_max if backoff_max is not None else self.a.backoff_max, recent_days)
        self.start_uploader()
        self.log_offset = self.log_path.stat().st_size if self.log_path.exists() else 0
        self.backlog, self.t_end, self.notes = set(), 0.0, {}
        if live:
            self.start_recorder()
            time.sleep(self.T(8))
        self.manifest = acc.snapshot(self.cfg)            # the files that exist now must stay exactly as they are

    def after_fault(self) -> None:
        """Call at the moment the fault is over (line back, process started, storage healthy again)."""
        self.t_end = time.time()
        try:
            self.backlog = self.local_keys() - self.uploaded_keys()
        except sqlite3.Error:
            self.backlog = self.local_keys()
        self.notes["waiting_at_end_of_fault"] = len(self.backlog)

    def wait_backlog(self, timeout: float) -> float | None:
        """Seconds from the end of the fault until everything that was waiting is uploaded."""
        if not self.backlog:
            return 0.0
        ok = self.wait(lambda: self.backlog <= self.uploaded_keys(), timeout, every=1.0)
        return round(time.time() - self.t_end, 1) if ok else None

    def settle(self, timeout: float) -> bool:
        """Stop the recorder and wait until the queue is empty and every index record is handed over."""
        self.stop_recorder()
        keys = self.local_keys()
        return self.wait(lambda: keys <= self.uploaded_keys()
                         and self.query("SELECT COUNT(*) FROM segments WHERE state='uploaded' AND index_sent=0")[0][0] == 0,
                         timeout, every=1.0)

    def wait_uploading(self, timeout: float = 40) -> bool:
        return self.wait(lambda: self.count_state("uploading") > 0, timeout, every=0.05)


# ============================================================================================
# The 18 tests. Each `run` does the fault and calls rig.after_fault() when it is over.
# `post(rig, events)` returns extra expectations: [(what, ok, detail)].
# ============================================================================================
@dataclass
class Test:
    id: str
    group: str
    title: str
    run: Callable[[Rig], None]
    post: Callable[[Rig, list], list] = lambda rig, ev: []
    live: bool = True
    long: bool = False
    backoff_max: float | None = None


def count(events, **match) -> int:
    return sum(all(e.get(k) == v for k, v in match.items()) for e in events)


def outage_seen(rig, ev):
    n = count(ev, event="upload_failed", kind="outage")
    return [("the uploader noticed the outage and waited", n > 0, f"{n} failed attempts logged as outage")]


def not_hammering(rig, ev, mode="down", fault_s=0.0):
    n = rig.proxy.requests.get(mode, 0)
    limit = int(12 + fault_s)
    return [("the uploader did not hammer the storage", n <= limit,
             f"{n} requests reached the line during the {fault_s:.0f} s fault (limit {limit})")]


# ---- internet cuts -------------------------------------------------------------------------
def cut(seconds: float):
    def run(rig: Rig):
        # never shorter than one and a half recorder rounds, so the cut always meets an upload attempt
        s = max(rig.T(seconds), rig.a.interval * 1.5 + 2)
        rig.proxy.set_mode("down")
        time.sleep(s)
        rig.notes["fault_s"] = s
        rig.proxy.set_mode("ok")
        rig.after_fault()
    return run


def post_cut(rig, ev):
    return outage_seen(rig, ev) + not_hammering(rig, ev, "down", rig.notes.get("fault_s", 0))


def run_flaps(rig: Rig):
    rig.proxy.delay_before_put = 0.4                     # uploads are in flight when the line goes
    for _ in range(5):
        rig.wait_uploading(rig.a.interval + 10)          # cut the line while an upload is really in flight
        rig.proxy.set_mode("down")
        time.sleep(rig.T(3))
        rig.proxy.set_mode("ok")
        time.sleep(rig.T(4))
    rig.proxy.delay_before_put = 0.0
    rig.notes["fault_s"] = 5 * rig.T(3)
    rig.after_fault()


def run_agreed_outage(rig: Rig):
    a = rig.a
    days = a.outage_days
    pile_min = int(days * 1440)
    rig.proxy.set_mode("down")
    t0 = time.time()
    say(f"  C5: writing {pile_min * CAMERAS} segments ({days:g} days x {CAMERAS} cameras) while the storage is unreachable ...")
    rig.pile_keys = write_pile(rig, pile_min, a.outage_segment_kb)
    rig.notes.update(pile_segments=len(rig.pile_keys), pile_write_s=round(time.time() - t0, 1))
    say(f"  C5: keeping the storage unreachable for {a.outage_minutes:g} minutes of real time (uploader waits grow) ...")
    time.sleep(max(0.0, a.outage_minutes * 60 - (time.time() - t0)))
    rig.notes["fault_s"] = round(time.time() - t0, 1)
    rig.proxy.set_mode("ok")
    rig.start_recorder()                                 # live segments arrive again, behind the pile
    rig.after_fault()
    rig.backlog = rig.backlog & rig.pile_keys            # the catch-up time is the time for the pile
    rig.notes["pile_waiting"] = len(rig.backlog)


def write_pile(rig: Rig, minutes: int, kb: float) -> set[str]:
    """Segments for `minutes` minutes of recording from every camera, ending a minute ago (the files of an outage).

    Returns their object keys, so the pile can be told apart from segments of earlier tests."""
    end = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(seconds=90)
    start = end - timedelta(minutes=minutes - 1)
    size = max(int(kb * 1024), 1)
    keys: set[str] = set()
    for cam in range(1, CAMERAS + 1):
        cid = NAME_FORMAT.format(cam)
        for m in range(minutes):
            t = start + timedelta(minutes=m)
            d = rig.rec / cid / t.strftime("%Y-%m-%d")
            d.mkdir(parents=True, exist_ok=True)
            stamp, stop = t.strftime('%Y%m%dT%H%M%S'), (t + timedelta(seconds=60)).strftime('%Y%m%dT%H%M%S')
            final = d / f"{cid}_{stamp}Z_{stop}Z.mp4"    # the name the M7 archiver writes: start and end
            if final.exists():                           # a segment of an earlier test with the same second: never rewrite it
                continue
            part = d / f"{cid}_{stamp}Z.mp4.part"
            part.write_bytes(os.urandom(size))
            os.replace(part, final)
            keys.add(f"{rig.prefix}/{cid}/{d.name}/{final.name}")
    return keys


def post_agreed(rig, ev):
    out = outage_seen(rig, ev)
    waits = [float(e["retry_in"].rstrip("s")) for e in ev if e.get("event") == "upload_failed" and "retry_in" in e]
    rig.notes["longest_wait_s"] = max(waits) if waits else 0
    # the pile must have gone out oldest first, camera by camera
    rows = [r for r in rig.query("SELECT key, camera_id, start_utc, uploaded_at FROM segments WHERE state='uploaded' "
                                 "ORDER BY camera_id, start_utc") if r[0] in rig.pile_keys]
    bad, prev = 0, {}
    for _, cam, _, up in rows:
        if cam in prev and up < prev[cam] - 1e-6:
            bad += 1
        prev[cam] = up
    out.append(("the pile went out oldest first, camera by camera", bad == 0 and len(rows) == len(rig.pile_keys),
                f"{bad} segments out of order, {len(rows)} of {len(rig.pile_keys)} uploaded"))
    if rows:
        ups = [r[3] for r in rows]
        rig.notes["first_upload_after_fault_s"] = round(min(ups) - rig.t_end, 1)
        span = max(max(ups) - min(ups), 1e-3)
        rig.notes["pile_objects_per_s"] = round(len(rows) / span, 1)
        rig.notes["pile_mb_per_s"] = round(len(rows) * rig.a.outage_segment_kb / 1024 / span, 2)
    return out


# ---- gateway reboots -----------------------------------------------------------------------
def reboot_in_upload(down_s: float):
    def run(rig: Rig):
        rig.proxy.delay_before_put = 1.5                 # an upload is in flight when the power goes
        hit = rig.wait_uploading()
        rig.notes["killed_during_upload"] = hit
        rig.reboot(rig.T(down_s))
        rig.proxy.delay_before_put = 0.0
        rig.after_fault()
    return run


def run_reboot_idle(down_s: float):
    def run(rig: Rig):
        rig.reboot(rig.T(down_s))
        rig.after_fault()
    return run


def run_reboot_twice(rig: Rig):
    rig.reboot(rig.T(8))
    time.sleep(rig.T(15))
    rig.reboot(rig.T(8))
    rig.after_fault()


def run_reboot_during_outage(rig: Rig):
    rig.proxy.set_mode("down")
    time.sleep(rig.T(15))
    rig.reboot(rig.T(15))                                # the gateway boots with the internet still down
    time.sleep(rig.T(10))
    rig.notes["fault_s"] = rig.T(40)
    rig.proxy.set_mode("ok")
    rig.after_fault()


def post_interrupted(rig, ev):
    n = sum(int(e.get("recovered_interrupted", 0)) for e in ev if e.get("event") == "startup")
    return [("the restart found the interrupted upload and did it again", n >= 1,
             f"{n} interrupted upload(s) recovered at start-up")]


# ---- uploader kills ------------------------------------------------------------------------
def run_kill_in_upload(rig: Rig):
    rig.proxy.delay_before_put = 1.5
    rig.notes["killed_during_upload"] = rig.wait_uploading()
    rig.kill_uploader()
    rig.proxy.delay_before_put = 0.0
    time.sleep(rig.T(3))
    rig.start_uploader()
    rig.after_fault()


def run_kill_after_store(rig: Rig):
    rig.proxy.delay_after_put = 1.5                      # the object is stored, the answer is held back
    rig.notes["killed_after_stored"] = rig.proxy.held.wait(40)
    rig.kill_uploader()
    rig.proxy.delay_after_put = 0.0
    time.sleep(rig.T(3))
    rig.start_uploader()
    rig.after_fault()


def post_already_there(rig, ev):
    n = count(ev, event="already_present")
    return [("the restart found the object already stored and did not upload it again", n >= 1,
             f"{n} 'already_present' line(s)")]


def run_kill_long_queue(rig: Rig):
    rig.proxy.set_mode("down")                           # build a queue
    time.sleep(rig.T(25))
    rig.proxy.set_mode("ok")
    rig.proxy.delay_before_put = 0.5                     # and let it drain slowly
    rig.wait(lambda: rig.count_state("uploading") > 0 and rig.count_state("pending") >= 15, 70, every=0.1)
    rig.notes["queue_at_kill"] = rig.count_state("pending") + rig.count_state("uploading")
    rig.kill_uploader()
    rig.proxy.delay_before_put = 0.0
    time.sleep(rig.T(2))
    rig.start_uploader()
    rig.after_fault()


def run_kill_three_times(rig: Rig):
    for _ in range(3):
        rig.start_uploader()
        time.sleep(rig.T(3))
        rig.kill_uploader()
    rig.start_uploader()
    rig.after_fault()


def run_kill_and_stay_down(rig: Rig):
    rig.kill_uploader()
    time.sleep(rig.T(90))
    rig.start_uploader()
    rig.after_fault()


# ---- storage errors ------------------------------------------------------------------------
def storage_error(mode: str, seconds: float):
    def run(rig: Rig):
        s = rig.T(seconds)
        rig.proxy.set_mode(mode)
        time.sleep(s)
        rig.notes["fault_s"] = s
        rig.proxy.set_mode("ok")
        rig.after_fault()
    return run


def post_auth(rig, ev):
    n = count(ev, event="storage_refused_credentials")
    return ([("the uploader said loudly that the credentials were refused", n >= 1, f"{n} ERROR line(s)")]
            + not_hammering(rig, ev, "auth", rig.notes.get("fault_s", 0)))


def post_full(rig, ev):
    n = count(ev, event="storage_full")
    return ([("the uploader said loudly that the storage is full", n >= 1, f"{n} ERROR line(s)")]
            + not_hammering(rig, ev, "full", rig.notes.get("fault_s", 0)))


def run_server_errors(rig: Rig):
    s = rig.T(40)
    rig.proxy.set_mode("error")
    time.sleep(s)
    rig.proxy.set_mode("flaky")
    time.sleep(s)
    rig.notes["fault_s"] = 2 * s
    rig.proxy.set_mode("ok")
    rig.after_fault()


def tests(a) -> list[Test]:
    return [
        Test("C1", "Internet cut", "Internet cut for 15 s", cut(15), post_cut),
        Test("C2", "Internet cut", "Internet cut for 1 minute", cut(60), post_cut),
        Test("C3", "Internet cut", "Five short cuts of 3 s, uploads cut in the middle", run_flaps, outage_seen),
        Test("C4", "Internet cut", "Internet cut for 5 minutes", cut(300), post_cut),
        Test("C5", "Internet cut", f"Outage of the agreed length: {a.outage_days:g} days of video (compressed)",
             run_agreed_outage, post_agreed, live=False, long=True, backoff_max=300),
        Test("R1", "Gateway reboot", "Power cut during an upload, 10 s down", reboot_in_upload(10), post_interrupted),
        Test("R2", "Gateway reboot", "Reboot, 30 s down", run_reboot_idle(30)),
        Test("R3", "Gateway reboot", "Two reboots within a minute", run_reboot_twice),
        Test("R4", "Gateway reboot", "Reboot while the internet is also down", run_reboot_during_outage, outage_seen),
        Test("R5", "Gateway reboot", "Reboot, 2 minutes down", run_reboot_idle(120)),
        Test("K1", "Uploader killed", "Killed in the middle of an upload", run_kill_in_upload, post_interrupted),
        Test("K2", "Uploader killed", "Killed after the storage stored the file, before the uploader noted it",
             run_kill_after_store, post_already_there),
        Test("K3", "Uploader killed", "Killed with a long queue", run_kill_long_queue),
        Test("K4", "Uploader killed", "Killed three times in a row", run_kill_three_times),
        Test("K5", "Uploader killed", "Killed and left off for 90 s", run_kill_and_stay_down),
        Test("S1", "Storage error", "Storage refuses the credentials (403) for 1 minute", storage_error("auth", 60), post_auth),
        Test("S2", "Storage error", "Storage is full (507) for 1 minute", storage_error("full", 60), post_full),
        Test("S3", "Storage error", "Storage server errors (500), then every second request failing",
             run_server_errors, outage_seen),
    ]


RUN_ORDER = ["S1", "S2", "S3", "K1", "K2", "K3", "K4", "K5", "R1", "R2", "R3", "R4", "R5", "C1", "C2", "C3", "C4", "C5"]


# ============================================================================================
# Running and reporting
# ============================================================================================
def run_test(rig: Rig, t: Test) -> dict:
    a = rig.a
    res = {"id": t.id, "group": t.group, "title": t.title, "checks": [], "notes": {}}
    started = time.time()
    say(f"== {t.id}: {t.title}")
    try:
        rig.begin(live=t.live, backoff_max=t.backoff_max, recent_days=(math.ceil(a.outage_days) + 2) if t.long else 2)
        t.run(rig)
        timeout = (a.catchup_timeout_min * 60) if t.long else max(120.0, 4 * rig.conf_key[0] + 60)
        res["catchup_s"] = rig.wait_backlog(timeout)
        res["waiting_at_end_of_fault"] = rig.notes.get("waiting_at_end_of_fault", 0)
        res["checks"].append(("everything that was waiting got uploaded without anyone touching it",
                              res["catchup_s"] is not None,
                              f"{res['catchup_s']} s" if res["catchup_s"] is not None else f"not finished after {timeout:.0f} s"))
        settled = rig.settle(timeout)
        res["checks"].append(("the queue emptied after the recorder stopped", settled, "yes" if settled else "no"))
        events = rig.events_since(rig.log_offset)
        for name, ok, detail in t.post(rig, events):
            res["checks"].append((name, ok, detail))
        say(f"  checking from the outside (M19 checks, strict) ...")
        chk, info = acc.verify(rig.cfg, rig.manifest, acc.read_log(str(rig.log_path)), deep=False, grace=0.0)
        for name, ok, detail in chk.rows:
            res["checks"].append((name, ok, detail))
        g = gap_report.find_gaps(rig.cfg, gap_report.unfinished_rows(rig.cfg.state_db))
        res["checks"].append(("gap report: no segment lost", g["gap_segments"] == 0 and g["waiting"] == 0,
                              "no gaps, nothing waiting" if g["gap_segments"] == 0 and g["waiting"] == 0
                              else gap_report.format_text(g).replace("\n", " | ")))
        res["segments_total"] = info.get("local_files")
        stats = [e for e in events if e.get("event") == "stats"]
        if stats:
            res["cpu_max"] = max(float(e["cpu_pct"]) for e in stats)
            res["rss_max"] = max(float(e["rss_mb"]) for e in stats)
    except Exception as exc:                              # a broken test must not stop the others
        res["checks"].append(("the test ran to the end", False, f"{type(exc).__name__}: {exc}"))
    finally:
        rig.stop_recorder()
        rig.proxy.set_mode("ok")
        rig.proxy.delay_before_put = rig.proxy.delay_after_put = 0.0
    res["notes"] = dict(rig.notes)
    res["seconds"] = round(time.time() - started, 1)
    res["pass"] = all(ok for _, ok, _ in res["checks"])
    bad = [f"{n}: {d}" for n, ok, d in res["checks"] if not ok]
    say(f"  {'PASS' if res['pass'] else 'FAIL'}  catch-up {res.get('catchup_s')} s, test took {res['seconds']:.0f} s"
        + ("" if res["pass"] else "\n    " + "\n    ".join(bad)))
    return res


def markdown(results: list[dict], a, rig: Rig) -> str:
    order = {i: n for n, i in enumerate(["C1", "C2", "C3", "C4", "C5", "R1", "R2", "R3", "R4", "R5",
                                         "K1", "K2", "K3", "K4", "K5", "S1", "S2", "S3"])}
    results = sorted(results, key=lambda r: order.get(r["id"], 99))
    passed = sum(r["pass"] for r in results)
    out = ["# M20 recovery test report", "",
           f"Generated {datetime.now(timezone.utc):%Y-%m-%d %H:%M:%SZ}. Storage `{rig.upstream}`, bucket `{rig.cfg.bucket}`, "
           f"prefix `{rig.prefix}`.", "",
           f"**Result: {passed} of {len(results)} tests passed.**" + ("" if passed == len(results) else " **SOME FAILED.**"), "",
           "## Tests", "", "| # | Test | Waiting at end of fault | Catch-up | Result |", "|---|---|---|---|---|"]
    for r in results:
        cu = "-" if r.get("catchup_s") is None else (f"{r['catchup_s']:.0f} s" if r["catchup_s"] < 3600 else f"{r['catchup_s'] / 3600:.2f} h")
        out.append(f"| {r['id']} | {r['title']} | {r.get('waiting_at_end_of_fault', '-')} segments | {cu} | "
                   f"{'PASS' if r['pass'] else '**FAIL**'} |")
    out += ["", "What was checked after every test (all must hold): everything that was waiting got uploaded without anyone "
            "touching it; then, from the outside and in strict mode: every segment uploaded, size / ETag / SHA-256 equal, the bucket "
            "holds exactly the uploaded segments, none stuck, none uploaded twice, index records complete, local files unchanged; "
            "the gap report shows no lost segment. Test-specific expectations are listed below.", ""]
    for r in results:
        extra = [(n, ok, d) for n, ok, d in r["checks"]
                 if n.startswith(("the uploader", "the restart", "the pile", "the test"))]
        notes = {k: v for k, v in r["notes"].items() if k not in ("waiting_at_end_of_fault",)}
        if extra or notes or not r["pass"]:
            parts = [f"{n}: {'yes' if ok else 'NO'} ({d})" for n, ok, d in extra]
            if notes:
                parts.append(f"measured: {json.dumps(notes)}")
            out.append(f"**{r['id']}** " + "; ".join(parts))
            for n, ok, d in r["checks"]:
                if not ok and n not in [e[0] for e in extra]:
                    out.append(f"  - FAILED: {n} - {d}")
            out.append("")
    c5 = next((r for r in results if r["id"] == "C5"), None)
    out += ["## Catch-up after an outage of the agreed length", ""]
    if c5 and c5.get("catchup_s") is not None:
        n = c5["notes"]
        out += [f"Compressed test: {n.get('pile_segments')} segments of {a.outage_segment_kb:g} KB ({a.outage_days:g} days x {CAMERAS} cameras) "
                f"were written while the storage was unreachable for {n.get('fault_s')} s of real time. "
                f"The uploader's longest wait before a retry was {n.get('longest_wait_s')} s.",
                f"- First upload {n.get('first_upload_after_fault_s')} s after the storage came back (this is the uploader's wait, "
                f"at most the longest backoff).",
                f"- All {n.get('pile_waiting')} waiting segments uploaded {c5['catchup_s']:.0f} s after the storage came back; "
                f"{n.get('pile_objects_per_s')} segments per second ({n.get('pile_mb_per_s')} MB/s) on this machine and storage, with the live "
                f"recorder running at the same time.", "",
                "The test files are small (the time is dominated by the work per segment: hash, check, upload, read back, note). With real 1 MB "
                "segments the time is set by the upload speed instead, so for Vivo it is calculated from the agreed speed:", ""]
    else:
        out += ["The agreed-length test was not run or did not finish; see its row above.", ""]
    out += [retention.table(a.outage_days), "",
            f"(pile of {retention.pile_gb(a.outage_days, CAMERAS, a.real_segment_mb):.1f} GB; live rate "
            f"{retention.live_mbps(CAMERAS, a.real_segment_mb):.2f} Mbps; plus up to 5 minutes before the first retry)", "",
            "## Retention recommendation for M9", "",
            retention.recommendation(a.outage_days, a.speed_mbps, CAMERAS, a.real_segment_mb), "",
            "The uploader sends the oldest segment first, so the catch-up time does not make old files older; what must hold is "
            "`retention >= outage + margin`. The margin is a choice (late detection, gateway down at the same time), not a measurement.", "",
            "## How the tests were run, and what they do not show", "",
            f"- The uploader and the fake recorder (10 cameras, `{a.size_mb:g}` MB every `{a.interval:g}` s) ran as processes of the test script; "
            f"the settings are those of the gateway except: wait between retries at most {a.backoff_max:g} s (C5: 300 s, the gateway value), "
            "scan every second.",
            "- A reboot is simulated by killing the recorder and the uploader at the same moment with SIGKILL (no clean-up), waiting, and "
            "starting them again; the state folder and the recordings stay. While the gateway is off, nothing is recorded: that hole "
            "is not an uploader loss and is not in the gap report.",
            "- Internet cuts, refused credentials, a full storage and server errors are made by a test proxy between the uploader and the "
            "storage. The uploader's reaction is the real code; the proxy is a stand-in for the network and for the provider's error replies.",
            "- C5 is compressed: the segments of the whole outage are written at once, and the storage stays unreachable for minutes, not days. "
            "To run the full time on the gateway use `--outage-minutes` (2 days = 2880).",
            "- A segment that the recorder removed before the uploader had ever seen it cannot be reported; "
            "the retention recommendation exists to prevent that.", ""]
    return "\n".join(out)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--work", default=os.environ.get("M20_WORK", "m20-work"), help="folder for the test's own files")
    ap.add_argument("--out-dir", default=None, help="where the report goes (default: <work>/report)")
    ap.add_argument("--only", help="comma separated test ids, e.g. C1,K2")
    ap.add_argument("--list", action="store_true", help="list the tests and stop")
    ap.add_argument("--quick", action="store_true", help="a short smoke run (shorter faults, a small pile)")
    ap.add_argument("--scale", type=float, default=1.0, help="multiply all fault durations (0.3 = faster, less strict)")
    ap.add_argument("--outage-days", type=float, default=2.0, help="agreed outage length for C5 (default 2)")
    ap.add_argument("--outage-minutes", type=float, default=10.0, help="real minutes the storage stays down in C5")
    ap.add_argument("--outage-segment-kb", type=float, default=20.0, help="size of the segments in the C5 pile")
    ap.add_argument("--catchup-timeout-min", type=float, default=180.0)
    ap.add_argument("--speed-mbps", type=float, default=None, help="agreed upload speed, for the calculated catch-up")
    ap.add_argument("--real-segment-mb", type=float, default=1.0, help="size of a real segment per camera per minute")
    ap.add_argument("--interval", type=float, default=5.0, help="seconds between recorder segments")
    ap.add_argument("--size-mb", type=float, default=0.25, help="size of a recorder segment")
    ap.add_argument("--backoff-max", type=float, default=30.0, help="longest wait between retries in the tests (gateway: 300)")
    a = ap.parse_args(argv)
    if a.quick:
        a.scale = min(a.scale, 0.25)
        a.outage_days, a.outage_minutes = (0.05 if a.outage_days == 2.0 else a.outage_days), 1.0
        a.backoff_max = min(a.backoff_max, 10.0)
    all_tests = {t.id: t for t in tests(a)}
    if a.list:
        for t in all_tests.values():
            print(f"{t.id}  {t.group:16} {t.title}")
        return 0
    wanted = [i.strip().upper() for i in a.only.split(",")] if a.only else RUN_ORDER
    unknown = [i for i in wanted if i not in all_tests]
    if unknown:
        print(f"unknown test id(s): {', '.join(unknown)}", file=sys.stderr)
        return 2
    for var in ("S3_ENDPOINT_URL", "S3_BUCKET", "S3_ACCESS_KEY", "S3_SECRET_KEY"):
        if not os.environ.get(var):
            print(f"{var} is not set", file=sys.stderr)
            return 2

    rig = Rig(a)
    out_dir = Path(a.out_dir) if a.out_dir else rig.work / "report"
    out_dir.mkdir(parents=True, exist_ok=True)
    say(f"M20 recovery tests: {', '.join(i for i in RUN_ORDER if i in wanted)} -> prefix {rig.prefix}")
    results = []
    for tid in [i for i in RUN_ORDER if i in wanted]:
        results.append(run_test(rig, all_tests[tid]))
        (out_dir / "m20-recovery-results.json").write_text(json.dumps(results, indent=1, default=str))
        (out_dir / "m20-recovery-report.md").write_text(markdown(results, a, rig), encoding="utf-8")
    rig.shutdown()
    passed = sum(r["pass"] for r in results)
    say(f"RESULT: {'PASS' if passed == len(results) else 'FAIL'}  ({passed} of {len(results)} tests passed)")
    say(f"report: {out_dir / 'm20-recovery-report.md'}")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
