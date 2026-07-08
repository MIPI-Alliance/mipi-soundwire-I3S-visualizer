"""Register-map import assistant.

A small wizard-style dialog for importing a peripheral (chip) register map and
binding it to a device. It drives `model.regmap_import`: pick a vendor file
(e.g. Cirrus-style XML) or a pre-normalized `.json`, preview what was inferred
(register/field counts, address range, coalesced fields, warnings), choose the
target device, then accept to hand back a `PeripheralRegisterMap`.
"""

from __future__ import annotations

import os
from typing import Iterable, Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox,
                               QFileDialog, QFormLayout, QHBoxLayout, QLabel,
                               QPlainTextEdit, QPushButton, QTreeWidget,
                               QTreeWidgetItem, QVBoxLayout)

from ..model.regmap_import import (PeripheralRegisterMap, ImportReport,
                                   import_text, REGMAP_SCHEMA)

_PREVIEW_LIMIT = 50          # registers shown in the preview tree


class RegisterMapImportDialog(QDialog):
    """Import a peripheral register map and bind it to a device.

    On accept, `self.pmap` holds the imported PeripheralRegisterMap and
    `self.device` the chosen device number (or None if the user cancelled)."""

    def __init__(self, parent=None, device: int = 0,
                 devices: Optional[Iterable[int]] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Import Peripheral Register Map")
        self.setMinimumWidth(560)
        self.pmap: Optional[PeripheralRegisterMap] = None
        self.device: Optional[int] = None
        self._path: str = ""

        root = QVBoxLayout(self)

        # Source row: a path label + Browse.
        src = QHBoxLayout()
        self._path_label = QLabel("No file selected")
        self._path_label.setStyleSheet("color: gray;")
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse)
        src.addWidget(QLabel("Source:"))
        src.addWidget(self._path_label, 1)
        src.addWidget(browse)
        root.addLayout(src)

        # Options: target device + coalesce toggle.
        form = QFormLayout()
        self._device = QComboBox()
        for d in (devices if devices is not None else range(12)):
            self._device.addItem(f"Device {d}", d)
        self._device.setCurrentIndex(max(0, self._device.findData(device)))
        form.addRow("Bind to device:", self._device)
        self._coalesce = QCheckBox("Coalesce indexed bits into ranged fields (amp_lvl[5:0])")
        self._coalesce.setChecked(True)
        self._coalesce.toggled.connect(lambda _=False: self._reimport())
        form.addRow("", self._coalesce)
        root.addLayout(form)

        # Summary + warnings.
        self._summary = QLabel("Choose a vendor register-map file (.xml) or a "
                               "normalized map (.json) to import.")
        self._summary.setWordWrap(True)
        root.addWidget(self._summary)

        self._warnings = QPlainTextEdit()
        self._warnings.setReadOnly(True)
        self._warnings.setMaximumHeight(96)
        self._warnings.setPlaceholderText("Import notes / warnings appear here.")
        root.addWidget(self._warnings)

        # Preview tree (first N registers, with coalesced fields).
        self._preview = QTreeWidget()
        self._preview.setColumnCount(3)
        self._preview.setHeaderLabels(["Register", "Address", "Fields"])
        self._preview.setRootIsDecorated(False)
        root.addWidget(self._preview, 1)

        self._buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self._buttons.accepted.connect(self.accept)
        self._buttons.rejected.connect(self.reject)
        self._buttons.button(QDialogButtonBox.Ok).setEnabled(False)
        root.addWidget(self._buttons)

    # ---- import flow ----
    def _browse(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Select register map", "",
            "Register maps (*.xml *.json);;Vendor XML (*.xml);;Normalized (*.json);;All files (*)")
        if path:
            self._path = path
            self._path_label.setText(os.path.basename(path))
            self._path_label.setStyleSheet("")
            self._reimport()

    def _reimport(self) -> None:
        if not self._path:
            return
        try:
            with open(self._path, "r", encoding="utf-8", errors="replace") as f:
                text = f.read()
            name = os.path.splitext(os.path.basename(self._path))[0]
            if self._path.lower().endswith(".json"):
                self.pmap = PeripheralRegisterMap.from_json(text)
                report = _report_for_loaded(self.pmap)
            else:
                self.pmap, report = import_text(text, name, coalesce=self._coalesce.isChecked())
        except Exception as exc:  # noqa: BLE001 — surface any parse error to the user
            self.pmap = None
            self._summary.setText(f"<b>Import failed:</b> {exc}")
            self._warnings.setPlainText("")
            self._preview.clear()
            self._buttons.button(QDialogButtonBox.Ok).setEnabled(False)
            return
        self._show(report)

    def _show(self, report: ImportReport) -> None:
        rng = ("—" if report.address_min is None
               else f"0x{report.address_min:08X} – 0x{report.address_max:08X}")
        self._summary.setText(
            f"<b>{report.format}</b> · {report.register_count} registers · "
            f"{report.raw_bit_count} bits → {report.field_count} fields · {rng}")
        self._warnings.setPlainText(
            "\n".join(f"• {w}" for w in report.warnings) or "No warnings.")
        self._preview.clear()
        for reg in (self.pmap.registers[:_PREVIEW_LIMIT] if self.pmap else []):
            fields = ", ".join(
                f"{fs.name}[{fs.hi}:{fs.lo}]" if fs.hi != fs.lo else f"{fs.name}[{fs.hi}]"
                for fs in reg.fields)
            QTreeWidgetItem(self._preview, [reg.name, f"0x{reg.abs_address:08X}", fields])
        if self.pmap and len(self.pmap.registers) > _PREVIEW_LIMIT:
            QTreeWidgetItem(self._preview,
                            [f"… {len(self.pmap.registers) - _PREVIEW_LIMIT} more", "", ""])
        for c in range(3):
            self._preview.resizeColumnToContents(c)
        self._buttons.button(QDialogButtonBox.Ok).setEnabled(bool(report.ok))

    def accept(self) -> None:
        self.device = self._device.currentData()
        super().accept()


def _report_for_loaded(pmap: PeripheralRegisterMap) -> ImportReport:
    """Synthesize a summary for a pre-normalized map loaded from JSON."""
    rep = ImportReport(source_name=pmap.name, format="normalized json")
    rep.register_count = len(pmap.registers)
    rep.field_count = sum(len(r.fields) for r in pmap.registers)
    rep.raw_bit_count = rep.field_count
    if pmap.registers:
        addrs = [r.abs_address for r in pmap.registers]
        rep.address_min, rep.address_max = min(addrs), max(addrs)
    return rep
