"""Visualizer config-CSV version upgrader.

The SWI3S Visualizer / Studio has written its dataport-configuration CSV in
several formats over time:

* **legacy (v1.73 / v1.74)** — human-readable field names (``Columns per Row
  (2-32)``, ``Data Port Enabled``, ``Data Port Horizontal Start`` …) with a
  ``Save file using excess one`` encoding flag. v1.74 split the interval into
  ``Data Port Interval Integer`` + ``Data Port Interval Numerator`` and renamed
  the denominator; v1.73 used a single ``Data Port Interval``.
* **v2.0** — register-style field names (``NumColumns_REG``,
  ``EnableCh_REG`` …). This is what the C++ ``SwI3sConfig::LoadCsv`` /
  ``swi3score.grid_from_csv`` parser reads.
* **v3.0.0 (current)** — v2.0 plus a per-DP ``DataPortNumber`` row (logical DP
  number, distinct from the column index).

This module detects the format and upgrades any older one to the CURRENT format
so old files open everywhere (Compare, the decode config, the authoring panel).
What an upgrade could not carry over (an unrecognised field, a v2.0 file the model
could not read, a file with no config fields at all) is reported through
``import_notes`` under the original file's name, and ``report_csv_warnings`` reports
what the native loader did not read as written. The upgrade always converges on the
swviz model — parse into an ``Interface`` / ``VizConfig`` then re-serialise with
``CSVHandler.save_csv`` — so
new fields added in future versions are written automatically; the converter
only has to learn how to READ each older layout, not how to emit the newest one.

``convert()`` (legacy dict → model) is shared with the standalone
``tools/csv_converter/convert_174.py`` CLI, which wraps it.
"""
from __future__ import annotations

import csv
import io
import logging
import math
import os
import re
import tempfile
from typing import Callable, Dict, List, Optional, Tuple

from .. import import_notes
from .timebase import clipped, quoted

logger = logging.getLogger(__name__)

NUM_DP = 12

# ---- legacy (human-readable) field vocabulary -----------------------------
# Field names (already normalised: trailing "(range)" descriptors stripped).
_INTERFACE_FIELDS = {
    'Columns per Row', 'S0 S1 Enabled', 'S0 Width', 'CDS Guard Enabled',
    'CDS Guard Polarity', 'CDS Tail Width', 'Interval Denominator', 'Skipping Denominator',
    'CDS/S0 Handover Width', 'Draw S0 Handover', 'Row Rate [kHz]', 'Rows to Draw',
}
_DP_FIELDS = {
    'Data Port Name', 'Data Port Device Number', 'Data Port Channels',
    'Data Port Channel Grouping', 'Data Port Channel Group Spacing',
    'Data Port Sample Width', 'Data Port Sample Grouping',
    'Data Port Interval', 'Data Port Interval Integer', 'Data Port Interval Numerator',
    'Data Port Offset', 'Data Port Horizontal Start', 'Data Port Horizontal Count',
    'Data Port Tail Width', 'Data Port Bit Width', 'Source', 'Draw Data Port Handover',
    'Data Port Guard Enabled', 'Data Port Enabled', 'Data Port In Manager', 'Data Port DRI',
}
# Legacy files may include an encoding flag; act on it (see convert) but don't warn.
_SILENT_SKIP = {'Save file using excess one'}
_KNOWN = _INTERFACE_FIELDS | _DP_FIELDS | _SILENT_SKIP

# Field names that only the legacy human-readable export has (never register-style).
_LEGACY_MARKERS = ('Columns per Row', 'Save file using excess one', 'Data Port Name')

# Trailing "(2-32)", "(0-8)", "[kHz] (1-48000)" are descriptive — drop when matching.
_RANGE_RE = re.compile(r'\s*\([^)]*\)\s*$')


def _normalize(name: str) -> str:
    return _RANGE_RE.sub('', name).strip()


_TRUE_TEXT = ('true', '1', 'yes')
_FALSE_TEXT = ('false', '0', 'no')


def _parse_bool(s: str, field: str = '', clamped: Optional[Callable[[str], None]] = None
                ) -> bool:
    """`s` as a flag: true/1/yes and false/0/no, in any case. Any other text is false and,
    with `clamped`, reported as a value that did not fit, naming `field`."""
    text = str(s).strip().lower()
    if text in _TRUE_TEXT:
        return True
    if text not in _FALSE_TEXT and clamped:
        clamped(f"{field or 'a flag'} is {quoted(str(s).strip())}, not true or false; "
                f"false is used")
    return False


