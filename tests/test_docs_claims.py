"""Statements in the docs that the code decides, checked against the code."""
import dataclasses
import os
import re

import pytest
from conftest import current_notes

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(*parts: str) -> str:
    with open(os.path.join(_ROOT, *parts), encoding="utf-8") as f:
        return f.read()


def test_a_register_cell_holds_a_value_and_a_provenance_and_the_docs_promise_no_write_time():
    from swi3s_studio.model import registers
    assert [f.name for f in dataclasses.fields(registers._Cell)] == ["value", "provenance"]
    assert "write timestamp" not in _read("docs", "architecture.md")


def test_the_subsystem_map_says_every_leg_holds_a_whole_view():
    from swi3s_studio.timing import calculator, timeline
    assert tuple(timeline.WHOLE_LEGS) == tuple(calculator.INEQUALITIES)
    doc = _read("docs", "architecture.md")
    assert "Every leg's timeline also holds its `WholeView`" in doc
    assert "A setup or hold leg's timeline also holds" not in doc


def test_the_dev_loop_doc_says_the_abi_check_catches_a_stale_core_only_after_a_bump():
    # The check compares one number with a constant; the runner of changed tests never builds.
    from swi3s_studio import session
    assert isinstance(session._REQUIRED_SCORE_ABI, int)
    assert "build_local" not in _read("tools", "changed_tests.py")
    doc = " ".join(_read("docs", "DEVELOPMENT.md").split())
    assert "rebuild the core or ~half the suite fails an ABI check" not in doc
    assert "only when the edit bumped `score_abi`" in doc
    assert "`changed_tests.py` does not rebuild it" in doc


def test_the_release_checklist_gates_the_release_commit_before_the_tag():
    doc = _read("docs", "DEVELOPMENT.md")
    checklist = doc[doc.index("## Release checklist"):doc.index("### Pre-flight before publishing")]
    steps = {int(m[1]): m[2] for m in re.finditer(r"^(\d)\. (.*?)(?=^\d\. |\Z)", checklist,
                                                  re.M | re.S)}
    step4 = " ".join(steps[4].split())
    assert "Run the release gate of step 2 again on the release commit" in step4
    assert "gate.sh --release" in step4 and "gate.ps1 --release" in step4
    assert "run by hand at the release commit" not in doc


@pytest.mark.gui
def test_the_user_guide_says_play_mixes_several_selected_ports_to_16_bit_mono():
    from swi3s_studio.ui import audio_view
    assert "MIXED to mono" in audio_view.AudioView.play.__doc__
    assert '"Int16"' in audio_view.AudioView._mix_selected.__doc__
    guide = " ".join(_read("docs", "USER_GUIDE.md").split())
    assert "streams the first selected port" not in guide
    assert ("several are mixed to mono at 48 kHz, each attenuated by 1/N, always at 16 bits"
            in guide)


def test_the_readme_and_contributing_do_not_put_the_diagram_cover_check_in_the_commit_gate():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "gate_for_docs", os.path.join(_ROOT, "tools", "gate.py"))
    gate = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gate)
    assert gate._CHECKS["diagram-cover"][1] == frozenset({"release"})
    readme = _read("README.md")
    commit_line = next(ln for ln in readme.splitlines() if ln.startswith("bash tests/gate.sh "))
    assert "diagram cover" not in commit_line
    contributing = " ".join(_read("CONTRIBUTING.md").split())
    assert "the term diagram's cover check" not in contributing
    assert "cover check runs under `--release` only" in contributing


def test_a_tag_requires_the_release_gate_on_each_guest_and_the_full_matrix_is_nightly():
    rule = "Windows on Python 3.14, Linux on one of 3.11–3.13"
    development = " ".join(_read("docs", "DEVELOPMENT.md").split())
    assert rule in development
    assert "runs nightly on the gate machine and is not a tag requirement" in development
    assert "nightly and before every tag" not in development
    assert "nightly and before a tag" not in development
    testing = " ".join(_read("docs", "TESTING.md").split())
    assert ("a Windows guest on 3.14 and a Linux guest on one of 3.11–3.13; the full 3.11–3.15 "
            "matrix runs nightly and is not a tag requirement") in testing
    assert "a Linux guest on 3.11–3.13)" not in testing


