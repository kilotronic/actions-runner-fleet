#!/usr/bin/env python3
"""Converge this host's self-hosted runners to match runners.toml.

Reads the fleet config (see fleet_config.py), finds the entry for this host
(`hostname -s` by default, or $APPLY_HOST), compares to live state under
~/actions-runner/, and shells out to install.sh / uninstall.sh to add or remove
runners.

Usage:
    ./apply.py                  # converge, then restart hung listeners
                                #   (running here but offline on GitHub;
                                #   honors .no-runner-health)
    ./apply.py --dry-run        # print the plan, change nothing
    ./apply.py --config PATH    # override config path
    APPLY_HOST=other ./apply.py # use a different host key

Exit codes: 0 ok, 1 misconfig, 2 sub-command failed.
"""

import argparse
import json
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import time
from collections import Counter, namedtuple
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
import fleet_config  # noqa: E402
import runner_fleet  # noqa: E402

RUNNER_BASE = Path.home() / "actions-runner"
IS_MAC = platform.system() == "Darwin"
CONFIG_FILE = fleet_config.resolve_config_path()

MAX_REMOVE = 2  # blast-radius cap: refuse a decide() that would remove more than this


def clamp_to_slots(
    repo_name: str, desired: int, ci_slots: int, gated=()
) -> tuple[int, bool]:
    """Cap slot-gated repos' target at ci_slots. Returns (target, clamped).

    `gated` is the trailing repo names from fleet.slot_gated_repos. Downward
    only: a target already <= ci_slots (and every ungated repo) passes through.
    """
    if repo_name in gated and desired > ci_slots:
        return ci_slots, True
    return desired, False


def upsert_env(text: str, updates: dict) -> str | None:
    """Upsert KEY=VALUE lines in .env text. Pure.

    A None value means the key is unmanaged: an existing line is left exactly
    as-is and no line is added (lets a host omit e2e_workers in runners.toml
    while hand-set experiments survive). Returns the new text, or None when
    nothing would change.
    """
    lines = text.splitlines()
    seen = set()
    changed = False
    for i, line in enumerate(lines):
        m = re.match(r"([A-Za-z_][A-Za-z0-9_]*)=", line)
        if not m or m.group(1) not in updates:
            continue
        key = m.group(1)
        seen.add(key)
        val = updates[key]
        if val is not None and line != f"{key}={val}":
            lines[i] = f"{key}={val}"
            changed = True
    for key, val in updates.items():
        if val is not None and key not in seen:
            lines.append(f"{key}={val}")
            changed = True
    if not changed:
        return None
    return "\n".join(lines) + "\n"



def runner_env_updates(
    dirname: str, ci_slots: int, e2e_workers: int | None, gated: bool
) -> dict:
    """The .env keys apply.py converges into one runner. Pure.

    TMPDIR is converged for EVERY repo, not only the gated one: every job
    writes temp, and a host whose /tmp is a tmpfs has it exhausted by whatever
    leaks there. The failures that follow do not mention temp — a package
    manager cannot write its signature splits and dies behind a wall of GPG
    errors, a browser install reports success with nothing runnable in it, a
    browser dies mid-test — so this is cheap insurance against an expensive
    misdiagnosis. A dir on the runner's own disk also keeps job temp off a
    RAM-backed tmpfs, where it competes with the jobs for memory.

    Per runner, not one shared dir: two runners on a host can hold jobs at the
    same time, and the job-completed hook that empties this one must not be
    able to delete another job's in-flight temp.
    """
    updates: dict[str, str | None] = {"TMPDIR": str(RUNNER_BASE / dirname / "_tmp")}
    if gated:
        updates["CI_SLOTS"] = str(ci_slots)
        updates["E2E_WORKERS_OVERRIDE"] = (
            str(e2e_workers) if e2e_workers is not None else None
        )
    return updates

# ── Pure convergence core (no I/O — unit-tested in test_apply.py) ─────────────

Plan = namedtuple(
    "Plan", ["to_reregister", "to_install", "to_remove", "to_cleanup", "capped"]
)


def _dir_index(dirname: str) -> int:
    """Numeric suffix of a runner dir basename (app-3 → 3); 0 if none."""
    m = re.search(r"-(\d+)$", dirname)
    return int(m.group(1)) if m else 0


def decide(
    desired,
    local_dirs,
    gh_runners,
    *,
    host,
    busy_dirs=frozenset(),
    max_remove=MAX_REMOVE,
    allow_remove=True,
):
    """Plan convergence to `desired` healthy *registered* runners. Pure.

    desired      D from runners.toml for this repo.
    local_dirs   this repo's local runner dir basenames (e.g. ["app-1"]).
    gh_runners   [{"name","status"}] filtered to this host+repo, or None if the
                 gh query failed (then every action is un-tokenable → skip).
                 Registration-liveness only — busy/idle is decided locally.
    host         short hostname; a local dir `d` registers as f"{host}-{d}".
    busy_dirs    local dir basenames with a live Runner.Worker process
                 (runner_fleet.is_busy) — the idle-removal guard, decided from
                 this host's own process table, independent of GitHub's status.

    Classify each local dir: healthy (a matching GitHub registration exists) or
    dead (none — the sleep-deregistration case). Note "registration exists" is
    independent of online/offline: a watchdog-paused runner is offline but
    registered, hence healthy, and is never re-registered.

    Returns a Plan. Removals are busy-filtered (never touch a running job) and
    capped at max_remove (per call ≈ per run on single-repo hosts).
    """
    if gh_runners is None:
        return Plan([], 0, [], [], False)

    reg_names = {r["name"] for r in gh_runners}

    def gh_name(d):
        return f"{host}-{d}"

    healthy = sorted((d for d in local_dirs if gh_name(d) in reg_names), key=_dir_index)
    dead = sorted(
        (d for d in local_dirs if gh_name(d) not in reg_names), key=_dir_index
    )

    to_reregister = []
    to_install = 0
    to_remove = []
    to_cleanup = []

    if len(healthy) < desired:
        need = desired - len(healthy)
        to_reregister = dead[:need]
        to_install = need - len(to_reregister)
        to_cleanup = dead[need:]  # surplus dead beyond what we need to reach D
    else:
        to_cleanup = list(dead)  # healthy already meets D → every dead dir is cruft
        if len(healthy) > desired:
            excess = len(healthy) - desired
            idle_healthy = sorted(
                (d for d in healthy if d not in busy_dirs),
                key=_dir_index,
                reverse=True,  # drop the highest indices first, keep -1, -2 stable
            )
            to_remove = idle_healthy[:excess]

    capped = False
    if not allow_remove:
        # .no-auto-prune: suppress all destructive removals (adds/reregister stay).
        to_remove = []
        to_cleanup = []
    elif len(to_remove) > max_remove:
        capped = True
        to_remove = []

    return Plan(to_reregister, to_install, to_remove, to_cleanup, capped)


