#!/usr/bin/env python3
"""Unit tests for the load-watchdog decision core.

Run: python3 -m unittest test_load_watchdog   (or: python3 test_load_watchdog.py)

Only the pure `decide()` function is tested — it holds all the policy (thresholds,
debounce, dead-band, resume). Service control and idle detection are thin I/O
shims exercised by the manual fleet check in the design doc.
"""

import importlib.util
import unittest
from pathlib import Path

# load-watchdog.py isn't a valid module name; import via importlib spec.

spec = importlib.util.spec_from_file_location(
    "lw", Path(__file__).resolve().parents[1] / "load-watchdog.py"
)
lw = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lw)

# Bind defaults so tests read against the same constants the script ships with.
LOW, HIGH, DEBOUNCE = lw.LOW, lw.HIGH, lw.DEBOUNCE


class DecideTest(unittest.TestCase):
    def decide(self, load, high_ticks=0, paused=(), idle=()):
        return lw.decide(
            load, high_ticks, paused, idle, low=LOW, high=HIGH, debounce=DEBOUNCE
        )

    # ── Pausing: requires sustained high load AND idle runners ────────────────

    def test_high_tick_reaching_debounce_pauses_idle_runners(self):
        # The tick that brings the counter up to DEBOUNCE is the one that pauses
        # (with DEBOUNCE=1 that is the very first high tick).
        p = self.decide(HIGH + 0.5, high_ticks=DEBOUNCE - 1, idle=["r-1", "r-2"])
        self.assertEqual(p.to_pause, ["r-1", "r-2"])
        self.assertEqual(p.high_ticks, DEBOUNCE)

    def test_high_tick_below_debounce_only_counts(self):
        # A high tick that does not yet reach debounce advances the counter, no pause.
        p = lw.decide(HIGH + 0.5, 0, [], ["r-1", "r-2"], low=LOW, high=HIGH, debounce=2)
        self.assertEqual(p.to_pause, [])
        self.assertEqual(p.high_ticks, 1)

    def test_pause_skips_already_paused(self):
        p = self.decide(HIGH + 0.5, high_ticks=1, paused=["r-1"], idle=["r-1", "r-2"])
        self.assertEqual(p.to_pause, ["r-2"])  # r-1 already paused

    def test_no_idle_runners_means_nothing_to_pause(self):
        # A busy runner mid-job is not in `idle`, so it is never stopped.
        p = self.decide(HIGH + 5, high_ticks=5, idle=[])
        self.assertEqual(p.to_pause, [])

    # ── Dead-band: between LOW and HIGH, hold and reset the pause debounce ─────

    def test_deadband_resets_debounce(self):
        p = self.decide((LOW + HIGH) / 2, high_ticks=1, idle=["r-1"])
        self.assertEqual(p.to_pause, [])
        self.assertEqual(p.to_resume, [])
        self.assertEqual(p.high_ticks, 0)  # one dip resets → needs consecutive highs

    def test_deadband_leaves_paused_runners_paused(self):
        p = self.decide((LOW + HIGH) / 2, high_ticks=0, paused=["r-1"], idle=[])
        self.assertEqual(p.to_resume, [])  # not resumed until load < LOW

    def test_non_consecutive_highs_never_pause(self):
        # high, dead-band, high → the dead-band tick resets the counter, so highs
        # never accumulate to debounce when debounce >= 2.
        p1 = lw.decide(HIGH + 1, 0, [], ["r-1"], low=LOW, high=HIGH, debounce=2)
        self.assertEqual((p1.to_pause, p1.high_ticks), ([], 1))
        p2 = lw.decide(
            (LOW + HIGH) / 2, p1.high_ticks, [], ["r-1"], low=LOW, high=HIGH, debounce=2
        )
        self.assertEqual(p2.high_ticks, 0)
        p3 = lw.decide(
            HIGH + 1, p2.high_ticks, [], ["r-1"], low=LOW, high=HIGH, debounce=2
        )
        self.assertEqual((p3.to_pause, p3.high_ticks), ([], 1))

    # ── Resuming: only below LOW, and only what we paused ─────────────────────

    def test_low_load_resumes_all_paused(self):
        p = self.decide(LOW - 0.5, high_ticks=2, paused=["r-1", "r-2"])
        self.assertEqual(p.to_resume, ["r-1", "r-2"])
        self.assertEqual(p.to_pause, [])
        self.assertEqual(p.high_ticks, 0)  # counter cleared on recovery

    def test_low_load_with_nothing_paused_is_noop(self):
        p = self.decide(LOW - 0.5, high_ticks=0, paused=[])
        self.assertEqual((p.to_pause, p.to_resume, p.high_ticks), ([], [], 0))

    def test_resume_ignores_currently_idle_set(self):
        # Resume is independent of idle detection — we restart what we paused.
        p = self.decide(LOW - 0.5, paused=["r-1"], idle=["r-2"])
        self.assertEqual(p.to_resume, ["r-1"])

    # ── Boundary conditions ───────────────────────────────────────────────────

    def test_exactly_low_is_deadband_not_resume(self):
        # load == LOW is not < LOW, so it falls into the dead-band (no resume).
        p = self.decide(LOW, high_ticks=2, paused=["r-1"])
        self.assertEqual(p.to_resume, [])
        self.assertEqual(p.high_ticks, 0)

    def test_exactly_high_is_deadband_not_pause(self):
        # load == HIGH is not > HIGH, so no debounce advance and no pause.
        p = self.decide(HIGH, high_ticks=1, idle=["r-1"])
        self.assertEqual(p.to_pause, [])
        self.assertEqual(p.high_ticks, 0)


if __name__ == "__main__":
    unittest.main()
