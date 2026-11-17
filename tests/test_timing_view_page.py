"""The Timing page's term keys, restored settings, hints, diagram pane and substitution rows, and
the Manager-forces-keeper switch: keeper_Man's rows, summary and diagram say it does not apply,
the flag round-trips, a non-boolean one is reported, and Reset clears it.

Also here: the spec selectors; Save / Load of the Example column; the launch hint's limit and
its bound (setup, hold or t_RF, each leg against the toggle ceiling of the slowest edge its rows allow); the inequality rows'
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
from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication, QFileDialog, QWidget

from swi3s_studio import import_notes
from swi3s_studio.timing import CalcInputs, compute
from swi3s_studio.timing.spec_source import SPEC_SOURCES, LaunchMode

pytestmark = [pytest.mark.gui, pytest.mark.usefixtures("shared_compute")]


def _tv():
    """A Timing page with its first results in."""
    QApplication.instance() or QApplication([])
    from swi3s_studio.ui.timing_view import TimingView
    tv = TimingView()
    tv.flush_recompute()
    return tv


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


def test_the_ede_choice_reads_swi3s_phy2_ede_and_settings_load_it_by_its_key():
    """Both selectors offer the EDE source as "SWI3S PHY2 EDE", under its key; settings that
    name the key select it with no warning, and the title's tooltip names the bus."""
    tv = _tv()
    for combo in (tv._man_spec_combo, tv._per_spec_combo):
        i = combo.findData("SWI3S_PHY2_EDE")
        assert combo.itemText(i) == "SWI3S PHY2 EDE"
        assert not [combo.itemText(k) for k in range(combo.count())
                    if "proposed" in combo.itemText(k).lower()]
    assert _notes(tv, {"man_spec": "SWI3S_PHY2_EDE", "per_spec": "SWI3S_PHY2_EDE"}) == []
    assert tv._man_spec_combo.currentData() == tv._per_spec_combo.currentData() == \
        "SWI3S_PHY2_EDE"
    assert "SWI3S PHY2 EDE" in tv._title.toolTip(), tv._title.toolTip()
    assert tv._column_labels() == ("PHY2 Proposed", "PHY2 EDE")


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
def test_a_restored_value_that_is_not_a_number_takes_its_default_and_is_reported(bad):
    tv = _tv()
    before = tv._max[("proposed", "V_IH")].value()
    fclk = tv._fclk.value()
    d = tv.to_dict()
    tv._max[("proposed", "V_IH")].setValue(before - 0.1)
    tv._fclk.setValue(fclk - 1.0)
    d["proposed::V_IH::max"] = bad
    d["fclk_target"] = bad
    msgs = _notes(tv, d)
    assert tv._max[("proposed", "V_IH")].value() == before
    assert tv._fclk.value() == fclk
    assert len(msgs) == 1 and "not numbers, defaults used" in msgs[0], msgs
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
    assert all(r.text(0).endswith(" = —") and "MAX" not in r.text(0) for r in rows)
    summary = tv.summary_text().splitlines()
    assert "16) Bus Keeper — Manager Releasing | — | — | — | — | — | —" in summary
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


def test_a_saved_handover_flag_is_kept_and_a_missing_or_bad_one_follows_the_pair():
    """A saved handover_ui of true or false is kept, with no warning. A missing one takes the
    selected pair's allocation (`default_handover_UIs`: one UI on PHY2, none on PHY1 or EDE),
    with no warning. One that is text, a number or a list is reported and takes the pair's
    allocation too."""
    tv = _tv()
    for value in ("false", "no", 0, 1, [], [True]):
        tv._ho_ui_check.setChecked(False)
        notes = _notes(tv, {"handover_ui": value})
        assert any(f"'handover_ui' {value!r} is not true or false; the selected pair's "
                   "allocation (true) is used" in n for n in notes), (value, notes)
        assert tv._ho_ui_check.isChecked(), value
    for value in (True, False):
        assert not any("handover_ui" in n for n in _notes(tv, {"handover_ui": value}))
        assert tv._ho_ui_check.isChecked() is value
    for spec, implied in (("SWI3S_PHY2", True), ("SWI3S_PHY1", False),
                          ("SWI3S_PHY2_EDE", False)):
        tv._ho_ui_check.setChecked(not implied)
        assert not any("handover_ui" in n
                       for n in _notes(tv, {"man_spec": spec, "per_spec": spec}))
        assert tv._ho_ui_check.isChecked() is implied, spec


def test_a_saved_number_given_as_text_is_reported_as_not_a_number():
    """A range end or the rate saved as a string takes the pair's default and is reported."""
    tv = _tv()
    default = (tv._fclk.value(), tv._min[("proposed", "Man_t_DD")].value())
    tv._fclk.setValue(default[0] - 1.0)
    tv._min[("proposed", "Man_t_DD")].setValue(default[1] + 0.5)
    notes = _notes(tv, {"fclk_target": "12.5", "proposed::Man_t_DD::min": "3"})
    assert any("not numbers" in n and "'12.5'" in n and "'3'" in n for n in notes), notes
    assert (tv._fclk.value(), tv._min[("proposed", "Man_t_DD")].value()) == default


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
    calls = []
    real = tv._job.submit
    monkeypatch.setattr(tv._job, "submit", lambda fn: (calls.append(fn), real(fn))[1])
    tv._man_spec_combo.setCurrentIndex(tv._man_spec_combo.findData("SW13_1V2"))
    tv.flush_recompute()
    assert tv._launch_combo.currentData() == LaunchMode.ANALOG.value
    assert len(calls) == 1, calls


def test_the_launch_grid_is_placed_at_the_models_duty():
    from swi3s_studio.ui import timing_view
    assert timing_view._LAUNCH_DUTY == CalcInputs().duty_min


def test_save_and_load_example_start_in_the_last_timing_folder_and_remember_theirs(
        tmp_path, monkeypatch):
    tv = _tv()
    (tmp_path / "first").mkdir()
    (tmp_path / "second").mkdir()
    QSettings().setValue("capture/last_dir_timing", str(tmp_path / "first"))
    seen = {}

    def save(parent, title, start, flt):
        seen["save"] = start
        return str(tmp_path / "second" / "ex.json"), ""

    def load(parent, title, start, flt):
        seen["load"] = start
        return str(tmp_path / "second" / "ex.json"), ""

    monkeypatch.setattr(QFileDialog, "getSaveFileName", save)
    monkeypatch.setattr(QFileDialog, "getOpenFileName", load)
    tv._save_proposed()
    assert seen["save"] == os.path.join(str(tmp_path / "first"), "example.json")
    assert QSettings().value("capture/last_dir_timing") == str(tmp_path / "second")
    QSettings().setValue("capture/last_dir_timing", str(tmp_path / "first"))
    tv._load_proposed()
    assert seen["load"] == str(tmp_path / "first")
    assert QSettings().value("capture/last_dir_timing") == str(tmp_path / "second")


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



