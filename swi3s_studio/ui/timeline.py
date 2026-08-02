"""Timeline overview ribbon: the whole capture at a glance.

A thin custom-painted strip showing every command as a tick (coloured by kind:
CRC error / commit-with-SSP / commit / ping / other), plus the time cursor.
Clicking seeks — it emits the sample under the pointer so the rest of the app
(command table, register map, grid) can follow. This is the primary way to move
across a long capture and to get back to a region after drilling into a command.
"""
from __future__ import annotations

import bisect
from typing import List

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPen, QPolygonF
from PySide6.QtWidgets import QToolTip, QWidget

from .grid_view import bookmark_pair_color as _bookmark_pair_color
from .row_origin import RowOriginMixin
from .theme import VizTheme


def _diamond(cx: float, cy: float, r: float) -> QPolygonF:
    return QPolygonF([QPointF(cx, cy - r), QPointF(cx + r, cy),
                      QPointF(cx, cy + r), QPointF(cx - r, cy)])

_MARGIN = 8
_AXIS_H = 18                 # bottom strip reserved for the time ruler
_H = 92                      # taller so the config-band labels + a time axis both fit
_CFG_BAND_H = 18             # top strip for the config-region bands (holds the "Ncol" label),
                             # kept ABOVE the command ticks so the ticks read clearly

_C_ERROR = QColor(VizTheme.SEM_ERROR)
_C_SSP = QColor(VizTheme.SEM_SSP)
_C_COMMIT = QColor(VizTheme.SEM_COMMIT)
_C_SYNC = QColor(VizTheme.SEM_SYNC)     # Commit Synchronization Point (where a commit TAKES EFFECT)
_C_PING = QColor(VizTheme.SEM_PING)
_C_OTHER = QColor(VizTheme.SEM_OTHER)   # Write / other command (green)
_C_READ = QColor(VizTheme.SEM_READ)     # Read command (violet) — distinct from Write
_C_CURSOR = QColor(VizTheme.CURSOR)
_C_AXIS = QColor(VizTheme.AXIS)
_C_BOOKMARK = QColor(VizTheme.SEM_BOOKMARK)
# Link bring-up (§5.1.2 Bus Reset → PHY-select). A distinct cyan, deliberately
# unlike the gold Commit+SSP tick so the two aren't confused.
_C_BRINGUP = QColor(VizTheme.SEM_BRINGUP)

# Translucent band colours for distinct bus-config geometries (by column count),
# painted behind the ticks so a reconfiguration (e.g. cold-start 2col -> 8col) is
# visible at a glance. Cycled by distinct column count in ascending order.
_CONFIG_BANDS = [
    QColor(70, 110, 150, 70), QColor(70, 150, 110, 70), QColor(150, 120, 70, 70),
    QColor(130, 80, 140, 70), QColor(150, 90, 90, 70), QColor(80, 140, 150, 70),
]

# Link-control sub-phase colours — one distinct data-port-palette colour per named
# section (Idle / Bus Reset / Cold Start / Warm Start / LC_Request / PhyStart). Read
# live from VizTheme.TRACE_PALETTE so they track a theme switch.
_LC_SECTION_PALETTE_IDX = {
    "Idle": 5, "Bus Reset": 2, "Cold Start": 0, "Warm Start": 3,
    "LC_Request": 4, "PhyStart": 1,
}


def _lc_section_color(name: str, alpha: int = 255) -> QColor:
    pal = VizTheme.TRACE_PALETTE
    idx = _LC_SECTION_PALETTE_IDX.get(name, 0) % len(pal)
    c = QColor(pal[idx])
    c.setAlpha(alpha)
    return c

# (label, VizTheme colour attribute, meaning) — the ONE definition of the timeline
# legend, used to (re)build MARK_LEGEND both at import and after a theme switch, so the
# painter and the Help ▸ Timeline Legend dialog never drift from what's drawn.
_MARK_LEGEND_SPEC = [
    ("CRC error",         "SEM_ERROR",    "Manager-packet CRC failed (tall red tick)"),
    ("Commit + SSP",      "SEM_SSP",      "Confirmed commit carrying a Stream Sync Point (tall gold tick)"),
    ("Commit",            "SEM_COMMIT",   "Commit phase, not yet SSP-confirmed (short blue tick)"),
    ("Commit Sync Point", "SEM_SYNC",     "Where a confirmed commit TAKES EFFECT — the SSP, Row_Delay rows "
                                          "after the command (dotted orange line)"),
    ("SSP Announce",      "SEM_SSP",      "SSPA — announces a Stream Sync Point without committing: "
                                          "re-asserts data-port synchronization (tall gold tick, "
                                          "drawn like Commit + SSP — hover to tell them apart)"),
    ("Ping",              "SEM_PING",     "Ping phase (short gray tick)"),
    ("Read",              "SEM_READ",     "Read phase (short violet tick)"),
    ("Write / other",     "SEM_OTHER",    "Write or other command phase (short green tick)"),
    ("Cursor",            "CURSOR",       "Time cursor — shared across every pane (white line)"),
    ("Bookmark",          "SEM_BOOKMARK", "Bookmarked sample (magenta diamond at the baseline)"),
    ("Link bring-up",     "SEM_BRINGUP",  "§5.1.2 Bus Reset → PHY-select before audio mode "
                                          "(cyan band + flag; the PHY-select edge is the tick)"),
    ("Forced geometry",   "SEM_ERROR",    "Config region whose column count you pinned — its "
                                          "band reads '16col (forced)' and is outlined. The "
                                          "decode uses the pinned width for that region only"),
]


