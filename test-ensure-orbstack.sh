#!/usr/bin/env bash
# Sandbox tests for hooks/ensure-orbstack.sh's --watchdog mode.
#
# The --full mode is covered end-to-end through the job hook by
# test-job-started-hook.sh (which is also the regression test proving the
# extraction from job-started.sh was behaviour-neutral). This file covers the
# unattended path, where the interesting question is not "does it recover" but
# "does it correctly REFUSE to recover":
#
#   healthy           -> no orb calls at all
#   cleanly-down      -> start only, never stop
#   wedged+busy       -> stands down; a running job is never interrupted
#   wedged+slow       -> the patient re-probe answers: SLOW, not wedged, no restart
#   wedged+confirmed  -> idle host, all three probes hang: stop + start
#   busy-mid-backoff  -> idle at the first gate, busy by the second: no stop
#   lock-held         -> bails immediately rather than waiting out the holder
#
# Probe boxes are shrunk via ORB_PROBE_* / ORB_PATIENCE_* so the suite runs in
# seconds instead of waiting out real 10s/25s/60s timeouts.
#
# Usage: ./test-ensure-orbstack.sh   (exit 0 = all pass)

set -uo pipefail

ENSURE="$(cd "$(dirname "$0")" && pwd)/hooks/ensure-orbstack.sh"
FAILURES=0

# Fast timings. The backoff (3s) is deliberately the widest window: scenarios 4
# and 6 both need to change state DURING it, after the two quick probes have
# already failed. Timeline for a wedged sandbox, in seconds from launch:
#   0-1 fast probe (hangs) | 1-2 slow probe (hangs) | 2 first busy gate
#   2-5 backoff            | 5-7 patient probe     | 7 second busy gate
export ORB_PROBE_FAST=1 ORB_PROBE_SLOW=1 ORB_PATIENCE_DELAY=3 ORB_PATIENCE_PROBE=2

# _sandbox <docker-mode> <orb-status> <socket|nosocket> <after-start-mode>
# Builds a fake $HOME with stub docker/orb driven by files under state/, and
# echoes the sandbox path. The docker stub re-reads its mode on every call, so
# a background process can flip behaviour mid-run (used by the slow and
# busy-mid-backoff scenarios).
_sandbox() {
  local docker_mode=$1 orb_status=$2 make_socket=$3 after_start=$4
  local sb state
  sb="$(mktemp -d)"
  state="$sb/state"
  mkdir -p "$state" "$sb/home/.orbstack/bin" "$sb/home/.orbstack/run" "$sb/bin"
  echo "$docker_mode" >"$state/docker-mode"
  echo "$orb_status" >"$state/orb-status"
  echo "$after_start" >"$state/after-start-mode"
  : >"$state/orb-calls"

  cat >"$sb/home/.orbstack/bin/docker" <<EOF
#!/usr/bin/env bash
mode="\$(cat "$state/docker-mode")"
case "\$mode" in
  ok)    exit 0 ;;
  down)  exit 1 ;;
  wedge) sleep 600 ;;
esac
EOF
  cat >"$sb/home/.orbstack/bin/orb" <<EOF
#!/usr/bin/env bash
case "\$1" in
  status) cat "$state/orb-status" ;;
  stop)   echo stop >> "$state/orb-calls" ;;
  start)  echo start >> "$state/orb-calls"; cat "$state/after-start-mode" > "$state/docker-mode" ;;
esac
EOF
  chmod +x "$sb/home/.orbstack/bin/docker" "$sb/home/.orbstack/bin/orb"

  if [[ "$make_socket" == socket ]]; then
    python3 - "$sb/home/.orbstack/run/docker.sock" <<'PY'
import socket, sys
s = socket.socket(socket.AF_UNIX)
s.bind(sys.argv[1])
PY
  fi

  printf '#!/usr/bin/env bash\nexit 0\n' >"$sb/bin/open"
  chmod +x "$sb/bin/open"
  echo "$sb"
}

