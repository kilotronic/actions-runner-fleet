#!/usr/bin/env python3
"""Shared runner discovery + busy/idle detection for the runner fleet.

One module replacing three incompatible busy signals (`gh` API polling in
apply.py, `ps` process scanning in load-watchdog.py, `_diag` log parsing in
runner-status.30s.py) with a single one: presence of a `Runner.Worker` process
whose path is under the runner's directory. Works offline, needs no network,
parses no logs.

House pattern (mirrors runner_timers.py): pure functions take injected inputs
(a directory tree via `base_dir`, a process listing via `workers`) and are
unit-tested over fakes in test_runner_fleet.py; discover_runners/worker_cmdlines
are the thin adapters that gather real OS state when the input isn't injected.

NOTE: runner-status.30s.py (a SwiftBar menu bar plugin) imports this module and
runs under macOS system Python 3.9 (see commit 89b8748). Keep this file
3.9-compatible: no `match`, no `X | Y` union syntax, no 3.10+ stdlib APIs.

Usage:
    python3 runner_fleet.py --json      # emit discovered runners as a JSON array
    python3 runner_fleet.py --any-busy  # exit 0 if any runner is busy, 1 if idle
"""

import argparse
import json
import os
import re
import subprocess
import sys
from collections import namedtuple
from pathlib import Path

RUNNER_BASE = Path.home() / "actions-runner"

_GH_URL_RE = re.compile(r"https?://github\.com/([^/]+/[^/]+?)/?$")

Runner = namedtuple("Runner", ["dir", "name", "repo"])


# ── Pure helpers (no I/O — unit-tested over fakes in test_runner_fleet.py) ────


def _repo_from_gh_url(url):
    """owner/repo parsed from a .runner file's gitHubUrl, or None if unrecognized."""
    if not url:
        return None
    m = _GH_URL_RE.match(url)
    return m.group(1) if m else None


def is_busy(runner, workers=None):
    """True iff `runner` has a live Runner.Worker process under its directory.

    The fleet's one busy signal: process presence. `runner` is a Runner (from
    discover_runners) or a bare directory path. Matched by directory-plus-
    separator in the worker's argv, so partygame-1 never matches partygame-10's
    worker. `workers` is an injected list of `ps` command-line strings — pass a
    fake list in tests; real callers omit it and a live `ps -A -o command=`
    scan is used.

    **Caveats:** The match is an unanchored substring over the full cmdline,
    so a process merely referencing the runner dir path (e.g. `tail -f
    .../partygame-1/_diag/Runner.Worker....log`) counts as busy — fail-safe
    direction that defers action. The job-assigned-but-worker-not-yet-spawned
    window reads as idle; removals are backstopped by GitHub's server-side busy
    rejection in config.sh remove; the .env restart path re-checks immediately
    before acting to shrink the race.
    """
    if workers is None:
        workers = worker_cmdlines()
    runner_dir = runner.dir if isinstance(runner, Runner) else runner
    needle = str(runner_dir) + os.sep
    return any(needle in ln for ln in workers)


# ── Adapters (real OS state) ───────────────────────────────────────────────


def _load_runner_config(cfg_path):
    """Parse a .runner file's JSON (UTF-8 BOM safe). None if missing/unreadable/
    malformed — the runner dir is still discovered, just without a repo."""
    try:
        return json.loads(cfg_path.read_text(encoding="utf-8-sig"))
    except (json.JSONDecodeError, OSError):
        return None


def discover_runners(base_dir=None):
    """Scan base_dir (default ~/actions-runner) for runner directories: any dir
    containing a `.runner` file, one Runner per dir.

    A dir whose `.runner` file is missing/unreadable/malformed, or whose
    gitHubUrl doesn't parse, still yields a Runner (repo=None, name falls back
    to the dir basename) rather than being dropped — host-local consumers
    (load-watchdog's busy scan) need every runner dir regardless of whether
    it can be attributed to a repo; repo-scoped consumers filter on `.repo`.

    Returns a list of Runner(dir, name, repo), sorted by dir name.
    """
    base = Path(base_dir) if base_dir is not None else RUNNER_BASE
    out = []
    if not base.is_dir():
        return out
    for entry in sorted(base.iterdir()):
        if not entry.is_dir():
            continue
        cfg = entry / ".runner"
        if not cfg.is_file():
            continue
        data = _load_runner_config(cfg) or {}
        repo = _repo_from_gh_url(data.get("gitHubUrl", ""))
        name = data.get("agentName") or entry.name
        out.append(Runner(dir=entry, name=name, repo=repo))
    return out


def worker_cmdlines():
    """Command lines of every running Runner.Worker process on this host (one
    exists per active job). One shared `ps` scan feeds is_busy() for every
    runner, so a caller checking many runners still shells out once.
    """
    r = subprocess.run(["ps", "-A", "-o", "command="], capture_output=True, text=True, check=False)
    return [ln for ln in r.stdout.splitlines() if "Runner.Worker" in ln]


# ── CLI ─────────────────────────────────────────────────────────────────────


def _to_json(runner, workers):
    return {
        "dir": str(runner.dir),
        "name": runner.name,
        "repo": runner.repo,
        "busy": is_busy(runner, workers),
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--json", action="store_true", help="emit discovered runners as JSON"
    )
    ap.add_argument(
        "--any-busy",
        action="store_true",
        help="print nothing; exit 0 if any runner is busy, 1 if idle",
    )
    args = ap.parse_args(argv)

    if args.any_busy:
        # Fail toward busy: any exception here (permissions, no `ps`, ...)
        # must read as busy, not idle — see the caller's fail-safe contract
        # in hooks/ensure-orbstack.sh's _any_runner_busy.
        try:
            workers = worker_cmdlines()
            return 0 if any(is_busy(r, workers) for r in discover_runners()) else 1
        except Exception:
            return 0

    runners = discover_runners()
    workers = worker_cmdlines()

    if args.json:
        print(json.dumps([_to_json(r, workers) for r in runners]))
        return 0

    for r in runners:
        state = "busy" if is_busy(r, workers) else "idle"
        print("{}\t{}\t{}\t{}".format(r.dir.name, r.repo or "?", r.name, state))
    return 0


if __name__ == "__main__":
    sys.exit(main())
