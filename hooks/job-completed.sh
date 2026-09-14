#!/usr/bin/env bash
# Job-completed hook: close the sidecar pair, trigger a fleet update, and hold a
# short post-job sleep inhibitor (all best-effort).
# Set via ACTIONS_RUNNER_HOOK_JOB_COMPLETED in the runner's .env file.
#
# Nothing this hook starts may be a plain background child. When a job ends, the
# runner terminates every process still carrying that job's RUNNER_TRACKING_ID,
# milliseconds after this hook returns — a real job log shows it killing both
# things this hook used to background, about 40ms after they started. Each
# section below says how it gets out of the way.

HOOKS="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# The completion half of the sidecar pair job-started.sh opens — a sidecar that
# registers on start and never deregisters leaks whatever it opened. Runs before
# the fleet update below so it is not delayed behind it.
# shellcheck source=hooks/_sidecars.sh
[[ -r "$HOOKS/_sidecars.sh" ]] && . "$HOOKS/_sidecars.sh" && run_sidecars completed

# ── Fleet update ─────────────────────────────────────────────────────────────
#
# Ask the service manager to start the maintenance job instead of running
# update-host.sh as a child of this hook. Run as a child it was killed by the
# orphan cleanup on every job, so hosts only ever converged on the 2h timer.
#
# Stripping RUNNER_TRACKING_ID would keep it alive but is not enough: it would
# still live in this runner's launchd process group or systemd cgroup, and
# update-host.sh can restart this very runner once the job has finished. That
# restart kills everything in the group, the updater included — on macOS between
# `launchctl bootout` and `bootstrap`, which would leave the runner unloaded with
# nothing to bring it back. The maintenance job runs update-host.sh in its own
# launchd job or systemd unit, exactly as the timer does, outside all of that.
#
# The label and unit are maintenance-timer.py's LABEL and UNIT
# (tests/test_job_completed_hook.py pins them). A job that is already running
# makes this a no-op, and update-host.sh takes its own lock regardless.
if [[ -f "$HOME/actions-runner/.repo-path" ]]; then
  if command -v launchctl >/dev/null 2>&1; then
    launchctl kickstart "gui/$UID/com.github.actions-runner.maintenance" >/dev/null 2>&1 || true
  elif command -v systemctl >/dev/null 2>&1; then
    systemctl --user start --no-block github-runner-maintenance.service >/dev/null 2>&1 || true
  fi
fi

# ── Post-job grace ───────────────────────────────────────────────────────────
#
# The job-started inhibitor dies with the worker, so without this the box
# becomes suspendable the instant a job ends — and a multi-job workflow can hand
# the next job to a host that is already on its way down. A short grace keeps it
# awake across that gap.
#
# Started without RUNNER_TRACKING_ID, or the orphan cleanup ends a 900s grace
# within milliseconds of starting it. It still belongs to the runner's cgroup, so
# restarting this runner ends it early — that only shortens a grace nobody needs
# once the runner itself is cycling.
if command -v systemd-inhibit >/dev/null 2>&1; then
  env -u RUNNER_TRACKING_ID systemd-inhibit --what=sleep --mode=block \
    --who="actions-runner" --why="post-job grace" \
    sleep 900 >/dev/null 2>&1 &
  echo "systemd-inhibit PID $! — post-job idle-suspend grace (900s)"
fi

exit 0