def _build_mark_legend():
    """(label, QColor, meaning) rows read from the current palette."""
    return [(label, QColor(getattr(VizTheme, attr)), meaning)
            for label, attr, meaning in _MARK_LEGEND_SPEC]


MARK_LEGEND = _build_mark_legend()


def _kind_label(cmd: dict) -> str:
    if cmd.get("has_manager_packet") and not cmd.get("crc_valid"):
        return "CRC error"
    if cmd.get("is_commit"):
        return "Commit + SSP" if cmd.get("commit_confirmed") else "Commit"
    # An SSPA carries a Stream Sync Point without committing anything — it re-asserts
    # data-port synchronization. Label it as an SSP rather than lumping it in with
    # "Other command": it re-anchors every port's transport phase, which is exactly the
    # kind of event you want to find on the timeline.
    if cmd.get("has_sync_point"):
        return "SSP Announce"
    name = cmd.get("command", "") or ""
    if name == "Ping":
        return "Ping"
    if "Read" in name:
        return "Read"
    if "Write" in name:
        return "Write"
    return "Other command"


def _kind_color(cmd: dict) -> QColor:
    return {"CRC error": _C_ERROR, "Commit + SSP": _C_SSP, "Commit": _C_COMMIT,
            "SSP Announce": _C_SSP,
            "Ping": _C_PING, "Read": _C_READ, "Write": _C_OTHER,
            "Other command": _C_OTHER}.get(_kind_label(cmd), _C_OTHER)


# Painting precedence for colliding ticks (a long capture is ~98% Pings, so many
# commands land on one pixel). Higher rank is painted LATER and so wins the pixel:
# Ping < read/write/other < commit < SSP/CRC-error. This keeps a Write/Read/error
# from being overpainted by a colliding Ping that merely came later in time.
_TICK_RANK = {
    "Ping": 0, "Read": 1, "Write": 1, "Other command": 1,
    "Commit": 2, "Commit + SSP": 3, "SSP Announce": 3, "CRC error": 3,
}


def tick_rank(cmd: dict) -> int:
    """Paint-order rank for a command's timeline tick (see _TICK_RANK)."""
    return _TICK_RANK.get(_kind_label(cmd), 1)


