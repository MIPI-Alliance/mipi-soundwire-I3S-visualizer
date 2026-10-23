"""Timing mode — the SWI3S Spec Calculator (the Qt counterpart of
`timing-analysis/app/spec_calculator.py`), covering both forwarded-clock
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
times are slower; its mandatory F_CLK is half). They are two spec sources, chosen
per side, so a PHY1 Manager talking to a PHY2 Peripheral is expressible.

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

  The **launch mode** selects how the Manager places its data edge. An analog
  delay line gives Man_t_DD as a range, whose min-to-max spread the inequalities
  absorb on both MP legs at once (the max erodes setup, only the min funds hold).
  A clock-launched edge (2x or 4x) lands on a known clock phase, so min == max.

  It governs all three Manager output timings together — t_DD, t_ZD and t_DZ —
  because one Manager has one output stage.

  t_DD and t_ZD are the two DATA LAUNCH METHODS — from an already-driving output,
  or out of high-Z — and both must satisfy the same setup and hold requirements at
  the receiver; the revision states them identically ("9–23 ns or (0.5 UI 2x CLK,
  0.25 UI 4x CLK)" for each). A clocked mode sets their RESOLUTION: a 4x clock
  offers 0.00, 0.25, 0.50 and 0.75 UI, a 2x clock only 0.00 and 0.50. The rows
  stay editable, snapped to a point the selected clock can place.

  The two clock options are bounded from opposite ends (4x by hold, 2x by setup),
  so which is viable depends on F_CLK — the hint beneath the selector reports that
  bound at the current rate.
- A "Timing Inequalities" tree: one always-expanded row per inequality
  (MP/PM × setup/hold), each evaluated at *its own* worst corner, with the
  symbolic equation in the header and Specification / Example child rows showing
  the numeric substitution, Margin, and Max Frequency (every leg whose margin
  scales with UI; "—" where no rate bounds it) — failing margins / F_max coloured
  red.
- One Specification-vs-Example parameter grid per section (shared name column,
  Spec and Example min/max side by side, then the unit).

All compute lives in `swi3s_studio.timing` (validated against the timing-analysis
reference spreadsheets); this view only edits CalcRow ranges and formats the
CalcResults.
"""
from __future__ import annotations

import math
import re
from typing import Callable, Dict, List, Optional, Tuple

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

from .. import import_notes
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
    ("RX Timing", ["Man_t_IS", "Man_t_IH", "Per_t_IS", "Per_t_IH"]),
    ("TX Timing", ["Man_t_DD", "Per_t_DD"]),
    ("Handover Timing", ["Man_t_DZ", "Per_t_DZ", "Man_t_ZD", "Per_t_ZD"]),
    ("Rise/Fall Times", ["t_RF Man CLK", "t_RF Man DATA", "t_RF Per DATA",
                         "τ (per-edge slew)"]),
    ("System Properties", ["δ (V_SEOS tol)", "α (V_noise / V_SEOS)", "bus length (cm)",
                           "t_PD,mis fraction"]),
]

# All rows as masters (lock off), used to build the widgets and seed values.
# default_swi3s_rows() carries the PHY2 spec values; PHY1 overrides a handful below.
_ALL_ROWS: List[CalcRow] = default_swi3s_rows(lock_mgr_lanes=False)
_ROW_BY_NAME: Dict[str, CalcRow] = {r.name: r for r in _ALL_ROWS}

# PHY1 (FBCSE-slow) shares PHY2's inequality structure exactly — the SWI3S spec
# (v1.1r06 §11.2) documents PHY1 & PHY2 together and points both at the SAME
# tables ("PHY1 & PHY2 Input Timing" for RX setup/hold, "PHY1 & PHY2 Output Timing" for
# TX timing, "PHY2 Output Timing: Effect of EndDriveEarly" for EndDriveEarly) and the SAME
# "PHY1 & PHY2 Measurement" waveforms
# (§11.2.7).
#
# But the two tables are structured DIFFERENTLY, and the difference matters when
# reasoning about a revision rather than just reading a number:
#
#   The "PHY1 & PHY2 Input Timing" table (Man/Per_tIS, tIH) is genuinely shared — ONE row
#   per parameter, no PHY1/PHY2 distinction. (r06 names these with a `_Min`
#   suffix: Per_tIS_Min etc., a max on the min a datasheet may publish.)
#
#   The "PHY1 & PHY2 Output Timing" table prints EVERY parameter as a separate PHY1 row
#   and PHY2 row and NEVER collapses them. Three rows differ; three agree:
#       differ:  Per_tDD 14-50/2-20 · Per_tZD 14-50/2-18 · Man_tZD 14-30/2-18
#       agree:   Man_tDD 7-18 · Man_tDZ 0-10 · Per_tDZ 0-10
#   Only the differing rows need an override, so only they appear below — but
#   the agreeing ones are two coincident rows, NOT one shared value. Do not
#   reason from "the output timing table doesn't split it": it splits everything. If a
#   future revision diverges Man_tDD, this dict is where PHY1's row belongs.
#
# Everything else (V thresholds §11.2.2, t_RF in the single "PHY1 & PHY2 Output Ramp Time"
# table, noise, bus geometry) is shared or is not a device-spec value at all.
_PHY1_SPEC: Dict[str, ParamRange] = {
    # "PHY1 & PHY2 Output Timing" table, §11.2.3 — the PHY1 rows that DIFFER from PHY2
    # (PHY2 values, for contrast, live in the default rows).
    "Per_t_DD": ParamRange(14.0, 32.0, 50.0),   # Per_tDD  14–50 ns  (PHY2 2–20)
    "Per_t_ZD": ParamRange(14.0, 32.0, 50.0),   # Per_tZD  14–50 ns  (PHY2 2–18)
    "Man_t_ZD": ParamRange(14.0, 22.0, 30.0),   # Man_tZD  14–30 ns  (PHY2 2–18)
}
# PROPOSED revision, as ranges. Seeds the editable Example column so the page
# opens on a current-vs-proposed comparison (see _reset_to_defaults).
#
# Held here as ParamRanges rather than read from SpecPreset because the preset
# carries a SINGLE value for t_ZD and t_IH — enough for SoundWire, where they are
# single-valued, but the SWI3S proposal revises them as ranges.
_PHY2_PROPOSED: Dict[str, ParamRange] = {
    "Man_t_DD": ParamRange(9.0, 16.0, 23.0),    # 7-18   -> 9-23
    "Man_t_ZD": ParamRange(9.0, 16.0, 23.0),    # 2-18   -> 9-23 (tracks Man_t_DD)
    "Per_t_DD": ParamRange(2.0, 5.5, 9.0),      # 2-20   -> 2-9
    "Per_t_ZD": ParamRange(2.0, 5.5, 9.0),      # 2-18   -> 2-9  (tracks Per_t_DD)
    "Per_t_IH": ParamRange(0.0, 0.75, 1.5),     # 0-4.5  -> 0-1.5
    "Man_t_DZ": ParamRange(0.0, 4.5, 9.0),      # 0-10   -> 0-9
    "Per_t_DZ": ParamRange(0.0, 4.5, 9.0),      # 0-10   -> 0-9
}

