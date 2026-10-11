#!/usr/bin/env python3
"""Pause this host's idle runners while a laptop's lid is closed, or once it
has been on battery for too long.

GitHub will assign work to any connected listener. A lid-closed laptop can
still receive jobs (Power Nap keeps the long-poll alive) and then run them
throttled or asleep. So can one left unplugged: the job hooks' `caffeinate -s`
holds sleep off only on AC power, so on battery it sleeps mid-job. The one
lever a host controls is whether the listener is connected — a stopped service
goes *offline* and GitHub routes elsewhere (or queues the job if nothing is
free).

Either of two conditions holds a host offline:
  * the lid is closed. A machine is a laptop iff IOPM reports
    `AppleClamshellState`; desktops and minis lack the key.
  * it has been on battery for more than BATTERY_GRACE_SECONDS, per
    `pmset -g batt`. Desktops and minis list no InternalBattery.
Neither applies to a desktop, so this watchdog is a no-op there. Linux has
neither signal here; `--install-timer` is a no-op rather than an error.

Only idle runners are paused. A job already running is left alone. Runners
this watchdog paused are resumed once neither condition holds, except any the
load watchdog still has offline.

The battery clock is wall time, kept in the state file: it starts at the
first tick that reads battery and stops at the first that reads anything
else. The timer cannot tick while the Mac sleeps, but the time asleep still
counts, so the first tick after a wake acts on it. Nothing acts *before* that
tick: a listener still online when the Mac slept can take a job on a Power
Nap wake first.

A short launchd StartInterval drives it — mandatory, because a paused
runner accepts no jobs, so no job-completed hook ever fires to bring it
back. Install once with `--install-timer`.

Usage:
    ./lid-watchdog.py --tick            # one evaluation (default; what the timer runs)
    ./lid-watchdog.py --tick --dry-run  # evaluate and print, change nothing
    ./lid-watchdog.py --status          # show lid, power and what a tick would do
    ./lid-watchdog.py --install-timer   # install + start the per-host timer (idempotent)
    ./lid-watchdog.py --uninstall-timer # remove the timer

Opt out on a host: `touch ~/actions-runner/.no-lid-watchdog` turns the whole
watchdog off; `touch ~/actions-runner/.no-power-watchdog` only the battery
condition.
"""

import argparse
import importlib.util
import json
import platform
import re
import subprocess
import sys
import time
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
POWER_OPT_OUT_FILE = BASE_DIR / ".no-power-watchdog"
LOG_FILE = BASE_DIR / "logs" / "lid-watchdog.log"

WATCHDOG_LABEL = "com.github.actions-runner.lid-watchdog"
TICK_SECONDS = 15
# Long enough to ride out a brief unplug, short enough to stop a host that
# has been taken off power.
BATTERY_GRACE_SECONDS = 30 * 60

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
_POWER_SOURCE_RE = re.compile(r"Now drawing from '([^']+)'")


def parse_clamshell(text):
    """Lid state from `ioreg` text: True closed, False open, None not a laptop.

    A laptop is defined by the presence of AppleClamshellState, not by the
    hardware model string — Apple Silicon model ids are not `MacBook*`.
    """
    m = _CLAMSHELL_RE.search(text)
    if not m:
        return None
    return m.group(1) == "Yes"


def parse_power_source(text):
    """Power source from `pmset -g batt` text: True on battery, False on AC,
    None with no internal battery.

    A host has a battery iff pmset lists an InternalBattery. A desktop behind
    a UPS lists the UPS instead, and reports 'UPS Power' while it discharges;
    that is None too, since a desktop does not sleep on it.
    """
    if "-InternalBattery-" not in text:
        return None
    m = _POWER_SOURCE_RE.search(text)
    if not m:
        return None
    return m.group(1) == "Battery Power"


def battery_since(on_battery, since, now):
    """When this host went on battery (epoch seconds), or None if it is not on it.

    `since` is the value saved by the last tick. The first battery reading
    starts the clock and later ones keep its start. Any other reading stops
    it, so the next unplug starts the grace over.
    """
    if not on_battery:
        return None
    return now if since is None else since


def hold_reason(lid_closed, battery_for, *, grace=BATTERY_GRACE_SECONDS):
    """Why this host's runners should be offline: "lid", "battery", or None.

    battery_for is the seconds on battery, None when not on battery.
    """
    if lid_closed:
        return "lid"
    if battery_for is not None and battery_for > grace:
        return "battery"
    return None


def decide(
    lid_closed,
    paused,
    idle,
    *,
    load_paused=(),
    battery_for=None,
    grace=BATTERY_GRACE_SECONDS,
):
    """Decide what to pause/resume this tick. Pure: same inputs → same Plan.

    lid_closed   True closed, False open, None not a laptop (or ioreg failed)
    paused       runner ids this watchdog has already paused
    idle         runner ids currently idle (running a service, not mid-job)
    load_paused  runner ids the load watchdog still has offline — never resume
                 those here, or the two watchdogs fight
    battery_for  seconds this host has been on battery; None on AC, with no
                 battery, or with the battery condition opted out

    Either condition pauses. Resuming needs both released and the lid known
    to be open: an unreadable lid may be shut, so it resumes nothing.
    """
    paused, idle, load_paused = set(paused), set(idle), set(load_paused)

    if hold_reason(lid_closed, battery_for, grace=grace):
        return Plan(to_pause=sorted(idle - paused), to_resume=[])
    if lid_closed is None:
        return Plan(to_pause=[], to_resume=[])
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