def test_the_placement_picker_reads_peripheral_placement_and_worst_per_inequality():
    """The P->P placement picker is labelled Peripheral Placement, and its default choice, the
    persisted token `unknown`, reads Worst Per Inequality."""
    from PySide6.QtWidgets import QLabel
    tv = _tv()
    assert any(lab.text() == "Peripheral Placement" for lab in tv._topbar.findChildren(QLabel))
    assert tv._pp_combo.currentData() == "unknown"
    assert tv._pp_combo.currentText() == "Worst Per Inequality"
    assert tv.to_dict()["pp_placement"] == "unknown"
    assert "Peripheral A" in tv._pp_combo.toolTip()


# ---- hints -----------------------------------------------------------------------------


def test_the_top_bar_packs_its_blocks_into_the_width():
    """`_pack_top_bar` puts each block on its own row beside the pickers where it fits, else on
    the other, else on rows below that never run past the width; at the page's normal width
    every block sits beside the pickers."""
    from swi3s_studio.ui.timing_view import _pack_top_bar
    widths, rows = [600, 400, 300, 500], [0, 1, 0, 1]
    assert _pack_top_bar(1700, 560, widths, rows, 20) == ([[0, 2], [1, 3]], [])
    side, below = _pack_top_bar(1100, 560, widths, rows, 20)
    assert side == [[2], [1]] and below == [[0], [3]]
    side, below = _pack_top_bar(1300, 560, widths, rows, 20, indent=4)
    assert side == [[0], [1, 2]] and below == [[3]]
    side, below = _pack_top_bar(1000, 560, widths, rows, 20, indent=4)
    assert side == [[2], [1]] and below == [[0], [3]]
    side, below = _pack_top_bar(1200, 560, widths, rows, 20, indent=4)
    assert side == [[0], [1]] and below == [[2, 3]]
    for width in range(600, 2400, 50):
        side, below = _pack_top_bar(width, 560, widths, rows, 20, indent=4)
        assert sorted(i for r in side + below for i in r) == [0, 1, 2, 3], width
        for r in side:
            assert sum(widths[i] for i in r) + 20 * len(r) <= width - 560, width
        for r in below:
            assert len(r) == 1 or sum(widths[i] for i in r) + 20 * (len(r) - 1) <= width - 4


@pytest.mark.parametrize("size", [(1700, 1050), (1100, 760)])
def test_the_top_bar_uses_the_full_width_in_few_rows_and_clips_nothing(size):
    """The top bar is as wide as the page and shows no subtitle and no launch advisory line:
    the title's tooltip names the bus and the launch selector's the advisory. Each side's
    spec selector reads Manager or Peripheral with its ramp model beside it, the Manager's
    over the Peripheral's, the Manager data launch shares a row with the keeper switch and the
    Peripheral Placement one with the Handover UI box, in line where they stack. No
    control runs past the bar or overlaps another; at 1700 px every control sits on the two
    rows of the selectors, and at 1100 px they take at most four."""
    from PySide6.QtCore import QPoint, QRect
    from PySide6.QtWidgets import QAbstractButton, QComboBox, QDoubleSpinBox, QLabel
    tv = _tv()
    tv.resize(*size)
    tv.show()
    for _ in range(3):
        QApplication.processEvents()
    bar = tv._topbar
    assert tv.width() == size[0]
    assert bar.width() == tv.width() - 12
    assert tv._title.toolTip() == "SWI3S PHY2 Manager and Peripheral"
    assert "spread absorbed" in tv._launch_combo.toolTip()
    texts = [w.text() for w in tv.findChildren(QLabel) if w.isVisible()]
    assert "SWI3S PHY2 Manager and Peripheral" not in texts
    assert not [t for t in texts if "spread absorbed" in t]
    assert "Manager spec" not in texts and "Peripheral spec" not in texts

    def rect(w):
        return QRect(w.mapTo(bar, QPoint(0, 0)), w.size())

    controls = [w for w in bar.findChildren(QWidget)
                if isinstance(w, (QLabel, QComboBox, QAbstractButton, QDoubleSpinBox))
                and w.isVisible() and w is not tv._ceiling_label]
    for w in controls:
        assert bar.rect().contains(rect(w)), (w, rect(w), bar.rect())
    for i, a in enumerate(controls):
        for b in controls[i + 1:]:
            if a.isAncestorOf(b) or b.isAncestorOf(a):
                continue
            assert not rect(a).intersects(rect(b)), (a, b)
    man, per = rect(tv._man_spec_combo), rect(tv._per_spec_combo)
    assert man.left() == per.left() and man.bottom() < per.top()
    for spec, shape in ((man, rect(tv._man_shape)), (per, rect(tv._per_shape))):
        assert shape.left() > spec.right() and abs(shape.center().y() - spec.center().y()) <= 2
    launch, keeper = rect(tv._launch_combo), rect(tv._keeper_forced_check)
    assert keeper.left() > launch.right() and abs(keeper.center().y() - launch.center().y()) <= 2
    # The placement row mirrors the launch row: its selector, then the Handover UI box, each
    # in line with its counterpart where the two rows stack.
    pp, ho = rect(tv._pp_combo), rect(tv._ho_ui_check)
    assert ho.left() > pp.right() and abs(ho.center().y() - pp.center().y()) <= 2
    if pp.top() > launch.bottom() and abs(rect(tv._pp_label).left()
                                          - rect(tv._launch_label).left()) <= 1:
        assert (pp.left(), pp.width()) == (launch.left(), launch.width())
        assert ho.left() == keeper.left()
    centres = sorted({rect(w).center().y() for w in controls})
    rows = [y for i, y in enumerate(centres) if i == 0 or y - centres[i - 1] > 3]
    assert len(rows) <= (2 if size[0] == 1700 else 4), centres
    if size[0] == 1700:
        assert all(min(abs(y - man.center().y()), abs(y - per.center().y())) <= 1
                   for y in centres), centres


def test_the_trace_hint_says_a_click_draws_the_term():
    """The hint is one line; its tooltip says how a click clears a term and closes an
    inequality."""
    tv = _tv()
    assert tv._trace_hint.text() == "Click an inequality, or term, to highlight and draw it."
    tip = tv._trace_hint.toolTip()
    assert "click it again in the inequality drawn" in tip and "blank space" in tip
    assert "click it again to close it" in tip


