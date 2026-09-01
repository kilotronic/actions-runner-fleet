#!/usr/bin/env bash
# Uninstall all GitHub Actions self-hosted runners for a repository.
#
# Usage:
#   ./uninstall.sh <owner/repo>
#   ./uninstall.sh acme/app
#
# Finds all worker directories matching ~/actions-runner/<repo>-<N>/,
# stops their launchd agents, deregisters from GitHub, and removes files.

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

# Clean up cache and parent only if no other runner dirs remain.
# .shared-externals-* is shared by every runner on the host (see install.sh), so
# it must be excluded from the "is anything left" count — otherwise it looks
# like a surviving runner and the cleanup never fires — and removed only once
# the last runner is gone. Never remove it while other runners remain: their
# externals/ symlinks point at it, across repos.
REMAINING=$(find "$BASE_DIR" -maxdepth 1 -mindepth 1 -type d \
  ! -name '.cache' ! -name '.shared-externals-*' 2>/dev/null | wc -l)
if [[ "$REMAINING" -eq 0 ]]; then
  rm -rf "$BASE_DIR/.cache" 2>/dev/null || true
  rm -rf "$BASE_DIR"/.shared-externals-* 2>/dev/null || true
  rmdir "$BASE_DIR" 2>/dev/null || true
fi

echo ""
echo "Removed ${REMOVED} runner(s) for ${REPO}."
