"""Per-side spec sources for the cross-spec (SWI3S ↔ SoundWire 1.3) calculator.

A mixed bus has a Manager built to one spec and a Peripheral built to another. Each side
contributes the timing its own spec promises, and each receiver enforces its own setup/hold
requirement. This module holds the per-spec presets.

A SoundWire side is carried as its SWI3S EQUIVALENTS (`sw13_equivalents`): each Table 17
value re-measured between the SWI3S anchors for the same event, on the side it is on, at the
slews `sw13_equivalent_slews` names. The inequalities move each launch to the operating
slews by its own anchor (`sw13_anchor_correction`).

A SoundWire DATA input has its own threshold window (`data_input_window`). It guarantees a
high above 65 % and a low below 35 % of Vdd (Table 12), with no hysteresis, so its single
threshold, and therefore each edge's, lies anywhere in 35-65 %. Hysteresis does not move
where an edge is detected; the window each edge's threshold can occupy does. A SoundWire clock
input's thresholds (V_TP 50-65 %, V_TN 35-50 %) lie inside SWI3S's V_IH/V_IL windows, so clock
inputs keep the V_IH/V_IL rows.
"""

from __future__ import annotations

import functools
import math
from dataclasses import dataclass
from enum import Enum
from typing import Literal

from .delta_tpd import (
    CrossingCorner,
    DataWindow,
    RampShape,
    compute_delta_tpd_envelope,
    crossing_time_frac,
    per_tdd_offset_frac,
)

SpecSource = Literal["SWI3S_PHY1", "SWI3S_PHY2", "SWI3S_PHY2_PROP",
                     "SWI3S_PHY2_EDE", "SW13_1V2"]
SystemSize = Literal["small", "large"]
Corner = Literal["min", "max"]
Side = Literal["Man", "Per"]

SPEC_SOURCES: tuple[SpecSource, ...] = ("SWI3S_PHY1", "SWI3S_PHY2",
                                        "SWI3S_PHY2_PROP", "SWI3S_PHY2_EDE",
                                        "SW13_1V2")

SPEC_LABELS: dict[SpecSource, str] = {
    "SWI3S_PHY1": "SWI3S PHY1",
    "SWI3S_PHY2": "SWI3S PHY2",
    "SWI3S_PHY2_PROP": "SWI3S PHY2 (proposed)",
    "SWI3S_PHY2_EDE": "SWI3S PHY2 (EDE proposed)",
    "SW13_1V2": "SoundWire 1.3 (1.2 V)",
}

# Surfaced wherever a SoundWire side is selected, so the reader knows which threshold
# window each input uses.
THRESHOLD_NOTE = (
    "A SoundWire Data input guarantees only a high above 65 % and a low below 35 % of Vdd, "
    "with no hysteresis, so its rising and falling thresholds each lie anywhere in 35-65 %; "
    "its data crossings use that window. Clock inputs, and SWI3S Data inputs, use the "
    "V_IH / V_IL rows."
)

# A SoundWire Data input's threshold window, (V_IH_min, V_IH_max, V_IL_min, V_IL_max) as
# fractions of Vdd: V_ILmax_Data,min 35 % and V_IHmin_Data,max 65 % (Table 12).
_SW13_DATA_WINDOW: DataWindow = (0.35, 0.65, 0.35, 0.65)


def data_input_window(spec: SpecSource) -> DataWindow | None:
    """The threshold window of a Data input built to `spec`, or None where the V_IH / V_IL
    rows govern it (every SWI3S source)."""
    return _SW13_DATA_WINDOW if is_soundwire(spec) else None


# Surfaced wherever a SoundWire t_RF is in force.
TRF_NOTE = (
    "SoundWire t_Slew_Data has a min of 2.0 ns and no tabulated max, so "
    "t_Slew_Clock is used as the nearest available bound on the Data lane. "
    "That substitution is an assumption, not a spec value."
)

# Surfaced whenever a proposed (unratified) spec source is selected.
PROPOSAL_NOTE = (
    "These are PROPOSED SWI3S PHY2 values, not ratified. They are modelled so "
    "the revision can be evaluated, and must not be quoted as specification."
)

# The proposal's rate-relative alternative for the two Manager maxima.
PROPOSAL_UI_RELATIVE_NOTE = (
    "The proposal offers a clock-launched Manager output as an alternative to "
    "the 9-23 ns analog delay: the data edge is launched from a 2x or 4x clock, "
    "so Man_t_DD becomes a deterministic 0.5 UI or 0.25 UI instead of a range. "
    "Which is better is rate-dependent: 4x leaves more setup room and less hold, 2x the "
    "reverse, so 4x runs out of hold first as the clock rate rises."
)


class LaunchMode(str, Enum):
    """How the Manager launches its data edge.

    An analog delay line has a min-to-max spread that the inequalities absorb on both
    sides; a clock-launched edge is deterministic — min == max — so the spread vanishes.

    The two clock options trade in opposite directions on the two MP legs:

    * ``CLK4`` (0.25 UI) leaves the most setup room but the least hold, and is
      bounded by **hold** — 0.25*UI shrinks with the clock period, so it runs
      out at high rates.
    * ``CLK2`` (0.5 UI) is the reverse: ample hold, bounded by **setup**.

    `launch_mode_max_fclk_MHz` gives each mode's rate bound.
    """

    ANALOG = "analog"
    CLK2 = "2x"
    CLK4 = "4x"