# The proposal for a PHY1 side.
#
# The split follows how the SPEC IS STRUCTURED, not which numbers happen to
# coincide (v1.1r06, verified against the "PHY1 & PHY2 Input Timing" and "PHY1 & PHY2
# Output Timing" tables):
#
#   The "PHY1 & PHY2 Input Timing" table is genuinely shared — one row per parameter, no
#   PHY1/PHY2 distinction at all. Revising Per_tIH 0-4.5 -> 0-1.5 therefore revises it for
#   BOTH PHYs; there is no PHY1 row left holding 4.5. It carries.
#
#   The "PHY1 & PHY2 Output Timing" table prints EVERY parameter as a separate PHY1 row
#   and PHY2 row — it never collapses the two. So a revision to PHY2's row says nothing
#   about PHY1's, even where the two currently hold the same numbers:
#     Per_tDD  PHY1 14-50 vs PHY2 2-20   |  Man_tDD PHY1 7-18 == PHY2 7-18
#     Per_tZD  PHY1 14-50 vs PHY2 2-18   |  Man_tDZ 0-10      == 0-10
#     Man_tZD  PHY1 14-30 vs PHY2 2-18   |  Per_tDZ 0-10      == 0-10
#
# Man_t_DD, Man_t_DZ and Per_t_DZ are in the second column — rows that agree today. Their
# PHY2 revisions (9-23, 0-9, 0-9) carry onto PHY1 by decision, not because the "PHY1 & PHY2
# Output Timing" table implies it.
#
# Per_t_DD, Per_t_ZD and Man_t_ZD are in the FIRST column, and get PHY1's OWN revised
# ranges, stated for PHY1 rather than derived from PHY2's.
_PHY1_PROPOSED: Dict[str, ParamRange] = {
    "Man_t_DD": _PHY2_PROPOSED["Man_t_DD"],      # output timing, per-PHY rows that agree
    "Man_t_DZ": _PHY2_PROPOSED["Man_t_DZ"],      # 0-10  -> 0-9
    "Per_t_DZ": _PHY2_PROPOSED["Per_t_DZ"],      # 0-10  -> 0-9
    "Per_t_IH": _PHY2_PROPOSED["Per_t_IH"],      # input timing, one shared row
    "Per_t_DD": ParamRange(14.0, 28.0, 42.0),    # PHY1 14-50 -> 14-42
    "Per_t_ZD": ParamRange(17.0, 29.5, 42.0),    # PHY1 14-50 -> 17-42
    "Man_t_ZD": ParamRange(22.0, 39.0, 56.0),    # PHY1 14-30 -> 22-56
}

# The EDE candidate's analog turn-on / turn-off range, ns. The column's values are in
# `_ede_ranges`. The PERIPHERAL's release is not a row: it is an interval from its own
# ramp completion, carried by `CalcInputs.Per_ede_hold_*`.
_EDE_TURN_MIN, _EDE_TURN_MAX = 3.0, 9.0     # 3:1 analog turn-on / turn-off

# The EDE rows that are UI FRACTIONS rather than absolute nanoseconds. They follow
# F_CLK: `_refresh_ede_rate_rows` rewrites them at the new rate.
#
# EMPTY: every EDE value is absolute (`_ede_ranges`). A placement stated as a fraction of
# the UI must be listed here, or it carries across a rate change unscaled.
# `test_ede_ui_relative_rows_follow_the_rate` checks the behaviour, not the membership.
_EDE_UI_RELATIVE_ROWS: tuple[str, ...] = ()


def _UI_ns(f_clk_MHz: float, duty_min: float = 0.45) -> float:
    """UI at the duty floor, matching CalcInputs.UI_ns."""
    return 1000.0 * duty_min / f_clk_MHz if f_clk_MHz > 0 else 0.0


