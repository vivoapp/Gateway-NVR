"""Unit tests for the M11 power runner's times (tests/m11_faults.py): no gateway, no plug.

With an operator the cut and power-on times must err only towards a worse result: the cut time is
taken when the cut is confirmed, the power-on time before the power is switched on
(milestone-4-results.md, power cuts of Nov 20, 2026).

usage: python3 -m unittest discover -s tests/unit -v
"""
import builtins
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
import m11_faults  # noqa: E402


class Clock:
    """A fake time.time and time.sleep; the operator's prompts advance it as a person would."""

    def __init__(self, t: float = 1000.0):
        self.t = t

    def time(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.t += s


class FakeRemote:
    def __init__(self, clock: Clock, goes_down: bool = True):
        self.clock_ = clock
        self.goes_down = goes_down

    def wait_down(self, timeout: float):
        if self.goes_down:
            return self.clock_.t
        self.clock_.t += timeout
        return None


def operator(clock: Clock, reads: float, acts: float, confirms: float, log: list):
    """input(): the operator reads the prompt, switches the power, then presses Enter."""
    def answer(prompt: str) -> str:
        clock.t += reads
        log.append(("OFF" if "OFF" in prompt else "ON", clock.t + acts))   # when the power really changes
        clock.t += acts + confirms
        return ""
    return answer


class OperatorTimes(unittest.TestCase):
    def run_cut(self, reads=20.0, acts=2.0, confirms=5.0, goes_down=True, offset=0.0):
        clock, real = Clock(), []
        with mock.patch.object(m11_faults.time, "time", clock.time), \
                mock.patch.object(m11_faults.time, "sleep", clock.sleep), \
                mock.patch.object(builtins, "input", operator(clock, reads, acts, confirms, real)):
            t_cut, t_on, down = m11_faults.cut_and_restore(FakeRemote(clock, goes_down), 15, None, None, offset)
        return t_cut, t_on, down, dict(real)

    def test_cut_time_is_at_or_after_the_real_cut(self):
        # Nov 20: the old runner noted the cut 22 to 41 s before the real one (when it asked).
        t_cut, _, down, real = self.run_cut(reads=30, acts=2, confirms=4)
        self.assertTrue(down)
        self.assertGreaterEqual(t_cut, real["OFF"])
        self.assertLessEqual(t_cut - real["OFF"], 4.0)

    def test_power_on_time_is_at_or_before_the_real_power_on(self):
        # Nov 20: the old runner noted the power-on 8 to 34 s after the real one (when Enter came).
        _, t_on, _, real = self.run_cut(reads=25, acts=3, confirms=10)
        self.assertLessEqual(t_on, real["ON"])

    def test_times_are_on_the_gateways_clock(self):
        t_cut, t_on, _, real = self.run_cut(offset=7.5)
        self.assertAlmostEqual(t_cut - 7.5, real["OFF"] + 5.0)
        self.assertLess(t_on - 7.5, real["ON"])

    def test_a_cut_confirmed_too_early_is_flagged(self):
        _, _, down, _ = self.run_cut(goes_down=False)
        self.assertFalse(down)


class PlugTimes(unittest.TestCase):
    def test_plug_commands_bracket_the_same_way(self):
        clock, calls = Clock(), []

        def sh(cmd, timeout=120):
            clock.t += 0.5                      # the plug answers half a second later
            calls.append((cmd, clock.t))
            return mock.Mock(returncode=0, stderr="")

        with mock.patch.object(m11_faults.time, "time", clock.time), \
                mock.patch.object(m11_faults.time, "sleep", clock.sleep), \
                mock.patch.object(m11_faults, "sh", sh):
            t_cut, t_on, down = m11_faults.cut_and_restore(FakeRemote(clock), 15, "plug off", "plug on", 0.0)
        self.assertEqual([c for c, _ in calls], ["plug off", "plug on"])
        self.assertGreaterEqual(t_cut, calls[0][1])          # after the plug confirmed the cut
        self.assertLess(t_on, calls[1][1])                   # before the plug switched on
        self.assertTrue(down)


if __name__ == "__main__":
    unittest.main()
