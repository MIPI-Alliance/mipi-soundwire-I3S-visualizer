#!/usr/bin/env python3
"""The test dashboard: coverage by subsystem, gaps, redundancy and fragility, with the
latest durations (docs/TESTING.md §1.1, "What is measured"). Run on the development machine
before a tag.

    python3 tools/test_report.py              # run the suite with per-test coverage, report
    python3 tools/test_report.py --data FILE  # report from an existing coverage data file

Runs the functional suite in parallel with branch coverage and one coverage context per test,
then writes build/test-report/report.md:
  * coverage of lines and branches per subsystem, and the files with the most lines unrun;
  * redundancy: the tests that run no line any other test misses, and those whose every line
    one other test also runs. Line coverage cannot see test data: a sweep over spec values
    reads as covering nothing new, so read these beside the durations, not alone;
  * fragility: per subsystem, the lines exactly one test reaches;
  * durations: the slowest files and tests, from build/test-durations/<platform>.txt as the
    last gate run wrote it.
"""
from __future__ import annotations

import collections
import contextlib
import os
import sqlite3
import subprocess
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_OUT = os.path.join(_ROOT, "build", "test-report")


def _subsystem(rel: str) -> str:
    parts = rel.replace(os.sep, "/").split("/")
    if parts[0] == "tools":
        return "tools"
    if len(parts) > 3 and parts[1] == "ui" and parts[2] == "authoring":
        return "ui.authoring"
    return parts[1][:-3] if parts[1].endswith(".py") else parts[1]


def _run_suite(data: str) -> None:
    env = dict(os.environ, PYTHONPATH=".", QT_QPA_PLATFORM="offscreen", COVERAGE_FILE=data)
    rc = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-m",
                         "not perf", "-n", "auto", "--dist", "loadfile", "--cov",
                         "--cov-context=test", "--cov-report="], cwd=_ROOT, env=env).returncode
    if rc != 0:
        raise SystemExit(f"the suite failed (exit {rc}); no report")


def _data_problem(data: str) -> str | None:
    """Why `data` is not a coverage data file this report can read, or None: it must be one
    written with branch coverage and one context per test (what _run_suite writes)."""
    try:
        with contextlib.closing(sqlite3.connect(data)) as db:
            arcs = db.execute("select count(*) from arc").fetchone()[0]
            contexts = db.execute("select count(*) from context where context != ''"
                                  ).fetchone()[0]
    except sqlite3.Error:
        return "is not a coverage data file"
    if not arcs or not contexts:
        return "holds no per-test branch data (it needs branch coverage and --cov-context=test)"
    return None


def _per_test_lines(data: str) -> dict[str, set]:
    lines: dict[str, set] = collections.defaultdict(set)
    with contextlib.closing(sqlite3.connect(data)) as db:
        ctx = dict(db.execute("select id, context from context"))
        for fid, cid, a, b in db.execute("select file_id, context_id, fromno, tono from arc"):
            name = (ctx.get(cid) or "").split("|")[0]
            if name:
                lines[name].update((fid, n) for n in (a, b) if n > 0)
    return lines


