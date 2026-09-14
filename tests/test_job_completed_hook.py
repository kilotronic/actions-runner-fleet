"""Tests for hooks/job-completed.sh: the fleet update and the post-job sleep
inhibitor must both outlive the end of the job.

When a job ends, the runner terminates every process still carrying that job's
RUNNER_TRACKING_ID, milliseconds after this hook returns. Both things the hook
used to background were killed by that on every job. The fleet update is now
handed to the service manager's maintenance job; the inhibitor runs with the
tracking ID removed.

PATH holds only recording stubs plus the two real tools the hook needs, so no
test can reach a real launchctl or systemctl and converge the developer's own
machine.
"""

import importlib.util
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
HOOK = REPO / "hooks" / "job-completed.sh"
SIDECAR_LIB = REPO / "hooks" / "_sidecars.sh"
BASH = shutil.which("bash")
REAL_TOOLS = ("env", "dirname")
TRACKING_ID = "github_9b31e0"
DIRECT_UPDATE = "update-host.sh ran as a child of the hook"


def _script(path, body):
    path.write_text(f"#!{BASH}\n{body}\n")
    path.chmod(0o755)


def _sandbox(tmp_path, tools=(), bodies=None, fleet_host=True):
    """A hook dir (the hook and sidecar library, no sidecars), a HOME that is or
    is not set up for fleet updates, and a PATH of recording stubs. Each stub
    logs `<name> <argv> tracking=<RUNNER_TRACKING_ID or UNSET>`, then runs its
    entry in `bodies`. Returns (hook, bindir, home, calls)."""
    bodies = bodies or {}
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    shutil.copy(HOOK, hooks / "job-completed.sh")
    shutil.copy(SIDECAR_LIB, hooks / "_sidecars.sh")

    bindir = tmp_path / "bin"
    bindir.mkdir()
    for tool in REAL_TOOLS:
        (bindir / tool).symlink_to(shutil.which(tool))
    calls = tmp_path / "calls.txt"
    for name in tools:
        _script(
            bindir / name,
            f'echo "{name} $* tracking=${{RUNNER_TRACKING_ID-UNSET}}" >> "{calls}"\n'
            f"{bodies.get(name, '')}",
        )

    # The checkout .repo-path names, with an updater that records being run.
    # Nothing may run it directly any more.
    fleet = tmp_path / "fleet"
    fleet.mkdir()
    _script(fleet / "update-host.sh", f'echo "{DIRECT_UPDATE}" >> "{calls}"')

    home = tmp_path / "home"
    (home / "actions-runner").mkdir(parents=True)
    if fleet_host:
        (home / "actions-runner" / ".repo-path").write_text(str(fleet))
    return hooks / "job-completed.sh", bindir, home, calls


def _run(hook, bindir, home):
    return subprocess.run(
        [BASH, str(hook)],
        env={
            "PATH": str(bindir),
            "HOME": str(home),
            "RUNNER_TRACKING_ID": TRACKING_ID,
        },
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
    )


def _lines(calls):
    return calls.read_text().splitlines() if calls.exists() else []


def _wait_for(calls, needle, timeout=3.0):
    """The inhibitor is backgrounded, so poll for its stub's line."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        hits = [ln for ln in _lines(calls) if needle in ln]
        if hits:
            return hits
        time.sleep(0.05)
    return []


def _maintenance_timer():
    spec = importlib.util.spec_from_file_location(
        "maintenance_timer", REPO / "maintenance-timer.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ── Fleet update goes through the service manager ────────────────────────────


def test_macos_kickstarts_the_maintenance_job(tmp_path):
    hook, bindir, home, calls = _sandbox(tmp_path, tools=("launchctl",))
    r = _run(hook, bindir, home)
    assert (r.returncode, r.stderr) == (0, "")
    [line] = _lines(calls)
    expected = (
        f"launchctl kickstart gui/{os.getuid()}/com.github.actions-runner.maintenance"
    )
    assert line.startswith(expected + " ")


def test_linux_starts_the_maintenance_unit_without_blocking(tmp_path):
    hook, bindir, home, calls = _sandbox(tmp_path, tools=("systemctl",))
    r = _run(hook, bindir, home)
    assert (r.returncode, r.stderr) == (0, "")
    [line] = _lines(calls)
    assert line.startswith(
        "systemctl --user start --no-block github-runner-maintenance.service "
    )


@pytest.mark.parametrize("tools", [(), ("launchctl",), ("systemctl",)])
def test_update_host_is_never_a_child_of_the_hook(tmp_path, tools):
    """A child is killed by the orphan cleanup, or by a restart of this runner
    that update-host.sh itself performs."""
    hook, bindir, home, calls = _sandbox(tmp_path, tools=tools)
    assert _run(hook, bindir, home).returncode == 0
    time.sleep(0.3)
    assert DIRECT_UPDATE not in _lines(calls)


def test_host_not_set_up_for_fleet_updates_triggers_nothing(tmp_path):
    hook, bindir, home, calls = _sandbox(
        tmp_path, tools=("launchctl", "systemctl"), fleet_host=False
    )
    r = _run(hook, bindir, home)
    assert (r.returncode, r.stdout, r.stderr) == (0, "", "")
    assert _lines(calls) == []


@pytest.mark.parametrize("tool", ["launchctl", "systemctl"])
def test_failed_trigger_is_silent(tmp_path, tool):
    """An unloaded or already-running maintenance job must not fail the job."""
    hook, bindir, home, _ = _sandbox(tmp_path, tools=(tool,), bodies={tool: "exit 1"})
    r = _run(hook, bindir, home)
    assert (r.returncode, r.stdout, r.stderr) == (0, "", "")


def test_trigger_names_match_the_installed_maintenance_timer():
    """The hook hardcodes the label and unit that maintenance-timer.py installs.
    If they drift, kickstart targets a job that does not exist and fails
    silently — the per-job update would quietly stop again."""
    mt = _maintenance_timer()
    text = HOOK.read_text()
    assert f"gui/$UID/{mt.LABEL}" in text
    assert f"{mt.UNIT}.service" in text


# ── Post-job grace survives the orphan cleanup ───────────────────────────────


def test_post_job_grace_is_exempt_from_orphan_cleanup(tmp_path):
    hook, bindir, home, calls = _sandbox(
        tmp_path, tools=("systemd-inhibit",), fleet_host=False
    )
    r = _run(hook, bindir, home)
    assert r.returncode == 0
    assert "post-job idle-suspend grace (900s)" in r.stdout
    [line] = _wait_for(calls, "systemd-inhibit")
    assert "--what=sleep" in line and "--mode=block" in line
    assert "sleep 900" in line
    assert line.endswith("tracking=UNSET")
    assert TRACKING_ID not in line


def test_hook_does_not_wait_for_the_grace(tmp_path):
    sleep = shutil.which("sleep")
    hook, bindir, home, calls = _sandbox(
        tmp_path,
        tools=("systemd-inhibit",),
        bodies={"systemd-inhibit": f"{sleep} 5"},
        fleet_host=False,
    )
    started = time.monotonic()
    r = _run(hook, bindir, home)
    elapsed = time.monotonic() - started
    assert r.returncode == 0
    assert elapsed < 2.0, f"hook blocked for {elapsed:.2f}s behind the inhibitor"
    assert _wait_for(calls, "systemd-inhibit")


def test_no_inhibitor_on_this_host_is_silent(tmp_path):
    hook, bindir, home, _ = _sandbox(tmp_path, fleet_host=False)
    r = _run(hook, bindir, home)
    assert (r.returncode, r.stdout, r.stderr) == (0, "", "")
