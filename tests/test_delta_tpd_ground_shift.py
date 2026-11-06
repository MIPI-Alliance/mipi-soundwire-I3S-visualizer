"""Ground-shift (ΔGND / α) handling in the threshold-crossing envelope.

The PM legs send a signal out and get one back, so TX and RX roles swap between
them. One physical ground shift δ = V_Per_gnd − V_Man_gnd therefore raises one
leg's effective threshold and lowers the other's.

The ±δ terms cancel exactly only when t_cross is AFFINE in the threshold (a
linear ramp) AND the two legs carry EQUAL t_RF (so the ±δ terms are equally
weighted). Otherwise a residue survives, and because Σ_setup is a PENALTY it
reduces the PM setup margin. Under the exponential ramp t_cross is convex, so by
Jensen the ±δ pair sums to strictly more than the δ=0 value: ~+0.57 ns at
α = 0.10, t_RF = 5 ns.

What is pinned here:

1. Sigma_cross,PM,setup's same-polarity case cancels exactly (linear + equal t_RF),
   which is the default configuration.
2. The other two cases move with α, in the pessimistic direction.
3. The accessor agrees with an independent recomputation of the same-polarity case at
   its two ground-shift polarities and of the cross case, the data on the other polarity
   at the one ground shift that makes both crossings late, so it never cancels.
4. hold-rr keeps its δ=0 simplification, and that stays inert: in the worst-corner
   envelope rr never binds, and with one supply per device `compute` reports it only at
   α = 0, where the simplification is exact.
5. ∂margin/∂α ≤ 0 for EVERY inequality. The individual cross terms do NOT all
   move together — Δ_cross,MP and Σ_setup are penalties that shrink as α falls,
   while Σ_cross,PM,hold is a CREDIT that grows.

Items 1 to 4 hold on the envelope counted from the start of the ramp and on the one counted
from the edge's 20 % point (`from_anchor`), which is the one `CalcInputs.envelope` reads.
The anchor takes the same (1 ± τ) share off each crossing, so a cancellation, a residue
across α and a dominance between cases are the same in both; a worked number names its mode.

Run: PYTHONPATH=. python3 -m pytest tests/test_delta_tpd_ground_shift.py
"""
import math
from dataclasses import replace as dc_replace

import pytest

from swi3s_studio.timing import CalcInputs, compute
from swi3s_studio.timing.delta_tpd import (
    SIGMA_PM_SETUP_CASES,
    _t_cross_frac_falling,
    _t_cross_frac_rising,
    compute_delta_tpd_envelope,
)

# The V_IH / V_IL corners and TX swing Σ_setup is built on (compute_delta_tpd_envelope
# defaults), restated here so the oracle below is independent of the model.
_V_IH_MAX, _V_IL_MIN, _VDD_PLUS, _VDD_MINUS, _TRF_TOL = 0.65, 0.35, 1.05, 0.95, 0.05
# The swing is (V_OH − V_OL)/V_DD at those same defaults. Written as the subtraction, not as
# the literal 0.60: 0.80 − 0.20 is not exactly 0.60 in binary, and the oracle must match the
# model to the ULP.
_SWING = 0.80 - 0.20

_ALPHAS = (0.10, 0.075, 0.05, 0.025, 0.0)
# Both counting origins: from the start of the ramp, and from its 20 % point.
_ANCHORED = pytest.mark.parametrize("anchored", [False, True], ids=["from-start", "from-20%"])


# The default V_IH / V_IL window, and one below mid-rail where the falling-launch cases differ
# from the rising ones: (V_IH_min, V_IH_max, V_IL_min, V_IL_max).
_DEFAULT_WINDOW = (0.45, _V_IH_MAX, _V_IL_MIN, 0.55)
_LOW_WINDOW = (0.40, 0.60, 0.30, 0.50)


