#!/usr/bin/env bash
# Install and configure GitHub Actions self-hosted runners for a repository.
#
# Usage:
#   ./install.sh <owner/repo> [workers]
#   ./install.sh acme/app        # 1 worker (default)
#   ./install.sh acme/app 3      # 3 parallel workers
#
# Prerequisites: gh (authenticated), python3.11+
#                OrbStack is optional — enable per host with
#                container_runtime = "orbstack" in runners.toml
#
# Layout:
#   ~/actions-runner/<repo>-1/   (worker 1)
#   ~/actions-runner/<repo>-2/   (worker 2)
#   ...
#
# Each worker gets its own launchd agent and registration.

set -euo pipefail

# ── Configuration ────────────────────────────────────────────────────────────

RUNNER_VERSION="2.336.0"
RUNNER_ARCH="osx-arm64"
RUNNER_TARBALL="actions-runner-${RUNNER_ARCH}-${RUNNER_VERSION}.tar.gz"
RUNNER_URL="https://github.com/actions/runner/releases/download/v${RUNNER_VERSION}/${RUNNER_TARBALL}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# ── Helpers ──────────────────────────────────────────────────────────────────

die() {
  echo "error: $*" >&2
  exit 1
}
info() { echo "==> $*"; }

check_cmd() {
  command -v "$1" &>/dev/null || die "$1 is required but not found. Install it first."
}

# ── Argument parsing ─────────────────────────────────────────────────────────

