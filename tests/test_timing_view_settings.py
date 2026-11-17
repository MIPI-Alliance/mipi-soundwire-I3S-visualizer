"""The Example column's Manager launch rows (Man_t_DD, Man_t_ZD, Man_t_DZ) keep the user's
values across an F_CLK change, a to_dict/set_from_dict round trip and Load Example Settings. Under
a clocked launch a value is kept on the mode's grid, and a restored value that has to move
is reported. Each leg's F_max is the first rate at which the margin the view shows when that
rate is typed in fails as the rate rises.

Also here: Load Example Settings's refusals (a file that is not a mapping, expanding or oversized
YAML) and its readable forms (plain YAML, a byte-order mark); a saved selector choice that
is not a string is skipped and reported while the file's other keys are applied; the rate
box's own precision; and what starts a recompute (a committed edit of a typed number that
changed it, once, and never a keystroke; a discrete control, once; a read, for a value set
from code; a show, for one set while the view was closed)."""
import json
import os
import re

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication, QFileDialog

from swi3s_studio import import_notes
from swi3s_studio.timing import CalcInputs
from swi3s_studio.timing.spec_source import LaunchMode

pytestmark = [pytest.mark.gui, pytest.mark.usefixtures("shared_compute")]

_ROWS = ("Man_t_DD", "Man_t_ZD", "Man_t_DZ")


def _tv(mode: LaunchMode = LaunchMode.ANALOG):
    QApplication.instance() or QApplication([])
    from swi3s_studio.ui.timing_view import TimingView
    tv = TimingView()
    tv._launch_combo.setCurrentIndex(tv._launch_combo.findData(mode.value))
    tv.flush_recompute()
    return tv


def _box(tv, name, end="max"):
    return (tv._max if end == "max" else tv._min)[("proposed", name)]


def _step_ns(tv, frac=0.25):
    return frac * 1000.0 * CalcInputs().duty_min / tv._fclk.value()


# Analog: an edit is kept as typed. Values sit inside each row's analog range and
# differ from the seeds, so a reseed would show.
_ANALOG_EDITS = {("Man_t_DD", "max"): 20.0, ("Man_t_DD", "min"): 10.0,
                 ("Man_t_ZD", "min"): 3.0, ("Man_t_ZD", "max"): 6.0,
                 ("Man_t_DZ", "min"): 0.1,
                 ("Man_t_DZ", "max"): 8.0}


def _edit(tv, edits):
    for (name, end), v in edits.items():
        _box(tv, name, end).setValue(v)


def _values(tv, edits):
    return {k: _box(tv, *k).value() for k in edits}


def test_analog_launch_edits_survive_an_fclk_change():
    tv = _tv()
    _edit(tv, _ANALOG_EDITS)
    tv._fclk.setValue(11.788)
    assert _values(tv, _ANALOG_EDITS) == pytest.approx(_ANALOG_EDITS)


def test_analog_launch_edits_survive_a_settings_round_trip_without_a_warning():
    tv = _tv()
    _edit(tv, _ANALOG_EDITS)
    d = tv.to_dict()
    tv2 = _tv()
    with import_notes.collect() as notes:
        tv2.set_from_dict(d, source="t.json")
    assert notes == []
    assert _values(tv2, _ANALOG_EDITS) == pytest.approx(_ANALOG_EDITS)
    assert tv2.summary_text() == tv.summary_text()


def test_load_example_keeps_analog_launch_rows(tmp_path, monkeypatch):
    tv = _tv()
    payload = tv._serialize_side("proposed")
    payload.update({"Man_t_DD": {"min": 10.0, "max": 20.0},
                    "Man_t_DZ": {"min": 1.0, "max": 8.0}})
    path = tmp_path / "example.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *a, **k: (str(path), ""))
    with import_notes.collect() as notes:
        tv._load_proposed()
    assert notes == []
    assert (_box(tv, "Man_t_DD", "min").value(), _box(tv, "Man_t_DD").value()) == (10.0, 20.0)
    assert (_box(tv, "Man_t_DZ", "min").value(), _box(tv, "Man_t_DZ").value()) == (1.0, 8.0)


def test_a_clocked_launch_row_survives_a_settings_round_trip():
    tv = _tv(LaunchMode.CLK4)
    step = _step_ns(tv)
    for end in ("min", "max"):
        _box(tv, "Man_t_ZD", end).setValue(2 * step)       # 0.50 UI
    tv._snap_launch_rows()
    placed = _box(tv, "Man_t_ZD").value()
    assert placed == pytest.approx(2 * step, abs=0.05)
    d = tv.to_dict()
    tv2 = _tv()
    with import_notes.collect() as notes:
        tv2.set_from_dict(d, source="t.json")
    assert notes == []
    assert _box(tv2, "Man_t_ZD").value() == placed
    assert tv2.summary_text() == tv.summary_text()


