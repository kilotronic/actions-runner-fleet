#!/usr/bin/env bash
# Sidecar dispatch, shared by job-started.sh and job-completed.sh.
#
# Sidecars are small single-purpose scripts that both job hooks invoke with the
# event name ("started" / "completed"). They live here, defined once, because
# the two hooks MUST agree on the list: a sidecar registered on start and not on
# completion leaks whatever it opened. Sourced rather than duplicated so that
# agreement is structural instead of a comment asking two files to stay in step.
#
# The list is explicit rather than a glob of hooks/*.sh: dropping a file into
# hooks/ should not silently change what runs before every job of every repo on
# the fleet, and the hook entry points themselves live in that same directory.
#
# The cost of a list is that it can fall out of step with hooks/, and it did:
# an earlier rewrite of the job hooks dropped the calls while leaving the
# scripts installed on every box, so the env sampler stopped recording for two
# days with no error anywhere — the log simply stopped growing. Nothing detects
# a sidecar that is installed but uncalled, so test-job-started-hook.sh asserts
# that every name below is actually invoked.
#
# Usage:  source "$HOOKS/_sidecars.sh"; run_sidecars started

SIDECARS=(log-job-env.sh standup-presence.sh)

# Best-effort by contract: each sidecar is `|| true` with its output discarded.
# A diagnostic that can fail a job is worse than no diagnostic at all, and these
# run before every job on the host — any way they can fail loudly is a way to
# fail everything. Always returns 0.
run_sidecars() {
  local event=$1 dir="${SIDECAR_DIR:-$HOOKS}" s
  for s in "${SIDECARS[@]}"; do
    [[ -x "$dir/$s" ]] && { "$dir/$s" "$event" >/dev/null 2>&1 || true; }
  done
  return 0
}
