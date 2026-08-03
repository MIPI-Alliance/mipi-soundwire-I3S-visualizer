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

import swi3score
from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QFont, QPainterPath, QPen, QPolygonF
from PySide6.QtWidgets import QFrame, QGraphicsScene, QGraphicsView

from ..model.grid_slots import GridSlot
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


def dp_stream_color(device: int, dp: int) -> QColor:
    """Stable data-port colour keyed by the (device, dp) VALUE — never by appearance
    order — so a port keeps the same colour when other ports are enabled/disabled across a
    capture. dpNumber is 0..31, so device*32+dp is a unique id; mod the palette. Shared by
    the bus grid and the audio pane so a stream is the same colour in both."""
    idx = (max(0, int(device)) * 32 + max(0, int(dp))) % len(_DP_PALETTE)
    return QColor(_DP_PALETTE[idx])


def bookmark_pair_color(label: str) -> QColor:
    """Colour for a bookmark PAIR, rotating the data-port palette by pair letter (A, B,
    C, …) so each pair gets a distinct, consistent colour across every pane (timeline /
    capture / audio). Falls back to the first palette entry for an unlabelled mark."""
    letter = (str(label) or "A")[:1].upper()
    idx = (ord(letter) - 65) if "A" <= letter <= "Z" else 0
    return QColor(_DP_PALETTE[idx % len(_DP_PALETTE)])

# Slot enum ints, from the shared GridSlot vocabulary (model/grid_slots.py). 1–6
# (Data..Drq) coincide with the C++ SwI3sSlot enum, so decode-path cells render
# directly; 7–9 (S0/S1/Handover) are Visualizer-engine-only (mapped in viz_engine).
_DATA, _TXP, _G0, _G1, _TAIL, _DRQ = (int(GridSlot.DATA), int(GridSlot.TX_PRESENT),
                                      int(GridSlot.GUARD_0), int(GridSlot.GUARD_1),
                                      int(GridSlot.TAIL), int(GridSlot.DRQ))
_S0, _S1, _HANDOVER = int(GridSlot.S0), int(GridSlot.S1), int(GridSlot.HANDOVER)
_DATA_SLOTS = (_DATA, _TXP, _DRQ)
_SYS_SLOTS = (_G0, _G1, _TAIL, _S0, _S1, _HANDOVER)   # merge by type, not channel

# Device sentinels (swviz SpecialDevices) used by the CDS split-cell band routing.
_DEV_MANAGER, _DEV_UNIVERSAL = -1, -2

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


def _cells_key(cells):
    """Fast content-cache key for a grid-cell list. Cells are flat dicts of hashable
    scalars with a FIXED key order (gridCellToDict / render_payload build them the same
    way every time), so a tuple of each cell's values is an equivalent cache key to the
    recursive sorted-items _freeze — but without sorting ~13 keys per cell on every
    settled cursor move (the guard used to cost more than the redraw it gates). A
    differing key order across calls would only cause a false cache MISS (a harmless
    rebuild), never a false hit, so this is safe."""
    return tuple(tuple(c.values()) for c in cells)


def _freeze(x):
    """Recursively turn dicts/lists into hashable tuples so a render's inputs can key a
    content cache. Raises TypeError on anything unhashable, so the caller disables caching
    (always rebuilds) rather than risk a false cache hit that would show a stale grid."""
    if isinstance(x, dict):
        return tuple(sorted((k, _freeze(v)) for k, v in x.items()))
    if isinstance(x, (list, tuple)):
        return tuple(_freeze(v) for v in x)
    hash(x)                      # unhashable -> TypeError -> caching disabled for this call
    return x


