#!/usr/bin/env bash
# Install and configure GitHub Actions self-hosted runners on Debian/Ubuntu Linux.
#
# Usage:
#   ./install-linux.sh <owner/repo> [workers]
#   ./install-linux.sh acme/app        # 1 worker (default)
#   ./install-linux.sh acme/app 3      # 3 parallel workers
#
# Prerequisites: gh (will be installed if missing) authenticated as the user who
# can register runners on the repo. Run `gh auth login` before running this.
#
# Layout:
#   ~/actions-runner/<repo>-1/   (worker 1)
#   ~/actions-runner/<repo>-2/   (worker 2)
#   ...
#
# Each worker gets its own systemd --user unit and registration.
# Linger is enabled for the user so runners survive logout.

set -euo pipefail

# ── Configuration ────────────────────────────────────────────────────────────

RUNNER_VERSION="2.336.0"
RUNNER_ARCH="linux-x64"
RUNNER_TARBALL="actions-runner-${RUNNER_ARCH}-${RUNNER_VERSION}.tar.gz"
RUNNER_URL="https://github.com/actions/runner/releases/download/v${RUNNER_VERSION}/${RUNNER_TARBALL}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# ── Helpers ──────────────────────────────────────────────────────────────────

die() {
  echo "error: $*" >&2
  exit 1
}
info() { echo "==> $*"; }

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
HOSTNAME_S="$(hostname -s)"
# Port for the dedicated CI Postgres container. Kept off 5432 so it never
# collides with a developer's `make db` instance on a dual-use host. Exported
# into each runner's .env so the CI workflow connects to the right instance.
CI_PG_PORT="${CI_PG_PORT:-5433}"
# Cap what the CI workflow's `pytest -n auto` resolves to on self-hosted runners.
# `-n auto` uses one worker per core; on a dual-use box each E2E worker also spawns
# a headless Chromium + Flask, and one-per-core saturates RAM/scheduler enough to
# flake timing-sensitive UI tests. Half the cores (min 2) keeps unit-test
# parallelism high while giving each browser headroom. pytest-xdist reads this env.
XDIST_AUTO_WORKERS="${PYTEST_XDIST_AUTO_NUM_WORKERS:-$(($(nproc) / 2 > 2 ? $(nproc) / 2 : 2))}"
# Per-host worker count for the partygame CI browser-e2e pass. Declared as
# e2e_workers in runners.toml and exported by apply.py (the old per-host
# hardcode moved there); browser e2e is bound by *performance* cores, so pick
# the value from fleet.md's "E2E worker budget". Empty = ci.yml default (1).
HOOKS_DIR="$BASE_DIR/hooks"
SYSTEMD_USER_DIR="$HOME/.config/systemd/user"

if ! [[ "$WORKERS" =~ ^[1-9][0-9]*$ ]]; then
  die "workers must be a positive integer, got: $WORKERS"
fi

# ── Sanity checks ────────────────────────────────────────────────────────────

[[ "$(id -u)" -ne 0 ]] || die "Run as your normal user, not root. The script will sudo as needed."
command -v sudo &>/dev/null || die "sudo is required."
command -v systemctl &>/dev/null || die "systemd is required."
[[ -f /etc/debian_version ]] || die "This script targets Debian/Ubuntu (apt-based)."

# Detect distro family for Docker's apt repo (debian and ubuntu have separate repos).
DISTRO_ID="$(. /etc/os-release && echo "${ID}")"
case "$DISTRO_ID" in
  debian) DOCKER_REPO_PATH="debian" ;;
  ubuntu) DOCKER_REPO_PATH="ubuntu" ;;
  *) die "Unsupported distro: $DISTRO_ID (expected debian or ubuntu)" ;;
esac

# ── Prerequisites via apt ────────────────────────────────────────────────────

info "Updating apt package lists..."
sudo apt-get update -qq

# Base packages
APT_DEPS=(
  curl ca-certificates gnupg lsb-release
  git jq
  python3 python3-pip python3-venv
)

