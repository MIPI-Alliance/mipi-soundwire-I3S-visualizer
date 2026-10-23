"""Native build configuration.

The native build must fail at configure time, with an actionable message, when the Python
development HEADERS are missing. pybind11's legacy FindPythonInterp/FindPythonLibs shim
locates the interpreter and the shared library but never checks for Python.h, so the
configuration must use CMake's FindPython with Development.Module. The launchers and README
must name the headers as the prerequisite.

These tests parse CMakeLists and the launchers rather than running CMake, so they work on
any machine (CMake isn't a test dependency).
"""
import os
import re

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_CMAKE = os.path.join(_ROOT, "native", "CMakeLists.txt")


def _cmake_text() -> str:
    with open(_CMAKE, encoding="utf-8") as f:
        return f.read()


def test_uses_the_modern_findpython():
    """PYBIND11_FINDPYTHON must be ON before find_package(pybind11).

    pybind11 only defaults to CMake's FindPython when policy CMP0148 is NEW, which our
    cmake_minimum_required(3.15) leaves unset; without this it uses the legacy shim that
    skips the header check."""
    text = _cmake_text()
    assert re.search(r"^\s*set\(PYBIND11_FINDPYTHON ON\)", text, re.M), \
        "PYBIND11_FINDPYTHON must be set ON"
    find_flag = text.index("set(PYBIND11_FINDPYTHON ON)")
    find_pybind = text.index("find_package(pybind11")
    assert find_flag < find_pybind, \
        "PYBIND11_FINDPYTHON must be set BEFORE find_package(pybind11) to take effect"


def test_requires_the_development_headers():
    """Development.Module is the component that checks for Python.h; requiring only
    Interpreter (or nothing) lets a headerless environment configure."""
    text = _cmake_text()
    m = re.search(r"find_package\(Python[^)]*\)", text, re.S)
    assert m, "no find_package(Python ...) call"
    assert "Development.Module" in m.group(0), m.group(0)


def test_header_failure_names_the_missing_package():
    """The configure error names the actual fix (the python3-devel / python3-dev
    package, Python.h), not only "Could NOT find Python (missing: Development.Module)"."""
    text = _cmake_text()
    assert "Python_Development.Module_FOUND" in text, \
        "no explicit check that produces a readable error"
    lower = text.lower()
    for hint in ("python3-devel", "python3-dev", "python.h"):
        assert hint in lower, f"the FATAL_ERROR should mention {hint!r}"


def test_launchers_do_not_blame_the_compiler():
    """Neither launcher states a cause it hasn't checked: a failed native build does not
    claim "a C++ compiler is required", and the Python headers are named as a likely
    cause."""
    for name in ("run.sh", "run.ps1"):
        with open(os.path.join(_ROOT, name), encoding="utf-8") as f:
            text = f.read()
        # The unconditional compiler claim, in either launcher's phrasing.
        assert "a C++ compiler is required)" not in text, \
            f"{name} still asserts a missing compiler as the cause"
        assert "Python.h" in text, \
            f"{name} should point at the Python headers as a likely cause"


def test_readme_documents_the_header_prerequisite():
    """The README names the header prerequisite and the development package for the
    common distros."""
    with open(os.path.join(_ROOT, "README.md"), encoding="utf-8") as f:
        text = f.read()
    assert "python3-devel" in text and "python3-dev" in text, \
        "README must name the development package for the common distros"
    assert "Python.h" in text


def test_readme_has_a_no_root_recipe():
    """A locked-down build host can't `sudo dnf install`, which is the only advice the
    prerequisite block gives on its own. The README must offer at least one route that
    needs no root, and must name the REAL override variables."""
    with open(os.path.join(_ROOT, "README.md"), encoding="utf-8") as f:
        text = f.read()
    assert "No root / no sudo" in text, "no no-root section"
    assert "SWI3S_PYTHON" not in text, "SWI3S_PYTHON is not a real variable"
    # The interpreter override run.sh actually reads, and both header overrides.
    assert "PYTHON=" in text
    assert "SKBUILD_CMAKE_DEFINE" in text, "no override for the pip/scikit-build path"
    assert "PYTHON_INCLUDE=" in text, "no override for the offline build_local.sh path"


