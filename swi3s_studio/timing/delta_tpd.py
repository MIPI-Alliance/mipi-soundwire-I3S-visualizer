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

- ``Δ_cross,MP`` = MP differential.  Dimensionless coefficient × t_RF_Man.
  Both clock and data are driven by the Manager in MP, so a single t_RF
  applies; the binding metric is the DIFFERENCE between data and clock
  threshold-crossing times at the Peripheral RX.

- ``Σ_cross,PM,setup`` = PM setup SUM.  The clock travels Mgr→Per (forward
  leg, slew rate t_RF_Man) and the data travels Per→Mgr (backward leg,
  slew rate t_RF_Per).  The penalty is the SUM of the two per-leg
  threshold-crossing offsets — a true sum, not a difference, hence Σ.
  The dataclass exposes a per-leg dimensionless coefficient
  ``sigma_pm_setup``; the caller forms the total
      Σ_cross,PM,setup [ns] = sigma_pm_setup · (t_RF_Man + t_RF_Per).

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

# Exponential-ramp normalization. t_RF is defined as the 20%-80% V_DD slew
# time (matches the SWI3S V_OL/V_OH endpoints, Table 123). Under
# V(t)=V_DD(1-e^(-t/τ)) rising, t_80 - t_20 = τ·ln(0.8/0.2) = τ·ln(4),
# so τ = t_RF/ln(4) and any threshold-crossing time in units of t_RF
# carries a factor 1/ln(4).
_LN4 = math.log(4.0)


def _t_cross_frac_rising(V_th: float, V_DD: float, shape: RampShape) -> float:
    """Time from V=0 to V=V_th on a rising edge, in units of t_RF.

    V_th and V_DD are in the same (arbitrary) voltage units; only the
    ratio V_th/V_DD enters. swing_TX = 0.60·V_DD for the linear case.
    """
    if shape == "linear":
        return V_th / (0.60 * V_DD)
    # exponential
    x = V_th / V_DD
    if x >= 1.0:
        return math.inf
    return -math.log(1.0 - x) / _LN4


def _t_cross_frac_falling(V_th: float, V_DD: float, shape: RampShape) -> float:
    """Time from V=V_DD to V=V_th on a falling edge, in units of t_RF."""
    if shape == "linear":
        return (V_DD - V_th) / (0.60 * V_DD)
    # exponential
    x = V_th / V_DD
    if x <= 0.0:
        return math.inf
    return -math.log(x) / _LN4


