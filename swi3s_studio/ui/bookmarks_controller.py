"""The window's cursors (bookmarks): the set, Cursors ▸ Toggle / Next / Previous / Clear,
a drag from a pane, and the rows of the Cursors pane.

`BookmarksController` holds the window's `BookmarkSet` and pushes it to every pane that
draws cursors: each timeline band and each bottom lane its own Link's, and the Cursors
pane every pair. It reads the window's Links, cursor, panes and status bar through the
window it is given, and holds no other state.
"""
from __future__ import annotations

import math
from typing import TYPE_CHECKING, Dict, Optional

import numpy as np

from ..model.bookmarks import BookmarkSet

if TYPE_CHECKING:
    from .main_window import MainWindow

# Two Links count as running at "the same" UI / row rate (so a cross-Link bookmark pair can
# be read in UIs and rows) within 0.1%: far above two analyzers' timebase error, far below
# the gap between distinct bus rates.
_SAME_RATE_REL_TOL = 1e-3


def fmt_duration(seconds: float) -> str:
    """Human 'Δ time' for a bookmark pair: pick s / ms / µs / ns by magnitude."""
    a = abs(seconds)
    if a >= 1.0:
        return f"{seconds:,.4f} s"
    if a >= 1e-3:
        return f"{seconds * 1e3:,.3f} ms"
    if a >= 1e-6:
        return f"{seconds * 1e6:,.3f} µs"
    return f"{seconds * 1e9:,.1f} ns"


