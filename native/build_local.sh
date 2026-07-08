#!/usr/bin/env bash
# Offline build of the swi3score pybind11 module (no PyPI / scikit-build-core).
#
# Compiles the C++ decode core (reused verbatim from the Saleae plugin) + the
# ISampleSource/Decoder/Demo + pybind11 bindings straight into an importable
# extension. The normal build path is `pip install ./native` (scikit-build-core);
# use this when PyPI is unreachable. Requires clang++, Python dev headers, and
# pybind11 headers (auto-discovered, incl. the copy bundled with torch).
#
# Usage:  ./build_local.sh   then   PYTHONPATH=.. python3 -c "import swi3score"
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
# Decode core is vendored in-repo (see CMakeLists.txt / swi3score/core/README.md).
PLUGIN_SRC="$HERE/swi3score/core"

PYBIND="${PYBIND11_INCLUDE:-$(python3 -c 'import pybind11; print(pybind11.get_include())' 2>/dev/null || true)}"
if [ -z "${PYBIND}" ] || [ ! -f "${PYBIND}/pybind11/pybind11.h" ]; then
    # Fall back to the pybind11 headers bundled inside an installed torch.
    PYBIND="$(python3 - <<'PY' 2>/dev/null || true
import os, importlib.util
s = importlib.util.find_spec("torch")
print(os.path.join(os.path.dirname(s.origin), "include") if s else "")
PY
)"
fi
[ -f "${PYBIND}/pybind11/pybind11.h" ] || { echo "pybind11 headers not found; set PYBIND11_INCLUDE" >&2; exit 1; }

PYINC="$(python3 -c 'import sysconfig; print(sysconfig.get_path("include"))')"
SUFFIX="$(python3 -c 'import sysconfig; print(sysconfig.get_config_var("EXT_SUFFIX"))')"
OUT="$HERE/../swi3score${SUFFIX}"

echo "pybind11 : $PYBIND"
echo "python   : $PYINC"
echo "output   : $OUT"

# -O3 + LTO: decode is per-clock-edge (millions of UIs), and the hot loop spans
# several .cpp files, so cross-TU inlining matters. Portable (no -march=native).
clang++ -O3 -flto -std=c++17 -shared -fPIC -undefined dynamic_lookup -fvisibility=hidden \
    -I"$PYBIND" -I"$PYINC" \
    -I"$HERE/swi3score/compat" -I"$HERE/swi3score" -I"$PLUGIN_SRC" \
    "$HERE/swi3score/bindings.cpp" \
    "$HERE/swi3score/Decoder.cpp" \
    "$HERE/swi3score/Demo.cpp" \
    "$PLUGIN_SRC/C8b10bDecoder.cpp" \
    "$PLUGIN_SRC/CCommandTransportParser.cpp" \
    "$PLUGIN_SRC/CRegisterModel.cpp" \
    "$PLUGIN_SRC/CColumnDetector.cpp" \
    "$PLUGIN_SRC/CDpConfig.cpp" \
    "$PLUGIN_SRC/CDataPort.cpp" \
    "$PLUGIN_SRC/CFlowControlPort.cpp" \
    "$PLUGIN_SRC/CPayloadEngine.cpp" \
    "$PLUGIN_SRC/SwI3sProtocolDefs.cpp" \
    -o "$OUT"

echo "built $(basename "$OUT")"
