"""docs/TESTING.md names every test suite, and no suite that is gone.

It is the document an audit starts from ("does every feature have a suite?"), and it drifted
the way prose does: by 3.0.19 it said 65 suites against 98 on disk, described 47, and still
told readers to run each file as a script, which by then executed nothing. A count in prose
cannot be kept true by hand; a check that each file is NAMED can, and adding a file forces
the one line that says what it is for.
"""
import os
import re

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DOC = os.path.join(_ROOT, "docs", "TESTING.md")


def _doc() -> str:
    with open(_DOC, encoding="utf-8") as f:
        return f.read()


def _suites() -> set:
    return {n[:-3] for n in os.listdir(os.path.join(_ROOT, "tests"))
            if n.startswith("test_") and n.endswith(".py")}


def test_every_suite_is_named_in_testing_md():
    doc = _doc()
    missing = sorted(n for n in _suites() if not re.search(rf"\b{re.escape(n)}\b", doc))
    assert not missing, (
        "tests with no mention in docs/TESTING.md — add a line under §3.2 saying what each "
        "pins:\n  " + "\n  ".join(missing))


def test_testing_md_names_no_suite_that_is_gone():
    doc = _doc()
    named = set(re.findall(r"\btest_[a-z0-9_]+(?=\.py\b|`)", doc))
    # Other things legitimately share the prefix: test FUNCTIONS (`test_real_sal_if_present`)
    # and the native C++ suite, which the doc names as `test_x.cpp` in §3.1 and bare in the
    # coverage matrix. Only a backticked name that is none of those is a stale suite.
    stale = sorted(n for n in named if n not in _suites()
                   and re.search(rf"`{re.escape(n)}(\.py)?`", doc)
                   and f"{n}.cpp" not in doc
                   and not _is_function_name(n))
    assert not stale, "docs/TESTING.md names suites that no longer exist: " + ", ".join(stale)


def _is_function_name(name: str) -> bool:
    """A backticked test FUNCTION (`test_real_sal_if_present`) is a legitimate reference."""
    for n in _suites():
        with open(os.path.join(_ROOT, "tests", n + ".py"), encoding="utf-8") as f:
            if re.search(rf"^def {re.escape(name)}\(", f.read(), re.M):
                return True
    return False