# ── Hung-listener detection (pure; unit-tested in test_apply.py) ─────────────

# A listener that HANGS rather than exits is invisible to every restart
# mechanism this fleet has. The launchd plist's KeepAlive/SuccessfulExit and the
# systemd unit's Restart= both key off process *exit*; a run.sh whose
# Runner.Listener is wedged never exits, so neither ever fires. This is not
# hypothetical: a runner took a job, lost its worker, and then sat with that job
# still assigned for TEN DAYS — process alive, PID present in `launchctl list`,
# and `offline` on GitHub the entire time. Nothing noticed, and the host quietly
# ran at half its configured capacity for a week and a half.
#
# The signal is exactly that mismatch: GitHub reports the runner offline while
# its service is running here and no Runner.Worker exists. Listener log mtime
# was evaluated and rejected as the signal — a listener rotates to a fresh
# _diag/Runner_*.log on every restart (and apply.py restarts runners routinely),
# so "stale log" tracks restarts rather than health, and the wedged listener
# above still logged sporadically across its ten days.

HUNG_GRACE_SECONDS = (
    7200  # must stay hung this long to be acted on (= 1 maintenance tick)
)
HEALTH_STATE_FILE = RUNNER_BASE / "runner-health.state"
NO_HEALTH_FILE = RUNNER_BASE / ".no-runner-health"


def hung_dirs(
    gh_runners,
    local_dirs,
    *,
    host,
    service_running,
    busy_dirs=frozenset(),
    paused_dirs=frozenset(),
):
    """Local dir basenames whose listener looks hung. Pure given its inputs.

    A dir qualifies only if ALL of these hold:
      * GitHub has a registration for it with status "offline" — an
        *unregistered* dir is decide()'s dead/re-register case, not this one;
      * it is not in `busy_dirs` (no Runner.Worker) — a busy runner is working,
        and an offline status against a live worker is a GitHub-side blip;
      * it is not in `paused_dirs` — the load and lid watchdogs stop services
        on purpose, and offline is the intended result, not a fault;
      * `service_running(dir)` is True — the whole point is "running here but
        offline there". A stopped service is someone's deliberate act (or the
        watchdog's), and restarting it would fight them.

    `service_running` is probed LAST and only for dirs that already passed the
    cheap filters: it shells out per dir, and apply.py runs on every
    job-completed hook, not just the 2h timer. In the overwhelmingly common
    case (nothing offline) it is never called at all.

    The paused_dirs and service_running guards overlap on purpose: a
    watchdog-paused runner has its service stopped, so service_running alone
    already excludes it. paused_dirs is the belt to that braces, and keeps the
    exclusion correct even if load-watchdog.state is unreadable.

    gh_runners is None (the gh query failed) → no dirs, same skip contract as
    decide(): with no GitHub side there is no mismatch to observe.
    """
    if gh_runners is None:
        return []

    offline = {
        r["name"] for r in gh_runners if (r.get("status") or "").lower() == "offline"
    }
    candidates = [
        d
        for d in local_dirs
        if f"{host}-{d}" in offline and d not in busy_dirs and d not in paused_dirs
    ]
    return sorted((d for d in candidates if service_running(d)), key=_dir_index)


def debounce_hung(
    now, first_seen, hung_now, evaluated, *, known=None, grace=HUNG_GRACE_SECONDS
):
    """Decide which suspects have been hung long enough to restart. Pure.

    now         epoch seconds.
    first_seen  {dirname: epoch} persisted from earlier runs.
    hung_now    dirnames that look hung on THIS pass (from hung_dirs).
    evaluated   dirnames we actually had GitHub data for on this pass. A dir
                we could not evaluate (its repo's gh query failed) keeps its
                record untouched: absence of evidence is not evidence of health,
                and clearing it would restart the grace clock forever on a host
                with flaky `gh`.
    known       every runner dir that still exists on the host, or None to skip
                the check. Records for dirs outside it are dropped — a removed
                runner must not leave its clock behind, because runner dir names
                are REUSED (remove app-2, install a new one, same name)
                and an inherited ancient timestamp would make the new runner
                restart on its first sighting, skipping the grace window
                entirely. Distinct from `evaluated`: a dir that still exists but
                whose gh query failed keeps its clock; one that is gone loses it.

    Returns (to_restart, new_first_seen).

    Elapsed time, not a run count, is what gates the restart — apply.py runs on
    every job-completed hook as well as the 2h maintenance timer, so "seen hung
    twice in a row" can mean two passes eight seconds apart on a busy host,
    which is no confirmation at all. Requiring `grace` to elapse makes the
    guarantee independent of how often this runs: a runner is restarted only if
    it was still hung a full maintenance tick after we first noticed.

    A restarted dir is dropped from the state, so if the restart does not take,
    the next pass re-records it and it gets another full `grace` before the next
    attempt. That bounds this to at most one restart per runner per `grace` —
    a wedged runner that cannot be revived bounces every 2h and says so in
    update.log, rather than spinning in a tight relaunch loop.
    """
    hung_now = set(hung_now)
    evaluated = set(evaluated)

    # Carry forward records for dirs we had no GitHub data for this pass,
    # dropping any whose runner dir no longer exists.
    new_first_seen = {
        dn: ts
        for dn, ts in first_seen.items()
        if dn not in evaluated and (known is None or dn in known)
    }

    to_restart = []
    for dn in sorted(hung_now, key=_dir_index):
        started = first_seen.get(dn)
        if started is None:
            new_first_seen[dn] = now  # first sighting: record, act next time
        elif now - started >= grace:
            to_restart.append(dn)  # confirmed hung across a full tick
        else:
            new_first_seen[dn] = started  # still inside the grace window
    return to_restart, new_first_seen


