"""The release gate is ONE definition, and the docs must describe reality.

The gate is `tools/gate.py`. These tests fail the build if:

  * `ci.yml` declares a check that `tools/gate.py` does not run, or
  * the docs repeat any of the stale "CI runs nowhere" claims, or stop saying where CI
    actually does run.

THIS FILE HAS TWO HALVES. The drift guards read SOURCE TEXT, the right tool for "does ci.yml
still name this check" and "do the docs still say where CI runs" — claims about documents.
The behavioural half EXECUTES tools/gate.py and tools/mypy_gate.py with their slow parts
stubbed: the exit code is asserted from a seeded outcome, not read off the source.
"""
import importlib.util
import json
import os
import pathlib
import re
import sys

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_CI = os.path.join(_ROOT, ".github", "workflows", "ci.yml")
_GATE = os.path.join(_ROOT, "tools", "gate.py")          # the implementation
_GATE_SH = os.path.join(_ROOT, "tests", "gate.sh")       # bash wrapper
_GATE_PS1 = os.path.join(_ROOT, "tests", "gate.ps1")     # PowerShell wrapper
# A version reference in a release-notes file, EXCLUDING a spec citation: "Section 14.1.10"
# parses as 14.1.10 and compares greater than every 3.0.x release. Narrowed rather than
# allowlisted, as tools/leak_scan.py does for a §10.x spec citation against its private-IP
# pattern. Do not name an address here: the scanner would flag this file.
_VERSION_NOT_A_CITATION = (
    # not introduced by a citation word or §
    r"(?<!§)(?<!Section )(?<!section )(?<!Table )(?<!table )(?<!Figure )(?<!figure )"
    # not starting PART-WAY through a longer dotted number: the tail "1.6.2" of
    # "section 9.1.6.2" is out of reach of any lookbehind for "section ".
    r"(?<![\w.])"
    r"v?(\d+)\.(\d+)\.(\d+)"          # `v?` — "v3.1.0" is a version reference too
    # reject a FOURTH part, but allow a sentence-final period: only a dot FOLLOWED BY A DIGIT
    # means "this is a longer citation" (as in tools/leak_scan.py's _IPV4_TAIL).
    r"(?!\.?\d)"
)

_DOCS = ["README.md", os.path.join("docs", "DEVELOPMENT.md"), os.path.join("docs", "TESTING.md")]


def _read(path: str) -> str:
    with open(os.path.join(_ROOT, path) if not os.path.isabs(path) else path,
              encoding="utf-8") as f:
        return f.read()


def _yaml_code(path: str) -> str:
    """ci.yml with comment lines stripped. The guards below look for constructs the file's
    comments may also DESCRIBE, so the raw text would report a false failure."""
    return "\n".join(ln for ln in _read(path).splitlines()
                     if not ln.lstrip().startswith("#"))


# Commands in ci.yml that are SETUP, not checks — they install or prepare, they do not
# assert anything, so the gate has no counterpart for them.
_CI_SETUP = ("apt-get", "pip install --upgrade pip", "python -m pip install -r",
             "pip install -r requirements.txt", "pip install -r requirements-dev.txt")

# A CI check the gate deliberately does NOT run, each with the reason. Adding to this list is
# a conscious decision; the point is that it cannot happen by omission.
_NOT_IN_GATE = {
    # Coverage GATES (--cov-fail-under) in CI only. The local gate skips the second,
    # instrumented pass over the whole suite (~2.5 min): the documented "neither gate
    # subsumes the other" split — see docs/DEVELOPMENT.md.
    "--cov=swi3s_studio": "coverage gates in CI only; the local gate skips the instrumented "
                          "second pass (see DEVELOPMENT.md, 'Neither gate subsumes the other')",
}

# CI check -> the fragment that proves tools/gate.py runs the same thing.
_CI_TO_GATE = {
    'pytest -q -m "not perf"': 'check_pytest("not perf"',
    "pytest -q -m perf": 'check_pytest("perf"',
    "ruff check .": '"ruff", ["check", "."]',
    "tools/mypy_gate.py": "mypy_gate.py",
    "pip install ./native": "build_local.sh",
}


def _ci_run_commands(ci: str) -> list[str]:
    """Every command ci.yml actually executes, from `run:` steps (inline and block form).

    Shell backslash continuations are joined, or the tail of a multi-line command arrives as
    its own entry and looks like an unrecognised check."""
    cmds, in_block, indent = [], False, 0
    for raw in ci.splitlines():
        line = raw.rstrip()
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        stripped = line.strip()
        if in_block:
            cur = len(line) - len(line.lstrip())
            if cur > indent:
                if cmds and cmds[-1].endswith("\\"):
                    cmds[-1] = cmds[-1][:-1].rstrip() + " " + stripped
                else:
                    cmds.append(stripped)
                continue
            in_block = False
        if stripped.startswith("run:"):
            rest = stripped[len("run:"):].strip()
            if rest in ("|", ">", "|-", ">-"):
                in_block, indent = True, len(line) - len(line.lstrip())
            elif rest:
                cmds.append(rest)
    return cmds


# --- the gate script is the single definition --------------------------------------------

def test_gate_exists_and_reports_every_failure():
    assert os.path.isfile(_GATE), "tools/gate.py is the release gate; it must exist"
    text = _read(_GATE)
    # It must aggregate failures, not stop at the first one: "pytest failed" alone hides
    # whether ruff and mypy would also have failed, which is half the value of a gate.
    assert "_FAILED" in text and "return 1" in text, \
        "gate.py must collect every failure and exit non-zero"


def test_gate_is_cross_platform():
    """The gate runs on Windows too, where there is no bash on PATH and
    native/build_local.sh is a Unix compile path: pin the platform handling."""
    text = _read(_GATE)
    assert 'os.name == "nt"' in text, "gate.py must branch for Windows"
    assert "build_local.sh" in text and "pip" in text, \
        "it must build the native core the Unix way AND the Windows (pip/MSVC) way"
    assert "sys.executable" in text, \
        "never assume a `python3` name — Windows has no reliable python3"
    assert "shutil.which" in text, \
        "linters may be venv-installed and not on PATH; fall back to `-m`"
    for path, name in ((_GATE_SH, "gate.sh"), (_GATE_PS1, "gate.ps1")):
        assert os.path.isfile(path), f"tests/{name} entry point is missing"


def test_wrappers_delegate_and_hold_no_logic():
    """Two shells, one implementation: a wrapper holds no checks of its own."""
    run_all = os.path.join(_ROOT, "tests", "run_all.sh")
    for path, name in ((_GATE_SH, "gate.sh"), (_GATE_PS1, "gate.ps1"), (run_all, "run_all.sh")):
        text = _read(path)
        assert "tools/gate.py" in text, f"tests/{name} must delegate to tools/gate.py"
        for leaked in ("pytest", "ruff check", "mypy_gate"):
            assert leaked not in text, (
                f"tests/{name} runs {leaked!r} itself — put it in tools/gate.py so both "
                "platforms get it")


