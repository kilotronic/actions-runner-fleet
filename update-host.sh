#!/usr/bin/env bash
# Pull latest actions-runner changes and apply hook + host-state fixes.
#
# Called by job-completed hooks (best-effort, fire-and-forget). Also safe to
# run manually any time. Serialized via flock so concurrent runner hooks
# don't race.
#
# Repo location is read from ~/actions-runner/.repo-path (written by install.sh
# at install time) or from $ACTIONS_RUNNER_REPO. If the path is missing, this
# script silently exits.
#
# Runs on every invocation (job-completed hook + the 2h github-runner-maintenance
# timer): pull (ff-only), sync hooks, ensure both timers, then converge runners
# to runners.toml. Logs to ~/actions-runner/logs/update.log.
# Disable everything: `touch ~/actions-runner/.no-auto-update`.
# Disable only destructive runner removals: `touch ~/actions-runner/.no-auto-prune`.

set -euo pipefail

BASE_DIR="$HOME/actions-runner"
[[ -e "$BASE_DIR/.no-auto-update" ]] && exit 0

REPO_DIR="${ACTIONS_RUNNER_REPO:-}"
[[ -z "$REPO_DIR" && -f "$BASE_DIR/.repo-path" ]] && REPO_DIR="$(<"$BASE_DIR/.repo-path")"
[[ -n "$REPO_DIR" && -d "$REPO_DIR/.git" ]] || exit 0

LOG_DIR="$BASE_DIR/logs"
mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/update.log"

# Serialize: only one update at a time across all of a host's runners' hooks
# (concurrent git ops on the one checkout would corrupt it). flock(1) is
# Linux-only and ABSENT on the macOS runners — there `flock -n 9` errored
# "command not found" and the `|| exit 0` bailed *before* deploying, so the
# macOS boxes never auto-updated their hooks. Take the advisory lock with flock
# where present, else via fcntl.flock through python3: the *syscall* exists on
# macOS (only the CLI is missing), and the runners already depend on python3
# (scripts/with_ci_slot.py uses the same lock). The shell keeps fd 9 open, so
# the lock the python child takes on it survives the child's exit and holds for
# the whole script — and auto-releases on process death, so no stale lock can
# wedge updates.
exec 9>"$BASE_DIR/.update.lock"
if command -v flock >/dev/null 2>&1; then
  flock -n 9 || exit 0
else
  python3 -c 'import fcntl, sys
try:
    fcntl.flock(9, fcntl.LOCK_EX | fcntl.LOCK_NB)
except OSError:
    sys.exit(1)' 2>/dev/null || exit 0
fi

cd "$REPO_DIR" || exit 0

# Resolve a Python that can actually run the fleet scripts, rather than trusting
# whatever bare `python3` resolves to. apply.py needs `tomllib` and
# load-watchdog.py needs `datetime.UTC` — both 3.11+. The runner launchd/systemd
# plists bake in a PATH that finds a modern Homebrew/pyenv python3, so hook- and
# timer-driven runs are fine; but a plain `ssh host ./update-host.sh` (or any
# context without that PATH) gets macOS system python3 3.9, where those scripts
# die with ImportError/ModuleNotFoundError — previously swallowed by `|| true`,
# i.e. convergence silently degraded with nothing in the log to say so.
#
# `import tomllib` is the capability probe: it succeeds on exactly the 3.11+
# interpreters these scripts need. First match wins; explicit versioned names
# and the Homebrew path come before bare `python3` so we skip a stale 3.9.
_resolve_py() {
  local c
  for c in python3.14 python3.13 python3.12 python3.11 \
    /opt/homebrew/bin/python3 /usr/local/bin/python3 python3; do
    command -v "$c" >/dev/null 2>&1 || continue
    "$c" -c 'import tomllib' >/dev/null 2>&1 && {
      command -v "$c"
      return 0
    }
  done
  return 1
}
PY="$(_resolve_py || true)"

before=$(git rev-parse HEAD 2>/dev/null || echo "")
git fetch --quiet origin 2>/dev/null || exit 0

# Determine the tracked branch (usually main) and only fast-forward.
upstream=$(git rev-parse --abbrev-ref --symbolic-full-name '@{u}' 2>/dev/null || echo "")
[[ -z "$upstream" ]] && exit 0

git merge --ff-only "$upstream" >/dev/null 2>&1 || exit 0
after=$(git rev-parse HEAD)
# Always sync the installed hooks from the repo (idempotent, zero-downtime —
# the runner re-reads them each job), even when the checkout is already current:
# a host whose repo advanced out-of-band (e.g. a manual pull) would otherwise
# keep running stale installed hooks. apply.py is the single writer of
# installed hook files: it discovers hooks/*.sh dynamically (Linux overlaid by
# hooks/linux/*.sh) instead of a hardcoded list here — a hardcoded list is what
# let issue #19's new hook silently never reach the fleet. Cheap; silent on
# success; a failure here must not abort the rest of this script.
# A missing 3.11+ interpreter is loud, once, up front: everything below that
# needs $PY (hook sync, timers, convergence) is about to no-op, and a silent
# skip is exactly the failure that hid here before. Not fatal — the polkit block
# and git ops still run — but the log now says why the host stopped converging.
if [[ -z "$PY" ]]; then
  echo "[$(date -u +%FT%TZ)] WARN: no Python 3.11+ found (need tomllib); skipping hook sync, timers, and convergence. PATH=$PATH" >>"$LOG"