def _rows(text: str) -> List[Tuple[str, List[str]]]:
    """(field name, values) per row. A row the csv module cannot read (a cell over its
    field limit) raises ValueError naming the line."""
    out: List[Tuple[str, List[str]]] = []
    reader = csv.reader(io.StringIO(text))
    try:
        for row in reader:
            if not row or not row[0].strip():
                continue
            out.append((_normalize(row[0]), [v.strip() for v in row[1:]]))
    except csv.Error as exc:
        raise ValueError(f"line {reader.line_num}: {exc}") from None
    return out


def legacy_dict_from_text(text: str) -> Dict[str, List[str]]:
    """Parse legacy CSV text into {normalised field name -> values}."""
    return {f: v for f, v in _rows(text)}


# The largest config CSV read, in characters: a register file is a few KB, so a file over
# this is not one.
MAX_CONFIG_CHARS = 8 << 20


def read_config_text(path) -> str:
    """The text of the config CSV at `path` (a spreadsheet's BOM is not a field). ValueError
    naming the file when it is over `MAX_CONFIG_CHARS` characters or is not UTF-8 text; a
    device or pipe is read no further than the limit."""
    name = import_notes.source_name(str(path))
    try:
        with open(path, 'r', encoding='utf-8-sig') as f:
            text = f.read(MAX_CONFIG_CHARS + 1)
    except UnicodeDecodeError as exc:
        raise ValueError(f"{name}: not a text config CSV ({exc})") from None
    if len(text) > MAX_CONFIG_CHARS:
        raise ValueError(f"{name}: too large for a config CSV (over {MAX_CONFIG_CHARS:,} "
                         f"characters); is it a capture?")
    return text


def read_legacy_csv(path) -> Dict[str, List[str]]:
    """Read a legacy CSV file into {normalised field name -> values}; ValueError naming
    the file as `read_config_text` does."""
    return legacy_dict_from_text(read_config_text(path))


def _whole(text: str, field: str, port: Optional[int] = None) -> int:
    """`text` as a whole number; ValueError naming the field (and data port) when it is
    not one."""
    try:
        return int(text)
    except ValueError:
        where = f" for data port {port}" if port is not None else ""
        raise ValueError(f"{field}{where} is {quoted(text)}, not a whole number") from None


def _get_int(old: Dict[str, List[str]], field: str, i: int, default: int) -> int:
    vals = old.get(field, [])
    return _whole(vals[i], field, i) if i < len(vals) else default


def _get_bool(old: Dict[str, List[str]], field: str, i: int, default: bool,
              clamped: Optional[Callable[[str], None]] = None) -> bool:
    vals = old.get(field, [])
    if i >= len(vals):
        return default
    return _parse_bool(vals[i], f"{field} for data port {i}", clamped)


def _get_str(old: Dict[str, List[str]], field: str, i: int, default: str) -> str:
    vals = old.get(field, [])
    return vals[i] if i < len(vals) else default


# Most unrecognised legacy field names one import warning lists.
_LISTED_FIELDS = 8

# Most channels a data port enables (EnableCh_REG is 16 bits).
_MAX_CHANNELS = 16


def _store(obj, attr: str, value, field: str, text: str) -> None:
    """Set `obj.attr` to `value`, read from the legacy row `field` as `text`; a value the
    model refuses raises ValueError naming the field and the cell."""
    try:
        setattr(obj, attr, value)
    except ValueError as exc:
        raise ValueError(f"{field} is {quoted(text)}, which the model refuses "
                         f"({exc})") from None