def short_hostname() -> str:
    return os.environ.get("APPLY_HOST") or socket.gethostname().split(".")[0]


def load_desired(
    host: str,
) -> tuple[dict[str, int], int, int | None, str | None, tuple[str, ...]]:
    h = fleet_config.load_host(CONFIG_FILE, host)
    return h.counts, h.ci_slots, h.e2e_workers, h.work_root, h.labels


_default_ci_slots = fleet_config._default_ci_slots


def discover_live() -> dict[str, list[Path]]:
    """Scan ~/actions-runner for configured runners. Returns {repo: [dirs]}."""
    live: dict[str, list[Path]] = {}
    for r in runner_fleet.discover_runners(base_dir=RUNNER_BASE):
        if r.repo is None:
            continue
        live.setdefault(r.repo, []).append(r.dir)
    for dirs in live.values():
        dirs.sort()
    return live


def discover_installed_dirs(repo_name: str) -> list[str]:
    """Basenames of installed runner dirs for `repo_name` that discover_live can't
    classify: `<repo_name>-<n>` dirs that have config.sh but no `.runner` (an
    interrupted re-register left them bare). They're unregistered but still real
    runners this host owns, so convergence must see them (to re-register or clean
    them up) instead of treating them as absent and installing duplicates.
    """
    out: list[str] = []
    if not RUNNER_BASE.is_dir():
        return out
    for entry in sorted(RUNNER_BASE.glob(f"{repo_name}-[0-9]*")):
        if (
            entry.is_dir()
            and (entry / "config.sh").is_file()
            and not (entry / ".runner").is_file()
        ):
            out.append(entry.name)
    return out


def _redact(cmd: list[str]) -> list[str]:
    """The command as it is safe to print. The value after --token is a runner
    registration or removal token, and run()'s output lands in update.log —
    anyone who can read that log within the token's lifetime could register a
    runner on the repo."""
    shown = list(cmd)
    for i, arg in enumerate(shown):
        if arg == "--token" and i + 1 < len(shown):
            shown[i + 1] = "***"
        elif arg.startswith("--token="):
            shown[i] = "--token=***"
    return shown


def run(cmd: list[str]) -> int:
    print(f"  $ {' '.join(_redact(cmd))}")
    return subprocess.call(cmd)


def _run(cmd) -> int:
    """Quiet run (captures output). Use `run` for the verbose, echoed variant."""
    return subprocess.run(cmd, capture_output=True, text=True, check=False).returncode


def fetch_gh_runners(repo: str, host: str, repo_name: str):
    """[{name,status}] for this host+repo, or None if the gh query fails.

    Registration-liveness only (is this runner registered on GitHub?) — busy/
    idle guarding is decided locally now, via runner_fleet.is_busy.

    Paginates: a repo's fleet-wide runner count can exceed one API page.
    """
    prefix = f"{host}-{repo_name}-"
    cmd = [
        "gh",
        "api",
        "--paginate",
        f"repos/{repo}/actions/runners?per_page=100",
        "--jq",
        ".runners[]",
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, check=False)
    except FileNotFoundError:
        return None
    if r.returncode != 0:
        return None
    out = []
    for line in r.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            return None
        name = obj.get("name", "")
        if name.startswith(prefix):
            out.append(
                {
                    "name": name,
                    "status": obj.get("status"),
                    "id": obj.get("id"),
                    "labels": [lbl.get("name") for lbl in obj.get("labels", [])],
                }
            )
    return out


def converge_labels(repo: str, gh_runners, extra_labels) -> int:
    """Add any missing `extra_labels` to this host's live registrations.

    Labels have to be converged over the API rather than at registration time,
    because re-registration only happens for DEAD runners — a healthy runner
    would keep its old label set forever otherwise, and adding a label would
    silently require an uninstall/reinstall of the whole fleet.

    Additive only: it never removes a label it did not expect. Hand-added
    labels (a one-off experiment, a manual pin) are somebody's deliberate act,
    and a convergence loop quietly deleting them would be worse than drift.

    Returns the number of runners updated. Best-effort: a gh failure is
    reported and skipped, never fatal — the next tick retries.
    """
    if not extra_labels or not gh_runners:
        return 0
    updated = 0
    for r in gh_runners:
        rid = r.get("id")
        missing = [x for x in extra_labels if x not in (r.get("labels") or [])]
        if rid is None or not missing:
            continue
        cmd = [
            "gh",
            "api",
            f"repos/{repo}/actions/runners/{rid}/labels",
            "--method",
            "POST",
        ]
        for lbl in missing:
            cmd += ["-f", f"labels[]={lbl}"]
        if _run(cmd) == 0:
            print(f"  + {r['name']}: added label(s) {', '.join(missing)}")
            updated += 1
        else:
            print(f"  ! {r['name']}: could not add label(s) {', '.join(missing)}")
    return updated