def _ede_ranges(UI_ns: float) -> Dict[str, ParamRange]:
    """The EDE column's proposed values, all absolute nanoseconds.

    `UI_ns` is unused: no value here is a UI fraction (so `_EDE_UI_RELATIVE_ROWS` is
    empty). The parameter stays so a rate-relative seed needs no new argument through
    `_seed_range`.

        Manager     launches (t_DD, t_ZD) at the revision's analog 9-23 ns and stops
                    driving AT the UI boundary: `Man_t_DZ,max` = 0 against Note 1's own
                    reference in the "PHY1 & PHY2 Output Timing" table, the bottom of the
                    ratified 0-10 ns range.
                    `Man_t_DZ` is a pure delay from the Manager's internal timing
                    reference for its own clock edge (Note 3), so the move to high-Z
                    can be gated from that edge. This is not an EndDriveEarly release:
                    `_man_ede_for` returns False and the EndDriveEarly ceiling does not bound it.
                    `CalcInputs.Man_ede` keeps a mid-UI Manager release expressible
                    from code.

        Peripheral  has no mid-UI reference (Sec. 11.1.5). It holds its own COMPLETED
                    level for 3-9 ns and then releases. That interval is
                    `CalcInputs.Per_ede_hold_*`, NOT a row here. Its t_DD, t_ZD and
                    t_IH are the revision's values.

    No clocked launch is seeded; select 4x in the launch mode combo to see one.
    """
    return {
        # The launch, and the OTHER launch method: the revision's own analog range,
        # unchanged. t_DD and t_ZD both launch a data edge -- from an already-driving
        # output, or out of high-Z -- and both must satisfy the same setup and hold
        # requirements at the receiver, so the revision states them identically.
        "Man_t_DD": ParamRange(9.0, 16.0, 23.0),
        "Man_t_ZD": ParamRange(9.0, 16.0, 23.0),
        # The release: AT the boundary, under the end-of-UI reference of Note 1 of the
        # "PHY1 & PHY2 Output Timing" table.
        # Collapsed to a point because it is gated from a clock edge the Manager owns.
        "Man_t_DZ": ParamRange(0.0, 0.0, 0.0),
        # Peripheral: the revision's own values, unchanged.
        "Per_t_DD": ParamRange(2.0, 5.5, 9.0),
        # Also the revision's own value. A peripheral's t_ZD is analog (about 3:1 over
        # PVT); `test_ede_peripheral_values_are_not_tighter_than_3_to_1` rejects a
        # tighter range. See ../timing-analysis/docs/MODEL_AUDIT.md.
        "Per_t_ZD": ParamRange(2.0, 5.5, 9.0),
        "Per_t_IH": ParamRange(0.0, 0.75, 1.5),
    }



_PROPOSED_BY_SPEC: Dict[str, Dict[str, ParamRange]] = {
    "SWI3S_PHY2": _PHY2_PROPOSED,
    "SWI3S_PHY1": _PHY1_PROPOSED,
}

# Mandatory max F_CLK is 6.6 MHz (PHY1) / 13.2 MHz (PHY2) — the "PHY1 & PHY2 Clock
# Frequencies" table. The spec gives no nominal, so these are TARGETS to open on, not
# limits: the limit is `fclk_ceiling_MHz`, which clamps whatever is seeded here.
#
# PHY2 opens on 12.288 MHz — 256 x 48 kHz, the audio design point — not the
# 13.2 MHz mandatory maximum. PHY1 opens on its mandatory maximum, 6.6 MHz.
_SPEC_FCLK_TARGET: Dict[str, float] = {"SWI3S_PHY1": 6.6, "SWI3S_PHY2": 12.288}
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

def _red() -> QColor:
    """The failing-margin red of the palette in force (read per use, so it follows a
    theme switch)."""
    return QColor(VizTheme.SEM_ERROR)


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
_SECTION_GAP = _px(6)   # vertical gap between sections


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
# NAMED FOR WHO OWES WHAT TO WHOM, not for a direction arrow: every leg pairs ONE device's
# output timing against ANOTHER device's receiver window, and the title says which is which.
#
# THE REQUIREMENT NAMES THE LEG, NOT THE MECHANISM: "Setup" (the level is ready in time for
# the other device's window), not "Launching"; "Holding" (the level persists).
#
# P->P, the only family with two devices of the same class, uses the letters the terms use:
# A drives, B samples, on all four of its legs.
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
    # Named by DIRECTION, like the setup/hold headings above. The equation on each row
    # shows the bound itself.
    "MP_contention": "Handover Contention — Manager to Peripheral",
    "PM_contention": "Handover Contention — Peripheral to Manager",
    "PP_contention": "Handover Contention — Peripheral to Peripheral",
    # NAMED FOR WHO RELEASES, not for whose keeper: there is one keeper, in the Manager
    # ({ASW3805}), so "Bus Keeper — Manager Releasing" means that keeper with the MANAGER
    # letting go.
    # Two headings: two inequalities, each with its own corners and binding device.
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
    # One heading per RELEASING device: the same keeper, two independent margins.
    ("keeper_Man", ("keeper_Man",)),
    ("keeper_Per", ("keeper_Per",)),
]

# Reference numbers, 1) … 17), in DISPLAY order — so "inequality 9" names the same row on
# screen and in the summary text. One map, derived from the display grouping and read by
# both renderers.
#
# ")" AND NOT ".": the equations are full of the "·" operator and of decimals like
# "1.44·8.30", so a leading "1." would read as part of the arithmetic.
_INEQ_NUMBER = {ineq: n for n, ineq in enumerate(
    (m for _base, members in _INEQ_GROUPS for m in members), start=1)}


