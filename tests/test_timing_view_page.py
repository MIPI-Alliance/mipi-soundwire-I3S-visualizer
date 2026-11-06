"""The Timing page's term keys, restored settings, hints, diagram pane and substitution rows, and
the Manager-forces-keeper switch: keeper_Man's rows, summary and diagram say it does not apply,
the flag round-trips, a non-boolean one is reported, and Reset clears it.

Also here: the spec selectors; Save / Load of the Example column; the launch hint's limit and
its bound (setup, hold or t_RF, each leg against its own toggle ceiling); the inequality rows'
colours and substitution signs; inputs the model cannot evaluate, the notice above the
results and ranges typed the wrong way round, exchanged on restore and when editing
finishes; focus and clicks that keep or clear the drawn term; the summary header, column
headings and the summary's Terms block; and one equation row per column where the columns'
terms differ, with each row's substitution summing to its column's margin."""
import json
import math
import os
from dataclasses import replace
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication, QFileDialog

from swi3s_studio import import_notes
from swi3s_studio.timing import CalcInputs, compute
from swi3s_studio.timing.spec_source import SPEC_SOURCES, LaunchMode, is_ede

pytestmark = pytest.mark.gui


def _tv():
    QApplication.instance() or QApplication([])
    from swi3s_studio.ui.timing_view import TimingView
    return TimingView()


def _notes(tv, d):
    with import_notes.collect() as notes:
        tv.set_from_dict(d, source="t.json")
    return [n.message for n in notes]


# ---- spec selectors --------------------------------------------------------------------


def test_the_spec_selectors_start_on_phy2_and_leave_out_only_the_proposed_revision():
    tv = _tv()
    for combo in (tv._man_spec_combo, tv._per_spec_combo):
        offered = [combo.itemData(i) for i in range(combo.count())]
        assert combo.currentData() == "SWI3S_PHY2"
        assert sorted(offered) == sorted(set(SPEC_SOURCES) - {"SWI3S_PHY2_PROP"})


# ---- term keys -------------------------------------------------------------------------


def test_an_ede_release_term_keys_on_the_row_it_reads():
    """Every printed EDE Delay and t_DZ term, on the legs that print one, keys on the input row
    of the same name."""
    from swi3s_studio.timing import default_swi3s_rows
    from swi3s_studio.ui.timing_view import _term_key
    rows = {r.name for r in default_swi3s_rows()}
    seen = set()
    for kw in ({}, {"Man_ede": True, "Per_ede": True}):
        for b in compute(replace(CalcInputs(), **kw)).breakdowns.values():
            for t in b.terms:
                if "_t_EDE" in t.symbol or "_t_DZ" in t.symbol:
                    assert _term_key(t.symbol) == t.symbol and t.symbol in rows, t.symbol
                    seen.add(t.symbol)
    assert seen == {"Man_t_EDE", "Per_t_EDE", "Man_t_DZ", "Per_t_DZ"}, seen


def test_a_soundwire_anchor_priced_on_the_fast_edge_keys_on_its_row():
    """A trailing `,fast` after an anchor suffix is dropped; a ramp named for its fast edge
    keys on its lane."""
    from swi3s_studio.ui.timing_view import _term_key
    for symbol, key in (("Man_t_DD,anchor,ff,fast", "Man_t_DD"),
                        ("Per_t_DD,anchor,rise,fast", "Per_t_DD"),
                        ("Per_t_DD,anchor,fall,fast", "Per_t_DD"),
                        ("Man_t_DD,anchor,rf,fast", "Man_t_DD"),
                        ("Man_t_ZD,anchor,fr", "Man_t_ZD"),
                        ("Man_t_RF,DATA,fall,fast", "t_RF Man DATA")):
        assert _term_key(symbol) == key, symbol


def test_each_row_an_ede_release_reads_washes_its_term():
    """An EDE contention leg shows its release as the releaser's t_DD, ramp, EDE Delay and the
    driven UI. A click on each of those terms washes the input row of the same name; UI has no
    input row, so it washes none."""
    from swi3s_studio.timing import default_swi3s_rows
    from swi3s_studio.ui.theme import VizTheme
    from swi3s_studio.ui.timing_view import _term_key
    rows = {r.name for r in default_swi3s_rows()}
    tv = _bus("SWI3S_PHY2_EDE", "SWI3S_PHY2_EDE")
    b = compute(replace(CalcInputs(), Man_ede=True, Per_ede=True)).breakdowns
    for leg, dev in (("MP_contention", "Man"), ("PM_contention", "Per"),
                     ("PP_contention", "Per")):
        keys = {_term_key(t.symbol) for t in b[leg].terms}
        wanted = [f"{dev}_t_DD", f"t_RF {dev} DATA", f"{dev}_t_EDE", "UI"]
        assert set(wanted) <= keys, (leg, keys)
        for key in wanted:
            tv._on_term_clicked(key, leg)
            washed = {n for n, lab in tv._row_labels.items()
                      if VizTheme.HILITE_BG in lab.styleSheet()}
            if key == "UI":
                assert key not in rows and not washed, (leg, washed)
            else:
                assert washed == {key}, (leg, key, washed)
            tv._on_term_clicked(key, leg)
            assert not tv._hl, (leg, key)


def test_every_row_less_key_is_reachable():
    from swi3s_studio.timing import default_swi3s_rows
    from swi3s_studio.timing.spec_source import LaunchMode as LM
    from swi3s_studio.ui.timing_view import _ROWLESS_TERM_KEYS, _term_key

    rows = {r.name for r in default_swi3s_rows()}
    keys = set()
    for kw in ({}, {"handover_UIs": 0.0}, {"Man_ede": True, "Per_ede": True},
               {"pp_placement": "driver_far"}, {"launch_mode": LM.CLK4},
               {"Man_spec": "SW13_1V2"}, {"Per_spec": "SW13_1V2"}, {"t_keeper_ns": 0.0}):
        for b in compute(replace(CalcInputs(), **kw)).breakdowns.values():
            keys |= {_term_key(t.symbol) for t in b.terms}
    unreachable = set(_ROWLESS_TERM_KEYS) - keys
    assert not unreachable, unreachable
    assert not (keys - rows - set(_ROWLESS_TERM_KEYS))


# ---- restored settings -----------------------------------------------------------------


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), "abc", None, [1.0], True])
def test_a_restored_value_that_is_not_a_number_is_kept_and_reported(bad):
    tv = _tv()
    before = tv._max[("proposed", "V_IH")].value()
    fclk = tv._fclk.value()
    d = tv.to_dict()
    d["proposed::V_IH::max"] = bad
    d["fclk_target"] = bad
    msgs = _notes(tv, d)
    assert tv._max[("proposed", "V_IH")].value() == before
    assert tv._fclk.value() == fclk
    assert len(msgs) == 1 and "not numbers, kept as they were" in msgs[0], msgs
    assert "proposed::V_IH::max" in msgs[0] and "fclk_target" in msgs[0]


