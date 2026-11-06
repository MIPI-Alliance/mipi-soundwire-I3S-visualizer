#!/usr/bin/env python3
"""The dev loop: the test files a change touches, run in parallel (docs/TESTING.md §1.1).

    python3 tools/changed_tests.py              # list the files for the working tree's changes
    python3 tools/changed_tests.py --run        # and run them, one file per worker
    python3 tools/changed_tests.py --base main  # changes since a ref, not since HEAD

A changed test file is run, and so is every test file that imports it, directly or through
another test file (a shared helper under tests/ counts the same way). A changed module runs
every test file that imports it, or the package it is in; a change under native/ runs every
test file that imports swi3score or the session; a changed file under tools/ (a script, its
data or a launcher) or a tests/ shell script runs every test file that names it, and a tools/
file that is not Python also runs the test files naming each tools/ Python script that names
it. Any other changed file under tests/ (a fixture, a golden file, a helper in a
subdirectory) or data/ runs every test file that names its file name or its path below
tests/, and a data/ file also runs the test files importing each swi3s_studio module that
names it. Any other changed file outside docs/ (visualizer_examples/, package data under
swi3s_studio/, .github/, a top-level file) runs every test file that names its file name or
its path. A change to tests/conftest.py, pyproject.toml or requirements*.txt runs
everything. What is not mapped (docs/, and a file no test names) runs nothing and is listed
as "not mapped"; the commit gate runs the whole suite.
"""
from __future__ import annotations

import ast
import glob
import os
import re
import subprocess
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_EVERYTHING = ("tests/conftest.py", "pyproject.toml", "requirements.txt", "requirements-dev.txt")


def changed_files(base: str) -> list[str]:
    """Paths changed against `base`, tracked or new and not ignored, relative to the root.
    Raises RuntimeError, carrying git's message, when a git command fails (an unknown `base`)."""
    out = set()
    for cmd in (["git", "diff", "--name-only", "-z", base],
                ["git", "diff", "--name-only", "--cached", "-z", base],
                ["git", "ls-files", "--others", "--exclude-standard", "-z"]):
        got = subprocess.run(cmd, cwd=_ROOT, capture_output=True, text=True)
        if got.returncode != 0:
            raise RuntimeError(f"{' '.join(cmd)} failed: {got.stderr.strip()}")
        out.update(name for name in got.stdout.split("\0") if name)
    return sorted(out)


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as f:
        return f.read()


def _module(path: str) -> str | None:
    if path.startswith("swi3s_studio/") and path.endswith(".py"):
        mod = path[:-3].replace("/", ".")
        return mod[: -len(".__init__")] if mod.endswith(".__init__") else mod
    return None


def imported_modules(source: str) -> set[str]:
    """The swi3s_studio modules a test file imports: each `import a.b` and `from a.b import c`
    names a.b, and the latter also a.b.c, since `c` may be a module (`from swi3s_studio import
    cli` imports swi3s_studio.cli)."""
    found = set(re.findall(r"(?:from|import)\s+(swi3s_studio(?:\.\w+)*)", source))
    # An import inside a string (a subprocess script) is not in the syntax tree.
    found.update(f"{m}.{n}" for m, n in re.findall(
        r"from\s+(swi3s_studio(?:\.\w+)*)\s+import\s+(\w+)", source))
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return found
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module and \
                (node.module == "swi3s_studio" or node.module.startswith("swi3s_studio.")):
            found.update(f"{node.module}.{a.name}" for a in node.names if a.name != "*")
    return found


def importing_tests(modules: set[str], text: dict[str, str]) -> set[str]:
    """The test files in `text` ({path: source}) that import any module in `modules` (names
    of tests/*.py files without the extension, `import x`, `from x import y`,
    `from tests import x`), and so on through the files found: a file that imports a file
    that imports one of `modules` is included. An import inside a function or a string
    counts. A module is not its own importer unless a file imports it by name."""
    found: set[str] = set()
    todo = set(modules)
    while todo:
        name = todo.pop()
        pat = re.compile(rf"(?:from|import)\s+(?:tests\.)?{re.escape(name)}\b|"
                         rf"from\s+tests\s+import\s+[\w\s,]*\b{re.escape(name)}\b")
        for t, s in text.items():
            if t not in found and pat.search(s):
                found.add(t)
                todo.add(os.path.basename(t)[:-3])
    return found


def tests_for(paths: list[str]) -> list[str]:
    """The test files `paths` touch (see the module docstring), sorted."""
    return sorted(set().union(*tests_by_path(paths).values()))


