#!/usr/bin/env bash
# SWI3S Studio launcher (macOS / Linux). Creates a local venv on first run, installs the
# Python deps + builds the C++ decode core, then starts the app. Re-run any time - the
# venv and build are reused. Pass --rebuild to force a fresh build of the native core.
set -euo pipefail
cd "$(dirname "$0")"

# The arguments are read here, once. Everything except `--rebuild` is forwarded to the app.
# `--rebuild` is this script's own flag (the native build below), accepted in any position,
# and never reaches the app.
app_args=()
headless=0
rebuild=0
for a in "$@"; do
    case "$a" in
        --rebuild) rebuild=1; continue ;;
        -o|--output|-o=*|--output=*|-h|--help) headless=1 ;;
    esac
    app_args+=("$a")
done

# Pick a Python the launcher supports: 3.11-3.13, where every platform has PySide6 /
# pyarrow / numpy wheels. Honour a PYTHON override, else prefer the newest supported
# interpreter on PATH.
supported() { "$1" -c 'import sys; raise SystemExit(0 if (3,11) <= sys.version_info[:2] <= (3,13) else 1)' 2>/dev/null; }

# Does this interpreter have the C API headers (Python.h)? The decode core is C++, so a
# build needs them — and they're a separate package on most Linux distros. sysconfig
# reports the BASE prefix's include dir, so a venv on a headerless system Python answers
# no, which is correct: a venv doesn't supply them.
has_headers() {
    "$1" -c 'import os,sysconfig,sys; sys.exit(0 if os.path.isfile(os.path.join(sysconfig.get_paths()["include"],"Python.h")) else 1)' 2>/dev/null
}

# Printed whenever a build is needed but the headers are missing. Lists the options that
# need no root and no other interpreter first.
header_help() {
    cat >&2 <<EOF

The decode core is C++ and needs the Python development headers (Python.h).
  Interpreter : $1
  Include dir : $("$1" -c 'import sysconfig; print(sysconfig.get_paths()["include"])' 2>/dev/null)

The interpreter works; only its C API headers are missing. A virtualenv does NOT
supply them (it inherits its base prefix).

Without root — try in this order:
  1. Another Python on this host may already have them. Check each candidate with:
       PY=/path/to/python3; \$PY -c 'import os,sysconfig; p=sysconfig.get_paths()["include"]; \\
         print(p, "OK" if os.path.isfile(p+"/Python.h") else "MISSING")'
     (look via: module avail python, ls /usr/bin/python3.*, /tools, /org/...)
     Then re-run:  PYTHON=/that/python3 ./run.sh
  2. Install a self-contained Python (always has headers, no root):
       curl -LsSf https://astral.sh/uv/install.sh | sh
       export PATH="\$HOME/.local/bin:\$PATH" && uv python install 3.12
       PYTHON="\$(uv python find 3.12)" ./run.sh
  3. Keep this Python and extract the headers into \$HOME:
       mkdir -p ~/pyhdr && cd ~/pyhdr
       rpm2cpio python3-devel-\$(python3 -c 'import sys;print("%d.%d"%sys.version_info[:2])')*.rpm | cpio -idmv
       export SKBUILD_CMAKE_DEFINE="Python_INCLUDE_DIR=\$HOME/pyhdr/usr/include/pythonX.Y"
     then re-run ./run.sh

With root:  sudo dnf install python3-devel   (or: apt install python3-dev)
See the README section "No root / no sudo" for detail.
EOF
}

PY="${PYTHON:-}"
if [ -n "$PY" ]; then
    if ! supported "$PY"; then
        echo "PYTHON=$PY is not in the supported 3.11-3.13 range ($("$PY" --version 2>&1))." >&2
        exit 1
    fi
else
    # Two passes: prefer a supported interpreter that also has the headers, else fall back
    # to version-only (the module may already be built, in which case no build is needed).
    for c in python3.13 python3.12 python3.11 python3; do
        if command -v "$c" >/dev/null 2>&1 && supported "$c" && has_headers "$c"; then PY="$c"; break; fi
    done
    if [ -z "$PY" ]; then
        for c in python3.13 python3.12 python3.11 python3; do
            if command -v "$c" >/dev/null 2>&1 && supported "$c"; then PY="$c"; break; fi
        done
    fi
fi
if [ -z "$PY" ]; then
    echo "Need Python 3.11-3.13 (the versions this launcher supports). Install one and re-run, or set PYTHON=/path/to/python3.12" >&2
    exit 1
fi

# Keep the venv OFF any cloud-synced folder (iCloud Drive / OneDrive). Qt cannot
# enumerate its plugin directories on those file systems, so the GUI dies with
# "Could not find the Qt platform plugin cocoa" even though the plugins are present.
# Put it in a local cache dir keyed to this checkout's path (override: SWI3S_VENV=...).
REPO="$(pwd)"
KEY="$(printf '%s' "$REPO" | (shasum 2>/dev/null || sha1sum) | cut -c1-12)"
VENV="${SWI3S_VENV:-${XDG_CACHE_HOME:-$HOME/.cache}/swi3s-studio/venv-$KEY}"
mkdir -p "$(dirname "$VENV")"

