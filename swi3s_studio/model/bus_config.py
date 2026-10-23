"""Authoring document for Visualization mode: a bus configuration built from
scratch (an Interface + 12 DataPorts, each with its Flow Control Port fields).

This mirrors the standalone SWI3S Visualizer's config vocabulary (the `_REG`
fields in its `CSVFields`). It serialises to the **same v2.0 CSV** the C++ core's
`SwI3sConfig::LoadCsv` reads (`native/swi3score/core/CDpConfig.cpp`), so
the authored config is placed by the *same* verified cascade used for decoded
captures: `BusConfig.to_csv_file()` → `swi3score.grid_from_csv()` for the grid,
and `swi3score.registers_from_csv()` for the expected register writes.

Values are register-level (excess-1 encoded where the field is — the placement
core adds the +1 internally), exactly as the Visualizer's `_REG` columns.
"""
from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field, fields
from typing import List, Tuple

NUM_DATA_PORTS = 12

# Per-source CDS guard/tail: the CDS (Column 0) is time-multiplexed, so the Manager
# and each peripheral device can drive their own guard polarity / tail width without a
# bus clash. Stored as a list of CDS_NUM_SOURCES values, index 0 = Manager, index i =
# Device (i-1). Guard values: 0 Off, 1 G0, 2 G1 (and 3 = "Gx" mixed, used only by the
# peripheral-aggregate helper, never stored per source).
CDS_NUM_SOURCES = 13                          # Manager + Device 0..11
CDS_GUARD_OFF, CDS_GUARD_G0, CDS_GUARD_G1, CDS_GUARD_GX = 0, 1, 2, 3

# CDS_DriveType (registers.json CDS block, offset 0x86 bit 7, the spec's encoding):
# 0 = Special (PassiveOne / ActiveZero), where a CDS bit of 1 is
# left high-Z and the Manager's bus keeper holds the level, which affects NRZS decode;
# 1 = Normal, where both levels are actively driven. PHY3 mandates Normal; PHY1/2 make it
# optional.
#
# PER SOURCE, like the guard and the tail. The register lives in each device's own CDS
# block, so the Manager and each of devices 0-11 carry their own value — it is not one
# bus-wide bit. Stored as a CDS_NUM_SOURCES list on the same index convention (0 =
# Manager, i = Device i-1).
CDS_DRIVE_SPECIAL, CDS_DRIVE_NORMAL = 0, 1
CDS_DRIVE_LABELS = {CDS_DRIVE_SPECIAL: "Special (passive 1)",
                    CDS_DRIVE_NORMAL: "Normal (actively driven)"}

# CDS_EndDriveEarly (CDS block 0x87 bit 4). The spec's "Control Data Stream Transport
# Register Fields (CDS_*)" table, verbatim:
#   0: Drive to the end of the UI.
#   1: Stop driving before the end of the last UI.
# Per source, like the drive type: the register is in each device's own CDS block. PHY3
# mandatory, PHY2 optional, PHY1 read-only 0.
#
# No duration is modelled: the granularity is bits, not UIs (a Wide Bit / guard / tail
# group gets one early release, Sec. 10.1.14 "Scope of EndDriveEarly"), and the exact point
# is ImpDef inside a PHY-specific bound (PHY2: Per_tZD_Actual + Per_tRampTime_Actual +
# 3.0 ns to UI - 2.0 ns, the "PHY2 Output Timing: Effect of EndDriveEarly" table). The name
# HALF_EARLY denotes the register value 1, not a duration.
#
# Every source defaults to the register's reset, 0 (drive to the end of the UI). The
# neighbouring CDS_DriveType defaults to Normal, opposite to its reset.
#
# Not the timing calculator's `Man_ede` / `Per_ede`, which model EndDriveEarly for a DATA
# handover (swi3s_studio/timing/); this is the CDS bit's own register.
CDS_EDE_FULL_UI, CDS_EDE_HALF_EARLY = 0, 1
CDS_EDE_LABELS = {CDS_EDE_FULL_UI: "Full", CDS_EDE_HALF_EARLY: "Early"}

# Per-source CDS fields that serialise as ONE plain 13-wide integer CSV row:
# (csv_key, attribute, default per source). The guard is NOT here — it splits into an
# enable row and a polarity row to mirror its two registers, and is handled on its own.
#
# Both engines' CSV loops read this shape. Adding a field: one entry here, one in the swviz
# handler's equivalent, and a dialog.
_CDS_PER_SOURCE_INT_ROWS = (
    ("CDS_TailWidthPerSource", "cds_tail", 0),
    ("CDS_DriveTypePerSource", "cds_drive_type", CDS_DRIVE_NORMAL),
    ("CDS_EndDriveEarlyPerSource", "cds_end_drive_early", CDS_EDE_FULL_UI),
)


