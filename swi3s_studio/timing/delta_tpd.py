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

  Which lane carries which is not fixed.  A data edge's direction follows the
  bit pattern, so either lane may rise or fall on any given UI; the
  coefficients are therefore named for EDGE POLARITY (``mp_late_rise``,
  ``mp_early_fall``), not for a lane.  One receiver at one instant sees one ground
  shift, so a late crossing pairs with an early crossing of the OTHER polarity:
  rising-late with falling-early under thresholds raised by α, and its mirror,
  falling-late with rising-early (``mp_late_fall``, ``mp_early_rise_data``) under
  thresholds lowered by α. The sampling edge may be of either polarity, so the
  caller evaluates both pairings (``sampling_rising``) and keeps the worse. The LANE
  the late edge sits on flips with direction:

    setup — the DATA must arrive late and the CLOCK sample early, so the
            late crossing scales with t_RF_Man_DATA.
    hold  — the CLOCK must sample late and the DATA transition early, so the
            late crossing scales with t_RF_Man_CLK.

  At equal per-lane t_RF the two are identical when the data input shares the clock
  input's threshold window and supplies; with a data window of its own
  (``Per_data_window``, a SoundWire Peripheral's ``data_input_window``) they differ even
  then, since setup reads ``mp_late_rise_data`` with ``mp_early_fall`` and hold
  ``mp_late_rise`` with ``mp_early_fall_data``.  The two Manager lanes may carry
  unequal t_RF rows, each edge inside the spec's range (`spec_source.trf_range_ns`,
  applied to every lane by `calculator.edge_factor`), and the two then differ, so
  ``delta_cross_MP_ns`` takes the direction explicitly.

