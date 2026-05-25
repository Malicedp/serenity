#!/usr/bin/env bash

# ── Always run from the directory this script lives in ────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo ""
echo " Starting Serenity..."
echo ""

# ── Check Python 3.11 is available ────────────────────────────────────────
# Prefer an explicit python3.11 binary, fall back to python3/python
if command -v python3.11 &>/dev/null; then
    PYTHON=python3.11
elif command -v python3 &>/dev/null; then
    PYTHON=python3
elif command -v python &>/dev/null; then
    PYTHON=python
else
    echo ""
    echo " [ERROR] Python not found on PATH."
    echo ""
    echo " Serenity requires Python 3.11 exactly."
    echo " Download: https://www.python.org/downloads/release/python-3119/"
    exit 1
fi

# ── Verify it is exactly 3.11 ─────────────────────────────────────────────
PY_OK=$($PYTHON -c "import sys; print(1 if sys.version_info[:2]==(3,11) else 0)" 2>/dev/null)
if [ "$PY_OK" != "1" ]; then
    echo ""
    echo " [ERROR] Wrong Python version."
    echo ""
    echo " Found:    $($PYTHON --version 2>&1)"
    echo " Required: Python 3.11"
    echo ""
    echo " Serenity's core is compiled for Python 3.11 and will not"
    echo " run on any other version."
    echo ""
    echo " Download Python 3.11: https://www.python.org/downloads/release/python-3119/"
    exit 1
fi

# ── Kill any existing gateway process ─────────────────────────────────────
pkill -f "sera gateway" 2>/dev/null || true
sleep 1

# ── Install / update all dependencies ─────────────────────────────────────
echo " Installing dependencies..."
if ! $PYTHON -m pip install -e ".[senses,spotify,obs]" -q --no-warn-script-location; then
    echo " pip install failed — attempting repair..."
    if ! $PYTHON -m pip install -e ".[senses,spotify,obs]" --force-reinstall -q --no-warn-script-location; then
        echo " [ERROR] Repair failed. Check your Python install."
        exit 1
    fi
fi

echo " Dependencies OK."
echo ""

# ── Install GitNexus if npm is available ──────────────────────────────────
if ! command -v npm &>/dev/null; then
    echo " [INFO] npm not found - GitNexus skipped. Install Node.js to enable code analysis."
    echo ""
else
    if ! command -v gitnexus &>/dev/null; then
        echo " Installing GitNexus (code analysis)..."
        if npm install -g gitnexus --silent 2>/dev/null; then
            echo " GitNexus installed."
        else
            echo " [WARNING] GitNexus install failed. Skipping."
        fi
    else
        echo " GitNexus already installed."
    fi
    echo ""

    # ── Index the repo if .gitnexus does not exist yet ────────────────────
    if [ ! -d "$SCRIPT_DIR/.gitnexus" ]; then
        echo " Indexing codebase with GitNexus..."
        gitnexus analyze "$SCRIPT_DIR" 2>/dev/null && echo " GitNexus index ready." || true
        echo ""
    fi
fi

# ── First-run: launch setup wizard if no config exists ────────────────────
CONFIG_FILE="$HOME/.serenity/config.json"
if [ ! -f "$CONFIG_FILE" ]; then
    echo " No config found - launching setup wizard..."
    echo ""
    exec serenity
fi

# ── Launch the gateway ─────────────────────────────────────────────────────
sera gateway
