"""exclude-spotlight.sh — report and verify; the GUI is what actually excludes.

The script used to WRITE the Privacy list with `plutil`. It does not any more,
and must not again: mds does not re-read VolumeConfiguration.plist after an edit
made behind its back, and the only live reload (`launchctl kickstart` of mds) is
refused whenever SIP is on. (Whether mds also DISCARDS the edit, and whether a
reboot would apply it, were not measured.) Measured 2026-09-17 on an 8 GiB Mac
mini: the write reported success, the index kept climbing (60126 -> 73360), and
the GUI then took it to 0 in seconds. So the tests below pin the ABSENCE of that
write path.

Two further traps are pinned here, because both have already produced a
confident wrong answer on this fleet.

`mdutil -i off <vol> && mdutil -i on <vol>` is not a config reload. It ERASES the
volume's index and starts a full rebuild — which is expensive on exactly the
small hosts that need the exclusion, and, worse, makes every path report zero
indexed items for the duration.

Which feeds the second trap: `mdfind -onlyin <p> -count '*'` returning 0 means
"not indexed" ONLY if some control path returns non-zero at the same instant.
Measured during a rebuild, an exclusion that did nothing is indistinguishable
from one that worked. The script must always measure a control and say
INCONCLUSIVE rather than guess.

A note on how these tests are written. The static assertions here are
denylists, and a denylist of literals is walked past by any synonym — PlistBuddy
for plutil, a quoted key, a `tee`, a command chained onto an echo. So the
structural tests below assert on the SHAPE ("$PLIST is only ever read") rather
than on spellings, and the behaviour that matters runs the script for real
against stub `mdfind`/`mdutil` on PATH.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "exclude-spotlight.sh"
BASH = shutil.which("bash")
DARWIN = os.uname().sysname == "Darwin"
macos_only = pytest.mark.skipif(not DARWIN, reason="Spotlight is macOS-only")


def run(*args: str, **env: str):
    return subprocess.run(
        [BASH, str(SCRIPT), *args],
        capture_output=True,
        text=True,
        env={**os.environ, **env},
        check=False,
    )


def _lines():
    """(code, printed) — executable lines, and everything the script can emit.

    A line is dropped from `code` only when it is ENTIRELY a print. A print
    chained to a real command (`echo x && sudo plutil ...`) keeps its command
    half; dropping the whole line is how a re-introduced write would hide.

    Heredoc bodies are the opposite: printed text, never code. They must be IN
    `printed`, including body lines that begin with `#` — a `#`-prefixed line
    inside a heredoc is still printed, and filtering it is how forbidden advice
    would slip past the printed-text checks.
    """
    code, printed, in_heredoc = [], [], False
    for line in SCRIPT.read_text().splitlines():
        stripped = line.strip()
        if in_heredoc:
            if stripped in ("EOF", "'EOF'"):
                in_heredoc = False
            else:
                printed.append(line)
            continue
        if re.match(r"^cat\s+<<-?'?EOF'?", stripped):
            in_heredoc = True
            continue
        if stripped.startswith("#"):
            continue  # a whole-line comment is neither
        printed.append(line)
        if re.match(r"^(echo|printf|say)\s", stripped) and not re.search(
            r"&&|\|\||;|\|", stripped
        ):
            continue  # a line that only prints
        code.append(line)
    return "\n".join(code), "\n".join(printed)


def _code() -> str:
    return _lines()[0]


def _printed() -> str:
    return _lines()[1]


# ── the write path must stay gone ────────────────────────────────────────────


def test_the_plist_is_only_ever_read() -> None:
    """The shape, not a list of spellings.

    A denylist of `plutil -insert` walks past PlistBuddy, a quoted key, or a
    `tee`. So: every line that touches $PLIST must be the one `-extract` read.
    """
    code = _code()
    touching = [
        ln
        for ln in code.splitlines()
        if "PLIST" in ln and not re.match(r'^\s*PLIST=', ln.strip())
    ]
    assert touching, "expected the read to still exist"
    for ln in touching:
        assert "-extract Exclusions" in ln, f"non-read use of $PLIST: {ln.strip()}"
    # And no writer, by any name, anywhere in the executable lines.
    for forbidden in (
        "PlistBuddy",
        "defaults write",
        "plutil -insert",
        "plutil -replace",
        "plutil -remove",
        "tee ",
    ):
        assert forbidden not in code, forbidden


def test_it_never_reloads_or_erases_the_index() -> None:
    """No reload is possible (SIP), and none is wanted; nothing may rebuild.

    The per-volume `mdutil -i off` the script PRINTS as advice is printed text,
    which `_code()` excludes — running one is what is forbidden.
    """
    code = _code()
    for forbidden in ("kickstart", "mdutil -i off", "mdutil -i on", "mdutil -E", "mdutil -X"):
        assert forbidden not in code, forbidden
    assert "launchctl" not in code


def test_it_does_not_recommend_disabling_the_whole_data_volume() -> None:
    """`mdutil -i off -d /System/Volumes/Data` also kills Finder and Spotlight
    search for the whole login. Right only on a host with nobody at the console,
    which this script cannot determine — so it must not suggest it.

    Asserted on the TARGET, not the flag spelling: without `-d` it is the same
    catastrophic advice. Heredoc bodies are included, so `#`-prefixed advice
    inside one cannot hide.
    """
    printed = "\n".join(
        ln
        for ln in _printed().splitlines()
        if not ln.strip().startswith("PLIST=")  # the plist genuinely lives there
    )
    assert "/System/Volumes/Data" not in printed
    assert "-i off -d" not in printed


# ── the report must stay honest ──────────────────────────────────────────────


def test_verification_measures_a_control() -> None:
    """Zero indexed items proves nothing unless something else is non-zero."""
    code = _code()
    assert "_control" in code
    assert "INCONCLUSIVE" in _printed()
    # The branch itself, not just the word: deleting it must fail this.
    assert re.search(r'if\s+\[\[\s+-z\s+"\$_control"', code)


def test_the_volume_advice_skips_the_boot_volume() -> None:
    """macOS firmlinks the system volume back under /Volumes.

    So "/Volumes/Macintosh HD" reports "Indexing enabled" and shares a device id
    with /. Without a skip, the per-volume advice would suggest `mdutil -i off`
    on the BOOT DISK. The skip must COMPARE device ids, not merely compute one,
    and must not be a name match: the alias is renameable.
    """
    code = _code()
    assert "_root_dev" in code
    # The comparison, not just the assignment.
    assert re.search(r'\[\[\s*"\$\(stat -f \'%d\' "\$v".*==\s*"\$_root_dev"', code)
    assert "Macintosh HD" not in code


def test_help_is_derived_from_the_header_not_a_fixed_range(tmp_path: Path) -> None:
    """A hardcoded `sed -n '2,53p'` is correct until the header changes.

    It had already overshot into `set -uo pipefail`. Assert the derivation, then
    prove it: a copy with an extra header line must show that line and still
    stop before the code.
    """
    code = _code()
    assert not re.search(r"sed -n '\d+,\d+p'", code), "fixed line range is back"

    src = SCRIPT.read_text().splitlines(keepends=True)
    end = next(i for i, ln in enumerate(src) if i and not ln.startswith("#"))
    marker = "# UNIQUE-HEADER-MARKER-FOR-TEST\n"
    copy = tmp_path / "copy.sh"
    copy.write_text("".join(src[:end] + [marker] + src[end:]))
    copy.chmod(0o755)
    out = subprocess.run(
        [BASH, str(copy), "--help"], capture_output=True, text=True, check=False
    ).stdout
    assert "UNIQUE-HEADER-MARKER-FOR-TEST" in out, "derivation missed a header line"
    assert "set -uo pipefail" not in out
    assert "PLIST=" not in out


def test_it_sends_the_human_to_the_gui() -> None:
    """It must TELL the user, not merely mention the GUI in a comment."""
    printed = _printed()
    assert "Search Privacy" in printed
    assert "com.apple.Spotlight-Settings.extension" in _code()


def test_it_is_not_wired_into_any_timer_or_installer() -> None:
    """A timer could only half-do the job — but no longer for #21's reason.

    #21 kept it out because it "needs an interactive sudo". After the write path
    was removed that is no longer true: the report needs no privileges and the
    verify explicitly does not. The reason now is stronger — the script cannot
    apply anything at all (only the GUI can), and it exits 1 whenever a path is
    still indexed, which is the common case on a host nobody has fixed yet.

    exclude-ci-paths.sh may NAME it — that is how a human finds it — but nothing
    may invoke it. Covers the real timer machinery, not just the installers.
    """
    for name in (
        "update-host.sh",
        "install.sh",
        "install-linux.sh",
        "maintenance-timer.py",
        "runner_timers.py",
        "apply.py",
    ):
        path = REPO / name
        if not path.exists():
            continue
        code = "\n".join(
            ln.split("#", 1)[0] for ln in path.read_text().splitlines()
        )
        assert "exclude-spotlight.sh" not in code, name
    for tmpl in (REPO / "templates").rglob("*"):
        if tmpl.is_file():
            assert "exclude-spotlight.sh" not in tmpl.read_text(), tmpl.name
    # The reporting script points at it, in its manual-steps output.
    assert "exclude-spotlight.sh" in (REPO / "exclude-ci-paths.sh").read_text()


def test_sudo_is_non_interactive() -> None:
    """These hosts have no passwordless sudo (#21) and sudo prompts on /dev/tty,
    which 2>/dev/null does not suppress. A report-only script must never block
    on a password for a list it says the verify does not need."""
    for ln in _code().splitlines():
        if "sudo" in ln:
            assert re.search(r"sudo -n\b", ln), f"bare sudo: {ln.strip()}"


# ── behaviour ────────────────────────────────────────────────────────────────


def test_it_is_a_no_op_off_macos() -> None:
    """Static assertion — it needs no skipif, and the Linux case is the point."""
    assert "not macOS" in SCRIPT.read_text()


def test_an_unknown_flag_is_refused() -> None:
    assert run("--wat").returncode == 2


def test_removed_flags_are_refused_not_silently_ignored() -> None:
    """--dry-run and --remove described the write path. They must not linger."""
    for gone in ("--dry-run", "--remove"):
        assert run(gone).returncode == 2, gone
    text = SCRIPT.read_text()
    assert "--dry-run" not in text
    assert "--remove" not in text


def test_help_needs_no_privileges_and_no_macos() -> None:
    proc = run("--help")
    assert proc.returncode == 0
    assert "Privacy list" in proc.stdout


@macos_only
def test_a_path_that_is_not_a_directory_is_refused() -> None:
    """Otherwise it reports 0 indexed and reads as EXCLUDED — a typo announcing
    success against a live control."""
    proc = run("--status", "/definitely/not/a/real/dir")
    assert proc.returncode == 1
    assert "not a directory" in proc.stdout + proc.stderr


@macos_only
def test_status_does_not_write_an_existing_plist(tmp_path: Path) -> None:
    """Non-creation is not enough; prove an EXISTING file is untouched."""
    plist = tmp_path / "VolumeConfiguration.plist"
    plist.write_bytes(b"original bytes, do not touch\n")
    before = hashlib.sha256(plist.read_bytes()).hexdigest()
    proc = run("--status", str(tmp_path), SPOTLIGHT_PLIST=str(plist))
    assert proc.returncode == 0
    assert hashlib.sha256(plist.read_bytes()).hexdigest() == before


# ── behaviour, against stub mdfind/mdutil ────────────────────────────────────


def _stubs(tmp_path: Path, counts: dict[str, int]) -> str:
    """A PATH with a fake `mdfind` (per-path counts) and a silent `mdutil`.

    Lets the control/INCONCLUSIVE logic — the reason this script exists — run
    for real instead of being asserted at as a string.
    """
    binp = tmp_path / "bin"
    binp.mkdir(exist_ok=True)
    cases = "\n".join(
        f'  {path!r}) echo {n} ;;'.replace("'", '"') for path, n in counts.items()
    )
    (binp / "mdfind").write_text(
        "#!/usr/bin/env bash\n"
        'target=""\n'
        'while (($#)); do [[ "$1" == "-onlyin" ]] && target="$2"; shift; done\n'
        f'case "$target" in\n{cases}\n  *) echo 0 ;;\nesac\n'
    )
    (binp / "mdutil").write_text('#!/usr/bin/env bash\necho "Indexing disabled."\n')
    for f in ("mdfind", "mdutil"):
        (binp / f).chmod(0o755)
    return f"{binp}:{os.environ['PATH']}"


@macos_only
def test_inconclusive_when_no_control_is_indexed(tmp_path: Path) -> None:
    """During a rebuild EVERY path reads 0, so an exclusion that did nothing is
    indistinguishable from one that worked. It must refuse to guess."""
    target = tmp_path / "tree"
    target.mkdir()
    proc = run(str(target), PATH=_stubs(tmp_path, {}))  # everything 0
    assert "INCONCLUSIVE" in proc.stdout
    assert "EXCLUDED" not in proc.stdout
    assert proc.returncode == 0


@macos_only
def test_excluded_only_against_a_live_control(tmp_path: Path) -> None:
    target = tmp_path / "tree"
    target.mkdir()
    (target / "f").write_text("x")  # non-empty, or 0 is unknown rather than excluded
    counts = {str(Path.home() / "Library"): 42, str(target): 0}
    proc = run(str(target), PATH=_stubs(tmp_path, counts))
    assert "EXCLUDED" in proc.stdout
    assert "INCONCLUSIVE" not in proc.stdout
    assert proc.returncode == 0


@macos_only
def test_still_indexed_exits_nonzero_and_names_the_gui(tmp_path: Path) -> None:
    target = tmp_path / "tree"
    target.mkdir()
    (target / "f").write_text("x")
    counts = {str(Path.home() / "Library"): 42, str(target): 7}
    proc = run(str(target), PATH=_stubs(tmp_path, counts))
    assert "STILL INDEXED" in proc.stdout
    assert "Search Privacy" in proc.stdout
    assert proc.returncode == 1


@macos_only
def test_an_empty_directory_is_not_reported_as_excluded(tmp_path: Path) -> None:
    """A tree with no files reads 0 whether or not it is excluded.

    The control proves the INDEX is live, never that THIS path would have had
    entries — so 0 here is unknown, not success. Reachable for real:
    ~/actions-runner on a fresh host, or _work just after prune.sh. Calling it
    EXCLUDED tells the operator a step is done that was never started.
    """
    empty = tmp_path / "empty"
    empty.mkdir()
    counts = {str(Path.home() / "Library"): 42, str(empty): 0}
    proc = run(str(empty), PATH=_stubs(tmp_path, counts))
    assert "EMPTY" in proc.stdout
    assert "EXCLUDED" not in proc.stdout


@macos_only
def test_a_populated_excluded_directory_still_reads_excluded(tmp_path: Path) -> None:
    """The empty-tree guard must not swallow the real success case."""
    tree = tmp_path / "tree"
    tree.mkdir()
    (tree / "a file").write_text("x")
    counts = {str(Path.home() / "Library"): 42, str(tree): 0}
    proc = run(str(tree), PATH=_stubs(tmp_path, counts))
    assert "EXCLUDED" in proc.stdout
    assert "EMPTY" not in proc.stdout
    assert proc.returncode == 0
