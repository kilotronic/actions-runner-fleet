#!/usr/bin/env bash
# Show status of all GitHub Actions self-hosted runners for a repository.
#
# Usage:
#   ./status.sh <owner/repo>
#   ./status.sh jasonluther/partygame
#
# OS-aware: on Linux the runners are systemd --user services backed by Docker;
# on macOS they are launchd agents backed by OrbStack. CI Postgres is a bare
# per-job container provisioned by partygame's scripts/ensure-ci-db.sh — this
# script just reports whether one is currently up.

set -euo pipefail

if [[ $# -lt 1 ]]; then
  echo "Usage: $0 <owner/repo>"
  exit 1
fi

REPO="$1"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
OS="$(uname -s)"
CI_PG_PORT="${CI_PG_PORT:-5433}"

# ── Container runtime + CI Postgres ──────────────────────────────────────────

# Report CI Postgres, matching the container name EXACTLY. This filter used to
# be `name=ci-postgres`, a substring match — which happily matched
# `partygame-ci-ci-postgres-1`, a leftover of the retired compose pre-warm that
# outlived its own compose file (deleted in cf484e5) and then served every CI
# job on a host for two days while this line reported it healthy. A monitor that
# accepts anything shaped like the right answer cannot detect the wrong one.
#
# So: exact name, and if nothing matches, say who is holding the port instead
# of a bare "not running" — a foreign holder is the interesting failure, and
# partygame's ensure-ci-db.sh now reclaims exactly that case.
_report_ci_postgres() {
  local docker=$1 port=$2 line holder
  line="$("$docker" ps --filter 'name=^ci-postgres$' --format '{{.Status}}' 2>/dev/null | head -1)"
  if [[ -n "$line" ]]; then
    echo "CI Postgres: ${line} (localhost:${port})"
    return
  fi
  holder="$("$docker" ps --filter "publish=${port}" --format '{{.Names}}' 2>/dev/null | head -1)"
  if [[ -n "$holder" ]]; then
    echo "CI Postgres: WRONG CONTAINER — '${holder}' holds ${port}, not 'ci-postgres'"
    echo "             (unowned by any code path; partygame's ensure-ci-db.sh will reclaim it)"
  else
    echo "CI Postgres: not running (provisioned per-job by partygame's scripts/ensure-ci-db.sh)"
  fi
}

if [[ "$OS" == "Linux" ]]; then
  if docker info &>/dev/null; then
    echo "Docker: running"
  else
    echo "Docker: not available (daemon down or user lacks docker group)"
  fi

  _report_ci_postgres docker "$CI_PG_PORT"
else
  DOCKER="$HOME/.orbstack/bin/docker"
  if [[ -x "$DOCKER" ]] && "$DOCKER" info &>/dev/null; then
    echo "OrbStack: running"
  else
    echo "OrbStack: not running (open OrbStack.app)"
  fi

  _report_ci_postgres "$DOCKER" "$CI_PG_PORT"
fi

echo ""

# ── Local inference (ollama) ─────────────────────────────────────────────────

# ollama_serve.py --status reports the declared intent, the configured tailnet
# forwarder, and whether both the loopback and tailnet endpoints actually answer
# — the last part matters because the forwarder can be configured while ollama
# itself is down, which reads as "serving" from the tailscale side alone.
#
# It needs a 3.11+ interpreter for tomllib, and bare `python3` is 3.9 on macOS —
# the same trap update-host.sh documents. Say so rather than degrade silently.
_resolve_py() {
  local c
  for c in python3.14 python3.13 python3.12 python3.11 \
    /opt/homebrew/bin/python3 /usr/local/bin/python3 python3; do
    command -v "$c" >/dev/null 2>&1 || continue
    "$c" -c 'import tomllib' >/dev/null 2>&1 && {
      command -v "$c"
      return 0
    }
  done
  return 1
}
if [[ -f "$SCRIPT_DIR/ollama_serve.py" ]]; then
  if PY="$(_resolve_py)"; then
    "$PY" "$SCRIPT_DIR/ollama_serve.py" --status || true
  else
    echo "ollama serve: skipped (no Python 3.11+ on PATH)"
  fi
  echo ""
fi

# ── Local workers ────────────────────────────────────────────────────────────

echo "Local workers:"
FOUND=0
# Discovery via runner_fleet.py (single source of "what runner dirs exist and
# which repo they belong to" — replaces a bash glob on the dir name, which
# only guessed at repo ownership rather than reading each .runner file).
while IFS= read -r RUNNER_DIR; do
  [[ -n "$RUNNER_DIR" ]] || continue
  FOUND=$((FOUND + 1))

  DIR_BASENAME="$(basename "$RUNNER_DIR")"
  STATUS="not running"

  if [[ "$OS" == "Linux" ]]; then
    UNIT="github-runner-${DIR_BASENAME}.service"
    if systemctl --user is-active --quiet "$UNIT" 2>/dev/null; then
      PID="$(systemctl --user show -p MainPID --value "$UNIT" 2>/dev/null || echo '?')"
      STATUS="running (PID ${PID})"
    fi
  else
    PLIST_LABEL="com.github.actions-runner.${DIR_BASENAME}"
    if launchctl print "gui/$(id -u)/${PLIST_LABEL}" &>/dev/null; then
      PID=$(launchctl print "gui/$(id -u)/${PLIST_LABEL}" 2>/dev/null | grep -o 'pid = [0-9]*' | grep -o '[0-9]*' || echo "?")
      STATUS="running (PID ${PID})"
    fi
  fi

  WORK_SIZE="$(du -sh "$RUNNER_DIR/_work" 2>/dev/null | cut -f1 || true)"
  echo "  ${DIR_BASENAME} — ${STATUS}${WORK_SIZE:+  [_work: ${WORK_SIZE}]}"
done < <(python3 "$SCRIPT_DIR/runner_fleet.py" --json | python3 -c '
import json, sys
repo = sys.argv[1]
for r in json.load(sys.stdin):
    if r["repo"] == repo:
        print(r["dir"])
' "$REPO")

if [[ $FOUND -eq 0 ]]; then
  echo "  (none installed)"
fi

echo ""

# ── GitHub status ────────────────────────────────────────────────────────────

echo "GitHub runners:"
gh api "repos/${REPO}/actions/runners" \
  --jq '.runners[] | "  \(.name) — \(.status)\(if .busy then " (busy)" else "" end) (\(.labels | map(.name) | join(", ")))"' \
  2>/dev/null || echo "  (could not query — check gh auth)"
