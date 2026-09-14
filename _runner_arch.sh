#!/usr/bin/env bash
# Which GitHub Actions runner build this machine needs. Shared by install.sh and
# install-linux.sh.
#
# The runner release ships one build per OS and architecture: linux-x64,
# linux-arm64, linux-arm, osx-x64 and osx-arm64. Both installers used to
# hardcode theirs — linux-x64 and osx-arm64 — so an ARM Linux machine or an
# Intel Mac downloaded a runner it could not execute, and the install died at
# config.sh.
#
# Usage: . "$SCRIPT_DIR/_runner_arch.sh"; RUNNER_ARCH="$(runner_build linux)"

# runner_build <linux|osx>: print the release build for this machine, or print
# nothing and return 1 if the runner has no build for it.
runner_build() {
  local os=$1 machine
  machine="$(uname -m)"
  # A shell running under Rosetta reports x86_64 on Apple silicon. The runner
  # should still be the native build, so ask the hardware, not the process.
  if [[ "$os" == osx && "$(sysctl -n hw.optional.arm64 2>/dev/null)" == 1 ]]; then
    machine=arm64
  fi
  case "$os:$machine" in
    *:x86_64 | *:amd64) echo "$os-x64" ;;
    *:aarch64 | *:arm64) echo "$os-arm64" ;;
    linux:armv7l | linux:armv6l) echo "linux-arm" ;;
    *) return 1 ;;
  esac
}
