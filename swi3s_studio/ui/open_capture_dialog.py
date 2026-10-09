"""File ▸ Open Capture's one-page dialog: what the file is, its Links, how much to decode,
and whether it replaces or adds to the open Links.

It replaces a chain of small modal questions: a clock picker, a data picker, "Take another
Link from this file?", a blind start and length for a large .sal, a separate Open Capture
Time Window, and the analog CSV dialog. Everything here is decided before anything is decoded,
from an `ingest.probe.CaptureProbe`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QButtonGroup,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QRadioButton,
    QSpinBox,
    QVBoxLayout,
)

from ..ingest.probe import CaptureProbe
from .theme import VizTheme, analyzer_stylesheet


@dataclass
class LinkRequest:
    name: str
    clock: object                # a ProbedChannel.value
    data: object
    clock_thresh: Optional[Tuple[float, float]] = None   # analog: (hi, lo) volts, None = auto
    data_thresh: Optional[Tuple[float, float]] = None
    auto_clock: bool = False     # the user left a two-channel default: the loader orients it


@dataclass
class OpenRequest:
    links: List[LinkRequest] = field(default_factory=list)
    window_s: Optional[Tuple[float, float]] = None       # None: all of it
    add: bool = False            # add to the open Links (else replace them)
    offset_s: float = 0.0        # where this file's sample 0 sits in global time (adding)
    sample_rate_hz: Optional[int] = None                 # the rate to load at, when asked for


def _gb(n: Optional[int]) -> str:
    if n is None:
        return "—"
    return f"{n / 1e9:.1f} GB" if n >= 1e9 else f"{n / 1e6:.0f} MB"


def _edges(n: int, approx: bool) -> str:
    """An edge count as the dialog shows it: to two or three figures (it is for picking the
    busy line from the quiet one, and the larger counts are estimates anyway)."""
    if n >= 1_000_000:
        return f"~{n / 1e6:.1f}M"
    if n >= 10_000:
        return f"~{n / 1e3:.0f}k"
    return f"{'~' if approx else ''}{n:,}"


def _time_unit(span_s: float) -> Tuple[str, float]:
    """The unit the window fields read in, from the capture's length: s from a second up,
    ms below, µs below a millisecond; three decimals in it is ms / µs / ns."""
    if span_s >= 1:
        return "s", 1.0
    if span_s >= 1e-3:
        return "ms", 1e-3
    return "µs", 1e-6


class _TimeSpin(QDoubleSpinBox):
    """A window end in the unit `_time_unit` picks, three decimals, read and set in s."""

    def __init__(self, span_s: float) -> None:
        super().__init__()
        self._span = float(span_s)
        unit, self._scale = _time_unit(span_s)
        self.setDecimals(3)
        self.setSuffix(f" {unit}")
        self.setRange(0.0, (span_s if span_s > 0 else 1e9) / self._scale)

    def seconds(self) -> float:
        # The top of the range is the capture's end, exactly: three decimals of the unit
        # round it, and rounded down a window could not reach the last 0.0005 of a unit.
        if self._span > 0 and self.value() >= self.maximum():
            return self._span
        return self.value() * self._scale

    def set_seconds(self, s: float) -> None:
        self.setValue(s / self._scale)


def _dur(s: Optional[float]) -> str:
    if s is None:
        return "—"
    if s >= 1:
        return f"{s:.3f} s"
    if s >= 1e-3:
        return f"{s * 1e3:.3f} ms"
    return f"{s * 1e6:.3f} µs"


def _default_names(taken: List[str], n: int) -> List[str]:
    """The n smallest "Link k" names not in `taken` (as LinkSet._next_default_name)."""
    out: List[str] = []
    k = 1
    while len(out) < n:
        name = f"Link {k}"
        if name not in taken:
            out.append(name)
        k += 1
    return out


class _LinkRow:
    """One row of the Links table: name, clock, data (and analog thresholds)."""

    def __init__(self, dlg: "OpenCaptureDialog", clock: int, data: int) -> None:
        self.dlg = dlg
        pr = dlg.probe
        self.name = QLineEdit()
        self.name.setMinimumWidth(110)
        self.name_edited = False
        self.name.textEdited.connect(lambda _t: self._mark_named())
        self.clock = QComboBox()
        self.data = QComboBox()
        for combo in (self.clock, self.data):
            for ch in pr.channels:
                combo.addItem(dlg.channel_text(ch), ch.value)
        self.clock.setCurrentIndex(clock)
        self.data.setCurrentIndex(data)
        self.default_pair = (clock, data)
        for combo in (self.clock, self.data):
            combo.currentIndexChanged.connect(lambda _i: dlg._validate())
        self.thresh: List[QLineEdit] = []
        if pr.analog:
            for tip in ("clock hi (V)", "clock lo (V)", "data hi (V)", "data lo (V)"):
                e = QLineEdit()
                e.setPlaceholderText("auto")
                e.setToolTip(f"Schmitt threshold, {tip}; blank detects it from the trace")
                e.setFixedWidth(64)
                e.textChanged.connect(lambda _t: dlg._validate())
                self.thresh.append(e)
        self.remove = QPushButton("–")
        self.remove.setToolTip("Remove this Link")
        self.remove.setFixedWidth(28)
        self.remove.clicked.connect(lambda: dlg._remove_row(self))

    def _mark_named(self) -> None:
        self.name_edited = True
        # A typed name another row holds only by default moves that row on to the next
        # free "Link n", rather than reading as a clash the user did not make.
        self.dlg._renumber_defaults()
        self.dlg._validate()

    def widgets(self) -> list:
        return [self.name, self.clock, self.data, *self.thresh, self.remove]

    def pair(self) -> Tuple[int, int]:
        return self.clock.currentIndex(), self.data.currentIndex()

    def thresholds(self):
        """(clock (hi, lo) or None, data (hi, lo) or None); raises ValueError on a bad
        or half-filled pair."""
        if not self.thresh:
            return None, None
        vals = [e.text().strip() for e in self.thresh]

        def pair(hi: str, lo: str):
            if not hi and not lo:
                return None
            if not hi or not lo:
                raise ValueError("give both hi and lo, or neither")
            h, low = float(hi), float(lo)
            if h <= low:
                raise ValueError("hi must be above lo")
            return (h, low)
        return pair(vals[0], vals[1]), pair(vals[2], vals[3])


class OpenCaptureDialog(QDialog):
    """`result_request()` after exec() returns Accepted."""

    def __init__(self, probe: CaptureProbe, open_link_names: Optional[List[str]] = None,
                 parent=None, prefer_add: bool = False, budget_bytes: int = 0,
                 reference: bool = False, large_bytes: int = 0) -> None:
        """`reference` is Locate Sub-Capture's form: the file is searched for, not opened as
        a Link, so it has one row and no name, and no Into. `budget_bytes` is what refuses
        a load (memory_budget); a cost past `large_bytes` (large_load_bytes) is labelled
        large and still allowed, so the person deciding can see it."""
        super().__init__(parent)
        self.probe = probe
        self._reference = bool(reference)
        if self._reference:
            open_link_names = []
        self._open_names = list(open_link_names or [])
        self._budget = int(budget_bytes or 0)
        self._large = int(large_bytes or 0)
        self._rows: List[_LinkRow] = []
        self.setWindowTitle("Locate Sub-Capture: the reference" if self._reference
                            else "Open Capture")
        self.setStyleSheet(analyzer_stylesheet())
        lay = QVBoxLayout(self)

        # ---- Source ----
        src = QGroupBox("Source")
        sl = QVBoxLayout(src)
        # As many figures as the rate has (500 MHz, 98.304 MHz), not a fixed three decimals.
        rate = (f"{probe.sample_rate_hz / 1e6:.6g} MHz" if probe.sample_rate_hz else "rate —")
        sl.addWidget(QLabel(f"<b>{probe.name}</b> · {probe.format_label} · "
                            f"{len(probe.channels)} channels · {rate}"))
        self._source_line = QLabel()           # its cost follows the Links (_show_costs)
        sl.addWidget(self._source_line)
        self._rate_spin: Optional[QSpinBox] = None
        if probe.kind == "saleae_binary" and not probe.sample_rate_hz:
            # A .bin stores times, not a rate; when the times do not give it away, ask.
            row = QHBoxLayout()
            row.addWidget(QLabel("Sample rate (Hz), not found in the files:"))
            self._rate_spin = QSpinBox()
            self._rate_spin.setRange(1, 2_000_000_000)
            self._rate_spin.setSingleStep(1_000_000)
            self._rate_spin.setValue(98_304_000)
            row.addWidget(self._rate_spin)
            row.addStretch(1)
            sl.addLayout(row)
        lay.addWidget(src)

        # ---- Links ----
        links = QGroupBox("Signals" if self._reference else "Links")
        ll = QVBoxLayout(links)
        self._grid = QGridLayout()
        heads = ["Name", "Clock", "Data"]
        if probe.analog:
            heads += ["Clock hi", "Clock lo", "Data hi", "Data lo"]
        for c, h in enumerate(heads):
            head = QLabel(f"<b>{h}</b>")
            head.setVisible(not (self._reference and h == "Name"))
            self._grid.addWidget(head, 0, c)
        ll.addLayout(self._grid)
        self._add_row_btn = QPushButton("+ Add a Link from this file")
        self._add_row_btn.clicked.connect(self._add_row_from_free)
        ll.addWidget(self._add_row_btn, 0, Qt.AlignLeft)
        lay.addWidget(links)

        # ---- Decode ----
        dec = QGroupBox("Decode")
        dl = QGridLayout(dec)
        self._all = QRadioButton(f"All of it, {_dur(probe.duration_s)}")
        self._part = QRadioButton("From")
        grp = QButtonGroup(self)
        grp.addButton(self._all)
        grp.addButton(self._part)
        span = float(probe.duration_s or 0.0)
        self._t0 = _TimeSpin(span)
        self._t1 = _TimeSpin(span)
        # Set before connecting: an edit selects From/to and validates, which needs the
        # Into radios and the rows, built below.
        self._t1.set_seconds(span)
        for spin in (self._t0, self._t1):
            spin.valueChanged.connect(lambda _v: self._on_window_edited())
        self._window_line = QLabel()
        dl.addWidget(self._all, 0, 0, 1, 4)
        dl.addWidget(self._part, 1, 0)
        dl.addWidget(self._t0, 1, 1)
        dl.addWidget(QLabel("to"), 1, 2)
        dl.addWidget(self._t1, 1, 3)
        dl.addWidget(self._window_line, 1, 4)
        if not span:
            # Without a length there is nothing to place a window in.
            self._part.setEnabled(False)
            self._part.setToolTip("The capture's length could not be read before decoding.")
        self._all.toggled.connect(lambda _on: self._validate())
        lay.addWidget(dec)

        # ---- Into ----
        self._into = QGroupBox("Into")
        il = QHBoxLayout(self._into)
        self._replace = QRadioButton("Replace the open Links")
        self._add = QRadioButton("Add to them")
        g2 = QButtonGroup(self)
        g2.addButton(self._replace)
        g2.addButton(self._add)
        il.addWidget(self._replace)
        il.addWidget(self._add)
        il.addSpacing(16)
        self._offset_label = QLabel("Offset")
        self._offset = QDoubleSpinBox()
        self._offset.setDecimals(3)            # ms to the µs; Set Offset… takes ps
        self._offset.setRange(-1e6, 1e6)
        self._offset.setSuffix(" ms")
        self._offset.setToolTip("Where this file's time zero sits on the open Links' timeline. "
                                "Bookmarks ▸ Align Links on Bookmark Pair can set it from an "
                                "event later.")
        il.addWidget(self._offset_label)
        il.addWidget(self._offset)
        il.addStretch(1)
        self._into.setVisible(bool(self._open_names))
        (self._add if (prefer_add and self._open_names) else self._replace).setChecked(True)
        self._add.toggled.connect(lambda _on: self._on_into_changed())
        lay.addWidget(self._into)

        self._problem = QLabel()
        self._problem.setStyleSheet(f"color:{VizTheme.SEM_ERROR};")
        lay.addWidget(self._problem)
        self._buttons = QDialogButtonBox(QDialogButtonBox.Cancel)
        self._open_btn = self._buttons.addButton("Search" if self._reference else "Open",
                                                 QDialogButtonBox.AcceptRole)
        self._open_btn.setDefault(True)
        self._buttons.accepted.connect(self.accept)
        self._buttons.rejected.connect(self.reject)
        lay.addWidget(self._buttons)

        clk, dat = probe.busiest_pair()
        self._append_row(clk, dat)
        self._choose_default_range()
        self._on_into_changed()

    # ---- helpers ----
    def channel_text(self, ch) -> str:
        if ch.edges is None:
            return ch.label
        return f"{ch.label}  ({_edges(ch.edges, ch.approx)} edges)"

    def _cost(self, t0: Optional[float] = None, t1: Optional[float] = None) -> Optional[int]:
        """The predicted peak for what is asked: one Link's cost (the probe's) times the
        Links on the page, each loaded and kept."""
        one = self.probe.cost_bytes(t0, t1)
        return None if one is None else one * max(1, len(self._rows))

    def _show_costs(self) -> None:
        """The Source line's load-all cost and the window's, for the Links on the page."""
        n = max(1, len(self._rows))
        cost = self._cost()
        fits = (f" ({self._fit_word(cost)})" if self._fits(cost) else " (does not fit)") \
            if (cost is not None and self._budget) else ""
        how = "" if self.probe.windowed else " · reads the whole file"
        links = f" for {n} Links" if n > 1 else ""
        self._source_line.setText(f"Length {_dur(self.probe.duration_s)} · ~{_gb(cost)} to "
                                  f"load all{links}{fits}{how}")
        if self._part.isChecked():
            t0, t1 = self._t0.seconds(), self._t1.seconds()
            wcost = self._cost(t0, t1)
            note = "" if self.probe.windowed else ", reads the whole file"
            fits = (" — does not fit" if not self._fits(wcost)
                    else " — large, may be slow" if self._is_large(wcost) else "")
            self._window_line.setText(f"({_dur(max(0.0, t1 - t0))}, ~{_gb(wcost)}{links}"
                                      f"{note}{fits})")
        else:
            self._window_line.setText("")

    def _fits(self, cost: Optional[int]) -> bool:
        return cost is None or not self._budget or cost <= self._budget

    def _is_large(self, cost: Optional[int]) -> bool:
        return cost is not None and self._large > 0 and cost > self._large

    def _fit_word(self, cost: Optional[int]) -> str:
        return (f"fits; large, past {_gb(self._large)}, so opening may be slow"
                if self._is_large(cost) else "fits")

    def _choose_default_range(self) -> None:
        """All when it fits; else From/to on the largest window predicted to fit (a
        windowed read), starting at 0."""
        cost = self._cost()
        span = float(self.probe.duration_s or 0.0)
        if self._fits(cost) or not span or not self.probe.windowed:
            self._all.setChecked(True)
            return
        frac = max(0.0, min(1.0, self._budget / float(cost))) if cost else 1.0
        self._part.setChecked(True)
        self._t0.set_seconds(0.0)
        self._t1.set_seconds(max(min(span, 1e-3), span * frac))

    def _on_window_edited(self) -> None:
        if not self._part.isChecked():
            self._part.setChecked(True)
        self._validate()

    def _used(self, skip: Optional[_LinkRow] = None) -> set:
        out: set = set()
        for row in self._rows:
            if row is not skip:
                out.update(row.pair())
        return out

    def _append_row(self, clock: int, data: int) -> None:
        row = _LinkRow(self, clock, data)
        row.name.setVisible(not self._reference)
        self._rows.append(row)
        r = len(self._rows)
        for c, w in enumerate(row.widgets()):
            self._grid.addWidget(w, r, c)
        self._renumber_defaults()
        self._validate()

    def _add_row_from_free(self) -> None:
        """A Link from this file's unused channels, paired as the first row was (the
        busiest as clock, a quieter line as data: CaptureProbe.busiest_pair)."""
        free = [i for i in range(len(self.probe.channels)) if i not in self._used()]
        if len(free) < 2:
            return
        self._append_row(*self.probe.busiest_pair(among=free))

    def _remove_row(self, row: _LinkRow) -> None:
        if len(self._rows) <= 1:
            return
        self._rows.remove(row)
        for w in row.widgets():
            self._grid.removeWidget(w)
            w.setParent(None)
            w.deleteLater()
        # Close the gap: re-place the remaining rows under the header.
        for r, rw in enumerate(self._rows, start=1):
            for c, w in enumerate(rw.widgets()):
                self._grid.addWidget(w, r, c)
        self._renumber_defaults()
        self._validate()

    def _taken_names(self) -> List[str]:
        return self._open_names if self._add.isChecked() else []

    def _renumber_defaults(self) -> None:
        """Rows the user has not named get the next free "Link n" names (continuing the
        open Links' numbering when adding)."""
        taken = self._taken_names() + [r.name.text() for r in self._rows if r.name_edited]
        auto = [r for r in self._rows if not r.name_edited]
        for row, name in zip(auto, _default_names(taken, len(auto))):
            row.name.setText(name)

    def _on_into_changed(self) -> None:
        adding = self._add.isChecked() and bool(self._open_names)
        self._offset.setVisible(adding)
        self._offset_label.setVisible(adding)
        self._renumber_defaults()
        self._validate()

    def _validate(self) -> None:
        """Open only when every row names a fresh Link, on two distinct channels no other
        row uses, with readable thresholds and a window inside the capture."""
        problem = ""
        names = [r.name.text().strip() for r in self._rows]
        taken = set(self._taken_names())
        seen: set = set()
        used: set = set()
        for row, name in zip(self._rows, names):
            c, d = row.pair()
            if not name:
                problem = "Every Link needs a name."
            elif name in taken or name in seen:
                problem = f"“{name}” is already a Link's name."
            elif c == d:
                problem = f"{name}: the clock and data must be different channels."
            elif c in used or d in used:
                problem = f"{name}: a channel can belong to only one Link."
            else:
                try:
                    row.thresholds()
                except ValueError as exc:
                    problem = f"{name}: thresholds: {exc}."
            if problem:
                break
            seen.add(name)
            used.update((c, d))
        if not problem and self._part.isChecked():
            t0, t1 = self._t0.seconds(), self._t1.seconds()
            if t1 <= t0:
                problem = "The window must end after it starts."
        self._problem.setText(problem)
        self._open_btn.setEnabled(not problem)
        two = len(self.probe.channels) - len(used) >= 2 and not self.probe.two_file
        self._add_row_btn.setVisible(len(self.probe.channels) > 2 and not self.probe.two_file
                                     and not self._reference)
        self._add_row_btn.setEnabled(two)
        for row in self._rows:
            row.remove.setEnabled(len(self._rows) > 1)
            row.remove.setVisible(len(self._rows) > 1)
        self._show_costs()

    # ---- result ----
    def result_request(self) -> OpenRequest:
        req = OpenRequest(add=self._add.isChecked() and bool(self._open_names),
                          offset_s=self._offset.value() / 1e3)
        if self._part.isChecked():
            req.window_s = (self._t0.seconds(), self._t1.seconds())
        if self._rate_spin is not None:
            req.sample_rate_hz = int(self._rate_spin.value())
        two = len(self.probe.channels) == 2
        for row in self._rows:
            c, d = row.pair()
            ct, dt = row.thresholds()
            req.links.append(LinkRequest(
                name=row.name.text().strip(),
                clock=self.probe.channels[c].value, data=self.probe.channels[d].value,
                clock_thresh=ct, data_thresh=dt,
                auto_clock=two and row.pair() == row.default_pair))
        return req

    # For tests and for callers that drive the dialog without a screen.
    def rows(self) -> List[_LinkRow]:
        return list(self._rows)