def test_the_handover_box_shows_no_status_text_and_its_bar_keeps_its_height():
    """No label on the page states the handover allocation, with the Handover UI box ticked or
    cleared, on a PHY1, PHY2 or EDE pairing; toggling the box leaves the top bar's height as it
    was. The box's tooltip says what it allocates."""
    from PySide6.QtWidgets import QLabel
    tv = _tv()
    tv.resize(1400, 900)
    tv.show()
    assert "1·UI" in tv._ho_ui_check.toolTip()
    for spec in ("SWI3S_PHY1", "SWI3S_PHY2", "SWI3S_PHY2_EDE"):
        for combo in (tv._man_spec_combo, tv._per_spec_combo):
            combo.setCurrentIndex(combo.findData(spec))
        heights = set()
        for checked in (True, False):
            tv._ho_ui_check.setChecked(checked)
            QApplication.processEvents()
            heights.add(tv._topbar.height())
            texts = [w.text() for w in tv.findChildren(QLabel)]
            assert not [t for t in texts if "N_HO" in t or "UI scheduled" in t
                        or "UI allocated" in t or "handover must close" in t], (spec, checked)
        assert len(heights) == 1, (spec, heights)
    tv.close()


def _launch(tv, mode):
    tv._launch_combo.setCurrentIndex(tv._launch_combo.findData(mode.value))


@pytest.mark.parametrize("mode", [LaunchMode.CLK4, LaunchMode.CLK2])
def test_the_launch_hint_limit_is_the_pages_own_mp_limit(mode):
    tv = _tv()
    tv._fclk.setValue(12.288)
    _launch(tv, mode)
    tv.flush_recompute()
    legs = [tv._worst["proposed"][leg].breakdowns[leg]
            for leg in ("MP_setup", "MP_setup_ho", "MP_hold", "MP_hold_ho")]
    tight = min(legs, key=lambda b: b.F_max_MHz)
    text = tv._launch_combo.toolTip()
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
    assert "viable to ~37.5 MHz (bound by t_RF)" in tv._launch_combo.toolTip()


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
    before = tv._launch_combo.toolTip()
    box = tv._max[("proposed", "Per_t_IS")]
    box.setValue(box.value() + 3.0)
    tv.flush_recompute()
    assert tv._launch_combo.toolTip() != before


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


def _default_view(tv):
    """Whether the diagram shows its default view: inequality 1) drawn whole from the current
    results, its heading washed."""
    from swi3s_studio.ui.timing_view import _whole_class
    head = tv._tree.topLevelItem(0)
    return (not tv._diagram_pane.isHidden() and tv._diagram_whole
            and tv._diagram_leg == "MP_setup" and tv._diagram_term == ""
            and tv._diagram.whole and len(tv._diagram.panels) == 2
            and tv._diagram.leg_title.startswith("1) Manager Setup for Peripheral")
            and tv._tree._delegate.whole() == "MP_setup"
            and f'class="{_whole_class("MP_setup")}"' in head.text(0))


def test_the_page_opens_on_inequality_1_drawn_whole_and_blank_until_its_result():
    """A new page draws 1) whole, its heading washed: while the first recompute runs the
    pane shows the title and "Computing…", then the inequality."""
    QApplication.instance() or QApplication([])
    from swi3s_studio.ui.timing_view import TimingView
    tv = TimingView()
    d = tv._diagram
    assert not tv._diagram_pane.isHidden() and tv._diagram_whole
    assert tv._diagram_leg == "MP_setup" and d.whole and d.leg_title.startswith("1) ")
    if tv._job.busy:
        assert d.pending and d.panels == [] and not d.can_export()
    tv.flush_recompute()
    assert _default_view(tv) and not d.pending and d.can_export()


def test_reset_and_load_example_return_the_diagram_to_inequality_1(tmp_path, monkeypatch):
    """With a term drawn, or another inequality drawn whole, Reset and Load Example Settings draw 1)
    whole again; a spec change keeps what is drawn."""
    tv = _tv()
    tv._on_term_clicked("Per_t_IS", "MP_hold")
    assert tv._diagram_term == "Per_t_IS" and not tv._diagram_whole
    tv._on_reset()
    tv.flush_recompute()
    assert _default_view(tv)
    tv._on_inequality_clicked("PM_hold")
    assert tv._diagram_leg == "PM_hold" and tv._tree._delegate.whole() == "PM_hold"
    path = tmp_path / "example.json"
    path.write_text(json.dumps(tv._serialize_side("proposed")))
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *a, **k: (str(path), ""))
    tv._load_proposed()
    tv.flush_recompute()
    assert _default_view(tv)
    tv._on_inequality_clicked("PM_hold")
    tv._man_spec_combo.setCurrentIndex(tv._man_spec_combo.findData("SWI3S_PHY1"))
    tv.flush_recompute()
    assert tv._diagram_leg == "PM_hold" and tv._diagram_whole


def test_a_click_after_the_default_view_behaves_as_on_any_page():
    """A term click draws the term and drops the heading's wash; a heading click draws that
    inequality and washes its heading; a second click on it closes the pane."""
    from swi3s_studio.ui.timing_view import _whole_class
    tv = _tv()
    assert _default_view(tv)
    tv._on_term_clicked("Per_t_IS", "MP_setup")
    assert tv._diagram_term == "Per_t_IS" and not tv._diagram_whole
    assert tv._tree._delegate.whole() == ""
    tv._on_inequality_clicked("MP_setup_ho")
    assert tv._diagram_leg == "MP_setup_ho" and tv._diagram_whole
    assert tv._tree._delegate.whole() == "MP_setup"      # the heading 2) is under
    tv._on_inequality_clicked("PP_hold")
    assert tv._tree._delegate.whole() == "PP_hold"
    css = tv._tree._delegate._doc(_option(tv._tree, tv._tree.topLevelItem(0))).defaultStyleSheet()
    assert f".{_whole_class('PP_hold')} " in css and _whole_class("MP_setup") not in css
    tv._on_inequality_clicked("PP_hold")
    assert tv._diagram_pane.isHidden() and tv._tree._delegate.whole() == ""


def _option(tree, item):
    from PySide6.QtWidgets import QStyleOptionViewItem
    index = tree.indexFromItem(item, 0)
    opt = QStyleOptionViewItem()
    tree.initViewItemOption(opt)
    opt.rect = tree.visualRect(index)
    tree._delegate.initStyleOption(opt, index)
    return opt


def test_the_tree_is_one_column_under_a_blank_header():
    """The rows' text is the tree's only column: no margin or Max Frequency column, and the
    header over it is blank."""
    tv = _tv()
    assert tv._tree.columnCount() == 1 and tv._tree.headerItem().text(0) == ""


