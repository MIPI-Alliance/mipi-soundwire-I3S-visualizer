"""The command lines and listings of the small tools: a mistyped option is refused, a
non-ASCII path is read, a bad tree is an error, and the launchers and the bundle spec carry
what they promise. Every tree and repository here is synthetic."""
import importlib.metadata
import os
import pathlib
import re
import subprocess
import sys
import time
import types

import pytest
from conftest import commit_files, git_in, load_tool

pytestmark = pytest.mark.platform

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


_tool = load_tool


# Private-range addresses are assembled at run time so this file carries none for the scan.
_PRIVATE_192 = ".".join(["192", "168", "4", "7"])
_PRIVATE_192B = ".".join(["192", "168", "0", "9"])
_PRIVATE_10 = ".".join(["10", "1", "4", "4"])
_PRIVATE_172 = ".".join(["172", "20", "3", "1"])


def _text(rel):
    return pathlib.Path(_ROOT, rel).read_text(encoding="utf-8")


# ----------------------------------------------------------------------------- tools/gate.py

@pytest.mark.parametrize("argv", [["--relase"], ["--gest"], ["--quik"], ["--guest", "--release"],
                                  ["--release", "stray"], ["--release", "--quick"],
                                  ["--guest", "--quick"], ["--on-guest"]])
def test_the_gate_refuses_an_unknown_option_and_the_two_stages_together(argv, capsys,
                                                                         monkeypatch):
    g = _tool("gate")
    ran = []
    monkeypatch.setattr(g, "_CHECKS", {name: (lambda name=name: ran.append(name), stages, quick)
                                       for name, (_check, stages, quick) in g._CHECKS.items()})
    monkeypatch.setattr(sys, "argv", ["tools/gate.py", *argv])
    assert g.main() == 2
    assert "gate:" in capsys.readouterr().err
    assert ran == []


def test_the_gate_accepts_every_option_it_documents():
    g = _tool("gate")
    assert g._bad_arguments(["gate.py", "--release", "--on-guest", "--verbose"]) is None
    assert g._bad_arguments(["gate.py", "--only", "suite,native", "--quick"]) is None
    assert g._bad_arguments(["gate.py", "--guest"]) is None
    assert g._bad_arguments(["gate.py", "--guest", "--on-guest"]) is None
    assert g._bad_arguments(["gate.py", "--quick"]) is None


def test_the_native_cpp_suite_runner_comes_from_the_environment(monkeypatch):
    g = _tool("gate")
    monkeypatch.delenv("SWI3S_NATIVE_CPP_SUITE", raising=False)
    g._NOT_RUN[:] = []
    g.check_native_cpp()
    assert len(g._NOT_RUN) == 1 and "SWI3S_NATIVE_CPP_SUITE" in g._NOT_RUN[0]


@pytest.mark.parametrize("script, rc, failed, not_run", [
    ("runner.sh", 0, [], 0), ("runner.sh", 1, ["native C++ suite"], 0),
    ("absent.sh", 0, [], 1)])
def test_the_native_cpp_suite_runs_the_script_the_variable_names(tmp_path, monkeypatch, script,
                                                                  rc, failed, not_run):
    """The runner $SWI3S_NATIVE_CPP_SUITE names is run with bash and its exit status is the
    check's; a variable naming no file is NOT RUN and runs nothing."""
    g = _tool("gate")
    (tmp_path / "runner.sh").write_text("exit 0\n", encoding="utf-8")
    runner = str(tmp_path / script)
    monkeypatch.setenv("SWI3S_NATIVE_CPP_SUITE", runner)
    monkeypatch.setattr(g, "_WINDOWS", False)
    ran = []
    monkeypatch.setattr(g, "_run", lambda cmd, **k: ran.append(cmd)
                        or subprocess.CompletedProcess(cmd, rc))
    g._FAILED[:] = []
    g._NOT_RUN[:] = []
    g.check_native_cpp()
    assert g._FAILED == failed and len(g._NOT_RUN) == not_run
    assert ran == ([] if not_run else [["bash", runner]])


# ------------------------------------------------------------------------- tools/leak_scan.py

@pytest.mark.parametrize("rev", [False, True])
def test_a_leak_in_a_file_with_a_non_ascii_name_is_found(tmp_path, monkeypatch, capsys, rev,
                                                         isolated_git):
    ls = _tool("leak_scan")
    repo = tmp_path / "repo"
    repo.mkdir()
    git_in(repo, "init", "-q")
    commit_files(repo, {"café.md": f"host {_PRIVATE_192}\n"}, "x")
    monkeypatch.setattr(ls, "_ROOT", str(repo))
    monkeypatch.delenv("SWI3S_LEAK_PATTERNS", raising=False)
    monkeypatch.setattr(sys, "argv", ["leak_scan.py", *(["HEAD"] if rev else [])])
    assert ls.main() == 1
    out = capsys.readouterr().out
    assert _PRIVATE_192 in out and "café.md" in out


def test_a_revision_is_scanned_from_a_root_below_the_repositorys_top_level(
        tmp_path, monkeypatch, capsys, isolated_git):
    ls = _tool("leak_scan")
    repo = tmp_path / "repo"
    repo.mkdir()
    git_in(repo, "init", "-q")
    commit_files(repo, {"sub/a.md": f"host {_PRIVATE_192}\n", "top.md": "clean\n"}, "x")
    monkeypatch.setattr(ls, "_ROOT", str(repo / "sub"))
    monkeypatch.delenv("SWI3S_LEAK_PATTERNS", raising=False)
    monkeypatch.setattr(sys, "argv", ["leak_scan.py", "HEAD"])
    monkeypatch.setattr(ls, "_UNREADABLE", [])
    assert ls.main() == 1
    out = capsys.readouterr().out
    assert _PRIVATE_192 in out and "a.md" in out and "COULD NOT BE READ" not in out


def test_a_revision_git_cannot_resolve_exits_two_with_a_message(tmp_path, monkeypatch, capsys,
                                                                isolated_git):
    """Exit 1 means possible leaks; a revision the scan cannot read is an input error, 2."""
    ls = _tool("leak_scan")
    repo = tmp_path / "repo"
    repo.mkdir()
    git_in(repo, "init", "-q")
    commit_files(repo, {"a.md": "clean\n"}, "x")
    monkeypatch.setattr(ls, "_ROOT", str(repo))
    monkeypatch.delenv("SWI3S_LEAK_PATTERNS", raising=False)
    monkeypatch.setattr(sys, "argv", ["leak_scan.py", "abc1234"])
    assert ls.main() == 2
    got = capsys.readouterr()
    assert "abc1234" in got.err and "Traceback" not in got.err and "no leaks found" not in got.out


def test_without_git_a_revision_exits_two_and_the_working_tree_is_walked(
        tmp_path, monkeypatch, capsys):
    ls = _tool("leak_scan")
    (tmp_path / "a.md").write_text(f"host {_PRIVATE_192}\n", encoding="utf-8")

    def no_git(cmd, **kw):
        raise FileNotFoundError(cmd[0])
    monkeypatch.setattr(ls, "_ROOT", str(tmp_path))
    monkeypatch.setattr(ls.subprocess, "run", no_git)
    monkeypatch.delenv("SWI3S_LEAK_PATTERNS", raising=False)
    monkeypatch.setattr(sys, "argv", ["leak_scan.py", "HEAD"])
    assert ls.main() == 2
    assert "git is not installed" in capsys.readouterr().err
    monkeypatch.setattr(sys, "argv", ["leak_scan.py"])
    assert ls.main() == 1
    assert _PRIVATE_192 in capsys.readouterr().out


def test_a_listed_file_that_cannot_be_read_is_a_failure(tmp_path, monkeypatch, capsys,
                                                         isolated_git):
    ls = _tool("leak_scan")
    repo = tmp_path / "repo"
    repo.mkdir()
    git_in(repo, "init", "-q")
    commit_files(repo, {"a.md": "clean\n"}, "x")
    monkeypatch.setattr(ls, "_ROOT", str(repo))
    monkeypatch.delenv("SWI3S_LEAK_PATTERNS", raising=False)
    monkeypatch.setattr(sys, "argv", ["leak_scan.py", "HEAD"])
    real = subprocess.run

    def refuse_show(cmd, *a, **k):
        if cmd[:2] == ["git", "show"]:
            return subprocess.CompletedProcess(cmd, 128, b"", b"fatal")
        return real(cmd, *a, **k)

    monkeypatch.setattr(ls.subprocess, "run", refuse_show)
    assert ls.main() == 1
    assert "COULD NOT BE READ" in capsys.readouterr().out


def test_the_private_address_patterns_separate_addresses_from_citations():
    ls = _tool("leak_scan")
    hit = [re.compile(rx) for rx, _ in ls._STRUCTURAL]

    def found(text):
        return any(r.search(text) for r in hit)

    assert found(f"a host at {_PRIVATE_10}.") and found(_PRIVATE_192B)
    assert found(f"a host at {_PRIVATE_172}")
    assert not found(".".join(["172", "15", "3", "1"]))          # below 172.16/12
    # A four-part section citation is refused by the section sign alone.
    assert not found(f"§{_PRIVATE_10}") and not found(f"section §{_PRIVATE_10}.")
    # A fifth part is refused with or without the sign.
    assert not found(f"section §{_PRIVATE_10}.2") and not found(f"{_PRIVATE_10}.2")


# --------------------------------------------------------------------- tools/native_coverage.py

def test_native_coverage_reports_only_this_checkouts_native_sources(tmp_path, monkeypatch):
    nc = _tool("native_coverage")
    # Directory names holding "python", "build" or "checkout" must not hide sources.
    root = tmp_path / "python-work" / "Xcode-checkout"
    (root / "native" / "swi3score").mkdir(parents=True)
    (root / "native" / "build").mkdir()
    for rel in ("native/swi3score/a.cpp", "native/swi3score/a.h", "native/build/gen.cpp",
                "native/CMakeLists.txt"):
        (root / rel).write_text("", encoding="utf-8")
    monkeypatch.setattr(nc, "_ROOT", str(root))
    assert [os.path.relpath(p, root) for p in nc._native_sources()] == \
        [os.path.join("native", "swi3score", "a.cpp"), os.path.join("native", "swi3score", "a.h")]
    header = "Filename   Regions  Missed Regions\n-----------------\n"
    assert nc._lists_native_source(header + "Decoder.cpp   10  0\nTOTAL  10  0\n")
    assert not nc._lists_native_source(header + "TOTAL  0  0\n")


@pytest.mark.parametrize("argv", [["--out"], ["--outt", "x"], ["--out", "a", "b"],
                                  ["--out", ""], ["--out", "--quick"]])
def test_native_coverage_refuses_a_bad_command_line(argv, capsys):
    assert _tool("native_coverage").main(argv) == 2
    assert "usage" in capsys.readouterr().err


# ------------------------------------------------------------------------ tools/diagram_cover.py

def test_diagram_cover_refuses_any_argument(capsys):
    assert _tool("diagram_cover").main(["--quick"]) == 2
    assert "usage" in capsys.readouterr().err


def test_diagram_cover_reads_the_tests_skipped_as_the_full_suites(tmp_path):
    xml = tmp_path / "report.xml"
    xml.write_text(
        '<testsuites><testsuite>'
        '<testcase classname="tests.test_x" name="test_a[wide-stacked 900]">'
        '<skipped message="the platform scope sweeps this at stacked 380 only"/></testcase>'
        '<testcase classname="tests.test_x" name="test_b"><skipped message="needs bash"/>'
        '</testcase><testcase classname="tests.test_x" name="test_c"/>'
        '</testsuite></testsuites>', encoding="utf-8")
    assert _tool("diagram_cover")._left_out(str(xml)) == ["tests/test_x.py::test_a[wide-stacked 900]"]


def _cover_run(monkeypatch, capsys, platform, rest, left=("tests/test_x.py::test_a",),
               status=(0, 0)):
    """diagram_cover's main with each pytest run stubbed: the platform scope's run reaching
    the arcs `platform`, the run of the tests it left out (`left`) reaching `rest` (arc ->
    the tests reaching it), with the exit statuses `status`. (main's code, its output.)"""
    dc = _tool("diagram_cover")
    runs = iter([(status[0], "platform", "x.xml"), (status[1], "rest", None)])
    monkeypatch.setattr(dc, "_run", lambda tmp, label, marker, targets: next(runs))
    monkeypatch.setattr(dc, "_left_out", lambda xml: list(left))
    monkeypatch.setattr(dc, "_arcs", lambda data: {
        "platform": {arc: {"tests/test_x.py::test_p|run"} for arc in platform},
        "rest": rest}[data])
    return dc.main([]), capsys.readouterr().out


_TD, _ID = "swi3s_studio/ui/timing_diagram.py", "swi3s_studio/ui/inequality_diagram.py"


