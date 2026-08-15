"""Per-data-port editing dialogs — Qt ports of the SWI3S Visualizer's dialogs
(`src/ui/dialogs/*`). Each mirrors the original's title, buttons, layout, and the
green-selected / grey-unselected button styling, and exposes its result + a
`cancelled` flag after `exec()`.

- DeviceSelectorDialog   — Manager (M) or device 0-11 (radio)
- ChannelSelectorDialog  — toggle channels 0-15 (Clear / All)
- GuardSelectorDialog    — Guard 0 / Guard 1 / Off
- FlowModeSelectorDialog — Normal / Tx / Rx / Async + Flow Control Port params
- PortModeSelectorDialog — Normal (Off) / Ones / Zeros
- DisplayOptionsDialog   — Draw DataPort + Sample / Channel / Bit label fields
- CdsGuardDialog         — per-source (Manager + Device 0-11) Guard 0 / Guard 1 / Off
- CdsTailDialog          — per-source (Manager + Device 0-11) Tail Width 0-3
"""
from __future__ import annotations

from typing import List

from PySide6.QtCore import Qt
from PySide6.QtGui import QIntValidator
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ...model.bus_config import (
    CDS_DRIVE_NORMAL,
    CDS_DRIVE_SPECIAL,
    CDS_EDE_FULL_UI,
    CDS_EDE_HALF_EARLY,
)
from ..theme import VizTheme

MANAGER = -1                              # device id for the Manager
_SELECTED = "#4CAF50"                     # green
_UNSELECTED = "#808080"                   # grey
# ONE SELECTED COLOUR, AND "OFF" GETS IT TOO. A previous revision gave a selected
# "Off"/"0" its own blue-grey, on the reasoning that green reads as on/active and thirteen
# green "Off" buttons look like everything is enabled. That reasoning was wrong, and the
# cure was worse than the disease:
#
# GREEN HERE MEANS "THIS IS THE CHOSEN OPTION IN THIS GROUP", not "this feature is on".
# That is how every other selector in this file already uses it — GuardSelectorDialog shows
# a green "Off" for the very same Off/G0/G1 choice, PortModeSelectorDialog a green
# "Normal (Off)", DeviceSelectorDialog a green device number with no on/off sense at all.
# So the second colour did not add a distinction; it broke an existing convention, and it
# broke it INCONSISTENTLY — the per-source CDS guard dialog was blue-grey while the per-DP
# guard dialog one click away stayed green for the identical three options.
#
# The label carries the value ("Off" says off) and the colour carries the selection. Where a
# whole-bus summary is wanted, it belongs in TEXT: CDS Settings already shows "off" /
# "all Normal" / "all Full" beside each per-source row, which says what a highlight colour
# was being stretched to imply.
_BTN_BASE = "border:none; border-radius:8px; font-weight:500;"
_SEL_CSS = f"background:{_SELECTED}; color:white; {_BTN_BASE}"
_UNSEL_CSS = f"background:{_UNSELECTED}; color:white; {_BTN_BASE}"


class _ElidingLabel(QLabel):
    """A label that shortens its own text to fit, keeping the whole of it in the tooltip.

    For the per-source summaries, whose length is UNBOUNDED: "Manager tail 3; 12 devices
    tail 3" already needs more room than the dialog has, and a guard config with three
    polarities across twelve devices is longer still. So no fixed dialog width is safe, and
    the alternatives are both worse — letting the summary widen the window reintroduces the
    jumpy layout, and letting Qt squeeze the label truncates it with no indication that
    anything is missing (which is how "Enforce CDS Handc…" shipped).

    Eliding is honest: the ellipsis says there is more, the tooltip has it, and the dialog
    behind the button is the real source of detail anyway.
    """

    def __init__(self, text: str = "") -> None:
        super().__init__()
        self._full = text
        self.setMinimumWidth(40)      # never collapse to nothing under layout pressure

    def setText(self, text: str) -> None:      # noqa: N802 (Qt signature)
        self._full = text
        self.setToolTip(text)
        self._apply_elide()

    def full_text(self) -> str:
        """The unelided string — what a test should assert on, not the displayed text."""
        return self._full

    def resizeEvent(self, event) -> None:      # noqa: N802 (Qt signature)
        super().resizeEvent(event)
        self._apply_elide()

    def _apply_elide(self) -> None:
        super().setText(self.fontMetrics().elidedText(
            self._full, Qt.ElideRight, max(0, self.width() - 2)))


def _action_btn(text: str) -> QPushButton:
    """A rounded push button for an action ("Per source…"), as opposed to `_toggle_btn`'s
    pick-one-of-N. Styled because the platform default is a square bordered button, which
    next to the rounded toggles and the rounded OK/Cancel read as an unstyled leftover."""
    b = QPushButton(text)
    b.setFixedHeight(28)
    b.setStyleSheet(
        "QPushButton { border:none; border-radius:8px; padding:4px 12px;"
        " background:#6b6b6b; color:white; }"
        "QPushButton:hover { background:#7a7a7a; }")
    return b


def _toggle_btn(text: str) -> QPushButton:
    b = QPushButton(text)
    b.setFixedHeight(30)
    b.setStyleSheet(_UNSEL_CSS)          # rounded look even before a _refresh selects it
    return b