@pytest.mark.parametrize("mode", ["light", "dark"])
def test_a_substitution_row_ends_in_its_margin_coloured_by_its_sign(mode):
    """Each substitution row ends in " = " and its margin: green (the diagram's margin
    colour) where positive, the page's red where negative, the text colour at zero and for
    "—"; the summary's margins are the rows'. Both palettes' colours read at 4.5:1 or better
    on the page background, the dark red at 3:1."""
    import re

    from swi3s_studio.ui.theme import VizTheme, apply_palette
    from swi3s_studio.ui.timing_view import _SUBST_LABEL_ROLE
    before = VizTheme.MODE
    apply_palette(mode)
    try:
        tv = _tv()
        parent = tv._tree.topLevelItem(0)
        b = tv._worst["spec"]["MP_setup"].breakdowns["MP_setup"]
        for margin, text, colour in ((1.5, "+1.50 ns", VizTheme.DIAG_OK),
                                     (-0.25, "-0.25 ns", VizTheme.SEM_ERROR),
                                     (0.0, "+0.00 ns", VizTheme.TEXT),
                                     (math.nan, "—", VizTheme.TEXT)):
            item = tv._add_side_term(parent, "x", replace(b, margin_ns=margin),
                                     tv._fclk.value())
            assert (f' = <span style="color:{colour}">{text}</span> (F<sub>MAX</sub> '
                    in item.text(0)), (
                item.text(0))
        shown = {}
        for i in range(tv._tree.topLevelItemCount()):
            head = tv._tree.topLevelItem(i)
            current = None
            for c in range(head.childCount()):
                kid = head.child(c)
                plain = re.sub(r"<[^>]+>", "", kid.text(0))
                if "≥ 0" in plain:
                    current = int(plain.split(")")[0])
                elif kid.data(0, _SUBST_LABEL_ROLE):
                    shown[current, kid.data(0, _SUBST_LABEL_ROLE)] = (
                        plain.rsplit(" = ", 1)[1].split(" (FMAX ")[0])
        summary = tv.summary_text().splitlines()
        left, right = tv._column_labels()
        for n in (1, 3, 5, 9):
            cells = next(ln for ln in summary if ln.startswith(f"{n}) ")).split(" | ")
            assert (shown[n, left], shown[n, right]) == (f"{cells[1]} ns", f"{cells[2]} ns")
    finally:
        apply_palette(before)

    def lum(h):
        c = [int(h[i:i + 2], 16) / 255 for i in (1, 3, 5)]
        c = [x / 12.92 if x <= 0.04045 else ((x + 0.055) / 1.055) ** 2.4 for x in c]
        return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]

    from swi3s_studio.ui import theme
    pal = theme._LIGHT if mode == "light" else theme._DARK
    for key, floor in (("DIAG_OK", 4.5), ("SEM_ERROR", 4.5 if mode == "light" else 3.0)):
        hi, lo = sorted((lum(pal[key]), lum(pal["FRAME_BG"])), reverse=True)
        assert (hi + 0.05) / (lo + 0.05) >= floor, (mode, key)


def test_the_page_prints_crossings_without_late_or_early_and_the_keeper_as_man_t_keeper():
    """The tree's equation rows, the summary, the term diagram's headings and an exported
    diagram print each crossing without ,late / ,early and the keeper's response as
    Man_t_Keeper; the terms keep their full symbols, and the keys the page traces by."""
    import re

    from swi3s_studio.ui.timing_view import _term_key
    tv = _tv()
    tv.resize(1800, 1100)
    tv.show()
    QApplication.processEvents()
    tree = tv._tree
    rows = [tree.topLevelItem(i).child(j).text(0) for i in range(tree.topLevelItemCount())
            for j in range(tree.topLevelItem(i).childCount())]
    shown = "\n".join(rows)
    assert "X<sub>MP,fall</sub>" in shown and "X<sub>K,fall</sub>" in shown
    assert "Man_t<sub>Keeper</sub>" in shown
    summary = tv.summary_text()
    assert "X_MP,fall · Man_t_RF,CLK" in summary and "Man_t_Keeper" in summary
    for text in (re.sub(r"<[^>]+>", "", shown), summary):
        assert not re.search(r",(late|early)\b", text) and "Keeper_Response" not in text
    keeper = next(t for t in tv._per_inequality_worst("spec")["keeper_Man"].terms
                  if t.symbol == "Man_t_Keeper_Response")
    assert _term_key(keeper.symbol) == "Man_t_Keeper_Response"
    d = tv._diagram
    for symbol, leg, want in (("X_MP,fall,early · Man_t_RF,CLK", "MP_setup", "X<sub>MP,fall</sub>"),
                              ("Man_t_Keeper_Response", "keeper_Man", "Man_t<sub>Keeper</sub>")):
        tv._on_term_clicked(_term_key(symbol), leg)
        QApplication.processEvents()
        heads = [d._heading(pn) for pn in d.panels]
        assert heads and all(want in h for h in heads), heads
        svg = d.export("svg").decode()
        assert not re.search(r",(late|early)\b", svg) and "Keeper_Response" not in svg
    tv.close()


def test_the_page_prints_anchors_and_ramps_without_fast_and_crossings_with_fast_or_slow():
    """On SoundWire buses the tree, the summary, the term diagram's headings and an exported
    diagram print an anchor correction priced on the fast data edge, and a ramp to V_OH /
    V_OL priced there, without ,fast; a crossing keeps its ,fast or ,slow. The terms keep
    their full symbols."""
    import re

    from swi3s_studio.ui.timing_view import _term_key

    def shown(tv):
        tree = tv._tree
        return "\n".join(tree.topLevelItem(i).child(j).text(0)
                         for i in range(tree.topLevelItemCount())
                         for j in range(tree.topLevelItem(i).childCount()))

    tv = _bus("SW13_1V2", "SW13_1V2")
    symbols = {t.symbol for b in tv._per_inequality_worst("spec").values() for t in b.terms}
    assert {"Per_t_DD,anchor,fall,fast", "Man_t_DD,anchor,rr,fast"} <= symbols
    html, summary = shown(tv), tv.summary_text()
    assert "Per_t<sub>DD,anchor,fall</sub>" in html and "Man_t<sub>DD,anchor,rr</sub>" in html
    assert "Per_t_DD,anchor,fall " in summary and "Man_t_DD,anchor,rr " in summary
    assert "X<sub>PM,rf,fast</sub>" in html and "X<sub>MP,A,rise,slow</sub>" in html
    assert "X_PM,rf,fast · Per_t_RF,DATA" in summary and "X_MP,A,rise,slow" in summary
    for text in (re.sub(r"<[^>]+>", "", html), summary):
        assert not re.search(r"anchor,[a-z]+,fast", text), text
    d = tv._diagram
    tv._on_term_clicked(_term_key("Per_t_DD,anchor,fall,fast"), "PM_setup")
    QApplication.processEvents()
    heads = [d._heading(pn) for pn in d.panels]
    assert heads and all("Per_t<sub>DD,anchor,fall</sub> =" in h for h in heads), heads
    svg = d.export("svg").decode()
    assert "anchor,fall" in svg and ",fast" not in svg
    tv.close()
    tv = _bus("SWI3S_PHY2_EDE", "SWI3S_PHY2")
    ramp = "Man_t_RF,DATA,rise,fast"
    assert ramp in {t.symbol for t in tv._per_inequality_worst("proposed")["keeper_Man"].terms}
    html, summary = shown(tv), tv.summary_text()
    assert "Man_t<sub>RF,DATA,rise</sub>" in html and "Man_t_RF,DATA,rise" in summary
    assert "RF,DATA,rise,fast" not in re.sub(r"<[^>]+>", "", html) + summary
    tv._on_term_clicked(_term_key(ramp), "keeper_Man")
    QApplication.processEvents()
    heads = [d._heading(pn) for pn in tv._diagram.panels]
    assert any("Man_t<sub>RF,DATA,rise</sub>" in h for h in heads), heads
    assert not any("DATA,rise,fast" in h for h in heads), heads
    tv.close()