# Node + npm: only pull the distro packages when Node is absent. Hosts using
# the NodeSource repo already have nodejs (which bundles npm), and the distro
# `npm` package Conflicts with NodeSource's nodejs — installing it would abort
# the whole apt transaction.
if ! command -v node &>/dev/null || ! command -v npm &>/dev/null; then
  APT_DEPS+=(nodejs npm)
fi

info "Installing base packages..."
sudo apt-get install -y -qq "${APT_DEPS[@]}"

# gh CLI (from GitHub's apt repo)
if ! command -v gh &>/dev/null; then
  info "Installing GitHub CLI..."
  sudo mkdir -p -m 755 /etc/apt/keyrings
  curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
    | sudo tee /etc/apt/keyrings/githubcli-archive-keyring.gpg >/dev/null
  sudo chmod go+r /etc/apt/keyrings/githubcli-archive-keyring.gpg
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
    | sudo tee /etc/apt/sources.list.d/github-cli.list >/dev/null
  sudo apt-get update -qq
  sudo apt-get install -y -qq gh
fi

# uv (single-user install, official installer)
if ! command -v uv &>/dev/null && [[ ! -x "$HOME/.local/bin/uv" ]]; then
  info "Installing uv..."
  curl -LsSf https://astral.sh/uv/install.sh | sh
fi

# Docker Engine (official Docker apt repo)
if ! command -v docker &>/dev/null; then
  info "Installing Docker Engine from Docker's official repo..."
  sudo install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/debian/gpg \
    | sudo tee /etc/apt/keyrings/docker.asc >/dev/null
  sudo chmod a+r /etc/apt/keyrings/docker.asc
  DEB_CODENAME="$(. /etc/os-release && echo "${VERSION_CODENAME}")"
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/${DOCKER_REPO_PATH} ${DEB_CODENAME} stable" \
    | sudo tee /etc/apt/sources.list.d/docker.list >/dev/null
  sudo apt-get update -qq
  sudo apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
  sudo systemctl enable --now docker
fi

# Add user to docker group so the runner can reach the Docker socket without sudo.
if ! id -nG "$USER" | tr ' ' '\n' | grep -qx docker; then
  info "Adding $USER to docker group..."
  sudo usermod -aG docker "$USER"
fi

# The systemd --user manager caches its supplementary groups from when it first
# started. If it predates the docker-group membership (common: the user was
# already logged in before this script ran), runner services started under it
# inherit the stale set and container jobs fail with "No container runtime".
# Restart the manager so newly-started runners pick up the docker group. Login
# session scopes are separate, so this does not log the user out.
DOCKER_GID="$(getent group docker | cut -d: -f3)"
MANAGER_PID="$(pgrep -u "$USER" -f 'systemd --user' | head -1 || true)"
if [[ -n "$DOCKER_GID" && -n "$MANAGER_PID" ]] \
  && ! awk '/^Groups:/' "/proc/$MANAGER_PID/status" | grep -qw "$DOCKER_GID"; then
  info "Refreshing user systemd manager so runners inherit the docker group..."
  sudo systemctl restart "user@$(id -u "$USER").service"
fi

# Python 3.11+ (tomllib)
PY_MAJOR=$(python3 -c 'import sys; print(sys.version_info.major)')
PY_MINOR=$(python3 -c 'import sys; print(sys.version_info.minor)')
if [[ "$PY_MAJOR" -lt 3 ]] || [[ "$PY_MAJOR" -eq 3 && "$PY_MINOR" -lt 11 ]]; then
  die "Python 3.11+ required, found ${PY_MAJOR}.${PY_MINOR}."
fi

gh auth status &>/dev/null || die "gh is not authenticated. Run: gh auth login"

info "All prerequisites satisfied."

# ── Enable linger so user services survive logout ────────────────────────────

if [[ "$(loginctl show-user "$USER" 2>/dev/null | grep -c 'Linger=yes')" -eq 0 ]]; then
  info "Enabling linger for $USER (so runners survive logout)..."
  sudo loginctl enable-linger "$USER"
fi

# ── Download runner (shared cache) ───────────────────────────────────────────