def test_a_forced_keeper_shows_keeper_man_as_not_applicable_in_every_view():
    """With the switch on, both columns' keeper_Man rows read "does not apply" with "—" for
    margin and F_max, the summary prints "—" and the reason, a drawn keeper_Man term's
    diagram says so in each column, and keeper_Per is unchanged."""
    from swi3s_studio.ui.timing_view import _term_key
    tv = _tv()
    before = tv._per_inequality_worst("proposed")["keeper_Per"].margin_ns
    key = _term_key("Man_t_Keeper_Response")
    tv._hl = {key}
    tv._draw_term(key, "keeper_Man")
    tv._keeper_forced_check.setChecked(True)
    tv.flush_recompute()
    rows = [kid for i in range(tv._tree.topLevelItemCount())
            for head in [tv._tree.topLevelItem(i)]
            for kid in (head.child(c) for c in range(head.childCount()))
            if "does not apply" in kid.text(0)]
    assert [r.text(0).split(":")[0] for r in rows] == [f"16) {lab}"
                                                       for lab in tv._column_labels()]
    assert all((r.text(1), r.text(2)) == ("—", "—") for r in rows)
    summary = tv.summary_text().splitlines()
    assert "16) keeper Man | — | — | — | — | — | —" in summary
    assert sum("16)" in ln and "does not apply" in ln for ln in summary) == 2
    assert (tv._diagram_leg, tv._diagram_term) == ("keeper_Man", key)
    assert len(tv._diagram.panels) == 2
    assert all(pn.note.startswith("Does not apply:") for pn in tv._diagram.panels)
    assert tv._per_inequality_worst("proposed")["keeper_Per"].margin_ns == before


def test_the_forced_keeper_flag_round_trips_is_checked_and_reset_clears_it():
    tv = _tv()
    tv._keeper_forced_check.setChecked(True)
    d = tv.to_dict()
    assert d["man_keeper_forced"] is True
    tv._keeper_forced_check.setChecked(False)
    assert not _notes(tv, d) and tv._keeper_forced_check.isChecked()
    for value in ("true", 1, [True]):
        notes = _notes(tv, {"man_keeper_forced": value})
        assert any(f"'man_keeper_forced' {value!r} is not true or false" in n
                   for n in notes), value
        assert not tv._keeper_forced_check.isChecked()
    tv._keeper_forced_check.setChecked(True)
    assert not any("man_keeper_forced" in n for n in _notes(tv, {}))
    assert not tv._keeper_forced_check.isChecked()          # a missing flag is false
    tv._keeper_forced_check.setChecked(True)
    tv._reset_to_defaults()
    assert not tv._keeper_forced_check.isChecked()


def test_a_saved_handover_flag_that_is_not_a_boolean_is_reported_and_follows_the_pair():
    """A handover_ui that is text, a number or a list is reported, and the box takes the
    selected pair's setting, as a missing key does."""
    tv = _tv()
    pair = tv._ho_ui_check.isChecked()
    for value in ("false", "no", 0, 1, [], [True]):
        tv._ho_ui_check.setChecked(not pair)
        notes = _notes(tv, {"handover_ui": value})
        assert any(f"'handover_ui' {value!r} is not true or false" in n for n in notes), value
        assert tv._ho_ui_check.isChecked() == pair, value
    for value in (True, False):
        assert not any("handover_ui" in n for n in _notes(tv, {"handover_ui": value}))
        assert tv._ho_ui_check.isChecked() is value


def test_a_saved_number_given_as_text_is_reported_as_not_a_number():
    """A range end or the rate saved as a string keeps the box as it was and is reported."""
    tv = _tv()
    before = (tv._fclk.value(), tv._min[("proposed", "Man_t_DD")].value())
    notes = _notes(tv, {"fclk_target": "12.5", "proposed::Man_t_DD::min": "3"})
    assert any("not numbers" in n and "'12.5'" in n and "'3'" in n for n in notes), notes
    assert (tv._fclk.value(), tv._min[("proposed", "Man_t_DD")].value()) == before


def test_a_restored_range_with_its_minimum_above_its_maximum_is_exchanged_and_reported():
    tv = _tv()
    d = tv.to_dict()
    d["proposed::Per_t_IS::min"] = 5.0
    d["proposed::Per_t_IS::max"] = 2.0
    lo, hi = tv._min[("proposed", "Per_t_IS")], tv._max[("proposed", "Per_t_IS")]
    msgs = _notes(tv, d)
    assert (lo.value(), hi.value()) == (2.0, 5.0)
    assert msgs == ["1 range(s) with the minimum above the maximum, exchanged: "
                    "proposed::Per_t_IS"]


def test_load_example_reports_a_non_number_and_an_inverted_range(tmp_path, monkeypatch):
    tv = _tv()
    payload = tv._serialize_side("proposed")
    keep = payload["Man_t_IS"]["max"]
    payload["Man_t_IS"]["max"] = "abc"
    payload["Per_t_IS"] = {"min": 5.0, "max": 2.0}
    path = tmp_path / "example.json"
    path.write_text(json.dumps(payload))
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *a, **k: (str(path), ""))
    with import_notes.collect() as notes:
        tv._load_proposed()
    msgs = [n.message for n in notes]
    assert any("not numbers" in m and "Man_t_IS max" in m for m in msgs), msgs
    assert any("minimum above the maximum" in m and "proposed::Per_t_IS" in m for m in msgs)
    assert tv._max[("proposed", "Man_t_IS")].value() == keep
    assert tv._min[("proposed", "Per_t_IS")].value() == 2.0


def test_names_this_version_does_not_read_are_cut_in_the_warning():
    """An oversized key or row name in a file makes neither warning oversized."""
    from swi3s_studio.ui.timing_view import _SHOWN_VALUE_CHARS
    big = "k" * 50_000
    tv = _tv()
    msgs = _notes(tv, {big: 1})
    assert msgs and all(len(m) < 300 for m in msgs), [len(m) for m in msgs]
    assert "…" in msgs[0] and big not in msgs[0]
    payload = tv._serialize_side("proposed")
    payload[big] = {"min": 1.0, "max": 2.0}
    with import_notes.collect() as notes:
        tv._deserialize_side("proposed", payload, source="t.json")
    msgs = [n.message for n in notes]
    assert msgs and all(len(m) < 300 for m in msgs), [len(m) for m in msgs]
    assert any("k" * (_SHOWN_VALUE_CHARS - 2) + "…" in m for m in msgs)


def test_names_this_version_does_not_read_are_listed_three_at_a_time():
    """The unknown-name warnings list the first three names and count the rest, as every
    importer does (import_notes.names_listed)."""
    tv = _tv()
    msgs = _notes(tv, {f"unknown_{i}": 1 for i in range(5)})
    assert any("unknown_0, unknown_1, unknown_2 and 2 more" in m for m in msgs), msgs
    payload = tv._serialize_side("proposed")
    payload.update({f"row_{i}": {"min": 1.0, "max": 2.0} for i in range(4)})
    with import_notes.collect() as notes:
        tv._deserialize_side("proposed", payload, source="t.json")
    assert any("row_0, row_1, row_2 and 1 more" in n.message for n in notes), \
        [n.message for n in notes]


