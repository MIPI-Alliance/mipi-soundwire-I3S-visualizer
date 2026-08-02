"""Devices dialog — per-device name, register map, and hub depth.

One row per device (0–11). Lets the user name each device, assign/import/clear
its peripheral register map, and set its hub depth (0–5). Hub depth feeds the
decoder: a peripheral N hub levels deep has its response delayed 2·N frame rows.
This is the single place these per-device settings live (the old standalone
register-map import menu folds in here).
"""

from __future__ import annotations

from typing import Dict

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from .theme import VizTheme

MAX_HUB_DEPTH = 5

# The per-device aspects this dialog can edit. The File ▸ Devices menu opens it
# focused on ONE aspect (Names / Register Maps / Hub Depth); the full set is used
# when everything is edited together.
ALL_SECTIONS = ("names", "maps", "depths")
_SECTION_TITLE = {"names": "Name", "maps": "Register Map", "depths": "Hub Depth"}


class DevicesDialog(QDialog):
    """Edit per-device name / register map / hub depth.

    `sections` selects which aspects to show (a subset of ALL_SECTIONS); the
    hidden aspects keep the values passed in. On accept, read results from
    `.names` ({dev: str}), `.hub_depths` ({dev: int}), and `.regmaps`
    ({dev: PeripheralRegisterMap})."""

    def __init__(self, parent, devices, names: Dict[int, str],
                 hub_depths: Dict[int, int], regmaps: Dict[int, object],
                 sections=ALL_SECTIONS) -> None:
        super().__init__(parent)
        self._sections = tuple(s for s in ALL_SECTIONS if s in sections) or ALL_SECTIONS
        # Title the dialog by what it edits: the single-aspect openers read as
        # "Device Names" / "Device Register Maps" / "Device Hub Depth".
        self.setWindowTitle("Devices" if len(self._sections) > 1
                            else f"Device {_SECTION_TITLE[self._sections[0]]}s")
        self.setMinimumWidth(560 if "maps" in self._sections else 360)
        self._devices = list(devices)
        self.names: Dict[int, str] = dict(names)
        self.hub_depths: Dict[int, int] = dict(hub_depths)
        self.regmaps: Dict[int, object] = dict(regmaps)

        self._name_edits: Dict[int, QLineEdit] = {}
        self._depth_spins: Dict[int, QSpinBox] = {}
        self._map_labels: Dict[int, QLabel] = {}

        root = QVBoxLayout(self)
        # A one-line blurb only where it adds something: the Names / Register Map
        # dialogs are self-explanatory in context, so they get none; Hub Depth explains
        # what the number does; the combined dialog keeps a short header.
        blurb = ""
        if self._sections == ("depths",):
            blurb = "Expected device responses delayed by 2 rows per hub."
        elif len(self._sections) > 1:
            blurb = "Per-device settings (re-decodes on apply)."
        if blurb:
            root.addWidget(QLabel(blurb))

        # Scrollable grid (up to 12 device rows). Use the app's grey border +
        # rounded corners (not Qt's default dark engraved frame).
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet(
            f"QScrollArea {{ border: {VizTheme.BORDER_WIDTH}px solid {VizTheme.BORDER}; "
            f"border-radius: {VizTheme.CORNER_RADIUS}px; }}")
        host = QWidget()
        grid = QGridLayout(host)
        # Column layout is dynamic: "Device" then one column per shown section.
        headers = ["Device"] + [_SECTION_TITLE[s] for s in self._sections]
        col_of = {s: 1 + i for i, s in enumerate(self._sections)}
        if "names" in col_of:
            grid.setColumnStretch(col_of["names"], 1)
        if "maps" in col_of:
            grid.setColumnStretch(col_of["maps"], 2)
        for c, title in enumerate(headers):
            lab = QLabel(title)
            lab.setStyleSheet("font-weight: bold;")
            lab.setAlignment(Qt.AlignCenter)             # centred over its section
            grid.addWidget(lab, 0, c)

        for r, dev in enumerate(self._devices, start=1):
            dev_lbl = QLabel(f"Device {dev}")
            dev_lbl.setAlignment(Qt.AlignCenter)
            grid.addWidget(dev_lbl, r, 0)

            if "names" in col_of:
                name = QLineEdit(self.names.get(dev, ""))
                name.setPlaceholderText(f"Device {dev}")
                self._name_edits[dev] = name
                grid.addWidget(name, r, col_of["names"])

            if "maps" in col_of:
                # Register map controls grouped in a grey, slightly-rounded box (a small
                # radius suits this short row) so the label + Set…/Clear read as one
                # "Register Map" control — matching the app's border colour, not black.
                cell = QFrame()
                cell.setStyleSheet(
                    f"QFrame {{ border: {VizTheme.BORDER_WIDTH}px solid {VizTheme.BORDER}; "
                    f"border-radius: 4px; }}")
                cl = QHBoxLayout(cell)
                cl.setContentsMargins(6, 2, 6, 2)
                map_lbl = QLabel(self._map_text(dev))
                map_lbl.setStyleSheet("border: none;")          # children inherit no border
                self._map_labels[dev] = map_lbl
                set_btn = QPushButton("Set…")
                clr_btn = QPushButton("Clear")
                set_btn.clicked.connect(lambda _=False, d=dev: self._set_map(d))
                clr_btn.clicked.connect(lambda _=False, d=dev: self._clear_map(d))
                cl.addWidget(map_lbl, 1)
                cl.addWidget(set_btn)
                cl.addWidget(clr_btn)
                grid.addWidget(cell, r, col_of["maps"])

            if "depths" in col_of:
                spin = QSpinBox()
                spin.setRange(0, MAX_HUB_DEPTH)
                spin.setAlignment(Qt.AlignCenter)
                spin.setValue(int(self.hub_depths.get(dev, 0)))
                self._depth_spins[dev] = spin
                grid.addWidget(spin, r, col_of["depths"], alignment=Qt.AlignCenter)

        scroll.setWidget(host)
        root.addWidget(scroll, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    def _map_text(self, dev: int) -> str:
        pm = self.regmaps.get(dev)
        if pm is None:
            return "(none)"
        return f"{pm.name} ({len(pm.registers)})"

    def _set_map(self, dev: int) -> None:
        from .regmap_import_dialog import RegisterMapImportDialog
        dlg = RegisterMapImportDialog(self, device=dev, devices=[dev])
        if dlg.exec() and dlg.pmap is not None:
            self.regmaps[dev] = dlg.pmap
            self._map_labels[dev].setText(self._map_text(dev))

    def _clear_map(self, dev: int) -> None:
        self.regmaps.pop(dev, None)
        self._map_labels[dev].setText(self._map_text(dev))

    def accept(self) -> None:
        # Only the shown sections have widgets; hidden sections keep their passed-in
        # values (the focused openers edit one aspect at a time).
        for dev in self._devices:
            if dev in self._name_edits:
                name = self._name_edits[dev].text().strip()
                if name:
                    self.names[dev] = name
                else:
                    self.names.pop(dev, None)
            if dev in self._depth_spins:
                self.hub_depths[dev] = self._depth_spins[dev].value()
        super().accept()
