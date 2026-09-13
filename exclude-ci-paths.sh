#!/usr/bin/env bash
# Keep macOS desktop background services off the CI work trees. Idempotent;
# safe to run on every maintenance tick. No-op on Linux.
#
# ── Why ──────────────────────────────────────────────────────────────────────
# A Mac configured as a desktop indexes, backs up, and uploads everything under
# $HOME. A CI runner rewrites tens of thousands of files per job, so on a Mac
# that also runs runners those two facts compose into a feedback loop: every
# job's checkout, .venv, node_modules and build output is newly-changed data
# that Spotlight must index, Time Machine must back up, and Backblaze must
# upload — forever, because the next job dirties it all again.
#
# Measured on an 8 GiB Mac mini running 6 runners, before this ran:
#
#   mdworker_shared   7 processes, ~204% CPU combined
#   backupd           18-25% CPU instantaneous, against a 4.13 TB /
#                     6,025,027-file set — on a machine using 162 GB — and
#                     still running after 5 days
#   bzfilelist        24% CPU
#   mediaanalysisd    17.6% CPU (Photos analysis, on a CI box)
#
# ~265% of CPU in the spike and continuous disk I/O, none of it doing useful
# work: the indexed content is regenerated scratch, and the backed-up files are
# already tracked by git. On the fleet's RAM-tight boxes that is the difference
# between a container job taking 22s and taking 29 minutes.
#
# Read the instantaneous numbers as spikes during CI churn, not steady state —
# backupd's CUMULATIVE cost is 14.3 CPU-hours over 5 days (~11% of one core on
# average). An earlier revision of this comment said "820 CPU-hours", which was
# a misreading of `ps -o time`: its format is MM:SS.ss, so 820:16 is 820
# minutes, not 820 hours. The 4.13 TB / 6 M-file set it never finishes is the
# durable problem; the CPU spike is the symptom.
#
# ── What is excluded, and what deliberately is NOT ───────────────────────────
# ~/actions-runner is excluded wholesale — every byte under it is regenerable
# (the runner re-downloads and re-checks-out on demand).
#
# Source checkouts are NOT excluded. Only the disposable dependency and build
# directories inside them are. Excluding a whole source tree from Time Machine
# would drop UNCOMMITTED work from every backup, which is precisely the loss
# mode this fleet has already hit once. Committed work is safe in git; a
# half-finished edit is not, and it is exactly what a backup is for.
#
# ── Spotlight: NOT automatable, and the obvious trick does not work ──────────
# This script CANNOT exclude a directory from Spotlight. It reports the paths
# and leaves it to a human, because every scriptable option was tried and
# measured, and none of them work:
#
#   `.metadata_never_index`   INERT for ordinary directories. Measured
#                             2026-08-08: with the marker in place under
#                             ~/actions-runner, a newly created file there was
#                             indexed within 45s — identical to an unexcluded
#                             control. The marker is honored at VOLUME/MOUNT
#                             roots only. OrbStack gets away with it because
#                             ~/OrbStack is an NFS mount root (`mount` shows
#                             `OrbStack:/OrbStack on /Users/jason/OrbStack`),
#                             not because the file works on directories.
#   `mdutil -i off <dir>`     mdutil operates on volumes/stores, not paths.
#   Privacy list              The supported mechanism, but it lives in a
#                             root-owned VolumeConfiguration.plist and these
#                             hosts have no passwordless sudo, so neither this
#                             script nor the maintenance timer can write it.
#
# Beware of verifying this with the wrong query: `mdfind -onlyin <p>
# 'kMDItemFSName == "*"'` returns 0 for EVERY path, indexed or not, which reads
# as a false success. Use `mdfind -onlyin <p> 'kMDItemFSSize > 0' | head -1`,
# or search for a filename known to exist in the tree.
#
# Usage: ./exclude-ci-paths.sh [--dry-run]

set -uo pipefail

DRY_RUN=0
[[ "${1:-}" == "--dry-run" ]] && DRY_RUN=1

[[ "$(uname -s)" == "Darwin" ]] || {
  echo "not macOS — nothing to exclude"
  exit 0
}

