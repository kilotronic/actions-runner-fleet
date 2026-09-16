"""Tests for the uninstallers' host teardown (_teardown.sh).

Two defects made uninstalling not stick for a new user:

- Neither uninstaller removed the host's timers, so two hours later the
  maintenance timer ran apply.py and reinstalled every runner still listed in
  runners.toml.
- The "last runner gone" cleanup counted leftover directories as runners
  (hooks/ and logs/ on macOS; logs/ and .shared-externals-* on Linux), so on a
  real host it never ran.

Each test runs a real uninstaller against a sandbox HOME, with stubs for gh,
launchctl, systemctl, the runner's config.sh and the timer scripts, so nothing
touches the machine running the tests.
"""

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
BASH = shutil.which("bash")
SCRIPTS = ["uninstall.sh", "uninstall-linux.sh"]
TIMERS = [
    "maintenance-timer.py",
    "load-watchdog.py",
    "lid-watchdog.py",
    "orbstack-watchdog.py",
]
REAL_TOOLS = ("basename", "dirname", "rm", "rmdir", "id", "sed")
KIT_FILES = (
    "hooks",
    ".cache",
    ".shared-externals-2.337.0",
    ".shared-tool-cache",
    ".repo-path",
    ".update.lock",
    ".uv-self-update-stamp",
    "load-watchdog.state",
    "runner-health.state",
)


def _script(path, body):
    path.write_text(f"#!{BASH}\n{body}\n")
    path.chmod(0o755)


class Sandbox:
    def __init__(self, tmp_path, script, inventory, timer_rc=0):
        self.script = script
        self.calls = tmp_path / "calls.txt"

        self.tools = tmp_path / "tools"
        self.tools.mkdir()
        for name in (script, "_teardown.sh", "fleet_config.py"):
            shutil.copy(REPO / name, self.tools / name)
        for timer in TIMERS:
            (self.tools / timer).write_text(
                "import sys\n"
                f"open({str(self.calls)!r}, 'a').write('{timer} ' + ' '.join(sys.argv[1:]) + '\\n')\n"
                f"sys.exit({timer_rc})\n"
            )

        self.bindir = tmp_path / "bin"
        self.bindir.mkdir()
        for tool in REAL_TOOLS:
            (self.bindir / tool).symlink_to(shutil.which(tool))
        (self.bindir / "python3").symlink_to(sys.executable)
        _script(self.bindir / "gh", f'echo "gh $*" >> "{self.calls}"; echo token-123')
        for name in ("launchctl", "systemctl"):
            _script(self.bindir / name, f'echo "{name} $*" >> "{self.calls}"')

        self.home = tmp_path / "home"
        self.base = self.home / "actions-runner"
        self.base.mkdir(parents=True)
        inv_dir = self.home / ".config" / "actions-runner"
        (inv_dir / "sounds").mkdir(parents=True)
        self.inventory = inv_dir / "runners.toml"
        self.inventory.write_text(inventory)
        self.sound = inv_dir / "sounds" / "job-start.aiff"
        self.sound.write_bytes(b"FORM")

    def runner(self, dirname):
        d = self.base / dirname
        d.mkdir()
        (d / ".runner").write_text("{}")
        _script(d / "config.sh", f'echo "{dirname}/config.sh $*" >> "{self.calls}"')
        agents = self.home / "Library" / "LaunchAgents"
        agents.mkdir(parents=True, exist_ok=True)
        (agents / f"com.github.actions-runner.{dirname}.plist").write_text("<plist/>")
        units = self.home / ".config" / "systemd" / "user"
        units.mkdir(parents=True, exist_ok=True)
        (units / f"github-runner-{dirname}.service").write_text("[Unit]\n")

    def kit_files(self, logs=True):
        for name in KIT_FILES:
            path = self.base / name
            if "." in name and not name.startswith(".shared") and name != ".cache":
                path.write_text("x")
            else:
                path.mkdir()
        (self.base / "hooks" / "job-started.sh").write_text("#!/bin/sh\n")
        if logs:
            (self.base / "logs").mkdir()
            (self.base / "logs" / "update.log").write_text("log\n")

    def run(self, repo="acme/app"):
        return subprocess.run(
            [BASH, str(self.tools / self.script), repo],
            env={
                "PATH": str(self.bindir),
                "HOME": str(self.home),
                "APPLY_HOST": "host-a",
                "ACTIONS_RUNNER_CONFIG": str(self.inventory),
            },
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )

    def lines(self):
        return self.calls.read_text().splitlines() if self.calls.exists() else []


LISTED = '[hosts.host-a]\n"acme/app" = 1\n"acme/docs" = 1\n'
RETIRED = '[hosts.host-a]\n"acme/app" = 0\n"acme/docs" = 1\n'


