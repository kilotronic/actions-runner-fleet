#!/usr/bin/env python3
"""Pause this host's idle runners when sustained load is high; resume when it drops.

GitHub has no load-aware scheduling: among idle runners matching a job's labels
its pick is arbitrary. The one lever a host controls is whether a runner's
listener is connected — a stopped service goes *offline* and GitHub routes
elsewhere (or queues the job if nothing is free). This watchdog uses that lever:
when per-core load stays above HIGH for DEBOUNCE consecutive ticks and a runner
is idle, it stops that runner's service (offline, still *registered* — jobs queue,
never fail). When load falls below LOW it restarts the runners it paused.

It is per-host and deliberately dumb: no GitHub API, no fleet coordination. If
every matching box pauses at once, jobs simply queue until one recovers.

A small periodic timer drives it (launchd StartInterval on macOS, a systemd
--user timer on Linux) — mandatory, because a paused runner accepts no jobs, so
no job-completed hook ever fires to bring it back. Install the timer once with
`--install-timer`; thereafter it runs `--tick` every 60s.

Usage:
    ./load-watchdog.py --tick            # one evaluation (default; what the timer runs)
    ./load-watchdog.py --tick --dry-run  # evaluate and print, change nothing
    ./load-watchdog.py --status          # show load, state, and what a tick would do
    ./load-watchdog.py --install-timer   # install + start the per-host timer (idempotent)
    ./load-watchdog.py --uninstall-timer # remove the timer

Opt out on a host: `touch ~/actions-runner/.no-load-watchdog`.
"""

import argparse
import json
import os
import platform
import subprocess
import sys
from collections import namedtuple
from datetime import UTC, datetime
from pathlib import Path

# The timer install/uninstall boilerplate is shared with the maintenance timer;
# discovery + the busy/idle process signal are shared via runner_fleet.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import runner_fleet
import runner_timers

# ── Tunables (v1 defaults; per-core load = loadavg[0] / cpu_count) ────────────

HIGH = 1.25  # pause when per-core load exceeds this (and a runner is idle)
LOW = 0.7  # resume paused runners once per-core load drops below this
DEBOUNCE = 1  # consecutive high ticks required before pausing (with TICK=60s, ~1 min)
TICK_SECONDS = 60  # timer cadence; baked into the installed launchd/systemd timer

# ── Paths ─────────────────────────────────────────────────────────────────────

IS_MAC = platform.system() == "Darwin"
SCRIPT_PATH = Path(__file__).resolve()
BASE_DIR = (
    Path.home() / "actions-runner"
)  # NB: no leading dot (matches .repo-path etc.)
STATE_FILE = BASE_DIR / "load-watchdog.state"
OPT_OUT_FILE = BASE_DIR / ".no-load-watchdog"
LOG_FILE = BASE_DIR / "logs" / "load-watchdog.log"

WATCHDOG_LABEL = "com.github.actions-runner.load-watchdog"  # macOS launchd label
WATCHDOG_UNIT = "github-runner-load-watchdog"  # Linux systemd unit stem

# ── Pure decision core (no I/O — unit-tested in test_load_watchdog.py) ────────

Plan = namedtuple("Plan", ["to_pause", "to_resume", "high_ticks"])


def decide(load, high_ticks, paused, idle, *, low=LOW, high=HIGH, debounce=DEBOUNCE):
    """Decide what to pause/resume this tick. Pure: same inputs → same Plan.

    load        per-core load (loadavg[0] / cpu_count)
    high_ticks  consecutive prior ticks above `high` (from persisted state)
    paused      runner ids this watchdog has already paused
    idle        runner ids currently idle (running a service, not mid-job)

    Returns a Plan of runner ids to pause and resume plus the new high_ticks.
    Only runners this watchdog paused are ever resumed; only idle runners are
    ever paused.
    """
    paused, idle = set(paused), set(idle)

    if load < low:
        # Comfortable again: bring back everything we paused.
        return Plan(to_pause=[], to_resume=sorted(paused), high_ticks=0)

    if load > high:
        high_ticks += 1
        if high_ticks >= debounce:
            return Plan(
                to_pause=sorted(idle - paused), to_resume=[], high_ticks=high_ticks
            )
        return Plan(to_pause=[], to_resume=[], high_ticks=high_ticks)

    # Dead-band (low <= load <= high): hold steady. Reset the pause debounce so
    # only *consecutive* high ticks pause; leave already-paused runners paused
    # (they return only when load truly drops below `low`).
    return Plan(to_pause=[], to_resume=[], high_ticks=0)


# ── State persistence ─────────────────────────────────────────────────────────


def load_state():
    try:
        data = json.loads(STATE_FILE.read_text())
        return int(data.get("high_ticks", 0)), list(data.get("paused", []))
    except (FileNotFoundError, json.JSONDecodeError, OSError, ValueError):
        return 0, []


def save_state(high_ticks, paused):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(
        json.dumps({"high_ticks": high_ticks, "paused": sorted(paused)})
    )


# ── Runner discovery & idle detection (shared via runner_fleet) ───────────────


def per_core_load():
    return os.getloadavg()[0] / (os.cpu_count() or 1)


def discover_runners():
    """Return {runner_id: runner_dir} for every configured runner on this host.

    runner_id is the directory basename (e.g. "partygame-1") — the stable key
    that maps to the launchd label / systemd unit. Delegates to runner_fleet;
    every dir with a .runner file is included regardless of repo (this
    watchdog is per-host, not per-repo).
    """
    return {r.dir.name: r.dir for r in runner_fleet.discover_runners(base_dir=BASE_DIR)}


