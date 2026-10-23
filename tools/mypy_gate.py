#!/usr/bin/env python3
"""Type-check gate: mypy must not get WORSE, per file.

The codebase carries pre-existing mypy debt, so this gates on NON-REGRESSION against a
checked-in per-file baseline:

  * a file with MORE errors than its baseline fails
  * a file with errors that is absent from the baseline fails (new debt in clean code)
  * a file with FEWER errors passes, and the script tells you to re-baseline

Per-file, not a single total, because a total lets a fix in one file mask a regression in
another. Re-baseline deliberately with `--update` when you improve things.

The same count on every machine. mypy runs with `--no-site-packages`, so it reads no
installed third-party package or stub (numpy, PySide6 and the rest resolve to Any under
`ignore_missing_imports`). Its result then depends only on the source and on mypy's own
version, which is the environment fingerprint the baseline records; ci.yml installs that
exact mypy version so the CI run compares.

Every supported platform, not just this one. mypy evaluates `sys.platform` checks for one
platform, the host's by default, so code that only runs elsewhere is not type-checked by a
single run. The tree is checked once per platform in `_PLATFORMS`, and each platform's
per-file counts are compared with that platform's counts in the baseline (`per_platform`),
so a regression on any one of them fails and names it, whatever the other platforms count.
`per_file` holds each file's worst count over the platforms and `total` their sum; a
baseline without `per_platform` compares every platform with `per_file`.

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


def _baseline_mypy() -> str:
    """The mypy version the baseline was recorded with, or "?" when it cannot be read."""
    try:
        with open(_BASELINE, encoding="utf-8") as f:
            return str(json.load(f).get("env", {}).get("mypy", "?"))
    except (OSError, ValueError, AttributeError):
        return "?"
_TARGET = "swi3s_studio"
_LINE = re.compile(r"^(?P<file>[^:]+):\d+:(?:\d+:)? error:")
_PLATFORMS = ("darwin", "linux", "win32")
_BY_PLATFORM: dict[str, dict[str, int]] = {}    # the last run's counts, per platform


def _mypy_cmd() -> list[str]:
    """mypy may be a standalone binary (Homebrew, pipx) or a module in this interpreter.
    Prefer the binary — a Homebrew install is not importable by an unrelated python3."""
    exe = shutil.which("mypy")
    return [exe] if exe else [sys.executable, "-m", "mypy"]


def _env_fingerprint() -> dict[str, str]:
    """What mypy's output depends on besides the source: mypy's version, and what it reads
    from site-packages (nothing, by `--no-site-packages`). The baseline records it, and the
    gate refuses to call a count from a different environment a regression."""
    mypy_ver = subprocess.run([*_mypy_cmd(), "--version"], capture_output=True, text=True)
    return {"mypy": mypy_ver.stdout.split()[1] if mypy_ver.stdout.split() else "?",
            "site_packages": "not read"}


def _run_mypy() -> tuple[dict[str, int], str]:
    """Each file's worst error count over `_PLATFORMS`, plus every run's output, labelled."""
    _BY_PLATFORM.clear()
    worst: dict[str, int] = {}
    raws = []
    for plat in _PLATFORMS:
        counts, raw = _run_mypy_on(plat)
        _BY_PLATFORM[plat] = counts
        for path, n in counts.items():
            worst[path] = max(worst.get(path, 0), n)
        raws.append(f"--- mypy --platform {plat} ---\n{raw}")
    return worst, "\n".join(raws)


def _run_mypy_on(platform: str) -> tuple[dict[str, int], str]:
    """Per-file error counts for `platform`, plus mypy's raw output for reporting.

    Runs with `--no-incremental`: a warm cache can report counts that differ from a clean
    run over the same tree. Runs with `--no-site-packages`: see the module docstring.

    Two cross-platform rules:

    * mypy reports NATIVE path separators (`swi3s_studio\\ui\\theme.py` on Windows). Keys
      are normalised to forward slashes here and in the baseline.
    * mypy follows imports into installed stubs, and an error there is an ENVIRONMENT
      problem, not this project's debt (a fatal stub error aborts the whole check). Errors
      outside the target package are separated out and reported as an environment failure
      instead of being counted.
    """
    # Plain output, whatever the terminal: with FORCE_COLOR set mypy colours even a pipe,
    # and no error line would match _LINE.
    env = {k: v for k, v in os.environ.items()
           if k not in ("FORCE_COLOR", "MYPY_FORCE_COLOR", "CLICOLOR_FORCE")}
    proc = subprocess.run([*_mypy_cmd(), "--no-incremental", "--no-color-output",
                           "--no-site-packages", "--platform", platform, _TARGET],
                          cwd=_ROOT, capture_output=True, text=True, env=env)
    if "No module named" in proc.stderr or "usage:" in proc.stderr:
        print(f"mypy is not installed. Install the baseline's version ({_baseline_mypy()}), "
              "which requirements-dev.txt pins:\n"
              "  python3 -m pip install -r requirements-dev.txt\n"
              "Any other version makes the ratchet NOT COMPARABLE.", file=sys.stderr)
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
    if proc.returncode == 2 or "prevented further checking" in proc.stdout:
        # A blocking error (a syntax error, an unreadable module): mypy stopped before
        # checking the rest, so the lines it printed are not a count of the tree.
        head = "\n".join(proc.stdout.splitlines()[-5:] or proc.stderr.splitlines()[-5:])
        print(f"mypy exited {proc.returncode} and stopped before checking the whole tree "
              f"(--platform {platform}), so its counts cover only what it reached. Last "
              f"lines:\n{head}", file=sys.stderr)
        raise SystemExit(2)
    if proc.returncode != 0 and not counts:
        # mypy says it found errors (exit 1) or broke (2), and not one line was read: the
        # output is in a shape this does not parse, so a count of 0 would be a pass about
        # nothing. Refuse rather than report a clean tree.
        head = "\n".join((proc.stdout or proc.stderr).splitlines()[:5])
        print(f"mypy exited {proc.returncode} but no error line could be read from its "
              f"output, so there is no count to compare. First lines:\n{head}",
              file=sys.stderr)
        raise SystemExit(2)
    return counts, proc.stdout


def main() -> int:
    counts, raw = _run_mypy()
    total = sum(counts.values())
    env = _env_fingerprint()

    if "--update" in sys.argv:
        record = {"env": env, "total": total, "per_file": dict(sorted(counts.items()))}
        if _BY_PLATFORM:
            record["per_platform"] = {plat: dict(sorted(c.items()))
                                      for plat, c in sorted(_BY_PLATFORM.items())}
        with open(_BASELINE, "w", encoding="utf-8") as f:
            json.dump(record, f, indent=2)
            f.write("\n")
        print(f"baseline updated: {total} errors in {len(counts)} files  (env: {env})")
        return 0

    with open(_BASELINE, encoding="utf-8") as f:
        base = json.load(f)
    per_file: dict[str, int] = base["per_file"]
    per_platform: dict[str, dict[str, int]] = base.get("per_platform") or {}

    # NOT COMPARABLE is a distinct outcome from PASS and FAIL. Exit 3 so tools/gate.py can
    # list it as "not run here" rather than either failing the build on an environment
    # difference or — worse — quietly implying the ratchet applied when it did not.
    #
    # `--allow-incomparable` maps that to exit 0 for a caller that cannot interpret 3. No
    # comparison runs on that path, so a regression there is not caught. ci.yml does not pass
    # it: CI installs the baseline's mypy version, compares, and fails on exit 3.
    #
    # The flag does NOT relax 1 (regression) or 2 (mypy could not run) where a comparison
    # does run.
    base_env = base.get("env")
    if base_env and base_env != env:
        print("mypy ratchet NOT COMPARABLE in this environment.")
        print(f"  baseline recorded with: {base_env}")
        print(f"  this environment has  : {env}")
        print(f"  (this run: {total} errors in {len(counts)} files)")
        print("\nA different mypy version can change the count for identical source. Install\n"
              f"mypy {base_env.get('mypy', '?')} (or re-baseline deliberately with --update);\n"
              "this result is neither a pass nor a regression.")
        if "--allow-incomparable" in sys.argv:
            print("\n--allow-incomparable: exiting 0. mypy ran, but no count was compared,\n"
                  "so this run does NOT catch a regression.")
            return 0
        return 3

    # STALE BASELINE ENTRIES ARE A FAILURE. A deleted file's entry lingers until --update,
    # and a recreated file would inherit its count. The baseline must describe files that
    # exist.
    listed = set(per_file).union(*per_platform.values())
    stale = sorted(p for p in listed if not os.path.isfile(os.path.join(_ROOT, p)))
    if stale:
        print("mypy baseline is STALE — it lists files that no longer exist:\n")
        for p in stale:
            print(f"  {p}  (baseline {per_file.get(p, 0)})")
        print("\nA lingering entry lets a recreated file inherit a count nobody reviewed.")
        print("Re-baseline deliberately:  python3 tools/mypy_gate.py --update")
        return 1

    # Each platform against its own baseline counts. A run with no per-platform result
    # compares its combined counts with per_file.
    runs: dict[str, dict[str, int]] = dict(_BY_PLATFORM) or {"": counts}
    regressions, improvements, fixed = [], [], []
    for plat, plat_counts in runs.items():
        was_counts = per_platform.get(plat, per_file)
        where = f"   on {plat}" if plat else ""
        for p, n in sorted(plat_counts.items()):
            if n > was_counts.get(p, 0):
                regressions.append((p, n, was_counts.get(p, 0), where))
            elif p in was_counts and n < was_counts[p]:
                improvements.append((p, n, was_counts[p], where))
        fixed += [(p, where) for p in sorted(was_counts) if was_counts[p]
                  and p not in plat_counts]

    if regressions:
        print("mypy REGRESSED — new type errors:\n")
        for path, now, was, where in sorted(regressions):
            print(f"  {path}: {was} -> {now}   (+{now - was}){where}")
        print(f"\ntotal {base['total']} -> {total}")
        print("\nFix them, or if the new errors are unavoidable and reviewed, re-baseline")
        print("deliberately with:  python3 tools/mypy_gate.py --update")
        print("\nFull mypy output:\n")
        print(raw)
        return 1

    plats = ", ".join(f"{p} {sum(c.values())}" for p, c in _BY_PLATFORM.items())
    print(f"mypy: {total} errors in {len(counts)} files "
          f"(baseline {base['total']} in {len(per_file)}) — no regression"
          + (f"  [{plats}]" if plats else ""))
    if improvements or fixed:
        for path, now, was, where in sorted(improvements):
            print(f"  improved: {path}: {was} -> {now}{where}")
        for path, where in sorted(fixed):
            print(f"  now clean: {path}{where}")
        print("\nRe-baseline to lock the improvement in:  python3 tools/mypy_gate.py --update")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