def test_the_maintainers_gate_notes_state_the_same_tag_requirement():
    notes = os.path.join(_ROOT, "docs", "internal", "vm_gate.md")
    if not os.path.isfile(notes):
        pytest.skip("the maintainers' tier is not in this tree")
    internal = " ".join(_read("docs", "internal", "vm_gate.md").split())
    assert "A tag does not require it" in internal
    assert "Windows on 3.14 and Linux on one of 3.11–3.13" in internal
    assert "and before every release tag" not in internal


def test_the_bug_report_guidance_says_where_macos_puts_about():
    assert "AboutRole" in _read("swi3s_studio", "ui", "main_window.py")
    text = " ".join(_read("CONTRIBUTING.md").split())
    assert "**Help ▸ About SWI3S Studio** (on macOS, SWI3S Studio ▸ About SWI3S Studio)" in text


def test_the_setup_dev_instructions_name_the_interpreter_the_gate_runs():
    for name, anchor in (("CONTRIBUTING.md", "tests/gate.sh --quick"),
                         ("README.md", "It prints the venv's activate command"),
                         (os.path.join("docs", "DEVELOPMENT.md"), "prints that venv's activate")):
        text = " ".join(_read(name).split())
        near = text[text.index(anchor):][:700]
        assert "PYTHON=" in near and "$env:PYTHON" in near and "gate" in near, name


def test_the_documented_parallel_pytest_command_keeps_each_file_on_one_worker():
    for name in ("README.md", os.path.join("docs", "DEVELOPMENT.md"),
                 os.path.join("docs", "TESTING.md")):
        for line in _read(name).splitlines():
            if "pytest" in line and "-n auto" in line:
                assert "--dist loadfile" in line, (name, line)


def test_the_readme_says_what_the_release_gate_runs_where_and_which_pythons_the_gate_covers():
    readme = " ".join(_read("README.md").split())
    assert "at the release commit —" not in readme
    assert "the term diagram's cover check" not in readme
    assert "diagrams' cover check" in readme
    assert "On other platforms 3.14 is unverified" not in readme
    dev = " ".join(_read("docs", "DEVELOPMENT.md").split())
    assert "every Python from 3.11 to 3.15 on all three platforms" in dev
    assert "3.11–3.15 on macOS, Windows and Linux" in readme


def test_published_docs_do_not_send_the_reader_to_the_maintainers_notes():
    dev = " ".join(_read("docs", "DEVELOPMENT.md").split())
    testing = " ".join(_read("docs", "TESTING.md").split())
    for phrase in ("maintainers' performance notes", "maintainers' spacing", "SWI3S_GATE_STAGE",
                   "queue watcher", "login agent"):
        assert phrase not in dev, phrase
    for phrase in ("maintainers' performance notes", "maintainers' .sal format",
                   "roadmap / memory notes"):
        assert phrase not in testing, phrase
    assert "tools/gate.py --release --on-guest" in dev
    assert "Cursor move / settle" in dev


def test_the_development_guide_states_rules_without_cost_comparisons_or_regression_history():
    dev = " ".join(_read("docs", "DEVELOPMENT.md").split())
    for phrase in ("at about the cost of an uncovered run", "several times slower",
                   "delegated to cheaper models", "regression class"):
        assert phrase not in dev, phrase
    assert "Coverage measures through `sys.monitoring` there (`COVERAGE_CORE=sysmon`)" in dev


@pytest.mark.gui
def test_the_style_guide_names_the_surfaces_that_build_the_action_button_recipe():
    from swi3s_studio.ui.authoring import authoring_panel
    assert "padding:5px" in authoring_panel._action_button_css()
    assert "padding: 4px 8px" in _read("swi3s_studio", "ui", "theme.py")
    guide = " ".join(_read("docs", "STYLE_GUIDE.md").split())
    assert "`5px` in the authoring panel (`_action_button_css`)" in guide
    assert "authoring and notifications panels" not in guide


@pytest.mark.gui
def test_the_style_guide_names_the_grid_constants_that_exist():
    from swi3s_studio.ui import grid_view
    assert grid_view._CW == grid_view._s(39) and grid_view._RH == grid_view._s(30)
    guide = " ".join(_read("docs", "STYLE_GUIDE.md").split())
    assert "COLUMN_SIZE" not in guide and "ROW_SIZE" not in guide
    assert "`_CW` and `_RH`" in guide
    assert "COLUMN_SIZE" not in _read("swi3s_studio", "ui", "grid_view.py")


