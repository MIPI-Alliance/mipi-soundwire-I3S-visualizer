"""2D bus-grid view: the Rows x Columns raster, rendered like the SWI3S Visualizer.

Cells come from the C++ placement cascade (`Session.grid_cells()` for a decoded
bus, or `grid_from_csv` for an authored config) — the same renderer serves both.
The grid is MULTI-EMIT: each (row, col) may carry a *source* transport (drawn in
the top half of the row) and a *sink* transport (bottom half), exactly like the
Visualizer's source/sink split. Data cells are coloured per (device, data-port)
using the Visualizer's pastel palette ("Eddie's colours"); each sample is labelled
``S<sample>C<channel>`` (merged across its bit columns), TxPresent as ``TxP<ch>``,
DRQ as ``DRQ``. Guards show ``G0``/``G1``; tails draw a decaying ringing squiggle;
Column 0 is the full-height Control Data Stream. A right-hand key labels each
row's Source / Sink halves. Handover turnaround ↔ arrows are a Visualizer-only
annotation: they are drawn from the engine's BusModel (set_bus_model) when the
config enables handover, and are NOT synthesized for a decoded (analyzer) grid.

Faithful port of `mipi-soundwire-I3S-visualizer/src/ui/frame_renderer.py`
(COLUMN_SIZE=39, ROW_SIZE=30, Helvetica). Renders on a QGraphicsScene so the
key/labels are included in the SVG/PNG export.

Deferred: the *system* CDS / S1 handover columns (a fixed-column turnaround driven
by the interface's CDS/S1 handover-enable, independent of the data ports) — the
C++ grid models CDS only as Column 0, so those leading arrows aren't placed yet.
"""
from __future__ import annotations

import math
import sys
from typing import Dict, List, Tuple

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QFont, QPainterPath, QPen, QPolygonF
from PySide6.QtWidgets import QGraphicsScene, QGraphicsView

import swi3score

from .theme import VizTheme

# Visualizer cell proportions (src/ui/constants.py COLUMN_SIZE=39, ROW_SIZE=30),
# scaled here so the grid reads a little larger than the Visualizer's base in both
# the Visualizer and the Analyzer (both render through this view). _round-half-up
# keeps the .x5 sizes from Python's banker's rounding.
_SCALE = 1.125
def _s(v: float) -> int:
    return int(v * _SCALE + 0.5)
# Windows renders the grid text noticeably larger than macOS at the same point size,
# so shrink ONLY the fonts (not the cells) ~20% there. _fpt() scales a base point size
# by _SCALE (like the cells) then applies the platform font factor.
_FONT_SCALE = 0.8 if sys.platform.startswith("win") else 1.0
def _fpt(base: float) -> int:
    return max(1, int(_s(base) * _FONT_SCALE + 0.5))
_CW = _s(39)
_RH = _s(30)
_HALF = _RH / 2
_ROWHDR_W = _s(30)             # left gutter for row numbers
_COLHDR_H = _s(22)             # column-number header band
_KEY_H = _s(26)                # top data-port colour-key band
_SSKEY_W = _s(48)              # right-hand Source/Sink key column
_APP_FONT = "Helvetica"        # matches the visualizer; portable in SVG

# Grid colours come from the shared visualizer theme (lines/text a touch lighter,
# background matching the parameters section).
_LINE = QColor(VizTheme.GRID_LINE)     # grid lines
_LABEL = QColor(VizTheme.GRID_TEXT)    # row/column numbers + key text
_INK = QColor(VizTheme.GRID_INK)       # in-cell text (data-port colours are light)
_BG = QColor(VizTheme.GRID_BG)         # canvas / empty background = parameters bg
_CDS_FILL = QColor(VizTheme.GRID_CDS_FILL)  # CDS / system full-height fill
_TX_FILL = QColor(0x35, 0xC2, 0x8A)         # TX map: a UI with a data-line transition

# Visualizer data-port palette (src/config/constants.py Colors.DP_COLORS — light
# pastels, "thanks to Eddie").
_DP_PALETTE = [
    "#FF80BF", "#FFA080", "#FFFF80", "#A0FF80", "#80FFFF", "#8080FF",
    "#BF80FF", "#FFBFFF", "#FFBFBF", "#FFFFBF", "#BFFFBF", "#BFFFFF",
]