def report(data: str) -> str:
    import coverage
    cov = coverage.Coverage(data_file=data)
    cov.load()
    if not callable(getattr(cov, "_analyze", None)):         # per-file numbers, coverage 7
        raise SystemExit(f"test_report: coverage {coverage.__version__} has no "
                         f"Coverage._analyze, which this report reads its numbers from")
    sub = collections.defaultdict(lambda: [0, 0, 0, 0])
    per_file = []
    for path in cov.get_data().measured_files():
        if not os.path.exists(path):
            continue
        rel = os.path.relpath(path, _ROOT)
        n = cov._analyze(path).numbers
        s = sub[_subsystem(rel)]
        s[0] += n.n_statements; s[1] += n.n_missing; s[2] += n.n_branches
        s[3] += n.n_missing_branches
        per_file.append((rel, n.n_statements, n.n_missing))
    out = ["# Test report", "", "## Coverage by subsystem", "",
           "| subsystem | statements | lines | branches |", "|---|---|---|---|"]
    for name, (st, mi, br, mb) in sorted(sub.items(), key=lambda x: -x[1][0]):
        out.append(f"| {name} | {st} | {100 * (st - mi) / max(1, st):.1f}% | "
                   f"{100 * (br - mb) / max(1, br):.1f}% |")
    out += ["", "## Most lines unrun", "", "| file | unrun | of |", "|---|---|---|"]
    out += [f"| {r} | {mi} | {st} |" for r, st, mi in sorted(per_file, key=lambda x: -x[2])[:20]]

    lines = _per_test_lines(data)
    reach = collections.Counter(ln for ls in lines.values() for ln in ls)
    holders = collections.defaultdict(list)
    for t, ls in lines.items():
        for ln in ls:
            holders[ln].append(t)
    no_unique = [t for t, ls in lines.items() if all(reach[ln] > 1 for ln in ls)]
    inside_one = 0
    for t in no_unique:
        ls = lines[t]
        if ls:
            rare = min(ls, key=lambda ln: reach[ln])
            inside_one += any(o != t and ls <= lines[o] for o in holders[rare])
    by_file = collections.Counter(t.split("::")[0] for t in no_unique)
    of_file = collections.Counter(t.split("::")[0] for t in lines)
    out += ["", "## Redundancy", "",
            f"{len(no_unique)} of {len(lines)} tests run no line another test misses; "
            f"{inside_one} run only lines one other test also runs. Files where most tests add "
            "no line (a sweep over values reads this way and is not redundant):", "",
            "| file | tests adding no line | tests |", "|---|---|---|"]
    out += [f"| {f} | {n} | {of_file[f]} |" for f, n in by_file.most_common(15)
            if n * 2 > of_file[f]]

    db = sqlite3.connect(data)
    try:
        files = {fid: os.path.relpath(p, _ROOT) for fid, p in
                 db.execute("select id, path from file")}
    finally:
        db.close()
    single, covered = collections.Counter(), collections.Counter()
    for (fid, _n), k in reach.items():
        s = _subsystem(files[fid])
        covered[s] += 1
        single[s] += k == 1
    out += ["", "## Fragility: lines exactly one test reaches", "",
            "| subsystem | lines | of covered |", "|---|---|---|"]
    out += [f"| {s} | {single[s]} | {covered[s]} ({100 * single[s] / covered[s]:.0f}%) |"
            for s in sorted(covered, key=lambda s: -single[s])]

    record = os.path.join(_ROOT, "build", "test-durations", f"{sys.platform}.txt")
    out += ["", "## Durations (the last gate run here)", ""]
    if os.path.isfile(record):
        rows = []
        with open(record, encoding="utf-8", errors="replace") as f:
            for ln in f:
                parts = ln.strip().split(maxsplit=2)
                try:
                    rows.append((parts[0], float(parts[1]), parts[2]))
                except (IndexError, ValueError):
                    continue                            # a line that does not parse
        out += ["| slowest files | s |", "|---|---|"]
        out += [f"| {p} | {s:.1f} |" for k, s, p in rows if k == "file"][:10]
        out += ["", "| slowest tests | s |", "|---|---|"]
        out += [f"| {p} | {s:.2f} |" for k, s, p in rows if k == "test"][:10]
    else:
        out.append("No durations recorded here: run the gate (tools/gate.py) first.")
    return "\n".join(out) + "\n"


def main(argv: list[str]) -> int:
    if argv and not (len(argv) == 2 and argv[0] == "--data" and argv[1]
                     and not argv[1].startswith("-")):
        print("usage: python3 tools/test_report.py [--data COVERAGE_FILE]", file=sys.stderr)
        return 2
    if argv and not os.path.isfile(argv[1]):
        print(f"test_report: --data {argv[1]}: no such file", file=sys.stderr)
        return 2
    problem = _data_problem(argv[1]) if argv else None
    if problem:
        print(f"test_report: --data {argv[1]} {problem}; run without --data to make one",
              file=sys.stderr)
        return 2
    os.makedirs(_OUT, exist_ok=True)
    data = argv[1] if argv else os.path.join(_OUT, ".coverage")
    if not argv:
        if os.path.exists(data):
            os.remove(data)
        _run_suite(data)
    text = report(data)
    with open(os.path.join(_OUT, "report.md"), "w", encoding="utf-8") as f:
        f.write(text)
    print(text)
    print(f"report: {os.path.relpath(os.path.join(_OUT, 'report.md'), _ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