def test_a_clocked_launch_row_keeps_its_ui_fraction_across_an_fclk_change():
    tv = _tv(LaunchMode.CLK4)
    for end in ("min", "max"):
        _box(tv, "Man_t_ZD", end).setValue(3 * _step_ns(tv))   # 0.75 UI
    tv._snap_launch_rows()
    tv._fclk.setValue(11.788)
    assert _box(tv, "Man_t_ZD").value() == pytest.approx(3 * _step_ns(tv), abs=0.05)
    tv._fclk.setValue(12.288)
    assert _box(tv, "Man_t_ZD").value() == pytest.approx(3 * _step_ns(tv), abs=0.05)


def test_a_rate_step_that_raises_a_launch_row_to_its_floor_is_undone_by_the_step_back():
    """A box a rescale raises to the row's tabulated minimum remembers its UI fraction, so
    returning to the earlier rate (directly or by way of a third) gives back the placement
    the user had."""
    tv = _tv(LaunchMode.CLK4)
    tv._fclk.setValue(12.288)
    start = {(n, e): _box(tv, n, e).value() for n in _ROWS for e in ("min", "max")}
    for via in (13.2, 20.0, 1.0):
        tv._fclk.setValue(via)
        raised = {k: _box(tv, *k).value() for k in start}
        tv._fclk.setValue(12.288)
        assert {k: _box(tv, *k).value() for k in start} == pytest.approx(start), via
        if via == 13.2:
            assert any(raised[k] > start[k] + 1.0 for k in start), "the step raised no row"


def test_a_restored_off_grid_clocked_value_is_snapped_and_reported():
    tv = _tv(LaunchMode.CLK4)
    step = _step_ns(tv)
    d = tv.to_dict()
    d["proposed::Man_t_ZD::max"] = round(2.4 * step, 1)
    with import_notes.collect() as notes:
        tv.set_from_dict(d, source="t.json")
    assert _box(tv, "Man_t_ZD").value() == pytest.approx(2 * step, abs=0.05)
    assert len(notes) == 1 and notes[0].source == "t.json"
    assert notes[0].message.startswith(
        "1 Manager launch value(s) not on the 0.25 UI grid a 4x clock places, moved to "
        f"the nearest point: proposed::Man_t_ZD::max = {round(2.4 * step, 1):g} (set to ")


def test_load_example_snaps_and_reports_an_off_grid_clocked_value(tmp_path, monkeypatch):
    tv = _tv(LaunchMode.CLK4)
    step = _step_ns(tv)
    payload = tv._serialize_side("proposed")
    payload["Man_t_DD"] = {"min": round(2.4 * step, 1), "max": round(2.4 * step, 1)}
    path = tmp_path / "example.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *a, **k: (str(path), ""))
    with import_notes.collect() as notes:
        tv._load_proposed()
    assert _box(tv, "Man_t_DD").value() == pytest.approx(2 * step, abs=0.05)
    assert [n.source for n in notes] == ["example.json"]
    assert "2 Manager launch value(s) not on the 0.25 UI grid" in notes[0].message
    assert "Man_t_DD min = " in notes[0].message


def test_a_launch_mode_change_reseeds_the_rows():
    tv = _tv()
    seeded = {n: _box(tv, n).value() for n in _ROWS}
    _edit(tv, _ANALOG_EDITS)
    tv._launch_combo.setCurrentIndex(tv._launch_combo.findData(LaunchMode.CLK4.value))
    tv._launch_combo.setCurrentIndex(tv._launch_combo.findData(LaunchMode.ANALOG.value))
    assert {n: _box(tv, n).value() for n in _ROWS} == seeded


def _clocked_view(mode: str, f_MHz: float, man: str = "SWI3S_PHY2", per: str = "SWI3S_PHY2",
                  recompute: bool = True):
    """The Example column under `mode` at `f_MHz`, the rate box widened to nine decimals so an
    F_max is typed in without rounding. With `recompute` False the page runs no recompute
    (`_example_worst` reads its numbers)."""
    QApplication.instance() or QApplication([])
    from swi3s_studio.ui.timing_view import TimingView
    tv = TimingView()
    if not recompute:
        tv._job.discard()
        tv._job.submit = lambda fn: 0
    tv._man_spec_combo.setCurrentIndex(tv._man_spec_combo.findData(man))
    tv._per_spec_combo.setCurrentIndex(tv._per_spec_combo.findData(per))
    tv._launch_combo.setCurrentIndex(tv._launch_combo.findData(mode))
    tv._fclk.setDecimals(9)
    tv._fclk.setValue(f_MHz)
    return tv


def _shown(tv, leg):
    tv.flush_recompute()          # a typed edit's recompute is pending
    b = tv._worst["proposed"][leg].breakdowns[leg]
    return b.margin_ns, b.F_max_MHz


