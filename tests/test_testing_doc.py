"""docs/TESTING.md names every test suite in §3.2, and the docs name no suite that is gone.

Adding a test file forces the one line in the doc that says what it is for.
"""
import functools
import os
import re

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DOC = os.path.join(_ROOT, "docs", "TESTING.md")
_DOCS = (_DOC, os.path.join(_ROOT, "docs", "DEVELOPMENT.md"))


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as f:
        return f.read()


def _doc() -> str:
    return _read(_DOC)


def _suites() -> set:
    return {n[:-3] for n in os.listdir(os.path.join(_ROOT, "tests"))
            if n.startswith("test_") and n.endswith(".py")}


def _section(doc: str, heading: str) -> str:
    """The text of the `## ` or `### ` section whose heading starts with `heading`."""
    start = re.search(rf"^#+ {re.escape(heading)}\b.*$", doc, re.M)
    assert start, f"docs/TESTING.md has no section {heading}"
    level = len(start.group(0)) - len(start.group(0).lstrip("#"))
    end = re.search(rf"^#{{1,{level}}} ", doc[start.end():], re.M)
    return doc[start.end():start.end() + end.start()] if end else doc[start.end():]


def _entries(section: str) -> set:
    """The suites an entry line names in `section`: a bullet that opens with one or more
    `test_x.py` names, comma-separated (`- `test_a.py`, `test_b.py` — what they pin`)."""
    names = set()
    for lead in re.findall(r"^- ((?:`test_[a-z0-9_]+\.py`(?:, )?)+)", section, re.M):
        names |= set(re.findall(r"`(test_[a-z0-9_]+)\.py`", lead))
    return names


def _backticked_names(doc: str) -> set:
    """Every `test_x` a backtick span names, as `test_x`, `test_x.py`, `tests/test_x.py` or
    `test_x.py::function`."""
    return set(re.findall(r"`(?:tests/)?(test_[a-z0-9_]+)(?:\.py)?(?:::[^`]*)?`", doc))


def test_every_suite_has_an_entry_in_section_3_2():
    entries = _entries(_section(_doc(), "3.2"))
    missing = sorted(_suites() - entries)
    assert not missing, (
        "tests with no entry in docs/TESTING.md §3.2 (a bullet opening `test_x.py` — what it pins):"
        "\n  " + "\n  ".join(missing))


def test_testing_md_names_no_suite_that_is_gone():
    """Every doc that cites a suite names one that exists, by bare name, by tests/ path or
    with a function after `::`."""
    # Other things legitimately share the prefix: test FUNCTIONS (`test_real_sal_if_present`)
    # and the native C++ suite, which TESTING.md names as `test_x.cpp` in §3.1 and bare in the
    # coverage matrix. Only a backticked name that is none of those is a stale suite.
    cpp = _doc()
    stale = {}
    for path in _DOCS:
        doc = _read(path)
        for n in sorted(_backticked_names(doc)):
            if n not in _suites() and f"{n}.cpp" not in cpp and not _is_function_name(n):
                stale.setdefault(os.path.basename(path), []).append(n)
    assert not stale, f"the docs name suites that no longer exist: {stale}"


def test_an_entry_is_a_bullet_that_opens_with_the_suite_name():
    section = ("- `test_a.py`, `test_b.py` — two suites\n"
               "- `test_c.py` — one\n"
               "  continued, and `test_d.py` — mentioned mid-entry\n"
               "- prose that cites `test_e.py` later\n")
    assert _entries(section) == {"test_a", "test_b", "test_c"}


def test_the_stale_name_pattern_reads_every_way_the_docs_cite_a_suite():
    doc = ("`test_a` `test_b.py` `tests/test_c.py` `test_d.py::test_one` "
           "`tests/test_e.py::test_two[x]` and `test_f(...)` plus test_g.py in prose")
    assert _backticked_names(doc) == {"test_a", "test_b", "test_c", "test_d", "test_e"}


@functools.lru_cache(maxsize=None)
def _test_functions() -> frozenset:
    """Every function a test file defines, module-level or a class method."""
    sources = "\n".join(_read(os.path.join(_ROOT, "tests", n + ".py")) for n in sorted(_suites()))
    return frozenset(re.findall(r"^[ \t]*def (test_[a-z0-9_]+)\(", sources, re.M))


def _is_function_name(name: str) -> bool:
    """A backticked test FUNCTION (`test_real_sal_if_present`), module-level or a class
    method, is a legitimate reference."""
    return name in _test_functions()
