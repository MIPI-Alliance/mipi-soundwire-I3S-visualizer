#!/usr/bin/env python3
"""THE gate — one implementation, every platform. Exit 0 is the gate.

    python3 tools/gate.py              # the commit gate: everything, in parallel
    python3 tools/gate.py --guest      # a test guest, per batch: gui + platform + a core smoke set
    python3 tools/gate.py --release    # before a tag: also the single-process run
    python3 tools/gate.py --release --on-guest   # before a tag, on a test guest
    python3 tools/gate.py --quick      # the commit gate without the perf ceilings
    python3 tools/gate.py --only suite --verbose    # one or more named checks
    python3 tools/gate.py --only native,native-cpp  # (comma-separated; see _CHECKS)

The stages, their scope and their budgets are docs/TESTING.md §1.1. `tests/gate.sh` and
`tests/gate.ps1` are thin wrappers around this file so either shell has a natural entry point
without a second copy of the logic.

Python, so it runs on Windows too, where `bash` may not be on PATH,
`native/build_local.sh` is a Unix build path (Windows builds the core through pip/MSVC),
`python3` is not a reliable name, and venv-installed linters may not be on PATH. All four
are handled here.

Checks, in order (a failure never stops the rest — you get the whole picture):
  1. native core rebuilt, the imported module ASSERTED to be the one just built, and its
     score_abi ASSERTED against session.py's _REQUIRED_SCORE_ABI
  2. suite: every test file in its own process, several at once ($SWI3S_TEST_WORKERS, else one
     per CPU), which catches a teardown crash a pooled run can mask; --guest runs the `gui`
     and `platform` tests and _CORE_SMOKE. Each run writes its per-test durations to
     build/test-durations/<platform>.txt (<platform>-guest.txt for --guest)
  3. pytest -m perf, in one process          (skipped by --quick)
  4. the single-process run of the suite     (--release only, and not --on-guest) — catches
     state leaking from one file to the next, which is a property of the tests, not the platform
  5. ruff                                    (not on --guest: the same on every platform)
  6. mypy, non-regression vs tools/mypy_baseline.json   (not on --guest)
  7. leak scan
  8. screenshots: every mode in both themes, checked for theme, contrast and clipped-text
     faults (tools/screenshots.py); the contact sheet is the gate's visual review
  9. native coverage from an instrumented core (--release only, and not --on-guest;
     tools/native_coverage.py), reported in build/native-coverage/report.txt
  (native-cpp, an external C++ suite, runs only when named with --only and only when
   $SWI3S_NATIVE_CPP_SUITE names its runner script; otherwise the check is NOT RUN)

Each check's wall time is printed in the summary, and a stage over its budget (_BUDGETS) is
named there: a warning, not a failure. A process a check runs that outlives _STAGE_TIMEOUT is
killed with every process it started, and the check is a named failure.

`--only` runs a subset and says so: its summary is never a bare PASS, because a partial
run is not the gate. `tests/run_all.sh` is a wrapper over `--only`.

A MISSING TOOL IS A FAILURE, never a silent skip.
"""
from __future__ import annotations

import glob
import importlib.util
import os
import pathlib
import re
import shutil
import signal
import site as _site
import subprocess
import sys
import sysconfig
import textwrap
import time
from concurrent.futures import ThreadPoolExecutor

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


# Seconds a process a check runs (outside the suite's per-file limit) may take before it is
# killed with every process it started and its check is a named failure.
_STAGE_TIMEOUT = 7200


class _StageTimeout(Exception):
    """A check's process outlived `_STAGE_TIMEOUT`; main() names the check as failed."""


