#!/usr/bin/env bash
# One-command install for agentbell: prefers pipx, falls back to
# pip --user, falls back to a plain copy of the single source file.
#
# No build step. License keys are Ed25519-signed and every copy of
# agentbell.py verifies them with the public key it already contains
# (see DECISIONS.md 2b) - there is nothing to inject.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN_DIR="${XDG_BIN_HOME:-$HOME/.local/bin}"

if ! command -v python3 >/dev/null 2>&1; then
    echo "error: python3 not found. agentbell needs Python 3.9+." >&2
    echo "  Debian/Ubuntu:  sudo apt install python3" >&2
    echo "  macOS:          brew install python3" >&2
    exit 1
fi

cleanup() {
    rm -f "${ERR_LOG:-/nonexistent}"
}

# Every attempt keeps its error output. Swallowing it made a failing install
# look like one that simply chose another method - and when all of them
# failed, the user was told nothing at all.
ERR_LOG="$(mktemp)"

install_via_pipx() {
    command -v pipx >/dev/null 2>&1 || return 1
    if ! pipx install --force "$SCRIPT_DIR" >/dev/null 2>>"$ERR_LOG"; then
        echo "pipx failed, trying pip --user..." >&2
        return 1
    fi
    METHOD="pipx"
    return 0
}

install_via_pip() {
    if ! python3 -m pip install --user --quiet "$SCRIPT_DIR" >/dev/null 2>>"$ERR_LOG"; then
        echo "pip --user failed, falling back to a standalone copy..." >&2
        return 1
    fi
    BIN_DIR="$(python3 -m site --user-base)/bin"
    METHOD="pip --user"
    return 0
}

install_via_copy() {
    mkdir -p "$BIN_DIR" 2>>"$ERR_LOG" || return 1
    cp "$SCRIPT_DIR/agentbell.py" "$BIN_DIR/agentbell" 2>>"$ERR_LOG" || return 1
    chmod +x "$BIN_DIR/agentbell" 2>>"$ERR_LOG" || return 1
    METHOD="standalone copy"
    return 0
}

trap cleanup EXIT

METHOD=""
if ! { install_via_pipx || install_via_pip || install_via_copy; }; then
    echo "error: every install method failed (pipx, pip --user, standalone copy)." >&2
    echo >&2
    echo "--- what they reported ---" >&2
    cat "$ERR_LOG" >&2
    echo "--------------------------" >&2
    echo "You can still run it straight from this checkout:" >&2
    echo "  python3 $SCRIPT_DIR/agentbell.py doctor" >&2
    exit 1
fi
hash -r 2>/dev/null || true

echo "installed agentbell via ${METHOD}"

if command -v agentbell >/dev/null 2>&1; then
    echo "  -> $(command -v agentbell)"
    echo
    echo "Next step - the setup wizard (topic, quiet hours, agent hooks, test push):"
    echo
    echo "  agentbell init"
    echo
    echo "Then, any time something looks wrong:  agentbell doctor"
else
    # Figure out which rc file to point at, so the user can paste one line.
    case "${SHELL##*/}" in
        zsh)  RC="$HOME/.zshrc" ;;
        bash) RC="$HOME/.bashrc" ;;
        fish) RC="$HOME/.config/fish/config.fish" ;;
        *)    RC="$HOME/.profile" ;;
    esac
    echo "  -> $BIN_DIR/agentbell (not on your PATH yet)"
    echo
    echo "Copy & paste these two lines:"
    echo
    if [ "${SHELL##*/}" = "fish" ]; then
        echo "  fish_add_path $BIN_DIR"
    else
        echo "  echo 'export PATH=\"$BIN_DIR:\$PATH\"' >> \"$RC\" && export PATH=\"$BIN_DIR:\$PATH\""
    fi
    echo "  agentbell init"
fi
