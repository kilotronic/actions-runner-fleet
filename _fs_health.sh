#!/usr/bin/env bash
# Filesystem-pressure reporting for status.sh. Sourced, not executed.
#
# A host that has run out of room fails jobs without ever saying so. Both of
# these cost a run here before anything reported the cause:
#
#   - a package manager cannot write its InRelease splits, so every signature
#     check fails and the job dies behind a wall of GPG errors that read as a
#     keyring problem
#   - a browser install reports success having downloaded nothing, and the
#     suite fails later at launch, once per test
#
# Hence one report for every filesystem that can fill, not just the volume. The
# temp filesystem earns its own line because it is the one that bites first: it
# is commonly a tmpfs, capped far below the disk, and whatever leaks into it
# accumulates until something unrelated breaks.
#
# Usage: . "$SCRIPT_DIR/_fs_health.sh"; fs_report tmp /tmp 1 85 "note"

# fs_report <label> <path> <min-free-gib> <max-used-pct> [low-note]
#
# LOW needs BOTH thresholds crossed: a large volume routinely sits above a used
# percentage with hundreds of GiB free, and a small one can be mostly free in
# percentage terms with nothing usable left. Either alone cries wolf.
fs_report() {
  local label=$1 path=$2 min_free=$3 max_used=$4 note=${5:-}
  local avail used ram="" line

  avail="$(df -Pk "$path" 2>/dev/null | awk 'NR==2 {printf "%d", $4/1048576}')"
  used="$(df -Pk "$path" 2>/dev/null | awk 'NR==2 {gsub(/%/,"",$5); print $5}')"
  [[ -z "$avail" || -z "$used" ]] && return 0 # unreadable path: say nothing

  # A tmpfs is worth naming: its pages are memory, so filling one does not just
  # fail writes, it takes RAM away from the jobs. `stat -f -c` is GNU; on a BSD
  # stat this simply does not match and the annotation is omitted.
  [[ "$(stat -f -c %T "$path" 2>/dev/null)" == tmpfs ]] && ram=" (tmpfs — RAM-backed)"

  if ((avail < min_free)) && ((used > max_used)); then
    line="  $label: LOW — ${avail}GiB free, ${used}% used on $path$ram"
    [[ -n "$note" ]] && line="$line ($note)"
  else
    line="  $label: ${avail}GiB free, ${used}% used on $path$ram"
  fi
  printf '%s\n' "$line"
}
