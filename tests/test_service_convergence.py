"""Service files (systemd unit / launchd plist) converge from one template.

They used to be heredocs inside the installers, which made drift permanent:
install.sh and install-linux.sh both `continue` on an already-configured runner,
so a service file written by an older kit was never rewritten — on any host,
ever. A fleet host carried `Restart=on-failure` and `KillMode=process` long after
the template moved to `Restart=always`, and its runners silently failed to come
back from a reboot, because the listener exits 0 on self-update and on-failure
does not restart a clean exit. Convergence can only fix what it can render.

The template is therefore a file, rendered by `_render.sh` for the installers and
by `apply.py`'s `render_template()` for convergence. The pair is pinned here: a
second renderer that drifts from the first would rewrite every service file on
every tick, restarting the fleet in a loop.
"""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
BASH = shutil.which("bash")
UNIT_TPL = REPO / "templates" / "github-runner.service.in"
PLIST_TPL = REPO / "templates" / "com.github.actions-runner.plist.in"


def _apply():
    spec = importlib.util.spec_from_file_location("apply", REPO / "apply.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


apply = _apply()


def _directives(text: str) -> str:
    """Config lines only, with comment prose stripped.

    The unit template EXPLAINS the settings it replaced, so a raw substring
    check for `KillMode=process` matches the comment describing why it is gone.
    """
    out = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(("#", "//", "<!--")):
            continue
        out.append(line)
    return "\n".join(out)


def _stale_variant(expected: str) -> str:
    """A plausible older service file, per platform."""
    if apply.IS_MAC:
        # SuccessfulExit=false was the old launchd shape: a clean self-update
        # exit went unrestarted, which is the same fault as Restart=on-failure.
        return expected.replace(
            "<key>SuccessfulExit</key>\n        <true/>",
            "<key>SuccessfulExit</key>\n        <false/>",
        )
    return expected.replace("Restart=always", "Restart=on-failure")


def _render_sh(template: Path, **values: str) -> str:
    """Render via _render.sh — the shell half of the pair."""
    args = []
    for k, v in values.items():
        args += [k, v]
    proc = subprocess.run(
        [
            BASH,
            "-c",
            f'. "{REPO}/_render.sh"; render_template "$@"',
            "_",
            str(template),
            *args,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


# ── the two renderers must agree ─────────────────────────────────────────────


def test_shell_and_python_renderers_produce_identical_units() -> None:
    values = {"RUNNER_NAME": "host-app-1", "RUNNER_DIR": "/home/u/actions-runner/app-1"}
    assert _render_sh(UNIT_TPL, **values) == apply.render_template(UNIT_TPL, **values)


def test_shell_and_python_renderers_produce_identical_plists() -> None:
    values = {
        "LABEL": "com.github.actions-runner.app-1",
        "RUNNER_DIR": "/Users/u/actions-runner/app-1",
        "BASE_DIR": "/Users/u/actions-runner",
        "HOME": "/Users/u",
    }
    assert _render_sh(PLIST_TPL, **values) == apply.render_template(PLIST_TPL, **values)


@pytest.mark.parametrize("tpl", [UNIT_TPL, PLIST_TPL])
def test_a_surviving_placeholder_is_an_error_not_an_output(tpl: Path) -> None:
    """A service file containing a literal @RUNNER_DIR@ loads and never works.

    Both renderers must refuse rather than emit one.
    """
    with pytest.raises(ValueError, match="survived substitution"):
        apply.render_template(tpl)  # no values at all

    proc = subprocess.run(
        [BASH, "-c", f'. "{REPO}/_render.sh"; render_template "{tpl}"'],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode != 0
    assert "after substitution" in proc.stderr


# ── the template is the one the installer uses ───────────────────────────────


def test_the_installers_render_the_template_rather_than_inlining_it() -> None:
    """The heredocs must not come back: an inlined copy cannot be converged."""
    for name in ("install-linux.sh", "install.sh"):
        body = (REPO / name).read_text()
        assert "render_template" in body, name
        assert 'cat >"$UNIT_PATH"' not in body, name
        assert 'cat >"$PLIST_PATH"' not in body, name


def test_the_unit_template_carries_the_settings_the_old_one_lacked() -> None:
    """The drift that motivated all this, pinned so it cannot regress."""
    body = _directives(UNIT_TPL.read_text())
    assert "Restart=always" in body
    assert "KillMode=control-group" in body
    assert "Restart=on-failure" not in body
    assert "KillMode=process" not in body


# ── drift detection ──────────────────────────────────────────────────────────


@pytest.fixture
def fake_host(tmp_path, monkeypatch):
    """A runner base and a service-file directory apply.py will look in."""
    base = tmp_path / "actions-runner"
    (base / "app-1").mkdir(parents=True)
    monkeypatch.setattr(apply, "RUNNER_BASE", base)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    svc_dir = (
        tmp_path / "Library/LaunchAgents"
        if apply.IS_MAC
        else tmp_path / ".config/systemd/user"
    )
    svc_dir.mkdir(parents=True)
    return base, svc_dir


def test_a_missing_service_file_is_not_drift(fake_host) -> None:
    """Installing one is the installer's job.

    Writing a file for a runner that was never set up here would resurrect
    something convergence had deliberately removed.
    """
    assert apply.service_drift(["app-1"]) == []


def test_a_matching_service_file_is_not_drift(fake_host) -> None:
    path, expected = apply.service_file_for("app-1")
    path.write_text(expected)
    assert apply.service_drift(["app-1"]) == []


def test_an_old_service_file_is_drift(fake_host) -> None:
    path, expected = apply.service_file_for("app-1")
    path.write_text(_stale_variant(expected))
    drift = apply.service_drift(["app-1"])
    assert [d[0] for d in drift] == ["app-1"]
    assert drift[0][2] == expected  # carries the corrected text


@pytest.mark.skipif(apply.IS_MAC, reason="systemd unit text")
def test_the_exact_shape_that_broke_a_host_is_detected(fake_host) -> None:
    """KillMode=process + Restart=on-failure — units disabled, listeners orphaned."""
    path, _ = apply.service_file_for("app-1")
    path.write_text(
        "[Unit]\nDescription=GitHub Actions Runner (host-app-1)\n\n"
        "[Service]\nExecStart=/x/run.sh\nRestart=on-failure\nKillMode=process\n\n"
        "[Install]\nWantedBy=default.target\n"
    )
    assert [d[0] for d in apply.service_drift(["app-1"])] == ["app-1"]


# ── applying the rewrite ─────────────────────────────────────────────────────


def test_a_busy_runner_is_left_alone(fake_host) -> None:
    """A rewrite ends in a restart, which would kill a job mid-flight.

    Drift is never urgent enough for that: it persists and the next tick retries.
    """
    path, expected = apply.service_file_for("app-1")
    stale = _stale_variant(expected)
    path.write_text(stale)
    drift = apply.service_drift(["app-1"])
    failed = apply.apply_service_rewrites(drift, set(), is_busy=lambda dn: True)
    assert failed == 0
    assert path.read_text() == stale  # untouched


def test_an_idle_runner_is_rewritten(fake_host, monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(apply, "_run", lambda cmd: calls.append(cmd) or 0)
    monkeypatch.setattr(
        apply, "_svc_restart", lambda dn: calls.append(["restart", dn]) or True
    )

    path, expected = apply.service_file_for("app-1")
    path.write_text(_stale_variant(expected))
    drift = apply.service_drift(["app-1"])
    failed = apply.apply_service_rewrites(drift, set(), is_busy=lambda dn: False)

    assert failed == 0
    assert path.read_text() == expected
    flat = " ".join(" ".join(c) for c in calls)
    if apply.IS_MAC:
        assert "restart app-1" in flat
    else:
        # enable --now, not just restart: the drift that motivated this left
        # units DISABLED, so a restart alone would fix the file and leave the
        # host still unable to come back from a reboot.
        assert "daemon-reload" in flat
        assert "enable --now github-runner-app-1.service" in flat
