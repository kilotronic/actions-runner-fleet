"""Tests for hooks/play-sound.sh: silent unless the inventory holds a sound, the
right player per OS, and a player that survives the runner's orphan cleanup
without holding the job open.

PATH is a sandbox of stubs plus only the real tools the script needs, so no test
can reach a real audio player and make noise on a developer's machine.
"""

import shutil
import subprocess
import time
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parents[1] / "hooks" / "play-sound.sh"
BASH = shutil.which("bash")
REAL_TOOLS = ("env", "dirname")
TRACKING_ID = "github_4f2c9a"


def _script(path, body):
    path.write_text(f"#!{BASH}\n{body}\n")
    path.chmod(0o755)


def _sandbox(tmp_path, os_name, players=(), player_body=""):
    """A PATH dir with a stub `uname`, the real tools, and a recording stub per
    name in `players`. Each stub logs `<name> <argv> tracking=<RUNNER_TRACKING_ID
    or UNSET>`. Returns (bindir, calls_log)."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    calls = tmp_path / "calls.txt"
    for tool in REAL_TOOLS:
        (bindir / tool).symlink_to(shutil.which(tool))
    _script(bindir / "uname", f"echo {os_name}")
    for p in players:
        _script(
            bindir / p,
            f'echo "{p} $* tracking=${{RUNNER_TRACKING_ID-UNSET}}" >> "{calls}"\n'
            f"{player_body}",
        )
    return bindir, calls


def _sound(inventory, name):
    sounds = inventory / "sounds"
    sounds.mkdir(parents=True, exist_ok=True)
    path = sounds / name
    path.write_bytes(b"FORM\x00\x00\x00\x04AIFF")
    return path


def _run(bindir, event, home, **env):
    full = {
        "PATH": str(bindir),
        "HOME": str(home),
        "RUNNER_TRACKING_ID": TRACKING_ID,
        **env,
    }
    return subprocess.run(
        [BASH, str(HOOK), event],
        env=full,
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
    )


def _calls(calls, timeout=3.0):
    """Lines the backgrounded player stub logged. The player is detached, so
    poll for it. A negative assertion passes a short timeout: a slow start there
    could only hide a bug, never invent one."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if calls.exists() and calls.read_text().strip():
            return calls.read_text().splitlines()
        time.sleep(0.05)
    return []


def _default_inventory(home):
    return home / ".config" / "actions-runner"


# ── Silent by default ────────────────────────────────────────────────────────


def test_silent_when_the_inventory_has_no_sounds(tmp_path):
    bindir, calls = _sandbox(tmp_path, "Darwin", players=("afplay",))
    for event in ("started", "completed"):
        r = _run(bindir, event, tmp_path)
        assert (r.returncode, r.stdout, r.stderr) == (0, "", "")
    assert _calls(calls, timeout=0.3) == []


@pytest.mark.parametrize(
    "event,name", [("started", "job-start.aiff"), ("completed", "job-end.aiff")]
)
def test_each_event_plays_its_own_sound(tmp_path, event, name):
    bindir, calls = _sandbox(tmp_path, "Darwin", players=("afplay",))
    sound = _sound(_default_inventory(tmp_path), name)
    assert _run(bindir, event, tmp_path).returncode == 0
    assert _calls(calls) == [f"afplay {sound} tracking=UNSET"]


def test_events_are_independent(tmp_path):
    """A start chime with no end chime is a valid setup, not an error."""
    bindir, calls = _sandbox(tmp_path, "Darwin", players=("afplay",))
    _sound(_default_inventory(tmp_path), "job-start.aiff")
    assert _run(bindir, "completed", tmp_path).returncode == 0
    assert _calls(calls, timeout=0.3) == []


@pytest.mark.parametrize("event", ["", "bogus", "job-start"])
def test_unknown_event_plays_nothing(tmp_path, event):
    bindir, calls = _sandbox(tmp_path, "Darwin", players=("afplay",))
    inventory = _default_inventory(tmp_path)
    _sound(inventory, "job-start.aiff")
    _sound(inventory, "job-end.aiff")
    assert _run(bindir, event, tmp_path).returncode == 0
    assert _calls(calls, timeout=0.3) == []


# ── Player selection ─────────────────────────────────────────────────────────


def test_macos_uses_afplay(tmp_path):
    bindir, calls = _sandbox(tmp_path, "Darwin", players=("afplay", "pw-play"))
    sound = _sound(_default_inventory(tmp_path), "job-start.aiff")
    _run(bindir, "started", tmp_path)
    assert _calls(calls) == [f"afplay {sound} tracking=UNSET"]