# ── Last runner on the host ──────────────────────────────────────────────────


@pytest.mark.parametrize("script", SCRIPTS)
def test_last_runner_removes_timers_and_kit_files_but_keeps_logs(tmp_path, script):
    """hooks/, logs/ and .shared-externals-* are the kit's, not runners — the old
    directory count mistook them for survivors and never cleaned up."""
    sb = Sandbox(tmp_path, script, RETIRED)
    sb.runner("app-1")
    sb.kit_files(logs=True)
    r = sb.run()
    assert r.returncode == 0, r.stderr
    assert not (sb.base / "app-1").exists()
    assert "app-1/config.sh remove --token token-123" in sb.lines()
    for timer in TIMERS:
        assert f"{timer} --uninstall-timer" in sb.lines()
    for name in KIT_FILES:
        assert not (sb.base / name).exists(), f"{name} was left behind"
    assert (sb.base / "logs" / "update.log").is_file()
    assert sb.inventory.is_file() and sb.sound.is_file()

    # The kept-message must not disown what it kept. logs/ and ci-env-jobs.jsonl
    # are BOTH written by this kit (hooks/log-job-env.sh writes the latter into
    # $base), and both survive a teardown on purpose — the reason for the
    # teardown is often in them. Saying "files the kit does not own" told the
    # operator the opposite.
    kept = [ln for ln in r.stdout.splitlines() if ln.strip().startswith("kept ")]
    assert kept, r.stdout
    assert "does not own" not in " ".join(kept)
    assert "logs" in " ".join(kept)


@pytest.mark.parametrize("script", SCRIPTS)
def test_nothing_left_removes_the_base_dir(tmp_path, script):
    sb = Sandbox(tmp_path, script, RETIRED)
    sb.runner("app-1")
    sb.kit_files(logs=False)
    assert sb.run().returncode == 0
    assert not sb.base.exists()
    assert sb.inventory.is_file() and sb.sound.is_file()


@pytest.mark.parametrize("script", SCRIPTS)
def test_failed_timer_removal_is_reported_not_fatal(tmp_path, script):
    sb = Sandbox(tmp_path, script, RETIRED, timer_rc=1)
    sb.runner("app-1")
    sb.kit_files()
    r = sb.run()
    assert r.returncode == 0, r.stderr
    assert "maintenance-timer.py --uninstall-timer failed" in r.stdout
    assert not (sb.base / "hooks").exists()  # the rest of the teardown still ran


# ── Other runners remain ─────────────────────────────────────────────────────


@pytest.mark.parametrize("script", SCRIPTS)
def test_other_runners_keep_the_timers_and_kit_files(tmp_path, script):
    sb = Sandbox(tmp_path, script, RETIRED)
    sb.runner("app-1")
    sb.runner("docs-1")
    sb.kit_files()
    r = sb.run()
    assert r.returncode == 0, r.stderr
    assert not (sb.base / "app-1").exists()
    assert (sb.base / "docs-1" / "config.sh").is_file()
    assert not any("--uninstall-timer" in line for line in sb.lines())
    assert (sb.base / "hooks").is_dir() and (sb.base / ".repo-path").is_file()
    assert "still listed" not in r.stdout


@pytest.mark.parametrize("script", SCRIPTS)
def test_warns_when_the_timer_would_reinstall_the_repo(tmp_path, script):
    sb = Sandbox(tmp_path, script, LISTED)
    sb.runner("app-1")
    sb.runner("docs-1")
    sb.kit_files()
    r = sb.run()
    assert r.returncode == 0, r.stderr
    assert "acme/app is still listed" in r.stdout
    assert "reinstall" in r.stdout


# ── Per-runner service removal is unchanged ──────────────────────────────────


def test_macos_uninstaller_boots_out_and_removes_the_runner_agent(tmp_path):
    sb = Sandbox(tmp_path, "uninstall.sh", RETIRED)
    sb.runner("app-1")
    assert sb.run().returncode == 0
    assert any(
        line.startswith("launchctl bootout")
        and "com.github.actions-runner.app-1.plist" in line
        for line in sb.lines()
    )
    assert not (
        sb.home / "Library/LaunchAgents/com.github.actions-runner.app-1.plist"
    ).exists()


def test_linux_uninstaller_disables_and_removes_the_runner_unit(tmp_path):
    sb = Sandbox(tmp_path, "uninstall-linux.sh", RETIRED)
    sb.runner("app-1")
    assert sb.run().returncode == 0
    assert "systemctl --user disable --now github-runner-app-1.service" in sb.lines()
    assert not (sb.home / ".config/systemd/user/github-runner-app-1.service").exists()