def _run(cmd: list[str], env: dict[str, str] | None = None, capture_output: bool = False,
         **kw) -> subprocess.CompletedProcess:
    """`cmd` in the checkout with `env` (else `_env()`), in a process group of its own, its
    output captured when `capture_output` is set. Past `_STAGE_TIMEOUT` seconds, or on an
    interrupt of the gate, the group is killed; the timeout raises `_StageTimeout`."""
    if capture_output:
        kw.update(stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    p = subprocess.Popen(cmd, cwd=_ROOT, env=_env() if env is None else env,
                         start_new_session=not _WINDOWS,
                         creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                         if _WINDOWS else 0, **kw)
    _LIVE.add(p)
    try:
        out, err = p.communicate(timeout=_STAGE_TIMEOUT)
    except subprocess.TimeoutExpired:
        _kill_tree(p)
        try:
            p.communicate(timeout=_KILL_GRACE)
        except subprocess.TimeoutExpired:
            pass                                # a process outside the group holds the pipes
        raise _StageTimeout(f"{os.path.basename(cmd[0])} {' '.join(cmd[1:3])}") from None
    except KeyboardInterrupt:
        _kill_tree(p)
        raise
    finally:
        _LIVE.discard(p)
    return subprocess.CompletedProcess(cmd, p.returncode, out, err)


# A child's captured output is read as UTF-8 on every platform, whatever the locale (_env
# sets PYTHONUTF8, so a Python child writes it), with errors="replace" so an undecodable
# byte never ends the gate: every capture here passes encoding="utf-8", errors="replace".


# Seconds the output of a killed process tree is waited for before it is given up.
_KILL_GRACE = 30
# The processes _run and _run_bounded have running. Each is in a process group of its own, so
# an interrupt of the gate does not reach it: check_suite kills them when it is interrupted.
_LIVE: set[subprocess.Popen] = set()


def _kill_tree(p: subprocess.Popen) -> None:
    """Kill `p` and every process it started: its process group on POSIX, its tree on
    Windows."""
    if _WINDOWS:
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(p.pid)], capture_output=True)
    else:
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    try:
        p.kill()
    except OSError:
        pass


def _run_bounded(cmd: list[str], timeout: float) -> subprocess.CompletedProcess:
    """`cmd` with its output captured as text, in a process group of its own. Past `timeout`
    seconds the whole group is killed, the processes it started (pytest's xdist workers)
    with it, and TimeoutExpired is raised carrying the output read by then."""
    p = subprocess.Popen(cmd, cwd=_ROOT, env=_env(), stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, encoding="utf-8", errors="replace",
                         start_new_session=not _WINDOWS,
                         creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                         if _WINDOWS else 0)
    _LIVE.add(p)
    try:
        out, err = p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_tree(p)
        try:
            out, err = p.communicate(timeout=_KILL_GRACE)
        except subprocess.TimeoutExpired:
            out, err = "", ""                   # a process outside the group holds the pipes
        raise subprocess.TimeoutExpired(cmd, timeout, output=out, stderr=err) from None
    finally:
        _LIVE.discard(p)
    return subprocess.CompletedProcess(cmd, p.returncode, out, err)


def _step(title: str) -> None:
    print(f"\n=== {title} ===", flush=True)


def _tool(name: str) -> list[str] | None:
    """A tool as a runnable command: prefer the executable, fall back to `-m` so a
    venv-installed linter works without PATH surgery (the Windows case)."""
    exe = shutil.which(name)
    if exe:
        return [exe]
    probe = _run([sys.executable, "-m", name, "--version"],
                 capture_output=True, encoding="utf-8", errors="replace")
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


def _load_build_overrides() -> tuple[str, ...]:
    """BUILD_OVERRIDES from tools/build_overrides.py, loaded by path (tools/ is not a package)."""
    spec = importlib.util.spec_from_file_location(
        "_build_overrides", os.path.join(_ROOT, "tools", "build_overrides.py"))
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.BUILD_OVERRIDES


# Variables native/build_local.sh honours that change the binary it builds.
_BUILD_OVERRIDES = _load_build_overrides()


def check_native() -> None:
    _step("native core: rebuild + ABI assert")
    if _WINDOWS:
        # build_local.sh is a Unix compile path; on Windows the core builds via MSVC through
        # pip. --force-reinstall replaces the installed module even when pip would judge it
        # current; --no-deps skips re-resolving the app deps.
        build = [sys.executable, "-m", "pip", "install", "--force-reinstall", "--no-deps",
                 os.path.join(_ROOT, "native")]
    else:
        build = ["bash", os.path.join("native", "build_local.sh")]
    # The build output path and flags are the caller's to set for their own builds; the core
    # the suite and the perf gate run is the default build.
    env = {k: v for k, v in _env().items() if k not in _BUILD_OVERRIDES}
    built = _run(build, env=env, capture_output=True, encoding="utf-8", errors="replace")
    if built.returncode != 0:
        tail = ((built.stdout or "") + (built.stderr or "")).strip().splitlines()[-40:]
        if tail:
            print(textwrap.indent("\n".join(tail), "    "))
        print("  build FAILED")
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
                 capture_output=True, encoding="utf-8", errors="replace")
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


