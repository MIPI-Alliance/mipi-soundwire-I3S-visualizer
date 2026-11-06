"""The TX swing is a PARAMETER of the crossing envelope, not a constant.

`compute_delta_tpd_envelope` accepts `V_OH_frac` / `V_OL_frac`, and `CalcInputs.envelope()`
threads the pair in from `V_OH_min_frac` / `V_OL_max_frac`. The linear crossing helpers divide
by the swing `V_OH_frac − V_OL_frac` (default 0.80 − 0.20 = 0.60), the same span
`calculator.py` reads as `V_OH_min_frac − V_OL_max_frac` (`_sw_keeper_case`).

Run: PYTHONPATH=. python3 -m pytest tests/test_delta_tpd_swing.py
"""
import dataclasses
import math

import pytest

from swi3s_studio.timing.delta_tpd import DeltaTpdEnvelope, compute_delta_tpd_envelope

# Every dimensionless coefficient the envelope publishes, read from its fields so a new one is
# covered; all of them are built from a linear crossing at the default ramp, so all of them
# must respond to the swing.
_COEFFS = tuple(f.name for f in dataclasses.fields(DeltaTpdEnvelope))


def test_the_threshold_fractions_move_every_linear_crossing():
    """On the unanchored helper (crossings counted from the start of the ramp), a wider
    output swing reaches any given threshold sooner, so every crossing coefficient shrinks;
    none stays at its default value. Each is a crossing time or a difference of two over one
    swing, so each scales as 1/swing: 0.60 → 0.80 takes every one to 0.75 of its value."""
    assert _COEFFS and all(
        isinstance(getattr(compute_delta_tpd_envelope(), c), float) for c in _COEFFS), _COEFFS
    base = compute_delta_tpd_envelope()                                  # swing 0.60
    wide = compute_delta_tpd_envelope(V_OH_frac=0.90, V_OL_frac=0.10)    # swing 0.80
    unchanged = [c for c in _COEFFS if getattr(wide, c) == getattr(base, c)]
    assert not unchanged, \
        f"these ignored V_OH_frac/V_OL_frac entirely: {unchanged}"
    # Direction, not just difference: a faster ramp crosses earlier, so |offset| falls.
    for c in _COEFFS:
        assert abs(getattr(wide, c)) < abs(getattr(base, c)), c
        assert getattr(wide, c) == pytest.approx(getattr(base, c) * 0.60 / 0.80,
                                                 rel=1e-9), c


def test_the_swing_scales_the_linear_crossings_inversely():
    """Counted from the start of a linear ramp, t_cross = V_th / (swing · V_DD), so halving
    the swing exactly doubles the crossing time. This pins the law, not a number."""
    narrow = compute_delta_tpd_envelope(V_OH_frac=0.70, V_OL_frac=0.40)   # swing 0.30
    wide = compute_delta_tpd_envelope(V_OH_frac=0.80, V_OL_frac=0.20)     # swing 0.60
    # sigma_pm_setup is a bare rising crossing with no ideal-mid offset subtracted, so the
    # ratio is clean.
    assert narrow.sigma_pm_setup == pytest.approx(2.0 * wide.sigma_pm_setup, rel=1e-12)


def test_the_default_pair_gives_the_tabulated_swing():
    """The default pair gives the 0.60 swing the tabulated numbers are derived against,
    on the unanchored helper and on the anchored path the calculator reads.

    Compared to rel=1e-12, not exactly: 0.80 − 0.20 is 0.6000000000000001 in binary.
    """
    got = compute_delta_tpd_envelope()
    assert got.sigma_pm_setup == pytest.approx(1.2572368421052635, rel=1e-12)
    assert got.correlated_diff == pytest.approx(0.9872807017543865, rel=1e-12)
    anchored = compute_delta_tpd_envelope(from_anchor=True)
    assert anchored.sigma_pm_setup == pytest.approx(0.9072368421052633, rel=1e-12)


