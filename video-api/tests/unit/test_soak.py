"""The 72-hour test's pass/fail rules (tests/soak72.py), on made-up logs."""
from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("soak72", ROOT / "tests" / "soak72.py")
soak = importlib.util.module_from_spec(spec)
spec.loader.exec_module(soak)

H = 3600.0
START, END = 0.0, 72 * H
EVENTS = [{"kind": "api_restart", "start": 6 * H, "end": 6 * H + 12, "recording_back_s": 0.0, "features_back_s": 20.0},
          {"kind": "stack_restart", "start": 12.5 * H, "end": 12.5 * H + 25, "recording_back_s": 31.0, "features_back_s": 40.0}]


def samples(mem_late: float = 300.0, cpu: float = 50.0):
    out = []
    t = 0.0
    while t < END:
        mem = 300.0 if t < 60 * H else mem_late
        out.append({"t": t, "cpu": cpu, "mem": {"frigate": mem, "video-api": 80.0}})
        t += 60
    return out


def test_gaps_during_planned_restarts_do_not_count():
    gaps = [(12.5 * H - 2, 12.5 * H + 40), (30 * H, 30 * H + 8), (40 * H, 40 * H + 15)]
    assert soak.gaps_outside(gaps, EVENTS) == [(40 * H, 40 * H + 15)]


def test_actions_in_restart_windows_are_not_counted():
    actions = [{"t": 6 * H + 30, "ok": False}, {"t": 7 * H, "ok": True}, {"t": 8 * H, "ok": True, "skipped": True}]
    assert soak.success_rate(actions, EVENTS) == (1, 1)


def test_memory_growth_and_cpu():
    grew = soak.memory_growth(samples(mem_late=350.0), START, END)
    assert grew["frigate"] == (300.0, 350.0, 1.167) and grew["video-api"][2] == 1.0
    assert soak.cpu_peak_5min(samples(cpu=50.0)) == 50.0
    s = samples(cpu=50.0)
    for x in s[100:106]:
        x["cpu"] = 95.0
    assert soak.cpu_peak_5min(s) >= 85


def test_evaluate_pass_and_fail():
    actions = [{"t": t * 60.0, "ok": True} for t in range(1000)]
    checks = soak.evaluate(EVENTS, actions, samples(), {"cam01": []}, START, END)
    assert all(ok for _, ok, _ in checks), checks
    slow = [dict(EVENTS[0], recording_back_s=75.0)]
    checks = dict((n, ok) for n, ok, _ in soak.evaluate(slow, actions, samples(mem_late=340.0), {"cam01": [(50 * H, 50 * H + 30)]}, START, END))
    assert not checks["no recording gap over 10 s outside planned restarts"]
    assert not checks["recording back within 60 s after each restart"]
    assert not checks["memory: last 12 h within 10% of hours 12 to 24"]