def convert(old: Dict[str, List[str]], warn: Callable[[str], None],
            source_stem: str = '', clamped: Optional[Callable[[str], None]] = None):
    """Convert a parsed legacy (v1.73/v1.74) config dict to (Interface, VizConfig).

    Shared by the app's silent open path and the convert_174 CLI. Unrecognised
    fields are reported via ``warn``. A value the converter changes to fit (a count below
    1, more channels than a port has, Row Rate or Rows to Draw outside its range, a flag that is not
    true/false/1/0/yes/no, which reads as false, a CDS/S0 Handover Width outside 0..3, a CDS
    Guard Polarity on a disabled guard) is reported via ``clamped`` as text
    naming the field, the port and the value used. The CDS guard, polarity and tail width
    are written to every CDS source. A cell that is not a whole number, a Row Rate that is
    not a finite number, and an interface value the model refuses (Columns per Row, S0 Width,
    CDS Tail Width, Skipping Denominator) raise ValueError naming the field and the cell.
    Per-port numbers are written as given; the native loader range-checks them."""
    from ..swviz.config.constants import InterfaceRanges, SpecialDevices
    from ..swviz.models import Interface
    from ..swviz.models.enums import DisplayField
    from ..swviz.version import app_version
    from ..swviz.viz import VizConfig

    # app_version(), not the APP_VERSION literal.
    note = f'converted from SWI3S legacy CSV to v{app_version()}'
    iface = Interface()
    viz = VizConfig()
    iface.description = f'{source_stem} — {note}' if source_stem else note

    # Legacy writes `Save file using excess one,True` when the four count-style DP
    # fields (Channels, Sample Width, Sample Grouping, Interval) are stored as raw
    # REGs; otherwise they're 1-based counts needing a -1. `Columns per Row` is
    # always a natural value regardless, so NumColumns_REG is always value - 1.
    already_excess_one = _parse_bool((old.get('Save file using excess one') or ['False'])[0],
                                     'Save file using excess one', clamped)

    def flag(field: str) -> bool:
        """The first value of the legacy row `field` as a flag."""
        return _parse_bool(old[field][0], field, clamped)

    # --- Interface --- (guard on truthiness: a present-but-empty row has [] values,
    # so `in old` would pass but old[k][0] would IndexError.)
    if old.get('Columns per Row'):
        _store(iface, 'NumColumns_REG',
               _whole(old['Columns per Row'][0], 'Columns per Row') - 1,
               'Columns per Row', old['Columns per Row'][0])
    if old.get('S0 S1 Enabled'):
        iface.phy3_enabled = flag('S0 S1 Enabled')
    if old.get('S0 Width'):
        _store(iface, 's0_width', _whole(old['S0 Width'][0], 'S0 Width'),
               'S0 Width', old['S0 Width'][0])
    if old.get('CDS Guard Enabled'):
        iface.CDS_GuardEnabled_REG = flag('CDS Guard Enabled')
    # Older files predate the Guard 0/Guard 1 polarity choice; absent -> Guard 0
    # (False), matching the pre-polarity behaviour.
    if old.get('CDS Guard Polarity'):
        iface.CDS_GuardPolarity_REG = flag('CDS Guard Polarity')
    # One guard for every source, as the current format's per-source rows carry it (0 off,
    # 1 Guard 0, 2 Guard 1). Those rows hold a polarity only with its guard enabled, so a
    # polarity set on a disabled guard is reported and written as Guard 0.
    if iface.CDS_GuardPolarity_REG and not iface.CDS_GuardEnabled_REG:
        if clamped:
            clamped("CDS Guard Polarity is true while CDS Guard Enabled is false; a polarity "
                    "is kept only with its guard, so false is used")
        iface.CDS_GuardPolarity_REG = False
    guard = (2 if iface.CDS_GuardPolarity_REG else 1) if iface.CDS_GuardEnabled_REG else 0
    iface.CDS_Guard_PerSource = [guard] * len(iface.CDS_Guard_PerSource)
    if old.get('CDS Tail Width'):
        _store(iface, 'CDS_TailWidth_REG', _whole(old['CDS Tail Width'][0], 'CDS Tail Width'),
               'CDS Tail Width', old['CDS Tail Width'][0])
        # One width for every source, as the current format's per-source row carries it.
        iface.CDS_Tail_PerSource = [iface.CDS_TailWidth_REG] * len(iface.CDS_Tail_PerSource)
    # v1.74's UI label is "Skipping Denominator"; its loader expects "Interval
    # Denominator". Accept either; prefer the newer name if both exist.
    denom = old.get('Skipping Denominator') or old.get('Interval Denominator')
    if denom:
        _store(iface, 'SkippingDenominator_REG', _whole(denom[0], 'Skipping Denominator'),
               'Skipping Denominator', denom[0])
    if old.get('CDS/S0 Handover Width'):
        # Legacy packed CDS handover and S1 tail into one width field. Split:
        # EnforceCDSHandover on when width>=1; S1TailWidth_REG is 0 for width 0-1,
        # 1 for width 2, 2 for width >=3.
        w = _whole(old['CDS/S0 Handover Width'][0], 'CDS/S0 Handover Width')
        iface.cds_handover_enabled = w > 0
        iface.tail_width = min(2, max(0, w - 1))
        if not 0 <= w <= 3 and clamped:
            clamped(f"CDS/S0 Handover Width {w} is outside 0 to 3; "
                    f"{'a handover and ' if w > 0 else ''}an S1 tail width of "
                    f"{iface.tail_width} {'are' if w > 0 else 'is'} used")
    if old.get('Draw S0 Handover'):
        iface.s1_handover_enabled = flag('Draw S0 Handover')
    if old.get('Row Rate [kHz]'):
        text = old['Row Rate [kHz]'][0]
        try:
            rate = float(text)
        except ValueError:
            raise ValueError(f"Row Rate [kHz] is {quoted(text)}, not a number") from None
        if not math.isfinite(rate):
            raise ValueError(f"Row Rate [kHz] is {quoted(text)}, not a finite number")
        low, high = InterfaceRanges.MIN_ROW_RATE, InterfaceRanges.MAX_ROW_RATE
        iface.row_rate = min(high, max(low, rate))
        if iface.row_rate != rate and clamped:
            clamped(f"Row Rate [kHz] {clipped(text)} is outside {low:g} to {high:g}; "
                    f"{iface.row_rate:g} is used")
    if old.get('Rows to Draw'):
        rows = _whole(old['Rows to Draw'][0], 'Rows to Draw')
        low, high = InterfaceRanges.MIN_ROWS, InterfaceRanges.MAX_ROWS
        viz.rows_to_draw = min(high, max(low, rows))
        if viz.rows_to_draw != rows and clamped:
            clamped(f"Rows to Draw {rows} is outside {low} to {high}; "
                    f"{viz.rows_to_draw} is used")

    # Subtract 1 unless the file was saved in excess-one mode (REGs on disk).
    def excess1(field: str, i: int, default: int) -> int:
        raw = _get_int(old, field, i, default)
        if already_excess_one:
            return raw
        if raw < 1 and clamped:
            clamped(f"{field} {raw} for data port {i} is below 1; 1 is used")
        return max(0, raw - 1)

    # v1.74 splits the interval into Integer + Numerator; v1.73 has a single
    # "Data Port Interval". Accept whichever the file carries.
    interval_field = ('Data Port Interval Integer'
                      if 'Data Port Interval Integer' in old else 'Data Port Interval')

    # --- Per-DP ---
    for i in range(NUM_DP):
        cfg = iface.data_ports[i].config

        # Channels: human form is a count N (1 -> 1 channel); excess-one form stores
        # N-1 on disk. Both end at a (1<<count)-1 bitmask.
        raw_ch = _get_int(old, 'Data Port Channels', i, 1)
        n_channels = (raw_ch + 1) if already_excess_one else raw_ch
        # The count the file means, when it stores one less.
        means = f" ({n_channels} channels)" if already_excess_one else ""
        if n_channels > _MAX_CHANNELS:
            if clamped:
                clamped(f"Data Port Channels {raw_ch}{means} for data port {i} is above "
                        f"{_MAX_CHANNELS}; {_MAX_CHANNELS} channels are enabled")
            n_channels = _MAX_CHANNELS
        elif n_channels < 1 and clamped:
            clamped(f"Data Port Channels {raw_ch}{means} for data port {i} is below 1; no "
                    f"channel is enabled")
        cfg.EnableCh_REG = (1 << n_channels) - 1 if n_channels > 0 else 0

        cfg.ChannelGrouping_REG = _get_int(old, 'Data Port Channel Grouping', i, 0)
        cfg.Spacing_REG = _get_int(old, 'Data Port Channel Group Spacing', i, 0)
        cfg.SampleSize_REG = excess1('Data Port Sample Width', i, 1)
        cfg.SampleGrouping_REG = excess1('Data Port Sample Grouping', i, 1)
        cfg.Interval_REG = excess1(interval_field, i, 1)
        cfg.SkippingNumerator_REG = _get_int(old, 'Data Port Interval Numerator', i, 0)
        cfg.Offset_REG = _get_int(old, 'Data Port Offset', i, 0)
        cfg.HorizontalStart_REG = _get_int(old, 'Data Port Horizontal Start', i, 0)
        cfg.HorizontalCount_REG = _get_int(old, 'Data Port Horizontal Count', i, 0)
        cfg.TailWidth_REG = _get_int(old, 'Data Port Tail Width', i, 0)
        cfg.BitWidth_REG = _get_int(old, 'Data Port Bit Width', i, 0)
        # Legacy Source=True means "is a source". PortDirection_REG=0 for source.
        cfg.PortDirection_REG = not _get_bool(old, 'Source', i, True, clamped)
        cfg.GuardEnable_REG = _get_bool(old, 'Data Port Guard Enabled', i, False, clamped)
        cfg.SubRowInterval_REG = _get_bool(old, 'Data Port DRI', i, False, clamped)

        # _REG fields with no legacy source — user-specified defaults.
        cfg.GuardPolarity_REG = False
        cfg.FlowMode_REG = 0
        cfg.PortMode_REG = 0
        cfg.ScramblerEn_REG = False

        fcp_cfg = iface.flow_control_ports[i].config
        fcp_cfg.FCP_HorizontalStart_REG = 0
        fcp_cfg.FCP_BitWidth_REG = 0
        fcp_cfg.FCP_TailWidth_REG = 0
        fcp_cfg.FCP_Offset_REG = 0
        fcp_cfg.FCP_GuardEnable_REG = False
        fcp_cfg.FCP_GuardPolarity_REG = False

        viz_dp = viz.data_ports[i]
        viz_dp.name = _get_str(old, 'Data Port Name', i, f'DP{i}')
        viz_dp.enabled = _get_bool(old, 'Data Port Enabled', i, False, clamped)
        viz_dp.enable_handover = _get_bool(old, 'Draw Data Port Handover', i, True, clamped)
        viz_dp.display_fields = DisplayField.SAMPLE | DisplayField.CHANNEL

        if _get_bool(old, 'Data Port In Manager', i, False, clamped):
            iface.set_dp_device(i, SpecialDevices.MANAGER)
        else:
            iface.set_dp_device(i, _get_int(old, 'Data Port Device Number', i, 0))

    for name in old:
        if name not in _KNOWN:
            warn(name)

    return iface, viz


