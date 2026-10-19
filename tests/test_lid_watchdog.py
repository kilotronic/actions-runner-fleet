#!/usr/bin/env python3
"""Unit tests for the lid watchdog: its parsers, decision core, and ticks.

Run: python3 -m pytest tests/test_lid_watchdog.py

The pure functions — parse_clamshell(), parse_power_source(), battery_since()
and decide() — are tested directly. tick() and status() run over a fake
laptop: ioreg, pmset and the clock are mocked, and the load watchdog's service
control is replaced by a fake that records what was paused and resumed.

Tests that expect nothing to happen carry a control arm that changes the one
input under test and expects a pause. A zero alone would also pass on a
harness that cannot see a pause, or before the power condition existed.
"""

import contextlib
import importlib.util
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


lidwd = _load("lidwd", "lid-watchdog.py")


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

# Minimal `pmset -g batt` output: the source line, plus the InternalBattery
# line a laptop always lists.
ON_AC = """Now drawing from 'AC Power'
 -InternalBattery-0 (id=1234567)\t100%; charged; 0:00 remaining present: true
"""
ON_BATTERY = """Now drawing from 'Battery Power'
 -InternalBattery-0 (id=1234567)\t85%; discharging; 5:12 remaining present: true
"""
# A desktop lists no InternalBattery. One behind a UPS that is discharging
# reports 'UPS Power', and that must not count: the desktop does not sleep.
NO_BATTERY = "Now drawing from 'AC Power'\n"
UPS_ON_BATTERY = """Now drawing from 'UPS Power'
 -UPS (id=1234567)\t80%; discharging; present: true
"""

MIN = 60
HOUR = 60 * MIN
GRACE = lidwd.BATTERY_GRACE_SECONDS
T0 = 1_800_000_000


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


class ParsePowerSourceTest(unittest.TestCase):
    def test_battery_power_means_on_battery(self):
        self.assertIs(lidwd.parse_power_source(ON_BATTERY), True)

    def test_ac_power_means_on_ac(self):
        self.assertIs(lidwd.parse_power_source(ON_AC), False)

    def test_no_internal_battery_means_not_applicable(self):
        self.assertIs(lidwd.parse_power_source(""), None)
        self.assertIs(lidwd.parse_power_source(NO_BATTERY), None)
        self.assertIs(lidwd.parse_power_source(UPS_ON_BATTERY), None)
        # Control arm: an internal battery on AC is applicable, just not held.
        self.assertIs(lidwd.parse_power_source(ON_AC), False)


class BatterySinceTest(unittest.TestCase):
    def test_clock_starts_at_the_first_battery_reading(self):
        self.assertEqual(lidwd.battery_since(True, None, T0), T0)

    def test_clock_keeps_its_start_while_on_battery(self):
        self.assertEqual(lidwd.battery_since(True, T0, T0 + HOUR), T0)

    def test_any_other_reading_stops_the_clock(self):
        # AC, and None (no battery, opted out, or pmset failed), both clear it.
        self.assertIsNone(lidwd.battery_since(False, T0, T0 + HOUR))
        self.assertIsNone(lidwd.battery_since(None, T0, T0 + HOUR))
        # Control arm: the same running clock, still on battery, is kept.
        self.assertEqual(lidwd.battery_since(True, T0, T0 + HOUR), T0)


class DecideBatteryTest(unittest.TestCase):
    def decide(self, lid_closed, paused=(), idle=(), battery_for=None):
        return lidwd.decide(lid_closed, paused, idle, battery_for=battery_for)

    def test_on_battery_past_grace_pauses_idle_runners(self):
        p = self.decide(False, idle=["r-1", "r-2"], battery_for=GRACE + 1)
        self.assertEqual((p.to_pause, p.to_resume), (["r-1", "r-2"], []))

    def test_on_battery_for_exactly_the_grace_is_not_yet_a_hold(self):
        # "More than 30 minutes": at the grace itself the lid alone decides,
        # and an open lid resumes what the watchdog paused.
        p = self.decide(False, paused=["r-1"], idle=["r-2"], battery_for=GRACE)
        self.assertEqual((p.to_pause, p.to_resume), ([], ["r-1"]))
        # Control arm: one second later it holds.
        p = self.decide(False, paused=["r-1"], idle=["r-2"], battery_for=GRACE + 1)
        self.assertEqual((p.to_pause, p.to_resume), (["r-2"], []))

    def test_battery_hold_does_not_need_a_lid_reading(self):
        # ioreg failing on a laptop reads as "no lid"; the battery still holds.
        p = self.decide(None, idle=["r-1"], battery_for=GRACE + 1)
        self.assertEqual(p.to_pause, ["r-1"])


