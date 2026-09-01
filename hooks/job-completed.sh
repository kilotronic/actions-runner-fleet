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