def _example_worst(tv, leg):
    """(margin, F_max) of `leg` in the Example column at the inputs the page reads, at the
    worst corner its recompute finds (`timing_view._worst_at`), computed here."""
    from swi3s_studio.ui import timing_view
    b = timing_view._worst_at(*tv._typ_baseline("proposed"))[0][leg].breakdowns[leg]
    return b.margin_ns, b.F_max_MHz


def _shown_at(case, leg, f_MHz):
    """The margin the view shows for `leg` when the rate is typed in at the case's target, from
    the inputs the page reads then (`_example_worst`)."""
    tv = _clocked_view(*case, recompute=False)
    tv._fclk.setValue(f_MHz)
    return _example_worst(tv, leg)[0]


_PHY2_4X = ("4x", 12.288)
_PHY1_2X = ("2x", 6.6, "SWI3S_PHY1", "SWI3S_PHY2")


@pytest.mark.parametrize("case,leg,steps", [(_PHY2_4X, "MP_setup", True),
                                            (_PHY2_4X, "keeper_Man", True),
                                            (_PHY1_2X, "MP_setup", False)])
def test_a_clocked_fmax_is_where_the_shown_margin_reaches_zero(case, leg, steps):
    """Under a clocked launch the view re-places the launch rows when F_CLK changes, raising
    one that falls below its tabulated minimum to a further grid point. F_max follows those
    placements: typed in, it shows a zero margin, on PHY2 past at least one such step."""
    _margin, f_max = _example_worst(_clocked_view(*case, recompute=False), leg)
    assert case[1] < f_max < 27.0
    tv = _clocked_view(*case)
    frac0 = _box(tv, "Man_t_DD").value() / _step_ns(tv, 1.0)
    tv._fclk.setValue(f_max)
    frac = _box(tv, "Man_t_DD").value() / _step_ns(tv, 1.0)
    assert (frac > frac0 + 0.1) == steps
    assert _shown(tv, leg)[0] == pytest.approx(0.0, abs=1e-6)


def test_a_clocked_fmax_at_a_placement_step_passes_just_below_and_fails_just_above():
    """A step that raises Man_t_ZD past what the setup leg can pay makes the shown
    margin jump through zero: it passes just below F_max and fails just above."""
    _margin, f_max = _shown(_clocked_view(*_PHY1_2X), "MP_setup_ho")
    assert _shown_at(_PHY1_2X, "MP_setup_ho", f_max * (1 - 1e-6)) >= 0.0
    assert _shown_at(_PHY1_2X, "MP_setup_ho", f_max * (1 + 1e-6)) < 0.0


def test_a_hold_failing_at_the_target_reads_its_first_failure():
    """F_max is the first failure as F_CLK rises. MP_hold on a 0.25 UI Man_t_DD passes at low
    rates, fails from 12.167 MHz, below the 12.288 MHz target, and passes again once the floor
    raises the placement to 0.50 UI, up to PHY2's 13.2 MHz and beyond. Its F_max is 12.167 MHz,
    so is the binding rate the launch hint names, and its note names the windows past the step,
    the last ending at the toggle ceiling. MP_hold holds no UI term, but its clocked
    Man_t_DD sits at a fraction of the UI, so its row shows that F_max, "(F<sub>MAX</sub>
    12.167 MHz)", with the note as its tooltip. One handover UI is allocated, so no handover
    leg fails at every rate."""
    from swi3s_studio.timing import INEQUALITIES

    tv = _clocked_view(*_PHY2_4X)
    tv._ho_ui_check.setChecked(True)
    margin, f_max = _shown(tv, "MP_hold")
    assert margin < 0.0 and f"{f_max:.3f}" == "12.167", (margin, f_max)
    worst = tv._worst["proposed"]
    first = next(iter(worst.values()))
    binding = min(worst[i]._rate_bound_MHz(worst[i].breakdowns[i]) for i in INEQUALITIES
                  if i not in first._PP_DATA_LEGS)
    assert f"{binding:.3f}" == "12.167", binding
    assert _shown_at(_PHY2_4X, "MP_hold", f_max * (1 - 1e-6)) >= 0.0
    assert _shown_at(_PHY2_4X, "MP_hold", f_max * (1 + 1e-6)) < 0.0
    for f in (12.75, 13.0, 13.2):
        assert _shown_at(_PHY2_4X, "MP_hold", f) > 0.0
    note = worst["MP_hold"].breakdowns["MP_hold"].F_max_note
    # The example `calculator._fmax_from_windows` quotes.
    assert note == ("passes again in 4 windows from 12.50 to 54.22 MHz: from 12.50 to "
                    "24.33 MHz, from 25.00 to 36.50 MHz, …, from 50.00 to 54.22 MHz"), note
    # MP_hold's clocked Man_t_DD makes its margin depend on the rate, so its row shows the
    # F_max and the note as its tooltip; the launch hint names the rate it binds.
    label = tv._column_labels()[1]
    from swi3s_studio.ui.timing_view import _FMAX_ROLE, _INEQ_NUMBER
    rows = [kid for i in range(tv._tree.topLevelItemCount())
            for head in [tv._tree.topLevelItem(i)]
            for kid in (head.child(c) for c in range(head.childCount()))]
    at = next(k for k, kid in enumerate(rows)
              if kid.text(0).startswith(f"{_INEQ_NUMBER['MP_hold']}) "))
    row = next(kid for kid in rows[at + 1:] if label in kid.text(0))
    assert " (F<sub>MAX</sub> " in row.text(0) and "12.167 MHz" in row.text(0), row.text(0)
    assert row.data(0, _FMAX_ROLE) == "12.167 MHz", row.data(0, _FMAX_ROLE)
    assert note in row.toolTip(0), row.toolTip(0)
    assert "viable to ~12.2 MHz (bound by hold)" in tv._launch_combo.toolTip(), \
        tv._launch_combo.toolTip()