def test_diagram_cover_names_an_arc_only_the_left_out_tests_reach(monkeypatch, capsys):
    """An arc of either diagram file only the left-out tests reach is named with its file;
    the same lines of the other file reached in the platform scope do not cover it."""
    code, out = _cover_run(monkeypatch, capsys, platform={(_TD, 1, 2), (_TD, 2, -1),
                                                          (_TD, 5, 6)},
                           rest={(_TD, 1, 2): {"tests/test_x.py::test_a|run"},
                                 (_ID, 5, 6): {"tests/test_x.py::test_a|run"}})
    assert code == 1 and f"{_ID} 5 -> 6 in" in out and "tests/test_x.py::test_a" in out, out
    assert "1 -> 2" not in out and "_COVER" in out and "_PLATFORM" in out


def test_diagram_cover_passes_when_the_platform_scope_reaches_every_arc(monkeypatch, capsys):
    code, out = _cover_run(monkeypatch, capsys, platform={(_TD, 1, 2), (_ID, 5, 6)},
                           rest={(_ID, 5, 6): {"tests/test_x.py::test_a|run"}})
    assert code == 0 and "0 of them not reached" in out, out


@pytest.mark.parametrize("left, status", [((), (0, 0)), (("t",), (1, 0)), (("t",), (0, 1))])
def test_diagram_cover_fails_a_failed_run_or_a_scope_that_leaves_nothing_out(
        monkeypatch, capsys, left, status):
    code, _out = _cover_run(monkeypatch, capsys, platform=set(), rest={}, left=left,
                            status=status)
    assert code == 1


# ------------------------------------------------------------------------ tools/changed_tests.py

def _changed_tests_in(tmp_path, monkeypatch, files):
    """changed_tests reading a synthetic checkout at `tmp_path` holding `files`."""
    ct = _tool("changed_tests")
    for rel, text in files.items():
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(text, encoding="utf-8")
    monkeypatch.setattr(ct, "_ROOT", str(tmp_path))
    return ct


def test_a_module_imported_by_name_from_the_package_maps_to_its_tests(tmp_path, monkeypatch):
    assert "tests/test_cli.py" in _tool("changed_tests").tests_for(["swi3s_studio/cli.py"])
    ct = _changed_tests_in(tmp_path, monkeypatch, {
        "tests/test_cli.py": "from swi3s_studio import cli\n",
        "tests/test_launch.py": "import swi3s_studio.app\n",
        "tests/test_exit.py": "def f():\n    from swi3s_studio.app import main\n",
        "tests/test_other.py": "x = 1\n"})
    assert ct.tests_for(["swi3s_studio/cli.py"]) == ["tests/test_cli.py"]
    assert ct.tests_for(["swi3s_studio/app.py"]) == ["tests/test_exit.py", "tests/test_launch.py"]
    assert ct.imported_modules("from swi3s_studio import cli, session\n") >= \
        {"swi3s_studio.cli", "swi3s_studio.session"}
    assert "swi3s_studio.model.registers" in ct.imported_modules(
        "from swi3s_studio.model import registers\n")


def test_a_module_the_suite_support_files_import_maps_to_the_tests_they_serve(tmp_path,
                                                                            monkeypatch):
    """A module a tests/ helper imports maps to the tests importing the helper (through
    another helper too); a module conftest.py imports maps to every test file."""
    ct = _changed_tests_in(tmp_path, monkeypatch, {
        "swi3s_studio/__init__.py": "",
        "swi3s_studio/model/__init__.py": "",
        "swi3s_studio/model/drawn.py": "",
        "swi3s_studio/model/styled.py": "",
        "swi3s_studio/model/other.py": "",
        "tests/conftest.py": "def fix():\n    from swi3s_studio.model import styled\n",
        "tests/helper.py": "from swi3s_studio.model.drawn import X\n",
        "tests/outer.py": "import helper\n",
        "tests/test_direct.py": "import helper\n",
        "tests/test_through.py": "import outer\n",
        "tests/test_alone.py": "x = 1\n"})
    assert ct.tests_for(["swi3s_studio/model/drawn.py"]) == [
        "tests/test_direct.py", "tests/test_through.py"]
    assert ct.tests_for(["swi3s_studio/model/styled.py"]) == [
        "tests/test_alone.py", "tests/test_direct.py", "tests/test_through.py"]
    assert ct.tests_for(["swi3s_studio/model/other.py"]) == []


def test_a_module_reached_through_other_package_modules_maps_to_their_tests(tmp_path,
                                                                           monkeypatch):
    """A package module no test imports maps to the tests of every package module that
    imports it, directly or through others, relative imports included."""
    ct = _changed_tests_in(tmp_path, monkeypatch, {
        "swi3s_studio/__init__.py": "",
        "swi3s_studio/base.py": "X = 1\n",
        "swi3s_studio/mid.py": "from .base import X\n",
        "swi3s_studio/sub/__init__.py": "",
        "swi3s_studio/sub/top.py": "from .. import mid\n",
        "swi3s_studio/alone.py": "Y = 2\n",
        "tests/test_mid.py": "from swi3s_studio import mid\n",
        "tests/test_top.py": "import swi3s_studio.sub.top\n",
        "tests/test_alone.py": "x = 1\n"})
    assert ct.tests_for(["swi3s_studio/base.py"]) == ["tests/test_mid.py", "tests/test_top.py"]
    assert ct.tests_for(["swi3s_studio/mid.py"]) == ["tests/test_mid.py", "tests/test_top.py"]
    assert ct.tests_for(["swi3s_studio/alone.py"]) == []


def test_run_fails_when_a_changed_package_or_native_file_has_no_test(tmp_path, monkeypatch,
                                                                    capsys):
    """--run exits 3, naming the file and the gate, for a changed swi3s_studio/ or native/
    file no test file covers; a docs-only change exits 0."""
    ct = _changed_tests_in(tmp_path, monkeypatch, {
        "swi3s_studio/__init__.py": "", "swi3s_studio/alone.py": "Y = 2\n",
        "tests/test_x.py": "x = 1\n"})
    monkeypatch.setattr(ct, "changed_files", lambda base: ["swi3s_studio/alone.py", "docs/a.md"])
    assert ct.main(["--run"]) == 3
    assert "no test file covers swi3s_studio/alone.py: run the gate" in capsys.readouterr().out
    assert ct.main([]) == 0
    monkeypatch.setattr(ct, "changed_files", lambda base: ["docs/a.md"])
    assert ct.main(["--run"]) == 0


def test_an_uncovered_package_file_exits_3_whatever_the_picked_tests_do(tmp_path, monkeypatch,
                                                                        capsys):
    """The status is 3 when a picked test fails as it is when the picked tests pass, and the
    output names the file."""
    import subprocess
    ct = _changed_tests_in(tmp_path, monkeypatch, {
        "swi3s_studio/__init__.py": "", "swi3s_studio/alone.py": "Y = 2\n",
        "swi3s_studio/session.py": "Z = 3\n", "tests/test_s.py": "import swi3s_studio.session\n"})
    monkeypatch.setattr(ct, "changed_files",
                        lambda base: ["swi3s_studio/alone.py", "swi3s_studio/session.py"])
    for status in (0, 1):
        monkeypatch.setattr(ct.subprocess, "run",
                            lambda cmd, status=status, **k: subprocess.CompletedProcess(cmd, status))
        assert ct.main(["--run"]) == 3, status
        assert "no test file covers swi3s_studio/alone.py" in capsys.readouterr().out


@pytest.mark.parametrize("tool", ["gate", "diagram_cover"])
def test_the_worker_count_variable_ignores_surrounding_blanks_and_non_ascii_digits(
        monkeypatch, tool):
    """`set SWI3S_TEST_WORKERS=4 && ...` in cmd.exe stores "4 ": it is 4 processes, as
    `-n " 4"` is, and a superscript or non-ASCII digit is not a count."""
    workers = _tool(tool)._workers
    monkeypatch.setattr(os, "cpu_count", lambda: 7)
    for value, expected in (("4", 4), ("4 ", 4), (" 4\t", 4), ("0", 1), ("", 7), ("four", 7),
                            ("\u00b2", 7), ("\u0664", 7)):
        monkeypatch.setenv("SWI3S_TEST_WORKERS", value)
        assert workers() == expected, repr(value)


def test_changed_tests_reads_the_worker_count_variable_without_surrounding_blanks():
    ct = _tool("changed_tests")
    assert ct._parse(["--run"], {"SWI3S_TEST_WORKERS": "4 "})[2] == "4"
    assert ct._parse(["--run"], {"SWI3S_TEST_WORKERS": "\u00b2"})[2] == "auto"


def test_run_gives_pytest_the_environment_the_gate_gives_it(tmp_path, monkeypatch):
    """The dev loop's pytest runs with the gate's variables: the Qt binding, UTF-8 text and
    the offscreen platform, whatever the caller's environment holds."""
    ct = _changed_tests_in(tmp_path, monkeypatch, {"tests/test_x.py": "x = 1\n"})
    monkeypatch.setattr(ct, "changed_files", lambda base: ["tests/test_x.py"])
    for name in ("PYQTGRAPH_QT_LIB", "PYTHONUTF8"):
        monkeypatch.setenv(name, "other")
    seen: list = []
    monkeypatch.setattr(ct.subprocess, "run", lambda cmd, **kw: seen.append(kw["env"]) or
                        subprocess.CompletedProcess(cmd, 0))
    assert ct.main(["--run"]) == 0
    gate_env = _tool("gate")._env()
    for name in ("PYTHONPATH", "QT_QPA_PLATFORM", "PYQTGRAPH_QT_LIB", "PYTHONUTF8"):
        assert seen[0][name] == gate_env[name], name


def test_a_changed_test_file_or_helper_maps_to_the_test_files_importing_it(tmp_path, monkeypatch):
    ct = _changed_tests_in(tmp_path, monkeypatch, {
        "tests/test_run.py": "x = 1\n",
        "tests/test_reseed.py": "from test_run import x\n",
        "tests/test_vectors.py": "y = 2\n",
        "tests/test_parser.py": "import test_vectors\n",
        "tests/diagram_support.py": "z = 3\n",
        "tests/test_diagram.py": "from diagram_support import z\n",
        "tests/test_unrelated.py": "w = 4\n"})
    assert ct.tests_for(["tests/test_run.py"]) == ["tests/test_reseed.py", "tests/test_run.py"]
    assert ct.tests_for(["tests/test_vectors.py"]) == ["tests/test_parser.py",
                                                       "tests/test_vectors.py"]
    assert ct.tests_for(["tests/diagram_support.py"]) == ["tests/test_diagram.py"]


def test_a_changed_tool_script_data_file_or_launcher_maps_to_the_tests_naming_it(
        tmp_path, monkeypatch):
    """A tools/ file of any kind, and a tests/ shell script, runs the test files that name it;
    a tools/ data file also runs the tests of each tools/ script that names it."""
    ct = _changed_tests_in(tmp_path, monkeypatch, {
        "tools/publish.py": "_SYNC = 'tools/sync_points.txt'\n",
        "tools/setup.sh": "echo\n",
        "tests/gate.sh": "echo\n",
        "tests/test_publish.py": "load('publish.py')\n",
        "tests/test_setup.py": "read('tools', 'setup.sh')\n",
        "tests/test_wrappers.py": "read('tests/gate.sh')\n",
        "tests/test_unrelated.py": "x = 1\n"})
    assert ct.tests_for(["tools/publish.py"]) == ["tests/test_publish.py"]
    assert ct.tests_for(["tools/sync_points.txt"]) == ["tests/test_publish.py"]
    assert ct.tests_for(["tools/setup.sh"]) == ["tests/test_setup.py"]
    assert ct.tests_for(["tests/gate.sh"]) == ["tests/test_wrappers.py"]
    assert ct.tests_for(["docs/guide.md"]) == []


def test_a_changed_fixture_or_data_file_maps_to_the_tests_naming_it_or_its_reader(
        tmp_path, monkeypatch):
    """A file under tests/ that is not a top-level test file or shell script, and a data/
    file, runs the test files naming its file name or its path below tests/; a data/ file
    also runs the tests importing a swi3s_studio module that names it."""
    ct = _changed_tests_in(tmp_path, monkeypatch, {
        "tests/fixtures/reference.json": "{}\n",
        "tests/visualizer/golden/fig.json": "{}\n",
        "data/regs.json": "{}\n",
        "swi3s_studio/__init__.py": "",
        "swi3s_studio/model/__init__.py": "",
        "swi3s_studio/model/regs.py": "_FILE = 'regs.json'\n",
        "tests/test_reference.py": "open('fixtures/reference.json')\n",
        "tests/test_golden.py": "GOLDEN = 'visualizer/golden'\nname = 'fig.json'\n",
        "tests/test_regs.py": "from swi3s_studio.model import regs\n",
        "tests/test_unrelated.py": "x = 1\n"})
    assert ct.tests_for(["tests/fixtures/reference.json"]) == ["tests/test_reference.py"]
    assert ct.tests_for(["tests/visualizer/golden/fig.json"]) == ["tests/test_golden.py"]
    assert ct.tests_for(["data/regs.json"]) == ["tests/test_regs.py"]
    assert ct.tests_for(["data/other.json"]) == []


