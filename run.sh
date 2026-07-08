#!/usr/bin/env bash
# SWI3S Studio launcher (macOS / Linux). Creates a .venv on first run, installs the
# Python deps + builds the C++ decode core, then starts the app. Re-run any time - the
# venv and build are reused. Pass --rebuild to force a fresh build of the native core.
set -euo pipefail
cd "$(dirname "$0")"

# Pick a Python the wheels support: PySide6 / pyarrow / numpy publish wheels for
# 3.11-3.13, not 3.14+ (pip would fall back to building from source and fail). Honour a
# PYTHON override, else prefer the newest supported interpreter on PATH.
supported() { "$1" -c 'import sys; raise SystemExit(0 if (3,11) <= sys.version_info[:2] <= (3,13) else 1)' 2>/dev/null; }
PY="${PYTHON:-}"
if [ -n "$PY" ]; then
    if ! supported "$PY"; then
        echo "PYTHON=$PY is not in the supported 3.11-3.13 range ($("$PY" --version 2>&1))." >&2
        exit 1
    fi
else
    for c in python3.13 python3.12 python3.11 python3; do
        if command -v "$c" >/dev/null 2>&1 && supported "$c"; then PY="$c"; break; fi
    done
fi
if [ -z "$PY" ]; then
    echo "Need Python 3.11-3.13 (PySide6/pyarrow/numpy have no 3.14+ wheels yet). Install one and re-run, or set PYTHON=/path/to/python3.12" >&2
    exit 1
fi

if [ ! -d .venv ]; then
    echo "Creating virtual environment (.venv) with $PY..."
    "$PY" -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate

# Install/refresh deps only when requirements change (a stamp keeps re-runs fast).
STAMP=.venv/.deps-stamp
if [ ! -f "$STAMP" ] || [ requirements.txt -nt "$STAMP" ]; then
    echo "Installing Python dependencies..."
    python -m pip install --upgrade pip >/dev/null
    python -m pip install -r requirements.txt
    touch "$STAMP"
fi

# Build the native decode core if missing or if --rebuild was asked for.
if [ "${1:-}" = "--rebuild" ] || ! python -c 'import swi3score' 2>/dev/null; then
    echo "Building the swi3score decode core..."
    python -m pip install ./native
fi

exec env PYTHONPATH=. python -m swi3s_studio.app