@pytest.mark.parametrize("v_oh, v_ol", [(0.80, 0.20), (0.90, 0.10), (0.70, 0.40), (0.70, 0.30)])
def test_an_anchored_linear_crossing_counts_from_the_v_ol_level(v_oh, v_ol):
    """Counted from the 20 % point, V_OL rising, a linear crossing is the unanchored one less
    V_OL over the swing, on the crossing's own edge: (V_th/V_DD/swing − V_OL/swing)·(1 + τ)
    on a slow one. The anchor does not move with the supply corner, so a τ and a VDD_tol of
    different sizes tell the two factors apart."""
    swing = v_oh - v_ol
    kw = dict(V_OH_frac=v_oh, V_OL_frac=v_ol, VDD_tol=0.05, tRF_tolerance=0.08)
    start = compute_delta_tpd_envelope(**kw)
    anchored = compute_delta_tpd_envelope(**kw, from_anchor=True)
    assert anchored.sigma_pm_setup == pytest.approx(
        start.sigma_pm_setup - v_ol / swing * 1.08, rel=1e-9)


@pytest.mark.parametrize("v_oh, v_ol", [(0.80, 0.20), (0.85, 0.25), (0.90, 0.30), (0.70, 0.20)])
def test_a_falling_edge_counts_from_its_v_oh_level(v_oh, v_ol):
    """A falling edge's 20 % point is its V_OH crossing, on both ramp shapes: the ramp reads
    V_OH there (`ramp_level`). Counted from it, a linear falling crossing is the drop from
    V_OH to the threshold over the slope, (V_OH·V_tx − V_th) / (swing·V_tx), on the
    crossing's own edge; for V_OH + V_OL ≠ 1 this differs from the rising edge's fraction."""
    from swi3s_studio.timing.delta_tpd import crossing_corners, per_tdd_offset_frac, ramp_level
    swing = v_oh - v_ol
    for shape in ("linear", "exp"):
        f = per_tdd_offset_frac(shape, V_OH_frac=v_oh, V_OL_frac=v_ol, rising=False)
        assert ramp_level(shape, False, f, V_DD=1.0, swing_frac=swing) == pytest.approx(v_oh)
        r = per_tdd_offset_frac(shape, V_OH_frac=v_oh, V_OL_frac=v_ol, rising=True)
        assert ramp_level(shape, True, r, V_DD=1.0, swing_frac=swing) == pytest.approx(v_ol)
        assert (f == pytest.approx(r)) is (abs(v_oh + v_ol - 1.0) < 1e-12), (shape, f, r)
    tau = 0.08
    env = compute_delta_tpd_envelope(V_OH_frac=v_oh, V_OL_frac=v_ol, tRF_tolerance=tau,
                                     from_anchor=True)
    c = crossing_corners()["mp_late_fall"]
    assert not c.rising and c.tau_sign == +1
    want = (v_oh * c.vdd_tx - c.threshold) / (swing * c.vdd_tx) * (1.0 + tau)
    assert env.mp_late_fall == pytest.approx(want, rel=1e-12)


def _live(shape, **kw):
    """The envelope the calculator reads, for a pair of sides of one ramp shape."""
    from swi3s_studio.timing import CalcInputs
    return CalcInputs(Man_shape=shape, Per_shape=shape, **kw).envelope()


def test_the_calculators_envelope_takes_the_swing_from_its_inputs():
    """CalcInputs.envelope() threads V_OH_min_frac / V_OL_max_frac into the anchored
    envelope: a linear pair's crossings all move with them."""
    base = _live("linear")
    wide = _live("linear", V_OH_min_frac=0.90, V_OL_max_frac=0.10)
    unchanged = [c for c in _COEFFS if getattr(wide, c) == getattr(base, c)]
    assert not unchanged, f"these ignored the inputs' swing: {unchanged}"
    assert base.sigma_pm_setup == pytest.approx(0.9072368421052633, rel=1e-12)
    assert wide.sigma_pm_setup == pytest.approx(
        compute_delta_tpd_envelope(V_OH_frac=0.90, V_OL_frac=0.10,
                                   from_anchor=True).sigma_pm_setup, rel=1e-12)