def test_save_example_asks_before_an_added_suffix_replaces_an_existing_file(tmp_path, monkeypatch):
    from PySide6.QtWidgets import QMessageBox
    existing = tmp_path / "ex.json"
    existing.write_text("earlier example")
    monkeypatch.setattr(QFileDialog, "getSaveFileName",
                        lambda *a, **k: (str(tmp_path / "ex"), ""))
    asked = []
    monkeypatch.setattr(QMessageBox, "question",
                        lambda _parent, _title, text, *a, **k: asked.append(text) or QMessageBox.No)
    _tv()._save_proposed()
    assert len(asked) == 1 and "ex.json" in asked[0]
    assert existing.read_text() == "earlier example"


def test_switching_to_a_soundwire_manager_with_a_clocked_launch_recomputes_once(monkeypatch):
    tv = _tv()
    _launch(tv, LaunchMode.CLK4)
    tv.flush_recompute()
    calls = []
    real = tv._worst_results
    monkeypatch.setattr(tv, "_worst_results", lambda side: (calls.append(side), real(side))[1])
    tv._man_spec_combo.setCurrentIndex(tv._man_spec_combo.findData("SW13_1V2"))
    tv.flush_recompute()
    assert tv._launch_combo.currentData() == LaunchMode.ANALOG.value
    assert calls == ["spec", "proposed"], calls


def test_the_launch_grid_is_placed_at_the_models_duty():
    from swi3s_studio.ui import timing_view
    assert timing_view._LAUNCH_DUTY == CalcInputs().duty_min


def test_save_example_writes_json_that_load_example_reads(tmp_path, monkeypatch):
    tv = _tv()
    tv._max[("proposed", "Man_t_IS")].setValue(4.5)
    path = tmp_path / "example.json"
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *a, **k: (str(path), ""))
    tv._save_proposed()
    assert json.loads(path.read_text())["Man_t_IS"]["max"] == 4.5
    tv._max[("proposed", "Man_t_IS")].setValue(3.0)
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *a, **k: (str(path), ""))
    tv._load_proposed()
    assert tv._max[("proposed", "Man_t_IS")].value() == 4.5


def test_load_example_without_a_yaml_reader_refuses_a_non_json_file(tmp_path, monkeypatch):
    from swi3s_studio.ui import timing_view
    tv = _tv()
    path = tmp_path / "example.yaml"
    path.write_text("Man_t_IS: {min: 1.0, max: 2.0}\n")
    shown = []
    monkeypatch.setattr(timing_view, "_HAVE_YAML", False)
    monkeypatch.setattr(timing_view.QMessageBox, "critical",
                        lambda *a, **k: shown.append(a[2]))
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *a, **k: (str(path), ""))
    tv._load_proposed()
    assert shown and "needs PyYAML" in shown[0]


def test_save_example_leaves_the_earlier_file_whole_when_a_write_fails(tmp_path, monkeypatch):
    from swi3s_studio.ui import timing_view
    tv = _tv()
    path = tmp_path / "example.json"
    path.write_text('{"kept": true}', encoding="utf-8")
    shown = []

    def failing_dump(obj, f, **kw):
        f.write("{ partial")
        raise OSError("disk full")

    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *a, **k: (str(path), ""))
    monkeypatch.setattr(timing_view.json, "dump", failing_dump)
    monkeypatch.setattr(timing_view.QMessageBox, "critical", lambda *a, **k: shown.append(a[2]))
    tv._save_proposed()
    assert shown == ["disk full"]
    assert path.read_text(encoding="utf-8") == '{"kept": true}'
    assert [p.name for p in tmp_path.iterdir()] == ["example.json"]


def test_save_example_stages_in_a_new_file_and_leaves_the_users_own_files_alone(
        tmp_path, monkeypatch):
    """The Example is written to a new, uniquely named sibling and moved over the target: a
    file named `<target>.tmp` beside it is neither overwritten nor removed, whether the save
    succeeds or the write fails, and no staging file is left."""
    tv = _tv()
    target = tmp_path / "example.json"
    scratch = tmp_path / "example.json.tmp"
    scratch.write_text("scratch")
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *a, **k: (str(target), ""))
    tv._save_proposed()
    assert json.loads(target.read_text(encoding="utf-8")) == tv._serialize_side("proposed")
    assert scratch.read_text() == "scratch"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["example.json", "example.json.tmp"]

    import swi3s_studio.ui.timing_view as view
    from swi3s_studio import fsutil
    shown = []
    monkeypatch.setattr(view.QMessageBox, "critical", lambda *a: shown.append(a[-1]))
    monkeypatch.setattr(fsutil.os, "replace", lambda *a: (_ for _ in ()).throw(OSError("full")))
    target.unlink()
    tv._save_proposed()
    assert shown == ["full"] and not target.exists()
    assert scratch.read_text() == "scratch"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["example.json.tmp"]


def test_load_example_caps_the_file_by_its_bytes(tmp_path, monkeypatch):
    """The size cap counts bytes on disk: a file of four-byte characters whose character count
    is under the cap is refused."""
    from swi3s_studio.ui import timing_view
    shown = []
    monkeypatch.setattr(timing_view.QMessageBox, "critical", lambda *a, **k: shown.append(a[2]))
    path = tmp_path / "example.json"
    chars = (timing_view._MAX_EXAMPLE_BYTES // 4) + 1000
    path.write_text("\U0001d11e" * chars, encoding="utf-8")
    assert chars < timing_view._MAX_EXAMPLE_BYTES < path.stat().st_size
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *a, **k: (str(path), ""))
    tv = _tv()
    tv._load_proposed()
    assert len(shown) == 1 and "larger than 1024 KiB" in shown[0], shown


def test_an_import_warning_shows_a_saved_value_at_a_bounded_length():
    tv = _tv()
    saved = tv.to_dict()
    saved["handover_ui"] = "x" * 5000
    saved["launch_mode"] = ["y"] * 5000
    saved["fclk_target"] = "z" * 5000
    with import_notes.collect() as notes:
        tv.set_from_dict(saved, source="t.json")
    msgs = [n.message for n in notes]
    assert any("'handover_ui'" in m for m in msgs) and any("'launch_mode'" in m for m in msgs)
    assert any("fclk_target" in m for m in msgs)
    assert all(len(m) < 300 for m in msgs), [len(m) for m in msgs]


def test_the_substitution_row_signs_every_coefficient_the_same_way():
    """A negative coefficient prints with a true minus, in the two-factor and the gathered
    form alike, and a coefficient that rounds to zero carries no sign."""
    from types import SimpleNamespace

    from swi3s_studio.ui.timing_view import TimingView
    num = TimingView._number

    def term(*factors):
        return SimpleNamespace(factors=factors, value_ns=1.0, op="+")

    assert "-" not in num(term(-0.04, -0.10, 8.30))
    assert num(term(-0.04, -0.10, 8.30)) == "(−0.04 − 0.10)·8.30"
    assert num(term(-0.04, 8.3)) == "(−0.04)·8.30"
    assert num(term(-0.001, 8.3)) == "0.00·8.30"
    assert num(term(0.04, -0.001, 8.3)) == "(0.04 + 0.00)·8.30"