def _env(alpha, shape, anchored=False, window=_DEFAULT_WINDOW):
    """Envelope at a given α (expressed as the spec's noise budget at 1.2 V), counted from the
    start of the ramp or, `anchored`, from its 20 % point, in the V_IH / V_IL `window`."""
    ih_min, ih_max, il_min, il_max = window
    return compute_delta_tpd_envelope(
        noise_budget_V=alpha * 1.2, V_nom_V=1.2, ramp_shape=shape, from_anchor=anchored,
        V_IH_min_frac=ih_min, V_IH_max_frac=ih_max, V_IL_min_frac=il_min,
        V_IL_max_frac=il_max)


def _anchor(shape):
    """The ramp before its 20 % point (V_OL = 0.20) in units of t_RF, from the ramp's
    definition: V_OL over the swing on a linear ramp, −ln(1 − V_OL)/ln 4 on an exponential
    one, whose t_RF is the 20-80 % time."""
    return 0.20 / _SWING if shape == "linear" else -math.log(1.0 - 0.20) / math.log(4.0)


def _same_polarity_ns(env, tRF_Man, tRF_Per):
    """Σ_cross,PM,setup's same-polarity case on a rising launching edge: the worse of its two
    ground-shift polarities."""
    return max(getattr(env, c) * tRF_Man + getattr(env, d) * tRF_Per
               for c, d in SIGMA_PM_SETUP_CASES[True][:2])


def _sigma_setup_oracle(alpha, tRF_Man, tRF_Per, shape, anchored=False, *,
                        launch_rising=True, window=_DEFAULT_WINDOW):
    """Σ_cross,PM,setup from first principles: one physical δ of magnitude α raises one leg's
    threshold and lowers the other's. Data on the clock's polarity: the worse δ polarity. Data
    on the other polarity: the δ that delays both crossings, +α under a rising clock (raising
    its threshold and lowering the falling data's) and −α under a falling one. A rising
    crossing is at V_IH,max from a low-supply transmitter, a falling one at V_IL,min from a
    high-supply one, each on a slow edge, (1 + τ), counted from the ramp's start or,
    `anchored`, from its 20 % point (the same fraction on either polarity at V_OH + V_OL = 1).

    Deliberately does NOT reuse the envelope's per-leg coefficients, so this
    stays an independent check rather than a restatement of the model.
    """
    a0 = _anchor(shape) if anchored else 0.0
    _, ih_max, il_min, _ = window

    def rise(shift):
        return (_t_cross_frac_rising(ih_max * _VDD_PLUS + shift, _VDD_MINUS, shape,
                                     swing_frac=_SWING) - a0) * (1.0 + _TRF_TOL)

    def fall(shift):
        return (_t_cross_frac_falling(il_min * _VDD_MINUS + shift, _VDD_PLUS, shape,
                                      swing_frac=_SWING) - a0) * (1.0 + _TRF_TOL)

    same, other, cross = (rise, fall, +alpha) if launch_rising else (fall, rise, -alpha)
    return max(same(+alpha) * tRF_Man + same(-alpha) * tRF_Per,
               same(-alpha) * tRF_Man + same(+alpha) * tRF_Per,
               same(cross) * tRF_Man + other(-cross) * tRF_Per)


@_ANCHORED
def test_linear_ramp_at_equal_tRF_still_cancels_the_ground_shift(anchored):
    """The one case where cancellation is exact, and the default config: the same-polarity
    case.

    "Exact" is analytic, not bit-exact: the two legs are summed as separate ±α
    terms, so the result carries a few ULPs of rounding. 1e-9 ns is six orders of
    magnitude below anything the margins resolve.
    """
    values = [_same_polarity_ns(_env(a, "linear", anchored), 5.0, 5.0) for a in _ALPHAS]
    assert max(values) - min(values) < 1e-9, \
        f"linear + equal t_RF must be flat in α, got {values}"
    # And equal to the δ=0 per-leg coefficient scaled across both legs.
    env = _env(0.10, "linear", anchored)
    assert _same_polarity_ns(env, 5.0, 5.0) == pytest.approx(
        env.sigma_pm_setup * 10.0, abs=1e-9)