LAUNCH_LABELS: dict[LaunchMode, str] = {
    LaunchMode.ANALOG: "analog delay (range as tabulated)",
    LaunchMode.CLK2: "2× CLK (1/2 UI grid)",
    LaunchMode.CLK4: "4× CLK (1/4 UI grid)",
}

_LAUNCH_UI_FRACTION: dict[LaunchMode, float] = {
    LaunchMode.CLK2: 0.5,
    LaunchMode.CLK4: 0.25,
}


def launch_grid_UI(mode: LaunchMode) -> float | None:
    """The clock's RESOLUTION, in UI, or None for analog.

    Numerically the same as the data edge's placement in `_LAUNCH_UI_FRACTION`,
    but a different quantity.  The placement says where the DATA edge lands; the
    resolution says which points the Manager can place ANY clocked output on:

        2x   multiples of 0.50 UI  ->  0.00, 0.50
        4x   multiples of 0.25 UI  ->  0.00, 0.25, 0.50, 0.75

    A 4x clock therefore offers three interior points, and t_ZD, t_DZ and t_DD do
    NOT have to pick the same one.  A 2x clock offers only 0.50.
    """
    return _LAUNCH_UI_FRACTION.get(mode)


# How far below its floor `snap_to_launch_grid` may place a value, in ns: a floor equal to a
# grid point at the display's precision keeps that point.
SNAP_TOL_NS = 5e-4


def _ui_ns(f_clk_MHz: float, duty_min: float) -> float:
    """One UI in ns: the shorter clock phase, period times the duty floor, as
    ``CalcInputs.UI_ns``."""
    return 1000.0 * duty_min / f_clk_MHz


def snap_to_launch_grid(value_ns: float, mode: LaunchMode, f_clk_MHz: float,
                        duty_min: float = 0.45, *, floor_ns: float | None = None) -> float:
    """`value_ns` moved to the nearest point the mode's clock can actually place.

    Analog leaves it alone.  Under a clocked mode an off-grid value describes a
    Manager that cannot be built with the selected clock, so it is snapped, and the
    caller reports that it was.

    `floor_ns` is a lower bound the result may not fall below by more than SNAP_TOL_NS --
    pass the row's tabulated minimum.  Without it nearest-point snapping can round down
    through the minimum, on a coarse grid to zero (at 12.288 MHz the PHY2 revision's 9.0 ns
    Man_t_DD,min is 0.246 UI, whose nearest 2x point is 0.00 UI).  With a floor the answer is the
    nearest point if it is at or above ``floor_ns − SNAP_TOL_NS``, and otherwise the lowest
    point that is.
    """
    grid = launch_grid_UI(mode)
    if grid is None or f_clk_MHz <= 0:
        return value_ns
    step = grid * _ui_ns(f_clk_MHz, duty_min)
    if step <= 0:
        return value_ns
    # Half away from zero, NOT the built-in round(), which is half-to-even: a tie
    # resolves to the same point from either side of the grid.
    k = value_ns / step
    snapped = (math.floor(k + 0.5) if k >= 0 else math.ceil(k - 0.5)) * step
    if floor_ns is not None and snapped < floor_ns - SNAP_TOL_NS:
        snapped = math.ceil((floor_ns - SNAP_TOL_NS) / step) * step
    return snapped


def launch_mode_tDD_ns(mode: LaunchMode, f_clk_MHz: float,
                       duty_min: float = 0.45) -> tuple[float, float] | None:
    """(min, max) Man_t_DD for a clock-launched edge, or None for analog.

    A clock-launched edge lands on a known clock phase, so the min and max
    collapse to the same value. UI matches ``CalcInputs.UI_ns`` (the shorter clock
    phase: period x duty floor).
    """
    frac = _LAUNCH_UI_FRACTION.get(mode)
    if frac is None:
        return None
    t = frac * _ui_ns(f_clk_MHz, duty_min)
    return (t, t)


def is_soundwire(spec: SpecSource) -> bool:
    return spec.startswith("SW13")


def is_ede(spec: SpecSource) -> bool:
    """Whether this source describes the EndDriveEarly proposal: the proposed
    revision's values, on a side that releases by its EDE Delay
    (`CalcInputs.Man_ede` / `Per_ede`) instead of its t_DZ.
    """
    return spec == "SWI3S_PHY2_EDE"


def is_proposed(spec: SpecSource) -> bool:
    """True for the proposed PHY2 revision source, `SWI3S_PHY2_PROP`.

    The UI uses it to keep that source out of the spec selector (it seeds the
    Example column instead) and to route its rows through `preset()`. It is NOT
    the test for "unratified": the EDE source is unratified too and is offered in
    the selector. See `is_unratified`.
    """
    return spec.endswith("_PROP")


