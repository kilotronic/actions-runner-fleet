#!/usr/bin/env python3
"""Shared per-user periodic-timer install/uninstall for the runner fleet.

Two timers use this: the load watchdog (every 60s) and fleet maintenance
(every 2h). macOS uses a launchd StartInterval agent; Linux a systemd --user
oneshot service + timer. The render_* functions are pure (unit-tested in
test_runner_timers.py); install_timer / uninstall_timer do the I/O.
"""

import os
import platform
import subprocess
import sys
from pathlib import Path

IS_MAC = platform.system() == "Darwin"


def render_launchd_plist(*, label, program, args, interval, log, path_env):
    prog_lines = "\n".join(
        f"        <string>{a}</string>" for a in [str(program), *args]
    )
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{label}</string>
    <key>ProgramArguments</key>
    <array>
{prog_lines}
    </array>
    <key>StartInterval</key>
    <integer>{interval}</integer>
    <key>RunAtLoad</key>
    <true/>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>{path_env}</string>
    </dict>
    <key>StandardOutPath</key>
    <string>{log}</string>
    <key>StandardErrorPath</key>
    <string>{log}</string>
</dict>
</plist>
"""


def render_systemd_service(*, description, program, args, path_env=None):
    execstart = " ".join([str(program), *args])
    body = (
        f"[Unit]\n"
        f"Description={description}\n"
        f"\n"
        f"[Service]\n"
        f"Type=oneshot\n"
        f"ExecStart={execstart}\n"
    )
    if path_env:
        body += f"Environment=PATH={path_env}\n"
    return body


def render_systemd_timer(*, description, interval):
    return f"""[Unit]
Description={description}

[Timer]
OnBootSec={interval}s
OnUnitActiveSec={interval}s
AccuracySec=10s

[Install]
WantedBy=timers.target
"""


def _run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True, check=False).returncode


def timer_needs_install(*, existing_text, desired_text, loaded):
    """True when the unit must be (re)written and (re)loaded.

    Two independent reasons: the rendered unit differs from what is on disk (a
    commit changed the interval, program path, or PATH), or the unit is not
    actually loaded (never installed here, or booted out by hand).

    Pure, so the decision is unit-tested without touching launchd/systemd.
    """
    return existing_text != desired_text or not loaded


def _read(path):
    """File contents, or None when absent — the 'never installed' signal."""
    try:
        return Path(path).read_text()
    except OSError:
        return None


def _launchd_loaded(label):
    return _run(["launchctl", "print", f"gui/{os.getuid()}/{label}"]) == 0


def _systemd_loaded(unit):
    return _run(["systemctl", "--user", "is-enabled", f"{unit}.timer"]) == 0


def install_timer(
    *,
    label,
    unit,
    program,
    args,
    interval,
    log,
    service_description,
    timer_description,
    path_env_mac,
    path_env_linux=None,
):
    """Install + start an idempotent per-user timer running `program args...`.

    macOS: launchd StartInterval agent labeled `label`.
    Linux: systemd --user oneshot `unit`.service + `unit`.timer.

    Cheap and safe to call on EVERY convergence tick: when the rendered unit
    already matches what is installed AND the unit is loaded, this returns
    without touching launchd/systemd. That self-check is what lets
    update-host.sh call it unconditionally instead of gating on "the checkout
    advanced" — a gate that could never adopt a NEWLY ADDED timer, because the
    pass that pulls the introducing commit still runs the pre-pull script body,
    and the next pass has the new body but no longer sees the checkout move.
    The OrbStack watchdog shipped and then failed to install on all three Macs
    for exactly that reason.

    Skipping when current also preserves what the old gate was protecting: the
    bootout/bootstrap cycle (whose RunAtLoad re-fires the timer's own program,
    i.e. update-host.sh) only happens on a real change, not every tick.
    """
    Path(log).parent.mkdir(parents=True, exist_ok=True)
    if IS_MAC:
        plist = Path.home() / "Library/LaunchAgents" / f"{label}.plist"
        desired = render_launchd_plist(
            label=label,
            program=program,
            args=args,
            interval=interval,
            log=log,
            path_env=path_env_mac,
        )
        if not timer_needs_install(
            existing_text=_read(plist),
            desired_text=desired,
            loaded=_launchd_loaded(label),
        ):
            print(f"launchd timer already current: {label}")
            return
        plist.parent.mkdir(parents=True, exist_ok=True)
        plist.write_text(desired)
        uid = os.getuid()
        _run(["launchctl", "bootout", f"gui/{uid}/{label}"])  # ignore if absent
        if _run(["launchctl", "bootstrap", f"gui/{uid}", str(plist)]) != 0:
            sys.exit(f"error: failed to bootstrap launchd agent {label}")
        print(f"installed launchd timer: {plist} (every {interval}s)")
    else:
        unit_dir = Path.home() / ".config/systemd/user"
        service_text = render_systemd_service(
            description=service_description,
            program=program,
            args=args,
            path_env=path_env_linux,
        )
        timer_text = render_systemd_timer(
            description=timer_description, interval=interval
        )
        # Both unit files are one logical install, so compare them as one blob:
        # a drift in either has to trigger the same rewrite + reload.
        existing = _read(unit_dir / f"{unit}.service")
        existing_timer = _read(unit_dir / f"{unit}.timer")
        if not timer_needs_install(
            existing_text=None
            if existing is None or existing_timer is None
            else existing + existing_timer,
            desired_text=service_text + timer_text,
            loaded=_systemd_loaded(unit),
        ):
            print(f"systemd --user timer already current: {unit}.timer")
            return
        unit_dir.mkdir(parents=True, exist_ok=True)
        (unit_dir / f"{unit}.service").write_text(service_text)
        (unit_dir / f"{unit}.timer").write_text(timer_text)
        _run(["systemctl", "--user", "daemon-reload"])
        if _run(["systemctl", "--user", "enable", "--now", f"{unit}.timer"]) != 0:
            sys.exit(f"error: failed to enable systemd timer {unit}")
        print(f"installed systemd --user timer: {unit}.timer (every {interval}s)")


def uninstall_timer(*, label, unit, message=None):
    # message overrides the default final print line, so callers can preserve their original wording.
    if IS_MAC:
        _run(["launchctl", "bootout", f"gui/{os.getuid()}/{label}"])
        (Path.home() / "Library/LaunchAgents" / f"{label}.plist").unlink(
            missing_ok=True
        )
    else:
        _run(["systemctl", "--user", "disable", "--now", f"{unit}.timer"])
        unit_dir = Path.home() / ".config/systemd/user"
        (unit_dir / f"{unit}.timer").unlink(missing_ok=True)
        (unit_dir / f"{unit}.service").unlink(missing_ok=True)
        _run(["systemctl", "--user", "daemon-reload"])
    print(message or f"uninstalled timer {label or unit}")
