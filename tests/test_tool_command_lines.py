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


def _spec_notices():
    """The spec's notice collection, as a namespace holding its functions."""
    src = _text("swi3s-studio.spec")
    ns = {"HERE": _ROOT}
    exec("import importlib.metadata, os, re\n"
         + src[src.index("_REQUIREMENTS ="):src.index("datas += _notice_datas()")], ns)
    return ns


class _Wheel:
    """A distribution as the spec reads it: its name, and the files its metadata lists."""

    def __init__(self, name, files=()):
        self.metadata = {"Name": name}
        self.files = list(files)
        self.requires = []


# The wheels each PySide6 release installs, none of which carries a notice file.
_QT_WHEELS = {"6.11": ("PySide6", "PySide6_Essentials", "PySide6_Addons", "shiboken6"),
              "6.12": ("PySide6", "PySide6_Essentials", "PySide6_Addons", "shiboken6",
                       "PySide6_WebEngine", "PySide6_Pdf")}


def _shipped_paths(shipped):
    return [os.path.join(dest, os.path.basename(src)) for src, dest in shipped
            if os.path.isfile(src)]


def test_the_bundle_ships_every_notice_file_of_the_runtime_distributions_under_its_own_path():
    ns = _spec_notices()
    ns["_runtime_closure"] = lambda: ([importlib.metadata.distribution("numpy")]
                                      + [_Wheel(n) for n in _QT_WHEELS["6.12"]])
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


@pytest.mark.parametrize("release", sorted(_QT_WHEELS))
def test_the_bundle_ships_the_notices_the_qt_for_python_wheels_do_not_carry(release):
    ns = _spec_notices()
    ns["_runtime_closure"] = lambda: [_Wheel(n) for n in _QT_WHEELS[release]]
    shipped = ns["_notice_datas"]()
    assert (os.path.join(_ROOT, "data", "licenses", "Qt-for-Python"),
            os.path.join("licenses", "Qt-for-Python")) in shipped
    assert [s for s, d in shipped if d not in (os.path.join("licenses", "FFmpeg"),
                                                os.path.join("licenses", "Python"))] == [
        os.path.join(_ROOT, "data", "licenses", "Qt-for-Python")]
    names = set(os.listdir(os.path.join(_ROOT, "data", "licenses", "Qt-for-Python")))
    assert {"NOTICE.txt", "LGPL-3.0.txt", "GPL-3.0.txt"} <= names


@pytest.mark.parametrize("release", sorted(_QT_WHEELS))
def test_the_bundle_ships_the_ffmpeg_notice_and_lgpl_2_1_text_with_qt_multimedia(release):
    ns = _spec_notices()
    assert ns["_imports_qt_multimedia"]()
    ns["_runtime_closure"] = lambda: [_Wheel(n) for n in _QT_WHEELS[release]]
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
    shipped = _spec_notices()["_notice_datas"]()
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
    ps_flags = set(re.findall(r'-(?:eq|like)\s+"([^"]+)"', headless))
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
    rel, done, _took = g._run_file("tests/test_x.py", "not perf")
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

    def interrupted(rel, marker):
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
