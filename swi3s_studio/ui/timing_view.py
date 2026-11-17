"""Timing mode — the SWI3S Spec Calculator, covering both forwarded-clock
single-ended PHYs.

A per-side **spec selector** picks what each end of the bus is built to: SWI3S
PHY1, SWI3S PHY2, or SoundWire 1.3 at 1.2 V. The Manager and Peripheral
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
describes the same physical event graph — and a SoundWire side is carried as its SWI3S
equivalents (`spec_source.sw13_equivalents`):
each value re-measured between the SWI3S anchors for the same event, on the side it is on.
A SoundWire Data input's crossings use its own 35-65 % threshold window. Selecting a
SoundWire side also surfaces the f_Clock ceiling (no SoundWire envelope reaches SWI3S PHY2's
mandatory 13.2 MHz) as an advisory that qualifies the margins without changing them.

The Specification column is read-only; only the Example column is editable.
Where a spec has a proposed revision, the Example column seeds from it, so the
page opens on a direct current-vs-proposed comparison.

Compare the SWI3S specification against an example implementation. Corners are
selected automatically — each inequality is evaluated at its own worst corner —
so there is no manual corner picker. The page:

- A fixed title, its tooltip naming the selected bus, with Reset, Save / Load Example
  Settings (the Example column, saved as JSON; loaded as JSON, or as YAML where PyYAML is
  installed) and Export Results (`export_results`) at the right of its row; then a top control
  bar the page's full width: the Manager's spec selector over the Peripheral's, each with its
  ramp model beside it, the SoundWire system-size envelope under them while a side is
  SoundWire, then the Manager data launch with the Manager forces keeper switch, the
  Peripheral Placement with the Handover UI box, and the F_CLK target, packed beside the
  selectors and under them into as few rows as the width allows (`_pack_top_bar`).

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
  so which is viable depends on F_CLK — the selector's tooltip reports that
  bound at the current rate, read from the Example column's own Manager-to-Peripheral legs.
- A "Timing Inequalities" tree: one always-expanded heading per inequality family
  (M→P, P→M and P→P setup and hold with their handover variants, the three contention legs
  and the two keeper legs), 17 numbered inequalities in all. Each is evaluated at *its own*
  worst corner, with the symbolic equation in the header and a child row per column (named
  by `_column_labels`) showing the numeric substitution ending in " = " and its margin (green
  where positive, red where negative, the text colour for "—"), then the leg's F_max in
  parentheses, "(F<sub>MAX</sub> 15.128 MHz)" (`FMAX_HTML`, `_fmax_text`; only where the column's margin depends on the
  clock rate, through a UI term at its corner or a Manager t_DD / t_ZD / t_DZ a clocked launch
  places at a fraction of the UI, `_shown_fmax`: the lowest rate at which it fails as F_CLK rises,
  nothing where it never fails, and a row tooltip naming any higher rates at which it passes
  again), an F_max below the rate in red. The tree
  has that one column. Beside its title each column names its binding F_max, the lowest over
  the legs that bound the bus, each at its own worst corner, and the number of the inequality
  that sets it (`_render_binding`, `calculator.binding_at_own_corners`). A term prints as `_shown_symbol` gives it: a crossing without its
  ,late / ,early, an anchor correction or a ramp to V_OH / V_OL without its ,fast, the
  keeper's response as Man_t_Keeper.
- One Specification-vs-Example parameter grid per section (shared name column,
  Spec and Example min/max side by side, then the unit). A row's name toggles its
  parameter's highlight, as a click on one of its terms does.
- The term diagram (`inequality_diagram.InequalityDiagram`), on the right behind a horizontal
  splitter. The page opens on inequality 1) drawn whole, as do Reset and Load Example Settings
  (`_show_default_diagram`). A click on a term in an inequality draws that term of that
  inequality at each column's worst corner (`_on_term_clicked`). A click on the text of an
  inequality's heading or equation row, off its terms, draws that inequality whole there
  instead, and a second click on that inequality closes the pane (`_on_inequality_clicked`).
  The heading of the inequality drawn whole is washed like a highlighted term, and titles the
  drawing with the inequality's number (`_heading_title`). The splitter gives the inequalities
  the width their typical row needs (`_typical_row_width`), a wider row scrolling sideways, and
  the diagram the rest (`_fit_split`) until the handle is dragged. Its ⤢, or a double-click
  on it, gives it the whole page with the columns side by side (`set_diagram_maximized`);
  beside the inequalities it is drawn at the not-maximized scale (`TimingDiagram.set_reduced`),
  the columns stacked. The buttons beside the ⤢ draw either column alone or both, in either
  view, the choice kept in QSettings (`_set_diagram_show`). While the diagram has the page,
  the corner row carries ← and → beside the ⤢ (also the Left and Right keys, while the focus
  is in this view or nowhere): each draws the previous or next inequality of the tree, in its
  numbered order, whole, with the term highlights cleared, and disables at the first and the
  last (`step_inequality`). A leg is named by its heading and, where the heading holds two
  launches, its launch: "1) Manager Setup for Peripheral, t_DD" (`_leg_name`).
  Its context menu, and the Timing file menu (`diagram_export_actions`), export it as drawn
  to PNG, SVG or PDF, in the page's palette or on the light one (`export_diagram`).

All compute lives in `swi3s_studio.timing` (cross-checked against the vendored
reference fixture, `tests/test_timing_vs_reference.py`); this view only edits CalcRow ranges
and formats the CalcResults. A recompute reads its inputs on the UI thread and runs the corner
search on a worker (`latest_job.LatestJob`); only the latest recompute's result is shown, and a
read of the results (`flush_recompute`) waits for it.

From the start of a recompute until its result is shown, the page holds no results
(`_blank_results`): every substitution number, margin and F_max in the tree
shows `_PENDING` in the dim text colour, keeping the tree's rows and size; the launch selector's
tooltip gives the viable rate as `_PENDING`; the term diagram keeps its size and draws "Computing…" in place of its
panels; the line beside the Timing Inequalities title reads "Computing…" and the binding line
is empty. A recompute superseded before its result is shown leaves the page blank until the
latest one's result.

A recompute starts on a discrete action (a selector, a checkbox, Reset, Load Example Settings, a
settings restore) and on a committed edit of a typed number: Enter, or focus leaving the
field (`editingFinished`), and only when the committed value differs from the one the last
recompute read. A keystroke, an arrow-key step or a value set from code starts none; a read of
the results (`flush_recompute`) recomputes any input changed since the last recompute.
"""
from __future__ import annotations

import csv
import functools
import html
import json
import math
import os
import re
import unicodedata
import weakref
from typing import Callable, Dict, List, Optional, Sequence, Tuple, cast

from PySide6.QtCore import QCoreApplication, QEvent, QPoint, QPointF, QSettings, QSize, Qt, Signal
from PySide6.QtGui import (
    QAbstractTextDocumentLayout,
    QAction,
    QColor,
    QContextMenuEvent,
    QFont,
    QGuiApplication,
    QKeySequence,
    QPalette,
    QTextDocument,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QAbstractSpinBox,
    QApplication,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMenu,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpacerItem,
    QSplitter,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QToolButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .. import import_notes
from ..build_id import display_version
from ..export.commands_csv import _text_cell
from ..fsutil import write_whole
from ..timing import (
    INEQUALITIES,
    PP_PLACEMENT_LABELS,
    CalcInputs,
    CalcRow,
    ParamRange,
    apply_row_picks,
    compute,
    default_swi3s_rows,
    find_worst_corner_rows_all,
)
from ..timing.calculator import (
    PP_MIS_SYMBOL,
    CalcResults,
    Inequality,
    InequalityBreakdown,
    Pick,
    binding_at_own_corners,
    placement_corner_text,
    toggle_ceiling_MHz,
)
from ..timing.spec_source import (
    LAUNCH_LABELS,
    SPEC_LABELS,
    SPEC_SOURCES,
    LaunchMode,
    SpecSource,
    SystemSize,
    default_handover_UIs,
    is_ede,
    is_proposed,
    is_soundwire,
    launch_grid_UI,
    launch_mode_tDD_ns,
    preset,
    snap_to_launch_grid,
)
from ..timing.timeline import leg_timeline
from . import timers
from .inequality_diagram import InequalityDiagram
from .latest_job import LatestJob
from .save_path import suffixed_save_path
from .theme import VizTheme, analyzer_stylesheet
from .timing_diagram import EXPORT_FORMATS, DiagramSection, pct_text

try:
    import yaml
    _HAVE_YAML = True
except Exception:                                  # pragma: no cover - env dependent
    _HAVE_YAML = False

# The QSettings key of the term diagram's column choice (`TimingView._set_diagram_show`): "spec",
# "proposed" or "both", the default.
_DIAGRAM_COLUMNS_KEY = "timing_diagrams/columns"
_DIAGRAM_COLUMNS = ("spec", "proposed", "both")


def _saved_diagram_show() -> str:
    """The term diagram's column choice kept in QSettings, "both" where none or another value
    is kept."""
    value = QSettings().value(_DIAGRAM_COLUMNS_KEY, "both")
    return value if value in _DIAGRAM_COLUMNS else "both"


# The QSettings key of the folder the Timing page's file dialogs last used, shared with the
# window's Open / Save Timing Settings.
_LAST_DIR_KEY = "capture/last_dir_timing"

# The largest Example file Load Example Settings reads, and the YAML it reads: a mapping of rows, with
# no anchors or aliases (an alias is how a few lines expand to an unbounded document).
_MAX_EXAMPLE_BYTES = 1 << 20

if _HAVE_YAML:
    class _ExampleLoader(yaml.SafeLoader):
        def compose_node(self, parent, index):
            if self.check_event(yaml.events.AliasEvent):
                raise yaml.YAMLError("YAML aliases are not read in an Example file")
            return super().compose_node(parent, index)

SIDES = ("spec", "proposed")

# Section layout — group default_swi3s_rows() into named boxes.
SECTIONS: List[Tuple[str, List[str]]] = [
    ("RX Thresholds", ["V_IH", "V_IL"]),
    ("RX Timing", ["Man_t_IS", "Man_t_IH", "Per_t_IS", "Per_t_IH"]),
    ("TX Timing", ["Man_t_DD", "Per_t_DD"]),
    ("Handover Timing", ["Man_t_DZ", "Per_t_DZ", "Man_t_ZD", "Per_t_ZD",
                         "Man_t_EDE", "Per_t_EDE"]),
    ("Rise/Fall Times", ["t_RF Man CLK", "t_RF Man DATA", "t_RF Per DATA",
                         "τ (per-edge slew)"]),
    ("System Properties", ["δ (V_SEOS tol)", "α (V_noise / V_SEOS)", "bus length (cm)",
                           "t_PD,mis fraction"]),
]

# All rows as masters (lock off), used to build the widgets and seed values.
# default_swi3s_rows() carries the PHY2 spec values; PHY1 overrides a handful below.
_ALL_ROWS: List[CalcRow] = default_swi3s_rows(lock_mgr_lanes=False)
_ROW_BY_NAME: Dict[str, CalcRow] = {r.name: r for r in _ALL_ROWS}
# The rows that set the bus length, which a pinned placement's corner line names by the
# peripherals' distance instead (`_diagram_sections`).
_BUS_ROWS = frozenset(r.name for r in _ALL_ROWS if "bus_length_cm" in r.binds)

# PHY1 (FBCSE-slow) shares PHY2's inequality structure exactly — the SWI3S spec
# (§11.2) documents PHY1 & PHY2 together and points both at the SAME
# tables ("PHY1 & PHY2 Input Timing" for RX setup/hold, "PHY1 & PHY2 Output Timing" for
# TX timing, "PHY2 Output Timing: Effect of EndDriveEarly" for EndDriveEarly) and the SAME
# "PHY1 & PHY2 Measurement" waveforms
# (§11.2.7).
#
# But the two tables are structured DIFFERENTLY, and the difference matters when
# reasoning about a revision rather than just reading a number:
#
#   The "PHY1 & PHY2 Input Timing" table (Man/Per_tIS, tIH) is genuinely shared — ONE row
#   per parameter, no PHY1/PHY2 distinction.
#
#   The "PHY1 & PHY2 Output Timing" table prints EVERY parameter as a separate PHY1 row
#   and PHY2 row and NEVER collapses them. Three rows differ; three agree:
#       differ:  Per_tDD 14-50/2-20 · Per_tZD 14-50/2-18 · Man_tZD 14-30/2-18
#       agree:   Man_tDD 7-18 · Man_tDZ 0-10 · Per_tDZ 0-10
#   Only the differing rows need an override, so only they appear below — but
#   the agreeing ones are two coincident rows, NOT one shared value. A parameter whose
#   PHY1 row diverges from PHY2's belongs in this dict.
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
# coincide (verified against the "PHY1 & PHY2 Input Timing" and "PHY1 & PHY2
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

def _ede_ranges() -> Dict[str, ParamRange]:
    """The EDE column's values, all absolute nanoseconds: the proposed revision's.

    A side on the EDE source releases by its EDE Delay row (seeded from
    `default_swi3s_rows`) instead of its t_DZ row (`CalcInputs.Man_ede` / `Per_ede`).
    No value is a fraction of the UI, so none moves with F_CLK.
    """
    return dict(_PHY2_PROPOSED)


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

# What a result the running recompute will replace shows until it is in (`_blank_results`).
_PENDING = "…"
# The line beside the Timing Inequalities title while a recompute runs.
_COMPUTING = "Computing…"
# The role on a tree row's column 0 holding the column label of a substitution row, so the
# row can be blanked to its label and `_PENDING` (`TimingView._blank_tree`).
_SUBST_LABEL_ROLE = Qt.UserRole + 1
# The role on an inequality's heading and equation rows' column 0 holding the inequality a click
# on the row's text, off its terms, draws whole (`TimingView._on_inequality_clicked`): the
# heading's first inequality, an equation row's own.
_WHOLE_ROLE = Qt.UserRole + 2
# The role on a substitution row's column 0 holding the F_max its text shows after the margin
# (`_fmax_text`), "" where it shows none.
_FMAX_ROLE = Qt.UserRole + 3


def _default_fclk_for(man: SpecSource, per: SpecSource, size: SystemSize) -> float:
    """The rate Reset seeds for a Manager on `man` and a Peripheral on `per`
    (`TimingView._default_fclk`): the lower of the two specs' targets, clamped to the pair's
    ceiling."""
    base = min(_SPEC_FCLK_TARGET.get(man, _FCLK_TARGET_DEFAULT),
               _SPEC_FCLK_TARGET.get(per, _FCLK_TARGET_DEFAULT))
    return min(base, CalcInputs(Man_spec=man, Per_spec=per,
                                system_size=size).F_CLK_ceiling_MHz())


def _column_labels_for(man: SpecSource, per: SpecSource) -> Tuple[str, str]:
    """The (left, right) column headings for a Manager on `man` and a Peripheral on `per`
    (`TimingView._column_labels`)."""
    man_ede, per_ede = is_ede(man), is_ede(per)
    if man_ede and per_ede:
        return "PHY2 Proposed", "PHY2 EDE"
    if man_ede or per_ede:
        return "Spec / PHY2 Proposed", "Example / PHY2 EDE"
    return "Specification", "Example"


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


# Rows whose value may be negative: a setup demand below zero lets the data cross after the
# clock reference (a SoundWire Manager's t_IS equivalent is −1.0 ns).
_SIGNED_ROWS = ("Man_t_IS", "Per_t_IS")

# Rows only the Example column holds. The Specification column releases every side by its
# t_DZ (`TimingView._base_inputs`), so it has no EDE Delay: its cells read `_NOT_IN_SPEC`,
# no box holds a value, its corner search has no such row and its settings carry no key.
_EXAMPLE_ONLY_ROWS = ("Man_t_EDE", "Per_t_EDE")
_NOT_IN_SPEC = "does not apply"


def _side_rows(side: str) -> List[CalcRow]:
    """The rows column `side` holds: every row on the Example column, and every row but
    `_EXAMPLE_ONLY_ROWS` on the Specification column."""
    return [r for r in _ALL_ROWS if side != "spec" or r.name not in _EXAMPLE_ONLY_ROWS]

# Rows that belong to one side of the bus, and which SpecPreset field seeds them.
# When a side is switched to SoundWire these are reseeded from that spec's tables;
# everything else (thresholds, noise, bus geometry) is a property of the bus or of
# the shared modelling convention, not of one device's spec.
_MAN_ROW_FIELDS: Dict[str, Tuple[str, str]] = {
    "Man_t_DD": ("t_DD_min_ns", "t_DD_max_ns"),
    "Man_t_IS": ("t_IS_max_ns", "t_IS_max_ns"),
    "Man_t_IH": ("t_IH_max_ns", "t_IH_max_ns"),
    "Man_t_DZ": ("t_DZ_min_ns", "t_DZ_max_ns"),
    "Man_t_ZD": ("t_ZD_min_ns", "t_ZD_ns"),
}
_PER_ROW_FIELDS: Dict[str, Tuple[str, str]] = {
    "Per_t_DD": ("t_DD_min_ns", "t_DD_max_ns"),
    "Per_t_IS": ("t_IS_max_ns", "t_IS_max_ns"),
    "Per_t_IH": ("t_IH_max_ns", "t_IH_max_ns"),
    "Per_t_DZ": ("t_DZ_min_ns", "t_DZ_max_ns"),
    "Per_t_ZD": ("t_ZD_min_ns", "t_ZD_ns"),
}
# t_RF rows follow their side's spec too — SoundWire's tabulated slew differs
# from SWI3S's, and every crossing and anchor scales with it.
_MAN_TRF_ROWS = ("t_RF Man CLK", "t_RF Man DATA")
_PER_TRF_ROWS = ("t_RF Per DATA",)


def _row_range_for_spec(name: str, spec: SpecSource, side: str,
                        system_size: SystemSize) -> Optional[ParamRange]:
    """Spec min/typ/max for a per-side row, or None if the row isn't side-specific.

    Driven by the preset rather than by which spec family it is, so any source
    with its own numbers reseeds: a SoundWire side from its SWI3S equivalents
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


def _margin_text(x: float) -> str:
    """A substitution row's margin: signed to the hundredth with its unit; "—" where it is not
    finite."""
    return f"{x:+.2f} ns" if math.isfinite(x) else "—"


# The label of an F_max: F with MAX as a subscript, and in plain text (the copied summary) the
# page's plain-text subscript form, an underscore before the subscript (as `Man_t_DD` is
# Man_t<sub>DD</sub>).
FMAX_HTML = "F<sub>MAX</sub>"
FMAX_PLAIN = "F_MAX"


def _fmax_text(x: float) -> str:
    """A substitution row's F_max: to the thousandth with its unit, "FAIL" where the leg fails
    at the lowest rates, and "" where it never fails (no F_max is shown)."""
    if not math.isfinite(x):
        return ""
    return "FAIL" if x <= 0 else f"{x:.3f} MHz"


def _binding_fmax_text(x: float) -> str:
    """A column's binding F_max (`TimingView._render_binding`): as `_fmax_text`, and "no limit"
    where no bounding leg fails at any rate."""
    return _fmax_text(x) or "no limit"


def _has_ui_term(terms) -> bool:
    """Whether `terms`, an inequality's at its corner, hold a UI term with a nonzero
    coefficient: the UI, or N·UI with N > 0. Such an inequality shows an F_max
    (`_shown_fmax`)."""
    for t in terms:
        head, dot, tail = t.symbol.rpartition("·")
        if t.symbol == "UI" or (dot and tail == "UI" and re.fullmatch(r"\d+(\.\d+)?", head)
                                and float(head) > 0):
            if t.value_ns != 0.0:
                return True
    return False


# The Manager output timings a clocked launch places at a fraction of the UI
# (`calculator._man_clocked_grid_ns`).
_CLOCKED_MAN_SYMBOLS = frozenset({"Man_t_DD", "Man_t_ZD", "Man_t_DZ"})


def _has_clocked_man_term(terms) -> bool:
    """Whether `terms`, an inequality's at its corner, hold a nonzero Manager t_DD, t_ZD or
    t_DZ, which a clocked launch places at a fraction of the UI."""
    return any(t.symbol in _CLOCKED_MAN_SYMBOLS and t.value_ns != 0.0 for t in terms)


def _shown_fmax(b, clocked: bool = False) -> float:
    """The F_max breakdown `b` shows: its own where its margin depends on the clock rate,
    else NaN, which shows none ("—" in the summary). The margin depends on the rate where its
    terms hold a UI term (`_has_ui_term`) or, with `clocked` (the column's Manager launch is
    clocked), a Manager t_DD, t_ZD or t_DZ (`_has_clocked_man_term`)."""
    if _has_ui_term(b.terms) or (clocked and _has_clocked_man_term(b.terms)):
        return b.F_max_MHz
    return math.nan


def _margin_colour(x: float) -> str:
    """The colour of a margin `x` in the palette in force: the diagram's margin green where
    positive, the page's red where negative, the text colour at zero and where not finite."""
    if not math.isfinite(x) or x == 0:
        return VizTheme.TEXT
    return VizTheme.DIAG_OK if x > 0 else VizTheme.SEM_ERROR


def _whole_class(base: str) -> str:
    """The CSS class the delegate washes while an inequality under heading `base` (an
    `_INEQ_GROUPS` key) is drawn whole."""
    return "w" + "".join(c if c.isalnum() else "_" for c in base)


def _whole_marked(base: str, body: str) -> str:
    """Heading `base`'s title `body`, washed while an inequality under it is drawn whole."""
    return f'<span class="{_whole_class(base)}">{body}</span>'


def _summary_label(label: str) -> str:
    """A result column's name in the copyable summary: the page's `_column_labels` entry,
    with the specification column named "Spec" wherever the summary names it."""
    return "Spec" if label == "Specification" else label


def _fmax_columns(left: str, right: str) -> List[str]:
    """The summary table's header for result columns named `left` and `right` (the page's
    `_column_labels`)."""
    left, right = _summary_label(left), _summary_label(right)
    return ["Inequality", f"{left} margin (ns)", f"{right} margin (ns)", "ΔMargin (ns)",
            f"{left} {FMAX_PLAIN} (MHz)", f"{right} {FMAX_PLAIN} (MHz)",
            f"Δ{FMAX_PLAIN} (MHz)"]

# Uniform scale for the whole Timing page (fonts AND fixed pixel dimensions scale
# together, so proportions and alignment are preserved). Scoped to this view.
_S = 1.25


def _px(n: float) -> int:
    return round(n * _S)


_FONT_BODY = _px(12)     # matches the theme body size, scaled
_FONT_HEADER = _px(15)   # section headers (theme HEADER_CSS, scaled)
_FONT_TITLE = _px(18)    # page title (theme TITLE_CSS, scaled)
_FONT_DIAGRAM = _px(14)  # the term diagram's body font, its labels' size
_HEADER_CSS = f"font-size:{_FONT_HEADER}px; font-weight:600;"
_TITLE_CSS = f"font-size:{_FONT_TITLE}px; font-weight:600;"

# The top bar's gap between rows, between blocks on a row, and the width it is packed to before
# the page has one (`TimingView._arrange_top_bar`).
_TOPBAR_ROW_GAP = 4
_TOPBAR_BLOCK_GAP = _px(24)
_TOPBAR_DEFAULT_W = 1700


def _pack_top_bar(width: int, pickers_w: int, widths: Sequence[int], rows: Sequence[int],
                  gap: int, indent: int = 0) -> Tuple[List[List[int]], List[List[int]]]:
    """The top bar's packing of blocks of `widths` into `width`, beside the pickers block of
    `pickers_w` and below it: (the blocks on each of the two rows beside the pickers, the
    blocks on each row below them), by index. Each block in turn takes its row `rows[i]`
    beside the pickers where it fits there, else the other one, else the rows below, where
    the blocks follow one another in order and a block that does not fit starts a new row.
    Blocks on a row are `gap` apart, the pickers `gap` from the blocks beside them, and a row
    below them starts `indent` in."""
    room = width - pickers_w - gap
    side: List[List[int]] = [[], []]
    used = [0, 0]
    rest: List[int] = []
    for i, w in enumerate(widths):
        for r in (rows[i], 1 - rows[i]):
            need = w + (gap if side[r] else 0)
            if used[r] + need <= room:
                side[r].append(i)
                used[r] += need
                break
        else:
            rest.append(i)
    below: List[List[int]] = []
    line: List[int] = []
    taken = 0
    for i in rest:
        need = widths[i] + (gap if line else 0)
        if line and taken + need > width - indent:
            below.append(line)
            line, need = [], widths[i]
            taken = 0
        line.append(i)
        taken += need
    if line:
        below.append(line)
    return side, below

# Fixed widths (scaled) so the Spec/Example boxes line up vertically across every
# section regardless of the longest label / unit text in any one section.
_NAME_COL_W = _px(150)
_BOX_W = _px(54)        # min/max spin width — fixed so columns align across sections
_UNIT_COL_W = _px(52)   # unit column width — fits the widest unit ("·V_DD")
_GAP_W = _px(40)        # gap between the Spec and Example groups
_HEAD_INDENT = _px(4)   # ~½ char: nudge titles/top-bar in to sit under the card borders
_LABEL_CELL_W = _px(96)  # minimum label cell; widened to the longest column heading
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
# The inequalities in that order: the order the maximized diagram's Previous and Next step
# through (`TimingView.step_inequality`).
_INEQ_ORDER: Tuple[str, ...] = tuple(sorted(_INEQ_NUMBER, key=_INEQ_NUMBER.__getitem__))
# The launch that tells apart the legs sharing a heading: a setup or hold leg launches the
# data from a driving output (t_DD), its "_ho" sibling from high-Z (t_ZD). A leg with a
# heading to itself (contention, keeper) has none. Written as `Man_t_DD` is, in plain text.
_LEG_LAUNCH = {m: "t_ZD" if m.endswith("_ho") else "t_DD"
               for _base, members in _INEQ_GROUPS if len(members) > 1 for m in members}


# ---------------------------------------------------------------------------
# Clickable terms: symbol → the INPUT ROW it comes from
# ---------------------------------------------------------------------------
#
# Clicking a term highlights every occurrence of the same thing: THE ROW A READER WOULD
# EDIT rather than the printed symbol. Clicking the Manager's clock slew highlights every
# leg it acts on and the `t_RF Man CLK` row above. The symbols follow the same rule
# (`_trf_sym`: name the lane so the term points at the row).
#
# So `Per_t_DD,anchor,rise` and `Per_t_DD` are ONE key: the `,anchor` suffix is a SoundWire
# anchor correction of one row's value, not a second parameter, and so is the `,ref` suffix of
# a SoundWire receive-row correction (`Man_t_IS,ref,rise` keys on `Man_t_IS`). A crossing keys on its t_RF
# LANE, because that is the row in the grid -- the dimensionless coefficient has no row of its
# own, and a gathered `(X_MP,A,rise,late − X_MP,B,fall,early) · Man_t_RF,CLK` is still one lane
# however many crossings it nets.
#
# TERMS WITH NO ROW key on themselves, so they stay clickable: `UI` (derived from the
# F_CLK spin box) and `Man_t_Keeper_Response` (a CalcInputs field the grid does not
# expose). Listed rather than detected, so a NEW row-less term fails
# `test_every_printed_term_resolves_to_a_key` instead of silently becoming unclickable.
_ROWLESS_TERM_KEYS = ("UI", "Man_t_Keeper_Response")

# Suffixes that qualify a value without naming a different parameter, so they do not change
# which row the term came from: a SoundWire anchor correction, with the edges it is placed on
# (`calculator.anchor_symbol`), and a SoundWire receive-row correction, with its sampling clock
# edge (`calculator.receive_symbol`). `,min`/`,max` are not here: no symbol carries them (see
# `Term.symbol`'s rule that a picker-owned corner never appears in a symbol). An anchor priced
# on the fast data edge ends in `,fast` after those suffixes (`Man_t_DD,anchor,rr,fast`,
# `Per_t_DD,anchor,rise,fast`); `_term_key` drops it first.
_FAST_SUFFIX = ",fast"
_TERM_SUFFIXES = (*(f",anchor,{q}" for q in ("rise", "fall", "rr", "rf", "fr", "ff")),
                  ",ref,rise", ",ref,fall")


def _term_key(symbol: str) -> str:
    """The input row a printed term reads, as the highlight key.

    Returns the row NAME where there is one (`Man_t_DD`, `t_RF Man CLK`, `bus length (cm)`)
    and the bare parameter otherwise. Never raises: an unknown symbol keys on itself, which
    keeps it clickable and self-consistent, and the test is what makes that a loud failure
    rather than a quiet one.
    """
    head = symbol.strip()
    # A product's LAST factor is the row: `X_MP,rise,late · Man_t_RF,DATA`, the ramp
    # `Per_t_RF,DATA,rise`, and the gathered `(…) · Man_t_RF,CLK` all identify a t_RF lane that
    # way; a ramp keys on its lane whichever edge it names.
    last = head.split("·")[-1].strip()
    if "_t_RF," in last:
        device, lane = last.split("_t_RF,")
        return f"t_RF {device} {lane.split(',')[0]}"
    # A leading integer coefficient is presentation (`1·UI`, `2·t_PD`), not identity.
    if "·" in head and head.split("·")[0].strip().replace(".", "").isdigit():
        head = last
    if ",anchor" in head and head.endswith(_FAST_SUFFIX):
        head = head[: -len(_FAST_SUFFIX)]
    for suffix in _TERM_SUFFIXES:
        if head.endswith(suffix):
            head = head[: -len(suffix)]
    if head == "t_PD":
        return "bus length (cm)"          # t_PD IS the bus length, in ns
    if head in ("t_PD,mis", PP_MIS_SYMBOL):
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

    def __init__(self, parent=None, keys=None, whole=None):
        super().__init__(parent)
        self.keys: Callable[[], frozenset] = keys or (lambda: frozenset())
        # The heading of the inequality drawn whole, "" for none: it is washed.
        self.whole: Callable[[], str] = whole or (lambda: "")

    def _doc(self, opt: QStyleOptionViewItem) -> QTextDocument:
        doc = QTextDocument()
        doc.setDefaultFont(opt.font)
        doc.setDocumentMargin(0)
        # ANCHORS TAKE THE TEXT COLOUR EXPLICITLY: Qt's rich-text CSS resolves
        # `color: inherit` to BLACK rather than to the paint context's Text role. The doc
        # is rebuilt on every paint, so a palette switch is picked up. A margin carries its
        # colour in its own span, outside every anchor.
        css = [f"a {{ color: {VizTheme.TEXT}; text-decoration: none; }}"]
        css += [f".{_key_class(k)} {{ background-color: {VizTheme.HILITE_BG}; }}"
                for k in sorted(self.keys())]
        if self.whole():
            css.append(f".{_whole_class(self.whole())} "
                       f"{{ background-color: {VizTheme.HILITE_BG}; }}")
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


def _supply_text(leg: str, supplies: Tuple[float, float, float]) -> str:
    """Each device's V_SEOS in a leg's supply corner, as rich text, e.g.
    "V<sub>SEOS</sub> Manager −5 %, Peripheral +5 %"; empty when every device is nominal."""
    names = (("Manager", 0), ("Peripheral A", 1), ("Peripheral B", 2)) if leg.startswith(
        "PP") else (("Manager", 0), ("Peripheral", 1))
    if all(abs(supplies[i] - 1.0) < 1e-12 for _, i in names):
        return ""
    parts = [f"{name} {pct_text(supplies[i] - 1.0)}" for name, i in names]
    return "V<sub>SEOS</sub> " + ", ".join(parts)


class _TermTree(QTreeWidget):
    """The results tree, with clickable terms.

    A click on a term emits `term_clicked` with the term's key and the inequality whose row
    it is on ("" for a row of none); a click on the text of an inequality's heading or
    equation row, off its terms, emits `inequality_clicked` with the inequality the row holds
    in `_WHOLE_ROLE`; a click in empty space emits `cleared` (see `_hit`). The pointer turns
    to a pointing hand over a term and over such a row's text.
    """

    term_clicked = Signal(str, str)
    inequality_clicked = Signal(str)
    cleared = Signal()

    def __init__(self, delegate_keys, delegate_whole=None):
        super().__init__()
        self._delegate = _RichTextDelegate(self, keys=delegate_keys, whole=delegate_whole)
        self.setItemDelegateForColumn(0, self._delegate)
        self.setMouseTracking(True)

    def _hit(self, pos) -> Tuple[Optional[str], bool]:
        """(term key under `pos`, whether `pos` is on this row's text at all).

        The operators between terms are not click targets, so a click on a `−` lands on
        the text but on no term: that is INERT, as is a click on a substitution row's margin
        or its F_max (a value to read), while a click in empty space clears.
        """
        index = self.indexAt(pos)
        if not index.isValid():
            return None, False
        if index.column() != 0:
            return None, True
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
            whole = self._whole_at(event.position().toPoint(), on_text)
            if key:
                leg = self.indexAt(event.position().toPoint()).data(Qt.UserRole)
                self.term_clicked.emit(key, leg or "")
            elif whole:
                self.inequality_clicked.emit(whole)
            elif not on_text:
                self.cleared.emit()      # empty space clears; another row's operator is inert
        super().mousePressEvent(event)

    def _whole_at(self, pos, on_text: bool) -> str:
        """The inequality a click at `pos` draws whole: the `_WHOLE_ROLE` of the row whose
        column-0 text it is on, or ""."""
        index = self.indexAt(pos)
        if not on_text or not index.isValid() or index.column() != 0:
            return ""
        return index.data(_WHOLE_ROLE) or ""

    def mouseMoveEvent(self, event):
        pos = event.position().toPoint()
        key, on_text = self._hit(pos)
        over = key or self._whole_at(pos, on_text)
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
    "Man_t_EDE": "Man EDE Delay",
    "Per_t_EDE": "Per EDE Delay",
}


