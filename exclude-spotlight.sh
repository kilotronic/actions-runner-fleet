#!/usr/bin/env bash
# Add a path to Spotlight's Privacy list — the one mechanism that actually
# excludes a directory from indexing.
#
# Run it as a human, at a shell on the host, with sudo. Deliberately NOT called
# by the maintenance timer: the Privacy list lives in a root-owned
# VolumeConfiguration.plist, these hosts have no passwordless sudo, and a timer
# that cannot complete its job is worse than one that never claims to.
# exclude-ci-paths.sh reports the path and points here.
#
# ── Why the obvious routes do not work ───────────────────────────────────────
#   .metadata_never_index   INERT on an ordinary directory. Measured: with the
#                           marker in place, a newly created file under it was
#                           indexed within 45s, identical to an unexcluded
#                           control. Honoured at VOLUME/MOUNT roots only.
#   mdutil -i off <dir>     mdutil operates on volumes and stores, not paths.
#
# ── Two traps this script exists to avoid ────────────────────────────────────
# 1. `mdutil -i off <vol> && mdutil -i on <vol>` is NOT a config reload. It
#    ERASES the volume's index and starts a full rebuild — hours of CPU on a
#    small box, and it makes every path read 0 for the duration. This script
#    restarts mds instead, which reloads the config and keeps the index.
#
# 2. Verifying with a query that cannot fail. `mdfind -onlyin <p> -count '*'`
#    returning 0 means "not indexed" ONLY if some control path returns non-zero
#    at the same moment; during a rebuild everything returns 0 and an exclusion
#    that did nothing looks like a success. So the check below always measures a
#    control, and reports INCONCLUSIVE rather than guessing.
#
# Usage:
#   ./exclude-spotlight.sh                      # exclude ~/actions-runner
#   ./exclude-spotlight.sh /path/one /path/two  # exclude specific paths
#   ./exclude-spotlight.sh --dry-run            # show what would change
#   ./exclude-spotlight.sh --status             # report only, change nothing
#   ./exclude-spotlight.sh --remove <path>      # take a path back off the list

set -uo pipefail

PLIST="${SPOTLIGHT_PLIST:-/System/Volumes/Data/.Spotlight-V100/VolumeConfiguration.plist}"
DRY_RUN=0
STATUS_ONLY=0
REMOVE=0
PATHS=()

while (($#)); do
  case "$1" in
    --dry-run) DRY_RUN=1 ;;
    --status) STATUS_ONLY=1 ;;
    --remove) REMOVE=1 ;;
    -h | --help)
      sed -n '2,37p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    -*)
      echo "unknown flag: $1" >&2
      exit 2
      ;;
    *) PATHS+=("$1") ;;
  esac
  shift
