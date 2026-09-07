#!/usr/bin/env python3
"""Unit tests for the lid-watchdog decision core and ioreg parser.

Run: python3 -m pytest tests/test_lid_watchdog.py

Only the pure functions are tested — parse_clamshell() and decide(). Service
control and the live ioreg call are thin I/O shims.
"""

import importlib.util
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "lidwd", Path(__file__).resolve().parents[1] / "lid-watchdog.py"
)
lidwd = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lidwd)


# Minimal ioreg snippets — the key and Yes/No are all the parser needs.
# No hardware identifiers: a desktop tree simply lacks the key.
CLOSED = """+-o IOPMrootDomain  <class IOPMrootDomain>
  |   "AppleClamshellState" = Yes
"""
OPEN = """+-o IOPMrootDomain  <class IOPMrootDomain>
  |   "AppleClamshellState" = No
"""
DESKTOP = """+-o IOPMrootDomain  <class IOPMrootDomain>
  |   "IOSleepSupported" = Yes
"""


class ParseClamshellTest(unittest.TestCase):
    def test_yes_means_lid_closed(self):
        self.assertIs(lidwd.parse_clamshell(CLOSED), True)

    def test_no_means_lid_open(self):
        self.assertIs(lidwd.parse_clamshell(OPEN), False)

    def test_missing_key_means_not_a_laptop(self):
        self.assertIs(lidwd.parse_clamshell(""), None)
        self.assertIs(lidwd.parse_clamshell(DESKTOP), None)

    def test_does_not_match_sibling_clamshell_keys(self):
        # AppleClamshellCausesSleep is a different IOPM key; it must not count.
        text = '  |   "AppleClamshellCausesSleep" = Yes\n'
        self.assertIs(lidwd.parse_clamshell(text), None)


class DecideTest(unittest.TestCase):
    def decide(self, lid_closed, paused=(), idle=(), load_paused=()):
        return lidwd.decide(lid_closed, paused, idle, load_paused=load_paused)

    def test_closed_pauses_idle_runners(self):
        p = self.decide(True, idle=["r-1", "r-2"])
        self.assertEqual(p.to_pause, ["r-1", "r-2"])
        self.assertEqual(p.to_resume, [])

    def test_closed_skips_already_paused(self):
        p = self.decide(True, paused=["r-1"], idle=["r-1", "r-2"])
        self.assertEqual(p.to_pause, ["r-2"])

    def test_closed_does_not_stop_busy_runners(self):
        # A mid-job runner is not in `idle`, so it is never stopped.
        p = self.decide(True, idle=[])
        self.assertEqual(p.to_pause, [])

    def test_closed_never_resumes(self):
        p = self.decide(True, paused=["r-1"], idle=[])
        self.assertEqual(p.to_resume, [])

    def test_open_resumes_what_this_watchdog_paused(self):
        p = self.decide(False, paused=["r-1", "r-2"])
        self.assertEqual(p.to_resume, ["r-1", "r-2"])
        self.assertEqual(p.to_pause, [])

    def test_open_does_not_resume_load_watchdog_pauses(self):
        # load-watchdog still wants these offline; do not fight it.
        p = self.decide(False, paused=["r-1", "r-2"], load_paused=["r-1"])
        self.assertEqual(p.to_resume, ["r-2"])

    def test_not_a_laptop_is_noop(self):
        p = self.decide(None, paused=["r-1"], idle=["r-2"])
        self.assertEqual((p.to_pause, p.to_resume), ([], []))


if __name__ == "__main__":
    unittest.main()
