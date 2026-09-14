#!/usr/bin/env bash
# Uninstall all GitHub Actions self-hosted runners for a repository.
#
# Usage:
#   ./uninstall.sh <owner/repo>
#   ./uninstall.sh acme/app
#
# Finds all worker directories matching ~/actions-runner/<repo>-<N>/,
# stops their launchd agents, deregisters from GitHub, and removes files. When
# the last runner on the host is gone, also removes the host's timers and the
# kit's files (see _teardown.sh).

set -euo pipefail

die() {
  echo "error: $*" >&2
  exit 1
}
info() { echo "==> $*"; }

if [[ $# -lt 1 ]]; then
  echo "Usage: $0 <owner/repo>"
  exit 1
fi

REPO="$1"
REPO_NAME="${REPO##*/}"
BASE_DIR="$HOME/actions-runner"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REMOVED=0

# Find all worker directories (repo-1, repo-2, ...)
for RUNNER_DIR in "$BASE_DIR/${REPO_NAME}"-[0-9]*; do
  [[ -d "$RUNNER_DIR" ]] || continue

  # Derive worker suffix for plist naming
  DIR_BASENAME="$(basename "$RUNNER_DIR")"
  PLIST_LABEL="com.github.actions-runner.${DIR_BASENAME}"
  PLIST_PATH="$HOME/Library/LaunchAgents/${PLIST_LABEL}.plist"

  info "Removing ${DIR_BASENAME}..."

  # Stop launchd agent
  if [[ -f "$PLIST_PATH" ]]; then
    launchctl bootout "gui/$(id -u)" "$PLIST_PATH" 2>/dev/null || true
    rm -f "$PLIST_PATH"
  fi

  # Deregister from GitHub
  if [[ -f "$RUNNER_DIR/config.sh" ]]; then
    REMOVE_TOKEN=$(gh api "repos/${REPO}/actions/runners/remove-token" --method POST --jq '.token' 2>/dev/null || true)
    if [[ -n "$REMOVE_TOKEN" ]]; then
      "$RUNNER_DIR/config.sh" remove --token "$REMOVE_TOKEN" 2>/dev/null || true
    else
      echo "  warning: could not get removal token for ${DIR_BASENAME}"
    fi
  fi

  rm -rf "$RUNNER_DIR"
  REMOVED=$((REMOVED + 1))
done

echo ""
echo "Removed ${REMOVED} runner(s) for ${REPO}."

# Host-wide cleanup once the last runner is gone: timers, then the kit's files.
# shellcheck source=_teardown.sh
. "$SCRIPT_DIR/_teardown.sh"
teardown_host "$SCRIPT_DIR" "$BASE_DIR" "$REPO"
