"""The Example column's Manager launch rows (Man_t_DD, Man_t_ZD, Man_t_DZ) keep the user's
values across an F_CLK change, a to_dict/set_from_dict round trip and Load Example. Under
a clocked launch a value is kept on the mode's grid, and a restored value that has to move
is reported. Each leg's F_max is the first rate at which the margin the view shows when that
rate is typed in fails as the rate rises.

Also here: Load Example's refusals (a file that is not a mapping, expanding or oversized
YAML) and its readable forms (plain YAML, a byte-order mark); a saved selector choice that
is not a string is skipped and reported while the file's other keys are applied; the rate
box's own precision; and the recompute debounce (a burst of edits costs one recompute, a
discrete action or a read flushes it, closing or deleting the view cancels it)."""
import json
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication, QFileDialog

from swi3s_studio import import_notes
from swi3s_studio.timing import CalcInputs
from swi3s_studio.timing.spec_source import LaunchMode

pytestmark = pytest.mark.gui

_ROWS = ("Man_t_DD", "Man_t_ZD", "Man_t_DZ")


def _tv(mode: LaunchMode = LaunchMode.ANALOG):
    QApplication.instance() or QApplication([])
    from swi3s_studio.ui.timing_view import TimingView
    tv = TimingView()
    tv._launch_combo.setCurrentIndex(tv._launch_combo.findData(mode.value))
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


def _clocked_view(mode: str, f_MHz: float, man: str = "SWI3S_PHY2", per: str = "SWI3S_PHY2"):
    """The Example column under `mode` at `f_MHz`, the rate box widened to nine decimals so an
    F_max is typed in without rounding."""
    QApplication.instance() or QApplication([])
    from swi3s_studio.ui.timing_view import TimingView
    tv = TimingView()
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


def _shown_at(case, leg, f_MHz):
    """The margin the view shows for `leg` when the rate is typed in at the case's target."""
    tv = _clocked_view(*case)
    tv._fclk.setValue(f_MHz)
    return _shown(tv, leg)[0]


_PHY2_4X = ("4x", 12.288)
_PHY1_2X = ("2x", 6.6, "SWI3S_PHY1", "SWI3S_PHY2")


@pytest.mark.parametrize("case,leg,steps", [(_PHY2_4X, "MP_setup", True),
                                            (_PHY2_4X, "keeper_Man", True),
                                            (_PHY1_2X, "MP_setup", False)])
def test_a_clocked_fmax_is_where_the_shown_margin_reaches_zero(case, leg, steps):
    """Under a clocked launch the view re-places the launch rows when F_CLK changes, raising
    one that falls below its tabulated minimum to a further grid point. F_max follows those
    placements: typed in, it shows a zero margin, on PHY2 past at least one such step."""
    _margin, f_max = _shown(_clocked_view(*case), leg)
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
    raises the placement to 0.50 UI, up to PHY2's 13.2 MHz and beyond. F_max reads 12.167 MHz,
    so does the binding rate, and the F_max cell's tooltip names the windows past the step, the
    last ending at the toggle ceiling."""
    from swi3s_studio.timing import INEQUALITIES

    tv = _clocked_view(*_PHY2_4X)
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
    label = tv._column_labels()[1]
    tips = [kid.toolTip(2) for i in range(tv._tree.topLevelItemCount())
            for head in [tv._tree.topLevelItem(i)]
            for kid in (head.child(c) for c in range(head.childCount()))
            if kid.text(2) == f"{f_max:.3f} MHz" and label in kid.text(0)]
    assert any(note in tip for tip in tips), tips


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
    which raises through the excepthook; it is reported as not one of the choices, the
    selector keeps what it held, and the other keys of the same file are still applied."""
    names = {"man_spec": "_man_spec_combo", "per_spec": "_per_spec_combo",
             "sw_system_size": "_size_combo", "launch_mode": "_launch_combo",
             "pp_placement": "_pp_combo", "man_shape": "_man_shape", "per_shape": "_per_shape"}
    for key, attr in names.items():
        tv = _tv()
        combo = getattr(tv, attr)
        # Off its default, so a reset to the default shows.
        combo.setCurrentIndex(combo.count() - 1)
        tv._recompute = lambda *a, **k: None     # the results are not under test
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
            assert (combo.currentIndex(), combo.currentData()) == held, (key, value)
            assert tv._fclk.value() == saved["fclk_target"], (key, value)