class BookmarksController:
    """The cursors of `win`, a MainWindow: `bookmarks` is the set, each bookmark on the
    Link it was placed on."""

    def __init__(self, win: "MainWindow") -> None:
        self._win = win
        self.bookmarks = BookmarkSet()

    def toggle(self) -> None:
        """Ctrl+B: if a bookmark sits at the cursor, remove it; otherwise drop the next
        one in the A1, A2, B1, B2, … sequence (each pair measures a region)."""
        if self._win._session is None:
            return
        s = int(self._win.cursor.sample)
        tol = self._tolerance()
        link = self._win._links.active_index      # a bookmark belongs to the Link it is on
        if not self.bookmarks.remove_at(s, tol=tol, link=link):
            bm = self.bookmarks.add(s, link=link)
            self._win._status.setText(f"Cursor {bm.display()} @ {s:,}")
        else:
            self._win._status.setText(f"{len(self.bookmarks)} cursor(s)")
        self.push()

    def _tolerance(self) -> int:
        """Hit tolerance (samples) for removing/snapping a bookmark: ~half a UI so a
        Ctrl+B on an existing marker toggles it rather than stacking a second one."""
        sess = self._win._session
        if sess is None:
            return 1
        try:
            spu = sess.sample_rate_hz / sess.ui_rate_hz
            return max(1, int(spu // 2))
        except Exception:                       # noqa: BLE001 — never break the toggle
            return 1

    def sample_of(self, bm) -> int:
        """Where a bookmark sits in the ACTIVE Link's samples, which is what the cursor and
        every per-Link pane count in. A bookmark on the active Link is its own sample."""
        if len(self._win._links) == 0:
            return int(bm.sample)
        return self._win._links.convert(bm.sample, bm.link, self._win._links.active_index)

    def marks(self, link: int) -> list:
        """(sample, label) for the bookmarks ON Link `link`, in its own samples: a view
        draws only its own Link's bookmarks; a pair across two Links is read in the
        Cursors pane."""
        return [(int(b.sample), b.display()) for b in self.bookmarks if b.link == link]

    def ps_of(self, bm) -> int:
        """A bookmark's global time (ps), for ordering and timing across Links."""
        return self._win._links[bm.link].to_ps(bm.sample)

    def _active_samples(self) -> list:
        """The active Link's bookmarks, in its samples: the ones its band shows, and so the
        only ones next / previous may land on (another Link's would park the cursor on a
        mark nothing on this Link draws)."""
        a = self._win._links.active_index
        return [int(b.sample) for b in self.bookmarks if b.link == a]

    def next(self) -> None:
        bms = sorted(self._active_samples())
        if not bms:
            return
        cur = self._win.cursor.sample
        self._win.cursor.set_sample(
            self._win._clamp_to_active(next((s for s in bms if s > cur), bms[0])))

    def previous(self) -> None:
        bms = sorted(self._active_samples(), reverse=True)
        if not bms:
            return
        cur = self._win.cursor.sample
        self._win.cursor.set_sample(
            self._win._clamp_to_active(next((s for s in bms if s < cur), bms[0])))

    def clear(self) -> None:
        self.bookmarks.clear()
        self.push()

    def push(self) -> None:
        """Refresh every bookmark consumer from the current set: the timeline, audio and
        raw panes (markers) and the Measurements pane (pair deltas)."""
        for j, ribbon in enumerate(self._win._ribbons[:max(1, len(self._win._links))]):
            ribbon.set_bookmarks(self.marks(j))              # each band: its own Link's
        for k, raw, audio in self._win._bottom_lanes():      # each lane: its Link's
            marks = self.marks(k)
            raw.set_bookmarks(marks)
            audio.set_bookmarks(marks)
        self._win._pair_view.set_rows(self.measure_rows())
        self._win._pair_view.set_show_link(len(self._win._links) > 1)

    def measure_rows(self) -> list:
        """Bookmark rows for the pane: per group, an A1 row, an A2 row (if it exists), and
        an A1-A2 delta row. A lone bookmark shows just its A1 row (no waiting for a pair).
        Each row is (label, UI, Row, Time, seek, Link name, Link index); a pair's Link is
        "Link 1 → Link 2" when it spans two, and its index is its first member's.

        A bookmark's UI, Row and Time are read on ITS OWN Link; Time includes the Link's
        offset, so it is global time. The seek is GLOBAL time (ps), converted only when the
        row is clicked (`MainWindow._on_pair_seek`), which first makes the row's Link active: a view
        draws only its own Link's bookmarks, so that is where the mark can be seen."""
        rows: list = []
        if len(self._win._links) == 0:
            return rows

        def rates(link):
            sess = self._win._links[link].session
            rate = float(getattr(sess, "sample_rate_hz", 0) or 0)
            ui_rate = float(getattr(sess, "ui_rate_hz", 0) or 0)
            return sess, rate, ui_rate, (rate / ui_rate) if (rate and ui_rate) else 0.0

        def ui_of(bm):
            try:                                    # a COUNT of UIs, exact on both PHYs
                return f"{self._win._links[bm.link].session.ui_index(int(bm.sample)):,}"
            except Exception:                       # noqa: BLE001
                return "—"

        def row_of(bm):
            sess = self._win._links[bm.link].session
            try:
                return f"{sess.display_row(sess.bus_row_for_sample(int(bm.sample))):,}"
            except Exception:                       # noqa: BLE001
                return "—"

        def link_name(i):
            return self._win._links[i].name if 0 <= i < len(self._win._links) else "?"

        def time_of(bm):
            rate = rates(bm.link)[1]
            if not rate:
                return f"{int(bm.sample):,} smp"
            return fmt_duration(bm.sample / rate + self._win._links[bm.link].offset_ps / 1e12)

        # Group members by their stable group letter (items() is creation-ordered A1,A2,B1…).
        groups: Dict[str, dict] = {}
        for bm in self.bookmarks.items:             # `items` is a property (all bookmarks)
            groups.setdefault(bm.group, {})[bm.index] = bm
        for g in sorted(groups):
            a1 = groups[g].get(1)
            a2 = groups[g].get(2)
            for member in (a1, a2):
                if member is not None:
                    rows.append((member.display(), ui_of(member), row_of(member),
                                 time_of(member), self.ps_of(member),
                                 link_name(member.link), member.link))
            if a1 is not None and a2 is not None:
                if a1.link == a2.link:
                    dui, drows_s, dt = self._same_link_delta(a1, a2, *rates(a1.link))
                    names = link_name(a1.link)
                else:
                    dui, drows_s, dt = self._cross_link_delta(a1, a2)
                    names = f"{link_name(a1.link)} → {link_name(a2.link)}"
                rows.append((f"{a1.display()}-{a2.display()}", dui, drows_s, dt,
                             self.ps_of(a1), names, a1.link))
        return rows

    @staticmethod
    def _same_link_delta(a1, a2, sess, rate, ui_rate, spu):
        """(ΔUI, ΔRows, ΔTime) for a pair on one Link, counted in that Link's samples.
        ΔUI and ΔRows are COUNTS of the UIs and rows between the two, so they stay right
        across a geometry change (on DLV the UI rate changes fourfold at the Safe-Lock-4
        to 16-column commit)."""
        dsamp = a2.sample - a1.sample
        dt = fmt_duration(dsamp / rate) if rate else f"{dsamp:,} smp"
        try:
            drows = abs(sess.bus_row_for_sample(a2.sample)
                        - sess.bus_row_for_sample(a1.sample))
            drows_s = f"{drows:,}"
        except Exception:                   # noqa: BLE001
            drows_s = "—"
        try:
            dui = f"{abs(sess.ui_index(a2.sample) - sess.ui_index(a1.sample)):,}"
        except Exception:                   # noqa: BLE001
            dui = "—"
        return dui, drows_s, dt

    def _cross_link_delta(self, a1, a2):
        """(ΔUI, ΔRows, ΔTime) for a pair on two Links. Time is exact global time. Row
        NUMBERS of two Links share no origin, so ΔRows and ΔUI are elapsed time at a
        common rate, and each is shown only when the Links SHARE that timing
        (`_shared_rates`): one UI (row) rate on both, across the whole interval."""
        dt_s = (self.ps_of(a2) - self.ps_of(a1)) / 1e12
        ui_hz, row_hz = self._shared_rates(a1, a2)
        dui = f"{abs(dt_s) * ui_hz:,.1f}" if ui_hz else "—"
        drows = f"{abs(dt_s) * row_hz:,.1f}" if row_hz else "—"
        return dui, drows, fmt_duration(dt_s)

    def _shared_rates(self, a1, a2):
        """(UI rate, row rate) in Hz that two Links share over the interval between two
        bookmarks, each None where they do not.

        A Link's timing is not one number: a geometry change moves its row rate (FBCSE) or
        its UI rate (DLV). So each Link's stretch of the interval (in its own samples) must
        lie inside its capture, every region it crosses (`Session.rate_regions`) must have
        a measured rate, and all of them, on both Links, must agree. Measured rates of two
        captures never agree to the last digit (two analyzers' timebases differ by tens of
        ppm), so "agree" is `_SAME_RATE_REL_TOL`, not equality."""
        t_lo, t_hi = sorted((self.ps_of(a1), self.ps_of(a2)))
        found: list = []
        for i in sorted({a1.link, a2.link}):
            entry = self._win._links[i]
            lo, hi = entry.to_sample(t_lo), entry.to_sample(t_hi)
            if lo < 0 or hi > entry.session.last_sample():
                return None, None              # this Link was not recording throughout
            found += [(u, r) for s, e, u, r in entry.session.rate_regions()
                      if s <= hi and e > lo]

        def common(values):
            if not values or any(v is None for v in values):
                return None
            if all(math.isclose(v, values[0], rel_tol=_SAME_RATE_REL_TOL) for v in values):
                return sum(values) / len(values)
            return None

        return common([u for u, _r in found]), common([r for _u, r in found])

    def on_moved(self, label: str, sample: int, final: bool,
                 link: Optional[int] = None) -> None:
        """A bookmark was dragged in a pane. Move it (snapping to the nearest edge on
        release) and refresh all consumers so the pair measurement updates live.

        `sample` counts in Link `link` (default: the active Link), the Link of the view it
        was dragged in, which since each view draws only its own Link's bookmarks is the
        bookmark's own. It is converted anyway, so a caller that names another Link is
        still right. The bookmark snaps to its own Link's clock edges."""
        target = next((b for b in self.bookmarks if b.display() == label), None)
        if target is None:
            return
        src = self._win._links.active_index if link is None else int(link)
        s = self._win._links.convert(int(sample), src, target.link)
        if final:
            s = self._snap_to_edge(s, self._win._links[target.link].session)
        self.bookmarks.move(target, s)
        self.push()
        if final:
            self._win.cursor.set_sample(self._win._clamp_to_active(self.sample_of(target)))

    def _snap_to_edge(self, sample: int, session=None) -> int:
        """Snap a dragged sample to the nearest clock edge of `session` (default: the active
        Link's), so a bookmark lands on a real UI boundary. Falls back to the raw sample if
        there are no edges."""
        src = session if session is not None else self._win._session
        if src is None:
            return int(sample)
        try:
            ce = src.capture.clock_edges
            if len(ce) == 0:
                return int(sample)
            i = int(np.searchsorted(ce, sample))
            cands = [c for c in (i - 1, i) if 0 <= c < len(ce)]
            return int(min((int(ce[c]) for c in cands), key=lambda e: abs(e - sample)))
        except Exception:                       # noqa: BLE001 — snap is best-effort
            return int(sample)