def test_the_cross_case_does_not_cancel_the_ground_shift():
    """A rising clock with falling data: δ = +α raises the clock's threshold at the Peripheral
    and lowers the data's at the Manager, so both crossings are late and α adds. Linear ramps,
    5 ns on both legs, (1 + τ) = 1.05, each crossing at its own worst supplies:

        clock  (0.65·1.05 + 0.1) / (0.6·0.95)          = 1.37281
        data   (1.05 − (0.35·0.95 − 0.1)) / (0.6·1.05) = 1.29762
        Σ      (1.37281 + 1.29762) · 1.05 · 5          = 14.0197 ns   from the ramp's start
        Σ      (1.37281 + 1.29762 − 2/3) · 1.05 · 5    = 10.5197 ns   from the 20 % point

    The second is the one `compute` reads: each crossing less the ramp before its 20 % point,
    0.20 / 0.60 = 1/3 of t_RF. It binds over the same-polarity case at α = 0.10, and falls
    with α, in both."""
    clk = (0.65 * 1.05 + 0.1) / (0.6 * 0.95)
    data = (1.05 - (0.35 * 0.95 - 0.1)) / (0.6 * 1.05)
    assert round(clk, 5) == 1.37281 and round(data, 5) == 1.29762
    for anchored, a0, total in ((False, 0.0, 14.0197), (True, 1.0 / 3.0, 10.5197)):
        env = _env(0.10, "linear", anchored)
        assert env.sigma_pm_setup_ns(5.0, 5.0) == pytest.approx(
            (clk + data - 2 * a0) * 1.05 * 5.0, abs=1e-9), anchored
        assert env.sigma_pm_setup_ns(5.0, 5.0) == pytest.approx(total, abs=5e-5), anchored
        assert env.sigma_pm_setup_ns(5.0, 5.0) > _same_polarity_ns(env, 5.0, 5.0) + 1.0
        assert env.sigma_pm_setup_fields(5.0, 5.0) == SIGMA_PM_SETUP_CASES[True][2]
        got = [_env(a, "linear", anchored).sigma_pm_setup_ns(5.0, 5.0) for a in _ALPHAS]
        assert got == sorted(got, reverse=True), (anchored, got)


@_ANCHORED
def test_exp_ramp_does_not_cancel_the_ground_shift(anchored):
    """Convex t_cross ⇒ the ±δ pair sums above the δ=0 value (Jensen), so
    Σ_setup must grow with α instead of sitting flat. The residue across α is the same
    counted from either origin."""
    got = [_same_polarity_ns(_env(a, "exp", anchored), 5.0, 5.0) for a in _ALPHAS]
    assert got == sorted(got, reverse=True), \
        f"Σ_setup must fall monotonically as α falls, got {got}"
    assert got[0] - got[-1] == pytest.approx(0.5701, abs=5e-4), \
        "the α=0.10 residue on the exp ramp"
    # The residue is a PENALTY: it never falls below the δ=0 baseline.
    flat = _same_polarity_ns(_env(0.0, "exp", anchored), 5.0, 5.0)
    assert all(v >= flat - 1e-12 for v in got)


@_ANCHORED
def test_unequal_leg_tRF_breaks_cancellation_even_for_a_linear_ramp(anchored):
    """Affine t_cross cancels ±δ only against an equal weight. Unequal per-leg
    t_RF leaves a residue linear in δ, which the single-coefficient form
    (σ · (t_RF_Man + t_RF_Per)) cannot express."""
    got = [_same_polarity_ns(_env(a, "linear", anchored), 2.0, 8.0) for a in _ALPHAS]
    assert got == sorted(got, reverse=True), \
        f"unequal t_RF must reintroduce α dependence, got {got}"
    assert got[0] > got[-1] + 1.0, \
        "a 4:1 leg asymmetry at α=0.10 should cost more than 1 ns"