def _cds_flag(values, odd_value: int, name: str) -> str:
    """One CDS cell flag, or "" — `name` when every source is at `odd_value`, `name`+"x" when
    only some are.

    The trailing `x` follows the guard labels' `Gx` convention for "the sources disagree"
    (GridView._cds_guard_core): the CDS bit is one cell driven by each source in turn.
    """
    kinds = {int(v) == odd_value for v in values}
    if kinds == {True}:
        return name
    return f"{name}x" if True in kinds else ""


def cds_symbol(drive_type, end_drive_early=None) -> str:
    """What a CDS cell is labelled: "CDS", or "CDS" over a line of flags.

    The cell reads

        CDS            (nothing unusual: every source drives both levels for the full UI)
        CDS / SP       (every source leaves a CDS 1 high-Z for the bus keeper)
        CDS / SPx      (only some do)
        CDS / SP EDE   (...and every source also stops driving before the UI ends)
        CDS / SPx EDEx (both, and the sources disagree about both)

    The flags line is omitted when no flag applies.

    Returned as one string with a newline; the renderer splits it to insert a merged run's
    " x{n}" into the first line, and sizes the CDS column to whichever line is wider (see
    GridView._cds_label).

    Both arguments accept a bare int (one device's value, or a legacy scalar) or a list.
    """
    def _as_list(v):
        return [v] if isinstance(v, (int, bool)) else list(v)

    flags = [_cds_flag(_as_list(drive_type), CDS_DRIVE_SPECIAL, "SP")]
    if end_drive_early is not None:
        flags.append(_cds_flag(_as_list(end_drive_early), CDS_EDE_HALF_EARLY, "EDE"))
    line2 = " ".join(f for f in flags if f)
    return f"CDS\n{line2}" if line2 else "CDS"


def cds_src_index(device_number: int) -> int:
    """Per-source index for a source: Manager (device_number -1) -> 0, Device d -> d+1."""
    return 0 if int(device_number) == -1 else int(device_number) + 1

# Per-dataport fields: (csv_key, attribute, kind). `kind` is "int" or "bool".
# Order here is also the row order shown in the authoring table.
DP_FIELDS = [
    ("Enabled", "enabled", "bool"),
    ("DeviceNumber_REG", "device_number", "int"),
    ("PortDirection_REG", "port_direction", "bool"),       # True = Sink, False = Source
    ("EnableCh_REG", "enable_ch", "int"),                  # channel-enable bitmask
    ("SampleSize_REG", "sample_size", "int"),
    ("SampleGrouping_REG", "sample_grouping", "int"),
    ("ChannelGrouping_REG", "channel_grouping", "int"),
    ("Spacing_REG", "spacing", "int"),
    ("Interval_REG", "interval", "int"),
    ("Offset_REG", "offset", "int"),
    ("HorizontalStart_REG", "horizontal_start", "int"),
    ("HorizontalCount_REG", "horizontal_count", "int"),
    ("BitWidth_REG", "bit_width", "int"),
    ("TailWidth_REG", "tail_width", "int"),
    ("SkippingNumerator_REG", "skipping_numerator", "int"),
    ("SubRowInterval_REG", "sub_row_interval", "bool"),
    ("FlowMode_REG", "flow_mode", "int"),                  # 0 Normal 1 Tx 2 Rx 3 Async
    ("PortMode_REG", "port_mode", "int"),                  # 0 Normal 2 TestOnes 3 TestZeros
    ("ScramblerEn_REG", "scrambler_en", "bool"),
    ("GuardEnable_REG", "guard_enable", "bool"),
    ("GuardPolarity_REG", "guard_polarity", "bool"),
    ("FCP_HorizontalStart_REG", "fcp_horizontal_start", "int"),
    ("FCP_BitWidth_REG", "fcp_bit_width", "int"),
    ("FCP_TailWidth_REG", "fcp_tail_width", "int"),
    ("FCP_Offset_REG", "fcp_offset", "int"),
    ("FCP_GuardEnable_REG", "fcp_guard_enable", "bool"),
    ("FCP_GuardPolarity_REG", "fcp_guard_polarity", "bool"),
    ("EnforceHandover", "enforce_handover", "bool"),       # visualizer display flag
    ("Name", "name", "str"),                               # per-DP display name
    ("DataPortNumber", "dp_number", "int"),                # logical DP number 0-31 (v3.0.0);
                                                           # written resolved, read into dp_number
]

