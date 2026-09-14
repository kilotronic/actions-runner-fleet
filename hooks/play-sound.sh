#!/usr/bin/env bash
# Play an operator-supplied chime when a job starts or ends (best-effort).
#
# Usage: play-sound.sh <started|completed>
#   started    plays <inventory>/sounds/job-start.aiff
#   completed  plays <inventory>/sounds/job-end.aiff
#
# <inventory> is the directory holding runners.toml, resolved in the same order
# as fleet_config.resolve_config_path: the directory of $ACTIONS_RUNNER_CONFIG,
# else $XDG_CONFIG_HOME/actions-runner, else ~/.config/actions-runner.
#
# Silent by default. This repo ships no sounds: an absent file is silence, so a
# host chimes only if its operator put a file in the inventory — and each event
# is independent, so a host can have a start chime and no end chime.
#
# Players: afplay on macOS; on Linux pw-play, then paplay, then ffplay. Never
# aplay. ALSA's aplay only understands voc/wav/raw/au, and on anything else it
# falls back to raw — it would play the AIFF header as noise.
#
# Two details keep the backgrounded player alive and the job unblocked:
#
#   1. RUNNER_TRACKING_ID is removed from the player's environment. When a job
#      ends, the runner terminates every process still carrying that job's
#      tracking ID, milliseconds after the job-completed hook returns — job logs
#      show it killing what that hook backgrounds about 40ms after it starts. A
#      player launched without this dies before it makes a sound.
#   2. All three stdio streams go to /dev/null, so a caller reading this
#      script's output is not held open for the length of the sound.
#
# Fail-safe like every sidecar: no sound, no player, or a failing player is a
# silent no-op that never fails or delays a job. Always exits 0.

EVENT="${1:-}"

case "$EVENT" in
  started) NAME=job-start.aiff ;;
  completed) NAME=job-end.aiff ;;
  *) exit 0 ;;
esac

if [[ -n "${ACTIONS_RUNNER_CONFIG:-}" ]]; then
  INVENTORY="$(dirname "$ACTIONS_RUNNER_CONFIG")"
elif [[ -n "${XDG_CONFIG_HOME:-}" ]]; then
  INVENTORY="$XDG_CONFIG_HOME/actions-runner"
else
  INVENTORY="$HOME/.config/actions-runner"
fi

SOUND="$INVENTORY/sounds/$NAME"
[[ -f "$SOUND" && -r "$SOUND" ]] || exit 0

player=()
if [[ "$(uname -s 2>/dev/null)" == Darwin ]]; then
  command -v afplay >/dev/null 2>&1 && player=(afplay)
elif command -v pw-play >/dev/null 2>&1; then
  player=(pw-play)
elif command -v paplay >/dev/null 2>&1; then
  player=(paplay)
elif command -v ffplay >/dev/null 2>&1; then
  player=(ffplay -nodisp -autoexit -loglevel quiet)
fi
[[ ${#player[@]} -gt 0 ]] || exit 0

env -u RUNNER_TRACKING_ID "${player[@]}" "$SOUND" </dev/null >/dev/null 2>&1 &

exit 0
