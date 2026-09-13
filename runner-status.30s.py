#!/usr/bin/env python3
# Copyright (c) 2024-2026 Kilotronic LLC. All rights reserved.

# <xbar.title>GitHub Actions Runner Status</xbar.title>
# <xbar.version>v1.1</xbar.version>
# <xbar.author>Jason Luther</xbar.author>
# <xbar.desc>Local self-hosted runner status with optional GH API enrichment</xbar.desc>
# <xbar.dependencies>python3</xbar.dependencies>
#
# Works with xbar (https://xbarapp.com) and SwiftBar.
# Install: symlink or copy to ~/Library/Application Support/xbar/plugins/
#
# Local checks run every 30s (the filename interval). GitHub API data is only
# fetched when you click "Refresh from GitHub" and cached for 5 minutes.
#
# Self-configuring: discovers all runners under ~/actions-runner/ by reading
# each <dir>/.runner file's gitHubUrl. No hardcoded repo list.

import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# Discovery + the busy/idle process signal are shared via runner_fleet. This
# plugin is installed as a *symlink* (install-menubar.sh), so resolve() finds
# the real repo path next to runner_fleet.py — same trick self_path uses below.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import runner_fleet

# ── Config ───────────────────────────────────────────────────────────────────

RUNNER_BASE = Path.home() / "actions-runner"
GH_CACHE_FILE = Path.home() / ".cache" / "runner-status-gh.json"
GH_CACHE_TTL = 300  # seconds

# ── Helpers ──────────────────────────────────────────────────────────────────


def run(cmd, timeout=5):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
        return r.stdout.strip(), r.returncode
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return "", 1


# ── Runner discovery ─────────────────────────────────────────────────────────


def discover_runners():
    """[(runner_dir, "owner/repo")] for every configured runner on this host,
    sorted by repo then dir. Delegates to runner_fleet; a dir whose .runner
    doesn't parse to a repo is skipped (unchanged: this plugin only shows
    registered, repo-attributable runners).
    """
    found = [(str(r.dir), r.repo) for r in runner_fleet.discover_runners() if r.repo]
    found.sort(key=lambda x: (x[1], x[0]))
    return found


def launchctl_running(runner_dir):
    basename = os.path.basename(runner_dir)
    label = f"com.github.actions-runner.{basename}"
    out, rc = run(["launchctl", "print", f"gui/{os.getuid()}/{label}"])
    if rc != 0:
        return False, None
    m = re.search(r"pid = (\d+)", out)
    return True, m.group(1) if m else "?"


def parse_runner_log(runner_dir):
    """Best-effort cosmetic detail from the latest Runner log: the last job's
    name/start time and, if idle, its result and the idle-since time.

    Display-only — busy/idle itself comes from runner_fleet.is_busy (process
    presence); log timestamps were one of the fleet's three incompatible busy
    signals and are no longer trusted for that.
    """
    diag = Path(runner_dir) / "_diag"
    logs = sorted(diag.glob("Runner_*.log"), key=lambda p: p.name)
    if not logs:
        return {}

    last_listening = None
    last_job_start = None
    last_job_name = None
    last_job_result = None

    for line in reversed(logs[-1].read_text(errors="replace").splitlines()):
        if last_listening is None and "Listening for Jobs" in line:
            m = re.search(r"\[([\d-]+ [\d:]+Z)", line)
            last_listening = m.group(1) if m else ""
        if last_job_start is None and "Running job:" in line:
            m = re.search(r"Running job: (.+)", line)
            last_job_name = m.group(1).strip() if m else "unknown"
            m2 = re.search(r"\[([\d-]+ [\d:]+Z)", line)
            last_job_start = m2.group(1) if m2 else ""
        if last_job_result is None and "finish job request" in line:
            m = re.search(r"result: (\w+)", line)
            last_job_result = m.group(1) if m else None
        if last_listening and last_job_start:
            break

    return {
        "job": last_job_name,
        "job_start": last_job_start,
        "idle_since": last_listening,
        "last_result": last_job_result,
    }


def workspace_size_mb(runner_dir):
    work = Path(runner_dir) / "_work"
    if not work.is_dir():
        return 0
    out, rc = run(["du", "-sm", str(work)])
    if rc == 0 and out:
        return int(out.split()[0])
    return 0