# ---------------------------------------------------------------------------
# Clickable terms: symbol → the INPUT ROW it comes from
# ---------------------------------------------------------------------------
#
# Clicking a term highlights every occurrence of the same thing: THE ROW A READER WOULD
# EDIT rather than the printed symbol. Clicking the Manager's clock slew highlights every
# leg it acts on and the `t_RF Man CLK` row above. The symbols follow the same rule
# (`_trf_sym`: name the lane so the term points at the row).
#
# So `Man_t_DD,pure` and `Man_t_DD` are ONE key: the `,pure` suffix is an anchor conversion
# of one row's value, not a second parameter. A crossing keys on its t_RF LANE, because that
# is the row in the grid -- the dimensionless coefficient has no row of its own, and a
# gathered `(X_PP,A,late − X_PP,B,early) · Man_t_RF,CLK` is still one lane however many
# crossings it nets.
#
# TERMS WITH NO ROW key on themselves, so they stay clickable: `UI` (derived from the
# F_CLK spin box), `Man_t_Keeper_Response` and `Per_t_hold` (CalcInputs fields the grid
# does not expose). Listed rather than detected, so
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

    THE HIT-TEST AND THE PAINT SHARE `_doc` AND `_origin`, so the clickable boxes stay on
    the drawn text.
    """

    def __init__(self, parent=None, keys=None):
        super().__init__(parent)
        self.keys = keys or (lambda: frozenset())

    def _doc(self, opt: QStyleOptionViewItem) -> QTextDocument:
        doc = QTextDocument()
        doc.setDefaultFont(opt.font)
        doc.setDocumentMargin(0)
        # ANCHORS TAKE THE TEXT COLOUR EXPLICITLY: Qt's rich-text CSS resolves
        # `color: inherit` to BLACK rather than to the paint context's Text role. The doc
        # is rebuilt on every paint, so a palette switch is picked up; only column 0 uses
        # this delegate, so the red negative margins in the Margin column are untouched.
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

        `opt` MUST COME FROM THE VIEW'S OWN `initViewItemOption`, so the caller passes it
        in: `initFrom(view)` leaves `opt.font` at Qt's default 9 pt while paint receives
        the view's font, and the hit-test document would lay out at a different width.
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

    Clicking a term toggles it; clicking in empty space clears the set (see `_hit`). The
    pointer turns to a pointing hand over a term.
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

        The operators between terms are not click targets, so a click on a `−` lands on
        the text but on no term: that is INERT, while a click in empty space clears.
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

        `_hit` initialises the option with the view's own `initViewItemOption`, so the
        document the hit-test lays out is the document paint lays out (see
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
    for "subscript from here". `Keeper_Response` is a token like any other under that rule.
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
    setButtonSymbols; zeroing the sub-controls here keeps the frame tight. The buttons'
    `subcontrol-origin: border` is required: without it, a styled spin box on Windows lays
    its edit field out 1 px wide and shows no value."""
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
        subcontrol-origin: border; width: 0px; height: 0px; border: none;
    }}
    """



# Keys an earlier version's settings carry that set_from_dict reads or ignores by design.
_LEGACY_KEYS = frozenset({"phy", "lock_mgr_lanes", "spec::man_shape", "spec::per_shape"})


def _unknown_choice(key: str, value, combo: QComboBox) -> str:
    return (f"'{key}' {value!r} is not one of the choices offered; "
            f"{combo.currentText() or combo.currentData()!r} is kept")


def _set_spin(spin, value, label: str, clamped: List[str]) -> None:
    """Set a spin box from a saved number, adding `label` to `clamped` when the number is
    outside the box's range (the box takes the nearest end)."""
    v = float(value)
    spin.setValue(v)
    if v < spin.minimum() or v > spin.maximum():
        clamped.append(f"{label} = {v:g} (read as {spin.value():g})")


class _DoubleClickLabel(QLabel):
    """A title that reports a double-click (the maximize toggle, as on a pane's title)."""
    doubleClicked = Signal()

    def mouseDoubleClickEvent(self, ev) -> None:  # noqa: N802 - Qt override
        if ev.button() == Qt.LeftButton:
            ev.accept()
            self.doubleClicked.emit()
            return
        super().mouseDoubleClickEvent(ev)


