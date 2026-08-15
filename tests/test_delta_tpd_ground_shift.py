"""Ground-shift (ΔGND / α) handling in the threshold-crossing envelope.

The PM legs send a signal out and get one back, so TX and RX roles swap between
them. One physical ground shift δ = V_Per_gnd − V_Man_gnd therefore raises one
leg's effective threshold and lowers the other's. The model used to treat that
as an exact cancellation and drop α from Σ_cross,PM,setup entirely.

The cancellation is real but narrower than that: it needs t_cross to be AFFINE
in the threshold (true only for a linear ramp) AND the two legs to carry EQUAL
t_RF (so the ±δ terms are equally weighted). Break either and a residue
survives — and because Σ_setup is a PENALTY, dropping it made the PM setup
margin optimistic. Under the exponential ramp t_cross is convex, so by Jensen
the ±δ pair sums to strictly more than the δ=0 value: ~+0.57 ns at α = 0.10,
t_RF = 5 ns, landing on the already-failing PM_setup leg.

What is pinned here:

1. The narrow case still cancels exactly (linear + equal t_RF), so the default
   configuration is untouched by the fix.
2. The two broken cases now move with α, in the pessimistic direction.
3. The accessor agrees with an independent two-polarity recomputation.
4. hold-rr keeps its δ=0 simplification, and that stays inert because rr never
   binds — asserted rather than assumed, since the docstring leans on it.
5. The invariant a reader should actually rely on: ∂margin/∂α ≤ 0 for EVERY
   inequality. The individual cross terms do NOT all move together — Δ_cross,MP
   and Σ_setup are penalties that shrink as α falls, while Σ_cross,PM,hold is a
   CREDIT that grows. Reading term magnitudes as if they shared a sign is what
   makes the α response look inconsistent when it is not.

Run: PYTHONPATH=. python3 -m pytest tests/test_delta_tpd_ground_shift.py
"""
from dataclasses import replace as dc_replace

import pytest

from swi3s_studio.timing import CalcInputs, compute
from swi3s_studio.timing.delta_tpd import (
    _t_cross_frac_rising,
    compute_delta_tpd_envelope,
)

# The V_IH corner and TX swing Σ_setup is built on (compute_delta_tpd_envelope
# defaults), restated here so the oracle below is independent of the model.
_V_IH_MAX, _VDD_PLUS, _VDD_MINUS, _TRF_TOL = 0.65, 1.05, 0.95, 0.05

_ALPHAS = (0.10, 0.075, 0.05, 0.025, 0.0)


def _env(alpha, shape):
    """Envelope at a given α (expressed as the spec's noise budget at 1.2 V)."""
    return compute_delta_tpd_envelope(
        noise_budget_V=alpha * 1.2, V_nom_V=1.2, ramp_shape=shape)


def _sigma_setup_oracle(alpha, tRF_Man, tRF_Per, shape):
    """Σ_cross,PM,setup from first principles: one physical δ of magnitude α
    raises one leg's threshold and lowers the other's; take the worse polarity.

    Deliberately does NOT reuse the envelope's per-leg coefficients, so this
    stays an independent check rather than a restatement of the model.
    """
    def leg(shift):
        return _t_cross_frac_rising(
            _V_IH_MAX * _VDD_PLUS + shift, _VDD_MINUS, shape) * (1.0 + _TRF_TOL)

    return max(leg(+alpha) * tRF_Man + leg(-alpha) * tRF_Per,
               leg(-alpha) * tRF_Man + leg(+alpha) * tRF_Per)


def test_linear_ramp_at_equal_tRF_still_cancels_the_ground_shift():
    """The one case where cancellation is exact — and the default config, so the
    fix must not perturb it.

    "Exact" is analytic, not bit-exact: the two legs are now summed as separate
    ±α terms, so the result carries a few ULPs of rounding the single-coefficient
    form did not. 1e-9 ns is six orders of magnitude below anything the margins
    resolve.
    """
    values = [_env(a, "linear").sigma_pm_setup_ns(5.0, 5.0) for a in _ALPHAS]
    assert max(values) - min(values) < 1e-9, \
        f"linear + equal t_RF must be flat in α, got {values}"
    # And equal to the δ=0 per-leg coefficient scaled across both legs.
    env = _env(0.10, "linear")
    assert env.sigma_pm_setup_ns(5.0, 5.0) == pytest.approx(
        env.sigma_pm_setup * 10.0, abs=1e-9)


def test_exp_ramp_does_not_cancel_the_ground_shift():
    """Convex t_cross ⇒ the ±δ pair sums above the δ=0 value (Jensen), so
    Σ_setup must grow with α instead of sitting flat."""
    got = [_env(a, "exp").sigma_pm_setup_ns(5.0, 5.0) for a in _ALPHAS]
    assert got == sorted(got, reverse=True), \
        f"Σ_setup must fall monotonically as α falls, got {got}"
    assert got[0] - got[-1] == pytest.approx(0.5701, abs=5e-4), \
        "the α=0.10 residue on the exp ramp is what the old model dropped"
    # The residue is a PENALTY: never let the fix understate the δ=0 baseline.
    flat = _env(0.0, "exp").sigma_pm_setup_ns(5.0, 5.0)
    assert all(v >= flat - 1e-12 for v in got)


def test_unequal_leg_tRF_breaks_cancellation_even_for_a_linear_ramp():
    """Affine t_cross cancels ±δ only against an equal weight. Unequal per-leg
    t_RF leaves a residue linear in δ, which the single-coefficient form
    (σ · (t_RF_Man + t_RF_Per)) cannot express."""
    got = [_env(a, "linear").sigma_pm_setup_ns(2.0, 8.0) for a in _ALPHAS]
    assert got == sorted(got, reverse=True), \
        f"unequal t_RF must reintroduce α dependence, got {got}"
    assert got[0] > got[-1] + 1.0, \
        "a 4:1 leg asymmetry at α=0.10 should cost more than 1 ns"


