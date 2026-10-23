#!/usr/bin/env python3
"""THE release gate — one implementation, every platform.

Run it on the development machine and on both test guests (Windows and Linux) at the
release commit. Exit 0 is the gate.

    python3 tools/gate.py            # full gate
    python3 tools/gate.py --quick    # skip perf + the per-suite pass (dev loop)
    python3 tools/gate.py --only per-suite --verbose     # one or more named checks
    python3 tools/gate.py --only native,native-cpp       # (comma-separated; see _CHECKS)

`tests/gate.sh` and `tests/gate.ps1` are thin wrappers around this file so either shell
has a natural entry point without a second copy of the logic.

Python, so it runs on Windows too, where `bash` may not be on PATH,
`native/build_local.sh` is a Unix build path (Windows builds the core through pip/MSVC),
`python3` is not a reliable name, and venv-installed linters may not be on PATH. All four
are handled here.

Checks, in order (a failure never stops the rest — you get the whole picture):
  1. native core rebuilt, the imported module ASSERTED to be the one just built, and its
     score_abi ASSERTED against session.py's _REQUIRED_SCORE_ABI
  2. pytest -m "not perf"
  3. pytest -m perf                       (skipped by --quick)
  4. each test file in its OWN process     (skipped by --quick) — catches teardown crashes
     a pooled run can mask
  5. ruff
  6. mypy, non-regression vs tools/mypy_baseline.json
  7. leak scan
  8. screenshots: every mode in both themes, checked for theme, contrast and clipped-text
     faults (tools/screenshots.py); the contact sheet is the gate's visual review
  (native-cpp, the C++ suite of the Saleae plugin sources, runs only when named with --only
   and needs that checkout beside this one; without it the check is NOT RUN)

`--only` runs a subset and says so: its summary is never a bare PASS, because a partial
run is not the gate. `tests/run_all.sh` is a wrapper over `--only`.

A MISSING TOOL IS A FAILURE, never a silent skip.
"""
from __future__ import annotations

import glob
import os
import pathlib
import re
import shutil
import site as _site
import subprocess
import sys
import sysconfig

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_WINDOWS = os.name == "nt"
_FAILED: list[str] = []
_NOT_RUN: list[str] = []      # a check that CANNOT apply here — listed loudly, never hidden


def _env() -> dict[str, str]:
    e = dict(os.environ)
    e.update(PYTHONPATH=".", QT_QPA_PLATFORM="offscreen",
             PYQTGRAPH_QT_LIB="PySide6", PYTHONUTF8="1")
    # $PYTHON NAMES THE INTERPRETER THIS GATE IS RUNNING, so a child script compiles for the
    # right one. native/build_local.sh takes EXT_SUFFIX, the include path and pybind11's
    # headers from a Python it invokes itself; left to find `python3` on PATH it picks the
    # SYSTEM interpreter, not the venv's.
    e.update(PYTHON=sys.executable)
    return e


def _run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=_ROOT, env=_env(), **kw)


def _step(title: str) -> None:
    print(f"\n=== {title} ===", flush=True)


def _tool(name: str) -> list[str] | None:
    """A tool as a runnable command: prefer the executable, fall back to `-m` so a
    venv-installed linter works without PATH surgery (the Windows case)."""
    exe = shutil.which(name)
    if exe:
        return [exe]
    probe = _run([sys.executable, "-m", name, "--version"],
                 capture_output=True, text=True)
    if probe.returncode == 0:
        return [sys.executable, "-m", name]
    return None


def _core_location_problem(path: str) -> str | None:
    """Why the swi3score at `path` is not the module check_native just built, or None.

    POSIX builds it beside the checkout as swi3score<EXT_SUFFIX>. Windows pip-installs it
    into this interpreter's site-packages, where a swi3score*.pyd in the checkout root (first
    on PYTHONPATH) would shadow it."""
    real = os.path.realpath(path)
    if _WINDOWS:
        site = {os.path.realpath(sysconfig.get_path(k)) for k in ("platlib", "purelib")}
        site.add(os.path.realpath(_site.getusersitepackages()))
        if not any(real.startswith(d + os.sep) for d in site):
            return f"imported {path}, not the build pip installed into {', '.join(sorted(site))}"
        return None
    want = os.path.join(_ROOT, "swi3score" + (sysconfig.get_config_var("EXT_SUFFIX") or ".so"))
    if real != os.path.realpath(want):
        return f"imported {path}, not the build at {want}"
    return None