def test_each_columns_binding_fmax_shows_beside_the_title_and_in_the_summary():
    """Beside the Timing Inequalities title, and in the copied summary after the margin table,
    each column names its binding F_max, the lowest over the legs that bound the bus, each at
    its own worst corner, and the inequality that sets it. On the Example column, PHY2/PHY2 at
    12.288 MHz with 4x CLK and one handover UI, that is MP_hold at 12.167 MHz, red as below the
    Clock Frequency Target. The line is empty while a recompute runs."""
    from swi3s_studio.timing import INEQUALITIES
    from swi3s_studio.ui.timing_view import _INEQ_NUMBER, _heading_title, _summary_label

    tv = _clocked_view(*_PHY2_4X)
    tv._ho_ui_check.setChecked(True)
    summary = tv.summary_text().splitlines()
    shown = tv._binding.text()
    assert shown.startswith("Binding F<sub>MAX</sub> — "), shown
    table = summary.index("")
    for side, lab in zip(("spec", "proposed"), tv._column_labels()):
        worst = tv._worst[side]
        f, leg = min(((worst[i].breakdowns[i].F_max_MHz, i) for i in INEQUALITIES
                      if i not in worst[i]._PP_DATA_LEGS
                      and not worst[i].breakdowns[i].not_applicable), key=lambda kv: kv[0])
        text = "no limit" if f == float("inf") else ("FAIL" if f <= 0 else f"{f:.3f} MHz")
        assert f"{lab}: " in shown and f"{text}" in shown and f"({_INEQ_NUMBER[leg]})" in shown
        assert (f"Binding F_MAX, {_summary_label(lab)}: {text}, {_heading_title(leg)}"
                in summary[table + 1:table + 3]), summary[table:table + 4]
        assert f"{_heading_title(leg)}" in tv._binding.toolTip()
        if side == "proposed":
            assert (f, leg) == (pytest.approx(12.167, abs=5e-4), "MP_hold")
            assert f'{lab}: <span style="color:' in shown and "12.167 MHz</span> (3)" in shown
    assert summary[table + 3:table + 5] == ["", "Terms"], summary[table:table + 6]
    tv._fclk.setValue(12.0)
    tv._recompute()
    assert tv._binding.text() == ""
    tv.flush_recompute()
    assert tv._binding.text().startswith("Binding F<sub>MAX</sub>")


def test_a_launch_row_keeps_its_ui_fraction_through_a_very_low_rate():
    """A launch box holds at most 10000 ns. Passing through a rate where the placement
    exceeds that returns the row to its UI fraction at the rate it started from."""
    tv = _tv(LaunchMode.CLK4)
    for end in ("min", "max"):
        _box(tv, "Man_t_ZD", end).setValue(3 * _step_ns(tv))   # 0.75 UI
    tv._snap_launch_rows()
    placed = _box(tv, "Man_t_ZD").value()
    tv._fclk.setValue(0.01)
    assert _box(tv, "Man_t_ZD").value() == _box(tv, "Man_t_ZD").maximum()
    tv._fclk.setValue(12.288)
    assert _box(tv, "Man_t_ZD").value() == pytest.approx(placed, abs=0.05)


def test_load_example_refuses_a_file_that_is_not_a_mapping(tmp_path, monkeypatch):
    shown = []
    from PySide6.QtWidgets import QMessageBox
    monkeypatch.setattr(QMessageBox, "critical", lambda *a, **k: shown.append(a[2]))
    monkeypatch.setattr(QFileDialog, "getOpenFileName",
                        lambda *a, **k: (str(tmp_path / "example.json"), ""))
    (tmp_path / "example.json").write_text("[1, 2]", encoding="utf-8")
    tv = _tv()
    before = tv.summary_text()
    tv._load_proposed()
    assert len(shown) == 1 and "mapping" in shown[0], shown
    assert tv.summary_text() == before


