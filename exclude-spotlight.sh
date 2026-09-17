#!/usr/bin/env bash
# Report what Spotlight is indexing under the CI trees, and take you to the one
# place that can change it — System Settings > Spotlight > Search Privacy.
#
# This script does NOT edit the Privacy list. An earlier version did, by writing
# Exclusions into mds's VolumeConfiguration.plist with `plutil`, and that does
# not work. Both halves were measured 2026-09-17 on an 8 GiB Mac mini:
#
#   the write      `plutil -insert Exclusions -string <path> -append` reported
#                  success and changed nothing that mds acted on. The script's
#                  own verify caught it: "added: ~/actions-runner", then,
#                  against a live control, "STILL INDEXED (68015 items)" — and
#                  the count kept CLIMBING (60126 -> 67100 -> 73360) while the
#                  edit sat in the file. Stated precisely, because this is all
#                  that was measured: mds did not re-read the file. Whether it
#                  would also discard the edit is NOT established — #21 asserted
#                  that, and this does not confirm it.
#   the reload     `launchctl kickstart -k system/com.apple.metadata.mds` is
#                  refused on any Mac with SIP on, which is to say nearly all of
#                  them: "Could not kickstart service: 150: Operation not
#                  permitted while System Integrity Protection is engaged".
#                  So there is no way to make mds re-read it live. A REBOOT was
#                  not tested — #21's fallback line ("mds will pick the change
#                  up on its next restart or at boot") may well hold. It is a
#                  bad deal either way: a CI host reboot to apply one exclusion,
#                  against a GUI that takes effect in seconds.
#
# The GUI works, immediately and without a reload, because it asks mds to change
# its own config instead of rewriting the file underneath it. Adding
# ~/actions-runner there took that path from 73360 indexed items to 0 against a
# live control, in seconds. So: report and verify from here, change it there.
#
# ── The cost, stated plainly ─────────────────────────────────────────────────
# #21 built the plist route for a real reason: "a human at a shell can, and over
# SSH the GUI is not available". That reason did not go away — the route simply
# does not work. So on a headless CI Mac this exclusion now needs someone at the
# console, or on Screen Sharing. `--open` cannot change that: over SSH it opens
# the pane in the console user's session, where nobody may be looking, so it
# warns rather than reporting a success nobody saw. Running this over SSH is
# still useful for the REPORT — what is indexed, and whether an exclusion took.
#
# ── Why the other scriptable routes do not work either ───────────────────────
#   .metadata_never_index   INERT on an ordinary directory. Measured: with the
#                           marker in place, a newly created file under it was
#                           indexed within 45s, identical to an unexcluded
#                           control. Honoured at VOLUME/MOUNT roots only.
#   mdutil -i off <dir>     mdutil operates on volumes and stores, not paths.
#                           It IS the right tool for a whole volume that should
#                           never be indexed (a backup destination); it cannot
#                           express "this directory".
#
# ── Two traps this script still exists to avoid ──────────────────────────────
# 1. Reading `mdutil -i off` as a way to RELOAD config. It is not one: off then
#    on ERASES the volume's index and starts a full rebuild — hours of CPU on a
#    small box, and every path reads 0 for the duration. #21 reached for it that
#    way; nothing here does, and there is no reload left to want.
#    It IS the right tool pointed at a WHOLE VOLUME that should never be indexed
#    — a disk that only receives backups or media — which is the only way this
#    script offers it below. Discarding that volume's index is the POINT there,
#    not a cost, and unlike the Privacy list it cannot silently revert.
# 2. Verifying with a query that cannot fail. `mdfind -onlyin <p> -count '*'`
#    returning 0 means "not indexed" ONLY if some control path returns non-zero
#    at the same moment; during a rebuild everything returns 0 and an exclusion
#    that did nothing looks like a success. So the check below always measures a
#    control, and reports INCONCLUSIVE rather than guessing.
#
# Usage:
#   ./exclude-spotlight.sh                      # report + verify ~/actions-runner
#   ./exclude-spotlight.sh /path/one /path/two  # report on specific paths
#   ./exclude-spotlight.sh --status             # report only, skip the verify
#   ./exclude-spotlight.sh --open               # open the Spotlight pane, exit

set -uo pipefail

PLIST="${SPOTLIGHT_PLIST:-/System/Volumes/Data/.Spotlight-V100/VolumeConfiguration.plist}"
PANE="x-apple.systempreferences:com.apple.Spotlight-Settings.extension"
STATUS_ONLY=0
OPEN_ONLY=0
PATHS=()

while (($#)); do
  case "$1" in
    --status) STATUS_ONLY=1 ;;
    --open) OPEN_ONLY=1 ;;
    -h | --help)
      # The header block itself, however long it grows: every line from #2
      # until the first that is not a comment. A hardcoded range silently
      # overshoots into the code the moment the header changes.
      awk 'NR==1{next} /^#/{sub(/^# ?/,""); print; next} {exit}' "${BASH_SOURCE[0]}"
      exit 0
      ;;
    -*)
      echo "unknown flag: $1" >&2
      exit 2
      ;;
    *) PATHS+=("$1") ;;
  esac
  shift