# Slot enum ints (SwI3sSlot): 0 Empty,1 Data,2 TxPresent,3 Guard0,4 Guard1,5 Tail,6 Drq
_DATA, _TXP, _G0, _G1, _TAIL, _DRQ = 1, 2, 3, 4, 5, 6# Extra slots when driven by the Visualizer engine (mapped in viz_engine):
_S0, _S1, _HANDOVER = 7, 8, 9
_DATA_SLOTS = (_DATA, _TXP, _DRQ)
_SYS_SLOTS = (_G0, _G1, _TAIL, _S0, _S1, _HANDOVER)   # merge by type, not channel

# Clash-marker colours (CanvasColors): bus red, device yellow, read overlap blue.
_CLASH_COLOR = {"bus": QColor(0xD3, 0x2F, 0x2F), "device": QColor(0xFB, 0xC0, 0x2D),
                "read": QColor(0x19, 0x76, 0xD2)}

# Config-vs-decoded comparison + authoring clash outlines (CanvasColors).
_DIFF_PEN = {
    "changed": QColor(0xFB, 0xC0, 0x2D),
    "decoded_only": QColor(80, 200, 110),
    "expected_only": QColor(0xD3, 0x2F, 0x2F),
    "clash": QColor(0xD3, 0x2F, 0x2F),
}