def test_any_other_changed_file_outside_docs_maps_to_the_tests_naming_it(
        tmp_path, monkeypatch, capsys):
    """Example data, package data, workflow files and top-level files run the test files
    naming their file name or path; docs/ and a file no test names run nothing and are listed
    as not mapped."""
    ct = _changed_tests_in(tmp_path, monkeypatch, {
        "tests/test_examples.py": "open('visualizer_examples/spec/x.csv')\n",
        "tests/test_assets.py": "name = 'check.svg'\n",
        "tests/test_ci.py": "read('.github/workflows/ci.yml')\n",
        "tests/test_readme.py": "read('README.md')\n",
        "tests/test_unrelated.py": "x = 1\n"})
    assert ct.tests_for(["visualizer_examples/spec/x.csv"]) == ["tests/test_examples.py"]
    assert ct.tests_for(["swi3s_studio/ui/assets/check.svg"]) == ["tests/test_assets.py"]
    assert ct.tests_for([".github/workflows/ci.yml"]) == ["tests/test_ci.py"]
    assert ct.tests_for(["README.md"]) == ["tests/test_readme.py"]
    assert ct.tests_for(["docs/README.md"]) == []
    monkeypatch.setattr(ct, "changed_files", lambda base: ["docs/a.md", "other.txt"])
    assert ct.main([]) == 0
    assert "not mapped (no test file selected): docs/a.md, other.txt" in capsys.readouterr().out


def test_the_importing_tests_follow_a_chain_and_every_spelling_of_an_import():
    ct = _tool("changed_tests")
    text = {"tests/test_a.py": "import helper\n",
            "tests/test_b.py": "def f():\n    from test_a import x\n",
            "tests/test_c.py": "from tests import helper, other\n",
            "tests/test_d.py": "from tests.test_b import y\n",
            "tests/test_e.py": "import helper_two\n"}
    assert ct.importing_tests({"helper"}, text) == {
        "tests/test_a.py", "tests/test_b.py", "tests/test_c.py", "tests/test_d.py"}
    assert ct.importing_tests({"nothing"}, text) == set()


def _pytest_workers(ct, monkeypatch, argv, env=None):
    """The `-n` value `ct.main(argv)` hands pytest, with $SWI3S_TEST_WORKERS set to `env` (unset
    for None); the exit status when it refuses the command line."""
    monkeypatch.delenv("SWI3S_TEST_WORKERS", raising=False)
    if env is not None:
        monkeypatch.setenv("SWI3S_TEST_WORKERS", env)
    seen: list = []
    monkeypatch.setattr(ct.subprocess, "run", lambda cmd, **kw: seen.append(cmd) or
                        subprocess.CompletedProcess(cmd, 0))
    code = ct.main(argv)
    if not seen:
        return code
    cmd = seen[0]
    assert "tests/test_x.py" in cmd and cmd[cmd.index("--dist") + 1] == "loadfile"
    return cmd[cmd.index("-n") + 1]


@pytest.mark.parametrize("argv, env, want", [
    (["--run"], None, "auto"),
    (["--run"], "3", "3"),
    (["--run", "-n", "4"], None, "4"),
    (["--run", "--workers", "2"], None, "2"),
    (["--run", "--workers=5"], None, "5"),
    (["--run", "-n", "4"], "8", "4"),              # the option wins over the variable
    (["--run"], "0", "1"),                         # as the gate reads it
    (["--run"], "four", "auto"), (["--run"], "", "auto"),   # not a number: one per CPU
    (["-n", "6", "--run", "--base", "main"], None, "6"),
])
def test_run_caps_the_test_processes_at_the_option_else_the_variable_else_one_per_cpu(
        tmp_path, monkeypatch, argv, env, want):
    ct = _changed_tests_in(tmp_path, monkeypatch, {
        "tests/test_x.py": "x = 1\n", "swi3s_studio/__init__.py": ""})
    monkeypatch.setattr(ct, "changed_files", lambda base: ["tests/test_x.py"])
    assert _pytest_workers(ct, monkeypatch, argv, env) == want


@pytest.mark.parametrize("argv, env", [
    (["--run", "-n"], None), (["--run", "-n", "0"], None), (["--run", "-n", "many"], None),
    (["--run", "-n", "-2"], None), (["--run", "--workers="], None),
    (["--run", "--workers", "2.5"], None), (["--run", "-n", "\u0664"], None),
    (["--run", "-n", "x"], "4"),
])
def test_run_refuses_an_option_worker_count_that_is_not_a_whole_number_of_at_least_one(
        tmp_path, monkeypatch, capsys, argv, env):
    """Each is a usage error (exit 2, nothing run). The groups:
    - no value after `-n`, or an empty `--workers=`;
    - a value below 1 (`0`, `-2`);
    - a value that is not an integer (`many`, `2.5`, `x`);
    - a non-ASCII decimal digit, which is not a whole number written in ASCII (the ASCII
      check refuses it; `int()` alone reads it as 4);
    - a bad option value with a valid $SWI3S_TEST_WORKERS set: the variable never rescues it.
    """
    ct = _changed_tests_in(tmp_path, monkeypatch, {"tests/test_x.py": "x = 1\n"})
    monkeypatch.setattr(ct, "changed_files", lambda base: ["tests/test_x.py"])
    assert _pytest_workers(ct, monkeypatch, argv, env) == 2
    assert "usage" in capsys.readouterr().err


@pytest.mark.parametrize("argv", [["--base"], ["--base", "--run"], ["--bse", "main"], ["x"]])
def test_changed_tests_refuses_a_bad_command_line(argv, capsys):
    assert _tool("changed_tests").main(argv) == 2
    assert "usage" in capsys.readouterr().err


# ------------------------------------------------------------- tools/csv_converter/convert_174.py

@pytest.mark.parametrize("directory", [True, False])
def test_a_legacy_csv_that_cannot_be_converted_is_named_and_the_rest_are_converted(
        tmp_path, capsys, directory):
    conv = _tool(os.path.join("csv_converter", "convert_174"))
    src = tmp_path / "in"
    src.mkdir()
    (src / "a_bad.csv").write_text("Row Rate [kHz],abc\n", encoding="utf-8")
    (src / "b_ok.csv").write_text("Columns per Row,24\nRow Rate [kHz],3072\n",
                                  encoding="utf-8")
    argv = [str(src)] if directory else [str(src / "a_bad.csv")]
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(sys, "argv", ["convert_174.py", *argv])
        assert conv.main() == 1
    err = capsys.readouterr().err
    assert "error: a_bad.csv: Row Rate [kHz] is 'abc', not a number" in err
    assert "Traceback" not in err
    if directory:
        assert sorted(os.listdir(tmp_path / "in_converted")) == ["b_ok_converted.csv"]
    else:
        assert not (src / "a_bad_converted.csv").exists()


def _convert(conv, capsys, *argv):
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(sys, "argv", ["convert_174.py", *argv])
        code = conv.main()
    return code, capsys.readouterr()


def test_the_converter_names_a_file_once_and_reports_each_failure_as_an_error_line(
        tmp_path, capsys):
    conv = _tool(os.path.join("csv_converter", "convert_174"))
    binary = tmp_path / "bin.csv"
    binary.write_bytes(b"\xff\xfe\x00\x80\x81")
    code, got = _convert(conv, capsys, str(binary))
    assert code == 1 and got.err.count("bin.csv") == 1 and got.err.startswith("error: bin.csv: ")
    ok = tmp_path / "ok.csv"
    ok.write_text("Columns per Row,24\n", encoding="utf-8")
    code, got = _convert(conv, capsys, str(ok), "-o", str(ok / "x" / "y.csv"))
    assert code == 1 and "error:" in got.err and "Traceback" not in got.err
    code, got = _convert(conv, capsys, str(tmp_path), "-o", str(ok / "d"))
    assert code == 1 and "error:" in got.err


def test_the_converter_warns_about_a_file_with_no_field_and_reads_an_upper_case_extension(
        tmp_path, capsys):
    conv = _tool(os.path.join("csv_converter", "convert_174"))
    (tmp_path / "empty.csv").write_text("", encoding="utf-8")
    code, got = _convert(conv, capsys, str(tmp_path / "empty.csv"))
    assert code == 0 and "warning: empty.csv: holds no legacy field" in got.err
    folder = tmp_path / "caps"
    folder.mkdir()
    (folder / "A.CSV").write_text("Columns per Row,24\n", encoding="utf-8")
    code, got = _convert(conv, capsys, str(folder))
    assert code == 0 and (tmp_path / "caps_converted" / "A_converted.csv").exists()


# --------------------------------------------------------------------------- tools/name_review.py

def _name_review(monkeypatch):
    """name_review with an empty baseline, so a test runs the same in a tree that is not a
    git checkout."""
    nr = _tool("name_review")
    monkeypatch.setattr(nr, "_read_rev", lambda rev, cwd=None: (set(), {}))
    return nr


@pytest.mark.parametrize("make_tree", [lambda p: str(p / "absent"), lambda p: str(p)])
def test_name_review_does_not_approve_a_tree_that_is_missing_or_empty(tmp_path, capsys, monkeypatch,
                                                                     make_tree):
    nr = _name_review(monkeypatch)
    assert nr.main([f"--tree={make_tree(tmp_path)}", "--baseline=HEAD"]) == 2
    assert "no names new" not in capsys.readouterr().out


# --------------------------------------------------------- launchers, spec and dependency files

def test_run_ps1_stops_when_an_install_step_fails_and_does_not_stamp():
    ps = _text("run.ps1")
    deps = ps[ps.index("pip install -r requirements.txt"):]
    assert deps.index("$LASTEXITCODE -ne 0") < deps.index("New-Item -ItemType File -Path $stamp")
    venv = ps[ps.index("-m venv"):]
    assert venv.index("$LASTEXITCODE -ne 0") < venv.index("$python = ")


def test_setup_dev_ps1_stops_when_the_virtual_environment_is_not_created():
    ps = _text("tools/setup_dev.ps1")
    assert ps.index("function Invoke-Checked") < ps.index("-m venv")
    venv = ps[ps.index("-m venv"):]
    assert venv.index('Invoke-Checked "creating the virtual environment"') < \
        venv.index("$python = ") < venv.index("Test-Path $python") < venv.index("& $python")


def test_run_ps1_does_not_send_users_to_the_debug_binaries_for_headers():
    assert "debug binaries" not in _text("run.ps1")


def test_the_bundle_carries_the_licences_and_a_real_identifier():
    spec = _text("swi3s-studio.spec")
    assert '"LICENSE.md"' in spec and "_notice_datas()" in spec
    assert "com.example" not in spec and "collect_dynamic_libs" not in spec


# Library and plugin paths as macOS, Windows and Linux bundles name them: those the spec leaves
# out, and those the app or pyqtgraph loads.
_UNSHIPPED_QT_PATHS = (
    "PySide6/Qt/lib/QtVirtualKeyboard.framework/QtVirtualKeyboard",
    "PySide6/Qt/lib/QtQuick.framework/QtQuick", "PySide6/Qt/lib/libQt6Qml.so.6",
    "PySide6/Qt6Pdf.dll", "PySide6/Qt/plugins/imageformats/libqpdf.dylib",
    "PySide6/plugins/imageformats/qpdf.dll",
    "PySide6/Qt/plugins/platforminputcontexts/libqtvirtualkeyboardplugin.dylib",
    "PySide6/Qt/plugins/generic/libqtuiotouchplugin.dylib")
_SHIPPED_QT_PATHS = tuple(f"PySide6/Qt/lib/Qt{m}.framework/Qt{m}" for m in (
    "Core", "Gui", "Widgets", "Svg", "Multimedia", "Network", "OpenGL", "OpenGLWidgets",
    "Test", "DBus")) + ("Qt6Core.dll", "libQt6XcbQpa.so.6", "plugins/platforms/libqcocoa.dylib",
                        "plugins/multimedia/libffmpegmediaplugin.dylib", "libavcodec.61.dylib")


def test_the_bundle_leaves_out_the_qt_modules_the_app_does_not_load():
    """`_UNSHIPPED_QT` names the virtual-keyboard and PDF plugins and QtVirtualKeyboard,
    QtQuick, QtQml and QtPdf, and none of the Qt modules the app or pyqtgraph loads; the spec
    applies it to the binaries and the data."""
    import re
    spec = _text("swi3s-studio.spec")
    m = re.search(r"_UNSHIPPED_QT = re\.compile\(\s*(r\"[^\"]+\")\)", spec)
    assert m, "the spec's _UNSHIPPED_QT was not found"
    pattern = re.compile(eval(m.group(1)))
    assert all(pattern.search(p) for p in _UNSHIPPED_QT_PATHS)
    assert not [p for p in _SHIPPED_QT_PATHS if pattern.search(p)]
    assert "a.binaries = [b for b in a.binaries if not _UNSHIPPED_QT.search(" in spec
    assert "a.datas = [d for d in a.datas if not _UNSHIPPED_QT.search(" in spec


def _spec_notices():
    """The spec's notice collection, as a namespace holding its functions."""
    src = _text("swi3s-studio.spec")
    ns = {"HERE": _ROOT}
    exec("import importlib.metadata, os, re\n"
         + src[src.index("_REQUIREMENTS ="):src.index("datas += _notice_datas()")], ns)
    return ns