def mint_token(repo: str, kind: str):
    """kind is 'registration-token' or 'remove-token'. Returns token or None."""
    r = subprocess.run(
        [
            "gh",
            "api",
            f"repos/{repo}/actions/runners/{kind}",
            "--method",
            "POST",
            "--jq",
            ".token",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if r.returncode != 0:
        return None
    return r.stdout.strip() or None


TEMPLATE_DIR = SCRIPT_DIR / "templates"


def render_template(path: Path, **values: str) -> str:
    """Mirror of _render.sh's render_template, so one template serves both.

    Raises on a leftover @PLACEHOLDER@ rather than returning it: a unit file
    containing a literal @RUNNER_DIR@ loads fine and then never works, which is
    strictly worse than no file at all.
    """
    text = path.read_text()
    for key, value in values.items():
        text = text.replace(f"@{key}@", value)
    leftover = re.search(r"@[A-Z_][A-Z0-9_]*@", text)
    if leftover:
        raise ValueError(f"{path}: {leftover.group(0)} survived substitution")
    return text


def service_file_for(dirname: str) -> tuple[Path, str]:
    """(path, expected content) of this runner's launchd plist / systemd unit."""
    runner_dir = RUNNER_BASE / dirname
    if IS_MAC:
        label = f"com.github.actions-runner.{dirname}"
        return (
            Path.home() / "Library/LaunchAgents" / f"{label}.plist",
            render_template(
                TEMPLATE_DIR / "com.github.actions-runner.plist.in",
                LABEL=label,
                RUNNER_DIR=str(runner_dir),
                BASE_DIR=str(RUNNER_BASE),
                HOME=str(Path.home()),
            ),
        )
    return (
        Path.home() / ".config/systemd/user" / f"github-runner-{dirname}.service",
        render_template(
            TEMPLATE_DIR / "github-runner.service.in",
            RUNNER_NAME=f"{short_hostname()}-{dirname}",
            RUNNER_DIR=str(runner_dir),
        ),
    )


def service_drift(dirnames) -> list[tuple[str, Path, str]]:
    """Runners whose service file no longer matches the template.

    This is the gap that let a host carry an old unit forever: both installers
    skip an already-configured runner, so a service file written by an earlier
    kit was never rewritten. One host's runners kept `Restart=on-failure` and
    `KillMode=process` long after the template moved to `Restart=always`, and
    silently failed to come back from a reboot — the listener exits 0 on
    self-update, which on-failure does not restart.

    A MISSING file is not drift and is skipped: installing one is the
    installer's job, and rewriting a file for a runner that was never set up
    here would resurrect something convergence had deliberately removed.
    """
    out = []
    for dn in sorted(dirnames):
        path, expected = service_file_for(dn)
        if not path.is_file():
            continue
        if path.read_text() != expected:
            out.append((dn, path, expected))
    return out


def apply_service_rewrites(drift, reregistered, *, is_busy=None) -> int:
    """Rewrite drifted service files and restart, skipping busy runners.

    Busy is re-checked here rather than trusted from the run-start snapshot, for
    the same reason apply_env_restarts does it: a restart is an unconditional
    kill+relaunch and acting on stale busy state kills a job mid-flight.

    On systemd the unit is reloaded and re-enabled, not merely restarted: the
    drift that motivated this left units DISABLED with their listeners detached,
    so a plain restart would have fixed the file and left the host still unable
    to come back from a reboot.
    """
    if is_busy is None:

        def is_busy(dn):
            return runner_fleet.is_busy(
                RUNNER_BASE / dn, runner_fleet.worker_cmdlines()
            )

    failed = 0
    reloaded = False
    for dn, path, expected in drift:
        if dn not in reregistered and is_busy(dn):
            continue  # retry next tick; drift is not urgent enough to kill a job
        path.write_text(expected)
        print(f"  service file rewritten from template: {path.name}")
        if IS_MAC:
            if not _svc_restart(dn):
                print(f"  ! {dn}: restart after service rewrite failed")
                failed += 1
            continue
        if not reloaded:
            _run(["systemctl", "--user", "daemon-reload"])
            reloaded = True
        unit = f"github-runner-{dn}.service"
        if _run(["systemctl", "--user", "enable", "--now", unit]) != 0:
            print(f"  ! {dn}: enable --now after service rewrite failed")
            failed += 1
        elif _run(["systemctl", "--user", "restart", unit]) != 0:
            print(f"  ! {dn}: restart after service rewrite failed")
            failed += 1
    return failed


def _svc_stop_remove(dirname: str) -> None:
    """Stop and delete the launchd/systemd unit for a runner dir. Best effort."""
    if IS_MAC:
        label = f"com.github.actions-runner.{dirname}"
        _run(["launchctl", "bootout", f"gui/{os.getuid()}/{label}"])
        (Path.home() / "Library/LaunchAgents" / f"{label}.plist").unlink(
            missing_ok=True
        )
    else:
        unit = f"github-runner-{dirname}.service"
        _run(["systemctl", "--user", "disable", "--now", unit])
        (Path.home() / ".config/systemd/user" / unit).unlink(missing_ok=True)
        _run(["systemctl", "--user", "daemon-reload"])


def _svc_restart(dirname: str) -> bool:
    """Restart a runner's existing service so it picks up fresh credentials."""
    if IS_MAC:
        label = f"com.github.actions-runner.{dirname}"
        plist = Path.home() / "Library/LaunchAgents" / f"{label}.plist"
        if not plist.is_file():
            print(f"  ! {dirname}: launchd plist missing; cannot restart")
            return False
        _run(["launchctl", "bootout", f"gui/{os.getuid()}/{label}"])
        return _run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(plist)]) == 0
    unit = f"github-runner-{dirname}.service"
    return _run(["systemctl", "--user", "restart", unit]) == 0