# ---- version detection + upgrade ------------------------------------------

def detect_version(text: str) -> str:
    """Classify config-CSV `text` as 'legacy', 'register_old' (v2.0, no
    DataPortNumber), 'current' (v3.0), or 'unknown'."""
    fields = {f for f, _ in _rows(text)}
    if any(f.startswith(m) for m in _LEGACY_MARKERS for f in fields):
        return "legacy"
    if any(f == "NumColumns_REG" or f.endswith("_REG") for f in fields):
        return "current" if "DataPortNumber" in fields else "register_old"
    return "unknown"


def _model_to_text(iface, viz) -> str:
    """Serialise an (Interface, VizConfig) to CURRENT-format CSV text."""
    from ..swviz.io.csv_handler import CSVHandler
    fd, tmp = tempfile.mkstemp(suffix=".csv", prefix="swi3s_cfg_")
    os.close(fd)
    try:
        CSVHandler.save_csv(tmp, iface, viz)
        with open(tmp, encoding="utf-8") as f:
            return f.read()
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


def _upgrade_register_text(text: str, source: str = "") -> str:
    """Upgrade a v2.0 register-style CSV to current by round-tripping through the
    swviz model (which fills in the fields v2.0 predates, e.g. DataPortNumber).
    Falls back to the original text if the model can't load it (the C++/BusConfig
    parsers already tolerate a v2.0 file, so opening still works). Fields the model
    does not recognise, which the upgraded text leaves out, and the fallback are
    import warnings for `source`."""
    from ..swviz.io.csv_handler import CSVHandler
    from ..swviz.models import Interface
    from ..swviz.viz import VizConfig
    fd, tmp = tempfile.mkstemp(suffix=".csv", prefix="swi3s_v2_")
    with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
        f.write(text)
    try:
        iface, viz = Interface(), VizConfig()
        res = CSVHandler.load_csv(tmp, iface, viz)
        if not getattr(res, "success", True):
            logger.warning("config CSV: v2.0 upgrade load reported failure; "
                           "using the file as-is")
            import_notes.warn(source, "this v2.0 config could not be upgraded to the "
                              f"current format ({res.error_message or 'the load failed'}); "
                              "it is read as written, and fields v2.0 does not have take "
                              "their defaults.")
            return text
        for note in res.notes(missing=False):
            import_notes.warn(source, f"upgrading this v2.0 config: {note}")
        return _model_to_text(iface, viz)
    except Exception as exc:  # noqa: BLE001 — never block opening a readable v2.0 file
        logger.warning("config CSV: could not upgrade v2.0 file to current; "
                       "using it as-is", exc_info=True)
        import_notes.warn(source, f"this v2.0 config could not be upgraded to the current "
                          f"format ({exc}); it is read as written, and fields v2.0 does not "
                          "have take their defaults.")
        return text
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