def check_native() -> None:
    _step("native core: rebuild + ABI assert")
    if _WINDOWS:
        # build_local.sh is a Unix compile path; on Windows the core builds via MSVC through
        # pip. --force-reinstall because native/pyproject.toml pins version 0.1.0, so a plain
        # install no-ops and silently keeps the OLD .pyd.
        build = [sys.executable, "-m", "pip", "install", "--force-reinstall", "--no-deps",
                 os.path.join(_ROOT, "native")]
    else:
        build = ["bash", os.path.join("native", "build_local.sh")]
    if _run(build, capture_output=True, text=True).returncode != 0:
        print("  build FAILED — re-run it directly to see the compiler output")
        _FAILED.append("native build")
        # SAY WHAT THIS FAILURE TOOK WITH IT. Returning here skips the ABI assert, and the
        # suite that follows then runs against whatever core is already installed. Do not
        # abort (one red must not hide the rest); list the skipped assert as NOT RUN.
        _NOT_RUN.append("score_abi assert (the build failed; any tests below ran against "
                        "whatever core was already installed)")
        return

    src = pathlib.Path(_ROOT, "swi3s_studio", "session.py").read_text(encoding="utf-8")
    want = int(re.search(r"_REQUIRED_SCORE_ABI\s*=\s*(\d+)", src).group(1))
    # One value per line: the module's path may contain spaces.
    probe = _run([sys.executable, "-c",
                  "import swi3score, swi3s_studio as s;"
                  "print(swi3score.score_abi, s.__version__, swi3score.__file__, sep='\\n')"],
                 capture_output=True, text=True)
    if probe.returncode != 0:
        print(f"  import FAILED:\n{probe.stderr.strip()}")
        _FAILED.append("native import")
        return
    abi, ver, core = probe.stdout.strip().splitlines()[-3:]
    print(f"  score_abi={abi}  __version__={ver}  (session.py requires {want})")
    print(f"  imported {core}")
    problem = _core_location_problem(core)
    if problem:
        print(f"  WRONG MODULE — {problem}")
        _FAILED.append("native core imported is not the one just built")
    if int(abi) != want:
        print("  MISMATCH — the built core does not match what the app requires")
        _FAILED.append("score_abi mismatch")


def check_pytest(marker: str, label: str) -> None:
    _step(f"pytest -m {marker!r}")
    if _run([sys.executable, "-m", "pytest", "-q", "--no-header",
             "-m", marker]).returncode != 0:
        _FAILED.append(label)


def check_per_suite() -> None:
    """Every test file in its own process. tests/run_all.sh is a wrapper over
    `--only per-suite`. A failing suite shows its last lines, and `--verbose` lists every
    suite with its count."""
    _step("per-suite run (one process per file)")
    files = sorted(glob.glob(os.path.join(_ROOT, "tests", "test_*.py")))
    bad, empty = [], []
    for path in files:
        rel = os.path.relpath(path, _ROOT)
        p = _run([sys.executable, "-m", "pytest", "-q", "--no-header", "-m", "not perf", rel],
                 capture_output=True, text=True)
        if p.returncode == 5:            # "no tests collected" — legitimate only for perf
            empty.append(rel)
            if _VERBOSE:
                print(f"  --   {rel} (no tests for -m 'not perf')")
        elif p.returncode != 0:
            bad.append(rel)
            print(f"  FAIL {rel} (exit {p.returncode})")
            for line in (p.stdout + p.stderr).strip().splitlines()[-12:]:
                print(f"         {line}")
        elif _VERBOSE:
            n = re.search(r"(\d+) passed", p.stdout)
            print(f"  ok   {rel:<46} {n.group(0) if n else ''}")
    print(f"  {len(files) - len(bad) - len(empty)} suites ok, {len(bad)} failed, "
          f"{len(empty)} with no tests for this marker")
    if bad:
        _FAILED.append(f"per-suite ({len(bad)} files)")