@pytest.mark.parametrize(
    "installed,expected",
    [
        (("pw-play", "paplay", "ffplay", "aplay"), "pw-play"),
        (("paplay", "ffplay", "aplay"), "paplay"),
        (("ffplay", "aplay"), "ffplay -nodisp -autoexit -loglevel quiet"),
    ],
)
def test_linux_player_preference(tmp_path, installed, expected):
    bindir, calls = _sandbox(tmp_path, "Linux", players=installed)
    sound = _sound(_default_inventory(tmp_path), "job-end.aiff")
    _run(bindir, "completed", tmp_path)
    assert _calls(calls) == [f"{expected} {sound} tracking=UNSET"]


def test_linux_never_falls_back_to_aplay(tmp_path):
    """aplay cannot decode AIFF and plays unrecognised files as raw noise."""
    bindir, calls = _sandbox(tmp_path, "Linux", players=("aplay",))
    _sound(_default_inventory(tmp_path), "job-start.aiff")
    r = _run(bindir, "started", tmp_path)
    assert (r.returncode, r.stderr) == (0, "")
    assert _calls(calls, timeout=0.3) == []


def test_no_player_installed_is_silent(tmp_path):
    bindir, _ = _sandbox(tmp_path, "Linux")
    _sound(_default_inventory(tmp_path), "job-start.aiff")
    r = _run(bindir, "started", tmp_path)
    assert (r.returncode, r.stdout, r.stderr) == (0, "", "")


def test_failing_player_changes_nothing(tmp_path):
    bindir, calls = _sandbox(
        tmp_path, "Linux", players=("pw-play",), player_body="exit 1"
    )
    _sound(_default_inventory(tmp_path), "job-start.aiff")
    r = _run(bindir, "started", tmp_path)
    assert (r.returncode, r.stdout, r.stderr) == (0, "", "")
    assert len(_calls(calls)) == 1  # it was tried; its failure went nowhere


# ── Inventory location (mirrors fleet_config.resolve_config_path) ────────────


def test_xdg_config_home_is_honored(tmp_path):
    bindir, calls = _sandbox(tmp_path, "Darwin", players=("afplay",))
    _sound(_default_inventory(tmp_path), "job-start.aiff")  # must be ignored
    xdg = tmp_path / "xdg"
    sound = _sound(xdg / "actions-runner", "job-start.aiff")
    _run(bindir, "started", tmp_path, XDG_CONFIG_HOME=str(xdg))
    assert _calls(calls) == [f"afplay {sound} tracking=UNSET"]


def test_actions_runner_config_wins_over_xdg(tmp_path):
    bindir, calls = _sandbox(tmp_path, "Darwin", players=("afplay",))
    xdg = tmp_path / "xdg"
    _sound(xdg / "actions-runner", "job-end.aiff")  # must be ignored
    inventory = tmp_path / "elsewhere" / "fleet"
    sound = _sound(inventory, "job-end.aiff")
    _run(
        bindir,
        "completed",
        tmp_path,
        XDG_CONFIG_HOME=str(xdg),
        ACTIONS_RUNNER_CONFIG=str(inventory / "runners.toml"),
    )
    assert _calls(calls) == [f"afplay {sound} tracking=UNSET"]


# ── Survives the job, never holds it ─────────────────────────────────────────


def test_player_is_exempt_from_orphan_cleanup(tmp_path):
    """The runner kills processes carrying the job's RUNNER_TRACKING_ID right
    after the job-completed hook. The hook's own environment has it; the player's
    must not."""
    bindir, calls = _sandbox(tmp_path, "Linux", players=("pw-play",))
    _sound(_default_inventory(tmp_path), "job-end.aiff")
    _run(bindir, "completed", tmp_path)
    [line] = _calls(calls)
    assert line.endswith("tracking=UNSET")
    assert TRACKING_ID not in line


def test_returns_before_a_slow_player_finishes(tmp_path):
    """capture_output reads pipes to EOF, so a player still holding this
    script's stdout would stall the call for the whole sound."""
    sleep = shutil.which("sleep")
    bindir, calls = _sandbox(
        tmp_path, "Darwin", players=("afplay",), player_body=f"{sleep} 4"
    )
    _sound(_default_inventory(tmp_path), "job-start.aiff")
    started = time.monotonic()
    r = _run(bindir, "started", tmp_path)
    elapsed = time.monotonic() - started
    assert r.returncode == 0
    assert elapsed < 2.0, f"hook blocked for {elapsed:.2f}s behind the player"
    assert len(_calls(calls)) == 1
