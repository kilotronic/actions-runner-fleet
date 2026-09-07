#!/usr/bin/env python3
"""Pause this host's idle runners while a laptop lid is closed.

GitHub will assign work to any connected listener. A lid-closed laptop can
still receive jobs (Power Nap keeps the long-poll alive) and then run them
throttled or asleep. The one lever a host controls is whether the listener
is connected — a stopped service goes *offline* and GitHub routes elsewhere
(or queues the job if nothing is free).

A machine is a laptop iff IOPM reports `AppleClamshellState`. Desktops and
minis lack the key, so this watchdog is a no-op there. Linux has no lid
signal here; `--install-timer` is a no-op rather than an error.

Only idle runners are paused. A job already running is left alone. Runners
this watchdog paused are resumed when the lid opens, except any the load
watchdog still has offline.

A short launchd StartInterval drives it — mandatory, because a paused
runner accepts no jobs, so no job-completed hook ever fires to bring it
back. Install once with `--install-timer`.

Usage:
    ./lid-watchdog.py --tick            # one evaluation (default; what the timer runs)
    ./lid-watchdog.py --tick --dry-run  # evaluate and print, change nothing
    ./lid-watchdog.py --status          # show lid state and what a tick would do
    ./lid-watchdog.py --install-timer   # install + start the per-host timer (idempotent)
    ./lid-watchdog.py --uninstall-timer # remove the timer

Opt out on a host: `touch ~/actions-runner/.no-lid-watchdog`.
"""

import argparse
import importlib.util
import json
import platform
import re
import subprocess
import sys
from collections import namedtuple
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import runner_fleet
import runner_timers

IS_MAC = platform.system() == "Darwin"
SCRIPT_PATH = Path(__file__).resolve()
BASE_DIR = Path.home() / "actions-runner"
STATE_FILE = BASE_DIR / "lid-watchdog.state"
LOAD_STATE_FILE = BASE_DIR / "load-watchdog.state"
OPT_OUT_FILE = BASE_DIR / ".no-lid-watchdog"
LOG_FILE = BASE_DIR / "logs" / "lid-watchdog.log"

WATCHDOG_LABEL = "com.github.actions-runner.lid-watchdog"
TICK_SECONDS = 15

_lw = None


