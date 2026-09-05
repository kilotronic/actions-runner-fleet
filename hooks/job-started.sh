#!/usr/bin/env bash
# Job-started hook: runs before each job on the self-hosted runner.
# Set via ACTIONS_RUNNER_HOOK_JOB_STARTED in the runner's .env file.
#
# Three host concerns, none of which a workflow step can handle:
#
# 1. Sample the host environment before the job starts (see SIDECARS below).
# 2. Hold a sleep inhibitor for the lifetime of the job, so an idle-suspend
#    policy cannot suspend the box mid-job (see below).
# 3. If this host opts into OrbStack via runners.toml
#    (`container_runtime = "orbstack"`), recover a stopped or wedged daemon
#    before GitHub sets up jobs.<name>.container.

HOOKS="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BASE="$(cd "$HOOKS/.." && pwd)"
RT="${CONTAINER_RUNTIME:-}"

# Sampled BEFORE ensure-orbstack.sh below: the snapshot is meant to describe the
# state the job arrived into, not the state after this hook has perturbed it.
# shellcheck source=hooks/_sidecars.sh
[[ -r "$HOOKS/_sidecars.sh" ]] && . "$HOOKS/_sidecars.sh" && run_sidecars started

if [[ -z "$RT" && -f "$BASE/.repo-path" ]]; then
  TOOLS="$(<"$BASE/.repo-path")"
  if [[ -f "$TOOLS/fleet_config.py" ]]; then
    RT="$(python3 "$TOOLS/fleet_config.py" --container-runtime 2>/dev/null || true)"
  fi
fi

if [[ "$RT" == orbstack && -x "$HOOKS/ensure-orbstack.sh" ]]; then
  "$HOOKS/ensure-orbstack.sh" --full || true
fi

# ── Keep the box awake for the duration of this job ──────────────────────────
#
# A desktop-flavoured host suspends itself when idle, and a runner is not a
# session that counts as activity. Worse, the usual hardening is not enough on
# its own: `logind`'s IdleAction only governs logind's OWN idle timer, while a
# graphical login screen left sitting at the greeter runs its session daemon's
# power policy and calls Suspend() directly. A box can therefore have
# IdleAction=ignore and still suspend on a 15-minute greeter timeout.
#
# The failure that follows is expensive and hard to read: the worker freezes
# mid-job, the service retires the job while the host is unreachable, and the
# runner wakes hours later to complete a job that no longer exists — surfacing
# as an unauthorized/404 completion with no step output and no log to upload.
# It looks like a network or service fault, not a power one.
#
# A block:sleep inhibitor makes logind refuse those Suspend() calls outright.
# `tail --pid` ties the inhibitor's lifetime to the job worker rather than to
# this hook, which exits immediately; the runner reaps both as orphans when the
# job ends, so there is nothing to clean up on the happy path or after a crash.
#
# Best-effort by contract: a host without systemd, or a polkit policy that
# refuses the inhibitor, must not fail the job. The warning is on stderr so an
# unexpected denial is visible in the job log instead of silently no-op'ing —
# a silent no-op here is what let this go unnoticed through many jobs.
if command -v systemd-inhibit >/dev/null 2>&1; then
  WORKER_PID="${PPID:-$$}"
  systemd-inhibit --what=sleep --mode=block \
    --who="actions-runner" --why="CI job in progress" \
    tail --pid="$WORKER_PID" -f /dev/null >/dev/null 2>&1 &
  INHIBIT_PID=$!
  # `cmd &` reports only that the fork happened, so it cannot tell us whether
  # the inhibitor was actually granted — a denied systemd-inhibit exits
  # immediately. Give it a moment, then check the process is still there.
  sleep 0.2
  if kill -0 "$INHIBIT_PID" 2>/dev/null; then
    echo "systemd-inhibit PID $INHIBIT_PID — blocking idle-suspend for job worker $WORKER_PID"
  else
    echo "warning: sleep inhibitor was refused; this box may suspend mid-job" >&2
  fi
fi

exit 0
