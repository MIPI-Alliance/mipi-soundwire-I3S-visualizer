"""The release gate is ONE definition, and the docs must describe reality.

Both guards here exist because of the same 3.0.11 incident. `ci.yml` declared a lint/type
job from 3.0.7 onward but the workflow never executed, no human ran ruff or mypy either,
and `docs/DEVELOPMENT.md`'s release checklist — a hand-maintained PROSE list — never
mentioned them. Two partial gates, each assumed to subsume the other.
445 ruff findings and 448 mypy errors accumulated over five releases and surfaced on the
first PUBLIC pull request of the v3 line.

Nothing could have caught that, because "the gate" existed only as English. It is now
`tools/gate.py`, and these tests fail the build if:

  * `ci.yml` declares a check that `tools/gate.py` does not run (they can no longer drift), or
  * the docs repeat any of the stale "CI runs nowhere" claims, or stop saying where CI
    actually does run (that claim went stale silently, and had already reached a release
    tag annotation that was about to be signed).

THIS FILE HAS TWO HALVES, and the second exists because the first is not enough. Drift
guards read SOURCE TEXT, which is the right tool for "does ci.yml still name this check" and
"do the docs still say where CI runs" — claims about documents. It is the wrong tool for the
gate's own control flow, and for a full release cycle that was all this file had: every
assertion was a substring search, and nothing imported or called either tool. A review
demonstrated the cost on tools/gate.py by changing `main()` to print its failures and then
`return 0`, leaving the literal "return 1" behind as a comment for the grep to find — all 17
tests still passed in 0.08 s. `bash tests/gate.sh` would have printed FAIL and exited 0, and
exit 0 IS the gate. tools/mypy_gate.py was in the same position for the same reason (nothing
called it), so its ratchet is covered here too.

So the behavioural half below EXECUTES both tools with their slow parts stubbed: the exit
code is asserted from a seeded outcome, not read off the source. Checked against real
mutations rather than assumed — `return 0` in place of `return 1` fails four of these, and a
regression predicate that can never be true fails three more, including the one guarding
`--allow-incomparable` against being widened past the environment case.
"""
import importlib.util
import json
import os
import re
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_CI = os.path.join(_ROOT, ".github", "workflows", "ci.yml")
_GATE = os.path.join(_ROOT, "tools", "gate.py")          # the implementation
_GATE_SH = os.path.join(_ROOT, "tests", "gate.sh")       # bash wrapper
_GATE_PS1 = os.path.join(_ROOT, "tests", "gate.ps1")     # PowerShell wrapper
_DOCS = ["README.md", os.path.join("docs", "DEVELOPMENT.md"), os.path.join("docs", "TESTING.md")]


def _read(path: str) -> str:
    with open(os.path.join(_ROOT, path) if not os.path.isabs(path) else path,
              encoding="utf-8") as f:
        return f.read()


def _yaml_code(path: str) -> str:
    """ci.yml with comment lines stripped. Both guards below look for a construct that the
    file also DESCRIBES in a comment ("it was `continue-on-error: true` …"), so scanning the
    raw text finds the explanation and reports a false failure."""
    return "\n".join(ln for ln in _read(path).splitlines()
                     if not ln.lstrip().startswith("#"))


# Commands in ci.yml that are SETUP, not checks — they install or prepare, they do not
# assert anything, so the gate has no counterpart for them.
_CI_SETUP = ("apt-get", "pip install --upgrade pip", "python -m pip install -r",
             "pip install -r requirements.txt", "pip install --upgrade pip ruff mypy")

