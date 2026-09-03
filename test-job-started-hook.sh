#!/usr/bin/env bash
# Sandbox tests for hooks/job-started.sh.
#
# Two independent concerns share this hook, and the interesting assertions are
# about what it must NOT do — the hook runs before every job, so any way it can
# fail loudly is a way it can fail every job on the host:
#
#   inhibitor granted   -> reports the PID, and the inhibitor really is held
#   inhibitor refused   -> warns on stderr, still exits 0 (never fails a job)
#   inhibitor absent    -> silent, exits 0 (macOS and non-systemd hosts)
#   inhibitor lifetime  -> dies with the job worker, not with the hook
#   orbstack opted in   -> ensure-orbstack.sh --full is called
#   orbstack not opted  -> it is NOT called
#
# The refusal case matters most. A sleep inhibitor that is silently not taken
# looks identical to one that is, right up until a host suspends mid-job and
# the job dies hours later as an unauthorized completion with no logs.
#
# Usage: ./test-job-started-hook.sh   (exit 0 = all pass)

set -uo pipefail

HOOK="$(cd "$(dirname "$0")" && pwd)/hooks/job-started.sh"
FAILURES=0

# _sandbox <inhibit-mode> <orbstack|none>
# Builds a fake hook dir plus a stub PATH. inhibit-mode is one of:
#   grant  - a stub that blocks like the real one (records that it was called)
#   refuse - a stub that exits 1 immediately (polkit denial)
#   absent - no systemd-inhibit on PATH at all
_sandbox() {
  local inhibit=$1 rt=$2 sb
  sb="$(mktemp -d)"
  mkdir -p "$sb/bin" "$sb/hooks" "$sb/state"
  : >"$sb/state/orb-calls"
  : >"$sb/state/inhibit-args"

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
      chmod +x "$sb/bin/systemd-inhibit" ;;
    refuse)
      printf '#!/usr/bin/env bash\nexit 1\n' >"$sb/bin/systemd-inhibit"
      chmod +x "$sb/bin/systemd-inhibit" ;;
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

_assert() {
  local name=$1 cond=$2 detail=$3
  if eval "$cond"; then
    echo "PASS: $name — $detail"
  else
    echo "FAIL: $name — $detail"
    echo "      stdout was:"; sed 's/^/        /' "${SB:-/dev/null}/out" 2>/dev/null
    echo "      stderr was:"; sed 's/^/        /' "${SB:-/dev/null}/err" 2>/dev/null
    FAILURES=$((FAILURES + 1))
  fi
}

# 1. Granted: the hook reports the PID it took, and asked for the right thing.
SB=$(_sandbox grant none); _run "$SB"
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
SB=$(_sandbox refuse none); _run "$SB"
_assert refused "grep -q 'refused' $SB/err" "warns on stderr"
_assert refused "[[ \$(cat $SB/rc) == 0 ]]" "STILL exits 0 — a denial must not fail the job"

# 4. Absent (macOS / non-systemd): silent, and still exits 0.
SB=$(_sandbox absent none); _run "$SB"
_assert absent "! grep -q 'systemd-inhibit' $SB/out" "no inhibitor line"
_assert absent "! [[ -s $SB/err ]]" "no warning — absence is not an error"
_assert absent "[[ \$(cat $SB/rc) == 0 ]]" "exits 0"

# 5. OrbStack opt-in still dispatches (the behaviour-neutrality regression).
SB=$(_sandbox grant orbstack); _run "$SB"
_assert orbstack "grep -q -- '--full' $SB/state/orb-calls" "ensure-orbstack.sh --full called"

# 6. Not opted in: never called.
SB=$(_sandbox grant none); _run "$SB"
_assert no-orbstack "[[ ! -s $SB/state/orb-calls ]]" "ensure-orbstack.sh NOT called"

if [[ $FAILURES -gt 0 ]]; then
  echo "$FAILURES assertion(s) failed"; exit 1
fi
echo "all assertions passed"
