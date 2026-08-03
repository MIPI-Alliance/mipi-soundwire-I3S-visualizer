#!/usr/bin/env bash
# Thin wrapper — the gate lives in tools/gate.py so there is ONE implementation for macOS,
# Linux and Windows. See tests/gate.ps1 for the PowerShell entry point.
#
# This file used to BE the gate, which made it Unix-only and quietly excluded the Windows
# half of the release gate: no bash on PATH there, and native/build_local.sh is a Unix
# build path. Keep this a wrapper; put logic in tools/gate.py.
set -uo pipefail
cd "$(dirname "$0")/.." || exit 2
exec "${PYTHON:-python3}" tools/gate.py "$@"