# Core test files the guest gate runs besides the `gui` and `platform` tests, one per area, so
# a guest still decodes, replays registers, times and parses on its own interpreter.
_CORE_SMOKE = ("tests/test_swi3score.py", "tests/test_register_model.py",
               "tests/test_timing_cross_spec.py", "tests/test_ingest_formats.py",
               "tests/test_regmap_import.py", "tests/test_publish_sync.py")
# The heaviest files, each spread over this many workers of its own (pytest-xdist, --dist
# load): the suite takes as long as its slowest file. Each file's tests are independent of
# order; tests/test_exit_teardown.py times the app's exit and stays whole.
_SPLIT = {"tests/test_links_gui.py": 6, "tests/test_maximize_pane.py": 4,
          "tests/test_bottom_all_links.py": 4, "tests/test_menus.py": 4,
          "tests/test_timing_cross_spec.py": 4, "tests/test_open_capture_dialog.py": 3,
          "tests/test_command_deltas.py": 3, "tests/test_load_queue_settles.py": 3,
          "tests/test_timing_view_page.py": 4, "tests/test_timing_view_settings.py": 3,
          "tests/test_timing_diagram_layout.py": 3}
_DURATION = re.compile(r"^\s*([0-9.]+)s (call|setup|teardown)\s+(\S+)")
DURATIONS_DIR = os.path.join("build", "test-durations")
# Seconds one test file may run before it is killed, with every process it started, and
# counted as failed, so a hung file (a deadlocked window, a teardown that never returns) ends
# the gate with a named failure. Windows runs the suite about three times slower than the
# development machine and gets twice the time.
_FILE_TIMEOUT = 3600 if _WINDOWS else 1800
_TIMED_OUT = 124            # the exit status a timed-out file is reported with


def _workers() -> int:
    """$SWI3S_TEST_WORKERS, else one per CPU."""
    env = os.environ.get("SWI3S_TEST_WORKERS", "")
    return max(1, int(env)) if env.isdigit() else (os.cpu_count() or 1)


def _durations_record(guest: bool) -> str:
    """The durations record of a stage's suite run: build/test-durations/<platform>.txt for the
    whole suite, <platform>-guest.txt for the guest scope, so a run of one scope never replaces
    the other's record."""
    return os.path.join(_ROOT, DURATIONS_DIR,
                        f"{sys.platform}{'-guest' if guest else ''}.txt")


def _suite_files(guest: bool) -> list[tuple[str, str]]:
    """(file, -m expression) for every test file the suite runs: the spread files first, then
    longest first by the last run's durations of the same scope here (else by size), so the
    slowest files do not start last. A record line that does not parse is skipped."""
    files = sorted(glob.glob(os.path.join(_ROOT, "tests", "test_*.py")))
    rels = [os.path.relpath(f, _ROOT).replace(os.sep, "/") for f in files]
    if guest:
        runs = [(r, "not perf" if r in _CORE_SMOKE else "(gui or platform) and not perf")
                for r in rels]
    else:
        runs = [(r, "not perf") for r in rels]
    last: dict[str, float] = {}
    record = _durations_record(guest)
    if os.path.isfile(record):
        with open(record, encoding="utf-8", errors="replace") as f:
            for line in f:
                parts = line.split()
                if len(parts) == 3 and parts[0] == "file":
                    try:
                        last[parts[2]] = float(parts[1])
                    except ValueError:
                        continue
    return sorted(runs, key=lambda fr: (fr[0] not in _SPLIT, -last.get(fr[0], os.path.getsize(
        os.path.join(_ROOT, fr[0])) / 1e4)))