# _run <sandbox> — invoke the watchdog against a sandbox. The busy gate is
# driven by the presence of state/busy (exit 0 = busy), standing in for
# runner_fleet.py without needing real runner directories.
#
# The wake clock is pinned to epoch 1 ("woke long ago, has not slept since") so
# every pre-existing scenario stays deterministic on a host that really is
# sleep-cycling — without this, a Mac that woke in the last two minutes would
# make every case defer at the wake-settle gate. Scenarios that care about
# sleep set WAKE_OVERRIDE to a command reading a mutable state file.
_run() {
  local sb=$1
  HOME="$sb/home" PATH="$sb/bin:$PATH" \
    ORB_RECOVERY_LOCK="${LOCK_OVERRIDE:-$sb/lock.d}" \
    ORB_BUSY_CHECK="test -f '$sb/state/busy'" \
    ORB_WAKE_CMD="${WAKE_OVERRIDE:-echo 1}" \
    bash -e -o pipefail "$ENSURE" --watchdog >"$sb/out" 2>&1 || true
}

_assert() {
  local name=$1 cond=$2 detail=$3
  if eval "$cond"; then
    echo "PASS: $name — $detail"
  else
    echo "FAIL: $name — $detail"
    echo "      output was:"
    sed 's/^/        /' "${SB:-/dev/null}/out" 2>/dev/null
    FAILURES=$((FAILURES + 1))
  fi
}

# 1. Healthy: nothing happens.
SB=$(_sandbox ok Running socket ok)
_run "$SB"
_assert healthy "grep -q 'container runtime is running' $SB/out" "reports running"
_assert healthy "[[ ! -s $SB/state/orb-calls ]]" "no orb calls"

# 2. Cleanly down: start, never stop — nothing was running to kill, so this
#    needs no busy gate and no patience.
SB=$(_sandbox down Stopped nosocket ok)
_run "$SB"
_assert cleanly-down "grep -q 'cleanly down' $SB/out" "takes the down branch"
_assert cleanly-down "grep -q '^start$' $SB/state/orb-calls" "orb start called"
_assert cleanly-down "! grep -q '^stop$' $SB/state/orb-calls" "orb stop NEVER called"

# 3. Wedged but a job is running: stand down entirely. This is the assertion
#    that keeps an unattended timer from ever interrupting CI.
SB=$(_sandbox wedge Running socket ok)
touch "$SB/state/busy"
_run "$SB"
_assert wedged-busy "grep -q 'a runner is BUSY' $SB/out" "reports standing down for a busy runner"
_assert wedged-busy "[[ ! -s $SB/state/orb-calls ]]" "neither stop nor start"

# 4. Looks wedged to both quick probes, but the patient probe answers: this is
#    a loaded box, not a wedge. The whole point of the timer's patience.
SB=$(_sandbox wedge Running socket ok)
# Heal at t=3.5s: inside the backoff, so both quick probes have already hung
# and only the patient probe gets to see a responsive daemon.
(
  sleep 3.5
  echo ok >"$SB/state/docker-mode"
) &
FLIP=$!
_run "$SB"
wait "$FLIP" 2>/dev/null || true
_assert wedged-slow "grep -q 'SLOW, not wedged' $SB/out" "patient probe reclassifies as slow"
_assert wedged-slow "[[ ! -s $SB/state/orb-calls ]]" "no restart of a merely-slow daemon"

# 5. Idle host, all three probes hang: a confirmed wedge, restart it.
SB=$(_sandbox wedge Running socket ok)
_run "$SB"
_assert wedged-confirmed "grep -q 'wedge CONFIRMED' $SB/out" "confirms the wedge"
_assert wedged-confirmed "grep -q '^stop$' $SB/state/orb-calls" "orb stop called"
_assert wedged-confirmed "grep -q '^start$' $SB/state/orb-calls" "orb start called"

# 6. Idle at the first gate, but a job lands during the backoff. The stop must
#    not fire — this is the stale-snapshot race apply.py hit in cd0f7f4.
SB=$(_sandbox wedge Running socket ok)
# Go busy at t=3.5s: after the first gate cleared at t=2, so only the second
# gate — the one guarding the stop itself — can catch it.
(
  sleep 3.5
  touch "$SB/state/busy"
) &
FLIP=$!
_run "$SB"
wait "$FLIP" 2>/dev/null || true
_assert busy-mid-backoff "grep -q 'arrived during' $SB/out" "notices the job that landed mid-backoff"
_assert busy-mid-backoff "! grep -q '^stop$' $SB/state/orb-calls" "orb stop NEVER called"

