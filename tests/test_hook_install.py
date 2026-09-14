"""Install-path guards for a brand-new public user.

A new host gets its hooks two ways: install.sh / install-linux.sh copy
hooks/*.sh when runners are first registered, and apply.py --sync-config re-syncs
them on every update. The job hooks then run only the sidecars listed in
hooks/_sidecars.sh, and silently skip any listed name that is not installed and
executable. So the failures a new user would hit are quiet ones: a sidecar named
in SIDECARS that never ships, or an installer that stops doing something the
hooks rely on, simply never runs — no error anywhere.

These tests pin both ends, then run a first job on a host that has nothing but a
fresh install: no inventory, no sounds, no .repo-path, no optional tools.
"""

import importlib.util
import os
import shutil
import subprocess
from pathlib import Path
from unittest import mock

import pytest

REPO = Path(__file__).resolve().parents[1]
HOOKS = REPO / "hooks"
BASH = shutil.which("bash")
INSTALLERS = ["install.sh", "install-linux.sh"]


def _sidecars():
    listed = subprocess.run(
        [
            BASH,
            "-c",
            f'. "{HOOKS / "_sidecars.sh"}"; printf "%s\\n" "${{SIDECARS[@]}}"',
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    assert listed, "SIDECARS is empty or hooks/_sidecars.sh failed to source"
    return listed


def _apply():
    spec = importlib.util.spec_from_file_location("apply", REPO / "apply.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ── Every listed sidecar actually reaches a new host ─────────────────────────


@pytest.mark.parametrize("name", _sidecars())
def test_every_listed_sidecar_ships_as_an_executable_hook(name):
    """test-job-started-hook.sh stubs sidecars from the list, so it cannot see a
    name that is listed but missing — and run_sidecars skips it silently."""
    path = HOOKS / name
    assert path.is_file(), f"SIDECARS lists {name}, but hooks/{name} does not exist"
    assert os.access(path, os.X_OK), f"hooks/{name} is not executable"


@pytest.mark.parametrize("name", _sidecars())
def test_every_listed_sidecar_is_picked_up_by_the_hook_sync(name):
    assert name in _apply().discover_hook_files()


# ── The installers do what the job hooks rely on ─────────────────────────────


@pytest.mark.parametrize("installer", INSTALLERS)
def test_installer_copies_hooks_by_glob(installer):
    """A hand-maintained list is how a new hook once silently never reached the
    fleet (the issue #19 class that discover_hook_files documents)."""
    text = (REPO / installer).read_text()
    assert 'cp "$SCRIPT_DIR/hooks/"*.sh "$HOOKS_DIR/"' in text


@pytest.mark.parametrize("installer", INSTALLERS)
def test_installer_writes_repo_path(installer):
    """job-completed.sh triggers the per-job fleet update only on hosts with
    ~/actions-runner/.repo-path. Without it a new host updates only every 2h."""
    text = (REPO / installer).read_text()
    assert '>"$BASE_DIR/.repo-path"' in text


@pytest.mark.parametrize("installer", INSTALLERS)
def test_installer_installs_the_maintenance_job_the_hook_triggers(installer):
    text = (REPO / installer).read_text()
    assert 'maintenance-timer.py" --install-timer' in text


# ── A new host's first job ───────────────────────────────────────────────────


def test_first_job_on_a_fresh_install_is_clean(tmp_path):
    """Nothing but a fresh hook sync: no runners.toml, no sounds, no .repo-path,
    and none of the optional tools (systemd-inhibit, launchctl, systemctl, afplay,
    pw-play, standup) on PATH. Both real hooks, with their real sidecars, must run
    to completion under the shell options the runner uses, and say nothing on
    stderr — an install that works has nothing to warn about."""
    home = tmp_path / "home"
    apply = _apply()
    with mock.patch.object(apply, "RUNNER_BASE", home / "actions-runner"):
        apply.sync_config()
    installed = home / "actions-runner" / "hooks"
    assert sorted(p.name for p in installed.glob("*.sh")) == sorted(
        p.name for p in HOOKS.glob("*.sh")
    )
    assert all(os.access(p, os.X_OK) for p in installed.glob("*.sh"))

    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "dirname").symlink_to(shutil.which("dirname"))
    env = {
        "PATH": str(bindir),
        "HOME": str(home),
        "RUNNER_TRACKING_ID": "github_fresh_host",
    }
    for hook in ("job-started.sh", "job-completed.sh"):
        r = subprocess.run(
            [
                BASH,
                "--noprofile",
                "--norc",
                "-e",
                "-o",
                "pipefail",
                str(installed / hook),
            ],
            env=env,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        assert r.returncode == 0, f"{hook} failed on a fresh host: {r.stderr}"
        assert r.stderr == "", f"{hook} warned on a fresh host: {r.stderr}"
