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
from typing import List

NUM_DATA_PORTS = 12

# Per-source CDS guard/tail: the CDS (Column 0) is time-multiplexed, so the Manager
# and each peripheral device can drive their own guard polarity / tail width without a
# bus clash. Stored as a list of CDS_NUM_SOURCES values, index 0 = Manager, index i =
# Device (i-1). Guard values: 0 Off, 1 G0, 2 G1 (and 3 = "Gx" mixed, used only by the
# peripheral-aggregate helper, never stored per source).
CDS_NUM_SOURCES = 13                          # Manager + Device 0..11
CDS_GUARD_OFF, CDS_GUARD_G0, CDS_GUARD_G1, CDS_GUARD_GX = 0, 1, 2, 3


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
    # CDS guard/tail are per-source now (see to_csv/from_csv_text); the legacy
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
        (dp_number < 0), so older configs without an explicit number behave as before."""
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
    # 0 Off, 1 G0, 2 G1. Tail: width 0-3. Replaces the old single global guard/tail;
    # the cds_guard_enabled/polarity/tail_width names live on as derived properties
    # (below) for the legacy consumers + CSV back-compat.
    cds_guard: List[int] = field(default_factory=lambda: [0] * CDS_NUM_SOURCES)
    cds_tail: List[int] = field(default_factory=lambda: [0] * CDS_NUM_SOURCES)
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
        only arise from explicit edits.)"""
        by_key: dict = {}
        for i, dp in enumerate(self.dataports):
            by_key.setdefault((dp.device_number, dp.number(i)), []).append(i)
        dups: List[int] = []
        for slots in by_key.values():
            if len(slots) > 1:
                dups.extend(slots)
        return sorted(dups)

    def _cds_layout(self):
        """Non-PHY3 CDS-block columns, left to right: CDS bits | guard | tails |
        handover. Returns (cds_cols, guard_col|None, tail_cols, handover_col|None),
        all clamped to the grid width. PHY3's S1-first layout isn't modelled yet."""
        cols = self.column_count()
        if self.phy3_enabled:
            return [], None, [], None
        cds_cols = list(range(0, int(self.cds_bit_width) + 1))
        x = len(cds_cols)
        guard = None
        if self.cds_guard_enabled:
            guard, x = x, x + 1
        tail = list(range(x, x + int(self.cds_tail_width)))
        x += int(self.cds_tail_width)
        handover = x if self.enforce_cds_handover else None
        tail = [c for c in tail if c < cols]
        if guard is not None and guard >= cols:
            guard = None
        if handover is not None and not (0 < handover < cols):
            handover = None
        return cds_cols, guard, tail, handover

    def system_slots(self):
        """(col, kind) system slots drawn at every row (kind: cds/guard/tail/
        handover). Column 0 is already drawn by the core, so only the CDS bits
        beyond it, the guard, the tails (rail symbol) and the handover are added."""
        cds_cols, guard, tail, handover = self._cds_layout()
        out = [(c, "cds") for c in cds_cols[1:]]
        if guard is not None:
            out.append((guard, "guard"))
        out += [(c, "tail") for c in tail]
        if handover is not None:
            out.append((handover, "handover"))
        return out

    def system_slot_cols(self):
        """Every reserved CDS-block column (incl. Column 0) — a data port placing
        into any of these collides with the Control Data Stream."""
        cds_cols, guard, tail, handover = self._cds_layout()
        s = set(cds_cols)
        if guard is not None:
            s.add(guard)
        s.update(tail)
        if handover is not None:
            s.add(handover)
        return s

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
        # 1=G0, 2=G1). The old scalar CDS_Guard*/Tail summary rows are no longer written
        # — they're derived from these on load.
        w.writerow(["CDS_GuardEnabledPerSource"] + ["1" if g != 0 else "0" for g in self.cds_guard])
        w.writerow(["CDS_GuardPolarityPerSource"] + ["1" if g == CDS_GUARD_G1 else "0" for g in self.cds_guard])
        w.writerow(["CDS_TailWidthPerSource"] + [str(int(t)) for t in self.cds_tail])
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
                # (the Visualizer's convention); written below.
                w.writerow([key] + [str(max(0, int(dp.device_number)))
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
    def from_csv(cls, path: str) -> "BusConfig":
        with open(path, newline="", encoding="utf-8") as f:
            return cls.from_csv_text(f.read())

    @classmethod
    def from_csv_text(cls, text: str) -> "BusConfig":
        cfg = cls()
        iface_by_key = {k: (a, kind) for k, a, kind in IFACE_FIELDS}
        dp_by_key = {k: (a, kind) for k, a, kind in DP_FIELDS}
        # CDS guard/tail: prefer the per-source rows; else migrate the legacy globals
        # onto ALL sources (Manager + all 12 devices share the CDS — see below). Guard
        # arrives as split enable + polarity rows; recombine into cds_guard (0/1/2).
        got_guard = got_tail = False
        guard_en = guard_pol = None
        legacy = {"en": False, "pol": False, "tw": 0}
        for row in csv.reader(io.StringIO(text)):
            if not row:
                continue
            key = row[0].strip()
            if key == "CDS_GuardEnabledPerSource":
                guard_en = [_parse(row[i + 1] if i + 1 < len(row) else "", "bool")
                            for i in range(CDS_NUM_SOURCES)]
                got_guard = True
            elif key == "CDS_GuardPolarityPerSource":
                guard_pol = [_parse(row[i + 1] if i + 1 < len(row) else "", "bool")
                             for i in range(CDS_NUM_SOURCES)]
            elif key == "CDS_TailWidthPerSource":
                for i in range(CDS_NUM_SOURCES):
                    cfg.cds_tail[i] = _parse(row[i + 1] if i + 1 < len(row) else "", "int")
                got_tail = True
            elif key == "CDS_GuardEnabled_REG":
                legacy["en"] = _parse(row[1] if len(row) > 1 else "", "bool")
            elif key == "CDS_GuardPolarity_REG":
                legacy["pol"] = _parse(row[1] if len(row) > 1 else "", "bool")
            elif key == "CDS_TailWidth_REG":
                legacy["tw"] = _parse(row[1] if len(row) > 1 else "", "int")
            elif key in iface_by_key:
                attr, kind = iface_by_key[key]
                setattr(cfg, attr, _parse(row[1] if len(row) > 1 else "", kind))
            elif key == "Description":
                cfg.description = row[1] if len(row) > 1 else ""
            elif key in dp_by_key:
                attr, kind = dp_by_key[key]
                for i in range(NUM_DATA_PORTS):
                    raw = row[i + 1] if i + 1 < len(row) else ""
                    setattr(cfg.dataports[i], attr, _parse(raw, kind))
            elif key == "ManagerDataport":
                # Manager ports are written as device 0 + this flag (the Visualizer's
                # convention); restore device -1 so the round trip preserves them.
                # Written after DeviceNumber_REG, so this correctly overrides the 0.
                for i in range(NUM_DATA_PORTS):
                    raw = row[i + 1] if i + 1 < len(row) else ""
                    if _parse(raw, "bool"):
                        cfg.dataports[i].device_number = -1
            elif key == "DisplayFields":
                for i in range(NUM_DATA_PORTS):
                    raw = row[i + 1] if i + 1 < len(row) else ""
                    cfg.dataports[i].display_fields = _disp_bits(raw)
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
        # the engine (see BusModelBuilder), so this stays byte-identical to the legacy
        # single-guard output while letting the CDS dialog show every source sharing it.
        if not got_guard and legacy["en"]:
            cfg.cds_guard = [CDS_GUARD_G1 if legacy["pol"] else CDS_GUARD_G0] * CDS_NUM_SOURCES
        if not got_tail and legacy["tw"]:
            cfg.cds_tail = [int(legacy["tw"])] * CDS_NUM_SOURCES
        return cfg

    # ---- workspace (dict) serialisation ----
    _IFACE_ATTRS = ("num_columns", "row_rate_khz", "skipping_denominator",
                    "phy3_enabled", "s0_width", "s1_tail_width", "enforce_s1_handover",
                    "cds_bit_width", "cds_guard", "cds_tail",
                    "enforce_cds_handover", "rows_to_draw", "description")

    def to_dict(self) -> dict:
        d = {a: getattr(self, a) for a in self._IFACE_ATTRS}
        d["dataports"] = [{f.name: getattr(dp, f.name) for f in fields(dp)}
                          for dp in self.dataports]
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "BusConfig":
        cfg = cls()
        for a in cls._IFACE_ATTRS:
            if a in d:
                setattr(cfg, a, list(d[a]) if a in ("cds_guard", "cds_tail") else d[a])
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
        each port's device number and logical DP number."""
        cfg = cls()
        for a in ("num_columns", "skipping_denominator", "phy3_enabled",
                  "row_rate_khz", "description"):
            if a in d:
                setattr(cfg, a, d[a])
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