# ---- hints -----------------------------------------------------------------------------


def test_the_trace_hint_says_a_click_draws_the_term():
    text = _tv()._trace_hint.text()
    assert "draw it on the right" in text and "inequality drawn" in text


def test_the_handover_hint_names_the_ede_source_for_a_zero_ui_handover():
    from swi3s_studio.timing.spec_source import SPEC_LABELS
    tv = _tv()
    tv._ho_ui_check.setChecked(False)
    text = tv._ho_hint.text()
    assert "no UI allocated" in text
    assert f"select {SPEC_LABELS['SWI3S_PHY2_EDE']} on a side" in text, text
    ede = next(i for i in range(tv._per_spec_combo.count())
               if is_ede(tv._per_spec_combo.itemData(i)))
    tv._per_spec_combo.setCurrentIndex(ede)
    tv._ho_ui_check.setChecked(False)
    assert "EDE Delay" in tv._ho_hint.text()


def _launch(tv, mode):
    tv._launch_combo.setCurrentIndex(tv._launch_combo.findData(mode.value))


@pytest.mark.parametrize("mode", [LaunchMode.CLK4, LaunchMode.CLK2])
def test_the_launch_hint_limit_is_the_pages_own_mp_limit(mode):
    tv = _tv()
    tv._fclk.setValue(12.288)
    _launch(tv, mode)
    legs = [tv._worst["proposed"][leg].breakdowns[leg]
            for leg in ("MP_setup", "MP_setup_ho", "MP_hold", "MP_hold_ho")]
    tight = min(legs, key=lambda b: b.F_max_MHz)
    text = tv._launch_hint.text()
    assert f"viable to ~{tight.F_max_MHz:.1f} MHz" in text, text
    assert f"bound by {'setup' if tight.is_setup else 'hold'}" in text
    assert ("above this mode's limit" in text) == (tv._fclk.value() > tight.F_max_MHz)
    grid = 0.25 if mode is LaunchMode.CLK4 else 0.5
    assert f"{grid:g} UI = " in text and f"editable on this clock's {grid:g} UI grid" in text


_MP_LEGS = ("MP_setup", "MP_setup_ho", "MP_hold", "MP_hold_ho")


def test_the_launch_hint_is_bound_by_t_rf_where_every_mp_leg_holds_to_the_ceiling():
    """With 12 ns edges on every lane and the windows, bus, slew tolerance, δ, α and t_PD
    mismatch at their floor, every MP leg under a 2x launch passes up to the toggle ceiling
    (1000 × 0.45 / 12 ns = 37.5 MHz), and the hint names t_RF as the bound."""
    from swi3s_studio.timing.calculator import toggle_ceiling_MHz
    tv = _tv()
    _launch(tv, LaunchMode.CLK2)
    for name in ("t_RF Man CLK", "t_RF Man DATA", "t_RF Per DATA"):
        tv._max[("proposed", name)].setValue(12.0)
        tv._min[("proposed", name)].setValue(12.0)
    for name in ("Per_t_IS", "Per_t_IH", "Man_t_IS", "Man_t_IH", "bus length (cm)",
                 "τ (per-edge slew)", "δ (V_SEOS tol)", "α (V_noise / V_SEOS)",
                 "t_PD,mis fraction"):
        for box in (tv._min[("proposed", name)], tv._max[("proposed", name)]):
            box.setValue(box.minimum())
    tv.flush_recompute()
    results = tv._worst["proposed"]
    for leg in _MP_LEGS:
        ceiling = toggle_ceiling_MHz(results[leg].inputs)
        assert ceiling == pytest.approx(37.5)
        assert results[leg].breakdowns[leg].F_max_MHz == pytest.approx(ceiling, rel=1e-12), leg
    assert "viable to ~37.5 MHz (bound by t_RF)" in tv._launch_hint.text()


def test_the_launch_limit_reads_each_legs_own_toggle_ceiling():
    """The limiting leg is judged against the ceiling of its own worst-corner inputs: a hold
    leg clipped at its own ceiling, lower than MP_setup's, is bound by t_RF; a hold leg below
    its ceiling is bound by hold."""
    from swi3s_studio.timing.calculator import toggle_ceiling_MHz
    tv = _tv()
    fast, slow = CalcInputs(tRF_Per_DATA_ns=12.0), CalcInputs(tRF_Per_DATA_ns=20.0)
    assert toggle_ceiling_MHz(fast) == pytest.approx(37.5)
    assert toggle_ceiling_MHz(slow) == pytest.approx(22.5)

    def result(leg, f_max, inputs):
        b = SimpleNamespace(F_max_MHz=f_max, is_setup="setup" in leg)
        return SimpleNamespace(breakdowns={leg: b}, inputs=inputs)

    def worst(hold_f_max):
        return {"proposed": {"MP_setup": result("MP_setup", 30.0, fast),
                             "MP_setup_ho": result("MP_setup_ho", 30.0, fast),
                             "MP_hold": result("MP_hold", hold_f_max, slow),
                             "MP_hold_ho": result("MP_hold_ho", math.inf, slow)}}
    tv._worst = worst(toggle_ceiling_MHz(slow))
    assert tv._launch_limit() == (pytest.approx(22.5), "t_RF")
    tv._worst = worst(20.0)
    assert tv._launch_limit() == (20.0, "hold")


def test_the_launch_hint_follows_an_input_change():
    tv = _tv()
    _launch(tv, LaunchMode.CLK2)
    before = tv._launch_hint.text()
    box = tv._max[("proposed", "Per_t_IS")]
    box.setValue(box.value() + 3.0)
    tv.flush_recompute()
    assert tv._launch_hint.text() != before


# ---- the diagram pane ------------------------------------------------------------------


def test_the_diagram_pane_closes_when_its_term_is_cleared():
    tv = _tv()
    tv._on_term_clicked("Per_t_IS", "MP_setup")
    assert tv._diagram_term == "Per_t_IS" and not tv._diagram_pane.isHidden()
    tv._clear_terms()
    assert tv._diagram_term == "" and tv._diagram_pane.isHidden()


def test_a_maximized_diagram_is_put_back_when_its_term_is_cleared():
    tv = _tv()
    tv._on_term_clicked("Per_t_IS", "MP_setup")
    tv.set_diagram_maximized(True)
    tv._clear_terms()
    assert not tv.is_diagram_maximized() and tv._diagram_pane.isHidden()
    assert not tv._ineq_scroll.isHidden()


def test_neither_side_of_the_splitter_collapses_to_a_sliver():
    from swi3s_studio.ui.timing_view import _px
    tv = _tv()
    assert not tv._splitter.childrenCollapsible()
    assert tv._diagram_pane.minimumWidth() == tv._diagram_min_width()
    assert tv._diagram_min_width() >= 22 * tv.fontMetrics().height()
    assert tv._ineq_scroll.minimumWidth() == _px(240)


