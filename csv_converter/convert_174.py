#!/usr/bin/env python3
"""Convert SWI3S visualizer v1.74 CSV configurations to the current format.

Target format version is read from src.version.APP_VERSION, so this tool
stays aligned with the rest of the app on version bumps.

Usage:
    python3 csv_converter/convert_174.py <input.csv>
    python3 csv_converter/convert_174.py <input_dir>
    python3 csv_converter/convert_174.py <input> -o <output>

If <input> is a file, writes one converted CSV (default:
    <input_stem>_converted.csv alongside the input).
If <input> is a directory, converts every *.csv inside (default output:
    <input_dir>_converted/ with each file also suffixed _converted.csv).

Unrecognized v1.74 fields are warned to stderr and skipped.
See csv_converter/README.md for the full mapping table and defaults.
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
from pathlib import Path
from typing import Callable, Dict, List, Tuple

# Make src.* importable when run directly.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config.constants import SpecialDevices
from src.io.csv_handler import CSVHandler
from src.models import Interface
from src.models.enums import DisplayField
from src.version import APP_VERSION
from src.viz import VizConfig


NUM_DP = 12

CONVERSION_NOTE_SUFFIX = f'converted from SWI3S v1.74 to v{APP_VERSION}'

# v1.74 field names (already normalized: trailing "(range)" stripped).
# "Skipping Denominator" is v1.74's current UI label; "Interval Denominator" is
# an older name still recognised by v1.74's loader — accept either.
_INTERFACE_FIELDS = {
    'Columns per Row',
    'S0 S1 Enabled',
    'S0 Width',
    'CDS Guard Enabled',
    'CDS Tail Width',
    'Interval Denominator',
    'Skipping Denominator',
    'CDS/S0 Handover Width',
    'Draw S0 Handover',
    'Row Rate [kHz]',
    'Rows to Draw',
}
_DP_FIELDS = {
    'Data Port Name',
    'Data Port Device Number',
    'Data Port Channels',
    'Data Port Channel Grouping',
    'Data Port Channel Group Spacing',
    'Data Port Sample Width',
    'Data Port Sample Grouping',
    'Data Port Interval Integer',
    'Data Port Interval Numerator',
    'Data Port Offset',
    'Data Port Horizontal Start',
    'Data Port Horizontal Count',
    'Data Port Tail Width',
    'Data Port Bit Width',
    'Source',
    'Draw Data Port Handover',
    'Data Port Guard Enabled',
    'Data Port Enabled',
    'Data Port In Manager',
    'Data Port DRI',
}
# v1.74 files may include a legacy encoding flag; we act on it (see convert())
# but don't want it to show up as unrecognized.
_SILENT_SKIP = {'Save file using excess one'}
_KNOWN = _INTERFACE_FIELDS | _DP_FIELDS | _SILENT_SKIP

# Trailing "(2-32)", "(0-8)", "[kHz] (1-6144)" are descriptive — drop when matching.
_RANGE_RE = re.compile(r'\s*\([^)]*\)\s*$')


def _normalize(name: str) -> str:
    return _RANGE_RE.sub('', name).strip()


def _parse_bool(s: str) -> bool:
    return s.strip().lower() in ('true', '1', 'yes')


def _read_old_csv(path: Path) -> Dict[str, List[str]]:
    rows: Dict[str, List[str]] = {}
    with open(path, 'r', encoding='utf-8') as f:
        for row in csv.reader(f):
            if not row:
                continue
            rows[_normalize(row[0])] = [v.strip() for v in row[1:]]
    return rows


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
            source_stem: str = '') -> Tuple[Interface, VizConfig]:
    iface = Interface()
    viz = VizConfig()

    iface.description = (f'{source_stem} — {CONVERSION_NOTE_SUFFIX}'
                         if source_stem else CONVERSION_NOTE_SUFFIX)

    # v1.74 writes `Save file using excess one,True` at the top when the four
    # DP fields (Channels, Sample Width, Sample Grouping, Interval Integer) are
    # stored as raw REGs; otherwise they're stored as 1-based counts that need
    # a -1 to become REGs. `columns_per_row` is always a natural value on disk
    # regardless of the flag, so NumColumns_REG is always csv_value - 1.
    already_excess_one = _parse_bool(old.get('Save file using excess one', ['False'])[0])

    # --- Interface ---
    if 'Columns per Row' in old:
        iface.NumColumns_REG = int(old['Columns per Row'][0]) - 1
    if 'S0 S1 Enabled' in old:
        iface.phy3_enabled = _parse_bool(old['S0 S1 Enabled'][0])
    if 'S0 Width' in old:
        iface.s0_width = int(old['S0 Width'][0])
    if 'CDS Guard Enabled' in old:
        iface.CDS_GuardEnabled_REG = _parse_bool(old['CDS Guard Enabled'][0])
    if 'CDS Tail Width' in old:
        iface.CDS_TailWidth_REG = int(old['CDS Tail Width'][0])
    # v1.74's UI label is "Skipping Denominator"; its loader expects
    # "Interval Denominator". Accept either; prefer the newer name if both exist.
    denom = old.get('Skipping Denominator') or old.get('Interval Denominator')
    if denom:
        iface.SkippingDenominator_REG = int(denom[0])
    if 'CDS/S0 Handover Width' in old:
        # v1.74 packed CDS handover and S1 tail into one width field.
        # Split: EnforceCDSHandover on when width>=1; S1TailWidth_REG is
        # 0 for width 0-1, 1 for width 2, 2 for width >=3.
        w = int(old['CDS/S0 Handover Width'][0])
        iface.cds_handover_enabled = w > 0
        iface.tail_width = min(2, max(0, w - 1))
    if 'Draw S0 Handover' in old:
        iface.s1_handover_enabled = _parse_bool(old['Draw S0 Handover'][0])
    if 'Row Rate [kHz]' in old:
        iface.row_rate = float(old['Row Rate [kHz]'][0])
    if 'Rows to Draw' in old:
        viz.rows_to_draw = int(old['Rows to Draw'][0])

    # Subtract 1 unless the file was saved in excess-one mode (REGs on disk).
    def excess1(field: str, i: int, default: int) -> int:
        raw = _get_int(old, field, i, default)
        return raw if already_excess_one else max(0, raw - 1)

    # --- Per-DP ---
    for i in range(NUM_DP):
        cfg = iface.data_ports[i].config

        # Channels: human form is a count N (1 → 1 channel); excess-one form
        # stores N-1 on disk. Both paths end at a (1<<count)-1 bitmask.
        raw_ch = _get_int(old, 'Data Port Channels', i, 1)
        n_channels = (raw_ch + 1) if already_excess_one else raw_ch
        cfg.EnableCh_REG = (1 << n_channels) - 1 if n_channels > 0 else 0

        cfg.ChannelGrouping_REG = _get_int(old, 'Data Port Channel Grouping', i, 0)
        cfg.Spacing_REG = _get_int(old, 'Data Port Channel Group Spacing', i, 0)
        cfg.SampleSize_REG = excess1('Data Port Sample Width', i, 1)
        cfg.SampleGrouping_REG = excess1('Data Port Sample Grouping', i, 1)
        cfg.Interval_REG = excess1('Data Port Interval Integer', i, 1)
        cfg.SkippingNumerator_REG = _get_int(old, 'Data Port Interval Numerator', i, 0)
        cfg.Offset_REG = _get_int(old, 'Data Port Offset', i, 0)
        cfg.HorizontalStart_REG = _get_int(old, 'Data Port Horizontal Start', i, 0)
        cfg.HorizontalCount_REG = _get_int(old, 'Data Port Horizontal Count', i, 0)
        cfg.TailWidth_REG = _get_int(old, 'Data Port Tail Width', i, 0)
        cfg.BitWidth_REG = _get_int(old, 'Data Port Bit Width', i, 0)
        # v1.74 Source=True means "is a source". PortDirection_REG=0 for source.
        cfg.PortDirection_REG = not _get_bool(old, 'Source', i, True)
        cfg.GuardEnable_REG = _get_bool(old, 'Data Port Guard Enabled', i, False)
        cfg.SubRowInterval_REG = _get_bool(old, 'Data Port DRI', i, False)

        # _REG fields with no v1.74 source — user-specified defaults.
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


def convert_file(in_path: Path, out_path: Path, verbose: bool) -> None:
    old = _read_old_csv(in_path)
    unknown: List[str] = []
    iface, viz = convert(old, unknown.append, source_stem=in_path.stem)
    CSVHandler.save_csv(str(out_path), iface, viz)
    if unknown:
        print(f"  warning: {in_path.name}: skipped {len(unknown)} unrecognized field(s): "
              f"{', '.join(unknown)}", file=sys.stderr)
    if verbose:
        print(f"  {in_path} -> {out_path}")


def main() -> int:
    p = argparse.ArgumentParser(description='Convert SWI3S v1.74 CSV to the current format.')
    p.add_argument('input', help='Input .csv file or directory of .csv files')
    p.add_argument('-o', '--output',
                   help='Output file (if input is a file) or directory (if input is a directory). '
                        'Defaults: <stem>_converted.csv or <input_dir>_converted/.')
    p.add_argument('-v', '--verbose', action='store_true', help='Print each file path as it is converted')
    args = p.parse_args()

    in_path = Path(args.input)
    if in_path.is_file():
        out = Path(args.output) if args.output else in_path.with_name(in_path.stem + '_converted.csv')
        out.parent.mkdir(parents=True, exist_ok=True)
        convert_file(in_path, out, args.verbose)
        print(f"Wrote {out}")
        return 0
    if in_path.is_dir():
        out_dir = Path(args.output) if args.output else in_path.parent / (in_path.name + '_converted')
        out_dir.mkdir(parents=True, exist_ok=True)
        csvs = sorted(in_path.glob('*.csv'))
        if not csvs:
            print(f"No .csv files in {in_path}", file=sys.stderr)
            return 1
        for src in csvs:
            convert_file(src, out_dir / (src.stem + '_converted.csv'), args.verbose)
        print(f"Converted {len(csvs)} file(s) to {out_dir}")
        return 0
    print(f"Error: {in_path} not found", file=sys.stderr)
    return 1


if __name__ == '__main__':
    sys.exit(main())
