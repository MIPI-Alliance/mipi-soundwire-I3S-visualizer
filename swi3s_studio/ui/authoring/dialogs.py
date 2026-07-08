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
"""
from __future__ import annotations

from typing import List

from PySide6.QtCore import Qt
from PySide6.QtGui import QIntValidator
from PySide6.QtWidgets import (QCheckBox, QDialog, QDialogButtonBox, QFormLayout,
                               QGridLayout, QGroupBox, QHBoxLayout, QLabel,
                               QLineEdit, QPushButton, QSpinBox, QVBoxLayout, QWidget)

MANAGER = -1                              # device id for the Manager
_SELECTED = "#4CAF50"                     # green
_UNSELECTED = "#808080"                   # grey
_SEL_CSS = f"background:{_SELECTED}; color:white;"
_UNSEL_CSS = f"background:{_UNSELECTED}; color:white;"


def _toggle_btn(text: str) -> QPushButton:
    b = QPushButton(text)
    b.setFixedHeight(30)
    return b


class _OkCancel(QDialogButtonBox):
    def __init__(self, dlg: QDialog) -> None:
        super().__init__(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.accepted.connect(dlg.accept)
        self.rejected.connect(dlg.reject)


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
            b = _toggle_btn(text); b.setFixedWidth(120)
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

