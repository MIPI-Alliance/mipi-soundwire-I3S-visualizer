"""Per-side spec sources for the cross-spec (SWI3S ↔ SoundWire 1.3) calculator.

A mixed bus has a Manager built to one spec and a Peripheral built to another.
Each side contributes the timing *its own spec* promises, and each receiver
enforces *its own* setup/hold requirement — but the two specs do not measure the
same quantities the same way, so the raw numbers cannot simply be dropped into
the same slots. This module holds the per-spec presets and the normalisation
that puts both on the common timing graph the inequalities assume.

See ``proposals/cross_spec_mapping.md`` in the timing-analysis repo for the full
derivation. The three divergences that bite, in order of size:

1. **Clock-to-output anchor** (§4.2) — the big one, ~7.2 ns at the baseline.
   SWI3S ``t_DD`` and SoundWire ``t_OV`` are both "clock edge → data ready", but
   they stop at different points on the same slewing waveform. The inequalities
   need a *pure* delay (clock event → start of physical slew, i.e. V=0 / V=V_DD),
   because the slew portion is already accounted for inside Δ_cross / Σ_cross.
   Double-counting it inflates the setup requirement on whichever leg the
   SoundWire device transmits. Handled by :func:`pure_output_delay`.

2. **Threshold representation** (§4.1) — SoundWire's *Data* thresholds
   (0.65/1.0, 0.0/0.35) are guaranteed-recognition levels, not a compliance
   window like SWI3S's V_IH ∈ [0.45, 0.65]. They are the same units but not the
   same kind of quantity, and feeding them into the window slots inflates
   ``correlated_diff`` by ~33 %. Both sides therefore keep the SWI3S-form
   window; see :data:`THRESHOLD_NOTE`.

3. **Hysteresis** (§4.5) — SWI3S requires input hysteresis generally; SoundWire
   scopes it to its Clock pin, because SWI3S's two pins swap clock/data roles
   between Link Control and audio mode while SoundWire's do not. The Schmitt
   assumption is therefore not spec-guaranteed for a SoundWire *Data* receiver.
   Treated globally (assumed OFF for SoundWire Data RX) rather than as a
   per-receiver flag: it is worth ~0.9 ns at the baseline and changes no verdict.
   :func:`schmitt_delta_ns` quantifies it so it can be reported, not hidden.

``t_DZ`` is deliberately NOT reconciled (§4.3): SoundWire ends it at an abstract
high-impedance event and SWI3S at a measurable 10 % V_SEOS departure, which makes
the SWI3S number arrive systematically ~1 ns later for the same driver. Each
spec's published value is used verbatim and the caveat surfaced, rather than
inventing an offset that would look like a spec value.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum
from typing import Literal

from .delta_tpd import RampShape, per_tdd_offset_frac

SpecSource = Literal["SWI3S_PHY1", "SWI3S_PHY2", "SWI3S_PHY2_PROP",
                     "SWI3S_PHY2_EDE", "SW13_1V8", "SW13_1V2"]
SystemSize = Literal["small", "large"]
Corner = Literal["min", "max"]
Side = Literal["Man", "Per"]

SPEC_SOURCES: tuple[SpecSource, ...] = ("SWI3S_PHY1", "SWI3S_PHY2",
                                        "SWI3S_PHY2_PROP", "SWI3S_PHY2_EDE",
                                        "SW13_1V8", "SW13_1V2")

SPEC_LABELS: dict[SpecSource, str] = {
    "SWI3S_PHY1": "SWI3S PHY1",
    "SWI3S_PHY2": "SWI3S PHY2",
    "SWI3S_PHY2_PROP": "SWI3S PHY2 (proposed)",
    "SWI3S_PHY2_EDE": "SWI3S PHY2 (EDE proposed)",
    "SW13_1V8": "SoundWire 1.3 (1.8 V)",
    "SW13_1V2": "SoundWire 1.3 (1.2 V)",
}

# Surfaced in the UI wherever a SoundWire side is selected, so the reader knows
# which threshold definition is in force (§4.1).
THRESHOLD_NOTE = (
    "Thresholds use the SWI3S compliance window on both sides. SoundWire's Data "
    "thresholds (0.65/1.0, 0.0/0.35) are guaranteed-recognition levels, not a "
    "switching-threshold window, so they are not the same kind of quantity and "
    "are not substituted here."
)

# Surfaced wherever a SoundWire t_RF is in force (§4.6).
TRF_NOTE = (
    "SoundWire t_Slew_Data has a min of 2.0 ns and no tabulated max, so "
    "t_Slew_Clock is used as the nearest available bound on the Data lane. "
    "That substitution is an assumption, not a spec value."
)

# Surfaced wherever a mixed bus reports a Z-handover margin (§4.3).
TDZ_NOTE = (
    "t_DZ is used verbatim from each spec. SoundWire ends it at an abstract "
    "high-impedance event, SWI3S at a measurable 10 % V_SEOS departure, so for "
    "the same driver the SWI3S value arrives ~1 ns later. No correction is "
    "applied — reconciling them would require a sourced definition, not an offset."
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
    "Which is better is rate-dependent — see launch_mode_tDD_ns()."
)


class LaunchMode(str, Enum):
    """How the Manager launches its data edge.

    This is an *architectural* choice, not just a tighter number, and the
    distinction is what makes it worth modelling: an analog delay line has a
    min-to-max spread that the inequalities must absorb on both sides, while a
    clock-launched edge is deterministic — min == max — so the spread vanishes.
    Under the proposal that spread is 14 ns (9..23), and removing it is worth
    more than any tightening of the range would be.

    The trade runs in opposite directions on the two MP legs, which is why the
    spec needs both clock options rather than picking one:

    * ``CLK4`` (0.25 UI) leaves the most setup room but the least hold, and is
      bounded by **hold** — 0.25*UI shrinks with the clock period, so it runs
      out at high rates.
    * ``CLK2`` (0.5 UI) is the reverse: ample hold, bounded by **setup**.

    At 12.288 MHz, 4x gives +18.33/+2.52 (setup/hold) against the analog
    +4.48/+2.36 — better on both legs. Above ~17 MHz its hold margin goes
    negative and 2x becomes necessary.
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
    and deliberately a separate function because it means something different and
    is used differently.  The placement says where the DATA edge lands; the
    resolution says which points the Manager can place ANY clocked output on:

        2x   multiples of 0.50 UI  ->  0.00, 0.50
        4x   multiples of 0.25 UI  ->  0.00, 0.25, 0.50, 0.75

    A 4x clock therefore offers three interior points, and t_ZD, t_DZ and t_DD do
    NOT have to pick the same one -- the EDE proposal is exactly such a design
    (t_DD 0.50, t_ZD 0.00, t_DZ 0.50).  A 2x clock offers only 0.50, which is why
    the EDE column's placements had to be checked against a 1/2 UI grid before 2x
    could be recommended.

    Collapsing the two meanings is what made an earlier version of this model force
    every clocked Manager output onto the data edge's point, which is a real
    restriction on the design space and not a conservative simplification.
    """
    return _LAUNCH_UI_FRACTION.get(mode)


def snap_to_launch_grid(value_ns: float, mode: LaunchMode, f_clk_MHz: float,
                        duty_min: float = 0.45, *, floor_ns: float | None = None,
                        ceil_ns: float | None = None) -> float:
    """`value_ns` moved to the nearest point the mode's clock can actually place.

    Analog leaves it alone.  Under a clocked mode an off-grid value describes a
    Manager that cannot be built with the selected clock, which reads as a
    perfectly good number rather than as the impossibility it is -- so it is
    snapped, and the caller reports that it was.

    `floor_ns` is a lower bound the result may not fall below -- pass the row's
    tabulated minimum.  Nearest-point snapping ROUNDS DOWN THROUGH IT otherwise, and
    on a coarse grid it rounds down to zero: the PHY2 revision's Man_t_DD,min of
    9.0 ns is 0.246 UI, so a 2x clock's nearest point is 0.00 UI and the row reads
    0.00 -- a Manager launching data with no delay at all, which the same revision's
    9 ns minimum exists to forbid.  It is also knife-edge: EDE's 0.25 UI seed is
    9.155 ns, which rounds the other way to 0.50 UI, so 0.155 ns of seed decided
    between 0.00 and 18.31.  With a floor the answer is the lowest placeable point
    that still honours the minimum.

    `ceil_ns` is the mirror image and exists for one caller: Table 130's cap on an
    EndDriveEarly release (`ede_release_ceiling_ns`).  Nearest-point snapping rounds
    UP through it just as readily -- a 0.75 UI release on a 1/2 UI grid rounds to
    1.00 UI, the boundary, which the table forbids -- so the result is the HIGHEST
    placeable point that still honours the cap.

    WHERE THE TWO CONFLICT THE CEILING WINS, and that case is not pathological: on a
    1/2 UI grid nothing sits between EDE's 0.75 UI seed and the 0.873 UI cap, which is
    exactly the statement "a 2x clock cannot express EDE".  The ceiling is a
    specification bound while the floor is the row's own low end, so the honest
    rendering is the latest legal point -- and the keeper leg then fails, which is how
    the impossibility reaches the reader as a number rather than as a comment.
    """
    grid = launch_grid_UI(mode)
    if grid is None or f_clk_MHz <= 0:
        return value_ns
    step = grid * 1000.0 * duty_min / f_clk_MHz
    if step <= 0:
        return value_ns
    # Half away from zero, NOT the built-in round(), which is half-to-even: a
    # t_ZD exactly between two points would land on 0.00 UI from one side of the
    # grid and 0.50 UI from the other, which is a coin flip dressed as a rule.
    # A tie is arbitrary either way; this at least makes it the same arbitrary
    # answer every time, and matches what a reader expects "nearest" to mean.
    k = value_ns / step
    snapped = (math.floor(k + 0.5) if k >= 0 else math.ceil(k - 0.5)) * step
    if floor_ns is not None and snapped < floor_ns - 5e-4:
        snapped = math.ceil(floor_ns / step - 5e-4) * step
    if ceil_ns is not None and snapped > ceil_ns + 5e-4:
        snapped = math.floor(ceil_ns / step + 5e-4) * step
    return snapped


# Table 130 bounds an EndDriveEarly release at 0.60 UI + 10 ns from the START of the
# last driven UI.  It is a CEILING on the release placement, and it is the ONLY thing
# that forbids the UI boundary: no inequality does.  A release at 1.00 UI feeds the
# keeper generously and leaves M->P contention comfortable -- the acquiring peripheral
# cannot start at the edge whatever the Manager does, since it owes a flight, a
# detection and its own t_ZD,min -- so a model without this bound reports the boundary
# as a working design, and a 1/2 UI grid as able to express EDE.
_EDE_RELEASE_CEIL_UI_FRAC = 0.60
_EDE_RELEASE_CEIL_TURN_NS = 10.0


def ede_release_ceiling_ns(UI_ns: float) -> float:
    """The latest an EndDriveEarly release may sit, per Table 130.

    31.97 ns at 12.288 MHz, which is 0.873 UI: the 0.75 UI placement clears it and the
    boundary does not.

    Computed rather than written as a UI fraction because the 10 ns turn-off allowance
    is ABSOLUTE, so the cap's position in UI moves with the rate -- and it moves the
    unhelpful way.  As the rate rises the UI shrinks while the 10 ns does not, so above
    ~18 MHz (where 0.40*UI < 10 ns) the cap sits past the boundary and stops binding at
    all; the definition of an early release is what forbids the boundary there.  Below
    that rate this is the tighter bound of the two and the one worth reporting.
    """
    return _EDE_RELEASE_CEIL_UI_FRAC * UI_ns + _EDE_RELEASE_CEIL_TURN_NS


def launch_mode_tDD_ns(mode: LaunchMode, f_clk_MHz: float,
                       duty_min: float = 0.45) -> tuple[float, float] | None:
    """(min, max) Man_t_DD for a clock-launched edge, or None for analog.

    A clock-launched edge lands on a known clock phase, so the min and max
    collapse to the same value — that determinism, not the magnitude, is the
    point. UI matches ``CalcInputs.UI_ns`` (half-period x duty floor).
    """
    frac = _LAUNCH_UI_FRACTION.get(mode)
    if frac is None:
        return None
    t = frac * 1000.0 * duty_min / f_clk_MHz
    return (t, t)


def ui_relative_limit_ns(f_clk_MHz: float, duty_min: float = 0.45,
                         ui_fraction: float = 0.5) -> float:
    """The clock-launched Manager output delay, in ns.

    ``ui_fraction`` is 0.5 for the 2x-CLK form and 0.25 for 4x. UI is the
    half-period times the duty floor, matching ``CalcInputs.UI_ns``.
    """
    return ui_fraction * 1000.0 * duty_min / f_clk_MHz


def is_soundwire(spec: SpecSource) -> bool:
    return spec.startswith("SW13")


def is_ede(spec: SpecSource) -> bool:
    """Whether this source describes the EndDriveEarly proposal.

    EDE is a rate-relative feature: its t_DZ values are UI fractions, so
    `preset()` cannot fill them in without knowing F_CLK. The UI-dependent half
    lives in `ede_ranges()`; this preset carries only the rate-independent part.
    """
    return spec == "SWI3S_PHY2_EDE"


def is_proposed(spec: SpecSource) -> bool:
    """True for a not-yet-ratified SWI3S revision.

    Kept separate from `is_soundwire` because the distinction the UI needs is
    "is this a published number?" — a proposed value must never be presented
    with the same authority as a tabulated one.
    """
    return spec.endswith("_PROP")


# ---------------------------------------------------------------------------
# Clock-to-output normalisation (§4.2)
# ---------------------------------------------------------------------------
#
# Both specs time "output ready" on the same slewing waveform, from different
# anchors. Expressed as a distance from V=0 (rising) / V=V_DD (falling), in
# units of t_RF, where t_RF is the 20 %–80 % slew time:
#
#             V_DD ─────────────────────────── ·······
#                                          ╱   ← SoundWire t_OV stops HERE
#              80 % ····················╱·····   (V_OH, end of valid swing)
#                                    ╱
#              50 % ··············╱···········
#                              ╱
#              20 % ········╱·················   ← SWI3S t_DD stops HERE
#                        ╱                        (V_OL, start of valid swing)
#              V=0 ───╱────────────────────────
#                     ↑
#                     start of physical slew — where a PURE delay must end
#
# Linear ramp (swing = 60 % V_DD, so t_RF spans 20 %→80 %):
#     V=0 → 20 %  = 0.20/0.60         = 1/3   · t_RF   (SWI3S t_DD)
#     V=0 → 80 %  = 0.80/0.60         = 4/3   · t_RF   (SoundWire t_OV)
# Exponential ramp (τ = t_RF/ln 4):
#     V=0 → 20 %  = −ln(0.8)/ln 4     ≈ 0.161 · t_RF
#     V=0 → 80 %  = −ln(0.2)/ln 4     ≈ 1.161 · t_RF
#
# The two anchors differ by exactly 1·t_RF under BOTH ramp shapes — the 20 %–80 %
# span is what t_RF *is*. But the absolute offset back to V=0 is shape-dependent,
# and that is the quantity needed here, so both terms are kept explicit.


def output_anchor_frac(spec: SpecSource, shape: RampShape) -> float:
    """Distance from the start of physical slew to this spec's t_DD/t_OV
    measurement endpoint, in units of t_RF.

    SWI3S t_DD stops at V_OL (20 % V_SEOS); SoundWire t_OV stops at V_OH (80 %),
    a full t_RF later. Returns 0.0 for a value that is already a pure delay.

    FIGURE 174 GOVERNS, NOT TABLE 129'S WORDING. The table says Per_tZD is measured "to the
    start point on the V-Ramp or G-Ramp" and Per_tDD to the "earliest change in data output",
    which read as a 0 % anchor and would make this whole conversion a no-op. The figure shows
    otherwise: both are drawn from the clock's V_IH,rising / V_IL,falling crossing to 20 % OF
    THE RAMP. The table text is inaccurate; the drawing is what the parameters mean. Reviewed
    and settled 2026-08-13 — see `docs/anchors.md`, which also records the argument that was
    made for the other reading so it is not re-litigated from scratch.

    t_DZ IS EXEMPT and is used raw everywhere: it marks the instant the driver goes high-Z,
    which is abrupt. There is no ramp portion inside it to take back out.
    """
    base = per_tdd_offset_frac(shape)          # V=0 → 20 %: 1/3 linear, 0.161 exp
    if is_soundwire(spec):
        return base + 1.0                      # V=0 → 80 %
    return base


# SoundWire's V_TP_Clock threshold band (Table 8). t_OV and t_ZD are measured
# from the clock crossing this, and Table 15's own note on t_OV says "the
# specified limit is a Max, so uses the LATER trigger points on the Clock
# waveform" — so the max edge, 65 %, is the corner a setup bound needs.
_V_TP_CLOCK_MAX = 0.65


def clock_anchor_frac(spec: SpecSource, shape: RampShape) -> float:
    """Clock-side re-anchor, in units of t_RF: how far AFTER the clock's V=0 a
    spec starts counting its clock-to-output delay.

    The model's slot is defined as [clock V=0] → [data V=0] (§3 of the memo:
    "Clock at the TX pin launches at t=0", under the pure-delay convention).
    Putting a tabulated number into that slot is interval arithmetic on one
    timeline::

        [clk V=0 → data V=0] = [clk V=0 → clk anchor]     ← this function
                             + tabulated
                             − [data V=0 → data anchor]   ← output_anchor_frac

    **SWI3S returns 0.0 deliberately.** Its Manager anchors (Fig. 176) are
    symmetric — clock 20 % and data 20 % — so the two offsets are equal and
    cancel, leaving the tabulated value already pure. Its Peripheral case
    (Fig. 174) is asymmetric, but the model's −t_RF/3 there is calibrated
    against the SE reference spreadsheet (58/58) and is NOT re-derived here:
    disturbing a validated path to close a SoundWire-side gap is a bad trade.

    **SoundWire returns V=0 → V_TP_Clock,max**, because t_OV and t_ZD start at
    the clock's threshold crossing rather than the start of its ramp. Omitting it
    understated a SoundWire transmitter's delay by 1.083·t_RF (5.85 ns at
    t_RF = 5.4). It survived the 2026-08-03 data-side correction because that fix
    addressed the *end* anchor and looked complete on its own.

    Same reference plane, so no propagation term: SoundWire v1.3 §5.2.1
    Permission 1 {1010} states the timing parameters are specified "with respect
    to the Clock signal at the external Clock signal node", which for a Manager
    is its own Clock output pin (§5.1.5.1). Both t_OV endpoints are therefore at
    the transmitting device's own boundary, exactly as SWI3S Fig. 176 is, so the
    two are directly comparable and t_PD stays outside on both sides.
    """
    if not is_soundwire(spec):
        return 0.0
    if shape == "linear":
        return _V_TP_CLOCK_MAX / 0.60
    return -math.log(1.0 - _V_TP_CLOCK_MAX) / math.log(4.0)


def pure_output_delay(
    t_spec_ns: float,
    *,
    spec: SpecSource,
    side: Side,
    shape: RampShape,
    corner: Corner = "max",
) -> float:
    """Convert a tabulated clock-to-output time into the pure delay the
    inequalities need (clock event → start of physical slew at the TX pin).

    THE BACK-OUT IS A FIXED OFFSET, NOT A FUNCTION OF THE OPERATING SLEW, and it took a
    reading of Fig. 174 to see why. The tabulated number was MEASURED at the spec's own
    reference test condition, so the slew portion embedded in it is the one realised at
    THAT slew — for SWI3S, `offset · 5.0 ns` = 1.667 ns. Per_t_DD,max,pure is therefore
    18.333 ns and Per_t_DD,min,pure 0.333 ns, t_RF-independent.

    This used to multiply by the OPERATING t_RF, which made the conversion track a
    parameter the measurement cannot depend on: at the 8.3 ns slow corner it backed out
    2.767 ns and reported Per_t_DD,max,pure as 17.233 — 1.1 ns of delay the device never
    saves, handed back on exactly the corner where the leg is thinnest. The operating slew
    is charged, correctly and heavily, by the crossing envelopes; it must not also be
    credited here.

    The reference slew is each SIDE's own (`preset(spec, side).tRF_ns`), which matters on a
    mixed bus. For SoundWire it is the least-bad reading rather than a stated nominal: that
    field is t_Slew_Clock MAX, since the spec publishes no nominal — see the CAUTION on
    `SpecPreset.tRF_ns`.

    Replaces the Peripheral-only hardcode this calculator used while both sides
    were SWI3S. That hardcode was correct then and wrong the moment a side
    becomes SoundWire, in both directions: a SoundWire Peripheral would have
    received SWI3S's −t_RF/3, and a SoundWire Manager no correction at all when
    t_OV needs the larger end-of-slew conversion (§4.2).

    Two values are exempt, for different reasons:

    * The SWI3S **Manager** t_DD is measured between symmetric anchors
      (Fig. 176), so the slew portions cancel and the tabulated number is
      already pure. Per_t_DD (Fig. 174) is asymmetric and is not.

    * A SoundWire **min** corner is ``t_OH_Data``, which is not a
      clock-referenced delay at all. SoundWire v1.3 Table 15 measures it
      "from: V_Data >= V_OH_Data_Min / <= V_OL_Data_Max, to: Data output
      becoming high impedance" — a *duration* the data stays valid, beginning
      where t_OV ends. There is no clock-edge-to-slew-start portion to remove,
      so converting it subtracts a slew that was never in the number. Doing so
      cost 7.2 ns of phantom pessimism on the SW↔SW MP hold margin
      (−2.83 → −10.03) before this was caught.
    """
    if not is_soundwire(spec) and side == "Man":
        return t_spec_ns                       # already pure — anchors cancel
    tRF_ref_ns = preset(spec, side).tRF_ns       # the measurement's condition, not the bus's
    if is_soundwire(spec) and corner == "min":
        # t_ZD_Data IS clock-referenced, but at the clock's V_TP threshold, so it
        # still takes the clock-side re-anchor. No data-side term: it ends at
        # low-impedance, before the ramp, so no slew is embedded in it.
        return t_spec_ns + clock_anchor_frac(spec, shape) * tRF_ref_ns
    return (t_spec_ns
            + clock_anchor_frac(spec, shape) * tRF_ref_ns
            - output_anchor_frac(spec, shape) * tRF_ref_ns)


def schmitt_delta_ns(
    *,
    shape: RampShape,
    tRF_ns: float,
    V_IH_max_frac: float = 0.65,
    V_IL_max_frac: float = 0.55,
    VDD_tol: float = 0.05,
    noise_frac: float = 0.10,
    tRF_tolerance: float = 0.05,
) -> float:
    """Timing cost of NOT assuming a Schmitt-trigger receiver, in ns.

    A Schmitt receiver releases its latch on a falling edge at V_IL; without
    hysteresis the falling crossing is taken at V_IH instead. The two affected
    coefficients (``correlated_diff`` and ``sigma_pm_hold_rf``) each differ by
    exactly that one substitution, so both move by this amount:

        Δ = (V_IH_eff − V_IL_eff) / swing_TX · (1 − τ)

    SoundWire guarantees hysteresis only on its Clock pin, so this is the honest
    cost to quote for a SoundWire Data receiver (§4.5). Exposed as a number
    rather than a flag: it is worth ~0.9 ns at the baseline and changes no
    verdict, which does not justify threading a receiver property through the
    envelope construction — but it should be *reported*, not silently claimed
    as margin.
    """
    vdd_plus = 1.0 + VDD_tol
    vdd_minus = 1.0 - VDD_tol
    V_IH_eff = V_IH_max_frac * vdd_plus + noise_frac
    V_IL_eff = V_IL_max_frac * vdd_plus + noise_frac
    if shape == "linear":
        frac = (V_IH_eff - V_IL_eff) / (0.60 * vdd_minus)
    else:
        ln4 = math.log(4.0)
        x_ih = V_IH_eff / vdd_minus
        x_il = V_IL_eff / vdd_minus
        if not (0.0 < x_il < 1.0 and 0.0 < x_ih < 1.0):
            return math.inf
        frac = (-math.log(x_il) + math.log(x_ih)) / ln4
    return frac * (1.0 - tRF_tolerance) * tRF_ns


# ---------------------------------------------------------------------------
# Presets
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SpecPreset:
    """One side's tabulated timing, before normalisation.

    ``t_DD_*`` is the spec's own clock-to-output number at its own anchor —
    :func:`pure_output_delay` converts it. Everything else is used as-is.
    """
    spec: SpecSource
    label: str
    t_DD_min_ns: float
    t_DD_max_ns: float
    t_IS_max_ns: float
    t_IH_max_ns: float
    t_DZ_max_ns: float
    t_ZD_ns: float
    # CAUTION: this field does NOT mean the same thing on both sides.
    #   SWI3S    5.0 ns  = the spec REFERENCE test condition (a nominal); its
    #                      tabulated range is 3.0-8.3 ns.
    #   SoundWire 5.4 ns = t_Slew_Clock MAX (small/1.8 V); the spec publishes no
    #                      nominal slew at all, only min 2.0 and that max.
    # So a comparison built from presets alone silently pits a SWI3S nominal
    # against a SoundWire worst case. For a worst-corner comparison put each
    # side at its own maximum (8.3 / 5.4) and say so; that is the only worst
    # corner well defined on both sides. See the memo's interop table caveat.
    tRF_ns: float
    V_nom_V: float
    F_CLK_max_MHz: float


# SWI3S v1.1r06 Tables 126 / 128 / 129. Man and Per differ, so both are held.
#
# The two timing tables are structured differently, which matters when reasoning
# about a proposed revision rather than just reading a value:
#
#   Table 128 (INPUT: t_IS, t_IH) is genuinely shared — ONE row per parameter,
#   no PHY1/PHY2 distinction at all.
#
#   Table 129 (OUTPUT) prints every parameter as a separate PHY1 row and PHY2
#   row and never collapses them. Per_tDD, Per_tZD and Man_tZD differ between
#   the PHYs; Man_tDD (7-18), Man_tDZ and Per_tDZ (0-10) are two rows that
#   happen to AGREE — not one shared value. The duplication below is therefore
#   deliberate: if a revision diverges them, each PHY has a row to change.
_SWI3S: dict[tuple[SpecSource, Side], dict] = {
    ("SWI3S_PHY2", "Man"): dict(t_DD_min_ns=7.0, t_DD_max_ns=18.0,
                                t_IS_max_ns=3.0, t_IH_max_ns=0.0,
                                t_DZ_max_ns=10.0, t_ZD_ns=18.0),
    ("SWI3S_PHY2", "Per"): dict(t_DD_min_ns=2.0, t_DD_max_ns=20.0,
                                t_IS_max_ns=4.0, t_IH_max_ns=4.5,
                                t_DZ_max_ns=10.0, t_ZD_ns=18.0),
    ("SWI3S_PHY1", "Man"): dict(t_DD_min_ns=7.0, t_DD_max_ns=18.0,
                                t_IS_max_ns=3.0, t_IH_max_ns=0.0,
                                t_DZ_max_ns=10.0, t_ZD_ns=30.0),
    ("SWI3S_PHY1", "Per"): dict(t_DD_min_ns=14.0, t_DD_max_ns=50.0,
                                t_IS_max_ns=4.0, t_IH_max_ns=4.5,
                                t_DZ_max_ns=10.0, t_ZD_ns=50.0),
    # PROPOSED PHY2 revision — NOT RATIFIED. Rebalances the two failing
    # inequalities by moving headroom from where it is spare to where it is
    # short, rather than by tightening everything:
    #
    #   Man_t_DD  7-18 -> 9-23   min +2 buys MP hold; max +5 spends MP setup
    #   Per_t_IH  0-4.5 -> 0-1.5 a cheaper receiver hold demand: MP hold +3
    #   Per_t_DD  2-20 -> 2-9    Peripheral replies sooner: PM setup +11
    #   Man_t_ZD  2-18 -> 9-23   tracks Man_t_DD (handover variant)
    #   Per_t_ZD  2-18 -> 2-9    tracks Per_t_DD
    #
    # Net at the reference corner: MP hold +5.00, PM setup +11.00, MP setup
    # -5.00. The Manager funds the hold deficit out of setup headroom it
    # already had; the Peripheral's faster t_DD funds PM setup outright.
    #
    # The proposal also offers a rate-relative alternative for the two Manager
    # maxima — "0.5 UI at 2x CLK, 0.25 UI at 4x CLK". That is NOT modelled here:
    # at the mandatory 13.2 MHz, 0.5 UI = 17.05 ns, which is TIGHTER than the
    # 23 ns fixed cap, and the two only cross at 9.78 MHz. Whether a device must
    # meet both or may pick the looser changes the answer, and the wording does
    # not say. See PROPOSAL_UI_RELATIVE_NOTE.
    ("SWI3S_PHY2_PROP", "Man"): dict(t_DD_min_ns=9.0, t_DD_max_ns=23.0,
                                     t_IS_max_ns=3.0, t_IH_max_ns=0.0,
                                     t_DZ_max_ns=10.0, t_ZD_ns=23.0),
    ("SWI3S_PHY2_PROP", "Per"): dict(t_DD_min_ns=2.0, t_DD_max_ns=9.0,
                                     t_IS_max_ns=4.0, t_IH_max_ns=1.5,
                                     t_DZ_max_ns=10.0, t_ZD_ns=9.0),
    # EDE, the combined position: the proposed revision PLUS a clocked Manager
    # release and a peripheral that releases by the UI boundary. t_DZ here is a
    # placeholder -- the real values are UI fractions, supplied by ede_ranges().
    ("SWI3S_PHY2_EDE", "Man"): dict(t_DD_min_ns=9.0, t_DD_max_ns=23.0,
                                    t_IS_max_ns=3.0, t_IH_max_ns=0.0,
                                    t_DZ_max_ns=10.0, t_ZD_ns=23.0),
    ("SWI3S_PHY2_EDE", "Per"): dict(t_DD_min_ns=2.0, t_DD_max_ns=9.0,
                                    t_IS_max_ns=4.0, t_IH_max_ns=1.5,
                                    t_DZ_max_ns=10.0, t_ZD_ns=9.0),
}
_SWI3S_FCLK: dict[SpecSource, float] = {"SWI3S_PHY1": 6.6, "SWI3S_PHY2": 13.2,
                                        "SWI3S_PHY2_PROP": 13.2,
                                        "SWI3S_PHY2_EDE": 13.2}

# SoundWire 1.3 Tables 16 (1.8 V) / 17 (1.2 V), indexed [small, large].
#
# small/large is a SYSTEM-INTEGRATION envelope (C_Bus / L_Bus, Tables 4–5), not a
# device class: requirements {1205}/{1206} oblige a device to meet the table
# whenever the load is inside those limits.
#     small: C_Bus ≤ 60 pF,  L_Bus ≤ 30 cm
#     large: C_Bus ≤ 100 pF, L_Bus ≤ 50 cm
#
# FIVE parameters split by size — t_OV_Data, t_Slew_Clock, t_High_Clock,
# t_Low_Clock, f_Clock. The rest (t_OH_Data, t_DZ_Data, t_ZD_Data, t_Slew_Data,
# t_ISetup, t_IHold) are common to both. Splitting one of the others would be
# inventing a spec value.
_SW13: dict[SpecSource, dict] = {
    "SW13_1V8": dict(t_OV_max_ns=(27.6, 31.6), tRF_ns=(5.4, 9.0),
                     f_Clock_MHz=(12.7, 10.1), t_DZ_max_ns=(4.0, 4.0),
                     t_ZD_ns=(7.9, 7.9), V_nom_V=1.8),
    "SW13_1V2": dict(t_OV_max_ns=(27.9, 29.0), tRF_ns=(5.0, 6.0),
                     f_Clock_MHz=(12.3, 11.0), t_DZ_max_ns=(5.0, 5.0),
                     t_ZD_ns=(8.1, 8.1), V_nom_V=1.2),
}
# Hold-side bound for a SoundWire transmitter: t_ZD_Data,MIN, taken from the
# per-supply table below rather than t_OH_Data.
#
# t_OH_Data (6.7 ns) is the WRONG parameter here and this model used it until
# 2026-08-04. Per SoundWire v1.3:
#   * Table 15 measures t_OH_Data "from data first becoming valid TO Data output
#     becoming high impedance" — data-referenced, not clock-referenced.
#   * It is a Permission ({1209}, §5.2.3), not a Requirement.
#   * It bounds drive-before-handover, not validity at the sampling instant:
#     driving that long "guarantees that the Bus-Keeper can subsequently maintain
#     the signal level up to the point that it is sampled on the subsequent Clock
#     edge" (§5.1.11, Fig. 16). The BUS-KEEPER is the declared hold mechanism.
# t_ZD_Data,Min is "Time to enable Data output signal after positive or negative
# edge on Clock input signal" (Table 15, limit is a Min) — the earliest a driver
# may drive the NEXT value, so it is what bounds destruction of the old level.
#
# SoundWire publishes no t_OV_Data minimum, so t_OV serves the t_DD,MAX slot only.
_SW13_tOH_min_ns_DEPRECATED = 6.7
_SW13_tIS_max_ns = 0.0      # per spec
_SW13_tIH_max_ns = 4.0


def preset(spec: SpecSource, side: Side,
           system_size: SystemSize = "small") -> SpecPreset:
    """Tabulated timing for one side built to ``spec``.

    ``system_size`` applies only to SoundWire; SWI3S does not split by bus
    envelope, so it is ignored for a SWI3S side.
    """
    if is_soundwire(spec):
        t = _SW13[spec]
        i = 0 if system_size == "small" else 1
        return SpecPreset(
            spec=spec,
            label=f"{SPEC_LABELS[spec]} {system_size}",
            t_DD_min_ns=t["t_ZD_ns"][i],      # t_ZD_Data,Min — see the note above
            t_DD_max_ns=t["t_OV_max_ns"][i],
            t_IS_max_ns=_SW13_tIS_max_ns,
            t_IH_max_ns=_SW13_tIH_max_ns,
            t_DZ_max_ns=t["t_DZ_max_ns"][i],
            t_ZD_ns=t["t_ZD_ns"][i],
            tRF_ns=t["tRF_ns"][i],
            V_nom_V=t["V_nom_V"],
            F_CLK_max_MHz=t["f_Clock_MHz"][i],
        )
    v = _SWI3S[(spec, side)]
    return SpecPreset(
        spec=spec,
        label=f"{SPEC_LABELS[spec]} {side}",
        tRF_ns=5.0,                     # spec reference test condition
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

    SoundWire is not a SWI3S PHY and does not schedule SWI3S handover UIs; it is
    treated as the PHY2-like case (1), which is the conservative reading — see
    the caveat raised alongside a cross-spec contention result.
    """
    return max(_HANDOVER_UIS.get(man_spec, 1.0), _HANDOVER_UIS.get(per_spec, 1.0))


