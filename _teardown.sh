#!/usr/bin/env bash
# Host teardown, shared by uninstall.sh and uninstall-linux.sh.
#
# Sourced after an uninstaller has removed one repo's runner directories. If no
# runner is left on the host, it removes what the kit installed host-wide: the
# per-host timers, then the kit's own files under ~/actions-runner. If runners
# remain, it leaves all of that alone and warns when the maintenance timer is
# about to reinstall the repo that was just removed.
#
# Shared rather than duplicated because the two copies of this cleanup had
# already drifted apart: each skipped a different leftover directory, so on a
# real host neither one ever ran. And removing the runners without the timers
# did not stick — two hours later the maintenance timer ran apply.py, which
# reinstalled every runner still listed in runners.toml.
#
# Usage: . "$SCRIPT_DIR/_teardown.sh"; teardown_host "$SCRIPT_DIR" "$BASE_DIR" "$REPO"

# A runner is any directory holding the runner's config.sh, registered or not.
# The kit's own directories never do — hooks/, logs/, .cache/, .shared-*/ —
# which is exactly what the old directory count got wrong.
_teardown_runners_left() {
  local d
  for d in "$1"/*/; do
    [[ -f "${d}config.sh" ]] && return 0
  done
  return 1
}

# The timer scripts need Python 3.11+ (tomllib, datetime.UTC). Resolve it the way
# update-host.sh does: a bare `python3` over SSH on macOS is often 3.9.
_teardown_python() {
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

# True if <repo> still has a nonzero count for this host in the inventory — the
# condition under which apply.py would install it again.
_teardown_still_listed() {
  local tools=$1 repo=$2 py=$3
  [[ -n "$py" ]] || return 1
  "$py" - "$tools" "$repo" 2>/dev/null <<'PY'
import os, socket, sys
sys.path.insert(0, sys.argv[1])
import fleet_config
host = os.environ.get("APPLY_HOST") or socket.gethostname().split(".")[0]
try:
    cfg = fleet_config.load_host(fleet_config.resolve_config_path(), host)
except SystemExit:
    sys.exit(1)
sys.exit(0 if cfg.counts.get(sys.argv[2], 0) > 0 else 1)
PY
}

teardown_host() {
  local tools=$1 base=$2 repo=$3 py t
  py="$(_teardown_python || true)"

  if _teardown_runners_left "$base"; then
    if _teardown_still_listed "$tools" "$repo" "$py"; then
      echo ""
      echo "note: ${repo} is still listed for this host in runners.toml. Other runners"
      echo "      remain, so the maintenance timer stays installed and will reinstall"
      echo "      ${repo} on its next update. Set its count to 0 (or remove the entry)"
      echo "      to retire it for good."
    fi
    return 0
  fi

  echo ""
  echo "==> Last runner removed; removing this host's timers and kit files..."
  if [[ -n "$py" ]]; then
    for t in maintenance-timer.py load-watchdog.py lid-watchdog.py orbstack-watchdog.py; do
      [[ -f "$tools/$t" ]] || continue
      if ! "$py" "$tools/$t" --uninstall-timer 2>&1 | sed 's/^/  /'; then
        echo "  warning: $t --uninstall-timer failed; run it by hand"
      fi
    done
  else
    echo "  warning: no Python 3.11+ found, so the timers are still installed. Remove each"
    echo "           with: <python3.11+> $tools/<timer>.py --uninstall-timer"
  fi

  # The kit's own files. .shared-externals-* goes only now, never while any
  # runner remains: every runner's externals/ symlink points at it, across repos.
  #
  # What STAYS is the kit's own diagnostics — logs/ and ci-env-jobs.jsonl, both
  # written by this kit — kept on purpose, because the reason someone is tearing
  # a host down is often in them. Plus anything here the kit did not put there.
  # The inventory under ~/.config/actions-runner is never touched.
  rm -rf "$base/hooks" "$base/.cache" "$base"/.shared-externals-* "$base/.shared-tool-cache" 2>/dev/null || true
  rm -f "$base/.repo-path" "$base/.update.lock" "$base/.uv-self-update-stamp" "$base"/*.state 2>/dev/null || true
  if rmdir "$base" 2>/dev/null; then
    echo "  removed $base"
  else
    echo "  kept $base (its logs and job-env records, plus anything else here)"
  fi
  if _teardown_still_listed "$tools" "$repo" "$py"; then
    echo "note: ${repo} is still listed for this host in runners.toml; running apply.py would reinstall it."
  fi
}
