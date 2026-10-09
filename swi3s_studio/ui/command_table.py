"""Command table: a virtual model over the decoded command stream.

Address is resolved to register + field names via the RegisterMap, so a
WriteA32 reads e.g. "SLC.NumColumns" with a "NumColumns=3" tooltip.
"""
from __future__ import annotations

import re
from typing import List, Optional

import numpy as np
from PySide6.QtCore import QAbstractTableModel, QModelIndex, QSortFilterProxyModel, Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QAbstractItemView, QMenu, QTableView, QWidget

from ..analysis import command_error, is_error
from ..analysis.responses import response_summary
from ..model import RegisterMap
from .row_origin import RowOriginMixin
from .theme import VizTheme, analyzer_stylesheet

_ERROR_BG = QColor(VizTheme.SEM_ERROR_BG)

# Row error shading + the "Errors only" filter both key off analysis.errors'
# is_error/command_error — that predicate now also covers a decoder-flagged
# EnableCh_CURR write with Interval != 1 Row (native Decoder::feed / CommandRec::
# enablechCurrError; see analysis/errors.command_error), so this module no longer
# keeps its own separate check — one predicate, so the two surfaces can't drift.
_row_is_error = is_error
_row_error_tooltip = command_error


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
        labels, response decode, …) of every row on each keystroke.

        THE CACHE IS COLD AFTER EVERY MODEL RESET (set_commands / set_peripheral_maps), and
        the first keystroke after one is what pays to fill it: QSortFilterProxyModel sweeps
        EVERY source row on the first invalidateFilter, so the whole table is rendered on the
        GUI thread in one go. Measured before this was factored out of data(): 0.58 s at 10k
        commands, 2.9 s at 50k, 11.5 s at 200k — with the second keystroke at 0.14 s, because
        by then the cache is warm. So it was a freeze once per load, not sustained lag.

        Built via _display_text rather than self.data(self.index(row, c)) for that reason: the
        index+role path cost twelve QModelIndex constructions and twelve role dispatches per
        row on top of the formatting that is actually needed. The filter's own debounce (see
        main_window's expression dialog) keeps the sweep to one per typing pause rather than
        one per character.
        """
        s = self._search_cache.get(row)
        if s is None:
            cmd = self._cmds[row]
            s = " ".join(str(self._display_text(cmd, n) or "") for n in _COLUMNS).lower()
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
            return _ERROR_BG if _row_is_error(cmd) else None
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
            err = _row_error_tooltip(cmd)
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
        return self._display_text(cmd, name)

    def _display_text(self, cmd: dict, name: str):
        """The DisplayRole text for one column of one command.

        ONE DEFINITION, called by data() above and by search_text() below. The free-text
        filter has to match what the user can SEE, so a second formatter here would be a
        divergence waiting to happen — a column whose rendering changed would silently stop
        being searchable the way it looks.

        TAKES (cmd, name) RATHER THAN A QModelIndex, which is the point: search_text can then
        build a row's text without constructing twelve QModelIndex objects and re-entering
        data()'s role dispatch per cell. That was 2.4 M Qt round-trips for one keystroke on a
        200k-command capture — see search_text for what it cost.
        """
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
        # Per-cell selection (spreadsheet-style): an obvious cell copy, plus drag-out
        # range copy. Safe even though this table drives the shared cursor, because the
        # cursor follows the CURRENT cell's row (main_window: currentChanged ->
        # _on_command_selected), not selectedRows() — so a partial-row cell selection
        # doesn't break the sync. A cursor move from another pane selects the whole row.
        from .copyable import enable_copy
        enable_copy(self, cells=True)
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
        """Size every column to its content. Response and Row are then widened toward their
        widest text (resizeColumnsToContents only samples ~1000 rows, so a long response —
        or the large bus-row number deep in a big capture — would otherwise be elided) — but
        only across a BOUNDED head+tail sample, not all rows: scanning every row of a
        200k-command capture cost ~5.5 s on the GUI thread on every (re-)decode. The Row
        number is ~monotonic (its max is at the tail), and resizeColumnsToContents already
        measured the head, so the extremes are covered; a rare very-long Response in the
        deep middle may clip until scrolled to (acceptable — it's a column width, not data)."""
        self.resizeColumnsToContents()
        model = self.model()
        if model is None:
            return
        fm = self.fontMetrics()
        n = model.rowCount()
        _SAMPLE = 2000                                     # head + tail rows measured per column
        rows = (range(n) if n <= 2 * _SAMPLE
                else [*range(_SAMPLE), *range(n - _SAMPLE, n)])

        def widen_to_true_max(col: int) -> None:
            widest = self.columnWidth(col)
            for r in rows:
                text = model.data(model.index(r, col), Qt.DisplayRole)
                if text:
                    widest = max(widest, fm.horizontalAdvance(str(text)) + 24)   # + cell padding
            self.setColumnWidth(col, widest)

        # By header, not _COL index: the All Links view leads with a Link column.
        headers = [model.headerData(c, Qt.Horizontal, Qt.DisplayRole)
                   for c in range(model.columnCount())]
        for name in ("Response", "Row"):
            if name in headers:
                widen_to_true_max(headers.index(name))

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


# begin/endFilterChange() arrived in Qt 6.10, which deprecates invalidateFilter().
_HAS_FILTER_CHANGE = hasattr(QSortFilterProxyModel, "beginFilterChange")


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
        self._kinds_none = False       # Commands ▸ None: no kind at all (not "empty = all")
        self._errors_only = False
        self._devices: set = set()     # empty = all; else device numbers to match
        self._groups: set = set()      # empty = all; else commit-group numbers
        self.setSortRole(SORT_ROLE)

    def _refilter(self, apply) -> None:
        """Run `apply()` (which changes a criterion) as one filter change. Qt 6.10
        deprecates invalidateFilter() for begin/endFilterChange() bracketing the change;
        the project supports PySide6 from 6.6, which has only the former."""
        if _HAS_FILTER_CHANGE:
            self.beginFilterChange()
            apply()
            self.endFilterChange()
        else:
            apply()
            self.invalidateFilter()

    def set_text(self, text: str) -> None:
        ast = parse_filter(text)
        self._text_src = str(text)
        self._refilter(lambda: setattr(self, "_text_ast", ast))

    def filter_text(self) -> str:
        """The text filter as typed (the parsed form is what filters)."""
        return getattr(self, "_text_src", "")

    def set_kinds(self, kinds, none: bool = False) -> None:
        """Show only these command kinds (empty set = all), or with `none`, no command."""
        def apply():
            self._kinds = set(kinds or ())
            self._kinds_none = bool(none)
        self._refilter(apply)

    def set_errors_only(self, on: bool) -> None:
        self._refilter(lambda: setattr(self, "_errors_only", bool(on)))

    def set_devices(self, devices) -> None:
        """Show only commands addressing any of these device numbers (empty = all)."""
        wanted = {int(d) for d in (devices or ())}
        self._refilter(lambda: setattr(self, "_devices", wanted))

    def set_groups(self, groups) -> None:
        """Show only commits in any of these commit groups (empty = all). Selecting
        groups hides non-commit commands, which have no group."""
        wanted = {int(g) for g in (groups or ())}
        self._refilter(lambda: setattr(self, "_groups", wanted))

    def describe_active(self) -> list:
        """Short labels for the currently-active filters (for the title indicator)."""
        bits = []
        if self._kinds_none:
            bits.append("Cmd: none")
        elif self._kinds:
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
        if self._kinds_none or (self._kinds and cmd.get("command") not in self._kinds):
            return False
        if self._errors_only and not _row_is_error(cmd):
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


class AllLinksCommandModel(QAbstractTableModel):
    """Every Link's commands in one table, in GLOBAL time order, led by a Link column.

    Each cell is drawn by its own Link's CommandTableModel, so this view and the per-Link
    view cannot disagree. The one exception is Time, which shows GLOBAL time (the Link's
    offset applied): two Links' local times share no origin once an offset is set.

    `parts` is one (name, model, positions_ps, time_offset_ps) per Link. positions_ps are
    the row-aligned global times of each command's cursor position, which is also what
    rows are ordered by (ties keep Link order), so a cursor instant bisects to a row.
    """

    TIME_COLUMN = 1 + _COL["Time (µs)"]        # after the leading Link column

    def __init__(self, parts) -> None:
        super().__init__()
        self._names = [p[0] for p in parts]
        self._models = [p[1] for p in parts]
        self._offsets = [float(p[3]) for p in parts]
        pos = [np.asarray(p[2], dtype=np.float64) for p in parts]
        link = np.concatenate([np.full(len(a), i, dtype=np.int64)
                               for i, a in enumerate(pos)]) if pos else np.zeros(0, np.int64)
        row = np.concatenate([np.arange(len(a), dtype=np.int64)
                              for a in pos]) if pos else np.zeros(0, np.int64)
        ps = np.concatenate(pos) if pos else np.zeros(0)
        order = np.lexsort((row, link, ps))            # by time, then Link, then row
        self._link = link[order]
        self._row = row[order]
        self.positions_ps = ps[order]                  # ascending: bisect a cursor instant
        self._search_cache: dict = {}

    def rowCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._row)

    def columnCount(self, parent=QModelIndex()) -> int:
        return 1 + len(_COLUMNS)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if role == Qt.DisplayRole and orientation == Qt.Horizontal:
            if section == 0:
                return "Link"
            name = _COLUMNS[section - 1]
            return _WRAP_HEADERS.get(name, name)
        return None

    def link_at(self, row: int) -> int:
        return int(self._link[row])

    def source_row_at(self, row: int) -> int:
        """The row in its own Link's command model."""
        return int(self._row[row])

    def command_at(self, row: int) -> Optional[dict]:
        if not 0 <= row < len(self._row):
            return None
        return self._models[int(self._link[row])].command_at(int(self._row[row]))

    def search_text(self, row: int) -> str:
        s = self._search_cache.get(row)
        if s is None:
            li, r = int(self._link[row]), int(self._row[row])
            s = f"{self._names[li].lower()} {self._models[li].search_text(r)}"
            self._search_cache[row] = s
        return s

    def _global_us(self, li: int, cmd: dict) -> float:
        model = self._models[li]
        return (int(cmd.get("start_sample", 0)) / model._rate * 1e6
                + self._offsets[li] / 1e6)

    def data(self, index: QModelIndex, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        li, r = int(self._link[index.row()]), int(self._row[index.row()])
        if index.column() == 0:
            if role == Qt.DisplayRole:
                return self._names[li]
            if role == SORT_ROLE:
                return li
            return None
        name = _COLUMNS[index.column() - 1]
        model = self._models[li]
        if name == "Time (µs)" and role in (Qt.DisplayRole, SORT_ROLE):
            us = self._global_us(li, model.command_at(r))
            return f"{us:,.2f}" if role == Qt.DisplayRole else us
        return model.data(model.index(r, index.column() - 1), role)