def _fit_title(dlg: QDialog) -> None:
    """Widen `dlg` so its window TITLE isn't truncated ('CDS Guard Sel…'). The title
    bar also holds the macOS traffic-light buttons, so allow for those plus margins on
    top of the measured title width. Only raises the minimum; never shrinks content."""
    fm = dlg.fontMetrics()
    dlg.setMinimumWidth(fm.horizontalAdvance(dlg.windowTitle()) + 150)


class _OkCancel(QDialogButtonBox):
    def __init__(self, dlg: QDialog) -> None:
        super().__init__(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.accepted.connect(dlg.accept)
        self.rejected.connect(dlg.reject)
        # Rounded buttons; the default (OK) gets the accent blue, like the original.
        self.setStyleSheet(
            "QPushButton { border:none; border-radius:8px; padding:6px 18px;"
            " background:#6b6b6b; color:white; }"
            "QPushButton:default { background:#2f6fd8; }"
            "QPushButton:hover { background:#7a7a7a; }"
            "QPushButton:default:hover { background:#3a7ee6; }")


class DeviceSelectorDialog(QDialog):
    def __init__(self, parent, initial_device: int = 0, dp_index: int = 0) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"DP{dp_index} Device Selection")
        self.device = initial_device
        self._btns: dict[int, QPushButton] = {}

        root = QVBoxLayout(self)
        root.addWidget(QLabel("Select Device:"))
        grid = QGridLayout()
        order = [MANAGER] + list(range(12))
        labels = {MANAGER: "M", **{i: str(i) for i in range(12)}}
        for n in order:
            b = _toggle_btn(labels[n])
            b.setFixedWidth(38)
            b.clicked.connect(lambda _c=False, d=n: self._select(d))
            self._btns[n] = b
        # Row 0: M + 0..6 ; Row 1: 7..11
        grid.addWidget(self._btns[MANAGER], 0, 0)
        for i in range(7):
            grid.addWidget(self._btns[i], 0, i + 1)
        for i in range(7, 12):
            grid.addWidget(self._btns[i], 1, i - 7)
        root.addLayout(grid)
        self._sel_label = QLabel()
        root.addWidget(self._sel_label)
        root.addWidget(_OkCancel(self))
        self._select(initial_device)

    def _select(self, d: int) -> None:
        self.device = d
        for n, b in self._btns.items():
            b.setStyleSheet(_SEL_CSS if n == d else _UNSEL_CSS)
        self._sel_label.setText("Selected: Manager" if d == MANAGER
                                else f"Selected: Device {d}")


class ChannelSelectorDialog(QDialog):
    def __init__(self, parent, initial_bitmask: int = 0, dp_index: int = 0) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"DP{dp_index} Channel Selection")
        self.result_mask = initial_bitmask
        self._btns: List[QPushButton] = []
        self._state = [bool(initial_bitmask & (1 << i)) for i in range(16)]

        root = QVBoxLayout(self)
        root.addWidget(QLabel("Channels 0-15:"))
        row = QHBoxLayout()
        for i in range(16):
            b = _toggle_btn(str(i)); b.setFixedWidth(28)
            b.clicked.connect(lambda _c=False, idx=i: self._toggle(idx))
            self._btns.append(b)
            row.addWidget(b)
        root.addLayout(row)
        self._count = QLabel()
        root.addWidget(self._count)
        actions = QHBoxLayout()
        clear = QPushButton("Clear"); clear.clicked.connect(self._clear)
        allb = QPushButton("All"); allb.clicked.connect(self._all)
        actions.addWidget(clear); actions.addWidget(allb)
        root.addLayout(actions)
        root.addWidget(_OkCancel(self))
        self._refresh()

    def _toggle(self, i: int) -> None:
        self._state[i] = not self._state[i]; self._refresh()

    def _clear(self) -> None:
        self._state = [False] * 16; self._refresh()

    def _all(self) -> None:
        self._state = [True] * 16; self._refresh()

    def _refresh(self) -> None:
        for i, b in enumerate(self._btns):
            b.setStyleSheet(_SEL_CSS if self._state[i] else _UNSEL_CSS)
        self._count.setText(f"Enabled: {sum(self._state)} channels")

    def accept(self) -> None:
        self.result_mask = sum((1 << i) for i, on in enumerate(self._state) if on)
        super().accept()