def reregister(
    repo: str,
    dirname: str,
    host: str,
    work_root: str | None = None,
    extra_labels: tuple[str, ...] = (),
) -> bool:
    """Re-register an installed runner in place (registration lost). Preserves .env.

    Fails safe: mint the registration token FIRST and only touch the runner's
    local state once we have it — so a token failure never strips a dir we then
    can't re-register (which would leave it un-runnable AND un-discoverable, the
    exact half-broken state this guards against). Handles both the sleep-dereg
    case (stale .runner present) and an already-stripped dir (only config.sh
    left). `.runner_migrated` is cleared too, else `config.sh --replace` refuses
    with "already configured".

    When the host offloads _work (work_root set), pass --disableupdate to match
    install.sh: a runner's macOS "Removable Volumes" TCC grant is keyed to the
    exact resolved bin.<version>/Runner.Listener path, so a self-update (which
    replaces that directory) invalidates the grant and re-triggers a one-time
    interactive approval dialog — which nobody will be at the console to click
    on an unattended reboot, hanging the runner indefinitely. Disabling
    self-update keeps the binary path (and thus the grant) stable.
    """
    d = RUNNER_BASE / dirname
    token = mint_token(repo, "registration-token")
    if not token:
        print(
            f"  ! {dirname}: no registration token — leaving intact to retry next tick"
        )
        return False
    for f in (".runner", ".credentials", ".credentials_rsaparams", ".runner_migrated"):
        (d / f).unlink(missing_ok=True)
    cmd = [
        str(d / "config.sh"),
        "--url",
        f"https://github.com/{repo}",
        "--token",
        token,
        "--name",
        f"{host}-{dirname}",
        "--work",
        "_work",
        "--replace",
        "--unattended",
    ]
    # No OS or architecture labels: the runner applies self-hosted, its OS and
    # its real architecture as default labels on its own. Hardcoding them tagged
    # every Linux runner X64 — a wrong custom label on an ARM machine. Only the
    # host's extra labels from runners.toml are custom.
    if extra_labels:
        cmd += ["--labels", ",".join(extra_labels)]
    if work_root:
        cmd.append("--disableupdate")
    rc = run(cmd)
    if rc != 0:
        print(f"  ! {dirname}: config.sh re-register exited {rc}")
        return False
    return _svc_restart(dirname)


def install_runners(
    repo: str,
    desired: int,
    ci_slots: int,
    e2e_workers: int | None,
    work_root: str | None = None,
) -> bool:
    """Fill a shortfall via the existing installer (idempotent: skips existing dirs).

    Exports CI_SLOTS (and E2E_WORKERS_OVERRIDE when declared) so the installer
    bakes the host's budget into each new runner's .env, where the repo's own
    workflow reads them (an in-job admission gate, a browser-e2e worker count).
    Exports
    WORK_ROOT when the host offloads _work to an external drive, so install.sh
    symlinks each new runner's _work there.
    """
    suffix = "" if IS_MAC else "-linux"
    cmd = [str(SCRIPT_DIR / f"install{suffix}.sh"), repo, str(desired)]
    env = {**os.environ, "CI_SLOTS": str(ci_slots)}
    shown = f"CI_SLOTS={ci_slots}"
    if work_root:
        env["WORK_ROOT"] = work_root
        shown += f" WORK_ROOT={work_root}"
    else:
        # Don't leak a stale WORK_ROOT from the installing host's ambient env
        # into a host that leaves _work on the internal disk (mirrors the
        # E2E_WORKERS_OVERRIDE pop below).
        env.pop("WORK_ROOT", None)
    if e2e_workers is not None:
        env["E2E_WORKERS_OVERRIDE"] = str(e2e_workers)
        shown += f" E2E_WORKERS_OVERRIDE={e2e_workers}"
    else:
        # install.sh only omits the .env line when this var is unset in ITS
        # environment ([[ -n "${E2E_WORKERS_OVERRIDE:-}" ]]) — apply.py itself
        # always runs with E2E_WORKERS_OVERRIDE already set (baked into every
        # runner's own .env for the host's sizing), so a bare
        # os.environ copy silently leaks that stale value into repos where
        # it's meant to stay unmanaged. Pop it explicitly. (Caught 2026-07-16:
        # a second repo's fresh .env inherited E2E_WORKERS_OVERRIDE=3 from the
        # installing host's ambient env.)
        env.pop("E2E_WORKERS_OVERRIDE", None)
    print(f"  $ {shown} {' '.join(cmd)}")
    return subprocess.call(cmd, env=env) == 0


def remove_runner(repo: str, dirname: str) -> bool:
    """Deregister and delete an idle healthy runner (surgical scale-down).

    Fails safe: if the remove-token can't be minted (or config.sh is missing),
    the runner is registered and we must NOT destroy local state — leave it
    intact and let the next tick retry, rather than orphan the GitHub
    registration. Returns True only when the runner was cleanly deregistered
    and removed.
    """
    d = RUNNER_BASE / dirname
    token = mint_token(repo, "remove-token")
    if not token or not (d / "config.sh").is_file():
        print(
            f"  ! {dirname}: no remove token / config.sh — skipping removal "
            f"(registered runner left intact to retry next tick)"
        )
        return False
    rc = run([str(d / "config.sh"), "remove", "--token", token])
    if rc != 0:
        print(
            f"  ! {dirname}: config.sh remove exited {rc} — leaving intact to retry "
            f"next tick (runner may have just picked up a job)"
        )
        return False
    _svc_stop_remove(dirname)
    shutil.rmtree(d, ignore_errors=True)
    return True


def cleanup_dead(dirname: str) -> None:
    """Delete a dead (unregistered) runner dir + its stale service. No deregister."""
    _svc_stop_remove(dirname)
    shutil.rmtree(RUNNER_BASE / dirname, ignore_errors=True)


