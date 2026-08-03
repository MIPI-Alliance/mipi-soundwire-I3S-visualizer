#!/usr/bin/env python3
"""Convert SWI3S Visualizer legacy (v1.73 / v1.74) CSV configs to the current format.

The conversion itself now lives in the app package
(:func:`swi3s_studio.ingest.visualizer_csv.convert`) so the GUI can open legacy
files silently without this tool; this remains as a batch CLI wrapper for
converting files on disk. Target format version tracks
``swi3s_studio.swviz.version.APP_VERSION``.

Usage:
    python3 tools/csv_converter/convert_174.py <input.csv>
    python3 tools/csv_converter/convert_174.py <input_dir>
    python3 tools/csv_converter/convert_174.py <input> -o <output>

If <input> is a file, writes one converted CSV (default:
    <input_stem>_converted.csv alongside the input).
If <input> is a directory, converts every *.csv inside (default output:
    <input_dir>_converted/ with each file also suffixed _converted.csv).

Unrecognized legacy fields are warned to stderr and skipped.
See tools/csv_converter/README.md for the full mapping table and defaults.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import List

# Make the swi3s_studio package importable when run directly (repo root is two
# levels up: tools/csv_converter/ -> repo root).
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from swi3s_studio.ingest.visualizer_csv import convert, read_legacy_csv
from swi3s_studio.swviz.io.csv_handler import CSVHandler


def convert_file(in_path: Path, out_path: Path, verbose: bool) -> None:
    old = read_legacy_csv(in_path)
    unknown: List[str] = []
    iface, viz = convert(old, unknown.append, source_stem=in_path.stem)
    CSVHandler.save_csv(str(out_path), iface, viz)
    if unknown:
        print(f"  warning: {in_path.name}: skipped {len(unknown)} unrecognized field(s): "
              f"{', '.join(unknown)}", file=sys.stderr)
    if verbose:
        print(f"  {in_path} -> {out_path}")


def main() -> int:
    p = argparse.ArgumentParser(description='Convert SWI3S legacy CSV to the current format.')
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