_QT_LICENCE = "LGPL-3.0-only OR GPL-2.0-only OR GPL-3.0-only"


class _Wheel:
    """A distribution as the spec reads it: its name, version, licence statement, and the
    files its metadata lists."""

    def __init__(self, name, files=(), version="6.11.1", licence=_QT_LICENCE):
        self.metadata = {"Name": name, "License": licence}
        self.version = version
        self.files = list(files)
        self.requires = []


# The wheels each PySide6 release installs, none of which carries a notice file, and the
# version of each release that the notice was reviewed for.
_QT_WHEELS = {"6.11": ("PySide6", "PySide6_Essentials", "PySide6_Addons", "shiboken6"),
              "6.12": ("PySide6", "PySide6_Essentials", "PySide6_Addons", "shiboken6",
                       "PySide6_WebEngine", "PySide6_Pdf")}
_REVIEWED = {"6.11": "6.11.1"}


def _shipped_paths(shipped):
    return [os.path.join(dest, os.path.basename(src)) for src, dest in shipped
            if os.path.isfile(src)]


def test_the_bundle_ships_every_notice_file_of_the_runtime_distributions_under_its_own_path():
    ns = _spec_notices()
    ns["_runtime_closure"] = lambda: ([importlib.metadata.distribution("numpy")]
                                      + [_Wheel(n) for n in _QT_WHEELS["6.11"]])
    shipped = ns["_notice_datas"]()
    sources = [s for s, _dest in shipped]
    assert len(set(sources)) == len(sources)
    # No two notices land on one path: a distribution that bundles the licences of several
    # components carries same-named files, each of which ships.
    paths = _shipped_paths(shipped)
    assert len(set(paths)) == len(paths)
    numpy_files = [p for p in paths if p.startswith(os.path.join("licenses", "numpy", ""))]
    metadata = [f for f in importlib.metadata.distribution("numpy").files
                if ns["_NOTICE_FILE"].search(str(f))
                and f.suffix.lower() in ("", ".txt", ".md", ".rst")]
    assert len(numpy_files) == len(metadata) >= 1


class _Unpacked(_Wheel):
    """A distribution whose files are on disk under `root`."""

    def __init__(self, name, root, files):
        super().__init__(name, [pathlib.PurePosixPath(f) for f in files])
        self.root = root

    def locate_file(self, f):
        return self.root / f


def test_same_named_notice_files_of_one_distribution_each_ship_under_their_own_path(tmp_path):
    files = ["pkg-1.0.dist-info/licenses/LICENSE.txt", "pkg/_vendor/a/LICENSE.txt",
             "pkg/_vendor/b/LICENSE.txt", "pkg/_vendor/b/NOTICE", "pkg/module.py",
             "pkg/licenses/logo.png"]
    for rel in files:
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text("x", encoding="utf-8")
    ns = _spec_notices()
    ns["_runtime_closure"] = lambda: [_Unpacked("pkg", tmp_path, files)]
    ns["_imports_qt_multimedia"] = lambda: False
    ns["_python_licence"] = lambda: str(tmp_path / files[0])
    paths = sorted(os.path.normpath(p) for p in _shipped_paths(ns["_notice_datas"]()))
    assert paths == sorted([os.path.join("licenses", "pkg", *pathlib.PurePosixPath(f).parts)
                            for f in files[:4]] + [os.path.join("licenses", "Python",
                                                                "LICENSE.txt")])


@pytest.mark.parametrize("release", sorted(_REVIEWED))
def test_the_bundle_ships_the_notices_the_qt_for_python_wheels_do_not_carry(release):
    ns = _spec_notices()
    ns["_runtime_closure"] = lambda: [_Wheel(n, version=_REVIEWED[release])
                                      for n in _QT_WHEELS[release]]
    shipped = ns["_notice_datas"]()
    assert (os.path.join(_ROOT, "data", "licenses", "Qt-for-Python"),
            os.path.join("licenses", "Qt-for-Python")) in shipped
    assert [s for s, d in shipped if d not in (os.path.join("licenses", "FFmpeg"),
                                                os.path.join("licenses", "Python"))] == [
        os.path.join(_ROOT, "data", "licenses", "Qt-for-Python")]
    names = set(os.listdir(os.path.join(_ROOT, "data", "licenses", "Qt-for-Python")))
    assert {"NOTICE.txt", "LGPL-3.0.txt", "GPL-3.0.txt", "THIRD-PARTY.txt"} <= names


@pytest.mark.parametrize("release", sorted(_REVIEWED))
def test_the_bundle_ships_the_ffmpeg_notice_and_lgpl_2_1_text_with_qt_multimedia(release):
    ns = _spec_notices()
    assert ns["_imports_qt_multimedia"]()
    ns["_runtime_closure"] = lambda: [_Wheel(n, version=_REVIEWED[release])
                                      for n in _QT_WHEELS[release]]
    shipped = ns["_notice_datas"]()
    assert (os.path.join(_ROOT, "data", "licenses", "FFmpeg"),
            os.path.join("licenses", "FFmpeg")) in shipped
    held = os.path.join(_ROOT, "data", "licenses", "FFmpeg")
    assert {"NOTICE.txt", "LGPL-2.1.txt"} <= set(os.listdir(held))
    with open(os.path.join(held, "LGPL-2.1.txt"), encoding="utf-8") as f:
        assert "Version 2.1, February 1999" in f.read()


def test_the_bundle_ships_no_ffmpeg_notice_without_a_qt_multimedia_import(tmp_path):
    ns = _spec_notices()
    package = tmp_path / "swi3s_studio"
    package.mkdir()
    (package / "view.py").write_text("from PySide6.QtCore import QObject\n", encoding="utf-8")
    assert not ns["_imports_qt_multimedia"](str(tmp_path))
    (package / "audio.py").write_text("try:\n    from PySide6.QtMultimedia import QAudio\n"
                                      "except ImportError:\n    pass\n", encoding="utf-8")
    assert ns["_imports_qt_multimedia"](str(tmp_path))


def test_the_installed_runtime_closure_has_a_notice_for_every_distribution():
    """The installed environment's notices; its Qt for Python release is a development one,
    which tools/bundle_constraints.txt pins for a bundle."""
    ns = _spec_notices()
    ns["_unreviewed_qt"] = lambda dists: []
    shipped = ns["_notice_datas"]()
    paths = _shipped_paths(shipped)
    assert len(set(paths)) == len(paths)
    assert (os.path.join(_ROOT, "data", "licenses", "Qt-for-Python"),
            os.path.join("licenses", "Qt-for-Python")) in shipped


def test_the_bundle_ships_the_licence_of_the_interpreter_it_freezes():
    ns = _spec_notices()
    licence = ns["_python_licence"]()
    assert licence is not None and os.path.isfile(licence)
    with open(licence, encoding="utf-8", errors="replace") as f:
        assert "PYTHON SOFTWARE FOUNDATION LICENSE" in f.read()
    ns["_runtime_closure"] = lambda: []
    ns["_imports_qt_multimedia"] = lambda: False
    assert ns["_notice_datas"]() == [(licence, os.path.join("licenses", "Python"))]
    ns["_python_licence"] = lambda: None
    with pytest.raises(SystemExit, match="Python"):
        ns["_notice_datas"]()


def test_a_runtime_distribution_with_no_notice_file_stops_the_bundle_build():
    ns = _spec_notices()
    ns["_runtime_closure"] = lambda: [_Wheel("bare-distribution")]
    with pytest.raises(SystemExit, match="bare-distribution"):
        ns["_notice_datas"]()


def test_a_runtime_requirement_that_is_not_installed_stops_the_bundle_build():
    ns = _spec_notices()
    ns["_RUNTIME"] = ("numpy", "no-such-distribution-here")
    with pytest.raises(SystemExit, match="no-such-distribution-here"):
        ns["_runtime_closure"]()


def test_the_bundle_takes_its_runtime_requirements_from_requirements_txt(tmp_path):
    import tomllib
    ns = _spec_notices()
    listed = tmp_path / "requirements.txt"
    listed.write_text("# runtime\n-r other.txt\nnumpy>=1.24  # data\n\n"
                      "pkg-a[extra]>=2; sys_platform == 'win32'\nPkg_B~=1.0\nplain\n",
                      encoding="utf-8")
    assert ns["_runtime_requirements"](str(listed)) == ("numpy", "pkg-a", "Pkg_B", "plain")
    canon = ns["_canon"]
    project = tomllib.loads(_text("pyproject.toml"))["project"]["dependencies"]
    declared = {canon(re.split(r"[\s;<>=!~(\[]", d, maxsplit=1)[0]) for d in project}
    assert {canon(n) for n in ns["_RUNTIME"]} == declared


def test_pybind11_is_a_build_requirement_with_one_floor():
    runtime = _text("requirements.txt")
    dev = _text("requirements-dev.txt")
    toml = _text("native/pyproject.toml")
    assert "pybind11" not in runtime
    floor = re.search(r"pybind11>=([\d.]+)", toml).group(1)
    assert f"pybind11>={floor}" in dev


def test_a_split_register_field_is_excess_one_on_its_least_significant_part_only():
    import json
    blocks = json.loads(_text("data/registers.json"))["blocks"]
    flags = {}

    def walk(node):
        if isinstance(node, dict):
            if "bits" in node and "name" in node and re.search(r"\[\d+:\d+\]$", node["name"]):
                flags[node["name"]] = bool(node.get("excess1"))
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(blocks)
    assert flags, "no split register fields were read"
    high = sorted(n for n, on in flags.items() if on and not re.search(r"\[\d+:0\]$", n))
    assert not high, f"excess1 on a part above the least significant one: {high}"
    assert flags["Interval[3:0]"] and not flags["Interval[11:4]"]
    assert flags["ConverterClockDiv[7:0]"] and not flags["ConverterClockDiv[15:8]"]


def test_the_setup_scripts_name_the_commit_gate_for_the_command_they_print():
    for script in ("tools/setup_dev.sh", "tools/setup_dev.ps1"):
        text = _text(script)
        assert "release gate" not in text, script
        assert "commit gate" in text and "--release" in text, script


# The work each tool does once its command line is read.
_TOOL_WORK = {"screenshots": ("render",), "test_report": ("_run_suite", "report"),
              "package_smoke": ("export_source",), "mypy_gate": ("_run_mypy",)}


def _refusing_tool(name, monkeypatch):
    """The tool, with the work it does after reading its command line replaced by a stub
    that fails the test."""
    mod = _tool(name)

    def ran(*_a, **_k):
        raise AssertionError(f"{name} ran its work on a command line it should refuse")

    for work in _TOOL_WORK[name]:
        monkeypatch.setattr(mod, work, ran)
    return mod


@pytest.mark.parametrize("tool, argv", [("screenshots", ["--out"]), ("test_report", ["--data"]),
                                        ("screenshots", ["--out", ""]),
                                        ("test_report", ["--data", ""])])
def test_an_option_that_needs_a_value_is_refused_without_one(tool, argv, capsys, monkeypatch):
    assert _refusing_tool(tool, monkeypatch).main(argv) == 2
    captured = capsys.readouterr()
    assert "usage" in captured.err and captured.out == ""


@pytest.mark.parametrize("tool, argv", [
    ("screenshots", ["--ouput", "x"]), ("screenshots", ["--out", "x", "extra"]),
    ("screenshots", ["--out", "--verbose"]), ("test_report", ["--dta", "f.cov"]),
    ("test_report", ["--data", "f.cov", "extra"]), ("test_report", ["--data", "--run"]),
    ("package_smoke", ["--help"]), ("package_smoke", ["x"]), ("mypy_gate", ["--updat"]),
    ("mypy_gate", ["--update", "stray"]), ("mypy_gate", ["--allow-incomparable", "-h"])])
def test_an_option_the_tool_does_not_have_is_refused_before_anything_runs(tool, argv, capsys,
                                                                          monkeypatch):
    assert _refusing_tool(tool, monkeypatch).main(argv) == 2
    assert "usage" in capsys.readouterr().err


def test_test_report_names_a_data_file_that_does_not_exist_and_creates_none(tmp_path, capsys,
                                                                            monkeypatch):
    missing = tmp_path / "absent.cov"
    assert _refusing_tool("test_report", monkeypatch).main(["--data", str(missing)]) == 2
    assert not missing.exists()
    assert "no such file" in capsys.readouterr().err


@pytest.mark.parametrize("kind", ["not sqlite", "lines only"])
def test_test_report_refuses_a_data_file_without_per_test_branch_data(tmp_path, capsys,
                                                                      monkeypatch, kind):
    import coverage
    data = tmp_path / "data.cov"
    if kind == "not sqlite":
        data.write_text("not a database\n" * 100, encoding="utf-8")
    else:                                            # a plain `pytest --cov` run writes this
        lines = coverage.CoverageData(basename=str(data))
        lines.add_lines({str(tmp_path / "m.py"): [1, 2]})
        lines.write()
    assert _refusing_tool("test_report", monkeypatch).main(["--data", str(data)]) == 2
    err = capsys.readouterr().err
    assert ("is not a coverage data file" if kind == "not sqlite"
            else "holds no per-test branch data") in err, err
    assert "run without --data" in err


