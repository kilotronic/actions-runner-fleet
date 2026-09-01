#!/usr/bin/env bash
# Ensure the container runtime (OrbStack) is up, recovering it when it is not.
#
# Extracted from hooks/job-started.sh so two callers with very different risk
# profiles can share one state machine:
#
#   --full      hooks/job-started.sh, with a job already blocked and waiting.
#               Latency is the binding constraint: decide fast, act fast.
#   --watchdog  the orbstack-watchdog launchd timer, unattended, every 300s.
#               Nothing is waiting on it, so it can buy certainty with time.
#
# ── The four states ──────────────────────────────────────────────────────────
# Telling them apart is the whole job, because three of them look identical to
# any cheap liveness check (socket exists, `orb status` says Running):
#
#   healthy         `docker info` returns in ~40ms.            -> nothing
#   cleanly down    socket absent, or orb reports not Running. -> `orb start`
#                   Nothing is running, so a bare start destroys nothing.
#   slow but alive  `docker info` returns, just late (this box
#                   runs multi-agent test fleets at load 3-4+). -> nothing.
#                   Restarting a merely-slow daemon kills every healthy
#                   container on the box — see the lock comment below.
#   WEDGED          socket present AND accepting connections, `orb status`
#                   still claims Running, but `docker info` connects, writes,
#                   and then never gets a reply — no error, no EOF, forever.
#                   Only a stop+start clears it.
#
# Wedged is the post-sleep failure: orbstack/orbstack#1933 shows the host<->VM
# transport dying across a sleep transition ("write tcp 0.250.250.1:28550:
# endpoint is closed for send"). The guest daemon is fine; the pipe to it is
# dead, and the macOS-side socket stays bound and accepting, which is exactly
# why the state is invisible without a timeout probe. Not an OrbStack-specific
# bug — docker/for-mac#1046 and #6546 are the same class. Every macOS runtime
# is a Linux VM behind a host<->guest transport that sleep tears down.
#
# ── Why the timer is more patient than the hook, not less ────────────────────
# Wake is the WORST moment to run a timeout-based wedge test. Every launchd
# agent whose interval elapsed during sleep fires at once on wake (load
# watchdog at 60s, maintenance at 2h, dotfiles-update, backup-check, wip-sync,
# Syncthing, ...), plus Spotlight and iCloud catching up. Load spikes exactly
# when we would be probing, so a healthy daemon can blow through both boxes.
#
# The hook can't wait — a job is blocked on it. The timer can, and time is the
# one signal that cleanly separates the two states: slow resolves, wedged never
# does. So --watchdog adds a third, much longer probe after a backoff before it
# will conclude anything, on top of gating on an idle host.

set -uo pipefail

MODE="${1:---full}"
case "$MODE" in
  --full | --watchdog) ;;
  *)
    echo "usage: $(basename "$0") [--full|--watchdog]" >&2
    exit 2
    ;;
esac

DOCKER="$HOME/.orbstack/bin/docker"
[[ -x "$DOCKER" ]] || DOCKER="$(command -v docker || true)"
ORB="$HOME/.orbstack/bin/orb"

# Run a command with a hard <secs> timeout (macOS ships no timeout/gtimeout).
# Returns the command's exit status, or non-zero if it was killed for overrunning.
# A background killer (rather than a poll loop) keeps the common fast path — a
# healthy `docker info` that returns in ~40ms — free of added latency.
_timed() {
  local secs=$1 pid killer rc=0
  shift
  "$@" &
  pid=$!
  # The killer's output MUST be redirected away. A `$(_timed ...)` command
  # substitution waits for every process holding its pipe open, and this
  # subshell inherits it — so without the redirect the substitution blocks for
  # the full timeout however fast the command answered, and killing the
  # subshell doesn't help because its `sleep` child keeps the pipe alive.
  # _daemon_cleanly_down calls _timed exactly that way, so every wedged-path
  # recovery silently cost an extra 10s before this.
  {
    sleep "$secs"
    kill -9 "$pid" 2>/dev/null
  } >/dev/null 2>&1 &
  killer=$!
  wait "$pid" 2>/dev/null || rc=$?
  kill "$killer" 2>/dev/null
  wait "$killer" 2>/dev/null
  return "$rc"
}