class FakeLaptop(unittest.TestCase):
    """A laptop with runners r-1 and r-2, both online and idle, lid open, on AC.

    Every path the watchdog touches is redirected into a temp dir. ioreg and
    pmset answer from self.ioreg / self.pmset, and the clock reads self.now.
    """

    RUNNERS = ("r-1", "r-2")

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.ioreg, self.pmset, self.now = OPEN, ON_AC, T0
        self.busy = set()
        # Busy only by the time of the fresh re-check just before a stop: a job
        # assigned between the tick's scan and the stop.
        self.busy_by_the_stop = set()
        self.online = set(self.RUNNERS)
        self.paused_calls, self.resumed_calls = [], []
        fake_lw = SimpleNamespace(
            discover_runners=lambda: {r: self.base / r for r in self.RUNNERS},
            service_active=lambda rid: rid in self.online,
            runner_idle=self._runner_idle,
            pause_service=self._pause,
            resume_service=self._resume,
        )
        self._patch(lidwd, "IS_MAC", True)
        self._patch(lidwd, "STATE_FILE", self.base / "lid-watchdog.state")
        self._patch(lidwd, "LOAD_STATE_FILE", self.base / "load-watchdog.state")
        self._patch(lidwd, "OPT_OUT_FILE", self.base / ".no-lid-watchdog")
        self._patch(lidwd, "POWER_OPT_OUT_FILE", self.base / ".no-power-watchdog")
        self._patch(lidwd, "LOG_FILE", self.base / "logs" / "lid-watchdog.log")
        self._patch(lidwd, "_lw", fake_lw)
        self._patch(lidwd, "subprocess", SimpleNamespace(run=self._run))
        self._patch(lidwd, "time", SimpleNamespace(time=lambda: self.now))
        self._patch(lidwd.runner_fleet, "worker_cmdlines", lambda: [])

    def _patch(self, target, name, value):
        patcher = mock.patch.object(target, name, value)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _run(self, argv, **_):
        out = {"ioreg": self.ioreg, "pmset": self.pmset}[argv[0]]
        return SimpleNamespace(stdout=out, returncode=0)

    def _runner_idle(self, runner_dir, workers=None):
        # workers=None is a fresh ps scan, as in runner_fleet.is_busy.
        late = self.busy_by_the_stop if workers is None else set()
        return runner_dir.name not in self.busy | late

    def _pause(self, rid):
        self.online.discard(rid)
        self.paused_calls.append(rid)
        return True

    def _resume(self, rid):
        self.online.add(rid)
        self.resumed_calls.append(rid)
        return True

    def tick_at(self, seconds, *, pmset=None, ioreg=None):
        """Run one tick `seconds` after T0, optionally changing power or lid first."""
        self.pmset = pmset or self.pmset
        self.ioreg = ioreg or self.ioreg
        self.now = T0 + seconds
        lidwd.tick()

    def offline(self):
        return sorted(set(self.RUNNERS) - self.online)

    def saved(self):
        return json.loads((self.base / "lid-watchdog.state").read_text())