def is_unratified(spec: SpecSource) -> bool:
    """True for every source whose values are not ratified specification: the
    proposed revision and the EDE proposal built on it.

    A result computed from one carries `PROPOSAL_NOTE`: a proposed value must
    never be presented with the same authority as a tabulated one.
    """
    return is_proposed(spec) or is_ede(spec)


# ---------------------------------------------------------------------------
# Presets
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SpecPreset:
    """One side's timing as SWI3S quantities: tabulated for a SWI3S side, the SWI3S
    equivalents (`sw13_equivalents`) for a SoundWire one.

    ``t_DD_*`` and ``t_ZD_*`` are measured to 20 % of the ramp (see "Launch anchors").
    """
    spec: SpecSource
    label: str
    t_DD_min_ns: float
    t_DD_max_ns: float
    t_IS_max_ns: float
    t_IH_max_ns: float
    t_DZ_max_ns: float
    t_ZD_ns: float                 # t_ZD's max; t_ZD_min_ns its min
    # CAUTION: this field does NOT mean the same thing on both sides.
    #   SWI3S    5.0 ns  = the spec REFERENCE test condition (a nominal); its
    #                      tabulated range is 3.0-8.3 ns.
    #   SoundWire 5.0 ns = t_Slew_Clock MAX (small); the spec publishes no
    #                      nominal slew at all, only min 2.0 and that max.
    tRF_ns: float
    V_nom_V: float
    F_CLK_max_MHz: float
    t_ZD_min_ns: float = 0.0
    t_DZ_min_ns: float = 0.0
    # Man_tKeeper_Response: the time the Manager's keeper takes to follow the data, from the
    # data crossing its input threshold. A Manager-side value.
    t_keeper_ns: float = 3.0


# SWI3S v1.1r06: the "PHY1 & PHY2 Clock Frequencies", "PHY1 & PHY2 Input Timing" and
# "PHY1 & PHY2 Output Timing" tables. Man and Per differ, so both are held.
#
# The two timing tables are structured differently, which matters when reasoning
# about a proposed revision rather than just reading a value:
#
#   The "PHY1 & PHY2 Input Timing" table (t_IS, t_IH) is genuinely shared — ONE row per
#   parameter, no PHY1/PHY2 distinction at all.
#
#   The "PHY1 & PHY2 Output Timing" table prints every parameter as a separate PHY1 row
#   and PHY2 row and never collapses them. Per_tDD, Per_tZD and Man_tZD differ between
#   the PHYs; Man_tDD (7-18), Man_tDZ and Per_tDZ (0-10) are two rows that
#   happen to AGREE — not one shared value. The duplication below is therefore
#   deliberate: if a revision diverges them, each PHY has a row to change.
_SWI3S: dict[tuple[SpecSource, Side], dict] = {
    ("SWI3S_PHY2", "Man"): dict(t_DD_min_ns=7.0, t_DD_max_ns=18.0,
                                t_IS_max_ns=3.0, t_IH_max_ns=0.0,
                                t_DZ_max_ns=10.0, t_ZD_ns=18.0, t_ZD_min_ns=2.0),
    ("SWI3S_PHY2", "Per"): dict(t_DD_min_ns=2.0, t_DD_max_ns=20.0,
                                t_IS_max_ns=4.0, t_IH_max_ns=4.5,
                                t_DZ_max_ns=10.0, t_ZD_ns=18.0, t_ZD_min_ns=2.0),
    ("SWI3S_PHY1", "Man"): dict(t_DD_min_ns=7.0, t_DD_max_ns=18.0,
                                t_IS_max_ns=3.0, t_IH_max_ns=0.0,
                                t_DZ_max_ns=10.0, t_ZD_ns=30.0, t_ZD_min_ns=14.0),
    ("SWI3S_PHY1", "Per"): dict(t_DD_min_ns=14.0, t_DD_max_ns=50.0,
                                t_IS_max_ns=4.0, t_IH_max_ns=4.5,
                                t_DZ_max_ns=10.0, t_ZD_ns=50.0, t_ZD_min_ns=14.0),
    # PROPOSED PHY2 revision — NOT RATIFIED. It differs from PHY2 in five rows:
    # Man_t_DD 9-23, Per_t_IH 0-1.5, Per_t_DD 2-9, Man_t_ZD 9-23 and Per_t_ZD 2-9.
    #
    # The proposal also offers a rate-relative alternative for the two Manager
    # maxima — "0.5 UI at 2x CLK, 0.25 UI at 4x CLK". The preset rows hold only the analog
    # ranges; the clocked alternative is the `LaunchMode` selector. At the mandatory
    # 13.2 MHz, 0.5 UI = 17.05 ns, which is TIGHTER than the 23 ns fixed cap, and the two
    # only cross at 9.78 MHz. Whether a device must
    # meet both or may pick the looser changes the answer, and the wording does
    # not say. See PROPOSAL_UI_RELATIVE_NOTE.
    ("SWI3S_PHY2_PROP", "Man"): dict(t_DD_min_ns=9.0, t_DD_max_ns=23.0,
                                     t_IS_max_ns=3.0, t_IH_max_ns=0.0,
                                     t_DZ_max_ns=10.0, t_ZD_ns=23.0, t_ZD_min_ns=9.0),
    ("SWI3S_PHY2_PROP", "Per"): dict(t_DD_min_ns=2.0, t_DD_max_ns=9.0,
                                     t_IS_max_ns=4.0, t_IH_max_ns=1.5,
                                     t_DZ_max_ns=10.0, t_ZD_ns=9.0, t_ZD_min_ns=2.0),
    # EDE: the proposed revision, on a side that releases by its EDE Delay. t_DZ is the
    # revision's and is not read on that side.
    ("SWI3S_PHY2_EDE", "Man"): dict(t_DD_min_ns=9.0, t_DD_max_ns=23.0,
                                    t_IS_max_ns=3.0, t_IH_max_ns=0.0,
                                    t_DZ_max_ns=10.0, t_ZD_ns=23.0, t_ZD_min_ns=9.0),
    ("SWI3S_PHY2_EDE", "Per"): dict(t_DD_min_ns=2.0, t_DD_max_ns=9.0,
                                    t_IS_max_ns=4.0, t_IH_max_ns=1.5,
                                    t_DZ_max_ns=10.0, t_ZD_ns=9.0, t_ZD_min_ns=2.0),
}
_SWI3S_FCLK: dict[SpecSource, float] = {"SWI3S_PHY1": 6.6, "SWI3S_PHY2": 13.2,
                                        "SWI3S_PHY2_PROP": 13.2,
                                        "SWI3S_PHY2_EDE": 13.2}