BASE_DIR="$HOME/actions-runner"
# Source roots to sweep for disposable subdirectories. Extra roots may be
# passed via CI_EXCLUDE_ROOTS (colon-separated) for hosts that check out
# elsewhere.
ROOTS="$HOME/Code${CI_EXCLUDE_ROOTS:+:$CI_EXCLUDE_ROOTS}"
# Regenerable dependency/build/cache directories. Kept deliberately short:
# every entry must be something a build can recreate from a lockfile or source.
DISPOSABLE=(.venv node_modules .pytest_cache __pycache__ .mypy_cache .ruff_cache target dist .next .tox)

_changed=0
_run() {
  if ((DRY_RUN)); then
    echo "  would: $*"
  else
    "$@" >/dev/null 2>&1
  fi
}

# tmutil addexclusion is a sticky (xattr) exclusion and is idempotent, but it
# is not free — skip paths already excluded so a 2h timer stays cheap.
_exclude_tm() {
  local p=$1
  [[ -e "$p" ]] || return 0
  if tmutil isexcluded "$p" 2>/dev/null | grep -q '^\[Excluded\]'; then
    return 0
  fi
  echo "  TM exclude: $p"
  _run tmutil addexclusion "$p"
  _changed=$((_changed + 1))
}

# Report-only: returns 0 (true) if Spotlight still has this path indexed.
# See the Spotlight header above for why nothing here can actually exclude it.
_spotlight_indexed() {
  local p=$1
  [[ -d "$p" ]] || return 1
  [[ -n "$(mdfind -onlyin "$p" 'kMDItemFSSize > 0' 2>/dev/null | head -1)" ]]
}

echo "==> CI scratch (excluded wholesale — all of it regenerable)"
_exclude_tm "$BASE_DIR"

echo "==> Disposable build/dependency dirs under source roots"
echo "    (source itself stays backed up — uncommitted work must survive)"
IFS=':' read -ra _roots <<<"$ROOTS"
for root in "${_roots[@]}"; do
  [[ -d "$root" ]] || continue
  # -prune so the walk does not descend INTO a node_modules to find nested
  # ones; on a box with several JS projects that walk alone is minutes.
  _find_args=()
  for d in "${DISPOSABLE[@]}"; do
    _find_args+=(-name "$d" -o)
  done
  unset '_find_args[${#_find_args[@]}-1]'
  while IFS= read -r -d '' dir; do
    _exclude_tm "$dir"
  done < <(find "$root" -maxdepth 5 \( "${_find_args[@]}" \) -type d -prune -print0 2>/dev/null)
done

# ── Manual steps this script cannot do ──────────────────────────────────────
# Reported every run rather than silently skipped. A script that quietly does
# two thirds of a job reads as "handled" and the remaining third never gets
# done — which is exactly how the Spotlight half of this sat broken while the
# Time Machine half looked like success.
_manual=()

# Spotlight cannot be excluded per-directory without root (see header). Only
# report paths Spotlight demonstrably still has indexed, so this line goes away
# on a host where it has actually been done.
if _spotlight_indexed "$BASE_DIR"; then
  _manual+=("Spotlight: System Settings > Siri & Spotlight > Spotlight Privacy… -> add $BASE_DIR")
fi

# Backblaze's exclusion rules live in a root-owned XML that the app rewrites on
# quit, so editing it from a timer races the app and can lose rules.
if [[ -d /Library/Backblaze.bzpkg ]]; then
  _manual+=("Backblaze: Settings > Exclusions -> add $BASE_DIR")
fi

# Photos media analysis on a CI box is pure waste (17.6% CPU observed).
if pgrep -xq mediaanalysisd 2>/dev/null; then
  _manual+=("Photos analysis: mediaanalysisd is running; disable Photos analysis on a CI host")
fi

if ((${#_manual[@]})); then
  echo "==> MANUAL steps still outstanding on this host (cannot be scripted):"
  for m in "${_manual[@]}"; do echo "      - $m"; done
fi

if ((DRY_RUN)); then
  echo "dry run — nothing changed"
elif ((_changed == 0)); then
  echo "already converged — no changes"
else
  echo "applied $_changed exclusion(s)"
fi