def test_anchors_names_the_references_contention_legs_as_the_references():
    import glob
    for path in glob.glob(os.path.join(_ROOT, "swi3s_studio", "timing", "*.py")):
        with open(path, encoding="utf-8") as f:
            assert not re.search(r"\bnc_(pm|pp|mp)\b", f.read()), path
    doc = " ".join(_read("docs", "anchors.md").split())
    assert "the reference's `nc_pm` and `nc_pp` (this model's PM_contention and PP_contention)" in doc
    assert "the reference's `nc_mp` (MP_contention)" in doc


def test_a_hold_leg_reads_the_peripheral_distance_term_at_the_bus_length_minimum():
    guide = " ".join(_read("docs", "USER_GUIDE.md").split())
    anchors = " ".join(_read("docs", "anchors.md").split())
    assert "whose worst Peripheral sits at 0 cm, reads 0" not in guide
    assert "hold reads it at the Bus Length's minimum (0 with the default range)" in guide
    assert "where it is 0" not in anchors
    assert "P→M hold at the bus length's min (0 with the default range)" in anchors


def test_the_architecture_doc_states_the_decode_loop_as_a_rule_with_no_throughput_figure():
    doc = " ".join(_read("docs", "architecture.md").split())
    assert "35M UIs/s" not in doc and "65M-UI" not in doc
    assert "releases the GIL" in doc
    assert "`tests/test_perf.py`" in doc[doc.index("releases the GIL"):][:400]


def test_the_architecture_doc_describes_the_timing_pane_as_the_code_draws_it():
    doc = " ".join(_read("docs", "architecture.md").split())
    i = doc.index("**Timing** pane (pyqtgraph)")
    para = doc[i:doc.index("**Statistics** panel")]
    assert "eye\"" not in para and "verdict" not in para and "no time cursor" not in para
    assert "one audio-mode region at a time" in para and "follows the playhead" in para
    assert "View Worst UI" in para and "heading" not in para
    src = _read("swi3s_studio", "ui", "eye_view.py")
    assert "verdict" not in src.lower() and "eye diagram" not in src.lower()



def test_the_architecture_doc_draws_handover_arrows_only_in_visualization_mode():
    doc = " ".join(_read("docs", "architecture.md").split())
    analysis = doc[doc.index("key column"):doc.index("**Visualization mode** feeds it")]
    assert "handover arrows" not in analysis
    vis = doc[doc.index("**Visualization mode** feeds it"):][:700]
    assert "handover arrows" in vis and "never on a decoded grid" in vis
    assert "NOT synthesized for a decoded (analyzer) grid" in _read(
        "swi3s_studio", "ui", "grid_view.py")


@pytest.mark.gui
def test_the_tech_debt_list_holds_no_entry_for_the_removed_scale_control():
    from swi3s_studio.ui import diagram_style, timing_diagram
    assert timing_diagram.REDUCED_SCALE == 0.7
    assert "unmax_scale" in diagram_style.RETIRED
    path = os.path.join(_ROOT, "docs", "internal", "TECH_DEBT.md")
    if not os.path.exists(path):
        pytest.skip("the maintainers' tier is not in this tree")
    assert "Remove the not-maximized scale control" not in _read("docs", "internal", "TECH_DEBT.md")


def test_the_release_notes_state_the_worker_variable_as_the_code_reads_it():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "changed_tests_for_docs", os.path.join(_ROOT, "tools", "changed_tests.py"))
    assert spec is not None and spec.loader is not None
    changed_tests = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(changed_tests)
    assert changed_tests._parse(["--run"], {"SWI3S_TEST_WORKERS": "four"})[2] == "auto"
    assert changed_tests._parse(["--run"], {"SWI3S_TEST_WORKERS": "0"})[2] == "1"
    notes = current_notes()
    assert "defaulting to `$SWI3S_TEST_WORKERS` when it is set" not in notes
    assert ("defaulting to `$SWI3S_TEST_WORKERS` when it is a whole number (0 read as 1) and "
            "else to one per CPU") in notes
    assert "an option count that is not a whole number of at least 1 is refused" in notes


@pytest.mark.gui
def test_the_release_notes_say_an_on_off_flag_of_0_or_1_is_read():
    from swi3s_studio.store.audio_store import StreamProcessing
    row = StreamProcessing().to_json()
    sp = StreamProcessing.from_json({**row, "dc_block": 1, "allow_clipping": 0})
    assert sp is not None and sp.dc_block is True and sp.allow_clipping is False
    assert StreamProcessing.from_json({**row, "dc_block": "yes"}) is None
    notes = current_notes()
    assert "an on/off flag that is not true, false, 0 or 1" in notes