# The three probe boxes. All overridable so the test suite can exercise every
# path in seconds instead of waiting out real 10s/25s/60s timeouts; nothing in
# production sets them.
_PROBE_FAST="${ORB_PROBE_FAST:-10}"
_PROBE_SLOW="${ORB_PROBE_SLOW:-25}"
_PATIENCE_DELAY="${ORB_PATIENCE_DELAY:-30}"
_PATIENCE_PROBE="${ORB_PATIENCE_PROBE:-60}"

# A wedged daemon makes `docker info` hang, so every probe is time-boxed.
_docker_ok() { _timed "$_PROBE_FAST" "$DOCKER" info &>/dev/null; }
# Second-chance probe: under heavy load a HEALTHY daemon can miss the fast box.
# Slow is not wedged — restarting a merely-slow daemon kills healthy containers.
_docker_ok_slow() { _timed "$_PROBE_SLOW" "$DOCKER" info &>/dev/null; }
# Third-chance probe, --watchdog only. See the patience rationale in the header.
_docker_ok_patient() { _timed "$_PATIENCE_PROBE" "$DOCKER" info &>/dev/null; }

# ── Sleep awareness ─────────────────────────────────────────────────────────
# A timeout-based liveness probe CANNOT distinguish "the daemon is hung" from
# "the host was asleep for the whole probe" — both look identical from here: a
# command that never answered. Every probe box above is a wall-clock `sleep`,
# and wall clock keeps running across a system sleep, so a suspended VM blows
# through all three boxes while the daemon is perfectly healthy.
#
# This is not hypothetical. j-mini idle-sleeps (`pmset sleep 15`, `powernap 1`,
# `tcpkeepalive 1`) and then DarkWakes every ~45s to service network, because
# the runner's own long-poll to GitHub keeps waking it. Its watchdog ticks land
# in those windows constantly. Measured 2026-08-08 over the window `pmset -g
# log` still covered: **25 of 25** `wedge CONFIRMED` restarts fell within five
# minutes of a sleep/wake transition, median under one minute — every one of
# them a false positive, and each one cycled every container on the box (the
# `Exited (255)` postgres pile in `docker ps -a` is their fingerprint).
#
# Worse, the --watchdog patience path AMPLIFIES this rather than damping it:
# `sleep 30` plus a 60s probe widens the window, making it *more* likely to
# span a sleep transition, and the `_any_runner_busy` idle gate actively
# SELECTS for the case, because a sleeping box is by definition idle.
#
# `kern.waketime` is the cheap primitive that separates the two states: it is
# the epoch of the host's last wake. If the host woke DURING a probe, that
# probe told us nothing about the daemon and its verdict must be discarded.
# Linux has no such sysctl, so `_wake_epoch` returns empty there and every
# check below degrades to "did not sleep" — i.e. exactly today's behaviour.
# ORB_WAKE_CMD overrides the clock for tests: stdout is the last-wake epoch, or
# empty for "no such concept here". A command rather than a plain value so a
# test can move the clock MID-RUN, the way ORB_BUSY_CHECK lets the busy gate
# flip during the backoff — the interesting case is precisely a host that was
# settled at entry and slept during the probe window.
_wake_epoch() {
  if [[ -n "${ORB_WAKE_CMD:-}" ]]; then
    eval "$ORB_WAKE_CMD" 2>/dev/null
    return 0
  fi
  # `{ sec = 1786204952, usec = 383606 }` — take the FIRST integer. A greedy
  # `.*sec = ` matches through to "usec = " and silently yields the microseconds
  # field instead, which reads as an epoch in 1970 and quietly disables every
  # check here. Caught only by running it against a live host; the injected
  # ORB_WAKE_CMD used by the tests bypasses this line entirely.
  sysctl -n kern.waketime 2>/dev/null | sed -n 's/^[^0-9]*\([0-9][0-9]*\).*/\1/p'
}

