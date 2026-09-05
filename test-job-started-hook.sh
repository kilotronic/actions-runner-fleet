#!/usr/bin/env bash
# Sandbox tests for hooks/job-started.sh (plus the sidecar half of
# hooks/job-completed.sh, which must stay paired with it).
#
# Three independent concerns share these hooks, and the interesting assertions
# are about what they must NOT do — they run around every job, so any way they
# can fail loudly is a way to fail every job on the host:
#
#   inhibitor granted   -> reports the PID, and the inhibitor really is held
#   inhibitor refused   -> warns on stderr, still exits 0 (never fails a job)
#   inhibitor absent    -> silent, exits 0 (macOS and non-systemd hosts)
#   inhibitor lifetime  -> dies with the job worker, not with the hook
#   orbstack opted in   -> ensure-orbstack.sh --full is called
#   orbstack not opted  -> it is NOT called
#   sidecars wired      -> every name in SIDECARS is really invoked, on BOTH
#                          hooks, with the right event
#   sidecar broken      -> a failing or missing sidecar changes nothing
#
# Two failure shapes drive this file, both of which have actually happened here
# and both of which are SILENT:
#
#   1. A sleep inhibitor that is not taken looks identical to one that is,
#      right up until a host suspends mid-job and the job dies hours later as
#      an unauthorized completion with no logs.
#   2. A sidecar that is installed but uncalled produces no error at all — a
#      rewrite of job-started.sh once dropped the calls while leaving the
#      scripts on every box, and the env sampler stopped recording for two days.
#      Nothing anywhere noticed; the log just stopped growing.
#
# The sidecar assertions therefore read the SIDECARS array out of
# hooks/_sidecars.sh rather than hardcoding names. A hardcoded copy here would
# reproduce exactly the drift the test exists to catch: adding a sidecar would
# leave it unasserted, which is the same failure one level up.
#
# Usage: ./test-job-started-hook.sh   (exit 0 = all pass)

set -uo pipefail

REPO="$(cd "$(dirname "$0")" && pwd)"
HOOK="$REPO/hooks/job-started.sh"
COMPLETED_HOOK="$REPO/hooks/job-completed.sh"
SIDECAR_LIB="$REPO/hooks/_sidecars.sh"
FAILURES=0

# The list under test, read from the source of truth.
# shellcheck source=hooks/_sidecars.sh
. "$SIDECAR_LIB"

# _sandbox <inhibit-mode> <orbstack|none> [sidecar-mode]
# Builds a fake hook dir plus a stub PATH. inhibit-mode is one of:
#   grant  - a stub that blocks like the real one (records that it was called)
#   refuse - a stub that exits 1 immediately (polkit denial)
#   absent - no systemd-inhibit on PATH at all
# sidecar-mode is one of:
#   stub   - (default) every SIDECARS entry records "<name> <event>" when run
#   broken - every entry exits 1 without recording (a sidecar that is failing)
#   none   - no sidecar scripts installed at all (a fresh or partial host)
_sandbox() {
  local inhibit=$1 rt=$2 sidecars=${3:-stub} sb s
  sb="$(mktemp -d)"
  mkdir -p "$sb/bin" "$sb/hooks" "$sb/state"
  : >"$sb/state/orb-calls"
  : >"$sb/state/inhibit-args"
  : >"$sb/state/sidecar-calls"

  # The real dispatch library, plus one stub per name it lists. Copying the
  # library (rather than stubbing it) is what makes the wiring assertions real:
  # the hook resolves the list exactly as it does in production.
  cp "$SIDECAR_LIB" "$sb/hooks/_sidecars.sh"
  if [[ "$sidecars" != none ]]; then
    for s in "${SIDECARS[@]}"; do
      if [[ "$sidecars" == broken ]]; then
        printf '#!/usr/bin/env bash\nexit 1\n' >"$sb/hooks/$s"
      else
        cat >"$sb/hooks/$s" <<EOS
#!/usr/bin/env bash
echo "$s \$1" >> "$sb/state/sidecar-calls"
EOS
      fi
      chmod +x "$sb/hooks/$s"
    done
  fi

  # Hermetic PATH: link in exactly the externals the hook uses, so `absent`
  # really is absent. Linking the whole of /usr/bin back in would drag the real
  # systemd-inhibit along and quietly turn that scenario into a second copy of
  # the grant scenario — which is how this test failed the first time it ran.
  # bash and env are themselves PATH-resolved: the hook is run via `bash` and
  # the stubs carry `#!/usr/bin/env bash` shebangs.
  for tool in bash env dirname sleep tail; do
    ln -sf "$(command -v "$tool")" "$sb/bin/$tool"
  done

  cp "$HOOK" "$sb/hooks/job-started.sh"
  cp "$COMPLETED_HOOK" "$sb/hooks/job-completed.sh"
  cat >"$sb/hooks/ensure-orbstack.sh" <<EOS
#!/usr/bin/env bash
echo "\$@" >> "$sb/state/orb-calls"
EOS
  chmod +x "$sb/hooks/ensure-orbstack.sh"

  case "$inhibit" in
    grant)
      cat >"$sb/bin/systemd-inhibit" <<EOS
