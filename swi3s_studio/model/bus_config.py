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
import math
import re
from dataclasses import dataclass, field, fields
from typing import List, Optional, Tuple

from ..fsutil import write_whole
from ..ingest.timebase import clipped

NUM_DATA_PORTS = 12

# Per-source CDS guard/tail: the CDS (Column 0) is time-multiplexed, so the Manager
# and each peripheral device can drive their own guard polarity / tail width without a
# bus clash. Stored as a list of CDS_NUM_SOURCES values, index 0 = Manager, index i =
# Device (i-1). Guard values: 0 Off, 1 G0, 2 G1.
CDS_NUM_SOURCES = 13                          # Manager + Device 0..11
CDS_GUARD_OFF, CDS_GUARD_G0, CDS_GUARD_G1 = 0, 1, 2

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
CDS_BIT_WIDTH_MAX = 7                  # CDS_BitWidth_REG is a 3-bit field

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


# Every CDS row a config CSV carries -> the legacy scalar row that also supplies it (None
# when there is none). Written per source; a file may carry either spelling.
_CDS_ROW_LEGACY = {
    "CDS_GuardEnabledPerSource": "CDS_GuardEnabled_REG",
    "CDS_GuardPolarityPerSource": "CDS_GuardPolarity_REG",
    "CDS_TailWidthPerSource": "CDS_TailWidth_REG",
    "CDS_DriveTypePerSource": "CDS_DriveType",
    "CDS_EndDriveEarlyPerSource": None,
}

# (attribute, CSV row, register width in bits) of the per-port fields held to their register
# width, checked on every enabled port that carries a channel. The same table as the native
# loader's, so both engines report the same fields.
_DP_REGISTER_WIDTHS = (
    ("sample_grouping", "SampleGrouping_REG", 3),
    ("channel_grouping", "ChannelGrouping_REG", 4),
    ("spacing", "Spacing_REG", 4),
    ("interval", "Interval_REG", 12),
    ("skipping_numerator", "SkippingNumerator_REG", 12),
    ("offset", "Offset_REG", 12),
    ("horizontal_start", "HorizontalStart_REG", 5),
    ("horizontal_count", "HorizontalCount_REG", 5),
    ("tail_width", "TailWidth_REG", 2),
    ("bit_width", "BitWidth_REG", 2),
    ("flow_mode", "FlowMode_REG", 2),
    ("port_mode", "PortMode_REG", 2),
    ("fcp_horizontal_start", "FCP_HorizontalStart_REG", 5),
    ("fcp_bit_width", "FCP_BitWidth_REG", 2),
    ("fcp_tail_width", "FCP_TailWidth_REG", 2),
    ("fcp_offset", "FCP_Offset_REG", 12),
)
SAMPLE_SIZE_BITS = 5
ENABLE_CH_BITS = 16
MAX_DP_NUMBER = 31
# The CDS per-source rows' register widths, by row.
_CDS_REGISTER_WIDTHS = (("cds_tail", "CDS_TailWidthPerSource", 2),
                        ("cds_drive_type", "CDS_DriveTypePerSource", 1),
                        ("cds_end_drive_early", "CDS_EndDriveEarlyPerSource", 1))

# RowRate's range in kHz; a rate outside it reads as 0 (the decode uses the rate it measures).
ROW_RATE_MIN_KHZ, ROW_RATE_MAX_KHZ = 1.0, 48000.0

# CDS_TailWidth defines 0, 1 and 2 UI; 3 is Reserved. A 3 read from a file is kept: the grid
# draws a 3-UI tail and the expected registers carry the 3.
CDS_TAIL_RESERVED = 3


def reserved_cds_tail_note(entries: List[str]) -> str:
    """The import warning for CDS_TailWidth 3 on `entries` ("Manager = 3", "Device 0 = 3"...).
    BusConfig and the native loader (native/swi3score/core/CDpConfig.cpp, character for
    character) word it so, so one file read by both reports it once. The Visualizer CSV
    handler words its own warning, with the line number and without the register clause."""
    return (f"CDS_TailWidthPerSource: CDS_TailWidth {CDS_TAIL_RESERVED} is Reserved "
            f"(0..2 UI are defined) for {_listed(entries)}; the value is kept as written, "
            f"the grid draws a {CDS_TAIL_RESERVED}-UI tail and the expected registers carry "
            f"{CDS_TAIL_RESERVED}.")


def _warn_negative_scalar(source: str, key: str, value: int) -> None:
    """Report a legacy scalar CDS row (`key`) holding the negative `value`: it is not applied
    to the sources, which keep their defaults."""
    from .. import import_notes
    import_notes.warn(source, f"{key} {value} is negative and is ignored; the CDS sources "
                              f"keep their defaults.")


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
    ("PortMode_REG", "port_mode", "int"),                  # 0 Normal 2 Test0 3 Test1
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
    ("DataPortNumber", "dp_number", "int"),                # logical DP number 0-31;
                                                           # written resolved, read into dp_number
]