# True if the host woke at or after <epoch> — i.e. it slept during the window
# starting at <epoch>. Empty waketime (Linux, or a sysctl that failed) is
# always false, never a guess.
_slept_since() {
  local since=$1 woke
  woke="$(_wake_epoch)"
  [[ -z "$woke" ]] && return 1
  ((woke >= since))
}

# Skip a tick entirely when the host woke moments ago. Wake is the worst moment
# to run a timeout probe: every launchd agent whose interval elapsed during
# sleep fires at once, plus Spotlight and iCloud catching up. Nothing is waiting
# on the timer, so deferring one tick costs nothing and avoids probing an
# unsettled system.
_WAKE_GRACE="${ORB_WAKE_GRACE:-120}"
_woke_recently() {
  local woke now
  woke="$(_wake_epoch)"
  [[ -z "$woke" ]] && return 1
  now="$(date +%s)"
  ((now - woke < _WAKE_GRACE))
}

# ── Restart-rate circuit breaker ────────────────────────────────────────────
# OrbStack does not genuinely wedge several times a day. A burst of confirmed
# wedges means the DETECTOR is wrong (see the sleep note above: 91 restarts in
# 20 days, all of them spurious), and continuing to "recover" cycles every
# container on the box on a timer. Past the cap, log loudly and stand down —
# a stuck runtime that a human must look at is a far better failure mode than
# an automation quietly restarting the world every few hours.
_BREAKER_MAX="${ORB_BREAKER_MAX:-2}"
_BREAKER_WINDOW="${ORB_BREAKER_WINDOW:-86400}"
_BREAKER_FILE="${ORB_BREAKER_FILE:-$HOME/actions-runner/logs/orbstack-restarts.state}"
# Recent restart epochs, newest last, pruned to the window.
_breaker_recent() {
  local now cutoff
  now="$(date +%s)"
  cutoff=$((now - _BREAKER_WINDOW))
  [[ -f "$_BREAKER_FILE" ]] || return 0
  while read -r ts; do
    [[ "$ts" =~ ^[0-9]+$ ]] && ((ts >= cutoff)) && echo "$ts"
  done <"$_BREAKER_FILE"
}
_breaker_tripped() {
  local n
  n="$(_breaker_recent | wc -l | tr -d ' ')"
  ((n >= _BREAKER_MAX))
}
_breaker_record() {
  mkdir -p "$(dirname "$_BREAKER_FILE")" 2>/dev/null || true
  {
    _breaker_recent
    date +%s
  } >"$_BREAKER_FILE.tmp" 2>/dev/null && mv "$_BREAKER_FILE.tmp" "$_BREAKER_FILE"
}

# Cleanly DOWN (vs wedged): the docker socket does not exist, or orb itself
# reports the machine stopped. Down means nothing is running, so a bare
# `orb start` is always safe — no stop, nothing to kill. (A WEDGED daemon
# looks different: socket present, connects accepted, replies never come, and
# `orb status` can still claim Running — observed 2026-07-15.)
_daemon_cleanly_down() {
  [[ ! -S "$HOME/.orbstack/run/docker.sock" ]] && return 0
  if [[ -x "$ORB" ]]; then
    local st
    st="$(_timed 10 "$ORB" status 2>/dev/null || true)"
    [[ "$st" != "Running" ]] && return 0
  fi
  return 1
}

