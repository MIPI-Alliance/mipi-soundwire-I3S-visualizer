"""The TX swing is a PARAMETER of the crossing envelope, not a constant.

`compute_delta_tpd_envelope` accepts `V_OH_frac` / `V_OL_frac`, the module's own docstring
writes the correlated differential as `(V_IH_eff + V_IL_eff − V_OH − V_OL) / swing_TX · t_RF`,
and `CalcInputs.envelope()` threads the pair in from `V_OH_min_frac` / `V_OL_max_frac`. But the
linear crossing helpers divided by a hardcoded `0.60 · V_DD`, which is just the default
0.80 − 0.20 — so the parameters were accepted, documented, passed, and ignored.

WHY THAT MATTERED WITHOUT MOVING A SINGLE SHIPPED NUMBER: `calculator.py` reads the same two
fields for real (`span = V_OH_min_frac − V_OL_max_frac`, in `_swing_ns` / `_swing_coeff`), so
the two halves of one model already disagreed about one knob. Nothing swept it — neither field
has a `CalcRow` — and at the default pair the literal happens to be exactly right, so the
disagreement was invisible. The next person to wire these to a UI row would have seen the
keeper/swing legs move while every setup and hold crossing term sat still.

Run: PYTHONPATH=. python3 -m pytest tests/test_delta_tpd_swing.py
"""
import pytest

from swi3s_studio.timing.delta_tpd import compute_delta_tpd_envelope

# Every dimensionless coefficient the envelope publishes; all of them are built from a linear
# crossing at the default ramp, so all of them must respond to the swing.
_COEFFS = ("rising_max", "rising_min", "falling_max", "falling_min",
           "mp_late_rise", "mp_early_fall", "correlated_diff", "uncorrelated_diff",
           "sigma_pm_setup", "sigma_pm_hold_rr",
           "sigma_pm_hold_rf_clk", "sigma_pm_hold_rf_data")


def test_the_threshold_fractions_move_every_linear_crossing():
    """The fix. A wider output swing reaches any given threshold sooner, so every crossing
    coefficient must shrink — not one of them may sit at its default value."""
    base = compute_delta_tpd_envelope()                                  # swing 0.60
    wide = compute_delta_tpd_envelope(V_OH_frac=0.90, V_OL_frac=0.10)    # swing 0.80
    unchanged = [c for c in _COEFFS if getattr(wide, c) == getattr(base, c)]
    assert not unchanged, \
        f"these ignored V_OH_frac/V_OL_frac entirely: {unchanged}"
    # Direction, not just difference: a faster ramp crosses earlier, so |offset| falls. The
    # rising terms are measured from V=0, so they scale as 1/swing.
    assert wide.rising_max < base.rising_max
    assert wide.sigma_pm_setup < base.sigma_pm_setup


def test_the_swing_scales_the_linear_crossings_inversely():
    """A linear ramp gives t_cross = V_th / (swing · V_DD), so halving the swing exactly
    doubles the crossing time. Pinning the LAW rather than a number keeps this meaningful if
    the corners are ever re-tabulated."""
    narrow = compute_delta_tpd_envelope(V_OH_frac=0.70, V_OL_frac=0.40)   # swing 0.30
    wide = compute_delta_tpd_envelope(V_OH_frac=0.80, V_OL_frac=0.20)     # swing 0.60
    # sigma_pm_setup is a bare rising crossing with no ideal-mid offset subtracted, so the
    # ratio is clean.
    assert narrow.sigma_pm_setup == pytest.approx(2.0 * wide.sigma_pm_setup, rel=1e-12)


def test_the_default_pair_still_gives_the_tabulated_swing():
    """Regression anchor: the shipped default must remain the 0.60 swing every tabulated
    number was derived against, so this fix moved no released value.

    Exact equality would be wrong here — 0.80 − 0.20 is 0.6000000000000001 in binary, so
    computing the swing from its definition (which is the point of the fix) differs from the
    old literal by ~2 ULP. That is 1e-16 relative on a dimensionless fraction of t_RF.
    """
    got = compute_delta_tpd_envelope()
    assert got.sigma_pm_setup == pytest.approx(1.2572368421052635, rel=1e-12)
    assert got.correlated_diff == pytest.approx(0.9872807017543865, rel=1e-12)


def test_an_impossible_swing_is_rejected_rather_than_divided_by():
    """V_OL above V_OH is not a pessimistic corner, it is a contradiction: the linear helper
    would divide by zero or by a negative number and return a nonsensical (or infinite)
    crossing time that would propagate into a margin as if it were physics."""
    for v_oh, v_ol in ((0.20, 0.80), (0.50, 0.50)):
        with pytest.raises(ValueError, match="must exceed"):
            compute_delta_tpd_envelope(V_OH_frac=v_oh, V_OL_frac=v_ol)


def test_the_exponential_ramp_is_deliberately_independent_of_the_swing():
    """NOT an oversight, and not to be "fixed" by symmetry with the linear branch. An
    exponential ramp is defined by its time constant — t_RF is the 20-80 % slew, so
    τ = t_RF/ln 4 — and the crossing time follows from V_th/V_DD alone. There is no
    straight-line slope between V_OL and V_OH for the swing to set.
    """
    base = compute_delta_tpd_envelope(ramp_shape="exp")
    wide = compute_delta_tpd_envelope(ramp_shape="exp",
                                      V_OH_frac=0.90, V_OL_frac=0.10)
    for coeff in _COEFFS:
        assert getattr(wide, coeff) == getattr(base, coeff), \
            f"{coeff} moved with the swing on an exp ramp, where it has no slope to set"