def test_test_report_closes_its_database_when_a_query_fails(tmp_path, monkeypatch):
    import sqlite3
    tr = _tool("test_report")
    data = tmp_path / "not-coverage.sqlite"
    sqlite3.connect(data).close()                    # a database with none of coverage's tables
    opened = []
    real = sqlite3.connect

    def connect(*a, **k):
        opened.append(real(*a, **k))
        return opened[-1]

    monkeypatch.setattr(tr.sqlite3, "connect", connect)
    with pytest.raises(sqlite3.OperationalError):
        tr._per_test_lines(str(data))
    with pytest.raises(sqlite3.ProgrammingError):    # a closed connection refuses a query
        opened[0].execute("select 1")


def test_a_screenshot_that_cannot_be_written_is_a_finding(tmp_path, monkeypatch):
    sh = _tool("screenshots")

    class Image:
        def save(self, path):
            return False

    class Window:
        def grab(self):
            return type("Pixmap", (), {"toImage": lambda self: Image()})()

    monkeypatch.setattr(sh, "check_window", lambda win, img, theme: ([], "ok"))
    monkeypatch.setattr(sh, "widget_tones", lambda win, img: {})
    path, found, _summary, _tones = sh.render_state(Window(), "light", "Mode_start-light",
                                                     str(tmp_path))
    assert found == [f"could not write {path}"]


def test_the_contact_sheet_orders_by_the_file_name_not_by_the_directory():
    order = _tool("screenshots")._sheet_order
    states = ["start-light", "start-dark", "switched-to-dark", "switched-to-light"]
    for folder in ("/tmp/run", "/tmp/start-dark-run", "/tmp/switched-to-light/start-dark"):
        names = [f"Mode_{s}.png" for s in states]
        ranked = sorted(reversed(names), key=lambda n: order(f"{folder}/{n}"))
        assert ranked == names, folder
    assert order("/o/Alpha_start-dark.png")[0] != order("/o/Beta_start-dark.png")[0]


def test_the_powershell_launcher_opens_no_window_for_the_same_spellings_as_the_shell_one():
    import fnmatch
    sh = _text("run.sh")
    ps = _text("run.ps1")
    sh_flags = set()
    for arm in re.findall(r"^\s*([^\n)#]+)\)\s*headless=1", sh, re.M):
        sh_flags |= {f.strip() for f in arm.split("|")}
    headless = ps.split("$headless =")[1].split(".Count")[0]
    assert not re.search(r"-(?:eq|like)\b", headless), "run.ps1 compares a flag case-insensitively"
    ps_flags = set(re.findall(r'-c(?:eq|like)\s+"([^"]+)"', headless))
    spellings = ["-o", "-oFILE", "-o=m.json", "--o", "--ou", "--out", "--outp", "--outpu",
                 "--output", "--out=m.json", "--output=m.json", "-h", "--h", "--he", "--hel",
                 "--help"]
    for arg in spellings:
        assert any(fnmatch.fnmatchcase(arg, p) for p in sh_flags), f"run.sh: {arg}"
        assert any(fnmatch.fnmatchcase(arg, p) for p in ps_flags), f"run.ps1: {arg}"


def test_the_bundle_spec_ships_the_example_register_map():
    assert '"data", "regmaps"' in _text("swi3s-studio.spec")


def test_a_base_ref_that_does_not_resolve_is_an_error_not_a_smaller_list(capsys):
    assert _tool("changed_tests").main(["--base", "no-such-ref-for-this-test"]) == 2
    captured = capsys.readouterr()
    assert "changed_tests:" in captured.err and "changed path" not in captured.out


def test_a_test_file_that_outlives_its_time_limit_is_a_failure_with_a_name(monkeypatch):
    g = _tool("gate")
    seen = {}

    def hang(cmd, timeout):
        seen["timeout"] = timeout
        raise subprocess.TimeoutExpired(cmd, timeout, output="partial output")

    monkeypatch.setattr(g, "_run_bounded", hang)
    rel, done, _took = g._run_file("tests/test_x.py", "not perf", "unused-basetemp")
    assert seen["timeout"] == g._FILE_TIMEOUT > 0
    assert rel == "tests/test_x.py" and done.returncode == g._TIMED_OUT
    assert "timed out" in done.stderr and "partial output" in done.stdout


def _gone(pid):
    """True when process `pid` no longer runs: it does not exist, or it is a zombie no init
    process has reaped yet (Linux, read from /proc). Windows asks the task list, since
    os.kill there terminates the process it names."""
    if sys.platform == "win32":
        listed = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"],
                                capture_output=True, text=True)
        return f'"{pid}"' not in listed.stdout
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:                  # the number now names another user's process
        return True
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8") as f:
            return f.read().rsplit(")", 1)[1].split()[0] == "Z"
    except (OSError, IndexError):
        return False


# Seconds the child of the kill test has to start an interpreter, start its own child and
# say so, on a machine running the whole suite at once.
_START_BUDGET = 10


def test_a_timed_out_file_is_killed_with_every_process_it_started(tmp_path):
    g = _tool("gate")
    pid_file = tmp_path / "grandchild.pid"
    # A child that starts a grandchild holding the same output pipes, then both sleep.
    script = ("import os, subprocess, sys, time\n"
              "c = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
              f"open({str(pid_file)!r}, 'w').write(str(c.pid))\n"
              "print('started', flush=True)\n"
              "time.sleep(120)\n")
    with pytest.raises(subprocess.TimeoutExpired) as raised:
        g._run_bounded([sys.executable, "-c", script], timeout=_START_BUDGET)
    assert "started" in raised.value.stdout
    grandchild = int(pid_file.read_text())
    deadline = time.monotonic() + 10
    while not _gone(grandchild):
        if time.monotonic() > deadline:
            pytest.fail("the grandchild outlived the timeout")
        time.sleep(0.05)


def test_a_check_whose_process_outlives_the_stage_limit_is_a_named_failure(monkeypatch,
                                                                            capsys):
    """A process a check runs past `_STAGE_TIMEOUT` is killed and the check is named as
    failed, and the gate goes on to the next check; within the limit, `_run` returns the
    process's status and captured output."""
    g = _tool("gate")
    ran = []
    hang = [sys.executable, "-c", "import time; time.sleep(120)"]
    monkeypatch.setattr(g, "_STAGE_TIMEOUT", 2)
    monkeypatch.setattr(g, "_CHECKS", {
        "hung": (lambda: g._run(hang), ("commit",), False),
        "after": (lambda: ran.append("after"), ("commit",), False)})
    monkeypatch.setattr(sys, "argv", ["tools/gate.py"])
    g._FAILED[:] = []
    g._NOT_RUN[:] = []
    assert g.main() == 1
    out = capsys.readouterr().out
    assert "    - hung: timed out after 2 s" in out and ran == ["after"], out
    done = g._run([sys.executable, "-c", "import sys; print('done'); sys.exit(3)"],
                  capture_output=True, encoding="utf-8", errors="replace")
    assert done.returncode == 3 and done.stdout == "done\n"


def test_an_interrupted_suite_kills_the_files_still_running(monkeypatch):
    g = _tool("gate")
    running, killed = object(), []

    def interrupted(rel, marker, basetemp):
        raise KeyboardInterrupt

    monkeypatch.setattr(g, "_suite_files", lambda guest: [("tests/test_x.py", "not perf")])
    monkeypatch.setattr(g, "_run_file", interrupted)
    monkeypatch.setattr(g, "_LIVE", {running})
    monkeypatch.setattr(g, "_kill_tree", killed.append)
    with pytest.raises(KeyboardInterrupt):
        g.check_suite()
    assert killed == [running]


def test_a_files_output_is_decoded_as_utf8_and_a_bad_byte_is_replaced(monkeypatch):
    """The children write UTF-8 (the gate sets PYTHONUTF8); the gate decodes their output as
    UTF-8 by name, not by the locale, and a byte that is not UTF-8 is replaced, not raised
    out of the suite run. Every capture of the gate reads so."""
    import types
    g = _tool("gate")
    asked = []

    def recording_popen(*a, **k):
        asked.append((k.get("encoding"), k.get("errors"), k.get("text")))
        return subprocess.Popen(*a, **k)

    monkeypatch.setattr(g, "subprocess",
                        types.SimpleNamespace(**{**vars(subprocess), "Popen": recording_popen}))
    done = g._run_bounded([sys.executable, "-c",
                           "import sys; sys.stdout.buffer.write('t\u207b \u221d caf\u00e9\\n'"
                           ".encode('utf-8') + b'\\xff\\x81\\n')"], timeout=60)
    assert asked == [("utf-8", "replace", None)]
    assert done.returncode == 0
    assert done.stdout.splitlines() == ["t\u207b \u221d caf\u00e9", "\ufffd\ufffd"]
    assert "text=True" not in _text("tools/gate.py")


def test_the_durations_record_is_kept_per_scope_and_a_bad_line_is_skipped(tmp_path,
                                                                           monkeypatch):
    g = _tool("gate")
    (tmp_path / "tests").mkdir()
    # test_a's size ranks it at 6.0, above test_b's recorded 5.0; its bad line read as 1.5
    # would rank it below.
    (tmp_path / "tests" / "test_a.py").write_text("x = 1\n" + "#" * 60_000, encoding="utf-8")
    (tmp_path / "tests" / "test_b.py").write_text("x = 1\n", encoding="utf-8")
    monkeypatch.setattr(g, "_ROOT", str(tmp_path))
    record = tmp_path / g.DURATIONS_DIR / f"{sys.platform}.txt"
    record.parent.mkdir(parents=True)
    record.write_text("file 1.5e tests/test_a.py\nfile 5.0 tests/test_b.py\nfile 2\n",
                      encoding="utf-8")
    assert [f for f, _m in g._suite_files(guest=False)] == ["tests/test_a.py",
                                                            "tests/test_b.py"]
    full = record.read_text(encoding="utf-8")
    g._write_durations({}, {"tests/test_b.py": 9.0}, guest=True)
    assert record.read_text(encoding="utf-8") == full
    guest = tmp_path / g.DURATIONS_DIR / f"{sys.platform}-guest.txt"
    assert guest.read_text(encoding="utf-8") == "file 9.000 tests/test_b.py\n"
    assert [f for f, _m in g._suite_files(guest=True)] == ["tests/test_b.py",
                                                           "tests/test_a.py"]
    g._write_durations({}, {"tests/test_b.py": 1.0})
    assert record.read_text(encoding="utf-8") == "file 1.000 tests/test_b.py\n"


def test_a_file_within_its_time_limit_returns_its_output_and_status():
    done = _tool("gate")._run_bounded(
        [sys.executable, "-c", "import sys; print('out'); print('err', file=sys.stderr); "
                               "sys.exit(3)"], timeout=60)
    assert (done.returncode, done.stdout.strip(), done.stderr.strip()) == (3, "out", "err")


@pytest.mark.parametrize("rc", [1, 2])
def test_a_native_coverage_failure_is_not_blamed_on_the_build_alone(monkeypatch, rc):
    g = _tool("gate")
    monkeypatch.setattr(g, "_run", lambda *a, **k: subprocess.CompletedProcess(a[0], rc))
    monkeypatch.setattr(g, "_FAILED", [])
    g.check_native_coverage()
    assert len(g._FAILED) == 1 and "empty report" in g._FAILED[0]


@pytest.mark.parametrize("rc, not_run", [(0, 0), (3, 1)])
def test_native_coverage_that_cannot_run_here_is_not_run_and_a_pass_is_neither(monkeypatch, rc,
                                                                                not_run):
    g = _tool("gate")
    monkeypatch.setattr(g, "_run", lambda *a, **k: subprocess.CompletedProcess(a[0], rc))
    monkeypatch.setattr(g, "_FAILED", [])
    monkeypatch.setattr(g, "_NOT_RUN", [])
    g.check_native_coverage()
    assert g._FAILED == [] and len(g._NOT_RUN) == not_run


