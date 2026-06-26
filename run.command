#!/bin/bash
# ATC Tracker — macOS launcher
# Double-click this file in Finder to open a Terminal and start the tracker.
# Or run it manually: bash run.command

# ── Go to the folder containing this script ───────────────────────────────────
cd "$(dirname "$0")"

# ── Load credentials from .env ────────────────────────────────────────────────
if [ -f .env ]; then
    set -a
    source .env
    set +a
else
    echo "⚠  No .env file found."
    echo "   Copy .env.example to .env and fill in your Telegram credentials."
    echo ""
fi

# ── Create virtual environment on first run ───────────────────────────────────
if [ ! -d venv ]; then
    echo "First run — creating virtual environment..."
    python3 -m venv venv
    echo "Installing dependencies (this may take a few minutes on first run)..."
    venv/bin/pip install --upgrade pip --quiet
    venv/bin/pip install -r requirements.txt --quiet
    echo "✓ Setup complete."
    echo ""
fi

# ── Launch ────────────────────────────────────────────────────────────────────
echo "Starting ATC Tracker — press K to toggle keywords, Q to quit."
echo ""
exec venv/bin/python atc_tracker.py