# Interface fields: (csv_key, attribute, kind). Mirrors the Visualizer's
# INTERFACE_FIELD_NAMES so a saved config round-trips its full "Other Parameters"
# set. The C++ placement core reads NumColumns, SkippingDenominator, PHY3Enabled,
# S1TailWidth, EnforceS1Handover, RowRate, CDS_BitWidth and the per-source CDS rows
# (SwI3sConfig::LoadCsv); S0Width, EnforceCDSHandover and RowsToDraw are Visualizer-level
# fields that round-trip through this class and leave Studio's grid unchanged.
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


# Names an import warning lists before it says "and N more".
_SHOWN = 3
# Names a saved-config warning lists before it says "and N more" (a saved config can hold
# many per-port and per-source values).
_SAVED_SHOWN = 20


def _echo(value) -> str:
    """`value` as text for an import warning, cut as `ingest.timebase.clipped` cuts it (the
    native loader and the Visualizer reader cut a quoted cell the same way)."""
    return clipped(str(value))


def _csv_rows(text: str, source: str):
    """The rows of config-CSV `text`, a line ending being LF, CR LF or a lone CR. Text the
    csv module rejects raises ValueError naming `source` and the line."""
    reader = csv.reader(io.StringIO(text, newline=""))
    try:
        yield from reader
    except csv.Error as exc:
        raise ValueError(f"{source or 'config CSV'}: line {reader.line_num}: {exc}") from None


def _listed(items, shown: int = _SHOWN) -> str:
    """The first `shown` of `items` comma-joined, then 'and N more' for the rest."""
    from .. import import_notes
    return import_notes.listed(items, shown)


def _report_csv_reading(source: str, bad: dict, short: List[str], extra: List[str],
                        unknown: List[str], missing: List[str]) -> None:
    """from_csv_text's import warnings for `source`, one per kind."""
    from .. import import_notes
    for key, cells in bad.items():
        n = len(cells)
        import_notes.warn(source, f"{key}: {n} {'cells are' if n != 1 else 'cell is'} not a "
                                  f"valid value: {_listed(cells)}.")
    if short:
        import_notes.warn(source, "rows with fewer values than data ports or CDS sources, "
                                  "the missing cells reading as their default: "
                                  f"{_listed(short)}.")
    if extra:
        import_notes.warn(source, "values past the last data port or CDS source column "
                                  f"are ignored: {_listed(extra)}.")
    if unknown:
        n = len(unknown)
        import_notes.warn(source, f"{n} {'fields are' if n != 1 else 'field is'} not "
                                  f"recognised and {'were' if n != 1 else 'was'} ignored: "
                                  f"{_listed(unknown)}.")
    if missing:
        n = len(missing)
        import_notes.warn(source, f"{n} {'rows are' if n != 1 else 'row is'} missing and "
                                  f"{'take their defaults' if n != 1 else 'takes its default'}"
                                  f": {_listed(missing)}.")


def _missing_rows(seen) -> List[str]:
    """The rows a config CSV writer always writes that `seen` (the row names a file carries)
    lacks, in file order. A CDS row with a legacy scalar spelling counts as present when
    either spelling is. Description is optional."""
    wanted = [k for k, _a, _kind in IFACE_FIELDS]
    wanted += list(_CDS_ROW_LEGACY)
    wanted += [k for k, _a, _kind in DP_FIELDS] + ["ManagerDataport", "DisplayFields"]
    return [k for k in wanted
            if k not in seen and _CDS_ROW_LEGACY.get(k) not in seen]


_ASCII_WHOLE = (
    (re.compile(r"0[bB][01]+"), 2, 2),
    (re.compile(r"0[xX][0-9a-fA-F]+"), 16, 2),
    (re.compile(r"[+-]?[0-9]+"), 10, 0),
)
# The longest number a float cell opens with: ASCII digits, one point, an exponent only
# when digits follow it (the C++ core's reading).
_FLOAT_PREFIX = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?")


def _whole_number(s: str) -> Optional[int]:
    """The whole number `s` is exactly (decimal, 0x or 0b, ASCII digits only), else None."""
    for pattern, base, skip in _ASCII_WHOLE:
        if pattern.fullmatch(s):
            try:
                return int(s[skip:], base)
            except ValueError:                  # a decimal run past Python's digit limit
                return None
    return None


def _float_prefix(s: str) -> Optional[float]:
    """The finite number a float cell `s` opens with, else None."""
    m = _FLOAT_PREFIX.match(s)
    if m is None:
        return None
    v = float(m.group())
    return v if math.isfinite(v) else None


