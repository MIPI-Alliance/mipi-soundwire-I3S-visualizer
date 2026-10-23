"""Meta-test: every test file must actually contribute tests.

A file that collects nothing does not fail in most runners. The per-suite runner is the
gate's per-suite check (tools/gate.py; run_all.sh wraps it), which runs each file through
pytest. A file that stops contributing tests — renamed helpers, an accidental module-level
`return`, a decorator that swallows collection, a bad `pytest.ini` marker — fails this test.
"""
import collections
import functools
import glob
import os
import subprocess
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)

# Files that legitimately collect nothing under the default (non-perf) marker, because
# every test in them carries @pytest.mark.perf. Keep this list SHORT and justified — it
# is the escape hatch this test exists to police.
_PERF_ONLY = {"test_perf.py"}

# This meta-test itself is collected, so it can never be empty; no exemption needed.


@functools.lru_cache(maxsize=None)
def _collected_per_file(marker: str):
    """{filename: number of tests} for the whole tests/ directory under `-m marker`.

    ONE `--collect-only` over the directory, parsed per file, not a subprocess per
    suite. Cached so the three checks below share a single collection pass.

    Uses a real pytest subprocess rather than importing the modules: a module can import
    cleanly and still yield zero tests.
    """
    out = subprocess.run(
        [sys.executable, "-m", "pytest", _HERE, "--collect-only", "-q",
         "-m", marker, "-p", "no:cacheprovider"],
        cwd=_ROOT, capture_output=True, text=True,
        env={**os.environ, "PYTHONPATH": _ROOT, "QT_QPA_PLATFORM": "offscreen"},
    )
    counts = collections.Counter()
    for line in out.stdout.splitlines():
        line = line.strip()
        # Node IDs look like "tests/test_x.py::test_name" (possibly with [params]).
        if "::" in line and line.endswith(")") is False:
            path = line.split("::", 1)[0]
            if path.endswith(".py"):
                counts[os.path.basename(path)] += 1
    return counts


def _count(name: str, marker: str) -> int:
    return _collected_per_file(marker).get(name, 0)


def test_every_test_file_collects_at_least_one_test():
    """No `tests/test_*.py` may collect zero tests under 'not perf' (or 'perf' for the
    perf-only suites). A silently-dead file is a coverage hole that reports as green."""
    empty = []
    for path in sorted(glob.glob(os.path.join(_HERE, "test_*.py"))):
        name = os.path.basename(path)
        marker = "perf" if name in _PERF_ONLY else "not perf"
        if _count(name, marker) == 0:
            empty.append(f"{name} (collected 0 under -m '{marker}')")
    assert not empty, (
        "test files that contribute no tests:\n  " + "\n  ".join(empty) +
        "\n\nEither the file has no test functions, or its tests are all marked/skipped "
        "at collection. Delete the file or fix collection — do not leave it in place "
        "reporting green."
    )


def test_perf_only_exemptions_are_still_perf_only():
    """Guard the exemption list itself: a file listed in _PERF_ONLY must genuinely have
    no non-perf tests, so the list can't quietly hide a broken suite."""
    for name in sorted(_PERF_ONLY):
        if not os.path.exists(os.path.join(_HERE, name)):
            continue                      # removed suite; nothing to police
        n = _count(name, "not perf")
        assert n == 0, (
            f"{name} is exempted as perf-only but collects {n} non-perf test(s). "
            f"Remove it from _PERF_ONLY so those tests are covered by the main check."
        )


def test_runner_sees_every_test_file():
    """The per-suite runner (tools/gate.py, via tests/run_all.sh) must walk the same
    files pytest collects.

    The runner globs `tests/test_*.py` and runs each through pytest; every file
    contributing tests must be one that glob visits.
    """
    collected = set(_collected_per_file("not perf"))
    walked = {os.path.basename(p) for p in glob.glob(os.path.join(_HERE, "test_*.py"))}
    missed = collected - walked
    assert not missed, (
        f"pytest collects tests from files the runner's glob never visits: "
        f"{sorted(missed)}. The per-suite pass would report green without running them."
    )
