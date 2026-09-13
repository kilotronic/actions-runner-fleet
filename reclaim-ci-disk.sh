#!/usr/bin/env bash
# Reclaim disk on a macOS CI host by thinning Time Machine local snapshots.
# Idempotent, safe to run on every maintenance tick, no-op on Linux and no-op
# when the host has plenty of space. Needs no root.
#
# ── Why this is needed, and why deleting files is not enough ─────────────────
# Time Machine takes an APFS *local* snapshot roughly every hour, independently
# of whether the destination drive is attached. A snapshot pins every block the
# volume held at that instant — including blocks belonging to files that have
# since been deleted. Until the snapshot goes, that space does not come back.
#
# On a desktop that is a fine trade. On a CI runner it is a slow leak, because
# a runner's whole job is to write tens of thousands of files and then throw
# them away. Every job's checkout, .venv, node_modules and build output gets
# frozen into the next snapshot and held for as long as it lives.
#
# Note this is NOT fixed by the Time Machine exclusions in exclude-ci-paths.sh.
# Those control what gets COPIED TO THE DESTINATION. A local snapshot is a
# whole-volume APFS snapshot and takes the excluded paths along with everything
# else. The two mechanisms are unrelated, which is exactly why this was missed.
#
# Measured on a small-disk CI Mac, at 92% full with 10 local snapshots:
#
#   brew cleanup -s   freed 1.1 GB  ->  free space unchanged
#   go clean -cache   freed 3.6 GB  ->  free space unchanged
#   thin 3 snapshots                ->  +6 GiB    (15 -> 21 GiB free)
#   thin 5 more                     ->  +11 GiB   (21 -> 32 GiB free)
#
# Deleting 4.7 GB of cache returned literally nothing while the snapshots held
# the blocks. If a CI host is mysteriously full and freeing files does not help,
# this is the reason — check `tmutil listlocalsnapshots /` before hunting for
# big directories.
#
# ── Why urgency 1 ───────────────────────────────────────────────────────────
# `tmutil thinlocalsnapshots <mount> <bytes> <urgency>` walks OLDEST FIRST.
# Urgency 1 is the gentle tier: it removes what it must to reach the target and
# leaves the newest snapshots alone, so the host keeps recent local restore
# points. Higher urgencies get progressively more willing to drop everything,
# which is not a trade worth making automatically on someone's machine — the
# newest snapshot is the one most likely to be wanted.
#
# Usage: ./reclaim-ci-disk.sh [--dry-run]

set -uo pipefail

DRY_RUN=0
[[ "${1:-}" == "--dry-run" ]] && DRY_RUN=1

[[ "$(uname -s)" == "Darwin" ]] || {
  echo "not macOS — no local snapshots to thin"
  exit 0
}

# Only act under real pressure. A CI host that is comfortably empty should keep
# its restore points; the whole point is to trade them for space when space is
# what is scarce. Both thresholds must be crossed, so a huge disk at 88% (still
# hundreds of GB free) is left alone.
MIN_FREE_GIB="${CI_DISK_MIN_FREE_GIB:-40}"
MAX_USED_PCT="${CI_DISK_MAX_USED_PCT:-85}"
# How much to try to reclaim once triggered. Aiming a little past the threshold
# avoids re-triggering on every single tick.
TARGET_GIB="${CI_DISK_TARGET_GIB:-25}"
VOL="${CI_DISK_VOLUME:-/System/Volumes/Data}"

_free_gib() { df -k "$VOL" | tail -1 | awk '{print int($4/1024/1024)}'; }
_used_pct() { df -k "$VOL" | tail -1 | awk '{gsub(/%/,"",$5); print $5}'; }
_snap_count() { tmutil listlocalsnapshots / 2>/dev/null | grep -c TimeMachine; }

free_before="$(_free_gib)"
used="$(_used_pct)"
snaps="$(_snap_count)"

echo "disk: ${free_before} GiB free, ${used}% used, ${snaps} local snapshot(s)"

if ((free_before >= MIN_FREE_GIB)) || ((used < MAX_USED_PCT)); then
  echo "above threshold (need <${MIN_FREE_GIB} GiB free AND >=${MAX_USED_PCT}% used) — nothing to do"
  exit 0
fi

if ((snaps == 0)); then
  # Genuinely full, and snapshots are not the reason. Say so rather than
  # implying the problem is handled — something else is eating the disk.
  echo "WARNING: low on disk but there are NO local snapshots to thin."
  echo "         Something else is consuming it; investigate by hand."
  exit 0
fi

if ((DRY_RUN)); then
  echo "would: tmutil thinlocalsnapshots / $((TARGET_GIB * 1024 * 1024 * 1024)) 1"
  exit 0
fi

echo "thinning oldest local snapshots to reclaim ~${TARGET_GIB} GiB..."
tmutil thinlocalsnapshots / $((TARGET_GIB * 1024 * 1024 * 1024)) 1 2>&1 | sed 's/^/  /'

free_after="$(_free_gib)"
echo "disk: ${free_after} GiB free (reclaimed $((free_after - free_before)) GiB), $(_snap_count) snapshot(s) left"