def test_the_calculators_exponential_ramp_moves_with_v_ol():
    """On the anchored path an exponential crossing is counted from V_OL, so the per-leg
    coefficients move with it; the edge-to-edge deviations, which have no anchor, do not."""
    base = _live("exp")
    low = _live("exp", V_OH_min_frac=0.90, V_OL_max_frac=0.10)
    high = _live("exp", V_OH_min_frac=0.70, V_OL_max_frac=0.40)
    for coeff in ("sigma_pm_setup", "mp_late_rise"):
        assert len({getattr(e, coeff) for e in (base, low, high)}) == 3, coeff
    for coeff in ("rising_max", "rising_min", "falling_max", "falling_min"):
        assert getattr(low, coeff) == getattr(base, coeff) == getattr(high, coeff), coeff


def test_an_impossible_swing_is_rejected_rather_than_divided_by():
    """V_OL at or above V_OH raises ValueError instead of dividing by a zero or negative
    swing, in the envelope and in the anchor and ramp-to-valid fractions on either shape. On
    an exponential ramp, which reaches neither rail, those two fractions also refuse a V_OL
    at or below 0 and a V_OH at or above 1, naming the fraction."""
    from swi3s_studio.timing.delta_tpd import per_tdd_offset_frac, ramp_to_valid_frac
    for v_oh, v_ol in ((0.20, 0.80), (0.50, 0.50), (0.20, 0.20)):
        with pytest.raises(ValueError, match="must exceed"):
            compute_delta_tpd_envelope(V_OH_frac=v_oh, V_OL_frac=v_ol)
        for fn in (per_tdd_offset_frac, ramp_to_valid_frac):
            for shape in ("linear", "exp"):
                for rising in (True, False):
                    with pytest.raises(ValueError, match="must exceed"):
                        fn(shape, V_OH_frac=v_oh, V_OL_frac=v_ol, rising=rising)
    for kw, name in ((dict(V_OH_frac=1.0), "V_OH_frac"), (dict(V_OL_frac=0.0), "V_OL_frac"),
                     (dict(V_OL_frac=-0.1), "V_OL_frac")):
        for fn in (per_tdd_offset_frac, ramp_to_valid_frac):
            for rising in (True, False):
                with pytest.raises(ValueError, match=name):
                    fn("exp", rising=rising, **kw)
            fn("linear", **kw)


def test_the_unanchored_exponential_ramp_is_independent_of_the_swing():
    """Counted from the start of the ramp, an exponential ramp is independent of the swing;
    do not align it with the linear branch. It is defined by its time constant (t_RF is
    the 20-80 % slew, so τ = t_RF/ln 4), and the crossing time follows from V_th/V_DD alone.
    """
    base = compute_delta_tpd_envelope(ramp_shape="exp", from_anchor=False)
    wide = compute_delta_tpd_envelope(ramp_shape="exp", from_anchor=False,
                                      V_OH_frac=0.90, V_OL_frac=0.10)
    for coeff in _COEFFS:
        assert getattr(wide, coeff) == getattr(base, coeff), \
            f"{coeff} moved with the swing on an exp ramp, where it has no slope to set"


