#!/usr/bin/env bash
# Run the full SWI3S Studio test suite and report a per-suite pass/fail summary.
#
#   ./run_all.sh                 # every Python suite (excludes perf, like CI)
#   ./run_all.sh --build         # rebuild the swi3score core first
#   ./run_all.sh --perf          # perf benchmarks instead of the functional suites
#   ./run_all.sh --native        # also run the native C++ suite
#   ./run_all.sh --build --native --verbose
#
# Each suite runs in its OWN pytest process. Two reasons:
#   1. Per-suite pass/fail, which a single pytest run doesn't give you.
#   2. Process isolation catches teardown crashes a pooled run can hide — Qt calls
#      abort() when a QThread is destroyed while running, and whether that fires
#      depends on which suites shared the process (it hid a real shutdown bug until
#      3.0.10; see docs/TESTING.md §6).
#
# It delegates to pytest rather than executing each file as a script. The old runner
# invoked `python tests/test_x.py`, so it only ran what that file's __main__ block
# happened to call: 60 tests were invisible, and six files with no __main__ block ran
# NOTHING, exited 0, and were reported "ok". Coverage here is identical to CI by
# construction.
#
# Exit status is non-zero if any suite fails. Paths resolve relative to this file,
# so it can be run from anywhere.
set -u

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
PY="${PYTHON:-python3}"

build=0 native=0 verbose=0 perf=0
for arg in "$@"; do
    case "$arg" in
        --build)   build=1 ;;
        --native)  native=1 ;;
        --perf)    perf=1 ;;
        --verbose|-v) verbose=1 ;;
        --help|-h)
            sed -n '2,26p' "$0" | sed 's/^# \{0,1\}//'
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

# Match the CI split: the functional job runs -m "not perf", the perf gate runs -m perf.
if [ "$perf" = 1 ]; then
    marker='perf'
    echo "== Python suites (perf benchmarks) =="
else
    marker='not perf'
    echo "== Python suites =="
fi

pass=0; fail=0; empty=0; failed=""; noruns=""
for t in "$ROOT"/tests/test_*.py; do
    name="$(basename "$t" .py)"
    out="$(cd "$ROOT" && PYTHONPATH="$ROOT" QT_QPA_PLATFORM=offscreen \
           "$PY" -m pytest "$t" -q -m "$marker" -p no:cacheprovider 2>&1)"
    code=$?
    # pytest exit 5 == "no tests collected". Expected for a suite that is entirely
    # perf-marked (or entirely not) given the active marker, so it isn't a failure —
    # but a suite that collects nothing under EITHER marker is a silently dead file,
    # which is what item 4 (tests/test_collection.py) fails the build on.
    if [ "$code" = 5 ]; then
        printf '  --   %s (no tests for -m "%s")\n' "$name" "$marker"
        empty=$((empty + 1)); noruns="$noruns $name"
        continue
    fi
    if [ "$code" = 0 ]; then
        # Surface the count so "ok" can never again mean "ran nothing".
        n="$(printf '%s' "$out" | grep -oE '[0-9]+ passed' | head -1)"
        printf '  ok   %-42s %s\n' "$name" "${n:-}"
        pass=$((pass + 1))
        [ "$verbose" = 1 ] && printf '%s\n' "$out" | sed 's/^/        /'
    else
        printf '  FAIL %s (exit %d)\n' "$name" "$code"
        printf '%s\n' "$out" | tail -12 | sed 's/^/        /'
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
if [ "$empty" -ne 0 ]; then
    echo "=== $pass passed, $fail failed, $empty with no tests for this marker ==="
    [ "$verbose" = 1 ] && echo "no tests:$noruns"
else
    echo "=== $pass passed, $fail failed ==="
fi
if [ "$fail" -ne 0 ]; then
    echo "failed:$failed"
    exit 1
fi
