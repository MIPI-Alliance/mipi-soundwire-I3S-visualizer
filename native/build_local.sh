#!/usr/bin/env bash
# Offline build of the swi3score pybind11 module (no PyPI / scikit-build-core).
#
# Compiles the C++ decode core + the ISampleSource/Decoder/Demo +
# pybind11 bindings straight into an importable extension. The normal build path
# is `pip install ./native` (scikit-build-core); use this when PyPI is
# unreachable. Requires a C++ compiler (clang++ preferred, g++ accepted, $CXX
# honoured), Python dev headers, and pybind11 headers: $PYBIND11_INCLUDE, else those of
# the pybind11 package installed for the interpreter.
#
# Usage:  bash native/build_local.sh   then   PYTHONPATH=. python3 -c "import swi3score"
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
# The decode core sources in swi3score/core are compiled directly (see CMakeLists.txt).
CORE_SRC="$HERE/swi3score/core"

# Every Python fact below comes from ONE interpreter, the one that imports the result:
# pybind11 from one Python and EXT_SUFFIX from another name the module for an interpreter
# that cannot load it. $PYTHON is set by tools/gate.py; the fallback is for a bare shell
# invocation.
PY="${PYTHON:-python3}"

PYBIND="${PYBIND11_INCLUDE:-$("$PY" -c 'import pybind11; print(pybind11.get_include())' 2>/dev/null || true)}"
[ -f "${PYBIND}/pybind11/pybind11.h" ] || { echo "pybind11 headers not found; pip install pybind11 or set PYBIND11_INCLUDE" >&2; exit 1; }

PYINC="${PYTHON_INCLUDE:-$("$PY" -c 'import sysconfig; print(sysconfig.get_path("include"))')}"
# Check for Python.h, not just the directory: sysconfig reports the BASE prefix's include
# path, which for a venv on a system Python without its -devel package does not exist.
# Fail here, with the fix.
if [ ! -f "${PYINC}/Python.h" ]; then
    cat >&2 <<EOF
Python development headers not found: ${PYINC}/Python.h is missing.

The interpreter is fine; the C API headers it was built against are not installed.
A virtualenv does NOT supply them — it inherits its base prefix.

  With root:     sudo dnf install python3-devel     # RHEL/CentOS/Fedora
                 sudo apt  install python3-dev      # Debian/Ubuntu
  Without root:  use a Python that ships its own headers, e.g.
                   uv python install 3.12   (or miniforge/conda)
                 or extract them from the matching RPM into your home dir and point
                 this script at them:
                   PYTHON_INCLUDE=\$HOME/pyhdr/usr/include/python3.12 bash native/build_local.sh

See the repository-root README.md ("No root / no sudo") for the full recipe.
EOF
    exit 1
fi
SUFFIX="$("$PY" -c 'import sysconfig; print(sysconfig.get_config_var("EXT_SUFFIX"))')"
# $SWI3S_NATIVE_OUT names another output path, and $SWI3S_NATIVE_FLAGS replaces the
# optimisation flags (tools/native_coverage.py builds an instrumented core this way).
OUT="${SWI3S_NATIVE_OUT:-$HERE/../swi3score${SUFFIX}}"
OPT_FLAGS="${SWI3S_NATIVE_FLAGS:--O3 -flto}"

# The compiler and the link flags are platform-specific: take clang++ or g++ (Debian/Ubuntu
# build-essential installs only g++), and pass `-undefined dynamic_lookup` on macOS only
# (GNU ld rejects it).
CXX="${CXX:-$(command -v clang++ || command -v g++ || true)}"
[ -n "$CXX" ] || { echo "no C++ compiler found; install clang++ or g++, or set CXX" >&2; exit 1; }

# Left unquoted at the call site so an empty value expands to nothing; a bash array would need
# the ${a[@]+"${a[@]}"} dance to survive `set -u` under macOS's bash 3.2.
LINK_FLAGS=""
case "$(uname -s)" in
    # Mach-O resolves the Python symbols at load time against the running interpreter, so the
    # extension links without libpython. ELF needs no equivalent: undefined symbols in a
    # shared object are permitted by default.
    Darwin) LINK_FLAGS="-undefined dynamic_lookup" ;;
esac

echo "pybind11 : $PYBIND"
echo "python   : $PYINC"
echo "compiler : $CXX"
echo "output   : $OUT"

# -O3 + LTO: decode is per-clock-edge (millions of UIs), and the hot loop spans
# several .cpp files, so cross-TU inlining matters. Portable (no -march=native).
"$CXX" $OPT_FLAGS -std=c++17 -shared -fPIC $LINK_FLAGS -fvisibility=hidden \
    -I"$PYBIND" -I"$PYINC" \
    -I"$HERE/swi3score/compat" -I"$HERE/swi3score" -I"$CORE_SRC" \
    "$HERE/swi3score/bindings.cpp" \
    "$HERE/swi3score/Decoder.cpp" \
    "$HERE/swi3score/Demo.cpp" \
    "$CORE_SRC/C8b10bDecoder.cpp" \
    "$CORE_SRC/CCommandTransportParser.cpp" \
    "$CORE_SRC/CRegisterModel.cpp" \
    "$CORE_SRC/CColumnDetector.cpp" \
    "$CORE_SRC/CDpConfig.cpp" \
    "$CORE_SRC/CDataPort.cpp" \
    "$CORE_SRC/CFlowControlPort.cpp" \
    "$CORE_SRC/CPayloadEngine.cpp" \
    -o "$OUT"

echo "built $(basename "$OUT")"