@pytest.mark.parametrize("name, text, said", [
    ("alias.yaml", "a: &a [1, 2]\nb: [*a, *a]\n", "aliases"),
    ("big.yaml", "# " + "x" * (1 << 20) + "\nMan_t_DD: {min: 10, max: 20}\n", "larger than"),
], ids=["aliases", "oversized"])
def test_load_example_refuses_yaml_that_expands_or_is_oversized(
        tmp_path, monkeypatch, name, text, said):
    """A YAML Example holds a mapping of rows: an alias (a few lines that expand without
    bound) and a file over the size cap are refused with a message, nothing is applied."""
    pytest.importorskip("yaml")
    from PySide6.QtWidgets import QMessageBox
    shown = []
    monkeypatch.setattr(QMessageBox, "critical", lambda *a, **k: shown.append(a[2]))
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *a, **k: (str(path), ""))
    tv = _tv()
    before = tv.summary_text()
    tv._load_proposed()
    assert len(shown) == 1 and said in shown[0], shown
    assert tv.summary_text() == before


def test_load_example_reads_a_plain_yaml_mapping(tmp_path, monkeypatch):
    pytest.importorskip("yaml")
    path = tmp_path / "example.yaml"
    path.write_text("Man_t_DD:\n  min: 10\n  max: 20\n", encoding="utf-8")
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *a, **k: (str(path), ""))
    tv = _tv()
    tv._load_proposed()
    assert (_box(tv, "Man_t_DD", "min").value(), _box(tv, "Man_t_DD").value()) == (10.0, 20.0)


def test_load_example_reads_a_file_with_a_byte_order_mark(tmp_path, monkeypatch):
    tv = _tv()
    payload = tv._serialize_side("proposed")
    payload["Man_t_DD"] = {"min": 10.0, "max": 20.0}
    path = tmp_path / "example.json"
    path.write_bytes(b"\xef\xbb\xbf" + json.dumps(payload).encode("utf-8"))
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *a, **k: (str(path), ""))
    tv._load_proposed()
    assert (_box(tv, "Man_t_DD", "min").value(), _box(tv, "Man_t_DD").value()) == (10.0, 20.0)


def test_an_fmax_typed_at_the_rate_boxs_own_precision_shows_a_margin_near_zero():
    """The rate box holds three decimals: F_max typed in as the box rounds it leaves the
    margin within a few picoseconds of zero."""
    from swi3s_studio.ui.timing_view import TimingView
    QApplication.instance() or QApplication([])
    tv = TimingView()
    tv._launch_combo.setCurrentIndex(tv._launch_combo.findData("4x"))
    tv._fclk.setValue(12.288)
    f_max = _shown(tv, "MP_setup")[1]
    assert tv._fclk.decimals() == 3
    tv._fclk.setValue(f_max)
    assert tv._fclk.value() == pytest.approx(f_max, abs=5e-4)
    assert abs(_shown(tv, "MP_setup")[0]) < 0.005


def test_a_saved_choice_that_is_not_a_string_is_skipped_and_reported():
    """A number past the C++ int range in a selector's slot must not reach Qt's findData,
    which raises through the excepthook; it is reported as not one of the choices, a spec,
    size, launch or placement selector keeps what it held, a ramp model takes its default
    (linear), and the other keys of the same file are still applied."""
    names = {"man_spec": "_man_spec_combo", "per_spec": "_per_spec_combo",
             "sw_system_size": "_size_combo", "launch_mode": "_launch_combo",
             "pp_placement": "_pp_combo", "man_shape": "_man_shape", "per_shape": "_per_shape"}
    for key, attr in names.items():
        tv = _tv()
        tv._recompute = lambda *a, **k: None     # the results are not under test
        combo = getattr(tv, attr)
        # Off its default, so a reset to the default shows.
        combo.setCurrentIndex(combo.count() - 1)
        tv.set_from_dict(tv.to_dict())
        held = (combo.currentIndex(), combo.currentData())
        assert held[0] != 0, key
        for n, value in enumerate((10 ** 30, 2 ** 63, 1.5, [1])):
            saved = tv.to_dict()
            saved[key] = value
            saved["fclk_target"] = 10.0 + 0.4 * n      # distinct, so each iteration must move it
            with import_notes.collect() as notes:
                tv.set_from_dict(saved, source="t.json")
            assert any(f"'{key}' {value!r} is not one of the choices" in n.message
                       for n in notes), (key, value)
            expected = (0, "linear") if key.endswith("_shape") else held
            assert (combo.currentIndex(), combo.currentData()) == expected, (key, value)
            assert tv._fclk.value() == saved["fclk_target"], (key, value)


def _count_recomputes(tv):
    """A list that gains one entry per recompute started (each evaluates every column once)."""
    calls = []
    real = tv._job.submit

    def counting(fn):
        calls.append(fn)
        return real(fn)

    tv._job.submit = counting
    return calls