class GuardSelectorDialog(QDialog):
    def __init__(self, parent, guard_enabled: bool = False, guard_polarity: int = 0,
                 dp_index: int = 0, title: str | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(title or f"DP{dp_index} Guard Selection")
        self.guard_enabled = guard_enabled
        self.guard_polarity = guard_polarity
        self._sel = ("g1" if guard_polarity else "g0") if guard_enabled else "off"

        root = QVBoxLayout(self)
        root.addWidget(QLabel("Select Guard:"))
        row = QHBoxLayout()
        self._g0 = _toggle_btn("Guard 0"); self._g0.clicked.connect(lambda: self._pick("g0"))
        self._g1 = _toggle_btn("Guard 1"); self._g1.clicked.connect(lambda: self._pick("g1"))
        self._off = _toggle_btn("Off"); self._off.clicked.connect(lambda: self._pick("off"))
        for b in (self._g0, self._g1, self._off):
            row.addWidget(b)
        root.addLayout(row)
        root.addWidget(_OkCancel(self))
        self._refresh()
        _fit_title(self)

    def _pick(self, s: str) -> None:
        self._sel = s; self._refresh()

    def _refresh(self) -> None:
        self._g0.setStyleSheet(_SEL_CSS if self._sel == "g0" else _UNSEL_CSS)
        self._g1.setStyleSheet(_SEL_CSS if self._sel == "g1" else _UNSEL_CSS)
        self._off.setStyleSheet(_SEL_CSS if self._sel == "off" else _UNSEL_CSS)

    def accept(self) -> None:
        self.guard_enabled = self._sel in ("g0", "g1")
        self.guard_polarity = 1 if self._sel == "g1" else 0
        super().accept()


class FlowModeSelectorDialog(QDialog):
    _MODES = [(0, "Normal"), (1, "Tx Synchronous"), (2, "Rx Synchronous"),
              (3, "Asynchronous")]

    def __init__(self, parent, flow_mode: int = 0, dp_index: int = 0,
                 fcp_h_start: int = 4, fcp_bit_width: int = 0, fcp_tail_width: int = 0,
                 fcp_offset: int = 0, fcp_guard_enable: bool = False,
                 fcp_guard_polarity: int = 0) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"DP{dp_index} Flow Control")
        self.flow_mode = flow_mode
        self.fcp_h_start = fcp_h_start
        self.fcp_bit_width = fcp_bit_width
        self.fcp_tail_width = fcp_tail_width
        self.fcp_offset = fcp_offset
        self.fcp_guard_enable = fcp_guard_enable
        self.fcp_guard_polarity = fcp_guard_polarity

        root = QVBoxLayout(self)
        root.addWidget(QLabel("Flow Control Mode"))
        grid = QGridLayout()
        self._mode_btns: dict[int, QPushButton] = {}
        for i, (m, text) in enumerate(self._MODES):
            b = _toggle_btn(text); b.setFixedWidth(150)   # fit "Tx Synchronous" etc.
            b.clicked.connect(lambda _c=False, mm=m: self._select(mm))
            self._mode_btns[m] = b
            grid.addWidget(b, i // 2, i % 2)
        root.addLayout(grid)

        self._fcp_box = QGroupBox("Flow Control Port Parameters")
        form = QFormLayout(self._fcp_box)
        self._hs = self._int_entry(0, 31, fcp_h_start)
        self._bw = self._int_entry(0, 2, fcp_bit_width)
        self._tw = self._int_entry(0, 2, fcp_tail_width)
        self._off = self._int_entry(0, 4095, fcp_offset)
        self._ge = QCheckBox(); self._ge.setChecked(fcp_guard_enable)
        self._gp = self._int_entry(0, 1, fcp_guard_polarity)
        form.addRow("Horizontal Start (0-31)", self._hs)
        form.addRow("Bit Width (0-2)", self._bw)
        form.addRow("Tail Width (0-2)", self._tw)
        form.addRow("Offset (0-4095)", self._off)
        form.addRow("Guard Enable", self._ge)
        form.addRow("Guard Polarity", self._gp)
        root.addWidget(self._fcp_box)
        root.addWidget(_OkCancel(self))
        self._select(flow_mode)
        _fit_title(self)

    @staticmethod
    def _int_entry(lo: int, hi: int, val: int) -> QLineEdit:
        e = QLineEdit(str(val)); e.setFixedWidth(56); e.setAlignment(Qt.AlignCenter)
        e.setValidator(QIntValidator(lo, hi))
        return e

    def _select(self, m: int) -> None:
        self.flow_mode = m
        for mm, b in self._mode_btns.items():
            b.setStyleSheet(_SEL_CSS if mm == m else _UNSEL_CSS)
        # FCP params apply only to Rx (2) and Async (3).
        self._fcp_box.setEnabled(m in (2, 3))

    def accept(self) -> None:
        self.fcp_h_start = int(self._hs.text() or 0)
        self.fcp_bit_width = int(self._bw.text() or 0)
        self.fcp_tail_width = int(self._tw.text() or 0)
        self.fcp_offset = int(self._off.text() or 0)
        self.fcp_guard_enable = self._ge.isChecked()
        self.fcp_guard_polarity = int(self._gp.text() or 0)
        super().accept()


class PortModeSelectorDialog(QDialog):
    _MODES = [(0, "Normal (Off)"), (2, "Ones"), (3, "Zeros")]

    def __init__(self, parent, port_mode: int = 0, dp_index: int = 0) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"DP{dp_index} Port Test Mode")
        self.port_mode = port_mode if port_mode != 1 else 0
        self._btns: dict[int, QPushButton] = {}

        root = QVBoxLayout(self)
        root.addWidget(QLabel("Port Test Mode"))
        row = QHBoxLayout()
        for m, text in self._MODES:
            b = _toggle_btn(text); b.setFixedWidth(110)
            b.clicked.connect(lambda _c=False, mm=m: self._select(mm))
            self._btns[m] = b
            row.addWidget(b)
        root.addLayout(row)
        root.addWidget(_OkCancel(self))
        self._select(self.port_mode)
        _fit_title(self)

    def _select(self, m: int) -> None:
        self.port_mode = m
        for mm, b in self._btns.items():
            b.setStyleSheet(_SEL_CSS if mm == m else _UNSEL_CSS)