# ── Infrastructure ───────────────────────────────────────────────────────────


def postgres_status():
    """Check the containerized CI Postgres (port 5433)."""
    _out, rc = run(["pg_isready", "-h", "localhost", "-p", "5433", "-q"])
    return "running" if rc == 0 else "stopped"


# ── GH API (cached, on-demand) ───────────────────────────────────────────────


def gh_load_cache():
    if not GH_CACHE_FILE.exists():
        return {}
    try:
        return json.loads(GH_CACHE_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def gh_cached_for(repo, cache):
    entry = cache.get(repo)
    if not entry:
        return None
    if time.time() - entry.get("ts", 0) >= GH_CACHE_TTL:
        return None
    return entry


def gh_refresh(repos):
    GH_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    cache = gh_load_cache()
    for repo in repos:
        entry = {"ts": time.time(), "runners": [], "runs": []}

        runners_out, rc = run(
            [
                "gh",
                "api",
                f"repos/{repo}/actions/runners",
                "--jq",
                ".runners[] | {name, status, busy, labels: [.labels[].name]}",
            ],
            timeout=15,
        )
        if rc == 0 and runners_out:
            for line in runners_out.strip().splitlines():
                try:
                    entry["runners"].append(json.loads(line))
                except json.JSONDecodeError:
                    pass

        for status in ("in_progress", "queued"):
            runs_out, rc = run(
                [
                    "gh",
                    "run",
                    "list",
                    "--repo",
                    repo,
                    "--status",
                    status,
                    "--json",
                    "name,status,headBranch,url",
                    "--limit",
                    "5",
                ],
                timeout=15,
            )
            if rc == 0 and runs_out:
                try:
                    entry["runs"].extend(json.loads(runs_out))
                except json.JSONDecodeError:
                    pass

        cache[repo] = entry

    GH_CACHE_FILE.write_text(json.dumps(cache))
    return cache


# ── Formatting ───────────────────────────────────────────────────────────────


def time_ago(ts_str):
    try:
        dt = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
        secs = int((datetime.now(timezone.utc) - dt).total_seconds())
        if secs < 60:
            return f"{secs}s"
        if secs < 3600:
            return f"{secs // 60}m"
        if secs < 86400:
            return f"{secs // 3600}h"
        return f"{secs // 86400}d"
    except (ValueError, TypeError):
        return ts_str or ""


SYM_BUSY = "▶"  # ▶
SYM_IDLE = "○"  # ○
SYM_STOPPED = "■"  # ■
SYM_OK = "✓"  # ✓
SYM_FAIL = "✗"  # ✗
SYM_QUEUED = "⏳"  # ⏳
FONT = "font=Menlo size=12"
FONT_SM = "font=Menlo size=11"


# ── Output (xbar protocol) ───────────────────────────────────────────────────


def main():
    discovered = discover_runners()
    workers = runner_fleet.worker_cmdlines()

    # Group by repo, preserving discovery order (already sorted by repo, dir).
    by_repo = {}
    for rd, repo in discovered:
        by_repo.setdefault(repo, []).append(rd)

    # Collect (repo, runner_dir, running, pid, info) for everything. busy/idle
    # comes from runner_fleet.is_busy (process presence) — the fleet's one
    # busy signal; parse_runner_log only supplies cosmetic job/time detail.
    rows = []
    for repo, dirs in by_repo.items():
        for rd in dirs:
            running, pid = launchctl_running(rd)
            if not running:
                info = {"state": "stopped"}
            else:
                detail = parse_runner_log(rd)
                if runner_fleet.is_busy(Path(rd), workers):
                    info = {
                        "state": "busy",
                        "job": detail.get("job") or "?",
                        "since": detail.get("job_start") or "",
                    }
                else:
                    info = {
                        "state": "idle",
                        "since": detail.get("idle_since") or "",
                        "last_job": detail.get("job"),
                        "last_result": detail.get("last_result"),
                    }
            rows.append((repo, rd, running, pid, info))

    states = [r[4]["state"] for r in rows]
    busy = states.count("busy")
    idle = states.count("idle")
    stopped = len(states) - busy - idle

    # ── Menu bar title ───────────────────────────────────────────────────────
    if not rows:
        print("R:?")
    elif stopped == len(states):
        print(f"R:{SYM_STOPPED}")
    elif busy > 0:
        print(f"R:{busy}{SYM_BUSY}")
    else:
        print(f"R:{SYM_IDLE}")
    print("---")

    if not rows:
        print(f"No runners configured under {RUNNER_BASE} | color=gray {FONT}")

    # ── Per-repo runner details ──────────────────────────────────────────────
    cache = gh_load_cache()
    for repo in by_repo:
        repo_rows = [r for r in rows if r[0] == repo]
        repo_name = repo.split("/")[-1]
        print(f"Runners ({repo_name}) | size=14")

        for _, rd, running, pid, info in repo_rows:
            name = os.path.basename(rd)
            state = info.get("state", "unknown")

            if state == "busy":
                job = info.get("job", "?")
                ago = time_ago(info.get("since", ""))
                sym = SYM_BUSY
                detail = f"{job} ({ago})"
            elif state == "idle":
                ago = time_ago(info.get("since", ""))
                last = info.get("last_job")
                result = info.get("last_result", "")
                r_icon = (
                    f" {SYM_OK}"
                    if result == "Succeeded"
                    else (f" {SYM_FAIL}" if result else "")
                )
                sym = SYM_IDLE
                detail = f"idle {ago}"
                if last:
                    detail += f" — {last}{r_icon}"
            elif state == "stopped":
                sym = SYM_STOPPED
                detail = "stopped"
            else:
                sym = SYM_STOPPED
                detail = state

            size = workspace_size_mb(rd)
            size_str = f" [{size}M]" if size else ""

            print(f"{sym} {name}: {detail}{size_str} | {FONT}")
            if pid:
                print(f"--PID {pid} | {FONT_SM}")
            if size > 500:
                print(
                    f"--⚠ Workspace {size}MB — consider pruning | color=orange {FONT_SM}"
                )

        # Per-repo GH cache slice
        gh = gh_cached_for(repo, cache)
        if gh:
            age = int(time.time() - gh["ts"])
            age_str = f"{age // 60}m" if age >= 60 else f"{age}s"
            print(f"--GitHub (cached {age_str} ago) | {FONT_SM}")
            if gh.get("runs"):
                for r in gh["runs"]:
                    status = r.get("status", "")
                    nm = r.get("name", "?")
                    branch = r.get("headBranch", r.get("head_branch", ""))
                    url = r.get("url", r.get("html_url", ""))
                    icon = SYM_BUSY if status == "in_progress" else SYM_QUEUED
                    extra = f" href={url}" if url else ""
                    print(f"--{icon} {nm} ({branch}) |{extra} {FONT_SM}")
            else:
                print(f"--No active runs | color=gray {FONT_SM}")
            for r in gh.get("runners", []):
                status = r.get("status", "?")
                b = r.get("busy", False)
                nm = r.get("name", "?")
                icon = (
                    SYM_BUSY if b else (SYM_IDLE if status == "online" else SYM_STOPPED)
                )
                print(f"--{icon} {nm}: {status} busy={b} | {FONT_SM}")

        # Per-repo links
        print(f"--Open Actions | href=https://github.com/{repo}/actions {FONT_SM}")
        print(
            f"--Open Runner Settings | href=https://github.com/{repo}/settings/actions/runners {FONT_SM}"
        )
        print("---")

    # ── Infrastructure ───────────────────────────────────────────────────────
    pg = postgres_status()
    pg_color = "green" if pg == "running" else "red"
    print(f"Postgres: {pg} | color={pg_color} {FONT}")

    # ── Actions ──────────────────────────────────────────────────────────────
    print("---")
    self_path = os.path.realpath(__file__)
    print(
        f"Refresh from GitHub | refresh=true terminal=false bash={self_path} param1=--gh-refresh"
    )


if __name__ == "__main__":
    if "--gh-refresh" in sys.argv:
        repos = sorted({repo for _, repo in discover_runners()})
        gh_refresh(repos)
        sys.argv.remove("--gh-refresh")
    main()