# SoundWire 1.3 Table 17 (1.2 V), indexed [small, large].
#
# small/large is a SYSTEM-INTEGRATION envelope (C_Bus / L_Bus, Table 5), not a
# device class: requirements {1207}/{1208} oblige a device to meet the table
# whenever the load is inside those limits.
#     small: C_Bus ≤ 60 pF,  L_Bus ≤ 30 cm
#     large: C_Bus ≤ 100 pF, L_Bus ≤ 50 cm
#
# FIVE parameters split by size — t_OV_Data, t_Slew_Clock, t_High_Clock, t_Low_Clock,
# f_Clock. The rest are common to both. One table binds every Device, Manager or
# Peripheral. `t_Slew_min_ns` is t_Slew_Clock's min, which t_Slew_Data's min equals.
_SW13: dict[SpecSource, dict] = {
    "SW13_1V2": dict(t_OV_max_ns=(27.9, 29.0), t_Slew_Clock_max_ns=(5.0, 6.0),
                     f_Clock_MHz=(12.3, 11.0), t_DZ_max_ns=5.0, t_ZD_min_ns=8.1,
                     t_ISetup_max_ns=0.0, t_IHold_max_ns=4.0, t_Keeper_Delay_max_ns=2.5,
                     t_Slew_min_ns=2.0, V_nom_V=1.2),
}

# The SoundWire bus keeper's trigger points (Tables 12–14, 1.2 V): V_TH_Keeper,max is 65 % of
# the larger of the keeper's Vdd and VddNom, V_TL_Keeper,min 35 % of the smaller. A releasing
# driver holds the data past the one its edge crosses for t_Keeper_Delay,max (Figures 18, 19).
_SW13_KEEPER_TH_MAX, _SW13_KEEPER_TL_MIN = 0.65, 0.35


def sw13_keeper_thresholds(keeper_supply: float) -> tuple[float, float]:
    """A SoundWire keeper's (V_TH_Keeper,max, V_TL_Keeper,min), as fractions of nominal, at
    its supply `keeper_supply` (a fraction of nominal)."""
    return (_SW13_KEEPER_TH_MAX * max(keeper_supply, 1.0),
            _SW13_KEEPER_TL_MIN * min(keeper_supply, 1.0))


# The SoundWire clock trigger points (Table 12, fractions of Vdd): a max parameter starts
# at V_TP_Clock,max (65 %, rising), a min one at V_TP_Clock,min (50 %).
_SW13_TP_MAX, _SW13_TP_MIN = 0.65, 0.50
# Where a SoundWire t_OV ends: 80 % of the data ramp.
_SW13_OV_END = 0.80
# The SWI3S t_DZ endpoint is the pin moved 10 % V_SEOS from its settled level; through the
# SWI3S test load (RL_DP 500 ohm, CL_DP 5 pF) that follows high impedance by RC·ln(1/0.9).
_SWI3S_TDZ_DEPARTURE_NS = 500.0 * 5e-12 * 1e9 * math.log(1.0 / 0.9)