fi

if [[ -n "$PY" && -f ./apply.py ]]; then
  "$PY" ./apply.py --sync-config >>"$LOG" 2>&1 || true
fi
case "$(uname -s)" in
  Linux)
    # Keep an ALREADY-INSTALLED polkit inhibitor rule in sync with the template.
    # Creating one is install-polkit-rule.sh's job, never this script's: it is a
    # privileged policy change and a machine that never suspends does not need
    # it, so opting in stays an explicit act. The test here is therefore "does
    # the destination exist", not "does a source exist" — the previous version
    # asked the latter about a path inside this checkout, which has shipped no
    # such file since the rule became a template, so it was a silent no-op on
    # every host. Hosts that predate the template keep working: their installed
    # copy is byte-compared against the freshly rendered one.
    #
    # sudo -n: a host without passwordless sudo warns rather than hanging a
    # hook-triggered update. Privileged host state, not runner config, so it
    # stays here rather than moving into apply.py.
    POLKIT_TEMPLATE=polkit/49-actions-runner-inhibit.rules.in
    POLKIT_DEST=/etc/polkit-1/rules.d/49-actions-runner-inhibit.rules
    # Every test against $POLKIT_DEST goes through sudo. On a stock polkit
    # install /etc/polkit-1/rules.d is 0750 root:polkitd, so `[[ -f ]]` as the
    # runner user is false whether the rule is there or not — which made this
    # re-sync a no-op on exactly the hosts that have the rule. sudo -n
    # throughout: a host without passwordless sudo reports absent and skips,
    # which is the fail-safe direction and matches the note above.
    if [[ -f "$POLKIT_TEMPLATE" ]] && sudo -n test -f "$POLKIT_DEST" 2>/dev/null; then
      POLKIT_RENDERED="$(sed "s/@RUNNER_USER@/$(id -un)/g" "$POLKIT_TEMPLATE")"
      if ! printf '%s\n' "$POLKIT_RENDERED" | sudo -n cmp -s - "$POLKIT_DEST" 2>/dev/null; then
        POLKIT_TMP="$(mktemp)"
        printf '%s\n' "$POLKIT_RENDERED" >"$POLKIT_TMP"
        if sudo -n cp "$POLKIT_TMP" "$POLKIT_DEST" 2>/dev/null \
          && sudo -n chmod 644 "$POLKIT_DEST" 2>/dev/null \
          && sudo -n chown root:root "$POLKIT_DEST" 2>/dev/null; then
          echo "re-rendered polkit inhibitor rule -> $POLKIT_DEST"
        else
          echo "warning: could not update $POLKIT_DEST (sudo -n denied?)"
        fi
        rm -f "$POLKIT_TMP"
      fi
    fi
    ;;
esac

