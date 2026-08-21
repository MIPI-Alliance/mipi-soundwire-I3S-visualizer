"""SWI3S spec calculator: 4 inequalities, t_cross, F_max, term-by-term breakdown.

Used by app/spec_calculator.py.  Independent of emit_numerics.py / the doc
pipeline; the doc keeps using single-shape compute_delta_tpd_envelope.

Per-lane t_RF (Man_CLK, Man_DATA, Per_DATA) and per-device ramp shape
(Man, Per) are first-class inputs.  Inequality structure follows
docs/SWI3S_Timing_Analysis.tex §3 (eqs 1-4) and §6 with V=0/V=V_DD anchor:
t_DD,pure = pure device latency from clock event to start of physical
slew at the TX pin; the slew portion lives in t_cross / Σ_cross.

Each side also carries a *spec source* (``Man_spec`` / ``Per_spec``), so a
mixed SWI3S ↔ SoundWire-1.3 bus can be evaluated in the same inequalities.
The spec only affects how a tabulated clock-to-output number is normalised to
a pure delay — the inequality structure is unchanged, because both specs
describe the same physical event graph.  See :mod:`swi3s_studio.timing.spec_source`
for the normalisation and for the divergences deliberately left unreconciled.
Defaults are SWI3S on both sides, which reproduces the single-spec behaviour
exactly.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from typing import ClassVar, Literal

from .delta_tpd import (
    DeltaTpdEnvelope,
    RampShape,
    compute_delta_tpd_envelope_per_device,
)
from .spec_source import (
    HANDOVER_CROSS_SPEC_NOTE,
    PROPOSAL_NOTE,
    PROPOSAL_UI_RELATIVE_NOTE,
    TDZ_NOTE,
    THRESHOLD_NOTE,
    TRF_NOTE,
    LaunchMode,
    SpecSource,
    SystemSize,
    ede_release_ceiling_ns,
    fclk_ceiling_MHz,
    is_proposed,
    is_soundwire,
    launch_grid_UI,
    launch_mode_tDD_ns,
    pure_output_delay,
    schmitt_delta_ns,
    snap_to_launch_grid,
)

Inequality = Literal["MP_setup", "MP_setup_ho", "MP_hold", "MP_hold_ho",
                     "PM_setup", "PM_setup_ho", "PM_hold", "PM_hold_ho",
                     "PP_setup", "PP_setup_ho", "PP_hold", "PP_hold_ho",
                     "MP_contention", "PM_contention", "PP_contention",
                     "keeper_Man", "keeper_Per"]
# TWO DATA LAUNCH METHODS, ONE SET OF REQUIREMENTS.  t_DD and t_ZD are not a
# parameter and its handover special case -- they are the two ways a device
# launches a data edge, from an already-driving output or out of high-Z, and BOTH
# must satisfy the same setup and the same hold requirement at the receiver.  The
# receiver does not know or care which path produced the edge.
#
# So the per-direction structure is a 2x2, launch method x (setup, hold):
#
#                    launched via t_DD      launched via t_ZD
#     setup          MP_setup               MP_setup_ho
#     hold           MP_hold                MP_hold_ho
#
# and likewise for PM and, as of this revision, PP.  The "_ho" suffix records WHEN the t_ZD path is taken (only
# at a handover) rather than marking it as a lesser case.
#
# The model carried THREE of those four per direction until 2026-08-06: the t_ZD
# hold legs were missing.  That is why the omission was structural rather than an
# oversight about one number, and it is also why the PHY2 revision gives t_DD and
# t_ZD identical values -- "9-23 ns or (0.5 UI 2x CLK, 0.25 UI 4x CLK)" for both.
# They are not similar by choice; they face the same inequalities, so they are
# forced together.  Man_t_ZD,min moved 2 -> 9 ns for exactly the reason
# Man_t_DD,min did: hold at the peripheral.  See MP_hold_ho in `compute`.
#
# The "_contention" trio is a DIFFERENT question from any of the four. The setup
# and hold legs ask whether the acquiring driver's edge is usable at the receiver;
# the contention legs ask whether the two drivers STRESS each other, i.e. whether
# either sees a low-impedance path to an opposing driver. That is bounded by the
# flight time between them and not by zero -- see the criterion note in `compute`.
#
# THE DIVISION OF LABOUR BETWEEN THE TWO FAMILIES IS EXACT, and it is what lets the
# contention legs allow an overlap at all. Contention bounds driver CURRENT. The
# LEVEL on the line during that overlap is the `*_hold_ho` legs' business: the bit
# sampled at the UI-opening edge must stay valid for t_IH,max past it, and the
# acquiring device's turn-on out of high-Z is exactly what ends it. Those two legs
# are live at N_HO = 0 and MP_hold_ho is the thinnest leg in the EDE column, so the
# level is not merely covered in principle -- it is the binding constraint.
#
# THE PP DATA LEGS ARE NEW, and their scope has been corrected once. A peripheral
# RECEIVING while another peripheral drives used to be uncovered on the grounds that
# peripheral-to-peripheral communication is not guaranteed, so no inequality owed it a
# margin -- the standalone tool this file was ported from prices the same pair as
# `setup_pp`/`hold_pp` and labels them HYPOTHETICAL for exactly that reason. They are legs
# here now because the scope widened, and they are worth having even where P->P is not
# promised: what they report is the PRICE of promising it, and on PHY2 as specified that
# price is not paid -- see the PP block in `compute`.
#
# BUT `PP_hold_ho` IS NOT PRICING A PROMISE, IT IS A REQUIREMENT, and reading its name
# instead of its terms is what hid that. The inequality names the device turning ON and the
# device whose hold window ends; the owner of the level at risk does not appear in it. When
# the MANAGER released -- every M->P handover -- that level is Manager-to-peripheral data
# and a peripheral sink is obliged to read it. So the leg binds there, no Manager parameter
# appears in it to fix it, and a handover UI is still owed wherever a peripheral acquires.
# `_PP_DATA_LEGS` in `CalcResults` carries the corrected split and the argument for it.
#
# The keeper pair is ONE constraint -- there is one keeper and it is in the
# Manager ({ASW3805}) -- evaluated per RELEASING device, and it is two entries
# rather than one worst-of-two because a min over legs is not affine and the
# worst-corner search assumes affinity. See the keeper block in `compute`.
INEQUALITIES: tuple[Inequality, ...] = ("MP_setup", "MP_setup_ho",
                                        "MP_hold", "MP_hold_ho",
                                        "PM_setup", "PM_setup_ho",
                                        "PM_hold", "PM_hold_ho",
                                        "PP_setup", "PP_setup_ho",
                                        "PP_hold", "PP_hold_ho",
                                        "MP_contention", "PM_contention",
                                        "PP_contention",
                                        "keeper_Man", "keeper_Per")
# ---------------------------------------------------------------------------
# ADDING AN INEQUALITY: the checklist, and where each item is enforced
# ---------------------------------------------------------------------------
#
# Every style drift in this file so far was invented in good faith by copying a neighbouring
# leg that happened to be the odd one out -- `X_PM,setup,clk` beside Delta's `X_MP,late`, an
# unsubscripted `X_PP`, `Man_tKeeper_Response` spelled the spec's way, and a P->P leg reusing
# the M->P crossing tuples and charging a lane no Manager drives. Prose in a style guide would
# not have stopped any of them, so each rule below is a TEST that fails, and this list exists
# to say where.
#
#   1. SYMBOLS.  `X_<pair>,<role> · <device>_t_RF,<LANE>`; role is late/early (setup is eroded
#      by a late crossing, hold by an early one) and it is MANDATORY and LAST -- P->P's device
#      letter goes BEFORE it and narrows it rather than replacing it. The lane is ALWAYS
#      qualified. Every symbol must subscript -- the underscore before a token is the marker.
#      A leg that reads ONE lane twice shows the role `net` instead, its two contributions
#      gathered into a single product (`_gather_lane_terms`) because one pin has one slew.
#      -> test_every_displayed_symbol_follows_one_naming_scheme (tests/test_timing.py)
#
#   2. LANES.  A leg may only read the lanes it has business reading: no Man DATA in a P->P
#      leg, because no Manager drives that link. Wrong-lane bugs are CONSISTENT -- symbol,
#      factors and behaviour all agree on the wrong row -- so only a declared table catches
#      them. Adding a leg means adding a row and defending it; that is the review step.
#      -> _LEGAL_LANES + test_no_leg_reads_a_lane_it_has_no_business_reading
#
#   2b. TWO PARTS SHARING A ROW.  Where a leg has two INDEPENDENT devices on it, a row read
#      TWICE with opposite signs must be cornered per device -- one shared value cannot be
#      both the charge and the credit. That is the clock lane on P->P (`tRF_CLK_tol_ns`,
#      2.6 ns on hold). A row read ONCE needs nothing: the ordinary corner search already
#      drives it to the end that hurts, which is why the device parameters carry the device
#      in their NOTE and not in the symbol -- a `Per_A_t_DD` would point at a row the input
#      grid does not have.
#      -> test_no_pp_leg_reads_one_per_row_for_both_devices (tests/test_timing.py)
#
#   3. ARITHMETIC.  The terms shown must sum to the margin reported. Setup legs compute the
#      two from separate expressions, so they can silently disagree.
#      -> test_every_leg_displays_the_margin_it_reports (tests/test_timing_cross_spec.py)
#
#   4. CORNERS.  The margin must be AFFINE in each row, or `find_worst_corner_rows` -- which
#      moves one row at a time -- is no longer exact. A term that multiplies two rows breaks
#      it (see `CalcInputs.pp_placement`, which is a selector for exactly this reason), and so
#      does a min/max over legs (see the keeper split).
#      -> the brute-force checks in tests/test_timing_cross_spec.py
#
#   5. ZEROS.  Three kinds, and they are not interchangeable: structurally zero (`0·t_PD`,
#      dropped), nothing-allocated (`0·UI`, dropped), and a PARAMETER that happens to read
#      zero (`Man_t_IH`, `Per_t_DZ` at its min -- SHOWN, because the value is the spec's).
#
#   6. THE REFERENCE.  If the leg exists in `timing-analysis/swtiming/emit_ede.py`, it is
#      compared against it and any delta must be declared.
#      -> tests/test_timing_vs_reference.py, and docs/anchors.md for what is deliberate
#
# When one of these rules CHANGES, the tests above are the list of places to review: each
# names the legs it covers, so "does this apply elsewhere?" is answered by running them rather
# than by remembering.

Pick = Literal["min", "typ", "max"]

# Where the two peripherals sit, for the P->P data legs. See `CalcInputs.pp_placement`.
#
# FOUR PLACEMENTS, because two ends and two devices is four and the enumeration should come
# from the geometry rather than from the cases that happened to be interesting. `both_far` was
# missing at first and read as "not modelled" when the answer is simply that it costs nothing.
#
# The key `unknown` is the PERSISTED token and does not change with the label -- it is written
# into saved sessions and read back by `set_from_dict`.
PPPlacement = Literal["unknown", "both_near", "driver_near", "driver_far", "both_far"]
PP_PLACEMENT_LABELS: dict[str, str] = {
    "unknown": "Worst Per Leg",
    "both_near": "both near the Manager",
    "driver_near": "driver near, sampler far",
    "driver_far": "driver far, sampler near",
    "both_far": "both far from the Manager",
}


def pp_net_pd(placement: str, *, is_setup: bool) -> float:
    """The P->P flight bracket in t_PD traversals, for one placement and one leg.

    THREE of the four placements cost nothing and one costs two traversals; "unknown" hands
    each leg the end that hurts it, which is 2 for setup (the bracket is a cost) and 0 for hold
    (it is a credit). Returning a COUNT rather than nanoseconds keeps the bus length a row the
    corner search owns -- see the note on `CalcInputs.pp_placement`.

    An unrecognised placement falls through to the "unknown" bounding answer rather than to a
    zero, so a key added to `PPPlacement` and forgotten here is conservative instead of free.
    """
    if placement == "driver_far":
        return 2.0
    if placement in ("both_near", "driver_near", "both_far"):
        return 0.0
    return 2.0 if is_setup else 0.0


# ---------------------------------------------------------------------------
# Parameter ranges (a typed view over min/typ/max for one spec parameter)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ParamRange:
    """min/typ/max numeric values for one spec parameter."""
    min: float
    typ: float
    max: float

    def at(self, pick: Pick) -> float:
        return {"min": self.min, "typ": self.typ, "max": self.max}[pick]


# ---------------------------------------------------------------------------
# Calculator inputs (one column: Spec or Proposed, evaluated at one corner)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CalcInputs:
    """All scalar inputs needed to evaluate the 4 SWI3S inequalities."""

    # Voltage thresholds (fractions of V_SEOS)
    V_OH_min_frac: float = 0.80
    V_OL_max_frac: float = 0.20
    V_IH_min_frac: float = 0.45
    V_IH_max_frac: float = 0.65
    V_IL_min_frac: float = 0.35
    V_IL_max_frac: float = 0.55

    # Supply
    V_SEOS_nom_V: float = 1.2
    V_SEOS_tol_frac: float = 0.05            # δ

    # Noise budget (α = noise_pp / V_SEOS_nom)
    V_noise_pp_frac: float = 0.10            # α

    # Slew tolerance (τ)
    tRF_tolerance_frac: float = 0.05

    # t_RF per lane (ns) — Mgr CLK/DATA may differ; Per CLK doesn't exist (Mgr drives CLK)
    tRF_Man_CLK_ns: float = 5.0
    tRF_Man_DATA_ns: float = 5.0
    tRF_Per_DATA_ns: float = 5.0
    # Ramp shape per device
    Man_shape: RampShape = "linear"
    Per_shape: RampShape = "linear"

    # Output timing (ns)
    Man_t_DD_max_ns: float = 18.0
    Man_t_DD_min_ns: float = 7.0
    Per_t_DD_max_ns: float = 20.0
    Per_t_DD_min_ns: float = 2.0

    # Input timing requirements (ns)
    Man_t_IS_max_ns: float = 3.0
    Man_t_IH_max_ns: float = 0.0
    Per_t_IS_max_ns: float = 4.0
    Per_t_IH_max_ns: float = 4.5

    # Z-handover (ns). t_ZD is a single parameter (min/typ/max range); the setup
    # handover inequalities use its max corner, the CONTENTION one its min.
    Man_t_DZ_max_ns: float = 10.0
    Per_t_DZ_max_ns: float = 10.0
    Man_t_ZD_ns: float = 10.0
    Per_t_ZD_ns: float = 10.0

    # UIs explicitly allocated to a handover, i.e. how far apart the releasing
    # driver's t_DZ reference edge and the acquiring driver's t_ZD reference edge
    # are.  This is a PHY property with a programmable default, NOT a constant:
    # PHY1 hands over intra-UI (0), PHY2 schedules one UI (1), and both may be
    # programmed longer for a larger system (§11.1.1.1).  Seeded per-PHY by
    # `default_handover_UIs`; overridable because "programmed longer" is exactly
    # the case a designer wants to explore.
    handover_UIs: float = 1.0

    # EndDriveEarly, per side.  The two classes reach it by DIFFERENT MECHANISMS,
    # and that is the whole reason these are two flags rather than one:
    #
    #   Manager     clocks a mid-UI release. Its t_DZ is referenced to the START of
    #               its last driven UI instead of the end -- one UI earlier -- so
    #               the tabulated value stays positive (Table 130's convention) and
    #               `_effective_t_dz_ns` applies the shift.
    #
    #   Peripheral  has no mid-UI reference (Sec. 11.1.5), so it CANNOT aim at a
    #               clock instant. It holds its own COMPLETED output level for
    #               `Per_ede_hold_*` ns and then releases -- referenced to its own
    #               ramp, not to any edge. See `_per_ede_release_vs_edge_ns`.
    #
    # Modelling the peripheral's as a t_DZ shift was wrong and was optimistic by
    # 15.4 ns on PM contention: it put the release exactly ON the boundary and then
    # also picked up the clock-detection lead the ramp already contains.
    Man_ede: bool = False
    Per_ede: bool = False

    # The peripheral's EDE hold interval: how long it holds the completed level
    # before going high-Z.  A DELAY, measured from its own ramp completion, which is
    # why it is an input in its own right rather than a t_DZ value -- there is no
    # clock-referenced number that expresses it.
    #
    # The minimum is what the keeper leg reads (the earliest release starves it) and
    # it happens to equal Man_tKeeper_Response, so the peripheral satisfies the
    # keeper BY CONSTRUCTION. The maximum is what every contention leg reads.
    Per_ede_hold_min_ns: float = 3.0
    Per_ede_hold_max_ns: float = 9.0

    # Man_tKeeper_Response, Table 125: THE DATA LANE MUST BE STABLE AT THE MANAGER PIN
    # FOR AT LEAST THIS LONG.  So it is a DURATION of settled level ending at the release,
    # not a delay after it: the keeper has nothing to latch otherwise, and the bus floats
    # at an indeterminate level through the sample point.
    #
    # "At the Manager pin" is where the keeper is, and it is why the keeper legs carry no
    # t_PD even for a releasing PERIPHERAL: the window arrives t_PD late but its LENGTH is
    # unchanged, and the length is what the table bounds.  That does assume the level is
    # settled at the far pin as soon as it arrives -- ringing/reflection is not modelled
    # here, so a line that needs settling time eats into this window unmodelled.
    t_keeper_ns: float = 3.0

    # P->P PLACEMENT. A peripheral references its launch to ITS OWN clock arrival and the
    # sampler its edge to its own, so what a P->P leg pays is the bracket
    # `t_clk(driver) + t_data(driver->sampler) − t_clk(sampler)`. Two ends and two devices is
    # FOUR placements, and on a shared trace the bracket takes only two values:
    #
    #   both peripherals near the Manager      0        nothing to travel
    #   driver near, sampler far               0        the data CHASES the clock down the
    #                                                   same line at the same speed, so it
    #                                                   arrives exactly as the clock does --
    #                                                   the reason MP setup pays no t_PD
    #   both peripherals FAR                   0        t_PD + 0 − t_PD: both take the clock
    #                                                   equally late, and the data has no
    #                                                   distance left to cover between them
    #   driver FAR, sampler near             2·t_PD     the data runs back against the clock
    #
    # `both_far` is listed even though it is one of the three that cost nothing, because the
    # enumeration should be the geometry: absent, it read as a case not modelled rather than as
    # a case that is free. The two that matter are still driver-far and everything else.
    #
    # The bracket is a COST for setup and a CREDIT for hold, so one placement cannot be
    # worst for both: "Worst Per Leg" (the default) gives each leg its own worst — setup 2,
    # hold 0 — and a pinned layout gives both legs that layout's bracket.

    #
    # A SELECTOR AND NOT A CORNER ROW, which was the first attempt and was wrong. As a row
    # its value multiplies the bus-length row, and `find_worst_corner_rows` moves one row at
    # a time -- exact only for a margin AFFINE in each parameter. A product of two rows is
    # bilinear, and the search duly missed the worst corner: it left the bus at 15 cm on
    # PP_setup where 30 cm is 2 ns worse, because with the placement still at its typical of
    # zero the leg looked insensitive to bus length. Same failure mode the keeper split
    # documents. As a selector the count is a constant per (placement, leg), the search sees
    # only the bus row, and affineness is restored.
    pp_placement: PPPlacement = "unknown"

    # Bus & topology
    bus_length_cm: float = 30.0
    vPCB_cm_per_ns: float = 15.0
    vPCB_err_frac: float = 0.10              # peak-to-peak trace mismatch as fraction of t_PD

    # Operating point
    F_CLK_target_MHz: float = 13.2
    duty_min: float = 0.45

    # Spec source per side.  Defaults reproduce the single-spec SWI3S behaviour
    # exactly.  Only the clock-to-output normalisation is spec-dependent; the
    # inequalities are not (see spec_source).
    Man_spec: SpecSource = "SWI3S_PHY2"
    Per_spec: SpecSource = "SWI3S_PHY2"
    # SoundWire's system-size envelope (C_Bus / L_Bus, Tables 4-5).  Ignored for
    # a SWI3S side, which does not split by bus envelope.
    system_size: SystemSize = "small"

    # How the Manager launches its data edge.  ANALOG uses the Man_t_DD range
    # as given; a clock-launched mode REPLACES that range with a deterministic
    # fraction of UI, collapsing min and max to one value.  Removing the
    # min-to-max spread is the point — an analog delay line forces the
    # inequalities to absorb it on both sides at once.  Only meaningful for a
    # SWI3S Manager; a SoundWire one has no such option.
    launch_mode: LaunchMode = LaunchMode.ANALOG

    # ---- derived ----

    def is_cross_spec(self) -> bool:
        """True when the two sides are built to different specs."""
        return self.Man_spec != self.Per_spec

    def F_CLK_ceiling_MHz(self) -> float:
        """Highest rate this pair of sides may legally clock."""
        return fclk_ceiling_MHz(self.Man_spec, self.Per_spec, self.system_size)

    def F_CLK_is_legal(self) -> bool:
        """Whether the target rate is inside both sides' ceilings.

        A margin computed above the ceiling is arithmetically fine and
        physically meaningless.  Every SoundWire envelope tops out below
        SWI3S PHY2's mandatory 13.2 MHz, so a mixed bus can never run there.
        """
        return self.F_CLK_target_MHz <= self.F_CLK_ceiling_MHz() + 1e-9

    def t_PD_ns(self) -> float:
        return self.bus_length_cm / self.vPCB_cm_per_ns

    def t_PD_mis_ns(self) -> float:
        return self.vPCB_err_frac * self.t_PD_ns()

    def UI_ns(self) -> float:
        """Half-period · duty floor at the target F_CLK."""
        return 1000.0 * self.duty_min / self.F_CLK_target_MHz

    def envelope(self) -> DeltaTpdEnvelope:
        return compute_delta_tpd_envelope_per_device(
            Man_shape=self.Man_shape,
            Per_shape=self.Per_shape,
            V_OH_frac=self.V_OH_min_frac,
            V_OL_frac=self.V_OL_max_frac,
            V_IH_min_frac=self.V_IH_min_frac,
            V_IH_max_frac=self.V_IH_max_frac,
            V_IL_min_frac=self.V_IL_min_frac,
            V_IL_max_frac=self.V_IL_max_frac,
            VDD_tol=self.V_SEOS_tol_frac,
            noise_budget_V=self.V_noise_pp_frac * self.V_SEOS_nom_V,
            V_nom_V=self.V_SEOS_nom_V,
            tRF_tolerance=self.tRF_tolerance_frac,
        )


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Term:
    """One term in a margin breakdown.  ``value_ns`` is signed (positive
    adds to the margin, negative subtracts) and feeds margin sums.
    ``op`` ('+' or '-') is the inequality-level sign of the term, kept
    explicit so equation rendering shows the right operator even when
    the numerical value is zero (e.g. Man_t_IH = 0 in PM_hold).
    ``note`` records provenance for a term whose value was converted from
    its tabulated form — e.g. a SoundWire t_OV normalised to a pure delay.
    It makes a cross-spec divergence visible where it is *used*, instead of
    only in the design document.

    NO CORNER IN A SYMBOL WHERE A ROW OWNS THE CORNER.  ``symbol`` names the
    PARAMETER, not which end of it is in force: `Man_t_DD`, not `Man_t_DD,max`.
    Choosing the end is `find_worst_corner_rows`' job, and it does it per
    inequality — the same field is pulled to opposite ends by the keeper and the
    contention legs — so a suffix baked into the symbol asserts a corner that may
    not be the one displayed beside it.  It is worse than redundant: `apply_row_picks`
    sets EVERY field a row binds to the same picked value, so `Man_t_DD,min` and
    `Man_t_DD,max` show the identical number while claiming to be two things, and a
    `,min` on a parameter the spec gives only a maximum for (t_DZ) invents a
    guarantee.  Enforced by
    `test_no_term_symbol_names_a_corner_the_picker_owns`.

    Suffixes that are NOT corners stay: `,pure` (an anchor conversion), `,EDE` (a
    different definition of the parameter), and a corner on a field NO row binds --
    `Per_t_hold,min` and the `V_OH,min`/`V_OL,max` inside the swing term, which the
    picker never touches, so naming their end is a fact rather than a claim.
    """
    symbol: str
    value_ns: float
    op: Literal["+", "-"] = "+"
    note: str = ""
    # (coefficient, operand) for a term that IS a product, so the numeric substitution can
    # show `1.4414·6.00` where the symbol says `X_MP,late · Man_t_RF,DATA`.
    #
    # SET IT WHERE AN OPERAND'S VALUE WOULD OTHERWISE APPEAR NOWHERE ON THE ROW. The rule
    # used to be "where the coefficient is not already visible in the symbol", which read
    # the wrong half of the product: `1.67·Man_t_RF` does show its coefficient, and that is
    # exactly why the t_RF in force was invisible — the row printed the product (8.33) and
    # named the multiplier, so the one number a reader wants to check, 5.00 ns of slew,
    # could only be found by dividing. Both crossing and swing terms now expand.
    #
    # Costs the same two-decimal rounding the crossing terms already carry: 1.67·5.00 reads
    # 8.35 against the 8.33 in force, because the coefficient is 1/(V_OH,min−V_OL,max) shown
    # to two places, not a rounded input. The term's note carries the exact arithmetic.
    # THE ROW VALUE IS ALWAYS LAST, the coefficients before it. Two entries is the ordinary
    # `coefficient x t_RF`; three is a GATHERED pair (`(a - b) x t_RF`, see
    # `_gather_lane_terms`), whose coefficients are signed relative to the term's own hoisted
    # operator. Everything that asks "which row does this term read?" uses `factors[-1]`.
    factors: tuple[float, ...] | None = None


@dataclass(frozen=True)
class InequalityBreakdown:
    name: Inequality
    terms: list[Term]
    required_ns: float          # sum of consumed terms (UI side); 0 for hold inequalities
    margin_ns: float            # sum of all signed terms
    F_max_MHz: float            # bound implied by required UI; math.inf if F_CLK-independent
    is_setup: bool              # True for setup, False for hold

    def __post_init__(self) -> None:
        # SAME-ROW CROSSINGS ARE GATHERED HERE, for every leg, because a leg cannot opt out
        # of physics: `Man_t_RF,CLK` is one pin and two products of it are one product. Doing
        # it at construction rather than at each of the seventeen build sites is what makes
        # that true of a leg added later as well. Margin-preserving by construction, so the
        # separately-computed `required_ns` / `margin_ns` stay valid — asserted by
        # test_every_leg_displays_the_margin_it_reports.
        object.__setattr__(self, "terms", _gather_lane_terms(self.terms))


@dataclass(frozen=True)
class CalcResults:
    inputs: CalcInputs
    # Δ_cross,MP is reported per DIRECTION, not once: the late crossing sits on
    # the data lane for setup and on the clock lane for hold, so the two differ
    # whenever the Manager's per-lane t_RF differ.  Equal when the lanes match.
    delta_cross_MP_setup_ns: float
    delta_cross_MP_hold_ns: float
    sigma_pm_setup_ns: float
    sigma_pm_hold_ns: float
    breakdowns: dict[Inequality, InequalityBreakdown]
    # Cross-spec reporting.  Both are advisory: they qualify the margins rather
    # than changing them.
    #
    # ``F_CLK_ceiling_MHz`` is the highest rate this pair of sides may legally
    # clock.  A margin computed above it is arithmetically fine and physically
    # meaningless — every SoundWire envelope tops out below SWI3S PHY2's
    # mandatory 13.2 MHz, so a mixed bus can never run at the SWI3S rate.
    #
    # ``schmitt_cost_ns`` is what the two Schmitt-dependent coefficients would
    # lose if the receiver has no input hysteresis.  SoundWire guarantees it
    # only on its Clock pin, so for a SoundWire Data receiver this margin is
    # not spec-guaranteed.  Reported rather than silently claimed (§4.5).
    F_CLK_ceiling_MHz: float = math.inf
    F_CLK_legal: bool = True
    schmitt_cost_ns: float = 0.0
    caveats: tuple[str, ...] = ()

    # THE HEADLINE RATE IS THE GUARANTEED BUS'S RATE, so the legs that only exist to price an
    # unguaranteed promise are excluded from it while keeping their own rows and margins.
    #
    # THREE OF THE FOUR P->P LEGS ARE SUCH LEGS. `PP_setup`, `PP_setup_ho` and `PP_hold` all
    # require peripheral B to READ a level peripheral A drove in order to be read, and the spec
    # does not promise that one peripheral can read another's data. Left in the bounding set, a
    # failing one pins `F_max_binding` to 0.0 for every configuration and names itself the
    # binding constraint, reporting "no clock rate works" for a bus whose guaranteed directions
    # are fine.
    #
    # `PP_hold_ho` IS NOT ONE OF THEM, AND THAT IS A CORRECTION. Read its terms: the only
    # devices in the inequality are the one turning ON out of high-Z and the one whose hold
    # window that ends. THE DEVICE THAT DROVE THE LEVEL AT RISK DOES NOT APPEAR -- it is, as the
    # per-leg note below says, "a third device, which appears in this leg only as the owner of
    # the level at risk". So the leg does not assert that B reads A's data; it asserts that B's
    # turn-on destroys a level A is still obliged to hold, and WHOSE level that is depends on
    # who released:
    #
    #   a peripheral released   the level is peripheral data -> not guaranteed, out of scope
    #   the MANAGER released    the level is Manager->peripheral data, which every peripheral
    #                           sink MUST read -> IN SCOPE, and this leg is what bounds it
    #
    # The second case is every M->P handover at N_HO = 0. Excluding the leg therefore excused a
    # requirement on guaranteed traffic on the strength of the leg's NAME, and both this model
    # and the reference analysis did it. An out-of-scope declaration is a claim about an
    # inequality's TERMS and has to be checked against them; `PP_hold` keeps the exclusion
    # because for that one the claim is true.
    #
    # If P->P data ever becomes guaranteed, delete `_PP_DATA_LEGS` and this note with it.
    _PP_DATA_LEGS: ClassVar[frozenset[str]] = frozenset(
        {"PP_setup", "PP_setup_ho", "PP_hold"})

    def _bounding(self) -> dict:
        return {k: b for k, b in self.breakdowns.items() if k not in self._PP_DATA_LEGS}

    @property
    def F_max_binding_MHz(self) -> float:
        """min over the bounding inequalities. Hold inequalities contribute inf if
        passing, 0 if failing (they don't bound F_CLK directly)."""
        vals = []
        for _ineq, b in self._bounding().items():
            if b.is_setup:
                vals.append(b.F_max_MHz)
            else:
                vals.append(math.inf if b.margin_ns >= 0 else 0.0)
        return min(vals) if vals else 0.0

    @property
    def binding_inequality(self) -> Inequality:
        """The inequality with the smallest setup F_max (or first failing hold)."""
        # Failing hold trumps anything else
        for ineq, b in self._bounding().items():
            if (not b.is_setup) and b.margin_ns < 0:
                return ineq
        # Otherwise pick smallest setup F_max
        setup_items = [(ineq, b.F_max_MHz) for ineq, b in self._bounding().items() if b.is_setup]
        return min(setup_items, key=lambda kv: kv[1])[0]


# ---------------------------------------------------------------------------
# compute()
# ---------------------------------------------------------------------------

def _releaser_ede(inp: CalcInputs, releasing: str) -> bool:
    return inp.Man_ede if releasing == "Man" else inp.Per_ede


def _swing_ns(inp: CalcInputs, tRF_ns: float) -> float:
    """Rail-to-rail transition time, from a t_RF that spans V_OL..V_OH.

    t_RF is a 20-80 % measurement (Table 127), so the FULL swing is 5/3 of it at the
    default thresholds. This is the addend the keeper legs need: the keeper is a
    weak holder and cannot finish a transition, so the driver's whole swing has to
    complete before it lets go -- not just the t_RF portion, which stops at V_OH.

    NOT A NEW PARAMETER, and the keeper legs name it as what it is --
    `t_RF/(V_OH,min-V_OL,max)` -- rather than as a `t_swing` of its own. Every piece
    is a parameter the rest of the model already carries, and a coined symbol in a
    displayed inequality reads as a quantity the spec defines when it is arithmetic on
    two that it does. The same objection retired `per_clock_detect`.
    """
    span = inp.V_OH_min_frac - inp.V_OL_max_frac
    return tRF_ns / span if span > 0 else 0.0


def _swing_note(inp: CalcInputs, tRF_ns: float) -> str:
    """Provenance for a swing term: which t_RF, and what fraction it spans."""
    span = inp.V_OH_min_frac - inp.V_OL_max_frac
    if span <= 0:
        return ""
    return (f"t_RF {tRF_ns:.2f} ns is a "
            f"{inp.V_OL_max_frac * 100:.0f}-{inp.V_OH_min_frac * 100:.0f} % "
            f"measurement (Table 127), so it spans {span:.2f} of the swing → "
            f"rail-to-rail {tRF_ns / span:.2f} ns")


def _release_note(t_dz_ns: float) -> str:
    """Provenance for a keeper leg's t_DZ term.

    The keeper wants the EARLIEST release, so the corner search pulls the one
    `*_t_DZ_max_ns` field to its row's low end here and to the high end in the
    contention legs. Worth saying on screen, because the field's NAME carries `max`
    while this leg is reading the other end of its row -- and because at a low end of
    0 the term vanishes, which should read as "this device may release the instant its
    UI closes" rather than as a missing term.
    """
    if t_dz_ns <= 5e-4:
        return ("earliest release: t_DZ = 0, i.e. the device may go high-Z the instant "
                "its UI closes, leaving the keeper nothing past the edge")
    return (f"earliest release: driving continues {t_dz_ns:.2f} ns into the next UI "
            f"before high-Z")


def _handover_ui_terms(inp: CalcInputs, UI: float) -> list["Term"]:
    """The allocated handover UIs as a term — or NO TERM when none are allocated.

    N_HO = 0 used to render as `0·UI`, kept on the argument that a term which becomes
    `1·UI` the moment an allocation is made says something a missing term does not. It
    still does, but it says it in the wrong place: a row of `+ 0·UI` in a displayed
    inequality is arithmetic the reader has to verify contributes nothing, on the leg where
    the whole point is that NOTHING separates the release from the turn-on. With no
    handover UI scheduled there is no handover UI in the equation.

    So the drop rule is now: a term contributing zero is dropped whether it is
    structurally zero (`0·t_PD`, zero for every input) or zero because nothing is
    allocated (`0·UI`). What stays is a PARAMETER that happens to read zero on this spec —
    `Man_t_IH` — because there the zero is the spec's value and worth seeing.

    Numerically a no-op: the term it drops is worth 0.0 ns, so no margin moves.
    """
    if inp.handover_UIs <= 0.0:
        return []
    return [Term(f"{inp.handover_UIs:g}·UI", +inp.handover_UIs * UI, op="+")]


def _pp_device_note(who: str, what: str, base: str = "") -> str:
    """Whose parameter a P->P device term is — in the NOTE, never in the symbol.

    A P->P leg reads two peripherals' device parameters (A launches, B samples) and the
    symbols do not letter them, which looks like an omission beside `X_PP,A,late` /
    `X_PP,B,late` next to them.

    A AND B ARE INDEPENDENT PARTS, INCLUDING IN THEIR DEVICE PARAMETERS. Each realises its own
    Per_t_DD, its own Per_t_IS, inside the spec's limits, exactly as each realises its own
    slew inside `tRF_CLK_tol_ns`. That is a fact about the hardware and not a modelling
    choice, and an earlier version of this note denied it -- claimed a letter would advertise
    a per-device corner "this model does not offer" -- which was simply wrong.

    WHAT IS TRUE IS THAT THE INDEPENDENCE DOES NOT BIND HERE. It costs something only when ONE
    row is read TWICE on one leg with opposite signs, because then a single value has to serve
    as both the charge and the credit and the shared corner flatters the leg. That is precisely
    the clock lane: `X_PP,A,late` and `X_PP,B,early` are both `Man_t_RF,CLK`, so each is pinned
    to its own end of the band, worth 2.6 ns on P->P hold. Every DEVICE parameter appears at
    most once per leg -- A's launch and B's sampling window are different rows -- so the corner
    search already gives each the end that hurts, and splitting the row would change no number.

    So the letter is withheld for the reason a symbol is withheld anywhere in this file: it
    would point at a row that does not exist. There is one `Per t_DD` in the input grid, and a
    term reading `Per_A_t_DD` sends the reader looking for `Per_B_t_DD` to edit beside it. The
    role goes in the note, where it says whose parameter it is without implying a second row.

    THE TRIP-WIRE, because this reasoning expires the moment it stops holding: if a P->P leg is
    ever added that reads one Per row for BOTH devices, the independence starts binding and
    that leg needs a band like the clock's. `test_no_pp_leg_reads_one_per_row_for_both_devices`
    fails when it happens rather than leaving it to be noticed.
    """
    lead = f"{who}: {what}"
    return f"{lead} — {base}" if base else lead


def _keeper_note(inp: CalcInputs) -> str:
    """Provenance for the Man_tKeeper_Response term.

    Quotes what the table REQUIRES rather than what the term costs, because the number is
    a demand on the wire and not a delay of the device: Table 125 asks for a settled level
    at the Manager pin, and this term is the tail of that window ending at the release.
    """
    return (f"Table 125: the data lane must be stable at the Manager pin for "
            f"{inp.t_keeper_ns:.2f} ns before the driver lets go")


def _lane_ns(inp: CalcInputs, lane: str) -> float:
    """The t_RF in force on a named lane."""
    return {"Man CLK": inp.tRF_Man_CLK_ns,
            "Man DATA": inp.tRF_Man_DATA_ns,
            "Per DATA": inp.tRF_Per_DATA_ns}[lane]


def _cross_terms(inp: CalcInputs, base_sym: str,
                 lanes: tuple[CrossLane, ...]) -> list[Term]:
    """A crossing envelope as one `coefficient · t_RF` term PER LANE.

    `lanes` is (role, t_RF lane, POSITIVE dimensionless coefficient, operator) per leg
    of the envelope -- the operator per leg rather than per envelope, because
    Delta_cross is a DIFFERENTIAL: its late leg costs and its early leg credits, and
    folding that into a signed coefficient under one minus sign renders as "minus a
    negative" and reads as an error.

    ONE LANE'S CROSSING IS `X`, NOT SIGMA OR DELTA. Those symbols name the COMBINING --
    Sigma a sum over the two legs, Delta a difference between them -- so once each leg
    is shown separately with its own t_RF there is nothing left for them to denote, and
    labelling an addend with the sum's name is simply wrong. The composites keep their
    names where they are still composites:

        Delta_cross,MP        = X_MP,late      - X_MP,early
        Sigma_cross,PM,setup  = X_PM,setup,clk + X_PM,setup,data
        Sigma_cross,PM,hold   = X_PM,hold,clk  + X_PM,hold,data

    THE CROSSING COEFFICIENTS ARE DIMENSIONLESS, which is the other half of why the
    per-leg form is worth having: each X is a coefficient, and the lanes carry DIFFERENT
    t_RF -- Man CLK, Man DATA and Per DATA are three independent rows.

    So a single pre-multiplied `Delta_cross,MP = 8.19 ns` hid three things at once:
    that the quantity scales with slew, that it scales with a DIFFERENT slew per lane,
    and -- on Delta_cross,MP -- that WHICH lane carries the late crossing swaps between
    setup and hold. One product per leg shows all three and stays exact.

    It cannot be collapsed to a single `Sigma x t_RF`: that is equal only when the lanes
    happen to share a t_RF, and both envelopes additionally select their coefficient
    pair by a max (setup) or min (hold) over branches, so even the coefficients are not
    fixed. The `*_lanes` accessors return the branch actually in force.

    CLOCK LANE FIRST, always, because the terms are read as a TIME ORDER: the clock edge is
    what the receiving device detects, and the data crossing is judged against it. The
    envelope tuples list their legs late-then-early (the differential's own order, which is
    about SIGN), so without this sort a leg's clock and data crossings appear in whichever
    order the branch selection happened to leave them -- and Delta_cross,MP swaps which lane
    carries the late crossing between setup and hold, so the two families printed the two
    lanes in opposite orders on adjacent rows of the same page.
    """
    out: list[Term] = []
    for role, lane, coeff, lane_op in sorted(
            lanes, key=lambda leg: 0 if leg[1].endswith("CLK") else 1):
        trf = _lane_ns(inp, lane)
        out.append(Term(f"{base_sym},{role} · {_trf_sym(base_sym, lane)}",
                        (-1.0 if lane_op == "-" else 1.0) * coeff * trf, op=lane_op,
                        note=f"{coeff:.2f} (dimensionless) · {trf:.2f} ns",
                        factors=(coeff, trf)))
    return out


def _split_cross_terms(terms: list[Term]) -> tuple[list[Term], list[Term]]:
    """A crossing envelope's terms as (clock lane, data lane).

    The two halves belong at DIFFERENT INSTANTS on a leg whose launcher is a peripheral:
    the clock crossing is that peripheral detecting the edge, which happens before it
    launches, and the data crossing is the receiver seeing the result, which happens after
    the data has crossed the bus. So the leg interleaves them with the launch and the two
    flights rather than printing the envelope as one block.
    """
    clk = [t for t in terms if "_t_RF,CLK" in t.symbol]
    data = [t for t in terms if "_t_RF,CLK" not in t.symbol]
    return clk, data


def _pd_term(t_PD_ns: float, *, credit: bool, note: str) -> Term:
    """ONE bus traversal, named for which flight it is.

    A peripheral-launched leg pays two traversals and they are two different events -- the
    clock reaching the launcher, then its data coming back -- so they are two terms with the
    launch between them instead of one `2·t_PD` sitting off to the side. The note says which
    flight this one is, because the symbol cannot: both are `t_PD` on the same trace.

    NOT the same decision as the contention legs' netted `2·t_PD`, which stays netted: there
    the pair is a skew and an overlap allowance that CANCEL on two of the three legs, so
    printing them apart invited a reader to look for a bus effect that is not there. Here
    they do not cancel -- both are charged on setup, both credited on hold.
    """
    return Term("t_PD", (+t_PD_ns if credit else -t_PD_ns),
                op=("+" if credit else "-"), note=note)


# Which flight each traversal is, on a leg whose launcher is a peripheral.
_PD_TO_LAUNCHER = "the clock's flight from the Manager to the launching peripheral"
_PD_FROM_LAUNCHER = "the launched data's flight back across the bus to the sampler"


def _trf_sym(base_sym: str, lane: str) -> str:      # noqa: ARG001 - see the note below
    """The t_RF symbol for a crossing leg: `Man_t_RF,CLK`, `Per_t_RF,DATA`, and so on.

    ALWAYS QUALIFIED, on every lane. The qualifier used to be dropped wherever the role was
    thought to imply it -- on Sigma_cross,PM the `clk` leg is the Manager's clock and the
    `data` leg is the Peripheral's data, so `Man_t_RF` was held to be unambiguous. It is only
    unambiguous to a reader who already knows how that envelope is built, which is the reader
    who least needs the symbol. And it was never uniform: X_MP had to qualify both of its
    lanes (both Manager-driven, so a bare `Man_t_RF` names two different rows), so the page
    carried two spellings of one thing.

    Naming the lane on every term also makes the term point at the ROW a reader would edit,
    which is what the qualifier is for. `base_sym` is now unused and kept so a future
    per-envelope exception has somewhere to go rather than being reinvented at a call site.
    """
    device, which = lane.split()                  # "Man"/"Per", "CLK"/"DATA"
    return f"{device}_t_RF,{which}"


# (role, t_RF lane, dimensionless coefficient, operator) -- see `_cross_terms`.
# Aliased because mypy infers a bare "-" as `str`, and the helper needs the Literal;
# annotating each tuple at its definition is what keeps the two in step.
CrossLane = tuple[str, str, float, Literal["+", "-"]]
# (ns, dimensionless coefficient, t_RF ns) -- see `_per_clk_cross_parts`.
CrossParts = tuple[float, float, float]


def _coeff_note(parts: CrossParts) -> str:
    """`(ns, coefficient, t_RF)` rendered as the product it is.

    The t_RF comes from the parts rather than being re-read from the row, so the displayed
    product and the value in force cannot disagree.

    An earlier version had a second job: saying which END of a tolerance band a crossing was
    pinned to, because P->P cornered its two clock crossings independently. That band is gone
    -- one clock pin realises one slew (see `_per_clk_cross_ns`) -- and with it the only case
    where a crossing read anything other than the row's picked value.
    """
    _, coeff, trf = parts
    if not trf:
        return ""
    return f"{coeff:.2f} (dimensionless) · {trf:.2f} ns"


def _gather_lane_terms(terms: list["Term"]) -> list["Term"]:
    """Fold crossing terms that multiply the SAME t_RF row into one product.

    ONE PIN CANNOT HAVE TWO SLEWS. A P->P or contention leg reads `Man_t_RF,CLK` twice --
    once for each device's detection of the clock -- and showing two separate products
    invites, and previously WAS, two independently cornered values for one physical edge.
    The Manager sources the clock from one pin; at any instant that edge has one slew, so
    the two crossings scale by the same number and their sum is a single product.

    COLLAPSING IS EXACT HERE, AND ONLY HERE. `_cross_terms`' own docstring rules out
    collapsing an envelope to one `Sigma x t_RF` because "that is equal only when the lanes
    happen to share a t_RF" -- which is precisely the condition this function tests for. Two
    terms on DIFFERENT rows (Man CLK vs Per DATA) are left alone, so the per-lane form that
    made a wrong-lane bug visible is untouched.

    THE COMBINED COEFFICIENT IS SHOWN POSITIVE, with the operator carrying the sign, because
    a difference folded under a minus renders as "minus a negative" and reads as an error --
    the same reason `_cross_terms` keeps an operator per leg. The note carries the
    decomposition, so which device contributed what is still on the page:

        - X_PP,A,late - X_PP,B,early · Man_t_RF,CLK    0.99 (1.44 late - 0.45 early) · 5.00 ns

    A gathered pair whose coefficients CANCEL is still emitted, at zero: that is a real
    statement about the leg (the two detections track each other out) and dropping it would
    read as a lane nobody looked at.
    """
    out: list[Term] = list(terms)
    # (lane -> the indices of the terms multiplying it, with their (coeff, t_RF) pair). Only
    # terms that HAVE factors qualify: a term without them is not a `coefficient x row`
    # product and there is nothing to gather.
    by_lane: dict[str, list[tuple[int, tuple[float, ...]]]] = {}
    for i, term in enumerate(terms):
        if " · " not in term.symbol or term.factors is None:
            continue
        # ALREADY GATHERED TERMS ARE LEFT ALONE. Their factors carry more than one coefficient,
        # so folding one again would treat a coefficient as a t_RF. Cheap to guard and the kind
        # of thing that only bites once `__post_init__` runs on a re-constructed breakdown.
        if len(term.factors) != 2:
            continue
        by_lane.setdefault(term.symbol.split(" · ")[1], []).append((i, term.factors))

    drop: set[int] = set()
    for lane, entries in by_lane.items():
        if len(entries) < 2:
            continue
        idxs = [i for i, _f in entries]
        facs = [f for _i, f in entries]
        if len({round(f[1], 9) for f in facs}) != 1:
            continue                       # not the same value after all: leave them apart
        trf = facs[0][1]
        group = [out[i] for i in idxs]
        # Signed coefficients: the term's own operator says whether it adds or subtracts.
        signed = [(-f[0] if g.op == "-" else f[0]) for g, f in zip(group, facs)]
        total = sum(signed)
        heads = [g.symbol.split(" · ")[0] for g in group]

        # THE INNER SIGNS ARE RELATIVE TO THE FACTORED-OUT ONE. The term shows |total| under
        # an operator, so each contribution has to be divided by the overall sign or the
        # equation states something else entirely: `- (A,late + B,early)` is -1.89, not the
        # -0.99 the leg actually has. Dividing flips them to `- (A,late - B,early)`, which is
        # what -1.44 + 0.45 means. This is the "minus a negative" trap `_cross_terms` avoids
        # by keeping an operator per leg, met again one level up.
        overall = -1.0 if total < 0 else 1.0
        rel = [s / overall for s in signed]
        # Positive contribution first, so the parenthesis never opens with a minus.
        order = sorted(range(len(rel)), key=lambda i: rel[i] < 0)

        def _joined(texts) -> str:
            bits = []
            for k, i in enumerate(order):
                sign = "-" if rel[i] < 0 else "+"
                bits.append(texts[i] if k == 0 else f"{sign} {texts[i]}")
            return " ".join(bits)

        # PARENTHESISED, WITH BOTH CROSSINGS NAMED. Two earlier spellings were tried and
        # both hid something. Joining the heads bare -- `X_PM,late - X_PM,early · Man_t_RF,CLK`
        # -- is an expression wearing a symbol's place and breaks the comma-separated grammar
        # the naming gate parses. Collapsing to a single role, `X_PM,net`, is a valid symbol
        # but says only that a netting happened: WHICH two crossings netted, and therefore
        # whether the right pair was combined, went into a note nobody has to read.
        #
        # `(X_PP,A,late - X_PP,B,early) · Man_t_RF,CLK` names both, and the parenthesis is
        # exactly the grouping the arithmetic needs -- the operator is hoisted out, so the
        # bracket is what makes `- (a - b)` legible instead of the "minus a negative" that
        # `_cross_terms` keeps a per-leg operator to avoid.
        #
        # THREE NUMBERS IN THE SUBSTITUTION ROW, not two: `(1.44 - 0.45)·5.00`. A single
        # pre-netted coefficient would leave the reader unable to check the netting against
        # the symbol beside it, which is the whole argument the per-leg crossing form was
        # built on. `factors` therefore carries the coefficients FIRST and the row value LAST
        # (see `Term.factors`), so `factors[-1]` is still "the t_RF this term reads".
        note = (f"{abs(total):.2f} = "
                f"{_joined([f'{abs(s):.2f} ' + h.split(',')[-1] for h, s in zip(heads, rel)])}"
                f", at t_RF {trf:.2f} ns")
        merged = Term(f"({_joined(heads)}) · {lane}", total * trf,
                      op="+" if total >= 0 else "-", note=note,
                      factors=(*[rel[i] for i in order], trf))
        out[idxs[0]] = merged
        drop.update(idxs[1:])
    return [term for i, term in enumerate(out) if i not in drop]


def _swing_sym(inp: CalcInputs, lane: str) -> str:
    """The swing addend as `coefficient · t_RF`, naming the lane it reads.

    `lane` is the row label ("Man DATA" / "Per DATA") rather than a device prefix, so
    the term points at the row a reader would edit, and it is spelled `Man_t_RF,DATA` for
    the same reason every crossing lane is -- one vocabulary, see `_trf_sym`.

    The coefficient is `1/(V_OH,min − V_OL,max)` = 1.67 at the default 20-80 %
    thresholds, and it is COMPUTED rather than frozen at 5/3, because V_OH,min and
    V_OL,max are inputs. Displayed to two places, so the exact product is in the term's
    note: 1.67 · 8.3 = 13.86 by hand against 13.83 in force, a 0.03 ns rounding of the
    label and not of the arithmetic.
    """
    device, which = lane.split()
    return f"{_swing_coeff(inp):.2f}·{device}_t_RF,{which}"


def _swing_coeff(inp: CalcInputs) -> float:
    """`1/(V_OH,min − V_OL,max)` — the dimensionless multiplier in the swing symbol.

    Its own function so the SYMBOL and the substitution row's FACTORS come from one
    expression: they are the same number shown two ways, and two copies of the formula
    would let the printed coefficient drift from the one the product was built with.
    """
    span = inp.V_OH_min_frac - inp.V_OL_max_frac
    return (1.0 / span) if span > 0 else 0.0


def _per_clk_cross_ns(inp: CalcInputs, env: DeltaTpdEnvelope, *, corner: str) -> float:
    """When a PERIPHERAL recognises the forwarded clock edge, relative to the
    Manager's own internal timing reference.

    Priced the way every other leg in this model prices a threshold crossing: the
    CLOCK-LANE coefficient of the Sigma_cross,PM envelope, times that lane's t_RF.
    A Manager gets nothing -- its outputs are referenced to its own internal
    reference and its anchors are symmetric (Figure 176), so no crossing enters.

    Why a lane coefficient and not a whole Sigma: contention turns on the CLOCK
    crossing alone. There is no data-readability question in it -- t_DZ and t_ZD
    bound driver state -- so the data lane of those envelopes does not apply. This
    is the same construction `emit_ede.py` uses for "peripheral A's clock crossing"
    in Sigma_cross,PP, where A is an independent device and is charged in full.

    The corner follows the DIRECTION, which is the part a single-valued term cannot
    express:

        corner="late"   Sigma_cross,PM,setup's clock lane. A RELEASING peripheral
                        recognises the edge this late, so it holds the bus that
                        much past the Manager's reference. A charge.
        corner="early"  Sigma_cross,PM,hold's clock lane. An ACQUIRING peripheral
                        may recognise it this soon, so it turns on that much
                        earlier. Its own turn-on is a credit, so the early corner
                        is the one that erodes the gap.

    At t_RF = 8.3 ns those are 11.96 and 4.10 ns.

    ONE CLOCK PIN, ONE SLEW, EVEN WITH TWO PERIPHERALS ON THE LEG. A previous revision
    cornered the two crossings of a P->P leg at OPPOSITE ends of a `tRF_CLK_tol_ns` band,
    arguing that two unrelated parts share the slew SETTING but not the tolerance around
    it. That is wrong, and it is wrong about the physics rather than the modelling: t_RF on
    the clock lane is the MANAGER's output slew. There is one clock, one driver, one pin,
    and at any instant one edge -- so the releasing and acquiring peripherals cannot see
    a slow edge and a fast edge, they see the SAME edge. What genuinely differs between
    two peripherals is where their thresholds sit on it, which is already the V_IH/V_IL
    spread inside the envelope, and their internal delays, which are their own parameters.

    So both crossings take the value the corner search picked for the one row, and a leg
    that reads the lane twice GATHERS the two into one product (`_gather_lane_terms`) --
    which is exact precisely because they share a t_RF, the condition the per-leg form's
    own docstring named as the only one under which collapsing is allowed.

    Removing the band moved P->P hold 2.6 ns in the generous direction and, tellingly,
    onto the standalone reference's answer: `emit_ede.py` had always evaluated both
    crossings at one t_RF. The band was a divergence invented here, and the test written
    to pin it was pinning this model's error against the reference's correct behaviour.

    THIS REPLACED A BESPOKE TERM, and the reason is consistency rather than size.
    An earlier version of these legs carried `V_IH,max x swing` = 8.99 ns, single
    valued, with no noise, supply or slew loading -- a second vocabulary for the
    same physics, sitting alongside Sigma_cross/Delta_cross everywhere else in the
    file. It also matched neither end of the band it was standing in for, so it
    flattered a releasing peripheral by 2.97 ns and over-charged an acquiring one
    by 4.89.

    Worth knowing what this re-opens. CONSTRAINTS.md Sec. 10.1 argues the peripheral's
    reference should be SINGLE-VALUED, on the grounds that Table 123's V_IH band is
    already absorbed by the specified range of any parameter measured from it. That
    argument is right about the DEVICE half -- do not charge a vendor's own threshold
    spread twice -- and it does not settle this, which is a different quantity: where
    that crossing falls on the Manager's timeline, which depends on the incoming
    edge's slew and on noise, not on the device. Both terms are needed and only one
    of them is the device's.
    """
    return _per_clk_cross_parts(inp, env, corner=corner)[0]


def _per_clk_cross_parts(inp: CalcInputs, env: DeltaTpdEnvelope, *,
                         corner: str) -> CrossParts:
    """(ns, dimensionless coefficient, t_RF in ns) for `_per_clk_cross_ns`.

    The pair is reported rather than recomputed by the caller so the displayed product and
    the value in force come from one expression.
    """
    coeff = (env.sigma_pm_setup_clk_dgnd_pos if corner == "late"
             else env.sigma_pm_hold_rf_clk)
    return (coeff * inp.tRF_Man_CLK_ns, coeff, inp.tRF_Man_CLK_ns)


def _per_ede_release_vs_edge_ns(inp: CalcInputs, *, corner: str = "max") -> float:
    """The peripheral's EDE release, relative to its OWN detection of the closing
    edge. Negative is before that edge, which is what makes it early.

    Its own ramp completion plus the hold interval:

        detect + Per_t_DD + swing + hold        (absolute, from the opening edge)
        Per_t_DD + swing + hold - UI            (relative to the closing edge)

    The detection lead is deliberately NOT in the returned value: the caller adds it
    once, alongside the releasing device's clock arrival delay. Adding it here as
    well is the double charge that made this mechanism look 5 ns earlier than it is.
    """
    dd = inp.Per_t_DD_max_ns if corner == "max" else inp.Per_t_DD_min_ns
    hold = (inp.Per_ede_hold_max_ns if corner == "max"
            else inp.Per_ede_hold_min_ns)
    return dd + _swing_ns(inp, inp.tRF_Per_DATA_ns) + hold - inp.UI_ns()


def _effective_t_dz_ns(t_dz_ns: float, *, ede: bool, UI_ns: float) -> float:
    """A MANAGER's t_DZ,max as an END-of-UI-referenced value.

    Without EDE the tabulated value already is one.  With EDE it is referenced to
    the START of the last driven UI, so subtracting one UI puts it on the same
    footing -- and makes it negative, which is the whole content of the feature:
    the releasing driver is gone before the edge the acquirer works from.

    Peripheral-side EDE does NOT go through here; it has no clock-referenced value
    to shift. See `_per_ede_release_vs_edge_ns`.
    """
    return t_dz_ns - UI_ns if ede else t_dz_ns


def _contention_fmax_MHz(inp: CalcInputs, *, t_dz_max_ns: float,
                         t_zd_min_ns: float, skew_ns: float = 0.0) -> float:
    """Highest F_CLK at which a handover still avoids driver contention.

    From ``N_HO·UI + t_ZD,min − t_DZ,max + skew ≥ 0`` with
    ``UI = 1000·duty_min/f``:

        f_max = N_HO · 1000 · duty_min / (t_DZ,max − t_ZD,min − skew)

    ``skew`` is the acquirer's clock arrival minus the releaser's — positive when
    the acquiring device sees the clock later, which is the case that helps.  The
    sole caller folds both the skew and the overlap allowance into
    ``t_dz_max_ns``, so ``skew`` is left at 0 there rather than double-counting.

    Two cases return infinity, for different reasons:

    * the deficit is already non-positive — the devices never contend whatever
      the rate, because the acquiring driver's earliest turn-on already trails
      the releasing driver's latest release. This is PHY1's case, and is the
      inequality the §11.1.5 note relies on to hand over in zero UIs.
    * ``N_HO = 0`` with a positive deficit — no UI is allocated, so slowing the
      clock adds nothing to the gap. The constraint fails at every rate; 0.0 is
      returned rather than infinity so it reads as "no legal rate".
    """
    deficit = t_dz_max_ns - t_zd_min_ns - skew_ns
    if deficit <= 0.0:
        return math.inf
    if inp.handover_UIs <= 0.0:
        return 0.0
    return inp.handover_UIs * 1000.0 * inp.duty_min / deficit


def _per_tdd_pure_max(inp: CalcInputs) -> float:
    """Per_t_DD,max as a pure delay, normalised by the Peripheral's spec.

    SWI3S: t_DD − offset(shape)·t_RF (the V=0→V_OL portion the Fig-174
    measurement embeds).  SoundWire: t_OV − (1+offset)·t_RF, a full t_RF more,
    because t_OV is anchored at the END of the slew rather than its start.
    """
    return pure_output_delay(inp.Per_t_DD_max_ns, spec=inp.Per_spec,
                             side="Per", shape=inp.Per_shape)


def _per_tdd_pure_min(inp: CalcInputs) -> float:
    """Per_t_DD,min as a pure delay.  Same conversion as the max corner."""
    return pure_output_delay(inp.Per_t_DD_min_ns, spec=inp.Per_spec,
                             side="Per", shape=inp.Per_shape, corner="min")


def _man_tdd_range(inp: CalcInputs) -> tuple[float, float]:
    """(min, max) Man_t_DD after applying the launch mode.

    THE LAUNCH MODE SETS THE CLOCK'S RESOLUTION, NOT THE PLACEMENT.  A clocked
    edge lands on a known clock phase, so both corners collapse to one value and
    the analog spread -- 14 ns under the proposal -- disappears from every MP
    inequality.  WHICH phase is a design choice on the grid the mode provides: a
    4x clock offers three interior placements (0.25, 0.50, 0.75 UI), not one.

    This used to pin t_DD to a single placement per mode -- 0.50 UI at 2x,
    0.25 UI at 4x -- which conflated the two and made whole designs
    inexpressible.  The EDE proposal is one: it wants a 4x RESOLUTION with t_DD at
    0.50 UI, so that the release can sit a grid step later at 0.75 UI and still
    leave the keeper its 3 ns.  Pinning 4x to 0.25 UI reported that design as
    failing MP hold by 0.74 ns when the design does not place t_DD there at all.
    """
    clk = launch_mode_tDD_ns(inp.launch_mode, inp.F_CLK_target_MHz, inp.duty_min)
    if clk is None:
        return inp.Man_t_DD_min_ns, inp.Man_t_DD_max_ns
    # Clocked: deterministic, and on a placeable point. The row carries the chosen
    # placement; the mode only says which points exist.
    snapped = snap_to_launch_grid(inp.Man_t_DD_max_ns, inp.launch_mode,
                                  inp.F_CLK_target_MHz, inp.duty_min)
    return snapped, snapped


def _man_tdd_pure_max(inp: CalcInputs) -> float:
    """Man_t_DD,max as a pure delay.

    A SWI3S Manager value is already pure — Man_t_DD is measured between
    symmetric anchors (Fig. 176), so the slew portions cancel.  A SoundWire
    Manager's t_OV is not: it needs the end-of-slew conversion, which the
    Per-only hardcode this replaced never applied (§4.2).
    """
    return pure_output_delay(_man_tdd_range(inp)[1], spec=inp.Man_spec,
                             side="Man", shape=inp.Man_shape)


def _man_tdd_pure_min(inp: CalcInputs) -> float:
    """Man_t_DD,min as a pure delay.  Same conversion as the max corner."""
    return pure_output_delay(_man_tdd_range(inp)[0], spec=inp.Man_spec,
                             side="Man", shape=inp.Man_shape, corner="min")


def _per_tzd_pure_max(inp: CalcInputs) -> float:
    """Per_t_ZD,max,pure — the SAME anchor back-out as Per_t_DD.

    Both are Peripheral output delays measured to 20 % of the ramp, so both carry
    the V=0 -> V_OL slew portion that the pure convention takes back out, and a
    9 ns Per_t_DD,max and a 9 ns Per_t_ZD,max must produce the SAME pure value.
    They are asymmetric-anchor measurements (Figure 174), unlike the Manager's
    (Figure 176), which is why the Manager side needs no back-out and this side
    does -- that is consistency per side, not a disagreement between the sides.

    Briefly changed to return the raw value, on a misreading of "t_ZD ends where
    the impedance ramp begins": the 20 % is 20 % OF THAT RAMP, which is the same
    endpoint Per_t_DD is measured to, not a point before the slew.  Removing the
    back-out made the two pure values differ by t_RF/3 for identical tabulated
    numbers, which is how the error showed itself.  Reverted.
    """
    return pure_output_delay(inp.Per_t_ZD_ns, spec=inp.Per_spec,
                             side="Per", shape=inp.Per_shape)


def _man_tzd_clocked(inp: CalcInputs) -> bool:
    """Does the launch mode govern the Manager's t_ZD?  Always yes.

    CLOCKED OR ANALOG, NOT A MIX.  One Manager, one output stage: if its data edge
    is launched from a multiplied clock then it has that clock, and its output
    ENABLE is launched from it too.  A device whose t_DD is a clock grid point
    while its t_ZD is an analog delay line is not a conservative model of anything
    -- it is two different devices, and that is the state this used to leave PHY2
    in: t_DD collapsed to 18.31 ns at 2x while t_ZD kept its analog range.

    t_ZD is a DATA LAUNCH METHOD, not a handover accessory: it and t_DD are the two
    ways this device launches an edge, and both answer the same setup and hold
    requirements at the receiver.  So a device that clocks one clocks the other, and
    the PHY2 revision states them identically -- "9-23 ns or (0.5 UI 2x CLK,
    0.25 UI 4x CLK)" for each.  They are not similar by choice; facing the same
    inequalities forces them together, which is also why Man_t_ZD,min moved from
    2 ns to 9 ns for the same reason Man_t_DD,min did: hold at the peripheral.

    What the mode dictates is that t_ZD is CLOCKED and at what RESOLUTION -- not
    which grid point it sits on.  A 4x clock offers 0.00, 0.25, 0.50 and 0.75 UI;
    a 2x clock offers 0.00 and 0.50.  So it stays an editable design choice,
    snapped to a point the selected clock can place (`snap_to_launch_grid`).

    EDE is NOT excluded here, and an earlier version of this model excluded it --
    wrongly.  EDE redefines `t_DZ` (a mid-UI release, Table 130); it says nothing
    about `t_ZD`, which keeps the revision's own pairing with `t_DD`.  Excluding it
    let the EDE column hold a `Man_t_ZD,min` of 3 ns, below the 9 ns the revision
    raised it to, and so below what MP_hold_ho requires.  See `_man_tdz_clocked`
    for the parameter EDE genuinely does redefine.
    """
    return True


def _man_tdz_clocked(inp: CalcInputs) -> bool:
    """Does the launch mode govern the Manager's t_DZ?  Yes -- including under EDE.

    EDE changes what t_DZ MEANS -- the release moves to mid-UI, referenced to the
    START of the last driven UI, inside Table 130's window -- but it does not
    exempt the value from being placeable by the clock the Manager actually has.
    A release the selected clock cannot generate is not a proposal, and an earlier
    version of this model exempted EDE's t_DZ entirely, which let a 2x column hold
    a 0.75 UI release that a 2x clock cannot place.

    Snapping happens on the RAW value, referenced to the UI start; the EDE shift to
    end-of-UI terms is applied afterwards by `_effective_t_dz_ns`.  The two are
    independent and must stay in that order.

    Consequence worth knowing, because it is what makes EDE need a 4x clock: the
    release has to clear the launch by the whole rail-to-rail swing PLUS the keeper's
    response -- 13.83 + 3.00 = 16.83 ns at the slow corner, 0.46 UI -- so on a 1/4 UI
    grid it must be TWO steps on, not merely a distinct point.  This said "at least
    t_keeper after the launch, on a DISTINCT grid point", which is the retired
    swing-for-t_keeper reading and admits ONE step: 0.25 -> 0.50 UI clears 3 ns
    comfortably and fails the keeper leg by 7.68.

    A 1/2 UI grid does contain a pair 16.83 ns apart -- launch 0.50 UI, release
    1.00 UI -- so the keeper is NOT what rules 2x out.  Table 130's ceiling is
    (`ede_release_ceiling_ns`), together with the definition of an early release: the
    boundary is neither legal EDE nor early, and with it excluded a 1/2 UI grid has no
    remaining point above the launch.
    """
    return True


def _man_clocked_grid_ns(inp: CalcInputs, value_ns: float,
                         *, governed: bool = True,
                         ceil_ns: float | None = None) -> tuple[float, str]:
    """`value_ns` on the grid the launch mode can place, plus a provenance note.

    Returned as a pair so the substitution shown in the results tree can say the
    value was moved and why, the way `_conv_note` does for anchor conversions.  A
    silent snap would be its own "the value on screen is not the value in force".

    `governed` is the caller's predicate -- `_man_tzd_clocked` for t_ZD,
    `_man_tdz_clocked` for t_DZ -- because EDE redefines one and not the other.

    `ceil_ns` is Table 130's cap on an EDE release, and it is reported SEPARATELY from
    the grid snap because they are different objections: off-grid means this Manager
    cannot generate the release, over the cap means the specification does not permit
    it.  It also applies where there is no grid at all -- an analog EDE release is
    still bounded by the table -- so it is not folded into `snap_to_launch_grid`.
    """
    grid = launch_grid_UI(inp.launch_mode) if governed else None
    if grid is None:
        # Analog, or a parameter this mode does not govern. The table still bounds it.
        capped = value_ns if ceil_ns is None else min(value_ns, ceil_ns)
        return capped, _ede_ceil_note(inp, value_ns, capped, ceil_ns)
    snapped = snap_to_launch_grid(value_ns, inp.launch_mode,
                                  inp.F_CLK_target_MHz, inp.duty_min,
                                  ceil_ns=ceil_ns)
    if ceil_ns is not None and value_ns > ceil_ns + 5e-4:
        return snapped, _ede_ceil_note(inp, value_ns, snapped, ceil_ns)
    if math.isclose(snapped, value_ns, abs_tol=5e-4):
        return snapped, (f"clocked: {snapped / inp.UI_ns():.2f} UI on the "
                         f"{grid:g} UI grid a {inp.launch_mode.value} clock places")
    return snapped, (f"{value_ns:.2f} ns is not on the {grid:g} UI grid a "
                     f"{inp.launch_mode.value} clock can place → snapped to "
                     f"{snapped:.2f} ns ({snapped / inp.UI_ns():.2f} UI)")


def _ede_ceil_note(inp: CalcInputs, value_ns: float, used_ns: float,
                   ceil_ns: float | None) -> str:
    """Why an EDE release was pulled back to Table 130's ceiling, or "" if it was not."""
    if ceil_ns is None or value_ns <= ceil_ns + 5e-4:
        return ""
    ui = inp.UI_ns()
    return (f"{value_ns:.2f} ns ({value_ns / ui:.2f} UI) is past Table 130's "
            f"EndDriveEarly ceiling of 0.60·UI + 10 ns = {ceil_ns:.2f} ns "
            f"({ceil_ns / ui:.2f} UI) → {used_ns:.2f} ns ({used_ns / ui:.2f} UI), "
            f"the latest the selected clock can place inside it")


def _man_tzd_ns(inp: CalcInputs) -> float:
    """Man_t_ZD, on the launch mode's grid where the launch mode governs it.

    ONE definition, read by BOTH the setup handover variants and the contention
    legs, which is what this helper exists for: they used to disagree, setup
    applying the launch mode while contention took the field raw, so a
    clock-launched bus modelled two different turn-on times for the same device.

    A clocked value is deterministic, so min == max and the same number serves the
    two corners the two families need.  Worth naming what that means, because it
    is the optimistic direction on one family: contention reads t_ZD,min, so a
    clocked turn-on carries no PVT spread at all.  Right if the enable really is
    clocked, but it does assume zero analog turn-on after the clock edge, which
    the EDE column deliberately does not (3-9 ns of turn-on on top of its point).
    """
    return _man_clocked_grid_ns(inp, inp.Man_t_ZD_ns)[0]


def _man_tdz_parts(inp: CalcInputs) -> tuple[float, str]:
    """Man_t_DZ,max as the model uses it, plus the note saying how it got there.

    ONE definition, read by BOTH the keeper leg and the contention legs, for the same
    reason `_man_tzd_ns` exists: they used to disagree.  The contention legs took the
    grid-snapped value while the keeper leg read `inp.Man_t_DZ_max_ns` raw, so a
    clocked launch modelled two different release times for one output stage -- and it
    kept the favourable half of each, which is the same hybrid described below.

    Note the DIRECTION differs between the two families, which is a reason to have
    wanted the coupling rather than a reason to fear it: a clocked release later than
    the tabulated 10 ns max means the Manager holds the bus longer, so MP contention
    gets WORSE while the keeper gets better.  The old hybrid took the 10 ns analog
    release together with a clocked data launch -- the favourable half of each.

    Table 130's ceiling applies only under EDE, because that is the table's subject: it
    bounds an EndDriveEarly release, not a conventional one, which is referenced to the
    closing edge and bounded by its own tabulated t_DZ,max instead.
    """
    return _man_clocked_grid_ns(
        inp, inp.Man_t_DZ_max_ns, governed=_man_tdz_clocked(inp),
        ceil_ns=ede_release_ceiling_ns(inp.UI_ns()) if inp.Man_ede else None)


def _man_tzd_pure_max(inp: CalcInputs) -> float:
    """Man_t_ZD,max,pure.  A SWI3S Manager t_ZD shares Man_t_DD's symmetric
    anchors and is already pure; a SoundWire one needs the conversion."""
    return pure_output_delay(_man_tzd_ns(inp), spec=inp.Man_spec,
                             side="Man", shape=inp.Man_shape)


def _man_tzd_pure_min(inp: CalcInputs) -> float:
    """Man_t_ZD,min,pure -- the corner the HOLD legs read.

    It exists rather than the hold legs reusing the max helper, and the
    distinction is not cosmetic: `pure_output_delay` branches on the corner, and
    for a SoundWire side the two differ.  A SoundWire t_ZD_Data ends at
    low-impedance, BEFORE the ramp, so no slew is embedded and only the clock-side
    re-anchor applies -- which is exactly what the `corner="min"` branch encodes.
    Feeding a SoundWire t_ZD through the max branch subtracts a data-side slew the
    number never contained.

    For a SWI3S Manager both corners return the tabulated value untouched
    (symmetric anchors, Fig. 176), so this changes nothing on an all-SWI3S bus --
    which is why reusing the max helper passed every test while still being wrong
    for the mixed case the calculator exists to model.
    """
    return pure_output_delay(_man_tzd_ns(inp), spec=inp.Man_spec,
                             side="Man", shape=inp.Man_shape, corner="min")


def _per_tzd_pure_min(inp: CalcInputs) -> float:
    """Per_t_ZD,min,pure.  Same reasoning as `_man_tzd_pure_min`."""
    return pure_output_delay(inp.Per_t_ZD_ns, spec=inp.Per_spec,
                             side="Per", shape=inp.Per_shape, corner="min")


def _conv_note(spec: SpecSource, side: str, tabulated: float,
               pure: float, tRF_ns: float) -> str:
    """Provenance for a clock-to-output term that was normalised."""
    if abs(pure - tabulated) < 1e-12:
        return f"{spec} {side} tabulated {tabulated:.2f} ns — already a pure delay"
    if is_soundwire(spec):
        return (f"{spec} t_OV {tabulated:.2f} ns ends at V_OH (end of slew) → "
                f"pure {pure:.2f} ns (−(1+offset)·t_RF, t_RF={tRF_ns:.2f} ns)")
    return (f"{spec} t_DD {tabulated:.2f} ns ends at V_OL (start of valid swing) → "
            f"pure {pure:.2f} ns (−offset·t_RF, t_RF={tRF_ns:.2f} ns)")


def compute(inp: CalcInputs) -> CalcResults:
    """Evaluate all four SWI3S inequalities at the supplied corner.

    Returns t_cross values (Δ_cross,MP, Σ_cross,PM,setup, Σ_cross,PM,hold)
    and a per-inequality breakdown with terms in ns and signs baked in.
    """
    env = inp.envelope()
    t_PD = inp.t_PD_ns()
    t_PDmis = inp.t_PD_mis_ns()
    UI = inp.UI_ns()

    # Δ_cross,MP differs between setup and hold once the Manager's two lanes
    # carry different t_RF: the LATE crossing sits on the data for setup and on
    # the clock for hold. Identical when the lanes match, which is the default.
    delta_MP_setup_ns = env.delta_cross_MP_ns(
        inp.tRF_Man_CLK_ns, inp.tRF_Man_DATA_ns, direction="setup")
    delta_MP_hold_ns = env.delta_cross_MP_ns(
        inp.tRF_Man_CLK_ns, inp.tRF_Man_DATA_ns, direction="hold")
    sigma_setup_ns = env.sigma_pm_setup_ns(inp.tRF_Man_CLK_ns, inp.tRF_Per_DATA_ns)
    sigma_hold_ns = env.sigma_pm_hold_ns(inp.tRF_Man_CLK_ns, inp.tRF_Per_DATA_ns)

    # THE SAME QUANTITIES, LANE BY LANE, for display. Each envelope is a sum of
    # (dimensionless coefficient) x (that lane's t_RF); the accessors return the branch
    # the max/min actually selects, so the products below sum to the ns values above
    # exactly. Asserted at the end of this block rather than trusted.
    _d_set = env.delta_cross_MP_lanes(direction="setup")
    _d_hold = env.delta_cross_MP_lanes(direction="hold")
    _s_clk, _s_data = env.sigma_pm_setup_lanes(inp.tRF_Man_CLK_ns, inp.tRF_Per_DATA_ns)
    _h_clk, _h_data = env.sigma_pm_hold_lanes(inp.tRF_Man_CLK_ns, inp.tRF_Per_DATA_ns)
    DELTA_SETUP: tuple[CrossLane, ...] = (("late", _d_set[0][0], _d_set[0][1], "-"),
                   ("early", _d_set[1][0], _d_set[1][1], "+"))
    DELTA_HOLD: tuple[CrossLane, ...] = (("late", _d_hold[0][0], _d_hold[0][1], "-"),
                  ("early", _d_hold[1][0], _d_hold[1][1], "+"))
    # THE SAME CONSTRUCTION ON P->P, BUT THE DATA IS A PERIPHERAL'S. B's clock detection and
    # its detection of the incoming data are one die at nearly one instant, so their
    # difference is the Delta_cross,MP construction -- but the DATA reaching B was driven by
    # peripheral A, not by the Manager, so the data lane is `Per DATA`. Reusing the MP tuples
    # verbatim charged the MANAGER's data slew on a link the Manager does not drive: with the
    # lanes split 5.0 / 8.3 the PP legs tracked the wrong row entirely.
    _pp_lane = {"Man DATA": "Per DATA", "Man CLK": "Man CLK"}
    DELTA_SETUP_PP: tuple[CrossLane, ...] = tuple(
        (role, _pp_lane[lane], coeff, op) for role, lane, coeff, op in DELTA_SETUP)
    DELTA_HOLD_PP: tuple[CrossLane, ...] = tuple(
        (role, _pp_lane[lane], coeff, op) for role, lane, coeff, op in DELTA_HOLD)
    # ROLE, NOT LANE. These read `late`/`early` like Delta's legs rather than `clk`/`data`,
    # because the lane is already named by the t_RF the term is multiplied BY -- `X_PM,setup,clk
    # · Man_t_RF` said "clock" twice and still needed the reader to know that Sigma's clock leg
    # is the Manager's. Setup is eroded by a LATE crossing and hold by an EARLY one, which is
    # the same convention Delta_cross,MP already used, so one vocabulary now covers both.
    SIGMA_SETUP: tuple[CrossLane, ...] = (("late", "Man CLK", _s_clk, "-"),
                                          ("late", "Per DATA", _s_data, "-"))
    SIGMA_HOLD: tuple[CrossLane, ...] = (("early", "Man CLK", _h_clk, "+"),
                                         ("early", "Per DATA", _h_data, "+"))
    for _lanes, _ns, _sgn in ((DELTA_SETUP, delta_MP_setup_ns, -1.0),
                              (DELTA_HOLD, delta_MP_hold_ns, -1.0),
                              (SIGMA_SETUP, sigma_setup_ns, -1.0),
                              (SIGMA_HOLD, sigma_hold_ns, +1.0)):
        _sum = _sgn * sum((-1.0 if o == "-" else 1.0) * c * _lane_ns(inp, ln)
                          for _, ln, c, o in _lanes)
        assert abs(_sum - _ns) < 1e-9, (
            f"lane decomposition {_sum} != envelope {_ns}; a displayed "
            f"coefficient x t_RF must equal the ns value, not approximate it")

    per_tdd_max_pure = _per_tdd_pure_max(inp)
    per_tdd_min_pure = _per_tdd_pure_min(inp)
    man_tdd_max_pure = _man_tdd_pure_max(inp)
    man_tdd_min_pure = _man_tdd_pure_min(inp)

    man_note = _conv_note(inp.Man_spec, "Man", inp.Man_t_DD_max_ns,
                          man_tdd_max_pure, inp.tRF_Man_DATA_ns)
    per_note = _conv_note(inp.Per_spec, "Per", inp.Per_t_DD_max_ns,
                          per_tdd_max_pure, inp.tRF_Per_DATA_ns)

    breakdowns: dict[Inequality, InequalityBreakdown] = {}

    # ----- MP setup: UI ≥ Man_t_DD,max,pure + Per_t_IS,max + t_PD,mis + Δ_cross,MP -----
    mp_setup_required = (
        man_tdd_max_pure + inp.Per_t_IS_max_ns + t_PDmis + delta_MP_setup_ns
    )
    mp_setup_terms = [
        Term("UI", +UI, op="+"),
        Term("Man_t_DD,pure", -man_tdd_max_pure, op="-", note=man_note),
        Term("t_PD,mis", -t_PDmis, op="-"),
        *_cross_terms(inp, "X_MP", DELTA_SETUP),
        Term("Per_t_IS", -inp.Per_t_IS_max_ns, op="-"),
    ]
    breakdowns["MP_setup"] = InequalityBreakdown(
        name="MP_setup",
        terms=mp_setup_terms,
        required_ns=mp_setup_required,
        margin_ns=UI - mp_setup_required,
        F_max_MHz=(1000.0 * inp.duty_min / mp_setup_required) if mp_setup_required > 0 else math.inf,
        is_setup=True,
    )

    # ----- MP setup, handover UI: as MP_setup but Man_t_DD,max → Man_t_ZD,max -----
    man_tzd_max_pure = _man_tzd_pure_max(inp)
    man_tzd_min_pure = _man_tzd_pure_min(inp)
    per_tzd_min_pure = _per_tzd_pure_min(inp)
    mp_setup_ho_required = (
        man_tzd_max_pure + inp.Per_t_IS_max_ns + t_PDmis + delta_MP_setup_ns
    )
    mp_setup_ho_terms = [
        Term("UI", +UI, op="+"),
        # Two things can have happened to this value: the launch mode may have
        # snapped it onto a placeable grid point, and the spec may need an anchor
        # conversion. Both are reported, in that order, and the conversion note is
        # given the SNAPPED value as its input -- passing the tabulated one made
        # `_conv_note` attribute the whole move to the anchor conversion, which for
        # a SWI3S Manager does nothing at all.
        Term("Man_t_ZD,pure", -man_tzd_max_pure, op="-",
             note="; ".join(n for n in (
                 _man_clocked_grid_ns(inp, inp.Man_t_ZD_ns)[1],
                 _conv_note(inp.Man_spec, "Man", _man_tzd_ns(inp),
                            man_tzd_max_pure, inp.tRF_Man_DATA_ns),
             ) if n)),
        Term("t_PD,mis", -t_PDmis, op="-"),
        *_cross_terms(inp, "X_MP", DELTA_SETUP),
        Term("Per_t_IS", -inp.Per_t_IS_max_ns, op="-"),
    ]
    breakdowns["MP_setup_ho"] = InequalityBreakdown(
        name="MP_setup_ho",
        terms=mp_setup_ho_terms,
        required_ns=mp_setup_ho_required,
        margin_ns=UI - mp_setup_ho_required,
        F_max_MHz=(1000.0 * inp.duty_min / mp_setup_ho_required) if mp_setup_ho_required > 0 else math.inf,
        is_setup=True,
    )

    # ----- MP hold: Man_t_DD,min,pure ≥ Per_t_IH,max + t_PD,mis + Δ_cross,MP -----
    mp_hold_terms = [
        Term("Man_t_DD,pure", +man_tdd_min_pure, op="+",
             note=_conv_note(inp.Man_spec, "Man", inp.Man_t_DD_min_ns,
                             man_tdd_min_pure, inp.tRF_Man_DATA_ns)),
        Term("t_PD,mis", -t_PDmis, op="-"),
        *_cross_terms(inp, "X_MP", DELTA_HOLD),
        Term("Per_t_IH", -inp.Per_t_IH_max_ns, op="-"),
    ]
    mp_hold_margin = sum(t.value_ns for t in mp_hold_terms)
    breakdowns["MP_hold"] = InequalityBreakdown(
        name="MP_hold",
        terms=mp_hold_terms,
        required_ns=0.0,
        margin_ns=mp_hold_margin,
        F_max_MHz=math.inf if mp_hold_margin >= 0 else 0.0,
        is_setup=False,
    )

    # ----- MP hold on a t_ZD launch: the fourth of the 2x2 ---------------------
    #
    # t_DD and t_ZD are the two ways the Manager launches a data edge, and the
    # peripheral's hold requirement applies to BOTH -- it is one requirement at the
    # receiver, which cannot tell which path produced the edge.  So this leg is not
    # a handover special case bolted on; it is the missing quarter of
    # (launch method) x (setup, hold).  The model carried the other three.
    #
    # Concretely: the bit sampled at the UI-opening edge must stay valid for
    # Per_t_IH,max past that edge, and the Manager's turn-on out of high-Z is what
    # ends it.  So t_ZD,min funds hold exactly as t_DD,min does on an ordinary UI.
    #
    # NOTHING ELSE COVERS IT.  Non-contention bounds driver OVERLAP: once the
    # releaser is off, the keeper holds the level and the acquirer may drive a
    # DIFFERENT level immediately with no driver conflict at all.  The keeper legs
    # bound the RELEASING side -- whether the keeper had a valid level to latch.
    # Neither says the sampled bit survives its hold window.
    #
    # The N_HO*UI term is why the omission stayed hidden: with a handover UI
    # allocated the turn-on is a whole UI further from the sampling edge, which at
    # 12.288 MHz is 36.62 ns of margin on a leg needing about 5.  Only a zero-UI
    # handover exercises it.
    #
    # And this is the constraint the PHY2 revision already answers: Man_t_ZD,min
    # moved from 2 ns to 9 ns *because 2 ns violated hold at the peripheral*, the
    # same reason Man_t_DD,min is 9.  Adding the leg reproduces the revision's own
    # reasoning and stops the model accepting a t_ZD,min the revision rules out.
    #
    # Man_t_ZD is ONE field and the auto-worst solver drives it to whichever corner
    # hurts each inequality -- its max for setup, its min here.  The pure-delay
    # CONVERSION also differs by corner on a SoundWire side, so this reads
    # `_man_tzd_pure_min` rather than the setup legs' max helper.
    mp_hold_ho_terms = [
        *_handover_ui_terms(inp, UI),
        Term("Man_t_ZD,pure", +man_tzd_min_pure, op="+",
             note="; ".join(n for n in (
                 _man_clocked_grid_ns(inp, inp.Man_t_ZD_ns)[1],
                 _conv_note(inp.Man_spec, "Man", _man_tzd_ns(inp),
                            man_tzd_min_pure, inp.tRF_Man_DATA_ns),
             ) if n)),
        Term("t_PD,mis", -t_PDmis, op="-"),
        *_cross_terms(inp, "X_MP", DELTA_HOLD),
        Term("Per_t_IH", -inp.Per_t_IH_max_ns, op="-"),
    ]
    mp_hold_ho_margin = sum(t.value_ns for t in mp_hold_ho_terms)
    breakdowns["MP_hold_ho"] = InequalityBreakdown(
        name="MP_hold_ho",
        terms=mp_hold_ho_terms,
        required_ns=0.0,
        margin_ns=mp_hold_ho_margin,
        F_max_MHz=math.inf if mp_hold_ho_margin >= 0 else 0.0,
        is_setup=False,
    )

    # ----- PM setup: UI ≥ Per_t_DD,max,pure + Man_t_IS,max + 2·t_PD + Σ_PM,setup -----
    pm_setup_required = (
        per_tdd_max_pure + inp.Man_t_IS_max_ns + 2.0 * t_PD + sigma_setup_ns
    )
    # IN TIME ORDER, which is what the two traversals and the split envelope buy: the clock
    # leaves the Manager, reaches the peripheral, the peripheral recognises it, launches, the
    # data crosses back, the Manager's receiver recognises THAT, and only then is the
    # Manager's own setup window the thing left to satisfy. Every leg below traces the same
    # way, so a reader can follow one row and know what happens next.
    _pm_clk, _pm_data = _split_cross_terms(_cross_terms(inp, "X_PM", SIGMA_SETUP))
    pm_setup_terms = [
        Term("UI", +UI, op="+"),
        _pd_term(t_PD, credit=False, note=_PD_TO_LAUNCHER),
        *_pm_clk,
        Term("Per_t_DD,pure", -per_tdd_max_pure, op="-", note=per_note),
        _pd_term(t_PD, credit=False, note=_PD_FROM_LAUNCHER),
        *_pm_data,
        Term("Man_t_IS", -inp.Man_t_IS_max_ns, op="-"),
    ]
    breakdowns["PM_setup"] = InequalityBreakdown(
        name="PM_setup",
        terms=pm_setup_terms,
        required_ns=pm_setup_required,
        margin_ns=UI - pm_setup_required,
        F_max_MHz=(1000.0 * inp.duty_min / pm_setup_required) if pm_setup_required > 0 else math.inf,
        is_setup=True,
    )

    # ----- PM setup, handover UI: as PM_setup but Per_t_DD → Per_t_ZD,max,pure -----
    per_tzd_max_pure = _per_tzd_pure_max(inp)
    pm_setup_ho_required = (
        per_tzd_max_pure + inp.Man_t_IS_max_ns + 2.0 * t_PD + sigma_setup_ns
    )
    pm_setup_ho_terms = [
        Term("UI", +UI, op="+"),
        _pd_term(t_PD, credit=False, note=_PD_TO_LAUNCHER),
        *_pm_clk,
        Term("Per_t_ZD,pure", -per_tzd_max_pure, op="-",
             note=_conv_note(inp.Per_spec, "Per", inp.Per_t_ZD_ns,
                             per_tzd_max_pure, inp.tRF_Per_DATA_ns)),
        _pd_term(t_PD, credit=False, note=_PD_FROM_LAUNCHER),
        *_pm_data,
        Term("Man_t_IS", -inp.Man_t_IS_max_ns, op="-"),
    ]
    breakdowns["PM_setup_ho"] = InequalityBreakdown(
        name="PM_setup_ho",
        terms=pm_setup_ho_terms,
        required_ns=pm_setup_ho_required,
        margin_ns=UI - pm_setup_ho_required,
        F_max_MHz=(1000.0 * inp.duty_min / pm_setup_ho_required) if pm_setup_ho_required > 0 else math.inf,
        is_setup=True,
    )

    # ----- PM hold: Per_t_DD,min,pure − Man_t_IH,max + 2·t_PD + Σ_PM,hold ≥ 0 -----
    _pmh_clk, _pmh_data = _split_cross_terms(_cross_terms(inp, "X_PM", SIGMA_HOLD))
    pm_hold_terms = [
        _pd_term(t_PD, credit=True, note=_PD_TO_LAUNCHER),
        *_pmh_clk,
        Term("Per_t_DD,pure", +per_tdd_min_pure, op="+",
             note=_conv_note(inp.Per_spec, "Per", inp.Per_t_DD_min_ns,
                             per_tdd_min_pure, inp.tRF_Per_DATA_ns)),
        _pd_term(t_PD, credit=True, note=_PD_FROM_LAUNCHER),
        *_pmh_data,
        Term("Man_t_IH", -inp.Man_t_IH_max_ns, op="-"),
    ]
    pm_hold_margin = sum(t.value_ns for t in pm_hold_terms)
    breakdowns["PM_hold"] = InequalityBreakdown(
        name="PM_hold",
        terms=pm_hold_terms,
        required_ns=0.0,
        margin_ns=pm_hold_margin,
        F_max_MHz=math.inf if pm_hold_margin >= 0 else 0.0,
        is_setup=False,
    )

    # ----- PM hold on the ACQUIRED UI: Per_t_ZD in place of Per_t_DD ------------
    #
    # The peripheral-acquiring counterpart of MP_hold_ho above; see that block for
    # what the leg bounds and why nothing else covers it.  Same substitution, same
    # N_HO*UI term, and the PM direction's own credits (+2*t_PD, +Sigma_cross)
    # rather than the MP direction's debits -- which is why it is the comfortable
    # one of the pair and the MP side is where a thin Man_t_ZD,min shows up.
    pm_hold_ho_terms = [
        *_handover_ui_terms(inp, UI),
        _pd_term(t_PD, credit=True, note=_PD_TO_LAUNCHER),
        *_pmh_clk,
        Term("Per_t_ZD,pure", +per_tzd_min_pure, op="+",
             note=_conv_note(inp.Per_spec, "Per", inp.Per_t_ZD_ns,
                             per_tzd_min_pure, inp.tRF_Per_DATA_ns)),
        _pd_term(t_PD, credit=True, note=_PD_FROM_LAUNCHER),
        *_pmh_data,
        Term("Man_t_IH", -inp.Man_t_IH_max_ns, op="-"),
    ]
    pm_hold_ho_margin = sum(t.value_ns for t in pm_hold_ho_terms)
    breakdowns["PM_hold_ho"] = InequalityBreakdown(
        name="PM_hold_ho",
        terms=pm_hold_ho_terms,
        required_ns=0.0,
        margin_ns=pm_hold_ho_margin,
        F_max_MHz=math.inf if pm_hold_ho_margin >= 0 else 0.0,
        is_setup=False,
    )

    # ----- P->P data legs: peripheral A drives, peripheral B samples ------------
    #
    # ONE PERIPHERAL SAMPLING ANOTHER'S DATA. Both take the same forwarded clock, but each
    # references its own arrival of it, which is what makes this a third direction rather
    # than PM with the labels changed:
    #
    #   setup:  UI  >=  Per_t_DD,max,pure + Per_t_IS + net·t_PD + Sigma_cross,PP,setup
    #   hold:   Per_t_DD,min,pure - Per_t_IH + net·t_PD + Sigma_cross,PP,hold  >=  0
    #
    # and the same pair again with Per_t_ZD in place of Per_t_DD -- the launch-method half
    # of the 2x2 this file treats as structural. The receiver cannot tell which path
    # produced the edge, so both must satisfy both.
    #
    # THE FLIGHT TERM IS THE PLACEMENT, NOT THE BUS LENGTH, and it is signed by geometry
    # rather than by direction: see `CalcInputs.pp_net_pd`. Setup reads the bracket at 2
    # (driver at the far end, sampler at the near end, data running back against the clock)
    # and hold reads it at 0 (either of the two placements where the data chases the clock),
    # so the two legs sit at OPPOSITE ends of one row -- exactly what the per-inequality
    # corner search is for. Hold therefore gets NO flight credit, where PM hold banks
    # +2·t_PD, and that difference is most of why P->P hold is the tightest leg here.
    #
    # THREE CROSSINGS ENTER, and only two of them are correlated:
    #
    #   clk@A    A's detection of the clock, where its launch is referenced
    #   data@B   B's detection of A's data
    #   clk@B    B's detection of the clock, which IS its sample edge
    #
    # clk@B and data@B are the same die at nearly the same instant, so they are correlated
    # and their difference is exactly the Delta_cross,MP construction -- which is why these
    # legs reuse DELTA_SETUP / DELTA_HOLD verbatim rather than coining an envelope. clk@A is
    # an unrelated part and is charged IN FULL, at the end of the slew tolerance band that
    # hurts, because the two peripherals may be assumed to share the slew SETTING (one
    # clock, one configuration) but not the tolerance around it.
    #
    # Ported from the standalone tool's `sigma_pp_setup` / `sigma_pp_hold`, where the same
    # decomposition is derived; there the pair is labelled HYPOTHETICAL because P->P
    # communication was out of scope. Reproducing its numbers is a test, not a coincidence.
    pp_clk_late_p = _per_clk_cross_parts(inp, env, corner="late")
    pp_clk_early_p = _per_clk_cross_parts(inp, env, corner="early")
    # Same rule as the contention legs' bus term and the handover UI: a flight term that the
    # placement does not levy is dropped rather than printed as a zero (`_handover_ui_terms`).
    def _pp_pd_terms(*, is_setup: bool) -> list[Term]:
        """The placement's traversals, ONE TERM EACH so the leg can interleave them.

        Two is the only non-zero count (`pp_placement` — driver far, sampler near), and it is
        two DIFFERENT flights: the clock reaching A at the far end, then A's data running back
        against it to B. So they bracket A's launch rather than sitting together, and each
        names its own flight. A count of zero emits nothing, as before: there the geometry
        makes it structurally absent, not zero-valued.
        """
        n = pp_net_pd(inp.pp_placement, is_setup=is_setup)
        notes = (_PD_TO_LAUNCHER.replace("the launching peripheral", "A, at the far end"),
                 _PD_FROM_LAUNCHER.replace("the sampler", "B"))
        return [_pd_term(t_PD, credit=not is_setup, note=notes[i]) for i in range(int(n))]

    pp_setup_pd_ns = pp_net_pd(inp.pp_placement, is_setup=True) * t_PD

    # Annotated so `_name` keeps the `Inequality` Literal rather than widening to `str`,
    # which the breakdowns dict and InequalityBreakdown.name both require. Same reason
    # `CrossLane` is aliased at its definition.
    _pp_setup_legs: tuple[tuple[Inequality, str, float, str], ...] = (
        ("PP_setup", "Per_t_DD,pure", per_tdd_max_pure,
         _conv_note(inp.Per_spec, "Per", inp.Per_t_DD_max_ns, per_tdd_max_pure,
                    inp.tRF_Per_DATA_ns)),
        ("PP_setup_ho", "Per_t_ZD,pure", per_tzd_max_pure,
         _conv_note(inp.Per_spec, "Per", inp.Per_t_ZD_ns, per_tzd_max_pure,
                    inp.tRF_Per_DATA_ns)),
    )
    for _name, _launch_sym, _launch_ns, _launch_note in _pp_setup_legs:
        _required = (_launch_ns + inp.Per_t_IS_max_ns + pp_setup_pd_ns
                     + pp_clk_late_p[0] + delta_MP_setup_ns)
        _pd = _pp_pd_terms(is_setup=True)
        _b_clk, _b_data = _split_cross_terms(
            _cross_terms(inp, "X_PP,B", DELTA_SETUP_PP))
        # THE TWO CLOCK CROSSINGS STAY ADJACENT, which is what lets
        # `_gather_lane_terms` fold them into one product: they read the same lane, so
        # `(X_PP,A,late − X_PP,B,early) · Man_t_RF,CLK` is exact. That one term therefore
        # spans TWO instants — A recognising the edge and B recognising it — and sits at
        # the earlier of them, A's, which is the one the launch follows.
        _terms = [
            Term("UI", +UI, op="+"),
            *_pd[:1],
            Term("X_PP,A,late · Man_t_RF,CLK", -pp_clk_late_p[0], op="-",
                 note="A's own clock detection, charged in full: an unrelated part — "
                      + _coeff_note(pp_clk_late_p),
                 factors=pp_clk_late_p[1:]),
            *_b_clk,
            Term(_launch_sym, -_launch_ns, op="-",
                 note=_pp_device_note("A, the driver", "its launch", _launch_note)),
            *_pd[1:],
            *_b_data,
            Term("Per_t_IS", -inp.Per_t_IS_max_ns, op="-",
                 note=_pp_device_note("B, the sampler", "its setup window")),
        ]
        breakdowns[_name] = InequalityBreakdown(
            name=_name, terms=_terms, required_ns=_required, margin_ns=UI - _required,
            F_max_MHz=(1000.0 * inp.duty_min / _required) if _required > 0 else math.inf,
            is_setup=True)

    _pp_hold_legs: tuple[tuple[Inequality, str, float, str, bool], ...] = (
        ("PP_hold", "Per_t_DD,pure", per_tdd_min_pure,
         _conv_note(inp.Per_spec, "Per", inp.Per_t_DD_min_ns, per_tdd_min_pure,
                    inp.tRF_Per_DATA_ns), False),
        # The handover UI credits hold and not setup, exactly as on MP/PM: it puts the
        # acquirer's turn-on a UI further from the sampling edge, while setup's sampling
        # edge moves with the launch and nets out.
        ("PP_hold_ho", "Per_t_ZD,pure", per_tzd_min_pure,
         _conv_note(inp.Per_spec, "Per", inp.Per_t_ZD_ns, per_tzd_min_pure,
                    inp.tRF_Per_DATA_ns), True),
    )
    for _name, _launch_sym, _launch_ns, _launch_note, _ho in _pp_hold_legs:
        # A DRIVES ON EVERY P->P LEG, BUT ON A HOLD LEG ITS LAUNCH IS THE AGGRESSOR, and on
        # the handover variant the level it destroys is not even A's. Worth spelling out per
        # leg, because "A, the driver" is true on all four and says the same thing about two
        # different situations:
        #
        #   PP_hold      continuous transmission. A drove the previous UI and drives the
        #                next, so the level B is still holding onto at the sampling edge is
        #                A's OWN earlier bit, ended by A's next launch.
        #   PP_hold_ho   a handover. A has just ACQUIRED the bus, so the level B holds onto
        #                belongs to the peripheral that just RELEASED -- a third device, which
        #                appears in this leg only as the owner of the level at risk. A's
        #                turn-on out of high-Z is what ends it.
        #
        # AND THAT ABSENCE IS WHY `PP_hold_ho` BINDS while its three neighbours do not. If the
        # releaser does not appear in the arithmetic, the leg cannot be asserting anything about
        # WHOSE data it is -- so when the Manager is the releaser, this leg is bounding a
        # Manager-to-peripheral hold requirement under a P->P name. See `_PP_DATA_LEGS`.
        #
        # That distinction is also what separates this leg from PP_contention next door: they
        # involve the same two drivers and bound different failures. Contention asks whether A
        # and the releaser are ever driving AT ONCE (a current, damaging drivers); this asks
        # whether A's turn-on ends the releaser's LEVEL before B has finished sampling it (a
        # lost bit). Either can fail with the other passing.
        _who = "A, the acquiring driver" if _ho else "A, the driver"
        _level = ("its launch out of high-Z, which ends the RELEASING peripheral's level"
                  if _ho else "its launch, which ends its own previous level")
        _pd = _pp_pd_terms(is_setup=False)
        _b_clk, _b_data = _split_cross_terms(
            _cross_terms(inp, "X_PP,B", DELTA_HOLD_PP))
        _terms = [
            *(_handover_ui_terms(inp, UI) if _ho else []),
            *_pd[:1],
            Term("X_PP,A,early · Man_t_RF,CLK", +pp_clk_early_p[0], op="+",
                 note="A's own clock detection, and it enters as a CREDIT because A's "
                      "launch is referenced to it: detecting later launches later and "
                      "leaves the level valid longer. The EARLY end is therefore the "
                      "worst corner — the smallest credit, not a helpful one — "
                      + _coeff_note(pp_clk_early_p),
                 factors=pp_clk_early_p[1:]),
            *_b_clk,
            Term(_launch_sym, +_launch_ns, op="+",
                 note=_pp_device_note(_who, _level, _launch_note)),
            *_pd[1:],
            *_b_data,
            Term("Per_t_IH", -inp.Per_t_IH_max_ns, op="-",
                 note=_pp_device_note("B, the sampler", "its hold window")),
        ]
        _margin = sum(_t.value_ns for _t in _terms)
        breakdowns[_name] = InequalityBreakdown(
            name=_name, terms=_terms, required_ns=0.0, margin_ns=_margin,
            F_max_MHz=math.inf if _margin >= 0 else 0.0, is_setup=False)

    # ----- Handover non-contention, both directions -----
    #
    #   N_HO·UI + t_ZD,min(acquiring) − t_DZ,max(releasing) + t_PD ≥ 0
    #
    # The final term is the overlap allowance and it is the whole reason a zero-UI
    # handover is arguable at all: two drivers may be on together for one flight time
    # without contending. See the criterion note below. Reading the margin against
    # the overlap it permits, where O is how long both drivers are on:
    #
    #   margin  =  t_PD − O
    #     > t_PD    an undriven (float) gap of (margin − t_PD), which the Manager's
    #               bus keeper holds the level through (§10.1.4.4.2, {ASW3805})
    #     = t_PD    the two drivers meet exactly, O = 0
    #     = 0       O = t_PD, the limit -- current still bounded by V/Z0
    #     < 0       CONTENTION: two drivers fighting, which nothing in the spec
    #               permits for an audio-mode data handover
    #
    # The two reference edges are N_HO UIs apart, which is what makes the raw
    # numbers legible. On a shared edge PHY2 would read 2.0 − 10.0 + 2.0 = −6 ns,
    # i.e. guaranteed contention; it is the dedicated handover UI that pays for it.
    # PHY1 needs no such UI because its own numbers already satisfy the
    # inequality at N_HO = 0 (14.0 ≥ 10.0) — the spec says so directly in the
    # §11.1.5 note: "handover between outputs without needing extra UIs due to
    # the inequality satisfied by the values of the output timing parameters
    # (t_ZD_Min_Phy1 ≥ t_DZ_Max_Phy1)".
    #
    # No anchor conversion is applied to either term, unlike the setup/hold
    # legs. t_DZ and t_ZD bound DRIVER STATE (release to high-Z, and turn-on out
    # of it), not the moment data becomes readable at a threshold, so the
    # V=0-referenced `pure_output_delay` normalisation has nothing to correct.
    # t_DZ's endpoint definition still differs across specs — see TDZ_NOTE — and
    # is deliberately left verbatim rather than fudged with an offset.
    #
    # Both terms take the corner that hurts, and the auto-worst solver finds it
    # without help: t_ZD enters with a + sign here and a − sign in the "_ho"
    # setup variants, so the same single `*_t_ZD_ns` field is driven to its min
    # for contention and its max for setup. `handover_UIs` is a float so a
    # fractional allocation can be explored, and 0 is meaningful (PHY1 intra-UI,
    # or PHY2 with EndDriveEarly).
    #
    # CLOCK SKEW is the third term, and it is not optional. Each device's t_DZ /
    # t_ZD is referenced to ITS OWN clock, and the clock propagates from the
    # Manager down the bus, so a peripheral sees it t_PD late. Contention is a
    # conflict between two DRIVERS in absolute time:
    #
    #   releasing driver off at:  t_clk(releaser) + t_DZ,max
    #   acquiring driver on at:   t_clk(acquirer) + N_HO·UI + t_ZD,min
    #
    # so the margin carries [t_clk(acquirer) − t_clk(releaser)]:
    #
    #   MP  Manager releases, Peripheral acquires   +t_PD   helps
    #   PM  Peripheral releases, Manager acquires   −t_PD   HURTS
    #   PP  Peripheral releases, Peripheral acquires  ±t_PD  worst case −t_PD
    #
    # This model omitted the term until 2026-08-04, which made PM_contention
    # OPTIMISTIC by t_PD (2.0 ns on a 30 cm bus) — margin reported that no bus
    # could deliver. MP was merely pessimistic, so only PM was unsafe. Adding the
    # PP leg is what surfaced it: two peripherals have no privileged reference
    # plane between them, which forces the question of what each t_DZ/t_ZD is
    # measured against, and the answer applies to all three legs.
    #
    # PP ASSUMPTION: both peripherals are modelled as being anywhere on a bus of
    # `bus_length_cm`, so the worst-case skew is the full −t_PD (releaser at the
    # far end, acquirer at the Manager end). A real pair has a separation of its
    # own that can be shorter — this is the bounding case, not a measurement of a
    # specific topology.
    #
    # WHAT COUNTS AS CONTENTION, which is a SPICE result and not a reading of the
    # spec. Two drivers may be on together for up to the flight time between them
    # with no contention at all. When the acquirer turns on, the line at its pin is
    # a Z0 load: it launches a wave and sources V/Z0, an ordinary switching current.
    # The releaser does not know it has company until that wave arrives, one
    # traversal later, and only then is there a low-impedance path between two
    # opposing drivers. So the bound is on OVERLAP, and it is not zero:
    #
    #   overlap  =  T_off(releaser) − T_on(acquirer)  ≤  t_PD      benign
    #
    # This model required the opposite sign. It compared the two events AT THE
    # ACQUIRER'S PIN, which charges the releaser's data one traversal to get there,
    # so `margin ≥ 0` demanded a GAP of one flight time where one flight of OVERLAP
    # is in fact free. Wrong side of the physics by 2·t_PD.
    #
    # The frame goes with it: the event being bounded is two drivers being on at
    # once, which is not located at either pin, so the data-flight traversal leaves
    # the model. What remains is the clock skew — each device's t_DZ/t_ZD is
    # referenced to its own clock arrival — plus the allowance.
    #
    # ANYONE PATCHING THE OLD FORM: the allowance is +2·t_PD in the pin frame, not
    # +t_PD, because that frame has already spent one flight demanding a gap. Adding
    # a single t_PD to the old margins lands exactly half-way and looks plausible.
    #
    # NO NEW PARAMETER. The two contending devices are taken to sit at opposite ends
    # of the bus, so their separation IS `t_PD` — and the bus row already sweeps
    # 0..30 cm, which lets the corner search find whichever end binds each leg. The
    # short end is now the one that binds MP, since a co-located pair has no
    # allowance to spend.
    #
    # WHERE THE LEVEL IS COVERED, since this bounds driver CURRENT and not the
    # voltage on the line during the overlap. The `*_hold_ho` legs own that: the bit
    # sampled at the UI-opening edge must stay valid for t_IH,max past it, and the
    # acquiring device's turn-on out of high-Z is what ends it. Both are live at
    # N_HO = 0, and MP_hold_ho is the thinnest leg in the EDE column -- so allowing
    # an overlap here does not hand ground to nobody. The case that used to be uncovered --
    # a peripheral receiving while another peripheral drives -- now has its own setup/hold
    # pair; see the PP block above.
    t_PD = inp.t_PD_ns()
    # The peripheral's clock crossing, at the two corners the directions need.
    # Priced through the Sigma_cross envelope like every other crossing in this
    # file -- see `_per_clk_cross_ns`.
    clk_late_p = _per_clk_cross_parts(inp, env, corner="late")
    clk_early_p = _per_clk_cross_parts(inp, env, corner="early")
    # P->P has two unrelated peripherals on it, so EACH is cornered on its own:
    # the acquirer's credit at the smallest the band allows, the releaser's charge at
    # the largest. Both are asked independently rather than relying on the corner
    # search happening to pick the end one of them needs. See `_per_clk_cross_ns`.
    ZERO_P = (0.0, 0.0, 0.0)
    # Notes travel with the values so a snapped grid point is visible in the
    # substitution, not only in the number.
    man_dz_ns, man_dz_note = _man_tdz_parts(inp)
    man_zd_ns, man_zd_note = _man_clocked_grid_ns(inp, inp.Man_t_ZD_ns)

    # THE FRAME, stated once because all three legs share it and getting it wrong is
    # worth up to 11 ns. Every event is placed absolutely and then differenced:
    #
    #   acquirer drives at      N_HO*UI + a_pd + [detect if Per] + t_ZD,min
    #   releaser is off at              r_pd + [detect if Per] + t_DZ,eff
    #   ...and the two may OVERLAP by                            t_PD
    #
    # a_pd is the acquiring device's clock arrival delay, r_pd the releasing one's.
    # A Manager sources the clock, so both are zero for it; a peripheral pays t_PD.
    # No traversal is charged for the releaser's data: the drivers are compared
    # against each other in absolute time, not at one device's pin -- see the
    # criterion note above for why that changed and by how much.
    #
    # ONE FORM FOR ALL THREE LEGS, and every term emitted even where it is zero.
    #
    # That is this file's existing convention -- `Term.op` is kept explicit so a zero
    # term still renders with the right operator (Man_t_IH,max = 0 in PM_hold is the
    # precedent) -- and an earlier version of these legs broke it by guarding each
    # term with `if value:`. Three legs then had three different shapes, which makes
    # them impossible to read against each other and hides which terms a leg is
    # missing rather than showing them as zero.
    #
    # THE BUS IS ONE NET TERM, not a pair that cancels. Composed of:
    #
    #     + t_PD   if the ACQUIRER is a peripheral   (its clock arrives late, which
    #                                                 delays its turn-on: a credit)
    #     - t_PD   if the RELEASER is a peripheral   (its clock arrives late, so it
    #                                                 holds the bus longer)
    #     + t_PD   always                            (the overlap allowance: the two
    #                                                 drivers are one traversal apart)
    #
    #   M->P   +t_PD + t_PD           = +2 t_PD   -> worst at the SHORT bus
    #   P->M   -t_PD + t_PD           =  0        -> BUS-LENGTH INDEPENDENT
    #   P->P   -t_PD + t_PD           =  0        -> BUS-LENGTH INDEPENDENT
    #
    # The two peripheral-releasing legs coming out bus-independent is not a
    # coincidence to be checked against a number: the distance that makes a releasing
    # peripheral late is the same distance that licenses the overlap, so the skew and
    # the allowance are the same quantity with opposite signs. Under the old pin
    # frame these read -2 t_PD and the legs looked bus-limited -- P->P wanted the bus
    # under 10 cm -- which was an artefact of the frame, not a property of the bus.
    #
    # Emitting the pair separately showed "+1.00" and "-1.00" -- a cancelling pair at
    # the bus row's TYP value, because a leg with zero sensitivity to a row makes
    # `find_worst_corner_rows` tie to typ on purpose. Correct, and it read as though
    # 15 cm had been chosen for a reason, and invited comparison against another
    # leg's 2.00 as if the two used different bus lengths. Netted, a "0·t_PD" states
    # the structural fact instead.
    _NET_PD = {"MP_contention": +2.0, "PM_contention": 0.0, "PP_contention": 0.0}
    _contention_legs: tuple[
        tuple[Inequality, str, str, CrossParts, CrossParts, str, str], ...] = (
        # (name, releasing, acquiring, acq_cross, rel_cross, dz_note, zd_note)
        ("MP_contention", "Man", "Per", clk_early_p, ZERO_P, man_dz_note, ""),
        ("PM_contention", "Per", "Man", ZERO_P, clk_late_p, "", man_zd_note),
        # Worst topology: the releaser at the far end (latest detection), the
        # acquirer at the Manager end (earliest edge). A real pair may be closer
        # together -- this is the bounding case, not a measurement of one layout.
        # Two unrelated peripherals, so each takes its own corner: the acquirer's
        # credit is NOT the one the releaser's charge forces out of a shared row-set.
        #
        # A NEGATIVE P->P MARGIN AT N_HO = 0 IS THE EXPECTED PHY2 ANSWER, not a defect
        # this model has found.  PHY2 allocates a UI to every handover by default
        # (§11.1.1.1, `default_handover_UIs`) and that UI is precisely how it separates
        # two peripherals' drivers: P->P is not expected to close without it.  So
        # evaluating this leg at zero allocated UIs is the EndDriveEarly hypothetical
        # rather than PHY2 as specified, and what the margin reports is THE PRICE OF
        # REMOVING THE UI on the transition that needs it most.  That is a key
        # motivation for EDE, not a red mark against the bus: allocation is
        # per-transition (the gap in the transport map IS the handover), so a P->P
        # boundary that does not close costs one UI AT THAT TRANSITION while the mixed
        # ones stay free.  Read the sign of this leg as "may this handover be zero-UI?",
        # never as "is this bus broken?".
        ("PP_contention", "Per", "Per", clk_early_p, clk_late_p, "", ""),
    )
    for (name, releasing, acquiring, acq_p, rel_p,
         dz_note, zd_note) in _contention_legs:
        acq_cross, rel_cross = acq_p[0], rel_p[0]
        if releasing == "Man":
            t_dz = _effective_t_dz_ns(man_dz_ns, ede=inp.Man_ede, UI_ns=UI)
        elif inp.Per_ede:
            t_dz = _per_ede_release_vs_edge_ns(inp, corner="max")
        else:
            t_dz = inp.Per_t_DZ_max_ns
        t_zd = man_zd_ns if acquiring == "Man" else inp.Per_t_ZD_ns
        n_pd = _NET_PD[name]
        # ONE CLOCK, ONE LANE NAME. The Manager is the only source of the forwarded clock, so
        # every device's detection of it is a crossing on `Man_t_RF,CLK` -- there is no other
        # clock for it to be. A peripheral end reads that row at an END of its min/max rather
        # than at the scalar the corner search picked (`tRF_CLK_tol_ns`, seeded straight from
        # the row), because two unrelated peripherals share the slew SETTING but not the
        # tolerance around it. That is a CORNER of the lane, not a different lane, and it is
        # the note's job to say which end -- briefly spelled `t_RF,CLK,band`, which invented a
        # second name for the one clock and implied the row was not being read at all. It is:
        # narrowing `t_RF Man CLK` narrows the band with it.

        # Symbols only, in the grammar the rest of the file uses -- the two clock
        # crossings are the SETUP and HOLD clock lanes of Sigma_cross,PM, so they are
        # named for those, and the bus is a signed count of traversals like 2*t_PD is
        # elsewhere. Which corner belongs to which role, and why, is the paper's job.
        # IN TIME ORDER: both devices detect the one forwarded clock edge, then the
        # releaser lets go and the acquirer turns on, and the allowance is the flight
        # between them.
        #
        # THE TWO DETECTIONS STAY ADJACENT rather than each sitting beside the device
        # event it triggers, and that is a deliberate trade: on P->P they read the same
        # lane and `_gather_lane_terms` folds them into one exact product, which
        # separating them would forfeit. They are also very nearly simultaneous -- one
        # edge, two thresholds -- so little time order is lost by pairing them, whereas
        # the release and the turn-on are the two instants the leg is actually about.
        terms = [
            *_handover_ui_terms(inp, UI),
            Term("X_PM,late · Man_t_RF,CLK", -rel_cross, op="-",
                 note=_coeff_note(rel_p), factors=rel_p[1:]),
            Term("X_PM,early · Man_t_RF,CLK", +acq_cross, op="+",
                 note=_coeff_note(acq_p), factors=acq_p[1:]),
            Term(f"{releasing}_t_DZ" + (",EDE" if _releaser_ede(inp, releasing) else ""),
                 -t_dz, op="-", note=dz_note),
            Term(f"{acquiring}_t_ZD", +t_zd, op="+", note=zd_note),
        ]
        # THE BUS TERM IS DROPPED WHERE ITS COUNT IS ZERO, and the test is "zero for
        # every input", not "zero at this corner". P->M and P->P net to no traversals
        # at all -- the releasing peripheral's late clock and the overlap allowance are
        # the same distance with opposite signs -- so the term can never be anything but
        # zero and printing it invites the reader to look for a bus effect that is not
        # there. `0·UI` is dropped for the same reason as of this revision, though it took
        # a second look to agree: it is zero only because nothing is allocated, and the
        # argument for keeping it was that it becomes `1·UI` the moment one is -- true, but
        # not worth a row of arithmetic that contributes nothing on the very leg whose point
        # is that nothing separates the release from the turn-on. See `_handover_ui_terms`.
        #
        # What still stays is a PARAMETER that happens to read zero on this spec --
        # `Man_t_IH` -- where the zero IS the spec's value and worth seeing.
        #
        # The cost is that the three legs no longer share one shape, which was a
        # deliberate property until now: a "0·t_PD" STATED the bus-independence where
        # its absence merely fails to contradict it. That fact now lives only in the
        # criterion note and the docs.
        if n_pd:
            terms.append(Term(f"{abs(n_pd):g}·t_PD", n_pd * t_PD,
                              op="+" if n_pd >= 0 else "-"))
        margin = sum(t.value_ns for t in terms)
        breakdowns[name] = InequalityBreakdown(
            name=name,
            terms=terms,
            required_ns=0.0,
            margin_ns=margin,
            # Rate-dependent ONLY when a handover UI is allocated: at N_HO = 0
            # the margin is a difference of device constants plus a bus delay, so
            # no clock rate can fix it and none can break it.
            F_max_MHz=_contention_fmax_MHz(
                inp, t_dz_max_ns=t_dz + rel_cross - n_pd * t_PD,
                t_zd_min_ns=t_zd + acq_cross, skew_ns=0.0),
            is_setup=False,
        )

    # ----- Bus keeper: the releasing driver holds a valid level long enough ---
    #
    #   release_earliest  -  t_DD,max  >=  t_keeper
    #
    # This is the constraint that BINDS EndDriveEarly, and it is why EDE and the
    # Manager launch architecture are not independent choices: an analog
    # Man_t_DD,max of 23 ns cannot satisfy it against a mid-UI release, while the
    # 4x clocked launch (0.25 UI) can.  Without this leg the model reported EDE
    # passing on an analog launch -- a false PASS, since the bit is lost.
    #
    # Uniform across EDE and non-EDE by expressing the release in UI-START-
    # referenced terms: EDE releases at t_DZ, a conventional handover a full UI
    # later.  So a non-EDE bus gets a genuine (large) margin rather than a
    # special case.
    #
    # t_DD,max is taken AFTER the launch mode, so selecting the analog delay
    # actually costs what it should.  t_DZ enters at its MIN corner here (the
    # earliest release is the one that starves the keeper) and at its max in the
    # contention legs -- the same field pulled to opposite ends, which the
    # auto-worst solver resolves per inequality.
    #
    # t_DZ and t_ZD are NOT symmetric, and the asymmetry is what makes this exact
    # rather than approximate.  SWI3S impedance control is sawtooth (Figure 166):
    # RAMPED ON, INSTANT OFF.
    #
    #   t_DZ  the driver is FULLY high-Z.  No turn-off ramp -- it drives at
    #         strength right up to t_DZ and then stops.  So t_DZ is precisely
    #         when the level stops being driven, and the margin below is exact,
    #         not a bound.
    #   t_ZD  measured to 20 % of the driver's CONDUCTANCE ramp -- so driving has
    #         begun by then, and a valid V_OH/V_OL is still a voltage slew away.
    #
    # Which is why the contention legs above use t_ZD RAW: overlap begins as soon
    # as the acquirer's impedance starts falling and ends at the releaser's t_DZ,
    # so a NEGATIVE contention margin's magnitude IS the overlap in EXCESS of what
    # the flight time licenses -- the overlap itself is `t_PD − margin`, and neither
    # is merely a fail.  Slightly optimistic, because t_ZD is 20 % into the G-ramp
    # and conduction began just before it; that residual is unmodelled (the G-ramp
    # duration is not a parameter here) and is far smaller than the t_RF/3 that
    # `_per_tzd_pure_max` subtracts for the SETUP legs, where the question is when
    # the level becomes readable rather than when the driver engages.
    # ONE keeper, and it is in the MANAGER ({ASW3805}) -- there is no peripheral
    # keeper.  The two legs below are therefore not "the Manager's keeper margin"
    # and "the Peripheral's": they are the same single keeper, evaluated for each
    # device that can be the one RELEASING.  Both release at some handover, and
    # each must have held a valid level long enough for that one keeper to latch
    # it.  Worth spelling out because a per-side loop invites exactly the wrong
    # reading.
    #
    # No t_PD term, and that is not an omission: the keeper sits at the Manager's
    # end, so a peripheral's level reaches it t_PD late -- but the constraint is on
    # the DURATION of driven valid level, and propagation shifts that window
    # without changing its length.  t_DZ - t_DD is the same at both pins.
    #
    # ONE INEQUALITY PER RELEASING DEVICE, not one inequality reporting the worse
    # of the two, and that is a correctness requirement rather than a presentation
    # choice.  `find_worst_corner_rows` finds a worst corner by moving one row at a
    # time, which is exact only for a margin that is AFFINE in each parameter.  A
    # min over two legs is not affine, and it failed in the way a saturating
    # objective always does: on the EDE column the Manager's leg sits at exactly
    # 0.00 ns, so no single move of a PERIPHERAL row could lower min(0.00, per) --
    # each one alone left the reported margin at 0.00 -- and the search stopped
    # with the peripheral rows at their typicals.  Reaching the peripheral's own
    # worst corner needs Per_t_DZ,min and Per_t_DD,max together, a two-coordinate
    # move no amount of iterating one coordinate at a time will make.
    #
    # It reported +0.00 at 12.6 MHz where the peripheral leg is -0.10, and +0.00 at
    # 13.2 MHz -- PHY2's mandatory maximum -- where it is -0.64.  A starved keeper
    # loses the bit, so that was a false PASS on the leg that binds the EDE rate.
    # Split, each leg is affine, the one-at-a-time search is exact again, and the
    # results tree names which device is short instead of leaving the reader to
    # work out whose corner they are looking at.
    # THE ADDEND IS THE WHOLE SWING, NOT t_keeper.
    #
    #     t_DD,max + t_RF/(V_OH,min-V_OL,max)
    #             + [t_keeper, only if the keeper must OBSERVE]  <=  release
    #
    # The swing addend is written out rather than given a symbol of its own: t_RF is a
    # 20-80 % measurement (Table 127) so it spans 0.60 of the transition, and the full
    # one is 5/3 of it. See `_swing_ns` for why a coined `t_swing` was the wrong way to
    # show that.
    #
    # The keeper is a high-impedance HOLDER, not a driver: it cannot pull the
    # remaining swing to the rail inside a UI, or inside several, so whatever level
    # the driver hands over is what the receiver samples. The ramp must therefore
    # FINISH before drive stops.
    #
    # This leg used to charge `t_keeper` alone (3 ns where the swing is 13.83), which
    # under-charged it by 10.83 ns. That error very nearly cancelled a second one --
    # a Manager launch at 0.50 UI rather than 0.25 -- so the reported margin looked
    # right (+6.16 against a true +4.48) while both inputs were wrong by ten
    # nanoseconds in opposite directions. Correcting the addend ALONE takes the
    # 0.50 UI launch to -4.68, i.e. a hard failure; the placement had to move too.
    #
    # Both t_DD and t_DZ are referenced to the same 0 % point -- the ramp begins
    # there, and the driver goes high-Z there -- so nothing is added or subtracted
    # and the addend is the full rail-to-rail time. Charging t_RF instead stops at
    # V_OH rather than at the rail the keeper cannot reach.
    #
    # MAN_TKEEPER_RESPONSE IS CHARGED UNCONDITIONALLY, and it used to be optional.  A
    # `keeper_forced` flag zeroed it on the argument that a Manager which FORCES the
    # keeper's state is telling it the value rather than making it observe the bus, so its
    # response time does not enter -- and the flag defaulted to forced, so the term
    # contributed nothing to any published margin.
    #
    # Table 125 does not describe the keeper's mechanism.  It requires THE DATA LANE TO BE
    # STABLE AT THE MANAGER PIN for 3 ns, which is a demand on the wire: a keeper told its
    # value does not make the lane settled, so nothing about forcing removes the
    # requirement.  Whether the keeper observes or is told is a design choice; whether the
    # lane held still long enough is not, and it is the lane the table bounds.
    #
    # It costs 3 ns off every keeper margin (at 13.2 MHz: Man +17.76 -> +14.76, Per
    # +15.76 -> +12.76) and it tightens every EDE placement argument by the same 3 ns.
    #
    # ONE INEQUALITY PER RELEASING DEVICE, not one reporting the worse of the two,
    # and that is a correctness requirement rather than a presentation choice.
    # `find_worst_corner_rows` moves one row at a time, which is exact only for a
    # margin that is AFFINE in each parameter. A min over two legs is not affine and
    # it failed the way a saturating objective always does: wherever one leg bound,
    # the gradient with respect to every parameter of the other vanished, so the
    # search read those rows as insensitive and left them at their typicals.
    # A 0 ns requirement (a corner sweep's low end, or a spec that states none) drops the
    # term rather than printing a zero row.
    keeper_term = inp.t_keeper_ns
    man_swing = _swing_ns(inp, inp.tRF_Man_DATA_ns)
    per_swing = _swing_ns(inp, inp.tRF_Per_DATA_ns)

    # --- the Manager releasing -------------------------------------------------
    #
    # THE UI IS A TERM, NOT AN ADJUSTMENT FOLDED INTO t_DZ. t_DD and t_ZD are
    # referenced to the edge that OPENS the UI; a conventional release is referenced
    # to the NEXT edge, the one that closes the last driven UI (Table 129 Note 1). So
    # the driver keeps driving PAST that edge and into the following UI, and stops at
    # `UI + t_DZ`. Under EDE the release moves inside the driven UI instead, which is
    # what the feature does, so there is no UI to add.
    #
    # THE CORNER IS THE SEARCH'S JOB, NOT THE LABEL'S. This leg wants the EARLIEST
    # release, so `find_worst_corner_rows` drives the one `*_t_DZ_max_ns` field to its
    # row's low end here while the contention legs pull the same field to the high end.
    # The term is therefore labelled `Man_t_DZ` with no corner suffix: writing
    # `Man_t_DZ,min` asserts a guaranteed-earliest release that the spec does not
    # define -- t_DZ is a MAXIMUM, "high-Z by this time" -- and a coined minimum in a
    # displayed inequality reads as a promise. The note records which end is in force.
    #
    # Under EDE the value is a PLACEMENT rather than a bound -- a clocked grid point,
    # deterministic, min == max -- so there the suffix is honest and it is
    # `Man_t_DZ,EDE`.
    #
    # THIS LEG HAS BEEN WRONG IN BOTH DIRECTIONS, one after the other:
    #
    #   * the UI was added INSIDE the t_DZ term's value while the term was labelled
    #     `Man_t_DZ,min`, so on PHY2 at 12.288 MHz it read 36.62 where the parameter is
    #     0 -- the non-EDE leg rendered in EDE's form with a UI hidden in it;
    #   * fixing that label, the t_DZ term was then dropped ENTIRELY, on the argument
    #     that a maximum cannot bound the earliest release. That over-corrected: the
    #     driver does go on driving into the next UI, and removing the term charged the
    #     keeper for a release the device does not make.
    #
    # Both are fixed by splitting the two terms and letting the corner search say
    # which end of t_DZ each leg reads.
    # The two cases are ONE SHAPE with a different release point: the closing edge plus
    # t_DZ (so `1*UI + Man_t_DZ`) or the EDE placement, and nothing else changes.
    # THE LAUNCH IS THE PURE ONE, and using the tabulated value here was an arithmetic
    # double-count. Figure 174 measures Per_t_DD from the clock's V_IH/V_IL crossing to 20 %
    # OF THE RAMP, so a tabulated value already contains the first fifth of the transition.
    # Adding the FULL rail-to-rail swing on top of it charges that fifth twice -- exactly
    # `offset·t_RF,nom` = 1.667 ns too much on every peripheral-releasing keeper leg.
    #
    # Either form is right as long as the pair is consistent: raw t_DD + the 20 %-to-rail
    # remainder, or pure t_DD + the full swing. The second is used because the swing addend
    # is shared with nothing else and `_swing_ns` is already the full 0 %-to-rail time.
    #
    # A SWI3S MANAGER's t_DD is measured between SYMMETRIC anchors (Fig. 176), so its pure
    # value equals its tabulated one and this changes nothing on that side -- which is why
    # the error only ever showed on keeper_Per.
    man_dd_max = _man_tdd_pure_max(inp)
    # `man_dz_ns` / `man_dz_note` are the contention legs' own values, reused here
    # rather than recomputed, and that reuse is the point: this leg used to read
    # `inp.Man_t_DZ_max_ns` RAW while the contention legs read it snapped to the launch
    # mode's grid, so one output stage had two release times and each family kept the
    # end that suited it. See `_man_tdz_parts`.
    man_keeper_terms = (
        [Term("Man_t_DZ,EDE", +man_dz_ns, op="+", note=man_dz_note)] if inp.Man_ede
        else [Term("1·UI", +UI, op="+"),
              Term("Man_t_DZ", +man_dz_ns, op="+",
                   note="; ".join(n for n in (man_dz_note,
                                              _release_note(man_dz_ns)) if n))]
    ) + [
        Term("Man_t_DD,pure", -man_dd_max, op="-"),
        Term(_swing_sym(inp, "Man DATA"), -man_swing, op="-",
             note=_swing_note(inp, inp.tRF_Man_DATA_ns),
             factors=(_swing_coeff(inp), inp.tRF_Man_DATA_ns)),
    ]
    if keeper_term:
        man_keeper_terms.append(Term("Man_t_Keeper_Response", -keeper_term, op="-",
                                     note=_keeper_note(inp)))
    man_keeper_margin = sum(t.value_ns for t in man_keeper_terms)
    breakdowns["keeper_Man"] = InequalityBreakdown(
        name="keeper_Man", terms=man_keeper_terms, required_ns=0.0,
        margin_ns=man_keeper_margin,
        F_max_MHz=math.inf if man_keeper_margin >= 0 else 0.0, is_setup=False)

    # --- a peripheral releasing ------------------------------------------------
    #
    # UNDER EDE THIS IS SATISFIED BY CONSTRUCTION, and the leg says so rather than
    # arriving there by arithmetic. "Release after your own ramp completes" IS the
    # keeper inequality, so what is left over is the hold interval itself -- and its
    # minimum is exactly Man_tKeeper_Response, so the leg closes even with the
    # keeper observing. The Manager side needs a placement argument to get here.
    #
    # Without EDE the peripheral drives its whole UI and then lets go, so the
    # earliest release is the closing edge itself (Table 129 gives Per_t_DZ a
    # minimum of 0) and the leg has a full UI of driven level to play with.
    if inp.Per_ede:
        per_keeper_terms = [
            Term("Per_t_hold,min", +inp.Per_ede_hold_min_ns, op="+"),
        ]
        if keeper_term:
            per_keeper_terms.append(Term("Man_t_Keeper_Response", -keeper_term, op="-",
                                         note=_keeper_note(inp)))
    else:
        per_keeper_terms = [
            # Same release point as the Manager's conventional leg: the peripheral
            # drives its UI and goes on driving into the next one until t_DZ. The
            # corner search reads the low end of the Per_t_DZ row here, which is what
            # makes this the earliest release rather than the guaranteed-by one.
            Term("1·UI", +UI, op="+"),
            Term("Per_t_DZ", +inp.Per_t_DZ_max_ns, op="+",
                 note=_release_note(inp.Per_t_DZ_max_ns)),
            Term("Per_t_DD,pure", -per_tdd_max_pure, op="-",
                 note=_conv_note(inp.Per_spec, "Per", inp.Per_t_DD_max_ns,
                                 per_tdd_max_pure, inp.tRF_Per_DATA_ns)),
            Term(_swing_sym(inp, "Per DATA"), -per_swing, op="-",
                 note=_swing_note(inp, inp.tRF_Per_DATA_ns),
                 factors=(_swing_coeff(inp), inp.tRF_Per_DATA_ns)),
        ]
        if keeper_term:
            per_keeper_terms.append(Term("Man_t_Keeper_Response", -keeper_term, op="-",
                                         note=_keeper_note(inp)))
    per_keeper_margin = sum(t.value_ns for t in per_keeper_terms)
    breakdowns["keeper_Per"] = InequalityBreakdown(
        name="keeper_Per", terms=per_keeper_terms, required_ns=0.0,
        margin_ns=per_keeper_margin,
        F_max_MHz=math.inf if per_keeper_margin >= 0 else 0.0, is_setup=False)

    # Cross-spec advisories.  A SoundWire receiver is the one exposed to the
    # Schmitt question; when both sides are SWI3S the assumption is sound and
    # the cost is not reported.
    sw_rx = is_soundwire(inp.Man_spec) or is_soundwire(inp.Per_spec)
    schmitt_cost = 0.0
    caveats: list[str] = []
    if sw_rx:
        schmitt_cost = max(
            schmitt_delta_ns(shape=inp.Man_shape, tRF_ns=inp.tRF_Man_DATA_ns,
                             V_IH_max_frac=inp.V_IH_max_frac,
                             V_IL_max_frac=inp.V_IL_max_frac,
                             VDD_tol=inp.V_SEOS_tol_frac,
                             noise_frac=inp.V_noise_pp_frac,
                             tRF_tolerance=inp.tRF_tolerance_frac),
            schmitt_delta_ns(shape=inp.Per_shape, tRF_ns=inp.tRF_Per_DATA_ns,
                             V_IH_max_frac=inp.V_IH_max_frac,
                             V_IL_max_frac=inp.V_IL_max_frac,
                             VDD_tol=inp.V_SEOS_tol_frac,
                             noise_frac=inp.V_noise_pp_frac,
                             tRF_tolerance=inp.tRF_tolerance_frac),
        )
        caveats = [THRESHOLD_NOTE, TRF_NOTE, TDZ_NOTE]
    if is_proposed(inp.Man_spec) or is_proposed(inp.Per_spec):
        # Prepended: an unratified number is the first thing a reader must know
        # about a result, ahead of any cross-spec modelling caveat.
        caveats = [PROPOSAL_NOTE, PROPOSAL_UI_RELATIVE_NOTE] + caveats
    if sw_rx:
        # The handover-UI allocation is a SWI3S schedule concept, so a mixed bus
        # gets a contention margin only under an assumption worth stating.
        caveats = caveats + [HANDOVER_CROSS_SPEC_NOTE]

    return CalcResults(
        inputs=inp,
        delta_cross_MP_setup_ns=delta_MP_setup_ns,
        delta_cross_MP_hold_ns=delta_MP_hold_ns,
        sigma_pm_setup_ns=sigma_setup_ns,
        sigma_pm_hold_ns=sigma_hold_ns,
        breakdowns=breakdowns,
        F_CLK_ceiling_MHz=inp.F_CLK_ceiling_MHz(),
        F_CLK_legal=inp.F_CLK_is_legal(),
        schmitt_cost_ns=schmitt_cost,
        caveats=tuple(caveats),
    )


# ---------------------------------------------------------------------------
# Worst-case corner solver (per inequality)
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Worst-case corner solver (per inequality, row-centric)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CalcRow:
    """A UI-level parameter row.  The picked value (one of min/typ/max from
    ``range``) is applied to every CalcInputs field in ``binds``.

    For ranged spec parameters (t_DD, t_ZD), ``binds`` lists both the
    min and max bound fields, so picking a single value collapses the band
    to that value across all inequalities.  Per-inequality auto-worst then
    selects the band edge that worst-cases each inequality independently.

    ``is_window`` marks a row that must NOT be collapsed, and it exists because
    collapsing is only sound for a parameter that ONE device realises.  A receiver
    threshold is a per-device corner -- a given part's rising threshold sits somewhere
    in [V_IH,min, V_IH,max] -- so on a leg that nets TWO receivers' crossings against
    each other, the worst case is one device at each end, which no single collapsed
    value can express.  A window row keeps both ends in force (``binds`` is applied
    as (min-field, max-field)) and the picker does not perturb it: the envelope already
    corners each ROLE at the end that role needs, so the intact window IS the
    independent per-device cornering rather than a refusal to search.

    Collapsing V_IH cost 2.38 ns of phantom margin on P->P contention and both P->P hold
    legs, and nothing anywhere else: within a single receiver what matters is the
    V_IH-to-V_IL separation, which a collapse preserves, while ACROSS two receivers it is
    the window's WIDTH, which a collapse destroys.  It could only ever read at or above
    the spec window's own answer, so a worst-corner search that collapsed it was
    searching a subspace that excluded the worst corner.

    ``linked_to``, if set, names another row in the same list whose pick
    this row mirrors.  Linked rows have their own min/typ/max numeric
    values (so the two lanes can be calibrated independently) but share a
    single corner pick — used to tie Man CLK and Man DATA t_RF so
    auto-worst can't put them at opposite spec edges on the same chip.
    """
    name: str
    binds: tuple[str, ...]
    range: ParamRange
    linked_to: str | None = None
    is_window: bool = False


def apply_row_picks(
    base: CalcInputs,
    rows: list[CalcRow],
    picks: dict[str, Pick],
) -> CalcInputs:
    """Apply each row's picked value to all of its bound CalcInputs fields.
    Linked rows use the master row's pick.

    A WINDOW ROW IS NOT COLLAPSED: its two bound fields take the range's two ends, so
    both remain in force and the envelope can corner two receivers at opposite ends of
    one compliance window.  See `CalcRow.is_window`.
    """
    overrides: dict[str, float] = {}
    for row in rows:
        if row.is_window:
            lo_field, hi_field = row.binds        # (min-bound, max-bound), in that order
            overrides[lo_field] = row.range.min
            overrides[hi_field] = row.range.max
            continue
        master = row.linked_to if row.linked_to else row.name
        pick = picks.get(master, "typ")
        val = row.range.at(pick)
        for field in row.binds:
            overrides[field] = val
    # ``overrides`` only ever holds the numeric (float) fields a CalcRow binds —
    # never the Literal-typed spec/shape fields — but mypy can't see that through
    # the **kwargs, so it matches them against the Literal members and complains.
    return replace(base, **overrides)      # type: ignore[arg-type]


def find_worst_corner_rows(
    inequality: Inequality,
    rows: list[CalcRow],
    base: CalcInputs,
) -> dict[str, Pick]:
    """For each MASTER row, pick min/typ/max to minimize the inequality's
    margin.  Linked rows aren't perturbed independently; their pick
    follows the master row.  Returns picks keyed by master row name only.

    ONE row is perturbed at a time, against a base of typicals, and the picks are
    then applied together.  That is exact under a condition every inequality here
    must meet: THE MARGIN IS MONOTONE IN EACH PARAMETER, in a direction that does
    not depend on the other parameters.  Affine margins satisfy it trivially, and
    all of these are affine in every parameter but t_RF, where the RSS crossing
    terms are monotone without being linear -- still fine, since only the
    direction has to hold.

    It is a real condition and not a technicality.  ``keeper`` used to be reported
    as the worse of its two legs, i.e. a MIN of two affine functions, and the
    gradient of a min is zero wherever the other leg binds -- so the direction
    does not merely change magnitude, it vanishes.  On the EDE column the
    Manager's leg sits at exactly 0.00 ns, so every single move of a peripheral
    row left min(0.00, per) at 0.00, the search read that as insensitivity, and
    the peripheral leg was never driven to its own corner.  Reaching it needs
    Per_t_DZ,min and Per_t_DD,max together -- a two-coordinate move.

    So a NEW inequality that is not monotone in this sense must be SPLIT into
    monotone parts, the way the keeper now is, rather than answered by making this
    search cleverer.  Iterating it to a fixed point was tried and does not work:
    coordinate descent cannot leave a local minimum that needs two coordinates to
    move at once, and it reported a comfortable +0.00 ns at 13.2 MHz on a leg that
    is actually -0.64.  A cleverer search would have to be a real one (exhaustive
    over the reading rows, or a proof obligation per inequality); splitting keeps
    the search trivially correct instead, and names the failing device while it is
    at it.

    WINDOW ROWS ARE NOT PERTURBED and get no pick, which is not a gap in the search.
    Their two ends are BOTH in force, and the envelope reads whichever end each role
    needs, so the worst corner over two receivers' thresholds is already the value being
    evaluated -- perturbing such a row could only narrow the window and report a better
    margin than the specification allows.  See `CalcRow.is_window`.
    """
    masters = [r for r in rows if r.linked_to is None and not r.is_window]
    base_picks: dict[str, Pick] = {row.name: "typ" for row in masters}
    out: dict[str, Pick] = {}
    for row in masters:
        margins: dict[Pick, float] = {}
        pick: Pick
        for pick in ("min", "typ", "max"):
            test_picks: dict[str, Pick] = {**base_picks, row.name: pick}
            test_inputs = apply_row_picks(base, rows, test_picks)
            r = compute(test_inputs)
            margins[pick] = r.breakdowns[inequality].margin_ns
        smallest = min(margins.values())
        # Ties go to typ, so an insensitive row is not reported as if its corner
        # had been chosen for a reason.
        if math.isclose(margins["typ"], smallest, abs_tol=1e-12):
            out[row.name] = "typ"
        else:
            out[row.name] = min(("min", "max"), key=lambda p: margins[p])
    return out


def default_swi3s_rows(lock_mgr_lanes: bool = True) -> list[CalcRow]:
    """Every parameter the calculator UI exposes, as a row.

    Ranged spec parameters (V_IH, V_IL, Man/Per_t_DD) bind to BOTH the
    min and max fields of CalcInputs — picking a value collapses the band
    to that value.  Single-bound spec parameters (t_IS, t_IH, t_DZ, t_ZD)
    bind to one field; the picker selects min/typ/max for what-if
    exploration.  Operating-point parameters (α, δ, τ, t_RF lanes, bus
    geometry) bind to one field each with the natural corner range.

    When ``lock_mgr_lanes`` is True (default), the Man DATA t_RF row links
    its corner pick to the Man CLK row, so auto-worst can't drive the
    same Mgr chip's CLK and DATA lanes to opposite spec edges.  Numeric
    min/typ/max values for the two lanes remain independently editable.
    """
    return [
        # Receiver thresholds: a COMPLIANCE WINDOW, kept as one. Each device's own
        # rising/falling threshold is a corner of THAT device somewhere inside it, so
        # two receivers can sit at opposite ends at the same time — which is the worst
        # case on every leg that nets two peripherals' crossings. A pick that collapsed
        # the window asserted one threshold for the whole bus and was worth 2.38 ns of
        # phantom margin on P→P contention and both P→P hold legs.
        CalcRow("V_IH", ("V_IH_min_frac", "V_IH_max_frac"),
                ParamRange(0.45, 0.55, 0.65), is_window=True),
        CalcRow("V_IL", ("V_IL_min_frac", "V_IL_max_frac"),
                ParamRange(0.35, 0.45, 0.55), is_window=True),

        # Output timing range (picker collapses min/max to one value)
        CalcRow("Man_t_DD", ("Man_t_DD_min_ns", "Man_t_DD_max_ns"),
                ParamRange(7.0, 12.5, 18.0)),
        CalcRow("Per_t_DD", ("Per_t_DD_min_ns", "Per_t_DD_max_ns"),
                ParamRange(2.0, 11.0, 20.0)),

        # Input timing demands (single-bound, max RX requirement). The ",max" is
        # dropped from the label — the min/max columns already convey the corner.
        CalcRow("Man_t_IS", ("Man_t_IS_max_ns",),
                ParamRange(0.0, 1.5, 3.0)),
        CalcRow("Man_t_IH", ("Man_t_IH_max_ns",),
                ParamRange(0.0, 0.0, 0.0)),
        CalcRow("Per_t_IS", ("Per_t_IS_max_ns",),
                ParamRange(0.0, 2.0, 4.0)),
        CalcRow("Per_t_IH", ("Per_t_IH_max_ns",),
                ParamRange(0.0, 2.25, 4.5)),

        # Z-handover. t_ZD is a single parameter; the handover setup inequalities
        # take its max corner. t_DZ is read by the contention legs (at its max) and by the
        # keeper legs (at its min — the earliest release is the one that starves the
        # keeper); the parenthetical here called it "unused by the inequalities", which
        # stopped being true when those legs landed.
        CalcRow("Man_t_DZ", ("Man_t_DZ_max_ns",),
                ParamRange(0.0, 5.0, 10.0)),
        CalcRow("Per_t_DZ", ("Per_t_DZ_max_ns",),
                ParamRange(0.0, 5.0, 10.0)),
        CalcRow("Man_t_ZD", ("Man_t_ZD_ns",),
                ParamRange(2.0, 10.0, 18.0)),
        CalcRow("Per_t_ZD", ("Per_t_ZD_ns",),
                ParamRange(2.0, 10.0, 18.0)),

        # Slew per lane.  Mgr DATA's corner pick is tied to Mgr CLK when
        # lock_mgr_lanes is on; numeric values remain independent.
        CalcRow("t_RF Man CLK",  ("tRF_Man_CLK_ns",),  ParamRange(3.0, 5.0, 8.3)),
        CalcRow("t_RF Man DATA", ("tRF_Man_DATA_ns",), ParamRange(3.0, 5.0, 8.3),
                linked_to="t_RF Man CLK" if lock_mgr_lanes else None),
        CalcRow("t_RF Per DATA", ("tRF_Per_DATA_ns",), ParamRange(3.0, 5.0, 8.3)),

        # Noise / supply / slew tolerance
        CalcRow("δ (V_SEOS tol)",       ("V_SEOS_tol_frac",),
                ParamRange(0.0, 0.025, 0.05)),
        CalcRow("α (V_noise / V_SEOS)", ("V_noise_pp_frac",),
                ParamRange(0.0, 0.05, 0.10)),
        CalcRow("τ (per-edge slew)",    ("tRF_tolerance_frac",),
                ParamRange(0.0, 0.025, 0.05)),

        # Bus & topology. 0..30 cm: 30 cm is the design target and SoundWire's
        # "small" envelope (L_Bus <= 30 cm), and 0 is a real corner rather than a
        # placeholder — a co-packaged or adjacent Peripheral contributes no
        # propagation delay, which is the worst case for the HOLD inequalities
        # (t_PD funds PM hold, so the short end is the one that binds). Sweeping
        # from 5 cm as before understated that end for no reason.
        CalcRow("bus length (cm)",     ("bus_length_cm",),
                ParamRange(0.0, 15.0, 30.0)),
        CalcRow("t_PD,mis fraction",   ("vPCB_err_frac",),
                ParamRange(0.0, 0.05, 0.10)),
    ]