# ---------------------------------------------------------------------------
# Launch anchors
# ---------------------------------------------------------------------------
#
# A tabulated t_DD or t_ZD ends at its ANCHOR: the data ramp's V_OL crossing rising, V_OH
# falling, 20 % into the transition (the Peripheral and Manager Output Timing figures). It holds
# at every t_RF of the ramp setting, so the anchor instant is bounded whatever the split between
# delay and ramp, and an inequality charges only the ramp after it. The calculator places every
# edge by this 20 % point and counts each crossing from it (`CalcInputs.envelope`); the ramp
# before it (`per_tdd_offset_frac`) is
#
#     V=0 → 20 %  = 0.20/0.60 = 1/3 · t_RF          linear ramp
#     V=0 → 20 %  = −ln(0.8)/ln 4 ≈ 0.161 · t_RF    exponential ramp (τ = t_RF/ln 4)
#
# THE CLOCK END. A Peripheral's is its clock input crossing V_IH / V_IL, which the crossing
# envelopes place. A Manager's is its clock output crossing V_OL / V_OH, its clock reference,
# across the clock's ramp setting. That holds for every Manager timing, output and input,
# because the Manager sources the clock. The UI runs between two consecutive clock references.
#
# A Peripheral's input timing is measured with a clock of exactly REF_TRF_NS; its anchors are
# threshold crossings at both ends, so the crossing envelopes charge the operating slew.
#
# t_DZ has no ramp in it and is used as tabulated.

# The 5 ns ramp setting's nominal: a Peripheral's input-timing test slew, and the data slew a
# SoundWire Peripheral's min-side equivalents are derived at (`sw13_equivalent_slews`).
REF_TRF_NS = 5.0


# The 5 ns ramp setting's limits, "PHY1 & PHY2 Output Ramp Time": Min (Nom −40 %) and Max
# (Nom +67 %), measured 20-80 %.
_SWI3S_TRF_RANGE_NS = (3.0, 8.3)


def trf_range_ns(spec: SpecSource, system_size: SystemSize = "small") -> tuple[float, float]:
    """(min, max) t_RF of one edge on a side built to `spec`: the 5 ns ramp setting's limits
    for SWI3S, t_Slew_Clock's min and max for SoundWire. No edge leaves this range,
    the per-edge slew tolerance included."""
    if is_soundwire(spec):
        t = _SW13[spec]
        return t["t_Slew_min_ns"], t["t_Slew_Clock_max_ns"][0 if system_size == "small" else 1]
    return _SWI3S_TRF_RANGE_NS


def sw13_equivalent_slews(spec: SpecSource, side: Side, *, corner: Corner,
                          system_size: SystemSize = "small") -> tuple[float, float]:
    """(k, s), the clock and data slews in ns that `sw13_equivalents` derives a SoundWire
    launch row at: t_Slew_Clock's max for both on the max side; on the min side t_Slew_Clock's
    min for the clock, and for the data a Peripheral's reference slew REF_TRF_NS or a
    Manager's t_Slew_Clock's min."""
    t = _SW13[spec]
    if corner == "max":
        k = t["t_Slew_Clock_max_ns"][0 if system_size == "small" else 1]
        return k, k
    k_min = t["t_Slew_min_ns"]
    return k_min, (REF_TRF_NS if side == "Per" else k_min)


@functools.lru_cache(maxsize=256)
def sw13_ramp_fracs(shape: RampShape, *, V_OH_frac: float = 0.80,
                    V_OL_frac: float = 0.20,
                    rising: bool = True) -> tuple[float, float, float, float]:
    """(z, p, q, r) on a ramp of `shape` and polarity `rising`, in units of its t_RF, which on
    a linear ramp runs from V_OL to V_OH and on an exponential one is the 20-80 % time: z from
    the ramp's start to its anchor (`per_tdd_offset_frac`: the V_OL crossing rising, the V_OH
    crossing falling), p and q from the anchor to the instants the ramp reaches 65 % and 50 %
    of Vdd rising, 35 % and 50 % falling (V_TP_Clock rising, V_TN_Clock falling), and r from
    the anchor to the instant it reaches 80 % of Vdd rising, 20 % falling, where a SoundWire
    t_OV ends. At the default V_OH / V_OL, at either polarity,
    linear: 1/3, 0.75, 0.5, 1; exponential: 0.161, 0.596, 0.339, 1. The two polarities agree
    where V_OH + V_OL = 1."""
    z = per_tdd_offset_frac(shape, V_OH_frac=V_OH_frac, V_OL_frac=V_OL_frac, rising=rising)

    def to(v: float) -> float:
        level = v if rising else 1.0 - v
        at = CrossingCorner(rising, f"{100 * level:g} %", level, 1.0, 0.0, 1.0, +1)
        return crossing_time_frac(at, shape, swing_frac=V_OH_frac - V_OL_frac) - z

    # A linear ramp's t_RF runs from V_OL to V_OH, so r is exactly 1 where the ramp's valid
    # level (V_OH rising, V_OL falling) is the t_OV end point.
    valid, end = (V_OH_frac, _SW13_OV_END) if rising else (V_OL_frac, 1.0 - _SW13_OV_END)
    exact = shape == "linear" and math.isclose(valid, end, abs_tol=1e-12)
    r = 1.0 if exact else to(_SW13_OV_END)
    return z, to(_SW13_TP_MAX), to(_SW13_TP_MIN), r