@pytest.mark.skipif(sys.platform == "win32", reason="the coverage tools are not run on Windows")
@pytest.mark.parametrize("outcome", ["fails", "passes", "raises"])
def test_native_coverage_removes_its_working_directory_whatever_the_outcome(
        tmp_path, monkeypatch, outcome):
    nc = _tool("native_coverage")
    made = []
    real_mkdtemp = nc.tempfile.mkdtemp

    def mkdtemp(*a, **k):
        made.append(real_mkdtemp(*a, dir=str(tmp_path), **k))
        return made[-1]

    monkeypatch.setattr(nc.tempfile, "mkdtemp", mkdtemp)
    monkeypatch.setattr(nc, "_llvm", lambda name: [name])
    monkeypatch.setattr(nc.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(nc.subprocess, "run",
                        lambda cmd, **k: subprocess.CompletedProcess(cmd, 1, "", ""))

    def measure(out, work, *_tools):
        assert os.path.isdir(work)
        if outcome == "raises":
            raise RuntimeError("the measurement stopped")
        return 0

    if outcome != "fails":                              # "fails" runs the real build, stubbed
        monkeypatch.setattr(nc, "_measure", measure)
    if outcome == "raises":
        with pytest.raises(RuntimeError):
            nc.main(["--out", str(tmp_path / "out")])
    else:
        assert nc.main(["--out", str(tmp_path / "out")]) == (1 if outcome == "fails" else 0)
    assert len(made) == 1 and not os.path.exists(made[0])


@pytest.mark.parametrize("failing", ["report", "show"])
def test_a_failed_llvm_cov_step_is_named_with_its_diagnostic(tmp_path, monkeypatch, capsys,
                                                             failing):
    nc = _tool("native_coverage")
    monkeypatch.setattr(nc, "_lists_native_source", lambda report: True)

    def run(cmd, **k):
        step = cmd[1] if cmd[0] == "llvm-cov" else ""
        rc = 1 if step == failing else 0
        return subprocess.CompletedProcess(cmd, rc, "", f"{step} diagnostic" if rc else "")

    monkeypatch.setattr(nc.subprocess, "run", run)
    assert nc._measure(str(tmp_path / "out"), str(tmp_path), "clang++", ["llvm-profdata"],
                       ["llvm-cov"]) == 1
    out = capsys.readouterr().out
    assert f"{failing} diagnostic" in out and f"llvm-cov {failing} FAILED" in out


def test_a_report_on_another_drive_than_the_checkout_is_named_by_its_absolute_path(
        tmp_path, monkeypatch):
    """`_shown` names a path relative to the checkout, and by its absolute path where
    `os.path.relpath` cannot relate the two (a path on another Windows drive)."""
    nc = _tool("native_coverage")
    inside = os.path.join(nc._ROOT, "build", "native-coverage", "report.txt")
    assert nc._shown(inside) == os.path.join("build", "native-coverage", "report.txt")

    def other_drive(path, start=None):
        raise ValueError("path is on mount 'C:', start on mount 'D:'")

    monkeypatch.setattr(nc.os.path, "relpath", other_drive)
    report = str(tmp_path / "report.txt")
    assert nc._shown(report) == os.path.abspath(report)


def test_a_suite_that_cannot_start_against_the_instrumented_core_shows_its_stderr(
        tmp_path, monkeypatch, capsys):
    nc = _tool("native_coverage")

    def run(cmd, **k):
        if "pytest" in cmd:
            return subprocess.CompletedProcess(cmd, 4, "", "pytest: unrecognized arguments: -n")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(nc.subprocess, "run", run)
    assert nc._measure(str(tmp_path / "out"), str(tmp_path), "clang++", ["llvm-profdata"],
                       ["llvm-cov"]) == 1
    out = capsys.readouterr().out
    assert "unrecognized arguments: -n" in out and "the suite FAILED" in out


def test_name_review_does_not_approve_a_tree_holding_a_directory_it_cannot_list(
        tmp_path, capsys, monkeypatch):
    if sys.platform == "win32" or (hasattr(os, "geteuid") and os.geteuid() == 0):
        pytest.skip("permissions do not hide a directory here")
    nr = _name_review(monkeypatch)
    tree = tmp_path / "tree"
    (tree / "sealed").mkdir(parents=True)
    (tree / "ok.md").write_text("plain words here\n", encoding="utf-8")
    (tree / "sealed" / "hidden.md").write_text("Vendorname\n", encoding="utf-8")
    (tree / "sealed").chmod(0)
    try:
        assert nr.main([f"--tree={tree}", "--baseline=HEAD"]) == 2
    finally:
        (tree / "sealed").chmod(0o700)
    captured = capsys.readouterr()
    assert "sealed/" in captured.err and "no names new" not in captured.out


def test_changed_files_reads_a_path_with_non_ascii_characters_unquoted(tmp_path, isolated_git):
    """git quotes such a path unless it is asked for NUL-separated names."""
    ct = _tool("changed_tests")
    git_in(tmp_path, "init", "-q")
    commit_files(tmp_path, {"caf\u00e9.txt": "a\n"}, "base")
    (tmp_path / "caf\u00e9.txt").write_text("b\n", encoding="utf-8")
    (tmp_path / "na\u00efve.txt").write_text("c\n", encoding="utf-8")
    ct._ROOT = str(tmp_path)
    assert ct.changed_files("HEAD") == ["caf\u00e9.txt", "na\u00efve.txt"]


def test_a_renamed_file_is_listed_under_its_old_path_and_its_new_one(tmp_path, isolated_git):
    """A test file may still import the old path, so a rename is a deletion plus an addition,
    whether the rename is staged or committed since the base."""
    ct = _tool("changed_tests")
    git_in(tmp_path, "init", "-q")
    body = "".join(f"line {i}\n" for i in range(40))
    commit_files(tmp_path, {"old.py": body, "keep.py": body + "x\n"}, "base")
    git_in(tmp_path, "mv", "old.py", "new.py")
    ct._ROOT = str(tmp_path)
    assert ct.changed_files("HEAD") == ["new.py", "old.py"]
    git_in(tmp_path, "commit", "-qm", "rename")
    assert ct.changed_files("HEAD~1") == ["new.py", "old.py"]


def test_changed_files_decodes_git_output_as_utf8_whatever_the_locale(tmp_path, monkeypatch):
    """The locale's code page (cp1252 on Windows without PYTHONUTF8) would mis-decode a UTF-8
    path, or raise on a byte it has no character for, so the capture names its encoding."""
    ct = _tool("changed_tests")
    asked = []

    def fake_run(cmd, **kw):
        asked.append((kw.get("encoding"), kw.get("errors"), kw.get("text"),
                      kw.get("universal_newlines")))
        return subprocess.CompletedProcess(cmd, 0, "caf\u00e9.txt\0", "")
    monkeypatch.setattr(ct, "subprocess", types.SimpleNamespace(run=fake_run))
    assert ct.changed_files("HEAD") == ["caf\u00e9.txt"]
    assert asked == [("utf-8", "replace", None, None)] * 3


@pytest.mark.parametrize("script", sorted(p.name for p in pathlib.Path(_ROOT, "tools").glob("*.py")))
def test_no_tool_decodes_a_child_process_with_the_locales_code_page(script):
    """A tool captures a child's output with `encoding="utf-8"`, never `text=True` or
    `universal_newlines=True`, which read it in the locale's code page."""
    import ast
    tree = ast.parse(_text(f"tools/{script}"))
    locale_decoded = [node.lineno for node in ast.walk(tree) if isinstance(node, ast.keyword)
                      and node.arg in ("text", "universal_newlines")
                      and isinstance(node.value, ast.Constant) and node.value.value is True]
    assert not locale_decoded, f"tools/{script} lines {locale_decoded}"


@pytest.mark.parametrize("pytest_status, expected", [(5, 0), (0, 0), (1, 1), (2, 2)])
def test_a_run_whose_picked_files_are_all_perf_exits_zero_and_says_nothing_ran(
        tmp_path, monkeypatch, capsys, pytest_status, expected):
    """pytest exits 5 when `-m "not perf"` deselects every test of the picked files; that is
    no failure. Any other status is pytest's own."""
    ct = _changed_tests_in(tmp_path, monkeypatch, {"tests/test_perf.py": "x = 1\n"})
    monkeypatch.setattr(ct, "changed_files", lambda base: ["tests/test_perf.py"])
    monkeypatch.setattr(ct.subprocess, "run",
                        lambda cmd, **kw: subprocess.CompletedProcess(cmd, pytest_status))
    assert ct.main(["--run"]) == expected
    out = capsys.readouterr().out
    assert ("hold no test outside the perf scope" in out) == (pytest_status == 5), out


def test_changed_tests_reads_the_test_files_once_however_many_paths_changed(
        tmp_path, monkeypatch, capsys):
    ct = _changed_tests_in(tmp_path, monkeypatch, {
        "tests/test_a.py": "x = 1\n", "tests/test_b.py": "x = 2\n"})
    reads = []
    real = ct._read
    monkeypatch.setattr(ct, "_read", lambda path: reads.append(path) or real(path))
    paths = [f"docs/n{i}.md" for i in range(20)] + ["tests/test_a.py"]
    monkeypatch.setattr(ct, "changed_files", lambda base: paths)
    assert ct.main([]) == 0
    assert len(reads) == 2
    assert "tests/test_a.py" in capsys.readouterr().out


def _signal_handlers_restored():
    import signal
    saved = {n: signal.getsignal(getattr(signal, n))
             for n in ("SIGTERM", "SIGHUP") if hasattr(signal, n)}

    def restore():
        for n, h in saved.items():
            signal.signal(getattr(signal, n), h)
    return restore


@pytest.mark.skipif(os.name == "nt", reason="POSIX signals")
@pytest.mark.parametrize("tool", ["gate", "publish_tree"])
@pytest.mark.parametrize("name", ["SIGTERM", "SIGHUP"])
def test_a_terminated_tool_is_interrupted_so_its_children_are_killed(tool, name):
    """SIGTERM and SIGHUP raise KeyboardInterrupt in the main thread, which runs the same
    kill and cleanup paths as ^C (each child is in a session of its own)."""
    import signal
    restore = _signal_handlers_restored()
    try:
        _tool(tool)._terminate_as_interrupt()
        with pytest.raises(KeyboardInterrupt):
            os.kill(os.getpid(), getattr(signal, name))
            time.sleep(1)
    finally:
        restore()


def test_a_failed_native_build_prints_the_end_of_its_output(monkeypatch, capsys):
    g = _tool("gate")
    out = "\n".join(f"line {i}" for i in range(100)) + "\n"
    monkeypatch.setattr(g, "_run", lambda cmd, **kw: subprocess.CompletedProcess(
        cmd, 1, out, "error: no such header\n"))
    g._FAILED[:] = []
    g._NOT_RUN[:] = []
    g.check_native()
    shown = capsys.readouterr().out
    assert "error: no such header" in shown and "line 99" in shown and "line 10\n" not in shown
    assert g._FAILED == ["native build"]
    g._FAILED[:] = []
    g._NOT_RUN[:] = []


def test_the_qt_notice_names_the_pinned_series_and_the_wheels_licence_statement():
    ns = _spec_notices()
    series, text = ns["_qt_reviewed"]()
    assert series == {(6, 11)}
    assert _QT_LICENCE in text
    assert {ns["_repo_notices"](ns["_canon"](n)) for n in _QT_WHEELS["6.12"]} == {
        "Qt-for-Python"}


def test_a_qt_for_python_wheel_of_an_unreviewed_series_stops_the_bundle_build():
    ns = _spec_notices()
    ns["_runtime_closure"] = lambda: ([_Wheel(n, version="6.12.0") for n in _QT_WHEELS["6.12"]]
                                      + [_Wheel("numpy", version="2.4.6", licence="BSD")])
    with pytest.raises(SystemExit) as e:
        ns["_notice_datas"]()
    message = str(e.value)
    assert all(f"{n} 6.12.0 (release series not reviewed)" in message
               for n in _QT_WHEELS["6.12"]) and "numpy" not in message


def test_a_qt_for_python_wheel_stating_another_licence_stops_the_bundle_build():
    ns = _spec_notices()
    wheels = [_Wheel(n) for n in _QT_WHEELS["6.11"]]
    wheels[1] = _Wheel("PySide6_Essentials", licence="LicenseRef-Qt-Commercial")
    ns["_runtime_closure"] = lambda: wheels
    with pytest.raises(SystemExit, match=r"PySide6_Essentials 6\.11\.1 \(licence "
                                         r"'LicenseRef-Qt-Commercial' not in the notice\)"):
        ns["_notice_datas"]()
    wheels[1] = _Wheel("PySide6_Essentials", licence=None)
    with pytest.raises(SystemExit, match="licence None"):
        ns["_notice_datas"]()


@pytest.mark.parametrize("line, series", [("6.11", {(6, 11)}), ("6.11, 6.12", {(6, 11), (6, 12)})])
def test_the_qt_notice_line_names_release_series(tmp_path, line, series):
    ns = _spec_notices()
    notice = tmp_path / "NOTICE.txt"
    notice.write_text(f"Qt for Python versions reviewed for this notice: {line}\n",
                      encoding="utf-8")
    assert ns["_qt_reviewed"](str(notice))[0] == series
    for bad in ("6.6 to 6.11\n", "6.11.1\n"):
        notice.write_text(f"Qt for Python versions reviewed for this notice: {bad}",
                          encoding="utf-8")
        with pytest.raises(SystemExit, match="major.minor"):
            ns["_qt_reviewed"](str(notice))
    notice.write_text("no line\n", encoding="utf-8")
    with pytest.raises(SystemExit, match="exactly one"):
        ns["_qt_reviewed"](str(notice))


# ------------------------------------------------- third-party components of the Qt libraries

_ALL, _MAC, _WIN, _LINUX = ("macOS", "Windows", "Linux"), ("macOS",), ("Windows",), ("Linux",)
_THIRD_PARTY = os.path.join("data", "licenses", "Qt-for-Python", "THIRD-PARTY.txt")

# The third-party components the shipped Qt libraries and plugins contain, as reviewed for the
# release series NOTICE.txt names: the component's name as THIRD-PARTY.txt heads it, the
# platforms whose bundle holds it, and the evidence it is in a shipped file there. This is a
# statement of intent, not something derived: moving to another Qt for Python release means
# re-reading the shipped binaries and Qt's attribution files, then changing a row here and in
# THIRD-PARTY.txt together, which is the review step.
_REVIEWED_QT_COMPONENTS = (
    ('PCRE2', _ALL,
     'PCRE2 error-message strings in QtCore; no system libpcre2 linked'),
    ('PCRE2 - Stack-less Just-In-Time Compiler', _ALL,
     'Qt builds the PCRE2 JIT for x86_64 (the macOS universal x86_64 slice, Windows, Linux)'),
    ('Efficient Binary-Decimal and Decimal-Binary Conversion Routines for IEEE Doubles', _ALL,
     'double-conversion cached-powers table in QtCore; no system library linked'),
    ('Easing Equations by Robert Penner', _ALL,
     'QEasingCurve, built unconditionally into QtCore'),
    ('forkfd', _MAC + _LINUX,
     'built into QtCore on Unix with QProcess'),
    ('MD4', _ALL,
     'MD4 round constants in QtCore'),
    ('MD5', _ALL,
     'MD5 constants in QtCore'),
    ('Secure Hash Algorithm SHA-1', _ALL,
     'QCryptographicHash SHA-1, built unconditionally into QtCore'),
    ('Secure Hash Algorithms SHA-384 and SHA-512', _ALL,
     'SHA-256/SHA-512 constants in QtCore'),
    ('Secure Hash Algorithm SHA-3 - Keccak', _ALL,
     'Keccak round constants in QtCore (Windows, Linux); built unconditionally'),
    ('Secure Hash Algorithm SHA-3 - brg_endian', _ALL,
     'header of the Keccak code in QtCore'),
    ('BLAKE2 (reference implementation)', _ALL,
     'built unconditionally into QtCore; no system libb2 linked'),
    ('SipHash Algorithm', _ALL,
     'SipHash constants in QtCore'),
    ('TinyCBOR', _ALL,
     'QCborStreamReader/Writer in QtCore'),
    ('Data Compression Library (zlib)', _WIN,
     'zlib messages in Qt6Core.dll; macOS and Linux link the system libz'),
    ('Unicode Character Database (UCD)', _ALL,
     'Unicode property tables in QtCore'),
    ('Unicode Common Locale Data Repository (CLDR)', _ALL,
     'QLocale/QTimeZone data in QtCore'),
    ('Apache Tika MimeType Definitions', _ALL,
     'MIME database strings in QtCore'),
    ('QEventDispatcher on macOS', _MAC,
     "QtCore's macOS event dispatcher"),
    ('HarfBuzz-NG', _ALL,
     'HarfBuzz strings in QtGui; no system libharfbuzz linked'),
    ('Freetype 2', _MAC + _WIN,
     'FreeType strings in QtGui; Linux links the system libfreetype'),
    ('Freetype 2 - zlib', _MAC + _WIN,
     "FreeType's gzip module, in the bundled FreeType"),
    ('Freetype 2 - Bitmap Distribution Format (BDF) support', _MAC + _WIN,
     "FreeType's BDF driver, in the bundled FreeType"),
    ('Freetype 2 - Portable Compiled Format (PCF) support', _MAC + _WIN,
     "FreeType's PCF driver, in the bundled FreeType"),
    ('Anti-aliasing rasterizer from FreeType 2', _ALL,
     "QtGui's anti-aliasing rasterizer, built unconditionally"),
    ('LibPNG', _ALL,
     'libpng error strings in QtGui; no system libpng linked'),
    ('MD4C', _ALL,
     'QTextMarkdownImporter strings in QtGui; no system libmd4c linked'),
    ('Emoji Segmenter', _ALL,
     "QtGui's emojisegmenter feature, on by default"),
    ('Smooth Scaling Algorithm', _ALL,
     'QImage scaling, built unconditionally into QtGui'),
    ('X Server helper', _ALL,
     'QRegion, built unconditionally into QtGui'),
    ('Adobe Glyph List For New Fonts', _ALL,
     "glyph-name table of QtGui's PDF writer"),
    ('sRGB color profile icc file', _ALL,
     'sRGB2014.icc resource in QtGui'),
    ('WebGradients', _ALL,
     'QGradient presets in QtGui'),
    ('OpenGL Headers', _WIN + _LINUX,
     'OpenGL header definitions compiled into QtGui on Windows and Linux'),
    ('OpenGL ES 2 Headers', _WIN + _LINUX,
     'OpenGL ES 2 header definitions compiled into QtGui on Windows and Linux'),
    ('Vulkan API Registry', _WIN + _LINUX,
     'Vulkan entry-point strings in QtGui (Windows, Linux); none on macOS'),
    ('Vulkan Memory Allocator', _WIN + _LINUX,
     'QRhi Vulkan backend in QtGui (Windows, Linux)'),
    ('D3D12 Memory Allocator', _WIN,
     'D3D12MA strings in Qt6Gui.dll'),
    ('Mipmap generator for D3D12', _WIN,
     'QRhi Direct 3D 12 backend in Qt6Gui.dll'),
    ('LibJPEG-turbo', _ALL,
     'libjpeg-turbo strings in qjpeg; no system libjpeg linked'),
    ('Cocoa Platform Plugin', _MAC,
     'the cocoa platform plugin'),
    ('Wintab API', _WIN,
     'Wintab strings in qwindows.dll and qdirect2d.dll'),
    ('XCB-XInput', _LINUX,
     'xcb_input symbols in libQt6XcbQpa; no system libxcb-xinput linked'),
    ('libdbus-1 headers', _MAC + _LINUX,
     'QtDBus ships on macOS and Linux'),
    ('Cycle', _ALL,
     "QtTest's benchmark tick counter"),
    ('Valgrind', _MAC + _LINUX,
     'callgrind strings in QtTest (macOS, Linux)'),
    ('Linux Performance Events', _LINUX,
     'perf_event strings in libQt6Test'),
    ('libpsl - C library to handle the Public Suffix List', _ALL,
     "QtNetwork's topleveldomain feature, on by default"),
    ('The Public Suffix List', _ALL,
     "QtNetwork's topleveldomain feature, on by default"),
    ('Wayland KDE DBus Menu Protocol', _LINUX,
     'protocol interface names in the Wayland client library or plugins'),
    ('Wayland Color Management Protocol', _LINUX,
     'protocol interface names in the Wayland client library or plugins'),
    ('Wayland Fractional Scale Protocol', _LINUX,
     'protocol interface names in the Wayland client library or plugins'),
    ('Wayland Fullscreen Shell Protocol', _LINUX,
     'protocol interface names in the Wayland client library or plugins'),
    ('Wayland Pointer Gestures Protocol', _LINUX,
     'protocol interface names in the Wayland client library or plugins'),
    ('Wayland Pointer Warp Protocol', _LINUX,
     'protocol interface names in the Wayland client library or plugins'),
    ('Wayland Session Management Protocol', _LINUX,
     'protocol interface names in the Wayland client library or plugins'),
    ('Wayland Tablet Protocol', _LINUX,
     'protocol interface names in the Wayland client library or plugins'),
    ('Wayland Text Input Protocol v1', _LINUX,
     'protocol interface names in the Wayland client library or plugins'),
    ('Wayland Text Input Protocol v2', _LINUX,
     'protocol interface names in the Wayland client library or plugins'),
    ('Wayland Text Input Protocol', _LINUX,
     'protocol interface names in the Wayland client library or plugins'),
    ('Wayland Viewporter Protocol', _LINUX,
     'protocol interface names in the Wayland client library or plugins'),
    ('Wayland Protocol', _LINUX,
     'protocol interface names in the Wayland client library or plugins'),
    ('Wlr Data Control Unstable V1 Protocol', _LINUX,
     'protocol interface names in the Wayland client library or plugins'),
    ('Wayland Primary Selection Protocol', _LINUX,
     'protocol interface names in the Wayland client library or plugins'),
    ('Wayland xdg-activation Protocol', _LINUX,
     'protocol interface names in the Wayland client library or plugins'),
    ('Wayland xdg-decoration Protocol', _LINUX,
     'protocol interface names in the Wayland client library or plugins'),
    ('Wayland Dialog Protocol', _LINUX,
     'protocol interface names in the Wayland client library or plugins'),
    ('Wayland XDG Foreign Protocol', _LINUX,
     'protocol interface names in the Wayland client library or plugins'),
    ('Wayland XDG Output Protocol', _LINUX,
     'protocol interface names in the Wayland client library or plugins'),
    ('Wayland XDG Shell Protocol', _LINUX,
     'protocol interface names in the Wayland client library or plugins'),
    ('Wayland XDG System Bell Protocol', _LINUX,
     'protocol interface names in the Wayland client library or plugins'),
    ('Wayland xdg-toplevel-icon Protocol', _LINUX,
     'protocol interface names in the Wayland client library or plugins'),
    ('Wayland IVI Extension Protocol', _LINUX,
     'ivi_application interface names in libivi-shell'),
    ('XSVG', _ALL,
     'QtSvg, built unconditionally'),
    ('TIFF Software Distribution (libtiff)', _MAC + _WIN,
     "libtiff strings in qtiff (macOS, Windows); Linux's qtiff links the system libtiff"),
    ('WebP (libwebp)', _ALL,
     'WebP code in qwebp; no system libwebp linked'),
    ('FFmpeg', _ALL,
     'the FFmpeg libraries ship with QtMultimedia'),
    ('libjpeg', _ALL,
     'libavcodec ships with QtMultimedia'),
    ('zlib', _ALL,
     'the FFmpeg libraries ship with QtMultimedia'),
    ('boost', _ALL,
     'the FFmpeg libraries ship with QtMultimedia'),
    ('Signalsmith Stretch', _ALL,
     'built unconditionally into the ffmpeg media plugin'),
    ('Two-Level Segregated Fit', _ALL,
     'TLSF strings in QtMultimedia'),
    ('DR Libs', _ALL,
     "QSoundEffect's WAV decoder, built unconditionally into QtMultimedia"),
    ('ICU (International Components for Unicode)', _LINUX,
     'libicu*.so.73 in the Linux wheel, linked by libQt6Core'),
    ('Mesa llvmpipe', _WIN,
     "opengl32sw.dll, which PyInstaller's Qt hooks collect on Windows; 'Mesa 11.2.2' string"),
    ('LLVM', _WIN,
     "'llvmpipe (LLVM' and 'based on LLVM 3.6.2' strings in opengl32sw.dll"),
)


def _third_party_entries():
    """{component name: (platforms, licence labels)} and {label: text count} of THIRD-PARTY.txt."""
    text = _text(_THIRD_PARTY)
    components, texts = text.split("\nPART 2. LICENCE TEXTS\n")
    entries = {}
    for m in re.finditer(r"^(\S[^\n]*)\n-+\nIn: .*?^Platforms:  ([^\n]+)\n.*?^Texts:      ([^\n]+)\n",
                         components, re.M | re.S):
        entries[m.group(1)] = (tuple(m.group(2).split(", ")), re.findall(r"\[(L\d+)\]", m.group(3)))
    labels = re.findall(r"^\[(L\d+)\] from ", texts, re.M)
    return entries, {label: labels.count(label) for label in labels}


def test_the_qt_third_party_notice_names_every_reviewed_component_on_its_platforms():
    entries, _labels = _third_party_entries()
    names = [name for name, _platforms, _evidence in _REVIEWED_QT_COMPONENTS]
    assert len(set(names)) == len(names)
    missing = [n for n in names if n not in entries]
    assert not missing, f"{_THIRD_PARTY} does not name: {missing}"
    wrong = [(n, entries[n][0], p) for n, p, _e in _REVIEWED_QT_COMPONENTS if entries[n][0] != p]
    assert not wrong, f"platforms differ from the reviewed set: {wrong}"
    unreviewed = sorted(set(entries) - set(names))
    assert not unreviewed, f"{_THIRD_PARTY} names components the reviewed set does not: {unreviewed}"


def test_each_licence_text_of_the_qt_third_party_notice_appears_once_and_is_referenced():
    entries, labels = _third_party_entries()
    assert labels and all(count == 1 for count in labels.values()), labels
    referenced = {label for _platforms, used in entries.values() for label in used}
    assert all(used for _platforms, used in entries.values())
    assert referenced == set(labels), (referenced ^ set(labels))


def test_the_qt_notice_names_the_third_party_notice():
    notice = _text(os.path.join("data", "licenses", "Qt-for-Python", "NOTICE.txt"))
    assert "THIRD-PARTY.txt" in notice


@pytest.mark.parametrize("project, absent", [("Qt-for-Python", "THIRD-PARTY.txt"),
                                             ("Qt-for-Python", "NOTICE.txt"),
                                             ("FFmpeg", "LGPL-2.1.txt")])
def test_a_notice_project_lacking_a_required_file_stops_the_bundle_build(tmp_path, project,
                                                                          absent):
    for name, files in (("Qt-for-Python", ("NOTICE.txt", "LGPL-3.0.txt", "GPL-3.0.txt",
                                           "THIRD-PARTY.txt")),
                        ("FFmpeg", ("NOTICE.txt", "LGPL-2.1.txt"))):
        held = tmp_path / "data" / "licenses" / name
        held.mkdir(parents=True)
        for f in files:
            if (name, f) != (project, absent):
                (held / f).write_text("x", encoding="utf-8")
    ns = _spec_notices()
    ns["HERE"] = str(tmp_path)
    ns["_runtime_closure"] = lambda: [_Wheel(n) for n in _QT_WHEELS["6.11"]]
    assert ns["_imports_qt_multimedia"]()
    with pytest.raises(SystemExit, match=rf"{project} \(data/licenses/{project} lacks {absent}\)"):
        ns["_notice_datas"]()
    (tmp_path / "data" / "licenses" / project / absent).write_text("x", encoding="utf-8")
    assert ns["_notice_datas"]()


# ------------------------------------------- the interpreter's OpenSSL and ncurses libraries

_EXTRAS = os.path.join(_ROOT, "data", "licenses", "Python-extras")
_OPENSSL_NOTICE = (os.path.join(_EXTRAS, "OpenSSL-1.1.txt"), os.path.join("licenses", "Python-extras"))
_NCURSES_NOTICE = (os.path.join(_EXTRAS, "ncurses.txt"), os.path.join("licenses", "Python-extras"))
_OPENSSL3_NOTICE = (os.path.join(_EXTRAS, "OpenSSL-3.txt"), os.path.join("licenses", "Python-extras"))


def _binaries(*names):
    """Analysis' binaries entries, (bundle path, source path, kind), for library `names`."""
    return [(n, os.path.join("/interp", "lib", os.path.basename(n)), "BINARY") for n in names]


@pytest.mark.parametrize("names, shipped", [
    (("libssl.1.1.dylib", "libcrypto.1.1.dylib", "libncursesw.5.dylib",
      "PySide6/Qt/lib/QtCore.framework/Versions/A/QtCore"), [_OPENSSL_NOTICE, _NCURSES_NOTICE]),
    (("libssl-1_1-x64.dll", "libcrypto-1_1.dll", "PySide6/Qt6Core.dll"), [_OPENSSL_NOTICE]),
    (("libssl.so.1.1", "libcrypto.so.1.1", "libtinfo.so.6", "libpanelw.so.6"),
     [_OPENSSL_NOTICE, _NCURSES_NOTICE]),
    (("libncursesw.so.6",), [_NCURSES_NOTICE]),
    (("libssl.3.dylib", "libcrypto.3.dylib", "libncursesw.5.dylib"), [_OPENSSL3_NOTICE, _NCURSES_NOTICE]),
    (("libssl-3-x64.dll", "libcrypto-3-x64.dll"), [_OPENSSL3_NOTICE]),
    (("libssl-3.dll", "libcrypto-3.dll"), [_OPENSSL3_NOTICE]),
    (("libssl.so.3", "libcrypto.so.3", "libtinfo.so.6"), [_OPENSSL3_NOTICE, _NCURSES_NOTICE]),
    (("libssl.1.1.dylib", "libssl.so.3"), [_OPENSSL_NOTICE, _OPENSSL3_NOTICE]),
    (("PySide6/Qt/lib/libQt6FFmpegStub-ssl.so.3", "PySide6/Qt/lib/libQt6FFmpegStub-crypto.so.3",
      "libssl3.so", "libQt6Core.so.6", "libavformat.61.dylib"), []),
])
def test_the_bundle_ships_the_notices_of_the_interpreters_openssl_and_ncurses_libraries(
        names, shipped):
    ns = _spec_notices()
    assert ns["_interpreter_notices"](_binaries(*names)) == shipped


@pytest.mark.parametrize("name, reason", [("libssl-4-x64.dll", "OpenSSL 4"),
                                          ("libcrypto.so.4", "OpenSSL 4"),
                                          ("libssl.so.1.0.0", "OpenSSL 1.0"),
                                          ("libssl.dylib", "OpenSSL release not identified")])
def test_an_openssl_release_whose_notice_is_not_held_stops_the_bundle_build(name, reason):
    ns = _spec_notices()
    with pytest.raises(SystemExit, match=re.escape(f"{name} ({reason}: data/licenses/Python-extras/")):
        ns["_interpreter_notices"](_binaries("libncursesw.5.dylib", name))


def test_a_missing_interpreter_library_notice_stops_the_bundle_build(tmp_path):
    ns = _spec_notices()
    ns["HERE"] = str(tmp_path)
    with pytest.raises(SystemExit, match=re.escape(
            "libssl.1.1.dylib (data/licenses/Python-extras/OpenSSL-1.1.txt is missing)")):
        ns["_interpreter_notices"](_binaries("libssl.1.1.dylib"))


def test_the_interpreter_library_notices_hold_the_openssl_and_ncurses_licences():
    with open(_OPENSSL_NOTICE[0], encoding="utf-8") as f:
        openssl = f.read()
    assert "OpenSSL License" in openssl and "Original SSLeay License" in openssl
    assert "This product includes software developed by the OpenSSL Project" in openssl
    with open(_NCURSES_NOTICE[0], encoding="utf-8") as f:
        ncurses = f.read()
    assert "Free Software Foundation, Inc." in ncurses and "Permission is hereby granted" in ncurses
    with open(_OPENSSL3_NOTICE[0], encoding="utf-8") as f:
        openssl3 = f.read()
    assert "The OpenSSL Project Authors" in openssl3
    assert "Version 2.0, January 2004" in openssl3
    assert all(f"   {n}. " in openssl3 for n in range(1, 10))
    assert openssl3.rstrip().endswith("END OF TERMS AND CONDITIONS")


def test_the_spec_ships_the_interpreter_library_notices_from_the_binaries_analysis_collects():
    spec = _text("swi3s-studio.spec")
    applied = spec.index("for src, dest in _interpreter_notices(a.binaries)]")
    assert spec.index("a = Analysis(") < spec.index("a.binaries = [b for b in a.binaries") < applied
    assert applied < spec.index("coll = COLLECT(exe, a.binaries, a.datas")


# ---------------------------------------------------------- the build environment's packages

_BUILD_ENV = [_Wheel(n) for n in ("numpy", "PyInstaller", "pyinstaller-hooks-contrib", "altgraph",
                                  "packaging", "setuptools", "pip", "wheel", "swi3score",
                                  "macholib", "pefile", "pywin32_ctypes", "torch", "Tesseract")]


@pytest.mark.parametrize("platform, extra", [
    ("darwin", ["pefile", "pywin32_ctypes", "Tesseract", "torch"]),
    ("win32", ["macholib", "Tesseract", "torch"]),
    ("linux", ["macholib", "pefile", "pywin32_ctypes", "Tesseract", "torch"])])
def test_the_build_environment_may_hold_only_the_runtime_closure_and_the_build_tools(platform,
                                                                                     extra):
    ns = _spec_notices()
    assert ns["_extra_distributions"]([_Wheel("numpy")], _BUILD_ENV, platform, {}) == extra


@pytest.mark.parametrize("allow, extra", [("torch,tesseract", []), (" Torch , ", ["Tesseract"]),
                                          ("*", ["Tesseract", "torch"]),
                                          ("all", ["Tesseract", "torch"]), ("", ["Tesseract", "torch"])])
def test_the_allow_extra_variable_accepts_only_the_distributions_it_names(allow, extra):
    ns = _spec_notices()
    environ = {"SWI3S_BUNDLE_ALLOW_EXTRA": allow}
    dists = [_Wheel(n) for n in ("numpy", "torch", "Tesseract")]
    assert ns["_extra_distributions"]([_Wheel("numpy")], dists, "darwin", environ) == extra


def test_the_build_environment_check_reads_the_user_site_packages(tmp_path, monkeypatch):
    import site
    info = tmp_path / "user_only_pkg-1.0.dist-info"
    info.mkdir()
    (info / "METADATA").write_text("Metadata-Version: 2.1\nName: user-only-pkg\nVersion: 1.0\n",
                                   encoding="utf-8")
    monkeypatch.setattr(site, "getusersitepackages", lambda: str(tmp_path))
    monkeypatch.setattr(site, "ENABLE_USER_SITE", True)
    ns = _spec_notices()
    assert str(tmp_path) in ns["_search_path"]()
    assert "user-only-pkg" in ns["_extra_distributions"]([], None, "darwin", {})
    monkeypatch.setattr(site, "ENABLE_USER_SITE", False)
    assert str(tmp_path) not in ns["_search_path"]()


def test_an_extra_distribution_stops_the_bundle_build_naming_each_one():
    ns = _spec_notices()
    ns["_extra_distributions"] = lambda closure: ["Tesseract", "torch"]
    with pytest.raises(SystemExit) as e:
        ns["_refuse_extra_distributions"]([])
    message = str(e.value)
    assert "Tesseract, torch." in message and "fresh virtual environment" in message
    assert "docs/PACKAGING.md, section 3" in message and "SWI3S_BUNDLE_ALLOW_EXTRA" in message
    ns["_extra_distributions"] = lambda closure: []
    assert ns["_refuse_extra_distributions"]([]) is None


def test_the_spec_checks_the_build_environment_before_analysis():
    spec = _text("swi3s-studio.spec")
    assert spec.index("_refuse_extra_distributions(list(_runtime_closure()))") < spec.index(
        "a = Analysis(")


@pytest.mark.parametrize("name", ["libreadline.8.dylib", "libreadline.so.8", "libreadline.so.8.2"])
def test_a_gnu_readline_library_stops_the_bundle_build(name):
    ns = _spec_notices()
    with pytest.raises(SystemExit, match=re.escape(
            f"the bundle collects GNU readline (GPL): {name}; the interpreter's readline module "
            "must link libedit, or be excluded")):
        ns["_interpreter_notices"](_binaries("libssl.1.1.dylib", name))
    assert ns["_interpreter_notices"](_binaries("libedit.3.dylib")) == []


def test_the_bundle_leaves_out_the_readline_module_which_the_app_does_not_import():
    spec = _text("swi3s-studio.spec")
    m = re.search(r"excludes=\[([^\]]*)\]", spec[spec.index("a = Analysis("):])
    assert m and '"readline"' in m.group(1)
    imports = re.compile(r"^\s*(import|from)\s+(readline|rlcompleter)\b", re.M)
    for base, _dirs, files in os.walk(os.path.join(_ROOT, "swi3s_studio")):
        for name in files:
            if name.endswith(".py"):
                path = os.path.join(base, name)
                with open(path, encoding="utf-8") as f:
                    assert not imports.search(f.read()), path


def test_no_file_of_a_qt_for_python_wheel_ships_and_its_notices_come_from_data_licenses(tmp_path):
    """The Windows wheels carry dist-info/licenses/LicenseRef-Qt-Commercial.txt; the bundle uses
    the LGPL-3.0 option, so no wheel file ships, whatever its name."""
    files = ["pyside6_essentials-6.11.1.dist-info/licenses/LicenseRef-Qt-Commercial.txt",
             "pyside6_essentials-6.11.1.dist-info/licenses/LICENSE.txt", "PySide6/NOTICE"]
    for rel in files:
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text("x", encoding="utf-8")
    ns = _spec_notices()
    ns["_runtime_closure"] = lambda: [_Unpacked("PySide6_Essentials", tmp_path, files)]
    ns["_imports_qt_multimedia"] = lambda: False
    shipped = ns["_notice_datas"]()
    assert not [s for s, _d in shipped if str(tmp_path) in s]
    assert (os.path.join(_ROOT, "data", "licenses", "Qt-for-Python"),
            os.path.join("licenses", "Qt-for-Python")) in shipped


# ------------------------------------------------------- an option given twice is refused

def test_leak_scan_refuses_a_value_option_given_more_than_once(tmp_path, monkeypatch, capsys):
    ls = _tool("leak_scan")
    (tmp_path / "old").mkdir()
    (tmp_path / "new").mkdir()
    for argv in (["--tree=old", "--tree=new"], ["--text=a", "--text=b"]):
        monkeypatch.setattr(sys, "argv", ["leak_scan.py", *argv])
        assert ls.main() == 2, argv
        assert "given more than once" in capsys.readouterr().err


@pytest.mark.parametrize("option", ["--upstream=public/main", "--message-out=m.txt",
                                    "--approve-names=abc", "--check-published=public/x"])
def test_publish_tree_refuses_a_value_option_given_more_than_once(option):
    pt = _tool("publish_tree")
    name = option.partition("=")[0]
    assert f"{name} given more than once: name it once" in \
        pt._usage_problems([option, option.partition("=")[0] + "=other"])


@pytest.mark.parametrize("twice", [["--tree=a", "--tree=b"], ["--baseline=a", "--baseline=b"],
                                   ["--approve-names=a", "--approve-names=b"],
                                   ["--approve-names=a", "--approve=a"]])
def test_name_review_refuses_a_value_option_given_more_than_once(twice, capsys):
    assert _tool("name_review").main(twice) == 2
    assert "given more than once" in capsys.readouterr().err