def _row_of(tv, leg):
    """`leg`'s first equation row in the tree."""
    from swi3s_studio.ui.timing_view import _WHOLE_ROLE
    tree = tv._tree
    return next(item for i in range(tree.topLevelItemCount())
                for head in [tree.topLevelItem(i)]
                for item in (head.child(j) for j in range(head.childCount()))
                if item.data(0, _WHOLE_ROLE) == leg)


def test_the_summary_names_each_inequality_by_its_number_and_name():
    """Each row of the summary's margin table starts with the number the inequality's
    equation row starts with and its name: the text of the heading it is under and, for the
    setup and hold inequalities that share a heading, the launch (t_DD, or t_ZD for the
    handover one); no internal name appears."""
    import re

    from swi3s_studio.timing.calculator import INEQUALITIES
    from swi3s_studio.ui.timing_view import _heading_of
    tv = _tv()

    def plain(item):
        return re.sub(r"<[^>]+>", "", item.text(0))

    lines = tv.summary_text().splitlines()
    names = [ln.split(" | ")[0] for ln in lines[1:lines.index("")]]
    tree = []
    for leg in INEQUALITIES:
        row = _row_of(tv, leg)
        shared = sum(_heading_of(m) == _heading_of(leg) for m in INEQUALITIES) > 1
        launch = (", t_ZD" if leg.endswith("_ho") else ", t_DD") if shared else ""
        tree.append(f"{plain(row).split(')')[0]}) {plain(row.parent())}{launch}")
    assert names == tree, names
    assert names[:2] == ["1) Manager Setup for Peripheral, t_DD",
                         "2) Manager Setup for Peripheral, t_ZD"]
    assert names[-1] == "17) Bus Keeper — Peripheral Releasing"
    assert not any(leg.replace("_", " ") in ln or leg in ln for ln in names
                   for leg in INEQUALITIES)
    tv.close()


def test_the_typical_row_width_is_the_90th_percentile_row_widened_by_15_percent():
    """The pane fits the 90th-percentile row (nearest rank) widened by 15 %, and never
    wider than the widest row."""
    from swi3s_studio.ui.timing_view import _typical_row_width
    assert _typical_row_width([100] * 9 + [200]) == 115
    assert _typical_row_width([100] * 10) == 100
    assert _typical_row_width([100] * 8 + [105, 300]) == 121       # 120.75, rounded
    assert _typical_row_width([300, 100] + [100] * 8) == 115      # order does not matter
    assert _typical_row_width([100] * 9 + [112]) == 112           # the widest row
    assert _typical_row_width([90]) == 90


def test_the_pp_setup_rows_fit_the_pane_at_the_default_page():
    """At the default 1700 x 1050 page with the diagram open, every row of the inequalities
    pane fits it, the four P->P setup rows, the widest, among them: nothing scrolls sideways."""
    from swi3s_studio.ui.timing_view import _typical_row_width
    tv = _tv()
    tv.resize(1700, 1050)
    tv.show()
    QApplication.processEvents()
    tv._on_inequality_clicked("PP_setup")
    QApplication.processEvents()
    widths = tv._row_widths()
    assert _typical_row_width(widths) == max(widths), sorted(widths)[-6:]
    assert tv._results_width == tv._tree.width()
    assert not tv._ineq_scroll.horizontalScrollBar().isVisible()
    tv.close()


def test_the_splitter_gives_the_inequalities_their_typical_row_and_the_diagram_the_rest():
    """With the diagram open the inequalities pane is as wide as the tree's typical row
    (`_typical_row_width`, a substitution row with its margin and F_max), and the diagram takes
    the rest; on a narrow page the diagram keeps its minimum. A drag keeps its split through a
    resize."""
    from swi3s_studio.ui.timing_view import _typical_row_width
    tv = _tv()
    tv.resize(1800, 1100)
    tv.show()
    QApplication.processEvents()
    tree, sc = tv._tree, tv._ineq_scroll
    total = tv._splitter.width() - tv._splitter.handleWidth()
    left, right = tv._splitter.sizes()
    assert left == tv._ineq_width() and right == total - left
    widths = tv._row_widths()
    cut = max(widths) - _typical_row_width(widths)
    assert tv._results_width == tree.width() - cut
    assert sc.viewport().width() >= tv._results_width
    assert sc.horizontalScrollBar().isVisible() is (cut > 0)   # a cut row scrolls sideways
    assert all(card.width() == max(tv._results_width, card.minimumSizeHint().width())
               for card in tv._section_cards)
    tv.resize(900, 1100)
    QApplication.processEvents()
    assert tv._splitter.sizes()[1] == tv._diagram_pane.minimumWidth()
    tv.resize(1800, 1100)
    QApplication.processEvents()
    assert tv._splitter.sizes()[0] == tv._ineq_width()
    tv._splitter.setSizes([500, total - 500])
    tv._splitter.splitterMoved.emit(500, 1)
    tv.resize(1700, 1100)
    QApplication.processEvents()
    assert tv._splitter.sizes()[0] == 500
    tv._on_inequality_clicked("MP_setup")               # closes, and forgets the drag
    tv._on_inequality_clicked("MP_setup")
    QApplication.processEvents()
    assert tv._splitter.sizes()[0] == tv._ineq_width()
    tv.close()


def test_a_row_much_wider_than_the_rest_scrolls_rather_than_widening_the_pane():
    """A row more than 15 % wider than the 90th-percentile row does not widen the pane:
    the pane fits the typical row, that row runs past the pane's edge, and the cards end where
    the pane does."""
    from PySide6.QtWidgets import QTreeWidgetItem

    from swi3s_studio.ui.timing_view import _typical_row_width
    tv = _tv()
    tv.resize(1800, 1100)
    tv.show()
    QApplication.processEvents()
    tree, sc = tv._tree, tv._ineq_scroll
    fitted = tv._results_width
    QTreeWidgetItem(tree.topLevelItem(0), ["1) " + "UI − Man_t_DD " * 40 + "≥ 0"])
    tv._fit_tree_width()
    tv._unify_section_widths()
    tv._fit_split()
    QApplication.processEvents()
    widths = tv._row_widths()
    typical = _typical_row_width(widths)
    assert typical < 0.8 * max(widths)                    # the rule cuts here
    assert tv._results_width == tree.width() - (max(widths) - typical)
    assert fitted <= tv._results_width < tree.columnWidth(0)
    assert tv._splitter.sizes()[0] == tv._ineq_width()
    assert sc.viewport().width() < tree.columnWidth(0)
    assert all(card.width() == max(tv._results_width, card.minimumSizeHint().width())
               for card in tv._section_cards)
    tv.close()


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


