"""Tests for _fs_health.sh, the filesystem-pressure lines in status.sh.

A host that has run out of room fails jobs without ever mentioning space: a
package manager cannot write its signature splits and dies behind a wall of GPG
errors, a browser install reports success with nothing runnable in it. Both
shapes cost real runs here before anything reported the cause, which is why the
report exists at all — and why it covers the temp filesystem, not just the
volume. A tmpfs /tmp is the one that bites hardest: it is capped far below the
disk and it spends RAM.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
LIB = REPO / "_fs_health.sh"
BASH = shutil.which("bash")


def _stub(path, body):
    path.write_text(f"#!/usr/bin/env bash\n{body}\n")
    path.chmod(0o755)


def _run(tmp_path, args, *, avail_kb, used_pct, fstype="ext4"):
    """Call fs_report with df and stat stubbed to a chosen filesystem state."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    _stub(
        bin_dir / "df",
        f'echo "Filesystem 1024-blocks Used Available Capacity Mounted"\n'
        f'echo "stub 100 100 {avail_kb} {used_pct}% /x"',
    )
    _stub(bin_dir / "stat", f'echo "{fstype}"')
    script = f'PATH="{bin_dir}:$PATH"; . "{LIB}"; fs_report {args}'
    return subprocess.run(
        [BASH, "-c", script], capture_output=True, text=True, check=False
    ).stdout.rstrip("\n")


@pytest.mark.skipif(not BASH, reason="bash not available")
class TestFsReport:
    def test_reports_free_space_and_used_percent(self, tmp_path):
        out = _run(tmp_path, "disk /x 40 85", avail_kb=200 * 1048576, used_pct=20)
        assert "200GiB free" in out
        assert "20% used" in out

    def test_uses_the_label_it_is_given(self, tmp_path):
        out = _run(tmp_path, "tmp /x 1 85", avail_kb=5 * 1048576, used_pct=20)
        assert out.startswith("  tmp:")

    def test_flags_low_when_both_thresholds_are_crossed(self, tmp_path):
        out = _run(tmp_path, "tmp /x 1 85", avail_kb=100 * 1024, used_pct=99)
        assert "LOW" in out

    def test_healthy_when_only_one_threshold_is_crossed(self, tmp_path):
        # A big volume can sit above the used-% line with plenty left.
        out = _run(tmp_path, "disk /x 40 85", avail_kb=200 * 1048576, used_pct=90)
        assert "LOW" not in out

    def test_a_tmpfs_is_named_as_ram_backed(self, tmp_path):
        out = _run(tmp_path, "tmp /x 1 85", avail_kb=5 * 1048576, used_pct=20, fstype="tmpfs")
        assert "RAM-backed" in out

    def test_a_disk_filesystem_is_not_called_ram_backed(self, tmp_path):
        out = _run(tmp_path, "disk /x 40 85", avail_kb=200 * 1048576, used_pct=20)
        assert "RAM-backed" not in out

    def test_a_low_note_is_appended_when_given(self, tmp_path):
        out = _run(
            tmp_path, "disk /x 40 85 'convergence will prune'", avail_kb=0, used_pct=99
        )
        assert "convergence will prune" in out
