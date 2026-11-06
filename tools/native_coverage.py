#!/usr/bin/env python3
"""Line and branch coverage of the native decode core, from an instrumented build.

    python3 tools/native_coverage.py            # build, run the suite, report
    python3 tools/native_coverage.py --out DIR  # where the report goes (build/native-coverage)

The report is OUT/report.txt; OUT/show/ holds each source file annotated with its line and
branch counts (llvm-cov show), where the lines and branches no test reaches are read.

Builds swi3score with clang's source-based coverage (`-fprofile-instr-generate
-fcoverage-mapping`, through native/build_local.sh) into a temporary directory placed first on
PYTHONPATH, runs the functional suite against it in parallel (each process writes its own
profile), merges the profiles with llvm-profdata and reports with llvm-cov, per source file
and in total. The report names the checkout's own native/ sources as llvm-cov's SOURCES
arguments, so the pybind11 and Python headers are left out whatever directory the checkout is
in; a report that lists none of them is a failure.

Exit 0 with a report, 3 when it cannot run here (no clang, no llvm-profdata / llvm-cov, or
Windows, where the core builds through MSVC), 1 when the build or the suite fails or the
report covers no native/ source, 2 for a bad command line. It measures
and reports; it enforces no floor. tools/gate.py runs it in the release gate as
"native-coverage".
"""
from __future__ import annotations

import glob
import os
import shutil
import subprocess
import sys
import tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _llvm(name: str) -> list[str] | None:
    """An llvm tool as a command: on PATH, or through xcrun on macOS."""
    exe = shutil.which(name)
    if exe:
        return [exe]
    if sys.platform == "darwin" and shutil.which("xcrun"):
        found = subprocess.run(["xcrun", "--find", name], capture_output=True, encoding="utf-8",
                               errors="replace")
        if found.returncode == 0 and found.stdout.strip():
            return [found.stdout.strip()]
    return None


_SOURCE_SUFFIXES = (".cpp", ".cc", ".h", ".hpp")


def _native_sources() -> list[str]:
    """This checkout's C++ sources under native/ (build directories excluded): the SOURCES
    arguments that keep llvm-cov to them."""
    found = []
    for base, dirs, names in os.walk(os.path.join(_ROOT, "native")):
        dirs[:] = sorted(d for d in dirs if d != "build")
        found += [os.path.join(base, n) for n in sorted(names) if n.endswith(_SOURCE_SUFFIXES)]
    return found


def _lists_native_source(report: str) -> bool:
    """Whether a llvm-cov report has a row for a source file. The report was restricted to
    this checkout's native/ sources, and llvm-cov prints their names relative to the
    directory they share."""
    return any(line.split(" ", 1)[0].endswith(_SOURCE_SUFFIXES)
               for line in report.splitlines())


def _shown(path: str) -> str:
    """`path` relative to the checkout, or absolute where it is on another drive (Windows)."""
    try:
        return os.path.relpath(path, _ROOT)
    except ValueError:
        return os.path.abspath(path)


def _measure(out: str, work: str, clang: str, profdata: list[str], cov: list[str]) -> int:
    """Build the instrumented core in `work`, run the suite against it and write the report
    into `out`; the exit status main returns."""
    suffix = subprocess.run([sys.executable, "-c",
                             "import sysconfig; print(sysconfig.get_config_var('EXT_SUFFIX'))"],
                            capture_output=True, encoding="utf-8",
                            errors="replace").stdout.strip()
    module = os.path.join(work, "swi3score" + suffix)
    env = dict(os.environ, PYTHON=sys.executable, CXX=clang, SWI3S_NATIVE_OUT=module,
               SWI3S_NATIVE_FLAGS="-O1 -fprofile-instr-generate -fcoverage-mapping")
    build = subprocess.run(["bash", os.path.join(_ROOT, "native", "build_local.sh")], env=env,
                           capture_output=True, encoding="utf-8", errors="replace")
    if build.returncode != 0:
        print(build.stdout[-2000:] + build.stderr[-2000:])
        print("the instrumented build FAILED")
        return 1
    env = dict(os.environ, PYTHONPATH=os.pathsep.join([work, _ROOT]),
               QT_QPA_PLATFORM="offscreen", PYTHONUTF8="1",
               LLVM_PROFILE_FILE=os.path.join(work, "prof-%p.profraw"))
    # -P: no working directory first on sys.path, where the regular core would shadow this one.
    suite = subprocess.run([sys.executable, "-P", "-m", "pytest", "-q", "--no-header", "-p",
                            "no:cacheprovider", "-m", "not perf", "-n", "auto",
                            "--dist", "loadfile"], cwd=_ROOT, env=env,
                           capture_output=True, encoding="utf-8", errors="replace")
    print((suite.stdout.strip().splitlines() or [""])[-1])
    if suite.returncode != 0:
        print(suite.stdout[-3000:] + suite.stderr[-2000:])
        print("the suite FAILED against the instrumented core")
        return 1
    raws = glob.glob(os.path.join(work, "prof-*.profraw"))
    merged = os.path.join(work, "merged.profdata")
    merge = subprocess.run(profdata + ["merge", "-sparse", *raws, "-o", merged],
                           capture_output=True, encoding="utf-8", errors="replace")
    if merge.returncode != 0:
        print(merge.stdout[-2000:] + merge.stderr[-2000:])
        print("llvm-profdata merge FAILED")
        return 1
    sources = _native_sources()
    report = subprocess.run(cov + ["report", module, f"-instr-profile={merged}", *sources],
                            capture_output=True, encoding="utf-8", errors="replace")
    os.makedirs(out, exist_ok=True)
    with open(os.path.join(out, "report.txt"), "w", encoding="utf-8") as f:
        f.write(report.stdout)
    shown = os.path.join(out, "show")
    shutil.rmtree(shown, ignore_errors=True)
    show = subprocess.run(cov + ["show", module, f"-instr-profile={merged}", "-format=text",
                                 "-show-branches=count", f"-output-dir={shown}", *sources],
                          capture_output=True, encoding="utf-8", errors="replace")
    print(report.stdout, end="")
    print(f"report: {_shown(os.path.join(out, 'report.txt'))} "
          f"({len(raws)} process profiles)")
    if report.returncode != 0:
        print(report.stderr[-2000:])
        print("llvm-cov report FAILED")
        return 1
    if show.returncode != 0:
        print(show.stderr[-2000:])
        print("llvm-cov show FAILED")
        return 1
    if not _lists_native_source(report.stdout):
        print("the coverage report lists no source under native/: nothing was measured")
        return 1
    return 0


def main(argv: list[str]) -> int:
    out = os.path.join(_ROOT, "build", "native-coverage")
    if argv and not (len(argv) == 2 and argv[0] == "--out" and argv[1]
                     and not argv[1].startswith("-")):
        print("usage: native_coverage.py [--out DIR]", file=sys.stderr)
        return 2
    if argv:
        out = os.path.abspath(argv[1])
    profdata, cov = _llvm("llvm-profdata"), _llvm("llvm-cov")
    clang = shutil.which("clang++")
    if os.name == "nt" or not (profdata and cov and clang):
        print("native coverage cannot run here: it needs clang++, llvm-profdata and llvm-cov "
              "(and builds with build_local.sh, which Windows does not use)")
        return 3
    work = tempfile.mkdtemp(prefix="swi3s-native-cov-")
    try:
        return _measure(out, work, clang, profdata, cov)
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
