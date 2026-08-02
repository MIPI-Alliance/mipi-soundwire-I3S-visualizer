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
    """If ci.yml grows a check, the gate must grow it too. This is the drift guard: the
    prose checklist silently omitted lint/type for five releases."""
    ci, gate = _read(_CI), _read(_GATE)
    # (what ci.yml runs, what tools/gate.py must therefore also run)
    required = [
        ('pytest -q -m "not perf"', 'check_pytest("not perf"'),
        ("pytest -q -m perf", 'check_pytest("perf"'),
        ("ruff check .", '"ruff", ["check", "."]'),
        ("tools/mypy_gate.py", "mypy_gate.py"),
        ("pip install ./native", "build_local.sh"),
    ]
    for ci_frag, gate_frag in required:
        if ci_frag in ci:
            assert gate_frag in gate, (
                f"ci.yml runs {ci_frag!r} but tools/gate.py has no {gate_frag!r} — the gate "
                "and CI have drifted, which is exactly how lint/type went unrun")


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