@_ANCHORED
@pytest.mark.parametrize("shape", ["linear", "exp"])
@pytest.mark.parametrize("tRF", [(5.0, 5.0), (5.0, 10.0), (2.0, 8.0), (20.0, 1.0)])
@pytest.mark.parametrize("launch_rising", [True, False], ids=["rising", "falling"])
@pytest.mark.parametrize("window", [_DEFAULT_WINDOW, _LOW_WINDOW], ids=["default", "low"])
def test_sigma_setup_matches_an_independent_two_polarity_recomputation(
        shape, tRF, anchored, launch_rising, window):
    """The accessor is exact, not approximate: the worst δ is at an endpoint
    (the sum is convex in δ), so no interior search is needed, and the cross case is
    at the endpoint that delays both crossings. On either launching edge, in the default
    window and one below mid-rail."""
    for alpha in _ALPHAS:
        got = _env(alpha, shape, anchored, window).sigma_pm_setup_ns(
            *tRF, launch_rising=launch_rising)
        assert got == pytest.approx(
            _sigma_setup_oracle(alpha, *tRF, shape, anchored, launch_rising=launch_rising,
                                window=window), abs=1e-12), \
            f"{shape} α={alpha} t_RF={tRF} anchored={anchored} rising={launch_rising} {window}"


@_ANCHORED
@pytest.mark.parametrize("shape", ["linear", "exp"])
def test_hold_rr_never_binds_in_the_worst_corner_envelope(shape, anchored):
    """hold-rr keeps the δ=0 form, which is exact only at equal per-leg t_RF. In the
    worst-corner envelope (no supplies) Σ_hold takes min(rr, rf), and rr's per-leg
    coefficients dominate rf's on BOTH legs, so rf always wins. With one supply per device,
    see test_the_reported_pm_hold_takes_the_rr_case_only_without_noise.
    """
    env = _env(0.10, shape, anchored)
    assert env.sigma_pm_hold_rr_clk >= env.sigma_pm_hold_rf_clk
    assert env.sigma_pm_hold_rr_data >= env.sigma_pm_hold_rf_data
    # Dominance per leg ⇒ rf binds for ANY positive t_RF pair, including the
    # extreme asymmetries where rr's own δ=0 shortcut would be least accurate.
    for tRF_Man, tRF_Per in ((1.0, 1.0), (1.0, 20.0), (20.0, 1.0), (5.0, 5.0)):
        rr = (env.sigma_pm_hold_rr_clk * tRF_Man
              + env.sigma_pm_hold_rr_data * tRF_Per)
        rf = (env.sigma_pm_hold_rf_clk * tRF_Man
              + env.sigma_pm_hold_rf_data * tRF_Per)
        assert rf < rr, f"rr became binding at t_RF=({tRF_Man}, {tRF_Per})"
        assert env.sigma_pm_hold_ns(tRF_Man, tRF_Per) == pytest.approx(rf)


@pytest.mark.parametrize("shape", ["linear", "exp"])
@pytest.mark.parametrize("man,per", [("SWI3S_PHY2", "SWI3S_PHY2"), ("SW13_1V2", "SWI3S_PHY2"),
                                     ("SWI3S_PHY2", "SW13_1V2"), ("SW13_1V2", "SW13_1V2")])
def test_reducing_alpha_never_worsens_any_margin(shape, man, per):
    """∂margin/∂α ≤ 0 on every leg: SWI3S and SoundWire on either side, both ramp shapes, zero
    and two handover UIs, and one supply for every device or one per device (δ 0 and 5 %).

    Checked on the margins, not on term magnitudes: Σ_cross,PM,hold is a credit
    and GROWS as α falls, while the two penalties shrink.
    """
    for ho, delta in ((0.0, 0.0), (0.0, 0.05), (2.0, 0.0), (2.0, 0.05)):
        base = CalcInputs(Man_spec=man, Per_spec=per, Man_shape=shape, Per_shape=shape,
                          handover_UIs=ho, V_SEOS_tol_frac=delta)
        series = [compute(dc_replace(base, V_noise_pp_frac=a)).breakdowns
                  for a in _ALPHAS]
        for name in series[0]:
            margins = [s[name].margin_ns for s in series]
            # α descends across the series, so margins must be non-decreasing.
            # Tolerance absorbs ULP-level wobble on the legs that cancel exactly.
            assert all(b >= a - 1e-9 for a, b in zip(margins, margins[1:])), \
                f"{name} margin must not worsen as α falls ({man}, {per}, {ho}, {delta}): " \
                f"{margins}"