def _legacy_short_rows(old: Dict[str, List[str]]) -> List[str]:
    """Per-DP legacy fields with fewer than NUM_DP values, as 'name (n of 12)'."""
    return [f"{name} ({len(vals)} of {NUM_DP})" for name, vals in old.items()
            if name in _DP_FIELDS and len(vals) < NUM_DP]


def to_current_text(text: str, source: str = "") -> str:
    """Upgrade config-CSV `text` to the CURRENT register-style format.

    Already-current (or unrecognised) text is returned unchanged; legacy
    (v1.73/v1.74) and v2.0 files are converted. `source` names the file in the import
    warnings: unrecognised legacy fields, legacy per-port rows short of a value per data
    port, a v2.0 file that does not upgrade, and text with no config fields at all (an empty
    or blank file included)."""
    version = detect_version(text)
    if version == "unknown":
        import_notes.warn(source, "no config field is recognised (no _REG row and "
                          "no legacy field); every setting takes its default.")
        return text
    if version == "current":
        return text
    if version == "legacy":
        skipped: List[str] = []
        changed: List[str] = []
        try:
            old = legacy_dict_from_text(text)
            iface, viz = convert(old, skipped.append, clamped=changed.append)
            out = _model_to_text(iface, viz)
        except Exception as exc:  # noqa: BLE001 — turn a cryptic model error into a clear one
            # A legacy CSV can't be read as-is (the C++/BusConfig parsers only read
            # register-style), so surface a clear failure rather than a raw
            # descriptor ValueError from an out-of-range field.
            raise ValueError(f"could not convert legacy visualizer CSV: {exc}") from exc
        for name in skipped:
            logger.warning("config CSV: skipped unrecognised legacy field %r", name)
        if skipped:
            n = len(skipped)
            import_notes.warn(source, f"{n} legacy field{'s are' if n != 1 else ' is'} not "
                              f"recognised and {'were' if n != 1 else 'was'} skipped: "
                              + ', '.join(clipped(f) for f in skipped[:_LISTED_FIELDS])
                              + (f" and {n - _LISTED_FIELDS:,} more" if n > _LISTED_FIELDS
                                 else "") + ".")
        if changed:
            n = len(changed)
            shown = '; '.join(changed[:4]) + (' …' if n > 4 else '')
            import_notes.warn(source, f"{n} legacy value{'s' if n != 1 else ''} did not fit "
                              f"and {'were' if n != 1 else 'was'} changed: {shown}.")
        short = _legacy_short_rows(old)
        if short:
            import_notes.warn(source, "legacy per-port rows with fewer values than data "
                              f"ports, the missing ports taking defaults: {', '.join(short)}.")
        return out
    return _upgrade_register_text(text, source)       # register_old (v2.0)