def per_tdd_offset_frac(shape: RampShape) -> float:
    """V=0 → V_OL slew portion baked into the spec Per_t_DD measurement
    (Fig 174), as a fraction of t_RF. Linear: V_OL/swing = 0.20/0.60 = 1/3.
    Exp: time from V=0 to V_OL=0.20·V_DD = -ln(0.8)/ln(4) ≈ 0.1610."""
    if shape == "linear":
        return 1.0 / 3.0
    return -math.log(0.80) / _LN4



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
    # Multiply by t_RF in ns to get the per-leg crossing time.
    mp_clk_slow: float        # MP +α corner: rising clock slow at V_IH,eff,+α
    mp_data_fast: float       # MP +α corner: falling data fast at V_IL,eff,+α

    # MP differential at the worst correlated corner (= Δ_cross,MP).
    # Total ns penalty = correlated_diff · t_RF_Man (Mgr drives both lanes in MP).
    correlated_diff: float
    # Uncorrelated MP bound (paranoia bound; independent corners per lane).
    uncorrelated_diff: float

    # PM SETUP per-leg coefficient (Σ_cross,PM,setup).  Symmetric in
    # single-shape mode; sigma_pm_setup_clk == sigma_pm_setup_data ==
    # sigma_pm_setup.  Splits when ramp shape differs between Mgr and Per.
    # Total ns penalty = sigma_pm_setup_clk · t_RF_Man + sigma_pm_setup_data · t_RF_Per.
    sigma_pm_setup: float
    sigma_pm_setup_clk: float
    sigma_pm_setup_data: float

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

    def delta_cross_MP_ns(
        self, tRF_Man_CLK_ns: float, tRF_Man_DATA_ns: float
    ) -> float:
        """Δ_cross,MP in ns with per-lane t_RF on the Mgr side.
        Clock and data lanes are both Mgr-driven in MP, but may have
        independent t_RF settings (Table 127 RFT only constrains the
        Peripheral; the Manager has no normative per-output slew control,
        so per-driver mismatch is allowed)."""
        return self.mp_clk_slow * tRF_Man_CLK_ns - self.mp_data_fast * tRF_Man_DATA_ns

    def sigma_pm_setup_ns(self, tRF_Man_ns: float, tRF_Per_ns: float) -> float:
        """Σ_cross,PM,setup in ns: clock leg (Mgr) + data leg (Per)."""
        return self.sigma_pm_setup_clk * tRF_Man_ns + self.sigma_pm_setup_data * tRF_Per_ns

    def sigma_pm_hold_ns(self, tRF_Man_ns: float, tRF_Per_ns: float) -> float:
        """Σ_cross,PM,hold in ns: min over the rr,hold case (symmetric) and
        the rf,hold case (asymmetric ε-mismatch corner with non-cancelling ΔGND).
        Smaller sum = data invalidates sooner at Mgr = tighter hold margin."""
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

    # --- rising-edge V_IH crossings ---
    V_IH_eff_late = V_IH_max_frac * vdd_plus + alpha
    V_DD_TX_late = vdd_minus
    # rising_max anchored at V=0 of slew, minus 0.5·t_RF ideal-mid offset.
    # (The ideal-mid offset cancels in any differential.)
    rising_max = _t_cross_frac_rising(V_IH_eff_late, V_DD_TX_late, s) - 0.5

    V_IH_eff_early = V_IH_min_frac * vdd_minus - alpha
    V_DD_TX_early = vdd_plus
    rising_min = _t_cross_frac_rising(V_IH_eff_early, V_DD_TX_early, s) - 0.5

    # --- falling-edge V_IL crossings ---
    V_IL_eff_late_fall = V_IL_min_frac * vdd_minus - alpha
    V_DD_TX_late_fall = vdd_plus
    falling_max = _t_cross_frac_falling(V_IL_eff_late_fall, V_DD_TX_late_fall, s) - 0.5

    V_IL_eff_early_fall = V_IL_max_frac * vdd_plus + alpha
    V_DD_TX_early_fall = vdd_minus
    falling_min = _t_cross_frac_falling(V_IL_eff_early_fall, V_DD_TX_early_fall, s) - 0.5

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
    mp_clk_slow = _t_cross_frac_rising(
        V_IH_eff_correlated, V_DD_TX_corr, s) * (1.0 + tRF_tolerance)
    mp_data_fast = _t_cross_frac_falling(
        V_IL_eff_correlated, V_DD_TX_corr, s) * (1.0 - tRF_tolerance)
    correlated_diff = mp_clk_slow - mp_data_fast

    uncorrelated_diff = rising_max - falling_min

    # --- PM SETUP per-leg coefficient ---
    # Forward (clock at Per RX): effective threshold = V_IH + ΔGND
    # Backward (data at Mgr RX): effective threshold = V_IH − ΔGND (sign
    # flip because ΔGND is V_RX_gnd − V_TX_gnd and TX/RX swap on return).
    # ΔGND drops out of the two-leg sum, so Σ_setup depends only on the
    # V_IH-corner and TX-swing geometry.  V=0 anchor: both legs rising at
    # slow t_RF (worst-case late) with (1+τ) per-edge scaling.
    pm_setup_VIH_eff = V_IH_max_frac * vdd_plus
    pm_setup_V_DD = vdd_minus
    sigma_pm_setup = _t_cross_frac_rising(
        pm_setup_VIH_eff, pm_setup_V_DD, s) * (1.0 + tRF_tolerance)

    # --- PM hold rr (NEW HIGH after OLD LOW) ---
    # Both legs rising at V_IH; symmetric ε MIN corner.  Both legs at fast
    # edge (1−τ) for the EARLY hold worst case.
    pm_hold_rr_VIH_eff = V_IH_min_frac * vdd_minus
    pm_hold_rr_V_DD = vdd_plus
    sigma_pm_hold_rr = _t_cross_frac_rising(
        pm_hold_rr_VIH_eff, pm_hold_rr_V_DD, s) * (1.0 - tRF_tolerance)

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
        pm_hold_rf_VIH_eff_fwd, pm_hold_rf_V_DD_fwd, s) * (1.0 - tRF_tolerance)
    sigma_pm_hold_rf_data = _t_cross_frac_falling(
        pm_hold_rf_VIL_eff_bwd, pm_hold_rf_V_DD_bwd, s) * (1.0 - tRF_tolerance)

    return DeltaTpdEnvelope(
        rising_max=rising_max,
        rising_min=rising_min,
        falling_max=falling_max,
        falling_min=falling_min,
        mp_clk_slow=mp_clk_slow,
        mp_data_fast=mp_data_fast,
        correlated_diff=correlated_diff,
        uncorrelated_diff=uncorrelated_diff,
        sigma_pm_setup=sigma_pm_setup,
        sigma_pm_setup_clk=sigma_pm_setup,
        sigma_pm_setup_data=sigma_pm_setup,
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

    MP coefficients (mp_clk_slow, mp_data_fast, correlated_diff,
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
        mp_clk_slow=env_man.mp_clk_slow,
        mp_data_fast=env_man.mp_data_fast,
        correlated_diff=env_man.correlated_diff,
        uncorrelated_diff=env_man.uncorrelated_diff,
        sigma_pm_setup=env_man.sigma_pm_setup,
        sigma_pm_setup_clk=env_man.sigma_pm_setup,
        sigma_pm_setup_data=env_per.sigma_pm_setup,
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
        print(f"mp_clk_slow         = {env.mp_clk_slow:+.4f}")
        print(f"mp_data_fast        = {env.mp_data_fast:+.4f}")
        print(f"Δ_cross,MP corr     = {env.correlated_diff:+.4f}  (× t_RF_Man → ns)")
        print(f"Δ_cross,MP uncorr   = {env.uncorrelated_diff:+.4f}")
        print(f"σ_PM,setup per-leg  = {env.sigma_pm_setup:+.4f}")
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