# Timing-symbol tokens rendered as subscripts (V_IH → V<sub>IH</sub>,
# Man_t_DD → Man_t<sub>DD</sub>, X_MP,rise → X<sub>MP,rise</sub>). The leading
# Man_/Per_ underscore is kept literal; only the underscore right before one of
# these tokens becomes the subscript, and any trailing ,qualifiers join it.
# `PP` joins MP/PM (the P->P legs), `K` is the keeper's threshold crossing (X_K),
# `TH_Keeper` / `TL_Keeper` a SoundWire keeper's thresholds (V_TH_Keeper,max), `Keeper` the
# keeper's response (Man_t_Keeper, `_shown_symbol`), `EDE` is the EDE Delay (Man_t_EDE), and
# the qualifier group takes digits and `min`/`max`. `Keeper` is tried before `K`.
_SUB_RE = re.compile(
    r"_(IH|IL|DD|IS|DZ|ZD|RF|PD|MP|PM|PP|EDE|TH_Keeper|TL_Keeper|Keeper|cross|K)"
    r"((?:,[A-Za-z0-9]+)*)")

# A crossing coefficient in a symbol: `X_MP,rise,late`, `X_MP,A,rise,slow,early`,
# `X_PM,rr,fast,late`, `X_K,fall,late`.
_CROSSING_RE = re.compile(r"X_[A-Za-z]+(?:,[A-Za-z0-9]+)*")
# The qualifiers a crossing is printed without: the term's sign gives its corner.
_CORNER_QUALIFIERS = frozenset({"late", "early"})
# An anchor correction or a ramp to V_OH / V_OL priced on the fast data edge, whose `,fast`
# the page does not print: `Man_t_DD,anchor,rr,fast`, `Per_t_ZD,anchor,fall,fast`,
# `Man_t_RF,DATA,rise,fast`. A crossing's `,fast` / `,slow` is printed.
_FAST_PRICED_RE = re.compile(r"(_t_(?:DD|ZD),anchor,[a-z]+|_t_RF,[A-Z]+,(?:rise|fall)),fast\b")


def _shown_crossing(crossing: str) -> str:
    """Crossing coefficient `crossing` as the page prints it: without its ,late / ,early."""
    return ",".join(q for q in crossing.split(",") if q not in _CORNER_QUALIFIERS)


def _shown_symbol(symbol: str) -> str:
    """Term symbol `symbol` as the page prints it, in plain text: each crossing coefficient
    without its ,late / ,early qualifier (`X_MP,rise,late` → `X_MP,rise`), an anchor
    correction or a ramp to V_OH / V_OL without its ,fast (`_FAST_PRICED_RE`:
    `Man_t_DD,anchor,rr,fast` → `Man_t_DD,anchor,rr`), and a keeper's response
    `_t_Keeper_Response` as `_t_Keeper` (`Man_t_Keeper`). Every place a symbol is shown goes
    through it, as text (the summary) or through `_subscript`; the symbol itself stays the
    term's identity (`_term_key`, the timeline's term lookups).
    `test_no_two_terms_of_an_inequality_print_alike` holds the rule to printing no two
    distinct terms, and no two distinct crossings, of one inequality alike."""
    symbol = _CROSSING_RE.sub(lambda m: _shown_crossing(m.group(0)), symbol)
    symbol = _FAST_PRICED_RE.sub(r"\1", symbol)
    return symbol.replace("_t_Keeper_Response", "_t_Keeper")


def _subscript(name: str) -> str:
    """HTML label with timing subscripts (for QLabel / the rich-text delegate), of `name`
    as the page prints it (`_shown_symbol`).

    Symbols are written `Prefix_t_TOKEN[,qualifier…]` and the spec writes `Prefix_tTOKEN`
    (`Man_tDD` here is `Man_t_DD`), so the underscore before the token is this file's marker
    for "subscript from here".
    """
    return _SUB_RE.sub(lambda m: f"<sub>{m.group(1)}{m.group(2)}</sub>", _shown_symbol(name))


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



# Keys of the single-PHY settings format that set_from_dict reads or ignores by design, and
# the Specification column's EDE Delay keys, which a settings file may carry and which name
# no box (`_EXAMPLE_ONLY_ROWS`).
_LEGACY_KEYS = frozenset({"phy", "lock_mgr_lanes", "spec::man_shape", "spec::per_shape"}
                         | {f"spec::{name}::{end}" for name in _EXAMPLE_ONLY_ROWS
                            for end in ("min", "max")})


def _find_choice(combo: QComboBox, value) -> int:
    """The index of the choice whose data is `value`, or -1. Every selector holds strings,
    so a value of any other type is not a choice: handing a number past the C++ int range to
    findData raises through Qt instead of returning -1."""
    return combo.findData(value) if isinstance(value, str) else -1


_SHOWN_VALUE_CHARS = 60

# The clock duty the launch grid is placed at: the one the model evaluates with.
_LAUNCH_DUTY = CalcInputs().duty_min


def _cut(text: str) -> str:
    """`text` cut to `_SHOWN_VALUE_CHARS` characters with an ellipsis, so a name or value read
    from a file cannot make a warning oversized."""
    return text if len(text) <= _SHOWN_VALUE_CHARS else text[:_SHOWN_VALUE_CHARS - 1] + "…"


def _shown(value) -> str:
    """A saved value's repr for a warning, cut by `_cut`."""
    return _cut(repr(value))


def _unknown_choice(key: str, value, combo: QComboBox, outcome: str = "kept") -> str:
    return (f"'{key}' {_shown(value)} is not one of the choices offered; "
            f"{combo.currentText() or combo.currentData()!r} is {outcome}")


_ROW_KEYS = frozenset({"min", "max", "pick"})   # the keys a saved row may hold


def _set_spin(spin, value, label: str, clamped: List[str],
              unread: Optional[List[str]] = None) -> None:
    """Set a spin box from a saved number, adding `label` to `clamped` when the number is
    outside the box's range (the box takes the nearest end). A value that is not a finite
    number (text, a flag, null) leaves the box as it is and adds `label` and the value to
    `unread`."""
    try:
        v = math.nan if isinstance(value, (bool, str)) else float(value)
    except (TypeError, ValueError, OverflowError):
        v = math.nan
    if not math.isfinite(v):
        if unread is not None:
            unread.append(f"{label} = {_shown(value)}")
        return
    spin.setValue(v)
    if v < spin.minimum() or v > spin.maximum():
        clamped.append(f"{label} = {v:g} (read as {spin.value():g})")


def _unread_note(unread: List[str], outcome: str = "kept as they were") -> str:
    """The import warning for saved settings that are not numbers; `outcome` says what the
    boxes hold instead."""
    more = f", and {len(unread) - 3} more" if len(unread) > 3 else ""
    return (f"{len(unread)} value(s) that are not numbers, {outcome}: "
            f"{', '.join(unread[:3])}{more}")



def _inverted_note(swapped: List[str]) -> str:
    """The import warning for ranges restored with their minimum above their maximum."""
    more = f", and {len(swapped) - 3} more" if len(swapped) > 3 else ""
    return (f"{len(swapped)} range(s) with the minimum above the maximum, exchanged: "
            f"{', '.join(swapped[:3])}{more}")


# What the model raises for inputs it cannot evaluate: a ValueError names the input (`compute`
# checks every input first); a ZeroDivisionError is a divisor an input drives to zero that the
# check did not name, shown with a generic message.
_UNEVALUABLE = (ValueError, ZeroDivisionError)


