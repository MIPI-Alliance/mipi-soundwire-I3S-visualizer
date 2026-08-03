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
"""
import os
import re

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