# Convergence runs on EVERY invocation, not just after a pull: the 2-hour
# maintenance timer's whole purpose is to heal idle / asleep /
# sleep-deregistered hosts whose checkout is already current. (Previously this
# block was gated on a new commit via `[[ before == after ]] && exit 0`, so an
# up-to-date idle host never self-healed.) Output stays terse so per-job hook
# runs don't flood update.log — apply.py stays silent on a fully in-sync run,
# so a quiet tick appends nothing.
{
  if [[ "$before" != "$after" ]]; then
    echo "[$(date -u +%FT%TZ)] update: ${before:0:7} -> ${after:0:7}"
  fi

  # 1. (Re)install the per-host timers on EVERY tick, not only when the checkout
  # advanced. install_timer self-checks: if the rendered unit already matches
  # what is installed and the unit is loaded, it returns without touching
  # launchd/systemd, so the bootout/bootstrap cycle (whose RunAtLoad re-fires
  # this very script) still only happens on a real change.
  #
  # The old "only when the checkout advanced" gate could never adopt a NEWLY
  # ADDED timer. The pass that pulls the introducing commit is still running the
  # PRE-pull script body, so it doesn't know the timer exists; the next pass has
  # the new body but no longer sees the checkout move, so the gate is false. The
  # timer then waits for some unrelated future commit. That is exactly how the
  # OrbStack watchdog shipped and then sat uninstalled on all three Macs.
  _install_timer() {
    # _install_timer <script> <human name>
    [[ -f "./$1" ]] || return 0
    [[ -n "$PY" ]] || return 0 # WARN already logged once above
    "$PY" "./$1" --install-timer >/dev/null 2>&1 || echo "WARN: $2 timer install failed"
  }
  _install_timer load-watchdog.py "load-watchdog"
  # macOS-only; --install-timer is a clean no-op on Linux.
  _install_timer lid-watchdog.py "lid-watchdog"
  _install_timer maintenance-timer.py "maintenance"
  # OrbStack watchdog is toml-opt-in (container_runtime = "orbstack").
  # Converge both directions so dropping the flag tears the timer down.
  if [[ -n "$PY" && -f ./fleet_config.py ]]; then
    RT="$("$PY" ./fleet_config.py --container-runtime 2>/dev/null || true)"
    if [[ "$RT" == orbstack ]]; then
      _install_timer orbstack-watchdog.py "OrbStack watchdog"
    elif [[ -f ./orbstack-watchdog.py ]]; then
      "$PY" ./orbstack-watchdog.py --uninstall-timer >/dev/null 2>&1 || true
    fi
  fi

  # Converge this host's tailnet exposure of ollama to ollama_serve in
  # runners.toml. Quiet when in sync. Fail-closed: a failure leaves the
  # endpoint missing, never over-exposed.
  if [[ -n "$PY" && -f ./ollama_serve.py ]]; then
    "$PY" ./ollama_serve.py || true
  fi

  # Converge runner inventory to runners.toml.
  if [[ -n "$PY" && -f ./apply.py ]]; then
    "$PY" ./apply.py || true
  fi

  # Keep macOS desktop background services off the CI work trees (macOS-only;
  # a clean no-op on Linux hosts). Converged every tick rather than at install
  # time because most of what needs excluding does not exist until jobs have
  # run — a checkout's .venv/node_modules appear long after install finished,
  # and each new worktree brings more. Idempotent and silent once converged.
  if [[ -x ./exclude-ci-paths.sh ]]; then
    ./exclude-ci-paths.sh 2>&1 | grep -vE '^(==>|    |already converged|not macOS)' || true
  fi

  # Reclaim disk by thinning Time Machine local snapshots when the host is
  # actually short of space (macOS-only, silent above the threshold). NOT
  # covered by the exclusions above: a local snapshot is a whole-volume APFS
  # snapshot, so it pins CI churn regardless of what Time Machine is configured
  # to copy. On one host this had taken the disk to 92%, and freeing 4.7 GB of
  # caches returned nothing at all because the snapshots still held the blocks.
  if [[ -x ./reclaim-ci-disk.sh ]]; then
    ./reclaim-ci-disk.sh 2>&1 | grep -vE '^(disk:|above threshold|not macOS)' || true
  fi

  # Reclaim runner disk under real pressure, on every platform. prune.sh has
  # existed for months and nothing ever called it, so a host filled up until a
  # job failed — and it fails in a way that does not mention disk at all: apt
  # cannot write its InRelease splits, so `playwright install --with-deps` dies
  # behind a wall of GPG signature errors. Convergence should reclaim before a
  # job discovers the problem.
  #
  # Same thresholds and the same both-must-cross rule as reclaim-ci-disk.sh, so
  # a large disk at 88% with hundreds of GB free is left alone. Only the
  # always-safe targets: stale _diag logs, and _work/_temp for runners GitHub
  # reports idle. Checkouts and the Playwright cache are reported, never
  # deleted — removing those only forces a re-download on the next job.
  if [[ -x ./prune.sh && -n "$PY" ]]; then
    # On macOS `/` is the read-only system volume — 22% used while the data
    # volume it shares a disk with sits at 84%. Measuring the wrong one would
    # make this block silently never fire on exactly the hosts with the
    # tightest disks. reclaim-ci-disk.sh picks the same path for the same reason.
    if [[ "$(uname -s)" == Darwin ]]; then
      _disk_vol="${CI_DISK_VOLUME:-/System/Volumes/Data}"
    else
      _disk_vol="${CI_DISK_VOLUME:-/}"
    fi
    _disk_avail_gib="$(df -Pk "$_disk_vol" | awk 'NR==2 {printf "%d", $4/1048576}')"
    _disk_used_pct="$(df -Pk "$_disk_vol" | awk 'NR==2 {gsub(/%/,"",$5); print $5}')"
    if ((_disk_avail_gib < ${CI_DISK_MIN_FREE_GIB:-40})) \
      && ((_disk_used_pct > ${CI_DISK_MAX_USED_PCT:-85})); then
      echo "disk low on $_disk_vol (${_disk_avail_gib}GiB free, ${_disk_used_pct}% used) — pruning runner dirs"
      while read -r _repo; do
        [[ -n "$_repo" ]] || continue
        ./prune.sh "$_repo" --apply 2>&1 | sed 's/^/  /' || true
      done < <("$PY" ./fleet_config.py --repos 2>/dev/null || true)
    fi
  fi
} >>"$LOG" 2>&1