class PowerTickTest(FakeLaptop):
    def test_on_ac_nothing_happens(self):
        self.tick_at(0, pmset=ON_AC)
        self.tick_at(2 * HOUR)
        self.assertEqual(self.offline(), [])
        self.assertIsNone(self.saved().get("on_battery_since"))
        # Control arm: the same stretch on battery takes them offline.
        self.tick_at(2 * HOUR, pmset=ON_BATTERY)
        self.tick_at(4 * HOUR)
        self.assertEqual(self.offline(), ["r-1", "r-2"])

    def test_on_battery_within_the_grace_nothing_happens(self):
        for minute in (0, 10, 20):
            self.tick_at(minute * MIN, pmset=ON_BATTERY)
        self.tick_at(GRACE)
        self.assertEqual(self.offline(), [])
        self.assertEqual(self.saved().get("on_battery_since"), T0)
        # Control arm: one second past the grace.
        self.tick_at(GRACE + 1)
        self.assertEqual(self.offline(), ["r-1", "r-2"])

    def test_on_battery_past_the_grace_pauses_idle_runners(self):
        self.tick_at(0, pmset=ON_BATTERY)
        self.tick_at(GRACE + 1)
        self.assertEqual(self.paused_calls, ["r-1", "r-2"])
        self.assertEqual(self.saved()["paused"], ["r-1", "r-2"])

    def test_time_asleep_counts_toward_the_grace(self):
        # The timer cannot tick while the Mac sleeps. The clock is wall time,
        # so the first tick after a two-hour sleep on battery acts at once.
        self.tick_at(0, pmset=ON_BATTERY)
        self.tick_at(2 * HOUR)
        self.assertEqual(self.offline(), ["r-1", "r-2"])

    def test_clock_starts_at_the_first_tick_that_sees_battery(self):
        # What the clock cannot see: unplugged while asleep, the grace runs
        # from the first tick after the wake, not from the unplug.
        self.tick_at(0, pmset=ON_AC)
        self.tick_at(HOUR, pmset=ON_BATTERY)
        self.tick_at(HOUR + GRACE)
        self.assertEqual(self.offline(), [])
        self.tick_at(HOUR + GRACE + 1)
        self.assertEqual(self.offline(), ["r-1", "r-2"])

    def test_busy_runner_is_left_alone(self):
        self.busy = {"r-1"}
        self.tick_at(0, pmset=ON_BATTERY)
        self.tick_at(GRACE + 1)
        self.assertEqual(self.paused_calls, ["r-2"])
        # Once its job ends, the next tick takes it offline too.
        self.busy = set()
        self.tick_at(GRACE + 16)
        self.assertEqual(self.offline(), ["r-1", "r-2"])

    def test_runner_that_takes_a_job_mid_tick_is_left_alone(self):
        self.tick_at(0, pmset=ON_BATTERY)
        self.busy_by_the_stop = {"r-1"}
        self.tick_at(GRACE + 1)
        self.assertEqual(self.paused_calls, ["r-2"])
        self.assertEqual(self.saved()["paused"], ["r-2"])

    def test_back_on_ac_resumes_them(self):
        self.tick_at(0, pmset=ON_BATTERY)
        self.tick_at(GRACE + 1)
        self.tick_at(GRACE + MIN, pmset=ON_AC)
        self.assertEqual(self.resumed_calls, ["r-1", "r-2"])
        self.assertEqual(self.offline(), [])
        self.assertEqual(self.saved(), {"paused": [], "on_battery_since": None})
        # Unplugged again, the grace starts over.
        self.tick_at(2 * HOUR, pmset=ON_BATTERY)
        self.tick_at(2 * HOUR + GRACE)
        self.assertEqual(self.offline(), [])

    def test_back_on_ac_with_the_lid_closed_stays_offline(self):
        self.tick_at(0, pmset=ON_BATTERY)
        self.tick_at(GRACE + 1)
        # The battery parked them, with the lid open: without this the closed
        # lid below would park them itself, and the test would pin nothing new.
        self.assertEqual(self.paused_calls, ["r-1", "r-2"])
        self.tick_at(GRACE + MIN, pmset=ON_AC, ioreg=CLOSED)
        self.assertEqual(self.resumed_calls, [])
        self.tick_at(GRACE + 2 * MIN, ioreg=OPEN)
        self.assertEqual(self.resumed_calls, ["r-1", "r-2"])

    def test_back_on_ac_leaves_load_watchdog_pauses_offline(self):
        self.tick_at(0, pmset=ON_BATTERY)
        self.tick_at(GRACE + 1)
        self.assertEqual(self.paused_calls, ["r-1", "r-2"])
        (self.base / "load-watchdog.state").write_text(
            '{"high_ticks": 1, "paused": ["r-1"]}'
        )
        self.tick_at(GRACE + MIN, pmset=ON_AC)
        self.assertEqual(self.resumed_calls, ["r-2"])

    def test_desktop_without_a_battery_is_a_noop(self):
        self.tick_at(0, ioreg=DESKTOP, pmset=UPS_ON_BATTERY)
        self.tick_at(2 * HOUR)
        self.tick_at(4 * HOUR, pmset=NO_BATTERY)
        self.assertEqual(self.offline(), [])
        # Control arm: the same hours on a laptop's battery.
        self.tick_at(4 * HOUR, ioreg=OPEN, pmset=ON_BATTERY)
        self.tick_at(6 * HOUR)
        self.assertEqual(self.offline(), ["r-1", "r-2"])

    def test_linux_is_a_noop(self):
        with mock.patch.object(lidwd, "IS_MAC", False):
            self.tick_at(0, pmset=ON_BATTERY)
            self.tick_at(2 * HOUR)
        self.assertEqual(self.offline(), [])
        # Control arm: the same readings on macOS.
        self.tick_at(2 * HOUR)
        self.tick_at(4 * HOUR)
        self.assertEqual(self.offline(), ["r-1", "r-2"])

    def test_power_opt_out_marker_disables_the_power_condition(self):
        marker = self.base / ".no-power-watchdog"
        marker.touch()
        self.tick_at(0, pmset=ON_BATTERY)
        self.tick_at(2 * HOUR)
        self.assertEqual(self.offline(), [])
        # Control arm: without the marker the clock starts, and then holds.
        marker.unlink()
        self.tick_at(2 * HOUR)
        self.tick_at(2 * HOUR + GRACE + 1)
        self.assertEqual(self.offline(), ["r-1", "r-2"])

    def test_power_opt_out_marker_releases_what_it_held(self):
        self.tick_at(0, pmset=ON_BATTERY)
        self.tick_at(GRACE + 1)
        self.assertEqual(self.offline(), ["r-1", "r-2"])
        (self.base / ".no-power-watchdog").touch()
        self.tick_at(GRACE + MIN)
        self.assertEqual(self.offline(), [])

    def test_lid_opt_out_marker_disables_the_power_condition_too(self):
        marker = self.base / ".no-lid-watchdog"
        marker.touch()
        self.tick_at(0, pmset=ON_BATTERY)
        self.tick_at(2 * HOUR)
        self.assertEqual(self.offline(), [])
        # Control arm: the same, with the marker gone.
        marker.unlink()
        self.tick_at(2 * HOUR)
        self.tick_at(2 * HOUR + GRACE + 1)
        self.assertEqual(self.offline(), ["r-1", "r-2"])

    def test_status_shows_power_and_what_a_tick_would_do(self):
        self.tick_at(0, pmset=ON_BATTERY)
        self.now = T0 + 42 * MIN
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            lidwd.status()
        text = out.getvalue()
        self.assertIn("on_battery: True", text)
        self.assertIn("42m on battery", text)
        self.assertIn("holding: battery", text)
        self.assertIn("pause=['r-1', 'r-2']", text)
        # status() only reports: nothing was paused.
        self.assertEqual(self.offline(), [])