def test_an_f_max_follows_the_margin_red_below_the_rate_and_absent_where_none_bounds():
    """A substitution row ends in its margin, then the F_max to the thousandth of a MHz in
    parentheses, "(F<sub>MAX</sub> 15.128 MHz)", F with MAX as a subscript and no underscore:
    the F_max in red where the leg fails below the clock
    rate, in the row's text colour at or above it, "FAIL" where it fails at the lowest rates;
    a leg that never fails shows nothing after its margin, setup or hold. The note naming
    higher passing rates is the row's tooltip. An inequality with no UI term shows no F_max
    and no tooltip, whatever its F_max, unless its column's Manager launch is clocked and it
    holds a Manager t_DD, t_ZD or t_DZ."""
    import re

    from swi3s_studio.ui.theme import VizTheme
    from swi3s_studio.ui.timing_view import _FMAX_ROLE
    tv = _tv()
    parent = tv._tree.topLevelItem(0)
    fclk = tv._fclk.value()
    red = f'<span style="color:{VizTheme.SEM_ERROR}">'

    def shown(b):
        item = tv._add_side_term(parent, "x", b, fclk)
        return item.text(0), item.data(0, _FMAX_ROLE)

    setup = tv._worst["spec"]["MP_setup"].breakdowns["MP_setup"]
    hold = tv._worst["spec"]["MP_hold_ho"].breakdowns["MP_hold_ho"]
    assert setup.is_setup and not hold.is_setup and math.isfinite(setup.F_max_MHz)
    for b in (setup, hold):
        margin = re.escape(f"{b.margin_ns:+.2f} ns</span>")
        html, fmax = shown(replace(b, F_max_MHz=math.inf))
        assert fmax == "" and "MAX" not in html and re.search(margin + "</td>", html), html
        low = 0.5 * fclk
        html, fmax = shown(replace(b, F_max_MHz=low))
        assert fmax == f"{low:.3f} MHz", fmax
        assert re.search(margin + f" \\(F<sub>MAX</sub> {re.escape(red)}{low:.3f} MHz</span>\\)</td>",
                         html), html
        html, fmax = shown(replace(b, F_max_MHz=2.0 * fclk))
        assert html.endswith(f"</span> (F<sub>MAX</sub> {2.0 * fclk:.3f} MHz)</td></tr></table>"), \
            html
        html, fmax = shown(replace(b, F_max_MHz=0.0))
        assert fmax == "FAIL" and f" (F<sub>MAX</sub> {red}FAIL</span>)</td>" in html, html
        assert "F_max" not in html, html
    item = tv._add_side_term(parent, "x", replace(setup, F_max_note="passes again at 9 MHz"),
                             fclk)
    assert item.toolTip(0) == ("F<sub>MAX</sub> is the lowest rate at which the leg fails; it passes "
                               "again at 9 MHz."), item.toolTip(0)
    plain = tv._worst["spec"]["MP_hold"].breakdowns["MP_hold"]
    for f_max in (0.0, 0.5 * fclk, 2.0 * fclk, math.inf):
        item = tv._add_side_term(parent, "x", replace(plain, F_max_MHz=f_max,
                                                      F_max_note="passes again at 9 MHz"),
                                 fclk)
        assert "MAX" not in item.text(0) and not item.data(0, _FMAX_ROLE), item.text(0)
        assert not item.toolTip(0), item.toolTip(0)
    # Under a clocked Manager launch its Man_t_DD sits at a fraction of the UI, so the same
    # row shows its F_max and the note.
    assert any(t.symbol == "Man_t_DD" and t.value_ns for t in plain.terms)
    item = tv._add_side_term(parent, "x", replace(plain, F_max_MHz=0.5 * fclk,
                                                  F_max_note="passes again at 9 MHz"),
                             fclk, clocked=True)
    assert item.data(0, _FMAX_ROLE) == f"{0.5 * fclk:.3f} MHz", item.text(0)
    assert item.toolTip(0).endswith("passes again at 9 MHz."), item.toolTip(0)


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


@pytest.mark.parametrize("hi", [100.0, 10000.0])
def test_no_margins_quotes_the_tolerance_as_typed(hi):
    """τ typed 0 % to `hi`: the notice names τ at `hi`, the value in the box, not the range's
    midpoint and not a division by zero."""
    tv = _tv()
    tv._min[("proposed", _TAU)].setValue(0.0)
    tv._max[("proposed", _TAU)].setValue(hi)
    tv.flush_recompute()
    text = tv._ceiling_label.text()
    assert text.startswith("⚠ No margins.") and f"τ is {hi:g} %" in text, text
    assert "divide by zero" not in text


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
    tv._on_inequality_clicked("MP_setup")          # closes the default view of 1)
    assert tv._diagram_pane.isHidden()
    tv.set_diagram_maximized(True)
    assert not tv.is_diagram_maximized()
    assert tv._ineq_scroll.isVisibleTo(tv) and tv._topbar.isVisibleTo(tv)
    tv.close()


