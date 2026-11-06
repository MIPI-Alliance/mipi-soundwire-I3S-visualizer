"""SWI3S spec calculator: 17 inequalities, t_cross, F_max, term-by-term breakdown.

The legs are listed in `INEQUALITIES`; the "ADDING AN INEQUALITY" checklist follows it and
names the test that enforces each rule.

Per-lane t_RF (Man_CLK, Man_DATA, Per_DATA) and per-device ramp shape (Man, Per) are
first-class inputs. Inequality structure follows a DDR bus timing framework: setup and hold
per launch and capture, and threshold-crossing offsets.

EVERY EDGE IS PLACED BY ITS 20 % POINT, V_OL rising and V_OH falling: a launch at the anchor
its t_DD / t_ZD is measured to, the Manager's clock at its clock reference (the point its
timing is measured from), and the UI between two consecutive clock references. A crossing
coefficient counts from its edge's 20 % point (`CalcInputs.envelope`), so a crossing term is
the ramp from there to the receiver's threshold.

Each side also carries a *spec source* (``Man_spec`` / ``Per_spec``), so a
mixed SWI3S ↔ SoundWire-1.3 bus can be evaluated in the same inequalities. A
SoundWire side is carried as its SWI3S equivalents, so the inequality structure and
the anchor rules are the SWI3S ones on both sides. See
:mod:`swi3s_studio.timing.spec_source` for the equivalents and its `THRESHOLD_NOTE`,
`TRF_NOTE` and `HANDOVER_CROSS_SPEC_NOTE` for what a cross-spec selection changes.
Defaults are SWI3S on both sides, which reproduces the single-spec behaviour
exactly.
"""

from __future__ import annotations

import contextvars
import dataclasses
import functools
import itertools
import math
from dataclasses import dataclass, replace
from typing import Callable, ClassVar, Literal, Mapping, Sequence, TypedDict