_HANDOVER_UIS: dict[SpecSource, float] = {
    "SWI3S_PHY1": 0.0,          # intra-UI handover; t_ZD,min >= t_DZ,max
    "SWI3S_PHY2": 1.0,          # one UI explicitly scheduled
    "SWI3S_PHY2_PROP": 1.0,     # the proposal does not change the allocation
    "SWI3S_PHY2_EDE": 0.0,      # EDE's whole purpose: no UI allocated
    "SW13_1V8": 1.0,
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


def fclk_ceiling_MHz(man_spec: SpecSource, per_spec: SpecSource,
                     system_size: SystemSize = "small") -> float:
    """Highest rate a bus with these two sides may legally clock.

    A mixed bus can only run as fast as its slowest side permits, and every
    SoundWire envelope tops out below SWI3S PHY2's mandatory 13.2 MHz — so a
    SoundWire side always sets the ceiling and 13.2 MHz is never legal on a
    mixed bus. A margin computed above the ceiling is arithmetically fine and
    physically meaningless.
    """
    return min(preset(man_spec, "Man", system_size).F_CLK_max_MHz,
               preset(per_spec, "Per", system_size).F_CLK_max_MHz)


def launch_mode_max_fclk_MHz(mode: LaunchMode, *, tRF_ns: float = 5.0,
                             Per_t_IH_max_ns: float = 1.5,
                             Per_t_IS_max_ns: float = 4.0,
                             t_PD_mis_ns: float = 0.20,
                             delta_cross_coeff: float = 0.9873,
                             duty_min: float = 0.45) -> tuple[float, str]:
    """Highest f_clk at which a clock-launched mode closes both MP legs.

    Returns ``(f_max_MHz, binding_leg)``. The two modes fail from OPPOSITE
    ends, which is the whole reason the spec needs both:

    * 4x is bounded by **hold** — its 0.25*UI launch delay shrinks as the clock
      speeds up, until it no longer covers the receiver's hold requirement.
    * 2x is bounded by **setup** — its 0.5*UI delay consumes half the UI, so
      what remains stops covering t_IS as UI shrinks.

    At the default corner: 4x tops out at ~17.0 MHz (hold), 2x at ~24.6 MHz
    (setup). Below ~17 MHz 4x is the better choice — it gives more setup room
    at hold margin that is still adequate; above it, 2x is the only option.
    """
    frac = _LAUNCH_UI_FRACTION.get(mode)
    if frac is None:
        raise ValueError("analog launch has no UI-relative bound")
    dc = delta_cross_coeff * tRF_ns
    ui_for_hold = (Per_t_IH_max_ns + t_PD_mis_ns + dc) / frac
    ui_for_setup = (Per_t_IS_max_ns + t_PD_mis_ns + dc) / (1.0 - frac)
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