def _on_screen(tv):
    tv.resize(1800, 1100)
    tv.show()
    tv.activateWindow()
    QApplication.processEvents()
    return tv


def _key(box, key):
    from PySide6.QtTest import QTest
    QTest.keyClick(box, key)


def _type(box, text):
    """Focus `box`, select its text and type `text` over it, committing nothing."""
    from PySide6.QtTest import QTest
    box.setFocus()
    box.selectAll()
    QTest.keyClicks(box, text)


@pytest.mark.parametrize("which", ["example", "fclk"])
def test_typing_into_a_cell_recomputes_only_when_the_edit_is_committed(which):
    """Digits typed into an Example cell or the rate box change the box's value and start no
    recompute, however long the user pauses; Enter commits and recomputes once. Enter again,
    a value typed back to what the last recompute read, and an arrow-key step not yet
    committed start none; focus leaving the box after an edit commits it."""
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    tv = _on_screen(_tv())
    box = tv._fclk if which == "fclk" else _box(tv, "Per_t_IS", "min")
    first, second = ("10.5", "11") if which == "fclk" else ("3.5", "4")
    calls = _count_recomputes(tv)
    _type(box, first)
    assert box.value() == float(first) and calls == []
    QTest.qWait(400)
    assert calls == [], "a pause while typing recomputes nothing"
    _key(box, Qt.Key_Return)
    assert len(calls) == 1
    _key(box, Qt.Key_Return)
    assert len(calls) == 1, "Enter with the value unchanged"
    _type(box, second)
    for _ in second:
        _key(box, Qt.Key_Backspace)
    QTest.keyClicks(box, first)
    _key(box, Qt.Key_Return)
    assert len(calls) == 1, "a value typed back to the one the last recompute read"
    _key(box, Qt.Key_Up)
    assert box.value() > float(first) and len(calls) == 1, "an arrow step waits for a commit"
    _key(box, Qt.Key_Return)
    assert len(calls) == 2
    _type(box, second)
    assert len(calls) == 2
    _key(box, Qt.Key_Tab)
    assert QApplication.focusWidget() is not box and len(calls) == 3
    box.setFocus()
    _key(box, Qt.Key_Tab)
    assert len(calls) == 3, "focus leaving the box with no edit"
    tv.flush_recompute()
    assert len(calls) == 3
    tv.close()


def _discrete_actions(tv, monkeypatch, tmp_path):
    """(name, action) for every control that is a discrete choice."""
    from PySide6.QtWidgets import QPushButton

    def other(combo):
        return lambda: combo.setCurrentIndex((combo.currentIndex() + 1) % combo.count())

    def button(text):
        return next(b for b in tv.findChildren(QPushButton) if b.text() == text).click

    path = tmp_path / "example.json"
    path.write_text(json.dumps({"Per_t_IS": {"min": 1.0, "max": 2.0}}))
    monkeypatch.setattr(QFileDialog, "getOpenFileName",
                        staticmethod(lambda *a, **k: (str(path), "")))
    return [("Manager spec", other(tv._man_spec_combo)),
            ("Peripheral spec", other(tv._per_spec_combo)),
            ("system size", other(tv._size_combo)),
            ("Manager ramp model", other(tv._man_shape)),
            ("Peripheral ramp model", other(tv._per_shape)),
            ("data launch", other(tv._launch_combo)),
            ("Peripheral Placement", other(tv._pp_combo)),
            ("Handover UI", tv._ho_ui_check.toggle),
            ("Manager forces keeper", tv._keeper_forced_check.toggle),
            ("Reset", button("Reset")),
            ("Load Example Settings", button("Load Example Settings")),
            ("settings restore", lambda: tv.set_from_dict(tv.to_dict()))]


def test_every_placement_token_round_trips_through_the_settings():
    """Each placement's persisted token is saved by to_dict, and set_from_dict selects that
    choice again, under its label and with no import warning, and hands it to both columns.
    The picker offers the five tokens under the labels the calculator names."""
    from swi3s_studio.timing.calculator import PP_PLACEMENT_LABELS
    tv = _tv()
    offered = {tv._pp_combo.itemData(i): tv._pp_combo.itemText(i)
               for i in range(tv._pp_combo.count())}
    assert offered == PP_PLACEMENT_LABELS == {
        "unknown": "Worst Per Inequality", "both_near": "All near the Manager",
        "driver_near": "Driver near, sampler far", "driver_far": "Driver far, sampler near",
        "both_far": "All at the far end"}
    for token, label in PP_PLACEMENT_LABELS.items():
        other = next(t for t in PP_PLACEMENT_LABELS if t != token)
        tv._pp_combo.setCurrentIndex(tv._pp_combo.findData(token))
        saved = tv.to_dict()
        assert saved["pp_placement"] == token
        tv2 = _tv()
        tv2._pp_combo.setCurrentIndex(tv2._pp_combo.findData(other))
        with import_notes.collect() as notes:
            tv2.set_from_dict(json.loads(json.dumps(saved)), source="t.json")
        assert notes == [], (token, notes)
        assert tv2._pp_combo.currentData() == token and tv2._pp_combo.currentText() == label
        assert {tv2.inputs(side).pp_placement for side in ("spec", "proposed")} == {token}


