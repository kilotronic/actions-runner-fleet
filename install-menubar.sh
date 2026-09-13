#!/usr/bin/env bash
# Install the SwiftBar menu bar plugin for GitHub Actions runner status.
#
# Usage:
#   ./install-menubar.sh
#
# Installs SwiftBar via Homebrew (if needed) and symlinks the plugin.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PLUGIN_SRC="$SCRIPT_DIR/runner-status.30s.py"
PLUGIN_DIR="$HOME/Code/gh/menubar-plugins"

die()  { echo "error: $*" >&2; exit 1; }
info() { echo "==> $*"; }

# ── Install SwiftBar ─────────────────────────────────────────────────────────

if ! brew list --cask swiftbar &>/dev/null; then
    info "Installing SwiftBar..."
    brew install --cask swiftbar
else
    info "SwiftBar already installed."
fi

# ── Install plugin ───────────────────────────────────────────────────────────

[[ -f "$PLUGIN_SRC" ]] || die "Plugin not found: $PLUGIN_SRC"
chmod +x "$PLUGIN_SRC"

mkdir -p "$PLUGIN_DIR"

PLUGIN_DEST="$PLUGIN_DIR/runner-status.30s.py"
if [[ -L "$PLUGIN_DEST" || -f "$PLUGIN_DEST" ]]; then
    info "Plugin already installed — updating symlink."
    rm -f "$PLUGIN_DEST"
fi

ln -s "$PLUGIN_SRC" "$PLUGIN_DEST"
info "Plugin symlinked: $PLUGIN_DEST -> $PLUGIN_SRC"

# ── Launch SwiftBar ──────────────────────────────────────────────────────────

if ! pgrep -q SwiftBar; then
    info "Starting SwiftBar..."
    open /Applications/SwiftBar.app
else
    info "SwiftBar is already running — it will pick up the plugin automatically."
fi

echo ""
info "Done! Runner status should appear in the menu bar."
echo "  Click the R: icon and use 'Refresh from GitHub' for API data."