def tests_by_path(paths: list[str]) -> dict[str, set[str]]:
    """{path: the test files it touches} for each of `paths`; the test files are read and
    parsed once for all of them."""
    tests = sorted(os.path.relpath(f, _ROOT).replace(os.sep, "/")
                   for f in glob.glob(os.path.join(_ROOT, "tests", "test_*.py")))
    text = {t: _read(os.path.join(_ROOT, t)) for t in tests}
    imports = {t: imported_modules(s) for t, s in text.items()}
    result: dict[str, set[str]] = {}
    for p in paths:
        picked: set[str] = set()
        result[p] = picked
        if p in _EVERYTHING:
            picked.update(tests)
            continue
        top_level_test = (p.startswith("tests/") and p.endswith(".py")
                          and "/" not in p[len("tests/"):])
        if p in text:
            picked.add(p)
        if top_level_test:
            picked.update(importing_tests({os.path.basename(p)[:-3]}, text))
        mod = _module(p)
        if mod:
            picked.update(_importers(mod, imports))
        elif p.startswith("native/"):
            picked.update(t for t, s in text.items()
                          if "swi3score" in s or "swi3s_studio.session" in s)
        elif p.startswith("tools/") or (p.startswith("tests/") and p.endswith((".sh", ".ps1"))):
            name = os.path.basename(p)
            names = {name}
            if p.startswith("tools/") and not p.endswith(".py"):
                names.update(os.path.basename(s) for s in _tool_scripts() if name in _read(s))
            picked.update(t for t, s in text.items() if any(n in s for n in names))
        elif (p.startswith(("tests/", "data/")) and not top_level_test) or \
                (not p.startswith("docs/") and not top_level_test):
            names = {os.path.basename(p), p}
            if p.startswith("tests/"):
                names.add(p[len("tests/"):])
            picked.update(t for t, s in text.items() if any(n in s for n in names))
            if p.startswith("data/"):
                for m in _package_modules():
                    if os.path.basename(p) in _read(m):
                        rel = os.path.relpath(m, _ROOT).replace(os.sep, "/")
                        picked.update(_importers(_module(rel) or "", imports))
    return result


def _importers(mod: str, imports: dict[str, set[str]]) -> set[str]:
    """The test files whose imports (`imports`, {test file: modules}) reach module `mod`:
    the module itself, a module inside it, or the package holding it."""
    if not mod:
        return set()
    return {t for t, names in imports.items()
            if any(n == mod or n.startswith(mod + ".") or mod.startswith(n + ".") and
                   n != "swi3s_studio" for n in names)}


def _package_modules() -> list[str]:
    """The Python modules of the swi3s_studio package, as absolute paths."""
    return sorted(glob.glob(os.path.join(_ROOT, "swi3s_studio", "**", "*.py"), recursive=True))


def _tool_scripts() -> list[str]:
    """The Python scripts under tools/, as absolute paths."""
    return sorted(glob.glob(os.path.join(_ROOT, "tools", "**", "*.py"), recursive=True))


def _parse(argv: list[str]) -> tuple[str, bool] | None:
    """(base ref, run) from the command line, or None for a bad one."""
    base, run, args = "HEAD", False, list(argv)
    while args:
        arg = args.pop(0)
        if arg == "--run":
            run = True
        elif arg == "--base" and args and not args[0].startswith("-"):
            base = args.pop(0)
        else:
            return None
    return base, run


def main(argv: list[str]) -> int:
    parsed = _parse(argv)
    if parsed is None:
        print("usage: changed_tests.py [--base REF] [--run]", file=sys.stderr)
        return 2
    base, run = parsed
    try:
        paths = changed_files(base)
    except RuntimeError as e:
        print(f"changed_tests: {e}", file=sys.stderr)
        return 2
    by_path = tests_by_path(paths)
    picked = sorted(set().union(*by_path.values()))
    print(f"{len(paths)} changed path(s) since {base}; {len(picked)} test file(s):")
    for t in picked:
        print(f"  {t}")
    unmapped = [p for p in paths if not by_path[p]]
    if unmapped:
        print(f"not mapped (no test file selected): {', '.join(unmapped)}")
    if not run or not picked:
        return 0
    env = dict(os.environ, PYTHONPATH=".", QT_QPA_PLATFORM="offscreen")
    return subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
                           "-m", "not perf", "-n", "auto", "--dist", "loadfile", *picked],
                          cwd=_ROOT, env=env).returncode


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