# Interface fields: (csv_key, attribute, kind). Mirrors the Visualizer's
# INTERFACE_FIELD_NAMES so a saved config round-trips its full "Other Parameters"
# set. The S0/S1/CDS fields and RowsToDraw are layout/visualizer-level — the C++
# placement core reads only NumColumns/SkippingDenominator/PHY3/RowRate, so they
# round-trip but don't (yet) alter Studio's simplified grid.
IFACE_FIELDS = [
    ("NumColumns_REG", "num_columns", "int"),              # excess-1: columns = +1
    ("SkippingDenominator_REG", "skipping_denominator", "int"),
    ("PHY3Enabled", "phy3_enabled", "bool"),
    ("S0Width", "s0_width", "int"),
    ("S1TailWidth_REG", "s1_tail_width", "int"),
    ("EnforceS1Handover", "enforce_s1_handover", "bool"),
    ("CDS_BitWidth_REG", "cds_bit_width", "int"),
    # CDS guard/tail/drive-type are per-source (see to_csv/from_csv_text); the legacy
    # CDS_GuardEnabled_REG / CDS_GuardPolarity_REG / CDS_TailWidth_REG keys are written
    # (derived) and read (migrated) there, not through this scalar map.
    ("EnforceCDSHandover", "enforce_cds_handover", "bool"),
    ("RowRate", "row_rate_khz", "float"),
    ("RowsToDraw", "rows_to_draw", "int"),
]


def _disp_letters(df: int) -> str:
    """display_fields bitmask (1=Sample 2=Channel 4=Bit) -> the 'scb' letter string
    the Visualizer's CSV uses."""
    return "".join(c for c, b in (("s", 1), ("c", 2), ("b", 4)) if int(df) & b)


def _disp_bits(letters: str) -> int:
    """Inverse of _disp_letters: 'scb' letter string -> display_fields bitmask."""
    s = str(letters).lower()
    return sum(b for c, b in (("s", 1), ("c", 2), ("b", 4)) if c in s)


def _fmt(value, kind: str) -> str:
    if kind == "bool":
        return "True" if value else "False"
    if kind == "float":
        return f"{value:g}"
    if kind == "str":
        return str(value)
    return str(int(value))


def _report_csv_reading(source: str, bad: dict, short: List[str], extra: List[str],
                        unknown: List[str]) -> None:
    """from_csv_text's import warnings for `source`, one per kind."""
    from .. import import_notes
    for key, cells in bad.items():
        n = len(cells)
        import_notes.warn(source, f"{key}: {n} {'cells are' if n != 1 else 'cell is'} not a "
                                  f"valid value: {', '.join(cells)}.")
    if short:
        import_notes.warn(source, "rows with fewer values than data ports or CDS sources, "
                                  "the missing cells reading as their default: "
                                  f"{', '.join(short)}.")
    if extra:
        import_notes.warn(source, "values past the last data port or CDS source column "
                                  f"are ignored: {', '.join(extra)}.")
    if unknown:
        n = len(unknown)
        import_notes.warn(source, f"{n} {'fields are' if n != 1 else 'field is'} not "
                                  f"recognised and {'were' if n != 1 else 'was'} ignored: "
                                  f"{', '.join(unknown)}.")


def _reads_as_written(raw: str, kind: str) -> bool:
    """Whether _parse reads `raw` as written: blank, a recognised boolean, a whole number
    (decimal, 0x, 0b) or a number, by `kind`."""
    s = (raw or "").strip()
    if not s or kind == "str":
        return True
    if kind == "bool":
        return s.lower() in ("true", "1", "yes", "false", "0", "no")
    try:
        if kind == "float":
            float(s)
        elif s[:2].lower() == "0b":
            int(s[2:], 2)
        elif s[:2].lower() == "0x":
            int(s[2:], 16)
        else:
            int(s)
    except ValueError:
        return False
    return True


def _parse(raw: str, kind: str):
    s = (raw or "").strip()
    if kind == "bool":
        return s.lower() in ("true", "1", "yes")
    if kind == "str":
        return raw if raw is not None else ""
    if kind == "float":
        try:
            return float(s)
        except ValueError:
            return 0.0
    if not s:
        return 0
    try:
        if s[:2].lower() == "0b":
            return int(s[2:], 2)
        if s[:2].lower() == "0x":
            return int(s[2:], 16)
        return int(s)
    except ValueError:
        return 0


# The Manager's device_number. The device register cannot hold it, which is why a config
# CSV encodes the Manager as device 0 plus the ManagerDataport flag.
MANAGER_DEVICE = -1