# ── Busy gate (--watchdog only) ──────────────────────────────────────────────
# An unattended `orb stop` must never land while a job is running. The fleet's
# one busy signal is process presence (runner_fleet.is_busy: a live
# Runner.Worker under the runner's directory), and its unanchored-substring
# match OVER-reports busy — which fails toward deferring, the direction we
# want. Every failure here (no runner_fleet.py, no python3, a crash) also
# reports busy, so the watchdog degrades into doing nothing rather than into
# restarting blind.
#
# ORB_BUSY_CHECK overrides the command for tests: exit 0 = busy, non-zero = idle.
#
# Path note: this resolves runner_fleet.py as a sibling of the CHECKOUT (../),
# which is where the timer invokes this script from. The copy of this file that
# apply.py --sync-config installs into ~/actions-runner/hooks/ has no such
# sibling — but that copy is only ever invoked as --full, which never gates.
_any_runner_busy() {
  if [[ -n "${ORB_BUSY_CHECK:-}" ]]; then
    eval "$ORB_BUSY_CHECK"
    return $?
  fi
  local fleet
  fleet="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." 2>/dev/null && pwd)/runner_fleet.py"
  if [[ ! -f "$fleet" ]] || ! command -v python3 &>/dev/null; then
    echo "warning: cannot reach runner_fleet.py ($fleet) — assuming BUSY and standing down"
    return 0
  fi
  python3 "$fleet" --any-busy 2>/dev/null
  return $?
}

# ── Recovery lock ────────────────────────────────────────────────────────────
# Serialize recovery across concurrent callers: this machine runs multiple
# runners, and two of them restarting OrbStack at once can kill each other's
# freshly-started containers (observed: one job's `orb stop` cycled the dev DB
# and CI Postgres out from under everything else on the box). mkdir is the
# atomic lock primitive macOS bash has; a stale lock (holder crashed) is
# stolen. stat is BSD-first with a GNU fallback for Linux runners.
_RECOVERY_LOCK="${ORB_RECOVERY_LOCK:-/tmp/orbstack-recovery.lock.d}"
# Staleness and wait budget are SEPARATE knobs. They used to share one value,
# which is fine at 180/180 but breaks the moment a caller wants to wait 0s:
# a zero wait budget would also declare every lock instantly stale and steal a
# live holder's lock. The watchdog wants exactly that zero wait (bail now,
# retry in 300s), so the stale threshold has to stand on its own.
_LOCK_STALE="${ORB_RECOVERY_LOCK_STALE:-180}"
if [[ "$MODE" == "--watchdog" ]]; then
  _LOCK_WAIT="${ORB_RECOVERY_LOCK_WAIT:-0}"
else
  _LOCK_WAIT="${ORB_RECOVERY_LOCK_WAIT:-180}"
fi
_lock_age() {
  local m
  m="$(stat -f %m "$_RECOVERY_LOCK" 2>/dev/null || stat -c %Y "$_RECOVERY_LOCK" 2>/dev/null || echo 0)"
  echo $(($(date +%s) - m))
}
_acquire_recovery_lock() {
  local waited=0
  while ! mkdir "$_RECOVERY_LOCK" 2>/dev/null; do
    if [[ -d "$_RECOVERY_LOCK" ]] && (($(_lock_age) > _LOCK_STALE)); then
      rmdir "$_RECOVERY_LOCK" 2>/dev/null || true
      continue
    fi
    ((waited >= _LOCK_WAIT)) && return 1
    sleep 2
    waited=$((waited + 2))
  done
  return 0
}
_release_recovery_lock() { rmdir "$_RECOVERY_LOCK" 2>/dev/null || true; }

# Wait up to 60s for the daemon to answer, reporting the outcome loudly.
_wait_docker_ready() {
  for _ in $(seq 1 60); do
    _docker_ok && break
    sleep 1
  done
  if _docker_ok; then
    echo "container runtime is running"
  else
    echo "warning: OrbStack did not become ready within 60s; container steps may fail"
  fi
}

_orb_start_only() {
  if [[ -x "$ORB" ]]; then _timed 60 "$ORB" start || true; else open -a OrbStack || true; fi
}

_orb_restart() {
  [[ -x "$ORB" ]] && { _timed 30 "$ORB" stop || true; }
  _orb_start_only
  _wait_docker_ready
}

# Wedged, and a job is waiting: restart immediately. Its containers are already
# unreachable, so the stop destroys nothing that works, and it is the only
# thing that clears a hung daemon.
_wedged_full() {
  echo "container runtime wedged (${_PROBE_FAST}s + ${_PROBE_SLOW}s probes hung) — restarting OrbStack..."
  _orb_restart
}