def _text_point(tree, item, text):
    """A point in the middle of `text` in row `item`'s laid-out text: on the text, on no
    term."""
    from PySide6.QtCore import QPoint, QPointF, Qt
    from PySide6.QtWidgets import QStyleOptionViewItem
    index = tree.indexFromItem(item, 0)
    opt = QStyleOptionViewItem()
    tree.initViewItemOption(opt)
    opt.rect = tree.visualRect(index)
    tree._delegate.initStyleOption(opt, index)
    doc = tree._delegate._doc(opt)
    found = doc.find(text)
    assert not found.isNull(), (text, item.text(0))
    lo, hi = found.selectionStart(), found.selectionEnd()
    origin = tree._delegate._origin(opt, doc, tree)
    y = opt.rect.center().y()
    xs = [x for x in range(opt.rect.left(), opt.rect.right())
          if lo < doc.documentLayout().hitTest(QPointF(x - origin.x(), y - origin.y()),
                                               Qt.FuzzyHit) < hi]
    assert xs, (text, item.text(0))
    pos = QPoint(xs[len(xs) // 2], y)
    assert tree._hit(pos) == (None, True), item.text(0)
    return pos


def test_a_click_on_a_margin_or_an_f_max_does_not_clear_the_term():
    """A substitution row's margin and its F_max are not blank space: a click on either keeps
    the highlight and the diagram. A click in the empty area below the rows clears them."""
    import re

    from PySide6.QtCore import QPoint, Qt
    from PySide6.QtTest import QTest

    from swi3s_studio.ui.timing_view import _FMAX_ROLE
    tv = _shown_view()
    key, leg = _draw_first_term(tv)
    tree = tv._tree
    head = tree.topLevelItem(0)
    item = next(it for it in (head.child(c) for c in range(head.childCount()))
                if it.data(0, _FMAX_ROLE))
    plain = re.sub(r"<[^>]+>", "", item.text(0))
    margin = plain.rsplit(" = ", 1)[1].split(",")[0]
    for pos in (_text_point(tree, item, margin),
                _text_point(tree, item, item.data(0, _FMAX_ROLE))):
        QTest.mouseClick(tree.viewport(), Qt.LeftButton, pos=pos)
        assert tv._hl == {key} and tv._diagram_term == key, pos
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
    tv.flush_recompute()
    assert "minimum above the maximum, exchanged" in tv._ceiling_label.text()
    ordered = _tv()
    ordered._min[("proposed", "V_IL")].setValue(0.55)
    ordered._max[("proposed", "V_IL")].setValue(0.75)
    assert tv.summary_text() == ordered.summary_text()
    hi.editingFinished.emit()
    tv.flush_recompute()
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
    tv.flush_recompute()
    assert "exchanged" not in tv._ceiling_label.text()
    _type_into(hi, "30")
    assert (lo.value(), hi.value()) == (20.0, 30.0)
    tv.flush_recompute()
    assert "exchanged" not in tv._ceiling_label.text()
    _type_into(lo, "40")
    _type_into(hi, "")
    assert (lo.value(), hi.value()) == (30.0, 40.0)
    tv.flush_recompute()
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
                for m in [re.match(r"(?:\d+\) )?\s*(Spec|PHY2 Proposed|PHY2 EDE|Example):",
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
             "Example / PHY2 EDE"),
            ("SWI3S_PHY2", "SWI3S_PHY2_EDE", "2x", "Spec / PHY2 Proposed",
             "Example / PHY2 EDE"),
            ("SWI3S_PHY2_EDE", "SWI3S_PHY2_EDE", "2x", "PHY2 Proposed", "PHY2 EDE")):
        tv = _tv()
        tv._man_spec_combo.setCurrentIndex(tv._man_spec_combo.findData(man))
        tv._per_spec_combo.setCurrentIndex(tv._per_spec_combo.findData(per))
        tv._launch_combo.setCurrentIndex(tv._launch_combo.findData(mode))
        tv.flush_recompute()
        assert tv._column_labels() == (left, right)
        text = tv._launch_combo.toolTip()
        assert f"applies to the {right} column only; the {left} column keeps its analog" in text
        assert "Specification" not in text or left == "Specification", text
        assert "Spec / " not in text or left.startswith("Spec / "), text
        assert "seeded with" not in text and "both columns" not in text, text
    for man, per, seeded in (("SWI3S_PHY2_EDE", "SWI3S_PHY2", True),
                             ("SWI3S_PHY2", "SWI3S_PHY2_EDE", False)):
        tv = _bus(man, per)
        tv._launch_combo.setCurrentIndex(tv._launch_combo.findData("analog"))
        text = tv._launch_combo.toolTip()
        assert "both columns use the tabulated delay ranges" in text, text
        assert "applies to" not in text and "keeps its analog" not in text, text
        assert ("is seeded with the analog" in text) is seeded, text
    tv = _bus("SWI3S_PHY2", "SWI3S_PHY2")
    tv._launch_combo.setCurrentIndex(tv._launch_combo.findData("analog"))
    assert "applies to" not in tv._launch_combo.toolTip()
    assert "both columns" not in tv._launch_combo.toolTip()


def test_the_summary_header_names_the_columns_the_page_shows():
    """The copyable summary heads its columns with the page's column names: Spec and Example
    on a plain bus, PHY2 Proposed and PHY2 EDE with both sides on the EDE source, and
    both sides' names (non-EDE side first) with one."""
    tv = _tv()
    assert tv._column_labels() == ("Specification", "Example")
    plain = tv.summary_text().splitlines()[0]
    assert plain == ("Inequality | Spec margin (ns) | Example margin (ns) | ΔMargin (ns) | "
                     "Spec F_MAX (MHz) | Example F_MAX (MHz) | ΔF_MAX (MHz)")
    for man, per, left, right in (
            ("SWI3S_PHY2_EDE", "SWI3S_PHY2_EDE", "PHY2 Proposed", "PHY2 EDE"),
            ("SWI3S_PHY2", "SWI3S_PHY2_EDE", "Spec / PHY2 Proposed", "Example / PHY2 EDE"),
            ("SWI3S_PHY2_EDE", "SWI3S_PHY2", "Spec / PHY2 Proposed", "Example / PHY2 EDE")):
        tv._man_spec_combo.setCurrentIndex(tv._man_spec_combo.findData(man))
        tv._per_spec_combo.setCurrentIndex(tv._per_spec_combo.findData(per))
        tv.flush_recompute()
        assert tv._column_labels() == (left, right), (man, per)
        assert tv.summary_text().splitlines()[0] == (
            f"Inequality | {left} margin (ns) | {right} margin (ns) | ΔMargin (ns) | "
            f"{left} F_MAX (MHz) | {right} F_MAX (MHz) | ΔF_MAX (MHz)"), (man, per)


def test_a_column_heading_names_only_what_the_column_holds_on_every_row():
    """For each of the four EDE pairings, every Manager row and every Peripheral row of a
    column holds what the heading names for that row's side: a side off the EDE source holds
    the spec as it stands (left) and its Example (right), a side on it holds the PHY2 proposal
    (left) and EDE (right). Both headings name both sides' contents once only one side is on
    the EDE source."""
    from swi3s_studio.ui.timing_view import _ALL_ROWS

    def columns(tv):
        return {key: (tv._min[key].value(), tv._max[key].value()) for key in tv._min}

    plain, both = columns(_bus("SWI3S_PHY2", "SWI3S_PHY2")), columns(
        _bus("SWI3S_PHY2_EDE", "SWI3S_PHY2_EDE"))
    for side_name, prefix in (("Man", "Man_"), ("Per", "Per_")):
        names = [r.name for r in _ALL_ROWS if r.name.startswith(prefix)]
        assert any(plain[("spec", n)] != both[("spec", n)] for n in names), side_name
    expected = {(False, False): ("Specification", "Example"),
                (True, False): ("Spec / PHY2 Proposed", "Example / PHY2 EDE"),
                (False, True): ("Spec / PHY2 Proposed", "Example / PHY2 EDE"),
                (True, True): ("PHY2 Proposed", "PHY2 EDE")}
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
                        assert held.get((side, row.name)) == ref.get((side, row.name)), (
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
    """On the page, with a SoundWire Manager and an EDE Peripheral, across every inequality a
    device's t_DZ and its EDE Delay (the two ways of deciding when to stop driving) are never
    in one row, and every term of every row is a clickable anchor on its column's key. Every
    pairing of the spec selectors is checked at the calculator, in
    `test_timing_view_columns.py`."""
    from swi3s_studio.timing.calculator import INEQUALITIES
    man, per = "SW13_1V2", "SWI3S_PHY2_EDE"
    tv = _bus(man, per)
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
    """A substitution row's numbers, as Python arithmetic: the row less its label and the
    " = " margin at its end."""
    import re
    plain = re.sub(r"<[^>]+>", "", html.split("<td>", 2)[-1]).rsplit(" = ", 1)[0]
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
    """After its margin table and a binding line per column the summary lists each
    inequality's terms: one shared equation line where the columns hold the same terms, one
    line per column, headed by its label, where they differ; each column's numbers follow."""
    from swi3s_studio.timing.calculator import INEQUALITIES
    from swi3s_studio.ui.timing_view import _INEQ_NUMBER, _summary_label
    for man, per in _BUSES:
        tv = _bus(man, per)
        lines = tv.summary_text().splitlines()
        table = lines[:lines.index("")]
        left_lab, right_lab = (_summary_label(lab) for lab in tv._column_labels())
        binding = lines[len(table) + 1:len(table) + 3]
        assert len(table) == len(INEQUALITIES) + 1 and [
            ln.split(": ")[0] for ln in binding] == [
            f"Binding F_MAX, {lab}" for lab in (left_lab, right_lab)], binding
        terms = len(table) + 5
        assert lines[terms - 1] == "Terms"
        for leg in INEQUALITIES:
            n = _INEQ_NUMBER[leg]
            rows = _leg_rows(tv, leg)
            mine = [ln for ln in lines[terms:] if ln.startswith(f"{n}) ")]
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


# ---- EDE Delay -------------------------------------------------------------------------

_EDE_ROWS = ("Man_t_EDE", "Per_t_EDE")
_EDE_PAIRINGS = [(man, per) for man in ("SWI3S_PHY2", "SWI3S_PHY2_EDE")
                 for per in ("SWI3S_PHY2", "SWI3S_PHY2_EDE")]


def _ede_boxes(tv, name):
    return tv._min[("proposed", name)], tv._max[("proposed", name)]


def test_an_ede_delay_row_reads_does_not_apply_in_the_specification_column():
    """At each EDE pairing, each EDE Delay row's Specification cell reads 'does not apply' and
    holds no box, its Example cells are two editable boxes seeded 3-7 ns, the settings carry
    the Example pair and no Specification key, and the Specification column's corners name no
    EDE Delay row."""
    for man, per in _EDE_PAIRINGS:
        tv = _bus(man, per)
        d = tv.to_dict()
        for name in _EDE_ROWS:
            assert ("spec", name) not in tv._min and ("spec", name) not in tv._max
            assert tv._not_in_spec[name].text() == "does not apply"
            assert tv._not_in_spec[name].isVisibleTo(tv)
            boxes = _ede_boxes(tv, name)
            assert [b.value() for b in boxes] == [3.0, 7.0], (man, per, name)
            assert not any(b.isReadOnly() for b in boxes)
            assert (d[f"proposed::{name}::min"], d[f"proposed::{name}::max"]) == (3.0, 7.0)
            assert not any(k.startswith(f"spec::{name}") for k in d), (man, per, name)
            assert all(name not in picks for picks in tv._worst_picks["spec"].values())
        tv.close()


def test_an_ede_delay_edit_leaves_the_specification_column_as_it_was():
    """With both sides on the EDE source, a committed edit of both EDE Delay ranges moves the
    Example column's margins and leaves every Specification margin as it was."""
    tv = _bus("SWI3S_PHY2_EDE", "SWI3S_PHY2_EDE")
    spec = {k: b.margin_ns for k, b in tv._per_inequality_worst("spec").items()}
    example = {k: b.margin_ns for k, b in tv._per_inequality_worst("proposed").items()}
    for name in _EDE_ROWS:
        lo, hi = _ede_boxes(tv, name)
        lo.setValue(1.0)
        hi.setValue(12.0)
        hi.editingFinished.emit()
    tv.flush_recompute()
    assert {k: b.margin_ns for k, b in tv._per_inequality_worst("spec").items()} == spec
    assert {k: b.margin_ns for k, b in tv._per_inequality_worst("proposed").items()} != example
    tv.close()


def test_an_ede_delay_typed_with_its_minimum_above_its_maximum_is_exchanged():
    tv = _tv()
    lo, hi = _ede_boxes(tv, "Per_t_EDE")
    lo.setValue(8.0)
    lo.editingFinished.emit()
    assert (lo.value(), hi.value()) == (7.0, 8.0)
    tv.flush_recompute()
    assert "Per EDE Delay: minimum above the maximum, exchanged" in tv._ceiling_label.text()
    tv.close()


def test_the_ede_delay_ranges_round_trip_through_the_settings_and_the_example_file(
        tmp_path, monkeypatch):
    """Both EDE Delay pairs are saved as min and max, in the settings and in an Example file,
    and read back with no import warning."""
    tv = _bus("SWI3S_PHY2_EDE", "SWI3S_PHY2_EDE")
    edits = {"Man_t_EDE": (4.0, 6.5), "Per_t_EDE": (2.5, 8.0)}
    for name, (lo, hi) in edits.items():
        _ede_boxes(tv, name)[0].setValue(lo)
        _ede_boxes(tv, name)[1].setValue(hi)
    d = tv.to_dict()
    path = tmp_path / "example.json"
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *a, **k: (str(path), ""))
    tv._save_proposed()
    saved = json.loads(path.read_text())
    assert {n: (saved[n]["min"], saved[n]["max"]) for n in edits} == edits
    restored = _tv()
    assert _notes(restored, d) == []
    assert {n: tuple(b.value() for b in _ede_boxes(restored, n)) for n in edits} == edits
    assert restored.summary_text() == tv.summary_text()
    loaded = _bus("SWI3S_PHY2_EDE", "SWI3S_PHY2_EDE")
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *a, **k: (str(path), ""))
    with import_notes.collect() as notes:
        loaded._load_proposed()
    assert notes == []
    assert {n: tuple(b.value() for b in _ede_boxes(loaded, n)) for n in edits} == edits
    for t in (tv, restored, loaded):
        t.close()


def test_settings_saved_with_a_specification_ede_delay_or_with_none_load_without_a_warning():
    """Settings that carry a Specification EDE Delay pair load with no import warning and set
    no box from it; settings with no EDE Delay key at all load with no warning and leave both
    Example pairs at their seeds."""
    tv = _tv()
    d = tv.to_dict()
    d["proposed::Man_t_EDE::max"] = 6.0
    with_spec = {**d, **{f"spec::{n}::{end}": v for n in _EDE_ROWS
                         for end, v in (("min", 3.0), ("max", 7.0))}}
    fresh = _tv()
    assert _notes(fresh, with_spec) == []
    assert [b.value() for b in _ede_boxes(fresh, "Man_t_EDE")] == [3.0, 6.0]
    without = {k: v for k, v in d.items() if "_t_EDE" not in k}
    fresh = _tv()
    assert _notes(fresh, without) == []
    for name in _EDE_ROWS:
        assert [b.value() for b in _ede_boxes(fresh, name)] == [3.0, 7.0], name