def csv_device_identity(device_number: int) -> Tuple[int, bool]:
    """How a config CSV will ENCODE this device number: (DeviceNumber_REG, ManagerDataport).

    The register cannot hold the −1 Manager sentinel, so the CSV carries device 0 plus a
    boolean flag. Anything outside {−1} ∪ [0, 11] therefore collapses onto plain device 0 —
    the encoding has nowhere else to put it.

    The single definition of the encoding, shared by the CSV writer and
    `duplicate_dp_numbers`. Keying on the encoded pair keeps the Manager distinct from
    device 0; that pair is legitimate, not a clash.
    """
    dn = int(device_number)
    return (max(0, dn), dn == MANAGER_DEVICE)


@dataclass
class DataPortConfig:
    enabled: bool = False
    device_number: int = 0
    port_direction: bool = False        # True = Sink, False = Source
    enable_ch: int = 0                  # channel-enable bitmask (e.g. 0b11 = ch 0,1)
    sample_size: int = 0                # excess-1 (bits = +1)
    sample_grouping: int = 0            # excess-1
    channel_grouping: int = 0
    spacing: int = 0
    interval: int = 0                   # excess-1 (rows = +1)
    offset: int = 0
    horizontal_start: int = 0
    horizontal_count: int = 0           # excess-1 (cols owned = +1)
    bit_width: int = 0                  # excess-1 (wide-bit replay = +1)
    tail_width: int = 0
    skipping_numerator: int = 0
    sub_row_interval: bool = False
    flow_mode: int = 0
    port_mode: int = 0
    scrambler_en: bool = True           # PortControl.ScramblerEn resets to 1
    guard_enable: bool = False
    guard_polarity: bool = False
    enforce_handover: bool = True       # visualizer display flag
    name: str = ""                      # per-DP display name (defaults to "DP{n}")
    dp_number: int = -1                 # logical data-port number 0-31 (analyzer DP id);
                                        # -1 = unset, resolves to the slot index. See number().
    display_fields: int = 6             # bitmask: 1=Sample 2=Channel 4=Bit (cell labels);
                                        # 6 = Channel|Bit matches the Visualizer default
    fcp_horizontal_start: int = 0
    fcp_bit_width: int = 0
    fcp_tail_width: int = 0
    fcp_offset: int = 0
    fcp_guard_enable: bool = False
    fcp_guard_polarity: bool = False

    def num_channels(self) -> int:
        return bin(self.enable_ch & 0xFFFF).count("1")

    def number(self, slot_index: int) -> int:
        """Logical data-port number 0-31. Falls back to the slot index when unset
        (dp_number < 0), as in a config without an explicit number."""
        return slot_index if self.dp_number < 0 else self.dp_number