def test_launcher_interpreter_override_is_named_correctly():
    """Guard the variable the README tells people to use against the launcher that
    reads it, so the two can't drift."""
    with open(os.path.join(_ROOT, "run.sh"), encoding="utf-8") as f:
        run_sh = f.read()
    assert 'PY="${PYTHON:-}"' in run_sh, \
        "run.sh no longer reads PYTHON; update the README's no-root recipe too"


def test_offline_builder_honours_a_header_override_and_checks_the_headers():
    """native/build_local.sh (the route a locked-down or offline host takes) honours a
    PYTHON_INCLUDE override and checks that Python.h exists."""
    with open(os.path.join(_ROOT, "native", "build_local.sh"), encoding="utf-8") as f:
        text = f.read()
    assert '${PYTHON_INCLUDE:-' in text, "no PYTHON_INCLUDE override"
    assert 'Python.h' in text, "does not verify the headers exist"
    assert "python3-devel" in text, "failure message should name the fix"


def _run_sh() -> str:
    with open(os.path.join(_ROOT, "run.sh"), encoding="utf-8") as f:
        return f.read()


def test_launcher_prefers_an_interpreter_with_headers():
    """A managed build host often has several Pythons, only some with dev headers.
    Selection makes two passes — supported+headers first, version-only as a fallback —
    so the usable one is picked automatically instead of failing the build minutes later.
    The fallback must stay: with no headers anywhere the module may already be built, and
    then no build (and no error) is needed."""
    text = _run_sh()
    assert "has_headers()" in text, "no header predicate"
    assert text.count("for c in python3.13 python3.12 python3.11 python3;") == 2, \
        "expected two selection passes (prefer headers, then version-only fallback)"
    first = text.index("for c in python3.13")
    assert "has_headers" in text[first:text.index("for c in python3.13", first + 1)], \
        "the FIRST pass is the one that must require headers"


def test_launcher_fails_before_installing_deps():
    """On a headerless host the header check comes before the venv is created and ~200 MB
    of wheels are downloaded, not after."""
    text = _run_sh()
    guard = text.index("Cannot build the decode core with")
    venv_create = text.index('"$PY" -m venv "$VENV"')
    deps = text.index("Installing Python dependencies...")
    assert guard < venv_create < deps, \
        "the header guard must run before the venv is created and deps installed"


def test_launcher_guard_has_an_escape_hatch():
    """Someone who has extracted headers into $HOME points the build at them with
    SKBUILD_CMAKE_DEFINE. The guard must not block that — the headers ARE available, just
    not where sysconfig looks."""
    text = _run_sh()
    for m in re.finditer(r"if ! has_headers [^\n]*\n", text):
        assert "SKBUILD_CMAKE_DEFINE" in m.group(0), \
            f"header guard without an override escape hatch: {m.group(0)!r}"


def test_launcher_guard_covers_the_rebuild_path_too():
    """--rebuild or a native/ edit reaches a build with the venv already in place; that
    path needs the same check (and there must be more than one guard)."""
    text = _run_sh()
    assert len(re.findall(r"if ! has_headers ", text)) >= 2, \
        "expected a guard on both the fresh-venv and the rebuild paths"


def test_launcher_guidance_leads_with_the_no_root_options():
    """The sudo line is not the only advice, and the no-root routes come first."""
    text = _run_sh()
    assert "Without root" in text
    assert text.index("Without root") < text.index("With root:"), \
        "lead with the options that need no root"
    for hint in ("uv python install", "rpm2cpio", "PYTHON=/that/python3"):
        assert hint in text, f"guidance should mention {hint!r}"