def test_each_discrete_control_recomputes_once_at_once(tmp_path):
    """A selector, a checkbox, Reset, Load Example Settings and a settings restore each start one
    recompute when acted on, and leave nothing for a read to recompute."""
    tv = _tv()
    with pytest.MonkeyPatch.context() as mp:
        for name, action in _discrete_actions(tv, mp, tmp_path):
            tv.flush_recompute()
            calls = _count_recomputes(tv)
            action()
            assert len(calls) == 1, name
            tv.flush_recompute()
            assert len(calls) == 1, name
            del tv._job.submit


def test_a_value_set_from_code_is_recomputed_by_the_next_read():
    tv = _tv()
    before = tv.summary_text()
    box = _box(tv, "Per_t_IS")
    calls = _count_recomputes(tv)
    box.setValue(box.value() + 3.0)
    assert calls == []
    assert tv.summary_text() != before
    assert len(calls) == 1
    tv.flush_recompute()
    assert len(calls) == 1, "nothing changed since: a flush recomputes nothing"


def test_a_flush_shows_the_new_margins_in_the_tree():
    tv = _tv()
    margin = tv._worst["proposed"]["MP_setup"].breakdowns["MP_setup"].margin_ns
    box = _box(tv, "Per_t_IS")
    box.setValue(box.value() + 3.0)
    assert tv._worst["proposed"]["MP_setup"].breakdowns["MP_setup"].margin_ns == margin
    tv.flush_recompute()
    assert tv._worst["proposed"]["MP_setup"].breakdowns["MP_setup"].margin_ns != margin


def test_showing_the_view_recomputes_a_value_set_while_it_was_closed():
    tv = _tv()
    tv.show()
    tv.close()
    calls = _count_recomputes(tv)
    box = _box(tv, "V_IL")
    box.setValue(box.value() + 0.01)
    assert calls == []
    tv.show()
    assert len(calls) == 1
    tv.close()


def _launch_state(tv):
    return {n: (_box(tv, n, "min").value(), _box(tv, n).value()) for n in _ROWS}


def _mp_hold(tv):
    tv.flush_recompute()
    return tv._worst["proposed"]["MP_hold"].breakdowns["MP_hold"].margin_ns


@pytest.mark.parametrize("row", [None, "Man_t_DD", "Man_t_ZD"])
def test_focus_leaving_an_unedited_launch_row_changes_nothing(row, monkeypatch):
    """4x launch, 12.288 -> 24.576 -> 12.288 MHz restores each row's UI fraction. Focus
    leaving a launch row at 24.576 with no edit leaves the round trip's result, the rows
    and MP_hold, as without it, and runs no recompute."""
    tv = _tv(LaunchMode.CLK4)
    tv._fclk.setValue(12.288)
    tv.flush_recompute()
    start = _launch_state(tv)
    tv._fclk.setValue(24.576)
    if row is not None:
        calls = []
        monkeypatch.setattr(tv, "_recompute", lambda *a, **k: calls.append(a))
        _box(tv, row).editingFinished.emit()
        assert calls == []
        monkeypatch.undo()
    tv._fclk.setValue(12.288)
    assert _launch_state(tv) == start
    assert _mp_hold(tv) == pytest.approx(-0.09, abs=0.01)


def test_an_edit_of_one_launch_row_keeps_the_others_rate_placement():
    """An edit of Man_t_DZ at 24.576 MHz forgets only Man_t_DZ's remembered placement:
    back at 12.288, Man_t_DD and Man_t_ZD return to their UI fraction."""
    tv = _tv(LaunchMode.CLK4)
    tv._fclk.setValue(12.288)
    tv.flush_recompute()
    start = _launch_state(tv)
    tv._fclk.setValue(24.576)
    tv.flush_recompute()
    box = _box(tv, "Man_t_DZ")
    box.setValue(box.value() + _step_ns(tv))
    box.editingFinished.emit()
    tv._fclk.setValue(12.288)
    after = _launch_state(tv)
    assert {n: after[n] for n in ("Man_t_DD", "Man_t_ZD")} == \
        {n: start[n] for n in ("Man_t_DD", "Man_t_ZD")}


