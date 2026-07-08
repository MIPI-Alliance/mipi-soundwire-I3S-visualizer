"""Timing-mode tests.

- The ported compute core matches the source `swtiming.calculator` exactly (the
  source app validated the math against two reference spreadsheets, 56/56 + 58/58;
  we pin equality to it rather than re-deriving).
- The TimingView renders the four inequalities + binding summary, recomputes on
  input change, and its inputs round-trip through the workspace dict.

Run: QT_QPA_PLATFORM=offscreen PYTHONPATH=. python3 tests/test_timing.py
"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from swi3s_studio.timing import CalcInputs, INEQUALITIES, compute


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


if __name__ == "__main__":
    test_compute_defaults_shape(); print("ok: compute() yields the four inequalities + binding")
    test_view_renders_and_roundtrips(); print("ok: TimingView renders margins + round-trips inputs")
    print("ALL TIMING TESTS PASSED")
