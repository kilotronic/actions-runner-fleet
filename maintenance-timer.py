#!/usr/bin/env python3
"""Install/uninstall the fleet-maintenance timer.

Runs update-host.sh every 2 hours so idle / asleep / sleep-deregistered hosts
self-heal even when no job (and thus no job-completed hook) ever lands. Shares
the launchd/systemd boilerplate with the load watchdog via runner_timers.

Usage:
    ./maintenance-timer.py --install-timer     # install + start (idempotent)
    ./maintenance-timer.py --uninstall-timer   # remove

Opt out of the convergence it triggers with ~/actions-runner/.no-auto-update
(disables all of update-host.sh) or ~/actions-runner/.no-auto-prune (removals only).
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import runner_timers

INTERVAL_SECONDS = 7200  # 2 hours
LABEL = "com.github.actions-runner.maintenance"  # macOS launchd label
UNIT = "github-runner-maintenance"  # Linux systemd unit stem

SCRIPT_DIR = Path(__file__).resolve().parent
UPDATE_HOST = SCRIPT_DIR / "update-host.sh"
LOG_FILE = Path.home() / "actions-runner" / "logs" / "update.log"

# update-host.sh needs git, python3, gh, docker on PATH under the timer's minimal
# env — and ~/.local/bin so a standalone-installed uv is visible to the uv
# freshness step (brew installs are covered by /opt/homebrew/bin already).
MAC_PATH = (
    f"{Path.home()}/.orbstack/bin:{Path.home()}/.local/bin:"
    "/opt/homebrew/bin:/opt/homebrew/sbin:"
    "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
)
LINUX_PATH = f"/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:{Path.home()}/.local/bin"


def install():
    runner_timers.install_timer(
        label=LABEL,
        unit=UNIT,
        program=UPDATE_HOST,
        args=[],
        interval=INTERVAL_SECONDS,
        log=LOG_FILE,
        service_description="GitHub runner fleet maintenance (converge to runners.toml every 2h)",
        timer_description=f"Run GitHub runner fleet maintenance every {INTERVAL_SECONDS}s",
        path_env_mac=MAC_PATH,
        path_env_linux=LINUX_PATH,
    )


def uninstall():
    runner_timers.uninstall_timer(label=LABEL, unit=UNIT)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument(
        "--install-timer", action="store_true", help="install + start the timer"
    )
    g.add_argument("--uninstall-timer", action="store_true", help="remove the timer")
    args = ap.parse_args()
    if args.install_timer:
        install()
    else:
        uninstall()
    return 0


if __name__ == "__main__":
    sys.exit(main())