class DisplayOptionsDialog(QDialog):
    SAMPLE, CHANNEL, BIT = 1, 2, 4

    def __init__(self, parent, enabled: bool = False, display_fields: int = 6,
                 dp_index: int = 0) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"DP{dp_index} Display Options")
        self.enabled = enabled
        self.display_fields = display_fields

        root = QVBoxLayout(self)
        self._draw = QCheckBox("Draw DataPort"); self._draw.setChecked(enabled)
        root.addWidget(self._draw)
        root.addWidget(QLabel("Show Fields:"))
        row = QHBoxLayout()
        self._s = _toggle_btn("Sample (s)"); self._s.clicked.connect(lambda: self._toggle(self.SAMPLE))
        self._c = _toggle_btn("Channel (c)"); self._c.clicked.connect(lambda: self._toggle(self.CHANNEL))
        self._b = _toggle_btn("Bit (b)"); self._b.clicked.connect(lambda: self._toggle(self.BIT))
        for w in (self._s, self._c, self._b):
            row.addWidget(w)
        root.addLayout(row)
        self._preview = QLabel()
        root.addWidget(self._preview)
        root.addWidget(_OkCancel(self))
        self._refresh()

    def _toggle(self, bit: int) -> None:
        self.display_fields ^= bit
        self._refresh()

    def _refresh(self) -> None:
        self._s.setStyleSheet(_SEL_CSS if self.display_fields & self.SAMPLE else _UNSEL_CSS)
        self._c.setStyleSheet(_SEL_CSS if self.display_fields & self.CHANNEL else _UNSEL_CSS)
        self._b.setStyleSheet(_SEL_CSS if self.display_fields & self.BIT else _UNSEL_CSS)
        parts = []
        if self.display_fields & self.SAMPLE: parts.append("S0")
        if self.display_fields & self.CHANNEL: parts.append("C0")
        if self.display_fields & self.BIT: parts.append("B0")
        self._preview.setText(f"Preview: {''.join(parts) or '(no label)'}")

    def accept(self) -> None:
        self.enabled = self._draw.isChecked()
        super().accept()


class GridLabelFieldsDialog(QDialog):
    """Pick which fields a decoded Bus Grid cell's label shows, for ONE data port.

    The Analyzer sibling of DisplayOptionsDialog: same Sample/Channel/Bit toggles
    and the same flag values, minus "Draw DataPort" — a decoded port is drawn
    because it's on the wire, not because a config says to. Opened by clicking the
    port's swatch in the grid's colour key.

    The preview shows the port's ACTUAL first cell where the caller can supply one,
    so "S0C0B15" is the real label rather than a generic sample.
    """
    SAMPLE, CHANNEL, BIT = 1, 2, 4

    def __init__(self, parent, title: str, fields: int = 6,
                 sample: int = 0, channel: int = 0, bit: int = 0,
                 apply_all: bool = False) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.fields = int(fields) & 7
        self.apply_to_all = False
        self._vals = (int(sample), int(channel), int(bit))

        root = QVBoxLayout(self)
        root.addWidget(QLabel("Show Fields:"))
        row = QHBoxLayout()
        self._s = _toggle_btn("Sample (s)")
        self._s.clicked.connect(lambda: self._toggle(self.SAMPLE))
        self._c = _toggle_btn("Channel (c)")
        self._c.clicked.connect(lambda: self._toggle(self.CHANNEL))
        self._b = _toggle_btn("Bit (b)")
        self._b.clicked.connect(lambda: self._toggle(self.BIT))
        for w in (self._s, self._c, self._b):
            row.addWidget(w)
        root.addLayout(row)
        self._preview = QLabel()
        root.addWidget(self._preview)
        self._all = None
        if apply_all:
            self._all = QCheckBox("Apply to every data port")
            root.addWidget(self._all)
        root.addWidget(_OkCancel(self))
        self._refresh()

    def _toggle(self, bit: int) -> None:
        self.fields ^= bit
        self._refresh()

    def _refresh(self) -> None:
        self._s.setStyleSheet(_SEL_CSS if self.fields & self.SAMPLE else _UNSEL_CSS)
        self._c.setStyleSheet(_SEL_CSS if self.fields & self.CHANNEL else _UNSEL_CSS)
        self._b.setStyleSheet(_SEL_CSS if self.fields & self.BIT else _UNSEL_CSS)
        smp, ch, bit = self._vals
        parts = []
        if self.fields & self.SAMPLE:
            parts.append(f"S{smp}")
        if self.fields & self.CHANNEL:
            parts.append(f"C{ch}")
        if self.fields & self.BIT:
            parts.append(f"B{bit}")
        self._preview.setText(f"Preview: {''.join(parts) or '(no label)'}")

    def accept(self) -> None:
        self.apply_to_all = bool(self._all and self._all.isChecked())
        super().accept()


class DataPortIdentityDialog(QDialog):
    """Edit a data port's logical number (0-31) and display name. Opened from the
    DP name chip in the authoring table. The (device, number) uniqueness rule is
    enforced by the caller (a device may not reuse a number; the same number is
    fine across devices)."""

    def __init__(self, parent, dp_number: int = 0, name: str = "",
                 dp_index: int = 0) -> None:
        super().__init__(parent)
        self.setWindowTitle("Data Port Number & Name")
        self.dp_number = dp_number
        self.name = name

        root = QVBoxLayout(self)
        form = QFormLayout()
        self._num = QSpinBox()
        self._num.setRange(0, 31)
        self._num.setValue(max(0, min(31, dp_number)))
        form.addRow("Data port number (0-31):", self._num)
        # Show the resolved default name (e.g. "DP0") as normal editable text rather
        # than a greyed-out placeholder, so the field always reads as a real value.
        self._name = QLineEdit(name or f"DP{dp_number}")
        form.addRow("Name:", self._name)
        root.addLayout(form)
        root.addWidget(_OkCancel(self))

    def accept(self) -> None:
        self.dp_number = self._num.value()
        name = self._name.text().strip()
        # If left at the resolved default ("DP{number}"), store empty so the name
        # keeps auto-following the number rather than pinning a literal default.
        self.name = "" if name == f"DP{self.dp_number}" else name
        super().accept()