# --- run.ps1: the same guard, and the quoting it depends on -----------------------------
#
# The Windows launcher has the same pre-flight check. Its probe runs through cmd /c, so
# these pin the quoting that lets it report headers that are present.

def _run_ps1() -> str:
    with open(os.path.join(_ROOT, "run.ps1"), encoding="utf-8") as f:
        return f.read()


def test_windows_launcher_checks_the_headers_too():
    """run.ps1 has a header predicate and guidance, since a Windows 'embeddable package'
    zip ships no headers."""
    text = _run_ps1()
    assert "function Test-PythonHeaders" in text, "no header predicate"
    assert "function Show-HeaderHelp" in text, "no actionable guidance to print"
    assert "SKBUILD_CMAKE_DEFINE" in text, "no override mentioned for headers elsewhere"


def test_windows_header_probe_quoting_survives_cmd():
    r"""The probe runs through `cmd /c`, where \" does NOT escape a quote — it breaks the
    argument, Python never sees valid source, and the non-zero exit reads as "no headers".
    Use single quotes for the Python string literals and doubled "" for the cmd level."""
    text = _run_ps1()
    body = text[text.index("function Test-PythonHeaders"):text.index("function Show-HeaderHelp")]
    # The function carries a comment quoting the \" form, so scan the code alone.
    code = "\n".join(ln for ln in body.splitlines() if not ln.lstrip().startswith("#"))
    assert '\\"' not in code, r'\" does not escape a quote through cmd /c'
    assert "get_paths()['include']" in code, \
        "the Python-level strings must use single quotes"
    assert "'Python.h'" in code, "the Python-level strings must use single quotes"


def test_windows_header_probe_call_sites_pass_the_interpreter_unquoted():
    """Test-PythonHeaders adds its own quoting, because it takes two shapes of argument: a
    PATH (may contain spaces, so needs quotes) or a launcher invocation like `py -3.12`
    (must NOT be quoted, or cmd treats the whole string as one executable name). A
    pre-quoted argument arrives double-quoted and the probe fails."""
    text = _run_ps1()
    calls = re.findall(r"Test-PythonHeaders ([^)]*)\)", text)
    assert calls, "no Test-PythonHeaders call sites found"
    for arg in calls:
        assert '"' not in arg and "'" not in arg, \
            f"call site pre-quotes the interpreter: {arg!r} (the function quotes it)"


def test_windows_launcher_fails_before_installing_deps():
    """As in run.sh, the header check runs before the venv is created and ~200 MB of
    wheels are pulled."""
    text = _run_ps1()
    guard = text.index("Cannot build the decode core with")
    venv_create = text.index("-m venv")
    deps = text.index("Installing Python dependencies...")
    assert guard < venv_create < deps, \
        "the header guard must run before the venv is created and deps installed"


def test_windows_launcher_guards_both_build_paths_with_an_escape_hatch():
    """-Rebuild or a native\\ edit reaches a build with the venv already present, so that
    path needs the check too — and neither guard may block someone who has pointed
    SKBUILD_CMAKE_DEFINE at headers of their own."""
    text = _run_ps1()
    guard_lines = [ln for ln in text.splitlines() if re.search(r"-not \(Test-PythonHeaders \$", ln)]
    assert len(guard_lines) >= 2, \
        "expected a guard on both the fresh-venv and the rebuild paths"
    for ln in guard_lines:
        assert "SKBUILD_CMAKE_DEFINE" in ln, \
            f"header guard without an override escape hatch: {ln.strip()!r}"


def test_readme_does_not_claim_windows_has_no_header_check():
    """The README's no-root section describes run.ps1's header check and does not claim
    the Windows launcher lacks one."""
    with open(os.path.join(_ROOT, "README.md"), encoding="utf-8") as f:
        text = f.read()
    assert "no equivalent check" not in text, \
        "README still says run.ps1 has no header check; it does"
    assert "run.ps1" in text.split("No root / no sudo", 1)[1].split("</details>", 1)[0], \
        "the no-root section should say what the Windows launcher does"
