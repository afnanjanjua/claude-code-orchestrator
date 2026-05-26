#!/usr/bin/env bash
# claude-code-orchestrator — optional install helper.
# Symlinks poll.py to ~/.local/bin/claude-code-orchestrator so you can run
# it as `claude-code-orchestrator --watch` from anywhere.
#
# Usage:
#   ./install.sh             # symlink to ~/.local/bin
#   ./install.sh --uninstall # remove the symlink

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET="$SCRIPT_DIR/poll.py"
BIN_DIR="${HOME}/.local/bin"
LINK="$BIN_DIR/claude-code-orchestrator"

if [[ "${1:-}" == "--uninstall" ]]; then
    if [[ -L "$LINK" ]]; then
        rm "$LINK"
        echo "Removed $LINK"
    else
        echo "Nothing to uninstall at $LINK"
    fi
    exit 0
fi

mkdir -p "$BIN_DIR"
chmod +x "$TARGET"
ln -sf "$TARGET" "$LINK"

echo "Installed: $LINK -> $TARGET"
echo
if ! echo ":$PATH:" | grep -q ":$BIN_DIR:"; then
    echo "NOTE: $BIN_DIR is not on your PATH."
    echo "Add this to your ~/.zshrc or ~/.bashrc:"
    echo "    export PATH=\"\$HOME/.local/bin:\$PATH\""
    echo
fi
echo "Now you can run: claude-code-orchestrator --watch"