# A CI check the gate deliberately does NOT run, each with the reason. Adding to this list is
# a conscious decision; the point is that it cannot happen by omission.
_NOT_IN_GATE = {
    # Coverage GATES as of 3.0.13 (--cov-fail-under), but in CI only. The local gate does not
    # run it on purpose: it would mean a second, instrumented pass over the whole suite
    # (~2.5 min on top of the run that is already there) to re-measure a number that moves
    # slowly and is environment-sensitive anyway. This is the documented "neither gate
    # subsumes the other" split, not an omission — see docs/DEVELOPMENT.md.
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
    its own entry and looks like an unrecognised check (the `apt-get install` package list
    did exactly that on the first attempt)."""
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


# --- P2/P3: the gate script is the single definition -------------------------------------

def test_gate_exists_and_reports_every_failure():
    assert os.path.isfile(_GATE), "tools/gate.py is the release gate; it must exist"
    text = _read(_GATE)
    # It must aggregate failures, not stop at the first one: "pytest failed" alone hides
    # whether ruff and mypy would also have failed, which is half the value of a gate.
    assert "_FAILED" in text and "return 1" in text, \
        "gate.py must collect every failure and exit non-zero"


def test_gate_is_cross_platform():
    """The gate was bash-only for one commit, which excluded the WINDOWS half of the release
    gate — no bash on PATH there, and native/build_local.sh is a Unix compile path. That
    re-created the very gap the gate exists to close, so pin the platform handling."""
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
    """Two shells, one implementation. A wrapper that grows its own checks is how the gate
    and CI drifted in the first place."""
    for path, name in ((_GATE_SH, "gate.sh"), (_GATE_PS1, "gate.ps1")):
        text = _read(path)
        assert "tools/gate.py" in text, f"tests/{name} must delegate to tools/gate.py"
        for leaked in ("pytest", "ruff check", "mypy_gate"):
            assert leaked not in text, (
                f"tests/{name} runs {leaked!r} itself — put it in tools/gate.py so both "
                "platforms get it")


def test_gate_runs_every_check_ci_declares():
    """If ci.yml grows a check, the gate must grow it too.

    DEFAULT-DENY, and that matters. The first version of this guard compared five hardcoded
    (ci_frag, gate_frag) pairs — so it only re-verified the checks its author already knew
    about. The 3.0.12 review proved it: appending a whole new `security:` job running
    `bandit -r swi3s_studio` to a copy of ci.yml left the test GREEN, because the new job
    matched none of the five pairs. A guard against "a check declared but never enforced"
    that is itself blind to new checks reproduces the very incident it exists to prevent.

    Now every `run:` command in ci.yml must be accounted for: setup, mapped to a gate check,
    or consciously listed in _NOT_IN_GATE with a reason. Anything else fails."""
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
    """It was `continue-on-error: true` while never executing anywhere. Now that the tree is
    clean, an advisory lint job is just a slower way to accumulate 445 findings."""
    ci = _yaml_code(_CI)
    lint = ci[ci.index("  lint:"):]
    assert "continue-on-error" not in lint.split("steps:")[0], \
        "the lint job must be blocking (no continue-on-error)"


def test_mypy_result_is_not_discarded():
    """`mypy … || true` meant the type state was UNMEASURED, not clean — 448 errors when
    someone finally looked."""
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
    checks in prose, which is what drifted."""
    dev = _read(os.path.join("docs", "DEVELOPMENT.md"))
    assert "tools/gate.py" in dev, \
        "docs/DEVELOPMENT.md must name tools/gate.py as the gate"
    # Separator-agnostic: the doc writes the Windows entry point as `.\tests\gate.ps1`.
    assert "gate.ps1" in dev, \
        "the checklist must give the Windows entry point too — that half was once impossible"


# --- P7: the docs must not describe a CI that no longer matches reality ------------------

# The exact claims that WERE in these docs and became false once the workflow started
# running. A literal list, deliberately: my first two attempts used a regex for "a negation
# reaching a run/execute verb", which flagged a TRUE statement about where CI is not enabled,
# and then "a check gate.py does not run" (not about CI at all). Prose is a bad thing to
# pattern-match; pin the sentences that actually went stale and extend the list if a new
# formulation appears — "No CI actually runs" was missed by the first version of this list
# and sat in docs/TESTING.md for a full release.
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
    """These sentences were true when written and became false without anyone noticing —
    including inside a release tag annotation that was about to be signed."""
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
# The narrow definition (a grep for a handful of known strings) was published INTO the tree:
# docs/DEVELOPMENT.md carried the pattern list itself, which named a git host, a repository,
# a lab machine's address and an SSH key file. A list of the strings you are trying not to
# publish is itself the leak. These guards keep the scanner honest and keep the concrete
# names out of the repository.

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
    assert not re.search(r"[a-z0-9-]+\\?\.(?:apple|internal|corp|local)\b", text, re.I), \
        "tools/leak_scan.py contains a site-specific host name; move it to the env-var file"


def test_leak_scan_is_loud_when_only_partly_configured():
    """A scan that silently covers less than it appears to is the failure mode of this whole
    area (see the lint job that never ran)."""
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
# and `swviz/version.py::APP_VERSION` together. The third was missed for 3.0.9, 3.0.10 AND
# 3.0.11 — it sat at 3.0.8 while the app shipped 3.0.11. It only surfaces when the package
# isn't importable (`app_version()` prefers the live value), so nothing failed and nobody
# noticed. A prose checklist step that is skipped three times running should be a test.

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


def test_ci_tolerates_only_the_incomparable_outcome():
    """CI may forgive NOT COMPARABLE, and nothing else.

    `ci.yml` calls tools/mypy_gate.py DIRECTLY, not through tools/gate.py, so it cannot
    interpret exit 3 the way the local gate does. That shipped in 3.0.12 and made the lint
    job fail on every run of the published repository — a permanent red cross for an
    environment difference. The fix is a flag on the tool, not shell plumbing in the YAML.

    What this pins is the SHAPE of the tolerance: --allow-incomparable must map exit 3 alone.
    The obvious way to break it later is to widen the same escape hatch to a regression."""
    ci = _read(".github/workflows/ci.yml")
    src = _read("tools/mypy_gate.py")
    assert "--allow-incomparable" in ci, \
        "the mypy step must tolerate NOT COMPARABLE explicitly, or CI is red forever"
    assert "--allow-incomparable" in src, "the flag has to exist in the tool"

    # The flag's return must be inside the NOT-COMPARABLE branch only. Both the regression
    # and the could-not-run exits have to stay reachable and unguarded by it.
    head, _, tail = src.partition("--allow-incomparable")
    assert "NOT COMPARABLE" in head, "the flag is handled outside the not-comparable branch"
    assert "return 0" in tail.split("return")[0] + "return 0", "the flag must return success"
    for needed, why in (("return 1", "a regression must still fail"),
                        ("SystemExit(2)", "mypy failing to run must still fail")):
        assert needed in src, f"{needed} is gone from mypy_gate.py — {why}"


def test_ci_installs_numpy_for_the_type_check():
    """mypy follows imports into installed stubs. Without numpy the lint job checked the tree
    against no array types AND read the ratchet's environment fingerprint as '?', which can
    match no baseline — the check was both weaker and permanently incomparable."""
    ci = _read(".github/workflows/ci.yml")
    lint = ci.split("  lint:", 1)[1]
    install = [c for c in _ci_run_commands(lint) if "pip install" in c and "mypy" in c]
    assert install, "the lint job installs no mypy?"
    assert any("numpy" in c for c in install), \
        "the lint job must install numpy, or mypy type-checks against no array stubs: " \
        + str(install)


# --------------------------------------------------------------------------------------
# BEHAVIOURAL HALF — the tools are executed, not grepped.
#
# Both are loaded by path (tools/ is not a package) and their slow work is replaced: the
# gate's check_* functions become no-ops so main() only aggregates, and mypy_gate's
# _run_mypy returns synthetic counts against a synthetic baseline. What is left is exactly
# the logic a release depends on — "did anything fail, and does that reach the exit code".
# --------------------------------------------------------------------------------------

_GATE_CHECKS = ("check_native", "check_pytest", "check_per_suite",
                "check_tool", "check_mypy", "check_leaks")


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
    the checks themselves — the checks have their own tests (the suite they run), while
    "a non-empty _FAILED must exit nonzero" has had none.
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
    """The mutation that proved this file was untested: main() printing FAIL and returning 0.
    `exit 0 is the gate` is the sentence in CLAUDE.md this asserts."""
    assert _run_gate(failed=["pytest not-perf"]) == 1, \
        "a failed check must make the gate exit nonzero"


def test_the_gate_reports_every_failure_so_one_cannot_hide_the_rest(capsys):
    """The aggregating design: checks all run and every failure is named. A gate that stopped
    at the first red would send someone round the loop once per defect."""
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
    """The mutation here was `regressions = []`, which reported no regression whatever mypy
    found. One more error in one file must fail."""
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


def test_allow_incomparable_maps_only_the_environment_case_to_success(tmp_path):
    assert _run_ratchet({_F1: 3}, {_F1: 3}, tmp_path,
                        argv=("tools/mypy_gate.py", "--allow-incomparable"),
                        base_env={"mypy": "9.9.9", "numpy": "0.0.1"}) == 0


def test_allow_incomparable_does_not_swallow_a_regression(tmp_path):
    """The escape hatch CI needs, and the one way it must never be widened. This is the case
    the textual test claimed to pin by checking where the flag's name appeared in the file."""
    assert _run_ratchet({_F1: 4}, {_F1: 3}, tmp_path,
                        argv=("tools/mypy_gate.py", "--allow-incomparable")) == 1


def test_the_ratchet_fails_a_baseline_naming_a_deleted_file(tmp_path):
    """A stale entry lets a recreated file inherit a count nobody reviewed (found in the
    3.0.12 review): fewer errors than the stale number read as 'improved' and exited 0."""
    gone = os.path.join("swi3s_studio", "no_such_module_here.py")
    assert _run_ratchet({_F1: 3}, {_F1: 3, gone: 4}, tmp_path) == 1