class LoadWatchdogHoldTest(FakeLaptop):
    """The load watchdog must not resume its own pauses while this watchdog
    holds the host offline on battery. It would undo the hold, and this
    watchdog would re-pause them a tick later."""

    def setUp(self):
        super().setUp()
        self.lw = _load("lw", "load-watchdog.py")
        (self.base / "load-watchdog.state").write_text(
            '{"high_ticks": 0, "paused": ["r-1"]}'
        )
        self.resume = mock.Mock(return_value=True)
        self._patch(self.lw, "IS_MAC", True)
        self._patch(self.lw, "_lidwd", lidwd)
        self._patch(self.lw, "STATE_FILE", self.base / "load-watchdog.state")
        self._patch(self.lw, "OPT_OUT_FILE", self.base / ".no-load-watchdog")
        self._patch(self.lw, "LOG_FILE", self.base / "logs" / "load-watchdog.log")
        self._patch(self.lw, "per_core_load", lambda: 0.0)
        self._patch(self.lw, "discover_runners", lambda: {})
        self._patch(self.lw, "resume_service", self.resume)

    def test_low_load_does_not_resume_during_a_battery_hold(self):
        self.tick_at(0, pmset=ON_BATTERY)
        self.tick_at(GRACE + 1)
        self.lw.tick()
        self.resume.assert_not_called()
        # Control arm: back on AC, low load brings its runner back.
        self.pmset = ON_AC
        self.lw.tick()
        self.resume.assert_called_once_with("r-1")


if __name__ == "__main__":
    unittest.main()