mkdir -p "$CACHE_DIR"

# Record this checkout's path so update-host.sh (called from job hooks) can
# find it for `git pull`.
echo "$SCRIPT_DIR" >"$BASE_DIR/.repo-path"

if [[ ! -f "$CACHE_DIR/$RUNNER_TARBALL" ]]; then
  info "Downloading GitHub Actions runner v${RUNNER_VERSION} (${RUNNER_ARCH})..."
  curl -fsSL -o "$CACHE_DIR/$RUNNER_TARBALL" "$RUNNER_URL"
else
  info "Runner tarball already cached."
fi

# Verify checksum
info "Verifying checksum..."
RELEASE_BODY=$(gh api "repos/actions/runner/releases/tags/v${RUNNER_VERSION}" --jq '.body')
EXPECTED_HASH=$(echo "$RELEASE_BODY" | sed -n "s/.*<!-- BEGIN SHA ${RUNNER_ARCH} -->\([a-f0-9]*\)<!-- END SHA.*/\1/p")
[[ -n "$EXPECTED_HASH" ]] || die "Could not extract checksum from release notes"
ACTUAL_HASH=$(sha256sum "$CACHE_DIR/$RUNNER_TARBALL" | awk '{print $1}')
if [[ "$EXPECTED_HASH" != "$ACTUAL_HASH" ]]; then
  rm -f "$CACHE_DIR/$RUNNER_TARBALL"
  die "Checksum mismatch: expected ${EXPECTED_HASH}, got ${ACTUAL_HASH}"
fi

# ── Install runner dependencies (libicu et al.) ──────────────────────────────

# The runner ships an installdependencies.sh; run it once on first install.
DEPS_MARKER="$CACHE_DIR/.deps-installed-${RUNNER_VERSION}"
if [[ ! -f "$DEPS_MARKER" ]]; then
  info "Installing runner OS dependencies (libicu, etc.)..."
  TMPX="$(mktemp -d)"
  tar xzf "$CACHE_DIR/$RUNNER_TARBALL" -C "$TMPX" ./bin/installdependencies.sh
  sudo "$TMPX/bin/installdependencies.sh"
  rm -rf "$TMPX"
  touch "$DEPS_MARKER"
fi

# ── Install job hooks and sounds ─────────────────────────────────────────────

info "Installing job hooks..."
mkdir -p "$HOOKS_DIR"
cp "$SCRIPT_DIR/hooks/"*.sh "$HOOKS_DIR/"
chmod +x "$HOOKS_DIR/"*.sh

# ── Install each worker ──────────────────────────────────────────────────────

mkdir -p "$SYSTEMD_USER_DIR"

for i in $(seq 1 "$WORKERS"); do
  RUNNER_DIR="$BASE_DIR/${REPO_NAME}-${i}"
  RUNNER_NAME="${HOSTNAME_S}-${REPO_NAME}-${i}"
  UNIT_NAME="github-runner-${REPO_NAME}-${i}.service"
  UNIT_PATH="$SYSTEMD_USER_DIR/$UNIT_NAME"

  echo ""
  info "Worker ${i}/${WORKERS}: ${RUNNER_NAME}"

  if [[ -f "$RUNNER_DIR/.runner" ]]; then
    info "  Already configured — skipping (uninstall first to reconfigure)"
    continue
  fi

  mkdir -p "$RUNNER_DIR"
  info "  Extracting runner..."
  tar xzf "$CACHE_DIR/$RUNNER_TARBALL" -C "$RUNNER_DIR"

  info "  Configuring job hooks..."
  cat >"$RUNNER_DIR/.env" <<ENV
