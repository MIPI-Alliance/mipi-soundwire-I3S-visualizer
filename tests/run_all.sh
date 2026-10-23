#!/usr/bin/env bash
# Every Python suite in its own process, with a per-suite summary: a thin wrapper over the
# per-suite check in tools/gate.py.
#
#   ./run_all.sh                 # every suite, one process each (excludes perf, like CI)
#   ./run_all.sh --build         # rebuild the swi3score core (and assert its ABI) first
#   ./run_all.sh --perf          # the perf benchmarks instead of the functional suites
#   ./run_all.sh --native        # also run the native C++ suite (sibling checkout)
#   ./run_all.sh --verbose       # list every suite with its test count
#
# The options map onto
#   python3 tools/gate.py --only [native,]per-suite|perf[,native-cpp] [--verbose]
# Keep this a wrapper; put logic in tools/gate.py.
set -u
cd "$(dirname "$0")/.." || exit 2
checks=per-suite pre="" post="" verbose=""
for arg in "$@"; do
    case "$arg" in
        --build)   pre="native," ;;
        --perf)    checks=perf ;;
        --native)  post=",native-cpp" ;;
        --verbose|-v) verbose="--verbose" ;;
        --help|-h) sed -n '2,13p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "unknown option: $arg" >&2; exit 2 ;;
    esac
done
exec "${PYTHON:-python3}" tools/gate.py --only "$pre$checks$post" $verbose