def _count_recomputes(tv):
    """A list that gains one entry per recompute of the Specification column (each recompute
    evaluates every column once)."""
    calls = []
    real = tv._worst_results

    def counting(side):
        if side == "spec":
            calls.append(side)
        return real(side)

    tv._worst_results = counting
    return calls


def _wait_past_debounce():
    from PySide6.QtTest import QTest

    from swi3s_studio.ui.timing_view import _RECOMPUTE_DEBOUNCE_MS
    QTest.qWait(_RECOMPUTE_DEBOUNCE_MS + 150)


def _wait_until_debounced(tv, timeout_ms=5000):
    """Wait for the pending recompute to fire, however long a loaded machine takes."""
    from PySide6.QtTest import QTest
    waited = 0
    while tv._recompute_timer.isActive() and waited < timeout_ms:
        QTest.qWait(20)
        waited += 20
    assert not tv._recompute_timer.isActive()


def test_a_burst_of_typed_edits_costs_one_recompute():
    tv = _tv()
    calls = _count_recomputes(tv)
    box = _box(tv, "V_IL")
    for _ in range(5):
        before = box.value()
        box.setValue(before + box.singleStep())
        assert box.value() != before, "each edit is a change the box holds"
    tv._fclk.setValue(tv._fclk.value() + 0.1)
    assert calls == [] and tv._recompute_timer.isActive()
    _wait_until_debounced(tv)
    assert len(calls) == 1


def test_a_discrete_action_recomputes_at_once_and_drops_the_pending_one():
    tv = _tv()
    _box(tv, "V_IL").setValue(_box(tv, "V_IL").value() + 0.01)
    calls = _count_recomputes(tv)
    assert tv._recompute_timer.isActive()
    tv._launch_combo.setCurrentIndex(tv._launch_combo.findData(LaunchMode.CLK2.value))
    assert len(calls) == 1 and not tv._recompute_timer.isActive()
    tv._per_shape.setCurrentIndex(1 - tv._per_shape.currentIndex())
    assert len(calls) == 2
    tv.set_from_dict(tv.to_dict())
    assert len(calls) == 3
    _wait_past_debounce()
    assert len(calls) == 3


def test_reading_after_an_edit_flushes_the_pending_recompute():
    tv = _tv()
    before = tv.summary_text()
    box = _box(tv, "Per_t_IS")
    box.setValue(box.value() + 3.0)
    assert tv._recompute_timer.isActive()
    calls = _count_recomputes(tv)
    assert tv.summary_text() != before
    assert len(calls) == 1 and not tv._recompute_timer.isActive()
    tv.flush_recompute()
    assert len(calls) == 1, "nothing pending: a flush recomputes nothing"


def test_a_flush_shows_the_new_margins_in_the_tree():
    tv = _tv()
    margin = tv._worst["proposed"]["MP_setup"].breakdowns["MP_setup"].margin_ns
    box = _box(tv, "Per_t_IS")
    box.setValue(box.value() + 3.0)
    assert tv._worst["proposed"]["MP_setup"].breakdowns["MP_setup"].margin_ns == margin
    tv.flush_recompute()
    assert tv._worst["proposed"]["MP_setup"].breakdowns["MP_setup"].margin_ns != margin


def test_closing_the_view_cancels_a_pending_recompute_and_showing_it_runs_it():
    tv = _tv()
    tv.show()
    box = _box(tv, "V_IL")
    box.setValue(box.value() + 0.01)
    calls = _count_recomputes(tv)
    tv.close()
    assert not tv._recompute_timer.isActive()
    _wait_past_debounce()
    assert calls == []
    tv.show()
    assert len(calls) == 1
    tv.close()


def test_deleting_the_view_with_a_pending_recompute_fires_nothing():
    from shiboken6 import Shiboken
    tv = _tv()
    box = _box(tv, "V_IL")
    box.setValue(box.value() + 0.01)
    timer = tv._recompute_timer
    assert timer.parent() is tv and timer.isActive()
    calls = _count_recomputes(tv)
    tv.deleteLater()
    QApplication.sendPostedEvents(None, 0)
    _wait_past_debounce()
    assert not Shiboken.isValid(timer)
    assert calls == []