# Source labels shared by the two per-source CDS dialogs below: index 0 = Manager,
# index i = Device (i-1), matching BusConfig.cds_guard/cds_tail's convention.
_CDS_SOURCE_LABELS = ["Manager"] + [f"Device {i}" for i in range(12)]


class _CdsPerSourceDialog(QDialog):
    """One pick-one-of-N row per CDS source (Manager + Device 0-11).

    ONE BASE FOR THREE DIALOGS. Guard, tail width and drive type are the same control:
    thirteen rows, one row of toggles each, differing only in the option list. They were
    two hand-written copies before drive type made it three, and every layout defect below
    existed in both copies — so the fixes are made once, here.

    WHAT WAS WRONG, all of it visible only by rendering the dialog and looking at it:

      * THE VERTICAL SCROLLBAR SAT ON TOP OF THE LAST COLUMN. The scroll area was sized to
        its contents, leaving no room for its own scrollbar, so the bar overlapped the
        right-hand button ("Guard 1", tail "3"). Width now reserves the scrollbar extent.
      * A HORIZONTAL SCROLLBAR APPEARED for the same reason, over the bottom row, scrolling
        content that already fitted. Disabled outright — the rows are fixed-width.
      * THE LAST ROW WAS CUT THROUGH THE MIDDLE, because the height was a flat 320 px with
        no relation to the row pitch. The visible height is now a whole number of rows.
      * (A fifth defect was reported here and turned out not to be one: a selected "Off"
        rendering in the same green as a selected setting. Green marks the CHOSEN option in
        a group throughout this file — GuardSelectorDialog has always shown a green "Off"
        for the same Off/G0/G1 choice — so giving it a second colour broke a convention
        rather than clarifying one. See the note on `_SEL_CSS`.)

    AND ONE UX GAP: thirteen sources x three or four options is up to fifty-two clicks for
    a config that is almost always uniform, with no way to say "all of them". An "All
    sources" strip at the top sets every row at once, so the common case is one click and
    the per-row grid is for the exceptions.

    Subclasses declare `TITLE`, `PROMPT`, `OPTIONS` ((value, label) pairs) and `BTN_W`;
    the accepted result is `self.values`.
    """

    TITLE = ""
    PROMPT = ""
    OPTIONS: tuple = ()
    BTN_W = 76
    # Optional per-value tooltip, for a field whose LABEL deliberately omits the detail
    # (see CdsEndDriveEarlyDialog, whose values name a behaviour and not a duration).
    TOOLTIPS: dict = {}

    def __init__(self, parent, values: List[int]) -> None:
        super().__init__(parent)
        self.setWindowTitle(self.TITLE)
        self.values = list(values)
        self._rows: List[dict] = []

        root = QVBoxLayout(self)
        root.addWidget(QLabel(self.PROMPT))

        # ONE GRID, NO SCROLL AREA. Thirteen rows at ~34 px is a ~560 px dialog, which
        # fits any display this app runs on — and the scroll area it replaces was the
        # direct cause of three separate defects: its bar drew over the last button
        # column, a horizontal bar appeared over the bottom row scrolling content that
        # already fitted, and its flat 320 px height cut the last visible row in half.
        # A fourth was alignment: the "All sources" strip sat outside the scrolled grid,
        # so the two never lined up. In one grid they cannot disagree.
        grid = QGridLayout()
        grid.setHorizontalSpacing(6)
        grid.setVerticalSpacing(4)

        # "All sources" FIRST, because it is what the common (uniform) config wants —
        # thirteen sources x three or four options is up to 52 clicks otherwise, with no
        # way to say "all of them". Rendered as action buttons, not toggles: it is a verb
        # ("set every row to this"), not a state, and nothing about it stays selected.
        all_lab = QLabel("All sources")
        all_lab.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        grid.addWidget(all_lab, 0, 0)
        for col, (value, text) in enumerate(self.OPTIONS, start=1):
            b = _action_btn(text)
            b.setFixedWidth(self.BTN_W)
            b.setToolTip(self.TOOLTIPS.get(value, ""))
            b.clicked.connect(lambda _c=False, v=value: self._set_all(v))
            grid.addWidget(b, 0, col)
        grid.addWidget(_hline(), 1, 0, 1, len(self.OPTIONS) + 1)

        for i, label in enumerate(_CDS_SOURCE_LABELS):
            lab = QLabel(label)
            lab.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            grid.addWidget(lab, i + 2, 0)
            btns = {}
            for col, (value, text) in enumerate(self.OPTIONS, start=1):
                b = _toggle_btn(text)
                b.setFixedWidth(self.BTN_W)
                b.setToolTip(self.TOOLTIPS.get(value, ""))
                b.clicked.connect(lambda _c=False, r=i, v=value: self._pick(r, v))
                grid.addWidget(b, i + 2, col)
                btns[value] = b
            self._rows.append(btns)

        root.addLayout(grid)
        root.addStretch(1)
        root.addWidget(_OkCancel(self))
        self._refresh()
        self.adjustSize()
        self.setFixedWidth(max(self.sizeHint().width(), _title_width(self)))

    def _pick(self, row: int, value: int) -> None:
        self.values[row] = value
        self._refresh()

    def _set_all(self, value: int) -> None:
        self.values = [value] * len(self.values)
        self._refresh()

    def _refresh(self) -> None:
        for row, btns in enumerate(self._rows):
            current = self.values[row]
            for value, b in btns.items():
                b.setStyleSheet(_SEL_CSS if value == current else _UNSEL_CSS)