def check_tool(name: str, args: list[str], label: str, hint: str) -> None:
    _step(name)
    cmd = _tool(name)
    if cmd is None:
        print(f"  MISSING TOOL: {name} — {hint}")
        _FAILED.append(f"{name} not installed")
        return
    if _run(cmd + args).returncode != 0:
        _FAILED.append(label)


def check_mypy() -> None:
    _step("mypy (non-regression vs tools/mypy_baseline.json)")
    if _tool("mypy") is None:
        print("  MISSING TOOL: mypy — python -m pip install -r requirements-dev.txt")
        _FAILED.append("mypy not installed")
        return
    rc = _run([sys.executable, os.path.join("tools", "mypy_gate.py")]).returncode
    if rc == 3:
        # Environment mismatch: mypy's count depends on the stub versions it reads, so the
        # ratchet is meaningless here. Not a failure, but NOT a pass either — it goes in the
        # summary.
        _NOT_RUN.append("mypy ratchet (environment differs from the baseline)")
    elif rc == 2:
        # Distinct from a regression: exit 2 means mypy could not run, or reported errors
        # OUTSIDE the target package (an installed stub that won't parse). Calling that a
        # "regression" sends someone hunting for a type error that does not exist.
        _FAILED.append("mypy could not run (environment problem, not a regression)")
    elif rc != 0:
        _FAILED.append("mypy regression")


_SCAN_COUNTS = re.compile(r"\((\d+) structural, (\d+) site-specific\)")


def check_leaks() -> None:
    """Structural leak scan. Always runs: the built-in patterns need no configuration, and a
    check that only runs when someone remembers to configure it is not a check. Site-specific
    names come from $SWI3S_LEAK_PATTERNS (kept outside the repo).

    The site-specific half is NOT RUN when the variable is unset or the scan reports zero
    site patterns from it, and a scan whose header names no pattern counts fails."""
    _step("leak scan (structural; site patterns via $SWI3S_LEAK_PATTERNS)")
    scan = _run([sys.executable, os.path.join("tools", "leak_scan.py")],
                capture_output=True, text=True)
    print(scan.stdout, end="")
    if scan.stderr:
        print(scan.stderr, end="", file=sys.stderr)
    counts = _SCAN_COUNTS.search(scan.stdout)
    if scan.returncode != 0:
        _FAILED.append("leak scan")
    elif counts is None:
        _FAILED.append("leak scan (its header names no pattern counts, so what it covered "
                       "is unknown)")
    elif not os.environ.get("SWI3S_LEAK_PATTERNS"):
        # The scanner discloses this inline; promote it to the summary too, beside the mypy
        # env-mismatch case, so partial coverage never reads as a clean pass.
        _NOT_RUN.append("leak scan: site-specific patterns "
                        "($SWI3S_LEAK_PATTERNS unset — structural checks only)")
    elif int(counts.group(2)) == 0:
        _NOT_RUN.append("leak scan: site-specific patterns ($SWI3S_LEAK_PATTERNS names a "
                        "file holding no patterns — structural checks only)")


SCREENSHOTS_DIR = os.path.join("build", "screenshots")


