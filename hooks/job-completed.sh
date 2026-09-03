#!/usr/bin/env bash
# Job-completed hook: pull and apply fleet updates (best-effort).
# Set via ACTIONS_RUNNER_HOOK_JOB_COMPLETED in the runner's .env file.
#
# update-host.sh is internally locked, so concurrent hooks across runners
# serialize cleanly.

REPO_PATH_FILE="$HOME/actions-runner/.repo-path"
if [[ -f "$REPO_PATH_FILE" ]]; then
  UPDATER="$(<"$REPO_PATH_FILE")/update-host.sh"
  if [[ -x "$UPDATER" ]]; then
    nohup "$UPDATER" </dev/null >/dev/null 2>&1 &
    disown 2>/dev/null || true
  fi
fi

# ── Post-job grace ───────────────────────────────────────────────────────────
#
# The job-started inhibitor dies with the worker, so without this the box
# becomes suspendable the instant a job ends — and a multi-job workflow can
# hand the next job to a host that is already on its way down. A short grace
# keeps it awake across that gap. Backgrounded so the hook returns at once;
# the runner reaps it as an orphan.
if command -v systemd-inhibit >/dev/null 2>&1; then
  systemd-inhibit --what=sleep --mode=block \
    --who="actions-runner" --why="post-job grace" \
    sleep 900 >/dev/null 2>&1 &
  echo "systemd-inhibit PID $! — post-job idle-suspend grace (900s)"
fi