def _refusal_text(exc: Exception) -> str:
    """The message shown in place of the margins for an input the model cannot evaluate."""
    if isinstance(exc, ZeroDivisionError):
        return "an input makes the model divide by zero"
    return str(exc)


def _worst_at(base: CalcInputs, rows: List[CalcRow]) -> Tuple[Dict[Inequality, CalcResults],
                                                               Dict[str, Dict[str, str]]]:
    """Each inequality's result at its own worst corner of `rows` about `base`, and its row
    picks for that corner."""
    out: Dict[Inequality, CalcResults] = {}
    picked: Dict[str, Dict[str, str]] = {}
    all_picks = find_worst_corner_rows_all(INEQUALITIES, rows, base)
    for ineq in INEQUALITIES:
        picks = all_picks[ineq]
        picked[ineq] = dict(picks)
        out[ineq] = compute(apply_row_picks(base, rows, picks))
    return out, picked


def _worst_of_sides(sides, cancelled: Callable[[], bool]):
    """The recompute the Timing page runs on its worker (`TimingView._recompute`): `_worst_at`
    for each (side, label, base, rows) of `sides`, as {side: (results, picks)}; or, for an input
    the model cannot evaluate, the refusal message naming its column; or None once
    `cancelled()`. Reads plain data only, so it may run off the UI thread."""
    out = {}
    for side, label, base, rows in sides:
        if cancelled():
            return None
        try:
            out[side] = _worst_at(base, rows)
        except _UNEVALUABLE as exc:
            return f"{label} column: {_refusal_text(exc)}"
    return out


def _discard_on_quit(job: LatestJob) -> None:
    """Drop `job`'s requests when the application quits, so the worker stops after the column
    it is on and the interpreter's exit waits for no more than that. Holds `job` weakly."""
    app = QCoreApplication.instance()
    if app is None:
        return
    ref = weakref.ref(job)

    def discard() -> None:
        held = ref()
        if held is not None:
            held.discard()

    app.aboutToQuit.connect(discard)


# The inequalities pane is fitted to the typical row (`_typical_row_width`): the row at this
# percentile of the row widths, widened by `_TYPICAL_SLACK`. A row wider than that is much
# wider than the rest, and scrolls sideways. The slack takes the four P->P setup rows, the
# widest of the default page.
_TYPICAL_PERCENTILE = 0.90
_TYPICAL_SLACK = 1.15


def _typical_row_width(widths: Sequence[int]) -> int:
    """The width the inequalities pane fits of rows `widths` (non-empty): the
    `_TYPICAL_PERCENTILE` row's, nearest rank, times `_TYPICAL_SLACK` and rounded, and no
    wider than the widest row."""
    ranked = sorted(widths)
    at = ranked[max(math.ceil(_TYPICAL_PERCENTILE * len(ranked)) - 1, 0)]
    return min(ranked[-1], round(_TYPICAL_SLACK * at))


def _heading_of(leg: str) -> str:
    """The `_INEQ_GROUPS` key of the heading inequality `leg` shows under."""
    return next(b for b, members in _INEQ_GROUPS if leg in members)


def _leg_name(leg: str) -> str:
    """The name of the ONE inequality `leg`: its heading's text (`_INEQ_LABEL`) and, where
    the heading holds two launches, the launch after a comma ("Manager Setup for Peripheral,
    t_DD"; `_LEG_LAUNCH`). A heading that groups both launches keeps the plain text."""
    name = _INEQ_LABEL[_heading_of(leg)]
    return f"{name}, {_LEG_LAUNCH[leg]}" if leg in _LEG_LAUNCH else name


def _heading_title(leg: str) -> str:
    """Inequality `leg`'s name (`_leg_name`) after `leg`'s number as the tree numbers it: the
    title of the whole-inequality diagram and of its terms' diagrams, and the row of the
    copied summary."""
    return f"{_INEQ_NUMBER[leg]}) {_leg_name(leg)}"


def _corner_text(leg: Inequality, res: CalcResults, picks: Dict[str, str]) -> str:
    """The corner line, as rich text, of `leg`'s worst corner `res` reached by row picks
    `picks`: "Corner: " and the rows off typical with their picks (or "every row typical"),
    then the supplies off nominal (`_supply_text`). Where the placement fixes the leg's
    peripheral distance the rows open with that distance (`placement_corner_text`) and name
    no bus-length row."""
    placed = placement_corner_text(res.inputs, leg)
    corner = ", ".join(([placed] if placed else []) + [
        f"{_display_name(name)} {pick}" for name, pick in picks.items()
        if pick != "typ" and not (placed and name in _BUS_ROWS)])
    text = f"Corner: {corner}" if corner else "Corner: every row typical"
    supplies = _supply_text(leg, res.breakdowns[leg].supplies)
    return f"{text}; {supplies}" if supplies else text


# The characters Export Results writes in ASCII in place of the page's, before any other is
# decomposed and the rest replaced with "?" (`_ascii`).
_ASCII = str.maketrans({
    "−": "-", "–": "-", "—": "-", "·": "*", "×": "x", "≥": ">=", "≤": "<=", "±": "+/-",
    "↔": "<->", "→": "->", "…": "...", "τ": "tau", "δ": "delta", "α": "alpha", "Δ": "Delta",
    "Σ": "Sigma", "σ": "sigma", "µ": "u", "μ": "u", "⚠": "!", "°": "deg"})


def _ascii(text: str) -> str:
    """`text`, rich text or plain, as plain ASCII: a subscript after "_" and a superscript
    after "^", every other tag dropped, entities decoded, the page's symbols spelt out
    (`_ASCII`), accents dropped and any other character "?"."""
    text = re.sub(r"<sub>(.*?)</sub>", r"_\1", text)
    text = re.sub(r"<sup>(.*?)</sup>", r"^\1", text)
    text = html.unescape(re.sub(r"<[^>]+>", "", text)).translate(_ASCII)
    return unicodedata.normalize("NFKD", text).encode("ascii", "replace").decode("ascii")


# The table header Export Results writes under its inputs block.
RESULTS_HEADER = ("Number", "Inequality", "Column", "Equation", "Substituted", "Margin (ns)",
                  f"{FMAX_PLAIN} (MHz)", "Worst corner")


def _diagram_sections(leg: Inequality, worst: Dict[str, Dict[Inequality, CalcResults]],
                      picks: Dict[str, Dict[str, Dict[str, str]]],
                      labels: Tuple[str, str]) -> Tuple[str, List[DiagramSection]]:
    """The term diagram's title for `leg` and its section for each column, labelled by
    `labels`: the leg's timeline at the column's worst corner (`worst[side][leg]`), its terms,
    and a corner line naming the rows `picks` takes off typ, the supplies, and the timeline's
    note; or, where the leg does not apply, a note saying why. Where the placement fixes the
    leg's peripheral distance the line names that distance (`placement_corner_text`) and no
    bus-length row."""
    title = _heading_title(leg)
    sections = []
    for side, label in zip(SIDES, labels):
        res = worst[side][leg]
        if res.breakdowns[leg].not_applicable:
            sections.append(DiagramSection(
                label, None, note=f"Does not apply: {res.breakdowns[leg].not_applicable}."))
            continue
        text = _corner_text(leg, res, picks.get(side, {}).get(leg, {}))
        tl = leg_timeline(res.inputs, leg)
        sections.append(DiagramSection(
            label, tl, list(res.breakdowns[leg].terms),
            corner=f"{text}. {tl.note}" if tl.note else text))
    return title, sections


# The Export Diagram dialog's filters, the first the default: each its text, the format and
# a PNG's scale.
_EXPORT_FILTERS: Tuple[Tuple[str, str, float], ...] = (
    ("PNG, 2× scale (*.png)", "png", 2.0), ("PNG, 1× scale (*.png)", "png", 1.0),
    ("PNG, 3× scale (*.png)", "png", 3.0), ("PNG, 4× scale (*.png)", "png", 4.0),
    ("SVG (*.svg)", "svg", 1.0), ("PDF (*.pdf)", "pdf", 1.0))


def _export_name(leg: str, term: str) -> str:
    """The file name Export Diagram offers, without an extension: the leg and the term,
    each run of characters but letters, digits, _ and - one _."""
    return re.sub(r"[^A-Za-z0-9_-]+", "_", f"{leg}_{term}").strip("_") or "diagram"


def _unevaluable_note(error: str) -> str:
    """The import warning for loaded settings the timing model cannot evaluate."""
    return f"the timing model cannot evaluate these settings, so no margins are shown: {error}"


def _snapped_note(mode_value: str, moved: List[Tuple[str, float, float]]) -> str:
    """The import note for launch values moved onto a clocked mode's grid: `moved` is
    (label, value read, value set)."""
    grid = launch_grid_UI(LaunchMode(mode_value))
    shown = ", ".join(f"{label} = {old:g} (set to {new:g})" for label, old, new in moved[:3])
    more = f", and {len(moved) - 3} more" if len(moved) > 3 else ""
    return (f"{len(moved)} Manager launch value(s) not on the {grid:g} UI grid a "
            f"{mode_value} clock places, moved to the nearest point: {shown}{more}")


