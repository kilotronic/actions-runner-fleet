#!/usr/bin/env bash
# render_template FILE KEY VALUE [KEY VALUE ...]
#
# Substitutes @KEY@ placeholders and writes the result to stdout. Shared by the
# installers and mirrored by apply.py's render_template(), so the service files
# have ONE source of truth.
#
# They used to be heredocs inside the installers, which made drift impossible to
# correct: install.sh and install-linux.sh skip an already-configured runner
# (`Already configured — skipping`), so a unit or plist written by an older kit
# was never rewritten — on any host, ever. A runner carrying the old systemd unit
# (Restart=on-failure, KillMode=process) silently failed to come back after a
# reboot, which is exactly what happened on one host. Convergence can only fix
# what it can render.
#
# Fails on an unsubstituted @PLACEHOLDER@ rather than emitting one: a service
# file with a literal @RUNNER_DIR@ in it is worse than no file, because launchd
# and systemd will happily load it and then never work.
render_template() {
  local file=$1
  shift
  [[ -r "$file" ]] || {
    echo "render_template: cannot read $file" >&2
    return 1
  }
  local out
  out="$(cat "$file")"
  while (($# >= 2)); do
    # Bash's ${var//pat/rep} avoids sed entirely, so a value containing /, &
    # or a newline needs no escaping — runner dirs are absolute paths.
    out="${out//@$1@/$2}"
    shift 2
  done
  (($# == 0)) || {
    echo "render_template: odd number of key/value arguments" >&2
    return 1
  }
  if [[ "$out" =~ @[A-Z_][A-Z0-9_]*@ ]]; then
    echo "render_template: $file still has ${BASH_REMATCH[0]} after substitution" >&2
    return 1
  fi
  printf '%s\n' "$out"
}
