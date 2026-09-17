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

# ── Empty this job's temp dir ────────────────────────────────────────────────
#
# The counterpart to job-started.sh's mkdir. Emptying it between jobs is what
# stops temp from accumulating on a host: a leaked cache dir per run is
# invisible until the filesystem is full, and then the box fails jobs in ways
# that never mention temp at all.
#
# The case guard is load-bearing, not defensive dressing. This hook runs after
# every job on the host, and `rm -rf` over an unset, empty, or ambient TMPDIR is
# exactly how a cleanup hook eats a home directory. Wipe ONLY a path that is a
# runner's own `_tmp` under this user's runner base; anything else — including a
# bare /tmp — is left alone. `find -delete` rather than a glob so hidden entries
# go too and the dir itself stays.
case "${TMPDIR:-}" in
  "$HOME"/actions-runner/*/_tmp)
    [[ -d "$TMPDIR" ]] && find "$TMPDIR" -mindepth 1 -delete 2>/dev/null
    ;;
esac

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
# One grace per host, not one per job. The inhibitor runs in a fixed-name
# transient systemd --user unit, and each job end replaces it: stop the unit,
# start a fresh 900s one. A busy host therefore holds a single inhibitor that
# keeps being refreshed, instead of a new systemd-inhibit + sleep pair for every
# job in the last 15 minutes. If two hooks race, systemd refuses to start a unit
# that is already active, so there is never more than one.
#
# The unit also keeps the grace clear of everything that would end it early. It
# is not a child of this hook — systemd-run does not pass RUNNER_TRACKING_ID into
# the unit — so the runner's orphan cleanup cannot reach it, and it is not in the
# runner's cgroup, so restarting this runner leaves it alone.
#
# With no usable user manager, fall back to a backgrounded inhibitor with the
# tracking ID stripped. That one does stack, but it still outlives the job.
GRACE_UNIT=actions-runner-post-job-grace
grace_refused() {
  echo "warning: post-job sleep inhibitor was refused; this box may suspend between jobs" >&2
}
if command -v systemd-inhibit >/dev/null 2>&1; then
  grace=""
  if command -v systemd-run >/dev/null 2>&1; then
    systemctl --user stop "$GRACE_UNIT.service" >/dev/null 2>&1 || true
    if systemd-run --user --unit="$GRACE_UNIT" --collect --quiet \
      systemd-inhibit --what=sleep --mode=block \
      --who="actions-runner" --why="post-job grace" \
      sleep 900 >/dev/null 2>&1; then
      # systemd-run only reports that the unit started. A refused inhibitor
      # (logind says "Access denied", e.g. where polkit is absent) exits within
      # milliseconds, and without this check the hook reported a grace that did
      # not exist. The same check job-started.sh makes.
      sleep 0.2
      if systemctl --user is-active --quiet "$GRACE_UNIT.service" >/dev/null 2>&1; then
        grace=held
      else
        grace=refused
      fi
    elif systemctl --user is-active --quiet "$GRACE_UNIT.service" >/dev/null 2>&1; then
      grace=held # another job-completed hook started it first
    fi
  fi
  case "$grace" in
    held) echo "post-job idle-suspend grace (900s) — held by $GRACE_UNIT.service" ;;
    # Refused in the unit means refused for this user: the fallback below asks
    # logind for the same inhibitor and would be refused the same way.
    refused) grace_refused ;;
    *)
      env -u RUNNER_TRACKING_ID systemd-inhibit --what=sleep --mode=block \
        --who="actions-runner" --why="post-job grace" \
        sleep 900 >/dev/null 2>&1 &
      grace_pid=$!
      # Poll, do not sample once. A refused inhibitor exits within milliseconds
      # of STARTING, but it may not have started when a single fixed sleep
      # elapses — and then "still alive" means "not started yet", not "holding".
      # Sampling once at 0.2s reported a refused grace as held in 4 of 12 runs of
      # the sandbox test. That is the dangerous direction: the hook claims a
      # grace it does not have, which is the exact silent failure it exists to
      # prevent. Five samples over the same window catch the exit wherever it
      # falls instead of at one arbitrary instant.
      for _ in 1 2 3 4 5; do
        sleep 0.1
        kill -0 "$grace_pid" 2>/dev/null || break
      done
      if kill -0 "$grace_pid" 2>/dev/null; then
        echo "systemd-inhibit PID $grace_pid — post-job idle-suspend grace (900s)"
      else
        grace_refused
      fi
      ;;
  esac
fi

exit 0