class GridView(QGraphicsView):
    #: A data-port swatch in the top colour key was clicked — (device, dp). The
    #: Analyzer opens that port's label-field chooser; nothing connects it elsewhere.
    dpKeyClicked = Signal(int, int)

    def __init__(self) -> None:
        super().__init__()
        self._scene = QGraphicsScene(self)
        self.setScene(self._scene)
        # No view frame: the default QGraphicsView (QFrame) border renders as a light
        # outline around the Bus Grid pane in dark mode. The canvas background is our own.
        self.setFrameShape(QFrame.NoFrame)
        self.setBackgroundBrush(_BG)
        self._stream_colors: dict = {}
        self._stream_channels: dict = {}
        self._by_dp = False        # engine mode colours by dp number (like the Visualizer)
        self._dp_display: Dict[Tuple[int, int], dict] = {}
        # [(QRectF in scene coords, (device, dp))] for the top colour-key swatches —
        # rebuilt by _draw_top_key, consumed by mousePressEvent. Empty in engine
        # (Visualizer) mode, where the key is coloured by dp number with no device.
        self._key_hits: List[Tuple[QRectF, Tuple[int, int]]] = []
        self._sys_slots: List[tuple] = []
        self._engine_mode = False
        self._clashes: dict = {}
        self._cell_at: dict = {}    # (row,col) -> cells; (re)built in set_bus_model, read by _clash_x
        # Peripheral (device>=0) roster "present on the bus" — devices that drive any
        # guard/tail/data/handover; the CDS split-cell peripheral guard label reads
        # "P Mix" when a present peripheral isn't guarding a given column (see
        # _draw_cds_system). Rebuilt in set_bus_model; empty otherwise.
        self._present_periph: set = set()
        self._show_key = True       # draw the top DP colour-key band (see set_show_key)
        self._tx_raster_key = None  # last-rendered set_tx_raster() content key (see there)
        self._content_key = None    # last-rendered set_cells/set_bus_model content key
        # Visualizer font scheme (src/ui/constants.py TEXT_SIZE=12): numbers 12,
        # in-cell labels 12-4=8, Source/Sink 12-3=9, colour key 12-2=10 — each
        # scaled up with the cells (see _SCALE).
        self._f_num = QFont(_APP_FONT); self._f_num.setPointSize(_fpt(12)); self._f_num.setBold(True)
        self._f_label = QFont(_APP_FONT); self._f_label.setPointSize(_fpt(10))
        self._f_ss = QFont(_APP_FONT); self._f_ss.setPointSize(_fpt(9))
        self._f_key = QFont(_APP_FONT); self._f_key.setPointSize(_fpt(10))

    # ---- public API ----
    def _dp_key_at(self, pos) -> Tuple[int, int] | None:
        """(device, dp) of the colour-key swatch under a viewport point, else None."""
        p = self.mapToScene(pos)
        for rect, key in self._key_hits:
            if rect.contains(p):
                return key
        return None

    def mousePressEvent(self, event) -> None:
        """Clicking a data-port swatch in the top key emits dpKeyClicked.

        The swatch is already the per-port affordance (it names the port and carries
        its colour), which is why the chooser hangs off it rather than a toolbar
        button — it mirrors the Visualizer, where clicking a port's row opens the same
        dialog. Left button only, and only over a swatch; everything else falls
        through so panning/scrolling is untouched.
        """
        if event.button() == Qt.LeftButton:
            key = self._dp_key_at(event.position().toPoint())
            if key is not None:
                self.dpKeyClicked.emit(key[0], key[1])
                event.accept()
                return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        """Pointing-hand cursor over a clickable swatch — without it the click target
        is invisible (the band looks like a static legend)."""
        over = self._dp_key_at(event.position().toPoint()) is not None
        self.viewport().setCursor(Qt.PointingHandCursor if over else Qt.ArrowCursor)
        super().mouseMoveEvent(event)

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
        # Colours are read fresh at draw time, not stored in the cache keys, so an
        # identical raster/grid after a theme switch must still be forced through a redraw.
        self._tx_raster_key = None
        self._content_key = None

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
        self._key_hits = []   # swatch rects died with the scene
        self._engine_mode = False
        self._clashes = {}
        self._tx_raster_key = None   # scene no longer holds the cached TX raster
        self._content_key = None     # nor the cached cell grid
        title = self._scene.addText(text, self._f_num)
        title.setDefaultTextColor(_LABEL)
        title.setPos(0, 0)
        if subtext:
            sub = self._scene.addText(subtext, self._f_label)
            sub.setDefaultTextColor(_LINE)
            sub.setPos(0, _RH)
        self._finalize_scene(reset_scroll=True)

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
        # Manual cursor navigation calls this repeatedly with unchanged content (the grid
        # only changes at register-write boundaries, not every sample); a full scene
        # rebuild is expensive, so skip it when the content key matches (mirrors
        # set_tx_raster). Colours aren't in the key — retheme() resets it. See _freeze.
        try:
            key = ("cells", int(column_count), self._show_key, _cells_key(cells),
                   _freeze(cds_labels), _freeze(dp_display), _freeze(system_slots))
        except TypeError:
            key = None
        if key is not None and key == self._content_key:
            return
        self._content_key = key
        self._scene.clear()
        self._key_hits = []   # swatch rects died with the scene
        self._tx_raster_key = None   # scene no longer holds the cached TX raster
        cds_labels = cds_labels or {}
        self._dp_display = dp_display or {}
        self._sys_slots = list(system_slots or [])
        self._engine_mode = False
        self._clashes = {}
        self._assign_stream_colors(cells)

        rows = max((c["row"] for c in cells), default=-1) + 1
        cols = max(column_count, (max((c["col"] for c in cells), default=-1) + 1))
        if rows <= 0 or cols <= 0:
            self._finalize_scene(reset_scroll=True)
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
        self._finalize_scene(reset_scroll=True)

    def set_bus_model(self, cells: List[dict], clashes: dict, column_count: int,
                      num_rows: int) -> None:
        """Render from the Visualizer engine's BusModel (via viz_engine.render_payload):
        the cells already include the CDS/S0/S1/handover system bits with device, so
        the per-source-port handover heuristic + CDS-block synthesis are suppressed
        and clash markers (X) are drawn from the model's clash lists."""
        # Content cache (see set_cells): skip the full rebuild when the model + clashes
        # are unchanged, as on a cursor move within one config region.
        try:
            key = ("model", int(column_count), int(num_rows), self._show_key,
                   _cells_key(cells), _freeze(clashes or {}))
        except TypeError:
            key = None
        if key is not None and key == self._content_key:
            return
        self._content_key = key
        self._scene.clear()
        self._key_hits = []   # swatch rects died with the scene
        self._tx_raster_key = None   # scene no longer holds the cached TX raster
        self._dp_display = {}
        self._sys_slots = []
        self._engine_mode = True
        self._clashes = clashes or {}
        self._assign_stream_colors(cells, by_dp=True)
        # Peripheral roster present on the bus: any device>=0 driving anything
        # (data/guard/tail/handover). Drives the CDS "P Mix" guard label.
        self._present_periph = {c["device"] for c in cells if c.get("device", -1) >= 0}
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
        row-periodic 'TX' gaps you can read vertically.

        Cheap content-cache guard: manual cursor navigation calls this repeatedly with
        an unchanged window (same rows/cols/tx bits), and each lit cell now costs 2
        QGraphicsPathItem (see _toggle_glyph) on top of the per-cell QGraphicsRectItem
        — a full scene rebuild on every call is hundreds of ms to seconds with a large
        Rows-To-Draw. Skip the rebuild entirely when the raster's content key matches
        the last-rendered one."""
        tx = raster.get("tx")
        cols = int(raster.get("column_count", 0))
        labels = list(raster.get("row_labels") or [])
        cds_col = int(raster.get("cds_col", 0))
        nrows = len(labels)
        key = (cols, cds_col, nrows,
               labels[0] if labels else None, labels[-1] if labels else None,
               None if tx is None else (tx.shape, tx.tobytes()))
        if key == getattr(self, "_tx_raster_key", None):
            return
        self._tx_raster_key = key
        self._content_key = None     # scene now holds a TX raster, not a cell grid
        self._scene.clear()
        self._key_hits = []   # swatch rects died with the scene
        self._engine_mode = False
        self._clashes = {}
        self._dp_display = {}
        self._sys_slots = []
        self._cell_at = {}
        if tx is None or cols <= 0 or nrows <= 0:
            self._finalize_scene(reset_scroll=True)
            return
        self._draw_tx_headers(nrows, cols, labels)
        for r in range(nrows):
            for c in range(cols):
                x, y = self._cx(c), self._cy(r)
                if c == cds_col:
                    # Match the config grid's CDS cell (drawn with _BG, not _CDS_FILL) so
                    # the CDS column doesn't change shade when toggling into the TX map.
                    fill, label, ink, is_tx = _BG, "CDS", _LABEL, False
                elif bool(tx[r][c]):
                    fill, label, ink, is_tx = _TX_FILL, "", _INK, True
                else:
                    fill, label, ink, is_tx = _BG, "", _LABEL, False
                self._scene.addRect(QRectF(x, y, _CW, _RH), QPen(_LINE, 1), QBrush(fill))
                if is_tx:
                    self._toggle_glyph(x, y, ink)     # crossover glyph == a line toggle
                elif label:
                    self._text(label, x, y, _CW, _RH, ink)
        self._draw_frame(nrows, cols, key_column=False)   # no Source/Sink key for the raster
        self._finalize_scene(reset_scroll=True)

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

    def _toggle_glyph(self, x: float, y: float, color: QColor) -> None:
        """A crossover ('toggle') glyph — two leads that swap top/bottom through a central
        X — drawn in a TX-map cell in place of the old 'TX' text: it reads as a line that
        toggled state during that UI."""
        m = _s(6)
        x0, x1 = x + m, x + _CW - m
        xa, xb = x + _CW * 0.40, x + _CW * 0.60      # where the leads meet the crossing
        yt, yb = y + _RH * 0.34, y + _RH * 0.66
        pen = QPen(color, _s(1.6))
        pen.setCapStyle(Qt.RoundCap)
        pen.setJoinStyle(Qt.RoundJoin)
        for lead_y, cross_y in ((yt, yb), (yb, yt)):  # top->bottom and bottom->top leads
            path = QPainterPath(QPointF(x0, lead_y))
            path.lineTo(xa, lead_y)
            path.lineTo(xb, cross_y)
            path.lineTo(x1, cross_y)
            self._scene.addPath(path, pen)

    def _clash_x(self, r: int, c: int, kind: str) -> None:
        """Draw a clash X contained to the cell's relevant region: full height over a
        full-height system slot (CDS/S0/S1), the sink half for a read overlap, else
        the source half (matching the Visualizer's clash geometry). A CDS system
        guard/tail cell (dp<0) is now SPLIT Manager-top/Peripheral-bottom (see
        _draw_cds_split), so its clash X is confined to whichever band(s) actually
        have an occupant there — full height only if both do."""
        x, y0 = self._cx(c), self._cy(r)
        at = self._cell_at.get((r, c), [])
        cds_sys_at = [cc for cc in at if cc["slot"] in (_G0, _G1, _TAIL) and cc["dp"] < 0]
        if cds_sys_at:
            has_mgr = any(cc["device"] < 0 for cc in cds_sys_at)
            has_periph = any(cc["device"] >= 0 for cc in cds_sys_at)
            if has_mgr and has_periph:
                ytop, h = y0, _RH
            elif has_mgr:
                ytop, h = y0, _HALF
            else:
                ytop, h = y0 + _HALF, _HALF
        else:
            full = any(cc.get("is_cds") or cc["slot"] in (_S0, _S1) for cc in at)
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
    def _finalize_scene(self, reset_scroll: bool = False) -> None:
        """Set the scene rect to the items' bounds plus a small margin, so the top
        column-number row (and left row gutter) aren't clipped by the view edge.

        `reset_scroll` re-anchors the viewport at the scene's top-left. Content is
        always drawn from the scene origin, but Qt clamps a shrinking scene's
        scrollbars to the NEW maximum (not to zero) — so navigating from a wide/tall
        scrolled grid to a smaller one (e.g. a narrower config region, or the bring-up
        'No PHY Selected' message drawn at x=0) leaves the fresh content scrolled off
        the left/top edge, invisible until the next event nudges the bars. The
        cursor-driven Analysis renders pass True so each new grid lands in view; the
        authoring path (set_bus_model) keeps the user's scroll."""
        r = self._scene.itemsBoundingRect()
        r.adjust(-4, -8, 4, 4)
        self._scene.setSceneRect(r)
        if reset_scroll:
            self.horizontalScrollBar().setValue(int(r.left()))
            self.verticalScrollBar().setValue(int(r.top()))

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
            # Key each stream's colour by its (device, dp) VALUE, not appearance order, so
            # a port keeps its colour when other ports enable/disable (see dp_stream_color).
            self._stream_colors = {sd: dp_stream_color(sd[0], sd[1]) for sd in streams}
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

        # CDS system guard/tail/handover. The CDS is drawn FULL-HEIGHT (plain "G0"/
        # "G1", full-row handover) via the normal lane loop below whenever the Manager
        # and peripherals do NOT diverge — either only the Manager drives the CDS
        # (the spec-figure goldens) or every driving source shares identical CDS
        # settings, so they collapse to one symbol. It is SPLIT into Manager (top) /
        # Peripheral (bottom) half-glyphs only when sources genuinely diverge (a
        # peripheral drives a guard/tail/handover at a different column or polarity
        # than the Manager). The S1 handover (before the CDS bit) is never part of
        # this and always stays full-height. Data-port guards/tails/handovers
        # (dp >= 0) are always left to the lane loop.
        guard_tail = [c for c in row_cells
                      if not c["is_cds"] and c["slot"] in (_G0, _G1, _TAIL) and c["dp"] < 0]
        cds_bit_cols = [c["col"] for c in row_cells if c["is_cds"]]
        cds_start = min(cds_bit_cols) if cds_bit_cols else 0
        cds_ho = [c for c in row_cells
                  if not c["is_cds"] and c["slot"] == _HANDOVER
                  and c["dp"] < 0 and c["col"] >= cds_start]
        cds_sys_all = guard_tail + cds_ho
        mgr_act = {(c["slot"], c["col"]) for c in cds_sys_all if self._cds_is_mgr(c["device"])}
        per_act = {(c["slot"], c["col"]) for c in cds_sys_all if not self._cds_is_mgr(c["device"])}
        # Split when sources diverge. The engine flags config-level divergence
        # (`cds_split`) — needed when only the Manager drives a guard while devices are
        # off/bare, which emits a single Manager cell that otherwise looks uniform.
        # Fall back to the cell-based check for defensiveness.
        cds_split_flag = any(c.get("cds_split") for c in cds_sys_all)
        cds_diverges = bool(cds_sys_all) and (cds_split_flag
                                              or (bool(per_act) and per_act != mgr_act))
        cds_sys_ids = {id(c) for c in cds_sys_all} if cds_diverges else set()
        if cds_diverges:
            self._draw_cds_system(r, cds_sys_all)

        # Data / guard / tail, split into source (top) and sink (bottom) lanes.
        # (CDS system guard/tail cells are handled above, so exclude them here.)
        for is_src in (True, False):
            lane = sorted((c for c in row_cells
                           if not c["is_cds"] and id(c) not in cds_sys_ids
                           and bool(c["is_source"]) == is_src),
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

    def _draw_cds_system(self, r: int, cells: List[dict]) -> None:
        """Draw the CDS system guard/tail cells for a row, SPLIT into a Manager band
        (top half) and a Peripheral band (bottom half) — the CDS is time-multiplexed,
        so the Manager and each device 0-11 can drive their own guard polarity / tail
        width in the same columns; the engine emits one cell per source (`device`=-1
        Manager, >=0 peripheral). A band with no occupant is left blank.

        Guards are single-column and labelled from the band's occupant polarity, with
        an ``M``/``P`` prefix so the source is unambiguous: the Manager band shows
        "M G0"/"M G1" (there's only ever one Manager); the peripheral band aggregates
        every device>=0 occupant into "P G0"/"P G1" if they agree, "P Gx" if they
        disagree. Tails draw the decaying ringing 'rail' glyph, half-height per band,
        and CONSECUTIVE tail columns are merged into one longer glyph rather than a
        squiggle per column. All ink is the theme text colour (_LABEL) since these
        sit on the _BG background (see _half_cell/_tail)."""
        for is_mgr, is_src in ((True, True), (False, False)):
            band = [c for c in cells if self._cds_is_mgr(c["device"]) == is_mgr]
            if not band:
                continue
            prefix = "M " if is_mgr else "P "
            # Guards: one cell per column (a guard is always a single column). The
            # Manager band has a single source, so its label is just its polarity.
            # The Peripheral band aggregates every present peripheral device: "Gx" if
            # the guarding ones disagree on polarity, and "Mix" if some present
            # peripheral (a device driving a data port / tail elsewhere) isn't
            # guarding this column at all (it hands the bus over) — see set_bus_model.
            guard_cols: Dict[int, dict] = {}      # col -> {device: guard-slot}
            for c in band:
                if c["slot"] in (_G0, _G1):
                    guard_cols.setdefault(c["col"], {})[c["device"]] = c["slot"]
            for col, dev_pol in sorted(guard_cols.items()):
                label = prefix + self._cds_guard_core(dev_pol, is_mgr)
                rep = next(c for c in band
                           if c["col"] == col and c["slot"] in (_G0, _G1))
                self._half_cell(r, col, 0, is_src, _BG, label, rep, ink=_LABEL)
            # Tails: merge runs of consecutive columns into one wider glyph.
            tail_cols = sorted({c["col"] for c in band if c["slot"] == _TAIL})
            i = 0
            while i < len(tail_cols):
                n = 1
                while i + n < len(tail_cols) and tail_cols[i + n] == tail_cols[i] + n:
                    n += 1
                rep = next(c for c in band
                           if c["col"] == tail_cols[i] and c["slot"] == _TAIL)
                self._tail(r, tail_cols[i], n - 1, is_src, _BG, rep,
                           force_half=True, label=("M" if is_mgr else "P"))
                i += n
            # Handovers: a compact M/P-labelled half-band turnaround arrow, only in
            # columns this band doesn't already fill with a guard/tail (those take
            # precedence). Covers the per-source source-side handovers, the region-end
            # turnaround, and the universal (-2) CDS-bit->next turnaround.
            occupied = {c["col"] for c in band if c["slot"] in (_G0, _G1, _TAIL)}
            for col in sorted({c["col"] for c in band
                               if c["slot"] == _HANDOVER and c["col"] not in occupied}):
                self._handover_half(r, col, is_src, "M" if is_mgr else "P")

    @staticmethod
    def _cds_is_mgr(device: int) -> bool:
        """Which band a CDS split-cell occupant belongs to. The top (Manager) band holds
        the Manager (-1) AND the universal (-2) region-end handover — the uniform-config
        turnaround, which should render full-height, not as a lone peripheral arrow. The
        bottom (peripheral) band holds real devices (>=0), including the per-device
        CDS-bit handovers (0-11). The CDS bit itself is is_cds (drawn full-width) and
        never reaches the split."""
        return device == _DEV_MANAGER or device == _DEV_UNIVERSAL

    def _cds_guard_core(self, dev_pol: dict, is_mgr: bool) -> str:
        """Guard-label core ("G0"/"G1"/"Gx"/"Mix") for one CDS split band at a column.

        `dev_pol` maps each device driving a guard here to its slot (_G0/_G1). The
        Manager band is a single source, so it's just that polarity. The Peripheral
        band is aggregated over the devices *present on the bus* (self._present_periph
        — any device>=0 driving data/guard/tail): if a present peripheral isn't
        guarding this column it's implicitly handing the bus over, so the mixture of
        guard + handover reads "Mix"; otherwise "Gx" when the guarding polarities
        disagree, else the shared polarity."""
        polarities = set(dev_pol.values())
        if not is_mgr:
            non_guarding = self._present_periph - set(dev_pol)
            if non_guarding:
                return "Mix"
        if len(polarities) > 1:
            return "Gx"
        return "G1" if _G1 in polarities else "G0"

    def _handover_half(self, r: int, col: int, is_src: bool, label: str = "") -> None:
        """A compact bus-turnaround glyph confined to one band's half of a CDS split
        cell — the same two opposing arrows (→ over ←) as the full-row _handover, but
        both stacked inside the half-height band so a source's handover fits alongside
        another source still driving the other band. `is_src` picks the top (Manager)
        or bottom (Peripheral) half; `label` ("M"/"P") is drawn left of the arrows to
        name the source (Manager vs peripheral)."""
        x = self._cx(col)
        y = self._cy(r) + (0 if is_src else _HALF)
        self._scene.addRect(QRectF(x, y, _CW, _HALF), QPen(_LINE, 1), QBrush(_BG))
        pen = QPen(_LABEL, 1.3)
        brush = QBrush(_LABEL)
        hl, hw = 4.5, 2.1                       # arrowhead length / half-width
        # Label on the left, arrows packed into the right of the cell when labelled.
        ax0 = 0.44 if label else 0.24
        if label:
            self._text(label, x + 1, y, _CW * 0.40, _HALF, _LABEL)
        x1, x2 = x + ax0 * _CW, x + 0.94 * _CW
        for frac, head_right in ((0.32, True), (0.68, False)):
            yy = y + frac * _HALF
            if head_right:
                self._scene.addLine(x1, yy, x2 - hl, yy, pen)
                head = QPolygonF([QPointF(x2, yy), QPointF(x2 - hl, yy - hw),
                                  QPointF(x2 - hl, yy + hw)])
            else:
                self._scene.addLine(x1 + hl, yy, x2, yy, pen)
                head = QPolygonF([QPointF(x1, yy), QPointF(x1 + hl, yy - hw),
                                  QPointF(x1 + hl, yy + hw)])
            self._scene.addPolygon(head, QPen(Qt.NoPen), brush)

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
                   scrambler: bool = False, ink: QColor = None) -> None:
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
            self._text(label, x, y, w, _HALF, ink if ink is not None else _INK)

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
              cell: dict, force_half: bool = False, label: str = "") -> None:
        """A tail bit drawn as an exponentially-decaying ringing squiggle (port of
        the Visualizer's _draw_tail). Data-port tails are half-height; system tails
        (dp < 0) span the full row — unless `force_half` (the CDS split-cell Manager/
        Peripheral bands, which each get a half-height rail like a data lane). `label`
        ("M"/"P") is drawn at the left of the split-cell rail to name the source, with
        the squiggle shifted right to clear it."""
        system = cell["dp"] < 0 and not force_half
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
        if label:
            self._text(label, x + 1, y0, _CW * 0.40, h, _LABEL)
        # zigzag with exponentially decaying amplitude, starting after any label
        s0 = 0.46 if label else 0.15                 # squiggle start (fraction of _CW)
        span = (cols - 0.15) - s0
        n_peaks = 8 if cols == 1 else 4 * cols
        amp0 = 0.40 * h
        pts = [(x + (s0 - 0.05) * _CW, y0 + h / 2)]
        for k in range(n_peaks):
            prog = k / (n_peaks - 1) if n_peaks > 1 else 0
            px = x + (s0 + prog * span) * _CW
            amp = amp0 * math.exp(-0.35 * k)
            py = y0 + h / 2 + (amp if k % 2 == 0 else -amp)
            pts.append((px, py))
        # System cells (dp < 0) sit on the _BG background, so their squiggle uses
        # the theme text ink (_LABEL); data-port tails sit on a light DP colour and
        # use _INK. Keying off dp (not `system`) keeps the CDS split-cell half-height
        # rails (force_half) legible in dark mode.
        pen = QPen(_LABEL if cell["dp"] < 0 else _INK, 1)
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
        # QGraphicsSimpleTextItem (plain text) instead of addText's QGraphicsTextItem
        # (full rich-text/QTextDocument engine): ~2x cheaper per label and these are all
        # plain strings. It has no document margin, matching the setDocumentMargin(0) the
        # rich-text path used, so placement/boundingRect are identical.
        t = self._scene.addSimpleText(text, font or self._f_label)
        t.setBrush(color)
        br = t.boundingRect()
        tx = x + w - br.width() - 3 if align_right else x + (w - br.width()) / 2
        t.setPos(tx, y + (h - br.height()) / 2)

    def _draw_headers(self, rows: int, cols: int) -> None:
        gy0 = self._cy(0)
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
        self._key_hits = []          # [(QRectF, (device, dp))] — see mousePressEvent
        for key, color in items:
            rect = QRectF(cur, y, sw_w, sw_h)
            self._scene.addRect(rect, QPen(_LINE), QBrush(color))
            if self._by_dp:
                label = f"DP{key}"
            else:
                dev, dp = key
                name = ((self._dp_display.get((dev, dp)) or {}).get("name") or "").strip()
                label = name or (f"DP{dp}" if single_dev else f"D{dev}·DP{dp}")
                # Only the decoded (analyzer) grid keys by (device, dp) and can map a
                # swatch back to a real port; engine mode colours by dp number alone.
                self._key_hits.append((rect, (int(dev), int(dp))))
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
        # A little breathing room on all sides (~half a column width), so the grid
        # doesn't butt against the image edge.
        margin = _CW * 0.5
        rect = rect.adjusted(-margin, -margin, margin, margin)
        if path.lower().endswith(".svg"):
            from PySide6.QtSvg import QSvgGenerator
            gen = QSvgGenerator()
            gen.setFileName(path)
            gen.setSize(QSize(int(rect.width()) + 1, int(rect.height()) + 1))
            gen.setViewBox(QRectF(0, 0, rect.width(), rect.height()))
            painter = QPainter(gen)
            # Fill the theme background first — QSvgGenerator doesn't paint the scene's
            # background brush, so without this the SVG has a transparent (viewer-white)
            # background while the content is themed for the current mode, giving a
            # light/dark mix. Filling _BG makes the export follow the active UI mode.
            painter.fillRect(QRectF(0, 0, rect.width(), rect.height()), _BG)
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
