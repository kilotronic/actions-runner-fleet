#!/usr/bin/env bash
# Append a host/job environment snapshot to the CI flake-diagnostics log.
#
# Paired with an in-test hook on the consuming repo's side: that hook sees one
# job from the inside; this one sees the whole host — how many jobs/test runs
# were live and whether the un-gated update-host.sh was competing for CPU.
# Correlate the two by timestamp + GITHUB_RUN_ID.
#
# Why a host-side sampler at all: failure-only data cannot disprove contention.
# Sampling EVERY job, green ones included, is what makes the log a control
# population rather than a pile of anecdotes — a resource verdict is only
# meaningful scored against the distribution of jobs that passed.
#
# Best-effort and NON-FATAL: a logging hiccup must never fail or delay a job.
# Portable across the Linux and macOS runners.
#
# Usage: log-job-env.sh <started|completed>
# Env:   CI_DIAG_DIR  log directory (default ~/actions-runner)

EVENT="${1:-unknown}"
LOG_DIR="${CI_DIAG_DIR:-$HOME/actions-runner}"
LOG="$LOG_DIR/ci-env-jobs.jsonl"
mkdir -p "$LOG_DIR" 2>/dev/null || exit 0

TS="$(date -u +%Y-%m-%dT%H:%M:%SZ 2>/dev/null)"
HOST="$(hostname -s 2>/dev/null || hostname 2>/dev/null)"

# Load average — portable: "... load average: 2.30, 2.32, 1.74" (Linux) /
# "... load averages: 2.30 2.32 1.74" (macOS). Normalize to comma-separated.
LOAD="$(uptime 2>/dev/null | sed -n 's/.*load average[s]*: *//p' | tr -s ' ' ',' | tr -d ' ')"

# Memory (kB). Linux: /proc/meminfo. macOS: total via sysctl (bytes -> kB);
# "available" has no clean analogue, so leave it empty there.
MEM_TOTAL_KB=""
MEM_AVAIL_KB=""
if [ -r /proc/meminfo ]; then
  MEM_TOTAL_KB="$(awk '/^MemTotal:/{print $2}' /proc/meminfo 2>/dev/null)"
  MEM_AVAIL_KB="$(awk '/^MemAvailable:/{print $2}' /proc/meminfo 2>/dev/null)"
elif command -v sysctl >/dev/null 2>&1; then
  _bytes="$(sysctl -n hw.memsize 2>/dev/null)"
  [ -n "$_bytes" ] && MEM_TOTAL_KB=$((_bytes / 1024))
fi

# Core count (portable): nproc on Linux, sysctl on macOS. Makes the log
# self-describing per host so we don't need SSH access to size each runner.
CORES="$(nproc 2>/dev/null || sysctl -n hw.ncpu 2>/dev/null)"

# Process counts (pgrep -c is not portable to macOS; pipe to wc -l instead).
# Dots are escaped so the pattern is a literal, not "any char".
#
# runner_listeners = the persistent per-runner agent processes on this box, one
#   per configured runner across ALL repos. This is the reliable, always-present
#   cross-repo concurrency *capacity* signal — it works on macOS, where it is
#   the count we actually trust.
# runner_workers = the transient per-job worker. On the macOS boxes this hook
#   often samples when no Runner.Worker is live, so it reads 0 there and
#   UNDERCOUNTS — do not treat a 0 as "no concurrent CI". It stays for the Linux
#   boxes (where the worker is reliably live at hook time: 1/2/3).
# pytest_procs is the best proxy for concurrent test work; loadavg (above) is
#   the contention signal. Correlate all three.
RUNNER_LISTENERS="$(pgrep -f 'Runner\.Listener' 2>/dev/null | wc -l | tr -d ' ')"
RUNNER_WORKERS="$(pgrep -f 'Runner\.Worker' 2>/dev/null | wc -l | tr -d ' ')"
PYTEST_PROCS="$(pgrep -f '[p]ytest' 2>/dev/null | wc -l | tr -d ' ')"
UPDATE_HOST=false
pgrep -f 'update-host.sh' >/dev/null 2>&1 && UPDATE_HOST=true

# Strip characters that would break a JSON string value (double-quote,
# backslash, control chars). The free-text fields are host/uptime/GITHUB_* —
# a workflow name can be arbitrary, so scrub before interpolating.
_j() { printf '%s' "$1" | tr -d '"\\' | tr -d '\000-\037'; }

# One short line (< PIPE_BUF) appended with O_APPEND, so concurrent hooks don't
# interleave. Numeric fields default to 0 so the JSON always parses.
printf '{"ts":"%s","event":"%s","host":"%s","gh_run_id":"%s","gh_job":"%s","gh_workflow":"%s","gh_run_attempt":"%s","cores":%s,"loadavg":"%s","mem_total_kb":"%s","mem_avail_kb":"%s","runner_listeners":%s,"runner_workers":%s,"pytest_procs":%s,"update_host_running":%s}\n' \
  "$(_j "$TS")" "$(_j "$EVENT")" "$(_j "$HOST")" \
  "$(_j "${GITHUB_RUN_ID:-}")" "$(_j "${GITHUB_JOB:-}")" "$(_j "${GITHUB_WORKFLOW:-}")" "$(_j "${GITHUB_RUN_ATTEMPT:-}")" \
  "${CORES:-0}" "$(_j "$LOAD")" "$MEM_TOTAL_KB" "$MEM_AVAIL_KB" \
  "${RUNNER_LISTENERS:-0}" "${RUNNER_WORKERS:-0}" "${PYTEST_PROCS:-0}" "$UPDATE_HOST" \
  >>"$LOG" 2>/dev/null

exit 0