class _ClickLabel(QLabel):
    """A label that reports a left click."""
    clicked = Signal()

    def mousePressEvent(self, ev) -> None:  # noqa: N802 - Qt override
        if ev.button() == Qt.LeftButton:
            ev.accept()
            self.clicked.emit()
            return
        super().mousePressEvent(ev)


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
    # The term diagram given the page, or put back (set_diagram_maximized).
    diagramMaximizedChanged = Signal(bool)

    def __init__(self) -> None:
        super().__init__()
        self._loading = False
        # True when a recompute a close dropped has left the page without the results of its
        # inputs; the next show or read recomputes (`_results_out_of_date`).
        self._stale = False
        # The inputs (`to_dict`) the last recompute read, so a commit that changes nothing
        # starts none.
        self._read_inputs: dict = {}
        # The corner search runs on a worker; its result is applied by `_apply_recompute`
        # unless a later recompute has superseded it.
        self._job = LatestJob(self, self._apply_recompute, self._recompute_failed)
        _discard_on_quit(self._job)
        self._maximized = False
        self._min: Dict[Tuple[str, str], QDoubleSpinBox] = {}
        self._max: Dict[Tuple[str, str], QDoubleSpinBox] = {}
        # The Specification column's cell of each `_EXAMPLE_ONLY_ROWS` row, by row name.
        self._not_in_spec: Dict[str, QLabel] = {}
        self._row_labels: Dict[str, QLabel] = {}
        # Every style built from the palette, as (widget, css builder), so retheme() can
        # rebuild them.
        self._themed: List[Tuple[QWidget, Callable[[], str]]] = []
        # The term diagram's leg and term key (None and "" while none is drawn), whether the
        # leg is drawn whole instead (`_on_inequality_clicked`, its term ""), and each side's
        # worst corner per inequality: its result and its row picks (`_worst_results`), both
        # set together by `_recompute`.
        self._diagram_leg: Optional[Inequality] = None
        self._diagram_term = ""
        self._diagram_whole = False
        # Whether the splitter's handle has been dragged since the diagram pane opened: until
        # it is, `_fit_split` sets the split.
        self._split_dragged = False
        self._worst: Dict[str, Dict[Inequality, CalcResults]] = {}
        self._worst_picks: Dict[str, Dict[str, Dict[str, str]]] = {}
        # Why the last recompute shows no margins ("" when it shows them): an input the model
        # cannot evaluate (`_show_compute_error`).
        self._compute_error = ""
        # The note for the range the last edit exchanged; the advisory line shows it until
        # the next edit of a range (`_on_range_edited`).
        self._exchange_note = ""
        # The last computed results the tree draws (`_render_from_last`); None while none are.
        self._rendered: Optional[tuple] = None
        # The width every section card matches: the tree's typical row (`_fit_tree_width`).
        self._results_width = 0

        root = QVBoxLayout(self)
        root.setContentsMargins(6, 6, 6, 6)
        self.setFont(_base_font())                        # scales combos/buttons/tree text
        self.setStyleSheet(_scaled_stylesheet())

        # The title at the left and the page's buttons at the right of one row. The title's
        # tooltip names the selected bus (`_update_title`).
        self._title = QLabel("SWI3S & SoundWire Timing Calculator")
        self._title.setStyleSheet(_TITLE_CSS)
        self._title.setContentsMargins(_HEAD_INDENT, 0, 0, 0)

        # The top bar, the page's full width, its controls packed into as few rows as the
        # width allows (`_arrange_top_bar`).
        self._topbar = QWidget()
        self._topbar.setLayout(self._build_top_bar())
        self._title_row = QWidget()
        title_row = QHBoxLayout(self._title_row)
        title_row.setContentsMargins(0, 0, 0, _px(4))
        title_row.addWidget(self._title)
        title_row.addStretch(1)
        title_row.addWidget(self._tb_buttons)
        root.addWidget(self._title_row)
        # Its widest block, not the packing in force, bounds the page: a narrower page packs
        # the blocks into more rows.
        self._topbar.setMinimumWidth(max(
            [self._tb_pickers.sizeHint().width()]
            + [w.sizeHint().width() for w, _row in self._tb_blocks]))
        root.addWidget(self._topbar)

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
        # "Computing…" while a recompute runs, else empty (`_blank_results`).
        self._status = QLabel("")
        self._theme(self._status, lambda: f"color:{VizTheme.TEXT_DIM};")
        head.addWidget(self._status)
        # Each column's binding F_max and the inequality that sets it (`_render_binding`).
        self._binding = QLabel("")
        self._binding.setTextFormat(Qt.RichText)
        self._theme(self._binding, lambda: f"color:{VizTheme.TEXT};")
        head.addWidget(self._binding)
        head.addStretch(1)
        self._head_layout = head
        head_row = QHBoxLayout()
        head_row.setContentsMargins(0, 0, 0, 0)
        head_row.addWidget(self._results_head)
        head_row.addStretch(1)
        body.addLayout(head_row)
        # Says what a click does; its tooltip says how a click clears. The input rows' names
        # respond to a click too.
        self._trace_hint = QLabel("Click an inequality, or term, to highlight and draw it.")
        self._trace_hint.setToolTip(
            "A term is highlighted in every inequality and its input row, and drawn on the "
            "right; click it again in the inequality drawn, or any blank space among the "
            "inequalities, to clear. An inequality's heading or equation, off its terms, draws "
            "the whole inequality; click it again to close it.")
        self._theme(self._trace_hint, lambda: f"color:{VizTheme.TEXT_DIM};")
        # Wrapped, so it takes the width of the tree under it and never widens the pane.
        self._trace_hint.setWordWrap(True)
        self._trace_hint.setContentsMargins(_HEAD_INDENT, 0, 0, 0)
        body.addWidget(self._trace_hint)
        # The advisory line's text while a recompute shows no margins and the top bar that
        # carries the line is hidden (`_sync_page_visibility`).
        self._results_notice = QLabel("")
        self._theme(self._results_notice, lambda: f"color:{VizTheme.SEM_ERROR};")
        self._results_notice.setWordWrap(True)
        self._results_notice.setContentsMargins(_HEAD_INDENT, 0, 0, 0)
        self._results_notice.setVisible(False)
        body.addWidget(self._results_notice)
        # The highlight set lives on the view and is read by the delegate through a
        # callable, so a toggle is a repaint: nothing is recomputed, no row is rebuilt and
        # the tree's fitted width cannot move (the wash has no padding).
        self._hl: set = set()
        self._tree = _TermTree(lambda: self._hl, self._whole_drawn)
        self._tree.term_clicked.connect(self._on_term_clicked)
        self._tree.inequality_clicked.connect(self._on_inequality_clicked)
        self._tree.cleared.connect(self._on_cleared)
        # One column, the rows' text, under a blank header that spaces the first heading from
        # the tree's top: a substitution row ends in its margin and its F_max.
        self._tree.setHeaderLabels([""])
        # Don't let the column stretch to fill — with a fixed tree width it would absorb the
        # fit padding and creep wider on every recompute.
        self._tree.header().setStretchLastSection(False)
        # Plain header, not table-cell chrome: no section background or borders.
        self._theme(self._tree.header(), lambda: (
            f"QHeaderView::section {{ background:{VizTheme.FRAME_BG}; border:none;"
            " padding:2px 6px; }"))
        # Always fully expanded and not collapsible — the per-side rows carry the
        # margin and F_max, so there's nothing to hide.
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

        # The term diagram, on the right behind a splitter, so it stays in view while the
        # inequalities scroll and keeps its place when the page is maximized. It opens on
        # inequality 1) drawn whole (`_show_default_diagram`).
        self._diagram = InequalityDiagram(_subscript, _term_key)
        diagram_font = _base_font()
        diagram_font.setPixelSize(_FONT_DIAGRAM)
        self._diagram.setFont(diagram_font)
        self._diagram.set_reduced(True)      # drawn at `REDUCED_SCALE` until maximized
        self._diagram_pane = QWidget()
        pane = QVBoxLayout(self._diagram_pane)
        pane.setContentsMargins(_px(6), 0, 0, 0)
        # A ⤢ at the diagram's top right (or a double-click on the diagram) gives it the
        # whole page, its columns side by side (set_diagram_maximized).
        self._diagram_max = False
        self._diagram_max_btn = QToolButton()
        self._diagram_max_btn.setText("⤢")
        self._diagram_max_btn.setCheckable(True)
        self._diagram_max_btn.setFocusPolicy(Qt.NoFocus)
        self._diagram_max_btn.setToolTip("Maximize Diagram")
        self._diagram_max_btn.clicked.connect(self.set_diagram_maximized)

        def corner_css() -> str:
            return (f"QToolButton {{ border:none; background:transparent;"
                    f" color:{VizTheme.TEXT_DIM}; padding:0 4px; }}"
                    f" QToolButton:hover, QToolButton:checked {{"
                    f" background:{VizTheme.BORDER}; color:{VizTheme.TEXT};"
                    f" border-radius:{VizTheme.CORNER_RADIUS}px; }}"
                    f" QToolButton:disabled {{ color:{VizTheme.BORDER}; }}")
        self._theme(self._diagram_max_btn, corner_css)

        def nav_css() -> str:
            # Filled as the ⤢ is while the diagram has the page; a disabled arrow keeps the
            # fill and dims its glyph.
            return (f"QToolButton {{ border:none; background:{VizTheme.BORDER};"
                    f" color:{VizTheme.TEXT}; padding:0 4px;"
                    f" border-radius:{VizTheme.CORNER_RADIUS}px; }}"
                    f" QToolButton:disabled {{ color:{VizTheme.TEXT_DIM}; }}")
        # Previous and Next sit beside the ⤢, built as it is and filled as it is while
        # maximized, and show only while the diagram has the page (`_sync_diagram_nav`).
        # Left and Right do the same while the view takes them (`eventFilter`).
        self._nav_prev = QToolButton()
        self._nav_prev.setText("←")
        self._nav_next = QToolButton()
        self._nav_next.setText("→")
        for btn, step, tip in ((self._nav_prev, -1, "Previous inequality (←)"),
                               (self._nav_next, +1, "Next inequality (→)")):
            btn.setFocusPolicy(Qt.NoFocus)
            btn.setToolTip(tip)
            btn.clicked.connect(lambda _on=False, d=step: self.step_inequality(d))
            self._theme(btn, nav_css)
            btn.setVisible(False)
        # Set by a step: the next restore of the diagram scrolls the inequality it ended on
        # into view (`_reveal_drawn`).
        self._stepped = False
        # A choice of the columns drawn: either column alone, or both, stacked beside the
        # inequalities and side by side maximized (`_set_diagram_show`). One choice serves
        # both views and is kept in QSettings.
        corner = QWidget()
        corner_row = QHBoxLayout(corner)
        corner_row.setContentsMargins(0, 0, 0, 0)
        corner_row.setSpacing(_px(2))
        self._diagram_show = _saved_diagram_show()
        self._diagram_show_btns: Dict[str, QToolButton] = {}
        show_group = QButtonGroup(corner)
        for key in ("spec", "proposed", "both"):
            btn = QToolButton()
            btn.setCheckable(True)
            btn.setFocusPolicy(Qt.NoFocus)
            btn.setChecked(key == self._diagram_show)
            btn.clicked.connect(lambda _on=False, k=key: self._set_diagram_show(k))
            self._theme(btn, corner_css)
            show_group.addButton(btn)
            corner_row.addWidget(btn)
            self._diagram_show_btns[key] = btn
        corner_row.addWidget(self._nav_prev)
        corner_row.addWidget(self._nav_next)
        corner_row.addWidget(self._diagram_max_btn)
        self._diagram.set_corner_widget(corner)
        self._diagram.doubleClicked.connect(
            lambda: self.set_diagram_maximized(not self._diagram_max))
        # Export Diagram…, and on the dark palette Export Diagram on Light Background…, in
        # the diagram's context menu and the Timing file menu (`diagram_export_actions`),
        # enabled while `TimingDiagram.can_export` holds (`_sync_export_actions`).
        self._export_action = QAction("Export Diagram…", self)
        self._export_action.triggered.connect(lambda: self.export_diagram())
        self._export_light_action = QAction("Export Diagram on Light Background…", self)
        self._export_light_action.triggered.connect(lambda: self.export_diagram(light=True))
        for act in (self._export_action, self._export_light_action):
            self._diagram.addAction(act)
        self._diagram.setContextMenuPolicy(Qt.ActionsContextMenu)
        self._diagram.exportableChanged.connect(self._sync_export_actions)
        self._sync_export_actions()
        self._diagram_scroll = QScrollArea()
        self._diagram_scroll.setWidgetResizable(True)
        self._diagram_scroll.setWidget(self._diagram)
        pane.addWidget(self._diagram_scroll, 1)
        self._diagram_pane.setVisible(False)
        # A drag cannot squeeze either side below a readable width.
        self._diagram_pane.setMinimumWidth(self._diagram_min_width())
        scroll.setMinimumWidth(_px(240))
        self._splitter = QSplitter(Qt.Horizontal)
        self._splitter.setChildrenCollapsible(False)
        self._ineq_scroll = scroll
        self._splitter.addWidget(scroll)
        self._splitter.addWidget(self._diagram_pane)
        # A width the split has not been fitted to goes to the diagram.
        self._splitter.setStretchFactor(0, 0)
        self._splitter.setStretchFactor(1, 1)
        self._splitter.splitterMoved.connect(self._on_split_dragged)
        root.addWidget(self._splitter, 1)

        self._sync_spec_controls()          # size picker only applies to a SoundWire side
        self._reset_to_defaults()           # seed values + first compute
        self._show_default_diagram()

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
        # The width every section card matches and the inequalities pane is fitted to
        # (`_fit_split`): the tree's, less what its widest row needs beyond the typical row
        # (`_typical_row_width`). A wider row runs past it.
        widths = self._row_widths()
        cut = max(widths) - _typical_row_width(widths) if widths else 0
        self._results_width = w - cut

    def _row_widths(self) -> List[int]:
        """The width each inequality row (an equation, a substitution with its margin and
        F_max, or a "does not apply" row) takes in the tree, with its indentation."""
        tree, out = self._tree, []
        for i in range(tree.topLevelItemCount()):
            head = tree.topLevelItem(i)
            for j in range(head.childCount()):
                item = head.child(j)
                if item.text(0):
                    out.append(tree.sizeHintForIndex(tree.indexFromItem(item, 0)).width()
                               + tree.indentation())
        return out

    def _unify_section_widths(self) -> None:
        """Make every parameter card, the title row and the hint under it as wide as the
        tree's typical row (`_results_width`), or as their content needs where that is wider,
        so the rounded rectangles line up with the equations, the title row's ⤢ lands on the
        equations' edge, and the hint wraps there. A row wider than the typical one lies past
        that edge. Cards absorb the extra via their trailing stretch column."""
        w = self._results_width
        for widget in (*getattr(self, "_section_cards", []),
                       getattr(self, "_results_head", None), getattr(self, "_trace_hint", None)):
            if widget is not None:
                widget.setFixedWidth(max(w, widget.minimumSizeHint().width()))

    def showEvent(self, e) -> None:      # noqa: N802 - Qt override
        super().showEvent(e)
        if not self._loading and self._results_out_of_date():
            self._recompute()            # an input set from code, or a recompute a close dropped
        if self._job.busy:
            self._fit_split()
            return                       # the result's render fits the tree and the cards
        # Now that real font metrics apply, re-fit the tree and match the cards.
        self._fit_tree_width()
        self._fit_tree_height()
        self._unify_section_widths()
        self._fit_split()

    def resizeEvent(self, e) -> None:    # noqa: N802 - Qt override
        super().resizeEvent(e)
        self._arrange_top_bar()
        self._fit_split()

    # ---- the split between the inequalities and the diagram ----
    def _ineq_width(self) -> int:
        """The width of the inequalities pane that shows the tree to its typical row
        (`_typical_row_width`, a substitution row with its margin and F_max) with its frame
        (`_results_width`), and the pane's frame and scroll bar. A wider row lies past it."""
        sc = self._ineq_scroll
        return (self._results_width + sc.verticalScrollBar().sizeHint().width()
                + 2 * sc.frameWidth())

    def _fit_split(self) -> None:
        """While the diagram pane shows and its handle has not been dragged, give the
        inequalities `_ineq_width` and the diagram the rest; where that leaves the diagram
        under its minimum width the inequalities give way, down to their own minimum."""
        if self._diagram_pane.isHidden() or self._split_dragged or not self.isVisible():
            return
        total = self._splitter.width() - self._splitter.handleWidth()
        if total <= 0:
            return
        left = max(min(self._ineq_width(), total - self._diagram_pane.minimumWidth()),
                   self._ineq_scroll.minimumWidth())
        self._splitter.setSizes([left, max(total - left, 1)])

    def _on_split_dragged(self, *_a) -> None:
        self._split_dragged = True

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
        self._sync_page_visibility()

    def set_diagram_maximized(self, on: bool) -> None:
        """Give the term diagram the whole page, its chosen columns side by side: the
        inequalities and the part above them are hidden. Off, the diagram beside the
        inequalities, its chosen columns stacked. With no term drawn there is no diagram to
        give the page, and the request is not taken."""
        self._diagram_max = bool(on) and self._diagram_drawn()
        self._diagram_max_btn.setChecked(self._diagram_max)
        self._diagram_max_btn.setToolTip("Restore Diagram" if self._diagram_max
                                         else "Maximize Diagram")
        self._ineq_scroll.setVisible(not self._diagram_max)
        self._diagram.set_side_by_side(self._diagram_max, reduced=not self._diagram_max)
        self._sync_diagram_columns()
        self._sync_diagram_nav()
        self._sync_page_visibility()
        if not self._diagram_max and self._stepped:
            self._stepped = False
            timers.after(0, self, self._reveal_drawn)
        self.diagramMaximizedChanged.emit(self._diagram_max)

    def step_inequality(self, delta: int) -> None:
        """While the diagram has the page, draw the inequality `delta` places from the one
        drawn, whole, in the tree's numbered order (`_INEQ_ORDER`), as a click on its row
        would; the term highlights clear. Nothing happens past the first or the last
        inequality, or while the diagram does not have the page."""
        if not self._diagram_max or self._diagram_leg is None:
            return
        i = _INEQ_ORDER.index(self._diagram_leg) + delta
        if not 0 <= i < len(_INEQ_ORDER):
            return
        self.flush_recompute()
        if not self._diagram_max:         # a compute error put the diagram back
            return
        self._draw_whole(_INEQ_ORDER[i])
        self._clear_terms()
        self._stepped = True

    def _sync_diagram_nav(self) -> None:
        """Show Previous and Next beside the ⤢ while the diagram has the page, Previous
        disabled at the first inequality and Next at the last, and take the Left and Right
        keys (`eventFilter`) for as long."""
        leg = self._diagram_leg
        on = self._diagram_max and leg is not None
        app = QApplication.instance()
        if app is not None:
            (app.installEventFilter if on else app.removeEventFilter)(self)
        i = _INEQ_ORDER.index(leg) if on and leg is not None else 0
        self._nav_prev.setVisible(on)
        self._nav_next.setVisible(on)
        self._nav_prev.setEnabled(i > 0)
        self._nav_next.setEnabled(i < len(_INEQ_ORDER) - 1)
        self._diagram.place_corner()

    def eventFilter(self, obj, event) -> bool:      # noqa: N802 - Qt override
        """While the diagram has the page (`_sync_diagram_nav` installs this on the
        application then only), an unmodified Left or Right key steps the inequality
        (`step_inequality`) when the focus is in this view or nowhere. A focused widget
        elsewhere in the window, a list or a tree, keeps its own arrow keys."""
        if (event.type() == QEvent.KeyPress and event.key() in (Qt.Key_Left, Qt.Key_Right)
                and not event.modifiers() & ~Qt.KeypadModifier
                and self._diagram_max and self.isVisible()):
            focus = QApplication.focusWidget()
            target = focus if focus is not None else obj
            if isinstance(target, QWidget) and (
                    target is self or self.isAncestorOf(target)
                    or (focus is None and target is self.window())):
                self.step_inequality(-1 if event.key() == Qt.Key_Left else +1)
                return True
        return False

    def _reveal_drawn(self) -> None:
        """Scroll the inequalities to the block of the inequality drawn, where it lies out
        of view."""
        base = self._whole_drawn()
        if self._diagram_max or not base:
            return
        for i in range(self._tree.topLevelItemCount()):
            head = self._tree.topLevelItem(i)
            if head.data(0, _WHOLE_ROLE) != base:
                continue
            top = self._tree.visualItemRect(head).top()
            last = self._tree.visualItemRect(head.child(head.childCount() - 1)).bottom()
            host_y = self._tree.viewport().mapTo(self._ineq_scroll.widget(), QPoint(0, 0)).y()
            half = (last - top) // 2 + 1                 # the span's rows, from heading to end
            self._ineq_scroll.ensureVisible(0, host_y + top + half, 0, half)
            return

    def _set_diagram_show(self, key: str) -> None:
        """Draw the diagram's `key` column alone ("spec" or "proposed"), or both ("both"),
        and keep the choice in QSettings (`_DIAGRAM_COLUMNS_KEY`)."""
        self._diagram_show = key
        QSettings().setValue(_DIAGRAM_COLUMNS_KEY, key)
        self._sync_diagram_columns()

    def _sync_diagram_columns(self) -> None:
        """Show the column choice and apply it, beside the inequalities and maximized alike.
        The buttons carry the columns' headings."""
        left, right = self._column_labels()
        for key, text in (("spec", left), ("proposed", right), ("both", "Both")):
            btn = self._diagram_show_btns[key]
            btn.setText(text)
            btn.setToolTip(("Draw both columns side by side" if self._diagram_max
                            else "Draw both columns, one over the other") if key == "both"
                           else f"Draw the {text} column alone")
            btn.setChecked(key == self._diagram_show)
        self._diagram.set_shown_column({"spec": 0, "proposed": 1}.get(self._diagram_show))
        self._diagram.place_corner()

    def is_diagram_maximized(self) -> bool:
        return self._diagram_max

    def restyle_diagram(self) -> None:
        """Draw the term diagram again from the `diagram_style` settings in force."""
        self._diagram.restyle()

    def diagram_units(self) -> tuple[float, int]:
        """The term diagram's line height and body font size in px, at full scale
        (`TimingDiagram.units`)."""
        return self._diagram.units()

    def _sync_page_visibility(self) -> None:
        covered = self._maximized or self._diagram_max
        for w in (self._title_row, self._topbar):
            w.setVisible(not covered)
        self._results_notice.setText(self._ceiling_label.text() if self._compute_error else "")
        self._results_notice.setVisible(bool(self._compute_error) and covered)

    # ---- top control bar ----
    def _build_top_bar(self) -> QVBoxLayout:
        """The top bar: the pickers block (each side's spec source over the other's, its ramp
        model beside it, and the SoundWire system size under them while a side is SoundWire),
        then the launch, handover and clock blocks packed beside it and below it by
        `_arrange_top_bar`, and the advisory line under all of them while it has text. Also
        builds the page's buttons (`_tb_buttons`), which sit on the title's row."""
        outer = QVBoxLayout()
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(_TOPBAR_ROW_GAP)

        # Per-side spec source and ramp model, Manager over Peripheral. A mixed bus has a
        # Manager built to one spec and a Peripheral to another; each side then contributes
        # the timing ITS OWN spec promises. Selecting a SoundWire side reseeds that side's
        # rows from the SoundWire tables and switches its clock-to-output normalisation. The
        # ramp models are shared by Spec and Example.
        self._tb_pickers = QWidget()
        grid = QGridLayout(self._tb_pickers)
        grid.setContentsMargins(_HEAD_INDENT, 0, 0, 0)
        grid.setHorizontalSpacing(_px(6))
        grid.setVerticalSpacing(_TOPBAR_ROW_GAP)
        self._man_spec_combo: QComboBox = self._make_spec_combo()
        self._per_spec_combo: QComboBox = self._make_spec_combo()
        self._man_shape: QComboBox = self._make_shape_combo()
        self._per_shape: QComboBox = self._make_shape_combo()
        for r, (side, spec_combo, shape) in enumerate(
                (("Manager", self._man_spec_combo, self._man_shape),
                 ("Peripheral", self._per_spec_combo, self._per_shape))):
            grid.addWidget(QLabel(side), r, 0)
            grid.addWidget(spec_combo, r, 1)
            grid.addItem(QSpacerItem(_px(10), 0), r, 2)
            grid.addWidget(QLabel("Ramp model"), r, 3)
            grid.addWidget(shape, r, 4)

        # SoundWire's small/large system envelope (C_Bus / L_Bus, Table 5), under the
        # pickers. Meaningless for an all-SWI3S bus, so the row is HIDDEN unless a side is
        # SoundWire — not merely disabled.
        self._size_row = QWidget()
        size_row = QHBoxLayout(self._size_row)
        size_row.setContentsMargins(0, 0, 0, 0)
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
        grid.addWidget(self._size_row, 2, 0, 1, 5)

        # Manager launch architecture and keeper: how the Manager launches its data edge and
        # whether it forces its keeper. An analog delay line carries a min-to-max spread that
        # the inequalities absorb on BOTH sides at once (the max erodes MP setup, only the
        # min funds MP hold). A clock-launched edge lands on a known clock phase, so
        # min == max. Only meaningful for a SWI3S Manager; a SoundWire one has no such
        # option. The selector's tooltip says what the selected mode costs at the current
        # rate (`_update_launch_hint`).
        self._tb_launch = QWidget()
        launch_row = QHBoxLayout(self._tb_launch)
        launch_row.setContentsMargins(0, 0, 0, 0)
        launch_row.setSpacing(_px(6))
        self._launch_label = QLabel("Manager data launch")
        launch_row.addWidget(self._launch_label)
        self._launch_combo = QComboBox()
        for mode in (LaunchMode.ANALOG, LaunchMode.CLK4, LaunchMode.CLK2):
            self._launch_combo.addItem(LAUNCH_LABELS[mode], mode.value)
        self._launch_combo.setMinimumWidth(_px(210))    # fits "4x CLK launch (0.25 UI)"
        self._launch_combo.currentIndexChanged.connect(self._on_launch_changed)
        launch_row.addWidget(self._launch_combo)
        # A Manager that forces its keeper to the level it drives: keeper_Man then does not
        # apply (`CalcInputs.Man_keeper_forced`). Both columns; cleared on Reset.
        launch_row.addSpacing(_px(10))
        self._keeper_forced_check = QCheckBox("Manager forces keeper")
        self._keeper_forced_check.setToolTip(
            "The Manager loads its keeper with the level it drives before it releases, so "
            "its release waits for no keeper response and the Manager-releasing keeper "
            "inequality does not apply. A Peripheral's release is unchanged.")
        self._keeper_forced_check.toggled.connect(self._on_keeper_forced_changed)
        launch_row.addWidget(self._keeper_forced_check)

        # Whether a UI is allocated to the handover, on both columns. Checked = one UI,
        # cleared = none: the six handover inequalities (the three handover holds and the
        # three contention legs) carry an N_HO·UI term only while it is checked.
        #
        # A checkbox: one UI or none. `CalcInputs.handover_UIs` is a float, so
        # "programmed to be longer for a larger system" (§11.1.1.1) is
        # expressible from code only.
        #
        # Reset and a spec change set it to the pair's allocation (`default_handover_UIs`:
        # one UI for PHY2, none for PHY1 or EDE, a mixed pair the larger), in
        # `_reset_to_defaults`; the user may then tick or clear it.
        self._tb_handover = QWidget()
        ho_row = QHBoxLayout(self._tb_handover)
        ho_row.setContentsMargins(0, 0, 0, 0)
        ho_row.setSpacing(_px(6))
        self._ho_ui_check = QCheckBox("Handover UI")
        self._ho_ui_check.setToolTip(
            "Allocates one UI to each handover, on both columns: the handover hold and "
            "contention inequalities then carry a 1·UI term. Cleared, a handover closes on "
            "device timing alone. Reset and a spec change set it to the pair's allocation: "
            "ticked for PHY2, cleared for PHY1 and EDE.")
        self._ho_ui_check.toggled.connect(self._on_ho_ui_changed)
        # P->P placement. A selector and not a corner row: its value multiplies the bus
        # length, so the bus-length row would move a margin in a direction that depends on
        # it, which the worst-corner search forbids (calculator checklist rule 4; see
        # CalcInputs.pp_placement).
        # "Worst Per Inequality" is the default and gives each P->P leg its own worst end of
        # the bracket -- setup pays two traversals, hold banks none -- so the bounding answer
        # is what you get without choosing.
        self._pp_label = QLabel("Peripheral Placement")
        ho_row.addWidget(self._pp_label)
        self._pp_combo = QComboBox()
        for _key, _label in PP_PLACEMENT_LABELS.items():
            self._pp_combo.addItem(_label, _key)
        self._pp_combo.setMinimumWidth(_px(215))   # fits "Driver near, sampler far"
        self._pp_combo.setCurrentIndex(self._pp_combo.findData("unknown"))
        self._pp_combo.setToolTip(
            "Where the Peripherals sit on the bus. Worst Per Inequality takes each inequality "
            "at its own worst Peripheral distance; All near the Manager puts every Peripheral "
            "at 0 cm and All at the far end at the Bus Length's maximum, on every inequality. "
            "The two driver placements set Peripheral A to Peripheral B only.")
        self._pp_combo.currentIndexChanged.connect(self._on_pp_placement_changed)
        ho_row.addWidget(self._pp_combo)
        ho_row.addSpacing(_px(10))
        ho_row.addWidget(self._ho_ui_check)
        # The placement row mirrors the launch row: label, selector, then its checkbox, each
        # as wide as its counterpart (`_arrange_top_bar`), so where they stack they line up.

        # Clock Frequency Target.
        self._tb_clock = QWidget()
        clk = QHBoxLayout(self._tb_clock)
        clk.setContentsMargins(0, 0, 0, 0)
        clk.setSpacing(_px(6))
        clk.addWidget(QLabel("Clock Frequency Target"))
        self._fclk = _NoWheelSpinBox()
        self._fclk.setButtonSymbols(QAbstractSpinBox.NoButtons)
        self._fclk.setRange(0.001, 27.0)
        self._fclk.setDecimals(3)
        self._fclk.setSingleStep(0.1)
        self._fclk.setAlignment(Qt.AlignRight)
        self._fclk.setMaximumWidth(_BOX_W)
        self._fclk.setValue(13.2)
        # The rate the clocked launch rows were last placed at: an F_CLK change keeps
        # each row at its UI fraction by rescaling from this rate.
        self._launch_fclk = self._fclk.value()
        # A launch box holds at most 10000 ns and, on a rescale, no less than the row's
        # tabulated minimum. Where a rescale lands past either, the box holds the limit
        # and this holds (key -> (value the box was left with, the value it would have
        # held)), so the next rescale starts from the placement and the UI fraction
        # survives a transit through a very low or very high rate.
        self._launch_unclamped: Dict[Tuple[str, str, str], Tuple[float, float]] = {}
        # Example rows whose boxes changed since the row's edit last finished.
        self._range_edited: set = set()
        # Example rows whose committed value a recompute has read but whose range is checked
        # once focus has left both of the row's boxes (`_on_range_edited`).
        self._range_unchecked: set = set()
        # Typed: the value changes per keystroke, the recompute waits for the commit.
        self._fclk.valueChanged.connect(self._on_fclk_changed)
        self._fclk.editingFinished.connect(self._on_fclk_committed)
        clk.addWidget(self._fclk)
        clk.addWidget(QLabel("MHz"))       # unit after the box, like the rest of the page

        # The page's actions, at the right of the title's row.
        self._tb_buttons = QWidget()
        btns = QHBoxLayout(self._tb_buttons)
        btns.setContentsMargins(0, 0, 0, 0)
        btns.setSpacing(_px(6))
        # Export Results…, beside them and in the Timing file menu (`results_export_action`).
        self._export_results_action = QAction("Export Results…", self)
        self._export_results_action.triggered.connect(lambda: self.export_results())
        # Copy Summary: the margins table and the terms as plain text on the clipboard
        # (`summary_text`), in the Timing file menu (`copy_summary_action`), in the page's
        # context menu, and on the Copy key while the focus is in the page. A focused text or
        # number box keeps the Copy key for its own selection.
        self._copy_summary_action = QAction("Copy Summary", self)
        self._copy_summary_action.setShortcut(QKeySequence.StandardKey.Copy)
        self._copy_summary_action.setShortcutContext(Qt.WidgetWithChildrenShortcut)
        self._copy_summary_action.triggered.connect(lambda: self.copy_summary())
        self.addAction(self._copy_summary_action)
        for text, slot in (("Reset", self._on_reset),
                           ("Save Example Settings", self._save_proposed),
                           ("Load Example Settings", self._load_proposed),
                           ("Export Results…", self._export_results_action.trigger)):
            b = QPushButton(text)
            b.clicked.connect(slot)
            btns.addWidget(b)

        # The rows the blocks are packed into: two beside the pickers, then as many under
        # them as the width needs (`_arrange_top_bar`). An empty row takes no height.
        self._tb_blocks: Tuple[Tuple[QWidget, int], ...] = (
            (self._tb_launch, 0), (self._tb_handover, 1), (self._tb_clock, 0))
        self._tb_grid = grid
        self._tb_rows = [QHBoxLayout() for _ in range(2 + len(self._tb_blocks))]
        for i, row in enumerate(self._tb_rows):
            # A row under the pickers starts where they do.
            row.setContentsMargins(_HEAD_INDENT if i >= 2 else 0, 0, 0, 0)
            row.setSpacing(_TOPBAR_BLOCK_GAP)
        side = QVBoxLayout()
        side.setContentsMargins(0, 0, 0, 0)
        side.setSpacing(_TOPBAR_ROW_GAP)
        side.addLayout(self._tb_rows[0])
        side.addLayout(self._tb_rows[1])
        side.addStretch(1)
        head = QHBoxLayout()
        head.setContentsMargins(0, 0, 0, 0)
        head.setSpacing(_TOPBAR_BLOCK_GAP)
        head.addWidget(self._tb_pickers, 0, Qt.AlignTop)
        head.addLayout(side, 1)
        outer.addLayout(head)
        for row in self._tb_rows[2:]:
            outer.addLayout(row)
        self._tb_arranged: Optional[tuple] = None
        self._arrange_top_bar(_TOPBAR_DEFAULT_W)

        # Advisory line, under the controls so it can run to full width without clipping,
        # shown while it has text (`_set_advisory`). Warns when the target exceeds what the
        # selected pair may legally clock (every SoundWire envelope tops out below SWI3S
        # PHY2's mandatory 13.2 MHz).
        self._ceiling_label = QLabel("")
        self._theme(self._ceiling_label, lambda: f"color:{VizTheme.SEM_ERROR};")
        self._ceiling_label.setWordWrap(True)
        self._ceiling_label.setContentsMargins(_HEAD_INDENT, 0, 0, 0)
        self._ceiling_label.setVisible(False)
        outer.addWidget(self._ceiling_label)
        return outer

    def _arrange_top_bar(self, width: Optional[int] = None) -> None:
        """Pack the top bar's blocks into `width` (the page's, less its margins, by default)
        with `_pack_top_bar`. Nothing moves where the packing is as it was."""
        if width is None:
            m = self.layout().contentsMargins() if self.layout() is not None else None
            width = self.width() - (m.left() + m.right() if m is not None else 0)
        for a, b in ((self._launch_label, self._pp_label),
                     (self._launch_combo, self._pp_combo)):
            w = max(x.sizeHint().width() for x in (a, b))
            a.setFixedWidth(w)
            b.setFixedWidth(w)
        # Every row is as tall as the tallest block, so the rows beside the pickers line up
        # with theirs.
        row_h = max(w.sizeHint().height() for w, _row in self._tb_blocks)
        for w, _row in self._tb_blocks:
            w.setMinimumHeight(row_h)
        for r in (0, 1):
            self._tb_grid.setRowMinimumHeight(r, row_h)
        widths = [w.sizeHint().width() for w, _row in self._tb_blocks]
        side, below = _pack_top_bar(width, self._tb_pickers.sizeHint().width(), widths,
                                    [row for _w, row in self._tb_blocks], _TOPBAR_BLOCK_GAP,
                                    _HEAD_INDENT)
        arranged = (tuple(map(tuple, side)), tuple(map(tuple, below)))
        if arranged == self._tb_arranged:
            return
        self._tb_arranged = arranged
        for row in self._tb_rows:
            while row.count():
                row.takeAt(0)
        for row, members in zip(self._tb_rows, [*side, *below]):
            for i in members:
                row.addWidget(self._tb_blocks[i][0])
            if members:
                row.addStretch(1)

    def _set_advisory(self, text: str, tip: str = "") -> None:
        """The advisory line's text and tooltip; the line shows only while it has text."""
        self._ceiling_label.setText(text)
        self._ceiling_label.setToolTip(tip)
        self._ceiling_label.setVisible(bool(text))

    def _make_spec_combo(self) -> QComboBox:
        # Every source but the proposed PHY2 revision (the EDE proposal and SoundWire
        # are offered). That revision seeds the editable Example column instead, so the
        # page is a current-vs-proposed comparison by default (see _reset_to_defaults).
        c = QComboBox()
        for src in SPEC_SOURCES:
            if is_proposed(src):
                continue
            c.addItem(SPEC_LABELS[src], src)
        c.setMinimumWidth(_px(200))       # fits "SoundWire 1.3 (1.2 V)"
        c.setCurrentIndex(c.findData("SWI3S_PHY2"))
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
        """(left, right) column headings, naming what each column holds on every row.

        A side not on the EDE source compares the spec as it stands (left) against its
        proposed revision (right, "Example"); a side on the EDE source compares the
        proposed revision (left) against EDE (right) (`_seed_range`). A heading therefore
        names one thing when both sides agree, and on a bus with one EDE side names both,
        the non-EDE side's first: "Spec / PHY2 Proposed" and "Example / PHY2 EDE".
        """
        return _column_labels_for(self._man_spec_combo.currentData(),
                                  self._per_spec_combo.currentData())

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
        i = _find_choice(combo, value)
        if i >= 0:
            combo.setCurrentIndex(i)

    def _add_param_row(self, name: str, grid: QGridLayout, gridrow: int) -> None:
        row = _ROW_BY_NAME[name]
        lab = _ClickLabel(_display_name(name))
        # Held so `_sync_row_highlight` can wash it: a term's key IS a row name. A click on
        # it highlights the parameter, or clears it, as a click on one of its terms does.
        self._row_labels[name] = lab
        lab.setCursor(Qt.PointingHandCursor)
        lab.clicked.connect(lambda n=name: self._toggle_term(n))
        grid.addWidget(lab, gridrow, 0)
        dec = _decimals_for(row)
        unit = _unit_for(row)
        for side, cmin, cmax, cunit in (self._SPEC_COLS, self._EX_COLS):
            if side == "spec" and name in _EXAMPLE_ONLY_ROWS:
                na = QLabel(_NOT_IN_SPEC)
                na.setAlignment(Qt.AlignCenter)
                na.setToolTip("This column releases each side by its t_DZ. The EDE Delay "
                              "applies on the Example column, to a side on the EDE source.")
                self._not_in_spec[name] = na
                grid.addWidget(na, gridrow, cmin, 1, cmax - cmin + 1)
                continue
            signed = name in _SIGNED_ROWS
            mn = self._make_spin(dec, signed); mx = self._make_spin(dec, signed)
            if side == "spec":
                # The Specification column shows the spec's own min/max — read-only,
                # so only the Example column is editable. setValue still drives it
                # (PHY switch / Reset reseed it), and the action that sets it recomputes.
                for sp in (mn, mx):
                    sp.setReadOnly(True)
                    sp.setFocusPolicy(Qt.NoFocus)
            else:
                # A typed value is committed on editingFinished (Enter, or focus leaving the
                # box), which recomputes; a keystroke only marks the row edited. On commit a
                # range with its minimum above its maximum is exchanged, and a clocked
                # t_ZD / t_DZ moved to where the clock can place it, but not while focus
                # moves between the range's two boxes, which is still one entry.
                mn.editingFinished.connect(
                    lambda n=name, other=mx: self._on_range_edited(n, other))
                mx.editingFinished.connect(
                    lambda n=name, other=mn: self._on_range_edited(n, other))
                for sp in (mn, mx):
                    sp.valueChanged.connect(lambda _v, n=name: self._mark_range_edited(n))
            self._min[(side, name)] = mn
            self._max[(side, name)] = mx
            grid.addWidget(mn, gridrow, cmin)
            grid.addWidget(mx, gridrow, cmax)
            grid.addWidget(QLabel(unit), gridrow, cunit)

    def _mark_range_edited(self, name: str) -> None:
        if not self._loading:
            self._range_edited.add(name)

    def _on_range_edited(self, name: str, other: Optional[QDoubleSpinBox] = None) -> None:
        """Commit an edit of an Example row (a box's editingFinished: Enter, or focus leaving
        it). The row changed when it was edited since its last commit and its boxes differ
        from what the last recompute read; only then is it recomputed.

        A box finishing because focus moved to the row's other box (`other`) commits its
        value and leaves the range as typed: the range is checked once focus has left both.
        Then a minimum above the maximum (or the reverse) is exchanged, as a loaded range is,
        and the advisory line says so, and a launch row is moved onto the clock's grid; a
        recompute follows any of these. A commit of a row that did not change, with no check
        pending, recomputes nothing; it only clears an exchange note on display."""
        if self._loading:
            return
        changed = name in self._range_edited and any(
            boxes[("proposed", name)].value()
            != self._read_inputs.get(f"proposed::{name}::{end}")
            for end, boxes in (("min", self._min), ("max", self._max)))
        self._range_edited.discard(name)
        if other is not None and QApplication.focusWidget() is other:
            if changed:
                self._range_unchecked.add(name)
                self._recompute()
            return
        shown = bool(self._exchange_note)
        if not changed and name not in self._range_unchecked:
            if shown:
                self._exchange_note = ""
                self._refresh_advisories()
            return
        self._range_unchecked.discard(name)
        swapped = self._exchange_inverted_ranges(("proposed",), (name,))
        self._exchange_note = (
            f"{_display_name(name)}: minimum above the maximum, exchanged" if swapped else "")
        moved = (self._conform_launch_rows(edited=name) if name in self._LAUNCH_ROWS
                 else [])
        if changed or swapped or moved:
            self._recompute()
        elif shown:
            self._refresh_advisories()

    @staticmethod
    def _make_spin(decimals: int = 2, signed: bool = False) -> QDoubleSpinBox:
        sp = _NoWheelSpinBox()
        sp.setButtonSymbols(QAbstractSpinBox.NoButtons)
        sp.setRange(-10000.0 if signed else 0.0, 10000.0)
        sp.setDecimals(decimals)
        sp.setSingleStep(10 ** -decimals)
        sp.setAlignment(Qt.AlignRight)
        sp.setFixedWidth(_BOX_W)            # fixed so box columns align across sections
        return sp

    # ---- state readers ----
    def _rows(self) -> List[CalcRow]:
        # Mgr CLK and Mgr DATA slew together in practice (same driver PVT), so tie
        # Mgr DATA's worst-corner pick to Mgr CLK — the auto-worst search can't drive
        # the two Mgr lanes to opposite spec edges. Numeric min/max stay independent.
        return default_swi3s_rows(lock_mgr_lanes=True)

    def _read_rows(self, side: str) -> List[CalcRow]:
        out: List[CalcRow] = []
        held = {r.name for r in _side_rows(side)}
        for row in self._rows():
            if row.name not in held:
                continue
            sc = _display_scale(row)             # % display → fraction for CalcInputs
            # An inverted pair (typed, not yet exchanged by `_on_range_edited`) is read
            # as the range it spans.
            mn, mx = sorted((self._min[(side, row.name)].value() / sc,
                             self._max[(side, row.name)].value() / sc))
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
        # The floor each clocked launch row is re-placed with when F_CLK changes
        # (`_conform_launch_rows`), so F_max follows the placements the rows take.
        floors = {name: float(self._seed_range(side, _ROW_BY_NAME[name], man, per,
                                               self._size_combo.currentData()).min)
                  for name in self._LAUNCH_ROWS}
        return CalcInputs(
            Man_shape=self._man_shape.currentData(),
            Per_shape=self._per_shape.currentData(),
            F_CLK_target_MHz=self._fclk.value(),
            Man_spec=man,
            Per_spec=per,
            system_size=self._size_combo.currentData(),
            launch_mode=self._launch_mode_for(side),
            Man_t_DD_floor_ns=floors["Man_t_DD"],
            Man_t_ZD_floor_ns=floors["Man_t_ZD"],
            Man_t_DZ_floor_ns=floors["Man_t_DZ"],
            # The Handover UI checkbox: 1 checked, 0 cleared (`_handover_UIs`).
            handover_UIs=self._handover_UIs(),
            # Where the peripherals sit on the bus (`CalcInputs.pp_placement`).
            pp_placement=self._pp_combo.currentData(),
            # Man_tKeeper_Response is the Manager's: a SoundWire Manager's is its
            # t_Keeper_Delay.
            t_keeper_ns=preset(man, "Man", self._size_combo.currentData()).t_keeper_ns,
            # A side on the EDE source releases by its EDE Delay, on the EDE column
            # only: the baseline column is the proposed revision, which has no EDE.
            Man_ede=self._man_ede_for(side),
            Per_ede=is_ede(per) and side == "proposed",
            Man_keeper_forced=self._keeper_forced_check.isChecked(),
        )

    def _man_ede_for(self, side: str) -> bool:
        """Whether the MANAGER releases by its EDE Delay on `side`: on the EDE column, when
        the Manager is on the EDE source. The baseline column is the proposed revision,
        which has no EndDriveEarly."""
        return is_ede(self._man_spec_combo.currentData()) and side == "proposed"

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

    def _clocked(self, side: str) -> bool:
        """Whether the Manager's launch on `side` is clocked (`_launch_mode_for`)."""
        return launch_grid_UI(self._launch_mode_for(side)) is not None

    def _typ_baseline(self, side: str):
        base = self._base_inputs(side)
        rows = self._read_rows(side)
        typ_picks: Dict[str, Pick] = {r.name: "typ" for r in rows if r.linked_to is None}
        return apply_row_picks(base, rows, typ_picks), rows

    def _worst_results(self, side: str) -> Tuple[Dict[Inequality, CalcResults],
                                                 Dict[str, Dict[str, str]]]:
        """Full CalcResults for `side`, each evaluated at ITS OWN inequality's worst
        corner, and each inequality's row picks for that corner. Corners are selected
        (find_worst_corner_rows_all), never picked by hand, so this is the single source the
        inequalities tree, `summary_text` and the term diagram read from (the last two through
        `_apply_recompute`'s `_worst` and `_worst_picks`). It stores nothing. On the UI thread;
        `_recompute` runs the same search (`_worst_at`) on the worker."""
        return _worst_at(*self._typ_baseline(side))

    def _per_inequality_results(self, side: str) -> Dict[Inequality, CalcResults]:
        """Each inequality's result at its own worst corner: the latest recompute's results
        (`flush_recompute`) where it holds them, else a fresh search."""
        self.flush_recompute()
        return self._worst.get(side) or self._worst_results(side)[0]

    def _per_inequality_worst(self, side: str) -> Dict[Inequality, InequalityBreakdown]:
        """Each inequality's breakdown at its own worst corner (`_per_inequality_results`)."""
        return {ineq: r.breakdowns[ineq]
                for ineq, r in self._per_inequality_results(side).items()}

    # ---- public interface (tests / workspace) ----
    def inputs(self, side: str = "spec") -> CalcInputs:
        """The nominal (typ) inputs for `side` — the midpoint of every range.
        Corners are selected per inequality for the tree; this is the neutral
        baseline for callers/tests that want a single representative input set."""
        return self._typ_baseline(side)[0]

    def summary_text(self) -> str:
        """Plain-text dump of the Max-Frequency table — used by tests and as a
        copyable summary.

        Each row of the margin table is named as the whole-inequality diagram is titled
        (`_heading_title`): the reference number the tree shows (`_INEQ_NUMBER`), so a number
        quoted from a pasted summary points at the row on screen, and the text of the heading
        the inequality shows under (`_INEQ_LABEL`). After the table and a blank line, a line
        per column names its binding F_max and the inequality that sets it
        (`_binding_lines`), then a blank line and the terms.
        """
        self.flush_recompute()
        lines = [" | ".join(_fmax_columns(*self._column_labels()))]
        try:
            spec_r = self._per_inequality_results("spec")
            prop_r = self._per_inequality_results("proposed")
            spec_w = {i: r.breakdowns[i] for i, r in spec_r.items()}
            prop_w = {i: r.breakdowns[i] for i, r in prop_r.items()}
        except _UNEVALUABLE as exc:
            # The same refusal the page shows in place of the margins.
            return "\n".join([*lines, f"⚠ No margins. {_refusal_text(exc)}"])
        for ineq in INEQUALITIES:
            bs, bp = spec_w[ineq], prop_w[ineq]
            lines.append(" | ".join([
                _heading_title(ineq),
                self._fmt_margin(bs.margin_ns), self._fmt_margin(bp.margin_ns),
                self._fmt_margin_delta(bs.margin_ns, bp.margin_ns),
                self._fmt_fmax(fs := _shown_fmax(bs, self._clocked("spec"))),
                self._fmt_fmax(fp := _shown_fmax(bp, self._clocked("proposed"))),
                self._fmt_fmax_delta(fs, fp),
            ]))
        left_lab, right_lab = (_summary_label(lab) for lab in self._column_labels())
        lines.append("")
        for lab, rate in zip((left_lab, right_lab), self._binding_rates(spec_r, prop_r)):
            if rate is not None:
                lines.append(f"Binding {FMAX_PLAIN}, {lab}: {_binding_fmax_text(rate[0])}, "
                             f"{_heading_title(rate[1])}")
        lines += ["", "Terms"]
        for ineq in INEQUALITIES:
            n, ls, ps = _INEQ_NUMBER[ineq], spec_w[ineq].terms, prop_w[ineq].terms
            if spec_w[ineq].not_applicable or prop_w[ineq].not_applicable:
                for lab, b in ((left_lab, spec_w[ineq]), (right_lab, prop_w[ineq])):
                    lines += ([f"{n}) {lab}: does not apply: {b.not_applicable}"]
                              if b.not_applicable else
                              [f"{n}) {lab}: {self._equation_text(b.terms, plain=True)} ≥ 0",
                               f"    {lab}: {self._numeric_only(b.terms, plain=True)}"])
                continue
            if self._same_terms(ls, ps):
                lines.append(f"{n}) {self._equation_text(ls, plain=True)} ≥ 0")
                lines += [f"    {lab}: {self._numeric_only(ts, plain=True)}"
                          for lab, ts in ((left_lab, ls), (right_lab, ps))]
                continue
            for lab, ts in ((left_lab, ls), (right_lab, ps)):
                lines += [f"{n}) {lab}: {self._equation_text(ts, plain=True)} ≥ 0",
                          f"    {lab}: {self._numeric_only(ts, plain=True)}"]
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
             "man_keeper_forced": self._keeper_forced_check.isChecked(),
             "pp_placement": self._pp_combo.currentData()}
        for side in SIDES:
            for row in _side_rows(side):
                d[f"{side}::{row.name}::min"] = self._min[(side, row.name)].value()
                d[f"{side}::{row.name}::max"] = self._max[(side, row.name)].value()
        return d

    def set_from_dict(self, d: dict, source: Optional[str] = None) -> None:
        """Restore the inputs `to_dict` saved. With a `source` (the file's name) these are
        reported through import_notes: a key this version does not read, a choice none of
        the selectors offers (`_unknown_choice`), an older file's `phy` that is not 1 or 2, a
        handover flag that is not true or false, a launch mode a SoundWire Manager cannot
        use, a number outside its field's range (set to the nearest end), a value that is
        not a number (`_unread_note`), a range with its minimum above its maximum
        (`_inverted_note`), launch values moved onto a clocked grid (`_snapped_note`) and
        settings the model cannot evaluate (`_unevaluable_note`).

        The selectors are set first; F_CLK, the ramp models and every row are then seeded
        for the selected pair (`_seed_values`), and the values the file holds are applied
        over them, the Example column's alone: the read-only Specification column keeps the
        selected pair's tables, and a saved `spec::` number that differs from them is
        reported. A value the file does not hold takes the selected pair's default, and for
        the Manager launch rows the selected launch mode's, silently; one that is not a number, or a ramp model that is not a choice, takes it
        with a note."""
        notes: List[str] = []
        self._exchange_note = ""
        known = set(self.to_dict()) | _LEGACY_KEYS
        unknown = sorted(str(k) for k in d if k not in known and not str(k).endswith("::pick"))
        if unknown:
            notes.append(f"{len(unknown)} setting(s) this version does not read, ignored: "
                         f"{import_notes.names_listed(unknown)}")
        self._loading = True
        try:
            # "lock_mgr_lanes" is ignored; a per-side "spec::man_shape" key sets the
            # shared shape picker when "man_shape" is absent.
            #
            # A "phy" key (one global PHY switch) restores as that PHY on BOTH sides. An
            # explicit man_spec/per_spec always wins.
            legacy_phy = d.get("phy")
            legacy_default = ("SWI3S_PHY1" if type(legacy_phy) is int and legacy_phy == 1
                              else "SWI3S_PHY2")
            if ("phy" in d and not (type(legacy_phy) is int and legacy_phy in (1, 2))
                    and not ("man_spec" in d and "per_spec" in d)):
                notes.append(f"'phy' {_shown(legacy_phy)} is not 1 or 2; SWI3S PHY2 is used")
            # A dict with no spec keys describes an all-SWI3S bus.
            for combo, key in ((self._man_spec_combo, "man_spec"),
                               (self._per_spec_combo, "per_spec")):
                j = _find_choice(combo, d.get(key, legacy_default))
                if j >= 0:
                    combo.setCurrentIndex(j)
                else:
                    notes.append(_unknown_choice(key, d.get(key, legacy_default), combo))
            k = _find_choice(self._size_combo, d.get("sw_system_size", "small"))
            if k >= 0:
                self._size_combo.setCurrentIndex(k)
            else:
                notes.append(_unknown_choice("sw_system_size", d.get("sw_system_size"),
                                             self._size_combo))
            # A missing launch_mode means the analog delay.
            lm = _find_choice(self._launch_combo,
                              d.get("launch_mode", LaunchMode.ANALOG.value))
            if lm >= 0:
                self._launch_combo.setCurrentIndex(lm)
            else:
                notes.append(_unknown_choice("launch_mode", d.get("launch_mode"),
                                             self._launch_combo))
            # A saved handover_ui is kept. A missing one, or one that is not a flag, takes
            # the selected pair's allocation (`_pair_handover_ui`).
            ho = d.get("handover_ui")
            pair_ho = self._pair_handover_ui()
            if ho is not None and not isinstance(ho, bool):
                notes.append(f"'handover_ui' {_shown(ho)} is not true or false; the selected "
                             f"pair's allocation ({str(pair_ho).lower()}) is used")
            self._ho_ui_check.setChecked(ho if isinstance(ho, bool) else pair_ho)
            # A missing man_keeper_forced is a keeper that follows the line.
            forced = d.get("man_keeper_forced", False)
            if not isinstance(forced, bool):
                notes.append(f"'man_keeper_forced' {_shown(forced)} is not true or false; "
                             "the keeper follows the line (false)")
            self._keeper_forced_check.setChecked(forced is True)
            # A missing pp_placement means "unknown" (each leg at its own worst placement).
            pp = _find_choice(self._pp_combo, d.get("pp_placement", "unknown"))
            if pp >= 0:
                self._pp_combo.setCurrentIndex(pp)
            else:
                notes.append(_unknown_choice("pp_placement", d.get("pp_placement"),
                                             self._pp_combo))
            requested = self._launch_combo.currentData()
            self._sync_spec_controls()
            if self._launch_combo.currentData() != requested:
                notes.append(f"'launch_mode' {_shown(requested)} does not apply to a SoundWire "
                             f"Manager; {self._launch_combo.currentText()!r} is used")
            self._seed_values()
            clamped: List[str] = []
            unread: List[str] = []
            if "fclk_target" in d:
                _set_spin(self._fclk, d["fclk_target"], "fclk_target", clamped, unread)
            self._sync_man_tdd_rows()    # an omitted launch row takes the mode's own default
            for combo, key, legacy in ((self._man_shape, "man_shape", "spec::man_shape"),
                                       (self._per_shape, "per_shape", "spec::per_shape")):
                value = d.get(key, d.get(legacy))
                self._set_shape(combo, value)
                if value is not None and _find_choice(combo, value) < 0:
                    notes.append(_unknown_choice(key, value, combo, "used"))
            for row in _side_rows("proposed"):
                self._apply_keys("proposed", row.name, d, clamped, unread)
            differ = self._spec_values_differing(d)
            if differ:
                more = f", and {len(differ) - 3} more" if len(differ) > 3 else ""
                notes.append(f"{len(differ)} Specification value(s) differ from this "
                             f"version's tables, which are used: "
                             f"{', '.join(differ[:3])}{more}")
            if clamped:
                more = f", and {len(clamped) - 3} more" if len(clamped) > 3 else ""
                notes.append(f"{len(clamped)} value(s) outside their field's range set to the "
                             f"nearest end: {', '.join(clamped[:3])}{more}")
            if unread:
                notes.append(_unread_note(unread, "defaults used"))
            swapped = self._exchange_inverted_ranges(SIDES)
            if swapped:
                notes.append(_inverted_note(swapped))
        finally:
            self._loading = False
        # The restored launch rows are kept, moved onto the restored mode's grid where
        # it is clocked; a value that moves is reported.
        moved = self._conform_launch_rows()
        if moved:
            notes.append(_snapped_note(self._launch_combo.currentData(),
                                       [(f"{side}::{name}::{end}", old, new)
                                        for side, name, end, old, new in moved]))
        if source is not None:
            for msg in notes:
                import_notes.warn(source, msg)
        self._update_title()
        self._recompute()
        if source is not None:
            self.flush_recompute()       # the warning names an input the model refuses
            if self._compute_error:
                import_notes.warn(source, _unevaluable_note(self._compute_error))

    def _apply_keys(self, side: str, name: str, d: dict,
                    clamped: Optional[List[str]] = None,
                    unread: Optional[List[str]] = None) -> None:
        # A "::pick" key is ignored: corners are selected per inequality.
        kmin, kmax = f"{side}::{name}::min", f"{side}::{name}::max"
        out = clamped if clamped is not None else []
        if kmin in d:
            _set_spin(self._min[(side, name)], d[kmin], kmin, out, unread)
        if kmax in d:
            _set_spin(self._max[(side, name)], d[kmax], kmax, out, unread)

    def _spec_values_differing(self, d: dict) -> List[str]:
        """The `spec::<row>::min/max` keys of `d` holding a number more than half a displayed
        step from the Specification box, which holds the selected pair's table value."""
        out: List[str] = []
        for row in _side_rows("spec"):
            half_step = 0.5 * 10 ** -_decimals_for(row) + 1e-9
            for end, boxes in (("min", self._min), ("max", self._max)):
                key = f"spec::{row.name}::{end}"
                v = d.get(key)
                if isinstance(v, bool) or not isinstance(v, (int, float)):
                    continue
                if math.isfinite(v) and abs(v - boxes[("spec", row.name)].value()) > half_step:
                    out.append(key)
        return out

    def _exchange_inverted_ranges(self, sides, names=None) -> List[str]:
        """Exchange the minimum and maximum of every row on `sides` (only `names`, when given)
        whose minimum is above its maximum; returns their `side::row` labels. The worst-corner
        search needs min <= max. The boxes are set without a recompute."""
        swapped: List[str] = []
        was, self._loading = self._loading, True
        try:
            for side in sides:
                for name in (n for (sd, n) in self._min
                             if sd == side and (names is None or n in names)):
                    lo, hi = self._min[(side, name)], self._max[(side, name)]
                    if lo.value() > hi.value():
                        a, b = lo.value(), hi.value()
                        lo.setValue(b)
                        hi.setValue(a)
                        swapped.append(f"{side}::{name}")
        finally:
            self._loading = was
        return swapped

    # ---- actions ----
    def _update_title(self) -> None:
        """Name the selected bus in the fixed page title's tooltip, Manager first."""
        man = self._man_spec_combo.currentData()
        per = self._per_spec_combo.currentData()
        if man == per:
            self._title.setToolTip(f"{SPEC_LABELS[man]} Manager and Peripheral")
        else:
            self._title.setToolTip(f"{SPEC_LABELS[man]} Manager  ↔  "
                                   f"{SPEC_LABELS[per]} Peripheral")

    @staticmethod
    def _seed_range(side: str, row: CalcRow, man_spec: SpecSource,
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
                rng = _ede_ranges().get(row.name)
        elif side == "proposed" and row.name in proposal:
            rng = proposal[row.name]
        if rng is None and which is not None:
            rng = _row_range_for_spec(row.name, spec, which, size)
        if rng is None:
            rng = _spec_range(row.name, phy)
        return rng

    def _seed_values(self) -> None:
        """Set F_CLK, both ramp models and every row of both columns to the selected pair's
        defaults (`_default_fclk`, linear, `_seed_range`), without a recompute."""
        was, self._loading = self._loading, True
        try:
            self._fclk.setValue(self._default_fclk())
            self._set_shape(self._man_shape, "linear")
            self._set_shape(self._per_shape, "linear")
            man_spec = self._man_spec_combo.currentData()
            per_spec = self._per_spec_combo.currentData()
            size = self._size_combo.currentData()
            for side in SIDES:
                for row in _side_rows(side):
                    rng = self._seed_range(side, row, man_spec, per_spec, size)
                    sc = _display_scale(row)
                    self._min[(side, row.name)].setValue(float(rng.min) * sc)
                    self._max[(side, row.name)].setValue(float(rng.max) * sc)
        finally:
            self._loading = was

    def _reset_to_defaults(self) -> None:
        self._exchange_note = ""
        self._loading = True
        try:
            # The pair's allocation; _handover_UIs() then reads the box.
            self._ho_ui_check.setChecked(self._pair_handover_ui())
            self._keeper_forced_check.setChecked(False)
            self._seed_values()
        finally:
            self._loading = False
        self._sync_man_tdd_rows()
        self._update_title()
        self._recompute()

    def _pair_handover_ui(self) -> bool:
        """The Handover UI box's default for the selected pair: ticked where the pair
        allocates a handover UI (`default_handover_UIs`: PHY2, SoundWire, a mixed pair with
        either), cleared where it allocates none (PHY1, EDE)."""
        return default_handover_UIs(self._man_spec_combo.currentData(),
                                    self._per_spec_combo.currentData()) > 0

    def _on_reset(self) -> None:
        """Reset: every input to its default (`_reset_to_defaults`), and the diagram to its
        default view (`_show_default_diagram`)."""
        self._show_default_diagram()
        self._reset_to_defaults()

    def _default_fclk(self) -> float:
        """Target rate to seed on reset.

        The per-side targets are what each spec is designed around; the pair's
        ceiling then clamps. A mixed bus cannot legally clock at SWI3S PHY2's
        mandatory 13.2 MHz — every SoundWire envelope tops out below it — so
        seeding the ceiling beats seeding a rate the bus may not run at.
        """
        return _default_fclk_for(self._man_spec_combo.currentData(),
                                 self._per_spec_combo.currentData(),
                                 self._size_combo.currentData())

    def _on_spec_changed(self, *_a) -> None:
        """Switching either side's spec resets the whole page (`_reset_to_defaults`): every row
        of both columns is reseeded from the selected pair's tables, F_CLK, the handover box
        and both ramp models return to their defaults, because each seed depends on the pair,
        and the Manager forces keeper switch is cleared. During a workspace load only the
        selection is tracked; saved values are applied afterwards."""
        was_loading = self._loading
        # The launch combo a SoundWire Manager forces fires _on_launch_changed; the reset
        # below is the one recompute.
        self._loading = True
        try:
            self._sync_spec_controls()
        finally:
            self._loading = was_loading
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

        Toggling on the KEY and not on the clicked glyph makes a second click on another
        occurrence turn it off: its row name, or the term elsewhere in the inequality it is
        drawn for (`_on_term_clicked` draws it for any other inequality instead).
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
        # The inputs are unchanged, so the results are redrawn in the new palette, not
        # recomputed.
        if self._rendered is not None:
            self._render_from_last()
        elif self._job.busy:             # its result renders in the new palette
            self._blank_tree()
        else:
            self._recompute()
        self._tree.viewport().update()
        self._diagram.update()
        self._sync_export_actions()

    def _refresh_highlight(self) -> None:
        """Repaint only. No recompute, no rebuild — see `self._hl`. A diagram whose term is
        no longer highlighted is cleared."""
        self._tree.viewport().update()
        self._sync_row_highlight()
        if (not self._diagram_whole and self._diagram_term
                and self._diagram_term not in self._hl):
            self._hide_diagram()

    def _hide_diagram(self) -> None:
        """Forget the drawn term and close the diagram pane (a maximized diagram is put back
        first, so the inequalities are on screen again)."""
        self._diagram_leg = None
        self._diagram_term = ""
        self._diagram_whole = False
        self._diagram.clear()
        if self._diagram_max:
            self.set_diagram_maximized(False)
        self._diagram_pane.setVisible(False)
        self._split_dragged = False
        self._tree.viewport().update()

    def _diagram_min_width(self) -> int:
        """The narrowest the diagram pane may be: 22 of the diagram's line heights."""
        return int(22 * self._diagram.fontMetrics().height())

    # ---- the term diagram ----
    def _on_term_clicked(self, key: str, leg: str) -> None:
        """A click on the term keyed `key` on a row of inequality `leg` ("" for none).

        On the term the diagram draws, in the inequality it is drawn for, the click clears the
        term's highlight and the diagram with it. On any other term of an inequality it
        highlights the term, keeping a highlight it has, and draws it for that inequality.
        Off an inequality it toggles the highlight."""
        self.flush_recompute()
        if leg not in INEQUALITIES or (leg, key) == (self._diagram_leg, self._diagram_term):
            self._toggle_term(key)
            return
        if key not in self._hl:
            self._hl.add(key)
            self._refresh_highlight()
        self._draw_term(key, leg)

    def _draw_term(self, key: str, leg: str) -> None:
        """Draw the term keyed `key` of inequality `leg` in the diagram pane, opening the pane
        on the first call. Not a signal slot: `_on_term_clicked` calls it, with the arguments
        in the order (key, leg). A term that is not highlighted is not drawn."""
        self.flush_recompute()
        if key not in self._hl or leg not in INEQUALITIES:
            return
        self._diagram_leg = cast(Inequality, leg)
        self._diagram_term = key
        self._diagram_whole = False
        self._tree.viewport().update()
        self._open_diagram_pane()
        self._show_diagram()
        self._sync_diagram_nav()

    def _diagram_drawn(self) -> bool:
        """Whether the pane draws a term or an inequality whole."""
        return bool(self._diagram_term) or self._diagram_whole

    def _on_cleared(self) -> None:
        """A click in empty space among the inequalities: the highlights clear, and a diagram
        of an inequality drawn whole closes with them."""
        self._clear_terms()
        if self._diagram_whole:
            self._hide_diagram()

    def _on_inequality_clicked(self, leg: str) -> None:
        """A click on the text of inequality `leg`'s heading or equation row, off its terms:
        draw the inequality whole in the diagram pane, opening it; on the inequality drawn
        whole, close the pane. A term's highlight is kept either way."""
        self.flush_recompute()
        if leg not in INEQUALITIES:
            return
        if self._diagram_whole and leg == self._diagram_leg:
            self._hide_diagram()
            return
        self._draw_whole(leg)

    def _draw_whole(self, leg: str) -> None:
        """Draw inequality `leg` whole in the diagram pane, opening it. With no results held
        (a recompute runs) the pane shows the inequality's title and "Computing…" until the
        result is in."""
        self._diagram_leg = cast(Inequality, leg)
        self._diagram_term = ""
        self._diagram_whole = True
        self._tree.viewport().update()
        self._open_diagram_pane()
        if self._worst:
            self._show_diagram()
        else:
            self._diagram.show_inequality(_heading_title(self._diagram_leg), [])
            self._sync_diagram_columns()
        self._sync_diagram_nav()

    def _show_default_diagram(self) -> None:
        """The diagram's default view: inequality 1) drawn whole, its heading washed as a click
        on it leaves it (`_whole_drawn`). The page opens on it, and Reset and Load Example Settings return to it."""
        self._draw_whole(next(ineq for ineq, n in _INEQ_NUMBER.items() if n == 1))

    def _whole_drawn(self) -> str:
        """The heading (`_INEQ_GROUPS` key) of the inequality drawn whole, or ""."""
        if not self._diagram_whole or self._diagram_leg is None:
            return ""
        return _heading_of(self._diagram_leg)

    def _open_diagram_pane(self) -> None:
        """Show the diagram pane, the split fitted on opening (`_fit_split`)."""
        if self._diagram_pane.isHidden():
            self._diagram_pane.setVisible(True)
            self._fit_split()

    def _show_diagram(self) -> None:
        """Rebuild the diagram for the shown term, or the inequality drawn whole, from each
        column's worst corner."""
        leg = self._diagram_leg
        if leg is None or not self._diagram_drawn() or not self._worst:
            return
        title, sections = _diagram_sections(leg, self._worst, self._worst_picks,
                                            self._column_labels())
        if self._diagram_whole:
            self._diagram.show_inequality(_heading_title(leg), sections)
        else:
            self._diagram.show_term(title, self._diagram_term, sections)
        self._sync_diagram_columns()

    def diagram_export_actions(self) -> Tuple[QAction, QAction]:
        """Export Diagram… and Export Diagram on Light Background…, for a menu."""
        return self._export_action, self._export_light_action

    def _sync_export_actions(self, *_args) -> None:
        """Enable the export actions while the diagram can be exported, and show the light
        background one on the dark palette only."""
        on = self._diagram.can_export()
        self._export_action.setEnabled(on)
        self._export_light_action.setEnabled(on)
        self._export_light_action.setVisible(VizTheme.MODE != "light")

    def export_diagram(self, light: bool = False) -> None:
        """Ask for a file and write the drawn diagram to it (`TimingDiagram.export`), on the
        light palette with `light`. The dialog's filter picks the format and a PNG's scale
        (`_EXPORT_FILTERS`, the first the default) and the file takes its extension. A write
        that fails, or a diagram closed while the dialog was open, is reported in a message
        naming the file."""
        title = "Export Diagram"
        if not self._diagram.can_export():
            return
        filters = [f for f, _fmt, _scale in _EXPORT_FILTERS]
        name = _export_name(self._diagram_leg or "",
                            "inequality" if self._diagram_whole else self._diagram_term)
        path, chosen = QFileDialog.getSaveFileName(self, title, f"{name}.png",
                                                   ";;".join(filters), filters[0])
        if not path:
            return
        _f, fmt, scale = next((f for f in _EXPORT_FILTERS if f[0] == chosen),
                              _EXPORT_FILTERS[0])
        path = suffixed_save_path(self, path, f".{fmt}", title,
                                  replaced=tuple(f".{x}" for x in EXPORT_FORMATS))
        if path is None:
            return
        self.flush_recompute()
        if not self._diagram.can_export():
            QMessageBox.warning(self, title, f"{path} was not written: the diagram closed "
                                             "while the file was chosen.")
            return
        try:
            data = self._diagram.export(fmt, scale, light)
            write_whole(path, lambda f: f.write(data))
        except (OSError, ValueError) as exc:
            QMessageBox.critical(self, title, f"{path} could not be written: {exc}")

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
        if not self._loading:
            self._recompute()

    def _on_keeper_forced_changed(self, *_a) -> None:
        """Forcing the keeper changes no spec table: no reseed, as for the handover UI."""
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

    def _on_fclk_changed(self, *_a) -> None:
        """F_CLK moves the clock-launched t_DD and the mode's viable ceiling, so
        the hint has to follow it, not just the spec selection. Typed, so the
        recompute waits for the commit (`_on_fclk_committed`)."""
        self._update_launch_hint()
        # A load or reset places the launch rows itself once every value is in.
        if self._loading:
            return
        # Each clocked launch row keeps its UI fraction; an analog one keeps its value.
        self._conform_launch_rows(rescale_from=self._launch_fclk)

    def _on_fclk_committed(self) -> None:
        """A committed F_CLK (Enter, or focus leaving the box) recomputes where an input
        differs from what the last recompute read."""
        if not self._loading and self._results_out_of_date():
            self._recompute()

    # Every Manager output timing the launch mode governs. All three, or none: one
    # Manager has one output stage. See `calculator._man_clocked_grid_ns`, which places all three.
    #
    # Each is constrained to the mode's RESOLUTION: a 4× clock offers 0.00, 0.25,
    # 0.50 and 0.75 UI and the three do not have to agree; a 2× clock offers 0.00
    # and 0.50. They stay editable and are snapped to a placeable point.
    _LAUNCH_ROWS = ("Man_t_DD", "Man_t_ZD", "Man_t_DZ")

    def _snap_launch_rows(self, edited: Optional[str] = None) -> None:
        """Move the editable clocked rows onto a point the selected clock places.
        `edited` names the row the user just edited (see `_conform_launch_rows`).

        An off-grid t_ZD or t_DZ describes a Manager that cannot be built with the
        selected clock. Snapped on `editingFinished` so it does not fight the
        user mid-keystroke; the model snaps too (`_man_clocked_grid_ns`), so a
        margin is never computed from an unplaceable value even between edits.
        """
        if self._loading:
            return
        self._conform_launch_rows(edited=edited)
        self._recompute()

    def _sync_man_tdd_rows(self) -> None:
        """Re-derive the Manager output timings for the selected launch mode.

        Called when the launch mode itself is set: on a mode change and on Reset. A
        clocked launch OVERRIDES the tabulated values in `_man_tdd_range`,
        `_man_tzd_ns` and `man_tdz_parts`, so the rows show the values in force.
        Each column follows its own mode (`_launch_mode_for`).

        A governed row is seeded from `_seed_range`, snapped to the grid (min ==
        max), and left EDITABLE: which grid point each sits on is a design choice
        the mode only constrains to its resolution. Handing them back to the analog
        mode RE-SEEDS from `_seed_range` rather than restoring what was there
        before, so a hand edit is lost across a round trip through a clocked mode;
        while the mode held, the model ignored an analog-range edit.

        A path that keeps the user's values (an F_CLK change, a restore, Load
        Example) uses `_conform_launch_rows` instead.
        """
        mode = LaunchMode(self._launch_combo.currentData())
        man_spec = self._man_spec_combo.currentData()
        per_spec = self._per_spec_combo.currentData()
        size = self._size_combo.currentData()
        self._launch_unclamped.clear()
        was, self._loading = self._loading, True    # setValue must not re-enter
        try:
            # Every launch row: leaving a clocked mode, or switching to a spec whose
            # values the mode does not govern, has to put the tabulated value back.
            for name in self._LAUNCH_ROWS:
                row = _ROW_BY_NAME[name]
                sc = _display_scale(row)
                for side in SIDES:
                    # PER SIDE, via the same predicate the margins use. The
                    # Specification column is always analog, so it reseeds tabulated
                    # here however the selector is set, matching the values that
                    # column computes from.
                    clk = launch_mode_tDD_ns(self._launch_mode_for(side),
                                             self._fclk.value(), _LAUNCH_DUTY)
                    if clk is None:
                        rng = self._seed_range(side, row, man_spec, per_spec, size)
                        lo, hi = float(rng.min), float(rng.max)
                    else:
                        # RESEED FROM THE COLUMN, THEN SNAP -- statelessly, never by
                        # snapping whatever the box currently shows: snapping the
                        # displayed value accumulates rounding across mode changes.
                        rng = self._seed_range(side, row, man_spec, per_spec, size)
                        lo = hi = snap_to_launch_grid(
                            float(rng.min), mode, self._fclk.value(), _LAUNCH_DUTY,
                            floor_ns=float(rng.min))
                    self._min[(side, name)].setValue(lo * sc)
                    self._max[(side, name)].setValue(hi * sc)
        finally:
            self._loading = was
        self._launch_fclk = self._fclk.value()
        self._update_launch_tooltips()

    def _conform_launch_rows(self, rescale_from: Optional[float] = None,
                             edited: Optional[str] = None
                             ) -> List[Tuple[str, str, str, float, float]]:
        """Keep the Manager output timings the boxes hold, on the launch mode's grid.

        - The Specification column is always analog and read-only: it is set to its
          tabulated range.
        - An analog Example column keeps its values.
        - A clocked Example column keeps each box's placement. With `rescale_from`
          (the rate the rows were placed at) each box keeps its UI fraction at the
          current rate, and a box held at the row's tabulated minimum or at the end of
          its range remembers that fraction, so a return to the earlier rate restores
          it; without it each box moves to the nearest placeable point, not below the
          row's tabulated minimum. A snap without `rescale_from` forgets the remembered
          fractions: only `edited`'s where it names the row the user edited, every row's
          otherwise.

        Returns (side, row, "min"/"max", old, new) for each box a snap without
        `rescale_from` moved by more than half its displayed step, for the caller to
        report. A rescale is a change of rate, not a correction, and is not returned.
        """
        mode = LaunchMode(self._launch_combo.currentData())
        f = self._fclk.value()
        man_spec = self._man_spec_combo.currentData()
        per_spec = self._per_spec_combo.currentData()
        size = self._size_combo.currentData()
        moved: List[Tuple[str, str, str, float, float]] = []
        if rescale_from is None and edited is None:
            self._launch_unclamped.clear()
        elif rescale_from is None:
            for key in [k for k in self._launch_unclamped if k[1] == edited]:
                del self._launch_unclamped[key]
        was, self._loading = self._loading, True    # setValue must not re-enter
        try:
            for name in self._LAUNCH_ROWS:
                row = _ROW_BY_NAME[name]
                sc = _display_scale(row)
                half_step = 0.5 * 10 ** -_decimals_for(row) + 1e-9
                for side in SIDES:
                    rng = self._seed_range(side, row, man_spec, per_spec, size)
                    if side == "spec":
                        self._min[(side, name)].setValue(float(rng.min) * sc)
                        self._max[(side, name)].setValue(float(rng.max) * sc)
                        continue
                    if launch_grid_UI(self._launch_mode_for(side)) is None:
                        continue
                    for end, boxes in (("min", self._min), ("max", self._max)):
                        box = boxes[(side, name)]
                        key = (side, name, end)
                        old = box.value()
                        held = self._launch_unclamped.get(key)
                        value = (held[1] if held and held[0] == old else old) / sc
                        if rescale_from is not None and f > 0:
                            value *= rescale_from / f
                        snapped = snap_to_launch_grid(value, mode, f, _LAUNCH_DUTY,
                                                      floor_ns=float(rng.min))
                        if rescale_from is not None:
                            box.setValue(snapped * sc)
                            # The placement the rate gives before the floor and the box's
                            # range apply is held while the box shows a different value.
                            free = snap_to_launch_grid(value, mode, f, _LAUNCH_DUTY)
                            if snapped * sc > box.maximum() or abs(free - snapped) > 1e-9:
                                self._launch_unclamped[key] = (box.value(), free * sc)
                            else:
                                self._launch_unclamped.pop(key, None)
                        elif abs(snapped * sc - old) > half_step:
                            box.setValue(snapped * sc)
                            moved.append((side, name, end, old, box.value()))
        finally:
            self._loading = was
        self._launch_fclk = f
        self._update_launch_tooltips()
        return moved

    def _update_launch_tooltips(self) -> None:
        """Say on each Example launch box whether the mode governs it, and on what grid."""
        mode = LaunchMode(self._launch_combo.currentData())
        # The tooltips describe the PROPOSED column's boxes, so they read that
        # column's mode -- which is the selector's, since only that side is governed.
        clk = launch_mode_tDD_ns(self._launch_mode_for("proposed"),
                                 self._fclk.value(), _LAUNCH_DUTY)
        grid = launch_grid_UI(mode)
        for name in self._LAUNCH_ROWS:
            if clk is not None and grid is not None:
                points = ", ".join(f"{k * grid:g} UI" for k in
                                   range(int(round(1.0 / grid))))
                tip = (f"Clocked: a {mode.value} clock places this on {points} "
                       f"or 1 UI. Edited values snap to the nearest.")
            else:
                tip = ""
            for box in (self._min[("proposed", name)], self._max[("proposed", name)]):
                box.setToolTip(tip)

    def _launch_limit(self) -> Optional[Tuple[float, str]]:
        """The highest rate at which the Example column's Manager-to-Peripheral setup and hold
        legs (handover forms included) all pass, and what sets it: "setup" or "hold" for the
        limiting leg, or "t_RF" where that leg's F_max is its own `toggle_ceiling_MHz`, read
        from the inputs at its own worst corner. None while the page holds no results (before
        the first recompute's result and while one runs). An unbounded leg counts as
        infinity."""
        results = self._worst.get("proposed")
        if not results:
            return None
        best: Optional[Tuple[float, str]] = None
        for leg in ("MP_setup", "MP_setup_ho", "MP_hold", "MP_hold_ho"):
            b = results[leg].breakdowns[leg]
            if best is None or b.F_max_MHz < best[0]:
                at_ceiling = math.isclose(b.F_max_MHz, toggle_ceiling_MHz(results[leg].inputs),
                                          rel_tol=1e-12)
                best = (b.F_max_MHz,
                        "t_RF" if at_ceiling else ("setup" if b.is_setup else "hold"))
        return best

    def _update_launch_hint(self) -> None:
        """Say what the selected launch mode costs at the current rate, in the launch
        selector's tooltip and its label's.

        The two clock modes are bounded from opposite ends — 4x by hold (its
        0.25 UI delay shrinks as the clock speeds up), 2x by setup (its 0.5 UI
        delay consumes half the UI) — so which is viable depends on F_CLK. The bound is
        read from the Example column's own Manager-to-Peripheral legs (`_launch_limit`),
        so it follows every input the margins do.
        """
        mode = LaunchMode(self._launch_combo.currentData())
        if mode is LaunchMode.ANALOG:
            txt = "ranges as tabulated — spread absorbed by both MP legs"
        else:
            f = self._fclk.value()
            t_lo, _ = launch_mode_tDD_ns(mode, f, _LAUNCH_DUTY) or (0.0, 0.0)
            grid = launch_grid_UI(mode) or 0.0
            txt = f"{grid:g} UI = {t_lo:.2f} ns at this rate"
            limit = self._launch_limit()
            if limit is not None:
                f_max, bound = limit
                if math.isfinite(f_max):
                    over = "  ⚠ above this mode's limit" if f > f_max else ""
                    txt += f" · viable to ~{f_max:.1f} MHz (bound by {bound}){over}"
                else:
                    txt += " · no rate limit"
            elif self._job.busy:
                txt += f" · viable to {_PENDING}"
            # The rows the clock places, constrained to its grid.
            txt += (f" · clocks {', '.join(_display_name(n) for n in self._LAUNCH_ROWS)} "
                    f"together, editable on this clock's {grid:g} UI grid")
        # A clocked mode applies to one column only (see _launch_mode_for), named by its
        # heading. In analog mode both columns of an EDE bus use the tabulated ranges.
        left, right = self._column_labels()
        man_ede = is_ede(self._man_spec_combo.currentData())
        if mode is not LaunchMode.ANALOG:
            txt += (f"  ·  applies to the {right} column only; the {left} column keeps "
                    f"its analog delays")
        elif man_ede or is_ede(self._per_spec_combo.currentData()):
            txt += "  ·  both columns use the tabulated delay ranges"
            if man_ede:
                txt += f"; the {right} column is seeded with the analog 9–23 ns launch"
        self._launch_combo.setToolTip(txt)
        self._launch_label.setToolTip(txt)

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

    @staticmethod
    def _last_dir() -> str:
        """The folder the Timing page last wrote or read a settings or results file in, or
        "" before any."""
        return QSettings().value(_LAST_DIR_KEY, "", type=str)

    @staticmethod
    def _remember_dir(path: str) -> None:
        QSettings().setValue(_LAST_DIR_KEY, os.path.dirname(os.path.abspath(path)))

    def _save_proposed(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, "Save Example Settings", os.path.join(self._last_dir(), "example.json"),
            "JSON (*.json)")
        if not path:
            return
        path = suffixed_save_path(self, path, ".json", "Save Example Settings")
        if path is None:
            return
        self._remember_dir(path)
        payload = self._serialize_side("proposed")
        # Written whole or not at all (fsutil.write_whole): a failed write leaves the
        # target's earlier content as it was and no other file of the user's is touched.
        try:
            write_whole(path, lambda f: json.dump(payload, f, indent=2, sort_keys=True),
                        mode="w", encoding="utf-8")
        except OSError as exc:
            QMessageBox.critical(self, "Save Example Settings", str(exc))

    def _load_proposed(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Load Example Settings", self._last_dir(),
            ("Config (*.json *.yaml *.yml);;All files (*)" if _HAVE_YAML
             else "JSON (*.json);;All files (*)"))
        if not path:
            return
        self._remember_dir(path)
        try:
            with open(path, "rb") as f:
                data = f.read(_MAX_EXAMPLE_BYTES + 1)
            if len(data) > _MAX_EXAMPLE_BYTES:
                raise ValueError(f"the file is larger than {_MAX_EXAMPLE_BYTES >> 10} KiB")
            text = data.decode("utf-8-sig")
            try:
                payload = json.loads(text)
            except ValueError as exc:
                if not _HAVE_YAML:
                    raise ValueError(f"the file is not valid JSON ({exc}), and reading YAML "
                                     f"needs PyYAML, which is not installed") from None
                payload = yaml.load(text, Loader=_ExampleLoader)
            if payload is None:
                payload = {}
            if not isinstance(payload, dict):
                raise ValueError('the file must hold a mapping of row name to '
                                 '{"min": ..., "max": ...}')
            with import_notes.collect() as notes:
                self._deserialize_side("proposed", payload,
                                       source=import_notes.source_name(path))
                # Loaded launch rows are kept, moved onto a clocked mode's grid.
                moved = self._conform_launch_rows()
                if moved:
                    import_notes.warn(import_notes.source_name(path), _snapped_note(
                        self._launch_combo.currentData(),
                        [(f"{name} {end}", old, new) for _s, name, end, old, new in moved]))
        except Exception as exc:  # noqa: BLE001
            self._recompute()                # the boxes a part-way load set
            QMessageBox.critical(self, "Load Example Settings", f"Load failed: {exc}")
            return
        self._show_default_diagram()
        self._recompute()
        self.flush_recompute()           # the warnings name an input the model refuses
        if self._compute_error:
            notes.append(import_notes.ImportNote(import_notes.source_name(path),
                                                    _unevaluable_note(self._compute_error)))
        show = getattr(self.window(), "show_import_warnings", None)
        if callable(show):
            show(notes, import_notes.source_name(path))
        else:
            import_notes.extend(notes)

    def results_export_action(self) -> QAction:
        """Export Results…, for a menu."""
        return self._export_results_action

    def copy_summary_action(self) -> QAction:
        """Copy Summary, for a menu."""
        return self._copy_summary_action

    def copy_summary(self) -> str:
        """Put `summary_text` on the clipboard and return it."""
        text = self.summary_text()
        QGuiApplication.clipboard().setText(text)
        return text

    def context_menu(self) -> QMenu:
        """The page's context menu: Copy Summary and Export Results…."""
        menu = QMenu(self)
        menu.addAction(self._copy_summary_action)
        menu.addAction(self._export_results_action)
        return menu

    def contextMenuEvent(self, event: QContextMenuEvent) -> None:
        """`context_menu` where no control under the pointer has a menu of its own."""
        menu = self.context_menu()
        menu.exec(event.globalPos())
        menu.deleteLater()

    def results_rows(self) -> List[List[str]]:
        """The rows Export Results writes, every cell ASCII (`_ascii`): a title row, then the
        inputs (each a name and its value: the spec pickers, the ramp models, the SoundWire
        system size where a side is SoundWire, the data launch, the keeper switch, the
        handover UI, the placement, the clock target, the advisory line where it has text,
        and the application's version), a blank row, `RESULTS_HEADER`, and a row per
        inequality per column in the tree's order. A row holds the inequality's number, its
        heading, the column's heading, the equation ending in ">= 0", the substitution ending
        in "=" and the margin, the margin in ns to the hundredth, the F_max the page shows in
        MHz to the thousandth ("FAIL" where it fails at the lowest rates, empty where none is
        shown) and the worst-corner line. A column the inequality does not apply to reads
        "does not apply: " and the reason as its substitution, with every other result cell
        empty. A leading =, +, -, @ is quoted in a text cell (`_text_cell`). Raises ValueError
        while the page shows no margins."""
        self.flush_recompute()
        if self._rendered is None or not self._worst:
            raise ValueError(self._compute_error or "the page shows no margins")
        spec_w, prop_w, _first = self._rendered
        man, per = self._man_spec_combo.currentData(), self._per_spec_combo.currentData()
        inputs = [["Manager", self._man_spec_combo.currentText()],
                  ["Peripheral", self._per_spec_combo.currentText()],
                  ["Manager ramp model", self._man_shape.currentText()],
                  ["Peripheral ramp model", self._per_shape.currentText()]]
        if is_soundwire(man) or is_soundwire(per):
            inputs.append(["SoundWire system size", self._size_combo.currentText()])
        inputs += [["Manager data launch", self._launch_combo.currentText()],
                   ["Manager forces keeper",
                    "yes" if self._keeper_forced_check.isChecked() else "no"],
                   ["Handover UI", "yes" if self._ho_ui_check.isChecked() else "no"],
                   ["Peripheral Placement", self._pp_combo.currentText()],
                   ["Clock Frequency Target (MHz)", f"{self._fclk.value():.3f}"]]
        if self._ceiling_label.text():
            inputs.append(["Advisory", self._ceiling_label.text()])
        inputs.append(["Application version", display_version()])
        rows: List[List[str]] = [["SWI3S Studio Timing Calculator results"], *inputs, [],
                                 list(RESULTS_HEADER)]
        labels = self._column_labels()
        for ineq in INEQUALITIES:
            head = _leg_name(ineq)
            for side, label, b in (("spec", labels[0], spec_w[ineq]),
                                   ("proposed", labels[1], prop_w[ineq])):
                if b.not_applicable:
                    rows.append([str(_INEQ_NUMBER[ineq]), head, label, "",
                                 f"does not apply: {b.not_applicable}", "", "", ""])
                    continue
                fmax = _shown_fmax(b, self._clocked(side))
                rows.append([
                    str(_INEQ_NUMBER[ineq]), head, label,
                    f"{self._equation_text(b.terms, plain=True)} ≥ 0",
                    f"{self._numeric_only(b.terms, plain=True)} = {b.margin_ns:.2f}",
                    f"{b.margin_ns:.2f}",
                    "" if not math.isfinite(fmax) else "FAIL" if fmax <= 0 else f"{fmax:.3f}",
                    _corner_text(ineq, self._worst[side][ineq],
                                 self._worst_picks.get(side, {}).get(ineq, {}))])
        return [[_text_cell(_ascii(str(c))) if k not in (5, 6) or not c else _ascii(str(c))
                 for k, c in enumerate(row)] for row in rows]

    def export_results(self) -> None:
        """Ask for a file and write the inequalities and results of both columns to it as CSV
        (`results_rows`), starting in the folder the Timing page last wrote or read a file in
        and remembering the chosen one. A write that fails, or a page that shows no margins,
        is reported in a message naming the file."""
        title = "Export Results"
        path, _ = QFileDialog.getSaveFileName(
            self, title, os.path.join(self._last_dir(), "timing_results.csv"), "CSV (*.csv)")
        if not path:
            return
        path = suffixed_save_path(self, path, ".csv", title)
        if path is None:
            return
        self._remember_dir(path)
        try:
            rows = self.results_rows()
        except ValueError as exc:
            QMessageBox.warning(self, title, f"{path} was not written: {exc}")
            return
        try:
            write_whole(path, lambda f: csv.writer(f).writerows(rows), mode="w", newline="",
                        encoding="utf-8")
        except OSError as exc:
            QMessageBox.critical(self, title, f"{path} could not be written: {exc}")

    def _serialize_side(self, side: str) -> dict:
        out: dict = {}
        for row in _side_rows(side):
            out[row.name] = {
                "min": self._min[(side, row.name)].value(),
                "max": self._max[(side, row.name)].value(),
            }
        return out

    def _deserialize_side(self, side: str, payload: dict, source: Optional[str] = None) -> None:
        """Set one side's ranges from {row: {"min", "max"}}. With a `source`, a row this
        version does not have, an entry that is not a mapping, a number outside its field's
        range, a value that is not a number, a range with its minimum above its maximum, an
        entry with neither a minimum nor a maximum and a key other than min, max and pick
        are reported through import_notes."""
        unknown: List[str] = []
        stray: List[str] = []
        clamped: List[str] = []
        unread: List[str] = []
        self._exchange_note = ""
        self._loading = True
        try:
            for name, body in payload.items():
                if (side, name) not in self._min or not isinstance(body, dict):
                    unknown.append(str(name))
                    continue
                if "min" not in body and "max" not in body:
                    unknown.append(str(name))
                    continue
                stray.extend(f"{name}.{key}" for key in body if key not in _ROW_KEYS)
                if "min" in body:
                    _set_spin(self._min[(side, name)], body["min"], f"{name} min", clamped,
                              unread)
                if "max" in body:
                    _set_spin(self._max[(side, name)], body["max"], f"{name} max", clamped,
                              unread)
                # a "pick" key is ignored: corners are selected per inequality
            swapped = self._exchange_inverted_ranges((side,))
        finally:
            self._loading = False
        if source is None:
            return
        if unread:
            import_notes.warn(source, _unread_note(unread))
        if swapped:
            import_notes.warn(source, _inverted_note(swapped))
        if unknown:
            import_notes.warn(source, f"{len(unknown)} row(s) this version does not have or "
                                      f"cannot read, ignored: {import_notes.names_listed(unknown)}")
        if stray:
            import_notes.warn(source, f"{len(stray)} key(s) this version does not read, "
                                      f"ignored: {import_notes.names_listed(stray)}")
        if clamped:
            more = f", and {len(clamped) - 3} more" if len(clamped) > 3 else ""
            import_notes.warn(source, f"{len(clamped)} value(s) outside their field's range set "
                                      f"to the nearest end: {', '.join(clamped[:3])}{more}")

    def closeEvent(self, e) -> None:     # noqa: N802 - Qt override
        # A recompute pending or running is dropped, its worker joined, so a closed view runs
        # nothing and keeps no results; showing the view again recomputes (`showEvent`).
        if self._job.stop():
            self._stale = True
        super().closeEvent(e)

    # ---- compute + render ----
    def _results_out_of_date(self) -> bool:
        """Whether the page lacks the results of its inputs: an input differs from what the
        last recompute read (`to_dict`), or a close dropped the last recompute."""
        return self._stale or self.to_dict() != self._read_inputs

    def flush_recompute(self) -> None:
        """Bring the results up to date with the inputs, blocking until they are: start a
        recompute where an input changed since the last one (`_results_out_of_date`), then
        wait for the latest recompute and apply its result. Nothing to do when the results are
        current and none is running. Call before reading the results (the tree, the diagram,
        `summary_text`); while a recompute runs the page holds none (`_blank_results`)."""
        if self._loading:
            return
        if self._results_out_of_date():
            self._recompute()
        self._job.wait()

    def _recompute(self, *_a) -> None:
        """Start a recompute of the margins, superseding any recompute pending or running.

        The inputs are read here, on the UI thread; the corner search runs on the worker
        (`_worst_of_sides`), the page holds no results until it is in (`_blank_results`), and
        `_apply_recompute` shows its result unless a later recompute has started since. An
        input that cannot be read shows its refusal at once."""
        if self._loading:
            return
        self._stale = False
        self._read_inputs = self.to_dict()
        sides = []
        for side, label in zip(SIDES, self._column_labels()):
            try:
                base, rows = self._typ_baseline(side)
            except _UNEVALUABLE as exc:
                self._job.discard()
                self._show_compute_error(f"{label} column: {_refusal_text(exc)}")
                return
            sides.append((side, label, base, rows))
        self._job.submit(functools.partial(_worst_of_sides, tuple(sides)))
        self._blank_results()

    def _blank_results(self) -> None:
        """Hold no results while a recompute runs: drop the last ones, so nothing reads them,
        and show every value the recompute replaces as `_PENDING` (`_blank_tree`). The term
        diagram keeps its size and draws "Computing…" in place of its panels, the launch hint
        shows its viable rate as `_PENDING`, the line beside the title reads `_COMPUTING` and the
        binding line is empty. The advisory line is kept: it describes the inputs."""
        self._rendered = None
        self._worst = {}
        self._worst_picks = {}
        self._blank_tree()
        self._diagram.set_pending(True)
        self._status.setText(_COMPUTING)
        self._render_binding()
        self._update_launch_hint()

    def _blank_tree(self) -> None:
        """Show `_PENDING`, dim, in place of each substitution row's numbers, its margin and
        the F_max where it shows one, keeping every row and the tree's size. Run again on a
        retheme while blank."""
        cell_w = self._label_cell_w()
        stack = [self._tree.topLevelItem(i) for i in range(self._tree.topLevelItemCount())]
        while stack:
            item = stack.pop()
            stack.extend(item.child(i) for i in range(item.childCount()))
            label = item.data(0, _SUBST_LABEL_ROLE)
            if label:
                fmax = _PENDING if item.data(0, _FMAX_ROLE) else ""
                item.setText(0, self._substitution_html(label, _PENDING, cell_w,
                                                        VizTheme.TEXT_DIM, _PENDING,
                                                        fmax=fmax))
                item.setData(0, _FMAX_ROLE, fmax)
                item.setToolTip(0, "")

    def _end_pending(self) -> None:
        """A recompute's result, or a refusal, is shown: the page is no longer blank."""
        self._status.setText("")
        self._diagram.set_pending(False)

    def _apply_recompute(self, result) -> None:
        """Show a recompute's result (`_worst_of_sides`): each column's worst corners, or the
        refusal message of an input the model cannot evaluate."""
        if isinstance(result, str):
            self._show_compute_error(result)
            return
        worst: Dict[str, Tuple[Dict[Inequality, CalcResults], Dict[str, Dict[str, str]]]] = result
        self._compute_error = ""
        self._results_notice.setVisible(False)
        (spec_res, spec_picks), (prop_res, prop_picks) = worst["spec"], worst["proposed"]
        spec_w = {i: r.breakdowns[i] for i, r in spec_res.items()}
        prop_w = {i: r.breakdowns[i] for i, r in prop_res.items()}
        self._rendered = (spec_w, prop_w, next(iter(spec_res.values())))
        # Each inequality's own worst corner, for the term diagram.
        self._worst = {"spec": spec_res, "proposed": prop_res}
        self._worst_picks = {"spec": spec_picks, "proposed": prop_picks}
        self._update_launch_hint()
        self._render_from_last()
        self._show_diagram()
        self._end_pending()

    def _recompute_failed(self, exc: BaseException) -> None:
        """A recompute raised: show no margins and name the error, so the page does not stay
        blank. The exception is raised after this, to the application's error handling."""
        self._show_compute_error(f"The margin search failed ({type(exc).__name__}: {exc}).")

    def _show_compute_error(self, msg: str) -> None:
        """Show no margins and say why: an input the model cannot evaluate, such as a
        threshold at or beyond the transmitter's rail. The tree is cleared, and the term diagram
        is closed with the highlight of the term it drew. The message shows in the advisory
        line, and above the results while the top bar is hidden, until a recompute succeeds."""
        self._compute_error = msg
        self._rendered = None
        self._worst = {}
        self._worst_picks = {}
        self._update_launch_hint()
        self._render_binding()
        self._tree.clear()
        self._hl.discard(self._diagram_term)
        self._hide_diagram()
        self._refresh_highlight()
        self._end_pending()
        self._set_advisory(f"⚠ No margins. {msg}", msg)
        self._sync_page_visibility()

    def _render_from_last(self) -> None:
        """Draw the last computed results: the tree, the binding line and the advisories."""
        if self._rendered is None:
            return
        spec_w, prop_w, first = self._rendered
        self._render_tree(spec_w, prop_w)
        self._render_binding()
        self._render_advisories(first)

    @staticmethod
    def _binding_rates(*per_side: Dict[Inequality, CalcResults]
                       ) -> List[Optional[Tuple[float, Inequality]]]:
        """Each column's binding F_max and the inequality that sets it, every inequality read
        at its own worst corner (`binding_at_own_corners`); None for a column no leg bounds."""
        return [binding_at_own_corners(results) for results in per_side]

    def _render_binding(self) -> None:
        """The line beside the Timing Inequalities title: "Binding F_MAX —", then per column its
        name, its binding F_max (`_binding_fmax_text`), red where below the Clock Frequency
        Target, and the number of the inequality that sets it; the tooltip names each
        inequality whole. Empty while the page holds no results."""
        if not self._worst:
            self._binding.setText("")
            self._binding.setToolTip("")
            return
        fclk = self._fclk.value()
        rates = self._binding_rates(self._worst["spec"], self._worst["proposed"])
        shown, tips = [], []
        for lab, rate in zip(self._column_labels(), rates):
            if rate is None:
                continue
            f, leg = rate
            text = html.escape(_binding_fmax_text(f))
            if f < fclk:
                text = f'<span style="color:{VizTheme.SEM_ERROR}">{text}</span>'
            shown.append(f"{html.escape(lab)}: {text} ({_INEQ_NUMBER[leg]})")
            tips.append(f"{lab}: {_binding_fmax_text(f)}, {_heading_title(leg)}")
        self._binding.setText(f"Binding {FMAX_HTML} — " + " · ".join(shown) if shown else "")
        self._binding.setToolTip(
            "The lowest F_MAX over the inequalities that bound the bus, each at its own worst "
            "corner. The Peripheral-to-Peripheral data inequalities and an inequality that "
            "does not apply do not bound it.\n" + "\n".join(tips) if shown else "")

    def _refresh_advisories(self) -> None:
        """Redraw the advisory line from the last results, which are unchanged (an exchange
        note cleared). With none held, the next result's render draws it."""
        if self._rendered is not None:
            self._render_advisories(self._rendered[2])

    def _render_advisories(self, res) -> None:
        """Qualify the margins: an illegal clock rate, and the result's caveats as the
        tooltip. A rate above the ceiling produces arithmetic that is fine and physically
        meaningless, so it is not silently absorbed."""
        bits = []
        if not res.F_CLK_legal:
            bits.append(f"above the {res.F_CLK_ceiling_MHz:g} MHz ceiling for this bus")
        if self._exchange_note:
            bits.append(self._exchange_note)
        self._set_advisory("⚠ " + "; ".join(bits) if bits else "",
                           "\n\n".join(res.caveats) if res.caveats else "")

    def _render_tree(self, spec_w, prop_w) -> None:
        self._tree.clear()
        fclk = self._fclk.value()
        clocked_l, clocked_r = self._clocked("spec"), self._clocked("proposed")
        for gi, (base, members) in enumerate(_INEQ_GROUPS):
            if gi > 0:
                spacer = QTreeWidgetItem(self._tree, [""])       # blank line between blocks
                spacer.setFlags(Qt.NoItemFlags)
            head = QTreeWidgetItem(self._tree, [_whole_marked(base, _INEQ_LABEL[base])])
            head.setData(0, _WHOLE_ROLE, members[0])
            f = QFont(self._tree.font()); f.setBold(True)   # scaled base font, bold
            head.setFont(0, f)
            for mi, ineq in enumerate(members):
                bs, bp = spec_w[ineq], prop_w[ineq]
                if mi > 0:
                    gap = QTreeWidgetItem(head, [""])            # separate the two inequalities
                    gap.setFlags(Qt.NoItemFlags)
                # Equation on its own line under the title, numbered for reference.
                left_lab, right_lab = self._column_labels()
                if bs.not_applicable or bp.not_applicable:
                    for lab, b, clk in ((left_lab, bs, clocked_l), (right_lab, bp, clocked_r)):
                        if b.not_applicable:
                            self._add_not_applicable(head, ineq, lab, b)
                        else:
                            self._add_equation(head, ineq, b.terms, lab)
                            self._add_side_term(head, lab, b, fclk, clk).setData(
                                0, Qt.UserRole, ineq)
                elif self._same_terms(bs.terms, bp.terms):
                    self._add_equation(head, ineq, bs.terms)
                    self._add_side_term(head, left_lab, bs, fclk, clocked_l).setData(
                        0, Qt.UserRole, ineq)
                    self._add_side_term(head, right_lab, bp, fclk, clocked_r).setData(
                        0, Qt.UserRole, ineq)
                else:
                    # The columns price different terms (a release by t_DZ against one by an
                    # EDE Delay): each shows its own equation row, then its own numbers.
                    for lab, b, clk in ((left_lab, bs, clocked_l), (right_lab, bp, clocked_r)):
                        self._add_equation(head, ineq, b.terms, lab)
                        self._add_side_term(head, lab, b, fclk, clk).setData(
                            0, Qt.UserRole, ineq)
            head.setExpanded(True)
        self._fit_tree_width()
        self._fit_tree_height()
        self._unify_section_widths()
        self._fit_split()

    @staticmethod
    def _same_terms(left, right) -> bool:
        """Whether both columns print the same symbols (`_shown_symbol`), in the same order:
        they then share one equation row."""
        return ([_shown_symbol(t.symbol) for t in left]
                == [_shown_symbol(t.symbol) for t in right])

    def _add_equation(self, parent: QTreeWidgetItem, ineq: str, terms,
                      label: str = "") -> None:
        """An inequality's equation row, numbered for reference; a column's own row carries
        the column's label after the number."""
        head = f"{_INEQ_NUMBER[ineq]}) " + (f"{label}: " if label else "")
        item = QTreeWidgetItem(parent, [f"{head}{self._equation_text(terms)} ≥ 0"])
        item.setData(0, Qt.UserRole, ineq)
        item.setData(0, _WHOLE_ROLE, ineq)

    def _add_not_applicable(self, parent: QTreeWidgetItem, ineq: str, label: str, b) -> None:
        """A column's row for an inequality that places no requirement on its bus: the
        reason, then " = —" for its margin in the text colour, and no F_max."""
        item = QTreeWidgetItem(parent, [f"{_INEQ_NUMBER[ineq]}) {label}: does not apply: "
                                        f"{b.not_applicable} = —"])
        item.setData(0, Qt.UserRole, ineq)

    def _label_cell_w(self) -> int:
        """The width of a substitution row's label cell: the widest column heading's."""
        fm = self._tree.fontMetrics()
        return max(_LABEL_CELL_W, *(fm.horizontalAdvance(f"{lab}:") + _px(8)
                                    for lab in self._column_labels()))

    @staticmethod
    def _substitution_html(label: str, numbers: str, cell_w: int,
                           colour: Optional[str] = None, margin: str = "",
                           margin_colour: Optional[str] = None, fmax: str = "",
                           fmax_colour: Optional[str] = None) -> str:
        """A substitution row's text: the label in a fixed-width table cell, so the right
        column's substitution lines up under the left one's (a proportional font cannot be
        padded), then `numbers`; with a `margin`, " = " and the margin in `margin_colour`
        where given; and with an `fmax`, " (", `FMAX_HTML`, " ", `fmax` in `fmax_colour` where
        given, and ")". `colour`, where given, colours all of it."""
        if margin:
            if margin_colour is not None:
                margin = f'<span style="color:{margin_colour}">{margin}</span>'
            numbers = f"{numbers} = {margin}"
        if fmax:
            if fmax_colour is not None:
                fmax = f'<span style="color:{fmax_colour}">{fmax}</span>'
            numbers = f"{numbers} ({FMAX_HTML} {fmax})"
        if colour is not None:
            numbers = f'<span style="color:{colour}">{numbers}</span>'
        return (f'<table border="0" cellspacing="0" cellpadding="0"><tr>'
                f'<td width="{cell_w}">{label}:</td>'
                f'<td>{numbers}</td></tr></table>')

    def _add_side_term(self, parent: QTreeWidgetItem, label: str, b,
                       fclk: float, clocked: bool = False) -> QTreeWidgetItem:
        # F_max after the margin where it depends on the clock rate (`_shown_fmax`; `clocked`,
        # the column's Manager launch is clocked), red where it is below the target rate; none
        # where the leg never fails.
        x = _shown_fmax(b, clocked)
        fmax = _fmax_text(x)
        html = self._substitution_html(
            label, self._numeric_only(b.terms), self._label_cell_w(),
            margin=_margin_text(b.margin_ns), margin_colour=_margin_colour(b.margin_ns),
            fmax=fmax, fmax_colour=VizTheme.SEM_ERROR if math.isfinite(x) and x < fclk else None)
        child = QTreeWidgetItem(parent, [html])
        child.setData(0, _SUBST_LABEL_ROLE, label)
        child.setData(0, _FMAX_ROLE, fmax)
        # F_max is the first failure as the rate rises; the note names any higher rates at
        # which the leg passes again.
        if fmax and b.F_max_note:
            child.setToolTip(0, f"{FMAX_HTML} is the lowest rate at which the leg fails; "
                                f"it {b.F_max_note}.")
        return child

    # ---- equation formatting ----
    @staticmethod
    def _op_of(t) -> str:
        """`-` or `+`: the inequality-level sign of `t`."""
        return getattr(t, "op", None) or ("-" if t.value_ns < 0 else "+")

    @staticmethod
    def _signed(ops, bodies) -> str:
        """`bodies` joined by their operators; the first carries only a minus."""
        pieces: List[str] = []
        for i, (op, body) in enumerate(zip(ops, bodies)):
            display_op = "−" if op == "-" else "+"
            pieces.append(("−" if op == "-" else "") + body if i == 0
                          else f" {display_op} {body}")
        return "".join(pieces)

    @staticmethod
    def _equation_text(terms, plain: bool = False) -> str:
        """The equation row: one symbol per term. Anchored so a click can identify it and the
        delegate can wash it; the operator stays outside, so `+`/`−` are not click targets
        and only the term itself highlights. `plain` prints the bare symbols instead."""
        return TimingView._signed(
            [TimingView._op_of(t) for t in terms],
            [_shown_symbol(t.symbol) if plain
             else _anchored(_term_key(t.symbol), _subscript(t.symbol)) for t in terms])

    @staticmethod
    def _numeric_only(terms, plain: bool = False) -> str:
        """The substitution row: one number per term, or the PRODUCT where a term is one.

        A crossing term reads `X_MP,rise · Man_t_RF,DATA` in the equation row, and its
        coefficient appears nowhere else on the row -- so the substitution shows `c·t_RF`, the
        coefficient times the lane's value, rather than their product, and the reader can
        check the multiplication against the symbol beside it.

        A negative coefficient is written `(−c)·t_RF`, so the operator before it reads as the
        term's own sign.

        A GATHERED crossing shows all THREE numbers: `(c_late − c_early)·t_RF`. It prints as
        `(X_MP,A,rise − X_MP,B,fall) · Man_t_RF,CLK` -- two crossings of one pin
        netted into a single product -- so the reader can check the netting against the symbol
        beside it. `Term.factors` carries the coefficients first and the row value last, so the
        count of entries is what selects the form.

        Terms with no product to show (`Man_t_DD`, `Per_t_IS`) and terms whose printed
        number IS the operand (`1·UI`) stay single numbers. A single number is the term's own
        value, its contribution with the operator taken off, so a subtracted negative value
        reads `− (−2.00)` and the row adds up to the margin.

        `plain` prints the cells without anchors.
        """
        ops, bodies = [], []
        for t in terms:
            ops.append(TimingView._op_of(t))
            num = TimingView._number(t)
            bodies.append(num if plain else _anchored(_term_key(t.symbol), num))
        return TimingView._signed(ops, bodies)

    @staticmethod
    def _number(t) -> str:
        """One term's cell in the substitution row (see `_numeric_only`)."""
        f = getattr(t, "factors", None)
        op = TimingView._op_of(t)
        if not f:
            own = t.value_ns if op == "+" else -t.value_ns
            return f"{abs(own):.2f}" if round(own, 2) >= 0 else f"(−{abs(own):.2f})"
        if len(f) == 2:
            coef = TimingView._coef(f[0])
            if coef.startswith("−"):
                coef = f"({coef})"
            return f"{coef}·{f[1]:.2f}"
        # Coefficients signed relative to the term's hoisted operator, row value last.
        inner = "".join(
            TimingView._coef(c) if k == 0
            else f" {'−' if round(c, 2) < 0 else '+'} {abs(c):.2f}"
            for k, c in enumerate(f[:-1]))
        return f"({inner})·{f[-1]:.2f}"

    @staticmethod
    def _coef(c: float) -> str:
        """A signed coefficient to two places: U+2212 for a negative one, no sign on a value
        that rounds to zero."""
        return f"−{abs(c):.2f}" if round(c, 2) < 0 else f"{abs(c):.2f}"

    @staticmethod
    def _fmt_fmax_delta(left: float, right: float) -> str:
        """The right column's F_max less the left's where both are rates; "—" otherwise."""
        if not (math.isfinite(left) and math.isfinite(right) and left > 0 and right > 0):
            return "—"
        return f"{right - left:+.3f}"

    @staticmethod
    def _fmt_margin(x: float) -> str:
        """A margin to the hundredth with its sign; "—" for a leg that does not apply."""
        return f"{x:+.2f}" if math.isfinite(x) else "—"

    @staticmethod
    def _fmt_margin_delta(left: float, right: float) -> str:
        """The right column's margin less the left's where both are finite; "—" otherwise."""
        return f"{right - left:+.2f}" if math.isfinite(left) and math.isfinite(right) else "—"

    @staticmethod
    def _fmt_fmax(x: float) -> str:
        if not math.isfinite(x):
            return "—"
        return "FAIL" if x <= 0 else f"{x:.3f}"