class TimingView(QWidget):
    # A double-click on the Timing Inequalities title: the window toggles the maximized view
    # (set_maximized), as its ⤢ button and View ▸ Maximize Pane do.
    maximizeRequested = Signal()

    def __init__(self) -> None:
        super().__init__()
        self._loading = False
        self._maximized = False
        self._min: Dict[Tuple[str, str], QDoubleSpinBox] = {}
        self._max: Dict[Tuple[str, str], QDoubleSpinBox] = {}
        self._row_labels: Dict[str, QLabel] = {}
        # Every style built from the palette, as (widget, css builder), so retheme() can
        # rebuild them.
        self._themed: List[Tuple[QWidget, Callable[[], str]]] = []
        self._man_shape: Optional[QComboBox] = None      # shared across Spec + Example
        self._per_shape: Optional[QComboBox] = None

        root = QVBoxLayout(self)
        root.setContentsMargins(6, 6, 6, 6)
        self.setFont(_base_font())                        # scales combos/buttons/tree text
        self.setStyleSheet(_scaled_stylesheet())

        # Title, then the bus it describes. Both left-aligned and full width, with
        # no buttons on the title's row (they would clip it).
        self._title = QLabel("SWI3S & SoundWire Timing Calculator")
        self._title.setStyleSheet(_TITLE_CSS)
        self._title.setContentsMargins(_HEAD_INDENT, 0, 0, 0)
        root.addWidget(self._title)

        # Which bus the numbers below describe. Dimmed and directly under the
        # fixed title, so the configuration is legible at a glance without the
        # title itself changing under the reader.
        self._subtitle = QLabel("")
        self._theme(self._subtitle, lambda: f"color:{VizTheme.TEXT_DIM};")
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

        # The title row: the title, and the window's ⤢ button at the tree's right edge
        # (add_title_button). Its width follows the tree's (_unify_section_widths).
        self._results_head = QWidget()
        head = QHBoxLayout(self._results_head)
        head.setContentsMargins(0, 0, 0, 0)
        self._results_title = _DoubleClickLabel("Timing Inequalities")
        self._results_title.setStyleSheet(_HEADER_CSS)
        self._results_title.setContentsMargins(_HEAD_INDENT, 0, 0, 0)
        self._results_title.doubleClicked.connect(self.maximizeRequested.emit)
        head.addWidget(self._results_title)
        head.addStretch(1)
        self._head_layout = head
        head_row = QHBoxLayout()
        head_row.setContentsMargins(0, 0, 0, 0)
        head_row.addWidget(self._results_head)
        head_row.addStretch(1)
        body.addLayout(head_row)
        # Names the clickable terms, the page's only click targets.
        self._trace_hint = QLabel("Click a term to trace it through every inequality and "
                                  "its input row; click it again, or any blank space, to clear.")
        self._theme(self._trace_hint, lambda: f"color:{VizTheme.TEXT_DIM};")
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
        self._theme(self._tree.header(), lambda: (
            f"QHeaderView::section {{ background:{VizTheme.FRAME_BG}; border:none;"
            " padding:2px 6px; }"))
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

        self._params = self._build_param_panes()
        body.addWidget(self._params)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(host)
        root.addWidget(scroll, 1)

        self._sync_spec_controls()          # size picker only applies to a SoundWire side
        self._reset_to_defaults()           # seed values + first compute

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
        for bar in (getattr(self, "_topbar", None), getattr(self, "_results_head", None)):
            if bar is not None:
                bar.setFixedWidth(w)

    def showEvent(self, e) -> None:      # noqa: N802 - Qt override
        super().showEvent(e)
        # Now that real font metrics apply, re-fit the tree and match the cards.
        self._fit_tree_width()
        self._fit_tree_height()
        self._unify_section_widths()

    # ---- maximized: the Timing Inequalities given the whole page ----
    def add_title_button(self, button: QWidget) -> None:
        """Put `button` (the window's ⤢) at the right of the Timing Inequalities title."""
        self._head_layout.addWidget(button)

    def is_maximized(self) -> bool:
        return self._maximized

    def set_maximized(self, on: bool) -> None:
        """Give the Timing Inequalities the whole page: the part above the scrolled area (the
        page title and the top bar) is hidden, and the parameter sections still scroll below
        the inequalities. Off, the whole page."""
        self._maximized = bool(on)
        for w in (self._title, self._subtitle, self._topbar):
            w.setVisible(not self._maximized)

    # ---- top control bar ----
    def _build_top_bar(self) -> QVBoxLayout:
        # One labelled control per row, so nothing clips when the bar is pinned
        # to the (narrower) results width: a cramped row overlaps its
        # neighbour's text rather than eliding. Reset/Save/Load sit on the
        # F_CLK row.
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

        # The Manager / Peripheral edge-ramp models, one of each, shared by Spec and
        # Example.
        ramp = QHBoxLayout()
        ramp.setContentsMargins(_HEAD_INDENT, 0, 0, 0)
        ramp.setSpacing(_px(6))
        self._man_shape = self._make_shape_combo()
        self._per_shape = self._make_shape_combo()
        ramp.addWidget(QLabel("Manager ramp model"))
        ramp.addWidget(self._man_shape)
        ramp.addSpacing(_px(16))
        ramp.addWidget(QLabel("Peripheral ramp model"))
        ramp.addWidget(self._per_shape)
        ramp.addStretch(1)
        outer.addLayout(ramp)

        # SoundWire's small/large system envelope (C_Bus / L_Bus, Tables 4-5).
        # Meaningless for an all-SWI3S bus, so the whole row is HIDDEN unless a
        # side is SoundWire — not merely disabled.
        #
        # Wrapped in a widget: Qt hides widgets, and a layout with every child
        # hidden still holds its spacing.
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

        # Manager launch architecture: how the Manager launches its data edge. An
        # analog delay line carries a min-to-max spread that the inequalities
        # absorb on BOTH sides at once (the max erodes MP setup, only the min
        # funds MP hold). A clock-launched edge lands on a known clock phase, so
        # min == max. Only meaningful for a SWI3S Manager; a SoundWire one has no
        # such option.
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
        # so which one is viable changes with F_CLK; this hint reports it. On its
        # own row so it can run to full width.
        self._launch_hint = QLabel("")
        self._theme(self._launch_hint, lambda: f"color:{VizTheme.TEXT_DIM};")
        self._launch_hint.setWordWrap(True)
        self._launch_hint.setContentsMargins(_HEAD_INDENT, 0, 0, 0)
        outer.addWidget(self._launch_hint)

        # Whether a UI is allocated to the handover. Checked = one UI scheduled,
        # cleared = none, and the three handover inequalities lose their N_HO·UI
        # term with it.
        #
        # A checkbox: one UI or none. `CalcInputs.handover_UIs` is a float, so
        # "programmed to be longer for a larger system" (§11.1.1.1) is
        # expressible from code only.
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
        self._pp_combo.setMinimumWidth(_px(215))   # fits "driver near, sampler far"
        self._pp_combo.setCurrentIndex(self._pp_combo.findData("unknown"))
        self._pp_combo.currentIndexChanged.connect(self._on_pp_placement_changed)
        ho_row.addWidget(self._pp_combo)
        ho_row.addStretch(1)
        outer.addLayout(ho_row)

        # Says which case is on screen: clearing the box on PHY2 makes the
        # handover legs fail with the CURRENT numbers, which is the spec's answer.
        self._ho_hint = QLabel("")
        self._theme(self._ho_hint, lambda: f"color:{VizTheme.TEXT_DIM};")
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
        # Reset/Save/Load ride the F_CLK row: it is the shortest labelled row, so
        # there is width to spare, and their right edge lands on the card/tree edge.
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
        self._theme(self._ceiling_label, lambda: f"color:{VizTheme.SEM_ERROR};")
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
        # One name-column width for every card, wide enough for the widest section title
        # (it sits in that column), so the boxes line up across the sections.
        probe = QLabel()
        probe.setStyleSheet(_HEADER_CSS)
        probe.ensurePolished()
        self._name_col_w = max([_NAME_COL_W] + [
            probe.fontMetrics().horizontalAdvance(title) + _px(8) for title, _rows in SECTIONS])
        for section_title, row_names in SECTIONS:
            outer.addSpacing(_SECTION_GAP)
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
        an EDE bus it shifts up a step to the revision against EDE.
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
        # section's grid, its title in the grid's top-left cell beside the column
        # headings.
        card = QFrame()
        card.setObjectName("card")
        self._theme(card, lambda: (
            f"QFrame#card {{ border:{VizTheme.BORDER_WIDTH}px solid {VizTheme.BORDER};"
            f" border-radius:{VizTheme.CORNER_RADIUS}px; }}"))
        col = QVBoxLayout(card)
        col.setContentsMargins(10, 6, 10, 6)
        col.setSpacing(0)

        grid = QGridLayout()
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(6)     # set, not inherited: the card's own spacing is 0
        grid.setVerticalSpacing(2)
        # Fixed column widths so Spec and Example boxes line up vertically across
        # every section (regardless of each section's unit text width).
        grid.setColumnMinimumWidth(0, self._name_col_w)
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
        title = QLabel(section_title)
        title.setStyleSheet(_HEADER_CSS)
        grid.addWidget(title, 0, 0, 2, 1, Qt.AlignLeft | Qt.AlignVCenter)
        grid.addWidget(lh, 0, 1, 1, 2)
        grid.addWidget(rh, 0, 5, 1, 2)
        for c in (1, 2, 5, 6):
            grid.addWidget(self._col_head("min" if c in (1, 5) else "max"), 1, c)
        for r, name in enumerate(row_names, start=2):
            self._add_param_row(name, grid, r)
        col.addLayout(grid)
        return card

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
        # Held so `_sync_row_highlight` can wash it: a term's key IS a row name.
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
            # threshold window on the way from the spin boxes to the model.
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
        """Whether the MANAGER releases early on `side`.  It does not, on either.

        THE MANAGER'S RELEASE IS NOT AN EndDriveEarly RELEASE.  `CalcInputs.Man_ede`
        means that `Man_t_DZ` is referenced to the START of the last driven UI, one UI
        earlier than the reference of Note 1 of the "PHY1 & PHY2 Output Timing" table, which
        is the calculator's convention for a mid-UI EndDriveEarly release.  The proposal has the
        Manager stopping AT the boundary -- `Man_t_DZ,max` = 0 under the ORDINARY reference -- so
        the shift does not apply, the EndDriveEarly ceiling does not bound it, and the launch grid
        has nothing to place.
        `_ede_ranges` sets the value; this predicate says which reference it is against.
        With `Man_ede` true and a row of 0.0, `_effective_t_dz_ns` would put the release
        a whole UI early.

        `Man_ede` stays in `CalcInputs` for a mid-UI Manager release:
        `tests/test_timing_vs_reference.py` evaluates a 0.25 / 0.75 grid placement
        through it.

        `_sync_man_tdd_rows` calls this too, to decide whether the EndDriveEarly ceiling bounds
        the Man_t_DZ row: keep one predicate for both.
        """
        return False

    def _launch_mode_for(self, side: str) -> LaunchMode:
        """The Manager's data launch method on `side`.

        A CLOCKED LAUNCH IS THE REVISION'S OPTION, NOT A TABULATED ONE. The wording
        that offers it -- "9-23 ns or (0.5 UI 2x CLK, 0.25 UI 4x CLK)" -- is in the
        PHY2 revision. Ratified PHY1 and PHY2 tabulate analog output delays and give
        a Manager no multiplied clock to launch from, so the Specification column is
        always ANALOG however the selector is set. As with `Man_ede` above, the
        column decides, not the combo: a clocked mode on the Specification column
        would snap its tabulated values onto the grid.
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
                self._fmt_fmax(bs.F_max_MHz),
                self._fmt_fmax(bp.F_max_MHz),
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

    def set_from_dict(self, d: dict, source: Optional[str] = None) -> None:
        """Restore the inputs `to_dict` saved. With a `source` (the file's name), a key this
        version does not read, a choice none of the selectors offers and a number outside
        its field's range (set to the nearest end) are reported through import_notes."""
        notes: List[str] = []
        known = set(self.to_dict()) | _LEGACY_KEYS
        unknown = sorted(str(k) for k in d if k not in known and not str(k).endswith("::pick"))
        if unknown:
            more = f", and {len(unknown) - 5} more" if len(unknown) > 5 else ""
            notes.append(f"{len(unknown)} setting(s) this version does not read, ignored: "
                         f"{', '.join(unknown[:5])}{more}")
        self._loading = True
        try:
            # Legacy "lock_mgr_lanes" is ignored; a legacy per-side "spec::man_shape"
            # falls back into the now-shared picker.
            #
            # A legacy "phy" key (one global PHY switch, before the per-side spec
            # selector) restores as that PHY on BOTH sides. An explicit
            # man_spec/per_spec always wins.
            legacy_phy = d.get("phy")
            legacy_default = ("SWI3S_PHY1" if legacy_phy == 1 else "SWI3S_PHY2")
            # A dict with no spec keys predates the cross-spec selector, so it can
            # only have described an all-SWI3S bus.
            for combo, key in ((self._man_spec_combo, "man_spec"),
                               (self._per_spec_combo, "per_spec")):
                j = combo.findData(d.get(key, legacy_default))
                if j >= 0:
                    combo.setCurrentIndex(j)
                else:
                    notes.append(_unknown_choice(key, d.get(key, legacy_default), combo))
            k = self._size_combo.findData(d.get("sw_system_size", "small"))
            if k >= 0:
                self._size_combo.setCurrentIndex(k)
            else:
                notes.append(_unknown_choice("sw_system_size", d.get("sw_system_size"),
                                             self._size_combo))
            # A dict with no launch_mode predates the selector, so it can only
            # have described the analog delay.
            lm = self._launch_combo.findData(
                d.get("launch_mode", LaunchMode.ANALOG.value))
            if lm >= 0:
                self._launch_combo.setCurrentIndex(lm)
            else:
                notes.append(_unknown_choice("launch_mode", d.get("launch_mode"),
                                             self._launch_combo))
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
            else:
                notes.append(_unknown_choice("pp_placement", d.get("pp_placement"),
                                             self._pp_combo))
            self._sync_spec_controls()
            clamped: List[str] = []
            if "fclk_target" in d:
                _set_spin(self._fclk, d["fclk_target"], "fclk_target", clamped)
            for combo, key, legacy in ((self._man_shape, "man_shape", "spec::man_shape"),
                                       (self._per_shape, "per_shape", "spec::per_shape")):
                value = d.get(key, d.get(legacy))
                self._set_shape(combo, value)
                if value is not None and combo is not None and combo.findData(value) < 0:
                    notes.append(_unknown_choice(key, value, combo))
            for side in SIDES:
                for row in _ALL_ROWS:
                    self._apply_keys(side, row.name, d, clamped)
            if clamped:
                more = f", and {len(clamped) - 3} more" if len(clamped) > 3 else ""
                notes.append(f"{len(clamped)} value(s) outside their field's range set to the "
                             f"nearest end: {', '.join(clamped[:3])}{more}")
        finally:
            self._loading = False
        if source is not None:
            for msg in notes:
                import_notes.warn(source, msg)
        # A saved Man_t_DD is only meaningful under the analog mode; if the
        # restored launch mode is clocked, the mode wins and the row says so.
        self._sync_man_tdd_rows()
        self._update_title()
        self._recompute()

    def _apply_keys(self, side: str, name: str, d: dict,
                    clamped: Optional[List[str]] = None) -> None:
        # A legacy "::pick" key (pre-auto-corner) is simply ignored.
        kmin, kmax = f"{side}::{name}::min", f"{side}::{name}::max"
        out = clamped if clamped is not None else []
        if kmin in d:
            _set_spin(self._min[(side, name)], d[kmin], kmin, out)
        if kmax in d:
            _set_spin(self._max[(side, name)], d[kmax], kmax, out)

    # ---- actions ----
    def _update_title(self) -> None:
        """Fixed page title, with the selected bus named beneath it.

        The subtitle restates the pair because which side is which changes the
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

        Used by `_reset_to_defaults` and by `_sync_man_tdd_rows` (which re-seeds a
        SINGLE row when the launch mode hands Man_t_DD back), so both read one
        precedence order.

        The read-only Specification column shows the spec as it stands; the
        editable Example column seeds from the proposed revision, so the page
        opens on a direct current-vs-proposed comparison — which is what the two
        columns are for. A SoundWire side has no proposal and seeds both columns
        identically; a PHY1 side gets only the shared part of it (see
        _PHY1_PROPOSED). Gated per SIDE, so a mixed bus still gets each side's
        own proposal.

        When a side is on the EDE source the comparison SHIFTS UP a step: the
        baseline becomes the proposed revision (not the current spec) and the
        candidate becomes EDE. Column headers follow, via _column_labels().
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
                # Candidate: EDE (`_ede_ranges`).
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

        It does not route through `_on_spec_changed`: that calls
        `_reset_to_defaults`, which would re-seed every row and reset F_CLK to
        the PHY default.

        It does re-derive the rows it governs (see `_sync_man_tdd_rows`), which
        is not a reseed: those rows are the mode's output, not the user's input.
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

    def _theme(self, widget: QWidget, css: Callable[[], str]) -> None:
        """Style `widget` from the palette now, and again on every retheme()."""
        self._themed.append((widget, css))
        widget.setStyleSheet(css())

    def retheme(self) -> None:
        """Re-read the palette (View ▸ Appearance, or the OS switching when it is followed):
        the page stylesheet, every palette-built style, the highlighted input rows, and the
        results, whose failing margins carry the palette's red. The tree's equations need
        nothing: the delegate reads the palette on every paint."""
        self.setStyleSheet(_scaled_stylesheet())
        for widget, css in self._themed:
            widget.setStyleSheet(css())
        self._sync_row_highlight()
        self._recompute()
        self._tree.viewport().update()

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
        offers only the binary case.
        """
        return 1.0 if self._ho_ui_check.isChecked() else 0.0

    def _update_ho_hint(self) -> None:
        """Name the case on screen, and say what a cleared box means.

        Clearing it on PHY2 makes the handover legs FAIL at the current numbers,
        because PHY2's t_ZD,min (2 ns) cannot cover a t_DZ,max of 10 ns without a
        UI to separate the reference edges. The hint names EndDriveEarly, which
        reaches a zero-UI handover by changing t_DZ.
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

        Only the rows in `_EDE_UI_RELATIVE_ROWS`, which are *defined* as fractions
        of the UI, are rewritten. Everything else in the column, edits included,
        is left untouched: reseeding the whole column would discard the user's
        edits.
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
    # Manager has one output stage. See `calculator._man_tzd_clocked`.
    #
    # Each is constrained to the mode's RESOLUTION: a 4× clock offers 0.00, 0.25,
    # 0.50 and 0.75 UI and the three do not have to agree; a 2× clock offers 0.00
    # and 0.50. They stay editable and are snapped to a placeable point.
    _LAUNCH_ROWS = ("Man_t_DD", "Man_t_ZD", "Man_t_DZ")
    # Nothing is pinned. The mode says which points exist; the rows say which are
    # used.
    _LAUNCH_FIXED_ROWS: tuple[str, ...] = ()

    def _launch_driven_rows(self) -> tuple[str, ...]:
        """Which of `_LAUNCH_ROWS` the launch mode governs on the current column.

        All three, on every column including EDE. EDE changes what t_DZ MEANS, not
        whether the selected clock can place it. The same rule decides the margins, in
        `calculator._man_tzd_clocked` / `_man_tdz_clocked`; keep the two in step, or
        the row and the term disagree.
        """
        return self._LAUNCH_ROWS

    def _snap_launch_rows(self) -> None:
        """Move the editable clocked rows onto a point the selected clock places.

        An off-grid t_ZD or t_DZ describes a Manager that cannot be built with the
        selected clock. Snapped on `editingFinished` so it does not fight the
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
        `_man_tzd_ns` and `_man_tdz_parts`, so the rows show the values in force.
        Each column follows its own mode (`_launch_mode_for`).

        A governed row is seeded from `_seed_range`, snapped to the grid (min ==
        max), and left EDITABLE: which grid point each sits on is a design choice
        the mode only constrains to its resolution. Handing them back to the analog
        mode RE-SEEDS from `_seed_range` rather than restoring what was there
        before, so a hand edit is lost across a round trip through a clocked mode;
        while the mode held, the model ignored an analog-range edit.
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
                    # here however the selector is set, matching the values that
                    # column computes from.
                    clk = launch_mode_tDD_ns(self._launch_mode_for(side),
                                             self._fclk.value(), 0.45)
                    if clk is None or name not in driven:
                        rng = self._seed_range(side, row, man_spec, per_spec, size)
                        lo, hi = float(rng.min), float(rng.max)
                    else:
                        # RESEED FROM THE COLUMN, THEN SNAP -- statelessly, never by
                        # snapping whatever the box currently shows: snapping the
                        # displayed value accumulates rounding across mode changes.
                        #
                        # THE EndDriveEarly CEILING BOUNDS AN EDE RELEASE (`ceil_ns`), so the row
                        # shows the latest legal point rather than the nearest one.
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
        delay consumes half the UI) — so which is viable depends on F_CLK.
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
            # Say what it FIXES and what it merely CONSTRAINS to its grid.
            free = [n for n in driven if n not in self._LAUNCH_FIXED_ROWS]
            txt = (f"Man_t<sub>DD</sub> = {t_lo:.2f} ns fixed · viable to "
                   f"~{f_max:.1f} MHz (bound by {bound}){over}")
            if free:
                txt += (f" · also clocks {' and '.join(_display_name(n) for n in free)}"
                        f", editable on this clock's {grid:g} UI grid")
        # On an EDE bus, name the column the selector applies to (see _launch_mode_for).
        if is_ede(self._man_spec_combo.currentData()):
            txt += ("  ·  applies to the EDE column only; the Specification column "
                    "keeps its tabulated analog delays. The EDE column is seeded with "
                    "the analog 9–23 ns launch")
        self._launch_hint.setText(txt)

    def _sync_spec_controls(self) -> None:
        """Show only the controls the selected pair can actually act on."""
        # The SoundWire system-size envelope applies to a SoundWire side only.
        # Hidden, not disabled.
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
            with import_notes.collect() as notes:
                self._deserialize_side("proposed", payload or {},
                                       source=import_notes.source_name(path))
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "Load Example", f"Load failed: {exc}")
            return
        self._sync_man_tdd_rows()       # a loaded Man_t_DD cannot outrank the mode
        self._recompute()
        show = getattr(self.window(), "show_import_warnings", None)
        if callable(show):
            show(notes, import_notes.source_name(path))
        else:
            import_notes.extend(notes)

    def _serialize_side(self, side: str) -> dict:
        out: dict = {}
        for row in _ALL_ROWS:
            out[row.name] = {
                "min": self._min[(side, row.name)].value(),
                "max": self._max[(side, row.name)].value(),
            }
        return out

    def _deserialize_side(self, side: str, payload: dict, source: Optional[str] = None) -> None:
        """Set one side's ranges from {row: {"min", "max"}}. With a `source`, a row this
        version does not have, an entry that is not a mapping and a number outside its
        field's range are reported through import_notes."""
        unknown: List[str] = []
        clamped: List[str] = []
        self._loading = True
        try:
            for name, body in payload.items():
                if (side, name) not in self._min or not isinstance(body, dict):
                    unknown.append(str(name))
                    continue
                if "min" in body:
                    _set_spin(self._min[(side, name)], body["min"], f"{name} min", clamped)
                if "max" in body:
                    _set_spin(self._max[(side, name)], body["max"], f"{name} max", clamped)
                # a legacy "pick" key is ignored (corners are auto-selected now)
        finally:
            self._loading = False
        if source is None:
            return
        if unknown:
            more = f", and {len(unknown) - 5} more" if len(unknown) > 5 else ""
            import_notes.warn(source, f"{len(unknown)} row(s) this version does not have or "
                                      f"cannot read, ignored: {', '.join(unknown[:5])}{more}")
        if clamped:
            more = f", and {len(clamped) - 3} more" if len(clamped) > 3 else ""
            import_notes.warn(source, f"{len(clamped)} value(s) outside their field's range set "
                                      f"to the nearest end: {', '.join(clamped[:3])}{more}")

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
        # F_max on every leg whose margin scales with UI, setup or not; "—" where no rate
        # bounds it, "FAIL" where no rate closes it.
        # Unit trails the number ("… MHz") to match the Margin column's "… ns".
        x = b.F_max_MHz
        if not math.isfinite(x):
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
            child.setForeground(1, QBrush(_red()))
        if (math.isfinite(x) and x < fclk) or (b.is_setup and not math.isfinite(x)):
            child.setForeground(2, QBrush(_red()))

    # ---- equation formatting (as in spec_calculator) ----
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
        multiplier is in the symbol but the slew in force is not. Which operand is missing
        decides this, not which one is named -- see `Term.factors`.

        A GATHERED crossing shows all THREE numbers: `(1.44 − 0.45)·5.00`. Its symbol is
        `(X_PP,A,late − X_PP,B,early) · Man_t_RF,CLK` — two crossings of one pin netted into a
        single product — so the reader can check the netting against the symbol beside it.
        `Term.factors` carries the coefficients first and
        the row value last, so the count of entries is what selects the form.

        Terms with no product to show (`Man_t_DD`, `Per_t_IS`) and terms whose printed
        number IS the operand (`1·UI`) stay single numbers. A single number is the term's own
        value, its contribution with the operator taken off, so a subtracted negative value
        reads `− (−2.00)` and the row adds up to the margin.
        """
        pieces: List[str] = []
        for i, t in enumerate(terms):
            f = getattr(t, "factors", None)
            op = getattr(t, "op", None) or ("-" if t.value_ns < 0 else "+")
            if not f:
                own = t.value_ns if op == "+" else -t.value_ns
                num = f"{abs(own):.2f}" if round(own, 2) >= 0 else f"(−{abs(own):.2f})"
            elif len(f) == 2:
                num = f"{f[0]:.2f}·{f[1]:.2f}"
            else:
                # Coefficients signed relative to the term's hoisted operator, row value last.
                inner = "".join(
                    f"{c:.2f}" if k == 0 else f" {'−' if c < 0 else '+'} {abs(c):.2f}"
                    for k, c in enumerate(f[:-1]))
                num = f"({inner})·{f[-1]:.2f}"
            display_op = "−" if op == "-" else "+"
            # THE SAME KEY AS THE SYMBOL: `_numeric_only` walks `terms` in the same order
            # as `_equation_text`, one number per term, so the two rows agree.
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
