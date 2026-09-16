#!/usr/bin/env bash
# Reclaim disk used by self-hosted runners on this host.
#
# Dry-run by default; pass --apply to actually delete. Only touches things that
# are safe to remove while the runners keep running:
#
#   Pruned with --apply:
#     - _diag/*.log older than 7 days (per runner) — always safe
#     - _work/_temp/* for IDLE runners only — never disturbs a running job
#
#   Reported only (delete manually if disk is tight — they force a re-download
#   on the next job, so not worth auto-pruning):
#     - each runner's _work checkout size
#     - the shared Playwright browser cache (~/.cache/ms-playwright)
#
# Idle/busy is read from GitHub; if that query fails the script errs on the side
# of caution and skips _temp pruning (still prunes the always-safe _diag logs).
#
# Usage:
#   ./prune.sh <owner/repo>           # dry-run report
#   ./prune.sh <owner/repo> --apply   # prune the safe targets

set -euo pipefail

REPO=""
APPLY=0
for arg in "$@"; do
  case "$arg" in
    --apply) APPLY=1 ;;
    -*)
      echo "unknown flag: $arg" >&2
      exit 1
      ;;
    *) REPO="$arg" ;;
  esac
done
[[ -z "$REPO" ]] && {
  echo "Usage: $0 <owner/repo> [--apply]"
  exit 1
}

REPO_NAME="${REPO##*/}"
BASE_DIR="$HOME/actions-runner"
HOSTNAME_S="$(hostname -s)"
DIAG_AGE_DAYS=7
PW_CACHE="$HOME/.cache/ms-playwright"

if [[ "$APPLY" == "1" ]]; then
  echo "== prune (APPLY — deleting) =="
else
  echo "== prune (dry-run; pass --apply to delete) =="
fi

# Runners GitHub currently reports as busy (empty if the query fails → treat all
# as busy so we never prune a running job's _temp).
if BUSY="$(gh api "repos/${REPO}/actions/runners" --jq '.runners[] | select(.busy) | .name' 2>/dev/null)"; then
  :
else
  echo "  warning: could not query GitHub; skipping _temp pruning (busy unknown)"
  BUSY="__all__"
fi

human() { du -sh "$1" 2>/dev/null | cut -f1 || true; }
is_busy() { [[ "$BUSY" == "__all__" ]] || grep -qxF "$1" <<<"$BUSY"; }

for RUNNER_DIR in "$BASE_DIR/${REPO_NAME}"-[0-9]*; do
  [[ -d "$RUNNER_DIR" ]] || continue
  DIR_BASENAME="$(basename "$RUNNER_DIR")"
  RUNNER_NAME="${HOSTNAME_S}-${DIR_BASENAME}"

  # Always-safe: stale _diag logs.
  DIAG="$RUNNER_DIR/_diag"
  if [[ -d "$DIAG" ]]; then
    mapfile -t OLD_LOGS < <(find "$DIAG" -type f -name '*.log' -mtime +"$DIAG_AGE_DAYS" 2>/dev/null)
    if [[ "${#OLD_LOGS[@]}" -gt 0 ]]; then
      echo "  ${DIR_BASENAME}: ${#OLD_LOGS[@]} _diag log(s) > ${DIAG_AGE_DAYS}d"
      [[ "$APPLY" == "1" ]] && printf '%s\0' "${OLD_LOGS[@]}" | xargs -0 rm -f
    fi
  fi

  # Idle-only: _work/_temp scratch.
  TEMP="$RUNNER_DIR/_work/_temp"
  if [[ -d "$TEMP" ]] && [[ -n "$(ls -A "$TEMP" 2>/dev/null)" ]]; then
    if is_busy "$RUNNER_NAME"; then
      echo "  ${DIR_BASENAME}: _work/_temp $(human "$TEMP") — SKIP (busy)"
    else
      echo "  ${DIR_BASENAME}: _work/_temp $(human "$TEMP") — reclaim (idle)"
      [[ "$APPLY" == "1" ]] && rm -rf "${TEMP:?}/"* 2>/dev/null || true
    fi
  fi

  # Report-only: full checkout size.
  [[ -d "$RUNNER_DIR/_work" ]] && echo "  ${DIR_BASENAME}: _work total $(human "$RUNNER_DIR/_work") (manual: rm -rf when idle to force a fresh checkout)"
done

if [[ -d "$PW_CACHE" ]]; then
  echo "  playwright cache $(human "$PW_CACHE") (manual: rm -rf '$PW_CACHE' when no runner is busy; reinstalled on next job)"
fi

echo "done."
