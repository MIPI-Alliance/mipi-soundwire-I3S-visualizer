"""Visualizer config-CSV version upgrader.

The SWI3S Visualizer / Studio has written its dataport-configuration CSV in
several formats over time:

* **legacy (v1.73 / v1.74)** — human-readable field names (``Columns per Row
  (2-32)``, ``Data Port Enabled``, ``Data Port Horizontal Start`` …) with a
  ``Save file using excess one`` encoding flag. v1.74 split the interval into
  ``Data Port Interval Integer`` + ``Data Port Interval Numerator`` and renamed
  the denominator; v1.73 used a single ``Data Port Interval``.
* **v2.0 ("Soko")** — register-style field names (``NumColumns_REG``,
  ``EnableCh_REG`` …). This is what the C++ ``SwI3sConfig::LoadCsv`` /
  ``swi3score.grid_from_csv`` parser reads.
* **v3.0.0 (current)** — v2.0 plus a per-DP ``DataPortNumber`` row (logical DP
  number, distinct from the column index).

This module detects the format and upgrades any older one to the CURRENT format
so old files open silently everywhere (Compare, the decode config, the authoring
panel). The upgrade always converges on the swviz model — parse into an
``Interface`` / ``VizConfig`` then re-serialise with ``CSVHandler.save_csv`` — so
new fields added in future versions are written automatically; the converter
only has to learn how to READ each older layout, not how to emit the newest one.

``convert()`` (legacy dict → model) is shared with the standalone
``tools/csv_converter/convert_174.py`` CLI, which is now a thin wrapper.
"""
from __future__ import annotations

import csv
import io
import logging
import os
import re
import tempfile
from typing import Callable, Dict, List, Tuple

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

# Trailing "(2-32)", "(0-8)", "[kHz] (1-6144)" are descriptive — drop when matching.
_RANGE_RE = re.compile(r'\s*\([^)]*\)\s*$')


def _normalize(name: str) -> str:
    return _RANGE_RE.sub('', name).strip()


def _parse_bool(s: str) -> bool:
    return str(s).strip().lower() in ('true', '1', 'yes')


def _rows(text: str) -> List[Tuple[str, List[str]]]:
    out: List[Tuple[str, List[str]]] = []
    for row in csv.reader(io.StringIO(text)):
        if not row or not row[0].strip():
            continue
        out.append((_normalize(row[0]), [v.strip() for v in row[1:]]))
    return out


def legacy_dict_from_text(text: str) -> Dict[str, List[str]]:
    """Parse legacy CSV text into {normalised field name -> values}."""
    return {f: v for f, v in _rows(text)}


def read_legacy_csv(path) -> Dict[str, List[str]]:
    """Read a legacy CSV file into {normalised field name -> values}."""
    with open(path, 'r', encoding='utf-8') as f:
        return legacy_dict_from_text(f.read())


def _get_int(old: Dict[str, List[str]], field: str, i: int, default: int) -> int:
    vals = old.get(field, [])
    return int(vals[i]) if i < len(vals) else default


def _get_bool(old: Dict[str, List[str]], field: str, i: int, default: bool) -> bool:
    vals = old.get(field, [])
    return _parse_bool(vals[i]) if i < len(vals) else default


def _get_str(old: Dict[str, List[str]], field: str, i: int, default: str) -> str:
    vals = old.get(field, [])
    return vals[i] if i < len(vals) else default


