"""Register-map view: per-device register tree with provenance colouring.

For the chosen device it shows SLC + every data-port block that has activity,
each register's current value (as-of the time cursor) and a field breakdown,
colour-coded by provenance: Cold Reset / Bus Write / Bus Read / CSV Import. An
optional CSV "expected config" overlay (Compare) shows expected values in the
CSV-Import colour alongside the values snooped from the bus.
"""
from __future__ import annotations

from typing import Dict, List

from PySide6.QtCore import Qt, QPoint, Signal
from PySide6.QtGui import QAction, QBrush, QColor, QFont
from PySide6.QtWidgets import (QComboBox, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout,
                               QInputDialog, QLabel, QMenu, QPushButton, QSpinBox,
                               QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget)

from ..model import RegisterMap, DeviceRegisterFile, Provenance
from ..model.registers import (SLC_BASE, DP_BASE, DP_STRIDE, CURR_RANK_OFFSET, CDS_BASE,
                               PHY1_BASE, PHY2_BASE, PHY3_BASE)
from .theme import VizTheme, analyzer_stylesheet

# The three selectable PHYs (spec Table 9): block name, region base, and the short
# kind. The active one is set from the §5.1.2 link-control decode (set_phy); PHY
# selection is NOT register-visible, so this is the only way the view knows it.
_PHYS = [("PHY1", PHY1_BASE, "FBCSE-slow"),
         ("PHY2", PHY2_BASE, "FBCSE-fast"),
         ("PHY3", PHY3_BASE, "DLV")]

# Provenance → value-column colour. Bus Read reuses the timeline's read colour so a
# read-revealed value reads the same across the analyzer.
_PROV_COLOR = {
    Provenance.DEFAULT: QColor(VizTheme.PROV_DEFAULT),
    Provenance.WRITTEN: QColor(VizTheme.PROV_WRITTEN),
    Provenance.READ:    QColor(VizTheme.SEM_READ),
    Provenance.CSV:     QColor(VizTheme.PROV_CSV),
    Provenance.UI:      QColor(VizTheme.PROV_UI),
}

# Legend labels for each provenance — what its colour means in the value column.
_PROV_LABEL = {
    Provenance.DEFAULT: "Cold Reset",
    Provenance.WRITTEN: "Bus Write",
    Provenance.READ:    "Bus Read",
    Provenance.CSV:     "CSV Import",
    Provenance.UI:      "Manual Edit",
}


def _is_reserved(name) -> bool:
    n = str(name).strip().lower()
    return n.startswith(("reserved", "rsv", "rsvd")) or "reserv" in n


def _field_value_max(field, current: int) -> int:
    """Highest value the user should be able to enter for `field`: the field's full
    range (2^width - 1) minus any TRAILING reserved values (so a 2-bit BitWidth whose
    value 3 is Reserved offers 0..2). The current value is always allowed, so a register
    that already holds a reserved value isn't silently rewritten."""
    top = (1 << (field.hi - field.lo + 1)) - 1
    while top > 0 and top in field.enum and _is_reserved(field.enum[top]):
        top -= 1
    return max(top, int(current))


def _enum_covers(field) -> bool:
    """True when the field's enum names EVERY value in its range — a true enumerated
    field (edited via a dropdown). A partial enum (e.g. only a reserved marker) is not
    covered, so it's edited as a bounded number instead."""
    return bool(field.enum) and all(v in field.enum
                                    for v in range(1 << (field.hi - field.lo + 1)))


class _RegisterTree(QTreeWidget):
    """QTreeWidget that reports body right-clicks via a signal. We override
    contextMenuEvent rather than use the CustomContextMenu policy because, for an
    item view, context-menu events are delivered to the viewport and the policy's
    customContextMenuRequested signal doesn't reliably fire for body clicks on macOS
    (it works for a header, which handles its own events). The override always fires;
    the position is emitted in GLOBAL coordinates so the handler needn't juggle the
    scroll-area/viewport coordinate spaces."""

    contextRequested = Signal(QPoint)   # global position of the right-click

    def contextMenuEvent(self, ev) -> None:  # noqa: N802 (Qt override)
        self.contextRequested.emit(ev.globalPos())
        ev.accept()


