"""A perf test over its ceiling is measured once more, and fails only if that run fails too.

A wall-clock ceiling can be crossed by a pause that belongs to the machine, not the code: a
scheduler stall, a neighbouring job. The rule, for every test marked `perf`, locally and on
CI: a test whose call fails an assertion is called once more, and the test fails only if the
second call fails too. An error that is not an assertion (an exception, a skip) is not
retried. The retried tests are listed at the end of the run (`perf re-measured`), whether
their second call passed or not, so a ceiling that is crossed regularly shows.

`tests/conftest.py` imports `pytest_runtest_call` and `pytest_terminal_summary` from here;
`tests/test_perf_retry.py` runs them in a synthetic project.
"""
import gc

import pytest

RETRIED: list = []      # (test id, the first call's failure, "passed" or "failed again")


@pytest.hookimpl(wrapper=True)
def pytest_runtest_call(item):
    if item.get_closest_marker("perf") is None:
        return (yield)
    try:
        return (yield)
    except AssertionError as first:
        entry = [item.nodeid, str(first).splitlines()[0] if str(first) else "AssertionError", ""]
        RETRIED.append(entry)
        gc.collect()
        try:
            item.runtest()
        except AssertionError:
            entry[2] = "failed again"
            raise
        entry[2] = "passed"


def pytest_terminal_summary(terminalreporter, exitstatus, config):  # noqa: ARG001
    if not RETRIED:
        return
    tr = terminalreporter
    tr.section("perf re-measured")
    for nodeid, first, outcome in RETRIED:
        tr.write_line(f"{nodeid}: {outcome}; first run: {first}")