def test_gate_runs_every_check_ci_declares():
    """If ci.yml grows a check, the gate must grow it too.

    DEFAULT-DENY: every `run:` command in ci.yml must be accounted for: setup, mapped to a
    gate check, or consciously listed in _NOT_IN_GATE with a reason. Anything else fails."""
    ci = _yaml_code(_CI)
    gate = _read(_GATE)
    unaccounted = []
    for cmd in _ci_run_commands(ci):
        if any(s in cmd for s in _CI_SETUP):
            continue
        if any(s in cmd for s in _NOT_IN_GATE):
            continue
        mapped = [g for c, g in _CI_TO_GATE.items() if c in cmd]
        if not mapped:
            unaccounted.append(cmd)
            continue
        for gate_frag in mapped:
            assert gate_frag in gate, (
                f"ci.yml runs {cmd!r} but tools/gate.py has no {gate_frag!r} — the gate and "
                "CI have drifted, which is exactly how lint/type went unrun for five releases")
    assert not unaccounted, (
        "ci.yml declares command(s) this guard does not recognise:\n  "
        + "\n  ".join(unaccounted)
        + "\n\nEither add the check to tools/gate.py and map it in _CI_TO_GATE, or record it "
          "in _NOT_IN_GATE with the reason the gate skips it. Do not leave it unaccounted: an "
          "unrecognised CI check is exactly the blind spot this test exists to remove.")


def test_lint_job_is_blocking():
    """The lint job is blocking: no `continue-on-error`."""
    ci = _yaml_code(_CI)
    lint = ci[ci.index("  lint:"):]
    assert "continue-on-error" not in lint.split("steps:")[0], \
        "the lint job must be blocking (no continue-on-error)"


def test_mypy_result_is_not_discarded():
    """No CI step swallows its own failure with `|| true`, and mypy runs through the
    non-regression gate."""
    ci = _yaml_code(_CI)
    assert "|| true" not in ci, "no CI step may swallow its own failure with `|| true`"
    assert "tools/mypy_gate.py" in ci, "mypy must run through the non-regression gate"


def test_mypy_baseline_is_present_and_per_file():
    """A single total lets a fix in one file mask a regression in another."""
    import json
    with open(os.path.join(_ROOT, "tools", "mypy_baseline.json"), encoding="utf-8") as f:
        base = json.load(f)
    assert "per_file" in base and isinstance(base["per_file"], dict) and base["per_file"], \
        "the baseline must be per-file, not just a total"
    assert base["total"] == sum(base["per_file"].values()), "baseline total is inconsistent"


def test_release_checklist_points_at_the_gate_script():
    """The checklist must delegate to the executable definition rather than re-listing the
    checks in prose."""
    dev = _read(os.path.join("docs", "DEVELOPMENT.md"))
    assert "tools/gate.py" in dev, \
        "docs/DEVELOPMENT.md must name tools/gate.py as the gate"
    # Separator-agnostic: the doc writes the Windows entry point as `.\tests\gate.ps1`.
    assert "gate.ps1" in dev, \
        "the checklist must give the Windows entry point too — that half was once impossible"


# --- the docs must not describe a CI that no longer matches reality ----------------------

# Stale claims that CI runs nowhere. A literal list, not a regex: prose is a bad thing to
# pattern-match. Extend the list if a new formulation appears.
_STALE_CI_CLAIMS = [
    "does not currently run anywhere",
    "does not currently execute anywhere",
    "does not currently execute on any host",
    "executes on no host",
    "runs on no host",
    "no ci runs today",
    "no ci actually runs",
    "never runs anywhere",
]


def test_docs_do_not_repeat_the_stale_ci_claims():
    """None of the docs repeats a claim from _STALE_CI_CLAIMS."""
    for doc in _DOCS:
        low = _read(doc).lower()
        for claim in _STALE_CI_CLAIMS:
            assert claim not in low, (
                f"{doc} says {claim!r}. CI runs on the public repo (Actions is enabled "
                "there) — say WHERE it runs and what it does not cover instead.")


def test_docs_state_where_ci_actually_runs():
    """The positive half, which is what keeps the negative half honest: if someone reverts
    to 'CI runs nowhere' they have to delete this statement, and this test fails."""
    dev = _read(os.path.join("docs", "DEVELOPMENT.md"))
    assert "Actions is enabled" in dev, \
        "docs/DEVELOPMENT.md must state that Actions is enabled on the public repo"
    assert "Where it runs" in dev, \
        "the CI section must lead with where the workflow actually runs"


# --- leak scan: "leak" is broader than secrets -------------------------------------------
#
# A list of the strings you are trying not to publish is itself the leak. These guards keep
# the scanner honest and keep the concrete names out of the repository.

_LEAK_SCAN = os.path.join(_ROOT, "tools", "leak_scan.py")


def test_leak_scan_exists_and_is_in_the_gate():
    assert os.path.isfile(_LEAK_SCAN), "tools/leak_scan.py must exist"
    assert "leak_scan.py" in _read(_GATE), \
        "the leak scan must run in tools/gate.py, not depend on someone remembering it"


def test_leak_scan_carries_no_site_specific_names():
    """Structural patterns only (private IPs, home paths, key file names). Anything
    site-specific belongs in $SWI3S_LEAK_PATTERNS, outside the repo."""
    text = _read(_LEAK_SCAN)
    assert "SWI3S_LEAK_PATTERNS" in text, "no way to supply site-specific patterns"
    # A hostname-shaped literal in the scanner would be the same mistake it exists to prevent.
    # Generic internal suffixes only: naming an employer's domain here would publish the very
    # string this test keeps out of the scanner (the site patterns hold those).
    assert not re.search(r"[a-z0-9-]+\\?\.(?:internal|local|lan|intranet)\b", text, re.I), \
        "tools/leak_scan.py contains a site-specific host name; move it to the env-var file"


def test_leak_scan_is_loud_when_only_partly_configured():
    """The scanner says so when it covers less than it appears to."""
    text = _read(_LEAK_SCAN)
    assert "NOT covered by this run" in text, \
        "the scanner must say so when no site-pattern file is configured"


def test_published_docs_do_not_re_add_a_pattern_list():
    """docs/DEVELOPMENT.md must describe leak CATEGORIES, not enumerate the strings."""
    dev = _read(os.path.join("docs", "DEVELOPMENT.md"))
    assert "SWI3S_LEAK_PATTERNS" in dev, "the docs should point at the out-of-repo pattern file"
    assert "git grep -inE" not in dev, \
        "docs/DEVELOPMENT.md is publishing a leak-pattern grep again — use tools/leak_scan.py"


# --- the three version literals must agree ----------------------------------------------
#
# The release checklist says to bump `swi3s_studio.__version__`, `pyproject.toml::version`
# and `swviz/version.py::APP_VERSION` together. A stale APP_VERSION only surfaces when the
# package isn't importable (`app_version()` prefers the live value), so it is pinned here.