def _sw13_anchor_ns(spec: SpecSource, side: Side, corner: Corner, k: float, s: float,
                    system_size: SystemSize, shape: RampShape = "linear", *,
                    V_OH_frac: float = 0.80, V_OL_frac: float = 0.20,
                    rising: bool = True, clk_rising: bool = True) -> float:
    """A SoundWire launch's anchor, the launched ramp's 20 % point (its V_OL crossing rising,
    its V_OH crossing falling) after the side's SWI3S clock reference, at clock slew `k` and
    data slew `s`, both ramps of `shape`. z and r are the data ramp's `sw13_ramp_fracs` at its
    polarity `rising`, p and q the Manager clock ramp's at its polarity `clk_rising`:

        Per max   t_OV − r·s                  t_OV ends at 80 % of the data ramp
        Per min   t_ZD + z·s                  t_ZD_Data ends at low impedance, the ramp's 0 %
        Man max   t_OV + p·k − r·s            from the clock's 65 % point
        Man min   t_ZD + q·k + z·s            from the clock's 50 % point
    """
    t = _SW13[spec]
    i = 0 if system_size == "small" else 1
    z, _, _, r = sw13_ramp_fracs(shape, V_OH_frac=V_OH_frac, V_OL_frac=V_OL_frac,
                                 rising=rising)
    _, late, early, _ = sw13_ramp_fracs(shape, V_OH_frac=V_OH_frac, V_OL_frac=V_OL_frac,
                                        rising=clk_rising)
    if corner == "max":
        return t["t_OV_max_ns"][i] - r * s + (late * k if side == "Man" else 0.0)
    return t["t_ZD_min_ns"] + z * s + (early * k if side == "Man" else 0.0)


def sw13_anchor_correction(spec: SpecSource, side: Side, *, corner: Corner,
                           t_rf_clk_ns: float, t_rf_data_ns: float,
                           system_size: SystemSize = "small",
                           shape: RampShape = "linear",
                           V_OH_frac: float = 0.80, V_OL_frac: float = 0.20,
                           rising: bool = True, clk_rising: bool = True) -> float:
    """A SoundWire launch's anchor less its row value, at the operating slews, ramp `shape`
    and V_OH / V_OL, for a data edge of polarity `rising` launched from a Manager clock edge of
    polarity `clk_rising`, in ns; 0 for a SWI3S side.

    The rows hold `sw13_equivalents`: the anchor (`_sw13_anchor_ns`) on linear ramps at the
    default V_OH / V_OL and the slews `sw13_equivalent_slews` names for `system_size`, where
    both polarities place it alike. The correction is the anchor at the operating slews,
    shape, V_OH / V_OL and polarities less that, so it is 0 at the equivalents' own slews on a
    linear ramp at the default V_OH / V_OL and follows an edited row, being a difference
    between two placements and not between a row and a table value. Each ramp is placed at
    its own polarity (`sw13_ramp_fracs`); the two polarities agree where V_OH + V_OL = 1.
    A Peripheral's launch has no clock part, so `clk_rising` does not move it.
    """
    if not is_soundwire(spec):
        return 0.0
    k0, s0 = sw13_equivalent_slews(spec, side, corner=corner, system_size=system_size)
    return (_sw13_anchor_ns(spec, side, corner, t_rf_clk_ns, t_rf_data_ns, system_size, shape,
                            V_OH_frac=V_OH_frac, V_OL_frac=V_OL_frac, rising=rising,
                            clk_rising=clk_rising)
            - _sw13_anchor_ns(spec, side, corner, k0, s0, system_size))


def _round_down(x: float) -> float:
    return math.floor(x * 10.0 + 1e-6) / 10.0


def _round_up(x: float) -> float:
    return math.ceil(x * 10.0 - 1e-6) / 10.0