done
((${#PATHS[@]})) || PATHS=("$HOME/actions-runner")

[[ "$(uname -s)" == "Darwin" ]] || {
  echo "not macOS — Spotlight does not apply here"
  exit 0
}

say() { printf '\n== %s ==\n' "$*"; }

_open_pane() { open "$PANE" 2>/dev/null; }

if ((OPEN_ONLY)); then
  _open_pane || {
    echo "could not open System Settings; open it by hand:" >&2
    echo "  System Settings > Spotlight > Search Privacy..." >&2
    exit 1
  }
  # `open` exits 0 once it has handed the URL off, which says nothing about
  # anyone seeing it. Over SSH that pane appears in the console user's session.
  if [[ -n "${SSH_CONNECTION:-}${SSH_TTY:-}" ]]; then
    echo "opened the pane in the CONSOLE user's session — over SSH you cannot"
    echo "drive it. Finish at the machine or on Screen Sharing:"
    echo "  System Settings > Spotlight > Search Privacy... -> add the path"
  else
    echo "opened System Settings > Spotlight (click \"Search Privacy...\")"
  fi
  exit 0
fi

# `sudo -n`, never a bare sudo: these hosts have no passwordless sudo (#21), and
# sudo's prompt goes to /dev/tty, so 2>/dev/null would hide the error but not the
# prompt — the script would block on a password to print a list it then says the
# verify does not need. Non-interactive: if it fails, say so and carry on.
_exclusions() { sudo -n plutil -extract Exclusions json -o - "$PLIST" 2>/dev/null; }

# ── Report ───────────────────────────────────────────────────────────────────

say "current Privacy list"
if _json="$(_exclusions)" && [[ -n "$_json" ]]; then
  python3 -c '
import json,sys
for p in json.loads(sys.argv[1]): print("  " + p)
' "$_json" 2>/dev/null || echo "  (could not parse)"
else
  echo "  (not readable without sudo — the verify below does not need it)"
fi

# A path that does not exist would report 0 indexed items and then read as
# "EXCLUDED (0 indexed, against a live index)" — a typo announcing success. Fail
# instead, before any of that is printed.
for p in "${PATHS[@]}"; do
  [[ -d "$p" ]] || {
    echo "refusing: $p is not a directory" >&2
    exit 1
  }
done

say "indexing status"
for p in "${PATHS[@]}"; do
  printf '  %-44s indexed items: %s\n' "$p" "$(mdfind -onlyin "$p" -count '*' 2>/dev/null || echo '?')"
done

((STATUS_ONLY)) && exit 0

# ── Verify, honestly ─────────────────────────────────────────────────────────

say "verifying"
echo "A path reading 0 proves nothing on its own — during a rebuild EVERY path"
echo "reads 0. So this measures a control at the same moment."

_control=""
_control_n=0
for c in "$HOME/Library" "$HOME/Documents" "/Applications"; do
  [[ -d "$c" ]] || continue
  n="$(mdfind -onlyin "$c" -count '*' 2>/dev/null || echo 0)"
  if [[ "${n:-0}" -gt 0 ]]; then
    _control="$c"
    _control_n="$n"
    break
  fi
done

if [[ -z "$_control" ]]; then
  echo "  INCONCLUSIVE — no control path is indexed either, so the index is"
  echo "  empty or still rebuilding. Re-run with --status once it has settled."
  exit 0
fi

echo "  control $_control: $_control_n indexed (so the index is live)"
_bad=0
for p in "${PATHS[@]}"; do
  n="$(mdfind -onlyin "$p" -count '*' 2>/dev/null || echo 0)"
  if [[ "${n:-0}" -gt 0 ]]; then
    printf '  %-44s STILL INDEXED (%s items)\n' "$p" "$n"
    _bad=1
  elif [[ -z "$(find "$p" -type f -print -quit 2>/dev/null)" ]]; then
    # A tree with no files in it reads 0 whether or not it is excluded, so the
    # control cannot rescue this one: it proves the INDEX is live, never that
    # THIS path would have had entries. Same false success as the missing
    # directory above, and reachable on purpose — ~/actions-runner on a freshly
    # provisioned host, or _work just after prune.sh empties it. Saying
    # EXCLUDED here tells the operator the Privacy-list step is done when it has
    # not been started, and CI then fills the tree and indexes all of it.
    printf '  %-44s EMPTY — nothing to measure, NOT proof of exclusion\n' "$p"
  else
    printf '  %-44s EXCLUDED (0 indexed, against a live index)\n' "$p"
  fi
done

((_bad)) || exit 0

# ── What to do about it ──────────────────────────────────────────────────────

say "how to exclude it"
cat <<'EOF'
Add the path in the GUI — the only mechanism that holds (see the header):

  System Settings > Spotlight > Search Privacy...  then + or drag the folder
  (in the file picker, Cmd-Shift-G types a path)

Re-run this script afterwards to confirm it took, against a live control.
EOF

# A whole volume that exists only to receive backups should not be indexed at
# all; that IS mdutil's job and it cannot silently revert. Offered per-volume on
# purpose. `mdutil -i off -d /System/Volumes/Data` would cover the CI tree too,
# but it disables Finder and Spotlight search for the ENTIRE login, so it is
# right only on a host with nobody at the console — which this script cannot
# determine, so it does not suggest it.
# Skip anything that IS the boot volume. macOS firmlinks the system volume back
# under /Volumes (e.g. "/Volumes/Macintosh HD"), so a name-based skip misses it
# and the loop would cheerfully advise `mdutil -i off` on the boot disk — the
# very machine-wide disable this script refuses to recommend. Compare device
# ids instead: the alias and / report the same one.
_root_dev="$(stat -f '%d' / 2>/dev/null)"
for v in /Volumes/*; do
  [[ -d "$v" ]] || continue
  [[ "$(stat -f '%d' "$v" 2>/dev/null)" == "$_root_dev" ]] && continue
  case "$(mdutil -s "$v" 2>/dev/null)" in
    *"Indexing enabled"*)
      echo ""
      echo "Also indexed, and a whole volume: $v"
      echo "  if it only receives backups or media, stop indexing it outright:"
      echo "    sudo mdutil -i off \"$v\""
      ;;
  esac
done

exit 1
