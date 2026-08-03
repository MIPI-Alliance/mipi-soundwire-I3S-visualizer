#!/usr/bin/env python3
"""Type-check gate: mypy must not get WORSE, per file.

The codebase has a large pre-existing mypy debt (448 errors in 29 files when first
measured, 2026-08-01 — it had never been run: `ci.yml` had `mypy … || true`, so even when
CI ran, the result was discarded). Fixing 448 errors is not a release-blocking task, but
letting the number grow silently is how it got to 448.

So this gates on NON-REGRESSION against a checked-in per-file baseline:

  * a file with MORE errors than its baseline fails
  * a file with errors that is absent from the baseline fails (new debt in clean code)
  * a file with FEWER errors passes, and the script tells you to re-baseline

Per-file, not a single total, because a total lets a fix in one file mask a regression in
another. Re-baseline deliberately with `--update` when you improve things.

    python3 tools/mypy_gate.py            # check (used by tests/gate.sh and ci.yml)
    python3 tools/mypy_gate.py --update   # record the current state as the new baseline
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_BASELINE = os.path.join(_ROOT, "tools", "mypy_baseline.json")
_TARGET = "swi3s_studio"
_LINE = re.compile(r"^(?P<file>[^:]+):\d+:(?:\d+:)? error:")


def _mypy_cmd() -> list[str]:
    """mypy may be a standalone binary (Homebrew, pipx) or a module in this interpreter.
    Prefer the binary — a Homebrew install is not importable by an unrelated python3."""
    exe = shutil.which("mypy")
    return [exe] if exe else [sys.executable, "-m", "mypy"]


def _env_fingerprint() -> dict[str, str]:
    """What makes mypy's output reproducible — or not.

    mypy's findings depend on the STUBS it reads, so a different numpy version yields a
    different count for the same source. Measured 2026-08-01: macOS/numpy 2.4.4 gave 205
    errors; the Windows VM with numpy 2.5.1 gave 222 for the identical tree (session.py
    alone 22 -> 38). Neither is wrong; they are not comparable. So the baseline records the
    environment it was taken in, and the gate refuses to pretend a mismatched environment
    is a regression.
    """
    def ver(mod: str) -> str:
        p = subprocess.run([sys.executable, "-c",
                            f"import {mod};print({mod}.__version__)"],
                           capture_output=True, text=True)
        return p.stdout.strip() or "?"
    mypy_ver = subprocess.run([*_mypy_cmd(), "--version"], capture_output=True, text=True)
    return {"mypy": mypy_ver.stdout.split()[1] if mypy_ver.stdout.split() else "?",
            "numpy": ver("numpy")}


def _run_mypy() -> tuple[dict[str, int], str]:
    """Per-file error counts, plus mypy's raw output for reporting.

    Runs with `--no-incremental`. A ratchet's whole value is that its number means the same
    thing twice, and with the cache on it did not: during the 3.0.13 paydown three runs over
    one tree reported 183, 205 and 207, the last two from a warm cache after unrelated files
    had been rewritten. It reported seven files as REGRESSED that had not been touched. The
    clean number was 183. A slower check that is reproducible beats a fast one that
    occasionally invents a regression — and a false regression is expensive here, because the
    honest response to it is to go looking for a bug that does not exist.

    Two cross-platform hazards, both found by running this on the Windows VM after it
    passed on macOS:

    * mypy reports NATIVE path separators, so Windows produced `swi3s_studio\\ui\\theme.py`
      against a baseline keyed on `swi3s_studio/ui/theme.py` — every file looked new. Keys
      are normalised to forward slashes here and in the baseline.
    * mypy follows imports into installed stubs, and an error there is an ENVIRONMENT
      problem, not our debt: on the VM (Python 3.14) numpy's stubs use a 3.12+ `type`
      statement, which was fatal under an older `python_version` and aborted the whole
      check after ONE error — the gate then read "total 205 -> 1" and called it a
      regression. Errors outside the target package are separated out and reported as an
      environment failure instead of being counted.
    """
    proc = subprocess.run([*_mypy_cmd(), "--no-incremental", _TARGET],
                          cwd=_ROOT, capture_output=True, text=True)
    if "No module named" in proc.stderr or "usage:" in proc.stderr:
        print("mypy is not installed. Install it with one of:\n"
              "  brew install mypy\n"
              "  python3 -m pip install mypy", file=sys.stderr)
        raise SystemExit(2)
    counts: dict[str, int] = {}
    foreign: dict[str, int] = {}
    for line in proc.stdout.splitlines():
        m = _LINE.match(line)
        if not m:
            continue
        path = m.group("file").replace("\\", "/")
        bucket = counts if path.startswith(_TARGET + "/") else foreign
        bucket[path] = bucket.get(path, 0) + 1
    if foreign:
        print("mypy reported errors OUTSIDE " + _TARGET + " — this is an environment "
              "problem, not type debt:\n", file=sys.stderr)
        for path, n in sorted(foreign.items()):
            print(f"  {path}: {n}", file=sys.stderr)
        print("\nA fatal error in an installed stub aborts the whole check, so the counts "
              "below would be meaningless.\nCheck that [tool.mypy] python_version is not "
              "OLDER than the syntax your installed stubs use.", file=sys.stderr)
        raise SystemExit(2)
    return counts, proc.stdout


def main() -> int:
    counts, raw = _run_mypy()
    total = sum(counts.values())
    env = _env_fingerprint()

    if "--update" in sys.argv:
        with open(_BASELINE, "w", encoding="utf-8") as f:
            json.dump({"env": env, "total": total,
                       "per_file": dict(sorted(counts.items()))}, f, indent=2)
            f.write("\n")
        print(f"baseline updated: {total} errors in {len(counts)} files  (env: {env})")
        return 0

    with open(_BASELINE, encoding="utf-8") as f:
        base = json.load(f)
    per_file: dict[str, int] = base["per_file"]

    # NOT COMPARABLE is a distinct outcome from PASS and FAIL. Exit 3 so tools/gate.py can
    # list it as "not run here" rather than either failing the build on an environment
    # difference or — worse — quietly implying the ratchet applied when it did not.
    #
    # `--allow-incomparable` maps that to exit 0 for callers that cannot interpret 3. CI is
    # one: `ci.yml` invokes this directly, not through tools/gate.py, so exit 3 failed the
    # lint job on EVERY run — the ratchet can never be comparable on a hosted runner whose
    # numpy is not the baseline's. That shipped in 3.0.12 and turned the type check into a
    # permanent red cross, which is precisely the "a check that cries wolf gets ignored"
    # failure this file's own comments warn about.
    #
    # The flag deliberately does NOT relax 1 (regression) or 2 (mypy could not run). Those
    # still fail, so the only thing tolerated is the one outcome that is not about the code.
    base_env = base.get("env")
    if base_env and base_env != env:
        print("mypy ratchet NOT COMPARABLE in this environment.")
        print(f"  baseline recorded with: {base_env}")
        print(f"  this environment has  : {env}")
        print(f"  (this run: {total} errors in {len(counts)} files)")
        print("\nmypy's findings depend on the stubs it reads, so a different numpy/mypy\n"
              "version changes the count for identical source. Run the ratchet where the\n"
              "baseline was taken (or re-baseline there); it is not a regression here.")
        if "--allow-incomparable" in sys.argv:
            print("\n--allow-incomparable: reporting this as success. The tree was type-checked;\n"
                  "only the COMPARISON was skipped. A regression here would still fail.")
            return 0
        return 3

    # STALE BASELINE ENTRIES ARE A FAILURE, not a curiosity. Found by the 3.0.12 release
    # review: if a file is deleted, its entry lingers (pruning only happens on --update,
    # which nothing forces). Should a path later reappear with brand-new errors that merely
    # number FEWER than the stale count, the ratchet reported "improved" and exited 0 — four
    # never-reviewed errors sailing through as no regression. Requiring the baseline to
    # describe files that actually exist closes that: the entry cannot outlive the file.
    stale = [p for p in per_file if not os.path.isfile(os.path.join(_ROOT, p))]
    if stale:
        print("mypy baseline is STALE — it lists files that no longer exist:\n")
        for p in stale:
            print(f"  {p}  (baseline {per_file[p]})")
        print("\nA lingering entry lets a recreated file inherit a count nobody reviewed.")
        print("Re-baseline deliberately:  python3 tools/mypy_gate.py --update")
        return 1

    regressions = [(p, n, per_file.get(p, 0)) for p, n in sorted(counts.items())
                   if n > per_file.get(p, 0)]
    improvements = [(p, n, per_file[p]) for p, n in sorted(counts.items())
                    if p in per_file and n < per_file[p]]
    fixed = sorted(p for p in per_file if p not in counts)

    if regressions:
        print("mypy REGRESSED — new type errors:\n")
        for path, now, was in regressions:
            print(f"  {path}: {was} -> {now}   (+{now - was})")
        print(f"\ntotal {base['total']} -> {total}")
        print("\nFix them, or if the new errors are unavoidable and reviewed, re-baseline")
        print("deliberately with:  python3 tools/mypy_gate.py --update")
        print("\nFull mypy output:\n")
        print(raw)
        return 1

    print(f"mypy: {total} errors in {len(counts)} files "
          f"(baseline {base['total']} in {len(per_file)}) — no regression")
    if improvements or fixed:
        for path, now, was in improvements:
            print(f"  improved: {path}: {was} -> {now}")
        for path in fixed:
            print(f"  now clean: {path}")
        print("\nRe-baseline to lock the improvement in:  python3 tools/mypy_gate.py --update")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