def test_the_deviation_fields_carry_no_slew_tolerance_and_uncorrelated_diff_is_their_span():
    """The `rising_*` / `falling_*` deviations count from ideal mid-swing at each crossing's
    own worst corner with no per-edge slew tolerance and no anchor, and `uncorrelated_diff` is
    `rising_max − falling_min`; `correlated_diff` carries (1 ± τ) and is not bounded by it.
    A threshold at mid-swing on a nominal supply with no noise deviates by 0 on either shape."""
    for shape in ("linear", "exp"):
        mid = compute_delta_tpd_envelope(
            ramp_shape=shape, V_IH_min_frac=0.5, V_IH_max_frac=0.5, V_IL_min_frac=0.5,
            V_IL_max_frac=0.5, VDD_tol=0.0, noise_budget_V=0.0)
        for field in ("rising_max", "rising_min", "falling_max", "falling_min"):
            assert getattr(mid, field) == pytest.approx(0.0, abs=1e-12), (shape, field)
    plain = compute_delta_tpd_envelope(tRF_tolerance=0.0)
    for env in (compute_delta_tpd_envelope(), compute_delta_tpd_envelope(tRF_tolerance=0.2),
                compute_delta_tpd_envelope(from_anchor=True)):
        for field in ("rising_max", "rising_min", "falling_max", "falling_min",
                      "uncorrelated_diff"):
            assert getattr(env, field) == getattr(plain, field), field
        assert env.uncorrelated_diff == env.rising_max - env.falling_min
    toleranced = compute_delta_tpd_envelope(tRF_tolerance=0.05)
    assert toleranced.correlated_diff > toleranced.uncorrelated_diff


@pytest.mark.parametrize("v_oh, v_ol", [(0.80, 0.20), (0.90, 0.10), (0.85, 0.25), (0.80, 0.15),
                                        (0.70, 0.40), (0.95, 0.20)])
def test_the_ramp_from_an_anchor_to_the_valid_level_is_the_ramps_own_crossing(v_oh, v_ol):
    """`ramp_to_valid_frac`, in units of t_RF, is the ramp's own time from its 20 % point to
    its 80 % point: V_OL to V_OH rising, V_OH to V_OL falling. On an exponential ramp it is
    log_4((1 − V_OL)/(1 − V_OH)) rising and log_4(V_OH/V_OL) falling, t_RF being the 20-80 %
    time, so 1 at 0.20 / 0.80 and the two polarities equal only where V_OH + V_OL = 1. On a
    linear ramp it is 1 at every level pair. Checked against the closed form and against
    `ramp_level`: the ramp reads V_OH (V_OL) that long after its anchor."""
    from swi3s_studio.timing.delta_tpd import per_tdd_offset_frac, ramp_level, ramp_to_valid_frac
    swing = v_oh - v_ol
    want = {True: math.log((1.0 - v_ol) / (1.0 - v_oh), 4.0),
            False: math.log(v_oh / v_ol, 4.0)}
    for rising in (True, False):
        exp = ramp_to_valid_frac("exp", V_OH_frac=v_oh, V_OL_frac=v_ol, rising=rising)
        assert exp == pytest.approx(want[rising], rel=1e-12), (rising, v_oh, v_ol)
        assert ramp_to_valid_frac("linear", V_OH_frac=v_oh, V_OL_frac=v_ol,
                                  rising=rising) == 1.0
        for shape in ("linear", "exp"):
            c = ramp_to_valid_frac(shape, V_OH_frac=v_oh, V_OL_frac=v_ol, rising=rising)
            z = per_tdd_offset_frac(shape, V_OH_frac=v_oh, V_OL_frac=v_ol, rising=rising)
            assert ramp_level(shape, rising, z + c, V_DD=1.0, swing_frac=swing) == \
                pytest.approx(v_oh if rising else v_ol, abs=1e-12), (shape, rising)
    tie = math.isclose(v_oh + v_ol, 1.0, abs_tol=1e-12)
    assert (want[True] == pytest.approx(want[False], rel=1e-12)) is tie
    if (v_oh, v_ol) == (0.80, 0.20):
        assert want[True] == pytest.approx(1.0, rel=1e-12)


def test_a_ramp_sampled_before_it_starts_is_at_its_start_rail():
    """`ramp_level` before the ramp starts (a negative time) is the rail it starts from: 0
    rising and V_DD falling, on either shape, as at time 0."""
    from swi3s_studio.timing.delta_tpd import ramp_level
    for shape in ("linear", "exp"):
        for rising, rail in ((True, 0.0), (False, 1.1)):
            for t in (-0.5, -3.0, 0.0):
                assert ramp_level(shape, rising, t, V_DD=1.1, swing_frac=0.6) == \
                    pytest.approx(rail, abs=1e-12), (shape, rising, t)
