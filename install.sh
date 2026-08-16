#!/usr/bin/env bash
set -euo pipefail

# Hermes Chess Plugin — Setup Script
#
# Detects the OS and installs system dependencies, creates a Python venv,
# and verifies the installation.
#
# Usage:
#   chmod +x install.sh && ./install.sh
#   # or as root: sudo ./install.sh (for system packages only)

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
PLUGIN_DIR="${HERMES_HOME:-$HOME/.hermes}/plugins/hermes-chess"
VENV_DIR="${REPO_DIR}/.venv"
PYTHON="${PYTHON:-python3}"

echo "=== Hermes Chess Plugin — Setup ==="
echo ""

# ── Detect OS ──────────────────────────────────────────────────────
OS="$(uname -s)"
case "$OS" in
    Darwin)
        PKG_MGR="brew"
        PKG_LIST="stockfish cairo"
        CAIRO_LIBDIR="$(brew --prefix cairo 2>/dev/null)/lib"
        echo "[detect] macOS — using Homebrew"
        ;;
    Linux)
        if command -v dnf &>/dev/null; then
            PKG_MGR="dnf"
            PKG_LIST="stockfish cairo cairo-devel python3-devel"
            echo "[detect] Linux (dnf)"
        elif command -v apt &>/dev/null; then
            PKG_MGR="apt"
            PKG_LIST="stockfish libcairo2-dev python3-dev"
            echo "[detect] Linux (apt)"
        else
            echo "[ERROR] Unsupported package manager. Install manually:"
            echo "  stockfish, cairo, cairo-devel, python3-devel"
            exit 1
        fi
        ;;
    *)
        echo "[ERROR] Unsupported OS: $OS"
        exit 1
        ;;
esac

# ── Install system packages ────────────────────────────────────────
echo ""
echo "=== System Dependencies ==="
echo "  Package manager: $PKG_MGR"
echo "  Packages: $PKG_LIST"
echo ""

if [ "$PKG_MGR" = "brew" ]; then
    brew install $PKG_LIST 2>&1 | tail -5
elif [ "$PKG_MGR" = "dnf" ]; then
    sudo dnf install -y $PKG_LIST 2>&1 | tail -5
elif [ "$PKG_MGR" = "apt" ]; then
    sudo apt update -qq && sudo apt install -y $PKG_LIST 2>&1 | tail -5
fi

echo "[OK] System dependencies installed."

# ── Create Python venv ─────────────────────────────────────────────
echo ""
echo "=== Python Virtual Environment ==="

if [ ! -d "$VENV_DIR" ]; then
    echo "  Creating venv at $VENV_DIR ..."
    "$PYTHON" -m venv "$VENV_DIR"
fi

echo "  Installing Python packages ..."
"$VENV_DIR/bin/pip" install --quiet --upgrade pip
"$VENV_DIR/bin/pip" install --quiet python-chess CairoSVG

echo "[OK] Python dependencies installed."

# ── Install as Hermes plugin ────────────────────────────────────────
echo ""
echo "=== Hermes Plugin Installation ==="

if command -v hermes &>/dev/null; then
    echo "  Hermes detected — installing via 'hermes plugins install' ..."
    # Use the repo dir as source so hermes plugins install can work from local
    if [ -d "$PLUGIN_DIR" ]; then
        echo "  Plugin already installed at $PLUGIN_DIR, syncing source ..."
        rsync -a --delete \
            --exclude='.venv' --exclude='__pycache__' --exclude='*.pyc' \
            "$REPO_DIR/src/hermes_chess/" "$PLUGIN_DIR/"
    else
        # hermes plugins install works from GitHub; if we're already cloned do it manually
        mkdir -p "$(dirname "$PLUGIN_DIR")"
        cp -r "$REPO_DIR/src/hermes_chess" "$PLUGIN_DIR"
        echo "  Plugin installed at $PLUGIN_DIR"
    fi
    echo "  Run 'hermes gateway restart' to load the plugin."
else
    echo "  Hermes not detected. Manual install options:"
    echo "    hermes plugins install xunlinkx/hermes-chess"
    echo "    or clone directly:"
    echo "      git clone https://github.com/xunlinkx/hermes-chess.git ~/.hermes/plugins/hermes-chess"
fi

# ── Verification ───────────────────────────────────────────────────
echo ""
echo "=== Verification ==="

if "$VENV_DIR/bin/python" -c "import hermes_chess; print('Import: OK')" 2>&1; then
    echo "[OK] hermes_chess imported successfully."
else
    echo "[WARN] hermes_chess import failed. Check dependencies."
fi

if command -v stockfish &>/dev/null; then
    STOCKFISH_VER=$(stockfish --version 2>&1 || echo "version info not available")
    echo "[OK] Stockfish found: $(which stockfish)"
else
    echo "[WARN] stockfish not on PATH. Set HERMES_CHESS_STOCKFISH in your .env or gateway startup."
fi

if [ -n "${CAIRO_LIBDIR:-}" ]; then
    if ls "$CAIRO_LIBDIR"/libcairo* 2>/dev/null; then
        echo "[OK] libcairo found at $CAIRO_LIBDIR"
        echo ""
        echo "  REMINDER: Set on macOS for gateway:"
        echo "    export DYLD_LIBRARY_PATH=$CAIRO_LIBDIR:\$DYLD_LIBRARY_PATH"
    fi
fi

echo ""
echo "=== Done ==="
echo "  Source: $REPO_DIR/src/hermes_chess"
echo "  Venv:   $VENV_DIR"
echo ""
echo "  Run tests:  DYLD_LIBRARY_PATH=$CAIRO_LIBDIR $VENV_DIR/bin/pytest $REPO_DIR/tests/"