def runner_idle(runner_dir, workers=None):
    """True iff this runner has no Runner.Worker (i.e. is not running a job).

    Delegates to runner_fleet.is_busy — the fleet's one busy signal (process
    presence).
    """
    return not runner_fleet.is_busy(runner_dir, workers)


# ── Service control (launchd on macOS, systemd --user on Linux) ───────────────


def _run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True, check=False).returncode


def service_active(runner_id):
    if IS_MAC:
        label = f"com.github.actions-runner.{runner_id}"
        return _run(["launchctl", "print", f"gui/{os.getuid()}/{label}"]) == 0
    return (
        _run(
            [
                "systemctl",
                "--user",
                "is-active",
                "--quiet",
                f"github-runner-{runner_id}.service",
            ]
        )
        == 0
    )


def pause_service(runner_id):
    """Stop the runner's service (offline, still registered). True on success."""
    if IS_MAC:
        label = f"com.github.actions-runner.{runner_id}"
        return _run(["launchctl", "bootout", f"gui/{os.getuid()}/{label}"]) == 0
    return (
        _run(["systemctl", "--user", "stop", f"github-runner-{runner_id}.service"]) == 0
    )


def resume_service(runner_id):
    """Restart a runner this watchdog paused. True on success."""
    if IS_MAC:
        plist = (
            Path.home()
            / "Library/LaunchAgents"
            / f"com.github.actions-runner.{runner_id}.plist"
        )
        if not plist.is_file():
            return False
        return _run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(plist)]) == 0
    return (
        _run(["systemctl", "--user", "start", f"github-runner-{runner_id}.service"])
        == 0
    )


# ── Logging ───────────────────────────────────────────────────────────────────


def log(msg):
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with LOG_FILE.open("a") as f:
        f.write(f"[{datetime.now(UTC):%Y-%m-%dT%H:%M:%SZ}] {msg}\n")


# ── Tick ──────────────────────────────────────────────────────────────────────


def tick(dry_run=False):
    if OPT_OUT_FILE.exists():
        return

    load = per_core_load()
    high_ticks, paused = load_state()
    runners = discover_runners()
    workers = runner_fleet.worker_cmdlines()
    idle = {
        rid
        for rid, d in runners.items()
        if service_active(rid) and runner_idle(d, workers)
    }

    plan = decide(load, high_ticks, paused, idle)

    if dry_run:
        print(
            f"per-core load={load:.2f} high_ticks={high_ticks} paused={sorted(paused)}"
        )
        print(f"idle={sorted(idle)}")
        print(
            f"plan: pause={plan.to_pause} resume={plan.to_resume} high_ticks→{plan.high_ticks}"
        )
        return

    done_pause, done_resume = [], []
    for rid in plan.to_pause:
        # Re-check idle (fresh ps) immediately before stopping, to shrink the
        # window where a job was assigned between the scan above and the stop.
        if rid in runners and runner_idle(runners[rid]) and pause_service(rid):
            done_pause.append(rid)
    for rid in plan.to_resume:
        if resume_service(rid):
            done_resume.append(rid)

    new_paused = (set(paused) | set(done_pause)) - set(done_resume)
    save_state(plan.high_ticks, new_paused)

    if done_pause or done_resume:
        log(
            f"load={load:.2f} high_ticks={plan.high_ticks} "
            f"paused={done_pause or '-'} resumed={done_resume or '-'} "
            f"now_paused={sorted(new_paused) or '-'}"
        )


# ── Timer install / uninstall ─────────────────────────────────────────────────


def install_timer():
    runner_timers.install_timer(
        label=WATCHDOG_LABEL,
        unit=WATCHDOG_UNIT,
        program=SCRIPT_PATH,
        args=["--tick"],
        interval=TICK_SECONDS,
        log=LOG_FILE,
        service_description=(
            "GitHub Actions runner load watchdog (pause runners under high load)"
        ),
        timer_description=f"Run the runner load watchdog every {TICK_SECONDS}s",
        path_env_mac="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
    )


def uninstall_timer():
    runner_timers.uninstall_timer(
        label=WATCHDOG_LABEL,
        unit=WATCHDOG_UNIT,
        message="uninstalled load-watchdog timer",
    )


def status():
    load = per_core_load()
    high_ticks, paused = load_state()
    runners = discover_runners()
    workers = runner_fleet.worker_cmdlines()
    idle = {
        rid
        for rid, d in runners.items()
        if service_active(rid) and runner_idle(d, workers)
    }
    plan = decide(load, high_ticks, paused, idle)
    print(f"host load (per-core): {load:.2f}   thresholds: pause>{HIGH} resume<{LOW}")
    print(f"runners: {sorted(runners)}")
    print(f"idle:    {sorted(idle)}")
    print(f"state:   high_ticks={high_ticks} paused={sorted(paused)}")
    print(f"opt-out: {OPT_OUT_FILE.exists()}")
    print(f"next tick would: pause={plan.to_pause} resume={plan.to_resume}")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--tick", action="store_true", help="evaluate once (default)")
    g.add_argument(
        "--status",
        action="store_true",
        help="print load/state and what a tick would do",
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
    else:  # --tick is the default
        tick(dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
