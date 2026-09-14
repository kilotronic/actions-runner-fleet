"""Tests for _runner_arch.sh and the installers' use of it.

Both installers used to hardcode one runner build — linux-x64 and osx-arm64 — so
an ARM Linux machine or an Intel Mac downloaded a runner it could not execute,
and the install died at config.sh. They also passed OS and architecture through
--labels, which on the wrong hardware adds a custom label that can route jobs to
the wrong machine; the runner already applies the real ones as default labels.
"""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
HELPER = REPO / "_runner_arch.sh"
BASH = shutil.which("bash")
INSTALLERS = ["install-linux.sh", "install.sh"]


def _stub(path, body):
    path.write_text(f"#!{BASH}\n{body}\n")
    path.chmod(0o755)


def _bin(tmp_path, machine, arm64_hw=None):
    """A PATH with a stub uname and, optionally, a stub sysctl answering
    hw.optional.arm64 — plus the real tools the installer block needs."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    _stub(bindir / "uname", f"echo {machine}")
    if arm64_hw is not None:
        _stub(
            bindir / "sysctl", f'[ "$*" = "-n hw.optional.arm64" ] && echo {arm64_hw}'
        )
    for tool in ("dirname", "sed"):
        (bindir / tool).symlink_to(shutil.which(tool))
    return bindir


def _runner_build(tmp_path, os_name, machine, arm64_hw=None):
    return subprocess.run(
        [BASH, "-c", f'. "{HELPER}"; runner_build {os_name}'],
        env={"PATH": str(_bin(tmp_path, machine, arm64_hw))},
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )


@pytest.mark.parametrize(
    "os_name,machine,arm64_hw,expected",
    [
        ("linux", "x86_64", None, "linux-x64"),
        ("linux", "amd64", None, "linux-x64"),
        ("linux", "aarch64", None, "linux-arm64"),
        ("linux", "armv7l", None, "linux-arm"),
        ("osx", "arm64", "1", "osx-arm64"),
        ("osx", "x86_64", None, "osx-x64"),
        ("osx", "x86_64", "0", "osx-x64"),
        ("osx", "x86_64", "1", "osx-arm64"),  # a Rosetta shell on Apple silicon
    ],
)
def test_runner_build_matches_the_machine(
    tmp_path, os_name, machine, arm64_hw, expected
):
    r = _runner_build(tmp_path, os_name, machine, arm64_hw)
    assert (r.returncode, r.stdout.strip(), r.stderr) == (0, expected, "")


@pytest.mark.parametrize("os_name,machine", [("linux", "riscv64"), ("osx", "armv7l")])
def test_runner_build_refuses_a_machine_with_no_build(tmp_path, os_name, machine):
    r = _runner_build(tmp_path, os_name, machine)
    assert (r.returncode, r.stdout) == (1, "")


def _configuration(tmp_path, installer, machine, arm64_hw=None):
    """Run the installer's real Configuration block — its header through the
    Helpers header — against a stub uname, with $0 set to the copied installer
    exactly as when it runs (SCRIPT_DIR comes from $0), and print RUNNER_URL."""
    kit = tmp_path / "kit"
    kit.mkdir()
    shutil.copy(REPO / installer, kit / installer)
    shutil.copy(HELPER, kit / "_runner_arch.sh")
    script = (
        'source <(sed -n "/^# ── Configuration/,/^# ── Helpers/p" "$0") && '
        'echo "$RUNNER_URL"'
    )
    return subprocess.run(
        [BASH, "-c", script, str(kit / installer)],
        env={"PATH": str(_bin(tmp_path, machine, arm64_hw))},
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )


@pytest.mark.parametrize(
    "installer,machine,build",
    [
        ("install-linux.sh", "aarch64", "linux-arm64"),
        ("install-linux.sh", "x86_64", "linux-x64"),
        ("install.sh", "x86_64", "osx-x64"),
        ("install.sh", "arm64", "osx-arm64"),
    ],
)
def test_installer_downloads_the_build_for_this_machine(
    tmp_path, installer, machine, build
):
    r = _configuration(tmp_path, installer, machine)
    assert (r.returncode, r.stderr) == (0, ""), r.stderr
    assert f"/v2.336.0/actions-runner-{build}-2.336.0.tar.gz" in r.stdout


def test_installer_stops_clearly_on_an_unsupported_machine(tmp_path):
    r = _configuration(tmp_path, "install-linux.sh", "riscv64")
    assert r.returncode == 1
    assert "no Linux build for riscv64" in r.stderr
    # It must be runner_build refusing the machine — not the helper failing to
    # load, which produces the same final message.
    assert "command not found" not in r.stderr
    assert "No such file" not in r.stderr


@pytest.mark.parametrize("installer", INSTALLERS)
def test_installer_does_not_pass_os_or_arch_labels(installer):
    """An actual --labels argument, not a mention in a comment."""
    text = (REPO / installer).read_text()
    assert not re.search(r"^\s*--labels\s", text, re.MULTILINE)
