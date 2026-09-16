"""install-polkit-rule.sh — the opt-in logind sleep-inhibitor rule.

Two things here are worth pinning, and both are quiet when broken.

The username is interpolated into a JavaScript string literal in a file that
grants a privilege. A name that closes the quote could widen what the rule
allows, and polkit would load it happily — so the script validates rather than
escapes, and that refusal is tested with an actual injection payload.

The rule must never be created by convergence. It is a privileged policy change,
so opting in is an explicit act; update-host.sh may only re-sync a rule that is
already installed. The previous version asked whether a *source* file existed
inside the checkout, which shipped no such file — so it silently did nothing on
every host for as long as the template has existed.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "install-polkit-rule.sh"
TEMPLATE = REPO / "polkit" / "49-actions-runner-inhibit.rules.in"
BASH = shutil.which("bash")

PLACEHOLDER = "@RUNNER_USER@"


def run(*args: str, dest: Path | None = None, **env: str):
    environ = {**os.environ, **env}
    if dest is not None:
        environ["POLKIT_DEST"] = str(dest)
    return subprocess.run(
        [BASH, str(SCRIPT), *args],
        capture_output=True,
        text=True,
        env=environ,
        check=False,
    )


def test_the_template_carries_a_placeholder_and_no_real_username() -> None:
    body = TEMPLATE.read_text()
    assert PLACEHOLDER in body
    # The rule this replaced hardcoded one operator's login. A template that
    # still named a person would install a rule granting nothing on any other
    # host, and look installed while doing it.
    assert 'subject.user == "@RUNNER_USER@"' in body


def test_rendering_substitutes_every_placeholder() -> None:
    out = run("--dry-run", "--user", "ci").stdout
    assert 'subject.user == "ci"' in out
    assert PLACEHOLDER not in out


def test_only_the_two_inhibit_actions_are_granted() -> None:
    body = TEMPLATE.read_text()
    granted = [ln for ln in body.splitlines() if "action.id ==" in ln]
    assert len(granted) == 2
    assert all("inhibit-block-" in ln for ln in granted)
    # One YES, and nothing else returned — a second Result would widen this.
    assert body.count("polkit.Result") == 1


@pytest.mark.parametrize(
    "hostile",
    [
        'x"; return polkit.Result.YES; //',
        'root" || subject.user == "anyone',
        "has space",
        "UPPER",
        "trailing\nnewline",
    ],
)
def test_a_username_that_is_not_a_plain_posix_name_is_refused(hostile: str) -> None:
    proc = run("--dry-run", "--user", hostile)
    assert proc.returncode == 2, proc.stdout
    assert "not a plain POSIX username" in proc.stdout + proc.stderr


def test_an_empty_username_is_refused_by_the_argument_parser() -> None:
    """A different guard than the shape check, so it exits 1, not 2."""
    proc = run("--dry-run", "--user", "")
    assert proc.returncode != 0
    assert "--user needs a username" in proc.stdout + proc.stderr


def test_dry_run_never_writes(tmp_path: Path) -> None:
    dest = tmp_path / "rule.rules"
    proc = run("--dry-run", "--user", "ci", dest=dest)
    assert proc.returncode == 0
    assert not dest.exists()


def test_dry_run_works_off_linux() -> None:
    """Previewing a privilege grant must not require the machine it targets."""
    proc = run("--dry-run", "--user", "ci")
    assert proc.returncode == 0
    assert "polkit.addRule" in proc.stdout


def test_uninstalling_an_absent_rule_is_a_no_op(tmp_path: Path) -> None:
    proc = run("--uninstall", "--dry-run", dest=tmp_path / "absent.rules")
    assert proc.returncode == 0
    assert "nothing to remove" in proc.stdout


def test_an_unknown_argument_is_refused() -> None:
    proc = run("--wat")
    assert proc.returncode == 2


def test_convergence_may_resync_the_rule_but_never_create_it() -> None:
    """update-host.sh gates on the DESTINATION existing, not on a source file.

    Asserted structurally: exercising it for real would need a stub `sudo` and a
    fake /etc, and the property that matters is which path the condition names.
    Gating on a source inside the checkout is exactly the bug this replaced —
    that file has not shipped since the rule became a template, so the block was
    a silent no-op everywhere.
    """
    body = (REPO / "update-host.sh").read_text()
    assert '-f "$POLKIT_DEST"' in body
    assert "POLKIT_TEMPLATE=polkit/49-actions-runner-inhibit.rules.in" in body
    # The old, broken shape must not come back.
    assert "POLKIT_RULE=polkit/49-actions-runner-inhibit.rules\n" not in body


def test_the_installer_is_not_called_by_any_installer() -> None:
    """Opting in stays an explicit act — no install path may run it for you."""
    for name in ("install.sh", "install-linux.sh", "update-host.sh"):
        # Comments may name it — update-host.sh explains whose job creation is.
        # An invocation is the thing to catch, so strip comment bodies first.
        code = "\n".join(
            ln.split("#", 1)[0] for ln in (REPO / name).read_text().splitlines()
        )
        assert "install-polkit-rule.sh" not in code, name


# ── The mutating path ────────────────────────────────────────────────────────
# Everything above uses --dry-run, which is most of the logic but none of the
# writing. These run the real install and uninstall against a temp destination,
# with `sudo` stubbed to drop the privilege flags it cannot honour in CI. Linux
# only: off Linux the script correctly refuses to touch anything.

SUDO_STUB = """#!/usr/bin/env bash
# Stand in for sudo: drop -n, and drop `install`'s -o/-g since the test user
# cannot chown to root. Everything else runs as given.
args=()
while (($#)); do
  case "$1" in
    -n) shift ;;
    -o|-g) shift 2 ;;
    *) args+=("$1"); shift ;;
  esac
