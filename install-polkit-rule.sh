#!/usr/bin/env bash
# Install the logind sleep-inhibitor polkit rule for the runner user.
#
# OPT-IN, and deliberately not run by install-linux.sh: this is a privileged
# policy change, and a machine that never suspends does not need it. Run it once
# per Linux host that idle-suspends; update-host.sh keeps an already-installed
# rule in sync from the same template afterwards, but will never create one.
#
# Why it is needed: polkit grants inhibit-block-* only to processes in an active
# login session, and the runner service is not one. Without the rule the job
# hooks' inhibitors are refused and the host can suspend mid-job — which surfaces
# as a job that dies with no logs, not as anything that looks power-related.
# Checking over SSH is misleading: SSH opens a session, so the inhibitor is
# granted while you look. Read a job log for `sleep inhibitor was refused`.
#
# Usage:
#   ./install-polkit-rule.sh                # install for the current user
#   ./install-polkit-rule.sh --user ci      # install for another user
#   ./install-polkit-rule.sh --dry-run      # print the rendered rule, change nothing
#   ./install-polkit-rule.sh --uninstall    # remove the installed rule

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TEMPLATE="${POLKIT_TEMPLATE:-$SCRIPT_DIR/polkit/49-actions-runner-inhibit.rules.in}"
DEST="${POLKIT_DEST:-/etc/polkit-1/rules.d/49-actions-runner-inhibit.rules}"
RUNNER_USER="${RUNNER_USER:-$(id -un)}"
DRY_RUN=0
UNINSTALL=0

while (($#)); do
  case "$1" in
    --user)
      RUNNER_USER="${2:?--user needs a username}"
      shift 2
      ;;
    --dry-run)
      DRY_RUN=1
      shift
      ;;
    --uninstall)
      UNINSTALL=1
      shift
      ;;
    -h | --help)
      sed -n '2,21p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *)
      echo "unknown argument: $1" >&2
      exit 2
      ;;
  esac
done

# The Linux guard sits on the MUTATING paths only, not here: rendering and
# validating are pure, and being able to preview the exact rule from any machine
# is worth more than a tidy early exit.
_require_linux() {
  [[ "$(uname -s)" == "Linux" ]] || {
    echo "not Linux — logind/polkit do not apply here"
    exit 0
  }
}

# The username is interpolated into a JavaScript string literal, so it is
# validated rather than trusted: anything outside a conservative POSIX username
# shape could close the quote and change what the rule grants. Refusing is the
# only safe answer — there is no escaping that is obviously correct in polkit's
# JS dialect.
[[ "$RUNNER_USER" =~ ^[a-z_][a-z0-9_-]{0,31}$ ]] || {
  echo "refusing: '$RUNNER_USER' is not a plain POSIX username" >&2
  echo "the name is interpolated into the rule's JavaScript, so it must be simple" >&2
  exit 2
}

if ((UNINSTALL)); then
  ((DRY_RUN)) || _require_linux
  if [[ ! -e "$DEST" ]]; then
    echo "nothing to remove ($DEST is absent)"
    exit 0
  fi
  ((DRY_RUN)) && {
    echo "would remove $DEST"
    exit 0
  }
  sudo rm -f "$DEST"
  echo "removed $DEST"
  exit 0
fi

[[ -f "$TEMPLATE" ]] || {
  echo "template not found: $TEMPLATE" >&2
  exit 1
}

rendered="$(sed "s/@RUNNER_USER@/$RUNNER_USER/g" "$TEMPLATE")"

# A template that still carries the placeholder would install a rule granting
# nothing to a user named "@RUNNER_USER@" — silently useless, and it would look
# installed. Fail instead.
if grep -q '@RUNNER_USER@' <<<"$rendered"; then
  echo "refusing: the rendered rule still contains @RUNNER_USER@" >&2
  exit 1
fi

if ((DRY_RUN)); then
  echo "would install -> $DEST (user: $RUNNER_USER)"
  echo "---"
  printf '%s\n' "$rendered"
  exit 0
fi

_require_linux

if [[ -f "$DEST" ]] && diff -q <(printf '%s\n' "$rendered") "$DEST" >/dev/null 2>&1; then
  echo "already current: $DEST (user: $RUNNER_USER)"
  exit 0
fi

command -v pkaction >/dev/null 2>&1 || command -v pkexec >/dev/null 2>&1 || {
  echo "warning: polkit does not appear to be installed; the rule will sit unused" >&2
  echo "         (Debian/Ubuntu: sudo apt-get install -y polkitd)" >&2
}

printf '%s\n' "$rendered" | sudo install -m 644 -o root -g root /dev/stdin "$DEST"
echo "installed $DEST (user: $RUNNER_USER)"
echo "polkit reads rules.d immediately — the next job's hooks hold their inhibitors."