def test_saved_settings_and_example_files_load_as_before_and_show_the_short_symbols(
        tmp_path, monkeypatch):
    """The settings and an Example file name rows, never a printed symbol, so a file the page
    wrote loads into a new page with no warning, gives the same summary, and the summary
    prints the crossings without ,late / ,early and the keeper's response as Man_t_Keeper."""
    tv = _tv()
    settings, payload = tv.to_dict(), tv._serialize_side("proposed")
    for key in [*settings, *payload]:
        assert not re.search(r"X_|Keeper", key), key
    path = tmp_path / "example.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *a, **k: (str(path), ""))
    tv2 = _tv()
    with import_notes.collect() as notes:
        tv2.set_from_dict(settings, source="t.json")
        tv2._load_proposed()
    assert notes == []
    summary = tv2.summary_text()
    assert summary == tv.summary_text()
    assert re.search(r"Man_t_Keeper(?!_)", summary) and "X_MP,rise" in summary
    assert not re.search(r",(late|early)\b|Keeper_Response", summary)


def test_a_partial_settings_file_seeds_what_it_leaves_out_for_the_pair_it_names():
    """Settings that name a pair but leave out F_CLK, the ramp models and the rows load as
    that pair's defaults, whatever the page held: the same inputs as choosing the pair by
    hand. A value the file holds is applied over the seeds."""
    ref = _tv()
    ref._man_spec_combo.setCurrentIndex(ref._man_spec_combo.findData("SW13_1V2"))
    tv = _tv()
    tv._fclk.setValue(10.0)
    tv._man_shape.setCurrentIndex(tv._man_shape.findData("exp"))
    tv._max[("proposed", "V_IH")].setValue(tv._max[("proposed", "V_IH")].value() - 0.1)
    tv._max[("spec", "V_IH")].setValue(tv._max[("spec", "V_IH")].value() - 0.1)
    with import_notes.collect() as notes:
        tv.set_from_dict({"man_spec": "SW13_1V2"}, source="t.json")
    assert [n.message for n in notes] == []
    assert tv.to_dict() == ref.to_dict()
    tv.set_from_dict({"man_spec": "SW13_1V2", "proposed::V_IH::max": 1.0})
    assert tv.to_dict() == {**ref.to_dict(), "proposed::V_IH::max": 1.0}


@pytest.mark.parametrize("mode", ["2x", "4x"])
def test_a_partial_settings_file_gives_an_omitted_launch_row_the_clocked_mode_default(mode):
    """A file that names a clocked launch mode and leaves the Manager launch rows out loads
    them as choosing that mode by hand does: one grid point each, and nothing reported."""
    ref = _tv()
    ref._launch_combo.setCurrentIndex(ref._launch_combo.findData(mode))
    tv = _tv()
    with import_notes.collect() as notes:
        tv.set_from_dict({"launch_mode": mode}, source="t.json")
    assert [n.message for n in notes] == []
    assert tv.to_dict() == ref.to_dict()


def test_a_legacy_phy_key_picks_the_pair_for_both_sides_and_a_bad_one_is_reported():
    """The one global `phy` key of an older file is 1 or 2 and sets both sides; any other
    value, a flag and text included, is reported and PHY2 is used. A file that names both
    sides itself is not asked about `phy`."""
    def load(d):
        tv = _tv()
        with import_notes.collect() as notes:
            tv.set_from_dict(d, source="t.json")
        return (tv._man_spec_combo.currentData(), tv._per_spec_combo.currentData(),
                [n.message for n in notes])
    assert load({"phy": 1}) == ("SWI3S_PHY1", "SWI3S_PHY1", [])
    assert load({"phy": 2}) == ("SWI3S_PHY2", "SWI3S_PHY2", [])
    for bad in ("1", 3, True, None):
        man, per, notes = load({"phy": bad})
        assert (man, per) == ("SWI3S_PHY2", "SWI3S_PHY2"), bad
        assert notes == [f"'phy' {bad!r} is not 1 or 2; SWI3S PHY2 is used"], bad
    both = {"phy": 3, "man_spec": "SWI3S_PHY1", "per_spec": "SWI3S_PHY1"}
    assert load(both) == ("SWI3S_PHY1", "SWI3S_PHY1", [])


def test_a_saved_specification_value_is_not_restored_and_a_differing_one_is_reported():
    """The read-only Specification column holds the selected pair's tables whatever a file
    saved for it; a saved number that differs from them is reported, one that matches is
    not, and the Example column's saved values are applied."""
    tv = _tv()
    d = tv.to_dict()
    table = d["spec::V_IH::max"]
    with import_notes.collect() as notes:
        _tv().set_from_dict(d, source="t.json")
    assert notes == []
    d["spec::V_IH::max"] = table - 0.1
    d["proposed::V_IH::max"] = table - 0.1
    with import_notes.collect() as notes:
        tv.set_from_dict(d, source="t.json")
    assert tv._max[("spec", "V_IH")].value() == table
    assert tv._max[("proposed", "V_IH")].value() == pytest.approx(table - 0.1)
    assert [n.message for n in notes] == [
        "1 Specification value(s) differ from this version's tables, which are used: "
        "spec::V_IH::max"]
