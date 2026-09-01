#!/usr/bin/env python3
"""Unit tests for the shared timer renderers.

Run: python3 -m unittest test_runner_timers

Only the pure render_* functions are tested — they hold all the unit-file layout.
install_timer/uninstall_timer are thin launchd/systemd I/O shims, verified by the
load-watchdog refactor and the on-host fleet check.

The "watchdog equivalence" tests pin the renderers to the exact launchd plist and
systemd units load-watchdog.py shipped before the refactor, so Task 2 can swap to
this module without changing a single byte of what lands on a host.
"""

import importlib.util
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "rt", Path(__file__).resolve().parents[1] / "runner_timers.py"
)
rt = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rt)

# The exact strings load-watchdog.py generated pre-refactor (60s tick, --tick arg).
WATCHDOG_MAC_PATH = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

EXPECTED_WATCHDOG_PLIST = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.github.actions-runner.load-watchdog</string>
    <key>ProgramArguments</key>
    <array>
        <string>/x/load-watchdog.py</string>
        <string>--tick</string>
    </array>
    <key>StartInterval</key>
    <integer>60</integer>
    <key>RunAtLoad</key>
    <true/>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    </dict>
    <key>StandardOutPath</key>
    <string>/x/logs/load-watchdog.log</string>
    <key>StandardErrorPath</key>
    <string>/x/logs/load-watchdog.log</string>
</dict>
</plist>
"""

EXPECTED_WATCHDOG_SERVICE = """[Unit]
Description=GitHub Actions runner load watchdog (pause runners under high load)

[Service]
Type=oneshot
ExecStart=/x/load-watchdog.py --tick
"""

EXPECTED_WATCHDOG_TIMER = """[Unit]
Description=Run the runner load watchdog every 60s

[Timer]
OnBootSec=60s
OnUnitActiveSec=60s
AccuracySec=10s

[Install]
WantedBy=timers.target
"""


class RenderTest(unittest.TestCase):
    def test_launchd_plist_matches_watchdog_output(self):
        out = rt.render_launchd_plist(
            label="com.github.actions-runner.load-watchdog",
            program="/x/load-watchdog.py",
            args=["--tick"],
            interval=60,
            log="/x/logs/load-watchdog.log",
            path_env=WATCHDOG_MAC_PATH,
        )
        self.assertEqual(out, EXPECTED_WATCHDOG_PLIST)

    def test_systemd_service_matches_watchdog_output(self):
        out = rt.render_systemd_service(
            description="GitHub Actions runner load watchdog (pause runners under high load)",
            program="/x/load-watchdog.py",
            args=["--tick"],
        )
        self.assertEqual(out, EXPECTED_WATCHDOG_SERVICE)

    def test_systemd_timer_matches_watchdog_output(self):
        out = rt.render_systemd_timer(
            description="Run the runner load watchdog every 60s", interval=60
        )
        self.assertEqual(out, EXPECTED_WATCHDOG_TIMER)

    def test_systemd_service_adds_path_env_when_given(self):
        out = rt.render_systemd_service(
            description="d",
            program="/x/update-host.sh",
            args=[],
            path_env="/usr/bin:/bin",
        )
        self.assertIn("ExecStart=/x/update-host.sh\n", out)
        self.assertIn("Environment=PATH=/usr/bin:/bin\n", out)

    def test_launchd_plist_no_args_renders_single_program_string(self):
        out = rt.render_launchd_plist(
            label="com.github.actions-runner.maintenance",
            program="/x/update-host.sh",
            args=[],
            interval=7200,
            log="/x/logs/update.log",
            path_env="/usr/bin:/bin",
        )
        self.assertIn("        <string>/x/update-host.sh</string>\n    </array>", out)
        self.assertIn("<integer>7200</integer>", out)

    def test_maintenance_launchd_runs_update_host_every_2h(self):
        out = rt.render_launchd_plist(
            label="com.github.actions-runner.maintenance",
            program="/repo/update-host.sh",
            args=[],
            interval=7200,
            log="/home/u/actions-runner/logs/update.log",
            path_env="/opt/homebrew/bin:/usr/bin:/bin",
        )
        self.assertIn("<string>com.github.actions-runner.maintenance</string>", out)
        self.assertIn("<string>/repo/update-host.sh</string>", out)
        self.assertIn("<integer>7200</integer>", out)

    def test_maintenance_systemd_service_has_path_env(self):
        out = rt.render_systemd_service(
            description="GitHub runner fleet maintenance (converge every 2h)",
            program="/repo/update-host.sh",
            args=[],
            path_env="/usr/bin:/bin:/home/u/.local/bin",
        )
        self.assertIn("ExecStart=/repo/update-host.sh\n", out)
        self.assertIn("Environment=PATH=/usr/bin:/bin:/home/u/.local/bin\n", out)


class TimerNeedsInstallTest(unittest.TestCase):
    """The decision that lets update-host.sh call install_timer every tick.

    It has to say yes in both adoption cases (never installed, booted out) and
    no in the steady state, or the bootout/bootstrap cycle — whose RunAtLoad
    re-fires update-host.sh — would run on every convergence tick.
    """

    def test_absent_unit_needs_install(self):
        # The case the old "checkout advanced" gate could never reach: a timer
        # introduced by a commit, on a host that already pulled that commit.
        self.assertTrue(
            rt.timer_needs_install(existing_text=None, desired_text="X", loaded=False)
        )

    def test_changed_unit_needs_install(self):
        self.assertTrue(
            rt.timer_needs_install(existing_text="old", desired_text="new", loaded=True)
        )

    def test_unloaded_but_identical_still_needs_install(self):
        # Booted out by hand, or never bootstrapped: the file on disk matching
        # is not evidence the agent is running.
        self.assertTrue(
            rt.timer_needs_install(existing_text="X", desired_text="X", loaded=False)
        )

    def test_current_and_loaded_is_a_noop(self):
        # The steady state, hit on nearly every tick — must not cycle launchd.
        self.assertFalse(
            rt.timer_needs_install(existing_text="X", desired_text="X", loaded=True)
        )


if __name__ == "__main__":
    unittest.main()