- ``Σ_cross,PM,setup`` = PM setup SUM.  The clock travels Mgr→Per (forward
  leg, slew rate t_RF_Man) and the data travels Per→Mgr (backward leg,
  slew rate t_RF_Per).  The penalty is the SUM of the two per-leg
  threshold-crossing offsets — a true sum, not a difference, hence Σ.

  The ground shift is ONE physical quantity δ = V_Per_gnd − V_Man_gnd with
  |δ| ≤ α and either sign.  TX and RX roles swap on the return leg, so the
  forward leg sees V_IH,eff + δ and the backward leg sees V_IH,eff − δ.
  The antisymmetry follows from the topology: δ cancels here while α
  accumulates in the MP differential.

  The cancellation is EXACT only when the ramp is linear *and* the two legs
  carry equal t_RF.  A linear ramp makes t_cross affine in the threshold, so
  ±δ cancels — but only against an equal weight; unequal t_RF weights the
  legs differently and leaves a residue linear in δ.  An exponential ramp
  makes t_cross convex, so by Jensen the ±δ pair sums to strictly MORE than
  the δ=0 value even at equal t_RF.  Σ_setup is a penalty, so the binding δ
  maximises the sum; convexity puts that maximum at an endpoint δ = ±α, so
  ``sigma_pm_setup_ns`` takes the max over the two polarities and no interior
  search is needed.

  ``sigma_pm_setup`` remains the δ=0 per-leg coefficient for reporting; it is
  NOT sufficient to form the total — call ``sigma_pm_setup_ns``.

  On a falling launching edge both legs fall and cross at V_IL,min (the ``_fall_``
  fields), under the same pair of polarities.

  The data's edge follows the bit pattern, not the clock, so Σ_setup also has two CROSS
  cases: a rising clock with falling data, and a falling clock with rising data. In a
  cross case one δ makes BOTH crossings late (δ = +α raises the rising clock's threshold
  and lowers the falling data's; δ = −α the mirror), so the ground shift does not cancel
  and each cross case is taken at that one polarity. ``sigma_pm_setup_ns`` is the max over
  the same-polarity and the cross case of a launching edge's polarity.

- ``Σ_cross,PM,hold`` = PM hold SUM.  Two cases; the binding one is
  MIN over them.  rr (NEW HIGH after OLD LOW) is symmetric in
  per-leg coefficient.  rf (NEW LOW after OLD HIGH, Schmitt latch
  release at V_IL) is asymmetric: separate per-leg coefficients for the
  clock and data sides.  On a falling launching edge the mirrored pair: ff, both
  legs falling at V_IL,max, and fr, the clock falling at V_IL,max + α and the data
  rising at V_IH,min − α.

The MP differential pairs CLK and DATA under ONE effective-threshold shift: both are
detected on one die at one instant and see the same V_IH / V_IL bound, VDD and VGND. A
rising V_IH crossing pairs with a falling V_IL crossing (Schmitt-trigger latch release; the
receiver hysteresis is ≥10 % V_SEOS), and the worst corner raises BOTH thresholds by α, which
delays the rising crossing and advances the falling one, so α adds to the differential.
``correlated_diff`` is that pair:

    [V_IH_eff/(swing·V_tx) − a]·(1 + τ)
      − [(V_tx − V_IL_eff)/(swing·V_tx) − a]·(1 − τ)

on a linear ramp, V_IH_eff and V_IL_eff being the raised thresholds, V_tx the transmitter's
supply corner and a the anchor fraction (0 from the ramp's start).

The ``rising_*`` / ``falling_*`` deviations are each crossing's own worst corner, counted from
ideal mid-swing, at the envelope's own supply corners and WITHOUT the per-edge slew tolerance;
``uncorrelated_diff`` is ``rising_max − falling_min``. They are not ``correlated_diff``'s terms
and bound nothing the calculator reads.

A threshold the ramp never reaches -- at or beyond the transmitter's rail -- has no crossing,
and the crossing functions raise ValueError for it.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

RampShape = Literal["linear", "exp"]
MPDirection = Literal["setup", "hold"]

# Exponential-ramp normalization. t_RF is defined as the 20%-80% V_DD slew
# time (the SWI3S V_OL/V_OH endpoints at their defaults, "PHY1 & PHY2 Input and Output
# Voltage Parameters" table), whatever V_OH_frac / V_OL_frac are set to. Under
# V(t)=V_DD(1-e^(-t/τ)) rising, t_80 - t_20 = τ·ln(0.8/0.2) = τ·ln(4), so τ = t_RF/ln(4)
# and any threshold-crossing time in units of t_RF carries a factor 1/ln(4).
_LN4 = math.log(4.0)

# A crossing exists only for a threshold strictly between the transmitter's two rails.
_NO_CROSSING = ("a {edge} edge never crosses a threshold of {V_th:.3f} against a transmitter "
                "rail of {V_DD:.3f} (fractions of V_SEOS): the threshold, its noise allowance "
                "and its supply corner must stay strictly between 0 and the rail")


def _t_cross_frac_rising(V_th: float, V_DD: float, shape: RampShape,
                         *, swing_frac: float) -> float:
    """Time from V=0 to V=V_th on a rising edge, in units of t_RF.

    V_th and V_DD are in the same (arbitrary) voltage units; only the
    ratio V_th/V_DD enters.  ``swing_frac`` is the TX swing as a
    fraction of V_DD — (V_OH − V_OL)/V_DD — so swing_TX = swing_frac·V_DD
    for the linear case.  The exponential case does not use it: there the
    ramp is defined by its time constant, not by a straight-line slope
    between the two output levels.

    ``swing_frac`` is required and keyword-only: every call site passes the swing
    in force.
    """
    if not 0.0 < V_th < V_DD:
        raise ValueError(_NO_CROSSING.format(V_th=V_th, V_DD=V_DD, edge="rising"))
    if shape == "linear":
        return V_th / (swing_frac * V_DD)
    return -math.log(1.0 - V_th / V_DD) / _LN4


def _t_cross_frac_falling(V_th: float, V_DD: float, shape: RampShape,
                          *, swing_frac: float) -> float:
    """Time from V=V_DD to V=V_th on a falling edge, in units of t_RF.

    ``swing_frac`` is the TX swing as a fraction of V_DD, required and
    keyword-only as in :func:`_t_cross_frac_rising`.
    """
    if not 0.0 < V_th < V_DD:
        raise ValueError(_NO_CROSSING.format(V_th=V_th, V_DD=V_DD, edge="falling"))
    if shape == "linear":
        return (V_DD - V_th) / (swing_frac * V_DD)
    return -math.log(V_th / V_DD) / _LN4


def ramp_level(shape: RampShape, rising: bool, t_over_trf: float, *, V_DD: float,
               swing_frac: float) -> float:
    """The level of a ramp `t_over_trf` t_RF after it starts, in the units of V_DD: the curve
    `_t_cross_frac_rising` / `_t_cross_frac_falling` invert. A linear ramp moves swing·V_DD per
    t_RF from 0 (rising) or V_DD (falling) and stops at the other rail; an exponential one is
    V_DD·(1 − 4^(−t/t_RF)) rising and V_DD·4^(−t/t_RF) falling."""
    t = max(t_over_trf, 0.0)
    if shape == "linear":
        moved = min(swing_frac * V_DD * t, V_DD)
        return moved if rising else V_DD - moved
    return V_DD * (1.0 - 4.0 ** -t) if rising else V_DD * 4.0 ** -t


@dataclass(frozen=True)
class CrossingCorner:
    """One crossing coefficient's corner: where the receiver's threshold sits and what the
    transmitter's ramp swings to, all as fractions of V_nom.

    `threshold` is the effective level the crossing is timed at, against the TRANSMITTER's
    ground: the receiver's own level `rx_threshold` (its V_IH or V_IL bound times its supply
    corner, `rx_supply`) plus `ground_shift`, the receiver's ground against the transmitter's.
    `vdd_tx` is the transmitter's supply corner, the rail its ramp swings to. `tau_sign` is +1
    on a late edge and −1 on an early one; `bound` names the spec level, e.g. "V_IH,max".
    """
    rising: bool
    bound: str
    rx_threshold: float
    rx_supply: float
    ground_shift: float
    vdd_tx: float
    tau_sign: int

    @property
    def threshold(self) -> float:
        return self.rx_threshold + self.ground_shift


def crossing_time_frac(c: CrossingCorner, shape: RampShape, *, swing_frac: float) -> float:
    """When a ramp at corner `c` crosses its threshold, in units of THAT EDGE's t_RF, from the
    start of the ramp."""
    cross = _t_cross_frac_rising if c.rising else _t_cross_frac_falling
    return cross(c.threshold, c.vdd_tx, shape, swing_frac=swing_frac)


def _corner_coefficient(c: CrossingCorner, shape: RampShape, *, swing_frac: float,
                        tRF_tolerance: float, anchor_frac: float = 0.0) -> float:
    """The crossing coefficient a corner gives: the crossing time in units of t_RF on an edge
    (1 + tau_sign·τ) times the row's t_RF, counted from `anchor_frac` of the way into the
    ramp (0, its start; `per_tdd_offset_frac`, its 20 % point)."""
    return ((crossing_time_frac(c, shape, swing_frac=swing_frac) - anchor_frac)
            * (1.0 + c.tau_sign * tRF_tolerance))


def crossing_corners(*, V_IH_min_frac: float = 0.45, V_IH_max_frac: float = 0.65,
                     V_IL_min_frac: float = 0.35, V_IL_max_frac: float = 0.55,
                     VDD_tol: float = 0.05, noise_budget_V: float = 0.120,
                     V_nom_V: float = 1.2, tx_supply: float | None = None,
                     rx_supply: float | None = None) -> dict[str, CrossingCorner]:
    """The corner of each crossing coefficient `compute_delta_tpd_envelope` computes, by the
    coefficient's field name (the ±α pair of Σ_setup as `sigma_pm_setup_dgnd_pos` / `_neg`).
    The envelope builds those coefficients from these corners.

    Each corner puts the transmitter's and the receiver's supply at the end that makes that
    crossing late or early (the low-supply transmitter, for the corners below stated so). With
    `tx_supply` / `rx_supply` every corner takes those instead:
    one supply per device, which a caller evaluating a whole leg passes."""
    alpha = noise_budget_V / V_nom_V
    vp, vm = 1.0 + VDD_tol, 1.0 - VDD_tol
    corners = {
        # MP +α correlated corner: a slow-edge rising crossing at V_IH,max raised by α, and a
        # fast-edge falling one at V_IL,max raised by α, from a low-supply transmitter.
        "mp_late_rise": CrossingCorner(True, "V_IH,max", V_IH_max_frac * vp, vp, +alpha, vm, +1),
        "mp_early_fall": CrossingCorner(False, "V_IL,max", V_IL_max_frac * vp, vp, +alpha, vm, -1),
        # Its mirror, both thresholds lowered by α: a slow-edge falling crossing at V_IL,min, from
        # a high-supply transmitter. Its early rising partner is `sigma_pm_hold_rf_clk`'s corner.
        "mp_late_fall": CrossingCorner(False, "V_IL,min", V_IL_min_frac * vm, vm, -alpha, vp, +1),
        # Σ_cross,PM,setup: both legs rising at V_IH,max on slow edges; the ground shift
        # raises one leg's threshold and lowers the other's.
        "sigma_pm_setup": CrossingCorner(True, "V_IH,max", V_IH_max_frac * vp, vp, 0.0, vm, +1),
        "sigma_pm_setup_dgnd_pos": CrossingCorner(True, "V_IH,max", V_IH_max_frac * vp, vp,
                                                  +alpha, vm, +1),
        "sigma_pm_setup_dgnd_neg": CrossingCorner(True, "V_IH,max", V_IH_max_frac * vp, vp,
                                                  -alpha, vm, +1),
        # Σ_cross,PM,hold rr: both legs rising at V_IH,min, fast edges, no ground shift.
        "sigma_pm_hold_rr": CrossingCorner(True, "V_IH,min", V_IH_min_frac * vm, vm, 0.0, vp, -1),
        # Σ_cross,PM,hold rf: the clock rising at V_IH,min lowered by α from a high-supply
        # Manager, the data falling at V_IL,max raised by α from a low-supply Peripheral.
        "sigma_pm_hold_rf_clk": CrossingCorner(True, "V_IH,min", V_IH_min_frac * vm, vm, -alpha,
                                               vp, -1),
        "sigma_pm_hold_rf_data": CrossingCorner(False, "V_IL,max", V_IL_max_frac * vp, vp,
                                                +alpha, vm, -1),
        # The mirrors on a falling launching edge. Σ_cross,PM,setup: both legs falling at
        # V_IL,min on slow edges, from a high-supply transmitter. Σ_cross,PM,hold ff: both legs
        # falling at V_IL,max, fast edges, no ground shift, from a low-supply transmitter.
        "sigma_pm_setup_fall_dgnd_pos": CrossingCorner(False, "V_IL,min", V_IL_min_frac * vm,
                                                       vm, +alpha, vp, +1),
        "sigma_pm_setup_fall_dgnd_neg": CrossingCorner(False, "V_IL,min", V_IL_min_frac * vm,
                                                       vm, -alpha, vp, +1),
        "sigma_pm_hold_ff": CrossingCorner(False, "V_IL,max", V_IL_max_frac * vp, vp, 0.0, vm,
                                           -1),
    }
    if tx_supply is None and rx_supply is None:
        return corners
    out = {}
    for name, c in corners.items():
        rx = c.rx_supply if rx_supply is None else rx_supply
        tx = c.vdd_tx if tx_supply is None else tx_supply
        out[name] = CrossingCorner(c.rising, c.bound, c.rx_threshold / c.rx_supply * rx, rx,
                                   c.ground_shift, tx, c.tau_sign)
    return out


def per_tdd_offset_frac(shape: RampShape, *,
                        V_OH_frac: float = 0.80, V_OL_frac: float = 0.20,
                        rising: bool = True) -> float:
    """The ramp before an edge's 20 % point, as a fraction of its t_RF: from the start of the
    ramp to its V_OL crossing on a `rising` edge and its V_OH crossing on a falling one, from
    the V_OH / V_OL pair the crossing envelope also reads. Linear: V_OL / (V_OH − V_OL) rising,
    (1 − V_OH) / (V_OH − V_OL) falling. Exponential, t_RF being the 20-80 % time:
    −ln(1 − V_OL) / ln 4 rising, −ln(V_OH) / ln 4 falling. The two polarities agree where
    V_OH + V_OL = 1.

    Every launch's anchor (t_DD and t_ZD end there, on either side) and the Manager's clock
    reference sit at this point, so it places those edges' ramps (`calculator.anchor_frac`).
    The anchor is the 20 % point of the Output Timing figures; docs/anchors.md states it.

    Raises ValueError for a V_OH / V_OL pair `_checked_levels` refuses.
    """
    _checked_levels(shape, V_OH_frac, V_OL_frac)
    if shape == "linear":
        moved = V_OL_frac if rising else 1.0 - V_OH_frac
        return moved / (V_OH_frac - V_OL_frac)              # moved / swing
    return -math.log(1.0 - V_OL_frac if rising else V_OH_frac) / _LN4


def ramp_to_valid_frac(shape: RampShape, *,
                       V_OH_frac: float = 0.80, V_OL_frac: float = 0.20,
                       rising: bool = True) -> float:
    """The ramp from an edge's 20 % point to its 80 % point, as a fraction of its t_RF: from
    its V_OL crossing to its V_OH crossing on a `rising` edge, V_OH to V_OL on a falling one,
    on the curve `ramp_level` draws. Linear: 1, t_RF being the V_OL-V_OH time. Exponential,
    t_RF being the 20-80 % time: (ln(1 − V_OL) − ln(1 − V_OH)) / ln 4 rising,
    (ln V_OH − ln V_OL) / ln 4 falling, so 1 at V_OL / V_OH = 0.20 / 0.80, and the two
    polarities agree where V_OH + V_OL = 1.

    An EndDriveEarly release charges this times the edge's t_RF from the launch's anchor to
    the valid level, and its keeper leg credits it (`calculator.to_voh_frac`).

    Raises ValueError for a V_OH / V_OL pair `_checked_levels` refuses."""
    _checked_levels(shape, V_OH_frac, V_OL_frac)
    if shape == "linear":
        return 1.0
    if rising:
        return (math.log(1.0 - V_OL_frac) - math.log(1.0 - V_OH_frac)) / _LN4
    return (math.log(V_OH_frac) - math.log(V_OL_frac)) / _LN4


def _data_offset(offsets: tuple[float, float] | None, rising: bool) -> float:
    """The (rising, falling) offset `offsets` of a data edge of polarity `rising`; 0 where
    there are none or the two are equal, so equal offsets leave a selection unchanged."""
    if offsets is None or offsets[0] == offsets[1]:
        return 0.0
    return offsets[0] if rising else offsets[1]


# Σ_cross,PM,setup's cases by the polarity of the Peripheral's launching edge, as (clock, data)
# coefficient field names of `DeltaTpdEnvelope`: the data on the clock's polarity at δ = +α
# and at δ = −α, then the data on the other polarity at the δ that makes both crossings late.
# `_dgnd_pos` is a leg's coefficient with its own threshold raised by α, `_dgnd_neg` lowered:
# the clock leg sees +δ and the data leg −δ.
SIGMA_PM_SETUP_CASES: dict[bool, tuple[tuple[str, str], ...]] = {
    True: (("sigma_pm_setup_clk_dgnd_pos", "sigma_pm_setup_data_dgnd_neg"),
           ("sigma_pm_setup_clk_dgnd_neg", "sigma_pm_setup_data_dgnd_pos"),
           ("sigma_pm_setup_clk_dgnd_pos", "sigma_pm_setup_fall_data_dgnd_neg")),
    False: (("sigma_pm_setup_fall_clk_dgnd_pos", "sigma_pm_setup_fall_data_dgnd_neg"),
            ("sigma_pm_setup_fall_clk_dgnd_neg", "sigma_pm_setup_fall_data_dgnd_pos"),
            ("sigma_pm_setup_fall_clk_dgnd_neg", "sigma_pm_setup_data_dgnd_pos")),
}


@dataclass(frozen=True)
class DeltaTpdEnvelope:
    """Worst-case threshold-crossing deviations as fractions of t_RF.

    All quantities are dimensionless (multiply by the appropriate t_RF to get ns).
    Sign convention of the rising_* / falling_* fields: positive = crossing happens LATER
    than ideal mid-swing; negative = earlier. Those four carry no per-edge slew tolerance and
    no anchor, at the envelope's own supply corners (module docstring).
    """

    rising_max: float                        # latest V_IH crossing (vs ideal mid)
    rising_min: float                        # earliest V_IH crossing
    falling_max: float                       # latest V_IL crossing
    falling_min: float                       # earliest V_IL crossing

    # Per-leg t_cross coefficients at the binding corners, from the ramp's start, or from
    # its 20 % point with `from_anchor`.
    # Multiply by t_RF in ns to get the per-leg crossing time.  Named for EDGE
    # POLARITY, not for a lane: a data edge's direction follows the bit pattern,
    # so either lane can carry either polarity, and which lane holds the LATE
    # edge flips between setup and hold (see delta_cross_MP_ns).
    mp_late_rise: float       # MP +α corner: rising crossing, slow, at V_IH,eff,+α
    mp_early_fall: float      # MP +α corner: falling crossing, fast, at V_IL,eff,+α
    # The same two crossings on a lane a PERIPHERAL drives, so they take the Peripheral's
    # ramp shape: peripheral A's data reaching peripheral B on the P->P legs. The clock lane on
    # those legs is driven by the Manager and reads mp_late_rise / mp_early_fall at A's input
    # and the `_b` fields at B's. Equal to mp_late_rise / mp_early_fall when both lanes share
    # one shape, one threshold window and the same transmitter and receiver supplies.
    mp_late_rise_per: float
    mp_early_fall_per: float
    # The same two crossings on the Manager-driven DATA lane, at the Peripheral's DATA input.
    # A receiver's data input may have its own threshold window (`DataWindow`); the clock
    # lane keeps mp_late_rise / mp_early_fall. Equal to those two when the windows match.
    mp_late_rise_data: float
    mp_early_fall_data: float

    # MP differential at the worst correlated corner (= Δ_cross,MP).
    # Total ns penalty = correlated_diff · t_RF_Man (Mgr drives both lanes in MP).
    correlated_diff: float
    # rising_max − falling_min: the latest rising and the earliest falling deviation, each at
    # its own corner, without the per-edge slew tolerance.
    uncorrelated_diff: float

    # PM SETUP per-leg coefficient (Σ_cross,PM,setup) at δ=0.  Symmetric in
    # single-shape mode; sigma_pm_setup_clk == sigma_pm_setup_data ==
    # sigma_pm_setup.  Splits when ramp shape differs between Mgr and Per.
    # REPORTING ONLY — these ignore the ground shift and take the data on the clock's
    # polarity, so they understate the penalty wherever the cross-polarity case binds, the
    # default linear ramp with equal per-leg t_RF included, and under an exp ramp or unequal
    # per-leg t_RF, where the ground shift does not cancel.  Form the total with
    # sigma_pm_setup_ns(), which maximises over its cases.
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

    # The clock detections of a SECOND peripheral, B on a P->P leg, which has a supply of its
    # own. Equal to mp_late_rise / mp_early_fall / sigma_pm_hold_rf_clk where the two
    # peripherals share a supply corner.
    mp_late_rise_b: float
    mp_early_fall_b: float
    sigma_pm_hold_rf_clk_b: float
    # The mirrored pair, both thresholds lowered by α (`crossing_corners`): the late falling
    # clock detection at the Peripheral's input and at B's, and the early rising crossing of
    # A's data at B's data input. A P->P hold leg whose sampling edge falls reads them.
    mp_late_fall: float
    mp_late_fall_b: float
    mp_early_rise_per: float
    # The same mirrored pair on the other data lanes a falling sampling edge reads: the
    # Manager's data at the Peripheral's data input (`_data`) and A's data at B's (`_per`).
    mp_late_fall_data: float
    mp_early_rise_data: float
    mp_late_fall_per: float
    # Σ_cross,PM,setup on a falling launching edge: each leg falling at V_IL,min with its own
    # threshold raised (`_dgnd_pos`) or lowered (`_dgnd_neg`) by α.
    sigma_pm_setup_fall_clk_dgnd_pos: float
    sigma_pm_setup_fall_clk_dgnd_neg: float
    sigma_pm_setup_fall_data_dgnd_pos: float
    sigma_pm_setup_fall_data_dgnd_neg: float
    # Σ_cross,PM,hold on a falling launching edge: ff, both legs falling at V_IL,max with no
    # ground shift, and fr, whose clock leg is `mp_early_fall` and whose data leg rises at
    # V_IH,min − α at the Manager's data input.
    sigma_pm_hold_ff_clk: float
    sigma_pm_hold_ff_data: float
    sigma_pm_hold_fr_data: float

    def delta_cross_MP_lanes(
        self, *, direction: MPDirection, sampling_rising: bool | None = None,
    ) -> tuple[tuple[str, float], tuple[str, float]]:
        """Δ_cross,MP as ((lane, coefficient), (lane, coefficient)), dimensionless.

        The pair the caller must multiply by that lane's t_RF and SUM to reproduce
        `delta_cross_MP_ns` exactly, the LATE leg subtracted and the EARLY leg added.
        Both coefficients are returned POSITIVE; the caller carries the sign in the
        operator (a late crossing costs, an early one credits). A displayed inequality
        uses this to show `coefficient x t_RF` per lane; which lane carries the late
        crossing swaps with the direction.

        `sampling_rising` is the polarity of the Peripheral's sampling edge; None takes a
        falling one on setup and a rising one on hold. The late crossing is rising and the
        early one falling under thresholds raised by α, and the mirror under thresholds
        lowered by α:

            setup, sampling falling   data late rising,   clock early falling
            setup, sampling rising    data late falling,  clock early rising
            hold,  sampling rising    clock late rising,  data early falling
            hold,  sampling falling   clock late falling, data early rising
        """
        rising = (direction == "hold") if sampling_rising is None else sampling_rising
        if direction == "setup" and rising:
            return (("Man DATA", self.mp_late_fall_data),
                    ("Man CLK", self.sigma_pm_hold_rf_clk))
        if direction == "setup":
            return (("Man DATA", self.mp_late_rise_data),
                    ("Man CLK", self.mp_early_fall))
        if rising:
            return (("Man CLK", self.mp_late_rise),
                    ("Man DATA", self.mp_early_fall_data))
        return (("Man CLK", self.mp_late_fall),
                ("Man DATA", self.mp_early_rise_data))

    def delta_cross_PP_lanes(
        self, *, direction: MPDirection, sampling_rising: bool | None = None,
    ) -> tuple[tuple[str, float], tuple[str, float]]:
        """Δ_cross,MP's construction at a P->P sampler, as ((lane, coefficient), ...).

        Peripheral B's detection of the clock and of A's data, differenced like
        `delta_cross_MP_lanes` and with its `sampling_rising`, with the data lane on
        `Per DATA`. Each lane takes the shape of the device driving it: the clock lane the
        Manager's (the `_b` fields, at B's input), the data lane the Peripheral's (the `_per`
        fields). Late leg first, both coefficients positive.
        """
        rising = (direction == "hold") if sampling_rising is None else sampling_rising
        if direction == "setup" and rising:
            return (("Per DATA", self.mp_late_fall_per),
                    ("Man CLK", self.sigma_pm_hold_rf_clk_b))
        if direction == "setup":
            return (("Per DATA", self.mp_late_rise_per),
                    ("Man CLK", self.mp_early_fall_b))
        if rising:
            return (("Man CLK", self.mp_late_rise_b),
                    ("Per DATA", self.mp_early_fall_per))
        return (("Man CLK", self.mp_late_fall_b),
                ("Per DATA", self.mp_early_rise_per))

    def delta_cross_PP_ns(
        self, tRF_Man_CLK_ns: float, tRF_Per_DATA_ns: float,
        *, direction: MPDirection, sampling_rising: bool | None = None,
    ) -> float:
        """`delta_cross_PP_lanes` in ns: the late leg times its lane's t_RF minus the
        early leg times its lane's."""
        trf = {"Man CLK": tRF_Man_CLK_ns, "Per DATA": tRF_Per_DATA_ns}
        (late_lane, late), (early_lane, early) = self.delta_cross_PP_lanes(
            direction=direction, sampling_rising=sampling_rising)
        return late * trf[late_lane] - early * trf[early_lane]

    def sigma_pm_setup_fields(
        self, tRF_Man_ns: float, tRF_Per_ns: float, *, launch_rising: bool = True,
        data_offset_ns: tuple[float, float] | None = None,
    ) -> tuple[str, str]:
        """The (clock, data) coefficient FIELD NAMES of the case Σ_setup selects on a launching
        edge of polarity `launch_rising`: the largest `clock·tRF_Man + data·tRF_Per` over
        `SIGMA_PM_SETUP_CASES[launch_rising]`, the first on a tie. A data field with `_fall_`
        in it is a falling data crossing. `data_offset_ns` is (rising, falling), ns added to a
        case's total by its data edge's polarity: the launch's anchor, which places the data
        edge (`calculator.sw_anchor_ns`); equal offsets do not enter."""
        def total(case: tuple[str, str]) -> float:
            return (getattr(self, case[0]) * tRF_Man_ns + getattr(self, case[1]) * tRF_Per_ns
                    + _data_offset(data_offset_ns, "_fall_" not in case[1]))
        return max(SIGMA_PM_SETUP_CASES[launch_rising], key=total)

    def sigma_pm_setup_lanes(
        self, tRF_Man_ns: float, tRF_Per_ns: float, *, launch_rising: bool = True,
        data_offset_ns: tuple[float, float] | None = None,
    ) -> tuple[float, float]:
        """(clock, data) coefficients of the case Σ_setup SELECTS (`sigma_pm_setup_fields`).

        Σ_setup is a max over its cases, so the coefficients are not fixed -- which case is
        in force depends on the two t_RF values. The returned pair makes a displayed
        `coefficient x t_RF` equal the ns value exactly. `launch_rising` is the polarity of
        the Peripheral's launching edge, which the clock crossing shares; `data_offset_ns` as
        `sigma_pm_setup_fields`.
        """
        clk, data = self.sigma_pm_setup_fields(tRF_Man_ns, tRF_Per_ns,
                                               launch_rising=launch_rising,
                                               data_offset_ns=data_offset_ns)
        return getattr(self, clk), getattr(self, data)

    def _sigma_pm_hold_pairs(
        self, launch_rising: bool,
    ) -> tuple[tuple[float, float], tuple[float, float]]:
        """Σ_hold's two cases as (clock, data) pairs: (rr, rf) on a rising launching edge and
        (ff, fr) on a falling one, the same-polarity case first. The same-polarity case's data
        edge is the launching edge's polarity, the other case's the other."""
        if launch_rising:
            return ((self.sigma_pm_hold_rr_clk, self.sigma_pm_hold_rr_data),
                    (self.sigma_pm_hold_rf_clk, self.sigma_pm_hold_rf_data))
        return ((self.sigma_pm_hold_ff_clk, self.sigma_pm_hold_ff_data),
                (self.mp_early_fall, self.sigma_pm_hold_fr_data))

    def sigma_pm_hold_lanes(
        self, tRF_Man_ns: float, tRF_Per_ns: float, *, launch_rising: bool = True,
        data_offset_ns: tuple[float, float] | None = None,
    ) -> tuple[float, float]:
        """(clock, data) coefficients of the case Σ_hold SELECTS, a min over its two cases
        on a launching edge of polarity `launch_rising` (`sigma_pm_hold_ns`), each case's sum
        with `data_offset_ns` (`sigma_pm_setup_fields`) at its data edge's polarity."""
        same, cross = self._sigma_pm_hold_pairs(launch_rising)
        return same if (same[0] * tRF_Man_ns + same[1] * tRF_Per_ns
                        + _data_offset(data_offset_ns, launch_rising)) <= (
            cross[0] * tRF_Man_ns + cross[1] * tRF_Per_ns
            + _data_offset(data_offset_ns, not launch_rising)) else cross

    def delta_cross_MP_ns(
        self, tRF_Man_CLK_ns: float, tRF_Man_DATA_ns: float,
        *, direction: MPDirection, sampling_rising: bool | None = None,
    ) -> float:
        """Δ_cross,MP in ns with per-lane t_RF on the Mgr side.

        Both lanes are Mgr-driven in MP and may carry unequal t_RF rows, each edge inside
        the spec's range (`spec_source.trf_range_ns`, applied to every lane by
        `calculator.edge_factor`). The differential is a LATE crossing minus an EARLY one,
        and WHICH LANE holds the late crossing depends on the direction:

          setup — the data must arrive late and the clock sample early, so the
                  late crossing scales with t_RF_Man_DATA.
          hold  — the clock must sample late and the data transition early, so
                  the late crossing scales with t_RF_Man_CLK.

        Identical at equal per-lane t_RF only where the data input shares the clock input's
        threshold window; with a data window of its own (``Per_data_window``), or unequal
        t_RF, they differ, so the caller states the direction. `sampling_rising` as
        `delta_cross_MP_lanes`.
        """
        trf = {"Man CLK": tRF_Man_CLK_ns, "Man DATA": tRF_Man_DATA_ns}
        (late_lane, late), (early_lane, early) = self.delta_cross_MP_lanes(
            direction=direction, sampling_rising=sampling_rising)
        return late * trf[late_lane] - early * trf[early_lane]

    def sigma_pm_setup_ns(self, tRF_Man_ns: float, tRF_Per_ns: float, *,
                          launch_rising: bool = True,
                          data_offset_ns: tuple[float, float] | None = None) -> float:
        """Σ_cross,PM,setup in ns: clock leg (Mgr) + data leg (Per), at the worst of its
        cases on a launching edge of polarity `launch_rising` (`SIGMA_PM_SETUP_CASES`).

        DATA ON THE CLOCK'S POLARITY: one physical δ = V_Per_gnd − V_Man_gnd raises one
        leg's threshold and lowers the other's, so the two candidate sums are (clk+α,
        data−α) and (clk−α, data+α). Σ_setup is a penalty and the sum is convex in δ, so
        the worst case is the larger endpoint — max, not min. These two cases reduce to the
        δ=0 value exactly for a linear ramp with equal per-leg t_RF, the only case where ±δ
        cancels.

        DATA ON THE OTHER POLARITY: the δ that delays the clock crossing also delays the
        data crossing, so the case is the one sum at that δ, both legs late. It binds at the
        default linear ramp with equal per-leg t_RF, above the δ=0 value.

        The case is selected with `data_offset_ns` (`sigma_pm_setup_fields`), which the value
        returned does not include.
        """
        clk, data = self.sigma_pm_setup_lanes(tRF_Man_ns, tRF_Per_ns,
                                              launch_rising=launch_rising,
                                              data_offset_ns=data_offset_ns)
        return clk * tRF_Man_ns + data * tRF_Per_ns

    def sigma_pm_hold_ns(self, tRF_Man_ns: float, tRF_Per_ns: float, *,
                         launch_rising: bool = True,
                         data_offset_ns: tuple[float, float] | None = None) -> float:
        """Σ_cross,PM,hold in ns: min over the rr,hold case (symmetric) and
        the rf,hold case (asymmetric ε-mismatch corner with non-cancelling ΔGND), or on a
        falling launching edge (`launch_rising` False) over their mirrors ff and fr.
        Smaller sum = data invalidates sooner at Mgr = tighter hold margin.

        This sum is a CREDIT (it is ADDED to the hold margin), so unlike
        Σ_setup the binding case is the MINIMUM — and reducing α makes it
        GROW.  That is the same statement as a shrinking penalty: less noise,
        more margin.  Do not read the term magnitudes as if they all moved
        together; the invariant is on the margins (∂margin/∂α ≤ 0).

        The rr case is evaluated at δ=0, which is exact at α = 0, where there is no
        ground shift. In the worst-corner envelope (no supplies) rr's per-leg
        coefficients dominate rf's on both legs, so min() selects rf
        (test_hold_rr_never_binds_in_the_worst_corner_envelope). With one
        supply per device rr can be the smaller sum at some assignments, and the
        assignment `calculator.compute` reports for PM_hold / PM_hold_ho takes rr
        only at α = 0 (test_the_reported_pm_hold_takes_the_rr_case_only_without_noise).
        ff mirrors rr and fr mirrors rf.

        The case is the one `sigma_pm_hold_lanes` selects with `data_offset_ns`, which the
        value returned does not include, so it is the sum the calculator shows.
        """
        clk, data = self.sigma_pm_hold_lanes(tRF_Man_ns, tRF_Per_ns,
                                             launch_rising=launch_rising,
                                             data_offset_ns=data_offset_ns)
        return clk * tRF_Man_ns + data * tRF_Per_ns


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
    from_anchor: bool = False,
    tx_supply: float | None = None,
    rx_supply: float | None = None,
) -> DeltaTpdEnvelope:
    """Worst-case envelope for a single-ended lane.

    The per-leg coefficients count from the start of the ramp, or with ``from_anchor`` from
    its 20 % point, V_OL rising and V_OH falling (`per_tdd_offset_frac`), where the timing
    the calculator reads is referenced, each field the coefficient of its `_FIELD_CORNERS`
    corner. The `rising_*` / `falling_*` deviations always count from ideal mid-swing;
    `from_anchor` does not affect them. `tx_supply` / `rx_supply` put the per-leg
    coefficients at one supply per device (`crossing_corners`).

    All fractions w.r.t. nominal V_nom.

    The ``noise_budget_V`` parameter is the TOTAL spec-bounded noise
    budget (V_GND shift + V_noise systematic + bilateral V_noise),
    treated as a single correlated coherent shift acting on V_IH and
    V_IL together at the worst-case sample event.  This is the spec's
    10 % V_SEOS cap (120 mV at V_nom = 1.2 V).  As one correlated
    shift it is the worst-case bound: no decomposition into
    LF/HF/bilateral exceeds it.

    ``tRF_tolerance`` (default 5%) bounds the per-edge slew tolerance
    relative to nominal t_RF.  In an opposite-polarity edge pair
    (rising clock + falling data, or falling clock + rising data),
    the LATE edge can be at +tol while the EARLY edge can be at -tol,
    giving a 2·tol = 10% mismatch.

    ``ramp_shape`` selects between the linear model (V/swing geometry,
    swing = V_OH_frac − V_OL_frac, 0.6 V_DD at the default 0.80 / 0.20) and the
    exponential model V(t)=V_DD(1-e^(-t/τ)) with t_RF the 20%-80% V_DD time
    whatever V_OH_frac / V_OL_frac are ⇒ τ = t_RF/ln(4).  All per-leg
    t_cross coefficients shift accordingly; the inequality structure
    (MP differential, PM sums) is identical for both shapes.
    """
    swing = _checked_swing(V_OH_frac, V_OL_frac)
    corner_kw = dict(V_IH_min_frac=V_IH_min_frac, V_IH_max_frac=V_IH_max_frac,
                     V_IL_min_frac=V_IL_min_frac, V_IL_max_frac=V_IL_max_frac, VDD_tol=VDD_tol,
                     noise_budget_V=noise_budget_V, V_nom_V=V_nom_V)
    rising_max, rising_min, falling_max, falling_min = _deviations(
        crossing_corners(**corner_kw), ramp_shape, swing_frac=swing)
    k = _coefficients(crossing_corners(**corner_kw, tx_supply=tx_supply, rx_supply=rx_supply),
                      ramp_shape, swing_frac=swing, tRF_tolerance=tRF_tolerance,
                      V_OH_frac=V_OH_frac, V_OL_frac=V_OL_frac, from_anchor=from_anchor)
    fields = {f: k[corner] for f, (_, corner) in _FIELD_CORNERS.items()}
    return DeltaTpdEnvelope(
        rising_max=rising_max, rising_min=rising_min,
        falling_max=falling_max, falling_min=falling_min,
        correlated_diff=fields["mp_late_rise_data"] - fields["mp_early_fall"],
        uncorrelated_diff=rising_max - falling_min,
        **fields)


def _checked_levels(shape: RampShape, V_OH_frac: float, V_OL_frac: float) -> None:
    """Raise ValueError, naming the fraction, for a V_OH / V_OL pair a ramp of `shape` has no
    20 % or 80 % point at: any pair `_checked_swing` refuses, and on an exponential ramp, which
    reaches neither rail, a V_OL_frac at or below 0 or a V_OH_frac at or above 1."""
    _checked_swing(V_OH_frac, V_OL_frac)
    if shape == "linear":
        return
    if V_OL_frac <= 0.0:
        raise ValueError(f"V_OL_frac ({V_OL_frac}) must be above 0: an exponential ramp "
                         "never reaches its low rail")
    if V_OH_frac >= 1.0:
        raise ValueError(f"V_OH_frac ({V_OH_frac}) must be below 1: an exponential ramp "
                         "never reaches its high rail")


def _checked_swing(V_OH_frac: float, V_OL_frac: float) -> float:
    """The TX swing as a fraction of V_DD, V_OH − V_OL: a parameter, not a constant. Every
    linear crossing divides by it, so a zero or negative one raises ValueError."""
    swing = V_OH_frac - V_OL_frac
    if swing <= 0.0:
        raise ValueError(
            f"V_OH_frac ({V_OH_frac}) must exceed V_OL_frac ({V_OL_frac}): the TX swing "
            "(V_OH - V_OL)/V_DD cannot be zero or negative")
    return swing


def _deviations(corners: dict[str, CrossingCorner], shape: RampShape, *,
                swing_frac: float) -> tuple[float, float, float, float]:
    """(rising_max, rising_min, falling_max, falling_min): the latest and earliest rising and
    falling crossings of `corners` (the envelope's own supply corners), less the ideal
    mid-swing crossing, the nominal ramp's 50 % point, 0.5 / swing on a linear ramp and 0.5 on
    an exponential one, in units of t_RF and with no per-edge slew tolerance."""
    mid = 0.5 / swing_frac if shape == "linear" else 0.5

    def dev(name: str) -> float:
        return crossing_time_frac(corners[name], shape, swing_frac=swing_frac) - mid

    return (dev("mp_late_rise"), dev("sigma_pm_hold_rf_clk"), dev("mp_late_fall"),
            dev("mp_early_fall"))


def _coefficients(corners: dict[str, CrossingCorner], shape: RampShape, *, swing_frac: float,
                  tRF_tolerance: float, V_OH_frac: float, V_OL_frac: float,
                  from_anchor: bool) -> dict[str, float]:
    """Each corner's coefficient (`_corner_coefficient`), by corner name: counted from the start
    of its ramp, or with `from_anchor` from the 20 % point of its own polarity
    (`per_tdd_offset_frac`)."""
    def anchor(rising: bool) -> float:
        if not from_anchor:
            return 0.0
        return per_tdd_offset_frac(shape, V_OH_frac=V_OH_frac, V_OL_frac=V_OL_frac,
                                   rising=rising)

    return {name: _corner_coefficient(c, shape, swing_frac=swing_frac,
                                      tRF_tolerance=tRF_tolerance,
                                      anchor_frac=anchor(c.rising))
            for name, c in corners.items()}


# A receiver input's threshold window, as fractions of V_SEOS:
# (V_IH_min, V_IH_max, V_IL_min, V_IL_max). The rising threshold lies in [V_IH_min, V_IH_max]
# and the falling one in [V_IL_min, V_IL_max].
DataWindow = tuple[float, float, float, float]


# Every coefficient field of `DeltaTpdEnvelope` as (lane, corner): the `crossing_corners`
# corner it is the coefficient of, on the lane `_lane_sources` names. One table builds both
# envelopes and `crossing_corners_per_device`, so a field and its corner cannot drift apart.
#
# Σ_cross,PM,setup carries the δ = 0 coefficient for reporting and the ±α pair: δ cancels in
# the two-leg sum only on a linear ramp at equal per-leg t_RF, so `sigma_pm_setup_ns` takes the
# worse polarity, which stays exact on a convex (exp) ramp and at unequal t_RF.
_FIELD_CORNERS: dict[str, tuple[str, str]] = {
    # Δ_cross,MP and its P->P construction: the clock at each peripheral's clock input, the
    # Manager's data at the Peripheral's data input, A's data at B's.
    "mp_late_rise": ("clk", "mp_late_rise"),
    "mp_early_fall": ("clk", "mp_early_fall"),
    "mp_late_fall": ("clk", "mp_late_fall"),
    "mp_late_rise_b": ("clk_b", "mp_late_rise"),
    "mp_early_fall_b": ("clk_b", "mp_early_fall"),
    "mp_late_fall_b": ("clk_b", "mp_late_fall"),
    "sigma_pm_hold_rf_clk_b": ("clk_b", "sigma_pm_hold_rf_clk"),
    "mp_late_rise_data": ("mp", "mp_late_rise"),
    "mp_early_fall_data": ("mp", "mp_early_fall"),
    "mp_late_fall_data": ("mp", "mp_late_fall"),
    "mp_early_rise_data": ("mp", "sigma_pm_hold_rf_clk"),
    "mp_late_rise_per": ("pp", "mp_late_rise"),
    "mp_early_fall_per": ("pp", "mp_early_fall"),
    "mp_late_fall_per": ("pp", "mp_late_fall"),
    "mp_early_rise_per": ("pp", "sigma_pm_hold_rf_clk"),
    # Σ_cross,PM,setup: the clock at the Peripheral, the data at the Manager.
    "sigma_pm_setup": ("clk", "sigma_pm_setup"),
    "sigma_pm_setup_clk": ("clk", "sigma_pm_setup"),
    "sigma_pm_setup_data": ("pm", "sigma_pm_setup"),
    "sigma_pm_setup_clk_dgnd_pos": ("clk", "sigma_pm_setup_dgnd_pos"),
    "sigma_pm_setup_clk_dgnd_neg": ("clk", "sigma_pm_setup_dgnd_neg"),
    "sigma_pm_setup_data_dgnd_pos": ("pm", "sigma_pm_setup_dgnd_pos"),
    "sigma_pm_setup_data_dgnd_neg": ("pm", "sigma_pm_setup_dgnd_neg"),
    "sigma_pm_setup_fall_clk_dgnd_pos": ("clk", "sigma_pm_setup_fall_dgnd_pos"),
    "sigma_pm_setup_fall_clk_dgnd_neg": ("clk", "sigma_pm_setup_fall_dgnd_neg"),
    "sigma_pm_setup_fall_data_dgnd_pos": ("pm", "sigma_pm_setup_fall_dgnd_pos"),
    "sigma_pm_setup_fall_data_dgnd_neg": ("pm", "sigma_pm_setup_fall_dgnd_neg"),
    # Σ_cross,PM,hold: rr and rf on a rising launching edge, ff and fr on a falling one.
    "sigma_pm_hold_rr": ("clk", "sigma_pm_hold_rr"),
    "sigma_pm_hold_rr_clk": ("clk", "sigma_pm_hold_rr"),
    "sigma_pm_hold_rr_data": ("pm", "sigma_pm_hold_rr"),
    "sigma_pm_hold_rf_clk": ("clk", "sigma_pm_hold_rf_clk"),
    "sigma_pm_hold_rf_data": ("pm", "sigma_pm_hold_rf_data"),
    "sigma_pm_hold_ff_clk": ("clk", "sigma_pm_hold_ff"),
    "sigma_pm_hold_ff_data": ("pm", "sigma_pm_hold_ff"),
    "sigma_pm_hold_fr_data": ("pm", "sigma_pm_hold_rf_clk"),
}


def _lane_sources(Man_data_window: DataWindow | None, Per_data_window: DataWindow | None,
                  supplies: tuple[float, float, float] | None,
                  ) -> dict[str, tuple[DataWindow | None, str, float | None, float | None]]:
    """Each lane of `_FIELD_CORNERS` as (receiver's window, driving device, transmitter's
    supply, receiver's supply), the window None for the bus's V_IH / V_IL and the supplies None
    without `supplies` (Manager, Peripheral, Peripheral B)."""
    man, per, per_b = supplies if supplies is not None else (None, None, None)
    return {
        "clk": (None, "Man", man, per),                 # the clock at the Peripheral's input
        "clk_b": (None, "Man", man, per_b),             # the clock at Peripheral B's input
        "mp": (Per_data_window, "Man", man, per),       # Manager data, Peripheral data input
        "pp": (Per_data_window, "Per", per, per_b),     # A's data, B's data input
        "pm": (Man_data_window, "Per", per, man),       # Peripheral data, Manager data input
    }


def _window_kw(window: DataWindow) -> dict[str, float]:
    ih_min, ih_max, il_min, il_max = window
    return dict(V_IH_min_frac=ih_min, V_IH_max_frac=ih_max, V_IL_min_frac=il_min,
                V_IL_max_frac=il_max)


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
    Man_data_window: DataWindow | None = None,
    Per_data_window: DataWindow | None = None,
    from_anchor: bool = False,
    supplies: tuple[float, float, float] | None = None,
) -> DeltaTpdEnvelope:
    """Per-device envelope: each crossing takes the ramp shape of the device DRIVING the
    lane and the threshold window of the input RECEIVING it, counted as
    `compute_delta_tpd_envelope`'s ``from_anchor`` says.

    ``supplies`` is (Manager, Peripheral, Peripheral B), each device's V_SEOS as a fraction
    of nominal: every crossing then takes its transmitter's and its receiver's. Peripheral B
    is the second peripheral of a P->P leg (its clock detections, the `_b` fields, and its
    detection of the Peripheral's data); the Peripheral is A there. Without it each crossing
    takes its own worst supplies (`crossing_corners`).

    * Shapes: the clock lane and the MP data lane are Manager-driven (Man_shape); the PM and
      P->P data lanes are Peripheral-driven (Per_shape).
    * Windows: every CLOCK input (a Peripheral detecting the forwarded clock) reads the
      V_IH/V_IL arguments. A DATA input reads its receiver's window: the Peripheral's
      (MP and P->P data) and the Manager's (PM data), each defaulting to the V_IH/V_IL
      arguments when None.

    `_FIELD_CORNERS` names each field's lane and corner. With equal shapes and no data windows
    this returns the same envelope as compute_delta_tpd_envelope(ramp_shape=Man_shape). The
    `rising_*` / `falling_*` deviations and `uncorrelated_diff` are the clock lane's, at the
    envelope's own supply corners.
    """
    swing = _checked_swing(V_OH_frac, V_OL_frac)
    bus: DataWindow = (V_IH_min_frac, V_IH_max_frac, V_IL_min_frac, V_IL_max_frac)
    common = dict(VDD_tol=VDD_tol, noise_budget_V=noise_budget_V, V_nom_V=V_nom_V)
    shapes = {"Man": Man_shape, "Per": Per_shape}
    coeffs = {
        lane: _coefficients(
            crossing_corners(**_window_kw(window or bus), tx_supply=tx, rx_supply=rx, **common),
            shapes[driver], swing_frac=swing, tRF_tolerance=tRF_tolerance,
            V_OH_frac=V_OH_frac, V_OL_frac=V_OL_frac, from_anchor=from_anchor)
        for lane, (window, driver, tx, rx)
        in _lane_sources(Man_data_window, Per_data_window, supplies).items()}
    fields = {f: coeffs[lane][corner] for f, (lane, corner) in _FIELD_CORNERS.items()}
    rising_max, rising_min, falling_max, falling_min = _deviations(
        crossing_corners(**_window_kw(bus), **common), Man_shape, swing_frac=swing)
    return DeltaTpdEnvelope(
        rising_max=rising_max, rising_min=rising_min,
        falling_max=falling_max, falling_min=falling_min,
        correlated_diff=fields["mp_late_rise_data"] - fields["mp_early_fall"],
        uncorrelated_diff=rising_max - falling_min,
        **fields)


