"""Command table: a virtual model over the decoded command stream.

Address is resolved to register + field names via the RegisterMap, so a
WriteA32 reads e.g. "SLC.NumColumns" with a "NumColumns=3" tooltip.
"""
from __future__ import annotations

import re
from typing import List, Optional

from PySide6.QtCore import (QAbstractTableModel, QModelIndex, QSortFilterProxyModel, Qt)
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QTableView, QAbstractItemView, QHeaderView, QMenu, QWidget

from ..model import RegisterMap
from ..analysis import command_error, is_error
from ..analysis.responses import response_summary
from .row_origin import RowOriginMixin
from .theme import VizTheme, analyzer_stylesheet

_ERROR_BG = QColor(VizTheme.SEM_ERROR_BG)

# Columns follow the on-wire command structure: PhaseID, Device Mask, Packet
# Length, then the Manager Packet (Opcode, Address, Register, Data, Group Mask),
# then CRC and the response. Row/Time lead for navigation.
_COLUMNS = ["Row", "Time (µs)", "PhaseID", "Devices", "Packet Length", "Opcode",
            "Address", "Register", "Data", "Group Mask", "CRC", "Response"]
_COL = {name: i for i, name in enumerate(_COLUMNS)}

# Two-line header labels for columns whose LABEL is wider than their (narrow) data —
# so the column shrinks to the wider word instead of the full label. The base name is
# still used for sorting / the column-picker menu.
_WRAP_HEADERS = {"Packet Length": "Packet\nLength", "Group Mask": "Group\nMask"}

# Sort key per column: numeric where it matters, else the display text. Used via a
# dedicated sort role so e.g. Row/Time/masks sort numerically, not lexically.
SORT_ROLE = Qt.UserRole + 7
ROW_COL = 0                              # the "Row" column index (0-based bus row)


def _collapse_ranges(nums) -> str:
    """Collapse a sorted list of ints into comma-separated runs, e.g.
    [1, 3, 4, 5] -> '1,3-5'."""
    out = []
    i = 0
    while i < len(nums):
        j = i
        while j + 1 < len(nums) and nums[j + 1] == nums[j] + 1:
            j += 1
        out.append(str(nums[i]) if i == j else f"{nums[i]}-{nums[j]}")
        i = j + 1
    return ",".join(out)


def _mask_list(mask: int) -> str:
    bits = [i for i in range(16) if mask & (1 << i)]
    return _collapse_ranges(bits) if bits else "—"


def _device_list(mask: int) -> str:
    devs = [i for i in range(12) if mask & (1 << i)]
    return _collapse_ranges(devs) if devs else "—"


def _response(cmd: dict) -> str:
    return response_summary(cmd)


def _payload(cmd: dict) -> bytes:
    """The byte payload to show in the Data column: a Read's returned bytes
    (`read_data`) for read commands, else the manager-packet `data` (writes). A
    Read carries no manager-packet data, so it would otherwise show blank."""
    if cmd.get("has_read_data") and cmd.get("read_data"):
        return cmd.get("read_data") or b""
    return cmd.get("data") or b""


# ---- boolean free-text filter (and / or / parentheses over substring terms) ----
# Tokeniser: split on parens and the AND/OR keywords (word-bounded, case-insensitive),
# keeping them; the text between is a literal substring term (spaces preserved).
_TOK_RE = re.compile(r"\s*(\(|\)|\band\b|\bor\b)\s*", re.IGNORECASE)


def _tokenize(text: str) -> list:
    toks = []
    for part in _TOK_RE.split(text):
        if not part:
            continue
        s = part.strip()
        if not s:
            continue
        low = s.lower()
        if s in ("(", ")"):
            toks.append(s)
        elif low in ("and", "or"):
            toks.append(low.upper())
        else:
            toks.append(("TERM", low))
    return toks