def test_the_diagram_corner_line_names_non_typical_rows_only():
    tv = _tv()
    tv._on_term_clicked("Per_t_IS", "MP_setup")
    tv._worst_picks["proposed"]["MP_setup"] = {"Per_t_IS": "typ", "Man_t_DD": "max"}
    tv._worst_picks["spec"]["MP_setup"] = {"Per_t_IS": "typ"}
    tv._show_diagram()
    spec, example = tv._diagram.panels
    assert spec.corner.startswith("Corner: every row typical")
    assert example.corner.startswith("Corner: ") and "every row typical" not in example.corner
    assert "Man_t<sub>DD</sub> max" in example.corner, example.corner
    assert "Per_t<sub>IS</sub>" not in example.corner


# ---- the inequality rows ---------------------------------------------------------------


def test_an_f_max_below_the_rate_is_red_and_one_that_never_fails_is_not():
    """A leg that fails below the clock rate shows its F_max in red; a leg that never fails
    shows "—" unmarked, setup or hold."""
    from swi3s_studio.ui.timing_view import _red
    tv = _tv()
    parent = tv._tree.topLevelItem(0)
    fclk = tv._fclk.value()

    def shown(b):
        item = tv._add_side_term(parent, "x", b, fclk)
        return item.text(2), item.foreground(2).color()

    setup = tv._worst["spec"]["MP_setup"].breakdowns["MP_setup"]
    hold = tv._worst["spec"]["MP_hold"].breakdowns["MP_hold"]
    assert setup.is_setup and not hold.is_setup and math.isfinite(setup.F_max_MHz)
    for b in (setup, hold):
        text, colour = shown(replace(b, F_max_MHz=math.inf))
        assert text == "—" and colour != _red(), b.is_setup
        text, colour = shown(replace(b, F_max_MHz=0.5 * fclk))
        assert colour == _red(), b.is_setup
        text, colour = shown(replace(b, F_max_MHz=2.0 * fclk))
        assert colour != _red(), b.is_setup


def test_a_negative_crossing_coefficient_is_parenthesised_in_the_substitution_row():
    from swi3s_studio.ui.timing_view import TimingView
    term = SimpleNamespace(symbol="X_MP,fall,early · Man_t_RF,DATA", value_ns=-0.33, op="+",
                           factors=(-0.0395, 8.3))
    text = TimingView._numeric_only([term])
    assert "(−0.04)·8.30" in text and "-0.04" not in text
    pos = SimpleNamespace(symbol="X_MP,rise,late · Man_t_RF,DATA", value_ns=1.0, op="+",
                          factors=(0.12, 8.3))
    assert "0.12·8.30" in TimingView._numeric_only([pos])


def test_the_summary_reads_the_last_recompute(monkeypatch):
    tv = _tv()
    calls = []
    real = tv._worst_results
    monkeypatch.setattr(tv, "_worst_results", lambda side: (calls.append(side), real(side))[1])
    first = tv.summary_text()
    assert calls == [] and first.count("\n") >= 17
    tv._worst = {}
    tv.summary_text()
    assert calls == ["spec", "proposed"]


# ---- inputs the model cannot evaluate, and ranges typed the wrong way round -----------


_TAU = "τ (per-edge slew)"


def test_a_slew_tolerance_of_100_percent_shows_no_margins_and_recovers():
    tv = _tv()
    shown = tv.summary_text()
    box = tv._max[("proposed", _TAU)]
    box.setValue(100.0)
    tv.flush_recompute()
    assert tv._compute_error and tv._tree.topLevelItemCount() == 0
    assert tv._ceiling_label.text().startswith("⚠ No margins.")
    assert "No margins" in tv.summary_text()
    with import_notes.collect() as notes:
        tv.set_from_dict(tv.to_dict(), source="t.json")
    assert any("cannot evaluate" in n.message for n in notes)
    box.setValue(5.0)
    tv.flush_recompute()
    assert not tv._compute_error and tv._tree.topLevelItemCount() > 0
    assert tv.summary_text() == shown


def _shown_view():
    tv = _tv()
    tv.resize(1800, 1100)
    tv.show()
    QApplication.processEvents()
    return tv


def _unevaluable(tv):
    tv._max[("proposed", _TAU)].setValue(100.0)
    tv.flush_recompute()
    assert tv._compute_error and tv._tree.topLevelItemCount() == 0


def _draw_first_term(tv):
    """Highlight and draw the first term of the first inequality; returns (key, leg)."""
    from swi3s_studio.timing.calculator import INEQUALITIES
    leg = INEQUALITIES[0]
    key = _own_keys(tv, "spec", leg)[0]
    tv._on_term_clicked(key, leg)
    assert tv._diagram_term == key and not tv._diagram_pane.isHidden()
    return key, leg


def test_an_unevaluable_edit_on_a_maximized_page_is_explained_above_the_results():
    """The advisory line sits in the top bar, which a maximized page hides, so the reason
    is also shown above the results while it is hidden."""
    tv = _shown_view()
    tv.set_maximized(True)
    assert not tv._ceiling_label.isVisibleTo(tv)
    _unevaluable(tv)
    assert tv._results_notice.isVisibleTo(tv)
    assert tv._results_notice.text().startswith("⚠ No margins.")
    tv._max[("proposed", _TAU)].setValue(5.0)
    tv.flush_recompute()
    assert not tv._compute_error and not tv._results_notice.isVisibleTo(tv)
    _unevaluable(tv)
    tv.set_maximized(False)
    assert tv._ceiling_label.isVisibleTo(tv) and not tv._results_notice.isVisibleTo(tv)
    tv.close()


def test_an_unevaluable_edit_closes_the_term_diagram_even_when_it_is_maximized():
    tv = _shown_view()
    _draw_first_term(tv)
    tv.set_diagram_maximized(True)
    assert tv.is_diagram_maximized()
    _unevaluable(tv)
    assert tv._diagram_term == "" and tv._diagram_leg is None
    assert tv._diagram_pane.isHidden() and not tv.is_diagram_maximized()
    assert tv._ineq_scroll.isVisibleTo(tv) and tv._ceiling_label.isVisibleTo(tv)
    tv._max[("proposed", _TAU)].setValue(5.0)
    tv.flush_recompute()
    assert tv._diagram_pane.isHidden(), "recovering does not reopen a closed diagram"
    tv.close()


def test_maximizing_the_diagram_with_no_term_drawn_leaves_the_page_as_it_is():
    tv = _shown_view()
    tv.set_diagram_maximized(True)
    assert not tv.is_diagram_maximized()
    assert tv._ineq_scroll.isVisibleTo(tv) and tv._topbar.isVisibleTo(tv)
    tv.close()