class CdsGuardDialog(_CdsPerSourceDialog):
    """Per-source CDS guard (Off / G0 / G1). The CDS is time-multiplexed, so the Manager
    and each of devices 0-11 drive their own guard polarity without a bus clash.

    `self.guard` is kept as an alias of `self.values` so existing callers are unchanged."""

    TITLE = "CDS Guard Selection"
    PROMPT = "Select Guard per source:"
    OPTIONS = ((0, "Off"), (1, "Guard 0"), (2, "Guard 1"))
    BTN_W = 76

    @property
    def guard(self) -> List[int]:
        return self.values

    @guard.setter
    def guard(self, v: List[int]) -> None:
        self.values = list(v)


class CdsTailDialog(_CdsPerSourceDialog):
    """Per-source CDS tail width (0-3 UI). `self.tail` aliases `self.values`."""

    TITLE = "CDS Tail Width"
    PROMPT = "Select Tail Width per source:"
    OPTIONS = ((0, "0"), (1, "1"), (2, "2"), (3, "3"))
    BTN_W = 34

    @property
    def tail(self) -> List[int]:
        return self.values

    @tail.setter
    def tail(self, v: List[int]) -> None:
        self.values = list(v)


class CdsDriveTypeDialog(_CdsPerSourceDialog):
    """Per-source CDS_DriveType (registers.json CDS 0x86 bit 7).

    Per source because the register is in each device's own CDS block — the CDS is
    time-multiplexed and each source drives it under its own configuration, so one device
    may hold a passive 1 while another drives both levels.

    Normal and Special are two ways of driving, not a feature being on or off. The button
    text leads with the register value, since that is what a reader programs."""

    TITLE = "CDS Drive Type"
    PROMPT = "Select Drive Type per source:"
    OPTIONS = ((CDS_DRIVE_NORMAL, "1 · Normal"), (CDS_DRIVE_SPECIAL, "0 · Special"))
    BTN_W = 96

    @property
    def drive_type(self) -> List[int]:
        return self.values

    @drive_type.setter
    def drive_type(self, v: List[int]) -> None:
        self.values = list(v)


class CdsEndDriveEarlyDialog(_CdsPerSourceDialog):
    """Per-source CDS_EndDriveEarly (CDS block 0x87 bit 4 _NEXT / 0xC7 _CURR).

    Spec v1.1 r08, Table 176, verbatim:

        0: Drive to the end of the UI.
        1: Stop driving before the end of the last UI.

    Per source because the register is in each device's own CDS block. PHY3 mandatory,
    PHY2 optional, PHY1 read-only 0 — PHY1 needs no handover UIs at all, its own output
    timing already satisfying t_ZD,min >= t_DZ,max.

    NO DURATION IN THE LABEL, which is why they are bare verbs. An earlier version read
    "1/2 UI early", carried over from the r06 extract; r08 deleted that idea deliberately —
    its revision history records "remove the idea of the time being explicitly a 'half UI'",
    the granularity is now BITS rather than UIs (Sec. 10.1.14, "Scope of
    EndDriveEarly"), and the amount is ImpDef
    inside a PHY-specific bound (PHY2: from Per_tZD_Actual + Per_tRampTime_Actual + 3.0 ns to
    UI - 2.0 ns, Table 132). A label naming a duration the spec no longer guarantees is worse
    than one naming none. `data/registers.json` has since been re-extracted to r08 and agrees.

    Full is the ordinary behaviour and Early is the feature being turned on, but both are
    just the chosen option here — the summary in CDS Settings ("All Full" / "All Early" /
    "2 devices Early") is where the bus-wide state is stated, not the button colour."""

    TITLE = "CDS End Drive Early"
    PROMPT = "Select End Drive Early per source:"
    # "Full" / "Early" — the behaviour, not a duration. See the class docstring.
    OPTIONS = ((CDS_EDE_FULL_UI, "Full"), (CDS_EDE_HALF_EARLY, "Early"))
    BTN_W = 76
    # The spec's own encoding, on each button's tooltip — the labels say nothing about WHEN
    # the drive stops, so this is where a reader finds out.
    TOOLTIPS = {
        CDS_EDE_FULL_UI: "0: Drive to the end of the UI",
        CDS_EDE_HALF_EARLY: "1: Stop driving before the end of the last UI\n"
                            "(exact point is PHY-specific and ImpDef)",
    }


# CDS Settings' per-source rows: summary key -> the attribute holding the list, and ->
# the dialog that edits it. Two small tables rather than a dict literal repeated in three
# methods; adding a per-source field means one line in each.
_CDS_SETTINGS_KINDS = {
    "drive": "drive_type",
    "ede": "end_drive_early",
    "guard": "guard",
    "tail": "tail",
}
_CDS_SETTINGS_DIALOGS = {
    "drive": CdsDriveTypeDialog,
    "ede": CdsEndDriveEarlyDialog,
    "guard": CdsGuardDialog,
    "tail": CdsTailDialog,
}


