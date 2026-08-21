#!/usr/bin/env python3
"""THE release gate — one implementation, every platform.

Run it on macOS AND Windows at the release commit. Exit 0 is the gate.

    python3 tools/gate.py            # full gate
    python3 tools/gate.py --quick    # skip perf + the per-suite pass (dev loop)

`tests/gate.sh` and `tests/gate.ps1` are thin wrappers around this file so either shell
has a natural entry point without a second copy of the logic.

Why Python and not bash: the first version of this gate was a bash script, which made it
Unix-only and so re-created the very gap it was written to close — the Windows half of the
release gate could not run it. `bash` is not on PATH on the Windows test VM,
`native/build_local.sh` is a Unix build path (Windows builds the core through pip/MSVC),
`python3` is not a reliable name there, and venv-installed linters are not on PATH. All
four are handled here instead of assumed away.

Checks, in order (a failure never stops the rest — you get the whole picture):
  1. native core rebuilt, and score_abi ASSERTED against session.py's _REQUIRED_SCORE_ABI
     (a stale .pyd/.so has faked a green Windows result before)
  2. pytest -m "not perf"
  3. pytest -m perf                       (skipped by --quick)
  4. each test file in its OWN process     (skipped by --quick) — catches teardown crashes
     a pooled run can mask; this is what surfaced the 3.0.10 QThread-on-quit bug
  5. ruff
  6. mypy, non-regression vs tools/mypy_baseline.json

A MISSING TOOL IS A FAILURE, never a silent skip: a check that quietly does nothing is how
the lint job went five releases without anyone noticing it had never run.
"""
from __future__ import annotations

import glob
import os
import re
import shutil
import subprocess
import sys

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
    # SYSTEM interpreter, which under a venv-driven gate is a different build of Python — on
    # the Ubuntu guest that has no pybind11 at all and the build simply failed. Silently
    # worse where the versions differ: the extension would be built with the wrong suffix
    # for the interpreter that then imports it.
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
        # suite that follows then runs against whatever core is already installed — the exact
        # stale-core hazard the assert exists to catch. A first run on a new platform failed
        # here and reported one problem where there were two: a broken build, and 976 tests
        # whose subject was unverified. Aggregation is deliberate (one red must not hide the
        # rest), so do not abort — but never let the skipped assert go unmentioned.
        _NOT_RUN.append("score_abi assert (the build failed; any tests below ran against "
                        "whatever core was already installed)")
        return

    src = open(os.path.join(_ROOT, "swi3s_studio", "session.py"), encoding="utf-8").read()
    want = int(re.search(r"_REQUIRED_SCORE_ABI\s*=\s*(\d+)", src).group(1))
    probe = _run([sys.executable, "-c",
                  "import swi3score, swi3s_studio as s;"
                  "print(swi3score.score_abi, s.__version__)"],
                 capture_output=True, text=True)
    if probe.returncode != 0:
        print(f"  import FAILED:\n{probe.stderr.strip()}")
        _FAILED.append("native import")
        return
    abi, ver = probe.stdout.split()
    print(f"  score_abi={abi}  __version__={ver}  (session.py requires {want})")
    if int(abi) != want:
        print("  MISMATCH — the built core does not match what the app requires")
        _FAILED.append("score_abi mismatch")


def check_pytest(marker: str, label: str) -> None:
    _step(f"pytest -m {marker!r}")
    if _run([sys.executable, "-m", "pytest", "-q", "--no-header",
             "-m", marker]).returncode != 0:
        _FAILED.append(label)


def check_per_suite() -> None:
    """Every test file in its own process. Implemented here rather than shelling out to
    tests/run_all.sh, which is bash — that is why this step had never run on Windows."""
    _step("per-suite run (one process per file)")
    files = sorted(glob.glob(os.path.join(_ROOT, "tests", "test_*.py")))
    bad, empty = [], []
    for path in files:
        rel = os.path.relpath(path, _ROOT)
        p = _run([sys.executable, "-m", "pytest", "-q", "--no-header", "-m", "not perf", rel],
                 capture_output=True, text=True)
        if p.returncode == 5:            # "no tests collected" — legitimate only for perf
            empty.append(rel)
        elif p.returncode != 0:
            bad.append(rel)
            print(f"  FAIL {rel}")
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
        print("  MISSING TOOL: mypy — brew install mypy  (or python -m pip install mypy)")
        _FAILED.append("mypy not installed")
        return
    rc = _run([sys.executable, os.path.join("tools", "mypy_gate.py")]).returncode
    if rc == 3:
        # Environment mismatch: mypy's count depends on the stub versions it reads, so the
        # ratchet is meaningless here. Not a failure, but NOT a pass either — it goes in the
        # summary, because a check that silently does nothing is the bug this gate exists for.
        _NOT_RUN.append("mypy ratchet (environment differs from the baseline)")
    elif rc == 2:
        # Distinct from a regression: exit 2 means mypy could not run, or reported errors
        # OUTSIDE the target package (an installed stub that won't parse). Calling that a
        # "regression" sends someone hunting for a type error that does not exist.
        _FAILED.append("mypy could not run (environment problem, not a regression)")
    elif rc != 0:
        _FAILED.append("mypy regression")


def check_leaks() -> None:
    """Structural leak scan. Always runs: the built-in patterns need no configuration, and a
    check that only runs when someone remembers to configure it is not a check. Site-specific
    names come from $SWI3S_LEAK_PATTERNS (kept outside the repo)."""
    _step("leak scan (structural; site patterns via $SWI3S_LEAK_PATTERNS)")
    if _run([sys.executable, os.path.join("tools", "leak_scan.py")]).returncode != 0:
        _FAILED.append("leak scan")
    elif not os.environ.get("SWI3S_LEAK_PATTERNS"):
        # The scanner discloses this inline, but only in its own scrollback — so a run with no
        # site patterns still ended with a bare "PASS". Promote it to the summary, where the
        # mypy env-mismatch case already appears: partial coverage that reads as a clean pass
        # is precisely the failure mode this gate exists to prevent.
        _NOT_RUN.append("leak scan: site-specific patterns "
                        "($SWI3S_LEAK_PATTERNS unset — structural checks only)")


def main() -> int:
    quick = "--quick" in sys.argv
    print(f"release gate — {sys.platform}, python {sys.version.split()[0]}"
          f"{' (--quick)' if quick else ''}")

    check_native()
    check_pytest("not perf", "pytest not-perf")
    if not quick:
        check_pytest("perf", "pytest perf")
        check_per_suite()
    check_tool("ruff", ["check", "."], "ruff",
               "brew install ruff  (or python -m pip install ruff)")
    check_mypy()
    check_leaks()

    print("\n=== gate summary ===")
    if _NOT_RUN:
        print(f"  NOT RUN HERE ({len(_NOT_RUN)}):")
        for n in _NOT_RUN:
            print(f"    - {n}")
    if not _FAILED:
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