LANG=en_US.UTF-8
ACTIONS_RUNNER_HOOK_JOB_STARTED=${HOOKS_DIR}/job-started.sh
ACTIONS_RUNNER_HOOK_JOB_COMPLETED=${HOOKS_DIR}/job-completed.sh
CI_PG_PORT=${CI_PG_PORT}
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

  info "  Obtaining registration token..."
  REG_TOKEN=$(gh api "repos/${REPO}/actions/runners/registration-token" --method POST --jq '.token')
  [[ -n "$REG_TOKEN" ]] || die "Failed to obtain registration token"

  info "  Configuring..."
  "$RUNNER_DIR/config.sh" \
    --url "https://github.com/${REPO}" \
    --token "$REG_TOKEN" \
    --name "$RUNNER_NAME" \
    --labels "self-hosted,Linux,X64" \
    --work "_work" \
    --replace \
    --unattended

  mkdir -p "$RUNNER_DIR/logs"

  cat >"$UNIT_PATH" <<UNIT
[Unit]
Description=GitHub Actions Runner (${RUNNER_NAME})
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=${RUNNER_DIR}
ExecStart=${RUNNER_DIR}/run.sh
# on-failure only restarts a non-zero exit. The runner's own self-update
# (GitHub bumping the minimum agent version) stops run.sh with a clean
# UserCancelled exit (0), not a crash — on-failure left that unrestarted.
# This is a long-running listener; it should always come back. See the
# macOS install.sh KeepAlive.SuccessfulExit comment for the same gap.
Restart=always
RestartSec=5
# control-group (the systemd default): on stop/restart, SIGTERM every process in
# the unit's cgroup — crucially the Runner.Listener itself, which traps SIGTERM,
# deregisters its GitHub session, and exits cleanly; the fresh unit then claims a
# clean session. KillMode=process SIGTERM'd only run.sh and left the listener
# child orphaned (kept serving GitHub unmanaged, silently dropped offline when it
# died). KillMode=mixed is no better here: the listener is a *child*, so mixed
# SIGKILLs it without a deregister, wedging the server-side session ("a session
# for this runner already exists") on the next start. Restarting an idle runner
# is therefore clean; restart only when idle (a job in flight is interrupted).
KillMode=control-group
KillSignal=SIGTERM
TimeoutStopSec=5min
Environment=PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:%h/.local/bin

[Install]
WantedBy=default.target
UNIT

  systemctl --user daemon-reload
  systemctl --user enable --now "$UNIT_NAME"

  sleep 1
  if systemctl --user is-active --quiet "$UNIT_NAME"; then
    info "  Running!"
  else
    echo "  warning: may not have started. Check: systemctl --user status $UNIT_NAME"
  fi
done

# ── Container runtime verification ───────────────────────────────────────────

info "Verifying container runtime..."
sudo systemctl is-active --quiet docker \
  || die "Docker daemon is not running. Check: sudo systemctl status docker"
sudo docker info &>/dev/null \
  || die "Docker daemon is not responding to API calls."
sudo docker run --rm hello-world &>/dev/null \
  || die "Docker failed to run a test container. Container actions will fail until this is fixed."
info "Docker runtime verified (daemon active, API responsive, test container ran)."

# ── Load watchdog timer ───────────────────────────────────────────────────────
# Pauses idle runners when sustained host load is high; resumes when it drops.
# Self-installing systemd --user timer (idempotent); opt out via ~/actions-runner/.no-load-watchdog.

info "Installing load watchdog timer..."
python3 "$SCRIPT_DIR/load-watchdog.py" --install-timer || echo "  warning: load watchdog timer install failed"

# ── Fleet maintenance timer ───────────────────────────────────────────────────
# Converges runners to runners.toml every 2h so idle/asleep hosts self-heal.
# Self-installing systemd --user timer (idempotent); opt out via ~/actions-runner/.no-auto-update.

info "Installing fleet maintenance timer..."
python3 "$SCRIPT_DIR/maintenance-timer.py" --install-timer || echo "  warning: maintenance timer install failed"

echo ""
info "Done! ${WORKERS} worker(s) installed for ${REPO}."
echo ""
echo "Management:"
echo "  Status:    systemctl --user status 'github-runner-${REPO_NAME}-*'"
echo "  Logs:      journalctl --user -u 'github-runner-${REPO_NAME}-*' -f"
echo "  Stop:      systemctl --user stop 'github-runner-${REPO_NAME}-*'"
echo "  Uninstall: ./uninstall-linux.sh ${REPO}"