def check_screenshots() -> None:
    """tools/screenshots.py: the three modes in light and dark, started in each theme and
    switched live, checked for faults and laid out on a contact sheet for review. It runs on
    the native windowing platform (the gate's offscreen setting is dropped), so the platform's
    own style is the one checked; with no display it falls back to offscreen, which is NOT RUN
    for the native style."""
    _step(f"screenshots (tools/screenshots.py, into {SCREENSHOTS_DIR})")
    env = _env()
    env.pop("QT_QPA_PLATFORM", None)
    shots = subprocess.run([sys.executable, os.path.join("tools", "screenshots.py"),
                            "--out", os.path.join(_ROOT, SCREENSHOTS_DIR)],
                           cwd=_ROOT, env=env, capture_output=True, text=True)
    print(shots.stdout, end="")
    if shots.returncode != 0:
        if shots.stderr:
            print(shots.stderr[-4000:], end="", file=sys.stderr)
        _FAILED.append("screenshots (see build/screenshots/report.txt)")
    elif "NOTE: no display" in shots.stdout:
        _NOT_RUN.append("screenshots: the native style (no display here; the offscreen "
                        "platform was checked)")


def check_native_cpp() -> None:
    """The native C++ suite, which lives in the sibling protocol-analyzer checkout. Opt-in
    (`--only native-cpp`): not part of the full gate, and a missing sibling is NOT RUN,
    not a silent skip."""
    _step("native C++ suite (sibling protocol-analyzer checkout)")
    runner = os.path.join(_ROOT, "..", "protocol-analyzer", "SwI3sAnalyzer", "test",
                          "run_tests.sh")
    if _WINDOWS or not os.path.isfile(runner):
        _NOT_RUN.append(f"native C++ suite ({runner} not available here)")
        return
    if _run(["bash", runner]).returncode != 0:
        _FAILED.append("native C++ suite")


_VERBOSE = False

# name -> (check, runs in the full gate, skipped by --quick). Order is run order. Each is
# looked up when it runs (the lambdas), so a test can stub a check by its module name.
_CHECKS = {
    "native": (lambda: check_native(), True, False),
    "pytest": (lambda: check_pytest("not perf", "pytest not-perf"), True, False),
    "perf": (lambda: check_pytest("perf", "pytest perf"), True, True),
    "per-suite": (lambda: check_per_suite(), True, True),
    "ruff": (lambda: check_tool("ruff", ["check", "."], "ruff",
                                "python -m pip install -r requirements-dev.txt"),
             True, False),
    "mypy": (lambda: check_mypy(), True, False),
    "leaks": (lambda: check_leaks(), True, False),
    "screenshots": (lambda: check_screenshots(), True, False),
    "native-cpp": (lambda: check_native_cpp(), False, False),
}


def _selected(argv: list[str]) -> list[str] | None:
    """The checks named by --only (None = the full gate). An unknown name is an error, not
    a silently empty run."""
    if "--only" not in argv:
        return None
    i = argv.index("--only")
    names = [n for n in (argv[i + 1] if i + 1 < len(argv) else "").split(",") if n]
    unknown = [n for n in names if n not in _CHECKS]
    if not names or unknown:
        raise SystemExit(f"--only needs a comma-separated list of {', '.join(_CHECKS)}"
                         + (f" (unknown: {', '.join(unknown)})" if unknown else ""))
    return names


def main() -> int:
    global _VERBOSE
    quick = "--quick" in sys.argv
    _VERBOSE = "--verbose" in sys.argv
    only = _selected(sys.argv)
    print(f"release gate — {sys.platform}, python {sys.version.split()[0]}"
          f"{' (--quick)' if quick else ''}"
          f"{' (--only ' + ','.join(only) + ')' if only else ''}")

    for name, (check, in_gate, quick_skip) in _CHECKS.items():
        if only is not None:
            if name in only:
                check()
        elif in_gate and not (quick and quick_skip):
            check()

    print("\n=== gate summary ===")
    if _NOT_RUN:
        print(f"  NOT RUN HERE ({len(_NOT_RUN)}):")
        for n in _NOT_RUN:
            print(f"    - {n}")
    if not _FAILED:
        if only is not None:
            print(f"  PASS ({', '.join(only)} only) — a partial run, not the release gate")
            return 0
        print("  PASS — every applicable check is green")
        if quick:
            print("  (--quick SKIPPED perf and the per-suite pass — not a release gate)")
        return 0
    print(f"  FAIL ({len(_FAILED)}):")
    for f in _FAILED:
        print(f"    - {f}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