def test_a_click_on_a_margin_or_a_max_frequency_value_does_not_clear_the_term():
    """A value in the Margin or Max Frequency column is not blank space: a click on it keeps
    the highlight and the diagram. A click in the empty area below the rows clears them."""
    from PySide6.QtCore import QPoint, Qt
    from PySide6.QtTest import QTest
    tv = _shown_view()
    key, leg = _draw_first_term(tv)
    tree = tv._tree
    for col in (1, 2):
        item = next(it for it in (tree.topLevelItem(0).child(c)
                                  for c in range(tree.topLevelItem(0).childCount()))
                    if it.text(col))
        rect = tree.visualRect(tree.indexFromItem(item, col))
        QTest.mouseClick(tree.viewport(), Qt.LeftButton, pos=rect.center())
        assert tv._hl == {key} and tv._diagram_term == key, col
    spacer = next(tree.indexFromItem(tree.topLevelItem(i), 0)
                  for i in range(tree.topLevelItemCount())
                  if not tree.topLevelItem(i).text(0))
    QTest.mouseClick(tree.viewport(), Qt.LeftButton,
                     pos=QPoint(5, tree.visualRect(spacer).center().y()))
    assert tv._hl == set() and tv._diagram_term == ""
    tv.close()


def test_a_typed_minimum_above_the_maximum_is_exchanged_when_editing_finishes():
    tv = _tv()
    lo, hi = tv._min[("proposed", "V_IL")], tv._max[("proposed", "V_IL")]
    lo.setValue(0.75)
    hi.setValue(0.55)
    lo.editingFinished.emit()
    assert (lo.value(), hi.value()) == (0.55, 0.75)
    assert "minimum above the maximum, exchanged" in tv._ceiling_label.text()
    ordered = _tv()
    ordered._min[("proposed", "V_IL")].setValue(0.55)
    ordered._max[("proposed", "V_IL")].setValue(0.75)
    assert tv.summary_text() == ordered.summary_text()
    hi.editingFinished.emit()
    assert "exchanged" not in tv._ceiling_label.text()


def _type_into(box, text):
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    box.setFocus()
    box.selectAll()
    QTest.keyClicks(box, text)
    QTest.keyClick(box, Qt.Key_Tab)


def test_a_range_typed_upward_box_by_box_is_not_exchanged_between_the_boxes():
    """Shifting a range up types the minimum first, above the old maximum, and tabs to the
    maximum: the range is checked once focus has left both boxes, so the typed 20 / 30 stays
    and nothing is reported. A range left inverted when focus leaves both is exchanged."""
    tv = _tv()
    tv.resize(1800, 1100)
    tv.show()
    tv.activateWindow()
    QApplication.processEvents()
    lo, hi = tv._min[("proposed", "Per_t_DD")], tv._max[("proposed", "Per_t_DD")]
    assert (lo.value(), hi.value()) == (2.0, 9.0)
    _type_into(lo, "20")
    assert QApplication.focusWidget() is hi
    assert (lo.value(), hi.value()) == (20.0, 9.0)
    assert "exchanged" not in tv._ceiling_label.text()
    _type_into(hi, "30")
    assert (lo.value(), hi.value()) == (20.0, 30.0)
    assert "exchanged" not in tv._ceiling_label.text()
    _type_into(lo, "40")
    _type_into(hi, "")
    assert (lo.value(), hi.value()) == (30.0, 40.0)
    assert "minimum above the maximum, exchanged" in tv._ceiling_label.text()
    tv.close()


def test_the_summary_names_a_column_one_way_throughout_and_fills_its_f_max_difference():
    """The header and the Terms lines of one summary name a column alike, and the last
    column holds the right column's F_max less the left's where both are rates."""
    import re

    from swi3s_studio.ui.timing_view import _summary_label
    for man, per in _BUSES:
        tv = _bus(man, per)
        lines = tv.summary_text().splitlines()
        header = lines[0].split(" | ")
        names = [_summary_label(lab) for lab in tv._column_labels()]
        assert [h.removesuffix(" margin (ns)") for h in header[1:3]] == names
        terms = lines[lines.index("Terms") + 1:]
        seen = {m.group(1) for ln in terms
                for m in [re.match(r"(?:\d+\) )?\s*(Spec|PHY2 Proposed|EDE Proposed|Example):",
                                   ln)] if m}
        assert seen <= set(names), (seen, names)
        rows = lines[1:lines.index("")]
        for row in rows:
            cells = row.split(" | ")
            left, right, delta = cells[4], cells[5], cells[6]
            if left.replace(".", "").isdigit() and right.replace(".", "").isdigit():
                assert float(delta) == pytest.approx(float(right) - float(left),
                                                     abs=0.0011), row
            else:
                assert delta == "—", row
    tv = _tv()
    rows = tv.summary_text().splitlines()[1:18]
    assert any(r.split(" | ")[6] != "—" for r in rows)


def test_the_launch_hint_names_the_columns_the_page_shows():
    """A clocked mode's hint names the column the selector drives and the one that keeps its
    analog delays by their headings on the page. In analog mode on an EDE bus both columns
    use the tabulated ranges, and the seeding sentence appears only where the Manager is on
    the EDE source."""
    for man, per, mode, left, right in (
            ("SWI3S_PHY2", "SWI3S_PHY2", "4x", "Specification", "Example"),
            ("SWI3S_PHY2_EDE", "SWI3S_PHY2", "4x", "Spec / PHY2 Proposed",
             "Example / EDE Proposed"),
            ("SWI3S_PHY2", "SWI3S_PHY2_EDE", "2x", "Spec / PHY2 Proposed",
             "Example / EDE Proposed"),
            ("SWI3S_PHY2_EDE", "SWI3S_PHY2_EDE", "2x", "PHY2 Proposed", "EDE Proposed")):
        tv = _bus(man, per)
        tv._launch_combo.setCurrentIndex(tv._launch_combo.findData(mode))
        assert tv._column_labels() == (left, right)
        text = tv._launch_hint.text()
        assert f"applies to the {right} column only; the {left} column keeps its analog" in text
        assert "Specification" not in text or left == "Specification", text
        assert "Spec / " not in text or left.startswith("Spec / "), text
        assert "seeded with" not in text and "both columns" not in text, text
    for man, per, seeded in (("SWI3S_PHY2_EDE", "SWI3S_PHY2", True),
                             ("SWI3S_PHY2", "SWI3S_PHY2_EDE", False)):
        tv = _bus(man, per)
        tv._launch_combo.setCurrentIndex(tv._launch_combo.findData("analog"))
        text = tv._launch_hint.text()
        assert "both columns use the tabulated delay ranges" in text, text
        assert "applies to" not in text and "keeps its analog" not in text, text
        assert ("is seeded with the analog" in text) is seeded, text
    tv = _bus("SWI3S_PHY2", "SWI3S_PHY2")
    tv._launch_combo.setCurrentIndex(tv._launch_combo.findData("analog"))
    assert "applies to" not in tv._launch_hint.text()
    assert "both columns" not in tv._launch_hint.text()


