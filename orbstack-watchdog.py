#!/usr/bin/env python3
"""Keep the container runtime (OrbStack) healthy on macOS hosts, unattended.

The fleet's Macs sleep between jobs, and OrbStack's host<->VM transport does not
reliably survive a sleep transition (orbstack/orbstack#1933; docker/for-mac#1046
and #6546 are the same class — this is structural to every macOS container
runtime, not an OrbStack bug). The daemon comes back WEDGED: socket present,
`orb status` says Running, `docker info` hangs forever. Until this timer, the
only thing that noticed was hooks/job-started.sh — i.e. a job had already landed
and was paying the recovery latency, or failing outright.

Why a plain StartInterval timer is the right wake hook: a launchd agent whose
interval elapsed during sleep fires immediately on wake. That gets wake coverage
for free, with no sleepwatcher dependency and no new plist shape, and it also
covers reboot, crash, and a mid-idle wedge that no wake event would announce.

macOS only. The Linux hosts run Docker Engine from Docker's apt repo and are
unaffected, so --install-timer is a no-op there rather than an error.

Usage:
    ./orbstack-watchdog.py --tick            # one check (what the timer runs)
    ./orbstack-watchdog.py --install-timer   # install + start the timer (idempotent)
    ./orbstack-watchdog.py --uninstall-timer # remove the timer

Opt out on a host: `touch ~/actions-runner/.no-orbstack-watchdog`.
"""

import argparse
import platform
import subprocess
import sys

# timezone.utc, not datetime.UTC: update-host.sh invokes this with a bare
# `python3`, which in hook and plain-SSH contexts on the Macs is system Python
# 3.9 (/usr/bin/python3) — where `from datetime import UTC` is an ImportError.
# The install is wrapped in `>/dev/null 2>&1`, so that failure is silent, which
# is precisely how it would go unnoticed. Keep this module 3.9-clean for the
# same reason runner_fleet.py is (see its module docstring).
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import runner_timers

# 300s, not the load watchdog's 60s. The check is cheap when healthy (~40ms),
# but each tick on a DarkWake box is a small nudge against the fleet's
# deliberate sleep-between-jobs policy (see fleet.md), and wake coverage does
# not improve with a shorter interval — launchd fires the missed tick on wake
# regardless of whether that interval was 60s or 300s.
INTERVAL_SECONDS = 300
LABEL = "com.github.actions-runner.orbstack-watchdog"  # macOS launchd label

IS_MAC = platform.system() == "Darwin"
SCRIPT_DIR = Path(__file__).resolve().parent
ENSURE = SCRIPT_DIR / "hooks" / "ensure-orbstack.sh"
BASE_DIR = Path.home() / "actions-runner"
OPT_OUT_FILE = BASE_DIR / ".no-orbstack-watchdog"
LOG_FILE = BASE_DIR / "logs" / "orbstack-watchdog.log"

# ensure-orbstack.sh needs docker/orb (~/.orbstack/bin) and python3 (the busy
# gate shells out to runner_fleet.py) under launchd's minimal environment.
MAC_PATH = (
    f"{Path.home()}/.orbstack/bin:/opt/homebrew/bin:/opt/homebrew/sbin:"
    "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
)

# The one line a healthy tick prints. Matching it lets a quiet tick append
# nothing to the log, the same convention update-host.sh uses for a fully
# in-sync apply.py run — otherwise a healthy host writes 288 lines a day and
# the interesting events drown.
HEALTHY_LINE = "container runtime is running"


def tick():
    """Run one ensure-orbstack.sh --watchdog pass; log only if something happened."""
    if OPT_OUT_FILE.exists():
        return 0
    if not ENSURE.is_file():
        print(f"error: {ENSURE} not found", file=sys.stderr)
        return 1

    proc = subprocess.run([str(ENSURE), "--watchdog"], capture_output=True, text=True, check=False)
    out = (proc.stdout or "") + (proc.stderr or "")
    lines = [ln for ln in out.splitlines() if ln.strip()]

    # Silent on the healthy path; loud about everything else, including a
    # stand-down, so a decision not to act is as diagnosable as a restart.
    if proc.returncode == 0 and lines == [HEALTHY_LINE]:
        return 0

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    for line in lines:
        print(f"[{stamp}] {line}")
    if proc.returncode != 0:
        print(f"[{stamp}] ensure-orbstack.sh exited {proc.returncode}")
    return 0


def install():
    if not IS_MAC:
        print("orbstack-watchdog: macOS-only (Linux uses Docker Engine); skipping")
        return
    runner_timers.install_timer(
        label=LABEL,
        unit=None,  # macOS-only above; the systemd branch never runs from here
        program=Path(__file__).resolve(),
        args=["--tick"],
        interval=INTERVAL_SECONDS,
        log=LOG_FILE,
        service_description="OrbStack health watchdog (recover a wedged/stopped daemon)",
        timer_description=f"Check OrbStack health every {INTERVAL_SECONDS}s",
        path_env_mac=MAC_PATH,
    )


def uninstall():
    if not IS_MAC:
        print("orbstack-watchdog: macOS-only; nothing to uninstall")
        return
    runner_timers.uninstall_timer(label=LABEL, unit=None)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    g = ap.add_mutually_exclusive_group()
    g.add_argument(
        "--tick", action="store_true", help="run one check (timer entry point)"
    )
    g.add_argument(
        "--install-timer", action="store_true", help="install + start the timer"
    )
    g.add_argument("--uninstall-timer", action="store_true", help="remove the timer")
    args = ap.parse_args()

    if args.install_timer:
        install()
        return 0
    if args.uninstall_timer:
        uninstall()
        return 0
    return tick()  # default: --tick


if __name__ == "__main__":
    sys.exit(main())