def test_the_hold_credit_grows_as_the_penalties_shrink():
    """Pins the sign asymmetry: less noise makes the data stay valid LONGER at the
    Manager, so the hold credit grows while Δ_cross,MP shrinks."""
    hi, lo = _env(0.10, "linear"), _env(0.0, "linear")
    assert (lo.delta_cross_MP_ns(5.0, 5.0, direction="hold")
            < hi.delta_cross_MP_ns(5.0, 5.0, direction="hold")), \
        "Δ_cross,MP is a penalty — it must shrink as α falls"
    assert lo.sigma_pm_hold_ns(5.0, 5.0) > hi.sigma_pm_hold_ns(5.0, 5.0), \
        "Σ_cross,PM,hold is a credit — it must grow as α falls"


# ---------------------------------------------------------------------------
# Δ_cross,MP: which LANE carries the late edge depends on the direction.
# ---------------------------------------------------------------------------

def test_delta_cross_MP_is_lane_agnostic_only_when_the_lanes_match():
    """Equal per-lane t_RF ⇒ setup and hold coincide, so the per-direction split
    changes nothing at equal t_RF."""
    env = _env(0.10, "linear")
    for tRF in (2.0, 5.0, 8.3):
        assert (env.delta_cross_MP_ns(tRF, tRF, direction="setup")
                == pytest.approx(env.delta_cross_MP_ns(tRF, tRF, direction="hold")))


@pytest.mark.parametrize("shape", ["linear", "exp"])
def test_setup_puts_the_late_edge_on_the_data_lane_and_hold_on_the_clock(shape):
    """The physics: MP setup is worst when the DATA arrives late and the clock
    samples early; MP hold is worst when the CLOCK samples late and the data
    transitions early. So the late (rising) crossing scales with t_RF_DATA for
    setup and t_RF_CLK for hold — the two t_RF swap between directions.

    Using the hold assignment for setup would understate a setup PENALTY.
    """
    env = _env(0.10, shape)
    tRF_CLK, tRF_DATA = 3.0, 8.0
    assert env.delta_cross_MP_ns(tRF_CLK, tRF_DATA, direction="setup") == pytest.approx(
        env.mp_late_rise * tRF_DATA - env.mp_early_fall * tRF_CLK)
    assert env.delta_cross_MP_ns(tRF_CLK, tRF_DATA, direction="hold") == pytest.approx(
        env.mp_late_rise * tRF_CLK - env.mp_early_fall * tRF_DATA)
    # Slow data against a fast clock: setup is the larger penalty.
    assert (env.delta_cross_MP_ns(tRF_CLK, tRF_DATA, direction="setup")
            > env.delta_cross_MP_ns(tRF_CLK, tRF_DATA, direction="hold") + 5.0), \
        "a 3:8 lane mismatch should separate the two directions by several ns"


