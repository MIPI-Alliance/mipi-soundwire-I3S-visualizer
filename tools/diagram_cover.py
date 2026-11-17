#!/usr/bin/env python3
"""Whether the platform scope reaches every branch of the timing diagrams the full sweep does.

    python3 tools/diagram_cover.py

Runs the term diagram's and the whole-inequality view's test files (`FILES`) under branch
coverage of swi3s_studio/ui/timing_diagram.py and swi3s_studio/ui/inequality_diagram.py alone
(`TARGETS`), spread over workers ($SWI3S_TEST_WORKERS, else one per CPU), twice: the platform
scope (`-m`, tools/gate.py's _GUEST_SCOPE), then, with `-m "not perf"`, the tests the
platform scope skipped as left to the full suite (a skip reason naming the platform scope).
The platform scope adds the cases of `test_timing_diagram_layout.py`'s `_COVER` to its
sweep, and sweeps `test_inequality_diagram.py`'s `_PLATFORM` cases, so that the second run
reaches no arc of either file the first does not.

Exit 0 when it reaches none; 1 listing each such arc (its lines and function) and the tests
that reach it, or when a run fails or the platform scope skips no test; 2 for a bad
argument. tools/gate.py runs it as "diagram-cover".
"""
from __future__ import annotations

import ast
import importlib.util
import os
import re
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TARGETS = ("swi3s_studio/ui/timing_diagram.py", "swi3s_studio/ui/inequality_diagram.py")
FILES = ("tests/test_timing_diagram_layout.py", "tests/test_timing_diagram.py",
         "tests/test_inequality_diagram.py")
_LEFT_OUT = "platform scope"        # in the reason of a skip the full suite runs instead


def _platform_scope() -> str:
    """The platform scope's -m expression: tools/gate.py's _GUEST_SCOPE."""
    spec = importlib.util.spec_from_file_location("gate", os.path.join(_ROOT, "tools", "gate.py"))
    assert spec is not None and spec.loader is not None
    gate = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gate)
    return str(gate._GUEST_SCOPE)


def _workers() -> int:
    env = os.environ.get("SWI3S_TEST_WORKERS", "").strip()
    return max(1, int(env)) if env.isascii() and env.isdigit() else (os.cpu_count() or 1)


def _run(tmp: str, label: str, marker: str, targets: list[str]) -> tuple[int, str, str]:
    """pytest over `targets` with `-m marker` under coverage of TARGETS, one context per
    test: (its exit status, the coverage data file, the JUnit XML report)."""
    data, rc, xml = (os.path.join(tmp, f"{label}.{ext}") for ext in ("coverage", "rc", "xml"))
    include = ", ".join(f"*/{t}" for t in TARGETS)
    with open(rc, "w", encoding="utf-8") as f:
        f.write(f"[run]\nbranch = True\ninclude = {include}\ndata_file = {data}\n"
                "disable_warnings = no-data-collected\n")
    cmd = [sys.executable, "-m", "pytest", "-q", "--no-header", "-p", "no:cacheprovider",
           "--basetemp", os.path.join(tmp, f"{label}-tmp"), "-n", str(_workers()),
           "--dist", "load", "--cov", f"--cov-config={rc}", "--cov-report=",
           "--cov-context=test", f"--junitxml={xml}", "-m", marker, *targets]
    env = dict(os.environ, QT_QPA_PLATFORM=os.environ.get("QT_QPA_PLATFORM", "offscreen"))
    status = subprocess.run(cmd, cwd=_ROOT, env=env).returncode
    return status, data, xml


def _left_out(xml: str) -> list[str]:
    """The node ids the JUnit report `xml` shows skipped with a reason naming the platform
    scope."""
    ids = []
    for case in ET.parse(xml).getroot().iter("testcase"):
        skip = case.find("skipped")
        if skip is not None and _LEFT_OUT in (skip.get("message") or ""):
            path = case.get("classname", "").replace(".", "/") + ".py"
            ids.append(f"{path}::{case.get('name')}")
    return ids


def _arcs(data_file: str) -> dict[tuple[str, int, int], set[str]]:
    """Each arc of each of TARGETS the data holds, as (the target, the arc's lines), with the
    contexts (tests) that reach it."""
    import coverage
    data = coverage.CoverageData(data_file)
    data.read()
    out: dict[tuple[str, int, int], set[str]] = {}
    for name in TARGETS:
        target = next((f for f in data.measured_files()
                       if f.replace(os.sep, "/").endswith(name)), None)
        if target is None:
            continue
        for ctx in sorted(data.measured_contexts()):
            data.set_query_contexts([f"^{re.escape(ctx)}$"])
            for a, b in data.arcs(target) or ():
                out.setdefault((name, a, b), set()).add(ctx)
    return out


def _function_of(target: str, line: int) -> str:
    """The name of the innermost function of `target` holding `line`, or "module"."""
    with open(os.path.join(_ROOT, target), encoding="utf-8") as f:
        tree = ast.parse(f.read())
    best, size = "module", None
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            end = node.end_lineno or node.lineno
            if node.lineno <= line <= end and (size is None or end - node.lineno < size):
                best, size = node.name, end - node.lineno
    return best


def main(argv: list[str]) -> int:
    if argv:
        print("usage: python3 tools/diagram_cover.py  (takes no options)", file=sys.stderr)
        return 2
    with tempfile.TemporaryDirectory(prefix="swi3s-diagram-cover-") as tmp:
        status, data, xml = _run(tmp, "platform", _platform_scope(), list(FILES))
        if status != 0:
            print(f"diagram_cover: the platform scope run failed (exit {status})")
            return 1
        left = _left_out(xml)
        if not left:
            print("diagram_cover: the platform scope left no test to the full suite")
            return 1
        reached = _arcs(data)
        status, rest_data, _ = _run(tmp, "rest", "not perf", left)
        if status != 0:
            print(f"diagram_cover: the run of the {len(left)} tests left to the full suite "
                  f"failed (exit {status})")
            return 1
        rest = _arcs(rest_data)
    missing = sorted(set(rest) - set(reached))
    print(f"diagram_cover: the platform scope reaches {len(reached)} arcs of "
          f"{' and '.join(TARGETS)}; the {len(left)} tests it leaves to the full suite reach "
          f"{len(rest)}, {len(missing)} of them not reached in the platform scope")
    for name, a, b in missing:
        tests = sorted({c.split("|")[0] for c in rest[(name, a, b)]})
        print(f"  {name} {a} -> {b} in {_function_of(name, abs(a))}: {', '.join(tests[:3])}"
              f"{' and others' if len(tests) > 3 else ''}")
    if missing:
        print("  add a case reaching each to _COVER in tests/test_timing_diagram_layout.py, or "
              "for inequality_diagram.py to _PLATFORM in tests/test_inequality_diagram.py")
    return 1 if missing else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