def convert(old: Dict[str, List[str]], warn: Callable[[str], None],
            source_stem: str = ''):
    """Convert a parsed legacy (v1.73/v1.74) config dict to (Interface, VizConfig).

    Shared by the app's silent open path and the convert_174 CLI. Unrecognised
    fields are reported via ``warn``."""
    from ..swviz.config.constants import SpecialDevices
    from ..swviz.models import Interface
    from ..swviz.models.enums import DisplayField
    from ..swviz.version import app_version
    from ..swviz.viz import VizConfig

    # app_version(), not the APP_VERSION literal: this note is the ONE place that read
    # the literal directly, so a legacy conversion recorded 'to v3.0.8' for three
    # releases while the app itself displayed the right version.
    note = f'converted from SWI3S legacy CSV to v{app_version()}'
    iface = Interface()
    viz = VizConfig()
    iface.description = f'{source_stem} — {note}' if source_stem else note

    # Legacy writes `Save file using excess one,True` when the four count-style DP
    # fields (Channels, Sample Width, Sample Grouping, Interval) are stored as raw
    # REGs; otherwise they're 1-based counts needing a -1. `Columns per Row` is
    # always a natural value regardless, so NumColumns_REG is always value - 1.
    already_excess_one = _parse_bool((old.get('Save file using excess one') or ['False'])[0])

    # --- Interface --- (guard on truthiness: a present-but-empty row has [] values,
    # so `in old` would pass but old[k][0] would IndexError.)
    if old.get('Columns per Row'):
        iface.NumColumns_REG = int(old['Columns per Row'][0]) - 1
    if old.get('S0 S1 Enabled'):
        iface.phy3_enabled = _parse_bool(old['S0 S1 Enabled'][0])
    if old.get('S0 Width'):
        iface.s0_width = int(old['S0 Width'][0])
    if old.get('CDS Guard Enabled'):
        iface.CDS_GuardEnabled_REG = _parse_bool(old['CDS Guard Enabled'][0])
    # Older files predate the Guard 0/Guard 1 polarity choice; absent -> Guard 0
    # (False), matching the pre-polarity behaviour.
    if old.get('CDS Guard Polarity'):
        iface.CDS_GuardPolarity_REG = _parse_bool(old['CDS Guard Polarity'][0])
    if old.get('CDS Tail Width'):
        iface.CDS_TailWidth_REG = int(old['CDS Tail Width'][0])
    # v1.74's UI label is "Skipping Denominator"; its loader expects "Interval
    # Denominator". Accept either; prefer the newer name if both exist.
    denom = old.get('Skipping Denominator') or old.get('Interval Denominator')
    if denom:
        iface.SkippingDenominator_REG = int(denom[0])
    if old.get('CDS/S0 Handover Width'):
        # Legacy packed CDS handover and S1 tail into one width field. Split:
        # EnforceCDSHandover on when width>=1; S1TailWidth_REG is 0 for width 0-1,
        # 1 for width 2, 2 for width >=3.
        w = int(old['CDS/S0 Handover Width'][0])
        iface.cds_handover_enabled = w > 0
        iface.tail_width = min(2, max(0, w - 1))
    if old.get('Draw S0 Handover'):
        iface.s1_handover_enabled = _parse_bool(old['Draw S0 Handover'][0])
    if old.get('Row Rate [kHz]'):
        iface.row_rate = float(old['Row Rate [kHz]'][0])
    if old.get('Rows to Draw'):
        viz.rows_to_draw = int(old['Rows to Draw'][0])

    # Subtract 1 unless the file was saved in excess-one mode (REGs on disk).
    def excess1(field: str, i: int, default: int) -> int:
        raw = _get_int(old, field, i, default)
        return raw if already_excess_one else max(0, raw - 1)

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
        cfg.PortDirection_REG = not _get_bool(old, 'Source', i, True)
        cfg.GuardEnable_REG = _get_bool(old, 'Data Port Guard Enabled', i, False)
        cfg.SubRowInterval_REG = _get_bool(old, 'Data Port DRI', i, False)

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
        viz_dp.enabled = _get_bool(old, 'Data Port Enabled', i, False)
        viz_dp.enable_handover = _get_bool(old, 'Draw Data Port Handover', i, True)
        viz_dp.display_fields = DisplayField.SAMPLE | DisplayField.CHANNEL

        if _get_bool(old, 'Data Port In Manager', i, False):
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


def _upgrade_register_text(text: str) -> str:
    """Upgrade a v2.0 register-style CSV to current by round-tripping through the
    swviz model (which fills in the fields v2.0 predates, e.g. DataPortNumber).
    Falls back to the original text if the model can't load it (the C++/BusConfig
    parsers already tolerate a v2.0 file, so opening still works)."""
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
            return text
        return _model_to_text(iface, viz)
    except Exception:  # noqa: BLE001 — never block opening a readable v2.0 file
        logger.warning("config CSV: could not upgrade v2.0 file to current; "
                       "using it as-is", exc_info=True)
        return text
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


def to_current_text(text: str) -> str:
    """Upgrade config-CSV `text` to the CURRENT register-style format.

    Already-current (or unrecognised) text is returned unchanged; legacy
    (v1.73/v1.74) and v2.0 files are converted."""
    version = detect_version(text)
    if version in ("current", "unknown"):
        return text
    if version == "legacy":
        try:
            iface, viz = convert(legacy_dict_from_text(text),
                                 lambda name: logger.warning(
                                     "config CSV: skipped unrecognised legacy field %r", name))
            return _model_to_text(iface, viz)
        except Exception as exc:  # noqa: BLE001 — turn a cryptic model error into a clear one
            # A legacy CSV can't be read as-is (the C++/BusConfig parsers only read
            # register-style), so surface a clear failure rather than a raw
            # descriptor ValueError from an out-of-range field.
            raise ValueError(f"could not convert legacy visualizer CSV: {exc}") from exc
    return _upgrade_register_text(text)               # register_old (v2.0)


def normalized_path(path: str) -> str:
    """Return a filesystem path to a CURRENT-format version of the CSV at `path`.

    If `path` is already current it is returned unchanged; otherwise it is upgraded
    (legacy or v2.0 -> current) and written to a temp file whose path is returned
    (the C++ parser reads files, not text). The temp file is left for the OS temp
    dir to reclaim; callers that want eager cleanup can remove it after use."""
    with open(path, encoding="utf-8") as f:
        text = f.read()
    upgraded = to_current_text(text)
    if upgraded == text:
        return path
    fd, tmp = tempfile.mkstemp(suffix=".cur.csv", prefix="swi3s_cfg_")
    with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
        f.write(upgraded)
    return tmp