if [ ! -d "$VENV" ]; then
    # No venv yet => the native core will have to be built. Check the headers before
    # creating the venv and downloading the wheels. Skipped when SKBUILD_CMAKE_DEFINE
    # already points the build at private headers.
    if ! has_headers "$PY" && [ -z "${SKBUILD_CMAKE_DEFINE:-}" ]; then
        echo "Cannot build the decode core with $PY: Python.h is missing." >&2
        header_help "$PY"
        exit 1
    fi
    echo "Creating virtual environment with $PY at $VENV ..."
    "$PY" -m venv "$VENV"
fi
# shellcheck disable=SC1091
source "$VENV/bin/activate"

# Install/refresh deps only when requirements change (a stamp keeps re-runs fast).
STAMP="$VENV/.deps-stamp"
installed_deps=0
if [ ! -f "$STAMP" ] || [ requirements.txt -nt "$STAMP" ]; then
    echo "Installing Python dependencies..."
    python -m pip install --upgrade pip >/dev/null
    python -m pip install -r requirements.txt
    touch "$STAMP"
    installed_deps=1
fi

# Build the native decode core when it's missing, out of date, or --rebuild was asked
# for. "Out of date" = a native/ source is newer than the last build (a stamp in the
# venv), so a git pull or edit that touches the C++ auto-rebuilds - the user never has
# to know a rebuild is needed. (build/ artefacts are excluded from the staleness check.)
NATIVE_STAMP="$VENV/.native-stamp"
need_native=0
if [ "$rebuild" = 1 ] || ! python -c 'import swi3score' 2>/dev/null; then
    need_native=1
elif [ ! -f "$NATIVE_STAMP" ] || \
     [ -n "$(find native -type f -not -path '*/build/*' -newer "$NATIVE_STAMP" -print -quit 2>/dev/null)" ]; then
    need_native=1
fi
if [ "$need_native" = 1 ]; then
    # Same check as the fresh-venv path above, for the cases that reach a build with a
    # venv already in place (--rebuild, or a native/ edit). `python` is the venv's, whose
    # base prefix is $PY's, so this asks the same question of the same headers.
    if ! has_headers python && [ -z "${SKBUILD_CMAKE_DEFINE:-}" ]; then
        echo "Cannot build the decode core: Python.h is missing." >&2
        header_help "$(command -v python)"
        exit 1
    fi
    echo "Building the swi3score decode core..."
    # --force-reinstall because the native version is a fixed 0.1.0, so a plain install
    # no-ops and keeps a stale .so. --no-deps skips re-resolving the app deps.
    if ! python -m pip install --force-reinstall --no-deps ./native; then
        # List the usual causes; do not assert one.
        echo "swi3score build failed; not stamping - will retry next run." >&2
        echo "  The build output above names the cause. The usual ones are:" >&2
        echo "    * 'Python.h: No such file or directory' or 'Python development" >&2
        echo "      HEADERS not found' -> install python3-devel (RHEL/Fedora) or" >&2
        echo "      python3-dev (Debian/Ubuntu); a venv inherits its base prefix, so" >&2
        echo "      the system Python needs them even inside a venv." >&2
        echo "    * 'No CMAKE_CXX_COMPILER could be found' -> install a C++17 compiler." >&2
        exit 1
    fi
    touch "$NATIVE_STAMP"
fi

# Announce the launch; the first launch after an install is slow while the OS verifies the
# Qt libraries. -o implies headless, so say nothing about a window that will not open. The
# app decides for real; this only picks the message.
if [ "$headless" = 1 ]; then
    :
elif [ "$installed_deps" = 1 ]; then
    echo "Starting SWI3S Studio - the first launch after an install is slow while the OS"
    echo "verifies the newly installed Qt libraries. Later runs start in about a second."
else
    echo "Starting SWI3S Studio..."
fi

# Under PYTHONPATH=. a decode core built at the checkout root (native/build_local.sh, which
# the gate runs) is imported instead of the venv's. Name it so a stale one is noticed.
root_core=$(python -c 'import sysconfig; print("swi3score" + sysconfig.get_config_var("EXT_SUFFIX"))')
if [ -e "$root_core" ]; then
    echo "Note: using ./$root_core from the checkout root, not the venv's build." >&2
    echo "  After a native/ change, rebuild it with: bash native/build_local.sh (or delete it)." >&2
fi

# Tell the app what to call itself in `usage:` — see cli.program_name().
exec env PYTHONPATH=. SWI3S_LAUNCHER=./run.sh python -m swi3s_studio.app "${app_args[@]+"${app_args[@]}"}"