def test_the_summary_header_names_the_columns_the_page_shows():
    """The copyable summary heads its columns with the page's column names: Spec and Example
    on a plain bus, PHY2 Proposed and EDE Proposed with both sides on the EDE source, and
    both sides' names (non-EDE side first) with one."""
    tv = _tv()
    assert tv._column_labels() == ("Specification", "Example")
    plain = tv.summary_text().splitlines()[0]
    assert plain == ("Inequality | Spec margin (ns) | Example margin (ns) | ΔMargin (ns) | "
                     "Spec F_max (MHz) | Example F_max (MHz) | ΔF_max (MHz)")
    for man, per, left, right in (
            ("SWI3S_PHY2_EDE", "SWI3S_PHY2_EDE", "PHY2 Proposed", "EDE Proposed"),
            ("SWI3S_PHY2", "SWI3S_PHY2_EDE", "Spec / PHY2 Proposed", "Example / EDE Proposed"),
            ("SWI3S_PHY2_EDE", "SWI3S_PHY2", "Spec / PHY2 Proposed", "Example / EDE Proposed")):
        tv._man_spec_combo.setCurrentIndex(tv._man_spec_combo.findData(man))
        tv._per_spec_combo.setCurrentIndex(tv._per_spec_combo.findData(per))
        tv.flush_recompute()
        assert tv._column_labels() == (left, right), (man, per)
        assert tv.summary_text().splitlines()[0] == (
            f"Inequality | {left} margin (ns) | {right} margin (ns) | ΔMargin (ns) | "
            f"{left} F_max (MHz) | {right} F_max (MHz) | ΔF_max (MHz)"), (man, per)


def test_a_column_heading_names_only_what_the_column_holds_on_every_row():
    """For each of the four EDE pairings, every Manager row and every Peripheral row of a
    column holds what the heading names for that row's side: a side off the EDE source holds
    the spec as it stands (left) and its Example (right), a side on it holds the PHY2 proposal
    (left) and EDE (right). Both headings name both sides' contents once only one side is on
    the EDE source."""
    from swi3s_studio.ui.timing_view import _ALL_ROWS

    def columns(tv):
        return {(side, row.name): (tv._min[(side, row.name)].value(),
                                   tv._max[(side, row.name)].value())
                for side in ("spec", "proposed") for row in _ALL_ROWS}

    plain, both = columns(_bus("SWI3S_PHY2", "SWI3S_PHY2")), columns(
        _bus("SWI3S_PHY2_EDE", "SWI3S_PHY2_EDE"))
    for side_name, prefix in (("Man", "Man_"), ("Per", "Per_")):
        names = [r.name for r in _ALL_ROWS if r.name.startswith(prefix)]
        assert any(plain[("spec", n)] != both[("spec", n)] for n in names), side_name
    expected = {(False, False): ("Specification", "Example"),
                (True, False): ("Spec / PHY2 Proposed", "Example / EDE Proposed"),
                (False, True): ("Spec / PHY2 Proposed", "Example / EDE Proposed"),
                (True, True): ("PHY2 Proposed", "EDE Proposed")}
    for man_ede, per_ede in expected:
        tv = _bus("SWI3S_PHY2_EDE" if man_ede else "SWI3S_PHY2",
                  "SWI3S_PHY2_EDE" if per_ede else "SWI3S_PHY2")
        assert tv._column_labels() == expected[(man_ede, per_ede)], (man_ede, per_ede)
        assert {h.text() for h in tv._left_heads} == {expected[(man_ede, per_ede)][0]}
        assert {h.text() for h in tv._right_heads} == {expected[(man_ede, per_ede)][1]}
        held = columns(tv)
        for prefix, ede in (("Man_", man_ede), ("Per_", per_ede)):
            ref = both if ede else plain
            for row in _ALL_ROWS:
                if row.name.startswith(prefix):
                    for side in ("spec", "proposed"):
                        assert held[(side, row.name)] == ref[(side, row.name)], (
                            man_ede, per_ede, side, row.name)


def test_an_inverted_range_gives_the_margins_of_the_exchanged_one_before_editing_finishes():
    """The margins follow a typed range as the span it covers, whichever box holds the larger
    end, with no wait for the focus to leave the box. V_IL is a threshold window, whose two
    ends are not interchangeable in the model."""
    inverted, ordered = _tv(), _tv()
    inverted._max[("proposed", "V_IL")].setValue(0.55)
    inverted._min[("proposed", "V_IL")].setValue(0.75)
    ordered._min[("proposed", "V_IL")].setValue(0.55)
    ordered._max[("proposed", "V_IL")].setValue(0.75)
    assert inverted._compute_error == "" and "exchanged" not in inverted._ceiling_label.text()
    assert inverted.summary_text() == ordered.summary_text()
    assert inverted.summary_text() != _tv().summary_text()


# ---- one equation row per column where the columns' terms differ -----------------------

_BUSES = (("SWI3S_PHY2", "SWI3S_PHY2"), ("SWI3S_PHY2_EDE", "SWI3S_PHY2_EDE"),
          ("SWI3S_PHY2", "SWI3S_PHY2_EDE"), ("SWI3S_PHY2_EDE", "SWI3S_PHY2"))


def _bus(man, per):
    tv = _tv()
    tv._man_spec_combo.setCurrentIndex(tv._man_spec_combo.findData(man))
    tv._per_spec_combo.setCurrentIndex(tv._per_spec_combo.findData(per))
    tv.flush_recompute()
    return tv


def _leg_rows(tv, leg):
    """[(equation row HTML, [(side, substitution row HTML), ...])] of `leg`: one entry when
    the columns share an equation row (both substitution rows under it), else one per column."""
    from PySide6.QtCore import Qt
    items = []
    for i in range(tv._tree.topLevelItemCount()):
        head = tv._tree.topLevelItem(i)
        items += [head.child(c) for c in range(head.childCount())
                  if head.child(c).data(0, Qt.UserRole) == leg]
    groups = []
    for it in items:
        if "≥ 0" in it.text(0):
            groups.append((it.text(0), []))
        else:
            groups[-1][1].append(it.text(0))
    sides = ("spec", "proposed")
    if len(groups) == 1:
        return [(groups[0][0], list(zip(sides, groups[0][1])))]
    assert len(groups) == 2 and all(len(g[1]) == 1 for g in groups), leg
    return [(eq, [(side, subs[0])]) for side, (eq, subs) in zip(sides, groups)]


def _anchors(html):
    """[(key, shown text)] of every clickable term in a row, in order."""
    import re
    return [(k, re.sub(r"<[^>]+>", "", body))
            for k, body in re.findall(r'<a href="k:([^"]*)"[^>]*>(.*?)</a>', html)]


def _own_keys(tv, side, leg):
    from swi3s_studio.ui.timing_view import _term_key
    return [_term_key(t.symbol) for t in tv._worst[side][leg].breakdowns[leg].terms]


_TWO_ROW_LEGS = {
    ("SWI3S_PHY2", "SWI3S_PHY2"): set(),
    ("SWI3S_PHY2_EDE", "SWI3S_PHY2_EDE"): {"MP_contention", "PM_contention", "PP_contention",
                                           "keeper_Man", "keeper_Per"},
    ("SWI3S_PHY2", "SWI3S_PHY2_EDE"): {"PM_contention", "PP_contention", "keeper_Per"},
    ("SWI3S_PHY2_EDE", "SWI3S_PHY2"): {"MP_contention", "keeper_Man"},
}