def _run_file(rel: str, marker: str) -> tuple[str, subprocess.CompletedProcess, float]:
    start = time.monotonic()
    spread = ["-n", str(_SPLIT[rel]), "--dist", "load"] if rel in _SPLIT else []
    cmd = [sys.executable, "-m", "pytest", "-q", "--no-header", "-p", "no:cacheprovider",
           "-m", marker, "--durations=0", "--durations-min=0", *spread, rel]
    try:
        p = _run_bounded(cmd, _FILE_TIMEOUT)
    except subprocess.TimeoutExpired as e:
        out = e.stdout.decode("utf-8", "replace") if isinstance(e.stdout, bytes) else e.stdout
        p = subprocess.CompletedProcess(cmd, _TIMED_OUT, out or "",
                                        f"timed out after {_FILE_TIMEOUT} s")
    return rel, p, time.monotonic() - start


def check_suite() -> None:
    """Every test file in its own process, `_workers()` at a time: the suite's result and a
    teardown crash a pooled run can mask, from one run. tests/run_all.sh is a wrapper over
    `--only suite`. A failing file shows its last lines, and `--verbose` lists every file
    with its count."""
    guest = _MODE == "guest"
    runs = _suite_files(guest)
    workers = _workers()
    _step(f"suite: each file in its own process, {workers} at a time"
          f"{' (guest scope: gui + platform + core smoke)' if guest else ''}")
    bad, empty, per_test, per_file = [], [], {}, {}
    totals = {"passed": 0, "skipped": 0}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        try:
            for rel, p, took in pool.map(lambda fr: _run_file(*fr), runs):
                per_file[rel] = took
                for key in totals:
                    m = re.search(rf"(\d+) {key}", p.stdout)
                    totals[key] += int(m.group(1)) if m else 0
                for line in p.stdout.splitlines():
                    d = _DURATION.match(line)
                    if d:
                        per_test[d.group(3)] = per_test.get(d.group(3), 0.0) + float(d.group(1))
                if p.returncode == 5:            # no tests for this file's -m expression
                    empty.append(rel)
                    if _VERBOSE:
                        print(f"  --   {rel} (no tests in scope)")
                elif p.returncode != 0:
                    bad.append(rel)
                    print(f"  FAIL {rel} (exit {p.returncode})")
                    tail = [ln for ln in (p.stdout + p.stderr).strip().splitlines()
                            if not _DURATION.match(ln)]
                    for line in tail[-12:]:
                        print(f"         {line}")
                elif _VERBOSE:
                    n = re.search(r"(\d+) passed", p.stdout)
                    print(f"  ok   {rel:<46} {n.group(0) if n else '':>12}  {took:6.1f} s")
        except BaseException:                  # an interrupt: stop what runs and what waits
            pool.shutdown(wait=False, cancel_futures=True)
            for running in list(_LIVE):
                _kill_tree(running)
            raise
    print(f"  {len(runs) - len(bad) - len(empty)} files ok, {len(bad)} failed, "
          f"{len(empty)} with no tests in scope; {totals['passed']} passed, "
          f"{totals['skipped']} skipped")
    _write_durations(per_test, per_file, guest)
    if bad:
        _FAILED.append(f"suite ({len(bad)} files)")


def _write_durations(per_test: dict[str, float], per_file: dict[str, float],
                     guest: bool = False) -> None:
    """The scope's record (_durations_record): `file <seconds> <path>` per file (wall time, its
    own process) and `test <seconds> <id>` per test (setup + call + teardown), slowest first."""
    record = _durations_record(guest)
    os.makedirs(os.path.dirname(record), exist_ok=True)
    lines = [f"file {s:.3f} {f}" for f, s in sorted(per_file.items(), key=lambda x: -x[1])]
    lines += [f"test {s:.3f} {t}" for t, s in sorted(per_test.items(), key=lambda x: -x[1])]
    with open(record, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print(f"  durations: {os.path.relpath(record, _ROOT)}")


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
                capture_output=True, encoding="utf-8", errors="replace")
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
    shots = _run([sys.executable, os.path.join("tools", "screenshots.py"),
                  "--out", os.path.join(_ROOT, SCREENSHOTS_DIR)],
                 env=env, capture_output=True, encoding="utf-8", errors="replace")
    print(shots.stdout, end="")
    if shots.returncode != 0:
        if shots.stderr:
            print(shots.stderr[-4000:], end="", file=sys.stderr)
        _FAILED.append("screenshots (see build/screenshots/report.txt)")
    elif "NOTE: no display" in shots.stdout:
        _NOT_RUN.append("screenshots: the native style (no display here; the offscreen "
                        "platform was checked)")


