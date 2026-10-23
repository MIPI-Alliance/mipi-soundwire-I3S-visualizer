#!/usr/bin/env bash
# Thin wrapper over tools/gate.py, the one gate implementation for macOS, Linux and
# Windows. See tests/gate.ps1 for the PowerShell entry point.
# Keep this a wrapper; put logic in tools/gate.py.
set -uo pipefail
cd "$(dirname "$0")/.." || exit 2
exec "${PYTHON:-python3}" tools/gate.py "$@"