def parse_filter(text: str):
    """Parse a free-text query into a predicate AST. Grammar (AND binds tighter
    than OR; parentheses override; adjacent terms imply AND):

        or_expr  := and_expr ('or' and_expr)*
        and_expr := factor (('and')? factor)*
        factor   := '(' or_expr ')' | TERM

    Returns an AST node (tuple) or None for empty input. Malformed input (e.g.
    stray parens) is parsed best-effort and never raises."""
    toks = _tokenize(text or "")
    if not toks:
        return None
    pos = 0

    def peek():
        return toks[pos] if pos < len(toks) else None

    def parse_or():
        nonlocal pos
        node = parse_and()
        while peek() == "OR":
            pos += 1
            node = ("or", node, parse_and())
        return node

    def parse_and():
        nonlocal pos
        node = parse_factor()
        while True:
            t = peek()
            if t == "AND":
                pos += 1
                node = ("and", node, parse_factor())
            elif t == "(" or (isinstance(t, tuple)):     # adjacency = implicit AND
                node = ("and", node, parse_factor())
            else:
                return node

    def parse_factor():
        nonlocal pos
        t = peek()
        if t == "(":
            pos += 1
            node = parse_or()
            if peek() == ")":
                pos += 1
            return node
        if isinstance(t, tuple):                          # TERM
            pos += 1
            return t
        pos += 1                                          # stray ')' / operator — skip
        return ("TERM", "")                               # matches anything

    return parse_or()


def eval_filter(node, hay: str) -> bool:
    """Evaluate a parse_filter AST against a lowercased haystack string."""
    if node is None:
        return True
    op = node[0]
    if op == "TERM":
        return node[1] in hay
    if op == "and":
        return eval_filter(node[1], hay) and eval_filter(node[2], hay)
    if op == "or":
        return eval_filter(node[1], hay) or eval_filter(node[2], hay)
    return True