def check_native_coverage() -> None:
    """tools/native_coverage.py: the native core's line and branch coverage from an
    instrumented build, measured and reported (build/native-coverage/report.txt), with no
    floor. NOT RUN where clang's coverage tools are missing (Windows builds through MSVC)."""
    _step("native coverage (tools/native_coverage.py, an instrumented core)")
    rc = _run([sys.executable, os.path.join("tools", "native_coverage.py")]).returncode
    if rc == 3:
        _NOT_RUN.append("native coverage (clang++, llvm-profdata or llvm-cov missing here)")
    elif rc != 0:
        _FAILED.append("native coverage (see the output above: the build, the suite, "
                       "an empty report or the command line)")


def check_native_cpp() -> None:
    """An external native C++ suite, run through the shell script that $SWI3S_NATIVE_CPP_SUITE
    names. Opt-in (`--only native-cpp`): not part of the full gate, and an unset variable or a
    missing script is NOT RUN, not a silent skip."""
    _step("native C++ suite ($SWI3S_NATIVE_CPP_SUITE)")
    runner = os.environ.get("SWI3S_NATIVE_CPP_SUITE", "")
    if _WINDOWS or not runner or not os.path.isfile(runner):
        _NOT_RUN.append("native C++ suite (set SWI3S_NATIVE_CPP_SUITE to the runner script; "
                        "bash required)")
        return
    if _run(["bash", runner]).returncode != 0:
        _FAILED.append("native C++ suite")


_VERBOSE = False
_MODE = "commit"             # commit | guest | release, from the command line
_ON_GUEST = False            # --on-guest: a test guest, which leaves _DEV_ONLY to the
                             # development machine
# Checks of the release gate the development machine runs for every platform (docs/TESTING.md
# §1.1): what they find does not depend on where they run.
_DEV_ONLY = ("single-process", "native-coverage")

# Seconds a stage may take before the summary names it (docs/TESTING.md §1.1).
_BUDGETS = {"commit": 600, "guest": 1200}

# name -> (check, the stages it runs in, skipped by --quick). Order is run order. Each is
# looked up when it runs (the lambdas), so a test can stub a check by its module name.
_ALL = frozenset({"commit", "guest", "release"})
_CHECKS = {
    "native": (lambda: check_native(), _ALL, False),
    "suite": (lambda: check_suite(), _ALL, False),
    "perf": (lambda: check_pytest("perf", "pytest perf"), _ALL, True),
    "single-process": (lambda: check_pytest("not perf", "pytest single-process"),
                       frozenset({"release"}), False),
    "ruff": (lambda: check_tool("ruff", ["check", "."], "ruff",
                                "python -m pip install -r requirements-dev.txt"),
             frozenset({"commit", "release"}), False),
    "mypy": (lambda: check_mypy(), frozenset({"commit", "release"}), False),
    "leaks": (lambda: check_leaks(), _ALL, False),
    "screenshots": (lambda: check_screenshots(), _ALL, False),
    "native-coverage": (lambda: check_native_coverage(), frozenset({"release"}), False),
    "native-cpp": (lambda: check_native_cpp(), frozenset(), False),
}


def _selected(argv: list[str]) -> list[str] | None:
    """The checks named by --only (None = the stage's checks). An unknown name is an error,
    not a silently empty run."""
    if "--only" not in argv:
        return None
    i = argv.index("--only")
    names = [n for n in (argv[i + 1] if i + 1 < len(argv) else "").split(",") if n]
    unknown = [n for n in names if n not in _CHECKS]
    if not names or unknown:
        raise SystemExit(f"--only needs a comma-separated list of {', '.join(_CHECKS)}"
                         + (f" (unknown: {', '.join(unknown)})" if unknown else ""))
    return names


_FLAGS = frozenset({"--guest", "--release", "--on-guest", "--quick", "--verbose", "--only"})