from .delta_tpd import (
    CrossingCorner,
    DataWindow,
    DeltaTpdEnvelope,
    MPDirection,
    RampShape,
    compute_delta_tpd_envelope_per_device,
    crossing_corners_per_device,
    crossing_time_frac,
    per_tdd_offset_frac,
    ramp_to_valid_frac,
)
from .spec_source import (
    HANDOVER_CROSS_SPEC_NOTE,
    HANDOVER_SW_PAIR_NOTE,
    PROPOSAL_NOTE,
    PROPOSAL_UI_RELATIVE_NOTE,
    SNAP_TOL_NS,
    THRESHOLD_NOTE,
    TRF_NOTE,
    LaunchMode,
    SpecSource,
    SystemSize,
    data_input_window,
    fclk_ceiling_MHz,
    is_soundwire,
    is_unratified,
    launch_grid_UI,
    snap_to_launch_grid,
    sw13_anchor_correction,
    sw13_equivalent_slews,
    sw13_keeper_thresholds,
    sw13_ramp_fracs,
    trf_range_ns,
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
# and likewise for PM and PP.  The "_ho" suffix records WHEN the t_ZD path is taken
# (only at a handover) rather than marking it as a lesser case.
#
# Because both face the same inequalities, the PHY2 revision states t_DD and t_ZD
# identically -- "9-23 ns or (0.5 UI 2x CLK, 0.25 UI 4x CLK)" for both.  See
# MP_hold_ho in `_compute_at`.
#
# The "_contention" trio is a DIFFERENT question from any of the four. The setup
# and hold legs ask whether the acquiring driver's edge is usable at the receiver;
# the contention legs ask whether the two drivers STRESS each other, i.e. whether
# either sees a low-impedance path to an opposing driver. That is bounded by the
# flight time between them and not by zero -- see the criterion note in `_compute_at`.
#
# THE DIVISION OF LABOUR BETWEEN THE TWO FAMILIES IS EXACT, and it is what lets the
# contention legs allow an overlap at all. Contention bounds driver CURRENT. The
# LEVEL on the line during that overlap is the `*_hold_ho` legs' business: the bit
# sampled at the UI-opening edge must stay valid for t_IH,max past it, and the
# acquiring device's turn-on out of high-Z is exactly what ends it. The `*_hold_ho`
# legs are live at N_HO = 0.
#
# THE PP DATA LEGS cover a peripheral RECEIVING while another peripheral drives. The
# spec does not guarantee peripheral-to-peripheral communication, so what they report
# is the PRICE of promising it -- see the PP block in `_compute_at`.
#
# BUT `PP_hold_ho` IS NOT PRICING A PROMISE, IT IS A REQUIREMENT. The inequality names
# the device turning ON and the device whose hold window ends; the owner of the level at
# risk does not appear in it. When
# the MANAGER released -- every M->P handover -- that level is Manager-to-peripheral data
# and a peripheral sink is obliged to read it. So the leg binds there, no Manager parameter
# appears in it to fix it, and a handover UI is still owed wherever a peripheral acquires.
# `_PP_DATA_LEGS` in `CalcResults` carries the split.
#
# The keeper pair is ONE constraint -- there is one keeper and it is in the
# Manager ({ASW3805}) -- evaluated as one leg per RELEASING device: every row must move each
# leg's margin in one direction for the worst-corner search (checklist rule 4), and a min
# over two legs does not. See the keeper block in `_compute_at`.

# Each device's V_SEOS as a fraction of nominal: (Manager, Peripheral, Peripheral B). Peripheral
# B is the second peripheral of a P->P leg; the Peripheral is A there.
Supplies = tuple[float, float, float]

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
# Each rule below is enforced by a test, and this list names it.
#
#   1. SYMBOLS.  `X_<pair>[,<device>],<edge>[,slow|fast],<role> · <device>_t_RF,<LANE>`, the
#      qualifiers in that order. The lane is ALWAYS qualified. Every symbol must subscript --
#      the underscore before a token is the marker.
#        * <pair> names a single crossing's direction, driver then receiver: a Peripheral's
#          detection of the Manager's clock is `X_MP` whatever envelope its coefficient comes
#          from (`clock_detection_symbol`); `X_PM` is a Peripheral's data crossing at the
#          Manager and a Sigma_cross,PM sum's addend; `X_PP` is A's data crossing at B on a
#          P->P data leg, on `Per_t_RF,DATA` only; `X_K` is the data's crossing of the
#          keeper's threshold.
#        * <device> is P->P's letter, `A` or `B` on `X_MP` and `B` on `X_PP`; a leg with a
#          single Peripheral carries none.
#        * <edge> is MANDATORY: `rise` or `fall` on a single crossing, and on both addends of a
#          Sigma_cross,PM sum the sum's case letters, clock edge then data edge, `rr` / `rf` /
#          `fr` / `ff`. No polarity is implied by an unqualified symbol.
#        * `slow` / `fast` names a clock detection whose edge's slew factor is the other
#          corner's (`_role_on_edge`), and `fast` before `late` a data-lane or keeper crossing
#          whose slow-edge role is evaluated on the fast edge (a lane in
#          `CalcInputs.fast_data_edge`, `_fast_role`): `X_MP,A,rise,slow,early`,
#          `X_PM,rr,fast,late`, `X_K,fall,fast,late`.
#        * <role> is late/early (setup is eroded by a late crossing, hold by an early one), and
#          it is MANDATORY and LAST.
#      A leg that reads ONE lane twice gathers its two crossings into one parenthesised
#      product, `(X_MP,A,rise,late − X_MP,B,fall,early) · Man_t_RF,CLK` (`_gather_lane_terms`),
#      because one pin has one slew; each symbol inside the bracket follows the same grammar.
#      One symbol has one value at a supply assignment, so a detection whose coefficient
#      equals an existing symbol's at every assignment takes that symbol and one whose
#      coefficient differs carries a qualifier that tells them apart. The ramp from a launch's
#      anchor to V_OH / V_OL is `<dev>_t_RF,DATA,rise` / `,fall`, naming the edge it is priced
#      on (`to_voh_symbol`), then `,fast` where it is priced on the fast edge; a SoundWire
#      `,anchor` term names its edges (`anchor_symbol`): a Peripheral's data edge,
#      `,anchor,rise` / `,anchor,fall`, and a Manager's clock then data edge with the case
#      letters, `,anchor,rr` / `,rf` / `,fr` / `,ff`, then `,fast` on the max corner where the
#      side's DATA lane is in `CalcInputs.fast_data_edge`. A slew ROW inside a product,
#      `Man_t_RF,CLK`, `Man_t_RF,DATA`, `Per_t_RF,DATA`, is one lane's slew for both edges and
#      names none.
#      -> test_every_displayed_symbol_follows_one_naming_scheme,
#         test_every_crossing_and_ramp_symbol_names_its_edge (tests/test_timing.py),
#         test_a_crossing_symbol_carries_one_coefficient_wherever_it_appears,
#         test_every_lone_clock_detection_is_an_x_mp_crossing,
#         test_one_ramp_symbol_has_one_value_on_every_leg_at_every_supply_assignment,
#         test_one_anchor_symbol_has_one_value_on_every_leg_at_every_supply_assignment
#         (tests/test_timing_cross_spec.py)
#
#   1b. POLARITIES.  A setup or hold leg is evaluated at both polarities of its sampling edge,
#      its launching edge on the polarity that follows from it, each crossing at its
#      polarity's corner, and reports the worse case (`_worse_case`,
#      `InequalityBreakdown.sampling_rising`); the timeline draws that case's polarities. A
#      contention leg is evaluated at both polarities of the edge closing the releaser's UI
#      the same way (`InequalityBreakdown.closing_rising`), and so is keeper_Per with a
#      conventional release, whose Peripheral detects the opening and the closing edge. A
#      Peripheral timing is counted from that Peripheral's own detection of its edge, so a
#      leg counting two of them across two edges carries the spacing of the two detections.
#      A data crossing's polarity follows the bit pattern, not the clock: Sigma_cross,PM,setup
#      takes its data on either polarity (`delta_tpd.SIGMA_PM_SETUP_CASES`). So does the ramp
#      from a launch's anchor to V_OH / V_OL on an EndDriveEarly release: the ramp's own
#      crossing time (`to_voh_frac`), on a contention leg on the edge that reaches V_OH / V_OL
#      the later (`valid_edge_rising`), and on its keeper leg on the edge whose keeper crossing
#      less that ramp is the larger (`_keeper_case`); a conventional release's keeper leg takes
#      the edge whose keeper crossing is the later. A SoundWire launch's anchor is placed
#      on its data edge's and its launching clock edge's polarities (`sw_anchor_ns`): a setup or
#      hold case gives both, a P->M envelope selects its case with each case's anchor added
#      (`data_offset_ns`), a contention leg's turn-on takes the edge whose anchor is the earlier
#      (`turn_on_rising`), a release the edge whose valid level or keeper crossing is the later
#      after the clock reference, and keeper_Man's opening edge the one that places its launch
#      the later (`keeper_open_rising`).
#      -> test_every_setup_and_hold_leg_evaluates_both_sampling_polarities,
#         test_the_two_sampling_polarities_tie_in_a_symmetric_window_at_equal_supplies,
#         test_every_contention_leg_evaluates_both_closing_edge_polarities,
#         test_the_two_closing_edge_polarities_tie_in_a_symmetric_window_at_equal_supplies,
#         test_pm_setup_takes_data_of_either_polarity,
#         test_an_exponential_ramp_to_v_oh_is_its_true_crossing_on_keeper_and_ede_legs,
#         test_a_keepers_ede_leg_takes_its_crossing_and_ramp_on_one_data_edge,
#         test_a_keepers_crossing_is_its_closed_form_on_either_shape and
#         test_every_soundwire_anchor_is_its_launchs_placement_at_the_edges_it_is_drawn_on
#         (tests/test_timing_cross_spec.py),
#         test_each_leg_is_drawn_at_the_polarities_of_the_case_it_reports,
#         test_contention_is_drawn_at_the_closing_polarity_it_reports and
#         test_the_ramp_to_v_oh_is_drawn_as_long_as_its_term (tests/test_timing_timeline.py)
#
#   2. LANES.  A leg may only read the lanes it has business reading: no Man DATA in a P->P
#      leg, because no Manager drives that link. Wrong-lane bugs are CONSISTENT -- symbol,
#      factors and behaviour all agree on the wrong row -- so only a declared table catches
#      them. Adding a leg means adding a row and defending it; that is the review step.
#      -> _LEGAL_LANES + test_no_leg_reads_a_lane_it_has_no_business_reading
#
#   2b. TWO PARTS SHARING A ROW.  Where a leg has two INDEPENDENT devices on it, a row read
#      TWICE with opposite signs must be cornered per device -- one shared value cannot be
#      both the charge and the credit. The P->P clock lane is read twice but realised by one
#      Manager pin, so it takes one value (see `_per_clk_cross_parts`). A row read ONCE needs
#      nothing: the ordinary corner search already drives it to the end that hurts, which is
#      why the device parameters carry the device in their NOTE and not in the symbol -- a
#      `Per_A_t_DD` would point at a row the input grid does not have.
#      -> test_no_pp_leg_reads_one_per_row_for_both_devices (tests/test_timing.py)
#
#   2c. SUPPLIES.  One V_SEOS per device: a crossing takes its transmitter's supply as its
#      rail and its receiver's for its threshold, and a leg is reported at the assignment of
#      the three devices' supplies that leaves it the least margin (`compute`). A new crossing
#      corner gets a row naming its two devices.
#      -> _CROSSING_DEVICES + test_one_device_has_one_supply_and_each_leg_reports_its_worst_
#         assignment (tests/test_timing.py), test_one_device_has_one_supply_in_a_leg
#         (tests/test_timing_timeline.py)
#
#   3. ARITHMETIC.  The terms shown must sum to the margin reported. Setup legs compute the
#      two from separate expressions, so they can silently disagree.
#      -> test_every_leg_displays_the_margin_it_reports (tests/test_timing_cross_spec.py)
#
#   3b. DIAGRAM.  Every term of every leg has a span in its timeline (`timeline.leg_timeline`),
#      as long as its value, placed from the inputs; a new leg or term needs its events placed
#      there.
#      -> test_every_term_has_one_span_as_long_as_its_value (tests/test_timing_timeline.py),
#         test_a_click_on_every_term_of_every_leg_draws_a_panel (tests/test_timing_diagram.py)
#
#   4. CORNERS.  `find_worst_corner_rows` returns the worst corner of the whole min/typ/max
#      grid, each slew row over its whole range, under three rules. Every row it moves on its
#      own must move each margin in ONE DIRECTION, whatever the other rows' values. The slews,
#      τ, α and δ are searched jointly (`_joint_rows`), so every row but those must change
#      each margin by an amount those rows do not change. Every term that reads a slew must be
#      affine in its edges' t_RF at given τ, α and δ, or the worse of affine alternatives, so
#      that a margin is least at an end of a slew row's range or at a kink of `edge_kinks_ns`
#      inside it, where the row is also evaluated (`_slew_picks`). A term that multiplies two
#      rows in a direction-changing way breaks the first (see `CalcInputs.pp_placement`, which
#      is a selector for exactly this reason), and so does a min/max over legs whose branches
#      move a row in different directions, because the margin is then flat at the typ base
#      while another branch binds (see the keeper split). The min over the eight supply
#      assignments `compute` takes is exempt: every assignment moves a row the same way. So is
#      a leg's min over its two sampling polarities (`_worse_case`), whose cases read the same
#      rows with the same signs, and `compute`'s min over the slow and fast data-edge sets
#      (`CalcInputs.fast_data_edge`), which differ only in the DATA lanes' slew factors, set
#      by the joint rows. A term that couples another row to the slews, τ, α or δ breaks the
#      second, and that row joins `_JOINT_FIELDS`. A slew term with another kink breaks the
#      third, and the kink joins `edge_kinks_ns`. τ, α and δ are then refined inside their
#      ranges (`_refine_fraction_row`), so their picks, like a slew row's, can be a point
#      inside the range (`RowPoint`).
#      -> the brute-force checks in tests/test_timing_cross_spec.py, including
#         test_the_corner_search_is_exact_over_each_slew_rows_whole_range,
#         test_each_edge_is_affine_in_its_row_between_two_kinks,
#         test_the_corner_search_is_exact_over_the_rows_a_soundwire_anchor_couples,
#         test_the_corner_search_is_exact_with_the_input_windows_set_low,
#         test_the_corner_search_is_exact_where_delta_binds_at_typ,
#         test_the_corner_search_finds_an_interior_delta_minimum_past_a_supply_case_boundary,
#         test_the_corner_search_is_exact_at_asymmetric_soundwire_levels,
#         test_the_corner_search_is_exact_on_a_swi3s_bus_whatever_the_slew_and_noise_ranges,
#         test_the_corner_search_is_exact_on_both_keeper_legs
#         and test_the_other_rows_move_a_margin_independently_of_the_coupled_rows
#
#   5. ZEROS.  Three kinds, and they are not interchangeable: structurally zero (`0·t_PD`,
#      a Manager end's clock crossing on a contention leg -- dropped), nothing-allocated
#      (`0·UI`, dropped), and a PARAMETER that happens to read zero (`Man_t_IH`, `Per_t_DZ`
#      at its min -- SHOWN, because the value is the spec's).
#      -> test_no_leg_shows_a_structurally_zero_term (tests/test_timing.py)
#
#   6. THE REFERENCE.  If the leg exists in the vendored reference fixture, it is compared
#      against it and any delta must be declared.
#      -> tests/test_timing_vs_reference.py, and docs/anchors.md for what is deliberate
#
# When one of these rules CHANGES, the tests above are the list of places to review: each
# names the legs it covers, so "does this apply elsewhere?" is answered by running them rather
# than by remembering.

Pick = Literal["min", "typ", "max"]


class RowPoint(str):
    """A joint row's pick strictly inside its range (`find_worst_corner_rows`). `at` is the
    position along the range: 0 at min, 0.5 at typ and 1 at max, linear from min to typ and
    from typ to max (`ParamRange.at`), and a row linked to the row takes the same position in
    its own range. Its text names the row's value there: "at 7.90 ns" for a slew row, whose
    `value` is in ns, and `text` for another ("at 9.00 %"). A copy or an unpickled point keeps
    its text and `at`."""

    at: float

    def __new__(cls, at: float, value: float, text: str | None = None) -> RowPoint:
        point = super().__new__(cls, text if text is not None else f"at {value:.2f} ns")
        point.at = at
        return point

    def __getnewargs__(self) -> tuple[float, float, str]:  # type: ignore[override]
        return (self.at, 0.0, str(self))


# A row's pick: a corner of its range, or a slew row's point inside it.
RowPick = Pick | RowPoint

# Where the two peripherals sit, for the P->P data legs. See `CalcInputs.pp_placement`.
#
# FOUR PLACEMENTS: two ends times two devices.
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

    def at(self, pick: RowPick) -> float:
        if isinstance(pick, RowPoint):
            return self.at_position(pick.at)
        return {"min": self.min, "typ": self.typ, "max": self.max}[pick]

    def at_position(self, at: float) -> float:
        """The value at position `at` (`RowPoint.at`): 0 is min, 0.5 typ and 1 max."""
        if at <= 0.5:
            return self.min + 2.0 * at * (self.typ - self.min)
        return self.typ + (2.0 * at - 1.0) * (self.max - self.typ)

    def positions(self, value: float) -> list[float]:
        """The positions (`RowPoint.at`) at which the range takes `value` strictly between min
        and typ or strictly between typ and max."""
        out = []
        for a, b, start in ((self.min, self.typ, 0.0), (self.typ, self.max, 0.5)):
            if min(a, b) < value < max(a, b):
                out.append(start + 0.5 * (value - a) / (b - a))
        return out


# ---------------------------------------------------------------------------
# Calculator inputs (one column: Spec or Proposed, evaluated at one corner)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CalcInputs:
    """All scalar inputs needed to evaluate the SWI3S inequalities (`INEQUALITIES`)."""

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
    # `default_handover_UIs`; overridable to explore a handover programmed longer.
    handover_UIs: float = 1.0

    # EndDriveEarly, per side.  A side that uses it holds its level for its EDE Delay
    # after its own ramp reaches 80 % (V_OH rising, V_OL falling) and then goes high-Z.
    # From the clock edge that opens the driven UI, the release is at
    #
    #     t_DD + (ramp to V_OH / V_OL) + EDE Delay
    #
    # the ramp being one t_RF on a linear ramp and its crossing time on an exponential one
    # (`_to_voh_ns`). It replaces the side's t_DZ, which is then not read. A contention leg
    # shows it as those parts and the UI back to the closing edge (`_ede_release_terms`).
    Man_ede: bool = False
    Per_ede: bool = False

    # The EDE Delay, per side.  The contention legs read the row's max end (the latest
    # release) and the keeper legs its min end (the earliest).
    Man_t_EDE_ns: float = 5.0
    Per_t_EDE_ns: float = 5.0

    # The Manager's keeper response, max: Man_tKeeper_Response ("PHY1 & PHY2 Bus Keeper
    # Behavior" table, from the keeper's input crossing V_IH / V_IL to its output current
    # changing sign), or a SoundWire Manager's t_Keeper_Delay. The keeper legs require the
    # releasing driver to go on driving at least this long after its data crosses the
    # keeper's threshold at the Manager (`keeper_corners`): the Manager's V_IH,max / V_IL,min
    # at its supply, or a SoundWire Manager's V_TH_Keeper,max / V_TL_Keeper,min.
    #
    # The keeper is in the Manager, and the keeper legs carry no t_PD even for a releasing
    # PERIPHERAL: its level and its high-Z both reach the keeper one flight later. The line is
    # taken as settled as soon as the level arrives; ringing and reflection are not modelled.
    t_keeper_ns: float = 3.0

    # A MANAGER THAT FORCES ITS KEEPER loads the keeper with the level it is driving, so the
    # keeper does not follow the line and the Manager's release waits for no keeper response:
    # keeper_Man places no requirement on the bus (`InequalityBreakdown.not_applicable`).
    # keeper_Per is unchanged: the keeper follows a Peripheral's level, which the Manager does
    # not drive.
    Man_keeper_forced: bool = False

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
    # The bracket is a COST for setup and a CREDIT for hold, so one placement cannot be
    # worst for both: "Worst Per Leg" (the default) gives each leg its own worst — setup 2,
    # hold 0 — and a pinned layout gives both legs that layout's bracket.
    #
    # A SELECTOR AND NOT A CORNER ROW. As a row its value would multiply the bus-length row,
    # so the direction the bus row moves a margin would depend on this row's pick, which
    # breaks checklist rule 4: `find_worst_corner_rows` moves the bus row on its own. As a
    # selector the count is a constant per (placement, leg), and the search sees only the
    # bus row.
    pp_placement: PPPlacement = "unknown"

    # Bus & topology
    bus_length_cm: float = 30.0
    vPCB_cm_per_ns: float = 15.0
    vPCB_err_frac: float = 0.10              # peak-to-peak trace mismatch as fraction of t_PD

    # Operating point
    F_CLK_target_MHz: float = 13.2
    duty_min: float = 0.45

    # Spec source per side.  Defaults reproduce the single-spec SWI3S behaviour
    # exactly.  The inequalities' structure is the same for every source; a SoundWire
    # side changes their inputs: its launch anchors (`sw_anchor_ns`), its Data input's
    # threshold window (`data_input_window`), its t_RF range (`edge_factor`, via
    # `trf_range_ns`), the keeper thresholds (`keeper_corners`), the Manager's launch
    # grid (`man_launch_mode`) and the fast-edge lane set (`fast_edge_lanes`). See
    # spec_source.
    Man_spec: SpecSource = "SWI3S_PHY2"
    Per_spec: SpecSource = "SWI3S_PHY2"
    # SoundWire's system-size envelope (C_Bus / L_Bus, Table 5).  Ignored for
    # a SWI3S side, which does not split by bus envelope.
    system_size: SystemSize = "small"

    # How the Manager launches its data edge.  ANALOG uses the Man_t_DD range
    # as given; a clock-launched mode collapses min and max to one value, the row's max
    # placed on the mode's grid at or above the row's floor (`_man_clocked_grid_ns`).
    # A SWI3S Manager's option: a SoundWire Manager launches as tabulated whatever this
    # holds (`man_launch_mode`).
    launch_mode: LaunchMode = LaunchMode.ANALOG
    # The lowest point a clocked Manager t_DD, t_ZD and t_DZ is placed at, in ns: the row's
    # tabulated minimum, or None. At another rate a clocked timing keeps its UI fraction
    # unless that puts it below its floor, where it takes the lowest grid point at or above
    # the floor (`snap_to_launch_grid`), which is how the Timing Calculator re-places its rows
    # when F_CLK changes. The rate solve follows these placements (`_rate_windows`); with
    # None it holds the fraction at every rate.
    Man_t_DD_floor_ns: float | None = None
    Man_t_ZD_floor_ns: float | None = None
    Man_t_DZ_floor_ns: float | None = None

    # DATA lanes ("Man DATA", "Per DATA") whose slow-edge role is evaluated on the fast edge,
    # (1 − τ). Set by `compute`, which evaluates every leg with none and with all of the lanes
    # `fast_edge_lanes` names and reports the worse; the breakdown records the set. No leg
    # reads the slow-edge role of two DATA lanes.
    fast_data_edge: frozenset[str] = frozenset()

    # ---- derived ----

    def fast_edge_lanes(self) -> tuple[str, ...]:
        """The DATA lanes whose launched or released edge can leave a leg the less margin on
        the fast edge than on the slow one: a SoundWire side's, whose t_OV ends at 80 % of the
        ramp, and an EndDriveEarly side's, whose keeper leg credits the ramp to V_OH / V_OL
        against the data's crossing of the keeper's threshold. A lane whose two edge factors
        are equal is not named."""
        out = []
        for lane, spec, ede in (("Man DATA", self.Man_spec, self.Man_ede),
                                ("Per DATA", self.Per_spec, self.Per_ede)):
            if not (is_soundwire(spec) or ede):
                continue
            plain = replace(self, fast_data_edge=frozenset())
            if edge_factor(plain, lane, "late") != edge_factor(plain, lane, "early"):
                out.append(lane)
        return tuple(out)

    def man_launch_mode(self) -> LaunchMode:
        """The launch mode in force: `launch_mode` for a SWI3S Manager, ANALOG for a SoundWire
        one, which has no clocked launch."""
        return LaunchMode.ANALOG if is_soundwire(self.Man_spec) else self.launch_mode

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
        """Period · duty floor (the shorter clock phase) at the target F_CLK: the time
        between two Manager clock references (20 % points)."""
        return 1000.0 * self.duty_min / self.F_CLK_target_MHz

    def envelope(self, supplies: Supplies | None = None) -> DeltaTpdEnvelope:
        """The crossing envelope; with `supplies`, every crossing at its devices' supplies
        (`compute_delta_tpd_envelope_per_device`), as `compute` evaluates each leg. The
        envelope reads no slew and no timing row, so one is computed per set of the fields it
        reads (`_envelope`)."""
        return _envelope(
            self.Man_shape, self.Per_shape, self.V_OH_min_frac, self.V_OL_max_frac,
            self.V_IH_min_frac, self.V_IH_max_frac, self.V_IL_min_frac, self.V_IL_max_frac,
            self.V_SEOS_tol_frac, self.V_noise_pp_frac * self.V_SEOS_nom_V,
            self.V_SEOS_nom_V, self.tRF_tolerance_frac,
            # Each receiver's DATA input takes its own spec's window; clock inputs and
            # SWI3S data inputs read the V_IH / V_IL rows.
            data_input_window(self.Man_spec), data_input_window(self.Per_spec), supplies)

    def crossing_corners(self, supplies: Supplies | None = None) -> dict[str, CrossingCorner]:
        """The corner behind each `envelope(supplies)` field: the threshold, the ground shift
        and the supplies it is evaluated at (`crossing_corners_per_device`)."""
        return crossing_corners_per_device(
            V_IH_min_frac=self.V_IH_min_frac,
            V_IH_max_frac=self.V_IH_max_frac,
            V_IL_min_frac=self.V_IL_min_frac,
            V_IL_max_frac=self.V_IL_max_frac,
            VDD_tol=self.V_SEOS_tol_frac,
            noise_budget_V=self.V_noise_pp_frac * self.V_SEOS_nom_V,
            V_nom_V=self.V_SEOS_nom_V,
            Man_data_window=data_input_window(self.Man_spec),
            Per_data_window=data_input_window(self.Per_spec),
            supplies=supplies,
        )

    def supply_corners(self) -> list[Supplies]:
        """Every assignment of a V_SEOS corner to each device, (Manager, Peripheral,
        Peripheral B): one supply per device, at either end of its tolerance."""
        d = self.V_SEOS_tol_frac
        ends = (1.0 - d, 1.0 + d) if d else (1.0,)
        return [(m, a, b) for m, a, b in itertools.product(ends, repeat=3)]


@functools.lru_cache(maxsize=4096)
def _envelope(Man_shape: RampShape, Per_shape: RampShape, V_OH: float, V_OL: float,
              V_IH_min: float, V_IH_max: float, V_IL_min: float, V_IL_max: float,
              VDD_tol: float, noise_budget_V: float, V_nom_V: float, tau: float,
              Man_data_window: DataWindow | None, Per_data_window: DataWindow | None,
              supplies: Supplies | None) -> DeltaTpdEnvelope:
    """`CalcInputs.envelope`'s envelope, a pure function of these arguments and so computed
    once per set of them. Every crossing counts from its edge's 20 % point, where every edge
    is placed."""
    return compute_delta_tpd_envelope_per_device(
        Man_shape=Man_shape, Per_shape=Per_shape, V_OH_frac=V_OH, V_OL_frac=V_OL,
        V_IH_min_frac=V_IH_min, V_IH_max_frac=V_IH_max,
        V_IL_min_frac=V_IL_min, V_IL_max_frac=V_IL_max,
        VDD_tol=VDD_tol, noise_budget_V=noise_budget_V, V_nom_V=V_nom_V,
        tRF_tolerance=tau, Man_data_window=Man_data_window, Per_data_window=Per_data_window,
        from_anchor=True, supplies=supplies)


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
    its tabulated form — e.g. a SoundWire equivalent moved to the operating slews.

    NO CORNER IN A SYMBOL WHERE A ROW OWNS THE CORNER.  ``symbol`` names the
    PARAMETER, not which end of it is in force: `Man_t_DD`, not `Man_t_DD,max`.
    Choosing the end is `find_worst_corner_rows`' job, per inequality — the same
    field is pulled to opposite ends by the keeper and the contention legs.
    `apply_row_picks` sets EVERY field a row binds to the same picked value, and a
    `,min` on a parameter the spec gives only a maximum for (t_DZ) would invent a
    guarantee.  Enforced by
    `test_no_term_symbol_names_a_corner_the_picker_owns`.

    Suffixes that are NOT corners stay: `,anchor` (a SoundWire anchor correction), and a
    corner on a field NO row binds, which the picker never touches, so naming its end is a
    fact rather than a claim. An `,anchor` term follows its parameter's corner as the
    parameter does (`_launch_terms`), so like `Man_t_DD` it shows one value on the setup and
    keeper legs and another on the hold legs and a turn-on. It names the edges it is placed on
    (`anchor_symbol`): `,anchor,rise` / `,anchor,fall` on a Peripheral, the clock-then-data
    letters `,anchor,rf` and so on on a Manager.
    """
    symbol: str
    value_ns: float
    op: Literal["+", "-"] = "+"
    note: str = ""
    # (coefficient, operand) for a term that IS a product, so the numeric substitution can
    # show `coefficient·t_RF` (e.g. `1.09·6.00`) where the symbol says
    # `X_MP,rise,late · Man_t_RF,DATA`.
    #
    # SET IT WHERE AN OPERAND'S VALUE WOULD OTHERWISE APPEAR NOWHERE ON THE ROW. Crossing
    # terms and the ramp to V_OH / V_OL (an edge factor times t_RF) set it, so the t_RF in
    # force is shown.
    #
    # The substitution shows each coefficient to two places, so its product can differ from
    # the value in force in the last place. The term's note carries the exact arithmetic.
    # THE ROW VALUE IS ALWAYS LAST, the coefficients before it. Two entries is the ordinary
    # `coefficient x t_RF`, its coefficient positive and the operator carrying the sign
    # (`_positive_coefficient`); three is a GATHERED pair (`(a - b) x t_RF`, see
    # `_gather_lane_terms`), whose coefficients are signed relative to the term's own hoisted
    # operator. Everything that asks "which row does this term read?" uses `factors[-1]`.
    factors: tuple[float, ...] | None = None


# Set while `find_worst_corner_rows_all` evaluates its candidate corners, of which it reads only
# the margins: a breakdown keeps its terms as built (`InequalityBreakdown.__post_init__`) and
# reports no passing rates (`_rate_windows`). No result built under it leaves the search.
_MARGINS_ONLY: contextvars.ContextVar[bool] = contextvars.ContextVar("margins_only",
                                                                     default=False)


@dataclass(frozen=True)
class InequalityBreakdown:
    name: Inequality
    terms: list[Term]
    required_ns: float          # sum of consumed terms (UI side); 0 for hold inequalities
    margin_ns: float            # sum of all signed terms
    F_max_MHz: float            # the lowest F_CLK at which the margin fails as the rate
                                # rises (`_fmax_from_windows`); math.inf if it never fails,
                                # 0.0 if it fails at the lowest rates
    is_setup: bool              # True for setup, False for hold
    # The devices' supplies the leg is evaluated at: the assignment that leaves it the least
    # margin (`compute`).
    supplies: Supplies = (1.0, 1.0, 1.0)
    # The DATA lanes whose slow-edge role the leg is evaluated on the fast edge
    # (`CalcInputs.fast_data_edge`).
    fast_data_edge: frozenset[str] = frozenset()
    # The polarity of the sampling edge in the case a setup or hold leg reports (`_worse_case`):
    # the Peripheral's, B's or the Manager's. None on a leg with no sampling edge.
    sampling_rising: bool | None = None
    # The higher rates at which the leg passes again past its F_max (`_fmax_from_windows`),
    # "" where there are none.
    F_max_note: str = ""
    # The polarity of the edge closing the releaser's UI in the case a contention leg or a
    # Peripheral's conventional keeper leg reports (`_worse_case`). None on every other leg.
    closing_rising: bool | None = None
    # Why the leg places no requirement on this bus, "" where it does. A leg that does not
    # apply has no terms, and its margin and F_max are infinite.
    not_applicable: str = ""

    def __post_init__(self) -> None:
        # SAME-ROW CROSSINGS ARE GATHERED HERE, for every leg, because a leg cannot opt out
        # of physics: `Man_t_RF,CLK` is one pin and two products of it are one product. Done
        # at construction, so a leg added later gets it too. Margin-preserving, so the
        # separately-computed `required_ns` / `margin_ns` stay valid — asserted by
        # test_every_leg_displays_the_margin_it_reports. Not done for a corner the search
        # reads only the margin of (`_MARGINS_ONLY`).
        if _MARGINS_ONLY.get():
            return
        object.__setattr__(self, "terms",
                           _gather_lane_terms([_positive_coefficient(t) for t in self.terms]))


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
    F_CLK_ceiling_MHz: float = math.inf
    F_CLK_legal: bool = True
    caveats: tuple[str, ...] = ()

    # THE HEADLINE RATE IS THE GUARANTEED BUS'S RATE, so the legs that only exist to price an
    # unguaranteed promise are excluded from it while keeping their own rows and margins.
    #
    # THREE OF THE FOUR P->P LEGS ARE SUCH LEGS. `PP_setup`, `PP_setup_ho` and `PP_hold` all
    # require peripheral B to READ a level peripheral A drove, and the spec does not promise
    # that one peripheral can read another's data. Left in the bounding set, a failing one
    # would pin `F_max_binding` to 0.0 for every configuration and name itself the binding
    # constraint.
    #
    # `PP_hold_ho` IS NOT ONE OF THEM. The only devices in the inequality are the one turning
    # ON out of high-Z and the one whose hold window that ends; THE DEVICE THAT DROVE THE LEVEL
    # AT RISK DOES NOT APPEAR. So the leg asserts that a turn-on destroys a level still
    # obliged to be held, and WHOSE level that is depends on who released:
    #
    #   a peripheral released   the level is peripheral data -> not guaranteed, out of scope
    #   the MANAGER released    the level is Manager->peripheral data, which every peripheral
    #                           sink MUST read -> IN SCOPE, and this leg is what bounds it
    #
    # The second case is every M->P handover at N_HO = 0, so the leg bounds guaranteed
    # traffic and stays in the bounding set. An out-of-scope declaration is a claim about an
    # inequality's TERMS and has to be checked against them; for `PP_hold` the claim holds.
    #
    # If P->P data ever becomes guaranteed, delete `_PP_DATA_LEGS` and this note with it.
    _PP_DATA_LEGS: ClassVar[frozenset[str]] = frozenset(
        {"PP_setup", "PP_setup_ho", "PP_hold"})

    def _bounding(self) -> dict:
        """The legs `F_max_binding_MHz` and `binding_inequality` read: every leg but
        `_PP_DATA_LEGS` and a leg that does not apply (`InequalityBreakdown.not_applicable`)."""
        return {k: b for k, b in self.breakdowns.items()
                if k not in self._PP_DATA_LEGS and not b.not_applicable}

    @staticmethod
    def _rate_bound_MHz(b: InequalityBreakdown) -> float:
        """A bounding leg's rate bound, its `F_max_MHz`: the one value `F_max_binding_MHz`
        and `binding_inequality` read per leg, and the seam the tests check them through."""
        return b.F_max_MHz

    @property
    def F_max_binding_MHz(self) -> float:
        """min over the bounding inequalities of `_rate_bound_MHz`. A leg with no UI-scaled
        part contributes inf if passing and 0 if failing."""
        vals = [self._rate_bound_MHz(b) for b in self._bounding().values()]
        return min(vals) if vals else 0.0

    @property
    def binding_inequality(self) -> Inequality:
        """The bounding inequality with the smallest `_rate_bound_MHz`; the first in
        `INEQUALITIES` order on a tie, so the first failing rate-independent leg names
        itself."""
        items = [(ineq, self._rate_bound_MHz(b)) for ineq, b in self._bounding().items()]
        return min(items, key=lambda kv: kv[1])[0]


# ---------------------------------------------------------------------------
# compute()
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class _ClosingCase:
    """A contention case at one polarity of the closing edge: each detecting peripheral's edge
    polarity and slew factor (`pp_clock_edges`), and the detections' `CrossParts` by role --
    the releaser's late one and the acquirer's early one, and on P->P the releaser's and
    Peripheral B's on those edges."""
    rel_rising: bool
    acq_rising: bool
    rel_edge: str
    acq_edge: str
    late: CrossParts
    early: CrossParts
    pp_rel: CrossParts
    pp_acq: CrossParts


def _releaser_ede(inp: CalcInputs, releasing: str) -> bool:
    return inp.Man_ede if releasing == "Man" else inp.Per_ede


def to_voh_frac(inp: CalcInputs, lane: str, rising: bool) -> float:
    """The ramp from a launch's anchor to V_OH (`rising`) / V_OL (falling) on a DATA lane
    ("Man DATA" or "Per DATA"), in units of the edge's t_RF, on the lane's ramp shape at the
    V_OH / V_OL in force (`ramp_to_valid_frac`): 1 on a linear ramp, the exponential ramp's
    own crossing time on an exponential one."""
    shape = inp.Man_shape if lane == "Man DATA" else inp.Per_shape
    return ramp_to_valid_frac(shape, V_OH_frac=inp.V_OH_min_frac, V_OL_frac=inp.V_OL_max_frac,
                              rising=rising)


def valid_edge_rising(inp: CalcInputs, lane: str, *, tie: bool,
                      clk_rising: bool = True) -> bool:
    """The polarity of a releasing driver's last data edge where nothing else sets it: the one
    that reaches V_OH / V_OL the later after the side's clock reference, which releases
    latest; `tie` where the two are equal. That is the launch's anchor
    (`sw_anchor_ns` at the max corner, from a Manager clock edge of polarity `clk_rising`) plus
    the ramp from it (`to_voh_frac`), so on a SWI3S side, or where the two anchors agree, the
    edge whose ramp is the longer. The data's polarity follows the bit pattern, so a leg takes
    the edge that leaves it the less margin."""
    side = "Man" if lane == "Man DATA" else "Per"
    rise, fall = to_voh_frac(inp, lane, True), to_voh_frac(inp, lane, False)
    corr_rise, corr_fall = (sw_anchor_ns(inp, side, "max", rising=r, clk_rising=clk_rising)
                            for r in (True, False))
    if corr_rise != corr_fall:
        rise, fall = (_to_voh_ns(inp, lane, True) + corr_rise,
                      _to_voh_ns(inp, lane, False) + corr_fall)
    if math.isclose(rise, fall, rel_tol=0.0, abs_tol=1e-12):
        return tie
    return rise > fall


def keeper_open_rising(inp: CalcInputs) -> bool:
    """The polarity of the clock edge opening keeper_Man's last driven UI: the one that places
    the Manager's launch the later (`sw_anchor_ns` at the max corner), which leaves the keeper
    the less, rising on a tie. Only a SoundWire Manager's anchor reads the clock's polarity."""
    rise, fall = (sw_anchor_ns(inp, "Man", "max", clk_rising=c) for c in (True, False))
    return math.isclose(rise, fall, rel_tol=0.0, abs_tol=1e-12) or rise > fall


def _to_voh_ns(inp: CalcInputs, lane: str, rising: bool) -> float:
    """From a launch's anchor to V_OH (`rising`) / V_OL (falling) on a DATA lane: the ramp's
    crossing time `to_voh_frac` on a late edge (`edge_factor`). The addend of every release
    that waits for a valid level."""
    return to_voh_frac(inp, lane, rising) * edge_factor(inp, lane, "late") * lane_ns(inp, lane)


def to_voh_symbol(inp: CalcInputs, lane: str, dev: str, rising: bool) -> str:
    """The symbol of the ramp from a launch's anchor to V_OH / V_OL on `lane`, driven by `dev`:
    `dev_t_RF,DATA,rise` or `dev_t_RF,DATA,fall`, naming the edge it is priced on whatever the
    two polarities' crossing times (`to_voh_frac`), then `,fast` where the ramp is priced on
    the fast edge (`CalcInputs.fast_data_edge`), so that one symbol has one value."""
    sym = f"{dev}_t_RF,DATA,{'rise' if rising else 'fall'}"
    return sym + (",fast" if lane in inp.fast_data_edge else "")


def _to_voh_term(inp: CalcInputs, lane: str, dev: str, rising: bool, *, credit: bool) -> "Term":
    """The ramp from a launch's anchor to V_OH / V_OL as a term (`to_voh_symbol`): a charge, or
    with `credit` a credit; its factors are (crossing time × edge factor, t_RF)."""
    v = _to_voh_ns(inp, lane, rising)
    return Term(to_voh_symbol(inp, lane, dev, rising), +v if credit else -v,
                op="+" if credit else "-",
                note=_to_voh_note(inp, lane, rising),
                factors=(to_voh_frac(inp, lane, rising) * edge_factor(inp, lane, "late"),
                         lane_ns(inp, lane)))


def _to_voh_note(inp: CalcInputs, lane: str, rising: bool) -> str:
    """Provenance for a to-V_OH term."""
    c = to_voh_frac(inp, lane, rising)
    level, start = ("V_OH", "V_OL") if rising else ("V_OL", "V_OH")
    edge = "rising" if rising else "falling"
    if (inp.Man_shape if lane == "Man DATA" else inp.Per_shape) == "linear":
        head = (f"t_RF {lane_ns(inp, lane):.2f} ns is a "
                f"{inp.V_OL_max_frac * 100:.0f}-{inp.V_OH_min_frac * 100:.0f} % measurement "
                '("PHY1 & PHY2 Output Ramp Time" table), so the linear ramp reaches '
                f"{level} one t_RF after the anchor t_DD is measured to")
    else:
        head = (f"t_RF {lane_ns(inp, lane):.2f} ns is the exponential ramp's 20-80 % time, so "
                f"on a {edge} edge it reaches {level} {c:.3f}·t_RF after its {start} crossing, "
                "the anchor t_DD is measured to")
    differ = not math.isclose(to_voh_frac(inp, lane, True), to_voh_frac(inp, lane, False),
                              rel_tol=0.0, abs_tol=1e-12)
    qual = (f"; the two polarities' ramps differ at these levels, and the symbol names the "
            f"{edge} edge it is priced on" if differ else "")
    slew = "fast" if lane in inp.fast_data_edge else "late"
    return (f"{head}; on a {slew} edge it is {c * edge_factor(inp, lane, 'late'):.3f}·t_RF = "
            f"{_to_voh_ns(inp, lane, rising):.2f} ns{qual}")


# A keeper leg's t_DZ at or below this, in ns, is a release at the instant the UI closes.
_ZERO_RELEASE_NS = 5e-4


def _release_note(t_dz_ns: float) -> str:
    """Provenance for a keeper leg's t_DZ term.

    The keeper wants the EARLIEST release, so the corner search pulls the one
    `*_t_DZ_max_ns` field to its row's low end here and to the high end in the
    contention legs. The note says so on screen: the field's NAME carries `max` while
    this leg reads the other end of its row, and at a low end of 0 it reads "this
    device may release the instant its UI closes".
    """
    if t_dz_ns <= _ZERO_RELEASE_NS:
        return ("earliest release: t_DZ = 0, i.e. the device may go high-Z the instant "
                "its UI closes, leaving the keeper nothing past the edge")
    return (f"earliest release: driving continues {t_dz_ns:.2f} ns into the next UI "
            f"before high-Z")


def _ede_keeper_note(inp: CalcInputs, side: str, t_ede_ns: float) -> str:
    """Provenance for an EndDriveEarly keeper leg's EDE Delay term."""
    lo, hi = keeper_threshold_names(inp)
    return (f"earliest release: {side} holds its level {t_ede_ns:.2f} ns past the ramp's "
            f"80 % point (V_OH / V_OL) before high-Z; the keeper's response counts from the "
            f"data crossing {lo} / {hi} at the Manager, the X_K term, and the ramp to "
            "V_OH / V_OL is credited back")


def _handover_ui_terms(inp: CalcInputs, UI: float) -> list["Term"]:
    """The allocated handover UIs as a term — or NO TERM when none are allocated.

    Drop rule: a term contributing zero is dropped whether it is structurally zero
    (`0·t_PD`, zero for every input) or zero because nothing is allocated (`0·UI`). What
    stays is a PARAMETER that happens to read zero on this spec — `Man_t_IH` — because
    there the zero is the spec's value.

    Numerically a no-op: the term it drops is worth 0.0 ns, so no margin moves.
    """
    if inp.handover_UIs <= 0.0:
        return []
    return [Term(f"{inp.handover_UIs:g}·UI", +inp.handover_UIs * UI, op="+")]


def _pp_device_note(who: str, what: str) -> str:
    """Whose parameter a P->P device term is — in the NOTE, never in the symbol.

    A P->P leg reads two peripherals' device parameters (A launches, B samples); the symbols
    do not letter them.

    A and B are independent parts, each realising its own Per_t_DD and Per_t_IS inside the
    spec's limits. The independence costs something only when ONE row that both devices
    realise is read TWICE on one leg with opposite signs. The clock lane is read twice
    (`X_MP,A,rise,late` and `X_MP,B,fall,early` are both `Man_t_RF,CLK`) but one Manager pin
    realises it. Every DEVICE parameter appears at most once per leg -- A's launch and B's
    sampling window are different rows -- so the corner search already gives each the end
    that hurts.

    The letter is withheld because it would point at a row that does not exist: there is
    one `Per t_DD` in the input grid. The role goes in the note.

    If a P->P leg is ever added that reads one Per row for BOTH devices, that leg needs a
    per-device band; `test_no_pp_leg_reads_one_per_row_for_both_devices` fails when it
    happens.
    """
    return f"{who}: {what}"


def _keeper_note(inp: CalcInputs) -> str:
    """Provenance for the Man_tKeeper_Response term: the source in force for the Manager's
    keeper, what it measures, and so what the release must wait for."""
    rise, fall = keeper_threshold_names(inp)
    if is_soundwire(inp.Man_spec):
        return ("SoundWire 1.3 t_Keeper_Delay (Tables 15 and 17): from the data crossing "
                "V_TH_Keeper,max / V_TL_Keeper,min (65 % / 35 %) to the keeper output High / "
                f"Low, {inp.t_keeper_ns:.2f} ns; the driver holds the data past that threshold "
                "that long (Figures 18 and 19)")
    return ('Man_tKeeper_Response ("PHY1 & PHY2 Bus Keeper Behavior" table): from the '
            "keeper's input crossing V_IH / V_IL to its output current changing sign, "
            f"{inp.t_keeper_ns:.2f} ns; the driver holds the data that long past its crossing "
            f"of the Manager's {rise} / {fall} at the Manager before high-Z")


def keeper_threshold_names(inp: CalcInputs) -> tuple[str, str]:
    """The keeper's input thresholds as (rising, falling) bounds: a SWI3S Manager's V_IH,max
    and V_IL,min, a SoundWire Manager's V_TH_Keeper,max and V_TL_Keeper,min."""
    if is_soundwire(inp.Man_spec):
        return "V_TH_Keeper,max", "V_TL_Keeper,min"
    return "V_IH,max", "V_IL,min"


def keeper_thresholds(inp: CalcInputs, man_supply: float) -> tuple[float, float]:
    """The keeper's (rising, falling) input thresholds (`keeper_threshold_names`) as fractions
    of nominal at the Manager's supply `man_supply`: a SWI3S Manager's V_IH,max and V_IL,min
    rows times the supply, a SoundWire Manager's `sw13_keeper_thresholds`."""
    if is_soundwire(inp.Man_spec):
        return sw13_keeper_thresholds(man_supply)
    return inp.V_IH_max_frac * man_supply, inp.V_IL_min_frac * man_supply


def keeper_corners(inp: CalcInputs, lane: str,
                   supplies: Supplies) -> dict[bool, CrossingCorner]:
    """The keeper's two crossings of the releasing data on `lane`, by polarity (True rising),
    at the Manager's pin, where the keeper's input is: its threshold (`keeper_thresholds`) at
    the Manager's supply, the end of each polarity's row that the data crosses the later
    after its anchor, on a ramp to the releaser's supply. A Peripheral's data is offset by the
    ground shift α, the Manager's ground against the Peripheral's, as on every P->M crossing;
    the Manager's own data carries none."""
    side = "Man" if lane == "Man DATA" else "Per"
    man, tx = supplies[0], supplies[0] if side == "Man" else supplies[1]
    alpha = 0.0 if side == "Man" else inp.V_noise_pp_frac
    th, tl = keeper_thresholds(inp, man)
    rise, fall = keeper_threshold_names(inp)
    return {True: CrossingCorner(True, rise, th, man, +alpha, tx, +1),
            False: CrossingCorner(False, fall, tl, man, -alpha, tx, +1)}


def _keeper_case(inp: CalcInputs, lane: str, supplies: Supplies, *,
                 ede: bool, clk_rising: bool = True) -> tuple[bool, float, str]:
    """A keeper leg's crossing, as (rising, coefficient, note): from the releasing launch's
    anchor to its data crossing the keeper's threshold at the Manager (`keeper_corners`), on a
    late edge, as a coefficient of the lane's t_RF. The releaser's ramp swings to its own
    supply, and a Peripheral's data reaches the keeper offset by the ground shift α.

    The leg is evaluated on one data edge. A conventional release takes the edge whose
    crossing is the later after the side's clock reference: the launch's anchor
    (`sw_anchor_ns` at the max corner, from a Manager clock edge of polarity `clk_rising`)
    plus the crossing after it, rising on a tie. An EndDriveEarly release (`ede`), whose leg
    counts from the anchor and also credits the ramp to V_OH / V_OL on the same edge, takes
    the edge whose crossing less that ramp (`to_voh_frac`) is the larger."""
    side = "Man" if lane == "Man DATA" else "Per"
    shape = inp.Man_shape if side == "Man" else inp.Per_shape
    alpha = 0.0 if side == "Man" else inp.V_noise_pp_frac
    swing = inp.V_OH_min_frac - inp.V_OL_max_frac
    corners = keeper_corners(inp, lane, supplies)
    edges = {"rising": corners[True], "falling": corners[False]}
    fracs = {k: (crossing_time_frac(c, shape, swing_frac=swing)
                 - anchor_frac(inp, side, c.rising))
             for k, c in edges.items()}
    held = {k: fracs[k] - (to_voh_frac(inp, lane, c.rising) if ede else 0.0)
            for k, c in edges.items()}
    corr = {k: 0.0 if ede else sw_anchor_ns(inp, side, "max", rising=c.rising,
                                            clk_rising=clk_rising)
            for k, c in edges.items()}
    if corr["rising"] != corr["falling"]:
        trf = edge_factor(inp, lane, "late") * lane_ns(inp, lane)
        held = {k: held[k] * trf + corr[k] for k in held}
    tie = math.isclose(held["rising"], held["falling"], rel_tol=0.0, abs_tol=1e-12)
    edge = "rising" if tie or held["rising"] > held["falling"] else "falling"
    coef = fracs[edge] * edge_factor(inp, lane, "late")
    c = edges[edge]
    why = ("leaves the less held level past V_OH / V_OL" if ede else
           "crosses later after the clock reference")
    src = ("SoundWire keeper (Figures 18 and 19, Table 12)" if is_soundwire(inp.Man_spec)
           else 'keeper ("PHY1 & PHY2 Bus Keeper Behavior" table)')
    note = (f"{src}: the response counts from the data crossing {c.bound} at the Manager, "
            f"{100 * c.rx_threshold:.1f} % of nominal at its {100 * c.rx_supply:.0f} % supply"
            + (f", offset {100 * c.ground_shift:+.0f} % by the ground shift α"
               if alpha else "")
            + f", on a ramp to {100 * c.vdd_tx:.0f} %; the {edge} edge {why}, crossing "
            + f"{coef:.3f}·t_RF after the anchor on a "
            + ("fast" if lane in inp.fast_data_edge else "slow") + " edge")
    return c.rising, coef, note


def lane_ns(inp: CalcInputs, lane: str) -> float:
    """The t_RF in force on a named lane."""
    return getattr(inp, _LANE_FIELDS[lane])


_LANE_FIELDS = {"Man CLK": "tRF_Man_CLK_ns", "Man DATA": "tRF_Man_DATA_ns",
                "Per DATA": "tRF_Per_DATA_ns"}


def _fast_role(inp: CalcInputs, lane: str, role: str) -> str:
    """A crossing role on `lane`, qualified `fast` before `late` where its slow-edge role is
    evaluated on the fast edge (`CalcInputs.fast_data_edge`), after the polarity:
    `X_PM,rr,fast,late` is a late data crossing on a fast edge."""
    if lane in inp.fast_data_edge and role.split(",")[-1] == "late":
        return f"{role[:-len('late')]}fast,late"
    return role


def _cross_terms(inp: CalcInputs, base_sym: str,
                 lanes: tuple[CrossLane, ...], *, clk_pair: str | None = None) -> list[Term]:
    """A crossing envelope as one `coefficient · t_RF` term PER LANE, each named
    `<base_sym>,<role>`, the clock lane `<clk_pair>,<role>` where `clk_pair` is given (a lone
    clock detection inside an envelope, `X_MP` or `X_MP,B`). Each role names its edge first,
    `rise,` / `fall,` on a single crossing and the case letters `rr,` / `rf,` / `fr,` / `ff,`
    on a Sigma_cross,PM sum's two addends.

    `lanes` is (role, t_RF lane, dimensionless coefficient, operator) per leg of the
    envelope -- the operator per leg rather than per envelope, because Delta_cross is a
    DIFFERENTIAL: its late leg costs and its early leg credits, and folding that into a
    signed coefficient under one minus sign renders as "minus a negative" and reads as an
    error. A negative coefficient (an early crossing before its edge's 20 % point, at a large
    ground shift) is shown positive under the other operator (`_positive_coefficient`).

    ONE LANE'S CROSSING IS `X`, NOT SIGMA OR DELTA. Those symbols name the COMBINING --
    Sigma a sum over the two legs, Delta a difference between them -- so once each leg
    is shown separately with its own t_RF there is nothing left for them to denote. The
    composites keep their names where they are still composites:

        Delta_cross,MP        = X_MP,rise,late − X_MP,fall,early   (and its mirror)
        Sigma_cross,PM,setup  = X_PM,<case>,late (clock) + X_PM,<case>,late (data)
        Sigma_cross,PM,hold   = X_PM,<case>,early (clock) + X_PM,<case>,early (data)

    Where a case's clock addend is a lone detection's coefficient (Sigma_cross,PM,hold's rf and
    fr cases) it carries that detection's `X_MP` name (`clk_pair`) and the data addend the lone
    data crossing's `X_PM,rise,early` / `X_PM,fall,early`.

    THE CROSSING COEFFICIENTS ARE DIMENSIONLESS, which is the other half of why the
    per-leg form is worth having: each X is a coefficient, and the lanes carry DIFFERENT
    t_RF -- Man CLK, Man DATA and Per DATA are three independent rows.

    It cannot be collapsed to a single `Sigma x t_RF`: that is equal only when the lanes
    happen to share a t_RF, and both envelopes additionally select their coefficient
    pair by a max (setup) or min (hold) over branches, so even the coefficients are not
    fixed. The `*_lanes` accessors return the branch actually in force.

    CLOCK LANE FIRST, always, because the terms are read as a TIME ORDER: the clock edge is
    what the receiving device detects, and the data crossing is judged against it. The
    envelope tuples list their legs late-then-early (the differential's own order, which is
    about SIGN), so the terms are sorted here.
    """
    out: list[Term] = []
    for role, lane, coeff, lane_op in sorted(
            lanes, key=lambda leg: 0 if leg[1].endswith("CLK") else 1):
        trf = lane_ns(inp, lane)
        role = _fast_role(inp, lane, role)
        pair = clk_pair if clk_pair and lane.endswith("CLK") else base_sym
        out.append(Term(f"{pair},{role} · {_trf_sym(lane)}",
                        (-1.0 if lane_op == "-" else 1.0) * coeff * trf, op=lane_op,
                        note=f"{abs(coeff):.2f} (dimensionless) · {trf:.2f} ns",
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

    The contention legs net their bus traversals (`_NET_PD`): the pair is a skew and an
    overlap allowance that CANCEL on two of the three legs, which then show no `t_PD` term,
    and M->P shows its net two as two `t_PD` terms. Here they do not cancel -- both are
    charged on setup, both credited on hold.
    """
    return Term("t_PD", (+t_PD_ns if credit else -t_PD_ns),
                op=("+" if credit else "-"), note=note)


# Which flight each traversal is, on a leg whose launcher is a peripheral.
_PD_TO_LAUNCHER = "the clock's flight from the Manager to the launching peripheral"
_PD_FROM_LAUNCHER = "the launched data's flight back across the bus to the sampler"


def _trf_sym(lane: str) -> str:
    """The t_RF symbol for a crossing leg: `Man_t_RF,CLK`, `Per_t_RF,DATA`, and so on.

    ALWAYS QUALIFIED, on every lane, so the term points at the ROW a reader would edit.
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
    """`(ns, coefficient, t_RF)` rendered as the product it is, the coefficient positive
    (`_positive_coefficient`).

    The t_RF comes from the parts rather than being re-read from the row, so the displayed
    product and the value in force cannot disagree.
    """
    _, coeff, trf = parts
    if not trf:
        return ""
    return f"{abs(coeff):.2f} (dimensionless) · {trf:.2f} ns"


def _positive_coefficient(t: "Term") -> "Term":
    """`t` with a negative product coefficient (`Term.factors`) shown positive under the other
    operator, so the operator is `-` exactly where the value subtracts. The value is
    unchanged."""
    if t.factors is None or len(t.factors) != 2 or t.factors[0] >= 0.0:
        return t
    return replace(t, op="+" if t.op == "-" else "-", factors=(-t.factors[0], t.factors[1]))


def _gather_lane_terms(terms: list["Term"]) -> list["Term"]:
    """Fold crossing terms that multiply the SAME t_RF row into one product.

    ONE ROW, ONE VALUE. A P->P or contention leg reads `Man_t_RF,CLK` twice -- once for each
    device's detection of the clock -- and two separate products would invite two
    independently cornered values for one Manager pin's slew setting. Each coefficient
    carries its own edge's slew factor: on a leg whose two detections are of one edge, that
    edge's single (1 + τ) or (1 − τ) (`pp_clock_edges`), and on a leg whose detections are of
    two edges, each edge's own. The row value is common to both, so their sum is a single
    product.

    COLLAPSING IS EXACT HERE, AND ONLY HERE. `_cross_terms`' own docstring rules out
    collapsing an envelope to one `Sigma x t_RF` because "that is equal only when the lanes
    happen to share a t_RF" -- which is precisely the condition this function tests for. Two
    terms on DIFFERENT rows (Man CLK vs Per DATA) are left alone and keep the per-lane form.

    THE COMBINED COEFFICIENT IS SHOWN POSITIVE, with the operator carrying the sign, because
    a difference folded under a minus renders as "minus a negative" and reads as an error --
    the same reason `_cross_terms` keeps an operator per leg. The note carries the
    decomposition, so which device contributed what is still on the page:

        - (X_MP,B,rise,late − X_MP,A,rise,early) · Man_t_RF,CLK
                                            c = c_late late - c_early early

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
        # so folding one again would treat a coefficient as a t_RF -- which happens when
        # `__post_init__` runs on a re-constructed breakdown.
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
        # equation states something else entirely: under a hoisted minus, a charge c_late and
        # a credit c_early net to -(c_late - c_early)·t_RF, and writing the bracket as
        # `- (late + early)` would state -(c_late + c_early)·t_RF. Dividing by the overall
        # sign gives `- (late - early)`.
        overall = -1.0 if total < 0 else 1.0
        rel = [s / overall for s in signed]
        # Positive contribution first, so the parenthesis never opens with a minus.
        order = sorted(range(len(rel)), key=lambda i: rel[i] < 0)

        def _joined(texts) -> str:
            bits = []
            for k, i in enumerate(order):
                sign = "−" if rel[i] < 0 else "+"
                bits.append(texts[i] if k == 0 else f"{sign} {texts[i]}")
            return " ".join(bits)

        # PARENTHESISED, WITH BOTH CROSSINGS NAMED: `(X_MP,A,rise,late − X_MP,B,fall,early) ·
        # Man_t_RF,CLK`. The operator is hoisted out, so the bracket is what makes
        # `- (a - b)` legible.
        #
        # THREE NUMBERS IN THE SUBSTITUTION ROW: `(c_late - c_early)·t_RF`, so the netting can
        # be checked against the symbol beside it. `factors` carries the coefficients FIRST and
        # the row value LAST (see `Term.factors`), so `factors[-1]` is still "the t_RF this
        # term reads".
        # Each contribution is named by its role, to three places so the stated sum adds up at
        # the shown precision.
        def _label(h: str) -> str:
            return h.split(",")[-1]

        note = (f"{abs(total):.3f} = "
                f"{_joined([f'{abs(s):.3f} ' + _label(h) for h, s in zip(heads, rel)])}"
                f", at t_RF {trf:.2f} ns")
        merged = Term(f"({_joined(heads)}) · {lane}", total * trf,
                      op="+" if total >= 0 else "-", note=note,
                      factors=(*[rel[i] for i in order], trf))
        out[idxs[0]] = merged
        drop.update(idxs[1:])
    return [term for i, term in enumerate(out) if i not in drop]


def _per_clk_cross_parts(inp: CalcInputs, env: DeltaTpdEnvelope, *,
                         corner: str, device: str = "A", edge: str | None = None,
                         rising: bool = True) -> CrossParts:
    """When a PERIPHERAL recognises the forwarded clock edge, relative to the Manager's own
    internal timing reference, as (ns, dimensionless coefficient, t_RF in ns): at the
    Peripheral's clock input (`device` "A") or Peripheral B's ("B"), on an edge of slew factor
    `edge` (`edge_factor`'s role; the corner's own when None).

    The pair is reported rather than recomputed by the caller so the displayed product and
    the value in force come from one expression.

    A rising detection is at V_IH: `mp_late_rise` late, `sigma_pm_hold_rf_clk` early. A
    falling one (`rising` False) is at V_IL: `mp_late_fall` late, `mp_early_fall` early. B's
    are the `_b` fields, at B's input and supply.

    THE SYMBOL NAMES THE CROSSING'S DIRECTION AND ITS EDGE (`clock_detection_symbol`): the
    Manager drives the clock and a Peripheral receives it, so every detection is `X_MP`,
    whichever envelope field carries its coefficient, and names its edge's polarity:

        X_MP,rise,late     rising, late     mp_late_rise           (MP_hold's clock lane)
        X_MP,fall,late     falling, late    mp_late_fall           (MP_hold's clock lane)
        X_MP,fall,early    falling, early   mp_early_fall          (MP_setup's clock lane)
        X_MP,rise,early    rising, early    sigma_pm_hold_rf_clk   (MP_setup's clock lane)

    so a detection carries the symbol of the M->P clock-lane crossing whose coefficient it
    equals at every supply assignment. On a P->P leg the device letter follows the pair:
    `X_MP,A,...` at Peripheral A, and `X_MP,B,...` at Peripheral B, the `_b` fields at B's
    input. `slow` / `fast` between the polarity and the role name an edge whose slew factor is
    not the corner's own (`_role_on_edge`), whose coefficient differs.

    Priced the way every other leg in this model prices a threshold crossing: a
    clock-lane crossing coefficient times that lane's t_RF.
    A Manager gets nothing -- its timing is measured from its clock reference, which the UI
    and every Manager time share, so no crossing enters.

    A lane coefficient and not a whole Sigma: contention turns on the CLOCK crossing
    alone. t_DZ and t_ZD bound driver state, so the data lane of those envelopes does
    not apply. The P->P data legs charge peripheral A's clock detection the same way, A
    being an independent device charged in full.

    The corner follows the DIRECTION, which is the part a single-valued term cannot
    express:

        corner="late"   Delta_cross,MP's late coefficient, `X_MP,rise,late`: a rising
                        crossing at V_IH,max raised by the full noise budget, on a
                        slow edge, from the transmitter's supply in the leg's supply
                        assignment (`compute` evaluates every assignment) -- the late clock
                        detection MP_hold charges on `Man_t_RF,CLK`. A RELEASING
                        peripheral recognises the edge this late, so it holds the
                        bus that much past the Manager's reference. A charge.
        corner="early"  Delta_cross,MP's early clock coefficient, `X_MP,fall,early` and
                        `X_MP,rise,early` rising, the clock lanes of Sigma_cross,PM,hold's fr
                        and rf cases. An ACQUIRING
                        peripheral may recognise it this soon, so it turns on that
                        much earlier. Its own turn-on is a credit, so the early
                        corner is the one that erodes the gap.

    THE LATE CORNER IS NOT Sigma_cross,PM,setup's CLOCK ADDEND. That addend belongs to a
    two-lane sum whose ground-shift polarity is chosen jointly with the data lane, and
    the polarity flips (lowering the clock threshold) once the Per DATA lane is the slower
    or the shapes differ. A lone clock detection has no data lane to trade against, so
    it is always cornered at the raised threshold, and it carries the `X_MP,rise,late` name
    whose coefficient it equals; the addend is `X_PM,rr,late` or `X_PM,rf,late`.

    ONE CLOCK EDGE, ONE SLEW, EVEN WITH TWO PERIPHERALS ON THE LEG. t_RF on the clock
    lane is the MANAGER's output slew: one clock, one driver, one pin. Where a leg's two
    peripherals detect ONE edge (`pp_shares_clock_edge`), both detections take that edge's
    single slew factor, (1 + τ) or (1 − τ), and the leg takes the one that leaves it the less
    margin (`pp_clock_edges`). What differs between the two peripherals is where their
    thresholds sit on that edge (each receiver's own supply, ground shift and V_IH/V_IL
    corner, so each keeps its own crossing coefficient) and their internal delays (their own
    parameters). A detection whose edge factor is not its corner's own carries the edge in
    its symbol: `X_MP,A,rise,slow,early` is A's early detection of a rising edge on a slow
    edge (`_role_on_edge`).
    Where the two detections are of DIFFERENT edges (setup's launching and sampling edges,
    a handover's edges N_HO UIs apart), each edge keeps its corner's own factor.

    Both crossings read the one row value the corner search picked for `Man_t_RF,CLK`, and a
    leg that reads the lane twice GATHERS the two into one product (`_gather_lane_terms`) --
    exact because they share a row.
    """
    b = device == "B"
    if not rising and corner == "late":
        c = env.mp_late_fall_b if b else env.mp_late_fall
    elif not rising:
        c = env.mp_early_fall_b if b else env.mp_early_fall
    elif corner == "late":
        c = env.mp_late_rise_b if b else env.mp_late_rise
    else:
        c = env.sigma_pm_hold_rf_clk_b if b else env.sigma_pm_hold_rf_clk
    coeff = c * _slew_adj(inp, "Man CLK", corner, edge or corner)
    return (coeff * inp.tRF_Man_CLK_ns, coeff, inp.tRF_Man_CLK_ns)


def clock_detection_symbol(corner: str, *, rising: bool, device: str = "",
                           edge: str | None = None) -> str:
    """The crossing symbol of a Peripheral's detection of the Manager's clock, for the same
    `corner`, `rising` and `edge` `_per_clk_cross_parts` prices: `X_MP`, then the device letter
    on a P->P leg (`device` "A" or "B"; "" for the single Peripheral of any other leg), then
    the edge's polarity, `rise` or `fall`, then the edge qualifier and the role
    (`_role_on_edge`)."""
    device_q = f"{device}," if device else ""
    polarity = "rise," if rising else "fall,"
    return f"X_MP,{device_q}{polarity}{_role_on_edge(corner, edge or corner)}"


def ede_release_rising(inp: CalcInputs, side: str, *, clk_rising: bool = True) -> bool:
    """The polarity of one side's EndDriveEarly release's last data edge on a contention leg,
    launched from a clock edge of polarity `clk_rising` (the opening edge): the edge that
    reaches V_OH / V_OL the later, the later release (`valid_edge_rising`), falling on a
    tie."""
    return valid_edge_rising(inp, "Man DATA" if side == "Man" else "Per DATA", tie=False,
                             clk_rising=clk_rising)


def _ede_terms(inp: CalcInputs, side: str, *,
               clk_rising: bool = True) -> tuple[float, float, float]:
    """(t_DD, ramp to 80 %, EDE Delay) of one side's EndDriveEarly release launched from a
    clock edge of polarity `clk_rising`, in ns: the latest release, so t_DD at its max corner
    and the data edge `ede_release_rising`. A closed form computed apart from the terms a leg
    shows (`_ede_release_terms`), which the tests check those terms against; no leg reads it.

    t_DD ends at the launch's anchor, 20 % of the ramp, and the ramp from there to 80 % is
    `_to_voh_ns`. A SoundWire side's t_DD carries its anchor correction (`_launch_anchor_ns`)
    at that data edge's polarity.
    """
    rising = ede_release_rising(inp, side, clk_rising=clk_rising)
    dd = _launch_anchor_ns(inp, side, "DD", "max", rising=rising, clk_rising=clk_rising)
    lane = "Man DATA" if side == "Man" else "Per DATA"
    return (dd, _to_voh_ns(inp, lane, rising),
            inp.Man_t_EDE_ns if side == "Man" else inp.Per_t_EDE_ns)


def _ede_release_vs_edge_ns(inp: CalcInputs, side: str, *, clk_rising: bool = True) -> float:
    """An EndDriveEarly release launched from a clock edge of polarity `clk_rising`, relative
    to the clock edge that closes the driven UI. Negative is before that edge, which is what
    makes it early.

        t_DD + (ramp to V_OH / V_OL) + EDE Delay − UI

    A Peripheral's edges are its own detections of the clock, which the caller adds with its
    clock arrival. A Manager's are its clock references, the time origin, so nothing is added.
    The closed form of `_ede_release_terms`'s sum, for the tests; no leg reads it.
    """
    return sum(_ede_terms(inp, side, clk_rising=clk_rising)) - inp.UI_ns()


def _ede_release_terms(inp: CalcInputs, side: str, *, clk_rising: bool = True) -> list["Term"]:
    """An EndDriveEarly release as a contention leg shows it, in time order from the edge that
    opens the driven UI, of polarity `clk_rising`: the launch to its anchor (`_launch_terms` at
    the max corner, with a SoundWire side's `,anchor`), the ramp from there to 80 %
    (`_to_voh_ns`), the EDE Delay, and the driven UI back to the closing edge, a credit. Their
    sum is `−_ede_release_vs_edge_ns`. The contention legs read these terms; the keeper legs
    compose their own EndDriveEarly release (`ede_held` in `_compute_at`) from the same ramp
    and EDE Delay, which carry the same symbols, values and factors there. The UI carries the
    setup legs' symbol."""
    lane = "Man DATA" if side == "Man" else "Per DATA"
    t_ede = inp.Man_t_EDE_ns if side == "Man" else inp.Per_t_EDE_ns
    rising = ede_release_rising(inp, side, clk_rising=clk_rising)
    return [
        *_launch_terms(inp, side, "DD", "max", credit=False, rising=rising,
                       clk_rising=clk_rising),
        _to_voh_term(inp, lane, side, rising, credit=False),
        Term(f"{side}_t_EDE", -t_ede, op="-",
             note=(f"latest release: {side} holds its level {t_ede:.2f} ns past the ramp's "
                   f"80 % point (V_OH / V_OL) before high-Z")),
        Term("UI", +inp.UI_ns(), op="+",
             note=("the driven UI: an EndDriveEarly release is referenced to the edge that "
                   "opens it, one UI before the closing edge")),
    ]


# A clocked Manager timing inside a margin's UI-scaled part, for `_rate_windows`: its row
# ("DD", "ZD" or "DZ"), the sign it enters the margin with, and its value at the target rate.
ClockedPart = tuple[str, float, float]

# A span of clock rates (lo, hi] in MHz over which a leg passes; hi may be math.inf.
RateWindow = tuple[float, float]


class FmaxFields(TypedDict):
    """An `InequalityBreakdown`'s F_max and its note, from its passing rates."""
    F_max_MHz: float
    F_max_note: str


def _fmax_fields(inp: CalcInputs, windows: Sequence[RateWindow]) -> FmaxFields:
    """`_fmax_from_windows` as the breakdown fields it fills. An F_max at
    `toggle_ceiling_MHz` says so in the note, with the numbers of its formula, as the leg itself
    never fails below it. `_clip_to_ceiling` leaves no window above the ceiling, so such a
    note names no higher window."""
    f_max, note = _fmax_from_windows(windows)
    ceiling = toggle_ceiling_MHz(inp)
    if math.isfinite(f_max) and math.isclose(f_max, ceiling, rel_tol=1e-12):
        d = inp.duty_min
        t_max = 1000.0 * d / ceiling
        note = (f"is the toggle ceiling 1000 × {d:.2f} / t_RF,max = 1000 × {d:.2f} / "
                f"{t_max:.2f} ns = {ceiling:.2f} MHz: the shortest half period, {d:.2f} of the "
                f"period, holds one complete edge of the slowest lane, and above it no edge "
                f"reaches V_OH / V_OL")
    return FmaxFields(F_max_MHz=f_max, F_max_note=note)


def _fmax_from_windows(windows: Sequence[RateWindow]) -> tuple[float, str]:
    """F_MAX IS THE FIRST FAILURE: the lowest rate at which the leg fails as F_CLK rises from
    zero, 0.0 if it fails at the lowest rates, math.inf if it never fails. `windows` are the
    leg's passing rates, ascending and disjoint.

    The note names the higher windows in which the leg passes again, "passes again from
    12.50 to 24.33 MHz", or "passes from ..." after an F_max of 0.0; an open window reads
    "from 12.50 MHz up". Up to three windows are each named; more are counted and spanned,
    with the first two and the last named: "passes again in 4 windows from 12.50 to 54.22 MHz:
    from 12.50 to 24.33 MHz, from 25.00 to 36.50 MHz, …, from 50.00 to 54.22 MHz". Empty when
    there is none."""
    if windows and windows[0][0] <= 0.0:
        f_max, later = windows[0][1], list(windows[1:])
    else:
        f_max, later = 0.0, list(windows)
    if not later:
        return f_max, ""

    def span(lo: float, hi: float) -> str:
        return f"from {lo:.2f} MHz up" if math.isinf(hi) else f"from {lo:.2f} to {hi:.2f} MHz"

    head = "passes again " if f_max > 0.0 else "passes "
    if len(later) <= 3:
        return f_max, head + ", and ".join(span(lo, hi) for lo, hi in later)
    return f_max, (f"{head}in {len(later)} windows {span(later[0][0], later[-1][1])}: "
                   f"{span(*later[0])}, {span(*later[1])}, …, {span(*later[-1])}")


def _windows_union(*sets: Sequence[RateWindow]) -> list[RateWindow]:
    """The rates at which any of `sets` passes, as ascending disjoint windows."""
    out: list[RateWindow] = []
    for lo, hi in sorted(w for ws in sets for w in ws):
        if out and lo <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], hi))
        else:
            out.append((lo, hi))
    return out


def _piece_windows(duty_min: float, b: float,
                   pieces: Sequence[tuple[float, float, float]]) -> list[RateWindow]:
    """The passing rates of a margin ``A·UI + B`` with ``UI = 1000·duty_min/f``, B = `b`, over
    consecutive rate pieces (lo, hi, A), as ascending disjoint windows. On a piece the margin
    is monotone in f:

        A > 0     passes up to f = 1000·duty_min·A/(−B), everywhere if B ≥ 0
        A < 0     passes from f = 1000·duty_min·(−A)/B, nowhere if B ≤ 0
        A = 0     passes everywhere if B ≥ 0, nowhere otherwise
    """
    k = 1000.0 * duty_min
    out: list[RateWindow] = []
    for lo, hi, a in pieces:
        if abs(a) <= 1e-12:
            w = (lo, hi) if b >= 0.0 else None
        elif a > 0.0:
            top = math.inf if b >= 0.0 else k * a / -b
            w = (lo, min(hi, top)) if top > lo else None
        else:
            bottom = k * -a / b if b > 0.0 else math.inf
            w = (max(lo, bottom), hi) if bottom < hi else None
        if w is not None and w[1] > w[0]:
            out = _windows_union(out, [w])
    return out


def toggle_ceiling_MHz(inp: CalcInputs) -> float:
    """The highest rate at which the bus can toggle at all, ``1000 · duty_min / t_RF,max``: the
    shortest half period, the UI every leg is evaluated on (`CalcInputs.UI_ns`), holds one
    complete V_OL-to-V_OH edge of the slowest lane, so the clock is a triangle that just reaches
    V_OH and V_OL. Above it no edge completes and no margin has a meaning.

    t_RF,max is the slowest edge any of the three lanes may have: the top of its spec's range
    (`trf_range_ns`, the per-edge slew tolerance included), or the lane's row value where that
    is higher (`edge_factor` widens the range the same way).
    """
    t_max = max(max(trf_range_ns(inp.Per_spec if lane == "Per DATA" else inp.Man_spec,
                                 inp.system_size)[1], lane_ns(inp, lane))
                for lane in ("Man CLK", "Man DATA", "Per DATA"))
    return 1000.0 * inp.duty_min / t_max if t_max > 0.0 else math.inf


def _clip_to_ceiling(windows: Sequence[RateWindow], ceiling: float) -> list[RateWindow]:
    """`windows` limited to rates up to `ceiling`: a leg passing there fails at the ceiling."""
    return [(lo, min(hi, ceiling)) for lo, hi in windows if lo < ceiling]


def _rate_windows(inp: CalcInputs, *, margin_ns: float, ui_part_ns: float,
                  clocked: Sequence[ClockedPart] = ()) -> list[RateWindow]:
    """The clock rates at which a leg's margin is non-negative, as ascending disjoint windows.

    `margin_ns` is the margin at the target rate and `ui_part_ns` the part of it that
    scales with UI there, so the margin at any rate is ``A·UI + B`` with
    ``A = ui_part_ns / UI`` and ``B = margin_ns − ui_part_ns`` (`_piece_windows`).

    The UI-scaled parts a leg carries:

    * the UI itself on a setup leg, `N_HO·UI` on a handover leg (`_handover_ui_ns`), and
      the closing UI of a conventional release on a keeper leg
    * a clocked Manager t_DD, t_ZD or t_DZ, which sits at a fraction of the UI
      (`_man_clocked_ui_part_ns`)
    * the −UI that references an EndDriveEarly release to the closing edge, on either
      side (`_ede_release_terms`)

    `clocked` lists the clocked Manager timings inside `ui_part_ns`. One with a floor
    (`CalcInputs.Man_t_DD_floor_ns` and its siblings) is placed at each rate as the Timing
    Calculator places it, so the margin is affine in UI between the rates where a placement
    steps, and `_rate_windows_floored` solves it piece by piece; there a leg can fail, pass
    again and fail again as the rate rises. Without a floor the fraction is held at its value
    at the target.

    Every leg reports its F_max through here, setup or not (`_fmax_from_windows`); a leg with
    no UI-scaled part passes at every rate or at none.

    An empty list under `_MARGINS_ONLY`: a breakdown built there carries F_max 0.0 and no
    note, and never leaves the corner search.

    A leg with a UI-scaled part passes no rate above `toggle_ceiling_MHz`, so its F_max is
    at most the ceiling; a leg without one keeps its every-rate-or-none answer.
    """
    if _MARGINS_ONLY.get():
        return []
    windows = _rate_windows_unbounded(inp, margin_ns=margin_ns, ui_part_ns=ui_part_ns,
                                      clocked=clocked)
    if ui_part_ns == 0.0:
        return windows
    return _clip_to_ceiling(windows, toggle_ceiling_MHz(inp))


def _rate_windows_unbounded(inp: CalcInputs, *, margin_ns: float, ui_part_ns: float,
                            clocked: Sequence[ClockedPart]) -> list[RateWindow]:
    """`_rate_windows` before the clock's ceiling is applied."""
    UI = inp.UI_ns()
    if UI <= 0.0:
        return [(0.0, math.inf)] if margin_ns >= 0.0 else []
    floors = {"DD": inp.Man_t_DD_floor_ns, "ZD": inp.Man_t_ZD_floor_ns,
              "DZ": inp.Man_t_DZ_floor_ns}
    floored = [(sign, value, floors[row]) for row, sign, value in clocked
               if floors[row] is not None]
    if floored and launch_grid_UI(inp.man_launch_mode()) is not None:
        return _rate_windows_floored(inp, margin_ns=margin_ns, ui_part_ns=ui_part_ns,
                                     floored=floored)
    return _piece_windows(inp.duty_min, margin_ns - ui_part_ns,
                          [(0.0, math.inf, ui_part_ns / UI)])


# The highest rate `_rate_windows_floored` searches, in MHz. Past it the margin is taken to keep
# the sign it has there.
_FLOORED_FMAX_CAP_MHZ = 10_000.0


def _rate_windows_floored(inp: CalcInputs, *, margin_ns: float, ui_part_ns: float,
                          floored: Sequence[tuple[float, float, float | None]]
                          ) -> list[RateWindow]:
    """`_rate_windows` with clocked Manager timings placed at each rate with their floors.

    `floored` holds (sign, value at the target, floor) for each. At rate f a timing is
    ``snap_to_launch_grid(value·f_target/f, floor_ns=floor)``, which is `k·g·UI` with g the
    grid in UI and k an integer constant between the rates where the floor moves it, so the
    margin is ``A_k·UI + B`` on each such piece: B is the part that does not scale with UI,
    and A_k the target's other UI-scaled parts per UI plus ``Σ sign·k·g``.

    The pieces run from zero up to a rate past every placement step beyond which the margin
    keeps one sign: as UI → 0 each timing tends to its floor less SNAP_TOL_NS, from above, so
    the margin tends to ``m_inf = B + Σ sign·(floor − SNAP_TOL_NS)``, and within one grid step
    of it once UI is small enough.
    Past that rate the leg passes if m_inf is positive and fails if it is negative; at zero
    the search stops at `_FLOORED_FMAX_CAP_MHZ`.
    """
    mode = inp.man_launch_mode()
    grid = launch_grid_UI(mode)
    assert grid is not None
    d, f_t, ui_t = inp.duty_min, inp.F_CLK_target_MHz, inp.UI_ns()
    g_ns_MHz = grid * 1000.0 * d                     # the grid step is g_ns_MHz / f
    b = margin_ns - ui_part_ns
    a0 = (ui_part_ns - sum(sign * value for sign, value, _ in floored)) / ui_t

    def ui(f: float) -> float:
        return 1000.0 * d / f

    def slope(f: float) -> float:
        """A on the piece holding rate f."""
        return a0 + sum(sign * snap_to_launch_grid(value * f_t / f, mode, f, d,
                                                    floor_ns=floor) / ui(f)
                        for sign, value, floor in floored)

    # Where each placement steps, with e = floor − SNAP_TOL_NS: above f_lift the target's grid
    # point k0 is below e and is raised to ceil(e/step), which steps where e·f/g_ns_MHz is an
    # integer.
    lifts = []
    for _sign, value, floor in floored:
        assert floor is not None
        k0 = math.floor(value * f_t / g_ns_MHz + 0.5)
        lifts.append(k0 * g_ns_MHz / (floor - SNAP_TOL_NS) if floor > SNAP_TOL_NS
                     else math.inf)
    m_inf = b + sum(sign * (floor - SNAP_TOL_NS) for sign, _v, floor in floored
                    if floor is not None)
    # Past every lift, each placement is within one step above its floor less SNAP_TOL_NS, so
    # the margin is within c·UI of m_inf, and on m_inf's side of m_inf / 2 where
    # UI < |m_inf| / (2c): c counts a step on the placements that move the margin towards zero.
    c_up = abs(a0) + grid * sum(1.0 for sign, _v, _f in floored if sign > 0)
    c_lo = abs(a0) + grid * sum(1.0 for sign, _v, _f in floored if sign < 0)
    f_hi = max([x for x in lifts if math.isfinite(x)], default=0.0)
    if m_inf < 0.0:
        f_hi = max(f_hi, 2000.0 * d * c_up / -m_inf)
    elif m_inf > 0.0:
        f_hi = max(f_hi, 2000.0 * d * c_lo / m_inf)
    else:
        f_hi = _FLOORED_FMAX_CAP_MHZ
    capped = f_hi >= _FLOORED_FMAX_CAP_MHZ
    f_hi = min(max(f_hi, f_t), _FLOORED_FMAX_CAP_MHZ) * (1.0 + 1e-9)
    edges = {f_hi}
    for (_sign, _value, floor), lift in zip(floored, lifts):
        assert floor is not None
        if lift < f_hi:
            edges.add(lift)
            e = floor - SNAP_TOL_NS
            n = max(math.ceil(e * lift / g_ns_MHz), 0)
            while (f_n := n * g_ns_MHz / e) < f_hi:
                if f_n > lift:
                    edges.add(f_n)
                n += 1
    tops = sorted(x for x in edges if x > 0.0)
    pieces = [(tops[i - 1] if i > 0 else 0.0, hi,
               slope(0.5 * ((tops[i - 1] if i > 0 else 0.0) + hi))) for i, hi in enumerate(tops)]
    windows = _piece_windows(d, b, pieces)
    passes_at_top = bool(windows) and windows[-1][1] >= f_hi
    if (capped and passes_at_top) or (not capped and m_inf > 0.0):
        windows = _windows_union(windows, [(f_hi, math.inf)])
    return windows


def _handover_ui_ns(inp: CalcInputs, UI: float) -> float:
    """The allocated handover UIs in ns: the value `_handover_ui_terms` shows, all of which
    scales with UI."""
    return max(inp.handover_UIs, 0.0) * UI


def _man_clocked_ui_part_ns(inp: CalcInputs, value_ns: float) -> float:
    """The part of a Manager output timing that scales with UI.

    All of it under a clocked launch mode, which places the value at a fraction of the
    UI (`snap_to_launch_grid`); none of it under the analog delay.
    """
    return value_ns if launch_grid_UI(inp.man_launch_mode()) is not None else 0.0


def _man_tdd_range(inp: CalcInputs) -> tuple[float, float]:
    """(min, max) Man_t_DD after applying the launch mode.

    THE LAUNCH MODE SETS THE CLOCK'S RESOLUTION, NOT THE PLACEMENT. A clocked edge lands on a
    known clock phase, so both corners collapse to one value, the row's max placed on the
    mode's grid with the row's floor (`_man_clocked_grid_ns`). WHICH phase is a design choice
    on the grid the mode provides: a 4x clock offers three interior placements (0.25, 0.50,
    0.75 UI), not one.
    """
    if launch_grid_UI(inp.man_launch_mode()) is None:
        return inp.Man_t_DD_min_ns, inp.Man_t_DD_max_ns
    snapped = _man_clocked_grid_ns(inp, inp.Man_t_DD_max_ns, inp.Man_t_DD_floor_ns)[0]
    return snapped, snapped


def _man_clocked_grid_ns(inp: CalcInputs, value_ns: float,
                         floor_ns: float | None) -> tuple[float, str]:
    """`value_ns` on the grid the launch mode can place, at or above `floor_ns`
    (`snap_to_launch_grid`), plus a provenance note; `value_ns` and no note under the analog
    delay.

    EVERY MANAGER OUTPUT TIMING IS PLACED HERE: t_DD (`_man_tdd_range`), t_ZD
    (`_man_tzd_parts`) and t_DZ (`man_tdz_parts`). One Manager has one output stage, so a
    clocked launch mode clocks all three, and its output enable with them; EndDriveEarly
    replaces the release, not the turn-on. The mode sets the RESOLUTION, not the grid point:
    each row stays an editable design choice, snapped to a point the selected clock can place.
    The floor is the row's (`CalcInputs.Man_t_DD_floor_ns` and its siblings), so the placement
    at the target rate is the one `_rate_windows` re-places at every other rate.

    Returned as a pair so the substitution shown in the results tree can say the value was
    moved and why.
    """
    mode = inp.man_launch_mode()
    grid = launch_grid_UI(mode)
    if grid is None:
        return value_ns, ""
    snapped = snap_to_launch_grid(value_ns, mode, inp.F_CLK_target_MHz, inp.duty_min,
                                  floor_ns=floor_ns)
    UI = inp.UI_ns()
    if math.isclose(snapped, value_ns, abs_tol=SNAP_TOL_NS):
        return snapped, (f"clocked: {snapped / UI:.2f} UI on the "
                         f"{grid:g} UI grid a {mode.value} clock places")
    nearest = snap_to_launch_grid(value_ns, mode, inp.F_CLK_target_MHz, inp.duty_min)
    if floor_ns is not None and not math.isclose(snapped, nearest, abs_tol=SNAP_TOL_NS):
        return snapped, (f"{value_ns:.2f} ns is not on the {grid:g} UI grid a "
                         f"{mode.value} clock can place → raised to {snapped:.2f} ns "
                         f"({snapped / UI:.2f} UI), the lowest point at or above its "
                         f"minimum {floor_ns:.2f} ns")
    return snapped, (f"{value_ns:.2f} ns is not on the {grid:g} UI grid a "
                     f"{mode.value} clock can place → snapped to "
                     f"{snapped:.2f} ns ({snapped / UI:.2f} UI)")


def _man_tzd_parts(inp: CalcInputs) -> tuple[float, str]:
    """Man_t_ZD as the model uses it, on the launch mode's grid (`_man_clocked_grid_ns`), plus
    the note saying how it got there.

    ONE definition, read by BOTH the setup and hold handover variants and the contention
    legs, so one device has one turn-on time.

    A clocked value is deterministic, so min == max and the same number serves the corners
    the two families need. That is the optimistic direction for contention, which reads
    t_ZD,min: a clocked turn-on carries no PVT spread, which assumes zero analog turn-on
    after the clock edge.
    """
    return _man_clocked_grid_ns(inp, inp.Man_t_ZD_ns, inp.Man_t_ZD_floor_ns)


def _man_tzd_ns(inp: CalcInputs) -> float:
    """`_man_tzd_parts`' value."""
    return _man_tzd_parts(inp)[0]


def man_tdz_parts(inp: CalcInputs) -> tuple[float, str]:
    """Man_t_DZ,max as the model uses it, on the launch mode's grid (`_man_clocked_grid_ns`),
    plus the note saying how it got there.

    ONE definition, read by BOTH the keeper leg and the contention legs, so one output
    stage has one release time.

    The DIRECTION differs between the two families: a later release means the Manager holds
    the bus longer, so MP contention gets WORSE while the keeper gets better. Under
    EndDriveEarly the row is not read; the release is the launch plus the ramp plus the EDE
    Delay (`_ede_release_terms`), and its launch is the clocked t_DD.
    """
    return _man_clocked_grid_ns(inp, inp.Man_t_DZ_max_ns, inp.Man_t_DZ_floor_ns)


def _memo(inp: CalcInputs) -> dict:
    """The memo of values computed from `inp`, held on the instance. `CalcInputs` is frozen, so
    a value that is a function of its fields alone is computed once per instance; `replace`
    builds a new instance with an empty memo."""
    return inp.__dict__.setdefault("_memo", {})


def edge_factor(inp: CalcInputs, lane: str, role: str) -> float:
    """One edge's t_RF over its lane's row value: (1 + τ) for a "late" edge, (1 − τ) for an
    "early" one, τ being the per-edge slew tolerance.

    NO EDGE LEAVES ITS SPEC'S RANGE (`trf_range_ns`): where (1 ± τ) would take an edge past an
    end, that edge stops at the end and the other edge takes the whole 2τ mismatch, so at the
    slow end of the 5 ns setting the pair is (1, 1 − 2τ) and at the fast end (1 + 2τ, 1).
    Where the range is narrower than the 2τ mismatch the pair is the range's two ends. A row
    value outside the range widens the range to include it.

    A lane in `CalcInputs.fast_data_edge` gives its "late" role the fast factor.
    """
    memo = _memo(inp)
    pair = memo.get(("edge", lane))
    if pair is None:
        trf = lane_ns(inp, lane)
        tau = inp.tRF_tolerance_frac
        spec = inp.Per_spec if lane == "Per DATA" else inp.Man_spec
        lo, hi = trf_range_ns(spec, inp.system_size)
        lo, hi = min(lo, trf), max(hi, trf)
        late, early = 1.0 + tau, 1.0 - tau
        if trf > 0 and trf * late > hi:
            late = hi / trf
            early = late - 2.0 * tau
        if trf > 0 and trf * early < lo:
            early = lo / trf
            late = min(early + 2.0 * tau, hi / trf)
        pair = memo["edge", lane] = (late, early)
    return pair[0] if role == "late" and lane not in inp.fast_data_edge else pair[1]


def edge_kinks_ns(inp: CalcInputs, lane: str) -> tuple[float, ...]:
    """The row values of `lane` at which `edge_factor` changes form, in ns: between two of them,
    and past the outermost, each edge's t_RF (the row value times its factor) is affine in the
    row value. None at τ = 0, where both edges are the row value.

    With the spec's range (lo, hi) (`trf_range_ns`) they are lo and hi, where the range starts
    to widen; inside the range, (hi − lo)/(2τ), past which the range is narrower than the 2τ
    mismatch, and below that hi/(1 + τ) and lo/(1 − τ), where an edge reaches an end; above
    the range lo/(1 − 2τ) and below it hi/(1 + 2τ), where the far edge reaches the range's
    other end. Checked by test_each_edge_is_affine_in_its_row_between_two_kinks."""
    tau = inp.tRF_tolerance_frac
    if tau <= 0.0:
        return ()
    lo, hi = trf_range_ns(inp.Per_spec if lane == "Per DATA" else inp.Man_spec,
                          inp.system_size)
    narrow = (hi - lo) / (2.0 * tau)
    inside = [k for k in (hi / (1.0 + tau), lo / (1.0 - tau), narrow)
              if lo < k < hi and k <= narrow]
    above = [k for k in ((lo / (1.0 - 2.0 * tau),) if tau < 0.5 else ()) if k > hi]
    below = [k for k in (hi / (1.0 + 2.0 * tau),) if k < lo]
    return tuple(sorted({lo, hi, *inside, *above, *below}))


def tau_adj(inp: CalcInputs, lane: str, role: str) -> float:
    """`edge_factor` over the (1 ± τ) the crossing envelope's coefficients already carry: 1
    unless the spec's range binds the edge, or the lane is in `CalcInputs.fast_data_edge` and
    `role` is "late", which takes the fast edge's factor over (1 + τ)."""
    return _slew_adj(inp, lane, role, role)


def _slew_adj(inp: CalcInputs, lane: str, corner: str, edge: str) -> float:
    """What turns an envelope coefficient of corner `corner` ("late" carries (1 + τ), "early"
    (1 − τ)) into one on an edge of slew factor `edge`: `edge_factor` for `edge` over the
    corner's own (1 ± τ)."""
    tau = inp.tRF_tolerance_frac
    return edge_factor(inp, lane, edge) / (1.0 + tau if corner == "late" else 1.0 - tau)


# The qualifier a crossing symbol carries when its edge's slew factor is not its corner's own:
# a late crossing on a fast edge, (1 − τ), or an early one on a slow edge, (1 + τ).
_EDGE_QUALIFIER = {"late": "slow", "early": "fast"}


def _role_on_edge(corner: str, edge: str) -> str:
    """A crossing symbol's role, `late` / `early`, preceded by the edge's qualifier where the
    edge's slew factor is not the corner's own: in `X_MP,A,rise,slow,early`, A's early
    detection of a rising edge, `slow,early` is the role on a slow edge."""
    return corner if corner == edge else f"{_EDGE_QUALIFIER[edge]},{corner}"


def _worse_case(cases: Sequence[tuple[bool, list[Term]]]) -> tuple[bool, list[Term]]:
    """Of a leg's (edge rising, terms) cases, the one whose terms sum to the less margin; the
    first within 1e-12 ns of the least, so a tie the rounding of the two sums splits is still
    a tie. The edge is the sampling edge on a setup or hold leg and the closing edge on a
    contention leg. Every leg lists first the case of a rising sampling edge on hold, a falling
    one on setup, and a rising closing edge on contention."""
    margins = [sum(t.value_ns for t in terms) for _, terms in cases]
    least = min(margins)
    return next(case for case, m in zip(cases, margins) if m <= least + 1e-12)


def handover_whole_UIs(inp: CalcInputs) -> int:
    """The handover count as a whole number of UIs: a fractional count takes the nearest
    whole one, a tie going up. Every edge rule reads this one count."""
    return math.floor(max(inp.handover_UIs, 0.0) + 0.5)


def pp_shares_clock_edge(inp: CalcInputs, leg: Inequality) -> bool:
    """Whether the two peripherals of P->P leg `leg` detect ONE Manager clock edge.

    PP_hold: A launches on the edge B samples. PP_hold_ho and PP_contention: at zero allocated
    handover UIs, the acquirer's turn-on is referenced to the edge that closes the releaser's
    UI, which is the edge B samples on PP_hold_ho and the edge a conventional release is
    referenced to on PP_contention. An EndDriveEarly release is referenced to the edge that
    opens that UI, so with a releasing EDE peripheral PP_contention's two detections are of
    two edges. PP_setup and PP_setup_ho: B samples one UI after A launches.
    """
    if leg == "PP_hold":
        return True
    if leg == "PP_hold_ho":
        return handover_whole_UIs(inp) == 0
    if leg == "PP_contention":
        return handover_whole_UIs(inp) == 0 and not inp.Per_ede
    return False


def handover_flips_edge(inp: CalcInputs) -> bool:
    """Whether a handover's launching clock edge is of the opposite polarity to the edge its
    UIs are counted from: an odd count of handover UIs, one edge per UI (`handover_whole_UIs`)."""
    return handover_whole_UIs(inp) % 2 == 1


def pp_clock_edges(inp: CalcInputs, env: DeltaTpdEnvelope, leg: Inequality, *,
                   sampling_rising: bool = True, closing_rising: bool = True
                   ) -> tuple[str, str]:
    """The slew factor of the clock edge each clock detection of P->P leg `leg` is on, as
    `edge_factor` roles: (Peripheral A's, Peripheral B's), A being the releaser and B the
    acquirer on PP_contention. `sampling_rising` is the polarity of B's sampling edge on a
    hold leg (`pp_hold_edges`), `closing_rising` that of the edge closing the releaser's UI on
    PP_contention.

    TWO EDGES: each takes its corner's own factor, A late and B early on setup and contention,
    A early and B late on hold.

    ONE EDGE (`pp_shares_clock_edge`): the Manager drives it from one pin, so both detections
    take that edge's one factor, (1 + τ) or (1 − τ), and the leg takes the one that leaves it
    the less margin. Each detection keeps its own crossing coefficient, set by its receiver's
    supply, ground shift and threshold, so the clock lane enters the margin as
    Σ ± c_raw · factor · t_RF,CLK, c_raw being a coefficient without its (1 ± τ); the larger
    factor (`late`) is the worse when that sum of signed coefficients is negative or zero, and
    the smaller (`early`) when it is positive. The sign is set by the thresholds, α and the
    supplies, not by the slews or τ.
    """
    if leg.startswith("PP_setup"):
        return "late", "early"
    own = ("early", "late") if leg.startswith("PP_hold") else ("late", "early")
    if not pp_shares_clock_edge(inp, leg):
        return own
    tau = inp.tRF_tolerance_frac
    if leg.startswith("PP_hold") and sampling_rising:
        # A's early detection is a credit, B's late one a charge.
        net = env.sigma_pm_hold_rf_clk / (1.0 - tau) - env.mp_late_rise_b / (1.0 + tau)
    elif leg.startswith("PP_hold"):
        net = env.mp_early_fall / (1.0 - tau) - env.mp_late_fall_b / (1.0 + tau)
    elif closing_rising:
        # The releaser's late detection is a charge, the acquirer's early one a credit.
        net = env.sigma_pm_hold_rf_clk_b / (1.0 - tau) - env.mp_late_rise / (1.0 + tau)
    else:
        net = env.mp_early_fall_b / (1.0 - tau) - env.mp_late_fall / (1.0 + tau)
    edge = "late" if net <= 0.0 else "early"
    return edge, edge


def pp_hold_edges(inp: CalcInputs, leg: Inequality) -> tuple[tuple[bool, bool], ...]:
    """The (A's launching edge rising, B's sampling edge rising) cases P->P hold leg `leg` is
    evaluated at; the leg reports the one that leaves it the less margin.

    B samples on either polarity, the rising case first. PP_hold: A launches on the edge B
    samples. PP_hold_ho: A launches on the edge its handover count gives after it
    (`handover_flips_edge`).
    """
    flip = leg == "PP_hold_ho" and handover_flips_edge(inp)
    return ((not flip, True), (flip, False))


def anchor_frac(inp: CalcInputs, side: str, rising: bool = True) -> float:
    """The ramp before a launch's anchor, in units of t_RF: from the start of the ramp to its
    V_OL crossing on a `rising` edge and its V_OH crossing on a falling one, which is where
    t_DD and t_ZD end (`per_tdd_offset_frac`). 1/3 on a linear ramp and 0.161 on an
    exponential one, at either polarity, at the default V_OL / V_OH. The same fraction of the
    Manager's clock ramp places the Manager's clock reference."""
    shape = inp.Man_shape if side == "Man" else inp.Per_shape
    return per_tdd_offset_frac(shape, V_OH_frac=inp.V_OH_min_frac, V_OL_frac=inp.V_OL_max_frac,
                               rising=rising)


def launch_row_ns(inp: CalcInputs, side: str, which: str, corner: str) -> float:
    """The row value a launch reads: `which` is "DD" or "ZD", `corner` "max" or "min". A
    Manager's is taken after the launch mode (`_man_tdd_range`, `_man_tzd_ns`)."""
    if side == "Per":
        if which == "DD":
            return inp.Per_t_DD_max_ns if corner == "max" else inp.Per_t_DD_min_ns
        return inp.Per_t_ZD_ns
    if which == "DD":
        return _man_tdd_range(inp)[1 if corner == "max" else 0]
    return _man_tzd_ns(inp)


def _sw_anchor_slews(inp: CalcInputs, side: str, corner: str) -> tuple[float, float]:
    """(k, s), the clock and data slews a SoundWire anchor correction is evaluated at: the
    edges' own (`edge_factor`). The data edge is the slow-edge role on the max corner, which
    setup, the keeper and an EndDriveEarly release's launch on a contention leg read (the fast
    edge for a lane in `CalcInputs.fast_data_edge`), and early on the min corner, which hold
    and a turn-on read. The Manager's clock is always its
    late edge, the one the Manager's reference sits on when the launch is on the edge a
    Peripheral detects late; a launch on a later edge (N_HO >= 1) is priced at the same
    clock slew, which is the model's one clock slew for an anchor."""
    lane = "Man DATA" if side == "Man" else "Per DATA"
    data = lane_ns(inp, lane) * edge_factor(inp, lane, "late" if corner == "max" else "early")
    clk = inp.tRF_Man_CLK_ns * edge_factor(inp, "Man CLK", "late")
    return clk, data


def sw_anchor_ns(inp: CalcInputs, side: str, corner: str, *, rising: bool = True,
                 clk_rising: bool = True) -> float:
    """A SoundWire side's anchor correction at the operating slews (`sw13_anchor_correction`,
    at `_sw_anchor_slews` and the bus's system size), for a launched data edge of polarity
    `rising` from a Manager clock edge of polarity `clk_rising`; 0 on a SWI3S side. A
    Peripheral's correction has no clock part, so `clk_rising` does not move it. Where
    V_OH + V_OL = 1 the two polarities place a ramp alike, and the rising placement is taken
    for both."""
    spec = inp.Man_spec if side == "Man" else inp.Per_spec
    if not is_soundwire(spec):
        return 0.0
    if side == "Per":
        clk_rising = True
    if math.isclose(inp.V_OH_min_frac + inp.V_OL_max_frac, 1.0, rel_tol=0.0, abs_tol=1e-12):
        rising = clk_rising = True
    memo = _memo(inp)
    key = ("anchor", side, corner, rising, clk_rising)
    corr = memo.get(key)
    if corr is None:
        clk, data = _sw_anchor_slews(inp, side, corner)
        corr = memo[key] = sw13_anchor_correction(
            spec, side,  # type: ignore[arg-type]
            corner=corner,  # type: ignore[arg-type]
            t_rf_clk_ns=clk, t_rf_data_ns=data,
            system_size=inp.system_size,
            shape=inp.Man_shape if side == "Man" else inp.Per_shape,
            V_OH_frac=inp.V_OH_min_frac, V_OL_frac=inp.V_OL_max_frac,
            rising=rising, clk_rising=clk_rising)
    return corr


def per_anchor_offsets(inp: CalcInputs, corner: str) -> tuple[float, float]:
    """A Peripheral's anchor correction (`sw_anchor_ns`) at `corner` for a (rising, falling)
    data edge: the offsets the P->M envelopes select their case with (`data_offset_ns`)."""
    return (sw_anchor_ns(inp, "Per", corner, rising=True),
            sw_anchor_ns(inp, "Per", corner, rising=False))


def _launch_anchor_ns(inp: CalcInputs, side: str, which: str, corner: str, *,
                      rising: bool = True, clk_rising: bool = True) -> float:
    """When a launch of data polarity `rising`, from a Manager clock edge of polarity
    `clk_rising`, reaches its anchor, after the side's clock reference: the row value, plus a
    SoundWire side's anchor correction (`sw_anchor_ns`)."""
    return launch_row_ns(inp, side, which, corner) + sw_anchor_ns(
        inp, side, corner, rising=rising, clk_rising=clk_rising)


def anchor_symbol(inp: CalcInputs, side: str, which: str, corner: str, *, rising: bool,
                  clk_rising: bool = True) -> str:
    """The symbol of a SoundWire launch's anchor correction (`sw_anchor_ns`) for row `which`
    ("DD" or "ZD") at `corner`, on a data edge of polarity `rising` from a Manager clock edge
    of polarity `clk_rising`, so that one symbol has one value at a corner, as `to_voh_symbol`
    names a ramp's edge. It always names the edges it is placed on, whatever their
    corrections: a Peripheral's its data edge, `,anchor,rise` / `,anchor,fall`, and a
    Manager's its clock edge then its data edge with the case letters of `X_PM,rr,early`:
    `,anchor,rr`, `,anchor,rf`, `,anchor,fr`, `,anchor,ff`, `r` rising and `f` falling. Then
    `,fast` on the max corner where the side's DATA lane is in `CalcInputs.fast_data_edge`,
    whose correction is priced on the fast data edge (`_sw_anchor_slews`), as `to_voh_symbol`
    names a ramp priced there."""
    sym = f"{side}_t_{which},anchor"
    lane = "Man DATA" if side == "Man" else "Per DATA"
    fast = ",fast" if corner == "max" and lane in inp.fast_data_edge else ""
    if side == "Man":
        return f"{sym},{'r' if clk_rising else 'f'}{'r' if rising else 'f'}{fast}"
    return f"{sym},{'rise' if rising else 'fall'}{fast}"


def turn_on_rising(inp: CalcInputs, side: str, *, clk_rising: bool = True) -> bool:
    """The data polarity of a contention leg's turn-on by `side`, from a Manager clock edge of
    polarity `clk_rising`: the one whose anchor is the earlier (`sw_anchor_ns` at the min
    corner), which leaves the less margin, rising on a tie. The data's polarity follows the
    bit pattern, so the leg takes the edge that leaves it the less margin."""
    rise, fall = (sw_anchor_ns(inp, side, "min", rising=r, clk_rising=clk_rising)
                  for r in (True, False))
    return math.isclose(rise, fall, rel_tol=0.0, abs_tol=1e-12) or rise < fall


def _launch_terms(inp: CalcInputs, side: str, which: str, corner: str, *, credit: bool,
                  note: str = "", rising: bool = True, clk_rising: bool = True) -> list[Term]:
    """A launch as displayed: its row, and on a SoundWire side the anchor correction that
    moves the equivalent to the operating slews, for a data edge of polarity `rising` from a
    Manager clock edge of polarity `clk_rising`. Together they are `_launch_anchor_ns`. A
    correction of zero (the operating slews are the equivalents' own) is no term. The
    `,anchor` term names a corner- and polarity-dependent quantity: on the max corner (setup,
    keeper, and an EndDriveEarly release's launch on a contention leg, `_ede_release_terms`)
    it corrects the t_OV-based equivalent, on the min corner (hold, handover hold, a
    contention leg's turn-on) the t_ZD-based one, each at its launch's polarities. Where its
    edges' corrections differ it names them (`anchor_symbol`): a Peripheral's data edge,
    `,anchor,rise` / `,anchor,fall`, a Manager's clock and data edges, `,anchor,rf` and so on;
    its note names the corner and the edges.

    A Manager's t_DD or t_ZD row carries its launch-mode placement note
    (`_man_clocked_grid_ns`) wherever it is shown, unless `note` is given."""
    sign = 1.0 if credit else -1.0
    row = launch_row_ns(inp, side, which, corner)
    if side == "Man" and not note and which in ("DD", "ZD"):
        note = (_man_clocked_grid_ns(inp, inp.Man_t_DD_max_ns, inp.Man_t_DD_floor_ns)[1]
                if which == "DD" else _man_tzd_parts(inp)[1])
    out = [Term(f"{side}_t_{which}", sign * row, op="+" if credit else "-", note=note)]
    spec = inp.Man_spec if side == "Man" else inp.Per_spec
    if is_soundwire(spec):
        corr = sw_anchor_ns(inp, side, corner, rising=rising, clk_rising=clk_rising)
        if abs(corr) > 1e-12:
            v = sign * corr
            out.append(Term(anchor_symbol(inp, side, which, corner, rising=rising,
                                          clk_rising=clk_rising),
                            v, op="+" if v >= 0 else "-",
                            note=_sw_anchor_note(inp, side, corner, corr, rising=rising,
                                                 clk_rising=clk_rising)))
    return out


def _sw_anchor_note(inp: CalcInputs, side: str, corner: str, corr: float, *,
                    rising: bool = True, clk_rising: bool = True) -> str:
    """Provenance for a SoundWire anchor correction `corr` (`sw_anchor_ns`) of a data edge of
    polarity `rising` from a Manager clock edge of polarity `clk_rising`: what the equivalent
    assumed and where the anchor is at the operating slews."""
    shape = inp.Man_shape if side == "Man" else inp.Per_shape
    z, _, _, r = sw13_ramp_fracs(shape, V_OH_frac=inp.V_OH_min_frac,
                                 V_OL_frac=inp.V_OL_max_frac, rising=rising)
    _, p, q, _ = sw13_ramp_fracs(shape, V_OH_frac=inp.V_OH_min_frac,
                                 V_OL_frac=inp.V_OL_max_frac, rising=clk_rising)
    rs = "s" if math.isclose(r, 1.0, abs_tol=1e-9) else f"{r:.3g}·s"
    tp = "65" if clk_rising else "35"
    what = {("Per", "max"): f"t_OV ends at 80 % of the data ramp: the 20 % point is t_OV − {rs}",
            ("Per", "min"): f"t_ZD_Data ends at low impedance: the 20 % point is "
                            f"t_ZD + {z:.3g}·s",
            ("Man", "max"): f"t_OV runs from the clock's {tp} % crossing to 80 % of the data "
                            f"ramp: the 20 % point is t_OV + {p:.3g}·k − {rs}",
            ("Man", "min"): f"t_ZD_Data runs from the clock's 50 % crossing to low impedance: "
                            f"the 20 % point is t_ZD + {q:.3g}·k + {z:.3g}·s"}[(side, corner)]
    spec = inp.Man_spec if side == "Man" else inp.Per_spec
    k0, s0 = sw13_equivalent_slews(spec, side,  # type: ignore[arg-type]
                                   corner=corner,  # type: ignore[arg-type]
                                   system_size=inp.system_size)
    k, s = _sw_anchor_slews(inp, side, corner)
    lane = "Man DATA" if side == "Man" else "Per DATA"
    edge = "slow" if corner == "max" and lane not in inp.fast_data_edge else "fast"
    data_pol = "rising" if rising else "falling"

    def slews(kk: float, ss: float) -> str:
        return f"k = {kk:.2f}, s = {ss:.2f} ns" if side == "Man" else f"s = {ss:.2f} ns"

    edges = (f"the late {'rising' if clk_rising else 'falling'} clock edge and the {edge} "
             f"{data_pol} data edge" if side == "Man" else f"the {edge} {data_pol} data edge")
    sym = anchor_symbol(inp, side, "DD", corner, rising=rising,
                        clk_rising=clk_rising).removesuffix(",fast")
    if side == "Man" and sym.count(",") == 2:
        edges += (f"; in `,{sym.rsplit(',', 1)[1]}` the first letter is the clock edge and the "
                  "second the data edge, r rising and f falling")
    return (f"SoundWire: {what}; the equivalent is that at the slews it is derived at "
            f"({slews(k0, s0)}), so at {slews(k, s)} ({edges}) the anchor moves by "
            f"{corr:+.2f} ns")


def _check_inputs(inp: CalcInputs) -> None:
    """Raise ValueError, naming the field, for an input no leg can be evaluated at: a number
    that is not finite, a per-edge slew tolerance τ outside [0, 1) (a fast edge is (1 − τ)
    of its row), a supply tolerance δ outside [0, 1) (a low supply is (1 − δ) of nominal),
    a clock rate, trace velocity or nominal supply that is not positive, a duty
    floor outside (0, 0.5] (the shorter clock phase's share of the period), a negative
    handover UI count, or a negative slew, bus length, trace mismatch, noise fraction or
    keeper response (`_NON_NEGATIVE_INPUTS`)."""
    for f in dataclasses.fields(inp):
        v = getattr(inp, f.name)
        if isinstance(v, (int, float)) and not isinstance(v, bool) and not math.isfinite(v):
            raise ValueError(f"{f.name} is {v}; every timing input must be a finite number")
    tau = inp.tRF_tolerance_frac
    if not 0.0 <= tau < 1.0:
        raise ValueError(f"the per-edge slew tolerance τ is {100 * tau:g} %; it must be at "
                         f"least 0 % and below 100 %, where a fast edge's t_RF reaches zero")
    delta = inp.V_SEOS_tol_frac
    if not 0.0 <= delta < 1.0:
        raise ValueError(f"the supply tolerance δ V_SEOS_tol_frac is {100 * delta:g} %; it "
                         f"must be at least 0 % and below 100 %, where a low supply reaches "
                         f"zero")
    for name, what in (("F_CLK_target_MHz", "the clock rate"),
                       ("vPCB_cm_per_ns", "the trace velocity"),
                       ("V_SEOS_nom_V", "the nominal supply")):
        if getattr(inp, name) <= 0.0:
            raise ValueError(f"{what} {name} is {getattr(inp, name):g}; it must be positive")
    if not 0.0 < inp.duty_min <= 0.5:
        raise ValueError(f"the duty floor duty_min is {inp.duty_min:g}; the shorter clock "
                         "phase is more than 0 and at most half the period")
    if inp.handover_UIs < 0.0:
        raise ValueError(f"the handover UI count handover_UIs is {inp.handover_UIs:g}; it "
                         "must be 0 or more")
    for name, what in _NON_NEGATIVE_INPUTS:
        if getattr(inp, name) < 0.0:
            raise ValueError(f"{what} {name} is {getattr(inp, name):g}; it must be 0 or more")


# The physical quantities `_check_inputs` refuses below zero: (field, what it is).
_NON_NEGATIVE_INPUTS = (
    ("tRF_Man_CLK_ns", "the Manager CLK slew"),
    ("tRF_Man_DATA_ns", "the Manager DATA slew"),
    ("tRF_Per_DATA_ns", "the Peripheral DATA slew"),
    ("bus_length_cm", "the bus length"),
    ("vPCB_err_frac", "the trace mismatch fraction"),
    ("V_noise_pp_frac", "the noise fraction α"),
    ("t_keeper_ns", "the keeper response"),
)


def compute(inp: CalcInputs) -> CalcResults:
    """Evaluate every leg in `INEQUALITIES` at the supplied corner, each at the assignment of
    a V_SEOS corner to each device that leaves it the least margin, and, where
    `CalcInputs.fast_edge_lanes` names a lane, at the slow or fast data-edge set that leaves it
    the least.

    Raises ValueError for an input the model cannot evaluate (`_check_inputs`, and a
    threshold at or beyond a transmitter's rail).

    ONE SUPPLY PER DEVICE. A device's V_SEOS sets both its output rail and its input
    thresholds, so on one leg a device transmitting and receiving does both at the same
    supply. Each leg is evaluated at every assignment (`CalcInputs.supply_corners`); its
    breakdown records the one it is reported at, and the Δ / Σ values reported are the ones
    of the legs they belong to.
    """
    _check_inputs(inp)
    # No leg reads the slow-edge role of two DATA lanes, so every lane `fast_edge_lanes`
    # names is evaluated slow together and fast together.
    lanes = inp.fast_edge_lanes()
    edges = [frozenset(), frozenset(lanes)] if lanes else [frozenset()]
    runs = [_compute_at(replace(inp, fast_data_edge=e), s)
            for e in edges for s in inp.supply_corners()]
    pick: dict[Inequality, CalcResults] = {}
    breakdowns: dict[Inequality, InequalityBreakdown] = {}
    # A tie between assignments, margins within 1e-12 ns as in `_worse_case`, keeps a setup,
    # hold or contention leg's first case (`_worse_case`), and otherwise the first assignment.
    def _second_case(r: CalcResults, leg: Inequality) -> bool:
        b = r.breakdowns[leg]
        return ((b.sampling_rising is not None and b.sampling_rising == b.is_setup)
                or b.closing_rising is False)

    for leg in INEQUALITIES:
        least = min(r.breakdowns[leg].margin_ns for r in runs)
        tied = [r for r in runs if r.breakdowns[leg].margin_ns <= least + 1e-12]
        r = min(tied, key=lambda r: _second_case(r, leg))
        pick[leg] = r
        breakdowns[leg] = r.breakdowns[leg]
    return replace(runs[0], breakdowns=breakdowns,
                   delta_cross_MP_setup_ns=pick["MP_setup"].delta_cross_MP_setup_ns,
                   delta_cross_MP_hold_ns=pick["MP_hold"].delta_cross_MP_hold_ns,
                   sigma_pm_setup_ns=pick["PM_setup"].sigma_pm_setup_ns,
                   sigma_pm_hold_ns=pick["PM_hold"].sigma_pm_hold_ns)


def _compute_at(inp: CalcInputs, supplies: Supplies) -> CalcResults:
    """Evaluate every leg in `INEQUALITIES` at the supplied corner and at `supplies`, one
    V_SEOS per device.

    Returns t_cross values (Δ_cross,MP, Σ_cross,PM,setup, Σ_cross,PM,hold)
    and a per-inequality breakdown with terms in ns and signs baked in.
    """
    env = inp.envelope(supplies)
    t_PD = inp.t_PD_ns()
    t_PDmis = inp.t_PD_mis_ns()
    UI = inp.UI_ns()

    # Δ_cross,MP differs between setup and hold once the Manager's two lanes
    # carry different t_RF: the LATE crossing sits on the data for setup and on
    # the clock for hold. Identical when the lanes match, which is the default.
    # EACH EDGE'S SLEW FACTOR IS KEPT INSIDE ITS RANGE (`edge_factor`). The envelope's
    # coefficients carry (1 ± τ); a lane value scaled by `tau_adj` for the role it plays
    # corrects them where the range binds, and leaves them alone elsewhere.
    def _t(lane: str, role: str) -> float:
        return lane_ns(inp, lane) * tau_adj(inp, lane, role)

    def _roled(pair):
        (late_lane, late_c), (early_lane, early_c) = pair
        return ((late_lane, late_c * tau_adj(inp, late_lane, "late")),
                (early_lane, early_c * tau_adj(inp, early_lane, "early")))

    # EVERY SETUP AND HOLD LEG IS EVALUATED AT BOTH POLARITIES OF ITS SAMPLING EDGE and
    # reports the worse (`_worse_case`). The envelopes below are keyed by the polarity their
    # case reads: the Peripheral's or B's sampling edge for Delta_cross,MP and its P->P
    # construction, `(direction, sampling_rising)`, and the Peripheral's launching edge for
    # Sigma_cross,PM, `launch_rising`. A Manager's own clock enters no crossing, so on M->P
    # the polarity is the sampling edge's alone, and on P->M it is the launching edge's:
    # the sampling edge's on hold, the other on setup, and on PM_hold_ho the one its handover
    # count gives after the sampling edge (`handover_flips_edge`).
    #
    # Each crossing is at its polarity's corner: a rising one at V_IH, a falling one at V_IL,
    # and a late crossing pairs with an early one of the other polarity under one ground
    # shift (`DeltaTpdEnvelope.delta_cross_MP_lanes`). Its symbol names its edge, `rise` or
    # `fall` before the role: `X_MP,rise,late` with `X_MP,fall,early`, and `X_MP,fall,late`
    # with `X_MP,rise,early`.
    _ROLES: dict[str, tuple[str, str]] = {"setup": ("early", "late"), "hold": ("late", "early")}
    _DIRECTIONS: tuple[MPDirection, ...] = ("setup", "hold")

    def _delta_lanes(lanes, direction: MPDirection, rising: bool) -> tuple[CrossLane, ...]:
        (late_lane, late_c), (early_lane, early_c) = _roled(lanes)
        own = (direction == "hold") == rising        # late rising, early falling
        return (("rise,late" if own else "fall,late", late_lane, late_c, "-"),
                ("fall,early" if own else "rise,early", early_lane, early_c, "+"))

    delta_MP: dict[tuple[str, bool], float] = {}
    delta_PP: dict[tuple[str, bool], float] = {}
    DELTA_MP: dict[tuple[str, bool], tuple[CrossLane, ...]] = {}
    # THE SAME CONSTRUCTION ON P->P, BUT THE DATA IS A PERIPHERAL'S. B's clock detection and
    # its detection of the incoming data are one die at nearly one instant, so their
    # difference is the Delta_cross,MP construction -- but the DATA reaching B was driven by
    # peripheral A, not by the Manager, so the data lane is `Per DATA` and its coefficient
    # takes the Peripheral's ramp shape. The clock lane keeps the Manager's. Each lane is at its
    # corner's own slew factor here; a hold leg whose clock detections are of one edge puts
    # B's clock lane on that edge's factor (the P->P hold legs below).
    DELTA_PP: dict[tuple[str, bool], tuple[CrossLane, ...]] = {}
    for _dir in _DIRECTIONS:
        _clk_role, _data_role = _ROLES[_dir]
        for _rising in (True, False):
            _k = (_dir, _rising)
            delta_MP[_k] = env.delta_cross_MP_ns(
                _t("Man CLK", _clk_role), _t("Man DATA", _data_role),
                direction=_dir, sampling_rising=_rising)
            delta_PP[_k] = env.delta_cross_PP_ns(
                _t("Man CLK", _clk_role), _t("Per DATA", _data_role),
                direction=_dir, sampling_rising=_rising)
            DELTA_MP[_k] = _delta_lanes(env.delta_cross_MP_lanes(
                direction=_dir, sampling_rising=_rising), _dir, _rising)
            DELTA_PP[_k] = _delta_lanes(env.delta_cross_PP_lanes(
                direction=_dir, sampling_rising=_rising), _dir, _rising)

    # ROLE, NOT LANE. These read `late`/`early` like Delta's legs rather than `clk`/`data`,
    # because the lane is already named by the t_RF the term is multiplied BY. Setup is eroded
    # by a LATE crossing and hold by an EARLY one, the same convention as Delta_cross,MP.
    # THE SAME QUANTITIES, LANE BY LANE, for display. Each envelope is a sum of
    # (dimensionless coefficient) x (that lane's t_RF); the accessors return the branch
    # the max/min actually selects, so the products below sum to the ns values above
    # exactly. Asserted at the end of this block rather than trusted.
    #
    # A SoundWire Peripheral's launch reaches its anchor at a time that depends on its data
    # edge's polarity (`sw_anchor_ns`), so each envelope selects its case with the anchor of
    # that case's data edge added (`data_offset_ns`), and the launch is placed on that edge
    # (`SIGMA_SETUP_DATA_RISING`, `SIGMA_HOLD_DATA_RISING`).
    sigma_setup: dict[bool, float] = {}
    sigma_hold: dict[bool, float] = {}
    SIGMA_SETUP: dict[bool, tuple[CrossLane, ...]] = {}
    SIGMA_HOLD: dict[bool, tuple[CrossLane, ...]] = {}
    SIGMA_HOLD_CLK_PAIR: dict[bool, str] = {}
    SIGMA_SETUP_DATA_RISING: dict[bool, bool] = {}
    SIGMA_HOLD_DATA_RISING: dict[bool, bool] = {}
    _per_anchor_max = per_anchor_offsets(inp, "max")
    _per_anchor_min = per_anchor_offsets(inp, "min")
    _adj_clk_late, _adj_clk_early = (tau_adj(inp, "Man CLK", r) for r in ("late", "early"))
    _adj_per_late, _adj_per_early = (tau_adj(inp, "Per DATA", r) for r in ("late", "early"))
    for _rising in (True, False):
        _late_k, _late_s = _t("Man CLK", "late"), _t("Per DATA", "late")
        _early_k, _early_s = _t("Man CLK", "early"), _t("Per DATA", "early")
        sigma_setup[_rising] = env.sigma_pm_setup_ns(_late_k, _late_s, launch_rising=_rising,
                                                     data_offset_ns=_per_anchor_max)
        # Sigma_cross,PM,setup's data crosses on either polarity
        # (`delta_tpd.SIGMA_PM_SETUP_CASES`) and its two addends are cornered jointly, so both
        # lanes carry the case's letters, the clock edge then the data edge: `X_PM,rr,late`,
        # `X_PM,rf,late`, `X_PM,fr,late`, `X_PM,ff,late`.
        _s_fields = env.sigma_pm_setup_fields(_late_k, _late_s, launch_rising=_rising,
                                              data_offset_ns=_per_anchor_max)
        _s_clk, _s_data = (getattr(env, f) for f in _s_fields)
        _s_data_rising = "_fall_" not in _s_fields[1]
        SIGMA_SETUP_DATA_RISING[_rising] = _s_data_rising
        _s_case = f"{'r' if _rising else 'f'}{'r' if _s_data_rising else 'f'},late"
        SIGMA_SETUP[_rising] = ((_s_case, "Man CLK", _s_clk * _adj_clk_late, "-"),
                                (_s_case, "Per DATA", _s_data * _adj_per_late, "-"))
        # Sigma_cross,PM,hold's two cases carry different coefficients on both lanes, so the
        # same-polarity case is named for itself on both lanes, `X_PM,rr,early` on a rising
        # launching edge and `X_PM,ff,early` on a falling one. The other case's clock lane is
        # the lone early detection, named for its direction (`clock_detection_symbol`), and its
        # data lane the lone data crossing: `X_MP,rise,early` with the falling data crossing
        # `X_PM,fall,early`, and `X_MP,fall,early` with the rising data crossing
        # `X_PM,rise,early` (`SIGMA_HOLD_CLK_PAIR`).
        _h_pair = env.sigma_pm_hold_lanes(_early_k, _early_s, launch_rising=_rising,
                                          data_offset_ns=_per_anchor_min)
        sigma_hold[_rising] = _h_pair[0] * _early_k + _h_pair[1] * _early_s
        _same = ((env.sigma_pm_hold_rr_clk, env.sigma_pm_hold_rr_data) if _rising
                 else (env.sigma_pm_hold_ff_clk, env.sigma_pm_hold_ff_data))
        SIGMA_HOLD_DATA_RISING[_rising] = (_h_pair == _same) == _rising
        if _h_pair == _same:
            _h_roles = ("rr,early",) * 2 if _rising else ("ff,early",) * 2
            SIGMA_HOLD_CLK_PAIR[_rising] = "X_PM"
        else:
            _h_roles = (("rise,early", "fall,early") if _rising
                        else ("fall,early", "rise,early"))
            SIGMA_HOLD_CLK_PAIR[_rising] = "X_MP"
        SIGMA_HOLD[_rising] = ((_h_roles[0], "Man CLK", _h_pair[0] * _adj_clk_early, "+"),
                               (_h_roles[1], "Per DATA", _h_pair[1] * _adj_per_early, "+"))
    _checks: list[tuple[tuple[CrossLane, ...], float, float]] = [
        *((DELTA_MP[k], delta_MP[k], -1.0) for k in DELTA_MP),
        *((DELTA_PP[k], delta_PP[k], -1.0) for k in DELTA_PP),
        *((SIGMA_SETUP[r], sigma_setup[r], -1.0) for r in SIGMA_SETUP),
        *((SIGMA_HOLD[r], sigma_hold[r], +1.0) for r in SIGMA_HOLD)]
    for _lanes, _ns, _sgn in _checks:
        _sum = _sgn * sum((-1.0 if o == "-" else 1.0) * c * lane_ns(inp, ln)
                          for _, ln, c, o in _lanes)
        # An internal invariant, checked under `python -O` too, to the precision of the sum.
        if not math.isclose(_sum, _ns, rel_tol=1e-12, abs_tol=1e-9):
            raise AssertionError(
                f"lane decomposition {_sum} != envelope {_ns}; a displayed "
                f"coefficient x t_RF must equal the ns value, not approximate it")

    # EVERY LAUNCH IS PLACED AT ITS ANCHOR, the 20 % point its t_DD / t_ZD is measured to,
    # which the reading in force bounds at every t_RF of the ramp setting, and a Manager's
    # timing at its clock reference, its clock's 20 % point. The crossings count from the
    # 20 % point of the edge they are on, so no part of a ramp before it enters a leg. A
    # SoundWire side's anchor depends on the polarities of the launched data edge and of the
    # Manager clock edge it is launched from (`sw_anchor_ns`), so each case places its launch
    # at its own edges.

    # One Manager turn-on, read by the handover setup, hold and contention legs.
    man_zd_ns = _man_tzd_ns(inp)

    breakdowns: dict[Inequality, InequalityBreakdown] = {}

    # ----- MP setup: UI ≥ Man_t_DD,max + Per_t_IS,max + t_PD,mis + Δ_cross,MP --------
    # (Δ_cross,MP from the Manager's clock reference and from the data's anchor.)
    # Both sampling polarities; a tie keeps the falling one.
    # The data edge and the launching clock edge are of the other polarity to the sampling
    # edge.
    _mp_setup_rising, mp_setup_terms = _worse_case([(_r, [
        Term("UI", +UI, op="+"),
        *_launch_terms(inp, "Man", "DD", "max", credit=False, rising=not _r,
                       clk_rising=not _r),
        Term("t_PD,mis", -t_PDmis, op="-"),
        *_cross_terms(inp, "X_MP", DELTA_MP["setup", _r]),
        Term("Per_t_IS", -inp.Per_t_IS_max_ns, op="-"),
    ]) for _r in (False, True)])
    mp_setup_required = (
        _launch_anchor_ns(inp, "Man", "DD", "max", rising=not _mp_setup_rising,
                          clk_rising=not _mp_setup_rising)
        + inp.Per_t_IS_max_ns + t_PDmis
        + delta_MP["setup", _mp_setup_rising]
    )
    breakdowns["MP_setup"] = InequalityBreakdown(
        name="MP_setup",
        terms=mp_setup_terms,
        required_ns=mp_setup_required,
        margin_ns=UI - mp_setup_required,
        # A clocked Man_t_DD is a fraction of the UI, so it shrinks with the rate.
        **_fmax_fields(inp, _rate_windows(
            inp, margin_ns=UI - mp_setup_required,
            ui_part_ns=UI - _man_clocked_ui_part_ns(inp, _man_tdd_range(inp)[1]),
            clocked=[("DD", -1.0, _man_tdd_range(inp)[1])])),
        is_setup=True,
        sampling_rising=_mp_setup_rising,
    )

    # ----- MP setup, handover UI: as MP_setup but Man_t_DD,max → Man_t_ZD,max -----
    _mp_setup_ho_rising, mp_setup_ho_terms = _worse_case([(_r, [
        Term("UI", +UI, op="+"),
        # The launch mode may have snapped the value onto a placeable grid point; the note
        # `_launch_terms` attaches says so.
        *_launch_terms(inp, "Man", "ZD", "max", credit=False, rising=not _r,
                       clk_rising=not _r),
        Term("t_PD,mis", -t_PDmis, op="-"),
        *_cross_terms(inp, "X_MP", DELTA_MP["setup", _r]),
        Term("Per_t_IS", -inp.Per_t_IS_max_ns, op="-"),
    ]) for _r in (False, True)])
    mp_setup_ho_required = (
        _launch_anchor_ns(inp, "Man", "ZD", "max", rising=not _mp_setup_ho_rising,
                          clk_rising=not _mp_setup_ho_rising)
        + inp.Per_t_IS_max_ns + t_PDmis
        + delta_MP["setup", _mp_setup_ho_rising]
    )
    breakdowns["MP_setup_ho"] = InequalityBreakdown(
        name="MP_setup_ho",
        terms=mp_setup_ho_terms,
        required_ns=mp_setup_ho_required,
        margin_ns=UI - mp_setup_ho_required,
        **_fmax_fields(inp, _rate_windows(
            inp, margin_ns=UI - mp_setup_ho_required,
            ui_part_ns=UI - _man_clocked_ui_part_ns(inp, man_zd_ns),
            clocked=[("ZD", -1.0, man_zd_ns)])),
        is_setup=True,
        sampling_rising=_mp_setup_ho_rising,
    )

    # ----- MP hold: Man_t_DD,min ≥ Per_t_IH,max + t_PD,mis + Δ_cross,MP -----
    # Both sampling polarities; a tie keeps the rising one. The Manager launches on the
    # sampling edge, its data edge of the other polarity.
    _mp_hold_rising, mp_hold_terms = _worse_case([(_r, [
        *_launch_terms(inp, "Man", "DD", "min", credit=True, rising=not _r, clk_rising=_r),
        Term("t_PD,mis", -t_PDmis, op="-"),
        *_cross_terms(inp, "X_MP", DELTA_MP["hold", _r]),
        Term("Per_t_IH", -inp.Per_t_IH_max_ns, op="-"),
    ]) for _r in (True, False)])
    mp_hold_margin = sum(t.value_ns for t in mp_hold_terms)
    breakdowns["MP_hold"] = InequalityBreakdown(
        name="MP_hold",
        terms=mp_hold_terms,
        required_ns=0.0,
        margin_ns=mp_hold_margin,
        # A clocked Man_t_DD,min is a fraction of the UI, so the hold it funds shrinks
        # with the rate.
        **_fmax_fields(inp, _rate_windows(
            inp, margin_ns=mp_hold_margin,
            ui_part_ns=_man_clocked_ui_part_ns(inp, _man_tdd_range(inp)[0]),
            clocked=[("DD", +1.0, _man_tdd_range(inp)[0])])),
        is_setup=False,
        sampling_rising=_mp_hold_rising,
    )

    # ----- MP hold on a t_ZD launch: the fourth of the 2x2 ---------------------
    #
    # t_DD and t_ZD are the two ways the Manager launches a data edge, and the
    # peripheral's hold requirement applies to BOTH -- it is one requirement at the
    # receiver, which cannot tell which path produced the edge.  So this leg is the
    # t_ZD quarter of (launch method) x (setup, hold).
    #
    # Concretely: the bit sampled at the UI-opening edge must stay valid for
    # Per_t_IH,max past that edge, and the Manager's turn-on out of high-Z is what
    # ends it.  So t_ZD,min funds hold exactly as t_DD,min does on an ordinary UI.
    #
    # NOTHING ELSE COVERS IT.  Non-contention bounds driver OVERLAP: once the
    # releaser is off, the keeper holds the level and the acquirer may drive a
    # DIFFERENT level immediately with no driver conflict at all.  The keeper legs
    # bound the RELEASING side -- whether the keeper followed the level before the release.
    # Neither says the sampled bit survives its hold window.
    #
    # Each allocated handover UI puts the turn-on a whole UI further from the sampling edge,
    # a credit of N_HO·UI on the leg, so a zero-UI handover is the one that binds it.
    #
    # Man_t_ZD is ONE field and the auto-worst solver drives it to whichever corner
    # hurts each inequality -- its max for setup, its min here.  A SoundWire side's anchor
    # correction also differs by corner, so this reads the min corner.
    #
    # The Manager's turn-on enters no crossing; it reads both sampling polarities as MP_hold
    # does, its data edge of the other polarity to the sampling edge and its launching clock
    # edge the one its handover count gives (`handover_flips_edge`), which places a SoundWire
    # Manager's anchor.
    _mp_hold_ho_rising, mp_hold_ho_terms = _worse_case([(_r, [
        *_handover_ui_terms(inp, UI),
        *_launch_terms(inp, "Man", "ZD", "min", credit=True, rising=not _r,
                       clk_rising=_r != handover_flips_edge(inp)),
        Term("t_PD,mis", -t_PDmis, op="-"),
        *_cross_terms(inp, "X_MP", DELTA_MP["hold", _r]),
        Term("Per_t_IH", -inp.Per_t_IH_max_ns, op="-"),
    ]) for _r in (True, False)])
    mp_hold_ho_margin = sum(t.value_ns for t in mp_hold_ho_terms)
    breakdowns["MP_hold_ho"] = InequalityBreakdown(
        name="MP_hold_ho",
        terms=mp_hold_ho_terms,
        required_ns=0.0,
        margin_ns=mp_hold_ho_margin,
        **_fmax_fields(inp, _rate_windows(
            inp, margin_ns=mp_hold_ho_margin,
            ui_part_ns=_handover_ui_ns(inp, UI) + _man_clocked_ui_part_ns(inp, man_zd_ns),
            clocked=[("ZD", +1.0, man_zd_ns)])),
        is_setup=False,
        sampling_rising=_mp_hold_ho_rising,
    )

    # ----- PM setup: UI ≥ Per_t_DD,max + Man_t_IS,max + 2·t_PD + Σ_PM,setup -----
    # (Σ_PM,setup from the Manager's clock reference and from the data's anchor.)
    # IN TIME ORDER, which is what the two traversals and the split envelope buy: the clock
    # leaves the Manager, reaches the peripheral, the peripheral recognises it, launches, the
    # data crosses back, the Manager's receiver recognises THAT, and only then is the
    # Manager's own setup window the thing left to satisfy. Every leg below traces the same
    # way, so a reader can follow one row and know what happens next.
    #
    # The Peripheral launches on the edge before the Manager's sampling edge, of the other
    # polarity. Both sampling polarities; a tie keeps the falling one, whose launching edge
    # rises.
    def _pm_setup_case(which: str, sampling_rising: bool) -> list[Term]:
        _clk, _data = _split_cross_terms(
            _cross_terms(inp, "X_PM", SIGMA_SETUP[not sampling_rising]))
        return [
            Term("UI", +UI, op="+"),
            _pd_term(t_PD, credit=False, note=_PD_TO_LAUNCHER),
            *_clk,
            *_launch_terms(inp, "Per", which, "max", credit=False,
                           rising=SIGMA_SETUP_DATA_RISING[not sampling_rising]),
            _pd_term(t_PD, credit=False, note=_PD_FROM_LAUNCHER),
            *_data,
            Term("Man_t_IS", -inp.Man_t_IS_max_ns, op="-"),
        ]

    _pm_setup_legs: tuple[tuple[Inequality, str], ...] = (
        ("PM_setup", "DD"), ("PM_setup_ho", "ZD"))
    _pm_setup_launch: dict[str, bool] = {}
    for _name, _which in _pm_setup_legs:
        _rising, _terms = _worse_case([(_r, _pm_setup_case(_which, _r)) for _r in (False, True)])
        _pm_setup_launch[_name] = not _rising
        _launch_ns = _launch_anchor_ns(inp, "Per", _which, "max",
                                       rising=SIGMA_SETUP_DATA_RISING[not _rising])
        _required = (_launch_ns + inp.Man_t_IS_max_ns + 2.0 * t_PD
                     + sigma_setup[not _rising])
        breakdowns[_name] = InequalityBreakdown(
            name=_name,
            terms=_terms,
            required_ns=_required,
            margin_ns=UI - _required,
            **_fmax_fields(inp, _rate_windows(inp, margin_ns=UI - _required, ui_part_ns=UI)),
            is_setup=True,
            sampling_rising=_rising,
        )

    # ----- PM hold: Per_t_DD,min − Man_t_IH,max + 2·t_PD + Σ_PM,hold ≥ 0 -----
    #
    # ----- PM hold on the ACQUIRED UI: Per_t_ZD in place of Per_t_DD ------------
    #
    # The peripheral-acquiring counterpart of MP_hold_ho above; see that block for
    # what the leg bounds and why nothing else covers it.  Same substitution, same
    # N_HO*UI term, and the PM direction's own credits (+2*t_PD, +Sigma_cross)
    # rather than the MP direction's debits -- which is why it is the comfortable
    # one of the pair and the MP side is where a thin Man_t_ZD,min shows up.
    #
    # THE PERIPHERAL LAUNCHES ON THE EDGE ITS HANDOVER COUNT GIVES (`handover_flips_edge`)
    # after the Manager's sampling edge: on PM_hold that edge itself, and on PM_hold_ho the
    # other polarity at an odd count. The leg reads Sigma_cross,PM,hold at the launching
    # edge's polarity (`SIGMA_HOLD`). Both sampling polarities; a tie keeps the rising one.
    def _pm_hold_case(which: str, launch_rising: bool, ho: bool) -> list[Term]:
        _clk, _data = _split_cross_terms(_cross_terms(
            inp, "X_PM", SIGMA_HOLD[launch_rising], clk_pair=SIGMA_HOLD_CLK_PAIR[launch_rising]))
        return [
            *(_handover_ui_terms(inp, UI) if ho else []),
            _pd_term(t_PD, credit=True, note=_PD_TO_LAUNCHER),
            *_clk,
            *_launch_terms(inp, "Per", which, "min", credit=True,
                           rising=SIGMA_HOLD_DATA_RISING[launch_rising]),
            _pd_term(t_PD, credit=True, note=_PD_FROM_LAUNCHER),
            *_data,
            Term("Man_t_IH", -inp.Man_t_IH_max_ns, op="-"),
        ]

    _pm_hold_legs: tuple[tuple[Inequality, str, bool], ...] = (
        ("PM_hold", "DD", False), ("PM_hold_ho", "ZD", True))
    _pm_hold_launch: dict[str, bool] = {}
    for _name, _which, _ho in _pm_hold_legs:
        _flip = _ho and handover_flips_edge(inp)
        _rising, _terms = _worse_case([(_r, _pm_hold_case(_which, _r != _flip, _ho))
                                       for _r in (True, False)])
        _pm_hold_launch[_name] = _rising != _flip
        _margin = sum(t.value_ns for t in _terms)
        breakdowns[_name] = InequalityBreakdown(
            name=_name,
            terms=_terms,
            required_ns=0.0,
            margin_ns=_margin,
            **_fmax_fields(inp, _rate_windows(inp, margin_ns=_margin,
                                     ui_part_ns=_handover_ui_ns(inp, UI) if _ho else 0.0)),
            is_setup=False,
            sampling_rising=_rising,
        )

    # ----- P->P data legs: peripheral A drives, peripheral B samples ------------
    #
    # ONE PERIPHERAL SAMPLING ANOTHER'S DATA. Both take the same forwarded clock, but each
    # references its own arrival of it, which is what makes this a third direction rather
    # than PM with the labels changed:
    #
    #   setup:  UI  >=  Per_t_DD,max + Per_t_IS + net·t_PD + Sigma_cross,PP,setup
    #   hold:   Per_t_DD,min - Per_t_IH + net·t_PD + Sigma_cross,PP,hold  >=  0
    #
    # and the same pair again with Per_t_ZD in place of Per_t_DD -- the launch-method half
    # of the 2x2 this file treats as structural. The receiver cannot tell which path
    # produced the edge, so both must satisfy both.
    #
    # THE FLIGHT TERM IS THE PLACEMENT, NOT THE BUS LENGTH, and it is signed by geometry
    # rather than by direction: see `pp_net_pd` and `CalcInputs.pp_placement`. Under the
    # unknown placement (Worst Per Leg) setup reads the bracket at 2 (driver at the far end,
    # sampler at the near end, data running back against the clock) and hold reads it at 0
    # (any placement whose bracket is zero), so the two legs sit at OPPOSITE ends of one row
    # -- exactly what the per-inequality corner search is for. Hold then gets NO flight
    # credit, where PM hold banks +2·t_PD, and that difference is most of why P->P hold is
    # the tightest leg here. A pinned placement gives both legs its own bracket, so
    # `driver_far` credits hold the same two traversals it charges setup.
    #
    # THREE CROSSINGS ENTER, and only two of them are correlated:
    #
    #   clk@A    A's detection of the clock, where its launch is referenced
    #   data@B   B's detection of A's data
    #   clk@B    B's detection of the clock, which IS its sample edge
    #
    # clk@B and data@B are the same die at nearly the same instant, so they are correlated
    # and their difference is the Delta_cross,MP construction (`DELTA_PP`). clk@A is an
    # unrelated part and is charged IN FULL. The construction is pinned as an identity by
    # test_pp_data_legs_carry_the_three_crossings_and_only_two_are_correlated; the vendored
    # reference fixture's P->P magnitudes are not compared.
    #
    # clk@A AND clk@B ARE OF ONE EDGE ON A HOLD LEG WHERE A LAUNCHES ON THE EDGE B SAMPLES
    # (`pp_shares_clock_edge`): both take that edge's one slew factor, the one that leaves the
    # leg the less margin, and each keeps its own coefficient (`pp_clock_edges`). On setup
    # they are of two edges a UI apart, each at its corner's own factor.
    # Same rule as the contention legs' bus term and the handover UI: a flight term that the
    # placement does not levy is dropped rather than printed as a zero (`_handover_ui_terms`).
    def _pp_pd_terms(*, is_setup: bool) -> list[Term]:
        """The placement's traversals, ONE TERM EACH so the leg can interleave them.

        Two is the only non-zero count (`pp_placement` — driver far, sampler near), and it is
        two DIFFERENT flights: the clock reaching A at the far end, then A's data running back
        against it to B. So they bracket A's launch rather than sitting together, and each
        names its own flight. A count of zero emits nothing: there the geometry makes it
        structurally absent, not zero-valued.
        """
        n = pp_net_pd(inp.pp_placement, is_setup=is_setup)
        notes = (_PD_TO_LAUNCHER.replace("the launching peripheral", "A, at the far end"),
                 _PD_FROM_LAUNCHER.replace("the sampler", "B"))
        return [_pd_term(t_PD, credit=not is_setup, note=notes[i]) for i in range(int(n))]

    pp_setup_pd_ns = pp_net_pd(inp.pp_placement, is_setup=True) * t_PD

    # Annotated so `_name` keeps the `Inequality` Literal rather than widening to `str`,
    # which the breakdowns dict and InequalityBreakdown.name both require. Same reason
    # `CrossLane` is aliased at its definition.
    _pp_setup_legs: tuple[tuple[Inequality, str], ...] = (
        ("PP_setup", "DD"),
        ("PP_setup_ho", "ZD"),
    )
    # B SAMPLES ON EITHER POLARITY and A launches on the edge before, of the other polarity,
    # detected at that polarity's late corner: `X_MP,A,rise,late` rising and
    # `X_MP,A,fall,late` falling. A tie keeps B sampling on a falling edge. A's data edge is
    # its launching edge's polarity. B's clock detection is `X_MP,B` and its detection of A's
    # data `X_PP,B` (`DELTA_PP`).
    def _pp_setup_case(which: str, b_rising: bool) -> tuple[float, list[Term]]:
        """(the leg's required time, its terms) for A's launch `which`, its anchor after A's
        detection, with B sampling on a rising edge or not."""
        launch_ns = _launch_anchor_ns(inp, "Per", which, "max", rising=not b_rising)
        a_p = _per_clk_cross_parts(inp, env, corner="late", rising=not b_rising)
        a_sym = clock_detection_symbol("late", rising=not b_rising, device="A")
        required = (launch_ns + inp.Per_t_IS_max_ns + pp_setup_pd_ns
                    + a_p[0] + delta_PP["setup", b_rising])
        _pd = _pp_pd_terms(is_setup=True)
        _b_clk, _b_data = _split_cross_terms(
            _cross_terms(inp, "X_PP,B", DELTA_PP["setup", b_rising], clk_pair="X_MP,B"))
        # THE TWO CLOCK CROSSINGS STAY ADJACENT, which is what lets
        # `_gather_lane_terms` fold them into one product: they read the same lane, so
        # `(X_MP,A,rise,late − X_MP,B,fall,early) · Man_t_RF,CLK` is exact. That one term
        # therefore spans TWO instants — A recognising the edge and B recognising it — and sits
        # at the earlier of them, A's, which is the one the launch follows.
        return required, [
            Term("UI", +UI, op="+"),
            *_pd[:1],
            Term(f"{a_sym} · Man_t_RF,CLK", -a_p[0], op="-",
                 note="A's own clock detection, charged in full: an unrelated part — "
                      + _coeff_note(a_p),
                 factors=a_p[1:]),
            *_b_clk,
            *_launch_terms(inp, "Per", which, "max", credit=False,
                           note=_pp_device_note("A, the driver", "its launch"),
                           rising=not b_rising),
            *_pd[1:],
            *_b_data,
            Term("Per_t_IS", -inp.Per_t_IS_max_ns, op="-",
                 note=_pp_device_note("B, the sampler", "its setup window")),
        ]

    for _name, _which in _pp_setup_legs:
        _pp_s = {_r: _pp_setup_case(_which, _r) for _r in (False, True)}
        _rising, _terms = _worse_case([(_r, _pp_s[_r][1]) for _r in (False, True)])
        _required = _pp_s[_rising][0]
        breakdowns[_name] = InequalityBreakdown(
            name=_name, terms=_terms, required_ns=_required, margin_ns=UI - _required,
            **_fmax_fields(inp, _rate_windows(inp, margin_ns=UI - _required, ui_part_ns=UI)),
            is_setup=True, sampling_rising=_rising)

    _pp_hold_legs: tuple[tuple[Inequality, str, bool], ...] = (
        ("PP_hold", "DD", False),
        # The handover UI credits hold and not setup, exactly as on MP/PM: it puts the
        # acquirer's turn-on a UI further from the sampling edge, while setup's sampling
        # edge moves with the launch and nets out.
        ("PP_hold_ho", "ZD", True),
    )
    for _name, _which, _ho in _pp_hold_legs:
        # A DRIVES ON EVERY P->P LEG, BUT ON A HOLD LEG ITS LAUNCH IS THE AGGRESSOR, and on
        # the handover variant the level it destroys is not even A's:
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
        # and the releaser are ever driving AT ONCE (a current that damages drivers); this asks
        # whether A's turn-on ends the releaser's LEVEL before B has finished sampling it (a
        # lost bit). Either can fail with the other passing.
        #
        # B SAMPLES ON THE EDGES `pp_hold_edges` NAMES, detected at that polarity's late corner,
        # and A LAUNCHES ON THE EDGE ITS HANDOVER COUNT GIVES, one edge per UI after B's
        # (`handover_flips_edge`), detected at that polarity's early corner: on PP_hold the
        # edge B samples. Each leg has two cases, B rising and B falling, and reports the
        # worse. Each clock detection names its device and its edge's polarity: B's late one
        # `X_MP,B,rise,late` / `X_MP,B,fall,late` and A's early one `X_MP,A,rise,early` /
        # `X_MP,A,fall,early`. B's detection of A's data is correlated with its clock detection
        # (`DELTA_PP`): falling data, `X_PP,B,fall,early`, against a rising sampling edge, and
        # rising data, `X_PP,B,rise,early` (`mp_early_rise_per`), against a falling one.
        _who = "A, the acquiring driver" if _ho else "A, the driver"
        _level = ("its launch out of high-Z, which ends the RELEASING peripheral's level"
                  if _ho else "its launch, which ends its own previous level")
        _pd = _pp_pd_terms(is_setup=False)
        _cases: list[tuple[bool, list[Term]]] = []
        for _a_rising, _b_rising in pp_hold_edges(inp, _name):
            # ONE EDGE ON PP_hold, AND ON PP_hold_ho AT ZERO HANDOVER UIs: A launches on the
            # edge B samples, so A's detection and B's take that edge's one slew factor
            # (`pp_clock_edges`). With handover UIs A's edge is that many UIs after B's and
            # each takes its corner's own factor.
            _a_edge, _b_edge = pp_clock_edges(inp, env, _name, sampling_rising=_b_rising)
            pp_clk_early_p = _per_clk_cross_parts(inp, env, corner="early", edge=_a_edge,
                                                  rising=_a_rising)
            _b_clk_p = _per_clk_cross_parts(inp, env, corner="late", device="B",
                                            edge=_b_edge, rising=_b_rising)
            _a_sym = clock_detection_symbol("early", rising=_a_rising, device="A",
                                            edge=_a_edge)
            _b_pol = "rise," if _b_rising else "fall,"
            _b_data_lane = DELTA_PP["hold", _b_rising][1]
            assert _b_data_lane[1] == "Per DATA", _b_data_lane
            _hold_pp: tuple[CrossLane, ...] = (
                (f"{_b_pol}{_role_on_edge('late', _b_edge)}", "Man CLK", _b_clk_p[1], "-"),
                _b_data_lane)
            _b_clk, _b_dat = _split_cross_terms(
                _cross_terms(inp, "X_PP,B", _hold_pp, clk_pair="X_MP,B"))
            _cases.append((_b_rising, [
                *(_handover_ui_terms(inp, UI) if _ho else []),
                *_pd[:1],
                Term(f"{_a_sym} · Man_t_RF,CLK",
                     +pp_clk_early_p[0], op="+",
                     note=("A's own clock detection, of a "
                           + ("rising edge at V_IH" if _a_rising else "falling edge at V_IL")
                           + ", and it enters as a CREDIT because A's "
                           "launch is referenced to it: detecting later launches later and "
                           "leaves the level valid longer. The EARLY end is therefore the "
                           "worst corner — the smallest credit, not a helpful one — "
                           + _coeff_note(pp_clk_early_p)),
                     factors=pp_clk_early_p[1:]),
                *_b_clk,
                *_launch_terms(inp, "Per", _which, "min", credit=True,
                               note=_pp_device_note(_who, _level), rising=not _b_rising),
                *_pd[1:],
                *_b_dat,
                Term("Per_t_IH", -inp.Per_t_IH_max_ns, op="-",
                     note=_pp_device_note("B, the sampler", "its hold window")),
            ]))
        # The worse case; a tie keeps the first, B sampling on a rising edge.
        _rising, _terms = _worse_case(_cases)
        _margin = sum(_t.value_ns for _t in _terms)
        breakdowns[_name] = InequalityBreakdown(
            name=_name, terms=_terms, required_ns=0.0, margin_ns=_margin,
            **_fmax_fields(inp, _rate_windows(
                inp, margin_ns=_margin,
                ui_part_ns=_handover_ui_ns(inp, UI) if _ho else 0.0)),
            is_setup=False, sampling_rising=_rising)

    # ----- Handover non-contention, both directions -----
    #
    #   N_HO·UI + t_ZD,min(acquiring) − t_DZ,max(releasing) + t_PD ≥ 0
    #
    # The final term is the overlap allowance: two drivers may be on together for one
    # flight time without contending. See the criterion note below. Reading the margin
    # against the overlap it permits, where O is how long both drivers are on:
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
    # t_DZ and t_ZD bound DRIVER STATE: release to high-Z, and turn-on out of it. The
    # turn-on is the acquirer's 20 % point, where t_ZD is measured to, so the leg reads
    # t_ZD as tabulated (a SoundWire side's at its anchor) and the device's split between
    # delay and ramp does not enter. A Manager's release and turn-on are measured from its
    # clock reference.
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
    # The event being bounded is two drivers being on at once, which is not located at
    # either pin, so no data-flight traversal is charged. What remains is the clock
    # skew — each device's t_DZ/t_ZD is referenced to its own clock arrival — plus the
    # allowance.
    #
    # NO NEW PARAMETER. The two contending devices are taken to sit at opposite ends
    # of the bus, so their separation IS `t_PD` — and the bus row already sweeps
    # 0..30 cm, which lets the corner search find whichever end binds each leg. The
    # short end binds MP, since a co-located pair has no allowance to spend.
    #
    # WHERE THE LEVEL IS COVERED, since this bounds driver CURRENT and not the
    # voltage on the line during the overlap. The `*_hold_ho` legs own that: the bit
    # sampled at the UI-opening edge must stay valid for t_IH,max past it, and the
    # acquiring device's turn-on out of high-Z is what ends it. They are live at
    # N_HO = 0. A peripheral
    # receiving while another peripheral drives has its own setup/hold pair; see the
    # PP block above.
    #
    # A releasing or acquiring peripheral's clock detection is `_per_clk_cross_parts`, at the
    # corner its direction needs: the Delta_cross,MP fields, `mp_late_rise` / `mp_late_fall`
    # late and `sigma_pm_hold_rf_clk` / `mp_early_fall` early.
    #
    # BOTH POLARITIES OF THE CLOSING EDGE, the edge that closes the releaser's UI: each leg is
    # evaluated with it rising and falling and reports the worse (`_worse_case`, the rising
    # case first), recording it in `InequalityBreakdown.closing_rising`. A releasing
    # peripheral detects the closing edge, or with an EndDriveEarly release the edge that
    # opens its UI, of the other polarity, late: `X_MP,rise,late` at V_IH when rising and
    # `X_MP,fall,late` at V_IL when falling. The acquirer turns on N_HO UIs after the closing
    # edge, on the polarity that count gives (`handover_flips_edge`), and detects it early:
    # `X_MP,rise,early` rising and `X_MP,fall,early` falling (`clock_detection_symbol`).
    #
    # P->P has two unrelated peripherals on it, so each takes its own corner of the
    # crossing: the acquirer's credit early, the releaser's charge late. See
    # `_per_clk_cross_parts`. AT ZERO HANDOVER UIs, WITH A CONVENTIONAL RELEASE, THE TWO DETECT
    # ONE EDGE, the closing edge: both take that edge's one slew factor and each keeps its own
    # coefficient (`pp_clock_edges`). Otherwise the acquirer's edge is N_HO UIs after the
    # releaser's, or an EDE release is referenced to the edge before it, and each edge takes
    # its corner's own factor.
    def _contention_parts(closing: bool) -> _ClosingCase:
        rel_rising = closing != inp.Per_ede
        acq_rising = closing != handover_flips_edge(inp)
        rel_edge, acq_edge = pp_clock_edges(inp, env, "PP_contention", closing_rising=closing)
        return _ClosingCase(
            rel_rising, acq_rising, rel_edge, acq_edge,
            late=_per_clk_cross_parts(inp, env, corner="late", rising=rel_rising),
            early=_per_clk_cross_parts(inp, env, corner="early", rising=acq_rising),
            pp_rel=_per_clk_cross_parts(inp, env, corner="late", edge=rel_edge,
                                        rising=rel_rising),
            pp_acq=_per_clk_cross_parts(inp, env, corner="early", device="B", edge=acq_edge,
                                        rising=acq_rising))

    _closing_parts = {_c: _contention_parts(_c) for _c in (True, False)}
    #
    # A Manager end has no clock crossing: the Manager sources the clock (Manager Output Timing
    # figure). Its detection is "" in the table below, and its term is structurally zero, so it
    # is not emitted (checklist rule 5).
    # Notes travel with the values so a snapped grid point is visible in the
    # substitution, not only in the number.
    man_dz_ns, man_dz_note = man_tdz_parts(inp)

    # THE FRAME, shared by all three legs. Every event is placed absolutely and then
    # differenced:
    #
    #   acquirer drives at      N_HO*UI + a_pd + [detect if Per] + t_ZD,min
    #   releaser is off at              r_pd + [detect if Per] + t_DZ,eff
    #   ...and the two may OVERLAP by                            t_PD
    #
    # a_pd is the acquiring device's clock arrival delay, r_pd the releasing one's.
    # A Manager sources the clock, so both are zero for it; a peripheral pays t_PD.
    # No traversal is charged for the releaser's data: the drivers are compared
    # against each other in absolute time, not at one device's pin -- see the
    # criterion note above.
    #
    # ONE FORM FOR ALL THREE LEGS; a bus or UI term whose count is zero is dropped
    # (see the bus-term note in the loop below).
    #
    # THE BUS IS A NET COUNT, not a pair that cancels. Composed of:
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
    # The two peripheral-releasing legs are bus-independent: the distance that makes a
    # releasing peripheral late is the same distance that licenses the overlap, so the
    # skew and the allowance are the same quantity with opposite signs. M->P's two are
    # shown as two `t_PD` terms, the clock's flight out and the acquirer's drive back.
    _NET_PD = {"MP_contention": +2.0, "PM_contention": 0.0, "PP_contention": 0.0}
    _contention_legs: tuple[
        tuple[Inequality, str, str, str, str, str], ...] = (
        # (name, releasing, acquiring, acquirer's detection, releaser's detection, dz_note):
        # the detections are `_contention_parts` keys, "" for a Manager end.
        ("MP_contention", "Man", "Per", "early", "", man_dz_note),
        ("PM_contention", "Per", "Man", "", "late", ""),
        # Worst topology: the releaser at the far end (latest detection), the
        # acquirer at the Manager end (earliest edge). A real pair may be closer
        # together -- this is the bounding case, not a measurement of one layout.
        # Two unrelated peripherals, so each takes its own corner: the acquirer's
        # credit is NOT the one the releaser's charge forces out of a shared row-set.
        #
        # A NEGATIVE P->P MARGIN AT N_HO = 0 IS THE EXPECTED PHY2 ANSWER, not a defect.
        # PHY2 allocates a UI to every handover by default (§11.1.1.1,
        # `default_handover_UIs`) and that UI is how it separates two peripherals'
        # drivers.  Evaluated at zero allocated UIs this leg is the EndDriveEarly
        # hypothetical, and the margin reports THE PRICE OF REMOVING THE UI on that
        # transition.  Allocation is per-transition (the gap in the transport map IS the
        # handover), so a P->P boundary that does not close costs one UI AT THAT
        # TRANSITION while the mixed ones stay free.  Read the sign of this leg as "may
        # this handover be zero-UI?", never as "is this bus broken?".
        #
        # The releaser is Peripheral A, at the Peripheral's supply, so its late detection is
        # `X_MP,A,rise,late` rising and `X_MP,A,fall,late` falling; the acquirer is B, at its
        # own supply, so its early detection is `X_MP,B,rise,early` rising and
        # `X_MP,B,fall,early` falling. A detection on a shared edge whose factor is not its
        # corner's own carries the edge in its symbol (`_role_on_edge`), e.g.
        # `X_MP,B,fall,slow,early`.
        ("PP_contention", "Per", "Per", "pp_acq", "pp_rel", ""),
    )
    for (name, releasing, acquiring, acq_key, rel_key, dz_note) in _contention_legs:
        # For the rate solve below: `dz_ui` is the UI-scaled part of the release, `zd_ui`
        # that of the turn-on, and `clocked` the clocked Manager timings among them.
        clocked: list[ClockedPart] = []
        if _releaser_ede(inp, releasing):
            t_dz = 0.0      # not read: the release is its parts, per case (`_case`)
            # The launch scales with UI when it is a clocked Manager t_DD.
            launch_ui = (_man_clocked_ui_part_ns(inp, _man_tdd_range(inp)[1])
                         if releasing == "Man" else 0.0)
            dz_ui = launch_ui - UI
            if releasing == "Man":
                clocked.append(("DD", -1.0, _man_tdd_range(inp)[1]))
        elif releasing == "Man":
            t_dz = man_dz_ns
            dz_ui = _man_clocked_ui_part_ns(inp, man_dz_ns)
            clocked.append(("DZ", -1.0, man_dz_ns))
        else:
            t_dz = inp.Per_t_DZ_max_ns
            dz_ui = 0.0
        # THE TURN-ON IS THE ACQUIRER'S 20 % POINT, the anchor its t_ZD is measured to, so the
        # device's split between delay and ramp does not enter. A Manager's is measured from
        # its clock reference, as is its release.
        if acquiring == "Man":
            zd_ui = _man_clocked_ui_part_ns(inp, man_zd_ns)
            clocked.append(("ZD", +1.0, man_zd_ns))
        else:
            zd_ui = 0.0
        n_pd = _NET_PD[name]
        # ONE CLOCK, ONE LANE NAME. The Manager is the only source of the forwarded clock, so
        # every device's detection of it is a crossing on `Man_t_RF,CLK`.

        # Symbols only, in the grammar the rest of the file uses -- the releaser's late
        # clock detection is `X_MP,rise,late` / `X_MP,fall,late` and the acquirer's early one
        # `X_MP,rise,early` / `X_MP,fall,early`, the names whose coefficients they carry
        # (`clock_detection_symbol`); on P->P the releaser is A, `X_MP,A,...`, and the acquirer
        # B, `X_MP,B,...`. The bus is a count of traversals.
        #
        # IN TIME ORDER: the releaser first (a peripheral's detection of the closing edge, or
        # of the edge opening its UI under EDE, then the release), then the allocated handover
        # UIs, then the acquirer (on M->P the clock's flight to the peripheral, its detection,
        # the turn-on and the flight of its drive back to the Manager, the two traversals as
        # two terms).
        #
        # ON P->P THE TWO DETECTIONS STAY ADJACENT, ahead of the release: they read the same
        # row, one Manager pin's slew setting, and `_gather_lane_terms` folds them into one
        # exact product.
        #
        # THE BUS TERMS ARE DROPPED WHERE THEIR COUNT IS ZERO, and the test is "zero for
        # every input", not "zero at this corner". P->M and P->P net to no traversals at
        # all -- the releasing peripheral's late clock and the overlap allowance are the
        # same distance with opposite signs -- so they can never be anything but zero.
        # `0·UI` is dropped too; see `_handover_ui_terms`.
        pp = releasing == acquiring
        # AN EndDriveEarly RELEASE SHOWS ITS PARTS (`_ede_release_terms`), from the edge that
        # opens the driven UI back to the closing edge; a conventional one is t_DZ after the
        # closing edge.
        assert n_pd in (0.0, 2.0), (name, n_pd)
        flight = [Term("t_PD", +t_PD, op="+")] if n_pd else []

        # THE TURN-ON IS PLACED ON ITS WORSE DATA EDGE (`turn_on_rising`), from the clock edge
        # its handover count gives; an EndDriveEarly release on the edge `ede_release_rising`
        # gives, from the opening edge, of the other polarity to the closing edge.
        acq_by_clk: dict[bool, list[Term]] = {}

        def _case(closing: bool) -> list[Term]:
            c = _closing_parts[closing]
            release = (_ede_release_terms(inp, releasing, clk_rising=not closing)
                       if _releaser_ede(inp, releasing)
                       else [Term(f"{releasing}_t_DZ", -t_dz, op="-", note=dz_note)])
            acq_clk = c.acq_rising or acquiring == "Per"     # only a Manager's reads it
            if acq_clk not in acq_by_clk:
                acq_by_clk[acq_clk] = _launch_terms(
                    inp, acquiring, "ZD", "min", credit=True, clk_rising=acq_clk,
                    rising=turn_on_rising(inp, acquiring, clk_rising=acq_clk))
            acq_terms = acq_by_clk[acq_clk]
            rel_sym = (clock_detection_symbol("late", rising=c.rel_rising, device="A",
                                              edge=c.rel_edge) if pp
                       else clock_detection_symbol("late", rising=c.rel_rising))
            acq_sym = (clock_detection_symbol("early", rising=c.acq_rising, device="B",
                                              edge=c.acq_edge) if pp
                       else clock_detection_symbol("early", rising=c.acq_rising))
            rel_p: CrossParts | None = getattr(c, rel_key) if rel_key else None
            acq_p: CrossParts | None = getattr(c, acq_key) if acq_key else None
            rel_detect = ([Term(f"{rel_sym} · Man_t_RF,CLK", -rel_p[0], op="-",
                                note=_coeff_note(rel_p), factors=rel_p[1:])]
                          if rel_p is not None else [])
            acq_detect = ([Term(f"{acq_sym} · Man_t_RF,CLK", +acq_p[0], op="+",
                                note=_coeff_note(acq_p), factors=acq_p[1:])]
                          if acq_p is not None else [])
            if pp:                                     # P->P: one gathered detection
                return [*rel_detect, *acq_detect, *release, *_handover_ui_terms(inp, UI),
                        *acq_terms]
            return [*rel_detect, *release, *_handover_ui_terms(inp, UI), *flight,
                    *acq_detect, *acq_terms, *flight]

        closing_rising, terms = _worse_case([(_c, _case(_c)) for _c in (True, False)])
        margin = sum(t.value_ns for t in terms)
        breakdowns[name] = InequalityBreakdown(
            name=name,
            terms=terms,
            required_ns=0.0,
            margin_ns=margin,
            # Rate-dependent through the allocated handover UIs and through any device
            # timing that scales with UI: an EDE release (its driven UI, and a clocked
            # Manager t_DD) and a clocked Manager t_DZ / t_ZD. With none of those the margin is
            # a difference of device constants plus a bus delay, which no clock rate
            # changes.
            **_fmax_fields(inp, _rate_windows(
                inp, margin_ns=margin,
                ui_part_ns=_handover_ui_ns(inp, UI) + zd_ui - dz_ui, clocked=clocked)),
            is_setup=False,
            closing_rising=closing_rising,
        )

    # ----- Bus keeper: the releasing driver holds its level until the keeper follows ---
    #
    #   (the data's crossing of the keeper's threshold) + t_keeper  <=  release_earliest
    #
    # both sides counted from the edge that opens the driven UI. The keeper's response is
    # measured from its input crossing V_IH / V_IL ("PHY1 & PHY2 Bus Keeper Behavior" table),
    # and its input is at the Manager, so the threshold is the Manager's, at the Manager's
    # supply: V_IH,max rising and V_IL,min falling, the ends the data crosses the latest
    # (`keeper_corners`). A SoundWire Manager's keeper takes V_TH_Keeper,max / V_TL_Keeper,min
    # instead. A Peripheral's data reaches that input swinging to the Peripheral's supply and
    # offset by the ground shift α, as on every P->M crossing; the Manager's own data carries
    # neither.
    #
    # The release is set by the releasing driver's mode: conventionally 1·UI + t_DZ from the
    # opening edge (a Peripheral's from its own two detections), and with EndDriveEarly the
    # launch, its ramp to its own 80 % point (V_OH / V_OL, `_to_voh_ns`) and its EDE Delay
    # (`ede_held`, in which the launch cancels).
    #
    # t_DD,max is taken AFTER the launch mode.  t_DZ enters at its MIN corner here (the
    # earliest release is the one that starves the keeper) and at its max in the
    # contention legs -- the same field pulled to opposite ends, which the
    # auto-worst solver resolves per inequality.
    #
    # t_DZ and t_ZD are NOT symmetric, and the asymmetry is what makes this exact
    # rather than approximate.  SWI3S impedance control is sawtooth (the G-Ramp case of
    # "PHY1&2 (FBCSE) Example Sequences of Ramp Types"): RAMPED ON, INSTANT OFF.
    #
    #   t_DZ  the driver is FULLY high-Z.  No turn-off ramp -- it drives at
    #         strength right up to t_DZ and then stops.  So t_DZ is precisely
    #         when the level stops being driven, and the margin below is exact,
    #         not a bound.
    #   t_ZD  measured to 20 % of the driver's CONDUCTANCE ramp -- so driving has
    #         begun by then, and a valid V_OH/V_OL is still a voltage slew away.
    #
    # The contention legs take the turn-on at t_ZD's 20 % point: overlap is counted from
    # there to the releaser's t_DZ, so a NEGATIVE contention margin's magnitude IS the
    # overlap in excess of what the flight time licenses -- the overlap itself is
    # `t_PD − margin`. Conduction before the 20 % point is not counted.
    # ONE keeper, and it is in the MANAGER ({ASW3805}) -- there is no peripheral
    # keeper.  The two legs below are therefore not "the Manager's keeper margin"
    # and "the Peripheral's": they are the same single keeper, evaluated for each
    # device that can be the one RELEASING.  Both release at some handover, and
    # each must hold its level until that one keeper has followed it.
    #
    # No t_PD term: the keeper sits at the Manager's end, so a Peripheral's level and its
    # high-Z both reach it one flight after they leave the Peripheral's pin, and the flight
    # cancels. The line is taken as settled as soon as the level arrives; ringing is not
    # modelled.
    #
    # ONE LEG PER RELEASING DEVICE (the keeper note at the top of this module), so the
    # results tree names which device is short.
    #
    # The terms read in time order from the edge that opens the UI: the launch and its
    # crossing of the keeper's threshold (`X_K,rise,late · <dev>_t_RF,DATA` on a rising data
    # edge, `X_K,fall,late` on a falling one, `_keeper_case`), the
    # closing edge (1·UI, and for a Peripheral the spacing of its two detections) and the
    # release after it (t_DZ, or the ramp to V_OH / V_OL and the EDE Delay), and last the
    # keeper's response, the time the level held past the crossing must cover.
    #
    # A 0 ns keeper requirement (a corner sweep's low end, or a spec that states none) drops
    # the term rather than printing a zero row.
    keeper_term = inp.t_keeper_ns

    def keeper_crossing(lane: str, dev: str, *, ede: bool,
                        clk_rising: bool = True) -> tuple[bool, Term]:
        """(the data edge's polarity, the term) from the release's anchor to the data's
        crossing of the keeper's threshold at the Manager (`_keeper_case`), on a launch from a
        clock edge of polarity `clk_rising`."""
        rising, coef, note = _keeper_case(inp, lane, supplies, ede=ede, clk_rising=clk_rising)
        k = lane_ns(inp, lane)
        role = _fast_role(inp, lane, f"{'rise' if rising else 'fall'},late")
        return rising, Term(f"X_K,{role} · {dev}_t_RF,DATA",
                            -coef * k, op="-", note=note, factors=(coef, k))

    def ede_held(lane: str, dev: str, t_ede_ns: float) -> list[Term]:
        """An EndDriveEarly release's leg, from the anchor: the release is the ramp to V_OH /
        V_OL plus the EDE Delay, so the leg charges the anchor to the keeper's crossing and
        credits the ramp and the EDE Delay, in that time order."""
        rising, crossing = keeper_crossing(lane, dev, ede=True)
        return [crossing,
                _to_voh_term(inp, lane, dev, rising, credit=True),
                Term(f"{dev}_t_EDE", +t_ede_ns, op="+",
                     note=_ede_keeper_note(inp, dev, t_ede_ns))]

    # --- the Manager releasing -------------------------------------------------
    #
    # A Manager that forces its keeper (`CalcInputs.Man_keeper_forced`) leaves this leg nothing
    # to require: it does not apply.
    #
    # THE UI IS A TERM, NOT AN ADJUSTMENT FOLDED INTO t_DZ. t_DD and t_ZD are
    # referenced to the edge that OPENS the UI; a conventional release is referenced
    # to the NEXT edge, the one that closes the last driven UI (Note 1 of the "PHY1 & PHY2
    # Output Timing" table). So the driver keeps driving PAST that edge and into the
    # following UI, and stops at `UI + t_DZ`.
    #
    # THE CORNER IS THE SEARCH'S JOB, NOT THE LABEL'S. This leg wants the EARLIEST
    # release, so `find_worst_corner_rows` drives the one `*_t_DZ_max_ns` field to its
    # row's low end here while the contention legs pull the same field to the high end.
    # The term is therefore labelled `Man_t_DZ` with no corner suffix: writing
    # `Man_t_DZ,min` asserts a guaranteed-earliest release that the spec does not
    # define -- t_DZ is a MAXIMUM, "high-Z by this time" -- and a coined minimum in a
    # displayed inequality reads as a promise. The note records which end is in force.
    #
    # The UI and t_DZ are separate terms, and the corner search says which end of t_DZ
    # each leg reads.
    #
    # NO CLOCK REFERENCE TERM. The Manager's release and its launch are both measured from
    # its clock reference, which therefore cancels out of the difference.
    # `man_dz_ns` / `man_dz_note` are the contention legs' own values, reused so one
    # output stage has one release time. See `man_tdz_parts`.
    # The opening edge is the polarity that places a SoundWire Manager's launch the later
    # (`keeper_open_rising`), and the data edge the one `keeper_crossing` gives from it.
    if inp.Man_ede:
        man_keeper_terms = ede_held("Man DATA", "Man", inp.Man_t_EDE_ns)
    else:
        _open = keeper_open_rising(inp)
        _man_k_rising, _man_crossing = keeper_crossing("Man DATA", "Man", ede=False,
                                                       clk_rising=_open)
        man_keeper_terms = [
            *_launch_terms(inp, "Man", "DD", "max", credit=False, rising=_man_k_rising,
                           clk_rising=_open),
            _man_crossing,
            Term("1·UI", +UI, op="+"),
            Term("Man_t_DZ", +man_dz_ns, op="+",
                 note="; ".join(n for n in (man_dz_note, _release_note(man_dz_ns)) if n)),
        ]
    # The keeper's response term is the spec's Man_tKeeper_Response, shown with its t
    # subscripted like every timing symbol here (the table's Man_tDD is `Man_t_DD`).
    if keeper_term:
        man_keeper_terms.append(Term("Man_t_Keeper_Response", -keeper_term, op="-",
                                     note=_keeper_note(inp)))
    man_keeper_margin = sum(t.value_ns for t in man_keeper_terms)
    if inp.Man_keeper_forced:
        breakdowns["keeper_Man"] = InequalityBreakdown(
            name="keeper_Man", terms=[], required_ns=0.0, margin_ns=math.inf,
            F_max_MHz=math.inf, is_setup=False,
            not_applicable=("the Manager forces its keeper to the level it drives, so its "
                            "release waits for no keeper response"))
    else:
        breakdowns["keeper_Man"] = InequalityBreakdown(
            name="keeper_Man", terms=man_keeper_terms, required_ns=0.0,
            margin_ns=man_keeper_margin,
            # The conventional release adds the whole closing UI and a clocked t_DZ, a fraction
            # of the UI; a clocked launch's t_DD is a fraction of the UI and comes off it. An
            # EndDriveEarly leg has no UI-scaled part.
            **_fmax_fields(inp, _rate_windows(inp, margin_ns=man_keeper_margin, ui_part_ns=0.0)
                           if inp.Man_ede else _rate_windows(
                               inp, margin_ns=man_keeper_margin,
                               ui_part_ns=UI + _man_clocked_ui_part_ns(inp, man_dz_ns)
                               - _man_clocked_ui_part_ns(inp, _man_tdd_range(inp)[1]),
                               clocked=[("DZ", +1.0, man_dz_ns),
                                        ("DD", -1.0, _man_tdd_range(inp)[1])])),
            is_setup=False)

    # --- a peripheral releasing ------------------------------------------------
    #
    # Without EDE the peripheral drives its whole UI and then lets go, so the
    # earliest release is the closing edge itself (the "PHY1 & PHY2 Output Timing" table
    # gives Per_t_DZ a minimum of 0) and the leg has a full UI of driven level to play with.
    #
    # THE PERIPHERAL'S TWO TIMINGS COUNT FROM ITS OWN TWO DETECTIONS: Per_t_DD from its
    # detection of the edge that opens the UI, Per_t_DZ from its detection of the closing
    # edge, the next edge, of the other polarity. `1·UI` spans the two edges' Manager
    # references, so the spacing of the two detections differs from it by the closing
    # detection's crossing less the opening one's. The keeper is starved by the opening edge
    # detected late and the closing edge early: `X_MP,<rise|fall>,late` and
    # `X_MP,<fall|rise>,early` on
    # `Man_t_RF,CLK`, each at its own edge's corner factor, from one device's thresholds at
    # its one supply. Both read the clock row, so they show as one gathered product
    # (`_gather_lane_terms`), placed after the UI it corrects. The leg is evaluated at both
    # polarities of the closing edge and reports the worse (`_worse_case`, rising first).
    per_keeper_closing: bool | None = None
    if inp.Per_ede:
        per_keeper_terms = ede_held("Per DATA", "Per", inp.Per_t_EDE_ns)
    else:
        def _per_keeper_case(closing: bool) -> list[Term]:
            opening = not closing
            late = _per_clk_cross_parts(inp, env, corner="late", rising=opening)
            early = _per_clk_cross_parts(inp, env, corner="early", rising=closing)
            _k_rising, _crossing = keeper_crossing("Per DATA", "Per", ede=False,
                                                   clk_rising=opening)
            return [
                *_launch_terms(inp, "Per", "DD", "max", credit=False, rising=_k_rising),
                _crossing,
                Term("1·UI", +UI, op="+"),
                Term(f"{clock_detection_symbol('late', rising=opening)} · Man_t_RF,CLK",
                     -late[0], op="-", note=_coeff_note(late), factors=late[1:]),
                Term(f"{clock_detection_symbol('early', rising=closing)} · Man_t_RF,CLK",
                     +early[0], op="+", note=_coeff_note(early), factors=early[1:]),
                # Same release point as the Manager's conventional leg: the peripheral
                # drives its UI and goes on driving into the next one until t_DZ. The
                # corner search reads the low end of the Per_t_DZ row here, which is what
                # makes this the earliest release rather than the guaranteed-by one.
                Term("Per_t_DZ", +inp.Per_t_DZ_max_ns, op="+",
                     note=_release_note(inp.Per_t_DZ_max_ns)),
            ]

        per_keeper_closing, per_keeper_terms = _worse_case(
            [(_c, _per_keeper_case(_c)) for _c in (True, False)])
    if keeper_term:
        per_keeper_terms.append(Term("Man_t_Keeper_Response", -keeper_term, op="-",
                                     note=_keeper_note(inp)))
    per_keeper_margin = sum(t.value_ns for t in per_keeper_terms)
    breakdowns["keeper_Per"] = InequalityBreakdown(
        name="keeper_Per", terms=per_keeper_terms, required_ns=0.0,
        margin_ns=per_keeper_margin,
        # The conventional release carries the closing UI; the EDE Delay does not.
        **_fmax_fields(inp, _rate_windows(inp, margin_ns=per_keeper_margin,
                                 ui_part_ns=0.0 if inp.Per_ede else UI)),
        is_setup=False,
        closing_rising=per_keeper_closing)

    # Cross-spec advisories: which threshold window a SoundWire Data input uses, and the
    # t_Slew_Data assumption.
    sw_rx = is_soundwire(inp.Man_spec) or is_soundwire(inp.Per_spec)
    caveats: list[str] = []
    if sw_rx:
        caveats = [THRESHOLD_NOTE, TRF_NOTE]
    if is_unratified(inp.Man_spec) or is_unratified(inp.Per_spec):
        # Prepended: an unratified number is the first thing a reader must know
        # about a result, ahead of any cross-spec modelling caveat.
        caveats = [PROPOSAL_NOTE, PROPOSAL_UI_RELATIVE_NOTE] + caveats
    if sw_rx:
        # The handover-UI allocation is a SWI3S schedule concept, so a bus with a SoundWire
        # side gets a contention margin only under an assumption worth stating: a mixed bus
        # takes the SWI3S side's allocation, a SoundWire pair the count as given.
        caveats = caveats + [HANDOVER_CROSS_SPEC_NOTE if inp.is_cross_spec()
                             else HANDOVER_SW_PAIR_NOTE]

    # Every breakdown is built in this call and not yet shared, so its run's supplies and edge
    # set are written in place; `replace` would gather its terms a second time.
    for b in breakdowns.values():
        object.__setattr__(b, "supplies", supplies)
        object.__setattr__(b, "fast_data_edge", inp.fast_data_edge)
    return CalcResults(
        inputs=inp,
        delta_cross_MP_setup_ns=delta_MP["setup", _mp_setup_rising],
        delta_cross_MP_hold_ns=delta_MP["hold", _mp_hold_rising],
        sigma_pm_setup_ns=sigma_setup[_pm_setup_launch["PM_setup"]],
        sigma_pm_hold_ns=sigma_hold[_pm_hold_launch["PM_hold"]],
        breakdowns=breakdowns,
        F_CLK_ceiling_MHz=inp.F_CLK_ceiling_MHz(),
        F_CLK_legal=inp.F_CLK_is_legal(),
        caveats=tuple(caveats),
    )


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

    ``is_window`` marks a row that must NOT be collapsed: collapsing is only sound
    for a parameter that ONE device realises.  A receiver
    threshold is a per-device corner -- a given part's rising threshold sits somewhere
    in [V_IH,min, V_IH,max] -- so on a leg that nets TWO receivers' crossings against
    each other, the worst case is one device at each end, which no single collapsed
    value can express.  A window row keeps both ends in force (``binds`` is applied
    as (min-field, max-field)) and the picker does not perturb it: the envelope already
    corners each ROLE at the end that role needs, so the intact window IS the
    independent per-device cornering rather than a refusal to search.

    Within a single receiver what matters is the V_IH-to-V_IL separation, which a
    collapse preserves; ACROSS two receivers it is the window's WIDTH, which a collapse
    destroys.

    ``linked_to``, if set, names another row in the same list whose pick
    this row mirrors.  Linked rows have their own min/typ/max numeric
    values (so the two lanes can be calibrated independently) but share a
    single corner pick — used to tie Man CLK and Man DATA t_RF so
    auto-worst can't put them at opposite spec edges on the same chip.
    One pick then moves the two lanes by their own ranges, so the direction it moves a
    margin depends on the other rows; a linked master is a slew row, searched jointly
    (`_joint_rows`). A `RowPoint` pick puts each linked row at the same position in its own
    range.
    """
    name: str
    binds: tuple[str, ...]
    range: ParamRange
    linked_to: str | None = None
    is_window: bool = False


def apply_row_picks(
    base: CalcInputs,
    rows: list[CalcRow],
    picks: Mapping[str, RowPick],
) -> CalcInputs:
    """Apply each row's picked value (`ParamRange.at`) to all of its bound CalcInputs fields.
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


# THE JOINT ROWS. The slews and τ set each edge's t_RF together (`edge_factor`, which keeps an
# edge inside its spec's range), the crossing terms multiply the coefficients α and δ set by
# those slews, and with a SoundWire side a launch's anchor correction (`_sw_anchor_slews`)
# credits the slews that the crossing terms charge. So the direction a slew, τ, α or δ row
# moves a margin can depend on the other rows' values, and α or δ can leave a margin smaller
# at typ than at either end. `find_worst_corner_rows_all` searches the rows bound to
# `_JOINT_FIELDS` together, over every combination of their picks: min, typ and max for τ, α
# and δ, and for a slew row the picks `_slew_picks` gives at that τ. Each leg's least
# combination is then refined along τ, α and δ inside their ranges (`_refine_fraction_row`),
# so their picks can be a `RowPoint` too.
_JOINT_FIELDS = frozenset({"tRF_Man_CLK_ns", "tRF_Man_DATA_ns", "tRF_Per_DATA_ns",
                           "tRF_tolerance_frac", "V_SEOS_tol_frac", "V_noise_pp_frac"})


def _joint_rows(rows: Sequence[CalcRow]) -> list[CalcRow]:
    """The master rows `find_worst_corner_rows_all` searches jointly: those bound to
    `_JOINT_FIELDS`. A linked row follows its master's pick, so it is searched with it."""
    return [r for r in rows if r.linked_to is None and not r.is_window
            and _JOINT_FIELDS.intersection(r.binds)]


_FIELD_LANES = {field: lane for lane, field in _LANE_FIELDS.items()}


def _slew_picks(inp: CalcInputs, rows: Sequence[CalcRow], master: CalcRow) -> list[RowPick]:
    """The picks of a joint master row the search evaluates at `inp`'s τ, α and δ: min and max,
    a `RowPoint` at every kink (`edge_kinks_ns`) of a lane the row or a row linked to it binds
    that falls strictly inside that row's range, and typ where a linked row's range is not
    the master's scaled about its typ, so that the lanes' values bend there. Between two
    consecutive picks every lane's edges are affine in the position. None for a row that binds
    no lane."""
    members = [master, *(r for r in rows if r.linked_to == master.name)]
    lanes = [(r, _FIELD_LANES[f]) for r in members for f in r.binds if f in _FIELD_LANES]
    if not lanes:
        return []
    m = master.range
    bends = any(not math.isclose((r.range.typ - r.range.min) * (m.max - m.typ),
                                 (r.range.max - r.range.typ) * (m.typ - m.min),
                                 rel_tol=1e-12, abs_tol=1e-12) for r in members[1:])
    ats = sorted({at for r, lane in lanes for k in edge_kinks_ns(inp, lane)
                  for at in r.range.positions(k)})
    picks: list[RowPick] = ["typ", "min", "max"] if bends else ["min", "max"]
    return picks + [RowPoint(at, m.at_position(at)) for at in ats]


def _pick_rank(p: RowPick) -> float:
    """A joint pick's place in the search's order, which settles a tie: typ, min, max, then a
    `RowPoint` by position."""
    return 3.0 + p.at if isinstance(p, RowPoint) else ("typ", "min", "max").index(p)


# `_refine_fraction_row`: the samples across a row's range (positions k / _REFINE_SAMPLES), and
# the parabolic steps that polish the least of them.
_REFINE_SAMPLES = 4
_REFINE_STEPS = 2


def _refine_fraction_row(row: CalcRow, i: int, best_m: float, best_c: tuple[RowPick, ...],
                         margin_of: Callable[[tuple[RowPick, ...]], float]
                         ) -> tuple[float, tuple[RowPick, ...]]:
    """`best_c` with the τ, α or δ row at index `i` moved inside its range where that leaves a
    smaller margin, and that margin (`find_worst_corner_rows`). The row is sampled at
    positions k / `_REFINE_SAMPLES` of its range (0 at min, 0.5 at typ, 1 at max), the others
    held; around the least sample inside the range, up to `_REFINE_STEPS` steps of parabolic
    interpolation through it and its two neighbours, each kept only where the parabola is
    convex and its vertex lies between them. A margin is the least of smooth cases, so it
    can turn at a case boundary as well as inside a case; the samples find the case, the
    steps its least point. No sample is taken with `_REFINE_SAMPLES` 0."""
    if _REFINE_SAMPLES <= 0:
        return best_m, best_c

    def at(pos: float) -> tuple[RowPick, ...]:
        value = row.range.at_position(pos)
        pick: RowPick = (("min", "typ", "max")[round(pos * 2)] if pos in (0.0, 0.5, 1.0)
                         else RowPoint(pos, value, f"at {100.0 * value:.2f} %"))
        return (*best_c[:i], pick, *best_c[i + 1:])

    points = {k / _REFINE_SAMPLES: margin_of(at(k / _REFINE_SAMPLES))
              for k in range(_REFINE_SAMPLES + 1)}
    for _ in range(_REFINE_STEPS):
        xs = sorted(points)
        j = min(range(len(xs)), key=lambda n: points[xs[n]])
        if j in (0, len(xs) - 1):
            break
        (x0, x1, x2) = xs[j - 1:j + 2]
        y0, y1, y2 = points[x0], points[x1], points[x2]
        den = (x1 - x0) * (y1 - y2) - (x1 - x2) * (y1 - y0)
        if not (y0 - y1) / (x0 - x1) < (y1 - y2) / (x1 - x2) or den == 0.0:
            break
        x = x1 - 0.5 * ((x1 - x0) ** 2 * (y1 - y2) - (x1 - x2) ** 2 * (y1 - y0)) / den
        if not x0 < x < x2 or min(abs(x - p) for p in points) < 1e-4:
            break
        points[x] = margin_of(at(x))
    pos, m = min(points.items(), key=lambda kv: kv[1])
    if m < best_m - 1e-12:
        return m, at(pos)
    return best_m, best_c


def find_worst_corner_rows(
    inequality: Inequality,
    rows: list[CalcRow],
    base: CalcInputs,
) -> dict[str, RowPick]:
    """For each MASTER row, pick min, typ or max, or for a slew row a point inside its range,
    to minimize the inequality's margin.  Linked rows aren't perturbed independently; their
    pick follows the master row.  Returns picks keyed by master row name only.

    The result is exact over the whole min/typ/max grid of the master rows, and over each slew
    row's whole range at every min/typ/max of τ, α and δ, and is then refined inside the τ, α
    and δ ranges (`_refine_fraction_row`): a slew row's pick, and a τ, α or δ pick, can be a
    point inside its range (`RowPoint`).

    A SINGLE ROW is perturbed on its own, against a base of typicals. That is exact under a
    condition every single row must meet: IT MOVES THE MARGIN IN ONE DIRECTION, WHATEVER THE
    OTHER ROWS' VALUES, so its worst end is the same for every combination of the others.

    THE JOINT ROWS (`_joint_rows`) are the slews, τ, α and δ, the direction each of which
    moves a margin depends on the others' values and on the range of a row linked to a slew
    row (`CalcRow.linked_to`). They are searched over every combination of their picks, every
    other row at typ. That is exact under one further condition: every other row changes the
    margin by an amount the joint rows do not change.

    A SLEW ROW IS SEARCHED OVER ITS WHOLE RANGE. At given τ, α and δ every case of a leg is
    affine in each edge's t_RF, or the worse of affine alternatives (checklist rule 4), and
    `edge_factor` makes each edge's t_RF piecewise affine in
    the row value, so a margin can be least at a kink inside the range (`edge_kinks_ns`), where
    an edge reaches an end of its spec's range and stops while the other edge's t_RF turns
    back. The row's picks are its range's ends and its kinks (`_slew_picks`), and the cases
    are summed over different lanes, so the product of the slew rows' picks holds the least
    margin over their ranges. Typ is not among them unless a linked row bends there; a tie is
    then resolved to typ by evaluating it.

    τ, α AND δ ARE REFINED INSIDE THEIR RANGES. A crossing coefficient is not affine in α or δ,
    nor a kink's place in τ, so a margin can be least between two of their picks. Each leg's
    best combination is searched further along each of the three in turn, the others held
    (`_refine_fraction_row`): sampled at quarters of its range, and the least sample polished
    by parabolic interpolation. A point is kept only where its margin is smaller, so the
    refinement never reports a better margin than the grid's. A minimum narrower than a
    quarter of the range that no sample falls in can still be missed.

    A MIN of two functions that move a row in different directions violates the single-row
    condition: the margin is flat at the typ base wherever the other branch binds.  So a NEW
    inequality that is not monotone in this sense is SPLIT into monotone parts, the way the
    keeper is.  The min over the eight supply assignments `compute` takes is exempt: every
    assignment moves a row the same way, and the brute-force tests confirm it. So is its min
    over the slow and fast data-edge sets (`CalcInputs.fast_data_edge`): the two differ only in
    the DATA lanes' slew factors, which the joint rows set.

    WINDOW ROWS ARE NOT PERTURBED and get no pick, which is not a gap in the search.
    Their two ends are BOTH in force, and the envelope reads whichever end each role
    needs, so the worst corner over two receivers' thresholds is already the value being
    evaluated -- perturbing such a row could only narrow the window and report a better
    margin than the specification allows.  See `CalcRow.is_window`.
    """
    return find_worst_corner_rows_all((inequality,), rows, base)[inequality]


def find_worst_corner_rows_all(
    inequalities: Sequence[Inequality],
    rows: list[CalcRow],
    base: CalcInputs,
) -> dict[Inequality, dict[str, RowPick]]:
    """`find_worst_corner_rows` for each of `inequalities`, from one evaluation of each set of
    picks: a set's inputs are the same for every inequality, and two sets that give the same
    inputs are evaluated once. A set is evaluated for its margins only (`_MARGINS_ONLY`)."""
    token = _MARGINS_ONLY.set(True)
    try:
        return _worst_corner_rows_all(inequalities, rows, base)
    finally:
        _MARGINS_ONLY.reset(token)


def _worst_corner_rows_all(
    inequalities: Sequence[Inequality],
    rows: list[CalcRow],
    base: CalcInputs,
) -> dict[Inequality, dict[str, RowPick]]:
    joint = _joint_rows(rows)
    joint_names = [r.name for r in joint]
    masters = [r for r in rows if r.linked_to is None and not r.is_window]
    single = [r for r in masters if r.name not in joint_names]
    base_picks: dict[str, RowPick] = {row.name: "typ" for row in masters}
    out: dict[Inequality, dict[str, RowPick]] = {leg: {} for leg in inequalities}
    evaluated: dict[CalcInputs, CalcResults] = {}

    def run(picks: Mapping[str, RowPick]) -> CalcResults:
        inp = apply_row_picks(base, rows, picks)
        if inp not in evaluated:
            evaluated[inp] = compute(inp)
        return evaluated[inp]

    for row in single:
        results: dict[Pick, CalcResults] = {p: run({**base_picks, row.name: p})
                                            for p in ("typ", "min", "max")}
        for leg in inequalities:
            margins = {p: r.breakdowns[leg].margin_ns for p, r in results.items()}
            smallest = min(margins.values())
            # Ties go to typ, so an insensitive row is not reported as if its corner
            # had been chosen for a reason.
            if math.isclose(margins["typ"], smallest, abs_tol=1e-12):
                out[leg][row.name] = "typ"
            else:
                out[leg][row.name] = "min" if margins["min"] <= margins["max"] else "max"
    if not joint:
        return out
    every: tuple[Pick, ...] = ("typ", "min", "max")
    slews = [i for i, r in enumerate(joint) if _slew_picks(base, rows, r)]
    others = [i for i in range(len(joint)) if i not in slews]
    combos: list[tuple[RowPick, ...]] = []
    for other in itertools.product(every, repeat=len(others)):
        at = apply_row_picks(base, rows, {**base_picks,
                                          **{joint_names[i]: p for i, p in zip(others, other)}})
        for slew in itertools.product(*(_slew_picks(at, rows, joint[i]) for i in slews)):
            by_index: dict[int, RowPick] = {**dict(zip(others, other)),
                                            **dict(zip(slews, slew))}
            combos.append(tuple(by_index[i] for i in range(len(joint))))
    combos.sort(key=lambda c: tuple(_pick_rank(p) for p in c))

    def margin(leg: Inequality, combo: Sequence[RowPick]) -> float:
        return run({**base_picks, **dict(zip(joint_names, combo))}).breakdowns[leg].margin_ns

    grid = [run({**base_picks, **dict(zip(joint_names, combo))}) for combo in combos]
    for leg in inequalities:
        best_m, best_c = math.inf, combos[0]
        for combo, r in zip(combos, grid):
            m = r.breakdowns[leg].margin_ns
            # Ties go to the combination with more rows at typ, as for a single row.
            if math.isclose(m, best_m, abs_tol=1e-12):
                if combo.count("typ") > best_c.count("typ"):
                    best_c = combo
            elif m < best_m:
                best_m, best_c = m, combo
        for i in others:
            best_m, best_c = _refine_fraction_row(joint[i], i, best_m, best_c,
                                                  functools.partial(margin, leg))
        # A slew row the leg does not move ties at typ, which its picks need not include.
        for i in slews:
            if best_c[i] != "typ":
                trial = (*best_c[:i], "typ", *best_c[i + 1:])
                m = margin(leg, trial)
                if m <= best_m + 1e-12:
                    best_m, best_c = min(m, best_m), trial
        out[leg].update(zip(joint_names, best_c))
    return out


def default_swi3s_rows(lock_mgr_lanes: bool = True) -> list[CalcRow]:
    """Every parameter the calculator UI exposes, as a row.

    Ranged spec parameters (Man/Per_t_DD) bind to BOTH the min and max
    fields of CalcInputs — picking a value collapses the band to that value.
    The receiver thresholds V_IH and V_IL are window rows (`CalcRow.is_window`):
    they set their two fields to the row's two ends and take no pick.
    Single-bound spec parameters (t_IS, t_IH, t_DZ, t_ZD) bind to one field;
    the picker selects min/typ/max for what-if exploration.  Operating-point parameters
    (α, δ, τ, t_RF lanes, bus geometry) bind to one field each with the natural corner
    range.

    When ``lock_mgr_lanes`` is True (default), the Man DATA t_RF row links
    its corner pick to the Man CLK row, so auto-worst can't drive the
    same Mgr chip's CLK and DATA lanes to opposite spec edges.  Numeric
    min/typ/max values for the two lanes remain independently editable.
    """
    return [
        # Receiver thresholds: a COMPLIANCE WINDOW, kept as one. Each device's own
        # rising/falling threshold is a corner of THAT device somewhere inside it, so
        # two receivers can sit at opposite ends at the same time — which is the worst
        # case on every leg that nets two peripherals' crossings. A collapsed pick would
        # assert one threshold for the whole bus.
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
        # keeper).
        CalcRow("Man_t_DZ", ("Man_t_DZ_max_ns",),
                ParamRange(0.0, 5.0, 10.0)),
        CalcRow("Per_t_DZ", ("Per_t_DZ_max_ns",),
                ParamRange(0.0, 5.0, 10.0)),
        CalcRow("Man_t_ZD", ("Man_t_ZD_ns",),
                ParamRange(2.0, 10.0, 18.0)),
        CalcRow("Per_t_ZD", ("Per_t_ZD_ns",),
                ParamRange(2.0, 10.0, 18.0)),
        # EDE Delay: how long an EndDriveEarly driver holds its level after its ramp's
        # 80 % point. Read only by a side that uses EndDriveEarly, in place of its t_DZ.
        CalcRow("Man_t_EDE", ("Man_t_EDE_ns",),
                ParamRange(3.0, 5.0, 7.0)),
        CalcRow("Per_t_EDE", ("Per_t_EDE_ns",),
                ParamRange(3.0, 5.0, 7.0)),

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
        # (t_PD funds PM hold, so the short end is the one that binds).
        CalcRow("bus length (cm)",     ("bus_length_cm",),
                ParamRange(0.0, 15.0, 30.0)),
        CalcRow("t_PD,mis fraction",   ("vPCB_err_frac",),
                ParamRange(0.0, 0.05, 0.10)),
    ]