done
exec "${args[@]}"
"""


@pytest.fixture
def stub_sudo(tmp_path: Path):
    binp = tmp_path / "bin"
    binp.mkdir()
    stub = binp / "sudo"
    stub.write_text(SUDO_STUB)
    stub.chmod(0o755)
    return f"{binp}:{os.environ['PATH']}"


linux_only = pytest.mark.skipif(
    os.uname().sysname != "Linux", reason="polkit/logind is Linux-only"
)


@linux_only
def test_install_writes_the_rendered_rule_then_is_idempotent(
    tmp_path: Path, stub_sudo: str
) -> None:
    dest = tmp_path / "49-actions-runner-inhibit.rules"

    first = run("--user", "ci", dest=dest, PATH=stub_sudo)
    assert first.returncode == 0, first.stderr
    assert dest.exists()
    body = dest.read_text()
    assert 'subject.user == "ci"' in body
    assert PLACEHOLDER not in body
    assert "installed" in first.stdout

    # Second run must change nothing and say so — update-host.sh relies on this
    # comparison to avoid reinstalling on every convergence pass.
    second = run("--user", "ci", dest=dest, PATH=stub_sudo)
    assert second.returncode == 0
    assert "already current" in second.stdout
    assert dest.read_text() == body


@linux_only
def test_changing_the_user_rewrites_an_existing_rule(
    tmp_path: Path, stub_sudo: str
) -> None:
    dest = tmp_path / "rule.rules"
    run("--user", "ci", dest=dest, PATH=stub_sudo)
    run("--user", "runner", dest=dest, PATH=stub_sudo)
    body = dest.read_text()
    assert 'subject.user == "runner"' in body
    assert '"ci"' not in body


@linux_only
def test_uninstall_removes_an_installed_rule(tmp_path: Path, stub_sudo: str) -> None:
    dest = tmp_path / "rule.rules"
    run("--user", "ci", dest=dest, PATH=stub_sudo)
    assert dest.exists()
    proc = run("--uninstall", dest=dest, PATH=stub_sudo)
    assert proc.returncode == 0
    assert not dest.exists()
