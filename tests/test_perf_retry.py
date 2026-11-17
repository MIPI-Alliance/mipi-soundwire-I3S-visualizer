"""A perf test over its ceiling is measured once more and fails only if that run is over too
(tests/perf_retry.py). Run in a synthetic project that loads the same hook, so no wall clock
is involved."""
import os
import subprocess
import sys
import textwrap

_TESTS = os.path.dirname(os.path.abspath(__file__))

_CONFTEST = """
from perf_retry import pytest_runtest_call, pytest_terminal_summary  # noqa: F401
"""
_INI = """[pytest]
markers =
    perf: a perf test
"""


def _run(tmp_path, body: str) -> tuple[int, str]:
    (tmp_path / "conftest.py").write_text(_CONFTEST)
    (tmp_path / "pytest.ini").write_text(_INI)
    (tmp_path / "test_synthetic.py").write_text(textwrap.dedent(body))
    env = dict(os.environ, PYTHONPATH=_TESTS, NO_COLOR="1")
    p = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
                        str(tmp_path)], cwd=tmp_path, env=env, capture_output=True,
                       encoding="utf-8", errors="replace")
    return p.returncode, p.stdout + p.stderr


def test_a_perf_test_over_its_ceiling_passes_when_the_second_measurement_is_under(tmp_path):
    rc, out = _run(tmp_path, """
        import pytest
        pytestmark = pytest.mark.perf
        CALLS = []
        def test_slow_once():
            CALLS.append(1)
            assert len(CALLS) > 1, "took 9.00s (ceiling 1s)"
    """)
    assert rc == 0, out
    assert "perf re-measured" in out and "test_slow_once: passed; first run: took 9.00s" in out


def test_a_perf_test_over_its_ceiling_twice_fails_and_is_called_exactly_twice(tmp_path):
    rc, out = _run(tmp_path, """
        import pytest
        pytestmark = pytest.mark.perf
        def test_always_slow():
            open("calls", "a").write("x")
            assert False, "took 9.00s (ceiling 1s)"
    """)
    assert rc == 1, out
    assert (tmp_path / "calls").read_text() == "xx"
    assert "test_always_slow: failed again" in out


def test_only_a_perf_test_that_fails_an_assertion_is_called_again(tmp_path):
    rc, out = _run(tmp_path, """
        import pytest
        def test_not_perf():
            open("plain", "a").write("x")
            assert False
        @pytest.mark.perf
        def test_error():
            open("err", "a").write("x")
            raise RuntimeError("not a ceiling")
        @pytest.mark.perf
        def test_passing():
            open("ok", "a").write("x")
    """)
    assert rc == 1, out
    assert (tmp_path / "plain").read_text() == "x"
    assert (tmp_path / "err").read_text() == "x"
    assert (tmp_path / "ok").read_text() == "x"
    assert "perf re-measured" not in out