#!/usr/bin/env bash
echo "\$@" >> "$sb/state/inhibit-args"
# Behave like the real thing: exec the wrapped command so the stub's lifetime
# is the wrapped command's lifetime. That is what makes the lifetime assertion
# below meaningful rather than a tautology.
while [ \$# -gt 0 ] && [ "\${1#--}" != "\$1" ]; do shift; done
exec "\$@"
EOS
      chmod +x "$sb/bin/systemd-inhibit"
      ;;
    refuse)
      printf '#!/usr/bin/env bash\nexit 1\n' >"$sb/bin/systemd-inhibit"
      chmod +x "$sb/bin/systemd-inhibit"
      ;;
    absent) : ;;
  esac

  [[ "$rt" == orbstack ]] && echo orbstack >"$sb/state/rt"
  echo "$sb"
}

# _run <sandbox> — invoke the hook against the sandbox bin ALONE. The PATH is
# identical in every scenario, so `absent` differs from `grant` only by the
# stub's presence, which is the whole point of the assertion.
_run() {
  local sb=$1 rt=""
  [[ -f "$sb/state/rt" ]] && rt=orbstack
  CONTAINER_RUNTIME="$rt" PATH="$sb/bin" \
    bash "$sb/hooks/job-started.sh" >"$sb/out" 2>"$sb/err"
  echo $? >"$sb/rc"
}

# _run_completed <sandbox> — the same for job-completed.sh.
#
# HOME is redirected into the sandbox on purpose: that hook reads
# $HOME/actions-runner/.repo-path and, on a real box, would background the
# actual update-host.sh. A test that converges the developer's own fleet is not
# a test. The sandbox has no .repo-path, so the updater branch is skipped.
_run_completed() {
  local sb=$1
  HOME="$sb" PATH="$sb/bin" \
    bash "$sb/hooks/job-completed.sh" >"$sb/out" 2>"$sb/err"
  echo $? >"$sb/rc"
}

_assert() {
  local name=$1 cond=$2 detail=$3
  if eval "$cond"; then
    echo "PASS: $name — $detail"
  else
    echo "FAIL: $name — $detail"
    echo "      stdout was:"
    sed 's/^/        /' "${SB:-/dev/null}/out" 2>/dev/null
    echo "      stderr was:"
    sed 's/^/        /' "${SB:-/dev/null}/err" 2>/dev/null
    FAILURES=$((FAILURES + 1))
  fi
}

# 1. Granted: the hook reports the PID it took, and asked for the right thing.
SB=$(_sandbox grant none)
_run "$SB"
_assert granted "grep -q 'blocking idle-suspend for job worker' $SB/out" "reports the inhibitor"
_assert granted "grep -q -- '--what=sleep' $SB/state/inhibit-args" "asks for what=sleep"
_assert granted "grep -q -- '--mode=block' $SB/state/inhibit-args" "asks for mode=block (not delay)"
_assert granted "[[ \$(cat $SB/rc) == 0 ]]" "exits 0"
_assert granted "! [[ -s $SB/err ]]" "says nothing on stderr"

# 2. Lifetime: the inhibitor must outlive the hook (it is tied to the worker
#    via `tail --pid`), otherwise it would be released the instant the hook
#    returns and the whole mechanism would be decorative.
_assert granted "grep -q 'tail' $SB/state/inhibit-args" "wraps tail --pid, not the hook itself"
_assert granted "grep -q -- '--pid' $SB/state/inhibit-args" "ties lifetime to a pid"