class GridView(QGraphicsView):
    def __init__(self) -> None:
        super().__init__()
        self._scene = QGraphicsScene(self)
        self.setScene(self._scene)
        self.setBackgroundBrush(_BG)
        self._stream_colors: dict = {}
        self._stream_channels: dict = {}
        self._by_dp = False        # engine mode colours by dp number (like the Visualizer)
        self._dp_display: Dict[Tuple[int, int], dict] = {}
        self._sys_slots: List[tuple] = []
        self._engine_mode = False
        self._clashes: dict = {}
        self._cell_at: dict = {}    # (row,col) -> cells; (re)built in set_bus_model, read by _clash_x
        self._show_key = True       # draw the top DP colour-key band (see set_show_key)
        # Visualizer font scheme (src/ui/constants.py TEXT_SIZE=12): numbers 12,
        # in-cell labels 12-4=8, Source/Sink 12-3=9, colour key 12-2=10 — each
        # scaled up with the cells (see _SCALE).
        self._f_num = QFont(_APP_FONT); self._f_num.setPointSize(_fpt(12)); self._f_num.setBold(True)
        self._f_label = QFont(_APP_FONT); self._f_label.setPointSize(_fpt(10))
        self._f_ss = QFont(_APP_FONT); self._f_ss.setPointSize(_fpt(9))
        self._f_key = QFont(_APP_FONT); self._f_key.setPointSize(_fpt(10))

    # ---- public API ----
    def retheme(self) -> None:
        """Re-read the palette after a theme switch: recompute the module-level grid
        colours (lines/labels/ink/background/CDS fill) from the now-active VizTheme
        and repaint the canvas background. The caller re-renders the current data
        (set_cells / set_bus_model) so every cell picks up the new colours."""
        global _LINE, _LABEL, _INK, _BG, _CDS_FILL
        _LINE = QColor(VizTheme.GRID_LINE)
        _LABEL = QColor(VizTheme.GRID_TEXT)
        _INK = QColor(VizTheme.GRID_INK)
        _BG = QColor(VizTheme.GRID_BG)
        _CDS_FILL = QColor(VizTheme.GRID_CDS_FILL)
        self.setBackgroundBrush(_BG)

    def set_show_key(self, on: bool) -> None:
        """Show/hide the top data-port colour-key band. The caller re-renders after
        toggling. Hidden by default in the Visualizer (saves vertical space); shown
        when the frame is maximized or exported. The reclaimed band height is given
        back to the grid (geometry follows _key_band())."""
        self._show_key = bool(on)

    @property
    def show_key(self) -> bool:
        return self._show_key

    def _key_band(self) -> int:
        return _KEY_H if self._show_key else 0

    def stream_color(self, device: int, dp: int) -> QColor:
        key = dp if self._by_dp else (device, dp)
        return self._stream_colors.get(key, _CDS_FILL)

    def show_message(self, text: str, subtext: str = "") -> None:
        """Replace the grid with a centred message — used to render a non-grid
        state (e.g. the link bring-up before a PHY is selected, when there is no
        bus geometry to draw yet)."""
        self._scene.clear()
        self._engine_mode = False
        self._clashes = {}
        title = self._scene.addText(text, self._f_num)
        title.setDefaultTextColor(_LABEL)
        title.setPos(0, 0)
        if subtext:
            sub = self._scene.addText(subtext, self._f_label)
            sub.setDefaultTextColor(_LINE)
            sub.setPos(0, _RH)
        self._finalize_scene()

    @property
    def item_count(self) -> int:
        return len(self._scene.items())

    def set_cells(self, cells: List[dict], column_count: int,
                  cds_labels: dict | None = None,
                  dp_display: dict | None = None,
                  system_slots: list | None = None) -> None:
        """Render the grid. `cds_labels` maps row -> short CDS symbol text for that
        row's Column 0. `dp_display` maps (device, dp) -> {"display_fields": int
        (1=Sample 2=Channel 4=Bit), "handover": bool} for the Visualizer display
        semantics that aren't register-level (label fields + handover-enable). When
        absent, a port defaults to Channel|Bit labels with handover on.
        `system_slots` is a list of (col, kind) interface-level CDS-block slots
        drawn at every row — kind in {'cds','guard','tail','handover'}."""
        self._scene.clear()
        cds_labels = cds_labels or {}
        self._dp_display = dp_display or {}
        self._sys_slots = list(system_slots or [])
        self._engine_mode = False
        self._clashes = {}
        self._assign_stream_colors(cells)

        rows = max((c["row"] for c in cells), default=-1) + 1
        cols = max(column_count, (max((c["col"] for c in cells), default=-1) + 1))
        if rows <= 0 or cols <= 0:
            self._finalize_scene()
            return

        self._draw_top_key(cols)
        self._draw_headers(rows, cols)

        # group cells by row for merging
        by_row: Dict[int, List[dict]] = {}
        for c in cells:
            by_row.setdefault(c["row"], []).append(c)
        for r in range(rows):
            self._draw_row(r, by_row.get(r, []), cols, cds_labels)

        self._draw_frame(rows, cols)
        self._finalize_scene()

    def set_bus_model(self, cells: List[dict], clashes: dict, column_count: int,
                      num_rows: int) -> None:
        """Render from the Visualizer engine's BusModel (via viz_engine.render_payload):
        the cells already include the CDS/S0/S1/handover system bits with device, so
        the per-source-port handover heuristic + CDS-block synthesis are suppressed
        and clash markers (X) are drawn from the model's clash lists."""
        self._scene.clear()
        self._dp_display = {}
        self._sys_slots = []
        self._engine_mode = True
        self._clashes = clashes or {}
        self._assign_stream_colors(cells, by_dp=True)
        rows = max(num_rows, (max((c["row"] for c in cells), default=-1) + 1))
        cols = max(column_count, (max((c["col"] for c in cells), default=-1) + 1))
        if rows <= 0 or cols <= 0:
            self._finalize_scene()
            return
        self._draw_top_key(cols)
        self._draw_headers(rows, cols)
        by_row: Dict[int, List[dict]] = {}
        self._cell_at: dict = {}
        for c in cells:
            by_row.setdefault(c["row"], []).append(c)
            self._cell_at.setdefault((c["row"], c["col"]), []).append(c)
        for r in range(rows):
            self._draw_row(r, by_row.get(r, []), cols, {})
        # clash markers (X) over the offending cells, contained to the cell region
        for (r, c), kind in self._clashes.items():
            if 0 <= r < rows and 0 <= c < cols:
                self._clash_x(r, c, kind)
        self._draw_frame(rows, cols)
        self._finalize_scene()

    def set_tx_raster(self, raster: dict) -> None:
        """Render the TX map: a raster of REAL capture rows x columns where each cell is
        filled 'TX' if its UI carried a physical data-line transition (see
        Session.tx_raster). Column 0 is drawn as the CDS column. The row gutter shows
        the real bus-row numbers. A where-is-real-data inspection that works with no
        config CSV: idle/DC columns stay blank; an interval/skipping port shows as
        row-periodic 'TX' gaps you can read vertically."""
        self._scene.clear()
        self._engine_mode = False
        self._clashes = {}
        self._dp_display = {}
        self._sys_slots = []
        self._cell_at = {}
        tx = raster.get("tx")
        cols = int(raster.get("column_count", 0))
        labels = list(raster.get("row_labels") or [])
        cds_col = int(raster.get("cds_col", 0))
        nrows = len(labels)
        if tx is None or cols <= 0 or nrows <= 0:
            self._finalize_scene()
            return
        self._draw_tx_headers(nrows, cols, labels)
        for r in range(nrows):
            for c in range(cols):
                x, y = self._cx(c), self._cy(r)
                if c == cds_col:
                    # Match the config grid's CDS cell (drawn with _BG, not _CDS_FILL) so
                    # the CDS column doesn't change shade when toggling into the TX map.
                    fill, label, ink = _BG, "CDS", _LABEL
                elif bool(tx[r][c]):
                    fill, label, ink = _TX_FILL, "TX", _INK
                else:
                    fill, label, ink = _BG, "", _LABEL
                self._scene.addRect(QRectF(x, y, _CW, _RH), QPen(_LINE, 1), QBrush(fill))
                if label:
                    self._text(label, x, y, _CW, _RH, ink)
        self._draw_frame(nrows, cols, key_column=False)   # no Source/Sink key for the raster
        self._finalize_scene()

    def _draw_tx_headers(self, rows: int, cols: int, labels: List[int]) -> None:
        """Like _draw_headers but the row gutter shows real bus-row numbers (`labels`)
        rather than 0..rows-1."""
        gy0 = self._cy(0)
        for c in range(cols):
            self._text(str(c), self._cx(c), gy0 - _COLHDR_H, _CW, _COLHDR_H,
                       _LABEL, font=self._f_num)
        for r in range(rows):
            self._text(f"{int(labels[r]):,}", 0, self._cy(r), _ROWHDR_W - 4, _RH, _LABEL,
                       align_right=True, font=self._f_num)

    def _clash_x(self, r: int, c: int, kind: str) -> None:
        """Draw a clash X contained to the cell's relevant region: full height over a
        system slot (CDS/S0/S1/system tail), the sink half for a read overlap, else
        the source half (matching the Visualizer's clash geometry)."""
        x, y0 = self._cx(c), self._cy(r)
        at = self._cell_at.get((r, c), [])
        full = any(cc.get("is_cds") or cc["slot"] in (_S0, _S1)
                   or (cc["slot"] == _TAIL and cc["dp"] < 0) for cc in at)
        if full:
            ytop, h = y0, _RH
        elif kind == "read":
            ytop, h = y0 + _HALF, _HALF
        else:
            ytop, h = y0, _HALF
        pen = QPen(_CLASH_COLOR.get(kind, _CLASH_COLOR["bus"]), 2)
        m = 2
        self._scene.addLine(x + m, ytop + m, x + _CW - m, ytop + h - m, pen)
        self._scene.addLine(x + _CW - m, ytop + m, x + m, ytop + h - m, pen)

    # ---- geometry ----
    def _finalize_scene(self) -> None:
        """Set the scene rect to the items' bounds plus a small margin, so the top
        column-number row (and left row gutter) aren't clipped by the view edge."""
        r = self._scene.itemsBoundingRect()
        r.adjust(-4, -8, 4, 4)
        self._scene.setSceneRect(r)

    def _cx(self, c: int) -> float:
        return _ROWHDR_W + c * _CW

    def _cy(self, r: int) -> float:
        return self._key_band() + _COLHDR_H + r * _RH

    # ---- colour assignment ----
    def _assign_stream_colors(self, cells: List[dict], by_dp: bool = False) -> None:
        """by_dp=True (engine/Bus-Visualizer): colour by DP NUMBER (`palette[dp%12]`,
        key "DP{n}") exactly like the Visualizer's frame_renderer. by_dp=False
        (Analysis): colour each (device, dp) stream distinctly by appearance."""
        self._by_dp = by_dp
        if by_dp:
            dps = sorted({c["dp"] for c in cells if not c["is_cds"] and c["dp"] >= 0})
            self._stream_colors = {dp: QColor(_DP_PALETTE[dp % len(_DP_PALETTE)])
                                   for dp in dps}
        else:
            streams = sorted({(c["device"], c["dp"]) for c in cells
                              if not c["is_cds"] and c["dp"] >= 0 and c["device"] >= 0})
            self._stream_colors = {sd: QColor(_DP_PALETTE[i % len(_DP_PALETTE)])
                                   for i, sd in enumerate(streams)}
        chans: dict = {}
        for c in cells:
            if not c["is_cds"] and c["dp"] >= 0 and c["channel"] >= 0 and (by_dp or c["device"] >= 0):
                chans.setdefault(c["dp"] if by_dp else (c["device"], c["dp"]), set()).add(c["channel"])
        self._stream_channels = chans

    def _color(self, cell: dict) -> QColor:
        key = cell["dp"] if self._by_dp else (cell["device"], cell["dp"])
        return self._stream_colors.get(key, _CDS_FILL)

    def _dp_disp(self, cell: dict) -> Tuple[int, bool]:
        """(display_fields, handover) for a cell. Engine cells carry their own
        display_fields; otherwise fall back to the dp_display map / Visualizer
        defaults (Channel|Bit labels, handover enabled)."""
        if "display_fields" in cell:
            return int(cell["display_fields"]), True
        d = self._dp_display.get((cell["device"], cell["dp"]))
        if d is None:
            return 6, True
        return int(d.get("display_fields", 6)), bool(d.get("handover", True))

    # ---- per-row drawing (merge + dispatch) ----
    def _draw_row(self, r: int, row_cells: List[dict], cols: int,
                  cds_labels: dict) -> None:
        # CDS full-height cells — merge consecutive CDS columns into one "CDS"
        # (or "CDS x{n}") cell with bit dividers, like the Visualizer.
        cds_cols = sorted(c["col"] for c in row_cells if c["is_cds"])
        i = 0
        while i < len(cds_cols):
            n = 1
            while i + n < len(cds_cols) and cds_cols[i + n] == cds_cols[i] + n:
                n += 1
            label = cds_labels.get(r) or ("CDS" if n == 1 else f"CDS x{n}")
            cell = {"row": r, "col": cds_cols[i], "is_cds": True, "slot": 0,
                    "dp": -1, "channel": -1, "is_source": True}
            self._full_cell(r, cds_cols[i], n - 1, _BG, label, _LABEL, cell)
            i += n

        # Data / guard / tail, split into source (top) and sink (bottom) lanes.
        for is_src in (True, False):
            lane = sorted((c for c in row_cells
                           if not c["is_cds"] and bool(c["is_source"]) == is_src),
                          key=lambda c: c["col"])
            i = 0
            while i < len(lane):
                cell = lane[i]
                df, _ = self._dp_disp(cell)
                n = 1
                while (i + n < len(lane)
                       and lane[i + n]["col"] == cell["col"] + n
                       and self._can_merge(cell, lane[i + n], df)):
                    n += 1
                self._draw_segment(r, cell, n, is_src, df)
                i += n

        # Interface CDS-block system slots (explicitly supplied by the caller). In
        # engine mode the model already supplies CDS/S0/S1/HANDOVER bits, so nothing
        # is synthesized here. Handover turnarounds are NOT drawn for a decoded
        # (analyzer) grid — they are a Visualizer-only annotation, emitted by the
        # engine (set_bus_model) as _HANDOVER cells when the config enables them.
        if not self._engine_mode:
            for col, kind in self._sys_slots:
                if not (0 <= col < cols):
                    continue
                if kind == "handover":
                    self._handover(r, col)
                elif kind == "tail":
                    self._sys_tail(r, col)
                elif kind == "guard":
                    self._sys_full(r, col, "G0")
                else:                                  # 'cds'
                    self._sys_full(r, col, "CDS")

    def _sys_full(self, r: int, col: int, label: str) -> None:
        cell = {"row": r, "col": col, "is_cds": label == "CDS", "slot": 0,
                "dp": -1, "channel": -1, "is_source": True}
        self._full_cell(r, col, 0, _BG, label, _LABEL, cell)

    def _sys_tail(self, r: int, col: int) -> None:
        """Full-height CDS/S1 tail: the decaying ringing 'rail' symbol."""
        cell = {"row": r, "col": col, "is_cds": False, "slot": _TAIL, "dp": -1,
                "channel": -1, "is_source": True}
        self._tail(r, col, 0, True, _BG, cell)

    @staticmethod
    def _can_merge(a: dict, b: dict, df: int) -> bool:
        if a["slot"] != b["slot"] or a["dp"] != b["dp"] or a["device"] != b["device"]:
            return False
        if a["slot"] in _SYS_SLOTS:                # guards/tail/S0/S1/handover: by type
            return True
        # data-ish: same channel + sample; if Bit is shown, also same bit (so only
        # wide-bit replay merges, matching the Visualizer's _process_data_bits).
        if a["channel"] != b["channel"] or a["sample"] != b["sample"]:
            return False
        if (df & 4) and a["bit"] != b["bit"]:
            return False
        return True

    def _draw_segment(self, r: int, cell: dict, n: int, is_src: bool, df: int) -> None:
        slot = cell["slot"]
        color = self._color(cell)
        if slot in _DATA_SLOTS:
            label = self._data_label(cell, df, n)
            self._half_cell(r, cell["col"], n - 1, is_src, color, label, cell,
                            scrambler=(slot == _DATA and cell.get("scrambler")))
        elif slot in (_G0, _G1):
            lbl = "G1" if slot == _G1 else "G0"
            if cell["dp"] >= 0:
                self._half_cell(r, cell["col"], n - 1, is_src, color, lbl, cell)
            else:
                self._full_cell(r, cell["col"], n - 1, _BG, lbl, _LABEL, cell)
        elif slot == _TAIL:
            self._tail(r, cell["col"], n - 1, is_src, color, cell)
        elif slot in (_S0, _S1):                   # full-height sync slots
            base = "S0" if slot == _S0 else "S1"
            self._full_cell(r, cell["col"], n - 1, _BG,
                            base + (f" x{n}" if n > 1 else ""), _LABEL, cell)
        elif slot == _HANDOVER:                    # engine handover bit → arrows
            self._handover(r, cell["col"])

    @staticmethod
    def _data_label(cell: dict, df: int, n: int) -> str:
        """Port of BitSlotData.to_label + the renderer's test-mode / merge suffix.
        TxPresent and DRQ have fixed labels; DATA honours the display fields, with
        the Bit field replaced by T1/T0 in test mode and an 'xN' suffix only when
        the Bit field is shown (wide-bit replay)."""
        slot = cell["slot"]
        if slot == _TXP:
            return f"TxP{cell['channel']}"
        if slot == _DRQ:
            return "DRQ"
        testind = {2: "T1", 3: "T0"}.get(cell.get("port_mode", 0))
        parts = []
        if df & 1:
            parts.append(f"S{cell['sample']}")
        if df & 2:
            parts.append(f"C{cell['channel']}")
        if df & 4:
            parts.append(testind if testind else f"B{cell['bit']}")
        label = "".join(parts)
        if n > 1 and (df & 4):
            label += f" x{n}"
        return label

    # ---- cell primitives ----
    def _bit_dividers(self, x: float, y: float, h: float, extra: int) -> None:
        """Short vertical lines marking the bit-column boundaries inside a merged
        (wide) cell — 25% height up from the bottom (Visualizer _draw_bit_rect)."""
        if extra <= 0:
            return
        pen = QPen(_LINE, 1)
        y1 = y + h * 0.75
        for k in range(1, extra + 1):
            lx = x + k * _CW
            self._scene.addLine(lx, y1, lx, y + h, pen)

    def _half_cell(self, r: int, c: int, extra: int, is_src: bool,
                   color: QColor, label: str, cell: dict,
                   scrambler: bool = False) -> None:
        x = self._cx(c)
        y = self._cy(r) + (0 if is_src else _HALF)
        w = (extra + 1) * _CW
        rect = self._scene.addRect(QRectF(x, y, w, _HALF), QPen(_LINE, 1), QBrush(color))
        self._diff(rect, cell)
        self._bit_dividers(x, y, _HALF, extra)
        if scrambler:
            # scrambler indicator: a black square flush in the cell's top-left
            # corner (touching the top and left borders) per scrambled bit column
            for k in range(extra + 1):
                sx = x + k * _CW
                self._scene.addRect(QRectF(sx, y, 6, 6),
                                    QPen(Qt.NoPen), QBrush(QColor(0, 0, 0)))
        if label:
            self._text(label, x, y, w, _HALF, _INK)

    def _full_cell(self, r: int, c: int, extra: int, fill: QColor,
                   label: str, ink: QColor, cell: dict) -> None:
        x = self._cx(c)
        y = self._cy(r)
        w = (extra + 1) * _CW
        rect = self._scene.addRect(QRectF(x, y, w, _RH), QPen(_LINE, 1), QBrush(fill))
        self._diff(rect, cell)
        self._bit_dividers(x, y, _RH, extra)
        if label:
            self._text(label, x, y, w, _RH, ink)

    def _tail(self, r: int, c: int, extra: int, is_src: bool, color: QColor,
              cell: dict) -> None:
        """A tail bit drawn as an exponentially-decaying ringing squiggle (port of
        the Visualizer's _draw_tail). Data-port tails are half-height; system tails
        (dp < 0) span the full row."""
        system = cell["dp"] < 0
        x = self._cx(c)
        cols = extra + 1
        if system:
            y0, h, fill = self._cy(r), _RH, _BG
        else:
            y0 = self._cy(r) + (0 if is_src else _HALF)
            h, fill = _HALF, color
        w = cols * _CW
        rect = self._scene.addRect(QRectF(x, y0, w, h), QPen(_LINE, 1), QBrush(fill))
        self._diff(rect, cell)
        # zigzag with exponentially decaying amplitude
        n_peaks = 8 if cols == 1 else 4 * cols
        amp0 = 0.40 * h
        pts = [(x + 0.10 * _CW, y0 + h / 2)]
        for k in range(n_peaks):
            prog = k / (n_peaks - 1) if n_peaks > 1 else 0
            px = x + (0.15 + prog * (cols - 0.30)) * _CW
            amp = amp0 * math.exp(-0.35 * k)
            py = y0 + h / 2 + (amp if k % 2 == 0 else -amp)
            pts.append((px, py))
        pen = QPen(_INK if not system else _LABEL, 1)
        for a, b in zip(pts, pts[1:]):
            self._scene.addLine(a[0], a[1], b[0], b[1], pen)

    def _diff(self, rect, cell: dict) -> None:
        diff = cell.get("diff")
        if diff and diff in _DIFF_PEN:
            rect.setPen(QPen(_DIFF_PEN[diff], 2))
        tip = f"row {cell['row']}, col {cell['col']}"
        if cell.get("is_cds"):
            tip += ": CDS"
        else:
            names = list(swi3score.SLOT_NAMES) + ["S0", "S1", "Handover"]
            slot = cell["slot"]
            name = names[slot] if 0 <= slot < len(names) else str(slot)
            tip += f": {name} ({'src' if cell['is_source'] else 'sink'})"
            if cell.get("device", -1) >= 0:
                tip += f", Dev{cell['device']}"
            if cell["dp"] >= 0:
                tip += f" DP{cell['dp']}"
            if cell["channel"] >= 0:
                tip += f", ch{cell['channel']}"
        rect.setToolTip(tip)

    # ---- handover turnarounds ----
    def _handover(self, r: int, c: int) -> None:
        """Two short arrows with filled heads (Visualizer _draw_handover): top →
        in the source half, bottom ← in the sink half. Emitted only for the
        Visualizer engine grid (set_bus_model _HANDOVER cells) and explicit system
        slots — never synthesized for a decoded (analyzer) grid."""
        x = self._cx(c)
        y0 = self._cy(r)
        pen = QPen(_LABEL, 1.4)
        brush = QBrush(_LABEL)
        x1, x2 = x + 0.20 * _CW, x + 0.80 * _CW
        hl, hw = 7.0, 3.2                       # arrowhead length / half-width
        for frac, head_right in ((0.34, True), (0.66, False)):
            y = y0 + frac * _RH
            if head_right:
                self._scene.addLine(x1, y, x2 - hl, y, pen)
                head = QPolygonF([QPointF(x2, y), QPointF(x2 - hl, y - hw),
                                  QPointF(x2 - hl, y + hw)])
            else:
                self._scene.addLine(x1 + hl, y, x2, y, pen)
                head = QPolygonF([QPointF(x1, y), QPointF(x1 + hl, y - hw),
                                  QPointF(x1 + hl, y + hw)])
            self._scene.addPolygon(head, QPen(Qt.NoPen), brush)

    # ---- headers, key, frame ----
    def _text(self, text: str, x: float, y: float, w: float, h: float,
              color: QColor, align_right: bool = False, font: QFont = None) -> None:
        t = self._scene.addText(text, font or self._f_label)
        t.setDefaultTextColor(color)
        t.document().setDocumentMargin(0)          # else ~4px margin skews centering
        br = t.boundingRect()
        tx = x + w - br.width() - 3 if align_right else x + (w - br.width()) / 2
        t.setPos(tx, y + (h - br.height()) / 2)

    def _draw_headers(self, rows: int, cols: int) -> None:
        gx0, gy0 = self._cx(0), self._cy(0)
        # column numbers (no background band — straight on the dark canvas)
        for c in range(cols):
            self._text(str(c), self._cx(c), gy0 - _COLHDR_H, _CW, _COLHDR_H,
                       _LABEL, font=self._f_num)
        # row numbers down the left gutter
        for r in range(rows):
            self._text(f"{r:,}", 0, self._cy(r), _ROWHDR_W - 4, _RH, _LABEL,
                       align_right=True, font=self._f_num)

    def _draw_top_key(self, cols: int) -> None:
        if not self._show_key or not self._stream_colors:
            return
        items = list(self._stream_colors.items())
        single_dev = self._by_dp or len({k[0] for k in self._stream_colors}) == 1
        sw_w, sw_h, gap = 46, 18, 8
        total = len(items) * (sw_w + gap) - gap
        gx0 = self._cx(0)
        start = gx0 + max(0, (cols * _CW - total) / 2)
        y = (_KEY_H - sw_h) / 2
        cur = start
        for key, color in items:
            self._scene.addRect(QRectF(cur, y, sw_w, sw_h), QPen(_LINE), QBrush(color))
            if self._by_dp:
                label = f"DP{key}"
            else:
                dev, dp = key
                name = ((self._dp_display.get((dev, dp)) or {}).get("name") or "").strip()
                label = name or (f"DP{dp}" if single_dev else f"D{dev}·DP{dp}")
            self._text(label, cur, y, sw_w, sw_h, QColor(0, 0, 0), font=self._f_key)
            cur += sw_w + gap

    def _draw_frame(self, rows: int, cols: int, key_column: bool = True) -> None:
        gx0, gy0 = self._cx(0), self._cy(0)
        grid_w = cols * _CW
        grid_h = rows * _RH
        right = gx0 + grid_w
        # The Source/Sink key column is per-row handover info for the config layout;
        # the TX raster is per-UI transitions, so it omits the key column entirely.
        outer_right = right + (_SSKEY_W if key_column else 0)
        pen = QPen(_LINE, 1)
        # Interior row separators (the top/bottom boundary is the rounded frame).
        for r in range(1, rows):
            y = gy0 + r * _RH
            self._scene.addLine(gx0, y, outer_right, y, pen)
        if key_column:
            # Interior vertical divider between the grid and the Source/Sink key column.
            self._scene.addLine(right, gy0, right, gy0 + grid_h, pen)
            # Source / Sink key column mid-lines + labels
            for r in range(rows):
                y = gy0 + r * _RH
                mid = y + _HALF
                self._scene.addLine(right, mid, outer_right, mid, QPen(_LINE, 1))
                self._text("Source", right, y, _SSKEY_W, _HALF, _LABEL, font=self._f_ss)
                self._text("Sink", right, mid, _SSKEY_W, _HALF, _LABEL, font=self._f_ss)
        # Rounded outer frame around the whole grid (+ key column), matching the
        # rounded design language. The corner cells draw square corners, so mask the
        # four corner nubs with the background, then stroke the rounded rectangle.
        # The masks extend OUTWARD past the frame edge by `pad` so they also swallow
        # the ~0.5px of the 1px cell-border pen that overhangs the rect — otherwise a
        # square nub of the corner cell pokes out beyond the rounded arc.
        rad = float(_s(6))
        pad = 2.0
        rect = QRectF(gx0, gy0, outer_right - gx0, grid_h)
        for left_x in (True, False):
            mx = rect.left() - pad if left_x else rect.right() - rad
            for top_y in (True, False):
                my = rect.top() - pad if top_y else rect.bottom() - rad
                m = self._scene.addRect(QRectF(mx, my, rad + pad, rad + pad),
                                        QPen(Qt.NoPen), QBrush(_BG))
                m.setZValue(50)
        path = QPainterPath()
        path.addRoundedRect(rect, rad, rad)
        self._scene.addPath(path, pen).setZValue(51)

    def export_image(self, path: str) -> str:
        """Render the current grid scene to PNG (.png) or SVG (.svg)."""
        from PySide6.QtCore import QSize
        from PySide6.QtGui import QImage, QPainter

        rect = self._scene.itemsBoundingRect()
        if path.lower().endswith(".svg"):
            from PySide6.QtSvg import QSvgGenerator
            gen = QSvgGenerator()
            gen.setFileName(path)
            gen.setSize(QSize(int(rect.width()) + 1, int(rect.height()) + 1))
            gen.setViewBox(QRectF(0, 0, rect.width(), rect.height()))
            painter = QPainter(gen)
            self._scene.render(painter, QRectF(), rect)
            painter.end()
        else:
            img = QImage(int(rect.width()) + 1, int(rect.height()) + 1,
                         QImage.Format_ARGB32)
            img.fill(_BG)
            painter = QPainter(img)
            self._scene.render(painter, QRectF(), rect)
            painter.end()
            img.save(path)
        return path