class CdsSettingsDialog(QDialog):
    """Every CDS setting behind one entry point.

    The interface column had grown a CDS row per setting — bit width, guard, tail,
    handover — each a different control shape, and drive type would have been the fifth.
    They belong together (one register block, one region of the frame), so the panel now
    carries a single "CDS Settings" row that opens this, and the per-source dialogs open
    FROM here rather than from their own panel rows.

    TWO SCALARS AND THREE PER-SOURCE LISTS, laid out in that order and separated. Bit width
    and handover are bus-wide; guard, tail and drive type each live in a device's own CDS
    block, so they get a "Per source…" button and a summary of the current state. Grouping
    them makes the shape of the register block legible instead of five unrelated rows.

    NOTHING IS COMMITTED UNTIL OK, including a change made two dialogs deep: the per-source
    dialogs edit copies handed back through `guard` / `tail` / `drive_type`, so cancelling
    the outer dialog discards them.

    ONE GRID, TWO COLUMNS, FIXED WIDTH. The first version used a QFormLayout with
    `addRow("", w)` for the note and the checkbox, which parked both in the narrow VALUE
    column: the note wrapped to three lines with half the dialog empty beside it, and the
    checkbox's own text ran off the right edge and was CLIPPED ("Enforce CDS Handc…"). The
    dialog also re-widened when the note changed length, so picking a value made the window
    jump. Now every label is right-aligned in column 0, every control sits in column 1, the
    note and dividers SPAN both, and the width is fixed — nothing moves, nothing is cut off.

    On accept: `bit_width`, `enforce_handover`, `guard`, `tail`, `drive_type`.
    """

    # Fixed, so no selection can resize the window. Wide enough for the footnote at two
    # lines; test_cds_settings_dialog_fits_its_content measures the real content against it
    # rather than trusting the number to stay right as wording changes.
    _WIDTH = 470

    def __init__(self, parent, *, bit_width: int, drive_type: List[int],
                 enforce_handover: bool, guard: List[int], tail: List[int],
                 end_drive_early: List[int]) -> None:
        super().__init__(parent)
        self.setWindowTitle("CDS Settings")
        self.bit_width = int(bit_width)
        self.enforce_handover = bool(enforce_handover)
        self.guard = list(guard)
        self.tail = list(tail)
        self.drive_type = list(drive_type)
        self.end_drive_early = list(end_drive_early)

        root = QVBoxLayout(self)
        grid = QGridLayout()
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(8)
        grid.setColumnStretch(1, 1)
        self._grid = grid
        self._row = 0

        # ---- bus-wide ----
        self._add_label("Bit Width")
        self._bit: dict = {}
        grid.addWidget(_toggle_strip(range(8), str, self._pick_bit, self._bit, 30),
                       self._row, 1)
        self._row += 1

        # "Enforce Handover", not "Enforce CDS Handover": the window is titled CDS Settings
        # and every other row here drops the prefix (Bit Width, Drive Type, Guard, …), so
        # keeping it on one row made that row look like it meant something different.
        self._add_label("Enforce Handover")
        self._handover = QCheckBox()
        self._handover.setChecked(self.enforce_handover)
        # LEFT-ALIGNED, AND THE INDICATOR TOO. Two separate things had to be said:
        #
        # `alignment=Qt.AlignLeft` places the WIDGET, because column 1 carries the stretch
        # and a small widget dropped into it centres itself in the whole column.
        #
        # The stylesheet override places the INDICATOR inside that widget. The authoring
        # panel sets `QCheckBox::indicator { subcontrol-position: center }` on purpose — its
        # own bool rows centre a checkbox inside a fixed 64 px band — and this dialog
        # inherits that rule from its parent. Centring makes the drawn position depend on
        # the widget's width, so with a different font the indicator drifts off the column
        # edge that every other control lines up on. Pinning it left removes the dependency
        # instead of tuning a number. Measuring the WIDGET, as the first test did, cannot
        # see this: the widget was aligned and the visible square was not.
        self._handover.setStyleSheet(
            "QCheckBox::indicator { subcontrol-position: left center; }")
        grid.addWidget(self._handover, self._row, 1, alignment=Qt.AlignLeft)
        self._row += 1

        self._span(_hline())

        # ---- per source (Manager + Device 0-11) ----
        # Drive type first: it is the one whose consequence the footnote explains.
        self._summaries: dict = {}
        for label, kind in (("Drive Type", "drive"), ("End Drive Early", "ede"),
                            ("Guard", "guard"), ("Tail Width", "tail")):
            self._add_label(label)
            cell = QHBoxLayout()
            cell.setContentsMargins(0, 0, 0, 0)
            cell.setSpacing(10)
            btn = _action_btn("Per source…")
            btn.setFixedWidth(110)
            btn.clicked.connect(lambda _c=False, k=kind: self._open_per_source(k))
            summary = _ElidingLabel()
            summary.setStyleSheet(f"color:{VizTheme.TEXT_DIM};")
            cell.addWidget(btn)
            cell.addWidget(summary, 1)
            host = QWidget()
            host.setLayout(cell)
            grid.addWidget(host, self._row, 1)
            self._summaries[kind] = summary
            self._row += 1

        root.addLayout(grid)
        root.addStretch(1)
        root.addWidget(_OkCancel(self))
        self._refresh()
        self.setFixedWidth(max(self._WIDTH, _title_width(self)))

    def _add_label(self, text: str) -> None:
        lab = QLabel(text)
        lab.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self._grid.addWidget(lab, self._row, 0)

    def _span(self, w: QWidget) -> None:
        """Add a widget across both columns — dividers and the footnote, which have no
        label and must not be squeezed into the value column."""
        self._grid.addWidget(w, self._row, 0, 1, 2)
        self._row += 1

    def _pick_bit(self, value: int) -> None:
        self.bit_width = int(value)
        self._refresh()

    def _open_per_source(self, kind: str) -> None:
        attr = _CDS_SETTINGS_KINDS[kind]
        d = _CDS_SETTINGS_DIALOGS[kind](self, list(getattr(self, attr)))
        if d.exec():
            setattr(self, attr, list(d.values))
        self._refresh()

    def _refresh(self) -> None:
        for value, btn in self._bit.items():
            btn.setStyleSheet(_SEL_CSS if value == self.bit_width else _UNSEL_CSS)
        self._bit[self.bit_width].setToolTip(
            f"Register value {self.bit_width} — excess-1, so "
            f"{self.bit_width + 1} UI per CDS bit")
        for kind, attr in _CDS_SETTINGS_KINDS.items():
            self._summaries[kind].setText(cds_summary(getattr(self, attr), kind))

    def accept(self) -> None:
        self.enforce_handover = bool(self._handover.isChecked())
        super().accept()