if [[ $# -lt 1 ]]; then
  echo "Usage: $0 <owner/repo> [workers]"
  echo "  e.g. $0 acme/app 3"
  exit 1
fi

REPO="$1"
REPO_NAME="${REPO##*/}"
WORKERS="${2:-1}"
BASE_DIR="$HOME/actions-runner"
CACHE_DIR="$BASE_DIR/.cache"
HOSTNAME="$(hostname -s)"
HOOKS_DIR="$BASE_DIR/hooks"

if ! [[ "$WORKERS" =~ ^[1-9][0-9]*$ ]]; then
  die "workers must be a positive integer, got: $WORKERS"
fi

# ── Prerequisites ────────────────────────────────────────────────────────────

# Homebrew is required for everything else
check_cmd brew

# Install missing brew packages (idempotent — skips already-installed)
BREW_DEPS=(gh python@3.13)
MISSING=()
for pkg in "${BREW_DEPS[@]}"; do
  brew list --formula "$pkg" &>/dev/null || MISSING+=("$pkg")
done
if [[ ${#MISSING[@]} -gt 0 ]]; then
  info "Installing missing packages: ${MISSING[*]}"
  brew install "${MISSING[@]}"
else
  info "All brew packages already installed."
fi

# Ensure python3 resolves to 3.11+ (tomllib)
if ! command -v python3 &>/dev/null; then
  die "python3 not found after brew install. Add brew python to PATH."
fi
PY_MAJOR=$(python3 -c 'import sys; print(sys.version_info.major)')
PY_MINOR=$(python3 -c 'import sys; print(sys.version_info.minor)')
if [[ "$PY_MAJOR" -lt 3 ]] || [[ "$PY_MAJOR" -eq 3 && "$PY_MINOR" -lt 11 ]]; then
  die "Python 3.11+ required, found ${PY_MAJOR}.${PY_MINOR}."
fi

# On macOS gh keeps its token in the login keychain, which is LOCKED in any
# non-GUI session — so `gh auth status` reports a *failed login* over SSH even
# when the token is perfectly valid. Distinguish that from a genuinely bad token
# so the error names the actual fix instead of sending you round the
# `gh auth login` loop (which re-stows the token in the keychain and regresses
# it again). See docs/gh-auth-over-ssh.md.
GH_HOSTS="${GH_CONFIG_DIR:-$HOME/.config/gh}/hosts.yml"
gh_uses_keychain() { ! grep -qs 'oauth_token:' "$GH_HOSTS"; }

if ! gh auth status &>/dev/null; then
  if [[ -n "${SSH_CONNECTION:-}" ]] && gh_uses_keychain; then
    die "gh's token is in the login keychain, which is unreadable over SSH (auth is probably fine).
  Convert this host to file storage — run in a GUI terminal ON this mac:
      gh auth token | gh auth login --with-token --insecure-storage
  Or pipe a token in from here:
      printf '%s' \"\$YOUR_TOKEN\" | gh auth login --with-token --insecure-storage -h github.com
  Verify: gh auth status  # source must read (…/hosts.yml), not (keyring)
  See docs/gh-auth-over-ssh.md"
  fi
  die "gh is not authenticated. Run: gh auth login (SSH/headless: see docs/gh-auth-over-ssh.md)"
fi

# Auth works, but if the token still lives in the keychain this host will break
# the next time anything runs over SSH (convergence included). Warn early.
if gh_uses_keychain; then
  echo "  WARN: gh token is in the login keychain, not ${GH_HOSTS}."
  echo "        SSH sessions on this host cannot read it — convergence will skip."
  echo "        Fix: gh auth token | gh auth login --with-token --insecure-storage"
fi

info "All prerequisites satisfied."

# ── Download runner (shared cache) ───────────────────────────────────────────

mkdir -p "$CACHE_DIR"
mkdir -p "$BASE_DIR/.shared-tool-cache"

# Record this checkout's path so update-host.sh (called from job hooks) can
# find it for `git pull`.
echo "$SCRIPT_DIR" >"$BASE_DIR/.repo-path"

if [[ ! -f "$CACHE_DIR/$RUNNER_TARBALL" ]]; then
  info "Downloading GitHub Actions runner v${RUNNER_VERSION} (${RUNNER_ARCH})..."
  curl -fsSL -o "$CACHE_DIR/$RUNNER_TARBALL" "$RUNNER_URL"
else
  info "Runner tarball already cached."
fi

# Verify checksum (every run, not just on download)
info "Verifying checksum..."
RELEASE_BODY=$(gh api "repos/actions/runner/releases/tags/v${RUNNER_VERSION}" --jq '.body')
EXPECTED_HASH=$(echo "$RELEASE_BODY" | sed -n "s/.*<!-- BEGIN SHA ${RUNNER_ARCH} -->\([a-f0-9]*\)<!-- END SHA.*/\1/p")
[[ -n "$EXPECTED_HASH" ]] || die "Could not extract checksum from release notes"
ACTUAL_HASH=$(shasum -a 256 "$CACHE_DIR/$RUNNER_TARBALL" | awk '{print $1}')
if [[ "$EXPECTED_HASH" != "$ACTUAL_HASH" ]]; then
  rm -f "$CACHE_DIR/$RUNNER_TARBALL"
  die "Checksum mismatch: expected ${EXPECTED_HASH}, got ${ACTUAL_HASH}"
fi

# ── Install job notification hooks ────────────────────────────────────────────

info "Installing job hooks..."
mkdir -p "$HOOKS_DIR"
cp "$SCRIPT_DIR/hooks/"*.sh "$HOOKS_DIR/"
chmod +x "$HOOKS_DIR/"*.sh

# Cap pytest-xdist `-n auto` on self-hosted runners. Uncapped it uses one
# worker per core; concurrent jobs then oversubscribe CPU. Half the physical
# cores (min 2) leaves headroom. pytest-xdist reads this env.
PHYS_CORES="$(sysctl -n hw.physicalcpu)"
XDIST_AUTO_WORKERS="${PYTEST_XDIST_AUTO_NUM_WORKERS:-$((PHYS_CORES / 2 > 2 ? PHYS_CORES / 2 : 2))}"

# ── Install each worker ──────────────────────────────────────────────────────

for i in $(seq 1 "$WORKERS"); do
  RUNNER_DIR="$BASE_DIR/${REPO_NAME}-${i}"
  RUNNER_NAME="${HOSTNAME}-${REPO_NAME}-${i}"
  PLIST_LABEL="com.github.actions-runner.${REPO_NAME}-${i}"
  PLIST_PATH="$HOME/Library/LaunchAgents/${PLIST_LABEL}.plist"

  echo ""
  info "Worker ${i}/${WORKERS}: ${RUNNER_NAME}"

  if [[ -f "$RUNNER_DIR/.runner" ]]; then
    info "  Already configured — skipping (uninstall first to reconfigure)"
    continue
  fi

  # Extract fresh copy
  mkdir -p "$RUNNER_DIR"
  info "  Extracting runner..."
  tar xzf "$CACHE_DIR/$RUNNER_TARBALL" -C "$RUNNER_DIR"

  # Share one externals/ across every runner on this host. It is the single
  # largest thing in a runner dir (~349M: node20 + node24) and is byte-identical
  # in each one, since every runner extracts the same tarball — so N runners
  # otherwise cost N × 349M for the same bytes. Six runners on one host went from
  # 434M each to 85M each this way (3.4G → 2.1G) with no external volume
  # involved, which is why the work_root/removable-volume approach is not the
  # lever it looked like: it moves _work (measured at 86M) and leaves this
  # behind. See docs/work-root-investigation.md.
  #
  # Keyed by RUNNER_VERSION on purpose. externals must match the agent version,
  # and a runner that self-updates writes a new versioned tree rather than
  # mutating the copy older runners are still pointing at.
  SHARED_EXTERNALS="$BASE_DIR/.shared-externals-${RUNNER_VERSION}"
  if [[ -d "$SHARED_EXTERNALS" ]]; then
    rm -rf "$RUNNER_DIR/externals"
  else
    mv "$RUNNER_DIR/externals" "$SHARED_EXTERNALS"
  fi
  ln -sfn "$SHARED_EXTERNALS" "$RUNNER_DIR/externals"
  info "  externals → ${SHARED_EXTERNALS##*/} (shared)"

  # Offload the large, churny _work tree to an external drive when the host
  # declares work_root in runners.toml (apply.py passes it as WORK_ROOT). Only
  # _work moves: the runner agent and its launchd stdout/stderr logs stay on the
  # internal disk, because a removable volume can't host a LaunchAgent's
  # executable or StandardOutPath without tripping launchd (exit 78 / a failed
  # open at boot before the volume mounts). The runner is configured with
  # --work "_work" (below), which follows this symlink. The mount-guard in
  # hooks/job-started.sh aborts a job cleanly if the drive is absent.
  if [[ -n "${WORK_ROOT:-}" ]]; then
    WORK_DIR="${WORK_ROOT%/}/${REPO_NAME}-${i}"
    mkdir -p "$WORK_DIR"
    rm -rf "$RUNNER_DIR/_work" # a symlink here removes only the link, not $WORK_DIR
    ln -s "$WORK_DIR" "$RUNNER_DIR/_work"
    info "  _work → ${WORK_DIR}"
  fi

  # Job notification hooks (via runner .env)
  info "  Configuring job hooks..."
  cat >"$RUNNER_DIR/.env" <<ENV
LANG=en_US.UTF-8
ACTIONS_RUNNER_HOOK_JOB_STARTED=${HOOKS_DIR}/job-started.sh
ACTIONS_RUNNER_HOOK_JOB_COMPLETED=${HOOKS_DIR}/job-completed.sh
PYTEST_XDIST_AUTO_NUM_WORKERS=${XDIST_AUTO_WORKERS}
ENV

  # Bake the host's CI slot count (from apply.py, sourced from runners.toml) so
  # partygame's with_ci_slot.py admission gate uses the right per-host capacity.
  # Only when set: an empty CI_SLOTS= line would make int("") raise in that script.
  if [[ -n "${CI_SLOTS:-}" ]]; then
    echo "CI_SLOTS=${CI_SLOTS}" >>"$RUNNER_DIR/.env"
  fi

  # Bake the host's browser-e2e worker budget (apply.py, from runners.toml
  # e2e_workers). Only when set: ci.yml defaults to 1 when the var is absent.
  if [[ -n "${E2E_WORKERS_OVERRIDE:-}" ]]; then
    echo "E2E_WORKERS_OVERRIDE=${E2E_WORKERS_OVERRIDE}" >>"$RUNNER_DIR/.env"
  fi

  # When _work is offloaded (above), tell the job-started mount-guard which path
  # must be mounted & writable before a job runs. WORK_DIR is set in the offload
  # block; this reaches the hook because the runner exports its .env to jobs.
  if [[ -n "${WORK_ROOT:-}" ]]; then
    echo "RUNNER_WORK_MOUNT=${WORK_DIR}" >>"$RUNNER_DIR/.env"
  fi

  # Registration token (one per worker — tokens are single-use)
  info "  Obtaining registration token..."
  REG_TOKEN=$(gh api "repos/${REPO}/actions/runners/registration-token" --method POST --jq '.token')
  [[ -n "$REG_TOKEN" ]] || die "Failed to obtain registration token"

  # Configure. When _work is offloaded (WORK_ROOT set), disable the runner's
  # built-in self-update: a self-update replaces bin.<version>/Runner.Listener
  # with a new versioned directory, and macOS's "Removable Volumes" TCC grant
  # (needed to access the offloaded _work) is keyed to that exact resolved
  # binary path — so a self-update invalidates the grant and re-triggers a
  # one-time interactive approval dialog. Nobody will be at the console to
  # click it on an unattended reboot, hanging the runner indefinitely (see
  # fleet.md's laptop-host section for the live repro). Trade-off: nothing in this
  # repo currently re-applies a pinned RUNNER_VERSION bump to an
  # already-configured runner (install.sh skips dirs with an existing
  # .runner), so a disableupdate'd runner only gets a newer version via a
  # manual uninstall+reinstall — same as this repo's own laptop-host runners
  # today — until GitHub forces it offline for falling below the minimum
  # agent version. The array + ${arr[@]+"..."} expansion is empty-array-safe
  # under `set -u` on bash 3.2.
  UPDATE_FLAGS=()
  if [[ -n "${WORK_ROOT:-}" ]]; then
    UPDATE_FLAGS+=(--disableupdate)
  fi
  info "  Configuring..."
  "$RUNNER_DIR/config.sh" \
    --url "https://github.com/${REPO}" \
    --token "$REG_TOKEN" \
    --name "$RUNNER_NAME" \
    --labels "self-hosted,macOS,ARM64" \
    --work "_work" \
    --replace \
    --unattended \
    ${UPDATE_FLAGS[@]+"${UPDATE_FLAGS[@]}"}

  # Install hooks
  HOOKS_SRC="$(cd "$(dirname "$0")" && pwd)/hooks"
  if [[ -d "$HOOKS_SRC" ]]; then
    mkdir -p "$RUNNER_DIR/hooks"
    cp -f "$HOOKS_SRC"/*.sh "$RUNNER_DIR/hooks/" 2>/dev/null || true
    chmod +x "$RUNNER_DIR/hooks/"*.sh 2>/dev/null || true
  fi

  # launchd plist
  mkdir -p "$RUNNER_DIR/logs"
  mkdir -p "$(dirname "$PLIST_PATH")"

  cat >"$PLIST_PATH" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${PLIST_LABEL}</string>

    <key>WorkingDirectory</key>
    <string>${RUNNER_DIR}</string>

    <key>ProgramArguments</key>
    <array>
        <string>${RUNNER_DIR}/run.sh</string>
    </array>

    <key>RunAtLoad</key>
    <true/>

    <!-- The runner's own self-update (GitHub bumping the minimum agent
         version) shuts run.sh down with a clean UserCancelled exit, not a
         crash. SuccessfulExit=false left that clean exit unrestarted —
         observed 2026-07-16: three fresh registrations went permanently
         offline right after their first forced self-update, needing a
         manual launchctl kickstart -k. This is a long-running listener
         service; it should always come back regardless of exit status. -->
    <key>KeepAlive</key>
    <dict>
        <key>SuccessfulExit</key>
        <true/>
    </dict>

    <key>ThrottleInterval</key>
    <integer>5</integer>

    <key>StandardOutPath</key>
    <string>${RUNNER_DIR}/logs/stdout.log</string>

    <key>StandardErrorPath</key>
    <string>${RUNNER_DIR}/logs/stderr.log</string>

    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>${HOME}/.orbstack/bin:/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
        <key>HOMEBREW_PREFIX</key>
        <string>/opt/homebrew</string>
        <key>ACTIONS_RUNNER_HOOK_JOB_STARTED</key>
        <string>${RUNNER_DIR}/hooks/job-started.sh</string>
        <key>RUNNER_TOOL_CACHE</key>
        <string>${BASE_DIR}/.shared-tool-cache</string>
        <key>AGENT_TOOLSDIRECTORY</key>
        <string>${BASE_DIR}/.shared-tool-cache</string>
    </dict>
</dict>
</plist>
PLIST

  # Start
  launchctl bootout "gui/$(id -u)" "$PLIST_PATH" 2>/dev/null || true
  launchctl bootstrap "gui/$(id -u)" "$PLIST_PATH"

  sleep 1
  if launchctl print "gui/$(id -u)/${PLIST_LABEL}" &>/dev/null; then
    info "  Running!"
  else
    echo "  warning: may not have started. Check $RUNNER_DIR/logs/"
  fi
done

# ── Load watchdog timer ───────────────────────────────────────────────────────
# Pauses idle runners when sustained host load is high; resumes when it drops.
# Self-installing launchd timer (idempotent); opt out via ~/actions-runner/.no-load-watchdog.

info "Installing load watchdog timer..."
python3 "$SCRIPT_DIR/load-watchdog.py" --install-timer || echo "  warning: load watchdog timer install failed"

# ── Fleet maintenance timer ───────────────────────────────────────────────────
# Converges runners to runners.toml every 2h so idle/asleep hosts self-heal.
# Self-installing launchd timer (idempotent); opt out via ~/actions-runner/.no-auto-update.

info "Installing fleet maintenance timer..."
python3 "$SCRIPT_DIR/maintenance-timer.py" --install-timer || echo "  warning: maintenance timer install failed"

echo ""
info "Done! ${WORKERS} worker(s) installed for ${REPO}."
echo ""
echo "Management:"
echo "  Converge:  ./apply.py --dry-run"
echo "  Uninstall: ./uninstall.sh ${REPO}"
