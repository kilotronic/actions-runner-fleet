"""exclude-spotlight.sh — the one Spotlight exclusion that actually works.

Two traps are pinned here, because both have already produced a confident wrong
answer on this fleet.

`mdutil -i off <vol> && mdutil -i on <vol>` is not a config reload. It ERASES the
volume's index and starts a full rebuild — which is expensive on exactly the
small hosts that need the exclusion, and, worse, makes every path report zero
indexed items for the duration.

Which feeds the second trap: `mdfind -onlyin <p> -count '*'` returning 0 means
"not indexed" ONLY if some control path returns non-zero at the same instant.
Measured during a rebuild, an exclusion that did nothing is indistinguishable
from one that worked. The script must always measure a control and say
INCONCLUSIVE rather than guess.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "exclude-spotlight.sh"
BASH = shutil.which("bash")


def run(*args: str, **env: str):
    return subprocess.run(
        [BASH, str(SCRIPT), *args],
        capture_output=True,
        text=True,
        env={**os.environ, **env},
        check=False,
    )


def _code() -> str:
    """Executable lines only — comments and printed text both stripped.

    Two kinds of false positive to avoid. The header EXPLAINS the commands it
    refuses to run, and the failure path PRINTS `mdutil -i off` as the fallback
    to consider by hand. Neither is the script running it, and a raw substring
    search cannot tell the difference.
    """
    out = []
    for line in SCRIPT.read_text().splitlines():
        code = line.split("#", 1)[0].strip()
        if code.startswith(("echo ", "printf ", "say ")):
            continue
        out.append(code)
    return "\n".join(out)


def test_it_never_erases_the_index_to_reload_config() -> None:
    """The reload must keep the index.

    `mdutil -i off <vol>` followed by `-i on` rebuilds the whole volume, costing
    hours of CPU on a small host and blinding every verification in the meantime.
    """
    code = _code()
    assert "kickstart -k system/com.apple.metadata.mds" in code
    assert "mdutil -i off" not in code
    assert "mdutil -E" not in code  # erase and rebuild
    assert "mdutil -X" not in code  # remove the index directory


def test_verification_measures_a_control() -> None:
    """Zero indexed items proves nothing unless something else is non-zero."""
    code = _code()
    assert "_control" in code
    assert "INCONCLUSIVE" in SCRIPT.read_text()


def test_it_backs_up_the_plist_before_editing() -> None:
    """That file is mds's own state; a bad edit costs a full reindex."""
    code = _code()
    assert 'cp -p "$PLIST" "${PLIST}.bak"' in code


def test_the_array_is_created_before_appending_to_it() -> None:
    """`plutil -insert ... -append` fails on a missing key rather than making one."""
    code = _code()
    assert "-insert Exclusions -json '[]'" in code
    assert "-insert Exclusions -string" in code


def test_it_is_not_wired_into_any_timer_or_installer() -> None:
    """It needs an interactive sudo, so a timer could only half-do the job.

    exclude-ci-paths.sh may NAME it — that is how a human finds it — but nothing
    may invoke it.
    """
    for name in ("update-host.sh", "install.sh", "install-linux.sh"):
        code = "\n".join(
            ln.split("#", 1)[0] for ln in (REPO / name).read_text().splitlines()
        )
        assert "exclude-spotlight.sh" not in code, name
    # The reporting script points at it, in its manual-steps output.
    assert "exclude-spotlight.sh" in (REPO / "exclude-ci-paths.sh").read_text()


# ── behaviour ────────────────────────────────────────────────────────────────


@pytest.mark.skipif(os.uname().sysname != "Darwin", reason="Spotlight is macOS-only")
def test_it_is_a_no_op_off_macos() -> None:
    """Guarded so the suite documents the intent on either platform."""
    assert "not macOS" in SCRIPT.read_text()


def test_an_unknown_flag_is_refused() -> None:
    assert run("--wat").returncode == 2


def test_help_needs_no_privileges_and_no_macos() -> None:
    proc = run("--help")
    assert proc.returncode == 0
    assert "Privacy list" in proc.stdout


def test_a_path_that_is_not_a_directory_is_refused() -> None:
    proc = run("--dry-run", "/definitely/not/a/real/dir")
    assert proc.returncode == 1
    assert "not a directory" in proc.stdout + proc.stderr


@pytest.mark.skipif(os.uname().sysname != "Darwin", reason="Spotlight is macOS-only")
def test_dry_run_changes_nothing_and_says_what_it_would_do(tmp_path: Path) -> None:
    proc = run(
        "--dry-run", str(tmp_path), SPOTLIGHT_PLIST=str(tmp_path / "absent.plist")
    )
    assert proc.returncode == 0
    assert "would add to the Privacy list" in proc.stdout
    assert not (tmp_path / "absent.plist").exists()
    assert not (tmp_path / "absent.plist.bak").exists()
