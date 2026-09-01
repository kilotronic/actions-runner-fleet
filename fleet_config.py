#!/usr/bin/env python3
"""Resolve and parse the fleet config (runners.toml).

Path order:
  1. explicit argument / --config
  2. $ACTIONS_RUNNER_CONFIG
  3. $XDG_CONFIG_HOME/actions-runner/runners.toml
  4. ~/.config/actions-runner/runners.toml
"""

from __future__ import annotations

import os
import re
import socket
import tomllib
from collections import namedtuple
from pathlib import Path

HostConfig = namedtuple(
    "HostConfig",
    [
        "counts",
        "ci_slots",
        "e2e_workers",
        "work_root",
        "labels",
        "container_runtime",
        "slot_gated_repos",
    ],
)

CONTAINER_RUNTIMES = frozenset({"orbstack"})

# Host-table keys that are not owner/repo counts. ollama_serve is owned by
# ollama_serve.py, not by apply.py, so it is popped here.
_HOST_RESERVED = (
    "ci_slots",
    "e2e_workers",
    "work_root",
    "labels",
    "container_runtime",
    "ollama_serve",
)


def resolve_config_path(*, explicit=None, env=None, home=None) -> Path:
    if explicit:
        return Path(explicit)
    environ = env if env is not None else os.environ
    if environ.get("ACTIONS_RUNNER_CONFIG"):
        return Path(environ["ACTIONS_RUNNER_CONFIG"])
    xdg = environ.get("XDG_CONFIG_HOME")
    if xdg:
        return Path(xdg) / "actions-runner" / "runners.toml"
    base = Path(home) if home is not None else Path.home()
    return base / ".config" / "actions-runner" / "runners.toml"


def _default_ci_slots() -> int:
    cores = os.cpu_count() or 8
    return max(1, (cores - 4) // 6)


def load_host(config_path: Path, host: str) -> HostConfig:
    if not Path(config_path).is_file():
        raise SystemExit(f"error: {config_path} not found")
    with Path(config_path).open("rb") as f:
        cfg = tomllib.load(f)

    fleet = cfg.get("fleet") or {}
    gated = fleet.get("slot_gated_repos", [])
    if gated is None:
        gated = []
    if not isinstance(gated, list) or not all(isinstance(x, str) for x in gated):
        raise SystemExit(
            "error: fleet.slot_gated_repos must be a list of repo names "
            f"(the trailing path component), got {gated!r}"
        )
    slot_gated_repos = tuple(gated)

    hosts = cfg.get("hosts", {})
    if host not in hosts:
        known = ", ".join(sorted(hosts)) or "(none)"
        raise SystemExit(
            f"error: no entry for host '{host}' in {Path(config_path).name}.\n"
            f"  known hosts: {known}\n"
            f"  override with APPLY_HOST=<name> if needed"
        )
    entry = dict(hosts[host])

    ci_slots = entry.pop("ci_slots", None)
    if ci_slots is not None and (not isinstance(ci_slots, int) or ci_slots < 1):
        raise SystemExit(
            f"error: ci_slots for host '{host}' must be a positive integer, "
            f"got {ci_slots!r}"
        )
    if ci_slots is None:
        ci_slots = _default_ci_slots()

    e2e_workers = entry.pop("e2e_workers", None)
    if e2e_workers is not None and (
        not isinstance(e2e_workers, int) or e2e_workers < 1
    ):
        raise SystemExit(
            f"error: e2e_workers for host '{host}' must be a positive integer, "
            f"got {e2e_workers!r}"
        )

    work_root = entry.pop("work_root", None)
    if work_root is not None and (
        not isinstance(work_root, str) or not work_root.startswith("/")
    ):
        raise SystemExit(
            f"error: work_root for host '{host}' must be an absolute path string, "
            f"got {work_root!r}"
        )

    labels = entry.pop("labels", None)
    if labels is not None and (
        not isinstance(labels, list)
        or not all(isinstance(x, str) and re.match(r"^[\w.-]+$", x) for x in labels)
    ):
        raise SystemExit(
            f"error: labels for host '{host}' must be a list of simple label "
            f"strings, got {labels!r}"
        )
    extra_labels = tuple(labels or ())

    container_runtime = entry.pop("container_runtime", None)
    if container_runtime is not None:
        if (
            not isinstance(container_runtime, str)
            or container_runtime not in CONTAINER_RUNTIMES
        ):
            allowed = ", ".join(sorted(CONTAINER_RUNTIMES))
            raise SystemExit(
                f"error: container_runtime for host '{host}' must be one of "
                f"{allowed}, got {container_runtime!r}"
            )

    entry.pop("ollama_serve", None)

    out: dict[str, int] = {}
    for repo, count in entry.items():
        if not isinstance(count, int) or count < 0:
            raise SystemExit(
                f"error: {repo} count must be a non-negative integer, got {count!r}"
            )
        if not re.match(r"^[^/]+/[^/]+$", repo):
            raise SystemExit(f"error: repo '{repo}' must be in owner/repo form")
        out[repo] = count

    return HostConfig(
        counts=out,
        ci_slots=ci_slots,
        e2e_workers=e2e_workers,
        work_root=work_root,
        labels=extra_labels,
        container_runtime=container_runtime,
        slot_gated_repos=slot_gated_repos,
    )


def this_host() -> str:
    return os.environ.get("APPLY_HOST") or socket.gethostname().split(".")[0]


def container_runtime_for_this_host(*, host=None, config_path=None) -> str:
    """Return this host's container_runtime, or '' if unset / host unknown."""
    path = Path(config_path) if config_path else resolve_config_path()
    try:
        cfg = load_host(path, host or this_host())
    except SystemExit:
        return ""
    return cfg.container_runtime or ""


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="Inspect the fleet config for this host.")
    ap.add_argument("--config", help="path to runners.toml")
    ap.add_argument("--host", help="host key (default: hostname -s / APPLY_HOST)")
    ap.add_argument(
        "--container-runtime",
        action="store_true",
        help="print this host's container_runtime (empty if unset)",
    )
    args = ap.parse_args()
    if args.config:
        os.environ["ACTIONS_RUNNER_CONFIG"] = args.config
    if args.container_runtime:
        print(container_runtime_for_this_host(host=args.host, config_path=args.config))
    else:
        ap.error("specify --container-runtime")