def sw13_equivalents(spec: SpecSource, side: Side,
                     system_size: SystemSize = "small") -> dict[str, float]:
    """A SoundWire side's Table 17 values as SWI3S rows, in ns.

    Each value is re-measured between the SWI3S anchors for the same event, with linear ramps;
    ``k`` is t_Slew_Clock and ``s`` t_Slew_Data:

    * A SWI3S PERIPHERAL starts at its clock-input threshold crossing, and V_IH,max is
      SoundWire's 65 % V_TP_Clock,max, so its values take no clock-side offset.
    * A SWI3S MANAGER starts at 20 % of its clock ramp, so a SoundWire 65 % trigger is
      ``0.75·k`` later and a 50 % one ``0.5·k`` later.
    * t_OV ends at 80 % of the data ramp, the SWI3S t_DD at 20 %: one ``s`` earlier. It bounds
      a turn-on too (§5.1.11), so it is the max of both t_DD and t_ZD.
    * t_ZD_Data ends at low impedance, where the ramp starts; the SWI3S t_DD / t_ZD end ``s/3``
      later. Every SoundWire bit is a turn-on (Table 22), so it is the min of both.

    Max-side values are taken at the slow end, ``k = s`` = t_Slew_Clock's max for
    `system_size` (t_Slew_Data has none). Min-side values take the clock at t_Slew_Clock's min,
    a Peripheral's data at the reference slew and a Manager's at t_Slew_Clock's min
    (`sw13_equivalent_slews`). `sw13_anchor_correction` moves each launch to the operating
    slews.
    Each value is rounded to 0.1 ns in the direction that keeps it a bound: a max (and a
    receiver's demand) up, a min down.
    """
    t = _SW13[spec]
    k = t["t_Slew_Clock_max_ns"][0 if system_size == "small" else 1]
    k_min = t["t_Slew_min_ns"]
    t_dz = t["t_DZ_max_ns"]
    t_is, t_ih = t["t_ISetup_max_ns"], t["t_IHold_max_ns"]
    _, late, early, _ = sw13_ramp_fracs("linear")       # clock 20 % → 65 %, → 50 %
    dd_max, dd_min = (
        _sw13_anchor_ns(spec, side, c, *sw13_equivalent_slews(spec, side, corner=c,
                                                              system_size=system_size),
                        system_size)
        for c in ("max", "min"))
    if side == "Per":
        dz = t_dz + _SWI3S_TDZ_DEPARTURE_NS
        t_is_eq, t_ih_eq = t_is, t_ih
    else:
        dz = t_dz + late * k + _SWI3S_TDZ_DEPARTURE_NS
        t_is_eq = t_is - early * k_min
        t_ih_eq = t_ih + late * k
    return {
        "t_DD_min_ns": _round_down(dd_min), "t_DD_max_ns": _round_up(dd_max),
        "t_ZD_min_ns": _round_down(dd_min), "t_ZD_max_ns": _round_up(dd_max),
        "t_DZ_max_ns": _round_up(dz),
        "t_IS_max_ns": _round_up(t_is_eq), "t_IH_max_ns": _round_up(t_ih_eq),
        "t_keeper_ns": t["t_Keeper_Delay_max_ns"],
        "t_RF_min_ns": k_min, "t_RF_max_ns": k,
    }


def preset(spec: SpecSource, side: Side,
           system_size: SystemSize = "small") -> SpecPreset:
    """Tabulated timing for one side built to ``spec``.

    ``system_size`` applies only to SoundWire; SWI3S does not split by bus
    envelope, so it is ignored for a SWI3S side.
    """
    if is_soundwire(spec):
        t = _SW13[spec]
        i = 0 if system_size == "small" else 1
        e = sw13_equivalents(spec, side, system_size)
        return SpecPreset(
            spec=spec,
            label=f"{SPEC_LABELS[spec]} {system_size}",
            t_DD_min_ns=e["t_DD_min_ns"], t_DD_max_ns=e["t_DD_max_ns"],
            t_IS_max_ns=e["t_IS_max_ns"], t_IH_max_ns=e["t_IH_max_ns"],
            t_DZ_max_ns=e["t_DZ_max_ns"], t_DZ_min_ns=0.0,
            t_ZD_ns=e["t_ZD_max_ns"], t_ZD_min_ns=e["t_ZD_min_ns"],
            tRF_ns=e["t_RF_max_ns"],
            V_nom_V=t["V_nom_V"],
            F_CLK_max_MHz=t["f_Clock_MHz"][i],
            t_keeper_ns=e["t_keeper_ns"],
        )
    v = _SWI3S[(spec, side)]
    return SpecPreset(
        spec=spec,
        label=f"{SPEC_LABELS[spec]} {side}",
        tRF_ns=REF_TRF_NS,
        V_nom_V=1.2,
        F_CLK_max_MHz=_SWI3S_FCLK[spec],
        **v,
    )


def default_handover_UIs(man_spec: SpecSource, per_spec: SpecSource) -> float:
    """UIs allocated to a handover by default, for this pair of sides.

    This is a PHY property, not a constant (§11.1.1.1): "Short handover duration
    (0 UIs for PHY1 and 1 UI for PHY2). Handovers can be programmed to be longer
    for larger systems."

    PHY1 needs none because its own output timing already satisfies
    ``t_ZD_Min_Phy1 >= t_DZ_Max_Phy1`` (14.0 >= 10.0), which is what the §11.1.5
    note means by "handover between outputs without needing extra UIs". PHY2's
    numbers do not (2.0 < 10.0), so it schedules a UI to separate the two
    reference edges.

    A mixed pair takes the LARGER of the two sides' defaults: the allocation is a
    property of the bus schedule, not of one device, and the side that needs a UI
    does not stop needing it because the other side would not have.

    The proposed PHY2 revision keeps PHY2's 1 UI; the EDE source allocates 0 UIs.

    SoundWire is not a SWI3S PHY and does not schedule SWI3S handover UIs; it is
    treated as the PHY2-like case (1), which is the conservative reading — see
    the caveat raised alongside a cross-spec contention result.

    Every source in `SPEC_SOURCES` has an entry in `_HANDOVER_UIS`; any other raises
    KeyError.
    """
    return max(_HANDOVER_UIS[man_spec], _HANDOVER_UIS[per_spec])


_HANDOVER_UIS: dict[SpecSource, float] = {
    "SWI3S_PHY1": 0.0,          # intra-UI handover; t_ZD,min >= t_DZ,max
    "SWI3S_PHY2": 1.0,          # one UI explicitly scheduled
    "SWI3S_PHY2_PROP": 1.0,     # the proposal does not change the allocation
    "SWI3S_PHY2_EDE": 0.0,      # EDE's whole purpose: no UI allocated
    "SW13_1V2": 1.0,
}