def test_the_three_version_literals_agree():
    import re as _re

    from swi3s_studio import __version__
    pyproject = _read("pyproject.toml")
    m = _re.search(r'^version\s*=\s*"([^"]+)"', pyproject, _re.M)
    assert m, "no version in pyproject.toml"
    assert m.group(1) == __version__, (
        f"pyproject.toml says {m.group(1)}, swi3s_studio.__version__ says {__version__}")

    swviz = _read(os.path.join("swi3s_studio", "swviz", "version.py"))
    m = _re.search(r"^APP_VERSION\s*=\s*'([^']+)'", swviz, _re.M)
    assert m, "no APP_VERSION in swviz/version.py"
    assert m.group(1) == __version__, (
        f"swviz APP_VERSION says {m.group(1)}, __version__ says {__version__} — this is the "
        "literal that silently sat at 3.0.8 for three releases")


def test_the_package_metadata_is_valid_and_ships_its_assets():
    """pyproject.toml is configuration setuptools accepts, it lists every package, and its
    package-data names every file the package reads from beside its modules (the theme's
    icons): a wheel holds only what [tool.setuptools] names. Checked with setuptools' own
    validator, so no build is needed."""
    import fnmatch
    import warnings

    pytest.importorskip("setuptools")
    from setuptools.config.pyprojecttoml import read_configuration
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")               # "[tool.setuptools] is still beta"
        conf = read_configuration(os.path.join(_ROOT, "pyproject.toml"))
    tool = conf["tool"]["setuptools"]
    pkg_root = pathlib.Path(_ROOT, "swi3s_studio")
    on_disk = {".".join(d.relative_to(_ROOT).parts) for d in [pkg_root, *pkg_root.rglob("*")]
               if (d / "__init__.py").is_file()}
    assert set(tool["packages"]) == on_disk
    data = tool.get("package-data", {})
    missing = []
    for f in pkg_root.rglob("*"):
        if not f.is_file() or f.suffix in (".py", ".pyc") or "__pycache__" in f.parts:
            continue
        owner = f.parent
        while not (owner / "__init__.py").is_file():
            owner = owner.parent
        rel = f.relative_to(owner).as_posix()
        if not any(fnmatch.fnmatch(rel, g) for g in data.get(".".join(
                owner.relative_to(_ROOT).parts), [])):
            missing.append(f.relative_to(_ROOT).as_posix())
    assert not missing, f"not in [tool.setuptools.package-data], so not in a wheel: {missing}"


def test_ci_runs_the_baselines_mypy_and_compares():
    """CI compares, or fails: the lint job installs requirements-dev.txt, which pins the mypy
    version the baseline records, and does not pass --allow-incomparable, so NOT COMPARABLE
    (exit 3) fails the lint job instead of passing it without a comparison."""
    ci = _yaml_code(_CI)
    lint = ci.split("  lint:", 1)[1]
    with open(os.path.join(_ROOT, "tools", "mypy_baseline.json"), encoding="utf-8") as f:
        recorded = json.load(f)["env"]["mypy"]
    install = [c for c in _ci_run_commands(lint) if "pip install -r requirements-dev.txt" in c]
    assert install, "the lint job does not install requirements-dev.txt"
    pins = [ln.split("#", 1)[0].strip() for ln in _read("requirements-dev.txt").splitlines()]
    assert f"mypy=={recorded}" in pins, (
        f"requirements-dev.txt must pin mypy=={recorded}, the version "
        f"tools/mypy_baseline.json records, or the ratchet cannot compare: {pins}")
    runs = [c for c in _ci_run_commands(lint) if "tools/mypy_gate.py" in c]
    assert runs and not any("--allow-incomparable" in c for c in runs), runs


def test_ci_and_the_setup_scripts_install_the_dev_requirements():
    """One list of what testing needs: requirements-dev.txt carries the runtime set and the
    tools, every CI job installs it rather than naming packages of its own, and the setup
    scripts install it."""
    lines = [ln.split("#", 1)[0].strip() for ln in _read("requirements-dev.txt").splitlines()]
    names = {re.split(r"[<>=!~ ]", ln, maxsplit=1)[0].lower() for ln in lines if ln}
    assert "-r requirements.txt" in lines
    assert {"pytest", "pytest-cov", "ruff", "mypy"} <= names, names
    installs = [c for c in _ci_run_commands(_yaml_code(_CI))
                if "pip install" in c and "--upgrade pip" not in c
                and c.split("#", 1)[0].split()[4:] != ["./native"]]      # the core alone
    assert len(installs) == 4, installs            # test, perf, coverage, lint
    assert installs and all("-r requirements-dev.txt" in c for c in installs), installs
    for script in ("tools/setup_dev.sh", "tools/setup_dev.ps1"):
        assert "pip install -r requirements-dev.txt" in _read(script), script


def test_allow_incomparable_maps_only_the_not_comparable_branch():
    """The flag's early return sits inside the NOT-COMPARABLE branch; the regression and the
    could-not-run exits stay reachable."""
    src = _read("tools/mypy_gate.py")
    head, _, _tail = src.partition('if "--allow-incomparable" in sys.argv')
    assert "NOT COMPARABLE" in head, "the flag is handled outside the not-comparable branch"
    for needed, why in (("return 1", "a regression must still fail"),
                        ("SystemExit(2)", "mypy failing to run must still fail")):
        assert needed in src, f"{needed} is gone from mypy_gate.py — {why}"


def test_the_ratchet_reads_no_site_packages(monkeypatch):
    """mypy reads no installed package, so its count depends on the source and mypy's own
    version only, and the fingerprint records exactly that."""
    import subprocess
    m = _load_tool("mypy_gate.py")
    seen = []

    def fake_run(cmd, **kw):
        seen.append(cmd)
        if "--version" in cmd:
            return subprocess.CompletedProcess(cmd, 0, "mypy 9.9.9 (compiled: yes)\n", "")
        return subprocess.CompletedProcess(cmd, 0, "Success: no issues found\n", "")
    monkeypatch.setattr(m.subprocess, "run", fake_run)
    m._run_mypy()
    assert all("--no-site-packages" in c for c in seen), seen
    assert m._env_fingerprint() == {"mypy": "9.9.9", "site_packages": "not read"}
    assert not any("numpy" in " ".join(c) for c in seen), seen


# --------------------------------------------------------------------------------------
# BEHAVIOURAL HALF — the tools are executed, not grepped.
#
# Both are loaded by path (tools/ is not a package) and their slow work is replaced: the
# gate's check_* functions become no-ops so main() only aggregates, and mypy_gate's
# _run_mypy returns synthetic counts against a synthetic baseline. What is left is exactly
# the logic a release depends on — "did anything fail, and does that reach the exit code".
# --------------------------------------------------------------------------------------

_GATE_CHECKS = ("check_native", "check_pytest", "check_per_suite",
                "check_tool", "check_mypy", "check_leaks", "check_screenshots",
                "check_native_cpp")


