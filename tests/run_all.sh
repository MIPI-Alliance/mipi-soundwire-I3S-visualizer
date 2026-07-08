#!/usr/bin/env bash
# Run the full SWI3S Studio test suite and report a pass/fail summary.
#
# Judges each suite by its EXIT CODE (not stdout text — several suites print
# "0 errors" etc., which fools naive greps; see docs/TESTING.md §6).
#
#   ./run_all.sh                 # run every Python suite
#   ./run_all.sh --build         # rebuild the swi3score core first
#   ./run_all.sh --native        # also run the native C++ suite
#   ./run_all.sh --build --native --verbose
#
# Exit status is non-zero if any suite fails. Paths resolve relative to this
# file, so it can be run from anywhere.
set -u

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
PY="${PYTHON:-python3}"

build=0 native=0 verbose=0
for arg in "$@"; do
    case "$arg" in
        --build)   build=1 ;;
        --native)  native=1 ;;
        --verbose|-v) verbose=1 ;;
        --help|-h)
            sed -n '2,14p' "$0" | sed 's/^# \{0,1\}//'
            exit 0 ;;
        *) echo "unknown option: $arg" >&2; exit 2 ;;
    esac
done

# Optionally rebuild the C++ core. A stale .so is the #1 source of confusing
# pass/fail results after a native/ edit (docs/TESTING.md §2).
if [ "$build" = 1 ]; then
    echo "== Rebuilding swi3score core =="
    PYBIND11_INCLUDE="${PYBIND11_INCLUDE:-$("$PY" -c 'import pybind11; print(pybind11.get_include())' 2>/dev/null)}" \
        bash "$ROOT/native/build_local.sh" || { echo "core build FAILED"; exit 1; }
    echo
fi

pass=0; fail=0; failed=""
echo "== Python suites =="
for t in "$ROOT"/tests/test_*.py; do
    name="$(basename "$t" .py)"
    if out="$(cd "$ROOT" && PYTHONPATH="$ROOT" QT_QPA_PLATFORM=offscreen "$PY" "$t" 2>&1)"; then
        printf '  ok   %s\n' "$name"
        pass=$((pass + 1))
        [ "$verbose" = 1 ] && echo "$out" | sed 's/^/        /'
    else
        printf '  FAIL %s\n' "$name"
        echo "$out" | tail -12 | sed 's/^/        /'
        fail=$((fail + 1)); failed="$failed $name"
    fi
done

# Optionally run the native C++ regression suite (separate runner).
if [ "$native" = 1 ]; then
    echo
    echo "== Native C++ suite =="
    nat="$ROOT/../protocol-analyzer/SwI3sAnalyzer/test/run_tests.sh"
    if [ -f "$nat" ]; then
        if bash "$nat"; then echo "  ok   native C++ suite"; pass=$((pass + 1))
        else echo "  FAIL native C++ suite"; fail=$((fail + 1)); failed="$failed native-cpp"; fi
    else
        echo "  (skipped: $nat not found)"
    fi
fi

echo
echo "=== $pass passed, $fail failed ==="
if [ "$fail" -ne 0 ]; then
    echo "failed:$failed"
    exit 1
fi