class RegisterView(QWidget):
    # Debug what-if: user double-clicked a register and set a byte (device, addr, value).
    registerEdited = Signal(int, int, int)
    overridesCleared = Signal()

    def __init__(self, rmap: RegisterMap) -> None:
        super().__init__()
        self.setStyleSheet(analyzer_stylesheet())
        self._rmap = rmap
        self._files: Dict[int, DeviceRegisterFile] = {}
        self._peripheral_maps: Dict[int, object] = {}   # device -> PeripheralRegisterMap
        self._device_names: Dict[int, str] = {}          # device -> display name
        self._csv_expected: Dict[tuple, int] = {}  # (device, address) -> CSV expected value
        self._active_phy: str | None = None      # 'PHY1'|'PHY2'|'PHY3' from §5.1.2 decode

        self._device = QComboBox()
        self._device.currentIndexChanged.connect(lambda _: self._rebuild())
        # Debug: clear all manual (what-if) register edits.
        self._clear_edits = QPushButton("Clear Edits")
        self._clear_edits.setToolTip("Remove all manual register overrides (what-if debug)")
        self._clear_edits.clicked.connect(lambda: self.overridesCleared.emit())
        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 6, 0)   # small right gap so Clear Edits clears the pane edge
        top.setSpacing(8)
        top.addWidget(self._device, 3)        # picker takes ~3/4 of the row...
        top.addStretch(1)                     # ...leaving a 25% gap before the button
        top.addWidget(self._clear_edits)

        self._tree = _RegisterTree()
        self._tree.setColumnCount(4)
        self._tree.setHeaderLabels(["Register", "Addr", "Value", "Fields"])
        self._tree.setAlternatingRowColors(True)
        self._tree.setIndentation(10)      # horizontal space is precious here (default ~20)
        from .copyable import enable_copy
        enable_copy(self._tree, multi=True)   # ⌘/Ctrl+C copies selected register rows (TSV)
        # Force a register's value (what-if debug) either by double-clicking its row
        # or right-clicking it for a context menu — the right-click affordance is the
        # discoverable one (double-click alone wasn't obvious). See _RegisterTree for
        # why we use contextMenuEvent, not the CustomContextMenu policy.
        self._tree.itemDoubleClicked.connect(self._on_double_click)
        self._tree.contextRequested.connect(self._on_context_menu)
        # The Fields column holds every decoded field and is often wider than the
        # pane — let it size to its full content and scroll horizontally rather than
        # stretch+elide, so all fields are readable by scrolling right.
        self._tree.setTextElideMode(Qt.ElideNone)
        self._tree.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self._tree.header().setStretchLastSection(False)

        # Provenance colour key, so a coloured value is self-explanatory.
        legend = QLabel(self._legend_html())
        legend.setTextFormat(Qt.RichText)
        legend.setContentsMargins(2, 0, 2, 2)
        self._legend = legend

        lay = QVBoxLayout(self)
        lay.setContentsMargins(2, 2, 2, 2)
        lay.addLayout(top)
        lay.addWidget(legend)
        lay.addWidget(self._tree)

        # Seed a default device (0) at construction so the pane shows the cold-reset
        # register map at app load — before any capture is decoded — instead of an
        # empty tree. _rebuild falls back to a default DeviceRegisterFile when no
        # decoded file exists, so this renders the spec's reset values.
        self.set_files({})

    def _legend_html(self) -> str:
        """The provenance colour-key swatches HTML (rebuilt on retheme)."""
        return "&nbsp;&nbsp;".join(
            f'<span style="color:{_PROV_COLOR[p].name()}; font-size:14px;">&#9632;</span>'
            f' {_PROV_LABEL[p]}' for p in
            (Provenance.DEFAULT, Provenance.WRITTEN, Provenance.READ, Provenance.CSV,
             Provenance.UI))

    def retheme(self) -> None:
        """Re-apply the palette after a theme switch: refresh the provenance value
        colours, the item-view stylesheet and the colour-key legend, then rebuild
        the tree so each row's foreground picks up the new theme."""
        global _PROV_COLOR
        _PROV_COLOR = {
            Provenance.DEFAULT: QColor(VizTheme.PROV_DEFAULT),
            Provenance.WRITTEN: QColor(VizTheme.PROV_WRITTEN),
            Provenance.READ:    QColor(VizTheme.SEM_READ),
            Provenance.CSV:     QColor(VizTheme.PROV_CSV),
            Provenance.UI:      QColor(VizTheme.PROV_UI),
        }
        self.setStyleSheet(analyzer_stylesheet())
        self._legend.setText(self._legend_html())
        self._rebuild()

    def set_files(self, files: Dict[int, DeviceRegisterFile]) -> None:
        self._files = files
        self._rebuild_device_combo()
        self._rebuild()

    def _rebuild_device_combo(self) -> None:
        """Repopulate the device dropdown from the tracked files UNION the devices a
        compare CSV references, keeping the current selection when it survives. Shared
        by set_files and set_expected so a CSV-only device (compare against a device
        not configured at the cursor) shows up immediately, not only after a cursor
        move happens to call set_files."""
        cur = self._device.currentData()
        self._device.blockSignals(True)
        self._device.clear()
        devices = sorted(set(self._files) | {d for d, _ in self._csv_expected}) or [0]
        for d in devices:
            self._device.addItem(self._device_label(d), d)
        idx = max(0, self._device.findData(cur))
        self._device.setCurrentIndex(idx)
        self._device.blockSignals(False)

    def set_peripheral_maps(self, maps: Dict[int, object]) -> None:
        """Bind per-device vendor register maps (device -> PeripheralRegisterMap),
        rendered as a 'Peripheral Registers' block under their device."""
        self._peripheral_maps = maps or {}
        self._rebuild()

    def set_device_names(self, names: Dict[int, str]) -> None:
        """Per-device display names, shown in the device dropdown."""
        self._device_names = names or {}
        cur = self._device.currentData()
        for i in range(self._device.count()):
            d = self._device.itemData(i)
            self._device.setItemText(i, self._device_label(d))
        # itemText change doesn't alter selection; keep current device.
        _ = cur

    def _device_label(self, dev: int) -> str:
        name = self._device_names.get(dev)
        return f"Device {dev} — {name}" if name else f"Device {dev}"

    def set_phy(self, phy_name: "str | None") -> None:
        """Mark which PHY (PHY1/2/3) the §5.1.2 link-control decode found active,
        so the register view highlights that block and greys the others. None when
        no bring-up was captured (then all three PHY blocks render neutrally)."""
        self._active_phy = phy_name
        self._rebuild()

    def _on_double_click(self, item: QTreeWidgetItem, _col: int) -> None:
        """Double-click an editable register row → the field editor when the register
        has decodable fields, else a raw-byte prompt."""
        if self._editable_fields(item):
            self._edit_fields(item)
        else:
            self._prompt_edit(item)

    def _on_context_menu(self, global_pos) -> None:
        """Right-click a register row → a context menu to edit fields / force the raw
        value / clear all manual edits. `global_pos` is the right-click in global
        coordinates (from _RegisterTree.contextMenuEvent); map it back to the viewport
        to find the row under the cursor."""
        item = self._tree.itemAt(self._tree.viewport().mapFromGlobal(global_pos))
        self._context_menu_for(item).exec(global_pos)

    def _context_menu_for(self, item: "QTreeWidgetItem | None") -> QMenu:
        """Build (but don't show) the register context menu for `item`: 'Edit fields…'
        (when the register has fields), 'Force value…' (raw byte), and 'Clear all
        manual edits'. Split out from _on_context_menu so it's testable without popping
        a modal menu."""
        menu = QMenu(self._tree)
        editable = bool(item is not None and item.data(0, Qt.UserRole))
        act_fields = QAction("Edit fields…", menu)
        act_fields.setEnabled(bool(self._editable_fields(item)))
        act_fields.triggered.connect(lambda: self._edit_fields(item))
        menu.addAction(act_fields)
        act_force = QAction("Force value…", menu)
        act_force.setEnabled(editable)
        act_force.triggered.connect(lambda: self._prompt_edit(item))
        menu.addAction(act_force)
        act_clear = QAction("Clear all manual edits", menu)
        act_clear.triggered.connect(lambda: self.overridesCleared.emit())
        menu.addAction(act_clear)
        return menu

    def _spec_for(self, dev: int, addr: int):
        """The RegisterSpec at `addr` for `dev` — a SWI3S register, else this device's
        peripheral (vendor) register — or None."""
        res = self._rmap.resolve(addr)
        if res:
            return res.register
        pmap = self._peripheral_maps.get(dev)
        if pmap is not None:
            return pmap.resolve(addr)
        return None

    def _editable_fields(self, item: "QTreeWidgetItem | None"):
        """The non-reserved decodable fields of the register on `item`, if it's an
        editable row whose register has fields worth editing individually (more than
        one field, or one that doesn't span the whole byte). Else None."""
        key = item.data(0, Qt.UserRole) if item is not None else None
        if not key:
            return None
        spec = self._spec_for(int(key[0]), int(key[1]))
        if spec is None:
            return None
        fields = [f for f in spec.fields if not f.name.lower().startswith("reserved")]
        if not fields:
            return None
        if len(fields) == 1 and (fields[0].hi - fields[0].lo + 1) >= 8:
            return None            # a single full-byte field == the raw byte; no gain
        return fields

    @staticmethod
    def _compose_fields(current: int, field_values) -> int:
        """Compose a byte from `current` by overlaying (FieldSpec, raw_value) pairs
        into their bit positions. Values are masked to the field width."""
        out = int(current) & 0xFF
        for f, raw in field_values:
            width = f.hi - f.lo + 1
            raw = int(raw) & ((1 << width) - 1)
            out = (out & ~f.mask) | ((raw << f.lo) & f.mask)
        return out & 0xFF

    def _edit_fields(self, item: "QTreeWidgetItem | None") -> None:
        """Field-level what-if editor: one input per field (enum → dropdown, else a
        numeric raw-value box), prefilled from the current byte. On OK, composes the
        byte and emits registerEdited. Falls back to the raw-byte prompt if the
        register has no editable fields."""
        fields = self._editable_fields(item)
        if not fields:
            self._prompt_edit(item)
            return
        key = item.data(0, Qt.UserRole)
        dev, addr = int(key[0]), int(key[1])
        try:
            current = int(item.text(2), 0) & 0xFF
        except ValueError:
            current = 0
        spec = self._spec_for(dev, addr)
        dlg = QDialog(self)
        dlg.setWindowTitle(f"Edit fields — {spec.name} @ 0x{addr:04X}")
        form = QFormLayout(dlg)
        editors = []                       # (FieldSpec, widget, kind)
        for f in fields:
            raw = f.extract(current)
            width = f.hi - f.lo + 1
            if _enum_covers(f):
                # Every value is named → a true enumerated field: pick from a dropdown.
                cb = QComboBox()
                for v in sorted(f.enum):
                    cb.addItem(f"{v} — {f.enum[v]}", v)
                cb.setCurrentIndex(max(0, cb.findData(raw)))
                editors.append((f, cb, "enum"))
                form.addRow(f.name, cb)
            else:
                # Numeric (or partial-enum, e.g. a reserved marker): a bounded spinbox
                # so every VALID value is enterable and reserved/out-of-range ones can't
                # be. Any named values are shown in the tooltip.
                sb = QSpinBox()
                vmax = _field_value_max(f, raw)
                sb.setRange(0, vmax)
                sb.setValue(min(raw, vmax))
                tip = f"bits [{f.hi}:{f.lo}], {width}-bit — enter 0..{vmax}"
                if f.excess1:
                    tip += " · excess-1: actual count = value + 1"
                if f.enum:
                    tip += " · named: " + ", ".join(f"{v}={f.enum[v]}" for v in sorted(f.enum))
                sb.setToolTip(tip)
                editors.append((f, sb, "int"))
                form.addRow(f"{f.name}  [{f.hi}:{f.lo}]", sb)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(dlg.accept)
        buttons.rejected.connect(dlg.reject)
        form.addRow(buttons)
        if dlg.exec() != QDialog.Accepted:
            return
        pairs = []
        for f, w, kind in editors:
            pairs.append((f, int(w.currentData()) if kind == "enum" else int(w.value())))
        self.registerEdited.emit(dev, addr, self._compose_fields(current, pairs))

    def _prompt_edit(self, item: "QTreeWidgetItem | None") -> None:
        """Prompt for a raw byte and emit a what-if override (device, address, value)
        for the given editable register row. No-op for non-editable rows (block
        headers, _CURR peers)."""
        key = item.data(0, Qt.UserRole) if item is not None else None
        if not key:
            return
        dev, addr = int(key[0]), int(key[1])
        text, ok = QInputDialog.getText(
            self, "Manual register edit",
            f"{item.text(0)}  @ 0x{addr:04X}\nForce value (hex or decimal byte):",
            text=item.text(2))
        if not ok:
            return
        try:
            val = int(text.strip(), 0) & 0xFF
        except ValueError:
            return
        self.registerEdited.emit(dev, addr, val)

    def _effective(self, dev: int, addr: int, dev_file: DeviceRegisterFile):
        key = (dev, addr)
        if key in self._csv_expected:
            # Compare mode: the expected (CSV) config value, in the CSV-Import colour
            # — distinct from a bus write/read so the user can eyeball expected-config
            # vs decoded values side by side.
            return self._csv_expected[key], Provenance.CSV
        return dev_file.value(addr), dev_file.provenance(addr)

    # ---- compare: expected config from a CSV ----
    def set_expected(self, expected_writes) -> None:
        """Overlay an expected register config (from a CSV) in the CSV-Import colour,
        so the expected config is visible alongside the values snooped from the bus.
        (device, address, value) triples. Persists across cursor moves until
        cleared."""
        self._csv_expected = {(int(d), int(a)): int(v) for d, a, v in expected_writes}
        self._rebuild_device_combo()          # a CSV-only device must appear in the picker
        self._rebuild()

    def clear_expected(self) -> None:
        self._csv_expected = {}
        self._rebuild_device_combo()          # drop any CSV-only device from the picker
        self._rebuild()

    def has_expected(self) -> bool:
        return bool(self._csv_expected)

    def _active_dps(self, dev: int, dev_file: DeviceRegisterFile) -> List[int]:
        addrs = {a for a, _ in dev_file.items()}
        addrs |= {a for (d, a) in self._csv_expected if d == dev}
        dps = {(a - DP_BASE) // DP_STRIDE
               for a in addrs if DP_BASE <= a < DP_BASE + DP_STRIDE * 32}
        return sorted(dps) or [0]

    def _make_row(self, parent: QTreeWidgetItem, name: str, spec, addr: int,
                  value: int, prov: Provenance, *, tip: str,
                  edit_key: "tuple | None" = None) -> QTreeWidgetItem:
        """One register row: name / 0xADDR / 0xVV / decoded fields, coloured by
        provenance. `edit_key` (device, address) marks the row as double-click
        editable for a what-if override."""
        fields = ", ".join(f"{n}={f.value_label(v)}" for n, v, f in spec.decode(value)
                           if not n.lower().startswith("reserved"))
        item = QTreeWidgetItem(parent, [name, f"0x{addr:04X}", f"0x{value:02X}", fields])
        brush = QBrush(_PROV_COLOR[prov])
        for c in range(4):
            item.setForeground(c, brush)
        if prov != Provenance.DEFAULT:
            f = QFont()
            f.setBold(True)
            item.setFont(0, f)
        if edit_key is not None:
            item.setData(0, Qt.UserRole, edit_key)     # (device, address) → editable
            tip += "  ·  right-click (or double-click) to edit fields / force a value"
        item.setToolTip(0, tip)
        return item

    def _add_register(self, parent: QTreeWidgetItem, block: str, spec, abs_addr: int,
                      dev: int, dev_file: DeviceRegisterFile) -> None:
        # Main row = the _NEXT (staged) value for dual-ranked registers, or the
        # single live value otherwise.
        value, prov = self._effective(dev, abs_addr, dev_file)
        name = f"{spec.name} (NEXT)" if spec.dual_ranked else spec.name
        tip = f"{block} {spec.name} — provenance: {prov.value}"
        if spec.dual_ranked:
            tip += "; staged _NEXT value (reaches _CURR on a matching Commit)"
        if prov == Provenance.CSV:                # expected (from the config file)
            tip += "; from the expected config CSV"
        # The main (NEXT / single) row is the double-click editable one; the override
        # targets its NEXT-base address so force_ui reaches both ranks.
        self._make_row(parent, name, spec, abs_addr, value, prov, tip=tip,
                       edit_key=(dev, abs_addr))

        if not spec.dual_ranked:
            return
        # Committed _CURR copy: a PEER row (same level as _NEXT under the block), at
        # the _CURR alias address. The CSV-expected overlay applies only to the staged
        # _NEXT value, so _CURR comes straight from the device file's committed bank.
        # It's editable too — a what-if edit here redirects to the register's NEXT-base
        # (edit_key = abs_addr), where force_ui writes BOTH ranks — so a right-click on
        # whichever rank the user is looking at works, not just the NEXT row.
        curr_delta = (spec.curr_offset - spec.offset
                      if spec.curr_offset is not None else CURR_RANK_OFFSET)
        curr_addr = abs_addr + curr_delta
        curr_val = dev_file.curr_value(abs_addr)
        curr_prov = dev_file.curr_provenance(abs_addr)
        self._make_row(parent, f"{spec.name} (CURR)", spec, curr_addr, curr_val, curr_prov,
                       tip=f"{block} {spec.name} — committed _CURR (live) value; "
                           f"provenance: {curr_prov.value}",
                       edit_key=(dev, abs_addr))

    def _rebuild(self) -> None:
        # Preserve which top-level blocks the user had expanded, so a cursor move
        # (which re-runs this on the as-of-cursor register files) doesn't collapse
        # the tree the user just opened. Block labels are stable across rebuilds.
        root = self._tree.invisibleRootItem()
        prev_expanded = {root.child(i).text(0) for i in range(root.childCount())
                         if root.child(i).isExpanded()}
        self._tree.clear()
        dev = self._device.currentData()
        if dev is None:
            return
        dev_file = self._files.get(dev) or DeviceRegisterFile(self._rmap, dev)

        slc = QTreeWidgetItem(self._tree, ["System & Link Control (SLC)", "", "", ""])
        for spec in self._rmap.blocks.get("SLC", []):
            self._add_register(slc, "SLC", spec, SLC_BASE + spec.offset, dev, dev_file)
        slc.setExpanded(True)

        # Control Data Stream Transport (CDS) registers (spec §15.2.1 region).
        cds_specs = self._rmap.blocks.get("CDS", [])
        if cds_specs:
            cds = QTreeWidgetItem(self._tree, ["Control Data Stream Transport (CDS)", "", "", ""])
            for spec in cds_specs:
                self._add_register(cds, "CDS", spec, CDS_BASE + spec.offset, dev, dev_file)

        active = self._active_dps(dev, dev_file)
        for dp in active:
            base = DP_BASE + dp * DP_STRIDE
            node = QTreeWidgetItem(self._tree, [f"Data Port Registers — DP{dp}", "", "", ""])
            for spec in self._rmap.blocks.get("DP", []):
                self._add_register(node, f"DP{dp}", spec, base + spec.offset, dev, dev_file)
            node.setExpanded(dp == active[0])

        # PHY1/2/3 electrical-control blocks. PHY selection is set by the Cold Start
        # LC sequence (§5.1.2), NOT by any register — so the active PHY (from the
        # link-control decode) is shown in bold/expanded and the others greyed +
        # collapsed, making "which PHY is live" visible even though it's reg-invisible.
        for name, base, kind in _PHYS:
            specs = self._rmap.blocks.get(name, [])
            if not specs:
                continue
            active_phy = (self._active_phy == name)
            unknown = self._active_phy is None
            tag = " — ACTIVE" if active_phy else ("" if unknown else " — inactive")
            node = QTreeWidgetItem(self._tree, [f"{name} ({kind}){tag}", "", "", ""])
            if active_phy:
                bold = QFont(); bold.setBold(True)
                node.setFont(0, bold)
            elif not unknown:
                node.setForeground(0, QBrush(QColor(VizTheme.AXIS)))   # greyed
            for spec in specs:
                self._add_register(node, name, spec, base + spec.offset, dev, dev_file)
            node.setExpanded(active_phy)

        # Per-device peripheral (vendor) register map, in device-defined space.
        # Imported via the register-map assistant; decoded from the same writes.
        pmap = self._peripheral_maps.get(dev)
        if pmap is not None and getattr(pmap, "registers", None):
            label = f"Peripheral Registers — {pmap.name} ({len(pmap.registers)})"
            node = QTreeWidgetItem(self._tree, [label, "", "", ""])
            for spec in pmap.registers:
                self._add_register(node, "PERIPH", spec, spec.abs_address, dev, dev_file)
            node.setExpanded(False)            # large block — user expands

        # Re-apply the user's prior expansion (overrides the per-block defaults
        # above) once anything has been opened, so expansion survives a rebuild.
        if prev_expanded:
            root = self._tree.invisibleRootItem()
            for i in range(root.childCount()):
                it = root.child(i)
                it.setExpanded(it.text(0) in prev_expanded)

        for c in range(4):                       # size every column to its content
            self._tree.resizeColumnToContents(c)  # (Fields scrolls horizontally)

    @property
    def device_count(self) -> int:
        return self._device.count()

    def current_device(self) -> "int | None":
        """The device number currently selected in the dropdown, or None."""
        return self._device.currentData()