def _reads_as_written(raw: str, kind: str) -> bool:
    """Whether _parse reads `raw` as written: blank, a recognised boolean, a whole number
    (decimal, 0x, 0b) or a number, by `kind`. Digits are ASCII; an underscore or another
    script's digit is not part of a number."""
    s = (raw or "").strip()
    if not s or kind == "str":
        return True
    if kind == "bool":
        return s.lower() in ("true", "1", "yes", "false", "0", "no")
    if kind == "float":
        m = _FLOAT_PREFIX.fullmatch(s)
        return m is not None and math.isfinite(float(s))
    return _whole_number(s) is not None


def _parse(raw: str, kind: str):
    s = (raw or "").strip()
    if kind == "bool":
        return s.lower() in ("true", "1", "yes")
    if kind == "str":
        return raw if raw is not None else ""
    if kind == "float":
        v = _float_prefix(s)
        return 0.0 if v is None else v
    if not s:
        return 0
    v = _whole_number(s)
    return _leading_int(s) if v is None else v


_LEADING_INT = (
    (re.compile(r"0[bB]([01]+)"), 2),
    (re.compile(r"0[xX]([0-9a-fA-F]+)"), 16),
    (re.compile(r"([+-]?[0-9]+)"), 10),
)


def _leading_int(s: str) -> int:
    """The whole number `s` starts with, the way the C++ core reads an int cell with
    trailing characters ('15.7' reads as 15, '0x1G' as 1); 0 when it starts with none."""
    for pattern, base in _LEADING_INT:
        m = pattern.match(s)
        if m:
            try:
                return int(m.group(1), base)
            except ValueError:                  # a decimal run past Python's digit limit
                return 0
    return 0


def _typed(value, default):
    """`value` as the type of `default` (bool, int, float or str), or None when it is not a
    value of that type: a bool field takes a bool or 0/1, an int field a whole number, a
    float field a finite number, a str field a str."""
    if isinstance(default, bool):
        if isinstance(value, bool) or (isinstance(value, int) and value in (0, 1)):
            return bool(value)
        return None
    if isinstance(default, int):
        if isinstance(value, (bool, int)):
            return int(value)
        if isinstance(value, float) and math.isfinite(value) and value.is_integer():
            return int(value)
        return None
    if isinstance(default, float):
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value) if math.isfinite(value) else None
        return None
    return value if isinstance(value, str) else None


# The Manager's device_number. The device register cannot hold it, which is why a config
# CSV encodes the Manager as device 0 plus the ManagerDataport flag.
MANAGER_DEVICE = -1