def normalized_path(path: str) -> str:
    """Return a filesystem path to a CURRENT-format version of the CSV at `path`.

    If `path` is already current it is returned unchanged (the native parser and the
    Visualizer engine both skip a leading byte-order mark); otherwise it is upgraded
    (legacy or v2.0 -> current) and written to a temp file whose path is returned
    (the C++ parser reads files, not text). The temp file is left for the OS temp
    dir to reclaim; callers that want eager cleanup can remove it after use."""
    text = read_config_text(path)
    name = import_notes.source_name(path)
    try:
        upgraded = to_current_text(text, name)
    except ValueError as exc:
        raise ValueError(f"{name}: {exc}") from None
    if upgraded == text:
        return path
    fd, tmp = tempfile.mkstemp(suffix=".cur.csv", prefix="swi3s_cfg_")
    with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
        f.write(upgraded)
    return tmp


def report_csv_warnings(cfg_path: str, source_path: str = "") -> List[str]:
    """Report, as import warnings for `source_path` (default `cfg_path`), what the native
    loader does not read as written in the current-format CSV at `cfg_path`
    (`swi3score.csv_warnings`): a port disabled for its DeviceNumber_REG, a value masked
    or clamped to its register field, a Reserved CDS_TailWidth 3 (worded as BusConfig words
    it, so one file read by both reports it once), an unreadable cell. Returns the messages; empty
    when the loaded core does not provide `csv_warnings`."""
    import swi3score
    fn = getattr(swi3score, "csv_warnings", None)
    if fn is None or not cfg_path:
        return []
    msgs = [str(m) for m in fn(str(cfg_path))]
    src = import_notes.source_name(source_path or cfg_path)
    for m in msgs:
        import_notes.warn(src, m)
    return msgs

