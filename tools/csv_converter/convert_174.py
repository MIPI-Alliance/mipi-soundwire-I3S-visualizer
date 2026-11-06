#!/usr/bin/env python3
"""Convert SWI3S Visualizer legacy (v1.73 / v1.74) CSV configs to the current format.

The conversion itself lives in the app package
(:func:`swi3s_studio.ingest.visualizer_csv.convert`), which the GUI uses to open
legacy files directly; this is a batch CLI wrapper for converting files on disk.
Target format version tracks
``swi3s_studio.swviz.version.app_version()``, the package ``__version__``.

Usage:
    python3 tools/csv_converter/convert_174.py <input.csv>
    python3 tools/csv_converter/convert_174.py <input_dir>
    python3 tools/csv_converter/convert_174.py <input> -o <output>

If <input> is a file, writes one converted CSV (default:
    <input_stem>_converted.csv alongside the input).
If <input> is a directory, converts every *.csv inside (default output:
    <input_dir>_converted/ with each file also suffixed _converted.csv).

Unrecognized legacy fields are warned to stderr and skipped; a value changed to fit
(a count below 1, more channels than a port has, Rows to Draw or Row Rate out of range, a
CDS/S0 Handover Width outside 0 to 3, a CDS Guard Polarity on a disabled guard, a flag that
is not true or false) is warned too. Per-port register values are copied as read.
A file with no field at all converts to the default config, with a warning. A file that
cannot be converted (unreadable, too large, a cell that is not a whole number, a Row Rate
that is not finite, or a Columns per Row, S0 Width, CDS Tail Width or Skipping Denominator
the format cannot hold) is
named on stderr as ``error: <file>: <reason>`` and the remaining files are still converted;
the exit status is 1 when any file failed.
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
    if not old:
        print(f"  warning: {in_path.name}: holds no legacy field; the default config was written",
              file=sys.stderr)
    unknown: List[str] = []
    changed: List[str] = []
    iface, viz = convert(old, unknown.append, source_stem=in_path.stem, clamped=changed.append)
    CSVHandler.save_csv(str(out_path), iface, viz)
    if unknown:
        print(f"  warning: {in_path.name}: skipped {len(unknown)} unrecognized field(s): "
              f"{', '.join(unknown)}", file=sys.stderr)
    for note in changed:
        print(f"  warning: {in_path.name}: {note}", file=sys.stderr)
    if verbose:
        print(f"  {in_path} -> {out_path}")


def _converted(in_path: Path, out_path: Path, verbose: bool) -> bool:
    """Convert one file; a file that cannot be converted is named on stderr and is False."""
    try:
        convert_file(in_path, out_path, verbose)
    except (ValueError, OSError) as exc:
        text = str(exc)
        if not text.startswith(in_path.name):
            text = f"{in_path.name}: {text}"
        print(f"error: {text}", file=sys.stderr)
        return False
    return True


def main() -> int:
    p = argparse.ArgumentParser(description='Convert SWI3S legacy CSV to the current format.')
    p.add_argument('input', help='Input .csv file or directory of .csv files')
    p.add_argument('-o', '--output',
                   help='Output file (if input is a file) or directory (if input is a directory). '
                        'Defaults: <stem>_converted.csv or <input_dir>_converted/.')
    p.add_argument('-v', '--verbose', action='store_true',
                   help='Print each file path as it is converted')
    args = p.parse_args()

    in_path = Path(args.input)
    if in_path.is_file():
        out = (Path(args.output) if args.output
               else in_path.with_name(in_path.stem + '_converted.csv'))
        try:
            out.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            print(f"error: {out.parent}: {exc}", file=sys.stderr)
            return 1
        if not _converted(in_path, out, args.verbose):
            return 1
        print(f"Wrote {out}")
        return 0
    if in_path.is_dir():
        out_dir = (Path(args.output) if args.output
                   else in_path.parent / (in_path.name + '_converted'))
        csvs = sorted(f for f in in_path.iterdir()
                      if f.suffix.lower() == '.csv' and f.is_file())
        if not csvs:
            print(f"No .csv files in {in_path}", file=sys.stderr)
            return 1
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            print(f"error: {out_dir}: {exc}", file=sys.stderr)
            return 1
        done = sum(_converted(src, out_dir / (src.stem + '_converted.csv'), args.verbose)
                   for src in csvs)
        print(f"Converted {done} of {len(csvs)} file(s) to {out_dir}")
        return 0 if done == len(csvs) else 1
    print(f"Error: {in_path} not found", file=sys.stderr)
    return 1


if __name__ == '__main__':
    sys.exit(main())