done
((${#PATHS[@]})) || PATHS=("$HOME/actions-runner")

[[ "$(uname -s)" == "Darwin" ]] || {
  echo "not macOS — Spotlight does not apply here"
  exit 0
}

say() { printf '\n== %s ==\n' "$*"; }

# Privileged steps report the step, the command and the tool's own words. A bare
# tool error standing in for a diagnosis costs more time than it saves.
_sudo_step() {
  local what=$1
  shift
  local out rc=0
  out="$(sudo "$@" 2>&1)" || rc=$?
  if ((rc)); then
    echo "FAILED: $what" >&2
    echo "  ran:  sudo $*" >&2
    [[ -n "$out" ]] && echo "  said: $out" >&2
    return "$rc"
  fi
  return 0
}

_exclusions() { sudo plutil -extract Exclusions json -o - "$PLIST" 2>/dev/null; }

_is_excluded() {
  local want=$1 json
  json="$(_exclusions)" || return 1
  # Exact element match, so /a/b does not look excluded because /a/bc is.
  python3 -c '
import json,sys
try: items = json.loads(sys.argv[1])
except Exception: sys.exit(1)
sys.exit(0 if sys.argv[2] in items else 1)
' "$json" "$want" 2>/dev/null
}

# ── Report ───────────────────────────────────────────────────────────────────

say "current Privacy list"
if _json="$(_exclusions)" && [[ -n "$_json" ]]; then
  python3 -c '
import json,sys
for p in json.loads(sys.argv[1]): print("  " + p)
' "$_json" 2>/dev/null || echo "  (could not parse)"
else
  echo "  (no Exclusions key, or not readable — sudo is required)"
fi

say "indexing status"
for p in "${PATHS[@]}"; do
  printf '  %-44s indexed items: %s\n' "$p" "$(mdfind -onlyin "$p" -count '*' 2>/dev/null || echo '?')"
done

((STATUS_ONLY)) && exit 0

# ── Change ───────────────────────────────────────────────────────────────────

_changed=0
_would=0
for p in "${PATHS[@]}"; do
  p="${p%/}"
  if ((REMOVE)); then
    if ! _is_excluded "$p"; then
      echo "not on the list: $p"
      continue
    fi
    ((DRY_RUN)) && {
      echo "would remove from the Privacy list: $p"
      _would=1
      continue
    }
    _idx="$(python3 -c '
import json,sys
print(json.loads(sys.argv[1]).index(sys.argv[2]))
' "$(_exclusions)" "$p" 2>/dev/null)" || continue
    _sudo_step "remove $p from the Privacy list" \
      plutil -remove "Exclusions.$_idx" "$PLIST" || exit 1
    _changed=1
    continue
  fi

  if _is_excluded "$p"; then
    echo "already excluded: $p"
    continue
  fi
  [[ -d "$p" ]] || {
    echo "refusing: $p is not a directory" >&2
    exit 1
  }
  ((DRY_RUN)) && {
    echo "would add to the Privacy list: $p"
    _would=1
    continue
  }

  # Back up before the first edit. This file is mds's own state; a malformed
  # edit can cost a full reindex, which is expensive on the boxes that need
  # this most.
  if ((_changed == 0)); then
    _sudo_step "back up $PLIST" cp -p "$PLIST" "${PLIST}.bak" || exit 1
    echo "backed up -> ${PLIST}.bak"
  fi

  # plutil -insert ... -append needs the key to exist; it does not create one.
  if ! _exclusions >/dev/null; then
    _sudo_step "create the Exclusions array" \
      plutil -insert Exclusions -json '[]' "$PLIST" || exit 1
  fi
  _sudo_step "add $p to the Privacy list" \
    plutil -insert Exclusions -string "$p" -append "$PLIST" || exit 1
  echo "added: $p"
  _changed=1
done

if ((DRY_RUN)); then
  ((_would)) || say "nothing to do"
  exit 0
fi
((_changed)) || {
  say "nothing to do"
  exit 0
}

say "reloading mds"
# A RESTART, not `mdutil -i off/on`: that erases the volume index and starts a
# full rebuild. kickstart -k reloads the config and keeps the index.
_sudo_step "restart mds" launchctl kickstart -k system/com.apple.metadata.mds \
  || echo "  (mds will pick the change up on its next restart or at boot)"

# ── Verify, honestly ─────────────────────────────────────────────────────────

say "verifying"
echo "A path reading 0 proves nothing on its own — during a rebuild EVERY path"
echo "reads 0. So this measures a control at the same moment."

_control=""
for c in "$HOME/Library" "$HOME/Documents" "/Applications"; do
  [[ -d "$c" ]] || continue
  n="$(mdfind -onlyin "$c" -count '*' 2>/dev/null || echo 0)"
  if [[ "${n:-0}" -gt 0 ]]; then
    _control="$c"
    _control_n="$n"
    break
  fi
done

if [[ -z "$_control" ]]; then
  echo "  INCONCLUSIVE — no control path is indexed either, so the index is"
  echo "  empty or still rebuilding. Re-run with --status once it has settled:"
  echo "  the change is applied, this just cannot be confirmed yet."
  exit 0
fi

echo "  control $_control: $_control_n indexed (so the index is live)"
_bad=0
for p in "${PATHS[@]}"; do
  n="$(mdfind -onlyin "$p" -count '*' 2>/dev/null || echo 0)"
  if ((REMOVE)); then
    printf '  %-44s %s indexed\n' "$p" "$n"
  elif [[ "${n:-0}" -eq 0 ]]; then
    printf '  %-44s EXCLUDED (0 indexed, against a live index)\n' "$p"
  else
    printf '  %-44s STILL INDEXED (%s items)\n' "$p" "$n"
    _bad=1
  fi
done

if ((_bad)); then
  echo ""
  echo "The edit did not take. mds owns that plist and can discard a change made"
  echo "behind its back. The remaining option for a CI-only host is to stop"
  echo "indexing the volume outright — it cannot silently revert:"
  echo "  sudo mdutil -i off -d /System/Volumes/Data"
  exit 1
fi