def _toggle_strip(values, text_of, on_pick, store: dict, width: int) -> QWidget:
    """A left-packed row of pick-one-of-N toggles, registered in `store` by value."""
    box = QHBoxLayout()
    box.setContentsMargins(0, 0, 0, 0)
    box.setSpacing(4)
    for value in values:
        b = _toggle_btn(text_of(value))
        b.setFixedWidth(width)
        b.clicked.connect(lambda _c=False, v=value: on_pick(v))
        box.addWidget(b)
        store[value] = b
    box.addStretch(1)
    host = QWidget()
    host.setLayout(box)
    return host


def _title_width(dlg: QDialog) -> int:
    """Never narrower than the window title, whose bar also holds the traffic lights."""
    return dlg.fontMetrics().horizontalAdvance(dlg.windowTitle()) + 150


def _hline() -> QFrame:
    """A divider in the THEME's border colour, not a hardcoded grey — a literal here stays
    dark when the app is switched to the Light theme."""
    line = QFrame()
    line.setFrameShape(QFrame.HLine)
    line.setStyleSheet(f"color:{VizTheme.BORDER};")
    return line


# kind -> (the value worth naming, all-default text, all-odd text, per-source suffix).
# For fields whose two values are "ordinary" and "notable"; see `cds_summary`.
_EXCEPTION_SUMMARY = {
    "drive": (CDS_DRIVE_SPECIAL, "all Normal", "all Special", " Special"),
    "ede": (CDS_EDE_HALF_EARLY, "All Full", "All Early", " Early"),
}


def cds_summary(values: List[int], kind: str) -> str:
    """One-line summary of a per-source CDS list, e.g. 'Manager G0; 2 devices G1'.

    Lives here beside the dialogs that render it rather than on the panel, because the
    panel's row tooltip and the settings dialog's inline summaries show the same string and
    a second copy would drift.

    DRIVE TYPE CANNOT USE THE any() SHORTCUT the guard and tail share. Guard and tail encode
    "not set" as 0, so an all-zero list means off; drive type encodes SPECIAL as 0, so the
    same test would report a bus where every source holds a passive one as "off" — the exact
    opposite of the truth. End-drive-early is the other way round (0 IS its ordinary value),
    which is exactly why both go through a declared table rather than a zero test.
    """
    # TWO-VALUED FIELDS REPORT THE EXCEPTION against their ordinary value, rather than
    # listing thirteen sources. Both of them read the "interesting" state as the one worth
    # naming — Special for the drive type, stopping early for end-drive-early.
    if kind in _EXCEPTION_SUMMARY:
        odd_value, all_default, all_odd, suffix = _EXCEPTION_SUMMARY[kind]
        odd = [i for i, v in enumerate(values) if int(v) == odd_value]
        if not odd:
            return all_default
        if len(odd) == len(values):
            return all_odd
        parts = []
        if 0 in odd:
            parts.append("Manager")
        devs = [i - 1 for i in odd if i > 0]
        if devs:
            parts.append(f"{len(devs)} device" + ("s" if len(devs) > 1 else ""))
        return " + ".join(parts) + suffix

    if not any(values):
        return "off"
    if kind == "guard":
        def fmt(v):
            return "G0" if v == 1 else "G1"
    else:
        def fmt(v):
            return f"tail {v}"
    groups: dict[int, List[int]] = {}
    parts = []
    if values[0]:
        parts.append(f"Manager {fmt(values[0])}")
    for i, v in enumerate(values[1:], start=1):
        if v:
            groups.setdefault(v, []).append(i - 1)
    for v, devs in groups.items():
        noun = "device" if len(devs) == 1 else "devices"
        parts.append(f"{len(devs)} {noun} {fmt(v)}")
    return "; ".join(parts) if parts else "off"