def read_battery():
    """Live power source: True on battery, False on AC, None where the battery
    condition does not apply — not macOS, no battery, or opted out.

    A pmset failure is None too, which stops the battery clock: it fails
    toward keeping runners online, and the lid condition still applies.
    """
    if not IS_MAC or POWER_OPT_OUT_FILE.exists():
        return None
    try:
        r = subprocess.run(
            ["pmset", "-g", "batt"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None
    return parse_power_source(r.stdout)


Power = namedtuple("Power", ["on_battery", "since", "seconds"])


def read_power(saved_since, now):
    """A live power reading, advanced from the clock the last tick saved."""
    on_battery = read_battery()
    since = battery_since(on_battery, saved_since, now)
    return Power(on_battery, since, None if since is None else now - since)


def holds_offline():
    """True iff this watchdog wants the host's runners offline right now.

    The load watchdog asks this before it resumes, so it never brings back a
    runner that this watchdog would stop again on its next tick. It reads the
    saved battery clock without advancing it; only tick() saves it.
    """
    _, saved_since = load_state()
    power = read_power(saved_since, time.time())
    return hold_reason(read_clamshell(), power.seconds) is not None


def load_state():
    """(paused runner ids, on-battery-since epoch or None)."""
    try:
        data = json.loads(STATE_FILE.read_text())
        since = data.get("on_battery_since")
        return list(data.get("paused", [])), None if since is None else float(since)
    except (FileNotFoundError, json.JSONDecodeError, OSError, ValueError, TypeError):
        return [], None


def save_state(paused, on_battery_since):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    since = None if on_battery_since is None else round(on_battery_since)
    STATE_FILE.write_text(
        json.dumps({"paused": sorted(paused), "on_battery_since": since})
    )


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


def _minutes(seconds):
    return "-" if seconds is None else f"{int(seconds // 60)}m"


def tick(dry_run=False):
    if OPT_OUT_FILE.exists():
        return

    lid_closed = read_clamshell()
    paused, saved_since = load_state()
    power = read_power(saved_since, time.time())
    lw = _load_watchdog()
    runners = lw.discover_runners()
    workers = runner_fleet.worker_cmdlines()
    idle = {
        rid
        for rid, d in runners.items()
        if lw.service_active(rid) and lw.runner_idle(d, workers)
    }
    plan = decide(
        lid_closed,
        paused,
        idle,
        load_paused=load_watchdog_paused(),
        battery_for=power.seconds,
    )

    if dry_run:
        print(
            f"lid_closed={lid_closed} on_battery_for={_minutes(power.seconds)} "
            f"paused={sorted(paused)}"
        )
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
    save_state(new_paused, power.since)

    if done_pause or done_resume:
        log(
            f"lid_closed={lid_closed} on_battery_for={_minutes(power.seconds)} "
            f"hold={hold_reason(lid_closed, power.seconds) or '-'} "
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
            "GitHub Actions runner lid watchdog "
            "(pause runners when the lid is closed or on battery)"
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
    paused, saved_since = load_state()
    power = read_power(saved_since, time.time())
    lw = _load_watchdog()
    runners = lw.discover_runners()
    workers = runner_fleet.worker_cmdlines()
    idle = {
        rid
        for rid, d in runners.items()
        if lw.service_active(rid) and lw.runner_idle(d, workers)
    }
    plan = decide(
        lid_closed,
        paused,
        idle,
        load_paused=load_watchdog_paused(),
        battery_for=power.seconds,
    )
    grace = _minutes(BATTERY_GRACE_SECONDS)
    print(f"lid_closed: {lid_closed}   (None = not a laptop / not macOS)")
    print(
        f"on_battery: {power.on_battery}   (None = no battery / not macOS / opted out)"
    )
    if power.seconds is not None:
        print(
            f"battery:    {_minutes(power.seconds)} on battery; offline after {grace}"
        )
    print(f"runners: {sorted(runners)}")
    print(f"idle:    {sorted(idle)}")
    print(f"state:   paused={sorted(paused)} on_battery_since={saved_since}")
    print(
        f"opt-out: {OPT_OUT_FILE.exists()}   power opt-out: {POWER_OPT_OUT_FILE.exists()}"
    )
    print(f"holding: {hold_reason(lid_closed, power.seconds) or '-'}")
    print(f"next tick would: pause={plan.to_pause} resume={plan.to_resume}")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--tick", action="store_true", help="evaluate once (default)")
    g.add_argument(
        "--status",
        action="store_true",
        help="print lid/power/state and what a tick would do",
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
