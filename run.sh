#!/usr/bin/env bash
# SWI3S Studio launcher (macOS / Linux). Creates a local venv on first run, installs the
# Python deps + builds the C++ decode core, then starts the app. Re-run any time - the
# venv and build are reused. Pass --rebuild to force a fresh build of the native core.
set -euo pipefail
cd "$(dirname "$0")"

# Pick a Python the wheels support: PySide6 / pyarrow / numpy publish wheels for
# 3.11-3.13, not 3.14+ (pip would fall back to building from source and fail). Honour a
# PYTHON override, else prefer the newest supported interpreter on PATH.
supported() { "$1" -c 'import sys; raise SystemExit(0 if (3,11) <= sys.version_info[:2] <= (3,13) else 1)' 2>/dev/null; }

# Does this interpreter have the C API headers (Python.h)? The decode core is C++, so a
# build needs them — and they're a separate package on most Linux distros. sysconfig
# reports the BASE prefix's include dir, so a venv on a headerless system Python answers
# no, which is correct: a venv doesn't supply them.
has_headers() {
    "$1" -c 'import os,sysconfig,sys; sys.exit(0 if os.path.isfile(os.path.join(sysconfig.get_paths()["include"],"Python.h")) else 1)' 2>/dev/null
}

# Printed whenever we need to build but have no headers. Managed build hosts often have
# several Pythons and no sudo, so lead with the options that need neither.
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
    # Two passes: prefer an interpreter that ALSO has the headers, so a host with a
    # headerless system python3 but a usable toolchain python is picked automatically
    # rather than failing the build minutes later. Falls back to version-only if none
    # has them (the module may already be built, in which case no build is needed).
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
    echo "Need Python 3.11-3.13 (PySide6/pyarrow/numpy have no 3.14+ wheels yet). Install one and re-run, or set PYTHON=/path/to/python3.12" >&2
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
    # No venv yet => the native core will certainly have to be built. Check the headers
    # NOW rather than after creating the venv and downloading ~200 MB of wheels: on a
    # headerless host that work is wasted and the real error arrives minutes later.
    # Skipped when SKBUILD_CMAKE_DEFINE already points the build at private headers.
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
if [ "${1:-}" = "--rebuild" ] || ! python -c 'import swi3score' 2>/dev/null; then
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
    # --force-reinstall because the native version is a fixed 0.1.0: a plain install
    # no-ops ("Requirement already satisfied") and silently keeps a stale .so after a
    # source change, which then trips the ABI check at import. --no-deps keeps it from
    # re-resolving the app deps on every rebuild.
    if ! python -m pip install --force-reinstall --no-deps ./native; then
        # Don't guess at the cause. This used to say "a C++ compiler is required",
        # which was wrong for the most common real failure — a working g++ but no
        # Python development headers — and sent people looking for a compiler that
        # was already installed and had just compiled two of the thirteen objects.
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

# Say that the launch has started. Everything above prints as it works, so without this
# the last thing on screen is a pip line and the window is the next event - which reads as
# a stall, especially right after an install: the OS verifies the ~450 MB of Qt libraries
# the first time they are loaded, so the window can take a while. A warm start is ~1s.
if [ "$installed_deps" = 1 ]; then
    echo "Starting SWI3S Studio - the first launch after an install is slow while the OS"
    echo "verifies the newly installed Qt libraries. Later runs start in about a second."
else
    echo "Starting SWI3S Studio..."
fi

exec env PYTHONPATH=. python -m swi3s_studio.app
