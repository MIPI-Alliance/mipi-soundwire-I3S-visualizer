"""Timing mode — a Qt port of the standalone SWI3S PHY2 Spec Calculator
(`timing-analysis/app/spec_calculator.py`).

Compare the SWI3S specification against an example implementation. Corners are
selected automatically — each inequality is evaluated at its own worst corner —
so there is no manual corner picker. The page:

- A top control bar: F_CLK target, Reset, and Save / Load of the Example
  column (YAML, or JSON if PyYAML is absent).
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

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import (QAbstractTextDocumentLayout, QBrush, QColor, QFont,
                           QPalette, QTextDocument)
from PySide6.QtWidgets import (QAbstractItemView, QAbstractSpinBox, QApplication,
                               QComboBox, QDoubleSpinBox,
                               QFileDialog, QFrame, QGridLayout, QHBoxLayout, QLabel,
                               QMessageBox, QPushButton, QScrollArea, QStyle,
                               QStyledItemDelegate, QStyleOptionViewItem,
                               QTreeWidget, QTreeWidgetItem,
                               QVBoxLayout, QWidget)

from ..timing import (CalcInputs, CalcRow, INEQUALITIES, ParamRange,
                      apply_row_picks, compute, default_swi3s_rows,
                      find_worst_corner_rows)
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
_ALL_ROWS: List[CalcRow] = default_swi3s_rows(lock_mgr_lanes=False)
_ROW_BY_NAME: Dict[str, CalcRow] = {r.name: r for r in _ALL_ROWS}

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
_UNIT_COL_W = _px(52)   # unit column width — fits the widest unit ("×V_DD")
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

# Full-word inequality titles (keys are the INEQUALITIES ids).
_INEQ_LABEL = {
    "MP_setup": "Manager-to-Peripheral Setup",
    "MP_hold": "Manager-to-Peripheral Hold",
    "PM_setup": "Peripheral-to-Manager Setup",
    "PM_hold": "Peripheral-to-Manager Hold",
}

# Which inequalities show under each heading. A setup heading also carries its
# handover ("_ho") variant, stacked under the same title (no separate heading).
_INEQ_GROUPS = [
    ("MP_setup", ("MP_setup", "MP_setup_ho")),
    ("MP_hold", ("MP_hold",)),
    ("PM_setup", ("PM_setup", "PM_setup_ho")),
    ("PM_hold", ("PM_hold",)),
]


class _RichTextDelegate(QStyledItemDelegate):
    """Render a tree column's text as HTML so <sub>…</sub> subscripts show (a plain
    QTreeWidgetItem would print the tags literally). Text colour comes from the
    paint palette so it applies to nested markup (e.g. the aligned label table)."""

    def _doc(self, opt: QStyleOptionViewItem) -> QTextDocument:
        doc = QTextDocument()
        doc.setDefaultFont(opt.font)
        doc.setDocumentMargin(0)
        doc.setHtml(opt.text)
        return doc

    def paint(self, painter, option, index):
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        style = opt.widget.style() if opt.widget else QApplication.style()
        doc = self._doc(opt)
        opt.text = ""
        style.drawControl(QStyle.CE_ItemViewItem, opt, painter, opt.widget)
        rect = style.subElementRect(QStyle.SE_ItemViewItemText, opt, opt.widget)
        painter.save()
        painter.translate(rect.left(),
                          rect.top() + max(0.0, (rect.height() - doc.size().height()) / 2))
        ctx = QAbstractTextDocumentLayout.PaintContext()
        ctx.palette.setColor(QPalette.Text, QColor(VizTheme.TEXT))
        doc.documentLayout().draw(painter, ctx)
        painter.restore()

    def sizeHint(self, option, index):
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        doc = self._doc(opt)
        return QSize(int(doc.idealWidth()) + 8, int(doc.size().height()))


class _NoWheelSpinBox(QDoubleSpinBox):
    """A spin box that ignores the mouse wheel, so scrolling the page while the
    pointer is over a box scrolls the page instead of nudging the value (which
    would re-fit and visibly resize the section)."""

    def wheelEvent(self, e):
        e.ignore()      # let the scroll propagate to the page

# Binds whose CalcInputs value is a fraction but is shown to the user as a percent
# (display value = fraction × 100). V_IH/V_IL are also *_frac but stay ×V_DD.
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
# Man_t_DD → Man_t<sub>DD</sub>, Δ_cross,MP → Δ<sub>cross,MP</sub>). The leading
# Man_/Per_ underscore is kept literal; only the underscore right before one of
# these tokens becomes the subscript, and any trailing ,qualifiers join it.
_SUB_RE = re.compile(r"_(IH|IL|DD|IS|DZ|ZD|RF|PD|cross)((?:,[A-Za-z]+)*)")


def _subscript(name: str) -> str:
    """HTML label with timing subscripts (for QLabel / the rich-text delegate)."""
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
    return 2                         # ×V_DD (V_IH/V_IL) and any fraction fallback


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
        return "×V_DD"
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
        self._man_shape: Optional[QComboBox] = None      # shared across Spec + Example
        self._per_shape: Optional[QComboBox] = None

        root = QVBoxLayout(self)
        root.setContentsMargins(6, 6, 6, 6)
        self.setFont(_base_font())                        # scales combos/buttons/tree text
        self.setStyleSheet(_scaled_stylesheet())

        title = QLabel("SWI3S PHY2 Timing Calculator")
        title.setStyleSheet(_TITLE_CSS)
        title.setContentsMargins(_HEAD_INDENT, 0, 0, 0)
        root.addWidget(title)

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
        self._tree = QTreeWidget()
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
        self._tree.setItemDelegateForColumn(0, _RichTextDelegate(self._tree))  # HTML subscripts
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
        results tree, so all the rounded rectangles line up and the top-bar
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
    def _build_top_bar(self) -> QHBoxLayout:
        bar = QHBoxLayout()
        bar.setContentsMargins(_HEAD_INDENT, 0, 0, 0)   # align with the section titles
        bar.addWidget(QLabel("Clock Frequency Target"))
        self._fclk = _NoWheelSpinBox()
        self._fclk.setButtonSymbols(QAbstractSpinBox.NoButtons)
        self._fclk.setRange(0.001, 27.0)
        self._fclk.setDecimals(3)
        self._fclk.setSingleStep(0.1)
        self._fclk.setAlignment(Qt.AlignRight)
        self._fclk.setMaximumWidth(_BOX_W)
        self._fclk.setValue(13.2)
        self._fclk.valueChanged.connect(self._recompute)
        bar.addWidget(self._fclk)
        bar.addWidget(QLabel("MHz"))       # unit after the box, like the rest of the page

        bar.addStretch(1)
        reset = QPushButton("Reset")
        reset.clicked.connect(self._reset_to_defaults)
        bar.addWidget(reset)
        save = QPushButton("Save Example")
        save.clicked.connect(self._save_proposed)
        bar.addWidget(save)
        load = QPushButton("Load Example")
        load.clicked.connect(self._load_proposed)
        bar.addWidget(load)
        return bar

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
        grid.addWidget(self._col_head("Specification"), 0, 1, 1, 2)
        grid.addWidget(self._col_head("Example"), 0, 5, 1, 2)
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
        grid.addWidget(QLabel(_display_name(name)), gridrow, 0)
        dec = _decimals_for(row)
        unit = _unit_for(row)
        for side, cmin, cmax, cunit in (self._SPEC_COLS, self._EX_COLS):
            mn = self._make_spin(dec); mx = self._make_spin(dec)
            mn.valueChanged.connect(self._recompute)
            mx.valueChanged.connect(self._recompute)
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
            out.append(CalcRow(row.name, row.binds,
                               ParamRange(mn, 0.5 * (mn + mx), mx), row.linked_to))
        return out

    def _base_inputs(self, side: str) -> CalcInputs:
        # The ramp models are shared across Spec and Example (side is ignored here).
        return CalcInputs(
            Man_shape=self._man_shape.currentData(),
            Per_shape=self._per_shape.currentData(),
            F_CLK_target_MHz=self._fclk.value(),
        )

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
        copyable summary."""
        lines = [" | ".join(_FMAX_COLS)]
        spec_w = self._per_inequality_worst("spec")
        prop_w = self._per_inequality_worst("proposed")
        for ineq in INEQUALITIES:
            bs, bp = spec_w[ineq], prop_w[ineq]
            lines.append(" | ".join([
                ineq.replace("_", " "),
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
             "per_shape": self._per_shape.currentData()}
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
            if "fclk_target" in d:
                self._fclk.setValue(float(d["fclk_target"]))
            self._set_shape(self._man_shape, d.get("man_shape", d.get("spec::man_shape")))
            self._set_shape(self._per_shape, d.get("per_shape", d.get("spec::per_shape")))
            for side in SIDES:
                for row in _ALL_ROWS:
                    self._apply_keys(side, row.name, d)
        finally:
            self._loading = False
        self._recompute()

    def _apply_keys(self, side: str, name: str, d: dict) -> None:
        # A legacy "::pick" key (pre-auto-corner) is simply ignored.
        kmin, kmax = f"{side}::{name}::min", f"{side}::{name}::max"
        if kmin in d:
            self._min[(side, name)].setValue(float(d[kmin]))
        if kmax in d:
            self._max[(side, name)].setValue(float(d[kmax]))

    # ---- actions ----
    def _reset_to_defaults(self) -> None:
        self._loading = True
        try:
            self._fclk.setValue(13.2)
            self._set_shape(self._man_shape, "linear")
            self._set_shape(self._per_shape, "linear")
            for side in SIDES:
                for row in _ALL_ROWS:
                    sc = _display_scale(row)
                    self._min[(side, row.name)].setValue(float(row.range.min) * sc)
                    self._max[(side, row.name)].setValue(float(row.range.max) * sc)
        finally:
            self._loading = False
        self._recompute()

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
                # Equation on its own line under the title. Kept in column 0 (not
                # spanned) so column 0 sizes to fit it and the Margin column lands
                # after the equation rather than overlapping it.
                QTreeWidgetItem(head, [f"{self._equation_text(bs.terms)} ≥ 0", "", ""])
                self._add_side_term(head, "Specification", bs, fclk)
                self._add_side_term(head, "Example", bp, fclk)
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
        child = QTreeWidgetItem(parent, [html, f"{b.margin_ns:+.1f} ns", fmax])
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
            sym = _subscript(t.symbol)      # HTML subscripts (rendered by the delegate)
            if i == 0:
                pieces.append(("−" if op == "-" else "") + sym)
            else:
                pieces.append(f" {display_op} {sym}")
        return "".join(pieces)

    @staticmethod
    def _numeric_only(terms) -> str:
        pieces: List[str] = []
        for i, t in enumerate(terms):
            mag = abs(t.value_ns)
            op = getattr(t, "op", None) or ("-" if t.value_ns < 0 else "+")
            display_op = "−" if op == "-" else "+"
            if i == 0:
                pieces.append(("−" if op == "-" else "") + f"{mag:.2f}")
            else:
                pieces.append(f" {display_op} {mag:.2f}")
        return "".join(pieces)

    @staticmethod
    def _fmt_fmax(x: float) -> str:
        if not math.isfinite(x):
            return "—"
        return "FAIL" if x <= 0 else f"{x:.3f}"