def discover_hook_files() -> dict[str, Path]:
    """{basename: source path} for every hook file to install. Dynamic — walks
    hooks/'s actual contents, never a hardcoded filename list (that's the bug
    class behind issue #19: a new hook silently never made update-host.sh's
    hand-maintained list). Every *.sh at the top of hooks/ is included; on
    Linux, hooks/linux/*.sh is overlaid on top under its own basename,
    overriding a same-named top-level entry. macOS ignores hooks/linux/.
    """
    hooks_dir = SCRIPT_DIR / "hooks"
    mapping: dict[str, Path] = {}
    if hooks_dir.is_dir():
        for p in sorted(hooks_dir.glob("*.sh")):
            mapping[p.name] = p
    if not IS_MAC:
        linux_dir = hooks_dir / "linux"
        if linux_dir.is_dir():
            for p in sorted(linux_dir.glob("*.sh")):
                mapping[p.name] = p
    return mapping


def sync_config() -> int:
    """Install every discovered hook file into RUNNER_BASE/hooks/ at mode 755.
    Prints one terse line per file actually written; silent when everything is
    already in sync (this runs on every update-host.sh tick, whose log must
    stay quiet on no-op runs). Returns the count of files changed.
    """
    dest_dir = RUNNER_BASE / "hooks"
    changed = 0
    for name, src in discover_hook_files().items():
        dest = dest_dir / name
        data = src.read_bytes()
        already_synced = (
            dest.is_file()
            and dest.read_bytes() == data
            and (dest.stat().st_mode & 0o777) == 0o755
        )
        if already_synced:
            continue
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        dest.chmod(0o755)
        print(f"sync-config: installed {name}")
        changed += 1
    return changed


def apply_env_restarts(env_work, reregistered, *, is_busy=None) -> int:
    """Write each dir's converged .env and restart its service — unless the
    dir is busy right now, in which case both are skipped (drift persists,
    next tick retries). Returns the count of restart failures.

    Busy is checked fresh here, immediately before acting, NOT off the
    host-wide snapshot taken once at run start (main()'s `busy_dirs`):
    install_runners() for an earlier repo in the same convergence loop can
    run for minutes, long enough for this dir to pick up a job in between.
    Unlike remove_runner() — where config.sh remove is rejected server-side
    for a busy runner — _svc_restart() is an unconditional kill+relaunch with
    no such guard, so acting on stale busy state can kill a runner mid-job.

    env_work      [(dirname, new_env_text), ...] for dirs whose .env drifted.
    reregistered  dirnames re-registered (and thus already restarted, and
                  known idle) earlier this repo iteration — skip re-checking
                  those, a fresh scan of a runner we just relaunched is just
                  wasted work.
    is_busy       injected fresh-busy predicate for tests; real callers omit
                  it and get a live runner_fleet check (one `ps` scan per dir
                  — cheap, and correctness beats saving a scan here).
    """
    if is_busy is None:

        def is_busy(dn):
            return runner_fleet.is_busy(
                RUNNER_BASE / dn, runner_fleet.worker_cmdlines()
            )

    failed = 0
    for dn, new_text in env_work:
        if dn not in reregistered and is_busy(dn):
            continue  # became busy since the snapshot: untouched, retry next tick
        (RUNNER_BASE / dn / ".env").write_text(new_text)
        if not _svc_restart(dn):
            print(f"  ! {dn}: restart after .env update failed")
            failed += 1
    return failed


# ── Hung-listener execution (I/O side of hung_dirs/debounce_hung) ────────────


def _svc_running(dirname: str) -> bool:
    """True iff this runner's service is currently loaded/active on the host.

    Mirrors load-watchdog.py's service_active(). "Running" here means the
    service manager holds it, which is the half of the hung signal we can only
    see locally — it says nothing about whether the listener inside is healthy.
    """
    if IS_MAC:
        label = f"com.github.actions-runner.{dirname}"
        return _run(["launchctl", "print", f"gui/{os.getuid()}/{label}"]) == 0
    return (
        _run(
            [
                "systemctl",
                "--user",
                "is-active",
                "--quiet",
                f"github-runner-{dirname}.service",
            ]
        )
        == 0
    )


# Every watchdog that parks a runner records it as {"paused": [dirname, ...]}
# in its own state file. Read them ALL: a laptop with its lid shut has runners
# parked by lid-watchdog alone, with nothing in load-watchdog.state.
WATCHDOG_STATE_FILES = ("load-watchdog.state", "lid-watchdog.state")


def load_watchdog_paused() -> set:
    """Dirnames a watchdog has deliberately parked, per the watchdog state files.

    Unreadable/absent/malformed → contributes nothing. Safe to fail open here
    only because a parked runner's service is stopped, so hung_dirs()'s
    service_running probe excludes it regardless — see that docstring.
    """
    paused: set = set()
    for name in WATCHDOG_STATE_FILES:
        try:
            data = json.loads((RUNNER_BASE / name).read_text())
            paused |= set(data.get("paused", []))
        except (
            FileNotFoundError,
            json.JSONDecodeError,
            OSError,
            ValueError,
            TypeError,
        ):
            continue
    return paused


def load_health_state() -> dict:
    """{dirname: first-seen-hung epoch} from the last run; {} if unusable."""
    try:
        data = json.loads(HEALTH_STATE_FILE.read_text())
        seen = data.get("first_seen", {})
        return {str(k): float(v) for k, v in seen.items()}
    except (FileNotFoundError, json.JSONDecodeError, OSError, ValueError, TypeError):
        return {}


