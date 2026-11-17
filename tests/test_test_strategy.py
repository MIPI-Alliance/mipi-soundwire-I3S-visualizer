"""The tooling of docs/TESTING.md §1.1: markers, the guest scope, the dev loop's map from a
change to test files, the test report's reading of coverage data, and native coverage's
refusal where it cannot run."""
import glob
import importlib.util
import os
import pathlib
import re
import shutil
import signal
import subprocess
import sys
import time

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_GUI = re.compile(r"QtWidgets|swi3s_studio\.ui|MainWindow|QApplication|QWidget")


def _tool(name):
    spec = importlib.util.spec_from_file_location(name, os.path.join(_ROOT, "tools", name + ".py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _is_gui_mark(node) -> bool:
    import ast
    return ast.unparse(node).startswith("pytest.mark.gui")


def _widget_builders(path: str, _seen=None) -> tuple:
    """(tree, functions by name, the names in `path` that build widgets): a function whose
    source names a widget (`_GUI`), calls a builder of this file or of a tests/ helper module
    it imports, or requests a builder fixture, followed to a fixed point."""
    import ast
    _seen = {} if _seen is None else _seen
    if path in _seen:
        return _seen[path]
    _seen[path] = (None, {}, set())
    src = pathlib.Path(path).read_text(encoding="utf-8")
    tree = ast.parse(src)
    funcs = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
    builders: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            helper = os.path.join(_ROOT, "tests", node.module + ".py")
            if os.path.isfile(helper) and helper != path:
                theirs = _widget_builders(helper, _seen)[2]
                builders |= {a.asname or a.name for a in node.names if a.name in theirs}
    conftest = os.path.join(_ROOT, "tests", "conftest.py")
    if path != conftest:
        builders |= _widget_builders(conftest, _seen)[2]
    text = {k: ast.get_source_segment(src, v) or "" for k, v in funcs.items()}
    changed = True
    while changed:
        changed = False
        for name, node in funcs.items():
            if name in builders or name.startswith("test_"):
                continue
            uses = set(re.findall(r"\w+", text[name])) | {a.arg for a in node.args.args}
            if _GUI.search(text[name]) or uses & builders:
                builders.add(name)
                changed = True
    _seen[path] = (tree, funcs, builders)
    return _seen[path]


def _unmarked_widget_tests(path: str) -> list[str]:
    """The tests of `path` that build widgets (`_widget_builders`) and carry no gui mark,
    neither their own nor the module's `pytestmark`."""
    import ast
    tree, funcs, builders = _widget_builders(path)
    for node in tree.body:
        if (isinstance(node, ast.Assign) and any(getattr(t, "id", "") == "pytestmark"
                                                 for t in node.targets)
                and "pytest.mark.gui" in ast.unparse(node.value)):
            return []
    src = pathlib.Path(path).read_text(encoding="utf-8")
    out = []
    for name, node in funcs.items():
        if not name.startswith("test_") or any(_is_gui_mark(d) for d in node.decorator_list):
            continue
        body = ast.get_source_segment(src, node) or ""
        uses = set(re.findall(r"\w+", body)) | {a.arg for a in node.args.args}
        if _GUI.search(body) or uses & builders:
            out.append(name)
    return out


def test_a_test_that_builds_widgets_is_marked_gui():
    """The guest gate and CI's platform jobs run `gui` tests on every platform; a widget test
    without the marker would run in the full-suite run only. A file marks itself
    (`pytestmark`), or marks each test that builds widgets, directly or through a helper or
    fixture of the file, of conftest.py or of a tests/ module it imports."""
    unmarked = {}
    for path in sorted(glob.glob(os.path.join(_ROOT, "tests", "test_*.py"))):
        if os.path.basename(path) in ("test_perf.py", "test_test_strategy.py"):
            continue
        found = _unmarked_widget_tests(path)
        if found:
            unmarked[os.path.basename(path)] = found
    assert not unmarked, f"build widgets but are not marked gui: {unmarked}"


def test_the_widget_check_follows_helpers_imports_and_fixtures(tmp_path, monkeypatch):
    """`_unmarked_widget_tests` finds a widget test through a helper of its file, a helper
    imported from a tests/ module and a fixture, and passes a test marked gui, a file marked
    as a whole and a test that builds nothing."""
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "conftest.py").write_text("", encoding="utf-8")
    (tests / "support.py").write_text(
        "def make():\n    from PySide6.QtWidgets import QLabel\n    return QLabel()\n",
        encoding="utf-8")
    (tests / "test_x.py").write_text(
        "import pytest\nfrom support import make\n\n"
        "def _page():\n    return make()\n\n"
        "@pytest.fixture\ndef page():\n    return _page()\n\n"
        "def test_via_helper():\n    _page()\n\n"
        "def test_via_fixture(page):\n    pass\n\n"
        "@pytest.mark.gui\ndef test_marked():\n    _page()\n\n"
        "def test_core():\n    assert 1 + 1 == 2\n", encoding="utf-8")
    (tests / "test_y.py").write_text(
        "import pytest\nfrom support import make\npytestmark = pytest.mark.gui\n\n"
        "def test_any():\n    make()\n", encoding="utf-8")
    monkeypatch.setitem(globals(), "_ROOT", str(tmp_path))
    assert _unmarked_widget_tests(str(tests / "test_x.py")) == ["test_via_helper",
                                                                "test_via_fixture"]
    assert _unmarked_widget_tests(str(tests / "test_y.py")) == []


def test_the_guest_scope_runs_gui_platform_and_the_core_smoke_files():
    from conftest import CORE_SMOKE, PLATFORM_SCOPE
    gate = _tool("gate")
    assert gate._GUEST_SCOPE == PLATFORM_SCOPE == "(gui or platform or smoke) and not perf"
    assert all(os.path.isfile(os.path.join(_ROOT, f)) for f in CORE_SMOKE)
    assert all(os.path.isfile(os.path.join(_ROOT, f)) for f in gate._SPLIT)
    assert {m for _f, m in gate._suite_files(guest=True)} == {gate._GUEST_SCOPE}
    order = [f for f, _ in gate._suite_files(guest=False)]
    assert set(order[:len(gate._SPLIT)]) == set(gate._SPLIT), "the spread files start first"


def test_the_files_that_would_serialize_the_run_are_spread_over_workers():
    """The suite takes as long as its slowest file, so a file of many window-building or
    sweep tests runs spread over several workers of its own (`_SPLIT`)."""
    gate = _tool("gate")
    for rel, workers in {"tests/test_inequality_diagram.py": 4,
                         "tests/test_main_window_rules.py": 4,
                         "tests/test_timing_timeline.py": 3,
                         "tests/test_timing.py": 2,
                         "tests/test_timing_cross_spec.py": 6}.items():
        assert gate._SPLIT.get(rel, 1) >= workers, (rel, gate._SPLIT.get(rel))


def test_every_test_of_a_core_smoke_file_is_marked_smoke_and_no_other_test_is():
    """`-m smoke` collects the whole of a CORE_SMOKE file and nothing of another file."""
    import subprocess
    import sys
    smoke, other = "tests/test_register_model.py", "tests/test_timing_fmax_rate.py"
    def ids(*args):
        out = subprocess.run([sys.executable, "-m", "pytest", "--collect-only", "-q",
                              "-p", "no:cacheprovider", *args, smoke, other], cwd=_ROOT,
                             capture_output=True, text=True, encoding="utf-8", check=False)
        return {ln for ln in out.stdout.splitlines() if "::" in ln}
    every = ids("-m", "not perf")
    marked = ids("-m", "smoke")
    assert marked and marked == {i for i in every if i.startswith(smoke)}, marked


def test_the_dev_loop_maps_a_change_to_the_test_files_it_touches():
    ct = _tool("changed_tests")
    assert ct.tests_for(["tests/test_cli.py"]) == ["tests/test_cli.py"]
    regmap = ct.tests_for(["swi3s_studio/model/regmap_import.py"])
    assert "tests/test_regmap_import.py" in regmap and "tests/test_timing.py" not in regmap
    assert "tests/test_swi3score.py" in ct.tests_for(["native/swi3score/Decoder.cpp"])
    assert "tests/test_leak_scan.py" in ct.tests_for(["tools/leak_scan.py"])
    assert ct.tests_for(["docs/USER_GUIDE.md"]) == []
    every = ct.tests_for(["pyproject.toml"])
    assert len(every) == len(glob.glob(os.path.join(_ROOT, "tests", "test_*.py")))


def test_a_native_change_maps_to_every_test_reaching_the_api_or_the_session():
    """The core decodes for swi3s_studio.api and .session, so a test that decodes through
    either, without naming the core, is picked for a change under native/."""
    ct = _tool("changed_tests")
    picked = ct.tests_for(["native/swi3score/core/CCommandTransportParser.cpp"])
    assert "tests/test_export.py" in picked, "decodes through decode_capture, naming no core"
    assert set(ct.tests_for(["swi3s_studio/api.py"])) <= set(picked)
    assert set(ct.tests_for(["swi3s_studio/session.py"])) <= set(picked)


def test_the_report_reads_each_test_s_lines_from_coverage_data(tmp_path):
    import coverage
    mod = tmp_path / "subject.py"
    mod.write_text("def a():\n    return 1\n\n\ndef b():\n    return 2\n", encoding="utf-8")
    data = str(tmp_path / ".coverage")
    cov = coverage.Coverage(data_file=data, branch=True, include=[str(mod)], config_file=False)
    spec = importlib.util.spec_from_file_location("subject", mod)
    subject = importlib.util.module_from_spec(spec)
    cov.start()
    spec.loader.exec_module(subject)
    cov.switch_context("tests/test_x.py::test_a|run")
    subject.a()
    cov.switch_context("tests/test_x.py::test_b|run")
    subject.b()
    cov.stop()
    cov.save()
    lines = _tool("test_report")._per_test_lines(data)
    a, b = lines["tests/test_x.py::test_a"], lines["tests/test_x.py::test_b"]
    assert {n for _f, n in a} >= {2} and {n for _f, n in b} >= {6} and not ({2} & {n for _f, n in b})


def test_native_coverage_says_when_it_cannot_run(monkeypatch, capsys):
    nc = _tool("native_coverage")
    monkeypatch.setattr(shutil, "which", lambda name: None)
    monkeypatch.setattr(nc, "_llvm", lambda name: None)
    assert nc.main([]) == 3
    assert "cannot run here" in capsys.readouterr().out


@pytest.mark.parametrize("stage", ["guest", "release"])
def test_the_guest_runner_passes_its_stage_to_the_gate(stage):
    runner = pathlib.Path(_ROOT, "docs", "internal", "vm_gate.sh")
    if not runner.is_file():
        pytest.skip("the maintainers' tier is not in this tree")
    text = runner.read_text(encoding="utf-8")
    assert "gate.sh --$STAGE" in text and "gate.py --$STAGE" in text
    assert f"{stage})" in text or "guest|release)" in text


def test_the_matrix_runs_each_version_in_a_venv_and_folder_of_its_own():
    """vm_gate_matrix.sh gates each version with SWI3S_GUEST_PYVER, and vm_gate.sh gives that
    version its own venv, run folder and log on the guest."""
    internal = pathlib.Path(_ROOT, "docs", "internal")
    matrix, runner = internal / "vm_gate_matrix.sh", internal / "vm_gate.sh"
    if not matrix.is_file():
        pytest.skip("the maintainers' tier is not in this tree")
    m, r = matrix.read_text(encoding="utf-8"), runner.read_text(encoding="utf-8")
    assert 'SWI3S_GUEST_PYVER="$v"' in m and "tools/gate.py" in m
    assert 'TAG="$SHA${PYVER:+-py$PYVER}"' in r
    assert 'DIR="swi3s-gate-$TAG"' in r and 'LOG="gate-$TAG.log"' in r
    assert "~/swi3s-venvs/$PYVER" in r and r"swi3s-venvs\\$PYVER" in r


def _on_windows():
    return os.name == "nt"


def _matrix_script():
    if _on_windows():
        pytest.skip("the matrix runs on the Mac, and bash on Windows may be the WSL launcher")
    matrix = pathlib.Path(_ROOT, "docs", "internal", "vm_gate_matrix.sh")
    if not matrix.is_file():
        pytest.skip("the maintainers' tier is not in this tree")
    if shutil.which("bash") is None:
        pytest.skip("needs bash")
    return matrix


def test_the_matrix_verdict_tests_skip_on_windows(monkeypatch):
    """The matrix runs on the Mac only. On a Windows guest `bash` on the path may be the WSL
    launcher, which cannot open the Windows paths the verdict tests pass to `source`, so they
    skip there rather than fail."""
    monkeypatch.setattr(sys.modules[__name__], "_on_windows", lambda: True)
    with pytest.raises(pytest.skip.Exception, match="runs on the Mac"):
        _matrix_script()


def _verdict(matrix, tmp_path, raw, code):
    log = tmp_path / "gate.log"
    log.write_bytes(raw)
    out = subprocess.run(
        ["bash", "-c", 'source "$1" && verdict "$2" "$3"', "_", str(matrix), str(log), str(code)],
        capture_output=True, text=True, timeout=60, check=True,
    )
    return out.stdout.rstrip("\n")


_PASS_LOG = b"checks\nPASS: every applicable check of the guest gate is green; total 95 s\n"
_FAIL_LOG = (
    b"x\r\nFAIL (2):\r\n\r\n  - tests\r\n\r\n  - lint\r\n\r\nother; total 7 s\r\n"
).replace(b"\n", b"\x00\n")


def test_the_matrix_waits_for_a_stopped_gate_to_exit_before_it_removes_the_marker(tmp_path):
    """`wait_gone` returns only when every pid has exited: a process that ignores TERM for a
    while is waited for, and one still alive at the deadline is killed."""
    matrix = _matrix_script()
    slow = subprocess.Popen(["bash", "-c", 'trap "" TERM; sleep 1.5'])
    stuck = subprocess.Popen(["bash", "-c", 'trap "" TERM; sleep 30'])
    try:
        out = subprocess.run(
            ["bash", "-c", 'source "$1"; SECONDS=0; wait_gone 3 "$2" "$3"; echo $SECONDS',
             "_", str(matrix), str(slow.pid), str(stuck.pid)],
            capture_output=True, text=True, timeout=60, check=True)
        assert 3 <= int(out.stdout.strip()) < 10
        assert slow.wait(timeout=5) == 0
        assert stuck.wait(timeout=5) != 0
    finally:
        slow.kill()
        stuck.kill()


def test_the_matrix_verdict_reads_a_pass_summary(tmp_path):
    matrix = _matrix_script()
    assert _verdict(matrix, tmp_path, _PASS_LOG, 0) == "PASS|95|"
    # A green summary with a nonzero exit is not a pass.
    assert _verdict(matrix, tmp_path, _PASS_LOG, 1).startswith("FAIL|95|")


def test_the_matrix_verdict_lists_the_failures_of_a_windows_log(tmp_path):
    matrix = _matrix_script()
    assert _verdict(matrix, tmp_path, _FAIL_LOG, 1) == "FAIL|7|tests; lint; "


@pytest.mark.parametrize(
    ("code", "text"), [(124, "no result within the wait; "), (125, "guest never reached; ")]
)
def test_the_matrix_verdict_names_a_wait_timeout_and_an_unreached_guest(tmp_path, code, text):
    matrix = _matrix_script()
    assert _verdict(matrix, tmp_path, b"nothing\n", code) == f"FAIL|?|{text}"
    assert _verdict(matrix, tmp_path, _FAIL_LOG, code) == f"FAIL|7|{text}tests; lint; "


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals")
def test_the_matrix_stops_its_lanes_and_their_gates_on_sigterm_before_dropping_the_marker(tmp_path):
    matrix = _matrix_script()
    for tool in ("git", "pgrep"):
        if shutil.which(tool) is None:
            pytest.skip(f"needs {tool}")
    # The matrix adds a worktree of the commit it gates: it needs a git checkout, which a
    # test guest's exported tree is not.
    if not os.path.exists(os.path.join(_ROOT, ".git")):
        pytest.skip("needs a git checkout")
    venv = tmp_path / "venvs" / "3.11" / "bin"
    venv.mkdir(parents=True)
    pidfile = tmp_path / "gate.pid"
    fake = venv / "python"
    fake.write_text(f'#!/bin/sh\nsleep 300 &\necho $! > "{pidfile}"\nwait\n')
    fake.chmod(0o755)
    env = {k: v for k, v in os.environ.items() if not k.startswith("SWI3S_VM_")}
    env.update(SWI3S_MATRIX_DIR=str(tmp_path / "m"), SWI3S_MAC_VENVS=str(tmp_path / "venvs"),
               SWI3S_MATRIX_PYTHONS="3.11")
    proc = subprocess.Popen(["bash", str(matrix)], env=env, cwd=_ROOT,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.monotonic() + 60
        while not pidfile.is_file() or not pidfile.read_text().strip():
            assert time.monotonic() < deadline and proc.poll() is None, "the gate never started"
            time.sleep(0.1)
        gate = int(pidfile.read_text())
        marker = next((tmp_path / "m").glob("*/.running"))
        assert marker.is_file()
        proc.send_signal(signal.SIGTERM)
        assert proc.wait(timeout=60) == 143
        assert not marker.exists()
        with pytest.raises(ProcessLookupError):
            os.kill(gate, 0)
    finally:
        if proc.poll() is None:
            proc.kill()
