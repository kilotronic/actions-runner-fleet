#!/usr/bin/env bash
# Post CI presence to the standup board (best-effort, non-fatal).
#
# Usage: standup-presence.sh <started|completed>
#   started    register a `type=runner` session for this runner (shows the job
#              building right now on the board)
#   completed  deregister that session
#
# Fail-safe: a missing binary, missing config, or an unreachable board is a
# silent no-op — it never fails or delays a CI job. Portable across the macOS
# and Linux runners.

EVENT="${1:-}"

# Runner services run under launchd/systemd with a minimal PATH that usually
# excludes ~/.local/bin (where the client installs itself), so `command -v`
# alone is unreliable. Prefer PATH, fall back to the known install location.
STANDUP="$(command -v standup 2>/dev/null || echo "$HOME/.local/bin/standup")"
[[ -x "$STANDUP" ]] || exit 0

SESSION_ID="runner:${RUNNER_NAME:-$(hostname -s)}"

case "$EVENT" in
  started)
    REPO="${GITHUB_REPOSITORY##*/}"
    "$STANDUP" register --type runner \
      --session-id "$SESSION_ID" \
      --repo "${REPO:-unknown}" \
      --machine "$(hostname -s)" \
      --goal "CI: ${REPO:-unknown}" \
      --step "${GITHUB_WORKFLOW:-workflow} · run ${GITHUB_RUN_NUMBER:-?}" \
      </dev/null >/dev/null 2>&1 || true
    ;;
  completed)
    "$STANDUP" deregister --session-id "$SESSION_ID" \
      </dev/null >/dev/null 2>&1 || true
    ;;
esac

exit 0