def save_health_state(first_seen: dict) -> None:
    """Persist the suspect clock. Best-effort: a write failure must not fail a
    convergence run — it only costs this tick's debounce progress."""
    try:
        HEALTH_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        HEALTH_STATE_FILE.write_text(
            json.dumps(
                {"first_seen": {k: round(v) for k, v in sorted(first_seen.items())}}
            )
        )
    except OSError as e:
        print(f"  ! could not write {HEALTH_STATE_FILE.name}: {e}")


def apply_hung_restarts(to_restart, *, is_busy=None) -> int:
    """Restart each confirmed-hung runner. Returns the count of failures.

    Busy is re-checked fresh immediately before acting, for the same reason
    apply_env_restarts() does it: _svc_restart() is an unconditional
    kill+relaunch with no server-side busy guard, and this runs at the very end
    of a convergence pass that may have spent minutes in install_runners(). A
    dir that picked up a job in the meantime is left alone — it is plainly not
    hung anymore, and killing it would abort a live job.
    """
    if is_busy is None:

        def is_busy(dn):
            return runner_fleet.is_busy(
                RUNNER_BASE / dn, runner_fleet.worker_cmdlines()
            )

    failed = 0
    for dn in to_restart:
        if is_busy(dn):
            continue  # took a job since the scan: not hung after all
        if _svc_restart(dn):
            print(
                f"  {dn}: restarted (listener was hung: running here, offline on GitHub)"
            )
        else:
            print(f"  ! {dn}: restart of hung listener failed")
            failed += 1
    return failed


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Converge this host's runners to runners.toml."
    )
    ap.add_argument(
        "--dry-run", action="store_true", help="print the plan, change nothing"
    )
    ap.add_argument(
        "--sync-config",
        action="store_true",
        help="sync installed hook files from hooks/ only, then exit "
        "(no runner convergence)",
    )
    ap.add_argument(
        "--config",
        metavar="PATH",
        help="fleet config path (default: $ACTIONS_RUNNER_CONFIG or "
        "~/.config/actions-runner/runners.toml)",
    )
    args = ap.parse_args()

    global CONFIG_FILE
    if args.config:
        CONFIG_FILE = Path(args.config)

    if args.sync_config:
        sync_config()
        return 0

    host = short_hostname()
    host_cfg = fleet_config.load_host(CONFIG_FILE, host)
    desired = host_cfg.counts
    ci_slots = host_cfg.ci_slots
    e2e_workers = host_cfg.e2e_workers
    work_root = host_cfg.work_root
    extra_labels = host_cfg.labels
    gated = host_cfg.slot_gated_repos
    live = discover_live()  # {repo: [Path, ...]}
    allow_remove = not (RUNNER_BASE / ".no-auto-prune").exists()

    # Idle-removal guard (decide()'s planning input) and the dry-run/plan display,
    # scanned once for the whole host (cheaper than a `ps` scan per repo; busy
    # state isn't repo-scoped anyway). Plan-time staleness here is fine for
    # removals: config.sh remove is rejected server-side for a busy runner, so a
    # stale read only costs a retry next tick. The .env-restart action is NOT
    # gated by this snapshot — apply_env_restarts() re-checks busy state fresh
    # right before it acts, since _svc_restart() has no server-side guard.
    _workers = runner_fleet.worker_cmdlines()
    busy_dirs = {
        r.dir.name
        for r in runner_fleet.discover_runners(base_dir=RUNNER_BASE)
        if runner_fleet.is_busy(r, _workers)
    }

    # Convergence runs on every job-completed hook and every maintenance tick, so
    # keep the common no-op case silent: emit the host/desired/live banner lazily,
    # only when a repo actually reports something (acted, capped, or gh down) — or
    # always under --dry-run, which is interactive. A fully in-sync run prints
    # nothing, so update.log doesn't grow on every job.
    header_shown = False

    def show_header() -> None:
        nonlocal header_shown
        if header_shown:
            return
        header_shown = True
        print(f"host: {host}")
        print(f"desired: {dict(sorted(desired.items()))}")
        print(f"live:    {dict(sorted((r, len(d)) for r, d in live.items()))}")
        if not allow_remove:
            print(
                "note: .no-auto-prune present — removals suppressed (adds/re-register still run)"
            )
        print()

    if args.dry_run:
        show_header()

    # Converge every repo in config, plus repos present locally but no longer in
    # config (desired 0 → drain the host of them, idle-guarded + cap-guarded).
    all_repos = sorted(set(desired) | set(live))
    # A bare (installed-but-unregistered) dir carries no owner/repo of its own, so
    # it can only be claimed by directory name — safe only when the short name maps
    # to a single configured repo. Ambiguous short names skip the bare-dir sweep.
    short_counts = Counter(r.split("/")[-1] for r in all_repos)
    failed = 0

    # Hung-listener sweep state, accumulated across the per-repo loop below and
    # acted on once after it (the debounce clock is per-host, not per-repo).
    health_enabled = not NO_HEALTH_FILE.exists()
    paused_dirs = load_watchdog_paused() if health_enabled else set()
    hung_now: set[str] = set()
    evaluated_dirs: set[str] = set()

    for repo in all_repos:
        D = desired.get(repo, 0)
        repo_name = repo.split("/")[-1]
        D, clamped = clamp_to_slots(repo_name, D, ci_slots, gated)
        if clamped:
            show_header()
            print(
                f"[{repo}] CLAMPED {desired[repo]}→{D} to ci_slots={ci_slots} "
                f"(never run more slot-gated runners than CI slots — fix runners.toml)"
            )
        local_dirs = [p.name for p in live.get(repo, [])]
        if short_counts[repo_name] == 1:
            known = set(local_dirs)
            local_dirs += [
                n for n in discover_installed_dirs(repo_name) if n not in known
            ]
        gh = fetch_gh_runners(repo, host, repo_name)

        if gh is None:
            show_header()
            print(f"[{repo}] gh unavailable — skipping convergence (needs tokens)")
            continue

        if health_enabled:
            # Record before the in-sync `continue` below: a host can be
            # perfectly converged on counts and still have a wedged listener —
            # in the observed case the runner stayed *registered* (so decide()
            # saw it as healthy) while its listener was dead.
            evaluated_dirs.update(local_dirs)
            hung_now.update(
                hung_dirs(
                    gh,
                    local_dirs,
                    host=host,
                    service_running=_svc_running,
                    busy_dirs=busy_dirs,
                    paused_dirs=paused_dirs,
                )
            )

        plan = decide(
            D, local_dirs, gh, host=host, busy_dirs=busy_dirs, allow_remove=allow_remove
        )

        # .env convergence: tuning runners.toml must reach EXISTING runners, not
        # only new installs. The runner process reads .env at startup, so a
        # change needs a restart — busy runners are deferred untouched (drift
        # persists → next tick retries). Dirs already leaving are skipped.
        #
        # This runs for EVERY repo, not just the gated one. It was gated when
        # the only keys were the gated repo's budget (ci_slots/e2e_workers);
        # TMPDIR applies to every runner, so the loop had to come out of that
        # branch. runner_env_updates decides which keys a given repo gets.
        env_work = []  # (dirname, new_text)
        leaving = set(plan.to_remove) | set(plan.to_cleanup)
        for dn in sorted(set(local_dirs) - leaving, key=_dir_index):
            env_path = RUNNER_BASE / dn / ".env"
            if not env_path.is_file():
                continue
            new_text = upsert_env(
                env_path.read_text(),
                runner_env_updates(dn, ci_slots, e2e_workers, repo_name in gated),
            )
            if new_text is not None:
                env_work.append((dn, new_text))

        # Service files converge for EVERY repo, not just the gated one: a unit
        # or plist written by an older kit is never rewritten by the installers,
        # which skip an already-configured runner. That is how a host ends up
        # carrying Restart=on-failure long after the template moved on, and
        # silently fails to come back from a reboot. Dirs already leaving are
        # skipped for the same reason .env skips them.
        leaving_svc = set(plan.to_remove) | set(plan.to_cleanup)
        svc_work = service_drift(set(local_dirs) - leaving_svc)

        # Labels converge independently of the runner-count plan: a host can be
        # perfectly in sync on counts and still be missing a label added to
        # runners.toml after its runners were registered. Skipped under
        # --dry-run, which must not mutate anything.
        labels_added = 0
        if not args.dry_run:
            labels_added = converge_labels(repo, gh, extra_labels)

        acted = (
            plan.to_reregister
            or plan.to_install
            or plan.to_remove
            or plan.to_cleanup
            or env_work
            or svc_work
            or labels_added
        )

        if not acted and not plan.capped:
            if args.dry_run:
                print(f"[{repo}] in sync ({D} healthy registered)")
            continue

        show_header()

        if plan.capped:
            print(
                f"[{repo}] REFUSING removals: plan would remove more than {MAX_REMOVE} "
                f"runners (likely a runners.toml mistake). Adds/re-register still run. "
                f"Raise the cap or prune by hand if intentional."
            )

        for dn in plan.to_reregister:
            print(f"[{repo}] re-register {dn}")
        if plan.to_install:
            print(f"[{repo}] install {plan.to_install} new → target {D}")
        for dn in plan.to_remove:
            print(f"[{repo}] remove (idle) {dn}")
        for dn in plan.to_cleanup:
            print(f"[{repo}] cleanup dead {dn}")
        for dn, _ in env_work:
            state = "busy — deferring" if dn in busy_dirs else "restart"
            print(f"[{repo}] converge .env in {dn} ({state})")
        for dn, path, _ in svc_work:
            state = "busy — deferring" if dn in busy_dirs else "rewrite + restart"
            print(f"[{repo}] converge {path.name} ({state})")

        if args.dry_run:
            continue

        for dn in plan.to_reregister:
            if not reregister(repo, dn, host, work_root, extra_labels):
                failed += 1
        if plan.to_install and not install_runners(
            repo, D, ci_slots, e2e_workers, work_root
        ):
            failed += 1
        for dn in plan.to_remove:
            remove_runner(repo, dn)
        for dn in plan.to_cleanup:
            cleanup_dead(dn)
        # Service files before .env: both end in a restart, and doing the
        # service first means a dir needing both is restarted once by .env
        # rather than twice.
        reregistered = set(plan.to_reregister)
        failed += apply_service_rewrites(svc_work, reregistered)
        failed += apply_env_restarts(env_work, reregistered)

    # Hung-listener sweep. Runs after every repo has been converged so a restart
    # here can never race an install/remove on the same dir, and so the fresh
    # busy re-check inside apply_hung_restarts() reflects the final state.
    if health_enabled:
        # Re-discover after the loop: removals/installs above have landed, so
        # this is the authoritative set of dirs whose clocks may survive.
        known_dirs = {
            r.dir.name for r in runner_fleet.discover_runners(base_dir=RUNNER_BASE)
        }
        to_restart, new_seen = debounce_hung(
            time.time(),
            load_health_state(),
            hung_now,
            evaluated_dirs,
            known=known_dirs,
        )
        grace_h = HUNG_GRACE_SECONDS // 3600
        watching = sorted(set(new_seen) & hung_now, key=_dir_index)
        if watching or to_restart:
            show_header()
        for dn in watching:
            print(
                f"[hung] {dn}: running here but offline on GitHub — "
                f"watching (restart if still hung in {grace_h}h)"
            )
        for dn in to_restart:
            print(f"[hung] {dn}: still offline after {grace_h}h — restarting listener")
        if not args.dry_run:
            failed += apply_hung_restarts(to_restart)
            save_health_state(new_seen)

    if args.dry_run:
        print("\n(dry-run; no changes made)")
    return 2 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
