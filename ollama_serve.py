#!/usr/bin/env python3
"""Converge this host's tailnet exposure of its local ollama server.

Hosts that declare `ollama_serve = true` in runners.toml publish their local
ollama (127.0.0.1:11434) to the tailnet as a raw TCP forwarder, so other fleet
boxes and CI jobs can reach it at `http://<tailnet-ip>:11434`. Hosts that don't
declare it get the forwarder torn down — convergence runs both directions.

Usage:
    ./ollama_serve.py                  # converge (quiet when already in sync)
    ./ollama_serve.py --dry-run        # print the plan, change nothing
    ./ollama_serve.py --status          # report what is configured and reachable
    ./ollama_serve.py --config PATH
    APPLY_HOST=other ./ollama_serve.py

Exit codes: 0 ok, 1 misconfig, 2 sub-command failed.

## Why a Tailscale proxy rather than rebinding ollama

The obvious alternative is to point ollama's own `OLLAMA_HOST` at the host's
tailscale0 address. It was rejected on three counts:

1. **It moves the listener instead of adding one.** `OLLAMA_HOST` takes a
   single bind address, so binding the tailnet address removes 127.0.0.1.
   Local clients then have to be told the new address. Serve keeps loopback
   and adds the tailnet path.
2. **It couples ollama's startup to tailscaled's.** On a laptop off the tailnet,
   or at boot before tailscaled is up, ollama cannot bind. Serve cannot fail
   that way: ollama never knows the tailnet exists.
3. **On macOS the config is Homebrew's to own.** `brew services` regenerates
   the plist and silently drops patched env. Serve state lives in tailscaled.

ollama still binds loopback only. The tailnet listener is tailscaled's, so
tailnet ACLs — not a local firewall — decide who may connect. ollama's API has
no authentication; narrowing the grant is an operator concern outside this kit.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import socket
import subprocess
import sys
import tomllib
from pathlib import Path

import fleet_config

IS_MAC = platform.system() == "Darwin"

PORT = 11434
LOCAL_TARGET = f"127.0.0.1:{PORT}"

MAC_TAILSCALE = "/Applications/Tailscale.app/Contents/MacOS/Tailscale"


def parse_tcp_forward(status_json: str, port: int = PORT) -> str | None:
    """The forward target configured for `port`, or None when there is none."""
    text = (status_json or "").strip()
    if not text:
        return None
    try:
        cfg = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(cfg, dict):
        return None
    entry = (cfg.get("TCP") or {}).get(str(port))
    if not isinstance(entry, dict):
        return None
    target = entry.get("TCPForward")
    return target if isinstance(target, str) else None


def plan(*, current_target: str | None, want_serve: bool) -> str:
    """One of "enable", "disable", "in-sync"."""
    if want_serve:
        return "in-sync" if current_target == LOCAL_TARGET else "enable"
    return "disable" if current_target is not None else "in-sync"


def load_want_serve(cfg: dict, host: str) -> bool:
    """Read `ollama_serve` for `host` out of an already-parsed runners.toml.

    Absent means false: publishing an unauthenticated inference endpoint is
    opt-in. A host not in the inventory is not an error here.
    """
    entry = (cfg.get("hosts") or {}).get(host)
    if not isinstance(entry, dict):
        return False
    value = entry.get("ollama_serve", False)
    if not isinstance(value, bool):
        sys.exit(
            f"error: ollama_serve for host '{host}' must be true or false, "
            f"got {value!r}"
        )
    return value


def short_hostname() -> str:
    return os.environ.get("APPLY_HOST") or socket.gethostname().split(".")[0]


def tailscale_cmd() -> list[str] | None:
    """The argv prefix that can CHANGE serve config, or None when unavailable."""
    if IS_MAC:
        return [MAC_TAILSCALE] if Path(MAC_TAILSCALE).is_file() else None
    binary = shutil.which("tailscale")
    if not binary:
        return None
    if os.geteuid() == 0:
        return [binary]
    return ["sudo", "-n", binary]


def read_cmd() -> list[str] | None:
    """Like tailscale_cmd(), but for reads — which never need elevation."""
    cmd = tailscale_cmd()
    return cmd[2:] if cmd and cmd[0] == "sudo" else cmd


def _capture(cmd: list[str]) -> tuple[int, str]:
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    return proc.returncode, proc.stdout


def current_target(read: list[str]) -> str | None:
    rc, out = _capture([*read, "serve", "status", "--json"])
    if rc != 0:
        return None
    return parse_tcp_forward(out)


def apply_plan(action: str, cmd: list[str]) -> int:
    if action == "enable":
        args = [*cmd, "serve", "--bg", "--tcp", str(PORT), f"tcp://{LOCAL_TARGET}"]
    elif action == "disable":
        args = [*cmd, "serve", f"--tcp={PORT}", "off"]
    else:
        return 0
    rc, _ = _capture(args)
    return rc


def probe(url_host: str) -> str:
    """One-line reachability report for `--status`. Never raises."""
    import urllib.error
    import urllib.request

    url = f"http://{url_host}:{PORT}/api/version"
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            return json.loads(resp.read()).get("version", "?")
    except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError) as exc:
        return f"unreachable ({exc})"


def tailnet_ipv4(read: list[str]) -> str | None:
    rc, out = _capture([*read, "ip", "-4"])
    return out.strip().splitlines()[0] if rc == 0 and out.strip() else None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--dry-run", action="store_true", help="print the plan, change nothing"
    )
    parser.add_argument(
        "--status", action="store_true", help="report configured + reachable state"
    )
    parser.add_argument("--config", help="path to runners.toml")
    args = parser.parse_args()

    host = short_hostname()
    config_file = fleet_config.resolve_config_path(explicit=args.config)
    if not config_file.is_file():
        sys.exit(f"error: {config_file} not found")
    with config_file.open("rb") as f:
        cfg = tomllib.load(f)
    want = load_want_serve(cfg, host)

    read = read_cmd()
    if read is None:
        if want:
            print(f"ollama_serve: {host} wants tailnet serve but tailscale is absent")
            return 2
        return 0

    target = current_target(read)

    if args.status:
        ip = tailnet_ipv4(read)
        print(f"ollama serve ({host}): want={want} forward={target or 'none'}")
        print(f"  local     : {LOCAL_TARGET} -> {probe('127.0.0.1')}")
        if ip:
            print(f"  tailnet   : {ip}:{PORT} -> {probe(ip)}")
        return 0

    action = plan(current_target=target, want_serve=want)
    if action == "in-sync":
        return 0

    if args.dry_run:
        print(f"ollama_serve: would {action} tcp/{PORT} -> {LOCAL_TARGET} on {host}")
        return 0

    cmd = tailscale_cmd()
    if cmd is None:
        print(f"ollama_serve: cannot {action} — no usable tailscale CLI")
        return 2
    rc = apply_plan(action, cmd)
    if rc != 0:
        print(
            f"ollama_serve: {action} failed (rc={rc}); is `sudo -n tailscale` allowed?"
        )
        return 2
    print(f"ollama_serve: {action}d tcp/{PORT} -> {LOCAL_TARGET} on {host}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