@pytest.mark.parametrize("shape", ["linear", "exp"])
@pytest.mark.parametrize("tRF", [(5.0, 5.0), (5.0, 10.0), (2.0, 8.0), (20.0, 1.0)])
def test_sigma_setup_matches_an_independent_two_polarity_recomputation(shape, tRF):
    """The accessor is exact, not approximate: the worst δ is at an endpoint
    (the sum is convex in δ), so no interior search is needed."""
    for alpha in _ALPHAS:
        assert _env(alpha, shape).sigma_pm_setup_ns(*tRF) == pytest.approx(
            _sigma_setup_oracle(alpha, *tRF, shape), abs=1e-12), \
            f"{shape} α={alpha} t_RF={tRF}"


@pytest.mark.parametrize("shape", ["linear", "exp"])
def test_hold_rr_never_binds_so_its_dgnd_simplification_is_inert(shape):
    """hold-rr keeps the δ=0 form, which is exact only at equal per-leg t_RF.
    That is safe ONLY while rr never binds — Σ_hold takes min(rr, rf), and rr's
    per-leg coefficients dominate rf's on BOTH legs, so rf always wins.

    Asserted because ``sigma_pm_hold_ns``'s docstring names this test as the
    justification; if a corner change ever made rr bind, the simplification
    would silently become a live error.
    """
    env = _env(0.10, shape)
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
def test_reducing_alpha_never_worsens_any_margin(shape):
    """The invariant worth documenting: ∂margin/∂α ≤ 0 everywhere.

    Term magnitudes are the wrong thing to watch — Σ_cross,PM,hold is a credit
    and GROWS as α falls, while the two penalties shrink. Both express the same
    thing, so the check belongs on the margins.
    """
    base = CalcInputs(Man_shape=shape, Per_shape=shape)
    series = [compute(dc_replace(base, V_noise_pp_frac=a)).breakdowns
              for a in _ALPHAS]
    for name in series[0]:
        margins = [s[name].margin_ns for s in series]
        # α descends across the series, so margins must be non-decreasing.
        # Tolerance absorbs ULP-level wobble on the legs that cancel exactly.
        assert all(b >= a - 1e-9 for a, b in zip(margins, margins[1:])), \
            f"{name} margin must not worsen as α falls: {margins}"


def test_the_hold_credit_grows_as_the_penalties_shrink():
    """Pins the sign asymmetry itself, since it is the part that reads as a bug:
    less noise makes the data stay valid LONGER at the Manager, so the hold
    credit grows while Δ_cross,MP shrinks."""
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
    """Equal per-lane t_RF ⇒ setup and hold coincide. That coincidence is why a
    single coefficient served both, and why the split changes nothing by default."""
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

    Using the hold assignment for setup understates a setup PENALTY, which is
    the defect this pins.
    """
    env = _env(0.10, shape)
    tRF_CLK, tRF_DATA = 3.0, 8.0
    assert env.delta_cross_MP_ns(tRF_CLK, tRF_DATA, direction="setup") == pytest.approx(
        env.mp_late_rise * tRF_DATA - env.mp_early_fall * tRF_CLK)
    assert env.delta_cross_MP_ns(tRF_CLK, tRF_DATA, direction="hold") == pytest.approx(
        env.mp_late_rise * tRF_CLK - env.mp_early_fall * tRF_DATA)
    # Slow data against a fast clock is the case that used to be understated.
    assert (env.delta_cross_MP_ns(tRF_CLK, tRF_DATA, direction="setup")
            > env.delta_cross_MP_ns(tRF_CLK, tRF_DATA, direction="hold") + 5.0), \
        "a 3:8 lane mismatch should separate the two directions by several ns"


@pytest.mark.parametrize("shape", ["linear", "exp"])
def test_the_rising_late_falling_early_pairing_binds_for_any_tRF_pair(shape):
    """Why no runtime search over polarity is needed.

    All four polarity pairings are candidates, but rising-late/falling-early
    dominates the falling-late/rising-early alternative for EVERY positive t_RF
    pair — its late crossing is later AND its early crossing is earlier, so the
    dominance cannot be reversed by reweighting the legs. If that ever stopped
    holding, the model would need to enumerate polarity per direction too.
    """
    from swi3s_studio.timing.delta_tpd import (
        _t_cross_frac_falling,
        _t_cross_frac_rising,
    )
    vp, vm, a, tau = 1.05, 0.95, 0.10, 0.05
    late_rise = _t_cross_frac_rising(0.65 * vp + a, vm, shape) * (1 + tau)
    early_fall = _t_cross_frac_falling(0.55 * vp + a, vm, shape) * (1 - tau)
    late_fall = _t_cross_frac_falling(0.35 * vm - a, vp, shape) * (1 + tau)
    early_rise = _t_cross_frac_rising(0.45 * vm - a, vp, shape) * (1 - tau)
    assert late_rise > late_fall, "the rising late crossing must be the later one"
    assert early_fall < early_rise, "the falling early crossing must be the earlier one"
    # Both inequalities strict ⇒ dominance for every positive (A, B) weighting.
    for A, B in ((1.0, 1.0), (1.0, 20.0), (20.0, 1.0), (3.0, 8.0)):
        assert late_rise * A - early_fall * B > late_fall * A - early_rise * B
