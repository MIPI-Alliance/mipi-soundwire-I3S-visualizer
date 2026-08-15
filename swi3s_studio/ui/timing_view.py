"""Timing mode — a Qt port of the standalone SWI3S Spec Calculator
(`timing-analysis/app/spec_calculator.py`), covering both forwarded-clock
single-ended PHYs.

A per-side **spec selector** picks what each end of the bus is built to: SWI3S
PHY1, SWI3S PHY2, or SoundWire 1.3 at either supply. The Manager and Peripheral
choose independently, so the page covers a single-spec bus and a mixed
SWI3S ↔ SoundWire one with the same controls, each side contributing the timing
its own spec promises.

PHY1 (FBCSE-slow) and PHY2 (FBCSE-fast) share the setup/hold inequality
structure exactly — the SWI3S spec documents them together (§11.2) and points
both at the same RX/TX timing tables and measurement waveforms — so only the
Specification values differ (PHY1's Peripheral/Manager high-Z and output-hold
times are slower; its mandatory F_CLK is half). They are two spec sources rather
than a separate PHY switch, because "which spec is this side built to" is one
question, not two: a PHY1 Manager talking to a PHY2 Peripheral is expressible,
and a global PHY switch could not say it.

The inequalities themselves are unchanged across all of this — every spec here
describes the same physical event graph — but a tabulated clock-to-output number
means different things on the two sides (SoundWire t_OV is anchored at the END
of the slew, a full t_RF later than SWI3S t_DD), so it is normalised per side.
See `swi3s_studio.timing.spec_source`. Selecting a SoundWire side also surfaces
two advisories that qualify the margins without changing them: the f_Clock
ceiling (no SoundWire envelope reaches SWI3S PHY2's mandatory 13.2 MHz) and the
cost of the hysteresis SoundWire does not guarantee on its Data receiver.

The Specification column is read-only; only the Example column is editable.
Where a spec has a proposed revision, the Example column seeds from it, so the
page opens on a direct current-vs-proposed comparison.

Compare the SWI3S specification against an example implementation. Corners are
selected automatically — each inequality is evaluated at its own worst corner —
so there is no manual corner picker. The page:

- A fixed title with the selected bus named beneath it, then a top control bar:
  per-side spec selector, SoundWire system-size envelope, Manager data-launch
  mode, F_CLK target, Reset, and Save / Load of the Example column (YAML, or
  JSON if PyYAML is absent).

  The default F_CLK target is **12.288 MHz** (256 × 48 kHz, the audio design
  point) clamped to whatever the selected pair may legally clock — not PHY2's
  13.2 MHz mandatory maximum, which is a worst-case corner no design targets.

  The **launch mode** is an architectural choice, not a tighter number. An analog
  delay line gives Man_t_DD as a range, and the inequalities absorb its
  min-to-max spread on both MP legs at once — the max erodes setup while only the
  min funds hold — so tightening the range cannot improve both. A clock-launched
  edge (2x or 4x) lands on a known clock phase, so min == max and the spread
  disappears.

  It governs all three Manager output timings together — t_DD, t_ZD and t_DZ —
  because one Manager has one output stage: if its data edge comes from a
  multiplied clock then it HAS that clock, and its output enable and disable come
  from it too. A clocked t_DD beside an analog t_ZD and t_DZ is not a conservative
  model, it is two different devices.

  t_DD and t_ZD are in fact the two DATA LAUNCH METHODS — from an already-driving
  output, or out of high-Z — and both must satisfy the same setup and hold
  requirements at the receiver, which is why the revision states them identically
  ("9–23 ns or (0.5 UI 2x CLK, 0.25 UI 4x CLK)" for each) rather than merely
  similarly. What the mode dictates is that they are clocked and at what
  RESOLUTION: a 4x clock offers 0.00, 0.25, 0.50 and 0.75 UI, a 2x clock only 0.00
  and 0.50. So t_DD is fixed at the placement the selector names, and t_ZD / t_DZ
  stay editable, snapped to a point the selected clock can actually place. On the
  EDE column only t_DZ is exempt, because EDE is a redefinition of t_DZ and says
  nothing about the launch methods.

  The two clock options are bounded from
  opposite ends (4x by hold, 2x by setup), so which is viable depends on F_CLK —
  the hint beneath the selector reports that bound at the current rate.
- A "Timing Inequality Results" tree: one always-expanded row per inequality
  (MP/PM × setup/hold), each evaluated at *its own* worst corner, with the
  symbolic equation in the header and Specification / Example child rows showing
  the numeric substitution, Margin, and Max Frequency (setup inequalities only) —
  failing margins / F_max coloured red.
- One Specification-vs-Example parameter grid per section (shared name column,
  Spec and Example min/max side by side, then the unit).

All compute lives in `swi3s_studio.timing` (ported verbatim from the standalone
timing-analysis app and validated against its reference spreadsheets); this view
only edits CalcRow ranges and formats the CalcResults.
"""
from __future__ import annotations

import math
import re
from typing import Dict, List, Optional, Tuple

