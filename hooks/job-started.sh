#!/usr/bin/env bash
# Job-started hook: runs before each job on the self-hosted runner.
# Set via ACTIONS_RUNNER_HOOK_JOB_STARTED in the runner's .env file.
#
# If this host opts into OrbStack via runners.toml
# (`container_runtime = "orbstack"`), recover a stopped or wedged daemon
# before GitHub sets up jobs.<name>.container. A workflow step is too late.

HOOKS="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BASE="$(cd "$HOOKS/.." && pwd)"
RT="${CONTAINER_RUNTIME:-}"

if [[ -z "$RT" && -f "$BASE/.repo-path" ]]; then
  TOOLS="$(<"$BASE/.repo-path")"
  if [[ -f "$TOOLS/fleet_config.py" ]]; then
    RT="$(python3 "$TOOLS/fleet_config.py" --container-runtime 2>/dev/null || true)"
  fi
fi

if [[ "$RT" == orbstack && -x "$HOOKS/ensure-orbstack.sh" ]]; then
  "$HOOKS/ensure-orbstack.sh" --full || true
fi

exit 0