class CommandTableModel(RowOriginMixin, QAbstractTableModel):
    def __init__(self, commands: List[dict], rmap: RegisterMap,
                 sample_rate_hz: int, peripheral_maps=None, row_origin: int = 0) -> None:
        super().__init__()
        self._cmds = commands
        self._rmap = rmap
        # Per-device peripheral (vendor) register maps {device: PeripheralRegisterMap},
        # used to name addresses in device-defined space for that device's commands.
        self._peripheral_maps = dict(peripheral_maps or {})
        self._rate = sample_rate_hz or 1
        # Displayed bus rows are 0-based (RowOriginMixin.display_row): subtract the
        # capture's first-row origin (a mid-row-start capture anchors internally at
        # row_base > 0) so the first row reads as 0. See Session.row_origin.
        self._row_origin = int(row_origin)
        self._search_cache: dict = {}        # row -> lowercased display haystack

    def _display_row(self, cmd: dict) -> int:
        """0-based bus row for a command's display cell."""
        return self.display_row(cmd.get("bus_row", 0))

    def set_commands(self, commands: List[dict]) -> None:
        self.beginResetModel()
        self._cmds = commands
        self._search_cache = {}
        self.endResetModel()

    def set_peripheral_maps(self, maps) -> None:
        """Rebind per-device peripheral register maps and refresh, so device-defined
        addresses show their vendor register names + field decode in the table."""
        self.beginResetModel()
        self._peripheral_maps = dict(maps or {})
        self._search_cache = {}
        self.endResetModel()

    def search_text(self, row: int) -> str:
        """Lowercased, space-joined display text of a row, cached — so the free-text
        filter compares a prebuilt string instead of re-rendering every cell (register
        labels, response decode, …) of every row on each keystroke."""
        s = self._search_cache.get(row)
        if s is None:
            s = " ".join(str(self.data(self.index(row, c)) or "")
                         for c in range(self.columnCount())).lower()
            self._search_cache[row] = s
        return s

    def rowCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._cmds)

    def columnCount(self, parent=QModelIndex()) -> int:
        return len(_COLUMNS)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if role == Qt.DisplayRole and orientation == Qt.Horizontal:
            name = _COLUMNS[section]
            return _WRAP_HEADERS.get(name, name)     # two-line label for wide-label columns
        return None

    def command_at(self, row: int) -> Optional[dict]:
        return self._cmds[row] if 0 <= row < len(self._cmds) else None

    @staticmethod
    def _cmd_device(cmd: dict) -> Optional[int]:
        """The command's single target device (first set bit of the mask), or None."""
        mask = int(cmd.get("device_mask", 0))
        if mask:
            for i in range(12):
                if mask & (1 << i):
                    return i
        return None

    def _peripheral_reg(self, cmd: dict, addr: int):
        """The peripheral RegisterSpec at `addr` for this command's device, or None."""
        dev = self._cmd_device(cmd)
        pmap = self._peripheral_maps.get(dev) if dev is not None else None
        return pmap.resolve(addr) if pmap is not None else None

    @staticmethod
    def _peripheral_field_summary(preg, byte: int) -> str:
        parts = [f"{n}={f.value_label(v)}" for n, v, f in preg.decode(byte)
                 if not n.lower().startswith("reserved")]
        return f"{preg.name}: " + ", ".join(parts) if parts else preg.name

    def _register_label(self, cmd: dict) -> str:
        if not cmd.get("has_address"):
            return ""
        res = self._rmap.resolve(cmd["address"])
        if not res:
            # Not a SWI3S register: try this device's peripheral (vendor) map, then
            # fall back to the address-region label (Table 163/164).
            preg = self._peripheral_reg(cmd, cmd["address"])
            if preg is not None:
                return preg.name
            from ..model.registers import region_name
            return region_name(cmd['address'])
        name = res.register.name
        if res.dp_index is not None:
            name = f"DP{res.dp_index}.{name}"
        if res.rank in ("NEXT", "CURR"):
            name += f"[{res.rank}]"
        # Drop the redundant SLC./DP. block prefixes (a DP register already reads
        # "DP{n}.…"); keep the block name for CDS / PHY1-3 where it disambiguates.
        if res.block in ("SLC", "DP"):
            return name
        return f"{res.block}.{name}"

    def data(self, index: QModelIndex, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        cmd = self._cmds[index.row()]
        name = _COLUMNS[index.column()]
        if role == Qt.BackgroundRole:
            return _ERROR_BG if is_error(cmd) else None
        if role == SORT_ROLE:                              # numeric where it helps
            if name == "Row":
                return self._display_row(cmd)
            if name == "Time (µs)":
                return int(cmd.get("start_sample", 0))
            if name == "Devices":
                return int(cmd.get("device_mask", 0))
            if name == "Packet Length":
                return int(cmd.get("packet_length", 0)) if cmd.get("has_manager_packet") else -1
            if name == "Group Mask":
                return int(cmd.get("group_mask", 0)) if cmd.get("is_commit") else -1
            return self.data(index, Qt.DisplayRole) or ""
        if role == Qt.ToolTipRole:
            err = command_error(cmd)
            if err:
                return f"⚠ {err}"
            if name in ("Address", "Register", "Data") and cmd.get("has_address") and _payload(cmd):
                lines = []
                for i, b in enumerate(_payload(cmd)):
                    s = self._rmap.field_summary(cmd["address"] + i, b)
                    if not s:
                        preg = self._peripheral_reg(cmd, cmd["address"] + i)
                        if preg is not None:
                            s = self._peripheral_field_summary(preg, b)
                    lines.append(s or f"0x{cmd['address']+i:08X} = 0x{b:02X}")
                return "\n".join(lines)
            if name == "Response":                         # full text — the cell is capped/elided
                return _response(cmd) or None
            return None
        if role != Qt.DisplayRole:
            return None
        data = _payload(cmd)
        if name == "Row":                                  # SWI3S bus row (0-based, Column-0 index)
            return f"{self._display_row(cmd):,}"
        if name == "Time (µs)":
            return f"{cmd.get('start_sample', 0) / self._rate * 1e6:,.2f}"
        if name == "PhaseID":
            return cmd.get("phase", "")
        if name == "Devices":
            return _device_list(cmd.get("device_mask", 0))
        if name == "Packet Length":
            return str(int(cmd.get("packet_length", 0))) if cmd.get("has_manager_packet") else ""
        if name == "Opcode":
            return cmd.get("command", "")
        if name == "Address":
            return f"0x{cmd['address']:08X}" if cmd.get("has_address") else ""
        if name == "Register":
            return self._register_label(cmd)
        if name == "Data":
            return " ".join(f"0x{b:02X}" for b in data) if data else ""
        if name == "Group Mask":                           # commit-group mask (commits only)
            if not cmd.get("is_commit"):
                return ""
            m = int(cmd.get("group_mask", 0))
            return f"0x{m:02X} ({_mask_list(m)})"
        if name == "CRC":
            if not cmd.get("has_manager_packet"):
                return ""
            return "OK" if cmd.get("crc_valid") else "BAD"
        if name == "Response":
            return _response(cmd)
        return None


class CommandTableView(QTableView):
    def __init__(self) -> None:
        super().__init__()
        self.setStyleSheet(analyzer_stylesheet())
        self.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.setSelectionMode(QAbstractItemView.SingleSelection)
        # ⌘/Ctrl+C copies the selected command row as TSV (single-select: the row
        # selection drives the shared cursor, so no range-select here).
        from .copyable import enable_copy
        enable_copy(self)
        self.setAlternatingRowColors(True)
        self.verticalHeader().setVisible(False)      # drop the empty row-index column (Row column carries the bus row)
        self.setSortingEnabled(True)                 # click a header to sort
        # Don't stretch the last column: every column sizes to its content. Response can
        # be long (a 12-device ping status), so it's sized to its FULL widest text (see
        # fit_columns) and the table scrolls horizontally (per-pixel) to reveal it — no
        # width cap, and never elided-with-no-way-to-see-the-rest.
        self.horizontalHeader().setStretchLastSection(False)
        self.setHorizontalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.setTextElideMode(Qt.ElideRight)
        # Blank the scroll-area corner (where the H/V scrollbars meet) so it doesn't
        # draw a bordered square; the plain QWidget inherits FRAME_BG from the sheet.
        self.setCornerWidget(QWidget())
        # Column picker: right-click the header to show/hide columns.
        hdr = self.horizontalHeader()
        hdr.setDefaultAlignment(Qt.AlignCenter)      # centres single- and two-line labels
        hdr.setContextMenuPolicy(Qt.CustomContextMenu)
        hdr.customContextMenuRequested.connect(self._column_menu)

    def retheme(self) -> None:
        """Re-apply the palette after a theme switch: refresh the error-row
        background colour and the item-view stylesheet, then repaint (the model's
        BackgroundRole reads the now-updated module colour)."""
        global _ERROR_BG
        _ERROR_BG = QColor(VizTheme.SEM_ERROR_BG)
        self.setStyleSheet(analyzer_stylesheet())
        self.viewport().update()

    def fit_columns(self) -> None:
        """Size every column to its content. Response is then widened to its TRUE widest
        text across ALL rows (resizeColumnsToContents only samples ~1000 rows, so a long
        response deeper in the capture would otherwise be elided with no way to scroll to
        it) — so the whole response is revealed by scrolling the table right. No cap."""
        self.resizeColumnsToContents()
        model = self.model()
        if model is None:
            return
        rcol = _COL["Response"]
        fm = self.fontMetrics()
        widest = self.columnWidth(rcol)
        for r in range(model.rowCount()):
            text = model.data(model.index(r, rcol), Qt.DisplayRole)
            if text:
                widest = max(widest, fm.horizontalAdvance(str(text)) + 24)   # + cell padding
        self.setColumnWidth(rcol, widest)

    def _column_menu(self, pos) -> None:
        if self.model() is None:
            return
        menu = QMenu(self)
        menu.addAction("Show / hide columns").setEnabled(False)
        menu.addSeparator()
        hdr = self.horizontalHeader()
        for c in range(self.model().columnCount()):
            name = self.model().headerData(c, Qt.Horizontal, Qt.DisplayRole)
            act = menu.addAction(str(name).replace("\n", " "))    # flatten wrapped labels
            act.setCheckable(True)
            act.setChecked(not self.isColumnHidden(c))
            # Keep at least one column visible.
            act.toggled.connect(lambda on, col=c: self.setColumnHidden(col, not on))
        menu.exec(hdr.mapToGlobal(pos))


class CommandFilterProxy(QSortFilterProxyModel):
    """Row filter over the command model: boolean free-text, multi-select command
    kind / addressed device / commit group, and an errors-only toggle. Sorts via a
    numeric sort role so Row/Time/masks order correctly.

    Free text supports `and` / `or` and parentheses over substring terms, e.g.
    `(write or read) and dp0`. AND binds tighter than OR; a multi-word run with no
    operator is a literal substring; adjacency implies AND."""

    def __init__(self) -> None:
        super().__init__()
        self._text_ast = None          # parsed boolean query (None = match all)
        self._kinds: set = set()       # empty = all command kinds
        self._errors_only = False
        self._devices: set = set()     # empty = all; else device numbers to match
        self._groups: set = set()      # empty = all; else commit-group numbers
        self.setSortRole(SORT_ROLE)

    def set_text(self, text: str) -> None:
        self._text_ast = parse_filter(text)
        self.invalidateFilter()

    def set_kinds(self, kinds) -> None:
        """Show only these command kinds (empty set = all)."""
        self._kinds = set(kinds or ())
        self.invalidateFilter()

    def set_errors_only(self, on: bool) -> None:
        self._errors_only = bool(on)
        self.invalidateFilter()

    def set_devices(self, devices) -> None:
        """Show only commands addressing any of these device numbers (empty = all)."""
        self._devices = {int(d) for d in (devices or ())}
        self.invalidateFilter()

    def set_groups(self, groups) -> None:
        """Show only commits in any of these commit groups (empty = all). Selecting
        groups hides non-commit commands, which have no group."""
        self._groups = {int(g) for g in (groups or ())}
        self.invalidateFilter()

    def describe_active(self) -> list:
        """Short labels for the currently-active filters (for the title indicator)."""
        bits = []
        if self._kinds:
            bits.append("Cmd: " + ", ".join(sorted(self._kinds)))
        if self._devices:
            bits.append("Dev " + ", ".join(str(d) for d in sorted(self._devices)))
        if self._groups:
            bits.append("Grp " + ", ".join(str(g) for g in sorted(self._groups)))
        if self._errors_only:
            bits.append("Errors")
        if self._text_ast is not None:
            bits.append("expr")
        return bits

    def filterAcceptsRow(self, row: int, parent: QModelIndex) -> bool:
        model = self.sourceModel()
        cmd = model.command_at(row)
        if cmd is None:
            return True
        if self._kinds and cmd.get("command") not in self._kinds:
            return False
        if self._errors_only and not is_error(cmd):
            return False
        if self._devices:
            dm = int(cmd.get("device_mask", 0))
            if not any(dm & (1 << d) for d in self._devices):
                return False
        if self._groups:
            if not cmd.get("is_commit"):
                return False
            gm = int(cmd.get("group_mask", 0))
            if not any(gm & (1 << g) for g in self._groups):
                return False
        if self._text_ast is not None:
            if not eval_filter(self._text_ast, model.search_text(row)):
                return False
        return True