class TimelineRibbon(RowOriginMixin, QWidget):
    seeked = Signal('qlonglong')     # absolute sample under the click (64-bit:
    #                                  large captures exceed a 32-bit int)
    #: a bookmark was dragged: (label, new_sample, final). final=False during the drag
    #: (live re-measure), True on mouse release (snap to the nearest edge there).
    bookmarkMoved = Signal(str, 'qlonglong', bool)

    def __init__(self) -> None:
        super().__init__()
        self.setMinimumHeight(_H)
        self.setMaximumHeight(_H)
        self._commands: List[dict] = []
        self._by_start: List[dict] = []      # commands sorted by start sample (nearest-tick)
        self._draw_order: List[dict] = []    # commands sorted by paint rank (computed once)
        self._starts: List[int] = []         # sorted command start samples (arrow nav)
        self._nav_starts = None              # filtered subset for arrow nav (None = all)
        self._draw_set = None                # visible raw start_samples for the paint filter
        self._decim_key = None               # cache key: (lo, hi, width, draw_order id, filter id)
        self._decim_ticks: List[tuple] = []  # cached [(x, cmd), …] — one per occupied pixel column
        self._commit_decim_key = None        # cache key for the decimated commit-point x's
        self._commit_decim: List[int] = []   # cached in-view commit-point pixel columns
        self._commit_starts: List[int] = []  # SSCR/DSCR starts (⌘+arrow jump)
        self._commit_points: List[int] = []  # SSP samples where confirmed commits TAKE EFFECT
        self._total = 1
        self._cursor = 0
        self._bookmarks: List[tuple] = []    # (sample, label) pairs, e.g. (12345, "A1")
        self._drag_bm = None                 # label of the bookmark being dragged, or None
        self._segments: List[dict] = []      # bus-config segments (start_sample, column_count, …)
        self._seg_band: dict = {}            # column_count -> band QColor
        self._bringup: dict = {}             # §5.1.2 link bring-up region (or empty)
        self._link_sections: List[dict] = [] # named LC sub-phases within the bring-up
        self._rate = 0.0                 # sample rate, for the hover tooltip's time
        self._row_label = None           # fn(sample) -> 0-based display row, for the hover
        self._view_lo = 0.0              # visible sample window (zoom/pan); full = [0,_total]
        self._view_hi = 1.0
        self._pan_x = None               # middle-drag pan anchor
        # Coalesce left-drag seeks: a raw mouse-move fires per pixel and each seek drives
        # the full cursor cascade (register + grid rebuild). Emit at most once per ~30 ms
        # with the latest sample (mirrors raw_view's coalescing); the final position is
        # flushed on mouse-release so it never lags behind the drag.
        self._pending_seek = None
        self._seek_timer = QTimer(self)
        self._seek_timer.setSingleShot(True)
        self._seek_timer.setInterval(30)
        self._seek_timer.timeout.connect(self._emit_pending_seek)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.StrongFocus)          # accept arrow keys
        # No widget tooltip: the "Click to seek…" help popped up over the ribbon and hid
        # the marks/cursor. The interactions are listed in Help ▸ Keyboard Shortcuts.

    def set_sample_rate(self, rate_hz: float) -> None:
        self._rate = float(rate_hz or 0.0)

    def set_row_label(self, fn) -> None:
        """Bind fn(sample) -> 0-based display row, used to add the bus row to the
        commit-point hover tooltip. None disables the row readout."""
        self._row_label = fn

    def retheme(self) -> None:
        """Re-read the palette after a theme switch: recompute every tick / cursor /
        axis / bring-up colour and rebuild MARK_LEGEND (so the Help legend matches),
        then repaint. paintEvent reads the module colours each frame, so update()
        alone is enough — the stored commands/segments are repainted in place."""
        global _C_ERROR, _C_SSP, _C_COMMIT, _C_PING, _C_OTHER, _C_READ
        global _C_CURSOR, _C_AXIS, _C_BOOKMARK, _C_BRINGUP, _C_SYNC, MARK_LEGEND
        _C_ERROR = QColor(VizTheme.SEM_ERROR)
        _C_SSP = QColor(VizTheme.SEM_SSP)
        _C_COMMIT = QColor(VizTheme.SEM_COMMIT)
        _C_SYNC = QColor(VizTheme.SEM_SYNC)
        _C_PING = QColor(VizTheme.SEM_PING)
        _C_OTHER = QColor(VizTheme.SEM_OTHER)
        _C_READ = QColor(VizTheme.SEM_READ)
        _C_CURSOR = QColor(VizTheme.CURSOR)
        _C_AXIS = QColor(VizTheme.AXIS)
        _C_BOOKMARK = QColor(VizTheme.SEM_BOOKMARK)
        _C_BRINGUP = QColor(VizTheme.SEM_BRINGUP)
        MARK_LEGEND = _build_mark_legend()
        self.update()

    def set_segments(self, segments, total_samples: int = 0) -> None:
        """Bus-config segments (from decoder.segments(): start_sample, column_count,
        row_base). Painted as translucent bands coloured by column count, so each
        distinct bus geometry shows in its own colour. A segment carrying a truthy
        "forced" key had its width pinned by the user (Session.force_column_count_at)
        and is labelled + outlined as overridden."""
        self._segments = sorted(segments, key=lambda s: s.get("start_sample", 0))
        if total_samples:
            self._total = max(1, int(total_samples))
        counts = sorted({int(s.get("column_count", 0)) for s in self._segments})
        self._seg_band = {c: _CONFIG_BANDS[i % len(_CONFIG_BANDS)]
                          for i, c in enumerate(counts)}
        self.update()

    def set_events(self, commands: List[dict], total_samples: int) -> None:
        self._commands = commands
        # Pre-sort once at load, not per paint/hover: by start sample (arrow-nav +
        # nearest-tick bisect) and by paint rank (so paintEvent never re-sorts the
        # whole list — it was O(N log N) every frame, on the GUI thread).
        self._by_start = sorted(commands, key=lambda c: int(c.get("start_sample", 0)))
        self._starts = [int(c.get("start_sample", 0)) for c in self._by_start]
        self._draw_order = sorted(commands, key=tick_rank)
        self._total = max(1, int(total_samples))
        self._reset_view()                       # new capture → show the whole thing

    def set_bringup(self, result) -> None:
        """Mark the §5.1.2 link bring-up region (Bus Reset → PHY-select → audio).
        `result` is a LinkControlResult; cleared when there's no bring-up so the
        ribbon shows nothing extra for mid-stream/plain captures."""
        if result is None or getattr(result, "sequence", "none") == "none":
            self._bringup = {}
            self._link_sections = []
        else:
            self._bringup = {
                "start": int(result.bus_reset_sample or 0),
                "phy_select": (int(result.phy_select_sample)
                               if result.phy_select_sample is not None else None),
                "phystart": (int(result.phystart_sample)
                             if result.phystart_sample is not None else None),
                "audio_start": int(result.audio_start_sample or 0),
                "label": result.label(),
                # For the safe-lock band label: the selected PHY + its Safe-Lock column
                # count, so the initial audio segment reads e.g. "Cold Start PHY2".
                "sequence": getattr(result, "sequence", "none"),
                "phy_name": getattr(result, "phy_name", None),
                "safe_lock_columns": getattr(result, "safe_lock_columns", None),
            }
            # Named LC sub-phases (Idle / Bus Reset / Cold|Warm Start / PhyStart /
            # LC_Request) — drawn as distinct colour bands within the bring-up region.
            self._link_sections = list(getattr(result, "sections", []) or [])
        self.update()

    def set_cursor(self, sample: int) -> None:
        self._cursor = int(sample)
        # Keep the cursor visible: if it lands outside the zoomed view (e.g. the user
        # scrolled away, then clicked a command or stepped the playhead), recenter on
        # it — same as arrow-key nav. No-op at full zoom (cursor is always in view).
        if not self._in_view(sample, margin=0.0):
            self._center_view_on(sample)
        self.update()

    def set_bookmarks(self, marks) -> None:
        """marks: iterable of (sample, label) — a pink diamond at the baseline with a
        vertical line up through the track and the pair label (e.g. 'A1') at the top."""
        self._bookmarks = [(int(s), str(lbl)) for s, lbl in marks]
        self.update()

    def _track_rect(self) -> QRectF:
        # Leave the bottom _AXIS_H px for the time ruler; the rest is the track.
        return QRectF(_MARGIN, 10, max(1, self.width() - 2 * _MARGIN),
                      _H - 28 - _AXIS_H)

    def _reset_view(self) -> None:
        """Show the whole capture (clears any zoom/pan)."""
        self._view_lo = 0.0
        self._view_hi = float(max(1, self._total))
        self.update()

    def _view_span(self) -> float:
        return max(1.0, self._view_hi - self._view_lo)

    def _x_for(self, sample: int) -> float:
        tr = self._track_rect()
        return tr.left() + ((sample - self._view_lo) / self._view_span()) * tr.width()

    def _sample_for(self, x: float) -> int:
        tr = self._track_rect()
        frac = (x - tr.left()) / tr.width() if tr.width() else 0.0
        return int(self._view_lo + max(0.0, min(1.0, frac)) * self._view_span())

    def _clampx(self, x: float) -> float:
        """Clamp a pixel x to the widget so off-view coordinates can't overflow
        Qt's 32-bit int in drawLine/drawRect when zoomed in."""
        return max(-2.0, min(float(self.width()) + 2.0, x))

    def _in_view(self, sample: float, margin: float = 0.05) -> bool:
        span = self._view_span()
        return (self._view_lo - span * margin) <= sample <= (self._view_hi + span * margin)

    def wheelEvent(self, event) -> None:
        """A vertical two-finger gesture / wheel **zooms** around the pointer; a
        horizontal gesture (or Shift+wheel) **scrolls** (pans) — matching the audio
        waveform. Zoom is proportional to the scroll delta, so trackpad gestures
        stay gentle instead of jumping a fixed step per event."""
        ad = event.angleDelta()
        dx, dy = ad.x(), ad.y()
        if dx == 0 and dy == 0:
            return
        horizontal = abs(dx) > abs(dy) or bool(event.modifiers() & Qt.ShiftModifier)
        if horizontal:
            d = dx if abs(dx) >= abs(dy) else dy
            shift = -(d / 120.0) * self._view_span() * 0.15   # ~15% of view per notch
            self._set_view(self._view_lo + shift, self._view_hi + shift)
        else:
            factor = 0.9985 ** dy                             # <1 zooms in; proportional
            pivot = self._sample_for(event.position().x())
            lo = pivot - (pivot - self._view_lo) * factor
            hi = pivot + (self._view_hi - pivot) * factor
            self._set_view(lo, hi)
        event.accept()

    def _set_view(self, lo: float, hi: float) -> None:
        """Clamp a proposed [lo, hi] view to the capture and a sensible min zoom,
        keeping the span when panning hits an edge."""
        total = float(max(1, self._total))
        span = max(min(hi - lo, total), 16.0)               # cap zoom-in at 16 samples
        if lo < 0.0:
            lo, hi = 0.0, span
        elif hi > total:
            lo, hi = total - span, total
        else:
            hi = lo + span
        self._view_lo, self._view_hi = max(0.0, lo), min(total, lo + span)
        self.update()

    def mouseDoubleClickEvent(self, event) -> None:
        self._reset_view()

    def set_nav_starts(self, starts) -> None:
        """Restrict Left/Right arrow navigation to these (sorted) command samples — the
        command table's currently-VISIBLE commands, at their Row-Sync-Point cursor samples
        (where nav parks the cursor). None (the default) navigates every command. This does
        NOT gate what's DRAWN — the drawn ticks are keyed by raw start_sample, so the paint
        filter is set separately (set_draw_filter)."""
        self._nav_starts = sorted(int(s) for s in starts) if starts is not None else None

    def set_draw_filter(self, start_samples) -> None:
        """Restrict the DRAWN command ticks to these raw start_samples (the timeline's own
        command anchor), so the marks reflect the command-table filter. None = draw all.
        Kept distinct from set_nav_starts because nav uses RSP samples, which don't match
        the raw start_sample the ticks are drawn at (so a commit tick would vanish)."""
        self._draw_set = ({int(s) for s in start_samples}
                          if start_samples is not None else None)
        self.update()

    def set_commit_starts(self, starts) -> None:
        """Sync-point commit (SSCR/DSCR) start samples, sorted — the targets for
        ⌘/Ctrl + Left/Right jump-to-commit navigation."""
        self._commit_starts = sorted(int(s) for s in starts) if starts else []

    def set_commit_points(self, points) -> None:
        """Effective-commit (SSP) samples — where a confirmed sync-point commit actually
        takes effect (Row_Delay rows after the command). Drawn as a dotted commit-colour
        line; distinct from the commit COMMAND tick at its own start sample."""
        self._commit_points = sorted(int(p) for p in points) if points else []
        self.update()

    def keyPressEvent(self, event) -> None:
        """Left/Right step the cursor to the previous/next command (respecting the
        command-table filter via _nav_starts). ⌘/Ctrl + Left/Right jump between the
        sync-point commits (SSCR/DSCR). When zoomed in, also recenter the view on the
        new cursor so it lands at the centre of the pane (at full zoom unchanged)."""
        commit_jump = bool(event.modifiers() & Qt.ControlModifier)   # ⌘ on macOS
        starts = self._commit_starts if commit_jump else (
            self._nav_starts if self._nav_starts is not None else self._starts)
        if not starts:
            super().keyPressEvent(event)
            return
        target = None
        if event.key() == Qt.Key_Right:
            i = bisect.bisect_right(starts, self._cursor)
            if i < len(starts):
                target = int(starts[i])
        elif event.key() == Qt.Key_Left:
            i = bisect.bisect_left(starts, self._cursor) - 1
            if i >= 0:
                target = int(starts[i])
        else:
            super().keyPressEvent(event)
            return
        if target is None:
            return
        self.seeked.emit(target)
        self._center_view_on(target)

    def _center_view_on(self, sample: int) -> None:
        """Recenter the visible window on `sample` (keeping the current span) when
        zoomed in — used by arrow navigation. No-op at full zoom."""
        if self._view_span() >= float(max(1, self._total)):
            return
        half = self._view_span() / 2.0
        self._set_view(sample - half, sample + half)

    def _decimated_ticks(self) -> List[tuple]:
        """[(x, cmd), …] — at most one command per pixel column (the MOST significant,
        by paint rank), for the current view range / width / filter. Recomputed only
        when one of those actually changed (cached otherwise), so paintEvent — driven
        by a 16 ms play timer — doesn't re-walk every command (tens of thousands at
        full zoom-out) each frame; it just redraws this ~width-sized list."""
        key = (self._view_lo, self._view_hi, self.width(),
               id(self._draw_order), id(self._draw_set))
        if key == self._decim_key:
            return self._decim_ticks
        drawn_px = set()
        ticks = []
        for cmd in reversed(self._draw_order):
            s = cmd.get("start_sample", 0)
            # Honor the command-table filter: don't draw ticks for filtered-out commands
            # (keyed by raw start_sample, matching the tick position — see set_draw_filter).
            if self._draw_set is not None and int(s) not in self._draw_set:
                continue
            if not self._in_view(s):                          # skip off-view (and avoid overflow)
                continue
            x = int(self._x_for(s))
            if x in drawn_px:                                 # a more significant tick owns this column
                continue
            drawn_px.add(x)
            ticks.append((x, cmd))
        self._decim_key = key
        self._decim_ticks = ticks
        return ticks

    def _decimated_commit_points(self) -> List[int]:
        """In-view commit-point x's, at most one per pixel column, cached by view/width —
        so paintEvent (16 ms play timer) doesn't re-walk every commit point (tens of
        thousands at full zoom-out) each frame. Same caching shape as _decimated_ticks."""
        key = (self._view_lo, self._view_hi, self.width(), id(self._commit_points))
        if key == self._commit_decim_key:
            return self._commit_decim
        seen_px: set = set()
        xs: List[int] = []
        for cp in self._commit_points:                    # sorted; one line per column
            if not self._in_view(cp):
                continue
            x = int(self._x_for(cp))
            if x in seen_px:
                continue
            seen_px.add(x)
            xs.append(x)
        self._commit_decim_key = key
        self._commit_decim = xs
        return xs

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, False)
        p.fillRect(self.rect(), QColor(VizTheme.PLOT_BG))
        tr = self._track_rect()
        # Bus-config bands sit in a TOP STRIP only (not the full track height), so the
        # command ticks below them read clearly instead of being crossed by the fill.
        # One translucent fill per segment, coloured by column count, labelled "{cols}col".
        band_bottom = tr.top() + _CFG_BAND_H
        label_font = QFont(self.font())
        label_font.setPointSize(9)
        # The decoder's first audio segment nominally starts at UI 1 (its column count
        # holds from the very beginning), so its band would otherwise be drawn ACROSS the
        # pre-audio link-control region — overlapping the LC sub-phase bands + labels that
        # own that strip there. When a bring-up is present, clip the audio config bands to
        # begin at audio_start (the LC bands cover [0, audio_start)).
        band_left = (self._clampx(self._x_for(int(self._bringup.get("audio_start", 0))))
                     if self._bringup else None)
        for i, seg in enumerate(self._segments):
            x0 = self._clampx(self._x_for(int(seg.get("start_sample", 0))))
            x1 = self._clampx(self._x_for(int(self._segments[i + 1].get("start_sample", 0)))
                              if i + 1 < len(self._segments) else tr.right())
            if band_left is not None and x0 < band_left:
                x0 = band_left
            if x1 <= x0:
                continue
            cols = int(seg.get("column_count", 0))
            p.fillRect(QRectF(x0, tr.top(), max(1.0, x1 - x0), _CFG_BAND_H),
                       self._seg_band.get(cols, _CONFIG_BANDS[0]))
            # A user-pinned region is outlined so "this width was forced, not snooped"
            # is visible at a glance even where the band is too narrow for its label.
            # NoBrush explicitly: drawRect FILLS with the painter's current brush, and
            # the bring-up flag below sets a solid one — this outline only stays an
            # outline because nothing has set a brush by the time the loop runs.
            if seg.get("forced"):
                p.setPen(QPen(QColor(VizTheme.SEM_ERROR), 1, Qt.DashLine))
                p.setBrush(Qt.NoBrush)
                p.drawRect(QRectF(x0, tr.top(), max(1.0, x1 - x0) - 1, _CFG_BAND_H - 1))
            if x1 - x0 > 30:
                p.setFont(label_font)
                p.setPen(QPen(QColor(VizTheme.TEXT)))     # theme text — visible in light AND dark
                p.drawText(QRectF(x0 + 6, tr.top(), x1 - x0 - 8, _CFG_BAND_H),
                           Qt.AlignLeft | Qt.AlignVCenter,
                           self._segment_label(i, cols, bool(seg.get("forced"))))
        # §5.1.2 link bring-up (Bus Reset → PHY-select → audio). In a multi-second
        # capture this region is well under a pixel wide, so draw an ALWAYS-VISIBLE
        # marker in a distinct cyan (NOT the gold Commit+SSP tick): a min-width band,
        # a solid vertical line at the PHY-select edge, and a downward flag on top.
        if self._bringup:
            # Named sub-phase bands (Idle / Bus Reset / Cold|Warm Start / PhyStart /
            # LC_Request), each its own data-port-palette colour, drawn in the SAME top
            # strip as the config "Ncol" bands (so LC regions read like the audio-mode
            # region labels), with a label where the band is wide enough.
            sec_font = QFont(self.font()); sec_font.setPointSize(8)
            for sec in self._link_sections:
                sx0 = self._clampx(self._x_for(int(sec.get("start", 0))))
                sx1 = self._clampx(self._x_for(int(sec.get("end", 0))))
                w = max(2.0, sx1 - sx0)
                sband = QRectF(sx0, tr.top(), w, _CFG_BAND_H)
                p.fillRect(sband, _lc_section_color(sec.get("name", ""), 120))
                p.setPen(QPen(_lc_section_color(sec.get("name", ""), 220), 1))
                p.drawRect(sband)
                if w > 44:
                    p.setFont(sec_font)
                    p.setPen(QPen(QColor(VizTheme.TEXT)))
                    p.drawText(QRectF(sx0 + 3, tr.top(), w - 5, _CFG_BAND_H),
                               Qt.AlignLeft | Qt.AlignVCenter, sec.get("name", ""))
            if not self._link_sections:                       # fallback: single cyan band
                bx0 = self._clampx(self._x_for(self._bringup["start"]))
                bx1 = self._clampx(self._x_for(self._bringup["audio_start"]))
                band = QRectF(bx0, tr.top(), max(4.0, bx1 - bx0), _CFG_BAND_H)
                p.fillRect(band, QColor(_C_BRINGUP.red(), _C_BRINGUP.green(), _C_BRINGUP.blue(), 120))
                p.setPen(QPen(_C_BRINGUP, 1))
                p.drawRect(band)
            ps = self._bringup.get("phy_select")
            mx = self._clampx(self._x_for(ps if ps is not None else self._bringup["start"]))
            p.setPen(QPen(_C_BRINGUP, 2))
            p.drawLine(int(mx), int(tr.top()), int(mx), int(tr.bottom()))
            flag = QPolygonF([QPointF(mx - 4, tr.top() - 7), QPointF(mx + 4, tr.top() - 7),
                              QPointF(mx, tr.top())])           # downward triangle above track
            p.setBrush(_C_BRINGUP); p.setPen(QPen(_C_BRINGUP, 1))
            p.drawPolygon(flag)
        # baseline
        p.setPen(QPen(_C_AXIS))
        p.drawLine(int(tr.left()), int(tr.bottom()), int(tr.right()), int(tr.bottom()))
        # event ticks. A long capture is ~98% Pings; with ~7M samples per pixel many
        # commands collide on one x. Paint order is by ASCENDING importance so the
        # significant marks WIN the pixel (Ping < other < commit < SSP/error), instead
        # of last-in-time-order overpainting a Write with a later Ping. At most ONE tick
        # per pixel column is drawn — the MOST significant — so a zoomed-out large
        # capture (tens of thousands of commands collapsing onto ~1000 pixels) doesn't
        # walk every command each frame (that made navigation/the play-timer sweep
        # janky). The decimated (x, cmd) list is cached by _decimated_ticks and only
        # recomputed when the view range / width / filter actually changes — paintEvent
        # itself just iterates the ~width-sized result. Visually identical: colliding
        # ticks overpaint anyway.
        for x, cmd in self._decimated_ticks():
            color = _kind_color(cmd)
            tall = color in (_C_ERROR, _C_SSP)
            # All ticks are 1px wide; the notable ones (CRC error, SSP/commit) are
            # distinguished by their FULL height alone (no extra width).
            p.setPen(QPen(color, 1))
            top = band_bottom if tall else band_bottom + (tr.bottom() - band_bottom) * 0.30
            p.drawLine(x, int(top), x, int(tr.bottom()))
        # Commit points: where a confirmed sync-point commit actually TAKES EFFECT (the
        # SSP, Row_Delay rows after the command) — the Commit Synchronization Point. Drawn
        # DOTTED in its own colour (distinct from the blue "Commit" tick and the gold
        # "Commit + SSP" command tick) so the deferred effect reads as its own event.
        if self._commit_points:
            dot = QPen(_C_SYNC, 1, Qt.DotLine)
            p.setPen(dot)
            for cx in self._decimated_commit_points():
                p.drawLine(int(cx), int(band_bottom), int(cx), int(tr.bottom()))
        # cursor
        if self._in_view(self._cursor):
            cx = self._x_for(self._cursor)
            p.setPen(QPen(_C_CURSOR, 1))
            p.drawLine(int(cx), 2, int(cx), int(tr.bottom()) + 4)
            p.setBrush(_C_CURSOR)
            p.drawRect(QRectF(cx - 3, 2, 6, 6))
        # bookmarks: a diamond at the baseline, a vertical line up through the track, and
        # the pair label (A1/A2/…) at the top — coloured per PAIR (rotating the DP palette).
        yb = int(tr.bottom())
        for bm, label in self._bookmarks:
            if not self._in_view(bm):
                continue
            bx = self._x_for(bm)
            col = _bookmark_pair_color(label)
            p.setPen(QPen(col, 1))
            p.drawLine(int(bx), int(tr.top()), int(bx), yb)
            p.setBrush(col)
            p.drawPolygon(_diamond(bx, yb + 2, 4))
            if label:
                p.drawText(QRectF(bx + 3, tr.top() - 1, 28, 12), Qt.AlignLeft | Qt.AlignVCenter, label)
        # time ruler along the bottom
        self._draw_axis(p, tr)
        p.end()

    def _draw_axis(self, p: QPainter, tr: QRectF) -> None:
        """Draw a time ruler beneath the track: evenly spaced ticks at 'nice'
        intervals, labelled in seconds (or sample index if the rate is unknown)."""
        y = tr.bottom() + 4
        p.setPen(QPen(_C_AXIS))
        p.drawLine(int(tr.left()), int(y), int(tr.right()), int(y))
        # Aim for ~8 ticks across the VISIBLE window; snap the interval to a
        # 1/2/5×10^n value in the axis unit (seconds when a rate is known).
        per_px = self._view_span() / max(1.0, tr.width())    # axis-units per pixel
        unit = (1.0 / self._rate) if self._rate else 1.0     # samples -> seconds
        target = per_px * 90 * unit                          # ~90 px between ticks
        step_u = self._nice(target)                          # in axis units (s or samples)
        if step_u <= 0:
            return
        step_samples = step_u / unit
        if step_samples < 1.0:                               # never sub-sample (deep zoom):
            step_samples = 1.0                               # otherwise the tick loop explodes
        p.setPen(QPen(QColor(170, 170, 175)))
        import math
        first = math.floor(self._view_lo / step_samples)
        last = math.ceil(self._view_hi / step_samples)
        if last - first > 200:                               # hard cap — never iterate forever
            return
        for i in range(first, last + 1):
            s = i * step_samples
            if s < 0 or s > self._total:
                continue
            x = self._x_for(s)
            p.setPen(QPen(_C_AXIS))
            p.drawLine(int(x), int(y), int(x), int(y) + 4)
            p.setPen(QPen(QColor(180, 180, 185)))
            p.drawText(QRectF(x - 44, y + 4, 88, _AXIS_H - 4),
                       Qt.AlignHCenter | Qt.AlignTop, self._fmt_axis(s, step_u))

    @staticmethod
    def _nice(x: float) -> float:
        """Round x up to the nearest 1/2/5 × 10^n (a 'nice' tick interval)."""
        import math
        if x <= 0:
            return 0.0
        exp = math.floor(math.log10(x))
        base = 10 ** exp
        for m in (1, 2, 5, 10):
            if m * base >= x:
                return m * base
        return 10 * base

    def _fmt_axis(self, sample: float, step_u: float) -> str:
        """Label a tick: seconds in a unit chosen by the capture's total duration
        (so labels read 0..N in one consistent unit), with decimals from the tick
        spacing. Falls back to the raw sample index when no rate is known."""
        if not self._rate:
            return f"{sample:,.0f}"
        total_s = self._total / self._rate
        if total_s >= 1.0:
            scale, unit = 1.0, "s"
        elif total_s >= 1e-3:
            scale, unit = 1e3, "ms"
        elif total_s >= 1e-6:
            scale, unit = 1e6, "µs"
        else:
            scale, unit = 1e9, "ns"
        t = (sample / self._rate) * scale             # value in the chosen unit
        step = step_u * scale                         # tick spacing in that unit
        import math
        dec = 0 if step >= 1 else int(min(6, max(0, math.ceil(-math.log10(step)))))
        return f"{t:,.{dec}f} {unit}"

    def _bookmark_at(self, x: float, px: float = 6.0):
        """Label of the visible bookmark whose vertical line is within `px` of x, or
        None. The whole line (not just the diamond) is a grab target."""
        best, best_d = None, px
        for s, label in self._bookmarks:
            if not self._in_view(s):
                continue
            d = abs(self._x_for(s) - x)
            if d <= best_d:
                best, best_d = label, d
        return best

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MiddleButton:
            self._pan_x = event.position().x()       # begin pan
            return
        if event.button() == Qt.LeftButton:
            label = self._bookmark_at(event.position().x())
            if label is not None:
                self._drag_bm = label                # grab the bookmark instead of seeking
                return
        self.seeked.emit(self._sample_for(event.position().x()))

    def mouseReleaseEvent(self, event) -> None:
        if self._drag_bm is not None:                # finalise: snap to nearest edge
            self.bookmarkMoved.emit(self._drag_bm, self._sample_for(event.position().x()), True)
            self._drag_bm = None
        self._pan_x = None
        self._flush_pending_seek()                   # apply the final drag position now

    def _emit_pending_seek(self) -> None:
        """Timer tick: emit the most recent drag sample (coalesced)."""
        if self._pending_seek is not None:
            s, self._pending_seek = self._pending_seek, None
            self.seeked.emit(s)

    def _flush_pending_seek(self) -> None:
        """Emit any queued seek immediately (drag end) so the resting cursor isn't a
        timer-interval behind the pointer."""
        self._seek_timer.stop()
        self._emit_pending_seek()

    def _segment_label(self, index: int, cols: int, forced: bool = False) -> str:
        """Config-band label. The initial audio segment of a cold/warm start is the
        selected PHY's Safe-Lock geometry, so label it by the PHY (e.g. 'PHY2
        Safe-Lock-2') — the PHY is what makes that 2-column (FBCSE) / 4-column (DLV)
        region meaningful. It is NOT labelled 'Cold Start': the cold-start SEQUENCE is
        the link-control region drawn before this band (Bus Reset / PHY-number clock /
        PhyStart), and this audio band begins only after the PHY is selected.

        A `forced` region had its width pinned by the user, so say so — otherwise a
        pinned width is indistinguishable from one the decoder read off the wire."""
        b = self._bringup
        if (index == 0 and b and b.get("phy_name")
                and b.get("safe_lock_columns") == cols and not forced):
            return f"{b['phy_name']} Safe-Lock-{cols}"
        return f"{cols}col (forced)" if forced else f"{cols}col"

    def _nearest(self, x: float, px: float = 5.0):
        """The command whose tick is within `px` of x (closest; higher paint rank wins
        exact ties), or None. Bisects the start-sorted list to the pixel neighbourhood."""
        if not self._starts:
            return None
        lo_s, hi_s = self._sample_for(x - px), self._sample_for(x + px)
        i0 = bisect.bisect_left(self._starts, lo_s - 1)
        i1 = bisect.bisect_right(self._starts, hi_s + 1)
        best, best_d, best_rank = None, px, -1
        for cmd in self._by_start[i0:i1]:
            d = abs(self._x_for(cmd.get("start_sample", 0)) - x)
            r = tick_rank(cmd)
            if d < best_d or (d <= best_d and r > best_rank):
                best, best_d, best_rank = cmd, d, r
        return best

    def _segment_at(self, sample: int):
        """The bus-config segment whose sample range contains `sample`, or None."""
        chosen = None
        for i, s in enumerate(self._segments):
            if int(s.get("start_sample", 0)) <= sample:
                chosen = (i, s)
            else:
                break
        return chosen

    def _link_section_at(self, sample: int):
        """The link-control sub-phase whose [start, end) contains `sample`, or None."""
        for sec in self._link_sections:
            if int(sec.get("start", 0)) <= sample < int(sec.get("end", 0)):
                return sec
        return None

    def _hover_text(self, x: float) -> str:
        """Describe what's under the pointer for the status bar (command / commit point /
        link-control phase / config band). Empty when there's nothing informative."""
        def when(s):
            return f"{s / self._rate * 1e6:,.1f} µs" if self._rate else f"sample {int(s):,}"
        cmd = self._nearest(x)
        if cmd is not None:
            # Concise: the command name, plus its kind only when that adds something
            # beyond the name (e.g. "CRC error" on a WriteA32) — NOT the legend's tick-
            # appearance blurb, which was just noise on a specific command.
            name = str(cmd.get("command", "")) or "?"
            label = _kind_label(cmd)
            tag = f" ({label})" if label and label.lower() not in name.lower() else ""
            s = int(cmd.get("start_sample", 0))
            return (f"{name}{tag} · row {self.display_row(cmd.get('bus_row', 0)):,} · "
                    f"{when(s)}")
        sample = self._sample_for(x)
        cp = self._nearest_commit_point(x)
        if cp is not None:
            row = ""
            if self._row_label is not None:
                try:
                    row = f" · row {int(self._row_label(cp)):,}"
                except Exception:                       # noqa: BLE001 - never break hover
                    row = ""
            return f"Commit Sync Point{row} · {when(cp)}"
        sec = self._link_section_at(sample)
        if sec is not None:
            dur = (sec["end"] - sec["start"]) / self._rate * 1e6 if self._rate else 0.0
            return f"Link Control: {sec['name']} · {dur:,.1f} µs"
        seg = self._segment_at(sample)
        if seg is not None:
            i, s = seg
            forced = bool(s.get("forced"))
            note = (" · column count pinned by you (Analyzer ▸ Force Column Count) — "
                    "the wire's own width is overridden for this region" if forced else "")
            return (f"Bus config: "
                    f"{self._segment_label(i, int(s.get('column_count', 0)), forced)} "
                    f"· from row {int(s.get('row_base', 0)):,}{note}")
        return ""

    def _nearest_commit_point(self, x: float, px: float = 5.0):
        """The commit-point (SSP) effective sample within `px` of x, or None. Bisects the
        sorted commit-point list to the pixel neighbourhood (mirrors _nearest)."""
        if not self._commit_points:
            return None
        lo_s, hi_s = self._sample_for(x - px), self._sample_for(x + px)
        i0 = bisect.bisect_left(self._commit_points, int(lo_s) - 1)
        i1 = bisect.bisect_right(self._commit_points, int(hi_s) + 1)
        best, best_d = None, px
        for cp in self._commit_points[i0:i1]:
            d = abs(self._x_for(cp) - x)
            if d < best_d:
                best, best_d = cp, d
        return best

    def mouseMoveEvent(self, event) -> None:
        x = event.position().x()
        if self._drag_bm is not None and (event.buttons() & Qt.LeftButton):
            self.bookmarkMoved.emit(self._drag_bm, self._sample_for(x), False)  # live
            return
        if event.buttons() & Qt.MiddleButton and self._pan_x is not None:
            tr = self._track_rect()
            shift = -(x - self._pan_x) / max(1.0, tr.width()) * self._view_span()
            self._pan_x = x
            self._set_view(self._view_lo + shift, self._view_hi + shift)
            return
        if event.buttons() & Qt.LeftButton:
            # Coalesce: stash the latest sample and let the timer emit it (steady ~30 ms
            # cadence — start only when idle so rapid moves collapse into one emit).
            self._pending_seek = self._sample_for(x)
            if not self._seek_timer.isActive():
                self._seek_timer.start()
            return
        # Hover (no drag): a tooltip naming what's under the pointer (command / commit
        # point / link-control phase / config band). The WIDGET-level "Click to seek…"
        # help tooltip was removed — it kept replacing this useful info popup.
        text = self._hover_text(x)
        if text:
            QToolTip.showText(event.globalPosition().toPoint(), text, self)
        else:
            QToolTip.hideText()
