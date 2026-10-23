"""The TX swing is a PARAMETER of the crossing envelope, not a constant.

`compute_delta_tpd_envelope` accepts `V_OH_frac` / `V_OL_frac`, and `CalcInputs.envelope()`
threads the pair in from `V_OH_min_frac` / `V_OL_max_frac`. The linear crossing helpers divide
by the swing `V_OH_frac − V_OL_frac` (default 0.80 − 0.20 = 0.60), the same span
`calculator.py` uses (`span = V_OH_min_frac − V_OL_max_frac`, in `_swing_ns` / `_swing_coeff`).

Run: PYTHONPATH=. python3 -m pytest tests/test_delta_tpd_swing.py
"""
import pytest

from swi3s_studio.timing.delta_tpd import compute_delta_tpd_envelope

# Every dimensionless coefficient the envelope publishes; all of them are built from a linear
# crossing at the default ramp, so all of them must respond to the swing.
_COEFFS = ("rising_max", "rising_min", "falling_max", "falling_min",
           "mp_late_rise", "mp_early_fall", "mp_late_rise_per", "mp_early_fall_per",
           "correlated_diff", "uncorrelated_diff",
           "sigma_pm_setup", "sigma_pm_hold_rr",
           "sigma_pm_hold_rf_clk", "sigma_pm_hold_rf_data")


def test_the_threshold_fractions_move_every_linear_crossing():
    """A wider output swing reaches any given threshold sooner, so every crossing
    coefficient shrinks; none stays at its default value."""
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
    doubles the crossing time. This pins the law, not a number."""
    narrow = compute_delta_tpd_envelope(V_OH_frac=0.70, V_OL_frac=0.40)   # swing 0.30
    wide = compute_delta_tpd_envelope(V_OH_frac=0.80, V_OL_frac=0.20)     # swing 0.60
    # sigma_pm_setup is a bare rising crossing with no ideal-mid offset subtracted, so the
    # ratio is clean.
    assert narrow.sigma_pm_setup == pytest.approx(2.0 * wide.sigma_pm_setup, rel=1e-12)


def test_the_default_pair_still_gives_the_tabulated_swing():
    """The default pair gives the 0.60 swing the tabulated numbers are derived against.

    Compared to rel=1e-12, not exactly: 0.80 − 0.20 is 0.6000000000000001 in binary.
    """
    got = compute_delta_tpd_envelope()
    assert got.sigma_pm_setup == pytest.approx(1.2572368421052635, rel=1e-12)
    assert got.correlated_diff == pytest.approx(0.9872807017543865, rel=1e-12)


def test_an_impossible_swing_is_rejected_rather_than_divided_by():
    """V_OL at or above V_OH raises ValueError instead of dividing by a zero or negative
    swing."""
    for v_oh, v_ol in ((0.20, 0.80), (0.50, 0.50)):
        with pytest.raises(ValueError, match="must exceed"):
            compute_delta_tpd_envelope(V_OH_frac=v_oh, V_OL_frac=v_ol)


def test_the_exponential_ramp_is_deliberately_independent_of_the_swing():
    """An exponential ramp is independent of the swing; do not align it with the linear
    branch. It is defined by its time constant (t_RF is the 20-80 % slew, so
    τ = t_RF/ln 4), and the crossing time follows from V_th/V_DD alone.
    """
    base = compute_delta_tpd_envelope(ramp_shape="exp")
    wide = compute_delta_tpd_envelope(ramp_shape="exp",
                                      V_OH_frac=0.90, V_OL_frac=0.10)
    for coeff in _COEFFS:
        assert getattr(wide, coeff) == getattr(base, coeff), \
            f"{coeff} moved with the swing on an exp ramp, where it has no slope to set"