@dataclass
class BusConfig:
    num_columns: int = 15               # register value; column count = num_columns + 1
    row_rate_khz: float = 3072.0
    skipping_denominator: int = 16
    phy3_enabled: bool = False
    s0_width: int = 1
    s1_tail_width: int = 0
    enforce_s1_handover: bool = True
    cds_bit_width: int = 0
    # Per-source CDS guard/tail (index 0 = Manager, 1..12 = Device 0..11). Guard:
    # 0 Off, 1 G0, 2 G1. Tail: width 0-3. The cds_guard_enabled/polarity/tail_width
    # derived properties (below) serve single-value consumers and the legacy CSV keys.
    cds_guard: List[int] = field(default_factory=lambda: [0] * CDS_NUM_SOURCES)
    cds_tail: List[int] = field(default_factory=lambda: [0] * CDS_NUM_SOURCES)
    # Per-source CDS_DriveType (see the constants above): the register is in each device's
    # own CDS block, so this is a 13-entry list on the same index convention as the guard
    # and the tail. Defaults to Normal for every source, not the register's reset of 0
    # (Special): a config that omits the field reads as Normal.
    cds_drive_type: List[int] = field(
        default_factory=lambda: [CDS_DRIVE_NORMAL] * CDS_NUM_SOURCES)
    # Per-source CDS_EndDriveEarly (see the constants above): 0 full UI, 1 stop early.
    # Defaults to the register's reset.
    cds_end_drive_early: List[int] = field(
        default_factory=lambda: [CDS_EDE_FULL_UI] * CDS_NUM_SOURCES)
    enforce_cds_handover: bool = True
    rows_to_draw: int = 64
    description: str = ""
    dataports: List[DataPortConfig] = field(
        default_factory=lambda: [DataPortConfig() for _ in range(NUM_DATA_PORTS)])

    # ---- derived CDS guard/tail views (legacy names + rendering aggregates) ----
    @property
    def cds_guard_enabled(self) -> bool:
        """Any source drives a CDS guard (drives the 'CDS Guard Enabled' checkbox and
        whether the frame reserves a guard column)."""
        return any(self.cds_guard)

    @property
    def cds_tail_enabled(self) -> bool:
        """Any source drives a CDS tail ('CDS Tail Enabled' checkbox)."""
        return any(w > 0 for w in self.cds_tail)

    @property
    def cds_tail_width(self) -> int:
        """Frame tail-column budget = the widest tail across all sources."""
        return max(self.cds_tail) if self.cds_tail else 0

    @property
    def cds_guard_polarity(self) -> bool:
        """Legacy single polarity = the Manager's (True = G1). For CSV back-compat."""
        return self.cds_guard[0] == CDS_GUARD_G1

    def cds_peripheral_guard(self) -> int:
        """Aggregate device (peripheral) guard for the split-cell's lower half:
        0 Off (no device guard), 1 all-G0, 2 all-G1, 3 Gx (enabled devices disagree)."""
        pol = {g for g in self.cds_guard[1:] if g}
        if not pol:
            return CDS_GUARD_OFF
        if pol == {CDS_GUARD_G0}:
            return CDS_GUARD_G0
        if pol == {CDS_GUARD_G1}:
            return CDS_GUARD_G1
        return CDS_GUARD_GX

    def cds_peripheral_tail(self) -> int:
        """Widest device (peripheral) tail — the lower half's extent."""
        return max(self.cds_tail[1:], default=0)

    def cds_peripheral_tail_mixed(self) -> bool:
        """True if enabled device tails have differing widths (lower half is a max)."""
        return len({w for w in self.cds_tail[1:] if w}) > 1

    def column_count(self) -> int:
        return int(self.num_columns) + 1

    def duplicate_dp_numbers(self) -> List[int]:
        """Slot indices that share a (device_number, dp_number) pair with another
        slot — ALL members of each colliding group, not just the later one, so a
        caller catches the collision no matter which slot was edited. A device may
        not reuse a data-port number; the same number across different devices is
        fine. (Slots default to distinct numbers = their column index, so collisions
        only arise from explicit edits.)

        Keyed on the encoded (device, is_manager) pair from `csv_device_identity`, not the
        raw attribute: the writer collapses anything outside {−1} ∪ [0, 11] onto device 0,
        so two ports at −2 and −9 sharing a dp_number collide, while the Manager and a
        device 0 do not.
        """
        by_key: dict = {}
        for i, dp in enumerate(self.dataports):
            by_key.setdefault((csv_device_identity(dp.device_number), dp.number(i)),
                              []).append(i)
        dups: List[int] = []
        for slots in by_key.values():
            if len(slots) > 1:
                dups.extend(slots)
        return sorted(dups)

    # ---- serialisation to the v2.0 CSV the C++ core reads ----
    def to_csv(self) -> str:
        from swi3s_studio.swviz.version import app_version
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["AppVersion", app_version()])   # live Studio version, not a literal
        for key, attr, kind in IFACE_FIELDS:
            w.writerow([key, _fmt(getattr(self, attr), kind)])
        # CDS guard/tail: authoritative per-source rows (Manager, Device 0..11). Guard
        # is split into enabled + polarity rows mirroring the CDS_GuardEnabled_REG /
        # CDS_GuardPolarity_REG register names; internally cds_guard packs both (0=off,
        # 1=G0, 2=G1). The scalar CDS_Guard*/Tail summary rows are not written; they are
        # derived from these on load.
        w.writerow(["CDS_GuardEnabledPerSource"] + ["1" if g != 0 else "0" for g in self.cds_guard])
        w.writerow(["CDS_GuardPolarityPerSource"] + ["1" if g == CDS_GUARD_G1 else "0" for g in self.cds_guard])
        # The plain 13-wide integer rows (tail, drive type, end-drive-early). Always
        # written, so a saved file is explicit rather than relying on a reader's default.
        for key, attr, _default in _CDS_PER_SOURCE_INT_ROWS:
            w.writerow([key] + [str(int(v)) for v in getattr(self, attr)])
        if self.description:
            w.writerow(["Description", self.description])
        for key, attr, kind in DP_FIELDS:
            if attr == "name":
                # Always write a name per DP (default "DP{i}", like the Visualizer),
                # not an empty cell for un-renamed ports.
                w.writerow([key] + [(dp.name or f"DP{i}")
                                    for i, dp in enumerate(self.dataports)])
            elif attr == "dp_number":
                # Write the RESOLVED logical number (sentinel -1 -> slot index), so the
                # analyzer/converter always read a concrete DP number per column.
                w.writerow([key] + [str(dp.number(i))
                                    for i, dp in enumerate(self.dataports)])
            elif attr == "device_number":
                # Manager (device -1) is encoded as device 0 + ManagerDataport=True
                # (the Visualizer's convention); the flag row is written below. Both rows
                # come from csv_device_identity, shared with duplicate_dp_numbers.
                w.writerow([key] + [str(csv_device_identity(dp.device_number)[0])
                                    for dp in self.dataports])
            else:
                w.writerow([key] + [_fmt(getattr(dp, attr), kind) for dp in self.dataports])
        # Visualizer viz-fields the engine reads for label/manager semantics.
        w.writerow(["ManagerDataport"] + ["True" if dp.device_number == -1 else "False"
                                          for dp in self.dataports])
        w.writerow(["DisplayFields"] + [_disp_letters(dp.display_fields)
                                        for dp in self.dataports])
        return buf.getvalue()

    def to_csv_file(self, path: str) -> str:
        with open(path, "w", newline="", encoding="utf-8") as f:
            f.write(self.to_csv())
        return path

    @classmethod
    def from_csv(cls, path: str, source: str = "") -> "BusConfig":
        """Read the CSV at `path`; import warnings name `source` (default `path`)."""
        from .. import import_notes
        with open(path, newline="", encoding="utf-8") as f:
            return cls.from_csv_text(f.read(), source=import_notes.source_name(source or path))

    @classmethod
    def from_csv_text(cls, text: str, source: str = "") -> "BusConfig":
        """Parse config-CSV `text`. What is not read as written is reported through
        import_notes for `source`, one warning per kind: cells that are not a valid value
        (read as 0, 0.0 or False), rows with fewer values than data ports or CDS sources
        (the missing cells read as their default), values past the last column, and
        fields not recognised."""
        cfg = cls()
        bad: dict = {}                  # field -> ["DP2 'abc' read as 0", ...], in row order
        short: List[str] = []
        extra: List[str] = []
        unknown: List[str] = []
        known = {k for k, _a, _kind in IFACE_FIELDS} | {k for k, _a, _kind in DP_FIELDS} | {
            k for k, _a, _d in _CDS_PER_SOURCE_INT_ROWS} | {
            "AppVersion", "Description", "ManagerDataport", "DisplayFields",
            "CDS_GuardEnabledPerSource", "CDS_GuardPolarityPerSource", "CDS_DriveType",
            "CDS_GuardEnabled_REG", "CDS_GuardPolarity_REG", "CDS_TailWidth_REG"}

        def cell(key: str, row: list, i: int, kind: str, where: str = ""):
            raw = row[i] if i < len(row) else ""
            value = _parse(raw, kind)
            if not _reads_as_written(raw, kind):
                at = f"{where} " if where else ""
                bad.setdefault(key, []).append(f"{at}'{raw.strip()}' read as {value}")
            return value

        def width(key: str, row: list, n: int, kind: str = "") -> None:
            have = len(row) - 1
            if have < n and kind != "str":
                short.append(f"{key} ({max(have, 0)} of {n})")
            past = [c.strip() for c in row[n + 1:] if c.strip()]
            if past:
                extra.append(f"{key} ({', '.join(repr(c) for c in past)})")

        def src(i: int) -> str:
            return "Manager" if i == 0 else f"Device {i - 1}"
        iface_by_key = {k: (a, kind) for k, a, kind in IFACE_FIELDS}
        dp_by_key = {k: (a, kind) for k, a, kind in DP_FIELDS}
        # CDS guard/tail: prefer the per-source rows; else migrate the legacy globals
        # onto ALL sources (Manager + all 12 devices share the CDS — see below). Guard
        # arrives as split enable + polarity rows; recombine into cds_guard (0/1/2).
        got_guard = False
        guard_en = guard_pol = None
        mgr_flags = None            # ManagerDataport, resolved after the loop (see below)
        legacy = {"en": False, "pol": False, "tw": 0}
        # The plain 13-wide rows, driven off one table (see _CDS_PER_SOURCE_INT_ROWS) so a
        # new per-source field is an entry there rather than another branch here.
        int_rows = {k: (a, d) for k, a, d in _CDS_PER_SOURCE_INT_ROWS}
        got_int_row = set()
        # CDS_DriveType also has a SCALAR spelling that broadcasts to every source. It is
        # read but never written.
        legacy_drive = None
        for line, row in enumerate(csv.reader(io.StringIO(text)), start=1):
            if not row:
                continue
            key = row[0].strip()
            if key == "CDS_GuardEnabledPerSource":
                width(key, row, CDS_NUM_SOURCES)
                guard_en = [cell(key, row, i + 1, "bool", src(i))
                            for i in range(CDS_NUM_SOURCES)]
                got_guard = True
            elif key == "CDS_GuardPolarityPerSource":
                width(key, row, CDS_NUM_SOURCES)
                guard_pol = [cell(key, row, i + 1, "bool", src(i))
                             for i in range(CDS_NUM_SOURCES)]
            elif key in int_rows:
                attr, default = int_rows[key]
                target = getattr(cfg, attr)
                width(key, row, CDS_NUM_SOURCES)
                for i in range(CDS_NUM_SOURCES):
                    raw = row[i + 1] if i + 1 < len(row) else ""
                    # A short or blank cell takes the field's default, not _parse's 0 (on
                    # CDS_DriveType 0 means Special).
                    target[i] = cell(key, row, i + 1, "int", src(i)) if raw.strip() else default
                got_int_row.add(key)
            elif key == "CDS_DriveType":
                legacy_drive = cell(key, row, 1, "int")
            elif key == "CDS_GuardEnabled_REG":
                legacy["en"] = cell(key, row, 1, "bool")
            elif key == "CDS_GuardPolarity_REG":
                legacy["pol"] = cell(key, row, 1, "bool")
            elif key == "CDS_TailWidth_REG":
                legacy["tw"] = cell(key, row, 1, "int")
            elif key in iface_by_key:
                attr, kind = iface_by_key[key]
                setattr(cfg, attr, cell(key, row, 1, kind))
            elif key == "Description":
                cfg.description = row[1] if len(row) > 1 else ""
            elif key in dp_by_key:
                attr, kind = dp_by_key[key]
                width(key, row, NUM_DATA_PORTS, kind)
                for i in range(NUM_DATA_PORTS):
                    setattr(cfg.dataports[i], attr, cell(key, row, i + 1, kind, f"DP{i}"))
            elif key == "ManagerDataport":
                # Manager ports are written as device 0 plus this flag: DeviceNumber_REG is
                # a register and cannot hold the -1 sentinel. Recorded here and applied
                # after the loop, so the result does not depend on row order.
                width(key, row, NUM_DATA_PORTS)
                mgr_flags = [cell(key, row, i + 1, "bool", f"DP{i}")
                             for i in range(NUM_DATA_PORTS)]
            elif key == "DisplayFields":
                width(key, row, NUM_DATA_PORTS, "str")
                for i in range(NUM_DATA_PORTS):
                    raw = row[i + 1] if i + 1 < len(row) else ""
                    cfg.dataports[i].display_fields = _disp_bits(raw)
            elif key and key not in known:
                unknown.append(f"{key} (line {line})")
        # The Manager flag wins whichever row came first: a flagged port gets device -1
        # regardless of DeviceNumber_REG. swviz's CSVHandler gives the same result by
        # skipping the number for a slot already marked Manager.
        if mgr_flags is not None:
            for i, is_mgr in enumerate(mgr_flags):
                if is_mgr:
                    cfg.dataports[i].device_number = -1

        # Recombine the split guard rows into cds_guard (0=off, 1=G0, 2=G1). A missing
        # polarity row defaults to G0 for the enabled entries.
        if guard_en is not None:
            pol = guard_pol if guard_pol is not None else [False] * CDS_NUM_SOURCES
            cfg.cds_guard = [(CDS_GUARD_G1 if pol[i] else CDS_GUARD_G0) if guard_en[i] else CDS_GUARD_OFF
                             for i in range(CDS_NUM_SOURCES)]
        # Legacy migration: a pre-per-source CSV only has the global CDS keys. Every
        # device can use the CDS regardless of its data ports, so the global guard/
        # tail is shared by ALL sources (Manager + all 12 devices) — apply it to every
        # entry. A uniform per-source config collapses back to one universal symbol in
        # the engine (see BusModelBuilder).
        if not got_guard and legacy["en"]:
            cfg.cds_guard = [CDS_GUARD_G1 if legacy["pol"] else CDS_GUARD_G0] * CDS_NUM_SOURCES
        if "CDS_TailWidthPerSource" not in got_int_row and legacy["tw"]:
            cfg.cds_tail = [int(legacy["tw"])] * CDS_NUM_SOURCES
        if "CDS_DriveTypePerSource" not in got_int_row and legacy_drive is not None:
            cfg.cds_drive_type = [int(legacy_drive)] * CDS_NUM_SOURCES
        _report_csv_reading(source, bad, short, extra, unknown)
        return cfg

    # ---- workspace (dict) serialisation ----
    _IFACE_ATTRS = ("num_columns", "row_rate_khz", "skipping_denominator",
                    "phy3_enabled", "s0_width", "s1_tail_width", "enforce_s1_handover",
                    "cds_bit_width", "cds_guard", "cds_tail", "cds_drive_type",
                    "cds_end_drive_early",
                    "enforce_cds_handover", "rows_to_draw", "description")

    # Attributes carrying a PER-SOURCE list rather than a scalar. `from_dict` copies these
    # (so a loaded workspace never shares the caller's list) and tolerates a scalar, which
    # a workspace written before the attribute became per-source would hold.
    _IFACE_LIST_ATTRS = ("cds_guard", "cds_tail", "cds_drive_type",
                         "cds_end_drive_early")

    def to_dict(self) -> dict:
        d = {a: getattr(self, a) for a in self._IFACE_ATTRS}
        d["dataports"] = [{f.name: getattr(dp, f.name) for f in fields(dp)}
                          for dp in self.dataports]
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "BusConfig":
        cfg = cls()
        for a in cls._IFACE_ATTRS:
            if a not in d:
                continue
            if a in cls._IFACE_LIST_ATTRS:
                v = d[a]
                setattr(cfg, a, [int(v)] * CDS_NUM_SOURCES
                        if isinstance(v, (int, bool)) else list(v))
            else:
                setattr(cfg, a, d[a])
        # Migrate an old workspace that predates per-source CDS guard/tail — the
        # global value is shared by ALL sources (Manager + all 12 devices).
        if "cds_guard" not in d and d.get("cds_guard_enabled"):
            cfg.cds_guard = [CDS_GUARD_G1 if d.get("cds_guard_polarity") else CDS_GUARD_G0] * CDS_NUM_SOURCES
        if "cds_tail" not in d and d.get("cds_tail_width"):
            cfg.cds_tail = [int(d["cds_tail_width"])] * CDS_NUM_SOURCES
        dps = d.get("dataports", [])
        for i, dd in enumerate(dps[:NUM_DATA_PORTS]):
            cfg.dataports[i] = DataPortConfig(**{
                f.name: dd[f.name] for f in fields(DataPortConfig) if f.name in dd})
        return cfg

    @classmethod
    def from_decoder_config(cls, d: dict) -> "BusConfig":
        """Build a BusConfig from the decoder's snooped config (swi3score
        `config_dataports()`), so an analyzed capture's bus grid can be exported as
        a visualizer settings CSV. The decoder's dataport vector is sparse and may
        exceed 12; pack the ENABLED ports into the 12 visualizer columns, preserving
        each port's device number and logical DP number.

        The CDS fields come across as per-source lists, so a capture's snooped CDS state
        reaches the exported CSV. Their MANAGER slot (index 0) is always the default:
        only peripherals have an addressable CDS register block, so nothing on the wire
        says how the Manager drives the CDS. A missing key keeps the constructed default.
        """
        cfg = cls()
        for a in ("num_columns", "skipping_denominator", "phy3_enabled",
                  "row_rate_khz", "description", "cds_bit_width"):
            if a in d:
                setattr(cfg, a, d[a])
        for a in cls._IFACE_LIST_ATTRS:
            if a in d and d[a]:
                v = list(d[a])
                # Tolerate a short list rather than truncating the 13 slots to it.
                setattr(cfg, a, [v[i] if i < len(v) else getattr(cfg, a)[i]
                                 for i in range(CDS_NUM_SOURCES)])
        names = {f.name for f in fields(DataPortConfig)}
        enabled = [dd for dd in d.get("dataports", []) if dd.get("enabled")]
        for i, dd in enumerate(enabled[:NUM_DATA_PORTS]):
            cfg.dataports[i] = DataPortConfig(**{k: v for k, v in dd.items() if k in names})
        return cfg, len(enabled)


def demo_config() -> BusConfig:
    """A small, valid starting point: one stereo PCM source on DP0 at 16 columns
    (4-bit samples, two channels) so a fresh authoring view shows a clean,
    clash-free placement. Data starts at column 2, leaving Column 0 for the CDS
    and Column 1 for the CDS handover."""
    cfg = BusConfig(num_columns=15, row_rate_khz=3072.0)
    dp = cfg.dataports[0]
    dp.enabled = True
    dp.enable_ch = 0b11          # two channels (stereo)
    dp.sample_size = 3           # 4-bit samples
    dp.interval = 7              # 8-row interval
    dp.horizontal_start = 2      # start at column 2 (CDS = col 0, CDS handover = col 1)
    dp.horizontal_count = 7      # owns 8 columns (excess-1) → 2 ch × 4 bits
    dp.scrambler_en = True
    return cfg