@pytest.mark.parametrize("man, per", _BUSES)
def test_a_leg_whose_columns_differ_shows_an_equation_row_per_column(man, per):
    """On an EDE bus the legs that release by an EDE Delay in one column and by t_DZ in the
    other show two equation rows, each headed by its column's label and holding only that
    column's terms in its own order, each followed by its substitution row; every other leg
    shows one shared row. No row holds both Man_t_DZ and Man_t_EDE."""
    from swi3s_studio.timing.calculator import INEQUALITIES
    tv = _bus(man, per)
    labels = tv._column_labels()
    two = _TWO_ROW_LEGS[(man, per)]
    for leg in INEQUALITIES:
        rows = _leg_rows(tv, leg)
        assert len(rows) == (2 if leg in two else 1), (man, per, leg)
        if leg not in two:
            continue
        for (eq, subs), side, label in zip(rows, ("spec", "proposed"), labels):
            assert f") {label}: " in eq, (leg, eq)
            assert [k for k, _ in _anchors(eq)] == _own_keys(tv, side, leg), (leg, side)
            assert [s for s, _ in subs] == [side]
            assert [k for k, _ in _anchors(subs[0][1])] == _own_keys(tv, side, leg)
            keys = {k for k, _ in _anchors(eq)}
            assert not ("Man_t_DZ" in keys and "Man_t_EDE" in keys), (man, per, leg)


def test_no_equation_row_holds_a_release_by_t_dz_and_by_an_ede_delay_of_one_device():
    """Across every inequality and every pairing of the spec selectors, a device's t_DZ and
    its EDE Delay (the two ways of deciding when to stop driving) are never in one row, and
    every term of every row is a clickable anchor."""
    from swi3s_studio.timing.calculator import INEQUALITIES
    tv = _tv()
    specs = [tv._man_spec_combo.itemData(i) for i in range(tv._man_spec_combo.count())]
    for man in specs:
        for per in specs:
            tv._man_spec_combo.setCurrentIndex(tv._man_spec_combo.findData(man))
            tv._per_spec_combo.setCurrentIndex(tv._per_spec_combo.findData(per))
            tv.flush_recompute()
            for leg in INEQUALITIES:
                for eq, subs in _leg_rows(tv, leg):
                    keys = {k for k, _ in _anchors(eq)}
                    for dev in ("Man", "Per"):
                        assert not {f"{dev}_t_DZ", f"{dev}_t_EDE"} <= keys, (man, per, leg)
                    assert eq.count("<a ") == len(_anchors(eq))
                    for side, _ in subs:
                        assert [k for k, _ in _anchors(eq)] == _own_keys(tv, side, leg), (
                            man, per, leg, side)
                    for _, sub in subs:
                        assert [k for k, _ in _anchors(sub)] == [k for k, _ in _anchors(eq)]


def test_a_plain_bus_shows_one_equation_row_per_inequality():
    from swi3s_studio.timing.calculator import INEQUALITIES
    tv = _bus("SWI3S_PHY2", "SWI3S_PHY2")
    for leg in INEQUALITIES:
        (eq, subs), = _leg_rows(tv, leg)
        assert [s for s, _ in subs] == ["spec", "proposed"], leg
        assert [k for k, _ in _anchors(eq)] == _own_keys(tv, "spec", leg) == _own_keys(
            tv, "proposed", leg), leg
        assert ": " not in eq.split(") ", 1)[1].split(" ", 1)[0], eq


def _substitution_text(html):
    import re
    plain = re.sub(r"<[^>]+>", "", html.split("<td>", 2)[-1])
    return plain.replace("−", "-").replace("·", "*")


def _substitution_sum(html):
    return eval(_substitution_text(html))


def _substitution_bound(html):
    """The most the row's printed sum can differ from the unrounded one: each printed number
    is good to half of its last digit, and moves the sum by its own partial derivative."""
    import re
    text = _substitution_text(html)
    numbers = list(re.finditer(r"\d+(?:\.(\d+))?", text))

    def value(shift):
        out, at = [], 0
        for i, m in enumerate(numbers):
            out += [text[at:m.start()], repr(float(m.group()) + shift.get(i, 0.0))]
            at = m.end()
        return eval("".join(out) + text[at:])

    bound = 0.0
    for i, m in enumerate(numbers):
        half = 0.5 * 10 ** -len(m.group(1) or "")
        bound += abs(value({i: half}) - value({i: -half})) / 2
    return bound


def test_each_rows_substitution_sums_to_its_columns_margin():
    """A row's printed numbers sum to its margin within the rounding of those numbers (each
    good to half of its last digit, weighted by how far it moves the sum)."""
    from swi3s_studio.timing.calculator import INEQUALITIES
    for man, per in _BUSES:
        tv = _bus(man, per)
        for leg in INEQUALITIES:
            for _, subs in _leg_rows(tv, leg):
                for side, sub in subs:
                    margin = tv._worst[side][leg].breakdowns[leg].margin_ns
                    assert _substitution_sum(sub) == pytest.approx(
                        margin, abs=_substitution_bound(sub) + 1e-9), (man, per, leg, side)


def test_the_summary_prints_an_equation_line_per_column_where_the_columns_differ():
    """After its margin table the summary lists each inequality's terms: one shared equation
    line where the columns hold the same terms, one line per column, headed by its label,
    where they differ; each column's numbers follow."""
    from swi3s_studio.timing.calculator import INEQUALITIES
    from swi3s_studio.ui.timing_view import _INEQ_NUMBER, _summary_label
    for man, per in _BUSES:
        tv = _bus(man, per)
        lines = tv.summary_text().splitlines()
        table = lines[:lines.index("")]
        assert len(table) == len(INEQUALITIES) + 1 and lines[len(table) + 1] == "Terms"
        left_lab, right_lab = (_summary_label(lab) for lab in tv._column_labels())
        for leg in INEQUALITIES:
            n = _INEQ_NUMBER[leg]
            rows = _leg_rows(tv, leg)
            mine = [ln for ln in lines[len(table) + 2:] if ln.startswith(f"{n}) ")]
            assert len(mine) == len(rows), (man, per, leg, mine)
            if len(rows) == 1:
                assert not mine[0].startswith(f"{n}) {left_lab}:")
            else:
                assert mine[0].startswith(f"{n}) {left_lab}: ")
                assert mine[1].startswith(f"{n}) {right_lab}: ")
                at = lines.index(mine[0])
                assert lines[at + 1].startswith(f"    {left_lab}: ")
                assert lines[at + 2] == mine[1] and lines[at + 3].startswith(
                    f"    {right_lab}: ")
                for line in mine:
                    for dev in ("Man", "Per"):
                        assert not (f"{dev}_t_DZ" in line and f"{dev}_t_EDE" in line), line