# Wedged, unattended: three gates before touching anything.
_wedged_watchdog() {
  if _woke_recently; then
    echo "host woke <${_WAKE_GRACE}s ago — too unsettled to trust a timeout probe; deferring to the next tick"
    return 0
  fi
  if _any_runner_busy; then
    echo "wedge suspected, but a runner is BUSY — standing down, the job hook will handle it"
    return 0
  fi
  echo "wedge suspected and host is idle — re-probing in ${_PATIENCE_DELAY}s with a ${_PATIENCE_PROBE}s box before acting"
  # Everything from here is timed against this mark, so a sleep anywhere in the
  # backoff OR the patient probe invalidates the verdict.
  local mark
  mark="$(date +%s)"
  sleep "$_PATIENCE_DELAY"
  if _docker_ok_patient; then
    echo "container runtime answered the patient probe — SLOW, not wedged; no restart"
    return 0
  fi
  if _slept_since "$mark"; then
    echo "host SLEPT during the probe window — the probes timed out because the VM was suspended, not because the daemon is wedged; no restart"
    return 0
  fi
  # A job can land during the backoff. apply.py learned this the hard way
  # (cd0f7f4: "fresh busy check before .env restart — snapshot went stale"),
  # so re-check immediately before the stop rather than trusting the snapshot.
  if _any_runner_busy; then
    echo "a job arrived during the ${_PATIENCE_DELAY}s backoff — standing down before the stop"
    return 0
  fi
  if _breaker_tripped; then
    echo "wedge CONFIRMED, but $(_breaker_recent | wc -l | tr -d ' ') restarts already in the last $((_BREAKER_WINDOW / 3600))h — REFUSING to restart again."
    echo "OrbStack does not wedge this often; suspect the detector or the host. Investigate by hand, then clear $_BREAKER_FILE."
    return 0
  fi
  echo "wedge CONFIRMED (${_PROBE_FAST}s + ${_PROBE_SLOW}s + ${_PATIENCE_PROBE}s probes all hung, host idle, no sleep during the window) — restarting OrbStack..."
  _breaker_record
  _orb_restart
  # Deliberately no container restoration. Restart policies handle it:
  # ci-postgres and awth are `unless-stopped` and come back on their own;
  # partygame's dev db is `restart=no` but is backed by the named volume
  # `postgres_data`, so `make db` restores it with no data loss.
  echo "note: containers with restart=no (e.g. partygame dev db) stay down until started; data on named volumes is intact"
}

# Every recovery step is `|| true`: a non-zero from orb stop/start (e.g.
# "OrbStack is not running", a timed-out start) was ABORTING the job with exit
# 143 mid-recovery (2026-07-15) instead of falling through to the readiness
# wait, which is the actual success criterion.
if [[ -z "$DOCKER" ]]; then
  echo "warning: no docker CLI found; cannot verify container runtime"
  exit 0
fi

if _docker_ok; then
  echo "container runtime is running"
elif _acquire_recovery_lock; then
  trap '_release_recovery_lock' EXIT
  if _docker_ok; then
    # A concurrent caller's recovery fixed it while we waited for the lock.
    echo "container runtime is running (recovered by a concurrent job)"
  elif _daemon_cleanly_down; then
    echo "container runtime cleanly down — starting OrbStack (nothing running to kill)"
    _orb_start_only
    _wait_docker_ready
  elif _docker_ok_slow; then
    echo "container runtime slow but alive (loaded box) — proceeding without restart"
  elif [[ "$MODE" == "--watchdog" ]]; then
    _wedged_watchdog
  else
    _wedged_full
  fi
  _release_recovery_lock
  trap - EXIT
elif [[ "$MODE" == "--watchdog" ]]; then
  echo "another caller holds the recovery lock — standing down, retrying next tick"
else
  echo "warning: another job's OrbStack recovery held the lock >${_LOCK_WAIT}s; proceeding — container steps may fail"
fi