def _bad_arguments(argv: list[str]) -> str | None:
    """Why `argv` is not a valid command line, or None. An unknown option, the two stages
    together, --quick with a stage (it trims the commit gate only) and --on-guest without a
    stage are errors: a mistyped or mismatched command line must not run a different gate and
    report PASS."""
    args = argv[1:]
    skip = args.index("--only") + 1 if "--only" in args else -1      # --only's value
    unknown = [a for i, a in enumerate(args) if i != skip and a not in _FLAGS]
    if unknown:
        return f"unknown argument(s): {' '.join(unknown)} (known: {' '.join(sorted(_FLAGS))})"
    if "--guest" in args and "--release" in args:
        return "--guest and --release name two stages; give one (--release --on-guest is the " \
               "release gate on a guest)"
    stage = next((a for a in ("--guest", "--release") if a in args), None)
    if "--quick" in args and stage:
        return f"--quick trims the commit gate only; {stage} runs its perf ceilings"
    if "--on-guest" in args and not stage:
        return "--on-guest goes with a stage: --release --on-guest or --guest --on-guest"
    return None


def _tolerant_output() -> None:
    """Make stdout and stderr write a character their encoding cannot hold as a '?' rather
    than raise: the tool echoes test and tool output, and a Windows console or redirected log
    is often cp1252."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(errors="replace")


def _terminate_as_interrupt() -> None:
    """SIGTERM and SIGHUP end the run as a KeyboardInterrupt does, so the process groups it
    started are killed and its working directories removed: each child is in a session of its
    own and would otherwise outlive a killed or hung-up parent."""
    def _raise(signum, frame):
        raise KeyboardInterrupt
    for name in ("SIGTERM", "SIGHUP"):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), _raise)


def main() -> int:
    global _VERBOSE, _MODE, _ON_GUEST
    problem = _bad_arguments(sys.argv)
    if problem:
        print(f"gate: {problem}", file=sys.stderr)
        return 2
    _terminate_as_interrupt()
    _tolerant_output()
    quick = "--quick" in sys.argv
    _ON_GUEST = "--on-guest" in sys.argv
    _VERBOSE = "--verbose" in sys.argv
    _MODE = "guest" if "--guest" in sys.argv else "release" if "--release" in sys.argv \
        else "commit"
    only = _selected(sys.argv)
    print(f"{_MODE} gate — {sys.platform}, python {sys.version.split()[0]}"
          f"{' (--quick)' if quick else ''}"
          f"{' (--only ' + ','.join(only) + ')' if only else ''}")

    took: dict[str, float] = {}
    for name, (check, stages, quick_skip) in _CHECKS.items():
        if only is not None:
            run = name in only
        else:
            run = (_MODE in stages and not (quick and quick_skip)
                   and not (_ON_GUEST and name in _DEV_ONLY))
        if run:
            start = time.monotonic()
            try:
                check()
            except _StageTimeout as e:
                print(f"  TIMED OUT after {_STAGE_TIMEOUT} s: {e}, killed with every process "
                      "it started")
                _FAILED.append(f"{name}: timed out after {_STAGE_TIMEOUT} s ({e})")
            took[name] = time.monotonic() - start

    print("\n=== gate summary ===")
    total = sum(took.values())
    print("  time: " + ", ".join(f"{n} {s:.0f} s" for n, s in took.items())
          + f"; total {total:.0f} s")
    budget = _BUDGETS.get(_MODE)
    if only is None and budget is not None and total > budget:
        print(f"  OVER BUDGET: the {_MODE} gate took {total:.0f} s against {budget} s "
              f"(docs/TESTING.md §1.1)")
    if _NOT_RUN:
        print(f"  NOT RUN HERE ({len(_NOT_RUN)}):")
        for n in _NOT_RUN:
            print(f"    - {n}")
    if not _FAILED:
        if only is not None:
            print(f"  PASS ({', '.join(only)} only) — a partial run, not the gate")
            return 0
        print(f"  PASS — every applicable check of the {_MODE} gate is green")
        if quick:
            print("  (--quick SKIPPED the perf ceilings — not the commit gate)")
        if _MODE == "guest":
            print("  (the guest scope: not the release gate, which is --release)")
        if _MODE == "release" and _ON_GUEST:
            print(f"  (on a guest: {' and '.join(_DEV_ONLY)} are the development machine's)")
        return 0
    print(f"  FAIL ({len(_FAILED)}):")
    for f in _FAILED:
        print(f"    - {f}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
