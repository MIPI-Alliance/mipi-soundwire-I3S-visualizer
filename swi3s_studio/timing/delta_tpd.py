"""Lane-agnostic threshold-crossing envelope for single-ended DDR buses.

Computes the worst-case deviation of a receiver's threshold-crossing time
from the ideal mid-swing crossing, accounting for:

- Receiver V_IH / V_IL tolerance (e.g., [0.45, 0.65]·V_nom)
- VDD tolerance (e.g., ±5 %)
- LF ground-shift (VGND, IR drop on the shared ground return between
  TX and RX driven by use-case currents — audio amps, chargers, etc.)

The result is **lane-agnostic**: for any electrically identical lane on the
same TX/RX chip pair, rising-edge V_IH crossings and falling-edge V_IL
crossings both obey the same envelope.  CLK and DATA lanes are not
distinguished physically — only in how their crossings combine into the
setup and hold inequalities (which is handled by the caller).

Notation:

- ``Δ_cross,MP`` = MP differential.  Both lanes are Manager-driven, so the
  binding metric is the DIFFERENCE between the two threshold-crossing times
  at the Peripheral RX: a LATE crossing minus an EARLY one.

  Which lane carries which is NOT fixed, and this is the part that misleads.
  A data edge's direction follows the bit pattern, so either lane may rise or
  fall on any given UI; the coefficients are therefore named for EDGE
  POLARITY (``mp_late_rise``, ``mp_early_fall``), not for a lane.  The
  rising-late/falling-early pairing dominates the other three enumerated
  pairings for ANY positive t_RF pair, so the polarity choice needs no
  runtime search — but the LANE the late edge sits on flips with direction:

    setup — the DATA must arrive late and the CLOCK sample early, so the
            late crossing scales with t_RF_Man_DATA.
    hold  — the CLOCK must sample late and the DATA transition early, so the
            late crossing scales with t_RF_Man_CLK.

  At equal per-lane t_RF the two are identical, which is why one coefficient
  served both for so long.  Unequal — legal, since Table 127's RFT constrains
  only the Peripheral and the Manager has no normative per-output slew
  control — they diverge by up to ~9.5 ns, so ``delta_cross_MP_ns`` takes the
  direction explicitly rather than assuming a lane.

- ``Σ_cross,PM,setup`` = PM setup SUM.  The clock travels Mgr→Per (forward
  leg, slew rate t_RF_Man) and the data travels Per→Mgr (backward leg,
  slew rate t_RF_Per).  The penalty is the SUM of the two per-leg
  threshold-crossing offsets — a true sum, not a difference, hence Σ.

  The ground shift is ONE physical quantity δ = V_Per_gnd − V_Man_gnd with
  |δ| ≤ α and either sign.  TX and RX roles swap on the return leg, so the
  forward leg sees V_IH,eff + δ and the backward leg sees V_IH,eff − δ.
  That antisymmetry is forced by the topology, not chosen — which is why δ
  cancels here while α accumulates in the MP differential below.

  The cancellation is EXACT only when the ramp is linear *and* the two legs
  carry equal t_RF.  A linear ramp makes t_cross affine in the threshold, so
  ±δ cancels — but only against an equal weight; unequal t_RF weights the
  legs differently and leaves a residue linear in δ.  An exponential ramp
  makes t_cross convex, so by Jensen the ±δ pair sums to strictly MORE than
  the δ=0 value even at equal t_RF.  Σ_setup is a penalty, so the binding δ
  maximises the sum; convexity puts that maximum at an endpoint δ = ±α, so
  ``sigma_pm_setup_ns`` takes the max over the two polarities and no interior
  search is needed.  Assuming cancellation instead understates the penalty by
  up to ~0.6 ns (exp ramp, α = 0.10, t_RF = 5 ns).

  ``sigma_pm_setup`` remains the δ=0 per-leg coefficient for reporting; it is
  NOT sufficient to form the total — call ``sigma_pm_setup_ns``.

- ``Σ_cross,PM,hold`` = PM hold SUM.  Two cases; the binding one is
  MIN over them.  rr (NEW HIGH after OLD LOW) is symmetric in
  per-leg coefficient.  rf (NEW LOW after OLD HIGH, Schmitt latch
  release at V_IL) is asymmetric: separate per-leg coefficients for the
  clock and data sides.

Two correlation modes for the MP differential:

- ``correlated`` (default): CLK and DATA on the same die see the same
  V_IH, same VDD, same VGND at the same instant.  Rising-clock V_IH
  crossings and falling-DATA V_IL crossings (Schmitt-trigger latch
  release, since the receiver hysteresis is ≥10 % V_SEOS) pair under
  the same effective thresholds, and the net differential at the
  worst corner equals
  (V_IH_eff + V_IL_eff − V_OH − V_OL) / swing_TX · t_RF.
  The VGND contribution cancels in the rising-vs-falling difference.

- ``uncorrelated``: CLK and DATA take their individual worst corners
  independently.  More pessimistic upper bound.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

RampShape = Literal["linear", "exp"]
MPDirection = Literal["setup", "hold"]

# Exponential-ramp normalization. t_RF is defined as the 20%-80% V_DD slew
# time (matches the SWI3S V_OL/V_OH endpoints, Table 123). Under
# V(t)=V_DD(1-e^(-t/τ)) rising, t_80 - t_20 = τ·ln(0.8/0.2) = τ·ln(4),
# so τ = t_RF/ln(4) and any threshold-crossing time in units of t_RF
# carries a factor 1/ln(4).
_LN4 = math.log(4.0)


def _t_cross_frac_rising(V_th: float, V_DD: float, shape: RampShape,
                         *, swing_frac: float) -> float:
    """Time from V=0 to V=V_th on a rising edge, in units of t_RF.

    V_th and V_DD are in the same (arbitrary) voltage units; only the
    ratio V_th/V_DD enters.  ``swing_frac`` is the TX swing as a
    fraction of V_DD — (V_OH − V_OL)/V_DD — so swing_TX = swing_frac·V_DD
    for the linear case.  The exponential case does not use it: there the
    ramp is defined by its time constant, not by a straight-line slope
    between the two output levels.

    REQUIRED AND KEYWORD-ONLY, DELIBERATELY. This was the literal 0.60, which is
    (0.80 − 0.20) — the default V_OH/V_OL pair and nothing more. Callers of the
    envelope could pass V_OH_frac/V_OL_frac and see no effect at all: the swing
    legs in calculator.py moved while every crossing term here silently did not.
    A default here would let the next call site reintroduce exactly that.
    """
    if shape == "linear":
        return V_th / (swing_frac * V_DD)
    # exponential
    x = V_th / V_DD
    if x >= 1.0:
        return math.inf
    return -math.log(1.0 - x) / _LN4


def _t_cross_frac_falling(V_th: float, V_DD: float, shape: RampShape,
                          *, swing_frac: float) -> float:
    """Time from V=V_DD to V=V_th on a falling edge, in units of t_RF.

    ``swing_frac`` is the TX swing as a fraction of V_DD; see
    :func:`_t_cross_frac_rising` for why it is required rather than defaulted.
    """
    if shape == "linear":
        return (V_DD - V_th) / (swing_frac * V_DD)
    # exponential
    x = V_th / V_DD
    if x <= 0.0:
        return math.inf
    return -math.log(x) / _LN4


def per_tdd_offset_frac(shape: RampShape, *,
                        V_OH_frac: float = 0.80, V_OL_frac: float = 0.20) -> float:
    """V=0 → V_OL slew portion baked into the spec Per_t_DD measurement
    (Fig 174), as a fraction of t_RF. Linear: V_OL/swing = 0.20/0.60 = 1/3.
    Exp: time from V=0 to V_OL=0.20·V_DD = -ln(0.8)/ln(4) ≈ 0.1610.

    FIGURE 174 is the authority here, not Table 129's wording, which describes a 0 % anchor
    and would make this fraction zero. See `output_anchor_frac` and docs/anchors.md.

    DERIVED FROM THE SAME V_OH/V_OL PAIR AS THE CROSSING ENVELOPE, not from the literals it
    used to carry (`1/3` and `-ln(0.80)`). Those are what 0.80/0.20 works out to, so today's
    numbers are unchanged to within a ULP — but they were a second place the output levels were
    hardcoded, and the crossing envelope's copy having gone stale is precisely the defect fixed
    one layer up in this module. Whoever wires those fields to a UI row should not have to
    discover this one separately.

    No caller varies the pair yet, deliberately: nothing sweeps `V_OH_min_frac` /
    `V_OL_max_frac` (neither has a CalcRow), so threading them through spec_source would be
    parameters for their own sake. When a caller does vary them, pass them here too.
    """
    if shape == "linear":
        return V_OL_frac / (V_OH_frac - V_OL_frac)          # V_OL / swing
    return -math.log(1.0 - V_OL_frac) / _LN4                # V=0 -> V_OL on an exp ramp



@dataclass(frozen=True)
class DeltaTpdEnvelope:
    """Worst-case threshold-crossing deviations as fractions of t_RF.

    All quantities are dimensionless (multiply by the appropriate t_RF
    to get ns).  Sign convention: positive = crossing happens LATER than
    ideal mid-swing; negative = earlier.
    """

    rising_max: float                        # latest V_IH crossing (vs ideal mid)
    rising_min: float                        # earliest V_IH crossing
    falling_max: float                       # latest V_IL crossing
    falling_min: float                       # earliest V_IL crossing

    # Per-leg t_cross coefficients at the binding corners (V=0/V=V_DD anchor).
    # Multiply by t_RF in ns to get the per-leg crossing time.  Named for EDGE
    # POLARITY, not for a lane: a data edge's direction follows the bit pattern,
    # so either lane can carry either polarity, and which lane holds the LATE
    # edge flips between setup and hold (see delta_cross_MP_ns).
    mp_late_rise: float       # MP +α corner: rising crossing, slow, at V_IH,eff,+α
    mp_early_fall: float      # MP +α corner: falling crossing, fast, at V_IL,eff,+α

    # MP differential at the worst correlated corner (= Δ_cross,MP).
    # Total ns penalty = correlated_diff · t_RF_Man (Mgr drives both lanes in MP).
    correlated_diff: float
    # Uncorrelated MP bound (paranoia bound; independent corners per lane).
    uncorrelated_diff: float

    # PM SETUP per-leg coefficient (Σ_cross,PM,setup) at δ=0.  Symmetric in
    # single-shape mode; sigma_pm_setup_clk == sigma_pm_setup_data ==
    # sigma_pm_setup.  Splits when ramp shape differs between Mgr and Per.
    # REPORTING ONLY — these ignore the ground shift, so they understate the
    # penalty under an exp ramp or unequal per-leg t_RF.  Form the total with
    # sigma_pm_setup_ns(), which maximises over the δ = ±α polarities.
    sigma_pm_setup: float
    sigma_pm_setup_clk: float
    sigma_pm_setup_data: float

    # PM SETUP per-leg coefficients at the two ground-shift polarities.
    # δ = V_Per_gnd − V_Man_gnd is one physical quantity: the forward (clock)
    # leg sees V_IH,eff + δ and the backward (data) leg sees V_IH,eff − δ.
    # ``_dgnd_pos`` is the leg's coefficient when its own threshold is raised
    # by α, ``_dgnd_neg`` when lowered — so a single physical polarity pairs
    # clk_dgnd_pos with data_dgnd_neg, and vice versa.
    sigma_pm_setup_clk_dgnd_pos: float
    sigma_pm_setup_clk_dgnd_neg: float
    sigma_pm_setup_data_dgnd_pos: float
    sigma_pm_setup_data_dgnd_neg: float

    # PM hold rr per-leg coefficient (NEW HIGH after OLD LOW; both
    # legs rising at V_IH; symmetric).  Splits across legs when shape
    # differs between Mgr and Per.
    sigma_pm_hold_rr: float
    sigma_pm_hold_rr_clk: float
    sigma_pm_hold_rr_data: float

    # PM hold rf per-leg coefficients (NEW LOW after OLD HIGH;
    # rising clock fwd at Mgr-high VDD, falling data bwd at Per-low VDD;
    # ε-mismatch corner; ΔGND does NOT cancel).  Asymmetric: separate
    # coefficients for the clock (fwd) and data (bwd) legs.
    # Total ns Σ_rf = sigma_pm_hold_rf_clk · t_RF_Man + sigma_pm_hold_rf_data · t_RF_Per.
    sigma_pm_hold_rf_clk: float
    sigma_pm_hold_rf_data: float

    def delta_cross_MP_lanes(
        self, *, direction: MPDirection,
    ) -> tuple[tuple[str, float], tuple[str, float]]:
        """Δ_cross,MP as ((lane, coefficient), (lane, coefficient)), dimensionless.

        The pair the caller must multiply by that lane's t_RF and SUM to reproduce
        `delta_cross_MP_ns` exactly, the LATE leg subtracted and the EARLY leg added
        -- both coefficients are returned POSITIVE so the caller carries the sign in the
        operator, which is what the differential actually says: a late crossing costs and
        an early one credits. Exists so a displayed inequality can show
        `coefficient x t_RF` per lane instead of one pre-multiplied nanosecond value
        -- the ns form hides both the per-lane t_RF and, here, the fact that WHICH
        lane carries the late crossing swaps with the direction.
        """
        if direction == "setup":
            return (("Man DATA", self.mp_late_rise),
                    ("Man CLK", self.mp_early_fall))
        return (("Man CLK", self.mp_late_rise),
                ("Man DATA", self.mp_early_fall))

    def sigma_pm_setup_lanes(
        self, tRF_Man_ns: float, tRF_Per_ns: float,
    ) -> tuple[float, float]:
        """(clock, data) coefficients of the delta-GND polarity Σ_setup SELECTS.

        Σ_setup is a max over two polarities, so the coefficients are not fixed --
        which pair is in force depends on the two t_RF values. Returning the winning
        pair keeps a displayed `coefficient x t_RF` identical to the ns value rather
        than merely close to it.
        """
        pos = (self.sigma_pm_setup_clk_dgnd_pos, self.sigma_pm_setup_data_dgnd_neg)
        neg = (self.sigma_pm_setup_clk_dgnd_neg, self.sigma_pm_setup_data_dgnd_pos)
        return pos if (pos[0] * tRF_Man_ns + pos[1] * tRF_Per_ns) >= (
            neg[0] * tRF_Man_ns + neg[1] * tRF_Per_ns) else neg

    def sigma_pm_hold_lanes(
        self, tRF_Man_ns: float, tRF_Per_ns: float,
    ) -> tuple[float, float]:
        """(clock, data) coefficients of the case Σ_hold SELECTS -- a min, so the
        rf pair in practice (see `sigma_pm_hold_ns`), but chosen rather than assumed."""
        rr = (self.sigma_pm_hold_rr_clk, self.sigma_pm_hold_rr_data)
        rf = (self.sigma_pm_hold_rf_clk, self.sigma_pm_hold_rf_data)
        return rr if (rr[0] * tRF_Man_ns + rr[1] * tRF_Per_ns) <= (
            rf[0] * tRF_Man_ns + rf[1] * tRF_Per_ns) else rf

    def delta_cross_MP_ns(
        self, tRF_Man_CLK_ns: float, tRF_Man_DATA_ns: float,
        *, direction: MPDirection,
    ) -> float:
        """Δ_cross,MP in ns with per-lane t_RF on the Mgr side.

        Both lanes are Mgr-driven in MP but may have independent t_RF (Table 127
        RFT constrains only the Peripheral; the Manager has no normative
        per-output slew control, so per-driver mismatch is allowed). The
        differential is a LATE crossing minus an EARLY one, and WHICH LANE holds
        the late crossing depends on the direction:

          setup — the data must arrive late and the clock sample early, so the
                  late (rising) crossing scales with t_RF_Man_DATA.
          hold  — the clock must sample late and the data transition early, so
                  the late (rising) crossing scales with t_RF_Man_CLK.

        Identical at equal per-lane t_RF. Unequal, they differ by up to ~9.5 ns,
        and using the hold assignment for setup understates a setup PENALTY.
        Taking a max over both instead would be wrong in the other direction --
        it would make hold pessimistic -- so the caller states which it wants.
        """
        if direction == "setup":
            return (self.mp_late_rise * tRF_Man_DATA_ns
                    - self.mp_early_fall * tRF_Man_CLK_ns)
        return (self.mp_late_rise * tRF_Man_CLK_ns
                - self.mp_early_fall * tRF_Man_DATA_ns)

    def sigma_pm_setup_ns(self, tRF_Man_ns: float, tRF_Per_ns: float) -> float:
        """Σ_cross,PM,setup in ns: clock leg (Mgr) + data leg (Per), at the
        worse of the two ground-shift polarities.

        One physical δ = V_Per_gnd − V_Man_gnd raises one leg's threshold and
        lowers the other's, so the two candidate sums are (clk+α, data−α) and
        (clk−α, data+α).  Σ_setup is a penalty and the sum is convex in δ, so
        the worst case is the larger endpoint — max, not min.  Reduces to the
        δ=0 value exactly for a linear ramp with equal per-leg t_RF, which is
        the only case where ±δ truly cancels.
        """
        pos = (self.sigma_pm_setup_clk_dgnd_pos * tRF_Man_ns
               + self.sigma_pm_setup_data_dgnd_neg * tRF_Per_ns)
        neg = (self.sigma_pm_setup_clk_dgnd_neg * tRF_Man_ns
               + self.sigma_pm_setup_data_dgnd_pos * tRF_Per_ns)
        return max(pos, neg)

    def sigma_pm_hold_ns(self, tRF_Man_ns: float, tRF_Per_ns: float) -> float:
        """Σ_cross,PM,hold in ns: min over the rr,hold case (symmetric) and
        the rf,hold case (asymmetric ε-mismatch corner with non-cancelling ΔGND).
        Smaller sum = data invalidates sooner at Mgr = tighter hold margin.

        This sum is a CREDIT (it is ADDED to the hold margin), so unlike
        Σ_setup the binding case is the MINIMUM — and reducing α makes it
        GROW.  That is the same statement as a shrinking penalty: less noise,
        more margin.  Do not read the term magnitudes as if they all moved
        together; the invariant is on the margins (∂margin/∂α ≤ 0).

        The rr case is evaluated at δ=0, which is exact only for equal per-leg
        t_RF; it is left that way because rr never binds — its per-leg
        coefficients dominate rf's on BOTH legs, so min() always selects rf
        (pinned by test_hold_rr_never_binds_so_its_dgnd_simplification_is_inert).
        """
        sigma_rr_ns = (self.sigma_pm_hold_rr_clk * tRF_Man_ns
                      + self.sigma_pm_hold_rr_data * tRF_Per_ns)
        sigma_rf_ns = (self.sigma_pm_hold_rf_clk * tRF_Man_ns
                      + self.sigma_pm_hold_rf_data * tRF_Per_ns)
        return min(sigma_rr_ns, sigma_rf_ns)


def compute_delta_tpd_envelope(
    *,
    V_OH_frac: float = 0.80,
    V_OL_frac: float = 0.20,
    V_IH_min_frac: float = 0.45,
    V_IH_max_frac: float = 0.65,
    V_IL_min_frac: float = 0.35,
    V_IL_max_frac: float = 0.55,
    VDD_tol: float = 0.05,
    noise_budget_V: float = 0.120,
    V_nom_V: float = 1.2,
    tRF_tolerance: float = 0.05,
    ramp_shape: RampShape = "linear",
) -> DeltaTpdEnvelope:
    """Worst-case envelope for a single-ended lane.

    All fractions w.r.t. nominal V_nom.

    The ``noise_budget_V`` parameter is the TOTAL spec-bounded noise
    budget (V_GND shift + V_noise systematic + bilateral V_noise),
    treated as a single correlated coherent shift acting on V_IH and
    V_IL together at the worst-case sample event.  This is the spec's
    10 % V_SEOS cap (120 mV at V_nom = 1.2 V).  Treating all of this
    as one correlated shift gives the worst-case bound — any
    decomposition into LF/HF/bilateral can only at most match this
    bound, never exceed it (because the deterministic worst-case
    enumeration always picks the same polarity at all events).

    ``tRF_tolerance`` (default 5%) bounds the per-edge slew tolerance
    relative to nominal t_RF.  In an opposite-polarity edge pair
    (rising clock + falling data, or falling clock + rising data),
    the LATE edge can be at +tol while the EARLY edge can be at -tol,
    giving a 2·tol = 10% mismatch.

    ``ramp_shape`` selects between the linear model (V/swing geometry,
    swing = 60% V_DD) and the exponential model V(t)=V_DD(1-e^(-t/τ))
    with t_RF defined 20%-80% V_DD ⇒ τ = t_RF/ln(4).  All per-leg
    t_cross coefficients shift accordingly; the inequality structure
    (MP differential, PM sums) is identical for both shapes.
    """
    alpha = noise_budget_V / V_nom_V
    vdd_plus = 1.0 + VDD_tol
    vdd_minus = 1.0 - VDD_tol

    s = ramp_shape
    # THE TX SWING IS A PARAMETER, NOT A CONSTANT: swing_TX/V_DD = V_OH_frac - V_OL_frac.
    # Every linear crossing below divides by it. It used to be a hardcoded 0.60 — which is
    # exactly the 0.80/0.20 default and nothing more — so V_OH_frac / V_OL_frac were
    # accepted, documented in this module's own formula, threaded in from CalcInputs, and
    # then ignored: calculator.py's swing legs moved with the pair while every crossing
    # term here did not. Two halves of one model disagreeing about one knob.
    swing = V_OH_frac - V_OL_frac
    if swing <= 0.0:
        raise ValueError(
            f"V_OH_frac ({V_OH_frac}) must exceed V_OL_frac ({V_OL_frac}): the TX swing "
            "(V_OH - V_OL)/V_DD cannot be zero or negative")

    # --- rising-edge V_IH crossings ---
    V_IH_eff_late = V_IH_max_frac * vdd_plus + alpha
    V_DD_TX_late = vdd_minus
    # rising_max anchored at V=0 of slew, minus 0.5·t_RF ideal-mid offset.
    # (The ideal-mid offset cancels in any differential.)
    rising_max = _t_cross_frac_rising(V_IH_eff_late, V_DD_TX_late, s, swing_frac=swing) - 0.5

    V_IH_eff_early = V_IH_min_frac * vdd_minus - alpha
    V_DD_TX_early = vdd_plus
    rising_min = _t_cross_frac_rising(V_IH_eff_early, V_DD_TX_early, s, swing_frac=swing) - 0.5

    # --- falling-edge V_IL crossings ---
    V_IL_eff_late_fall = V_IL_min_frac * vdd_minus - alpha
    V_DD_TX_late_fall = vdd_plus
    falling_max = _t_cross_frac_falling(V_IL_eff_late_fall, V_DD_TX_late_fall, s, swing_frac=swing) - 0.5

    V_IL_eff_early_fall = V_IL_max_frac * vdd_plus + alpha
    V_DD_TX_early_fall = vdd_minus
    falling_min = _t_cross_frac_falling(V_IL_eff_early_fall, V_DD_TX_early_fall, s, swing_frac=swing) - 0.5

    # --- MP differential (correlated mode) ---
    # V=0/V=V_DD anchor: t_cross^r is measured from V=0 (start of physical
    # rising slew at TX) to V_IH crossing at RX; t_cross^f is measured from
    # V=V_DD (start of physical falling slew at TX) to V_IL crossing at RX.
    # This pairs with the pure-delay t_DD model: t_DD,pure ends at the
    # start of the slew (V=0 / V=V_DD), and t_cross then carries the full
    # 20%-80% ramp time plus the bottom V_OL portion.
    V_IH_eff_correlated = V_IH_max_frac * vdd_plus + alpha
    V_IL_eff_correlated = V_IL_max_frac * vdd_plus + alpha
    V_DD_TX_corr = vdd_minus

    # Per-leg t_cross at the MP +α correlated corner (V=0/V=V_DD anchor).
    mp_late_rise = _t_cross_frac_rising(
        V_IH_eff_correlated, V_DD_TX_corr, s, swing_frac=swing) * (1.0 + tRF_tolerance)
    mp_early_fall = _t_cross_frac_falling(
        V_IL_eff_correlated, V_DD_TX_corr, s, swing_frac=swing) * (1.0 - tRF_tolerance)
    correlated_diff = mp_late_rise - mp_early_fall

    uncorrelated_diff = rising_max - falling_min

    # --- PM SETUP per-leg coefficient ---
    # Forward (clock at Per RX): effective threshold = V_IH + δ
    # Backward (data at Mgr RX): effective threshold = V_IH − δ (sign
    # flip because δ is V_RX_gnd − V_TX_gnd and TX/RX swap on return).
    # V=0 anchor: both legs rising at slow t_RF (worst-case late) with
    # (1+τ) per-edge scaling.
    #
    # δ cancels in the two-leg sum ONLY for a linear ramp at equal per-leg
    # t_RF.  Emit the δ=0 coefficient for reporting plus the ±α pair, and let
    # sigma_pm_setup_ns() take the worse polarity — that stays exact under a
    # convex (exp) ramp and under unequal t_RF, where cancellation fails.
    pm_setup_VIH_eff = V_IH_max_frac * vdd_plus
    pm_setup_V_DD = vdd_minus
    sigma_pm_setup = _t_cross_frac_rising(
        pm_setup_VIH_eff, pm_setup_V_DD, s, swing_frac=swing) * (1.0 + tRF_tolerance)
    sigma_pm_setup_dgnd_pos = _t_cross_frac_rising(
        pm_setup_VIH_eff + alpha, pm_setup_V_DD, s, swing_frac=swing) * (1.0 + tRF_tolerance)
    sigma_pm_setup_dgnd_neg = _t_cross_frac_rising(
        pm_setup_VIH_eff - alpha, pm_setup_V_DD, s, swing_frac=swing) * (1.0 + tRF_tolerance)

    # --- PM hold rr (NEW HIGH after OLD LOW) ---
    # Both legs rising at V_IH; symmetric ε MIN corner.  Both legs at fast
    # edge (1−τ) for the EARLY hold worst case.
    pm_hold_rr_VIH_eff = V_IH_min_frac * vdd_minus
    pm_hold_rr_V_DD = vdd_plus
    sigma_pm_hold_rr = _t_cross_frac_rising(
        pm_hold_rr_VIH_eff, pm_hold_rr_V_DD, s, swing_frac=swing) * (1.0 - tRF_tolerance)

    # --- PM hold rf (NEW LOW after OLD HIGH) ---
    # Rising clock fwd + falling data bwd (Schmitt latch release at V_IL).
    # ε-mismatch corner: ε_Per MIN, ε_Mgr MAX, ΔGND = −α (does NOT cancel).
    # Asymmetric per-leg coefficients: clock (fwd) at Mgr-high VDD, data
    # (bwd) at Per-low VDD.  Both legs at fast edge.
    pm_hold_rf_VIH_eff_fwd = V_IH_min_frac * vdd_minus - alpha   # Per low, ε_Per min, −α
    pm_hold_rf_V_DD_fwd = vdd_plus                                # Mgr-high VDD (fwd TX)
    pm_hold_rf_VIL_eff_bwd = V_IL_max_frac * vdd_plus + alpha    # Mgr high, ε_Mgr max, +α
    pm_hold_rf_V_DD_bwd = vdd_minus                               # Per-low VDD (bwd TX)
    sigma_pm_hold_rf_clk = _t_cross_frac_rising(
        pm_hold_rf_VIH_eff_fwd, pm_hold_rf_V_DD_fwd, s, swing_frac=swing) * (1.0 - tRF_tolerance)
    sigma_pm_hold_rf_data = _t_cross_frac_falling(
        pm_hold_rf_VIL_eff_bwd, pm_hold_rf_V_DD_bwd, s, swing_frac=swing) * (1.0 - tRF_tolerance)

    return DeltaTpdEnvelope(
        rising_max=rising_max,
        rising_min=rising_min,
        falling_max=falling_max,
        falling_min=falling_min,
        mp_late_rise=mp_late_rise,
        mp_early_fall=mp_early_fall,
        correlated_diff=correlated_diff,
        uncorrelated_diff=uncorrelated_diff,
        sigma_pm_setup=sigma_pm_setup,
        sigma_pm_setup_clk=sigma_pm_setup,
        sigma_pm_setup_data=sigma_pm_setup,
        sigma_pm_setup_clk_dgnd_pos=sigma_pm_setup_dgnd_pos,
        sigma_pm_setup_clk_dgnd_neg=sigma_pm_setup_dgnd_neg,
        sigma_pm_setup_data_dgnd_pos=sigma_pm_setup_dgnd_pos,
        sigma_pm_setup_data_dgnd_neg=sigma_pm_setup_dgnd_neg,
        sigma_pm_hold_rr=sigma_pm_hold_rr,
        sigma_pm_hold_rr_clk=sigma_pm_hold_rr,
        sigma_pm_hold_rr_data=sigma_pm_hold_rr,
        sigma_pm_hold_rf_clk=sigma_pm_hold_rf_clk,
        sigma_pm_hold_rf_data=sigma_pm_hold_rf_data,
    )


def compute_delta_tpd_envelope_per_device(
    *,
    Man_shape: RampShape,
    Per_shape: RampShape,
    V_OH_frac: float = 0.80,
    V_OL_frac: float = 0.20,
    V_IH_min_frac: float = 0.45,
    V_IH_max_frac: float = 0.65,
    V_IL_min_frac: float = 0.35,
    V_IL_max_frac: float = 0.55,
    VDD_tol: float = 0.05,
    noise_budget_V: float = 0.120,
    V_nom_V: float = 1.2,
    tRF_tolerance: float = 0.05,
) -> DeltaTpdEnvelope:
    """Per-device-shape envelope.

    MP coefficients (mp_late_rise, mp_early_fall, correlated_diff,
    uncorrelated_diff) take Man_shape: in MP both lanes are Mgr-driven,
    so the Mgr ramp shape sets both per-leg t_cross values.

    PM coefficients split per leg: clock (forward, Mgr TX → Per RX) uses
    Man_shape; data (backward, Per TX → Mgr RX) uses Per_shape.  The
    rising-rising hold (rr) and rising-falling hold (rf) corners both
    inherit this split.

    When Man_shape == Per_shape, this returns the same envelope as
    compute_delta_tpd_envelope(ramp_shape=Man_shape).
    """
    kw = dict(
        V_OH_frac=V_OH_frac, V_OL_frac=V_OL_frac,
        V_IH_min_frac=V_IH_min_frac, V_IH_max_frac=V_IH_max_frac,
        V_IL_min_frac=V_IL_min_frac, V_IL_max_frac=V_IL_max_frac,
        VDD_tol=VDD_tol, noise_budget_V=noise_budget_V,
        V_nom_V=V_nom_V, tRF_tolerance=tRF_tolerance,
    )
    env_man = compute_delta_tpd_envelope(ramp_shape=Man_shape, **kw)
    env_per = compute_delta_tpd_envelope(ramp_shape=Per_shape, **kw)
    return DeltaTpdEnvelope(
        rising_max=env_man.rising_max,
        rising_min=env_man.rising_min,
        falling_max=env_man.falling_max,
        falling_min=env_man.falling_min,
        mp_late_rise=env_man.mp_late_rise,
        mp_early_fall=env_man.mp_early_fall,
        correlated_diff=env_man.correlated_diff,
        uncorrelated_diff=env_man.uncorrelated_diff,
        sigma_pm_setup=env_man.sigma_pm_setup,
        sigma_pm_setup_clk=env_man.sigma_pm_setup,
        sigma_pm_setup_data=env_per.sigma_pm_setup,
        sigma_pm_setup_clk_dgnd_pos=env_man.sigma_pm_setup_clk_dgnd_pos,
        sigma_pm_setup_clk_dgnd_neg=env_man.sigma_pm_setup_clk_dgnd_neg,
        sigma_pm_setup_data_dgnd_pos=env_per.sigma_pm_setup_data_dgnd_pos,
        sigma_pm_setup_data_dgnd_neg=env_per.sigma_pm_setup_data_dgnd_neg,
        sigma_pm_hold_rr=env_man.sigma_pm_hold_rr,
        sigma_pm_hold_rr_clk=env_man.sigma_pm_hold_rr,
        sigma_pm_hold_rr_data=env_per.sigma_pm_hold_rr,
        sigma_pm_hold_rf_clk=env_man.sigma_pm_hold_rf_clk,
        sigma_pm_hold_rf_data=env_per.sigma_pm_hold_rf_data,
    )


if __name__ == "__main__":
    for shape in ("linear", "exp"):
        env = compute_delta_tpd_envelope(ramp_shape=shape)
        print(f"=== {shape} ramp ===")
        print(f"rising_max          = {env.rising_max:+.4f} · t_RF")
        print(f"rising_min          = {env.rising_min:+.4f} · t_RF")
        print(f"falling_max         = {env.falling_max:+.4f} · t_RF")
        print(f"falling_min         = {env.falling_min:+.4f} · t_RF")
        print(f"mp_late_rise         = {env.mp_late_rise:+.4f}")
        print(f"mp_early_fall        = {env.mp_early_fall:+.4f}")
        print(f"Δ_cross,MP corr     = {env.correlated_diff:+.4f}  (× t_RF_Man → ns)")
        print(f"Δ_cross,MP uncorr   = {env.uncorrelated_diff:+.4f}")
        print(f"σ_PM,setup per-leg  = {env.sigma_pm_setup:+.4f}  (δ=0, reporting only)")
        print(f"σ_PM,hold rr        = {env.sigma_pm_hold_rr:+.4f}")
        print(f"σ_PM,hold rf clk    = {env.sigma_pm_hold_rf_clk:+.4f}")
        print(f"σ_PM,hold rf data   = {env.sigma_pm_hold_rf_data:+.4f}")
        Σ_setup = env.sigma_pm_setup_ns(5.0, 5.0)
        Σ_hold = env.sigma_pm_hold_ns(5.0, 5.0)
        print(f"Σ_PM,setup [5 ns]   = {Σ_setup:+.4f} ns")
        print(f"Σ_PM,hold  [5 ns]   = {Σ_hold:+.4f} ns")
        print(f"per_tdd_offset      = {per_tdd_offset_frac(shape):.4f} · t_RF "
              f"= {per_tdd_offset_frac(shape)*5.0:.4f} ns at t_RF=5 ns")
        print()