# 3. Refused (polkit denial): warn, but never fail the job.
SB=$(_sandbox refuse none)
_run "$SB"
_assert refused "grep -q 'refused' $SB/err" "warns on stderr"
_assert refused "[[ \$(cat $SB/rc) == 0 ]]" "STILL exits 0 — a denial must not fail the job"

# 4. Absent (macOS / non-systemd): silent, and still exits 0.
SB=$(_sandbox absent none)
_run "$SB"
_assert absent "! grep -q 'systemd-inhibit' $SB/out" "no inhibitor line"
_assert absent "! [[ -s $SB/err ]]" "no warning — absence is not an error"
_assert absent "[[ \$(cat $SB/rc) == 0 ]]" "exits 0"

# 5. OrbStack opt-in still dispatches (the behaviour-neutrality regression).
SB=$(_sandbox grant orbstack)
_run "$SB"
_assert orbstack "grep -q -- '--full' $SB/state/orb-calls" "ensure-orbstack.sh --full called"

# 6. Not opted in: never called.
SB=$(_sandbox grant none)
_run "$SB"
_assert no-orbstack "[[ ! -s $SB/state/orb-calls ]]" "ensure-orbstack.sh NOT called"

# 7. Every sidecar is really invoked by job-started.sh, with event "started".
#    Derived from SIDECARS, so a new entry is asserted the moment it is added —
#    the whole point, since an uncalled sidecar reports nothing at all.
SB=$(_sandbox grant none)
_run "$SB"
for s in "${SIDECARS[@]}"; do
  _assert sidecars-started "grep -qx '$s started' $SB/state/sidecar-calls" "$s invoked with 'started'"
done

# 8. And by job-completed.sh, with event "completed". The pair matters: a
#    sidecar that registers on start and never deregisters leaks its session.
SB=$(_sandbox grant none)
_run_completed "$SB"
for s in "${SIDECARS[@]}"; do
  _assert sidecars-completed "grep -qx '$s completed' $SB/state/sidecar-calls" "$s invoked with 'completed'"
done
_assert sidecars-completed "[[ \$(cat $SB/rc) == 0 ]]" "job-completed.sh exits 0"

# 9. Sampling happens BEFORE ensure-orbstack.sh: the snapshot describes the
#    state the job arrived into, not the state this hook left behind. Ordering
#    is observable because the orbstack stub appends to its own file — compare
#    mtimes rather than trusting the source order to stay put.
SB=$(_sandbox grant orbstack)
_run "$SB"
_assert sidecar-order \
  "[[ ! $SB/state/sidecar-calls -nt $SB/state/orb-calls ]]" \
  "sidecars ran before ensure-orbstack.sh"

# 10. A sidecar that FAILS must change nothing the job can see. This is the
#     contract that lets these stay best-effort: a broken diagnostic is a lost
#     diagnostic, never a lost job.
SB=$(_sandbox grant none broken)
_run "$SB"
_assert sidecar-broken "[[ \$(cat $SB/rc) == 0 ]]" "a failing sidecar still exits 0"
_assert sidecar-broken "! [[ -s $SB/err ]]" "a failing sidecar says nothing on stderr"
_assert sidecar-broken "grep -q 'blocking idle-suspend' $SB/out" "the inhibitor is still taken"

# 11. Sidecars absent entirely (a fresh or partially-provisioned host): silent
#     no-op. `[[ -x ]]` guards each call, and the hook must not `set -e` its way
#     out of the rest of its work on a missing file.
SB=$(_sandbox grant orbstack none)
_run "$SB"
_assert sidecar-absent "[[ \$(cat $SB/rc) == 0 ]]" "exits 0 with no sidecars installed"
_assert sidecar-absent "! [[ -s $SB/err ]]" "silent — absence is not an error"
_assert sidecar-absent "grep -q -- '--full' $SB/state/orb-calls" "later work still runs"

if [[ $FAILURES -gt 0 ]]; then
  echo "$FAILURES assertion(s) failed"
  exit 1
fi
echo "all assertions passed"