def _load_tool(filename: str):
    """Import tools/<filename> as a module object, by path."""
    path = os.path.join(_ROOT, "tools", filename)
    spec = importlib.util.spec_from_file_location(filename[:-3], path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _run_gate(failed=(), not_run=(), argv=("tools/gate.py",)):
    """Run gate.main() with every check stubbed out and the outcome seeded.

    Seeding the two lists the checks would have appended to isolates the aggregation from
    the checks themselves.
    """
    g = _load_tool("gate.py")
    for name in _GATE_CHECKS:
        assert hasattr(g, name), f"gate.py no longer defines {name} — update this test"
        setattr(g, name, lambda *a, **k: None)
    g._FAILED[:] = list(failed)
    g._NOT_RUN[:] = list(not_run)
    saved = sys.argv
    sys.argv = list(argv)
    try:
        return g.main()
    finally:
        sys.argv = saved


def test_the_gate_exits_zero_only_when_nothing_failed():
    assert _run_gate() == 0, "a gate with no failures must exit 0"


def test_the_gate_exits_nonzero_when_a_check_failed():
    """A failed check makes main() return nonzero: "Exit 0 is the gate" (docs/DEVELOPMENT.md,
    the release checklist)."""
    assert _run_gate(failed=["pytest not-perf"]) == 1, \
        "a failed check must make the gate exit nonzero"


def test_the_gate_reports_every_failure_so_one_cannot_hide_the_rest(capsys):
    """The aggregating design: checks all run and every failure is named."""
    rc = _run_gate(failed=["native build", "ruff", "leak scan"])
    out = capsys.readouterr().out
    assert rc == 1
    assert "FAIL (3)" in out, out
    for name in ("native build", "ruff", "leak scan"):
        assert name in out, f"{name} missing from the summary:\n{out}"


def test_a_not_run_check_is_named_and_does_not_by_itself_fail(capsys):
    """NOT RUN HERE is a third outcome: it must be printed (never silently dropped), and it
    is not a failure — the reader decides what unrun coverage means for a tag."""
    rc = _run_gate(not_run=["mypy ratchet (environment differs from the baseline)"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "NOT RUN HERE (1)" in out, out
    assert "mypy ratchet" in out


def test_an_unrun_check_never_masks_a_real_failure(capsys):
    """Both sections must appear together. A run that skipped a check AND failed another
    must not report only the skip."""
    rc = _run_gate(failed=["pytest perf"], not_run=["leak scan: site-specific patterns"])
    out = capsys.readouterr().out
    assert rc == 1
    assert "NOT RUN HERE (1)" in out and "FAIL (1)" in out, out


def test_quick_mode_still_cannot_pass_with_a_failure(capsys):
    """--quick drops perf and the per-suite pass, which is why it says so in the summary —
    but it must not soften the verdict on what it DID run."""
    rc = _run_gate(failed=["ruff"], argv=("tools/gate.py", "--quick"))
    assert rc == 1
    assert "FAIL (1)" in capsys.readouterr().out


# --- the mypy ratchet -------------------------------------------------------------------

_BASE_ENV = {"mypy": "1.11.0", "numpy": "2.1.0"}
# Real tracked paths: the ratchet fails a baseline naming a file that no longer exists, so a
# made-up path would fail every case for the wrong reason.
_F1 = os.path.join("swi3s_studio", "__init__.py")
_F2 = os.path.join("swi3s_studio", "session.py")


def _run_ratchet(counts, baseline, tmp_path, argv=("tools/mypy_gate.py",), base_env=None):
    """Run mypy_gate.main() against synthetic counts and a synthetic baseline file."""
    m = _load_tool("mypy_gate.py")
    m._run_mypy = lambda: (dict(counts), "<mypy output>")
    m._env_fingerprint = lambda: dict(_BASE_ENV)
    path = tmp_path / "baseline.json"
    path.write_text(json.dumps({
        "env": dict(_BASE_ENV if base_env is None else base_env),
        "total": sum(baseline.values()),
        "per_file": dict(baseline),
    }), encoding="utf-8")
    m._BASELINE = str(path)
    saved = sys.argv
    sys.argv = list(argv)
    try:
        return m.main()
    finally:
        sys.argv = saved


def test_the_ratchet_passes_an_unchanged_count(tmp_path):
    assert _run_ratchet({_F1: 3}, {_F1: 3}, tmp_path) == 0


def test_the_ratchet_fails_a_file_that_gained_an_error(tmp_path):
    """One more error in one file must fail."""
    assert _run_ratchet({_F1: 4}, {_F1: 3}, tmp_path) == 1


def test_the_ratchet_fails_new_debt_in_a_file_absent_from_the_baseline(tmp_path):
    """Per-file, not a total: a clean file acquiring errors is a regression even when some
    other file improved enough to keep the total flat."""
    assert _run_ratchet({_F1: 1, _F2: 2}, {_F1: 3}, tmp_path) == 1


def test_the_ratchet_passes_an_improvement(tmp_path):
    assert _run_ratchet({_F1: 1}, {_F1: 3}, tmp_path) == 0


def test_the_ratchet_reports_an_incomparable_environment_as_three(tmp_path):
    """Exit 3 is neither pass nor fail, so tools/gate.py can list it as NOT RUN HERE instead
    of failing a build over a stub version or implying the comparison happened."""
    assert _run_ratchet({_F1: 3}, {_F1: 3}, tmp_path,
                        base_env={"mypy": "9.9.9", "numpy": "0.0.1"}) == 3


def test_allow_incomparable_maps_only_the_environment_case_to_success(tmp_path, capsys):
    """Exit 0, and the output says no comparison ran: a regression is NOT caught there."""
    assert _run_ratchet({_F1: 30}, {_F1: 3}, tmp_path,
                        argv=("tools/mypy_gate.py", "--allow-incomparable"),
                        base_env={"mypy": "9.9.9", "numpy": "0.0.1"}) == 0
    out = capsys.readouterr().out
    assert "does NOT catch a regression" in out
    assert "would still fail" not in out


def test_allow_incomparable_does_not_swallow_a_regression(tmp_path):
    """The escape hatch CI needs never maps a regression to success."""
    assert _run_ratchet({_F1: 4}, {_F1: 3}, tmp_path,
                        argv=("tools/mypy_gate.py", "--allow-incomparable")) == 1


def _run_ratchet_per_platform(by_platform, baseline, tmp_path, capsys, per_platform=None):
    """As _run_ratchet, but with one mypy result per platform, so the combining is real.
    `per_platform`, when given, is the baseline's per-platform counts."""
    m = _load_tool("mypy_gate.py")
    m._run_mypy_on = lambda plat: (dict(by_platform[plat]), f"<mypy {plat}>")
    m._env_fingerprint = lambda: dict(_BASE_ENV)
    path = tmp_path / "baseline.json"
    record = {"env": dict(_BASE_ENV), "total": sum(baseline.values()),
              "per_file": dict(baseline)}
    if per_platform is not None:
        record["per_platform"] = per_platform
    path.write_text(json.dumps(record), encoding="utf-8")
    m._BASELINE = str(path)
    saved = sys.argv
    sys.argv = ["tools/mypy_gate.py"]
    try:
        return m.main(), capsys.readouterr().out
    finally:
        sys.argv = saved


def test_the_ratchet_checks_every_platform_and_names_the_one_that_regressed(tmp_path, capsys):
    """mypy checks one platform per run, and only the host's unless told otherwise. Every
    platform is run, and a regression on any one of them fails and says which."""
    code, out = _run_ratchet_per_platform(
        {"darwin": {_F1: 3}, "linux": {_F1: 3}, "win32": {_F1: 5}}, {_F1: 3}, tmp_path, capsys)
    assert code == 1
    assert "on win32" in out and "on darwin" not in out and "on linux" not in out


def test_the_ratchet_passes_when_every_platform_holds(tmp_path, capsys):
    by_platform = {"darwin": {_F1: 3}, "linux": {_F1: 2}, "win32": {_F1: 3}}
    code, out = _run_ratchet_per_platform(by_platform, {_F1: 3}, tmp_path, capsys,
                                          per_platform=by_platform)
    assert code == 0 and "darwin 3, linux 2, win32 3" in out


def test_a_regression_on_a_platform_below_the_worst_count_fails(tmp_path, capsys):
    """Each platform is compared with its own baseline count: linux going 2 -> 3 fails even
    though darwin and win32 already count 3."""
    code, out = _run_ratchet_per_platform(
        {"darwin": {_F1: 3}, "linux": {_F1: 3}, "win32": {_F1: 3}}, {_F1: 3}, tmp_path, capsys,
        per_platform={"darwin": {_F1: 3}, "linux": {_F1: 2}, "win32": {_F1: 3}})
    assert code == 1
    assert "2 -> 3" in out and "on linux" in out and "on darwin" not in out, out


def test_a_baseline_without_per_platform_counts_compares_each_platform_with_per_file(
        tmp_path, capsys):
    code, _ = _run_ratchet_per_platform(
        {"darwin": {_F1: 3}, "linux": {_F1: 2}, "win32": {_F1: 3}}, {_F1: 3}, tmp_path, capsys)
    assert code == 0
    code, out = _run_ratchet_per_platform(
        {"darwin": {_F1: 3}, "linux": {_F1: 4}, "win32": {_F1: 3}}, {_F1: 3}, tmp_path, capsys)
    assert code == 1 and "on linux" in out


def test_update_records_each_platforms_counts(tmp_path, capsys):
    m = _load_tool("mypy_gate.py")
    by_platform = {"darwin": {_F1: 3}, "linux": {_F1: 2}, "win32": {_F1: 3, _F2: 1}}
    m._run_mypy_on = lambda plat: (dict(by_platform[plat]), "")
    m._env_fingerprint = lambda: dict(_BASE_ENV)
    m._BASELINE = str(tmp_path / "baseline.json")
    saved = sys.argv
    sys.argv = ["tools/mypy_gate.py", "--update"]
    try:
        assert m.main() == 0
    finally:
        sys.argv = saved
    base = json.loads((tmp_path / "baseline.json").read_text(encoding="utf-8"))
    assert base["per_platform"] == by_platform
    assert base["per_file"] == {_F1: 3, _F2: 1} and base["total"] == 4


def test_the_checked_in_baseline_has_counts_for_every_platform():
    """per_file is each file's worst count over the platforms the ratchet runs."""
    m = _load_tool("mypy_gate.py")
    with open(os.path.join(_ROOT, "tools", "mypy_baseline.json"), encoding="utf-8") as f:
        base = json.load(f)
    per_platform = base.get("per_platform", {})
    assert set(per_platform) == set(m._PLATFORMS), sorted(per_platform)
    worst: dict = {}
    for counts in per_platform.values():
        for path, n in counts.items():
            worst[path] = max(worst.get(path, 0), n)
    assert worst == base["per_file"]


def test_the_ratchet_runs_mypy_for_each_supported_platform():
    """The list is what makes the check cover Linux and Windows from any one machine."""
    m = _load_tool("mypy_gate.py")
    assert set(m._PLATFORMS) == {"darwin", "linux", "win32"}


def test_the_ratchet_fails_a_baseline_naming_a_deleted_file(tmp_path):
    """A baseline naming a deleted file fails: a stale entry would let a recreated file
    inherit a count nobody reviewed."""
    gone = os.path.join("swi3s_studio", "no_such_module_here.py")
    assert _run_ratchet({_F1: 3}, {_F1: 3, gone: 4}, tmp_path) == 1


# ---------------------------------------------------------------- release notes are ONE file
# One reviewed file per release, in the tree and therefore in the tag, referenced by every
# consumer instead of restated. These guard the two ways that can rot.

def _notes_path(version: str) -> str:
    return os.path.join("docs", "releases", f"v{version}.md")


def test_the_current_version_has_a_release_notes_file():
    """The current version's notes file exists and its title names the version; it is
    written DURING the cycle, not at push time."""
    from swi3s_studio import __version__
    rel = _notes_path(__version__)
    full = os.path.join(_ROOT, rel)
    assert os.path.isfile(full), (
        f"{rel} is missing. Every description of this release — the release body, the "
        f"publish branch's commit message, the pull request — reads from it, so it is written "
        f"as part of the cycle. See docs/DEVELOPMENT.md, 'One release, one description'.")
    text = pathlib.Path(full).read_text(encoding="utf-8")
    assert text.splitlines()[0].startswith("#"), f"{rel} must open with a markdown title"
    assert __version__ in text.splitlines()[0], (
        f"{rel} title line does not name {__version__}: {text.splitlines()[0]!r}")


def test_the_release_notes_claim_no_later_version():
    """A description may look back but never forward: naming a LATER version means it is
    describing work this tag does not contain.

    Scoped PER SECTION: only the '## Not in this release' section itself is exempt, not every
    section after it.

    A SPEC CITATION IS NOT A VERSION — see _VERSION_NOT_A_CITATION, and the test below for the
    narrowing checked in both directions.
    """
    import re as _re

    from swi3s_studio import __version__
    rel = _notes_path(__version__)
    text = pathlib.Path(os.path.join(_ROOT, rel)).read_text(encoding="utf-8")
    mine = tuple(int(p) for p in __version__.split("."))

    # (heading, body) for each '## ' section; the preamble before the first one is its own.
    sections, head, buf = [], "(preamble)", []
    for line in text.splitlines():
        if line.startswith("## "):
            sections.append((head, "\n".join(buf)))
            head, buf = line[3:].strip(), []
        else:
            buf.append(line)
    sections.append((head, "\n".join(buf)))

    offenders = []
    for heading, body in sections:
        if heading.lower() == "not in this release":
            continue        # the honest way to record a scope correction — see below
        for m in _re.finditer(_VERSION_NOT_A_CITATION, body):
            if tuple(int(g) for g in m.groups()) > mine:
                offenders.append((heading, m.group(0)))
    assert not offenders, (
        f"{rel} names a version later than {__version__}: {offenders} — the notes describe "
        f"work this tag does not contain. If the point is that something is NOT in this "
        f"release, say so under a '## Not in this release' heading, which is exempt.")


def test_the_citation_narrowing_works_in_both_directions():
    """The narrowing rejects spec citations and still catches real forward references, on the
    shapes that occur in these notes."""
    import re as _re

    def hits(text):
        return [m.group(0) for m in _re.finditer(_VERSION_NOT_A_CITATION, text)]

    for citation in ("Section 14.1.10", "section 9.1.6.2", "§14.1.10", "see Section 14.1.10.",
                     "Table 125.4.1", "Figure 174.1.2",
                     "per Section 9.1.6.2.1 the SSP"):     # its tail "1.6.2" is no version
        assert not hits(citation), f"citation read as a version: {citation!r} -> {hits(citation)}"
    for version in ("ship in 3.0.19", "landed in 4.0.0",
                    "v3.1.0 adds",                         # a leading v
                    "3.0.99 work", "ships in 3.0.19.",     # sentence-final period
                    "(3.0.20)"):
        assert hits(version), f"real version reference MISSED: {version!r}"


def test_every_released_version_kept_its_notes():
    """The directory accumulates. A release whose notes vanished cannot be re-described, and
    the publish tool would refuse to rebuild its tree."""
    import re as _re
    notes = os.listdir(os.path.join(_ROOT, "docs", "releases"))
    have = {m.group(1) for n in notes if (m := _re.fullmatch(r"v(\d+\.\d+\.\d+)\.md", n))}
    assert "3.0.17" in have, f"docs/releases lost v3.0.17.md; present: {sorted(have)}"


def test_the_publish_tool_refuses_a_tree_with_no_notes(tmp_path):
    """Behavioural, not a substring search: check_release_notes on a synthetic tree."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "publish_tree", os.path.join(_ROOT, "tools", "publish_tree.py"))
    pt = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(pt)

    pkg = tmp_path / "swi3s_studio"
    pkg.mkdir()
    (pkg / "__init__.py").write_text('__version__ = "9.9.9"\n', encoding="utf-8")

    notes, problems = pt.check_release_notes(str(tmp_path))
    assert notes is None and problems, "a tree with no notes file was accepted"
    assert "docs/releases/v9.9.9.md" in problems[0]

    (tmp_path / "docs" / "releases").mkdir(parents=True)
    target = tmp_path / "docs" / "releases" / "v9.9.9.md"
    target.write_text("# SWI3S Studio v9.9.9\n\nBody.\n", encoding="utf-8")
    notes, problems = pt.check_release_notes(str(tmp_path))
    assert notes and not problems, f"a valid notes file was rejected: {problems}"

    # An unfinalised file ("(in development)" in its title) is not publishable.
    target.write_text("# SWI3S Studio v9.9.9 (in development)\n\nBody.\n", encoding="utf-8")
    _, problems = pt.check_release_notes(str(tmp_path))
    assert any("in development" in p for p in problems), problems

    # A title naming the wrong version is caught too.
    target.write_text("# SWI3S Studio v1.2.3\n\nBody.\n", encoding="utf-8")
    _, problems = pt.check_release_notes(str(tmp_path))
    assert any("title line" in p for p in problems), problems


def test_preview_mode_allows_unfinished_notes_and_says_so(tmp_path):
    """A preview publishes an UNFINISHED cycle, so the '(in development)' refusal has to be
    lift-able — but only by saying plainly, in subject and body, that it is a preview.
    Releases stay strict.
    """
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "publish_tree", os.path.join(_ROOT, "tools", "publish_tree.py"))
    pt = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(pt)

    pkg = tmp_path / "swi3s_studio"
    pkg.mkdir()
    (pkg / "__init__.py").write_text('__version__ = "9.9.9"\n', encoding="utf-8")
    (tmp_path / "docs" / "releases").mkdir(parents=True)
    notes = tmp_path / "docs" / "releases" / "v9.9.9.md"
    notes.write_text("# SWI3S Studio v9.9.9 (in development)\n\nBody.\n", encoding="utf-8")

    # Default: refused, and the message points at the way out rather than just saying no.
    _text, problems = pt.check_release_notes(str(tmp_path))
    assert any("in development" in p for p in problems)
    assert any("--preview" in p for p in problems), (
        "the refusal must name --preview; a check that only says no invites deleting the marker")

    # --preview: allowed.
    text, problems = pt.check_release_notes(str(tmp_path), preview=True)
    assert text and not problems, problems

    # And the message must SAY it is a preview, in the subject and the body.
    msg = pt.branch_message(text, "9.9.9", ["a"], ["b"], "public/main", preview=True)
    assert "PREVIEW" in msg.splitlines()[0], f"subject does not mark it: {msg.splitlines()[0]!r}"
    assert "NOT A RELEASE" in msg
    assert "not tagged" in msg
    # A release message must NOT carry any of that.
    rel = pt.branch_message(text, "9.9.9", ["a"], ["b"], "public/main")
    assert "PREVIEW" not in rel and "NOT A RELEASE" not in rel
    # A preview has no tag, so it must not claim to differ from one. Compared on
    # whitespace-NORMALISED text: the preamble is wrapped at 95 columns, so "release tag" can
    # legitimately straddle a line break and a raw substring check reads that as absent.
    flat = lambda t: " ".join(t.split())        # noqa: E731
    assert "release tag" not in flat(msg), "the preview message names a tag that does not exist"
    assert "source commit it was built from" in flat(msg)
    assert "release tag" in flat(rel), "the release message SHOULD name the tag it differs from"


def _assembled_tree(tmp_path, text):
    """A minimal assembled tree: the scanner itself plus one file, no .git."""
    import shutil
    dest = tmp_path / "tree"
    (dest / "tools").mkdir(parents=True)
    shutil.copy2(os.path.join(_ROOT, "tools", "leak_scan.py"), dest / "tools")
    (dest / "README.md").write_text(text, encoding="utf-8")
    return dest


def test_the_publish_leak_check_fails_without_site_patterns(tmp_path, monkeypatch, capsys):
    """Structural patterns alone do not cover host, repository or third-party names, so a
    publish scan that ran none of the site patterns is a failure, and the scan's header is
    printed either way."""
    pt = _load_tool("publish_tree.py")
    dest = _assembled_tree(tmp_path, "A bus analyzer.\n")
    monkeypatch.delenv("SWI3S_LEAK_PATTERNS", raising=False)
    problems = pt.check_leak_scan(str(dest))
    assert any("SWI3S_LEAK_PATTERNS" in p for p in problems), problems
    assert "0 site-specific" in capsys.readouterr().out

    pats = tmp_path / "patterns.txt"
    pats.write_text("zzqq-not-in-this-tree\n", encoding="utf-8")
    monkeypatch.setenv("SWI3S_LEAK_PATTERNS", str(pats))
    assert pt.check_leak_scan(str(dest)) == []
    assert "1 site-specific" in capsys.readouterr().out

    pats.write_text("bus analy[sz]er\n", encoding="utf-8")
    assert pt.check_leak_scan(str(dest)) == ["leak scan failed on the assembled tree"]


def test_the_branch_message_is_derived_from_the_notes_verbatim():
    """The preamble is computed; the body is the file, verbatim."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "publish_tree", os.path.join(_ROOT, "tools", "publish_tree.py"))
    pt = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(pt)

    notes = "# SWI3S Studio v4.5.6\n\n## Ingest\n\n- a thing changed.\n"
    msg = pt.branch_message(notes, "4.5.6", ["a", "b"], ["c"], "public/main")
    assert msg.startswith("SWI3S Studio v4.5.6\n"), msg.splitlines()[0]
    assert "- a thing changed." in msg, "the notes body did not reach the message"
    assert "## Ingest" in msg
    assert "2 files" in msg and "1 file " in msg, "prune/graft counts are not derived"
    assert "public/main" in msg
    assert "docs/releases/v4.5.6.md" in msg, "the message must name the file it quotes"


def test_only_runs_the_named_checks_and_never_reads_as_the_gate(capsys):
    """`--only` (what tests/run_all.sh calls) runs just what it names, in gate order, and its
    summary says it was partial: a bare PASS from a subset would read as the release gate."""
    g = _load_tool("gate.py")
    ran = []
    for name in _GATE_CHECKS:
        setattr(g, name, lambda *a, _n=name, **k: ran.append(_n))
    g._FAILED[:] = []
    g._NOT_RUN[:] = []
    saved = sys.argv
    sys.argv = ["tools/gate.py", "--only", "per-suite,native"]
    try:
        rc = g.main()
    finally:
        sys.argv = saved
    out = capsys.readouterr().out
    assert rc == 0 and ran == ["check_native", "check_per_suite"]
    assert "a partial run, not the release gate" in out


def test_only_refuses_an_unknown_check():
    g = _load_tool("gate.py")
    with pytest.raises(SystemExit, match="unknown: typo"):
        g._selected(["tools/gate.py", "--only", "pytest,typo"])


def test_the_full_gate_does_not_run_the_opt_in_native_cpp_suite():
    g = _load_tool("gate.py")
    ran = []
    for name in _GATE_CHECKS:
        setattr(g, name, lambda *a, _n=name, **k: ran.append(_n))
    g._FAILED[:] = []
    g._NOT_RUN[:] = []
    saved = sys.argv
    sys.argv = ["tools/gate.py"]
    try:
        g.main()
    finally:
        sys.argv = saved
    assert "check_native_cpp" not in ran and "check_per_suite" in ran


def test_the_ratchet_reads_plain_output_and_refuses_output_it_cannot_read(monkeypatch):
    # With FORCE_COLOR set, mypy colours even a pipe and no error line parses. The run asks
    # for plain output with the colour variables removed, and a failing run with nothing
    # read is refused.
    import subprocess
    m = _load_tool("mypy_gate.py")
    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"], seen["env"] = cmd, kw.get("env") or {}
        return subprocess.CompletedProcess(
            cmd, 1, "\x1b[1mswi3s_studio/x.py:1: \x1b[31merror:\x1b[m nope\n", "")
    monkeypatch.setenv("FORCE_COLOR", "3")
    monkeypatch.setattr(m.subprocess, "run", fake_run)
    with pytest.raises(SystemExit) as exc:
        m._run_mypy()
    assert exc.value.code == 2
    assert "--no-color-output" in seen["cmd"] and "FORCE_COLOR" not in seen["env"]


# --- the native probe asserts WHICH swi3score it imported ---------------------------------

def _check_native_with_probe(monkeypatch, core_path, windows):
    """Run gate.check_native with the build stubbed green and the probe reporting
    `core_path` as swi3score.__file__, at the ABI session.py requires."""
    import subprocess
    g = _load_tool("gate.py")
    src = _read(os.path.join("swi3s_studio", "session.py"))
    want = re.search(r"_REQUIRED_SCORE_ABI\s*=\s*(\d+)", src).group(1)
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        out = "" if len(calls) == 1 else f"{want}\n9.9.9\n{core_path}\n"
        return subprocess.CompletedProcess(cmd, 0, out, "")
    monkeypatch.setattr(g, "_run", fake_run)
    monkeypatch.setattr(g, "_WINDOWS", windows)
    g._FAILED[:] = []
    g.check_native()
    return list(g._FAILED)


def test_the_native_probe_refuses_a_root_pyd_shadowing_the_windows_build(monkeypatch):
    """Windows builds into site-packages; a stale swi3score*.pyd in the checkout root comes
    first on PYTHONPATH and carries the same ABI number, so the path is what is checked."""
    import sysconfig
    stale = os.path.join(_ROOT, "swi3score.cp312-win_amd64.pyd")
    assert _check_native_with_probe(monkeypatch, stale, windows=True) == [
        "native core imported is not the one just built"]
    fresh = os.path.join(sysconfig.get_path("platlib"), "swi3score.cp312-win_amd64.pyd")
    assert _check_native_with_probe(monkeypatch, fresh, windows=True) == []


def test_the_native_probe_requires_the_core_built_beside_the_checkout(monkeypatch):
    import sysconfig
    built = os.path.join(_ROOT, "swi3score" + sysconfig.get_config_var("EXT_SUFFIX"))
    assert _check_native_with_probe(monkeypatch, built, windows=False) == []
    elsewhere = os.path.join(sysconfig.get_path("platlib"), "swi3score.so")
    assert _check_native_with_probe(monkeypatch, elsewhere, windows=False) == [
        "native core imported is not the one just built"]


# --- an empty pattern file, a mypy run that stopped early, check 5's file count, check 6's core

def _check_leaks_with_output(monkeypatch, stdout, returncode=0, patterns="/some/patterns.txt"):
    """Run gate.check_leaks with the scan reporting `stdout`; (FAILED, NOT_RUN) after it."""
    import subprocess
    g = _load_tool("gate.py")
    monkeypatch.setattr(g, "_run", lambda cmd, **kw: subprocess.CompletedProcess(
        cmd, returncode, stdout, ""))
    if patterns is None:
        monkeypatch.delenv("SWI3S_LEAK_PATTERNS", raising=False)
    else:
        monkeypatch.setenv("SWI3S_LEAK_PATTERNS", patterns)
    g._FAILED[:] = []
    g._NOT_RUN[:] = []
    g.check_leaks()
    return list(g._FAILED), list(g._NOT_RUN)


def test_a_pattern_file_with_no_patterns_is_not_run_not_clean(monkeypatch):
    head = "leak scan — the working tree: 5 files (5 text, 0 binary), {n} patterns " \
           "(13 structural, {m} site-specific)\n\nno leaks found\n"
    failed, not_run = _check_leaks_with_output(monkeypatch, head.format(n=13, m=0))
    assert failed == [] and len(not_run) == 1 and "holding no patterns" in not_run[0]
    assert _check_leaks_with_output(monkeypatch, head.format(n=16, m=3)) == ([], [])
    failed, not_run = _check_leaks_with_output(monkeypatch, head.format(n=13, m=0),
                                               patterns=None)
    assert failed == [] and "unset" in not_run[0]
    failed, _ = _check_leaks_with_output(monkeypatch, "no leaks found\n")
    assert failed and "names no pattern counts" in failed[0]


def test_the_ratchet_refuses_a_run_that_stopped_before_checking_the_tree(monkeypatch):
    """A blocking error makes mypy print one error line and stop; that one line is not a
    count of the tree, so the run is exit 2 whatever the baseline holds for that file."""
    import subprocess
    m = _load_tool("mypy_gate.py")
    out = ("swi3s_studio/ui/main_window.py:1: error: Invalid syntax  [syntax]\n"
           "Found 1 error in 1 file (errors prevented further checking)\n")
    monkeypatch.setattr(m.subprocess, "run",
                        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 2, out, ""))
    with pytest.raises(SystemExit) as exc:
        m._run_mypy_on("linux")
    assert exc.value.code == 2


def test_the_publish_leak_check_fails_a_scan_that_read_fewer_files_than_ship(
        tmp_path, monkeypatch):
    """A file in the assembled tree is published whatever .gitignore says (git archive
    exports tracked files), so a scan that left one out fails check 5."""
    pt = _load_tool("publish_tree.py")
    dest = _assembled_tree(tmp_path, "A bus analyzer.\n")
    pats = tmp_path / "patterns.txt"
    pats.write_text("zzqq-not-in-this-tree\n", encoding="utf-8")
    monkeypatch.setenv("SWI3S_LEAK_PATTERNS", str(pats))
    assert pt.check_leak_scan(str(dest)) == []
    (dest / ".gitignore").write_text("build/\n", encoding="utf-8")
    (dest / "packaging" / "build").mkdir(parents=True)
    (dest / "packaging" / "build" / "forced.md").write_text("x\n", encoding="utf-8")
    assert pt.check_leak_scan(str(dest)) == [
        "the leak scan read 3 of the 4 files in the assembled tree; every file in it ships"]


def test_check_6_compiles_the_core_from_the_assembled_trees_own_sources(tmp_path, monkeypatch):
    """The core the suite imports is built from a copy of the assembled tree's native/, in a
    work directory outside it, never copied from beside this checkout; a failed build is a
    problem, not a fallback."""
    import subprocess
    pt = _load_tool("publish_tree.py")
    monkeypatch.setattr(pt, "_WINDOWS", False)
    dest, work = tmp_path / "dest", tmp_path / "work"
    (dest / "native").mkdir(parents=True)
    work.mkdir()
    (dest / "native" / "build_local.sh").write_text("# published rev\n", encoding="utf-8")
    calls = []

    def fake_build(cmd, **kw):
        calls.append(cmd)
        script = pathlib.Path(cmd[-1])
        assert script.read_text(encoding="utf-8") == "# published rev\n"
        (script.parent.parent / "swi3score.cpython-312-test.so").write_bytes(b"core")
        return subprocess.CompletedProcess(cmd, 0, "", "")
    monkeypatch.setattr(pt.subprocess, "run", fake_build)
    lib, problem = pt.build_core(str(dest), str(work))
    assert problem is None and os.listdir(lib) == ["swi3score.cpython-312-test.so"]
    assert str(work) in calls[0][-1] and str(dest) not in calls[0][-1]
    assert sorted(p.name for p in dest.rglob("*")) == ["build_local.sh", "native"]

    monkeypatch.setattr(pt.subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(
        cmd, 1, "", "compiler error\n"))
    lib, problem = pt.build_core(str(dest), str(tmp_path / "work2"))
    assert lib is None and "did not build" in problem and "compiler error" in problem


# --- check 5 runs this checkout's scanner; symlinks are entries; the build is from HEAD

def test_the_publish_leak_check_runs_this_checkouts_scanner_not_the_trees(
        tmp_path, monkeypatch):
    """An assembled tree from an older commit carries that commit's scanner; check 5 scans
    it with this checkout's tools/leak_scan.py, so the current patterns apply."""
    pt = _load_tool("publish_tree.py")
    dest = _assembled_tree(tmp_path, "mounted at /" + "Volumes/Disk/work/x.csv\n")
    (dest / "tools" / "leak_scan.py").write_text(
        "print('leak scan — the working tree: 2 files (2 text, 0 binary), 1 patterns "
        "(0 structural, 1 site-specific)')\nprint()\nprint('no leaks found')\n",
        encoding="utf-8")
    pats = tmp_path / "patterns.txt"
    pats.write_text("zzqq-not-in-this-tree\n", encoding="utf-8")
    monkeypatch.setenv("SWI3S_LEAK_PATTERNS", str(pats))
    assert pt.check_leak_scan(str(dest)) == ["leak scan failed on the assembled tree"]


def test_a_symlink_to_a_directory_is_counted_and_checked(tmp_path, monkeypatch):
    """A link to a directory ships as a link: check 5 counts it and the scan reads its
    target, and check 2 resolves that target."""
    pt = _load_tool("publish_tree.py")
    dest = _assembled_tree(tmp_path, "A bus analyzer.\n")
    (dest / "data" / "real").mkdir(parents=True)
    (dest / "data" / "real" / "b.txt").write_text("x\n", encoding="utf-8")
    pats = tmp_path / "patterns.txt"
    pats.write_text("zzqq-not-in-this-tree\n", encoding="utf-8")
    monkeypatch.setenv("SWI3S_LEAK_PATTERNS", str(pats))
    os.symlink("real", dest / "data" / "relative")
    assert pt._file_count(str(dest)) == 4
    assert pt.check_leak_scan(str(dest)) == []
    os.symlink(str(dest / "data" / "real"), dest / "data" / "absolute")
    assert pt._file_count(str(dest)) == 5
    assert pt.check_leak_scan(str(dest)) == ["leak scan failed on the assembled tree"]
    (dest / "docs" / "internal").mkdir(parents=True)
    os.symlink("internal", dest / "docs" / "tier")
    assert pt.check_dangling(str(dest)) == ["docs/tier"]


def test_the_publish_tree_is_built_from_head_and_says_so(tmp_path, monkeypatch):
    """With no sha the tree is HEAD's: the usage says so, and uncommitted changes to tracked
    files are reported because they are not in it."""
    import subprocess
    pt = _load_tool("publish_tree.py")
    assert "from HEAD: uncommitted changes are NOT included" in pt.__doc__
    assert "from the working tree" not in pt.__doc__
    repo = tmp_path / "repo"
    repo.mkdir()
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.invalid",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.invalid")
    (repo / "a.md").write_text("one\n", encoding="utf-8")
    for cmd in (["git", "init", "-q"], ["git", "add", "a.md"],
                ["git", "commit", "-q", "-m", "a"]):
        subprocess.run(cmd, cwd=repo, check=True, env=env, capture_output=True)
    monkeypatch.setattr(pt, "_ROOT", str(repo))
    (repo / "untracked.md").write_text("x\n", encoding="utf-8")
    assert pt.uncommitted_changes() == []
    (repo / "a.md").write_text("two\n", encoding="utf-8")
    assert [ln[3:] for ln in pt.uncommitted_changes()] == ["a.md"]
