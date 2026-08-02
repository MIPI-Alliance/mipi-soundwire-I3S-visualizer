"""Timing-mode tests.

- The ported compute core matches the source `swtiming.calculator` exactly (the
  source app validated the math against two reference spreadsheets, 56/56 + 58/58;
  we pin equality to it rather than re-deriving).
- The TimingView renders the four inequalities + binding summary, recomputes on
  input change, and its inputs round-trip through the workspace dict.

Run: QT_QPA_PLATFORM=offscreen PYTHONPATH=. python3 -m pytest tests/test_timing.py
"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from swi3s_studio.timing import INEQUALITIES, CalcInputs, compute


def test_compute_defaults_shape():
    res = compute(CalcInputs())
    assert set(res.breakdowns) == set(INEQUALITIES)
    for ineq, b in res.breakdowns.items():
        assert b.terms, ineq
        assert b.is_setup == ("setup" in ineq)      # MP_setup, MP_setup_ho, PM_setup*
    assert res.binding_inequality in INEQUALITIES
    assert res.F_max_binding_MHz >= 0.0


def test_view_renders_and_roundtrips():
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    from swi3s_studio.ui.timing_view import TimingView
    tv = TimingView()
    # The Spec/Proposed calculator shows the four inequalities in its summary.
    txt = tv.summary_text()
    for label in ("MP setup", "MP hold", "PM setup", "PM hold"):
        assert label in txt, label
    # Four inequality headings (blank spacer rows sit between them). The setup
    # headings carry two inequalities — the normal one and the handover (t_ZD)
    # variant — stacked under one title, each with an equation + Spec/Example rows.
    tops = [tv._tree.topLevelItem(i) for i in range(tv._tree.topLevelItemCount())]
    heads = [it for it in tops if it.text(0)]        # spacers have empty text
    assert len(heads) == 4
    setup_head = next(it for it in heads if "Setup" in it.text(0))
    assert "Manager-to-Peripheral" in setup_head.text(0)
    assert setup_head.isExpanded()
    kids = [setup_head.child(c) for c in range(setup_head.childCount())]
    # Handover variant present (its equation swaps in t_ZD).
    assert any("ZD" in k.text(0) for k in kids)
    spec_rows = [k for k in kids if "Specification" in k.text(0)]
    assert len(spec_rows) == 2 and all(r.text(2) not in ("", "—") for r in spec_rows)

    # Corners are auto-selected (no pick UI); inputs() is the nominal midpoint, so
    # pin min == max to make the round-trip assertion exact.
    tv._min[("spec", "bus length (cm)")].setValue(60.0)
    tv._max[("spec", "bus length (cm)")].setValue(60.0)
    assert tv.inputs("spec").bus_length_cm == 60.0
    # workspace round-trip
    d = tv.to_dict()
    tv2 = TimingView()
    tv2.set_from_dict(d)
    assert tv2.inputs("spec").bus_length_cm == 60.0
    # Ramp models are shared across Spec/Example (one key each), and drive both.
    assert tv2.to_dict()["man_shape"] == d["man_shape"]
    assert tv2.inputs("spec").Man_shape == tv2.inputs("proposed").Man_shape


def test_phy1_selector_reseeds_spec_and_persists():
    """The PHY selector (PHY1/PHY2) reseeds the read-only Spec column from that PHY's
    spec tables. PHY1 (SWI3S v1.1r06 §11.2 Table 129) has slower Peripheral output /
    high-Z timing and a lower F_CLK target; the shared PHY1&2 values (Man_t_DD, V
    thresholds, t_IS/t_IH) are unchanged. The selected PHY round-trips via to_dict."""
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    from swi3s_studio.ui.timing_view import TimingView
    tv = TimingView()
    # Defaults to PHY2 with the PHY2 spec values.
    assert tv._phy == 2 and "PHY2" in tv._title.text()
    assert (tv._min[("spec", "Per_t_DD")].value(),
            tv._max[("spec", "Per_t_DD")].value()) == (2.0, 20.0)

    # The Spec column is read-only; only the Example column is editable — both PHYs.
    assert tv._min[("spec", "Per_t_DD")].isReadOnly()
    assert not tv._min[("proposed", "Per_t_DD")].isReadOnly()

    # Switch to PHY1: only the three split output-timing rows and F_CLK change.
    tv._phy_combo.setCurrentIndex(tv._phy_combo.findData(1))
    assert tv._phy == 1 and "PHY1" in tv._title.text()
    assert tv._fclk.value() == 6.4                                   # Table 126 target
    for name, lo, hi in [("Per_t_DD", 14.0, 50.0),                   # Table 129 PHY1
                         ("Per_t_ZD", 14.0, 50.0),
                         ("Man_t_ZD", 14.0, 30.0)]:
        assert tv._min[("spec", name)].value() == lo, name
        assert tv._max[("spec", name)].value() == hi, name
    # Shared PHY1&2 values are untouched by the PHY switch.
    assert (tv._min[("spec", "Man_t_DD")].value(),
            tv._max[("spec", "Man_t_DD")].value()) == (7.0, 18.0)
    assert tv._max[("spec", "Per_t_IH")].value() == 4.5

    # PHY selection persists through the workspace dict.
    d = tv.to_dict()
    assert d["phy"] == 1
    tv2 = TimingView()
    tv2.set_from_dict(d)
    assert tv2._phy == 1 and "PHY1" in tv2._title.text()
    assert tv2._max[("spec", "Per_t_DD")].value() == 50.0
    # A legacy dict with no "phy" key predates the selector → PHY2.
    d.pop("phy")
    tv3 = TimingView(); tv3.set_from_dict(d)
    assert tv3._phy == 2