@pytest.mark.parametrize("shape", ["linear", "exp"])
def test_which_polarity_pairing_binds_depends_on_the_window(shape):
    """Why every setup and hold leg evaluates both sampling polarities.

    A late crossing pairs with an early crossing of the other polarity: rising-late with
    falling-early, or falling-late with rising-early. In the default window at the
    worst-corner supplies the first dominates the second for every positive t_RF pair (its
    late crossing is later AND its early crossing earlier). In a window below mid-rail,
    V_IH 0.40..0.60 and V_IL 0.30..0.50, the dominance is reversed, so no pairing can be
    fixed in advance.
    """
    vp, vm, a, tau = 1.05, 0.95, 0.10, 0.05

    def pairings(ih_min, ih_max, il_min, il_max):
        late_rise = _t_cross_frac_rising(ih_max * vp + a, vm, shape, swing_frac=_SWING)
        early_fall = _t_cross_frac_falling(il_max * vp + a, vm, shape, swing_frac=_SWING)
        late_fall = _t_cross_frac_falling(il_min * vm - a, vp, shape, swing_frac=_SWING)
        early_rise = _t_cross_frac_rising(ih_min * vm - a, vp, shape, swing_frac=_SWING)
        return ((late_rise * (1 + tau), early_fall * (1 - tau)),
                (late_fall * (1 + tau), early_rise * (1 - tau)))

    for window, rising_binds in (((0.45, 0.65, 0.35, 0.55), True),
                                 ((0.40, 0.60, 0.30, 0.50), False)):
        (late_r, early_f), (late_f, early_r) = pairings(*window)
        assert (late_r > late_f) is rising_binds, window
        assert (early_f < early_r) is rising_binds, window
        # Both inequalities strict ⇒ the same pairing binds for every positive weighting.
        for A, B in ((1.0, 1.0), (1.0, 20.0), (20.0, 1.0), (3.0, 8.0)):
            assert (late_r * A - early_f * B > late_f * A - early_r * B) is rising_binds


@pytest.mark.parametrize("shape", ["linear", "exp"])
def test_each_falling_case_mirrors_its_rising_one_in_a_symmetric_window(shape):
    """In a window symmetric about mid-rail, at one supply for both devices, each envelope's
    case on a falling sampling or launching edge carries the coefficients of its rising one:
    Delta_cross,MP and its P->P construction in both directions, Sigma_cross,PM,setup's cases
    (its cross case's clock and data swap polarities), and Sigma_cross,PM,hold's ff and fr
    cases against rr and rf.
    Below mid-rail they differ."""
    from swi3s_studio.timing.delta_tpd import compute_delta_tpd_envelope_per_device
    for window, tie in (((0.45, 0.65, 0.35, 0.55), True), ((0.40, 0.60, 0.30, 0.50), False)):
        ih_min, ih_max, il_min, il_max = window
        env = compute_delta_tpd_envelope_per_device(
            Man_shape=shape, Per_shape=shape, V_IH_min_frac=ih_min, V_IH_max_frac=ih_max,
            V_IL_min_frac=il_min, V_IL_max_frac=il_max, from_anchor=True,
            supplies=(1.0, 1.0, 1.0))
        pairs = []
        for direction in ("setup", "hold"):
            for lanes in (env.delta_cross_MP_lanes, env.delta_cross_PP_lanes):
                pairs.append((lanes(direction=direction, sampling_rising=True),
                              lanes(direction=direction, sampling_rising=False)))
        # Raising a falling threshold makes its crossing early, so the polarity Sigma_setup
        # selects is the mirror one and the sums agree. Unequal slews, because below
        # mid-rail at equal ones both launching edges take their cross case, whose two
        # crossings are a late rising and a late falling one on either.
        for k, s_ in ((3.0, 8.3), (8.3, 3.0)):
            pairs.append(((env.sigma_pm_setup_ns(k, s_, launch_rising=True),),
                          (env.sigma_pm_setup_ns(k, s_, launch_rising=False),)))
        pairs.append(((env.sigma_pm_setup_clk_dgnd_pos, env.sigma_pm_setup_fall_data_dgnd_neg),
                      (env.sigma_pm_setup_fall_clk_dgnd_neg, env.sigma_pm_setup_data_dgnd_pos)))
        pairs.append(((env.sigma_pm_hold_rr_clk, env.sigma_pm_hold_rr_data),
                      (env.sigma_pm_hold_ff_clk, env.sigma_pm_hold_ff_data)))
        pairs.append(((env.sigma_pm_hold_rf_clk, env.sigma_pm_hold_rf_data),
                      (env.mp_early_fall, env.sigma_pm_hold_fr_data)))
        for rising, falling in pairs:
            same = rising == pytest.approx(falling, abs=1e-12) if isinstance(rising[0], float) \
                else all(r[0] == f[0] and r[1] == pytest.approx(f[1], abs=1e-12)
                         for r, f in zip(rising, falling))
            assert same is tie, (window, rising, falling)


