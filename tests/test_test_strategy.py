"""The tooling of docs/TESTING.md §1.1: markers, the guest scope, the dev loop's map from a
change to test files, the test report's reading of coverage data, and native coverage's
refusal where it cannot run."""
import glob
import importlib.util
import os
import pathlib
import re
import shutil

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_GUI = re.compile(r"QtWidgets|swi3s_studio\.ui|MainWindow|QApplication|QWidget")


def _tool(name):
    spec = importlib.util.spec_from_file_location(name, os.path.join(_ROOT, "tools", name + ".py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_a_test_file_that_builds_widgets_is_marked_gui():
    """The guest gate runs `gui` tests on every platform; a widget test without the marker
    would run on the development machine only."""
    unmarked = []
    for path in sorted(glob.glob(os.path.join(_ROOT, "tests", "test_*.py"))):
        text = pathlib.Path(path).read_text(encoding="utf-8")
        if os.path.basename(path) in ("test_perf.py", "test_test_strategy.py"):
            continue
        if _GUI.search(text) and not re.search(r"^pytestmark = .*pytest\.mark\.gui", text, re.M):
            unmarked.append(os.path.basename(path))
    assert not unmarked, f"build widgets but are not marked gui: {unmarked}"


def test_the_guest_scope_runs_gui_platform_and_the_core_smoke_files():
    gate = _tool("gate")
    runs = dict(gate._suite_files(guest=True))
    assert all(os.path.isfile(os.path.join(_ROOT, f)) for f in gate._CORE_SMOKE)
    assert all(os.path.isfile(os.path.join(_ROOT, f)) for f in gate._SPLIT)
    for f, marker in runs.items():
        want = "not perf" if f in gate._CORE_SMOKE else "(gui or platform) and not perf"
        assert marker == want, (f, marker)
    order = [f for f, _ in gate._suite_files(guest=False)]
    assert set(order[:len(gate._SPLIT)]) == set(gate._SPLIT), "the spread files start first"


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