def crossing_corners_per_device(
    *,
    V_IH_min_frac: float = 0.45,
    V_IH_max_frac: float = 0.65,
    V_IL_min_frac: float = 0.35,
    V_IL_max_frac: float = 0.55,
    VDD_tol: float = 0.05,
    noise_budget_V: float = 0.120,
    V_nom_V: float = 1.2,
    Man_data_window: DataWindow | None = None,
    Per_data_window: DataWindow | None = None,
    supplies: tuple[float, float, float] | None = None,
) -> dict[str, CrossingCorner]:
    """The corner of each coefficient field of `compute_delta_tpd_envelope_per_device`, by
    field name (`_FIELD_CORNERS`): each crossing at the window of the input receiving it and,
    with ``supplies``, at its devices' supplies, as that function selects them."""
    bus: DataWindow = (V_IH_min_frac, V_IH_max_frac, V_IL_min_frac, V_IL_max_frac)
    common = dict(VDD_tol=VDD_tol, noise_budget_V=noise_budget_V, V_nom_V=V_nom_V)
    corners = {
        lane: crossing_corners(**_window_kw(window or bus), tx_supply=tx, rx_supply=rx,
                               **common)
        for lane, (window, _driver, tx, rx)
        in _lane_sources(Man_data_window, Per_data_window, supplies).items()}
    return {f: corners[lane][corner] for f, (lane, corner) in _FIELD_CORNERS.items()}


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