def csv_device_identity(device_number: int) -> Tuple[int, bool]:
    """How a config CSV will ENCODE this device number: (DeviceNumber_REG, ManagerDataport).

    The register cannot hold the −1 Manager sentinel, so the CSV carries device 0 plus a
    boolean flag. A negative number other than −1 collapses onto plain device 0; a number
    above 11 is written as it is.

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


def _low_bits(bits: int) -> str:
    """'low bit' for a 1-bit field, else 'low N bits', as the native loader words it."""
    return "low bit" if bits == 1 else f"low {bits} bits"


@dataclass
class BusConfig:
    num_columns: int = 15               # register value; column count = num_columns + 1
    row_rate_khz: float = 3072.0
    skipping_denominator: int = 16      # a new document's seed; a CSV without the row reads 1
    phy3_enabled: bool = False
    s0_width: int = 1
    s1_tail_width: int = 0
    enforce_s1_handover: bool = True
    cds_bit_width: int = 0
    # Per-source CDS guard/tail (index 0 = Manager, 1..12 = Device 0..11). Guard:
    # 0 Off, 1 G0, 2 G1. Tail: width 0-2 UI (CDS_TailWidth 3 is Reserved). The
    # cds_guard_enabled/cds_tail_enabled/cds_guard_polarity derived properties (below) serve
    # single-value consumers and the legacy CSV keys.
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

    # ---- derived CDS guard/tail views (legacy names) ----
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
    def cds_guard_polarity(self) -> bool:
        """Legacy single polarity = the Manager's (True = G1). For CSV back-compat."""
        return self.cds_guard[0] == CDS_GUARD_G1

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
        raw attribute: the writer collapses a negative number other than −1 onto device 0,
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
        w.writerow(["ManagerDataport"] + ["True" if csv_device_identity(dp.device_number)[1]
                                          else "False"
                                          for dp in self.dataports])
        w.writerow(["DisplayFields"] + [_disp_letters(dp.display_fields)
                                        for dp in self.dataports])
        return buf.getvalue()

    def to_csv_file(self, path: str) -> str:
        """Write `to_csv()` to `path` (`fsutil.write_whole`: a failed write raises OSError and
        leaves an existing `path` as it was); returns `path`."""
        write_whole(path, lambda f: f.write(self.to_csv()), mode="w", encoding="utf-8",
                    newline="")
        return path

    @classmethod
    def from_csv(cls, path: str, source: str = "") -> "BusConfig":
        """Read the CSV at `path`; import warnings name `source` (default `path`)."""
        from .. import import_notes
        with open(path, newline="", encoding="utf-8-sig") as f:       # a BOM is not a field
            return cls.from_csv_text(f.read(), source=import_notes.source_name(source or path))

    @classmethod
    def from_csv_text(cls, text: str, source: str = "") -> "BusConfig":
        """Parse config-CSV `text`. What is not read as written is reported through
        import_notes for `source`, one warning per kind: cells that are not a valid value
        (read as 0, 0.0 or false; a whole number with trailing characters reads as its
        leading digits), a RowRate of 0 or outside 1..48000 kHz (read as 0), rows with fewer
        values than data ports or CDS sources (a blank cell counts as missing), interface
        rows with no value (the missing cells take the field's default), rows the file lacks
        altogether (they take their defaults), values past the last column, DisplayFields
        letters other than s, c and b, fields not recognised, text with no config field at
        all (empty, blank, or only AppVersion and Description: every setting takes its
        default), a CDS_BitWidth_REG,
        NumColumns_REG, S0Width, S1TailWidth_REG, SkippingDenominator_REG or RowsToDraw
        outside its range (clamped into it), and a per-port or per-source field that does
        not fit its register width (kept as written), a CDS_TailWidthPerSource of 3, which is
        Reserved (kept as written), and a negative legacy scalar CDS_TailWidth_REG or
        CDS_DriveType (ignored: the CDS sources keep their defaults). A warning lists its
        first few names and counts the rest. A line ending is LF, CR LF or a lone CR; text
        the csv module rejects raises ValueError naming `source`.

        A SkippingDenominator_REG row that is absent or blank reads as 1, the register's reset
        and what the C++ core and the Visualizer's Interface take; a RowRate row that is absent
        or blank reads as 0, the decode measuring the row rate, as the C++ core reads it. The
        dataclass defaults (the seed of a new authoring document) apply only to a config not
        read from a file.

        Digits are ASCII and a number is read as the C++ core reads it: a whole-number cell
        with trailing characters reads as its leading digits, a RowRate cell as its leading
        number, and an underscore or another script's digit ends the number. A
        SkippingDenominator_REG or CDS_BitWidth_REG past its range is held in the
        Visualizer's range here, with a note; the C++ decode keeps the value as written and
        says so in its own note."""
        text = text.lstrip("\ufeff")
        cfg = cls(skipping_denominator=1, row_rate_khz=0.0)
        bad: dict = {}                  # field -> ["DP2 'abc' read as 0", ...], in row order
        short: List[str] = []
        extra: List[str] = []
        unknown: List[str] = []
        seen: set = set()               # the row names the file carries
        known = {k for k, _a, _kind in IFACE_FIELDS} | {k for k, _a, _kind in DP_FIELDS} | {
            k for k, _a, _d in _CDS_PER_SOURCE_INT_ROWS} | {
            "AppVersion", "Description", "ManagerDataport", "DisplayFields",
            "CDS_GuardEnabledPerSource", "CDS_GuardPolarityPerSource", "CDS_DriveType",
            "CDS_GuardEnabled_REG", "CDS_GuardPolarity_REG", "CDS_TailWidth_REG"}

        def cell(key: str, row: list, i: int, kind: str, where: str = "", default=None):
            raw = row[i] if i < len(row) else ""
            if default is not None and kind != "str" and not raw.strip():
                return default            # a missing or blank cell takes the field's default
            value = _parse(raw, kind)
            if not _reads_as_written(raw, kind):
                at = f"{where} " if where else ""
                shown = str(value).lower() if kind == "bool" else value
                bad.setdefault(key, []).append(f"{at}'{_echo(raw.strip())}' read as {shown}")
            return value

        def width(key: str, row: list, n: int, kind: str = "", blank_defaults=False) -> None:
            have = len(row) - 1
            if blank_defaults:            # a blank cell takes the default, as a missing one does
                have = sum(1 for c in row[1:n + 1] if c.strip())
            if have < n and kind != "str":
                short.append(f"{key} ({max(have, 0)} of {n})")
            past = [c.strip() for c in row[n + 1:] if c.strip()]
            if past:
                extra.append(f"{_echo(key)} ({_listed(_echo(repr(c)) for c in past)})")

        def src(i: int) -> str:
            return "Manager" if i == 0 else f"Device {i - 1}"
        iface_by_key = {k: (a, kind) for k, a, kind in IFACE_FIELDS}
        dp_by_key = {k: (a, kind) for k, a, kind in DP_FIELDS}
        # CDS guard/tail: the per-source rows win; else the scalar globals apply to ALL
        # sources (Manager + all 12 devices share the CDS — see below). Guard
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
        row_rate_written = False        # a RowRate row with a value (a blank or absent one is
                                        # reported as missing or short)
        for line, row in enumerate(_csv_rows(text, source), start=1):
            if not row:
                continue
            key = row[0].strip()
            seen.add(key)
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
                width(key, row, CDS_NUM_SOURCES, blank_defaults=True)
                for i in range(CDS_NUM_SOURCES):
                    raw = row[i + 1] if i + 1 < len(row) else ""
                    # A short or blank cell takes the field's default, not _parse's 0 (on
                    # CDS_DriveType 0 means Special).
                    target[i] = cell(key, row, i + 1, "int", src(i)) if raw.strip() else default
                got_int_row.add(key)
            elif key == "CDS_DriveType":
                # A blank or bare scalar leaves the field's default, as an interface row does.
                if len(row) > 1 and row[1].strip():
                    legacy_drive = cell(key, row, 1, "int")
                else:
                    short.append(f"{key} (0 of 1)")
            elif key == "CDS_GuardEnabled_REG":
                legacy["en"] = cell(key, row, 1, "bool")
            elif key == "CDS_GuardPolarity_REG":
                legacy["pol"] = cell(key, row, 1, "bool")
            elif key == "CDS_TailWidth_REG":
                legacy["tw"] = cell(key, row, 1, "int")
            elif key in iface_by_key:
                attr, kind = iface_by_key[key]
                width(key, row, 1)
                if len(row) >= 2 and not row[1].strip():
                    short.append(f"{key} (0 of 1)")
                setattr(cfg, attr, cell(key, row, 1, kind, default=getattr(cfg, attr)))
                if key == "RowRate" and len(row) >= 2 and row[1].strip():
                    row_rate_written = True
                if key == "RowRate" and cfg.row_rate_khz < 0:
                    bad.setdefault(key, []).append(f"'{_echo(row[1].strip())}' read as 0.0")
                    cfg.row_rate_khz = 0.0
            elif key == "Description":
                cfg.description = row[1] if len(row) > 1 else ""
            elif key in dp_by_key:
                attr, kind = dp_by_key[key]
                width(key, row, NUM_DATA_PORTS, kind, blank_defaults=True)
                for i in range(NUM_DATA_PORTS):
                    dp = cfg.dataports[i]
                    setattr(dp, attr, cell(key, row, i + 1, kind, f"DP{i}",
                                           default=getattr(dp, attr)))
            elif key == "ManagerDataport":
                # Manager ports are written as device 0 plus this flag: DeviceNumber_REG is
                # a register and cannot hold the -1 sentinel. Recorded here and applied
                # after the loop, so the result does not depend on row order.
                width(key, row, NUM_DATA_PORTS)
                mgr_flags = [cell(key, row, i + 1, "bool", f"DP{i}")
                             for i in range(NUM_DATA_PORTS)]
            elif key == "DisplayFields":
                width(key, row, NUM_DATA_PORTS)
                for i in range(NUM_DATA_PORTS):
                    if i + 1 >= len(row):         # a missing cell keeps the default
                        break
                    raw = row[i + 1]
                    bits = _disp_bits(raw)
                    if set(raw.strip().lower()) - set("scb"):
                        kept = f"'{_disp_letters(bits)}'" if bits else "no fields"
                        bad.setdefault(key, []).append(
                            f"DP{i} '{_echo(raw.strip())}' read as {kept}")
                    cfg.dataports[i].display_fields = bits
            elif key and key not in known:
                unknown.append(f"{_echo(key)} (line {line})")
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
        # Scalar globals: a CSV with only the global CDS keys. Every device can use the
        # CDS regardless of its data ports, so the global guard/tail is shared by ALL
        # sources (Manager + all 12 devices) — apply it to every entry. A uniform per-source
        # config collapses back to one universal symbol in the engine (see BusModelBuilder).
        if not got_guard and legacy["en"]:
            cfg.cds_guard = [CDS_GUARD_G1 if legacy["pol"] else CDS_GUARD_G0] * CDS_NUM_SOURCES
        # The native loader broadcasts a scalar tail width above 0 and a scalar drive type of
        # 0 or more; a negative one is ignored there and here, and reported.
        if "CDS_TailWidthPerSource" not in got_int_row and legacy["tw"]:
            if int(legacy["tw"]) > 0:
                cfg.cds_tail = [int(legacy["tw"])] * CDS_NUM_SOURCES
            else:
                _warn_negative_scalar(source, "CDS_TailWidth_REG", int(legacy["tw"]))
        if "CDS_DriveTypePerSource" not in got_int_row and legacy_drive is not None:
            if int(legacy_drive) >= 0:
                cfg.cds_drive_type = [int(legacy_drive)] * CDS_NUM_SOURCES
            else:
                _warn_negative_scalar(source, "CDS_DriveType", int(legacy_drive))
        cfg._clamp_cds_bit_width(source)
        cfg._clamp_interface_ranges(source)
        held = cfg._hold_row_rate(source)
        if seen & (known - {"AppVersion", "Description"}):
            _report_csv_reading(source, bad, short, extra, unknown, _missing_rows(seen))
        else:
            from .. import import_notes
            _report_csv_reading(source, bad, short, extra, unknown, [])
            import_notes.warn(source, "no config field is recognised (no _REG row and no "
                                      "legacy field); every setting takes its default.")
        cfg._report_register_widths(source)
        if (cfg.row_rate_khz == 0.0 and "RowRate" not in bad and not held
                and row_rate_written):
            from .. import import_notes
            import_notes.warn(source, "RowRate is 0; the decode uses the row rate it measures.")
        return cfg

    def _clamp_cds_bit_width(self, source: str) -> None:
        """Hold cds_bit_width in the register field's 0..CDS_BIT_WIDTH_MAX; a value outside
        it is clamped and reported through import_notes under `source`."""
        w = int(self.cds_bit_width)
        if 0 <= w <= CDS_BIT_WIDTH_MAX:
            return
        from .. import import_notes
        self.cds_bit_width = min(max(w, 0), CDS_BIT_WIDTH_MAX)
        import_notes.warn(source, f"CDS_BitWidth_REG {w} is outside 0..{CDS_BIT_WIDTH_MAX}; "
                                  f"it is read as {self.cds_bit_width}.")

    # (attribute, field name in import notes, InterfaceRanges minimum, maximum) of the
    # integer fields held in their range on every read.
    _RANGE_FIELDS = (("num_columns", "NumColumns_REG", "MIN_COLUMNS_PER_ROW",
                      "MAX_COLUMNS_PER_ROW"),
                     ("s0_width", "S0Width", "MIN_S0_WIDTH", "MAX_S0_WIDTH"),
                     ("s1_tail_width", "S1TailWidth_REG", "MIN_TAIL_WIDTH", "MAX_TAIL_WIDTH"),
                     ("skipping_denominator", "SkippingDenominator_REG",
                      "MIN_SKIPPING_DENOMINATOR", "MAX_SKIPPING_DENOMINATOR"),
                     ("rows_to_draw", "RowsToDraw", "MIN_ROWS", "MAX_ROWS"))

    def _clamp_interface_ranges(self, source: str) -> None:
        """Hold each _RANGE_FIELDS attribute in its InterfaceRanges range; a value outside
        it is clamped and reported through import_notes under `source`."""
        from .. import import_notes
        from ..swviz.config.constants import InterfaceRanges
        for attr, name, lo_name, hi_name in self._RANGE_FIELDS:
            lo, hi = getattr(InterfaceRanges, lo_name), getattr(InterfaceRanges, hi_name)
            v = int(getattr(self, attr))
            if lo <= v <= hi:
                continue
            setattr(self, attr, min(max(v, lo), hi))
            import_notes.warn(source, f"{name} {v} is outside {lo}..{hi}; it is read as "
                                      f"{getattr(self, attr)}.")

    def _hold_row_rate(self, source: str) -> bool:
        """Read a RowRate outside ROW_RATE_MIN_KHZ..ROW_RATE_MAX_KHZ as 0 (the decode uses the
        row rate it measures) and report it through import_notes under `source`. Returns
        whether it did. A rate of exactly 0 is in range."""
        v = float(self.row_rate_khz)
        if v == 0.0 or ROW_RATE_MIN_KHZ <= v <= ROW_RATE_MAX_KHZ:
            return False
        from .. import import_notes
        self.row_rate_khz = 0.0
        import_notes.warn(source, f"RowRate {v:g} kHz is outside {ROW_RATE_MIN_KHZ:g}.."
                                  f"{ROW_RATE_MAX_KHZ:g} kHz; the decode uses the row rate it "
                                  "measures.")
        return True

    def reserved_cds_tail_sources(self) -> List[str]:
        """The sources whose cds_tail is the Reserved 3, as "Manager = 3", "Device 0 = 3"..."""
        return [f"{'Manager' if i == 0 else f'Device {i - 1}'} = {CDS_TAIL_RESERVED}"
                for i, v in enumerate(self.cds_tail) if int(v) == CDS_TAIL_RESERVED]

    def _report_register_widths(self, source: str) -> None:
        """Report, through import_notes under `source`, each per-port and per-source CDS field
        whose value does not fit its register width. The value is kept as written (the
        decode reads its low bits), one warning per field listing the ports or sources,
        worded as the native loader's. A cds_tail of 3 fits its field but is Reserved; it is
        kept and reported on its own (`reserved_cds_tail_note`)."""
        from .. import import_notes

        def over(name: str, bits: int, entries: List[str], tail: str) -> None:
            import_notes.warn(source, f"{name} does not fit its {bits}-bit register field "
                                      f"(0..{(1 << bits) - 1}) for {_listed(entries)}; {tail}")

        for attr, name, bits in _CDS_REGISTER_WIDTHS:
            entries = [f"{'Manager' if i == 0 else f'Device {i - 1}'} = {int(v)}"
                       for i, v in enumerate(getattr(self, attr))
                       if not 0 <= int(v) < (1 << bits)]
            if entries:
                over(name, bits, entries,
                     f"the expected registers carry its {_low_bits(bits)}.")
        reserved = self.reserved_cds_tail_sources()
        if reserved:
            import_notes.warn(source, reserved_cds_tail_note(reserved))
        sized, wide, numbered = [], [], []
        by_field: dict = {name: [] for _a, name, _b in _DP_REGISTER_WIDTHS}
        for i, dp in enumerate(self.dataports):
            if not dp.enabled:
                continue
            at = f"DP{i}"
            if not 0 <= dp.enable_ch < (1 << ENABLE_CH_BITS):
                wide.append(f"{at} = {dp.enable_ch}")
            if dp.num_channels() == 0:
                continue
            if not 0 <= dp.sample_size < (1 << SAMPLE_SIZE_BITS):
                sized.append(f"{at} = {dp.sample_size} (read as "
                             f"{dp.sample_size & ((1 << SAMPLE_SIZE_BITS) - 1)})")
            if not -1 <= dp.dp_number <= MAX_DP_NUMBER:
                numbered.append(f"{at} = {dp.dp_number}")
            for attr, name, bits in _DP_REGISTER_WIDTHS:
                v = getattr(dp, attr)
                if not 0 <= v < (1 << bits):
                    by_field[name].append(f"{at} = {v}")
        if sized:
            over("SampleSize_REG", SAMPLE_SIZE_BITS, sized,
                 "the audio decode, the grid and the registers read its low 5 bits.")
        if wide:
            import_notes.warn(source, f"EnableCh_REG does not fit its {ENABLE_CH_BITS} channel "
                                      f"bits for {_listed(wide)}; only channels "
                                      f"0..{ENABLE_CH_BITS - 1} are read.")
        if numbered:
            import_notes.warn(source, f"DataPortNumber is outside 0..{MAX_DP_NUMBER} for "
                                      f"{_listed(numbered)}; a negative number is ignored (the "
                                      "port keeps its column's number) and a larger one "
                                      "addresses no data port register block.")
        for _attr, name, bits in _DP_REGISTER_WIDTHS:
            if by_field[name]:
                over(name, bits, by_field[name],
                     f"the expected registers carry its {_low_bits(bits)} and the decode uses "
                     "the value as written.")

    # ---- workspace (dict) serialisation ----
    _IFACE_ATTRS = ("num_columns", "row_rate_khz", "skipping_denominator",
                    "phy3_enabled", "s0_width", "s1_tail_width", "enforce_s1_handover",
                    "cds_bit_width", "cds_guard", "cds_tail", "cds_drive_type",
                    "cds_end_drive_early",
                    "enforce_cds_handover", "rows_to_draw", "description")

    # Attributes carrying a PER-SOURCE list rather than a scalar. `from_dict` copies these
    # (so a loaded workspace never shares the caller's list) and reads a scalar as the
    # value of every source.
    _IFACE_LIST_ATTRS = ("cds_guard", "cds_tail", "cds_drive_type",
                         "cds_end_drive_early")

    def to_dict(self) -> dict:
        d = {a: getattr(self, a) for a in self._IFACE_ATTRS}
        d["dataports"] = [{f.name: getattr(dp, f.name) for f in fields(dp)}
                          for dp in self.dataports]
        return d

    @classmethod
    def from_dict(cls, d: dict, source: str = "") -> "BusConfig":
        """Read a saved config. What is not a value of its field's type (or a list where one
        is due, or a cds_guard entry other than 0 Off, 1 G0 or 2 G1) takes the field's default;
        a per-source list that is not CDS_NUM_SOURCES
        long is padded with the field's default (or truncated); a cds_bit_width,
        num_columns, s0_width, s1_tail_width, skipping_denominator or rows_to_draw outside
        its range (InterfaceRanges) is clamped; a row_rate_khz outside 1..48000 reads as 0; a
        per-port or per-source field that does not fit its register width, and a cds_tail
        entry of 3 (Reserved), are kept as written. A key this version does not read (top
        level or per port) and data ports past NUM_DATA_PORTS are ignored. Each kind is
        reported through import_notes under
        `source`, listing its first few names and counting the rest, each echoed value cut to
        a few dozen characters."""
        from .. import import_notes
        cfg = cls()
        bad: List[str] = []
        for a in cls._IFACE_ATTRS:
            if a not in d:
                continue
            have = getattr(cfg, a)
            if a in cls._IFACE_LIST_ATTRS:
                v = d[a]
                if isinstance(v, (int, bool)):
                    v = [v] * CDS_NUM_SOURCES
                elif not isinstance(v, (list, tuple)):
                    bad.append(f"{a} {_echo(repr(v))}")
                    continue
                v = list(v)
                if len(v) != CDS_NUM_SOURCES:
                    import_notes.warn(source, f"{a} has {len(v)} values for "
                                              f"{CDS_NUM_SOURCES} sources; the missing "
                                              f"ones take their default, extra ones are "
                                              f"ignored.")
                out = []
                for i in range(CDS_NUM_SOURCES):
                    got = _typed(v[i], have[i]) if i < len(v) else have[i]
                    if got is not None and a == "cds_guard" and not (
                            CDS_GUARD_OFF <= got <= CDS_GUARD_G1):
                        got = None              # not Off, G0 or G1
                    if got is None:
                        bad.append(f"{a}[{i}] {_echo(repr(v[i]))}")
                        got = have[i]
                    out.append(got)
                setattr(cfg, a, out)
            else:
                got = _typed(d[a], have)
                if got is None:
                    bad.append(f"{a} {_echo(repr(d[a]))}")
                else:
                    setattr(cfg, a, got)
        # The scalar guard and tail keys apply to ALL sources (Manager + all 12 devices).
        if "cds_guard" not in d and d.get("cds_guard_enabled"):
            cfg.cds_guard = [CDS_GUARD_G1 if d.get("cds_guard_polarity") else CDS_GUARD_G0] * CDS_NUM_SOURCES
        if "cds_tail" not in d and d.get("cds_tail_width"):
            width = _typed(d["cds_tail_width"], 0)
            if width is None:
                bad.append(f"cds_tail_width {_echo(repr(d['cds_tail_width']))}")
            else:
                cfg.cds_tail = [width] * CDS_NUM_SOURCES
        cfg._clamp_cds_bit_width(source)
        cfg._clamp_interface_ranges(source)
        cfg._hold_row_rate(source)
        dps = d.get("dataports", [])
        if not isinstance(dps, (list, tuple)):
            bad.append(f"dataports {_echo(repr(dps))}")
            dps = []
        base = DataPortConfig()
        for i, dd in enumerate(dps[:NUM_DATA_PORTS]):
            if not isinstance(dd, dict):
                bad.append(f"dataports[{i}] {_echo(repr(dd))}")
                continue
            kw = {}
            for f in fields(DataPortConfig):
                if f.name not in dd:
                    continue
                got = _typed(dd[f.name], getattr(base, f.name))
                if got is None:
                    bad.append(f"dataports[{i}].{f.name} {_echo(repr(dd[f.name]))}")
                else:
                    kw[f.name] = got
            cfg.dataports[i] = DataPortConfig(**kw)
        cfg._report_register_widths(source)
        if bad:
            import_notes.warn(source, "values that are not valid for their field read as "
                                      f"its default: {_listed(bad, _SAVED_SHOWN)}.")
        known = set(cls._IFACE_ATTRS) | {"dataports", "cds_guard_enabled",
                                         "cds_guard_polarity", "cds_tail_width"}
        unknown = sorted(_echo(k) for k in d if k not in known)
        port_keys = {f.name for f in fields(DataPortConfig)}
        unknown += [f"data port {k}" for k in sorted(
            {_echo(k) for dd in dps[:NUM_DATA_PORTS] if isinstance(dd, dict)
             for k in dd if k not in port_keys})]
        if unknown:
            import_notes.warn(source, "Visualizer config: settings this version does not "
                                      f"read, ignored: {_listed(unknown, _SAVED_SHOWN)}.")
        if len(dps) > NUM_DATA_PORTS:
            import_notes.warn(source, f"Visualizer config: {len(dps)} data ports saved; the "
                                      f"{len(dps) - NUM_DATA_PORTS} past the "
                                      f"{NUM_DATA_PORTS}th are not read.")
        return cfg

    @classmethod
    def from_decoder_config(cls, d: dict) -> Tuple["BusConfig", int]:
        """Build (BusConfig, enabled port count) from the decoder's snooped config (swi3score
        `config_dataports()`), so an analyzed capture's bus grid can be exported as
        a visualizer settings CSV. The decoder's dataport vector is sparse and may
        exceed 12; pack the ENABLED ports into the 12 visualizer columns, preserving
        each port's device number and logical DP number. The count is the number of
        enabled decoder ports, which may exceed the 12 that fit.

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