from PySide6.QtCore import QPointF, QSize, Qt, Signal
from PySide6.QtGui import (
    QAbstractTextDocumentLayout,
    QBrush,
    QColor,
    QFont,
    QPalette,
    QTextDocument,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QAbstractSpinBox,
    QApplication,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..timing import (
    INEQUALITIES,
    PP_PLACEMENT_LABELS,
    CalcInputs,
    CalcRow,
    ParamRange,
    apply_row_picks,
    compute,
    default_swi3s_rows,
    find_worst_corner_rows,
)
from ..timing.spec_source import (
    LAUNCH_LABELS,
    SPEC_LABELS,
    SPEC_SOURCES,
    LaunchMode,
    SpecSource,
    SystemSize,
    default_handover_UIs,
    ede_release_ceiling_ns,
    is_ede,
    is_proposed,
    is_soundwire,
    launch_grid_UI,
    launch_mode_max_fclk_MHz,
    launch_mode_tDD_ns,
    preset,
    snap_to_launch_grid,
)
from .theme import VizTheme, analyzer_stylesheet

try:
    import yaml
    _HAVE_YAML = True
except Exception:                                  # pragma: no cover - env dependent
    _HAVE_YAML = False

SIDES = ("spec", "proposed")

# Section layout — group default_swi3s_rows() into named boxes (mirrors the
# Streamlit app's SECTIONS).
SECTIONS: List[Tuple[str, List[str]]] = [
    ("RX Thresholds", ["V_IH", "V_IL"]),
    ("TX Timing", ["Man_t_DD", "Per_t_DD"]),
    ("RX Timing", ["Man_t_IS", "Man_t_IH", "Per_t_IS", "Per_t_IH"]),
    ("Handover Timing", ["Man_t_DZ", "Per_t_DZ", "Man_t_ZD", "Per_t_ZD"]),
    ("Rise/Fall Times", ["t_RF Man CLK", "t_RF Man DATA", "t_RF Per DATA"]),
    ("Noise / Supply / Rise Fall Time Tolerance",
     ["δ (V_SEOS tol)", "α (V_noise / V_SEOS)", "τ (per-edge slew)"]),
    ("Bus Properties", ["bus length (cm)", "t_PD,mis fraction"]),
]

# All rows as masters (lock off), used to build the widgets and seed values.
# default_swi3s_rows() carries the PHY2 spec values; PHY1 overrides a handful below.
_ALL_ROWS: List[CalcRow] = default_swi3s_rows(lock_mgr_lanes=False)
_ROW_BY_NAME: Dict[str, CalcRow] = {r.name: r for r in _ALL_ROWS}

# PHY1 (FBCSE-slow) shares PHY2's inequality structure exactly — the SWI3S spec
# (v1.1r06 §11.2) documents PHY1 & PHY2 together and points both at the SAME
# tables (Table 128 RX setup/hold, Table 129 TX timing, Table 130 EndDriveEarly)
# and the SAME setup/hold measurement waveforms (Figures 174–179).
#
# But the two tables are structured DIFFERENTLY, and the difference matters when
# reasoning about a revision rather than just reading a number:
#
#   Table 128 (INPUT: Man/Per_tIS, tIH) is genuinely shared — ONE row per
#   parameter, no PHY1/PHY2 distinction. (r06 renamed these with a `_Min`
#   suffix: Per_tIS_Min etc., a max on the min a datasheet may publish.)
#
#   Table 129 (OUTPUT) prints EVERY parameter as a separate PHY1 row and PHY2
#   row and NEVER collapses them. Three rows differ; three agree:
#       differ:  Per_tDD 14-50/2-20 · Per_tZD 14-50/2-18 · Man_tZD 14-30/2-18
#       agree:   Man_tDD 7-18 · Man_tDZ 0-10 · Per_tDZ 0-10
#   Only the differing rows need an override, so only they appear below — but
#   the agreeing ones are two coincident rows, NOT one shared value. Do not
#   reason from "Table 129 doesn't split it": it splits everything. If a future
#   revision diverges Man_tDD, this dict is where PHY1's row belongs.
#
# Everything else (V thresholds §11.2.2, t_RF Table 127 — a single PHY1&2 table,
# noise, bus geometry) is shared or is not a device-spec value at all.
_PHY1_SPEC: Dict[str, ParamRange] = {
    # Table 129 §11.2.3 — the PHY1 rows that DIFFER from PHY2 (PHY2 values, for
    # contrast, live in the default rows).
    "Per_t_DD": ParamRange(14.0, 32.0, 50.0),   # Per_tDD  14–50 ns  (PHY2 2–20)
    "Per_t_ZD": ParamRange(14.0, 32.0, 50.0),   # Per_tZD  14–50 ns  (PHY2 2–18)
    "Man_t_ZD": ParamRange(14.0, 22.0, 30.0),   # Man_tZD  14–30 ns  (PHY2 2–18)
}
# PROPOSED revision, as ranges. Seeds the editable Example column so the page
# opens on a current-vs-proposed comparison (see _reset_to_defaults).
#
# Held here as ParamRanges rather than read from SpecPreset because the preset
# carries a SINGLE value for t_ZD and t_IH — enough for SoundWire, where they are
# single-valued, but the SWI3S proposal revises them as ranges. Taking them from
# the preset collapsed min onto max (Per_t_IH became 1.5-1.5 instead of 0-1.5).
_PHY2_PROPOSED: Dict[str, ParamRange] = {
    "Man_t_DD": ParamRange(9.0, 16.0, 23.0),    # 7-18   -> 9-23
    "Man_t_ZD": ParamRange(9.0, 16.0, 23.0),    # 2-18   -> 9-23 (tracks Man_t_DD)
    "Per_t_DD": ParamRange(2.0, 5.5, 9.0),      # 2-20   -> 2-9
    "Per_t_ZD": ParamRange(2.0, 5.5, 9.0),      # 2-18   -> 2-9  (tracks Per_t_DD)
    "Per_t_IH": ParamRange(0.0, 0.75, 1.5),     # 0-4.5  -> 0-1.5
}

# The subset of the proposal that also applies to a PHY1 side.
#
# The split follows how the SPEC IS STRUCTURED, not which numbers happen to
# coincide (v1.1r06, verified against Tables 128/129):
#
#   Table 128 (INPUT) is genuinely shared — one row per parameter, no PHY1/PHY2
#   distinction at all. Revising Per_tIH 0-4.5 -> 0-1.5 therefore revises it for
#   BOTH PHYs; there is no PHY1 row left holding 4.5. It carries.
#
#   Table 129 (OUTPUT) prints EVERY parameter as a separate PHY1 row and PHY2
#   row — it never collapses the two. So a revision to PHY2's row says nothing
#   about PHY1's, even where the two currently hold the same numbers:
#     Per_tDD  PHY1 14-50 vs PHY2 2-20   |  Man_tDD PHY1 7-18 == PHY2 7-18
#     Per_tZD  PHY1 14-50 vs PHY2 2-18   |  Man_tDZ 0-10      == 0-10
#     Man_tZD  PHY1 14-30 vs PHY2 2-18   |  Per_tDZ 0-10      == 0-10
#
# Man_t_DD is in the second column — two rows that agree today. Carrying the
# 9-23 revision onto PHY1 is a DECISION (the rows are identical, so revising
# them identically is coherent), not something Table 129 implies.
#
# Per_t_DD and Per_t_ZD are in the FIRST column, and get PHY1's OWN revised
# maximum: 50 -> 44 ns, supplied for PHY1 rather than derived. That is the point
# of keeping them out of the PHY2 carry-over — pasting PHY2's proposed 9 ns onto
# a row whose PHY1 value is 50 would have invented a revision nobody wrote,
# whereas 44 is a number stated for this PHY. The minimum is untouched at 14.
_PHY1_PROPOSED: Dict[str, ParamRange] = {
    "Man_t_DD": _PHY2_PROPOSED["Man_t_DD"],      # Table 129, per-PHY rows that agree
    "Per_t_IH": _PHY2_PROPOSED["Per_t_IH"],      # Table 128, one shared row
    "Per_t_DD": ParamRange(14.0, 29.0, 44.0),    # PHY1 14-50 -> 14-44
    "Per_t_ZD": ParamRange(14.0, 29.0, 44.0),    # PHY1 14-50 -> 14-44 (tracks Per_t_DD)
}

# The EDE candidate, as a function of UI because the Manager's values are UI
# fractions.  See `_ede_ranges` for the placement argument and for what this column
# used to carry.
#
# CLOCK RESOLUTION CONSTRAINS THE GRID, and the keeper is what sets the multiplier:
# the launch and the release must clear one full rail-to-rail swing PLUS the keeper's
# response time (13.83 + 3.00 = 16.83 ns at the slow corner, 0.46 UI), which on a
# 1/4 UI grid means TWO steps. So:
#
#     Man_t_DD  0.25 UI     the launch
#     Man_t_ZD  0.25 UI     the other launch method, same point
#     Man_t_DZ  0.75 UI     the EDE release, two steps on
#
# A 1/2 UI grid DOES contain a pair 16.83 ns apart -- 0.50 and 1.00 UI -- so the keeper
# is not what rules 2x out. Table 130's ceiling is (0.60 UI + 10 ns = 0.873 UI here),
# together with the definition of an early release: the boundary is neither, and with it
# excluded a 1/2 UI grid has no point left above the launch. An earlier version of this
# column had the launch at 0.50 UI and read as passing only because the keeper leg
# charged t_keeper (3 ns) where it owes the whole swing (13.83): two ten-nanosecond
# errors cancelling.
#
# The Manager's t_DZ is referenced to the START of its last driven UI, which is
# Table 130's EDE convention; `CalcInputs.Man_ede` shifts it onto the end-of-UI
# footing the inequalities use, so it is POSITIVE here. The PERIPHERAL's release is
# not a value on this grid and is not a row: it is an interval from its own ramp
# completion, carried by `CalcInputs.Per_ede_hold_*`.
_EDE_TURN_MIN, _EDE_TURN_MAX = 3.0, 9.0     # 3:1 analog turn-on / turn-off

# The EDE rows that are UI FRACTIONS rather than absolute nanoseconds. These must
# follow F_CLK: "0.25 UI" is the definition, so an absolute value left over from
# another rate is not stale-but-usable, it is wrong. Refreshed by
# `_refresh_ede_rate_rows`. The rest of the EDE column (Per_t_DD, Per_t_ZD,
# Per_t_IH) is absolute and is left alone, including any user edit.
#
# Per_t_DZ is NOT here any more: the peripheral's EDE release is no longer expressed
# as a t_DZ value, so there is nothing rate-relative about that row.
_EDE_UI_RELATIVE_ROWS = ("Man_t_DD", "Man_t_ZD", "Man_t_DZ")


def _UI_ns(f_clk_MHz: float, duty_min: float = 0.45) -> float:
    """UI at the duty floor, matching CalcInputs.UI_ns."""
    return 1000.0 * duty_min / f_clk_MHz if f_clk_MHz > 0 else 0.0


def _ede_ranges(UI_ns: float) -> Dict[str, ParamRange]:
    """EDE proposed values at this UI, for BOTH mechanisms.

    The two device classes reach EDE differently, because a device can only release
    early against a reference it can actually see:

        Manager     sources the clock, so a 4x multiple of it places any quarter-UI
                    instant. Launch 0.25 UI, release 0.75 UI -- clocked, hence
                    deterministic, hence min == max.

        Peripheral  has no mid-UI reference (Sec. 11.1.5), so it cannot aim at a
                    clock instant at all. It holds its own COMPLETED level for
                    3-9 ns and then releases. That interval is
                    `CalcInputs.Per_ede_hold_*`, NOT a row here -- there is no
                    clock-referenced number that expresses it.

    WHY THE MANAGER'S TWO POINTS ARE TWO GRID STEPS APART, which is what sets the
    multiplier. The keeper needs the driver's whole swing to finish AND its own response
    time of settled level before drive stops, and at the slow corner that is
    13.83 + 3.00 = 16.83 ns, or 0.46 UI -- so launch and release must be at least that
    far apart, which on a 1/4 UI grid means TWO steps. Sweeping all six ordered pairs the
    grid offers leaves exactly one that also clears P->M contention and hold: 0.25 / 0.75.

    A 2x clock therefore cannot express EDE -- but NOT because of the keeper, which
    0.50 -> 1.00 UI satisfies. Its only interior point is 0.50 UI, which cannot be both
    the launch and the release; 0.00 UI fails hold; and 1.00 UI is the boundary, which
    Table 130's 0.60 UI + 10 ns ceiling forbids and which is not an early release
    anyway. `calculator._man_tdz_parts` enforces that ceiling, so selecting 2x here
    reports a failing keeper leg rather than a green column with an illegal release.

    Two things this used to carry, both retired:

      * a launch at 0.50 UI, which fails the keeper by 4.68 ns once the addend is the
        full swing rather than t_keeper. It looked fine only because the two errors
        cancelled.
      * `Per_t_DZ = ParamRange(UI/3, 2UI/3, UI)` -- a 3:1 turn-off ratio applied to a
        POSITION in the UI rather than to a delay. Nothing derived it, it credited the
        peripheral with a release spread several times the parameter's whole range,
        and because it was UI-scaled it manufactured a rate dependence. The
        peripheral's release is no longer a row at all.
    """
    q, threeq = 0.25 * UI_ns, 0.75 * UI_ns
    return {
        # The launch, on the 0.25 UI grid point. Deterministic: a clocked edge
        # carries no PVT spread.
        "Man_t_DD": ParamRange(q, q, q),
        # The OTHER launch method, same point and same value. t_DD and t_ZD both
        # launch a data edge -- from an already-driving output, or out of high-Z --
        # and both must satisfy the same setup and hold requirements, so the
        # revision states them identically.
        "Man_t_ZD": ParamRange(q, q, q),
        # The EDE release, two grid steps later, which is what gives the keeper the
        # whole swing and is why a 4x clock is needed.
        "Man_t_DZ": ParamRange(threeq, threeq, threeq),
        # Peripheral: the revision's own values, unchanged.
        "Per_t_DD": ParamRange(2.0, 5.5, 9.0),
        # ALSO UNCHANGED, and that is a finding rather than an omission.
        #
        # The EDE paper asks for Per_t_ZD,min to rise from 2 to 6 ns, to close P->P
        # hold at a zero-UI handover. Seeded as 6-9 that is a 1.5:1 range, and a
        # peripheral has NO CLOCK -- its t_ZD is analog end to end, so PVT gives
        # about 3:1. A 1.5:1 part is not buildable, which
        # `test_ede_peripheral_values_are_not_tighter_than_3_to_1` correctly rejects.
        #
        # Made buildable the ask does not survive: at 3:1 the max moves with the min
        # to 6-18 ns, and the setup legs read the MAX --
        #
        #     Per_t_ZD 2-6      P->P hold -3.60   P->P setup +2.46
        #     Per_t_ZD 6-18     P->P hold +0.40   P->P setup -9.54
        #
        # -- so the two legs pull this one parameter from opposite ends. Hold wants
        # the min >= 5.60 ns; P->P setup wants the max <= 8.46 ns; the window is
        # 1.51:1 where a PVT part needs 3:1. It is EMPTY.
        #
        # So P->P hold is the one leg this proposal does not close with a buildable
        # peripheral, and the column says so by seeding the revision's own value
        # rather than an unbuildable one. Raised in docs/MODEL_AUDIT.md.
        "Per_t_ZD": ParamRange(2.0, 5.5, 9.0),
        "Per_t_IH": ParamRange(0.0, 0.75, 1.5),
    }



_PROPOSED_BY_SPEC: Dict[str, Dict[str, ParamRange]] = {
    "SWI3S_PHY2": _PHY2_PROPOSED,
    "SWI3S_PHY1": _PHY1_PROPOSED,
}

# Mandatory max F_CLK is 6.6 MHz (PHY1) / 13.2 MHz (PHY2) — Table 126. The spec
# gives no nominal, so these are TARGETS to open on, not limits: the limit is
# `fclk_ceiling_MHz`, which clamps whatever is seeded here.
#
# PHY2 opens on 12.288 MHz — 256 x 48 kHz, the audio design point — rather than
# the 13.2 MHz mandatory maximum. 13.2 is a worst-case corner nobody targets, and
# opening there made every margin read as the answer at a rate the design does not
# run at. PHY1 keeps 6.4, which matches the SE reference spreadsheet.
_SPEC_FCLK_TARGET: Dict[str, float] = {"SWI3S_PHY1": 6.4, "SWI3S_PHY2": 12.288}
_FCLK_TARGET_DEFAULT = 12.288


def _phy_of(spec: SpecSource) -> int:
    """Which SWI3S PHY a spec source describes (2 for anything else).

    Only used to pick between the shared PHY1&2 tables and PHY1's three
    Table-129 overrides, all of which are side-specific rows — so the value is
    irrelevant for a SoundWire side and for the shared rows (thresholds, noise,
    bus geometry).
    """
    return 1 if spec == "SWI3S_PHY1" else 2


def _spec_range(name: str, phy: int) -> ParamRange:
    """The spec min/typ/max for a parameter on the selected PHY (PHY1 overrides a
    few Table-129 output-timing rows; everything else is the shared PHY1&2 value)."""
    if phy == 1 and name in _PHY1_SPEC:
        return _PHY1_SPEC[name]
    return _ROW_BY_NAME[name].range


# Rows that belong to one side of the bus, and which SpecPreset field seeds them.
# When a side is switched to SoundWire these are reseeded from that spec's tables;
# everything else (thresholds, noise, bus geometry) is a property of the bus or of
# the shared modelling convention, not of one device's spec.
_MAN_ROW_FIELDS: Dict[str, Tuple[str, str]] = {
    "Man_t_DD": ("t_DD_min_ns", "t_DD_max_ns"),
    "Man_t_IS": ("t_IS_max_ns", "t_IS_max_ns"),
    "Man_t_IH": ("t_IH_max_ns", "t_IH_max_ns"),
    "Man_t_DZ": ("t_DZ_max_ns", "t_DZ_max_ns"),
    "Man_t_ZD": ("t_ZD_ns", "t_ZD_ns"),
}
_PER_ROW_FIELDS: Dict[str, Tuple[str, str]] = {
    "Per_t_DD": ("t_DD_min_ns", "t_DD_max_ns"),
    "Per_t_IS": ("t_IS_max_ns", "t_IS_max_ns"),
    "Per_t_IH": ("t_IH_max_ns", "t_IH_max_ns"),
    "Per_t_DZ": ("t_DZ_max_ns", "t_DZ_max_ns"),
    "Per_t_ZD": ("t_ZD_ns", "t_ZD_ns"),
}
# t_RF rows follow their side's spec too — SoundWire's tabulated slew differs
# from SWI3S's 5 ns reference, and it is what the anchor conversion scales.
_MAN_TRF_ROWS = ("t_RF Man CLK", "t_RF Man DATA")
_PER_TRF_ROWS = ("t_RF Per DATA",)


def _row_range_for_spec(name: str, spec: SpecSource, side: str,
                        system_size: SystemSize) -> Optional[ParamRange]:
    """Spec min/typ/max for a per-side row, or None if the row isn't side-specific.

    Driven by the preset rather than by which spec family it is, so any source
    with its own tabulated numbers reseeds: a SoundWire side from Tables 16/17
    at the selected envelope, a proposed SWI3S revision from its proposal
    values. Stock SWI3S PHY1/PHY2 return None so the caller falls back to the
    PHY-table path, which is the validated single-spec behaviour — and which is
    NOT interchangeable with the preset path: `SpecPreset` holds one value for
    t_IS/t_IH/t_ZD, so routing SWI3S through it would collapse those rows' min
    onto their max.
    """
    if not (is_soundwire(spec) or is_proposed(spec)):
        return None                       # caller falls back to _spec_range()
    p = preset(spec, "Man" if side == "Man" else "Per", system_size)
    fields = _MAN_ROW_FIELDS if side == "Man" else _PER_ROW_FIELDS
    if name in fields:
        lo, hi = (getattr(p, f) for f in fields[name])
        return ParamRange(lo, 0.5 * (lo + hi), hi)
    if name in (_MAN_TRF_ROWS if side == "Man" else _PER_TRF_ROWS):
        if is_proposed(spec):
            return None                   # the proposal does not revise t_RF
        # SoundWire tabulates t_Slew_Clock 2.0..max; t_Slew_Data has no max, so
        # the clock bound stands in for it (an assumption, surfaced in the caveats).
        return ParamRange(2.0, 0.5 * (2.0 + p.tRF_ns), p.tRF_ns)
    return None


def _side_of_row(name: str) -> Optional[str]:
    if name in _MAN_ROW_FIELDS or name in _MAN_TRF_ROWS:
        return "Man"
    if name in _PER_ROW_FIELDS or name in _PER_TRF_ROWS:
        return "Per"
    return None

_RED = QColor(VizTheme.SEM_ERROR)
_FMAX_COLS = ["Inequality", "Spec margin (ns)", "Example margin (ns)", "ΔMargin (ns)",
              "Spec F_max (MHz)", "Example F_max (MHz)", "ΔF_max (MHz)"]

# Uniform scale for the whole Timing page (fonts AND fixed pixel dimensions scale
# together, so proportions and alignment are preserved). Scoped to this view.
_S = 1.25


def _px(n: float) -> int:
    return round(n * _S)


_FONT_BODY = _px(12)     # matches the theme body size, scaled
_FONT_HEADER = _px(15)   # section headers (theme HEADER_CSS, scaled)
_FONT_TITLE = _px(18)    # page title (theme TITLE_CSS, scaled)
_HEADER_CSS = f"font-size:{_FONT_HEADER}px; font-weight:600;"
_TITLE_CSS = f"font-size:{_FONT_TITLE}px; font-weight:600;"

# Fixed widths (scaled) so the Spec/Example boxes line up vertically across every
# section regardless of the longest label / unit text in any one section.
_NAME_COL_W = _px(150)
_BOX_W = _px(54)        # min/max spin width — fixed so columns align across sections
_UNIT_COL_W = _px(52)   # unit column width — fits the widest unit ("·V_DD")
_GAP_W = _px(40)        # gap between the Spec and Example groups
_HEAD_INDENT = _px(4)   # ~½ char: nudge titles/top-bar in to sit under the card borders
_LABEL_CELL_W = _px(96)  # Spec/Example label cell — numbers align under each other
_COMBO_W = _px(140)     # ramp-model combo width (fits "exponential")
_SECTION_GAP = _px(14)  # vertical gap between sections


def _scaled_stylesheet() -> str:
    """analyzer + spin stylesheet with the pinned 12px body font scaled up."""
    sheet = analyzer_stylesheet() + _spin_qss()
    return re.sub(r"font-size:\s*12px", f"font-size:{_FONT_BODY}px", sheet)


def _base_font() -> QFont:
    """The view's base font (scaled), so unstyled widgets — combos, buttons — and
    the tree delegate's text scale too."""
    f = QFont()
    f.setPixelSize(_FONT_BODY)
    return f

# Full-word inequality titles, keyed by INEQUALITIES id (a heading key is the id of
# its first member — same pattern as the setup/hold headings, which own their "_ho"
# sibling).
#
# NAMED FOR WHO OWES WHAT TO WHOM, not for a direction arrow. "Manager-to-Peripheral
# Hold" states the data direction and leaves the obligation to be inferred, and the
# inference is the part a reader gets wrong: every one of these legs pairs ONE device's
# output timing against ANOTHER device's receiver window, so the title should say which
# is which.
#
# THE REQUIREMENT NAMES THE LEG, NOT THE MECHANISM. These read "Setup" rather than
# "Launching" because the launch is a step on the way and setup is the goal — the leg is
# not satisfied by launching, it is satisfied by the level being ready in time for the
# other device's window. Same for hold: what is bounded is that the level persists.
#
# P->P gets letters because it is the only family with two devices of the same class, and
# they are the same letters the terms use: A drives, B samples, on all four of its legs.
_INEQ_LABEL = {
    "MP_setup": "Manager Setup for Peripheral",
    "MP_hold": "Manager Holding for Peripheral",
    "PM_setup": "Peripheral Setup for Manager",
    "PM_hold": "Peripheral Holding for Manager",
    # A third DIRECTION, not a variant of PM: both ends are peripherals, so each references
    # its own arrival of the one forwarded clock and the sampler's own clock crossing enters
    # as a term of its own. Grouped like the others -- the t_ZD launch stacks under the same
    # title as the t_DD one.
    "PP_setup": "Peripheral A Setup for Peripheral B",
    "PP_hold": "Peripheral A Holding for Peripheral B",
    # Named by DIRECTION, like the setup/hold headings above, now that there are
    # three of them: "Manager releases" was legible against one counterpart but
    # not against two, and the reader has to infer the acquirer either way.
    # "Non-Contention" is dropped — with all three present the section reads as
    # the handover family, and the equation on each row shows what is bounded.
    "MP_contention": "Handover — Manager to Peripheral",
    "PM_contention": "Handover — Peripheral to Manager",
    "PP_contention": "Handover — Peripheral to Peripheral",
    # NAMED FOR WHO RELEASES, not for whose keeper: there is one keeper and it is in the
    # Manager ({ASW3805}), so "Bus Keeper — Manager Releasing" means that one keeper with
    # the MANAGER as the device letting go. The pair shared a single heading before; two
    # headings because they are two inequalities with their own corners and their own
    # binding device, and a shared title read as one margin quoted twice.
    "keeper_Man": "Bus Keeper — Manager Releasing",
    "keeper_Per": "Bus Keeper — Peripheral Releasing",
}

# Which inequalities show under each heading. A setup OR hold heading also carries
# its handover ("_ho") variant, stacked under the same title (no separate
# heading) -- on the UI a device drives out of high-Z, t_ZD replaces t_DD in both
# families, at its max corner for setup and its min corner for hold.
#
# The contention pair gets its own headings rather than joining the setup ones:
# it asks a different question (did the old driver let go before the new one
# started?) and its margin means something different — a positive value is an
# undriven float gap the bus keeper holds, not spare time.
_INEQ_GROUPS = [
    ("MP_setup", ("MP_setup", "MP_setup_ho")),
    ("MP_hold", ("MP_hold", "MP_hold_ho")),
    ("PM_setup", ("PM_setup", "PM_setup_ho")),
    ("PM_hold", ("PM_hold", "PM_hold_ho")),
    ("PP_setup", ("PP_setup", "PP_setup_ho")),
    ("PP_hold", ("PP_hold", "PP_hold_ho")),
    ("MP_contention", ("MP_contention",)),
    ("PM_contention", ("PM_contention",)),
    ("PP_contention", ("PP_contention",)),
    # One heading per RELEASING device. They are the same keeper, and the previous single
    # heading said so — but it also put two independent margins under one title, so a
    # reader who saw a fail had to work out whose corner it belonged to before knowing
    # which device to change.
    ("keeper_Man", ("keeper_Man",)),
    ("keeper_Per", ("keeper_Per",)),
]

# Reference numbers, 1) … 17), in DISPLAY order — so "inequality 9" names the same row on
# screen, in the summary text and in conversation. One map, derived from the display
# grouping and read by both renderers, because two independent numberings would drift the
# moment either order changed and a stale number is worse than none.
#
# ")" AND NOT ".", which is Rod's point and a real one: these equations are full of the "·"
# multiplication operator and of decimals like "1.44·8.30", so a leading "1." reads as part
# of the arithmetic rather than as a label.
_INEQ_NUMBER = {ineq: n for n, ineq in enumerate(
    (m for _base, members in _INEQ_GROUPS for m in members), start=1)}


# ---------------------------------------------------------------------------
# Clickable terms: symbol → the INPUT ROW it comes from
# ---------------------------------------------------------------------------
#
# Clicking a term highlights every occurrence of the same thing, and "the same thing" is
# THE ROW A READER WOULD EDIT rather than the printed symbol. That choice is what makes the
# feature answer a question worth asking -- "where does the Manager's clock slew act?"
# highlights twelve legs and the `t_RF Man CLK` row above -- and it is the same rule the
# symbols themselves already follow (`_trf_sym`: name the lane so the term points at the row).
#
# So `Man_t_DD,pure` and `Man_t_DD` are ONE key: the `,pure` suffix is an anchor conversion
# of one row's value, not a second parameter. A crossing keys on its t_RF LANE, because that
# is the row in the grid -- the dimensionless coefficient has no row of its own, and a
# gathered `(X_PP,A,late − X_PP,B,early) · Man_t_RF,CLK` is still one lane however many
# crossings it nets.
#
# FOUR TERMS HAVE NO ROW and key on themselves, or they would be the only unclickable text
# on the page: `UI` (derived from the F_CLK spin box), `Man_t_Keeper_Response` and
# `Per_t_hold` (CalcInputs fields the grid does not expose). Listed rather than detected, so
# a NEW row-less term fails `test_every_printed_term_resolves_to_a_key` instead of silently
# becoming unclickable.
_ROWLESS_TERM_KEYS = ("UI", "Man_t_Keeper_Response", "Per_t_hold")

# Suffixes that qualify a value without naming a different parameter, so they do not change
# which row the term came from. `,min`/`,max` are not here: no symbol carries them (see
# `Term.symbol`'s rule that a picker-owned corner never appears in a symbol) except
# `Per_t_hold,min`, whose base is row-less anyway.
_TERM_SUFFIXES = (",pure", ",EDE", ",min")


def _term_key(symbol: str) -> str:
    """The input row a printed term reads, as the highlight key.

    Returns the row NAME where there is one (`Man_t_DD`, `t_RF Man CLK`, `bus length (cm)`)
    and the bare parameter otherwise. Never raises: an unknown symbol keys on itself, which
    keeps it clickable and self-consistent, and the test is what makes that a loud failure
    rather than a quiet one.
    """
    head = symbol.strip()
    # A product's LAST factor is the row: `X_MP,late · Man_t_RF,DATA`, `1.67·Per_t_RF,DATA`,
    # and the gathered `(…) · Man_t_RF,CLK` all identify a t_RF lane that way.
    last = head.split("·")[-1].strip()
    if "_t_RF," in last:
        device, lane = last.split("_t_RF,")
        return f"t_RF {device} {lane}"
    # A leading integer coefficient is presentation (`1·UI`, `2·t_PD`), not identity.
    if "·" in head and head.split("·")[0].strip().replace(".", "").isdigit():
        head = last
    for suffix in _TERM_SUFFIXES:
        if head.endswith(suffix):
            head = head[: -len(suffix)]
    if head == "t_PD":
        return "bus length (cm)"          # t_PD IS the bus length, in ns
    if head == "t_PD,mis":
        return "t_PD,mis fraction"
    return head


def _key_class(key: str) -> str:
    """A CSS class for `key`, for the delegate's stylesheet.

    QTextDocument's CSS subset supports class selectors but not attribute selectors, so the
    wash is applied by class while the click is read from the href — two spellings of one
    key, which is why `test_every_printed_term_resolves_to_a_key` also checks the classes
    stay distinct. Row names contain spaces, parentheses and Greek (`δ (V_SEOS tol)`), none
    of which are legal in a selector.
    """
    return "k" + "".join(c if c.isalnum() else "_" for c in key)


def _anchored(key: str, body: str) -> str:
    """`body` wrapped so it can be clicked and washed. The operator signs stay OUTSIDE, so
    only the term itself highlights and `+`/`−` do not become click targets."""
    return f'<a href="k:{key}" class="{_key_class(key)}">{body}</a>'


class _RichTextDelegate(QStyledItemDelegate):
    """Render a tree column's text as HTML so <sub>…</sub> subscripts show (a plain
    QTreeWidgetItem would print the tags literally). Text colour comes from the
    paint palette so it applies to nested markup (e.g. the aligned label table).

    Also draws the term highlight and answers "which term is under this point?".
    `keys()` returns the active highlight set, and the wash is applied by CLASS through
    the document's stylesheet rather than by mutating the document: QTextDocument's CSS
    subset supports class selectors, so a toggle costs one repaint and never touches the
    text, the metrics or the computed results.

    THE HIT-TEST AND THE PAINT SHARE `_doc` AND `_origin` DELIBERATELY. They are the only
    two places that turn an item into laid-out glyphs, and if they compute the origin
    differently the clickable boxes drift from the drawn text — invisible until someone
    clicks near a term's edge, which is exactly where a reader aims.
    """

    def __init__(self, parent=None, keys=None):
        super().__init__(parent)
        self.keys = keys or (lambda: frozenset())

    def _doc(self, opt: QStyleOptionViewItem) -> QTextDocument:
        doc = QTextDocument()
        doc.setDefaultFont(opt.font)
        doc.setDocumentMargin(0)
        # ANCHORS TAKE THE TEXT COLOUR EXPLICITLY. `color: inherit` looks like the right
        # answer and is not: Qt's rich-text CSS resolves it to BLACK rather than to the
        # paint context's Text role, so on the dark palette every anchored symbol rendered
        # near-invisible against the panel while the substitution numbers escaped it (their
        # table cell sets a colour of its own) -- a half-dimmed page that reads as a font
        # bug. Naming the colour is safe here because the doc is rebuilt on every paint, so
        # a palette switch is picked up; and only column 0 uses this delegate, so the red
        # negative margins in the Margin column are untouched.
        css = [f"a {{ color: {VizTheme.TEXT}; text-decoration: none; }}"]
        css += [f".{_key_class(k)} {{ background-color: {VizTheme.HILITE_BG}; }}"
                for k in sorted(self.keys())]
        doc.setDefaultStyleSheet("".join(css))
        doc.setHtml(opt.text)
        return doc

    @staticmethod
    def _origin(opt: QStyleOptionViewItem, doc: QTextDocument, widget) -> QPointF:
        """Where the document's (0, 0) lands inside the item — vertically centred."""
        style = widget.style() if widget else QApplication.style()
        rect = style.subElementRect(QStyle.SE_ItemViewItemText, opt, widget)
        return QPointF(rect.left(),
                       rect.top() + max(0.0, (rect.height() - doc.size().height()) / 2))

    def paint(self, painter, option, index):
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        style = opt.widget.style() if opt.widget else QApplication.style()
        doc = self._doc(opt)
        origin = self._origin(opt, doc, opt.widget)
        opt.text = ""
        style.drawControl(QStyle.CE_ItemViewItem, opt, painter, opt.widget)
        painter.save()
        painter.translate(origin)
        ctx = QAbstractTextDocumentLayout.PaintContext()
        ctx.palette.setColor(QPalette.Text, QColor(VizTheme.TEXT))
        doc.documentLayout().draw(painter, ctx)
        painter.restore()

    def key_at(self, opt: QStyleOptionViewItem, index, pos,
               widget) -> Tuple[Optional[str], bool]:
        """(term key under `pos`, whether `pos` is over this item's text at all).

        Qt does the glyph maths: every term is an anchor, so `anchorAt` resolves a point to
        a term without this code knowing anything about character widths or subscripts.

        `opt` MUST COME FROM THE VIEW'S OWN `initViewItemOption`, which is why the caller
        passes it in rather than this method building one. Constructing a
        QStyleOptionViewItem here and calling `initFrom(view)` looks equivalent and is not:
        `initFrom` copies the palette and the font METRICS but leaves `opt.font` at Qt's
        default 9 pt, while paint receives the view's 15 px font. The two documents then lay
        out at different widths -- 512.8 against 606.5 on the first leg -- so the hit boxes
        drifted further from the glyphs the further right the term sat, and clicking `DATA`
        in `Man_t_RF,DATA` selected `Per_t_IS` two terms along. Sharing `_doc` and `_origin`
        was not enough: the drift came in through the option, one level above the guard.
        """
        doc = self._doc(opt)
        local = QPointF(pos) - self._origin(opt, doc, widget)
        href = doc.documentLayout().anchorAt(local)
        on_text = 0 <= local.x() <= doc.idealWidth() and 0 <= local.y() <= doc.size().height()
        return (href[2:] if href.startswith("k:") else None), on_text

    def sizeHint(self, option, index):
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        doc = self._doc(opt)
        return QSize(int(doc.idealWidth()) + 8, int(doc.size().height()))


class _TermTree(QTreeWidget):
    """The results tree, with clickable terms.

    Clicking a term toggles it; clicking anywhere with no term under the pointer clears
    the set, which is the cheapest "undo everything" and needs no chrome. The pointing-hand
    cursor is the only affordance the page has — nothing else here is clickable, so without
    it the feature is invisible.
    """

    term_clicked = Signal(str)
    cleared = Signal()

    def __init__(self, delegate_keys):
        super().__init__()
        self._delegate = _RichTextDelegate(self, keys=delegate_keys)
        self.setItemDelegateForColumn(0, self._delegate)
        self.setMouseTracking(True)

    def _hit(self, pos) -> Tuple[Optional[str], bool]:
        """(term key under `pos`, whether `pos` is on this row's text at all).

        The second half is what stops a near-miss from wiping the reader's work. The
        operators between terms are deliberately not click targets, so a click on a `−`
        lands on the text but on no term: that is INERT, while a click in genuinely empty
        space clears. Without the distinction, aiming at a term and catching the sign beside
        it threw away every highlight -- the least forgiving possible response to a 2-pixel
        miss.
        """
        index = self.indexAt(pos)
        if not index.isValid() or index.column() != 0:
            return None, False
        opt = QStyleOptionViewItem()
        self.initViewItemOption(opt)
        opt.rect = self.visualRect(index)
        self._delegate.initStyleOption(opt, index)
        return self._delegate.key_at(opt, index, pos, self)

    def _key_at(self, pos) -> Optional[str]:
        """The term under `pos`, using the option the VIEW hands the delegate.

        `initViewItemOption` is the whole point of doing this here rather than in the
        delegate: it is the view's own initialisation, so the document the hit-test lays out
        is the document paint lays out — same font, same metrics, same width. Building an
        option by hand instead cost a scaling error that grew with x (see
        `_RichTextDelegate.key_at`).
        """
        return self._hit(pos)[0]

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            key, on_text = self._hit(event.position().toPoint())
            if key:
                self.term_clicked.emit(key)
            elif not on_text:
                self.cleared.emit()      # empty space clears; an operator is inert
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        over = self._key_at(event.position().toPoint())
        self.viewport().setCursor(Qt.PointingHandCursor if over else Qt.ArrowCursor)
        super().mouseMoveEvent(event)


class _NoWheelSpinBox(QDoubleSpinBox):
    """A spin box that ignores the mouse wheel, so scrolling the page while the
    pointer is over a box scrolls the page instead of nudging the value (which
    would re-fit and visibly resize the section)."""

    def wheelEvent(self, e):
        e.ignore()      # let the scroll propagate to the page

# Binds whose CalcInputs value is a fraction but is shown to the user as a percent
# (display value = fraction × 100). V_IH/V_IL are also *_frac but stay ·V_DD.
_PCT_BINDS = {"V_SEOS_tol_frac", "V_noise_pp_frac", "tRF_tolerance_frac", "vPCB_err_frac"}

# Friendlier row labels (the CalcRow name stays the model/lookup key). Labels may
# use HTML — QLabel auto-renders it — so subscripts come through as <sub>…</sub>.
# The Noise-section rows read name (symbol), not symbol (name).
_DISPLAY_NAME = {
    "bus length (cm)": "Bus Length",
    "t_PD,mis fraction": "Length Mismatch",
    "δ (V_SEOS tol)": "V<sub>SEOS</sub> tol (δ)",
    "α (V_noise / V_SEOS)": "V<sub>noise</sub> / V<sub>SEOS</sub> (α)",
    "τ (per-edge slew)": "per-edge slew (τ)",
}


# Timing-symbol tokens rendered as subscripts (V_IH → V<sub>IH</sub>,
# Man_t_DD → Man_t<sub>DD</sub>, X_MP,late → X<sub>MP,late</sub>). The leading
# Man_/Per_ underscore is kept literal; only the underscore right before one of
# these tokens becomes the subscript, and any trailing ,qualifiers join it.
# `PP` joins MP/PM (the P->P legs), `hold` catches Per_t_hold,min, and the qualifier
# group takes digits and `min`/`max` so `Per_t_hold,min` subscripts like its siblings.
_SUB_RE = re.compile(
    r"_(IH|IL|DD|IS|DZ|ZD|RF|PD|MP|PM|PP|hold|Keeper_Response|cross)((?:,[A-Za-z0-9]+)*)")


def _subscript(name: str) -> str:
    """HTML label with timing subscripts (for QLabel / the rich-text delegate).

    Symbols are written `Prefix_t_TOKEN[,qualifier…]` and the spec writes `Prefix_tTOKEN`
    (`Man_tDD` here is `Man_t_DD`), so the underscore before the token is this file's marker
    for "subscript from here". `Keeper_Response` is a token like any other under that rule —
    it was the one symbol spelled the spec's way, which is why it alone rendered upright.
    """
    return _SUB_RE.sub(lambda m: f"<sub>{m.group(1)}{m.group(2)}</sub>", name)


def _display_name(name: str) -> str:
    # Hand-authored labels win; everything else gets automatic subscripts.
    return _DISPLAY_NAME.get(name) or _subscript(name)


def _display_scale(row: CalcRow) -> float:
    """Factor between the value shown in the spin box and the CalcInputs value.
    Percent-displayed rows store a fraction, so display = fraction × 100."""
    return 100.0 if row.binds[0] in _PCT_BINDS else 1.0


def _decimals_for(row: CalcRow) -> int:
    """Spin-box decimal places by unit: ns and % to 1, V thresholds to 2."""
    u = _unit_for(row)
    if u in ("ns", "%", "cm"):
        return 1
    return 2                         # ·V_DD (V_IH/V_IL) and any fraction fallback


def _unit_for(row: CalcRow) -> str:
    """The unit a parameter's min/max carry, inferred from its binding (the CalcRow
    model doesn't store units). V_IH/V_IL are supply-relative thresholds; the
    tolerance / mismatch fractions are shown as percentages."""
    b = row.binds[0]
    if b.endswith("_ns"):
        return "ns"
    if b.endswith("_cm"):
        return "cm"
    if row.name in ("V_IH", "V_IL"):
        return "·V_DD"
    if b in _PCT_BINDS:
        return "%"
    if b.endswith("_frac"):
        return "frac"
    return ""


def _spin_qss() -> str:
    """QAbstractSpinBox styling to match the Visualizer entry look (the theme
    sheets style QLineEdit but not spin boxes). Buttonless boxes are also set via
    setButtonSymbols; zeroing the sub-controls here keeps the frame tight."""
    t = VizTheme
    return f"""
    QAbstractSpinBox {{
        background: {t.ENTRY_BG}; color: {t.TEXT};
        border: {t.BORDER_WIDTH}px solid {t.BORDER};
        border-radius: {t.CORNER_RADIUS}px;
        padding: 1px 4px; font-size: 12px;
        selection-background-color: {t.ACCENT};
    }}
    QAbstractSpinBox:disabled {{ color: {t.TEXT_DIM}; }}
    QAbstractSpinBox[readOnly="true"] {{
        background: {t.FRAME_BG}; color: {t.TEXT_DIM};
        border-color: {t.BORDER};
    }}
    QAbstractSpinBox::up-button, QAbstractSpinBox::down-button {{
        width: 0; height: 0; border: none;
    }}
    """


class TimingView(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self._loading = False
        self._min: Dict[Tuple[str, str], QDoubleSpinBox] = {}
        self._max: Dict[Tuple[str, str], QDoubleSpinBox] = {}
        self._row_labels: Dict[str, QLabel] = {}
        self._man_shape: Optional[QComboBox] = None      # shared across Spec + Example
        self._per_shape: Optional[QComboBox] = None

        root = QVBoxLayout(self)
        root.setContentsMargins(6, 6, 6, 6)
        self.setFont(_base_font())                        # scales combos/buttons/tree text
        self.setStyleSheet(_scaled_stylesheet())

        # Title, then the bus it describes. Both left-aligned and full width:
        # sharing the title's row with the Reset/Save/Load buttons clips the
        # title, because the bar is pinned to the results width and the buttons
        # win the squeeze.
        self._title = QLabel("SWI3S & SoundWire Timing Calculator")
        self._title.setStyleSheet(_TITLE_CSS)
        self._title.setContentsMargins(_HEAD_INDENT, 0, 0, 0)
        root.addWidget(self._title)

        # Which bus the numbers below describe. Dimmed and directly under the
        # fixed title, so the configuration is legible at a glance without the
        # title itself changing under the reader.
        self._subtitle = QLabel("")
        self._subtitle.setStyleSheet(f"color:{VizTheme.TEXT_DIM};")
        self._subtitle.setContentsMargins(_HEAD_INDENT, 0, 0, _px(6))
        root.addWidget(self._subtitle)

        # Top bar constrained to the rectangle width (set in _unify_section_widths)
        # and left-aligned, so the buttons' right edge lands on the card/tree edge.
        self._topbar = QWidget()
        self._topbar.setLayout(self._build_top_bar())
        topbar_row = QHBoxLayout()
        topbar_row.setContentsMargins(0, 0, 0, 0)
        topbar_row.addWidget(self._topbar)
        topbar_row.addStretch(1)
        root.addLayout(topbar_row)

        # Everything below scrolls together (the parameter panes are tall).
        host = QWidget()
        body = QVBoxLayout(host)
        body.setContentsMargins(0, 0, 0, 0)

        body.addWidget(self._section_label("Timing Inequality Results"))
        # The only affordance besides the cursor: nothing else on this page is clickable,
        # so an unmentioned click target is one nobody finds.
        self._trace_hint = QLabel("Click a term to trace it through every inequality and "
                                  "its input row; click it again, or any blank space, to clear.")
        self._trace_hint.setStyleSheet(f"color:{VizTheme.TEXT_DIM};")
        self._trace_hint.setContentsMargins(_HEAD_INDENT, 0, 0, 0)
        body.addWidget(self._trace_hint)
        # The highlight set lives on the view and is read by the delegate through a
        # callable, so a toggle is a repaint: nothing is recomputed, no row is rebuilt and
        # the tree's fitted width cannot move (the wash has no padding).
        self._hl: set = set()
        self._tree = _TermTree(lambda: self._hl)
        self._tree.term_clicked.connect(self._toggle_term)
        self._tree.cleared.connect(self._clear_terms)
        self._tree.setHeaderLabels(["", "Margin", "Max Frequency"])
        # Don't let the last column stretch to fill — with a fixed tree width it
        # would absorb the fit padding and creep wider on every recompute.
        self._tree.header().setStretchLastSection(False)
        # Plain header text, not table-cell chrome: no section background or borders.
        self._tree.header().setStyleSheet(
            f"QHeaderView::section {{ background:{VizTheme.FRAME_BG}; border:none;"
            " padding:2px 6px; }")
        # Always fully expanded and not collapsible — the per-side rows carry the
        # margin + F_max, so there's nothing to hide.
        self._tree.setRootIsDecorated(False)
        self._tree.setItemsExpandable(False)
        # No alternating shade (keeps the palette to one background) and no inner
        # scrollbar — the tree grows to its content so the whole page scrolls as one.
        self._tree.setAlternatingRowColors(False)
        self._tree.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._tree.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._tree.setSelectionMode(QAbstractItemView.NoSelection)   # read-only display
        # (the delegate is installed by _TermTree, which owns the hit-testing with it)
        tree_row = QHBoxLayout()
        tree_row.setContentsMargins(0, 0, 0, 0)
        tree_row.addWidget(self._tree)
        tree_row.addStretch(1)           # keep the bordered tree only as wide as its content
        body.addLayout(tree_row)

        body.addWidget(self._build_param_panes())

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(host)
        root.addWidget(scroll, 1)

        self._sync_spec_controls()          # size picker only applies to a SoundWire side
        self._reset_to_defaults()           # seed values + first compute

    @staticmethod
    def _section_label(text: str) -> QLabel:
        """A body section header — larger than the 12px body text (HEADER_CSS),
        so section titles read as titles, not as another row of parameters."""
        lab = QLabel(text)
        lab.setStyleSheet(_HEADER_CSS)
        lab.setContentsMargins(_HEAD_INDENT, 0, 0, 0)   # line up under the card border
        return lab

    def _fit_tree_height(self) -> None:
        """Size the breakdown tree to its currently-visible rows (top-level items
        plus the children of any expanded item), so it never scrolls on its own."""
        top = self._tree.topLevelItemCount()
        rows = top
        for i in range(top):
            it = self._tree.topLevelItem(i)
            if it.isExpanded():
                rows += it.childCount()
        rh = self._tree.sizeHintForRow(0) if top else 20
        h = self._tree.header().height() + rows * rh + 2 * self._tree.frameWidth() + 4
        self._tree.setFixedHeight(h)

    def _fit_tree_width(self) -> None:
        """Size the tree to exactly its columns so the rounded border hugs the
        content. Re-run on show (see showEvent) because column content widths are
        only correct once the widget has been laid out with the real font."""
        for c in range(self._tree.columnCount()):
            self._tree.resizeColumnToContents(c)
        w = 2 * self._tree.frameWidth() + 8
        for c in range(self._tree.columnCount()):
            w += self._tree.columnWidth(c)
        self._tree.setFixedWidth(w)
        self._results_width = w              # the width every section card matches

    def _unify_section_widths(self) -> None:
        """Make every parameter card — and the top bar — the same width as the
        results tree, so all the rounded rectangles line up and the F_CLK-row
        buttons' right edge lands on the card/tree edge (the tree is the widest,
        being sized to its equations; cards absorb the extra via their trailing
        stretch column, the top bar via its stretch before the buttons)."""
        w = getattr(self, "_results_width", 0)
        for card in getattr(self, "_section_cards", []):
            card.setFixedWidth(w)
        if getattr(self, "_topbar", None) is not None:
            self._topbar.setFixedWidth(w)

    def showEvent(self, e) -> None:      # noqa: N802 - Qt override
        super().showEvent(e)
        # Now that real font metrics apply, re-fit the tree and match the cards.
        self._fit_tree_width()
        self._fit_tree_height()
        self._unify_section_widths()

    # ---- top control bar ----
    def _build_top_bar(self) -> QVBoxLayout:
        # One labelled control per row, so nothing clips when the bar is pinned
        # to the (narrower) results width — three pickers do not fit side by
        # side at this scale, and a cramped row silently overlaps its
        # neighbour's text rather than eliding.
        #
        # Reset/Save/Load are NOT here: they sit on the title row, which has
        # spare width and no left-hand content of its own. Left on their own row
        # after the PHY picker was folded into the spec selector, they read as a
        # detached strip of buttons belonging to nothing.
        outer = QVBoxLayout()
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(4)

        # Row 2 — per-side spec source. A mixed bus has a Manager built to one
        # spec and a Peripheral to another; each side then contributes the timing
        # ITS OWN spec promises. Selecting a SoundWire side reseeds that side's
        # rows from the SoundWire tables and switches its clock-to-output
        # normalisation (SoundWire t_OV is anchored at the END of the slew, a full
        # t_RF later than SWI3S t_DD). Defaults are SWI3S both sides, which is the
        # validated single-spec path.
        #
        # Two rows rather than one: the three labelled pickers do not fit beside
        # each other at this scale, and a cramped row silently overlaps its
        # neighbour's text rather than eliding.
        spec = QHBoxLayout()
        spec.setContentsMargins(_HEAD_INDENT, 0, 0, 0)
        spec.setSpacing(_px(6))
        spec.addWidget(QLabel("Manager spec"))
        self._man_spec_combo: QComboBox = self._make_spec_combo()
        spec.addWidget(self._man_spec_combo)
        spec.addSpacing(_px(16))
        spec.addWidget(QLabel("Peripheral spec"))
        self._per_spec_combo: QComboBox = self._make_spec_combo()
        spec.addWidget(self._per_spec_combo)
        spec.addStretch(1)
        outer.addLayout(spec)

        # SoundWire's small/large system envelope (C_Bus / L_Bus, Tables 4-5).
        # Meaningless for an all-SWI3S bus, so the whole row is HIDDEN unless a
        # side is SoundWire — not merely disabled. A greyed control still asks
        # the reader to work out why it is greyed; on the default all-SWI3S page
        # it was a permanent dead row.
        #
        # Wrapped in a widget because hiding a QHBoxLayout is not a thing: Qt
        # hides widgets, and a layout with every child hidden still holds its
        # spacing.
        self._size_row = QWidget()
        size_row = QHBoxLayout(self._size_row)
        size_row.setContentsMargins(_HEAD_INDENT, 0, 0, 0)
        size_row.setSpacing(_px(6))
        self._size_label = QLabel("SoundWire system size")
        size_row.addWidget(self._size_label)
        self._size_combo = QComboBox()
        self._size_combo.addItem("small  (≤60 pF, ≤30 cm)", "small")
        self._size_combo.addItem("large  (≤100 pF, ≤50 cm)", "large")
        self._size_combo.setMinimumWidth(_px(190))
        self._size_combo.currentIndexChanged.connect(self._on_spec_changed)
        size_row.addWidget(self._size_combo)
        size_row.addStretch(1)
        outer.addWidget(self._size_row)

        # Manager launch architecture. Not a tighter number — a choice of how the
        # Manager launches its data edge. An analog delay line carries a
        # min-to-max spread that the inequalities absorb on BOTH sides at once
        # (the max erodes MP setup, only the min funds MP hold), so tightening it
        # cannot improve both legs. A clock-launched edge lands on a known clock
        # phase, so min == max and the spread disappears. Only meaningful for a
        # SWI3S Manager; a SoundWire one has no such option.
        launch_row = QHBoxLayout()
        launch_row.setContentsMargins(_HEAD_INDENT, 0, 0, 0)
        launch_row.setSpacing(_px(6))
        self._launch_label = QLabel("Manager data launch")
        launch_row.addWidget(self._launch_label)
        self._launch_combo = QComboBox()
        for mode in (LaunchMode.ANALOG, LaunchMode.CLK4, LaunchMode.CLK2):
            self._launch_combo.addItem(LAUNCH_LABELS[mode], mode.value)
        self._launch_combo.setMinimumWidth(_px(210))    # fits "4x CLK launch (0.25 UI)"
        self._launch_combo.currentIndexChanged.connect(self._on_launch_changed)
        launch_row.addWidget(self._launch_combo)
        launch_row.addStretch(1)
        outer.addLayout(launch_row)

        # The clock modes are rate-dependent: 4x is bounded by hold, 2x by setup,
        # so which one is viable changes with F_CLK. Report that rather than
        # leaving the user to discover it from a red margin. On its own row so it
        # can run to full width — at the pinned results width it does not fit
        # beside the combo, and a cramped label clips instead of eliding.
        self._launch_hint = QLabel("")
        self._launch_hint.setStyleSheet(f"color:{VizTheme.TEXT_DIM};")
        self._launch_hint.setWordWrap(True)
        self._launch_hint.setContentsMargins(_HEAD_INDENT, 0, 0, 0)
        outer.addWidget(self._launch_hint)

        # Whether a UI is allocated to the handover. Checked = one UI scheduled,
        # cleared = none, and the three handover inequalities lose their N_HO·UI
        # term with it.
        #
        # A checkbox rather than a spinner because the interesting question is
        # binary: does this bus need a dedicated UI to hand over, or can it do it
        # inside the bit cadence? `CalcInputs.handover_UIs` remains a float, so
        # "programmed to be longer for a larger system" (§11.1.1.1) is still
        # expressible from code — the UI just does not offer it yet.
        #
        # Reseeded from the selected PHYs on Reset / spec change (PHY2 scheduled,
        # PHY1 not), but freely overridable: allocating a UI on PHY1 is legal, and
        # CLEARING it on PHY2 is the EndDriveEarly case this control exists for.
        ho_row = QHBoxLayout()
        ho_row.setContentsMargins(_HEAD_INDENT, 0, 0, 0)
        ho_row.setSpacing(_px(6))
        self._ho_ui_check = QCheckBox("Handover UI")
        self._ho_ui_check.toggled.connect(self._on_ho_ui_changed)
        ho_row.addWidget(self._ho_ui_check)
        # P->P placement. A selector and not a corner row: its value multiplies the bus
        # length, and a product of two rows is not affine, which is the one thing the
        # per-inequality worst-corner search requires (see CalcInputs.pp_placement).
        # "Worst Per Leg" is the default and gives each P->P leg its own worst end of the
        # bracket -- setup pays two traversals, hold banks none -- so the bounding answer
        # is what you get without choosing.
        ho_row.addSpacing(_px(16))
        ho_row.addWidget(QLabel("P→P placement:"))
        self._pp_combo = QComboBox()
        for _key, _label in PP_PLACEMENT_LABELS.items():
            self._pp_combo.addItem(_label, _key)
        # It was the one combo here with no minimum, so it sized to the CURRENT item and
        # elided the longer placements the moment one was picked.
        self._pp_combo.setMinimumWidth(_px(215))   # fits "driver near, sampler far"
        self._pp_combo.setCurrentIndex(self._pp_combo.findData("unknown"))
        self._pp_combo.currentIndexChanged.connect(self._on_pp_placement_changed)
        ho_row.addWidget(self._pp_combo)
        ho_row.addStretch(1)
        outer.addLayout(ho_row)

        # Says which case is on screen, because clearing the box on PHY2 makes
        # the handover legs fail with the CURRENT numbers — that is the true
        # answer, not a bug, and the reason EndDriveEarly needs its own timing.
        self._ho_hint = QLabel("")
        self._ho_hint.setStyleSheet(f"color:{VizTheme.TEXT_DIM};")
        self._ho_hint.setWordWrap(True)
        self._ho_hint.setContentsMargins(_HEAD_INDENT, 0, 0, 0)
        outer.addWidget(self._ho_hint)

        # Row 3 — Clock Frequency Target, with the Example-column actions beside it.
        clk = QHBoxLayout()
        clk.setContentsMargins(_HEAD_INDENT, 0, 0, 0)
        clk.addWidget(QLabel("Clock Frequency Target"))
        self._fclk = _NoWheelSpinBox()
        self._fclk.setButtonSymbols(QAbstractSpinBox.NoButtons)
        self._fclk.setRange(0.001, 27.0)
        self._fclk.setDecimals(3)
        self._fclk.setSingleStep(0.1)
        self._fclk.setAlignment(Qt.AlignRight)
        self._fclk.setMaximumWidth(_BOX_W)
        self._fclk.setValue(13.2)
        self._fclk.valueChanged.connect(self._on_fclk_changed)
        clk.addWidget(self._fclk)
        clk.addWidget(QLabel("MHz"))       # unit after the box, like the rest of the page
        clk.addStretch(1)
        # Reset/Save/Load ride the F_CLK row rather than a row of their own: it
        # is the shortest labelled row, so there is width to spare, and their
        # right edge lands on the card/tree edge. On their own row — where they
        # sat beside the PHY picker before it was folded into the spec selector
        # — they read as a detached strip belonging to nothing. Not on the title
        # row either: that bar is pinned to the results width and the buttons
        # win the squeeze, clipping the title.
        for text, slot in (("Reset", self._reset_to_defaults),
                           ("Save Example", self._save_proposed),
                           ("Load Example", self._load_proposed)):
            b = QPushButton(text)
            b.clicked.connect(slot)
            clk.addWidget(b)
        outer.addLayout(clk)

        # Advisory line, on its own row so it can run to full width without
        # clipping — it carries two clauses on a mixed bus. Warns when the target
        # exceeds what the selected pair may legally clock (every SoundWire
        # envelope tops out below SWI3S PHY2's mandatory 13.2 MHz) and when a
        # margin depends on hysteresis SoundWire does not guarantee.
        self._ceiling_label = QLabel("")
        self._ceiling_label.setStyleSheet(f"color:{VizTheme.SEM_ERROR};")
        self._ceiling_label.setWordWrap(True)
        self._ceiling_label.setContentsMargins(_HEAD_INDENT, 0, 0, 0)
        outer.addWidget(self._ceiling_label)
        return outer

    def _make_spec_combo(self) -> QComboBox:
        # Ratified specs only. The proposed revision is not a selectable spec
        # here — it seeds the editable Example column instead, so the page is a
        # current-vs-proposed comparison by default (see _reset_to_defaults).
        c = QComboBox()
        for src in SPEC_SOURCES:
            if is_proposed(src):
                continue
            c.addItem(SPEC_LABELS[src], src)
        c.setMinimumWidth(_px(200))       # fits "SWI3S PHY2 (EDE proposed)"
        c.setCurrentIndex(SPEC_SOURCES.index("SWI3S_PHY2"))
        c.currentIndexChanged.connect(self._on_spec_changed)
        return c

    # ---- Specification-vs-Example parameter grid ----
    # One grid per section: a shared name column, then Spec min/max/unit and
    # Example min/max/unit side by side (directly comparable on one row).
    # Columns: 0 name · 1-2 Spec min/max · 3 Spec unit · 4 gap · 5-6 Example
    # min/max · 7 Example unit · 8 trailing slack. (side, min-col, max-col, unit-col)
    _SPEC_COLS = ("spec", 1, 2, 3)
    _EX_COLS = ("proposed", 5, 6, 7)

    def _build_param_panes(self) -> QWidget:
        host = QWidget()
        outer = QVBoxLayout(host)
        outer.setContentsMargins(0, 0, 0, 0)
        self._section_cards: List[QFrame] = []   # widths unified to the results tree
        for i, (section_title, row_names) in enumerate(SECTIONS):
            if i > 0:
                outer.addSpacing(_SECTION_GAP)   # separate a section title from the rows above
            outer.addWidget(self._section_label(section_title))
            card = self._build_section(section_title, row_names)
            self._section_cards.append(card)
            card_row = QHBoxLayout()
            card_row.setContentsMargins(0, 0, 0, 0)
            card_row.addWidget(card)
            card_row.addStretch(1)           # card is left-aligned; width set in _unify_widths
            outer.addLayout(card_row)
        outer.addStretch(1)
        return host

    def _column_labels(self) -> Tuple[str, str]:
        """(left, right) column headings, naming what each column actually holds.

        The pair is not fixed, because the comparison the page makes is not fixed:
        normally it is the spec as it stands against a proposed revision, but on
        an EDE bus it shifts up a step to the revision against EDE. A heading of
        "Specification" over proposed-revision values would be simply wrong, and
        the headings are the only thing telling a reader which is which.
        """
        man = self._man_spec_combo.currentData()
        per = self._per_spec_combo.currentData()
        if is_ede(man) or is_ede(per):
            return "PHY2 Proposed", "EDE Proposed"
        return "Specification", "Example"

    def _refresh_column_labels(self) -> None:
        left, right = self._column_labels()
        for lab in getattr(self, "_left_heads", []):
            lab.setText(left)
        for lab in getattr(self, "_right_heads", []):
            lab.setText(right)

    def _col_head(self, text: str) -> QLabel:
        """A centred column header. Uses the standard (light) text colour like the
        rest of the labels — no separate dim shade."""
        lab = QLabel(text)
        lab.setAlignment(Qt.AlignHCenter)
        return lab

    def _build_section(self, section_title: str, row_names: List[str]) -> QWidget:
        # A rounded, 1px-bordered card (matching the results tree) holding the
        # section's grid — and, for Rise/Fall, the ramp pickers above it.
        card = QFrame()
        card.setObjectName("card")
        card.setStyleSheet(
            f"QFrame#card {{ border:{VizTheme.BORDER_WIDTH}px solid {VizTheme.BORDER};"
            f" border-radius:{VizTheme.CORNER_RADIUS}px; }}")
        col = QVBoxLayout(card)
        col.setContentsMargins(10, 8, 10, 8)
        col.setSpacing(6)
        if section_title == "Rise/Fall Times":
            col.addWidget(self._build_shape_row())      # ramp pickers before the params

        grid = QGridLayout()
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setVerticalSpacing(2)
        # Fixed column widths so Spec and Example boxes line up vertically across
        # every section (regardless of each section's unit text width).
        grid.setColumnMinimumWidth(0, _NAME_COL_W)
        for c in (1, 2, 5, 6):
            grid.setColumnMinimumWidth(c, _BOX_W)
        for c in (3, 7):
            grid.setColumnMinimumWidth(c, _UNIT_COL_W)
        grid.setColumnMinimumWidth(4, _GAP_W)        # push Example clear of Spec
        grid.setColumnStretch(8, 1)                  # absorb extra width (cards are widened)
        left_lab, right_lab = self._column_labels()
        lh, rh = self._col_head(left_lab), self._col_head(right_lab)
        self._left_heads = getattr(self, "_left_heads", []) + [lh]
        self._right_heads = getattr(self, "_right_heads", []) + [rh]
        grid.addWidget(lh, 0, 1, 1, 2)
        grid.addWidget(rh, 0, 5, 1, 2)
        for c in (1, 2, 5, 6):
            grid.addWidget(self._col_head("min" if c in (1, 5) else "max"), 1, c)
        for r, name in enumerate(row_names, start=2):
            self._add_param_row(name, grid, r)
        col.addLayout(grid)
        return card

    def _build_shape_row(self) -> QWidget:
        """The Manager / Peripheral edge-ramp model pickers — one of each, shared by
        Spec and Example — shown under the Rise/Fall title, above the t_RF params.
        The two combos share column 1 so they line up vertically."""
        holder = QWidget()
        grid = QGridLayout(holder)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setVerticalSpacing(2)
        grid.setColumnStretch(2, 1)
        self._man_shape = self._make_shape_combo()
        self._per_shape = self._make_shape_combo()
        grid.addWidget(QLabel("Manager Ramp Model"), 0, 0)
        grid.addWidget(self._man_shape, 0, 1)
        grid.addWidget(QLabel("Peripheral Ramp Model"), 1, 0)
        grid.addWidget(self._per_shape, 1, 1)
        return holder

    def _make_shape_combo(self) -> QComboBox:
        cb = QComboBox()
        cb.addItem("linear", "linear")          # display, RampShape value
        cb.addItem("exponential", "exp")
        cb.setMinimumWidth(_COMBO_W)            # room for "exponential"
        cb.currentIndexChanged.connect(self._recompute)
        return cb

    @staticmethod
    def _set_shape(combo: QComboBox, value) -> None:
        if value is None:
            return
        i = combo.findData(value)
        if i >= 0:
            combo.setCurrentIndex(i)

    def _add_param_row(self, name: str, grid: QGridLayout, gridrow: int) -> None:
        row = _ROW_BY_NAME[name]
        lab = QLabel(_display_name(name))
        # Held so `_sync_row_highlight` can wash it: a term's key IS a row name, so the
        # click can point at the box a reader would edit, which is the whole reason the
        # keys are rows rather than symbols.
        self._row_labels[name] = lab
        grid.addWidget(lab, gridrow, 0)
        dec = _decimals_for(row)
        unit = _unit_for(row)
        for side, cmin, cmax, cunit in (self._SPEC_COLS, self._EX_COLS):
            mn = self._make_spin(dec); mx = self._make_spin(dec)
            if side == "spec":
                # The Specification column shows the spec's own min/max — read-only,
                # so only the Example column is editable. setValue still drives it
                # (PHY switch / Reset reseed it) and its valueChanged still recomputes.
                for sp in (mn, mx):
                    sp.setReadOnly(True)
                    sp.setFocusPolicy(Qt.NoFocus)
            mn.valueChanged.connect(self._recompute)
            mx.valueChanged.connect(self._recompute)
            if name in self._LAUNCH_ROWS:
                # A clocked t_ZD / t_DZ must land where the clock can place it.
                # editingFinished, not valueChanged, so it does not fight the
                # user mid-keystroke.
                mn.editingFinished.connect(self._snap_launch_rows)
                mx.editingFinished.connect(self._snap_launch_rows)
            self._min[(side, name)] = mn
            self._max[(side, name)] = mx
            grid.addWidget(mn, gridrow, cmin)
            grid.addWidget(mx, gridrow, cmax)
            grid.addWidget(QLabel(unit), gridrow, cunit)

    @staticmethod
    def _make_spin(decimals: int = 2) -> QDoubleSpinBox:
        sp = _NoWheelSpinBox()
        sp.setButtonSymbols(QAbstractSpinBox.NoButtons)
        sp.setRange(0.0, 10000.0)
        sp.setDecimals(decimals)
        sp.setSingleStep(10 ** -decimals)
        sp.setAlignment(Qt.AlignRight)
        sp.setFixedWidth(_BOX_W)            # fixed so box columns align across sections
        return sp

    # ---- state readers (mirror the Streamlit helpers) ----
    def _rows(self) -> List[CalcRow]:
        # Mgr CLK and Mgr DATA slew together in practice (same driver PVT), so tie
        # Mgr DATA's worst-corner pick to Mgr CLK — the auto-worst search can't drive
        # the two Mgr lanes to opposite spec edges. Numeric min/max stay independent.
        return default_swi3s_rows(lock_mgr_lanes=True)

    def _read_rows(self, side: str) -> List[CalcRow]:
        out: List[CalcRow] = []
        for row in self._rows():
            sc = _display_scale(row)             # % display → fraction for CalcInputs
            mn = self._min[(side, row.name)].value() / sc
            mx = self._max[(side, row.name)].value() / sc
            # `is_window` travels with the row: dropping it here would re-collapse the
            # threshold window on the way from the spin boxes to the model, which is the
            # whole defect, and only in the UI path where it is hardest to see.
            out.append(CalcRow(row.name, row.binds,
                               ParamRange(mn, 0.5 * (mn + mx), mx), row.linked_to,
                               row.is_window))
        return out

    def _base_inputs(self, side: str) -> CalcInputs:
        # The ramp models and the spec sources are shared across Spec and Example
        # (side is ignored here) — they describe the bus under test, not one
        # column of it.
        man = self._man_spec_combo.currentData()
        per = self._per_spec_combo.currentData()
        return CalcInputs(
            Man_shape=self._man_shape.currentData(),
            Per_shape=self._per_shape.currentData(),
            F_CLK_target_MHz=self._fclk.value(),
            Man_spec=man,
            Per_spec=per,
            system_size=self._size_combo.currentData(),
            launch_mode=self._launch_mode_for(side),
            # Driven by the Handover UI checkbox, which Reset/spec-change seeds
            # from the selected pair (0 for PHY1's intra-UI handover, 1 for
            # PHY2's scheduled one, §11.1.1.1) and the user may then override.
            handover_UIs=self._handover_UIs(),
            # Where the two peripherals sit, for the P->P data legs only.
            pp_placement=self._pp_combo.currentData(),
            # EDE shifts the releasing side's t_DZ reference one UI earlier. Only
            # the EDE column asserts it: the baseline column is the proposed
            # revision, which has no EDE.
            Man_ede=self._man_ede_for(side),
            Per_ede=is_ede(per) and side == "proposed",
        )

    def _man_ede_for(self, side: str) -> bool:
        """Whether the MANAGER releases early on `side`.

        A method rather than an inline condition because `_sync_man_tdd_rows` needs the
        same answer to decide whether Table 130's ceiling bounds the Man_t_DZ row. Two
        spellings of one predicate is how the launch mode came to snap the Specification
        column's tabulated values: the code had the right rule and only one of the
        places that applied it agreed with it.
        """
        return is_ede(self._man_spec_combo.currentData()) and side == "proposed"

    def _launch_mode_for(self, side: str) -> LaunchMode:
        """The Manager's data launch method on `side`.

        A CLOCKED LAUNCH IS THE REVISION'S OPTION, NOT A TABULATED ONE. The wording
        that offers it -- "9-23 ns or (0.5 UI 2x CLK, 0.25 UI 4x CLK)" -- is in the
        PHY2 revision. Ratified PHY1 and PHY2 tabulate analog output delays and give
        a Manager no multiplied clock to launch from, so the Specification column is
        always ANALOG however the selector is set. Same shape as `Man_ede` above,
        and for the same reason: the column is what decides, not the combo.

        THIS FIXES A DESTRUCTIVE BUG rather than tightening a definition. The mode
        was applied to both columns, so selecting 2x snapped the SPEC column's
        tabulated values onto a 0.5 UI grid -- and `snap_to_launch_grid(2.0)` is
        0.00, so PHY2's Man_t_DD 7-18, Man_t_ZD 2-18 and Man_t_DZ 0-10 all became
        0.00. A Manager launching data with zero delay is not a conservative
        rendering of anything; every leg reading those rows on that column was
        nonsense, silently. The tooltip and read-only loop in `_sync_man_tdd_rows`
        already scoped itself to `("proposed", ...)`, so the code had the right rule
        and only the value writes disagreed with it.
        """
        if side != "proposed":
            return LaunchMode.ANALOG
        return LaunchMode(self._launch_combo.currentData())

    def _typ_baseline(self, side: str):
        base = self._base_inputs(side)
        rows = self._read_rows(side)
        typ_picks = {r.name: "typ" for r in rows if r.linked_to is None}
        return apply_row_picks(base, rows, typ_picks), rows

    def _worst_results(self, side: str) -> Dict[str, object]:
        """Full CalcResults for `side`, each evaluated at ITS OWN inequality's worst
        corner. Corners are auto-selected (find_worst_corner_rows) — there is no
        manual pick — so this is the single source both the Max-Frequency table and
        the crossing-penalty Results table read from."""
        base, rows = self._typ_baseline(side)
        out: Dict[str, object] = {}
        for ineq in INEQUALITIES:
            picks = find_worst_corner_rows(ineq, rows, base)
            out[ineq] = compute(apply_row_picks(base, rows, picks))
        return out

    def _per_inequality_worst(self, side: str) -> Dict[str, object]:
        return {ineq: r.breakdowns[ineq]
                for ineq, r in self._worst_results(side).items()}

    # ---- public interface (tests / workspace) ----
    def inputs(self, side: str = "spec") -> CalcInputs:
        """The nominal (typ) inputs for `side` — the midpoint of every range.
        Corners are auto-selected per inequality for the tables; this is the neutral
        baseline for callers/tests that want a single representative input set."""
        return self._typ_baseline(side)[0]

    def summary_text(self) -> str:
        """Plain-text dump of the Max-Frequency table — used by tests and as a
        copyable summary.

        Carries the same reference numbers the tree shows (`_INEQ_NUMBER`), so a number
        quoted from a pasted summary points at the row on screen.
        """
        lines = [" | ".join(_FMAX_COLS)]
        spec_w = self._per_inequality_worst("spec")
        prop_w = self._per_inequality_worst("proposed")
        for ineq in INEQUALITIES:
            bs, bp = spec_w[ineq], prop_w[ineq]
            lines.append(" | ".join([
                f"{_INEQ_NUMBER[ineq]}) {ineq.replace('_', ' ')}",
                f"{bs.margin_ns:+.2f}", f"{bp.margin_ns:+.2f}",
                f"{bp.margin_ns - bs.margin_ns:+.2f}",
                self._fmt_fmax(bs.F_max_MHz) if bs.is_setup else "—",
                self._fmt_fmax(bp.F_max_MHz) if bp.is_setup else "—",
                "—",
            ]))
        return "\n".join(lines)

    def to_dict(self) -> dict:
        d = {"fclk_target": self._fclk.value(),
             "man_shape": self._man_shape.currentData(),
             "per_shape": self._per_shape.currentData(),
             "man_spec": self._man_spec_combo.currentData(),
             "per_spec": self._per_spec_combo.currentData(),
             "sw_system_size": self._size_combo.currentData(),
             "launch_mode": self._launch_combo.currentData(),
             "handover_ui": self._ho_ui_check.isChecked(),
             "pp_placement": self._pp_combo.currentData()}
        for side in SIDES:
            for row in _ALL_ROWS:
                d[f"{side}::{row.name}::min"] = self._min[(side, row.name)].value()
                d[f"{side}::{row.name}::max"] = self._max[(side, row.name)].value()
        return d

    def set_from_dict(self, d: dict) -> None:
        self._loading = True
        try:
            # Legacy "lock_mgr_lanes" is ignored; a legacy per-side "spec::man_shape"
            # falls back into the now-shared picker.
            #
            # A legacy "phy" key predates the per-side spec selector, when the PHY
            # was one global switch. PHY1 is now a spec source, so an old PHY1
            # workspace restores as PHY1 on BOTH sides — which is what the single
            # switch meant. An explicit man_spec/per_spec always wins, so this only
            # applies to workspaces written before the selector existed.
            legacy_phy = d.get("phy")
            legacy_default = ("SWI3S_PHY1" if legacy_phy == 1 else "SWI3S_PHY2")
            # A dict with no spec keys predates the cross-spec selector, so it can
            # only have described an all-SWI3S bus.
            for combo, key in ((self._man_spec_combo, "man_spec"),
                               (self._per_spec_combo, "per_spec")):
                j = combo.findData(d.get(key, legacy_default))
                if j >= 0:
                    combo.setCurrentIndex(j)
            k = self._size_combo.findData(d.get("sw_system_size", "small"))
            if k >= 0:
                self._size_combo.setCurrentIndex(k)
            # A dict with no launch_mode predates the selector, so it can only
            # have described the analog delay.
            lm = self._launch_combo.findData(
                d.get("launch_mode", LaunchMode.ANALOG.value))
            if lm >= 0:
                self._launch_combo.setCurrentIndex(lm)
            # A dict with no handover_ui predates the checkbox, so fall back to
            # what the selected PHYs imply — which is what that version computed.
            self._ho_ui_check.setChecked(bool(d.get(
                "handover_ui",
                default_handover_UIs(self._man_spec_combo.currentData(),
                                     self._per_spec_combo.currentData()) > 0)))
            # A dict with no pp_placement predates the selector; "unknown" is what that
            # version computed (each P->P leg at its own worst end).
            pp = self._pp_combo.findData(d.get("pp_placement", "unknown"))
            if pp >= 0:
                self._pp_combo.setCurrentIndex(pp)
            self._sync_spec_controls()
            if "fclk_target" in d:
                self._fclk.setValue(float(d["fclk_target"]))
            self._set_shape(self._man_shape, d.get("man_shape", d.get("spec::man_shape")))
            self._set_shape(self._per_shape, d.get("per_shape", d.get("spec::per_shape")))
            for side in SIDES:
                for row in _ALL_ROWS:
                    self._apply_keys(side, row.name, d)
        finally:
            self._loading = False
        # A saved Man_t_DD is only meaningful under the analog mode; if the
        # restored launch mode is clocked, the mode wins and the row says so.
        self._sync_man_tdd_rows()
        self._update_title()
        self._recompute()

    def _apply_keys(self, side: str, name: str, d: dict) -> None:
        # A legacy "::pick" key (pre-auto-corner) is simply ignored.
        kmin, kmax = f"{side}::{name}::min", f"{side}::{name}::max"
        if kmin in d:
            self._min[(side, name)].setValue(float(d[kmin]))
        if kmax in d:
            self._max[(side, name)].setValue(float(d[kmax]))

    # ---- actions ----
    def _update_title(self) -> None:
        """Fixed page title, with the selected bus named beneath it.

        The title used to carry the configuration ("SWI3S PHY2 Timing
        Calculator"), which meant it changed under the reader and could not say
        what the page covers. The pickers already show the configuration, and
        the subtitle restates the pair because which side is which changes the
        answer.
        """
        man = self._man_spec_combo.currentData()
        per = self._per_spec_combo.currentData()
        if man == per:
            self._subtitle.setText(f"{SPEC_LABELS[man]} Manager and Peripheral")
        else:
            self._subtitle.setText(f"{SPEC_LABELS[man]} Manager  ↔  "
                                   f"{SPEC_LABELS[per]} Peripheral")

    def _seed_range(self, side: str, row: CalcRow, man_spec: SpecSource,
                    per_spec: SpecSource, size) -> ParamRange:
        """The range a row seeds to for `side`, from the spec tables.

        Extracted from `_reset_to_defaults` so that re-seeding a SINGLE row —
        which `_sync_man_tdd_rows` does when the launch mode hands Man_t_DD back
        — reads the same source as a full reset, rather than a second copy of
        this precedence order that could drift from it.

        The read-only Specification column shows the spec as it stands; the
        editable Example column seeds from the proposed revision, so the page
        opens on a direct current-vs-proposed comparison — which is what the two
        columns are for. A SoundWire side has no proposal and seeds both columns
        identically; a PHY1 side gets only the shared part of it (see
        _PHY1_PROPOSED). Gated per SIDE, so a mixed bus still gets each side's
        own proposal.

        When a side is on the EDE source the comparison SHIFTS UP a step: the
        baseline becomes the proposed revision (not the current spec) and the
        candidate becomes EDE. The current spec is settled enough to stop being
        the thing EDE is measured against — what matters is what EDE adds ON TOP
        of the revision. Column headers follow, via _column_labels().
        """
        which = _side_of_row(row.name)
        spec = man_spec if which == "Man" else per_spec
        # A shared row (thresholds, noise, bus geometry) belongs to neither side.
        # It still needs A phy to resolve, and only PHY1's three side-specific
        # output rows differ, so the Manager's reading is as good as either.
        phy = _phy_of(spec if which is not None else man_spec)
        rng = None
        proposal = _PROPOSED_BY_SPEC.get(spec, {})
        if is_ede(spec):
            if side == "spec":
                # Baseline: the proposed revision.
                rng = _PHY2_PROPOSED.get(row.name)
            else:
                # Candidate: EDE, whose t_DZ / clocked values are UI fractions,
                # so they depend on the target rate.
                rng = _ede_ranges(_UI_ns(self._fclk.value())).get(row.name)
        elif side == "proposed" and row.name in proposal:
            rng = proposal[row.name]
        if rng is None and which is not None:
            rng = _row_range_for_spec(row.name, spec, which, size)
        if rng is None:
            rng = _spec_range(row.name, phy)
        return rng

    def _reset_to_defaults(self) -> None:
        self._loading = True
        try:
            self._fclk.setValue(self._default_fclk())
            # The checkbox follows the PHY on reset; _handover_UIs() then reads it.
            self._ho_ui_check.setChecked(
                default_handover_UIs(self._man_spec_combo.currentData(),
                                     self._per_spec_combo.currentData()) > 0)
            self._set_shape(self._man_shape, "linear")
            self._set_shape(self._per_shape, "linear")
            man_spec = self._man_spec_combo.currentData()
            per_spec = self._per_spec_combo.currentData()
            size = self._size_combo.currentData()
            for side in SIDES:
                for row in _ALL_ROWS:
                    rng = self._seed_range(side, row, man_spec, per_spec, size)
                    sc = _display_scale(row)
                    self._min[(side, row.name)].setValue(float(rng.min) * sc)
                    self._max[(side, row.name)].setValue(float(rng.max) * sc)
        finally:
            self._loading = False
        self._sync_man_tdd_rows()
        self._update_title()
        self._recompute()

    def _default_fclk(self) -> float:
        """Target rate to seed on reset.

        The per-side targets are what each spec is designed around; the pair's
        ceiling then clamps. A mixed bus cannot legally clock at SWI3S PHY2's
        mandatory 13.2 MHz — every SoundWire envelope tops out below it — so
        seeding the ceiling beats seeding a rate the bus may not run at.
        """
        man = self._man_spec_combo.currentData()
        per = self._per_spec_combo.currentData()
        base = min(_SPEC_FCLK_TARGET.get(man, _FCLK_TARGET_DEFAULT),
                   _SPEC_FCLK_TARGET.get(per, _FCLK_TARGET_DEFAULT))
        ceiling = CalcInputs(
            Man_spec=man,
            Per_spec=per,
            system_size=self._size_combo.currentData(),
        ).F_CLK_ceiling_MHz()
        return min(base, ceiling)

    def _on_spec_changed(self, *_a) -> None:
        """Switching either side's spec reseeds that side's rows from its own
        tables and re-evaluates. During a workspace load we only track the
        selection — saved values are applied afterwards."""
        self._sync_spec_controls()
        self._update_title()
        self._refresh_column_labels()
        if not self._loading:
            self._reset_to_defaults()

    def _on_launch_changed(self, *_a) -> None:
        """The launch mode changes no spec table, so it must NOT reseed.

        It deliberately does not route through `_on_spec_changed`: that calls
        `_reset_to_defaults`, which would re-seed every row and reset F_CLK to
        the PHY default — silently discarding a user's target rate just because
        they compared two launch options.

        It does re-derive the ONE row it governs (see `_sync_man_tdd_rows`),
        which is not a reseed: that row is the mode's output, not the user's
        input, and showing the tabulated range while the model used the grid
        point is what sent a reader looking for a bug that was on screen.
        """
        self._update_launch_hint()
        self._sync_man_tdd_rows()
        if not self._loading:
            self._recompute()

    def _toggle_term(self, key: str) -> None:
        """Add or remove `key` from the highlight set.

        Toggling on the KEY and not on the clicked glyph is what makes a second click
        anywhere on the same parameter turn it off — the reader does not have to find the
        occurrence they started from.
        """
        self._hl.symmetric_difference_update({key})
        self._refresh_highlight()

    def _clear_terms(self) -> None:
        if self._hl:
            self._hl.clear()
            self._refresh_highlight()

    def _refresh_highlight(self) -> None:
        """Repaint only. No recompute, no rebuild — see `self._hl`."""
        self._tree.viewport().update()
        self._sync_row_highlight()

    def _sync_row_highlight(self) -> None:
        """Wash the input rows whose parameter is highlighted.

        A key is a row NAME wherever the term has one, so this is a dict lookup rather than
        a second mapping. Row-less keys (UI, the keeper response) simply match nothing here,
        which is correct: there is no box to point at.
        """
        for name, lab in self._row_labels.items():
            on = name in self._hl
            lab.setStyleSheet(
                f"background-color:{VizTheme.HILITE_BG}; border-radius:3px;" if on else "")

    def _on_ho_ui_changed(self, *_a) -> None:
        """Allocating or dropping a handover UI changes no spec table either.

        Same reasoning as the launch mode: no reseed, or comparing the two
        handover cases would silently reset the rate and every edited row.
        """
        self._update_ho_hint()
        if not self._loading:
            self._recompute()

    def _on_pp_placement_changed(self, *_a) -> None:
        """Placement changes no spec table — same reasoning as the launch mode and the
        handover UI: reseeding here would reset the rate and every edited row."""
        if not self._loading:
            self._recompute()

    def _handover_UIs(self) -> float:
        """UIs allocated to a handover, per the checkbox.

        One when ticked, none when cleared. `CalcInputs.handover_UIs` is a float
        so a longer programmed allocation stays expressible from code; the UI
        offers only the binary case, which is the one that changes the answer's
        character rather than its size.
        """
        return 1.0 if self._ho_ui_check.isChecked() else 0.0

    def _update_ho_hint(self) -> None:
        """Name the case on screen, and say what a cleared box means.

        Clearing it on PHY2 makes the handover legs FAIL at the current numbers,
        because PHY2's t_ZD,min (2 ns) cannot cover a t_DZ,max of 10 ns without a
        UI to separate the reference edges. That is the true answer, and the
        reason EndDriveEarly needs timing values of its own — it reaches a
        zero-UI handover by releasing ~half a UI early, i.e. by changing t_DZ,
        not by removing the requirement. Said here so a red margin reads as the
        question it is rather than as a defect.
        """
        man = self._man_spec_combo.currentData()
        per = self._per_spec_combo.currentData()
        native = default_handover_UIs(man, per)
        if self._ho_ui_check.isChecked():
            txt = "one UI scheduled for the handover — the N_HO·UI term is in force"
            if native <= 0:
                txt += "; more than this bus needs, which is legal (§11.1.1.1)"
        else:
            txt = "no UI allocated — the handover must close on device timing alone"
            if native > 0:
                txt += ("; PHY2 does not, at its current t_DZ/t_ZD. EndDriveEarly "
                        "is the mechanism that would, by releasing early — its "
                        "timing values are not modelled yet.")
        self._ho_hint.setText(txt)

    def _on_fclk_changed(self, *_a) -> None:
        """F_CLK moves the clock-launched t_DD and the mode's viable ceiling, so
        the hint has to follow it, not just the spec selection."""
        self._update_launch_hint()
        self._refresh_ede_rate_rows()
        # After the EDE reseed, which rewrites Man_t_DD: a clocked grid point
        # scales with the rate, so it has to be re-derived here too, and last.
        self._sync_man_tdd_rows()
        self._recompute()

    def _refresh_ede_rate_rows(self) -> None:
        """Re-derive the EDE column's UI-fraction rows at the current rate.

        Deliberately narrow. Reseeding the whole column on a rate change would
        discard the user's edits, which is the trap `_on_launch_changed` avoids —
        but the four rows in `_EDE_UI_RELATIVE_ROWS` are *defined* as fractions of
        the UI, so carrying an absolute value across a rate change would show a
        number that is not the proposal at either rate. Everything else in the
        column, edits included, is left untouched.
        """
        if self._loading:
            return
        if not (is_ede(self._man_spec_combo.currentData())
                or is_ede(self._per_spec_combo.currentData())):
            return
        rng = _ede_ranges(_UI_ns(self._fclk.value()))
        self._loading = True                      # setValue must not re-enter
        try:
            for name in _EDE_UI_RELATIVE_ROWS:
                r = rng.get(name)
                if r is None:
                    continue
                sc = _display_scale(_ROW_BY_NAME[name])
                self._min[("proposed", name)].setValue(float(r.min) * sc)
                self._max[("proposed", name)].setValue(float(r.max) * sc)
        finally:
            self._loading = False

    # Every Manager output timing the launch mode governs. All three, or none: one
    # Manager has one output stage, so a t_DD on a clock grid point beside an
    # analog t_ZD and t_DZ is not a conservative model, it is two different
    # devices. See `calculator._man_tzd_clocked`.
    #
    # Man_t_DD is FIXED by the mode -- the selector names its placement, "4× CLK
    # launch (0.25 UI)" -- while t_ZD and t_DZ are only constrained to the mode's
    # RESOLUTION. A 4× clock offers 0.00, 0.25, 0.50 and 0.75 UI and the three do
    # not have to agree; a 2× clock offers 0.00 and 0.50. So those two stay
    # editable and are snapped to a placeable point rather than pinned to t_DD's.
    _LAUNCH_ROWS = ("Man_t_DD", "Man_t_ZD", "Man_t_DZ")
    # Nothing is pinned. The mode says which points exist; the rows say which are
    # used. A 4x clock offers three interior placements, so fixing t_DD to one of
    # them made designs like EDE's (4x resolution, t_DD at 0.50 UI) inexpressible.
    _LAUNCH_FIXED_ROWS: tuple[str, ...] = ()

    def _launch_driven_rows(self) -> tuple[str, ...]:
        """Which of `_LAUNCH_ROWS` the launch mode governs on the current column.

        All three, on every column including EDE. EDE changes what t_DZ MEANS, not
        whether the selected clock can place it — a release the clock cannot
        generate is not a proposal. The same rule decides the margins, in
        `calculator._man_tzd_clocked` / `_man_tdz_clocked`; one rule read in both
        places, or the row and the term drift apart again.
        """
        return self._LAUNCH_ROWS

    def _snap_launch_rows(self) -> None:
        """Move the editable clocked rows onto a point the selected clock places.

        An off-grid t_ZD or t_DZ describes a Manager that cannot be built with the
        selected clock, which reads as a perfectly good number rather than as the
        impossibility it is. Snapped on `editingFinished` so it does not fight the
        user mid-keystroke; the model snaps too (`_man_clocked_grid_ns`), so a
        margin is never computed from an unplaceable value even between edits.
        """
        if self._loading:
            return
        mode = LaunchMode(self._launch_combo.currentData())
        if launch_grid_UI(mode) is None:
            return
        f = self._fclk.value()
        driven = self._launch_driven_rows()
        man_spec = self._man_spec_combo.currentData()
        per_spec = self._per_spec_combo.currentData()
        size = self._size_combo.currentData()
        was, self._loading = self._loading, True
        try:
            for name in driven:
                if name in self._LAUNCH_FIXED_ROWS:
                    continue
                row = _ROW_BY_NAME[name]
                sc = _display_scale(row)
                for side in SIDES:
                    # PROPOSED ONLY. A tabulated value is not a design choice to be
                    # placed on a grid, and snapping one rounds it to zero -- see
                    # `_launch_mode_for`. The Specification boxes are left as tabulated.
                    if launch_grid_UI(self._launch_mode_for(side)) is None:
                        continue
                    # Same floor as the reseed path: an edit must not be rounded down
                    # through the row's tabulated minimum onto 0.00 UI.
                    floor = float(self._seed_range(side, row, man_spec,
                                                   per_spec, size).min)
                    for boxes in (self._min, self._max):
                        box = boxes[(side, name)]
                        snapped = snap_to_launch_grid(box.value() / sc, mode, f, 0.45,
                                                      floor_ns=floor)
                        box.setValue(snapped * sc)
        finally:
            self._loading = was
        self._recompute()

    def _sync_man_tdd_rows(self) -> None:
        """Show the Manager output timings the launch mode actually puts in force.

        A clocked launch OVERRIDES the tabulated values in `_man_tdd_range`,
        `_man_tzd_ns` and `_man_tdz_parts` — that is what the selector means, not
        a side effect — so leaving the rows showing tabulated ranges made the
        picker move the margins while the parameters those margins are computed
        from sat unchanged on screen. The number in force was nowhere on the page.
        Both columns, because `launch_mode` is shared.

        Man_t_DD is the mode's own output, so min == max and read-only. t_ZD and
        t_DZ are seeded to the data edge's point — the one placement the selector
        names, so the least surprising default — and left EDITABLE, because which
        grid point each sits on is a design choice the mode only constrains to its
        resolution. Handing all of them back to the analog mode RE-SEEDS from
        `_seed_range` rather than restoring what was there before, which does lose
        a hand edit across a round trip through a clocked mode. That is deliberate:
        while the mode held, an analog-range edit was being ignored by the model,
        so there is no edited value to honour — only a stale number.
        """
        mode = LaunchMode(self._launch_combo.currentData())
        man_spec = self._man_spec_combo.currentData()
        per_spec = self._per_spec_combo.currentData()
        size = self._size_combo.currentData()
        driven = self._launch_driven_rows()
        was, self._loading = self._loading, True    # setValue must not re-enter
        try:
            # Every candidate row, not just the driven ones: leaving a clocked mode,
            # or switching to a spec whose values the mode does not govern, has to
            # put the tabulated value back.
            for name in self._LAUNCH_ROWS:
                row = _ROW_BY_NAME[name]
                sc = _display_scale(row)
                for side in SIDES:
                    # PER SIDE, via the same predicate the margins use. The
                    # Specification column is always analog, so it reseeds tabulated
                    # here however the selector is set -- otherwise the rows on screen
                    # and the values that column computes from disagree, which is the
                    # exact drift `_launch_driven_rows` warns about.
                    clk = launch_mode_tDD_ns(self._launch_mode_for(side),
                                             self._fclk.value(), 0.45)
                    if clk is None or name not in driven:
                        rng = self._seed_range(side, row, man_spec, per_spec, size)
                        lo, hi = float(rng.min), float(rng.max)
                    else:
                        # RESEED FROM THE COLUMN, THEN SNAP -- statelessly, never by
                        # snapping whatever the box currently shows. Snapping the
                        # displayed value accumulates damage across mode changes: a
                        # 0.75 UI release visited at 2x rounds to 1.00 UI, and
                        # returning to 4x then finds 1.00 UI already on-grid and
                        # keeps it, so the design's placement is lost by a detour.
                        # Reseeding is idempotent and shows each mode's honest
                        # rendering of the same intent.
                        #
                        # AND TABLE 130 BOUNDS THE EDE RELEASE, so the row shows the
                        # latest legal point rather than the nearest one. Without it a
                        # 1/2 UI grid rounds the 0.75 UI seed UP to the boundary and
                        # displays 1.00 UI -- a release the table forbids and which is
                        # not early at all, reading as a working 2x EDE design.
                        rng = self._seed_range(side, row, man_spec, per_spec, size)
                        ede_dz = (name == "Man_t_DZ" and self._man_ede_for(side))
                        lo = hi = snap_to_launch_grid(
                            float(rng.min), mode, self._fclk.value(), 0.45,
                            floor_ns=float(rng.min),
                            ceil_ns=(ede_release_ceiling_ns(_UI_ns(self._fclk.value()))
                                     if ede_dz else None))
                    self._min[(side, name)].setValue(lo * sc)
                    self._max[(side, name)].setValue(hi * sc)
        finally:
            self._loading = was
        # The tooltips describe the PROPOSED column's boxes, so they read that
        # column's mode -- which is the selector's, since only that side is governed.
        clk = launch_mode_tDD_ns(self._launch_mode_for("proposed"),
                                 self._fclk.value(), 0.45)
        grid = launch_grid_UI(mode)
        for name in self._LAUNCH_ROWS:
            governed = clk is not None and name in driven
            fixed = governed and name in self._LAUNCH_FIXED_ROWS
            if fixed:
                tip = (f"Fixed by the Manager data launch mode "
                       f"({LAUNCH_LABELS[mode]}) at {self._fclk.value():.3f} MHz — "
                       f"select the analog delay to edit it")
            elif governed and grid is not None:
                points = ", ".join(f"{k * grid:g} UI" for k in
                                   range(int(round(1.0 / grid))))
                tip = (f"Clocked: a {mode.value} clock places this on {points} "
                       f"or 1 UI. Edited values snap to the nearest.")
            else:
                tip = ""
            for box in (self._min[("proposed", name)], self._max[("proposed", name)]):
                box.setReadOnly(fixed)
                box.setToolTip(tip)

    def _update_launch_hint(self) -> None:
        """Say what the selected launch mode costs at the current rate.

        The two clock modes are bounded from opposite ends — 4x by hold (its
        0.25 UI delay shrinks as the clock speeds up), 2x by setup (its 0.5 UI
        delay consumes half the UI) — so which is viable depends on F_CLK. A
        user changing the rate would otherwise discover the limit only as a red
        margin, with nothing to say which mode to switch to.
        """
        mode = LaunchMode(self._launch_combo.currentData())
        driven = self._launch_driven_rows()
        if mode is LaunchMode.ANALOG:
            txt = "ranges as tabulated — spread absorbed by both MP legs"
        else:
            f = self._fclk.value()
            t_lo, _ = launch_mode_tDD_ns(mode, f, 0.45) or (0.0, 0.0)
            f_max, bound = launch_mode_max_fclk_MHz(mode)
            grid = launch_grid_UI(mode) or 0.0
            over = "  ⚠ above this mode's limit" if f > f_max else ""
            # Say what it FIXES and what it merely CONSTRAINS. A reader who sees
            # three rows change needs to know t_DD is the mode's own placement
            # while t_ZD / t_DZ are choices on its grid — otherwise the editable
            # ones look like a bug and the read-only one looks arbitrary.
            free = [n for n in driven if n not in self._LAUNCH_FIXED_ROWS]
            txt = (f"Man_t<sub>DD</sub> = {t_lo:.2f} ns fixed · viable to "
                   f"~{f_max:.1f} MHz (bound by {bound}){over}")
            if free:
                txt += (f" · also clocks {' and '.join(_display_name(n) for n in free)}"
                        f", editable on this clock's {grid:g} UI grid")
        # An earlier version of this hint claimed the selector applied to the
        # baseline column only. It does not: launch_mode is shared, so it drives
        # Man_t_DD in BOTH columns. On an EDE bus the choice is a live trade, so
        # say which way rather than implying it does not apply.
        if is_ede(self._man_spec_combo.currentData()):
            txt += ("  ·  applies to BOTH columns. The EDE column is seeded for "
                    "a 2× clocked Manager (0.50 UI), preferred on Manager power "
                    "and no worse on timing: 4× costs MP hold (−0.7) and buys "
                    "only +0.2 keeper, the peripheral's leg binding either way")
        self._launch_hint.setText(txt)

    def _sync_spec_controls(self) -> None:
        """Show only the controls the selected pair can actually act on."""
        # The SoundWire system-size envelope applies to a SoundWire side only.
        # Hidden, not disabled: on the default all-SWI3S page a greyed row is
        # just a question the reader has to answer for themselves.
        mixed = (is_soundwire(self._man_spec_combo.currentData())
                 or is_soundwire(self._per_spec_combo.currentData()))
        self._size_row.setVisible(mixed)
        # A SoundWire Manager has no clock-launch option; force analog and lock it.
        sw_mgr = is_soundwire(self._man_spec_combo.currentData())
        self._launch_combo.setEnabled(not sw_mgr)
        self._launch_label.setEnabled(not sw_mgr)
        if sw_mgr and self._launch_combo.currentData() != LaunchMode.ANALOG.value:
            self._launch_combo.setCurrentIndex(
                self._launch_combo.findData(LaunchMode.ANALOG.value))
        self._update_launch_hint()
        self._update_ho_hint()

    def _save_proposed(self) -> None:
        ext = "YAML (*.yaml *.yml)" if _HAVE_YAML else "JSON (*.json)"
        default = "example.yaml" if _HAVE_YAML else "example.json"
        path, _ = QFileDialog.getSaveFileName(self, "Save Example", default, ext)
        if not path:
            return
        payload = self._serialize_side("proposed")
        try:
            with open(path, "w", encoding="utf-8") as f:
                if _HAVE_YAML and not path.lower().endswith(".json"):
                    yaml.safe_dump(payload, f, sort_keys=True)
                else:
                    import json
                    json.dump(payload, f, indent=2, sort_keys=True)
        except OSError as exc:
            QMessageBox.critical(self, "Save Example", str(exc))

    def _load_proposed(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Load Example", "",
            "Config (*.yaml *.yml *.json);;All files (*)")
        if not path:
            return
        try:
            with open(path, encoding="utf-8") as f:
                if _HAVE_YAML and not path.lower().endswith(".json"):
                    payload = yaml.safe_load(f)
                else:
                    import json
                    payload = json.load(f)
            self._deserialize_side("proposed", payload or {})
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "Load Example", f"Load failed: {exc}")
            return
        self._sync_man_tdd_rows()       # a loaded Man_t_DD cannot outrank the mode
        self._recompute()

    def _serialize_side(self, side: str) -> dict:
        out: dict = {}
        for row in _ALL_ROWS:
            out[row.name] = {
                "min": self._min[(side, row.name)].value(),
                "max": self._max[(side, row.name)].value(),
            }
        return out

    def _deserialize_side(self, side: str, payload: dict) -> None:
        self._loading = True
        try:
            for name, body in payload.items():
                if (side, name) not in self._min:
                    continue
                if "min" in body:
                    self._min[(side, name)].setValue(float(body["min"]))
                if "max" in body:
                    self._max[(side, name)].setValue(float(body["max"]))
                # a legacy "pick" key is ignored (corners are auto-selected now)
        finally:
            self._loading = False

    # ---- compute + render ----
    def _recompute(self, *_a) -> None:
        if self._loading:
            return
        spec_res = self._worst_results("spec")
        prop_res = self._worst_results("proposed")
        spec_w = {i: r.breakdowns[i] for i, r in spec_res.items()}
        prop_w = {i: r.breakdowns[i] for i, r in prop_res.items()}
        self._render_tree(spec_w, prop_w)
        self._render_advisories(next(iter(spec_res.values())))

    def _render_advisories(self, res) -> None:
        """Qualify the margins: an illegal clock rate, and the Schmitt cost.

        Both are advisory — they do not change a margin, they say what a margin
        is worth. A rate above the ceiling produces arithmetic that is fine and
        physically meaningless, and the Schmitt margin is not spec-guaranteed on
        a SoundWire Data receiver, so neither should be silently absorbed.

        ``res`` is one inequality's worst-corner result, so the Schmitt figure is
        quoted at that corner (t_RF driven to its max) rather than at nominal —
        it is the exposure, not the typical case, and is labelled as such.
        """
        bits = []
        if not res.F_CLK_legal:
            bits.append(f"above the {res.F_CLK_ceiling_MHz:g} MHz ceiling for this bus")
        if res.schmitt_cost_ns > 0:
            bits.append(f"assumes no hysteresis on the SoundWire Data RX "
                        f"(worth up to {res.schmitt_cost_ns:.2f} ns at the worst corner)")
        self._ceiling_label.setText("⚠ " + "; ".join(bits) if bits else "")
        self._ceiling_label.setToolTip("\n\n".join(res.caveats) if res.caveats else "")

    def _render_tree(self, spec_w, prop_w) -> None:
        self._tree.clear()
        fclk = self._fclk.value()
        for gi, (base, members) in enumerate(_INEQ_GROUPS):
            if gi > 0:
                spacer = QTreeWidgetItem(self._tree, ["", "", ""])   # blank line between blocks
                spacer.setFlags(Qt.NoItemFlags)
            head = QTreeWidgetItem(self._tree, [_INEQ_LABEL[base], "", ""])
            f = QFont(self._tree.font()); f.setBold(True)   # scaled base font, bold
            head.setFont(0, f)
            for mi, ineq in enumerate(members):
                bs, bp = spec_w[ineq], prop_w[ineq]
                if mi > 0:
                    gap = QTreeWidgetItem(head, ["", "", ""])        # separate the two inequalities
                    gap.setFlags(Qt.NoItemFlags)
                # Equation on its own line under the title, numbered for reference. Kept in
                # column 0 (not spanned) so column 0 sizes to fit it and the Margin column
                # lands after the equation rather than overlapping it.
                QTreeWidgetItem(head, [f"{_INEQ_NUMBER[ineq]}) "
                                       f"{self._equation_text(bs.terms)} ≥ 0", "", ""])
                left_lab, right_lab = self._column_labels()
                self._add_side_term(head, left_lab, bs, fclk)
                self._add_side_term(head, right_lab, bp, fclk)
            head.setExpanded(True)
        self._fit_tree_width()
        self._fit_tree_height()
        self._unify_section_widths()

    def _add_side_term(self, parent: QTreeWidgetItem, label: str, b, fclk: float) -> None:
        # F_max only bounds F_CLK for the setup inequalities; hold rows show "—".
        # Unit trails the number ("… MHz") to match the Margin column's "… ns".
        x = b.F_max_MHz
        if not b.is_setup:
            fmax = "—"
        elif not math.isfinite(x):
            fmax = "—"
        elif x <= 0:
            fmax = "FAIL"
        else:
            fmax = f"{x:.3f} MHz"
        # Label in a fixed-width table cell so the Example substitution lines up
        # column-wise under the Specification one (proportional font → can't pad).
        html = (f'<table border="0" cellspacing="0" cellpadding="0"><tr>'
                f'<td width="{_LABEL_CELL_W}">{label}:</td>'
                f'<td>{self._numeric_only(b.terms)}</td></tr></table>')
        child = QTreeWidgetItem(parent, [html, f"{b.margin_ns:+.2f} ns", fmax])
        if b.margin_ns < 0:
            child.setForeground(1, QBrush(_RED))
        if b.is_setup and (not math.isfinite(x) or x <= 0 or x < fclk):
            child.setForeground(2, QBrush(_RED))

    # ---- equation formatting (ported from spec_calculator) ----
    @staticmethod
    def _equation_text(terms) -> str:
        pieces: List[str] = []
        for i, t in enumerate(terms):
            op = getattr(t, "op", None) or ("-" if t.value_ns < 0 else "+")
            display_op = "−" if op == "-" else "+"
            # Anchored so a click can identify it and the delegate can wash it; the
            # operator stays outside, so `+`/`−` are not click targets and only the term
            # itself highlights.
            sym = _anchored(_term_key(t.symbol), _subscript(t.symbol))
            if i == 0:
                pieces.append(("−" if op == "-" else "") + sym)
            else:
                pieces.append(f" {display_op} {sym}")
        return "".join(pieces)

    @staticmethod
    def _numeric_only(terms) -> str:
        """The substitution row: one number per term, or the PRODUCT where a term is one.

        A crossing term reads `X_MP,late · Man_t_RF,DATA` in the equation row, and its
        coefficient appears nowhere else on the row -- so the substitution shows
        `1.4414·6.00` rather than the pre-multiplied 8.65, and the reader can check the
        multiplication against the symbol beside it.

        The swing term `1.67·Man_t_RF` expands too, for the OTHER operand's sake: the
        multiplier is in the symbol but the slew in force is not, so the row used to print
        8.33 and leave `t_RF = 5.00` to be recovered by division. Which operand is missing
        decides this, not which one is named -- see `Term.factors`.

        A GATHERED crossing shows all THREE numbers: `(1.44 − 0.45)·5.00`. Its symbol is
        `(X_PP,A,late − X_PP,B,early) · Man_t_RF,CLK` — two crossings of one pin netted into a
        single product — and printing only the netted coefficient would leave the reader unable
        to check the netting against the symbol beside it, which is the entire reason this row
        expands products in the first place. `Term.factors` carries the coefficients first and
        the row value last, so the count of entries is what selects the form.

        Terms with no product to show (`Man_t_DD`, `Per_t_IS`) and terms whose printed
        number IS the operand (`1·UI`) stay single numbers.
        """
        pieces: List[str] = []
        for i, t in enumerate(terms):
            f = getattr(t, "factors", None)
            if not f:
                num = f"{abs(t.value_ns):.2f}"
            elif len(f) == 2:
                num = f"{f[0]:.2f}·{f[1]:.2f}"
            else:
                # Coefficients signed relative to the term's hoisted operator, row value last.
                inner = "".join(
                    f"{c:.2f}" if k == 0 else f" {'−' if c < 0 else '+'} {abs(c):.2f}"
                    for k, c in enumerate(f[:-1]))
                num = f"({inner})·{f[-1]:.2f}"
            op = getattr(t, "op", None) or ("-" if t.value_ns < 0 else "+")
            display_op = "−" if op == "-" else "+"
            # THE SAME KEY AS THE SYMBOL, which is what makes "and the numbers" free:
            # `_numeric_only` walks `terms` in the same order as `_equation_text`, one
            # number per term, so the two rows need no alignment work to agree.
            num = _anchored(_term_key(t.symbol), num)
            if i == 0:
                pieces.append(("−" if op == "-" else "") + num)
            else:
                pieces.append(f" {display_op} {num}")
        return "".join(pieces)

    @staticmethod
    def _fmt_fmax(x: float) -> str:
        if not math.isfinite(x):
            return "—"
        return "FAIL" if x <= 0 else f"{x:.3f}"