# 7. Another caller holds a FRESH lock: bail immediately, do not wait it out.
#    A timer has a next tick in 300s; blocking here would pile ticks up.
SB=$(_sandbox wedge Running socket ok)
mkdir -p "$SB/held.d"
LOCK_OVERRIDE="$SB/held.d"
START=$(date +%s)
_run "$SB"
ELAPSED=$(($(date +%s) - START))
unset LOCK_OVERRIDE
_assert lock-held "grep -q 'standing down, retrying next tick' $SB/out" "reports standing down"
_assert lock-held "[[ ! -s $SB/state/orb-calls ]]" "no orb calls"
_assert lock-held "((ELAPSED < 10))" "returned immediately (${ELAPSED}s), did not wait out the holder"

# 8. The host SLEPT during the probe window. All three probes hang — identical
#    to scenario 5 from the daemon's point of view — but the reason they hung
#    is that the VM was suspended, not that it is wedged. This is the case that
#    produced 91 spurious restarts on j-mini (25/25 of the ones pmset still
#    covered fell within 5 min of a sleep/wake transition).
SB=$(_sandbox wedge Running socket ok)
echo 1 >"$SB/state/wake"
# Wake at t=3.5s: inside the backoff, after the wake-settle gate has already
# cleared, so only the post-probe sleep check can catch it.
(
  sleep 3.5
  date +%s >"$SB/state/wake"
) &
FLIP=$!
WAKE_OVERRIDE="cat '$SB/state/wake'"
_run "$SB"
unset WAKE_OVERRIDE
wait "$FLIP" 2>/dev/null || true
_assert slept-during-probe "grep -q 'host SLEPT during the probe window' $SB/out" "attributes the hung probes to sleep"
_assert slept-during-probe "! grep -q 'wedge CONFIRMED' $SB/out" "does NOT confirm a wedge"
_assert slept-during-probe "[[ ! -s $SB/state/orb-calls ]]" "neither stop nor start"

# 9. The host woke seconds ago: too unsettled to probe at all. Wake is when
#    every launchd agent that missed its interval fires at once, so a timeout
#    probe there is meaningless. Nothing waits on the timer; defer a tick.
SB=$(_sandbox wedge Running socket ok)
WAKE_OVERRIDE="date +%s"
_run "$SB"
unset WAKE_OVERRIDE
_assert woke-recently "grep -q 'too unsettled to trust a timeout probe' $SB/out" "defers instead of probing"
_assert woke-recently "[[ ! -s $SB/state/orb-calls ]]" "no orb calls"

# 10. Circuit breaker: a genuine-looking wedge, but the box has already been
#     restarted its allowance in the window. OrbStack does not wedge several
#     times a day — past the cap the detector is the suspect, so stand down
#     loudly rather than cycling every container on a timer.
SB=$(_sandbox wedge Running socket ok)
mkdir -p "$SB/home/actions-runner/logs"
{
  date +%s
  date +%s
} >"$SB/home/actions-runner/logs/orbstack-restarts.state"
_run "$SB"
_assert breaker "grep -q 'REFUSING to restart again' $SB/out" "trips the breaker"
_assert breaker "[[ ! -s $SB/state/orb-calls ]]" "no orb calls once tripped"

# 11. The breaker only counts restarts INSIDE its window — stale entries from
#     days ago must not wedge the recovery path shut forever.
SB=$(_sandbox wedge Running socket ok)
mkdir -p "$SB/home/actions-runner/logs"
{
  echo $(($(date +%s) - 200000))
  echo $(($(date +%s) - 190000))
} >"$SB/home/actions-runner/logs/orbstack-restarts.state"
_run "$SB"
_assert breaker-expiry "grep -q 'wedge CONFIRMED' $SB/out" "stale restarts do not count"
_assert breaker-expiry "grep -q '^stop$' $SB/state/orb-calls" "still recovers a real wedge"

echo
if ((FAILURES > 0)); then
  echo "$FAILURES assertion(s) FAILED"
  exit 1
fi
echo "all scenarios passed"