def test_a_threshold_the_ramp_never_reaches_is_refused_on_both_shapes():
    """A threshold at or beyond the transmitter's rail has no crossing: both ramp shapes raise
    ValueError naming it, and so does `compute`, rather than returning an infinite or
    past-the-ramp coefficient."""
    for shape in ("linear", "exp"):
        for v in (0.95, 1.2, 0.0, -0.1):
            with pytest.raises(ValueError, match="never crosses"):
                _t_cross_frac_rising(v, 0.95, shape, swing_frac=0.6)
            with pytest.raises(ValueError, match="never crosses"):
                _t_cross_frac_falling(v, 0.95, shape, swing_frac=0.6)
    for kw in ({"V_noise_pp_frac": 0.3, "Man_shape": "exp"}, {"V_noise_pp_frac": 0.3},
               {"V_IH_max_frac": 0.9, "Man_shape": "exp", "Per_shape": "exp"}):
        with pytest.raises(ValueError, match="never crosses"):
            compute(CalcInputs(**kw))


def test_the_reported_pm_hold_takes_the_rr_case_only_without_noise():
    """With one supply per device the same-polarity case, rr on a rising launching edge and ff
    on a falling one, can be the smaller sum at some assignments, but at the assignment
    `compute` reports for PM_hold and PM_hold_ho it is in force only at α = 0, where its
    δ = 0 evaluation is exact. Swept over SWI3S and SoundWire sides, both shapes on each
    device, α, τ, unequal slews and the default window and one below mid-rail; the sweep
    reports the case at α = 0 (the detector's positive control)."""
    import itertools

    seen = 0
    for man, per, ms, ps, a, t, (k, s), w in itertools.product(
            ("SWI3S_PHY2", "SW13_1V2"), ("SWI3S_PHY2", "SW13_1V2"), ("linear", "exp"),
            ("linear", "exp"), (0.0, 0.05, 0.10), (0.0, 0.05),
            ((3.0, 8.3), (8.3, 3.0), (3.0, 3.0)),
            ((0.45, 0.65, 0.35, 0.55), (0.40, 0.60, 0.30, 0.50))):
        inp = CalcInputs(Man_spec=man, Per_spec=per, Man_shape=ms, Per_shape=ps,
                         V_noise_pp_frac=a, V_SEOS_tol_frac=0.05, tRF_tolerance_frac=t,
                         tRF_Man_CLK_ns=k, tRF_Man_DATA_ns=k, tRF_Per_DATA_ns=s,
                         V_IH_min_frac=w[0], V_IH_max_frac=w[1],
                         V_IL_min_frac=w[2], V_IL_max_frac=w[3], handover_UIs=2.0)
        r = compute(inp).breakdowns
        for leg in ("PM_hold", "PM_hold_ho"):
            same = any(",rr," in x.symbol or ",ff," in x.symbol for x in r[leg].terms)
            assert not same or a == 0.0, (leg, man, per, ms, ps, a, t, k, s, w)
            seen += same
    assert seen, "the sweep must report the same-polarity case somewhere at α = 0"
    # And the detector sees a known case: below mid-rail, α = 0, a slow Peripheral DATA lane.
    known = compute(CalcInputs(V_noise_pp_frac=0.0, V_SEOS_tol_frac=0.0, tRF_tolerance_frac=0.0,
                               tRF_Man_CLK_ns=3.0, tRF_Man_DATA_ns=3.0, tRF_Per_DATA_ns=8.3,
                               V_IH_min_frac=0.40, V_IH_max_frac=0.60,
                               V_IL_min_frac=0.30, V_IL_max_frac=0.50)).breakdowns["PM_hold"]
    assert [x.symbol for x in known.terms if ",rr," in x.symbol] == [
        "X_PM,rr,early · Man_t_RF,CLK", "X_PM,rr,early · Per_t_RF,DATA"], known.terms


