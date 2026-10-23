#!/usr/bin/env bash
# Set up a checkout for testing (macOS / Linux): a venv with requirements-dev.txt installed
# and the swi3score decode core built into it. Re-run any time; it refreshes both.
#
#     bash tools/setup_dev.sh            # install
#     bash tools/setup_dev.sh --test     # install, then run the functional suite
#
# The venv is the one ./run.sh uses (same location, same SWI3S_VENV override), so the app
# and the tests share one install. PYTHON=/path/to/python3 picks the interpreter.
set -euo pipefail
cd "$(dirname "$0")/.."

run_tests=0
for a in "$@"; do
    case "$a" in
        --test) run_tests=1 ;;
        -h|--help) sed -n '2,9p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "unknown option: $a (see --help)" >&2; exit 2 ;;
    esac
done

# The interpreters run.sh supports: 3.11-3.13.
supported() { "$1" -c 'import sys; raise SystemExit(0 if (3,11) <= sys.version_info[:2] <= (3,13) else 1)' 2>/dev/null; }
PY="${PYTHON:-}"
if [ -n "$PY" ]; then
    supported "$PY" || { echo "PYTHON=$PY is not Python 3.11-3.13." >&2; exit 1; }
else
    for c in python3.13 python3.12 python3.11 python3; do
        if command -v "$c" >/dev/null 2>&1 && supported "$c"; then PY="$c"; break; fi
    done
fi
[ -n "$PY" ] || { echo "Need Python 3.11-3.13. Install one, or set PYTHON=/path/to/python3.12" >&2; exit 1; }

# The decode core is C++ and needs the interpreter's C API headers (Python.h).
if ! "$PY" -c 'import os,sysconfig,sys; sys.exit(0 if os.path.isfile(os.path.join(sysconfig.get_paths()["include"],"Python.h")) else 1)' \
        && [ -z "${SKBUILD_CMAKE_DEFINE:-}" ]; then
    echo "$PY has no Python.h, which the decode core build needs." >&2
    echo "  Install python3-dev (Debian/Ubuntu) or python3-devel (RHEL/Fedora), or see the" >&2
    echo "  README section \"No root / no sudo\"; ./run.sh prints the full list of options." >&2
    exit 1
fi

# Same location as run.sh: off any cloud-synced folder, keyed to this checkout's path.
REPO="$(pwd)"
KEY="$(printf '%s' "$REPO" | (shasum 2>/dev/null || sha1sum) | cut -c1-12)"
VENV="${SWI3S_VENV:-${XDG_CACHE_HOME:-$HOME/.cache}/swi3s-studio/venv-$KEY}"
mkdir -p "$(dirname "$VENV")"
if [ ! -d "$VENV" ]; then
    echo "Creating virtual environment with $PY at $VENV ..."
    "$PY" -m venv "$VENV"
fi
VPY="$VENV/bin/python"

echo "Installing development dependencies (requirements-dev.txt)..."
"$VPY" -m pip install --upgrade pip >/dev/null
"$VPY" -m pip install -r requirements-dev.txt

echo "Building the swi3score decode core..."
# --force-reinstall: the core's version is fixed, so a plain install keeps a stale build.
"$VPY" -m pip install --force-reinstall --no-deps ./native
# run.sh's stamps: the dependencies and the core it would install are now current.
touch "$VENV/.deps-stamp" "$VENV/.native-stamp"

# Headless Linux needs a few system libraries for Qt's offscreen platform.
if ! QT_QPA_PLATFORM=offscreen "$VPY" -c 'from PySide6.QtWidgets import QApplication; QApplication([])' >/dev/null 2>&1; then
    echo "" >&2
    echo "Qt cannot start (offscreen). On Debian/Ubuntu install its runtime libraries:" >&2
    echo "  sudo apt-get install -y --no-install-recommends libegl1 libgl1 libxkbcommon0 libdbus-1-3 libglib2.0-0" >&2
fi

root_core=$("$VPY" -c 'import sysconfig; print("swi3score" + sysconfig.get_config_var("EXT_SUFFIX"))')
if [ -e "$root_core" ]; then
    echo "Note: ./$root_core at the checkout root is imported before the venv's build." >&2
    echo "  Rebuild it with: bash native/build_local.sh (or delete it)." >&2
fi

cat <<EOT

Ready. From the checkout root, the functional suite (perf deselected), the perf benchmarks
in their own process, and the release gate (suite, perf, ruff, mypy, ABI):
  source "$VENV/bin/activate"
  python -m pytest
  python -m pytest -m perf
  PYTHON="$VPY" bash tests/gate.sh
See docs/TESTING.md.
EOT

if [ "$run_tests" = 1 ]; then
    exec "$VPY" -m pytest -q
fi