# Surfaced whenever a contention margin is reported for a bus with a SoundWire
# side: SWI3S's handover-UI allocation is not a SoundWire concept.
HANDOVER_CROSS_SPEC_NOTE = (
    "The handover-UI count is a SWI3S schedule property; SoundWire does not "
    "allocate one. A cross-spec contention margin therefore assumes the SWI3S "
    "side's allocation applies to the whole handover, which is a modelling "
    "assumption rather than a value either spec states."
)

# The same caveat on a bus with SoundWire on both sides, where no SWI3S side exists.
HANDOVER_SW_PAIR_NOTE = (
    "The handover-UI count is a SWI3S schedule property; SoundWire does not "
    "allocate one. With SoundWire on both sides the contention margins take the "
    "handover-UI count as given, which is a modelling assumption rather than a "
    "value the spec states."
)


def fclk_ceiling_MHz(man_spec: SpecSource, per_spec: SpecSource,
                     system_size: SystemSize = "small") -> float:
    """Highest rate a bus with these two sides may legally clock.

    The ceiling is the lower of the two sides' maxima: SWI3S PHY1 6.6 MHz, every PHY2
    source 13.2 MHz, SoundWire 12.3 MHz (small) or 11.0 MHz (large). Every SoundWire
    envelope is below PHY2's mandatory 13.2 MHz, so 13.2 MHz is never legal on a bus with
    a SoundWire side, and PHY1's 6.6 MHz is below both. A margin computed above the
    ceiling is arithmetically fine and physically meaningless.
    """
    return min(preset(man_spec, "Man", system_size).F_CLK_max_MHz,
               preset(per_spec, "Per", system_size).F_CLK_max_MHz)


def launch_mode_max_fclk_MHz(mode: LaunchMode, *, tRF_ns: float = 5.0,
                             Per_t_IH_max_ns: float = 1.5,
                             Per_t_IS_max_ns: float = 4.0,
                             t_PD_mis_ns: float = 0.20,
                             delta_cross_coeff: float | None = None,
                             duty_min: float = 0.45, tau: float = 0.05,
                             anchor_frac: float = 1.0 / 3.0) -> tuple[float, str]:
    """Highest f_clk at which a clock-launched mode closes both MP legs.

    Returns ``(f_max_MHz, binding_leg)``. The two modes fail from OPPOSITE
    ends:

    * 4x is bounded by **hold** — its 0.25*UI launch delay shrinks as the clock
      speeds up, until it no longer covers the receiver's hold requirement.
    * 2x is bounded by **setup** — its 0.5*UI delay consumes half the UI, so
      what remains stops covering t_IS as UI shrinks.

    At the default corner: 4x tops out at ~17.4 MHz (hold), 2x at ~25.1 MHz
    (setup). Below ~17.4 MHz 4x is the better choice — it gives more setup room
    at hold margin that is still adequate; above it, 2x is the only option. This is the
    closed form at this function's own corner; the Timing Calculator's launch hint reads the
    Example column's legs at their worst corner, which bounds 4x lower.

    `delta_cross_coeff` is Δ_cross,MP's coefficient counted from the ramp starts, by default
    the default envelope's (`compute_delta_tpd_envelope().correlated_diff`). Counted from
    the 20 % points the calculator places every edge by, each leg's two crossings sit on edges
    of opposite slew factor (the per-edge slew tolerance `tau`): on hold the Manager's
    reference on a late clock edge and the data anchor on an early data edge, on setup the late
    data edge and the early sampling edge, whose reference is one UI after the launch's. That
    takes `2·tau·anchor_frac·tRF_ns` off what each leg needs.
    """
    frac = _LAUNCH_UI_FRACTION.get(mode)
    if frac is None:
        raise ValueError("analog launch has no UI-relative bound")
    if delta_cross_coeff is None:
        delta_cross_coeff = compute_delta_tpd_envelope().correlated_diff
    dc = delta_cross_coeff * tRF_ns
    ui_for_hold = (Per_t_IH_max_ns + t_PD_mis_ns + dc
                   - 2.0 * tau * anchor_frac * tRF_ns) / frac
    ui_for_setup = (Per_t_IS_max_ns + t_PD_mis_ns + dc
                    - 2.0 * tau * anchor_frac * tRF_ns) / (1.0 - frac)
    if ui_for_hold >= ui_for_setup:
        return 1000.0 * duty_min / ui_for_hold, "hold"
    return 1000.0 * duty_min / ui_for_setup, "setup"


def recommended_launch_mode(f_clk_MHz: float, **kw) -> LaunchMode:
    """The launch mode to prefer at this rate.

    4x while its hold margin holds up, because it leaves far more setup room;
    2x once 4x's hold runs out. Mirrors the design guidance: use 4x at
    12.288 MHz, move to 2x as rates increase.
    """
    f4, _ = launch_mode_max_fclk_MHz(LaunchMode.CLK4, **kw)
    return LaunchMode.CLK4 if f_clk_MHz <= f4 else LaunchMode.CLK2
