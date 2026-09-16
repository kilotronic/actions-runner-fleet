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

# Every privileged step goes through this, so a failure names the step, the
# command, and what the tool actually said. The bare `install: No such file or
# directory` this replaced named none of the three, and two fixes were aimed at
# the wrong cause because of it.
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

# $DEST sits in a root-only directory on a stock polkit install
# (/etc/polkit-1/rules.d is 0750 root:polkitd), so `[[ -f "$DEST" ]]` run as the
# runner user is false whether the rule is there or not. Everything that asks
# about the destination must ask as root, or it silently answers "absent": this
# script would reinstall on every run, and update-host.sh's re-sync — gated on
# the same test — would never fire at all.
_dest_matches() { printf '%s\n' "$rendered" | sudo cmp -s - "$DEST" 2>/dev/null; }

if _dest_matches; then
  echo "already current: $DEST (user: $RUNNER_USER)"
  exit 0
fi

# rules.d is created by the polkit package, so its absence means polkit is not
# installed — and `install` will not create a missing parent, which is how this
# first failed: `install: No such file or directory`, naming neither the path
# nor the reason. Writing a rule nothing reads would be worse: a silent no-op
# that looks like success, which is the same shape as the bug this whole script
# exists to fix. So a missing polkit is refused with the command to fix it.
#
# POLKIT_PRESENT_CHECK overrides the probe for tests: exit 0 = present.
_polkit_present() {
  if [[ -n "${POLKIT_PRESENT_CHECK:-}" ]]; then
    eval "$POLKIT_PRESENT_CHECK"
    return $?
  fi
  command -v pkaction >/dev/null 2>&1 || command -v pkexec >/dev/null 2>&1 \
    || [[ -d /usr/share/polkit-1 ]]
}

POLKIT_DIR="$(dirname "$DEST")"
if [[ ! -d "$POLKIT_DIR" ]]; then
  if _polkit_present; then
    : # created below, alongside the other privileged steps
  else
    echo "refusing: polkit is not installed ($POLKIT_DIR does not exist)." >&2
    echo "A rule written there would be read by nothing. Install polkit first:" >&2
    echo "  Debian/Ubuntu:  sudo apt-get install -y polkitd" >&2
    echo "  Fedora/RHEL:    sudo dnf install -y polkit" >&2
    echo "  Arch:           sudo pacman -S --needed polkit" >&2
    echo "then re-run this script." >&2
    exit 1
  fi
elif ! _polkit_present; then
  echo "warning: $POLKIT_DIR exists but no polkit binary was found;" >&2
  echo "         the rule may sit unused until polkit is installed." >&2
fi

# Install from a real file, never `sudo install /dev/stdin`. That spelling is
# fragile in exactly the setups this runs in: sudoers with `Defaults use_pty`
# (now common on Debian/Ubuntu) gives the command a fresh pty for stdin, so
# /dev/stdin no longer names the pipe and `install` fails with a bare
# "No such file or directory" that names neither the path it meant nor why. A
# temp file has no such ambiguity, and the failure below can name the target.
_tmp_rule="$(mktemp)"
trap 'rm -f "$_tmp_rule"' EXIT
printf '%s\n' "$rendered" >"$_tmp_rule"

# Discrete steps, each reported on its own: one combined `install` call meant one
# bare message had to stand for a missing parent, an unwritable target, a sudo
# denial and an unresolvable owner.
_sudo_step "create $POLKIT_DIR" mkdir -p "$POLKIT_DIR" || exit 1
_sudo_step "copy the rule to $DEST" cp "$_tmp_rule" "$DEST" || exit 1
_sudo_step "set mode 0644 on $DEST" chmod 644 "$DEST" || exit 1
_sudo_step "set owner root:root on $DEST" chown root:root "$DEST" || exit 1

# Read it back as root. An install that reports success without the rule landing
# is the failure mode this whole script exists to remove.
if ! _dest_matches; then
  echo "FAILED: $DEST does not match the rendered rule after install" >&2
  exit 1
fi
echo "installed $DEST (user: $RUNNER_USER)"
echo "polkit reads rules.d immediately — the next job's hooks hold their inhibitors."