def test_each_default_corner_puts_its_supplies_and_bound_at_the_end_it_names():
    """Without per-device supplies every `crossing_corners` corner is at the worst end for
    its role: a late rising crossing at V_IH,max from a high-supply receiver and a low-supply
    transmitter, a late falling one at V_IL,min from a low-supply receiver and a high-supply
    transmitter, an early crossing the mirror of each. A crossing with a ground shift moves
    the same way, so a late rising one is raised by α, and the δ = 0 reporting case, the
    same-polarity hold cases and the two ±α setup polarities are the declared exceptions."""
    from swi3s_studio.timing.delta_tpd import crossing_corners
    vp, vm, alpha = 1.05, 0.95, 0.10
    window = {"V_IH,max": 0.65, "V_IH,min": 0.45, "V_IL,max": 0.55, "V_IL,min": 0.35}
    fixed_shift = {"sigma_pm_setup": 0.0, "sigma_pm_hold_rr": 0.0, "sigma_pm_hold_ff": 0.0,
                   "sigma_pm_setup_dgnd_pos": +alpha, "sigma_pm_setup_dgnd_neg": -alpha,
                   "sigma_pm_setup_fall_dgnd_pos": +alpha,
                   "sigma_pm_setup_fall_dgnd_neg": -alpha}
    corners = crossing_corners(VDD_tol=0.05, noise_budget_V=0.12, V_nom_V=1.2)
    assert {"sigma_pm_hold_rr", "sigma_pm_hold_ff", "mp_late_rise"} <= set(corners)
    for name, c in corners.items():
        late = c.tau_sign > 0
        high_threshold = c.rising == late          # late rising, early falling
        assert c.bound == ("V_IH," if c.rising else "V_IL,") + (
            "max" if high_threshold else "min"), name
        assert (c.rx_supply, c.vdd_tx) == ((vp, vm) if high_threshold else (vm, vp)), name
        assert c.rx_threshold == pytest.approx(window[c.bound] * c.rx_supply), name
        want = fixed_shift.get(name, alpha if high_threshold else -alpha)
        assert c.ground_shift == pytest.approx(want), name


@pytest.mark.parametrize("shape", ["linear", "exp"])
@pytest.mark.parametrize("launch_rising", [True, False])
def test_sigma_pm_hold_selects_its_case_with_each_data_edges_offset(shape, launch_rising):
    """Σ_hold's same-polarity case takes its data on the launching edge's polarity and the
    other case on the other, so with unequal (rising, falling) data offsets each case's sum
    carries its own data edge's offset (`sigma_pm_hold_lanes`), and `sigma_pm_hold_ns` returns
    the sum of the case so selected. An offset on the case the raw sums select, large enough
    to reverse the order, selects the other case; the same offset on the other data edge
    leaves the selection as it is."""
    env = _env(0.10, shape, anchored=True)
    t_man, t_per = 5.0, 5.0
    same, cross = env._sigma_pm_hold_pairs(launch_rising)
    sums = {"same": same[0] * t_man + same[1] * t_per,
            "cross": cross[0] * t_man + cross[1] * t_per}
    low = min(sums, key=sums.get)
    high = "cross" if low == "same" else "same"
    d = sums[high] - sums[low] + 1.0
    pair = {"same": same, "cross": cross}
    # The data edge of `low`: the launching edge's polarity for the same-polarity case.
    low_rising = launch_rising if low == "same" else not launch_rising
    on_low = (d, 0.0) if low_rising else (0.0, d)
    on_high = (0.0, d) if low_rising else (d, 0.0)
    for offsets, want in ((None, low), (on_low, high), (on_high, low)):
        got = env.sigma_pm_hold_lanes(t_man, t_per, launch_rising=launch_rising,
                                      data_offset_ns=offsets)
        assert got == pair[want], (offsets, want, got)
        assert env.sigma_pm_hold_ns(t_man, t_per, launch_rising=launch_rising,
                                    data_offset_ns=offsets) == pytest.approx(sums[want])