def _load_watchdog():
    """Load load-watchdog.py for its service-control helpers. Lazy so this
    module's pure functions stay importable without pulling that file in."""
    global _lw
    if _lw is None:
        spec = importlib.util.spec_from_file_location(
            "lw", SCRIPT_PATH.with_name("load-watchdog.py")
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _lw = mod
    return _lw


# ── Pure decision core (no I/O — unit-tested in tests/test_lid_watchdog.py) ──

Plan = namedtuple("Plan", ["to_pause", "to_resume"])

_CLAMSHELL_RE = re.compile(r'"AppleClamshellState"\s*=\s*(Yes|No)')


def parse_clamshell(text):
    """Lid state from `ioreg` text: True closed, False open, None not a laptop.

    A laptop is defined by the presence of AppleClamshellState, not by the
    hardware model string — Apple Silicon model ids are not `MacBook*`.
    """
    m = _CLAMSHELL_RE.search(text)
    if not m:
        return None
    return m.group(1) == "Yes"


def decide(lid_closed, paused, idle, *, load_paused=()):
    """Decide what to pause/resume this tick. Pure: same inputs → same Plan.

    lid_closed   True closed, False open, None not a laptop (no-op)
    paused       runner ids this watchdog has already paused
    idle         runner ids currently idle (running a service, not mid-job)
    load_paused  runner ids the load watchdog still has offline — never resume
                 those here, or the two watchdogs fight
    """
    if lid_closed is None:
        return Plan(to_pause=[], to_resume=[])

    paused, idle, load_paused = set(paused), set(idle), set(load_paused)

    if lid_closed:
        return Plan(to_pause=sorted(idle - paused), to_resume=[])
    return Plan(to_pause=[], to_resume=sorted(paused - load_paused))


# ── I/O ───────────────────────────────────────────────────────────────────────


def read_clamshell():
    """Live lid state, or None on non-macOS / missing key / ioreg failure."""
    if not IS_MAC:
        return None
    try:
        r = subprocess.run(
            ["ioreg", "-r", "-k", "AppleClamshellState", "-d", "4"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None
    return parse_clamshell(r.stdout)


def load_state():
    try:
        data = json.loads(STATE_FILE.read_text())
        return list(data.get("paused", []))
    except (FileNotFoundError, json.JSONDecodeError, OSError, ValueError):
        return []


def save_state(paused):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps({"paused": sorted(paused)}))


def load_watchdog_paused():
    try:
        data = json.loads(LOAD_STATE_FILE.read_text())
        return list(data.get("paused", []))
    except (FileNotFoundError, json.JSONDecodeError, OSError, ValueError):
        return []


def log(msg):
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with LOG_FILE.open("a") as f:
        f.write(f"[{datetime.now(UTC):%Y-%m-%dT%H:%M:%SZ}] {msg}\n")


def tick(dry_run=False):
    if OPT_OUT_FILE.exists():
        return

    lid_closed = read_clamshell()
    paused = load_state()
    lw = _load_watchdog()
    runners = lw.discover_runners()
    workers = runner_fleet.worker_cmdlines()
    idle = {
        rid
        for rid, d in runners.items()
        if lw.service_active(rid) and lw.runner_idle(d, workers)
    }
    plan = decide(lid_closed, paused, idle, load_paused=load_watchdog_paused())

    if dry_run:
        print(f"lid_closed={lid_closed} paused={sorted(paused)}")
        print(f"idle={sorted(idle)}")
        print(f"plan: pause={plan.to_pause} resume={plan.to_resume}")
        return

    done_pause, done_resume = [], []
    for rid in plan.to_pause:
        if rid in runners and lw.runner_idle(runners[rid]) and lw.pause_service(rid):
            done_pause.append(rid)
    for rid in plan.to_resume:
        if lw.resume_service(rid):
            done_resume.append(rid)

    new_paused = (set(paused) | set(done_pause)) - set(done_resume)
    save_state(new_paused)

    if done_pause or done_resume:
        log(
            f"lid_closed={lid_closed} "
            f"paused={done_pause or '-'} resumed={done_resume or '-'} "
            f"now_paused={sorted(new_paused) or '-'}"
        )


def install_timer():
    if not IS_MAC:
        print("lid-watchdog: macOS-only; skipping")
        return
    runner_timers.install_timer(
        label=WATCHDOG_LABEL,
        unit=None,
        program=SCRIPT_PATH,
        args=["--tick"],
        interval=TICK_SECONDS,
        log=LOG_FILE,
        service_description=(
            "GitHub Actions runner lid watchdog (pause runners when the lid is closed)"
        ),
        timer_description=f"Run the runner lid watchdog every {TICK_SECONDS}s",
        path_env_mac="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
    )


def uninstall_timer():
    if not IS_MAC:
        print("lid-watchdog: macOS-only; nothing to uninstall")
        return
    runner_timers.uninstall_timer(
        label=WATCHDOG_LABEL,
        unit=None,
        message="uninstalled lid-watchdog timer",
    )


def status():
    lid_closed = read_clamshell()
    paused = load_state()
    lw = _load_watchdog()
    runners = lw.discover_runners()
    workers = runner_fleet.worker_cmdlines()
    idle = {
        rid
        for rid, d in runners.items()
        if lw.service_active(rid) and lw.runner_idle(d, workers)
    }
    plan = decide(lid_closed, paused, idle, load_paused=load_watchdog_paused())
    print(f"lid_closed: {lid_closed}   (None = not a laptop / not macOS)")
    print(f"runners: {sorted(runners)}")
    print(f"idle:    {sorted(idle)}")
    print(f"state:   paused={sorted(paused)}")
    print(f"opt-out: {OPT_OUT_FILE.exists()}")
    print(f"next tick would: pause={plan.to_pause} resume={plan.to_resume}")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--tick", action="store_true", help="evaluate once (default)")
    g.add_argument(
        "--status",
        action="store_true",
        help="print lid/state and what a tick would do",
    )
    g.add_argument(
        "--install-timer",
        action="store_true",
        help="install + start the per-host timer",
    )
    g.add_argument(
        "--uninstall-timer", action="store_true", help="remove the per-host timer"
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="with --tick: evaluate but change nothing",
    )
    args = ap.parse_args()

    if args.install_timer:
        install_timer()
    elif args.uninstall_timer:
        uninstall_timer()
    elif args.status:
        status()
    else:
        tick(dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
