"""Smoke tests for hooks/standup-presence.sh: fail-safe when standup is absent,
and correct register/deregister argv when it is present."""

import subprocess
from pathlib import Path

HOOK = Path(__file__).resolve().parents[1] / "hooks" / "standup-presence.sh"


def _run(args, env):
    full = {"PATH": "/usr/bin:/bin", **env}
    return subprocess.run(
        ["bash", str(HOOK), *args],
        env=full,
        capture_output=True,
        text=True,
        check=False,
    )


def _fake_standup(tmp_path):
    """Install a fake `standup` on PATH that logs its argv; return (bindir, log)."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "calls.txt"
    fake = bindir / "standup"
    fake.write_text(f'#!/usr/bin/env bash\necho "$@" >> "{log}"\n')
    fake.chmod(0o755)
    return bindir, log


def test_noop_when_standup_absent(tmp_path):
    r = _run(["started"], {"HOME": str(tmp_path), "RUNNER_NAME": "host-a-1"})
    assert r.returncode == 0
    assert r.stdout == ""


def test_started_registers_runner(tmp_path):
    bindir, log = _fake_standup(tmp_path)
    env = {
        "HOME": str(tmp_path),
        "PATH": f"{bindir}:/usr/bin:/bin",
        "RUNNER_NAME": "host-a-1",
        "GITHUB_REPOSITORY": "kilotronic/partygame",
        "GITHUB_WORKFLOW": "CI",
        "GITHUB_RUN_NUMBER": "42",
    }
    r = subprocess.run(
        ["bash", str(HOOK), "started"],
        env={**env},
        capture_output=True,
        text=True,
        check=False,
    )
    assert r.returncode == 0
    out = log.read_text()
    assert "register --type runner" in out
    assert "--session-id runner:host-a-1" in out
    assert "--repo partygame" in out
    assert "--machine" in out


def test_completed_deregisters_runner(tmp_path):
    bindir, log = _fake_standup(tmp_path)
    env = {
        "HOME": str(tmp_path),
        "PATH": f"{bindir}:/usr/bin:/bin",
        "RUNNER_NAME": "host-a-1",
    }
    r = subprocess.run(
        ["bash", str(HOOK), "completed"],
        env={**env},
        capture_output=True,
        text=True,
        check=False,
    )
    assert r.returncode == 0
    out = log.read_text()
    assert "deregister --session-id runner:host-a-1" in out
