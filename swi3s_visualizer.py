#!/usr/bin/env python3
"""
SWI3S Visualizer - Unified CLI for GUI and headless modes.

Usage:
    # GUI mode (default - no -o flag)
    python swi3s_visualizer.py -c config.csv
    python swi3s_visualizer.py  # Opens file dialog

    # Headless mode (implied by -o flag)
    python swi3s_visualizer.py -c config.csv -o output.json
    python swi3s_visualizer.py -c config.csv -o output.json -s roundtrip.csv -v

Copyright (c) 2020-2026 MIPI Alliance and other contributors. All Rights Reserved.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

APP_VERSION = '2.1.9'


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description='SWI3S Visualizer - SoundWire I3S Frame Visualizer',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    # GUI mode (no -o flag)
    %(prog)s -c config.csv

    # Headless mode (-o flag implies headless)
    %(prog)s -c config.csv -o output.json
    %(prog)s -c config.csv -o output.json -s roundtrip.csv -v
        """
    )

    # Input/output
    parser.add_argument(
        '-c', '--config',
        dest='config_file',
        metavar='FILE',
        help='Input CSV configuration file'
    )
    parser.add_argument(
        '-o', '--output',
        dest='output_file',
        metavar='FILE',
        help='Output JSON file (implies headless mode)'
    )
    parser.add_argument(
        '-s', '--save-csv',
        dest='save_csv_file',
        metavar='FILE',
        help='Save configuration to CSV file'
    )

    # Options
    parser.add_argument(
        '-r', '--rows',
        type=int,
        default=0,
        metavar='N',
        help='Number of rows to draw (0 = use CSV value or 64)'
    )
    parser.add_argument(
        '-v', '--verbose',
        action='store_true',
        help='Enable verbose debug logging'
    )
    parser.add_argument(
        '-t', '--theme',
        choices=['light', 'dark', 'system'],
        default='system',
        help='UI appearance mode: light, dark, or system (default: system)'
    )

    return parser.parse_args()


def run_headless(args: argparse.Namespace) -> int:
    """Run in headless mode - no GUI.

    Args:
        args: Command-line arguments

    Returns:
        Exit code (0 = success, 1 = error, 2 = clashes detected)
    """
    from src.models import Interface
    from src.models.bus_model import BusModelJSONEncoder
    from src.core import BusModelBuilder
    from src.io.csv_handler import CSVHandler
    from src.viz import VizConfig
    from src.utils.logging_config import setup_logging

    # Setup logging
    logger = setup_logging(verbose=args.verbose)

    # Validate required arguments
    if not args.config_file:
        print("Error: --config/-c is required when using --output/-o", file=sys.stderr)
        return 1

    # Validate input file exists
    config_path = Path(args.config_file)
    if not config_path.exists():
        print(f"Error: Configuration file not found: {args.config_file}", file=sys.stderr)
        return 1

    # Create interface and viz config, then load configuration
    interface = Interface()
    viz_config = VizConfig()
    logger.info(f'Loading configuration from {args.config_file}')

    result = CSVHandler.load_csv(args.config_file, interface, viz_config)
    if not result.success:
        print(f"Error loading CSV: {result.error_message}", file=sys.stderr)
        return 1

    # Warn about unrecognized fields
    if result.unrecognized_fields:
        print(f"Warning: {len(result.unrecognized_fields)} unrecognized field(s) in CSV",
              file=sys.stderr)
        for line_num, field_name in result.unrecognized_fields[:5]:
            print(f"  Line {line_num}: '{field_name}'", file=sys.stderr)
        if len(result.unrecognized_fields) > 5:
            print(f"  ... and {len(result.unrecognized_fields) - 5} more", file=sys.stderr)

    # Warn about missing fields (using default values)
    if result.missing_fields:
        print(f"Warning: {len(result.missing_fields)} expected field(s) missing from CSV (using defaults)",
              file=sys.stderr)
        for field_desc in result.missing_fields[:5]:
            print(f"  {field_desc}", file=sys.stderr)
        if len(result.missing_fields) > 5:
            print(f"  ... and {len(result.missing_fields) - 5} more", file=sys.stderr)

    # Determine number of rows - command-line override takes precedence
    if args.rows > 0:
        viz_config.rows_to_draw = args.rows
    num_rows = viz_config.rows_to_draw
    logger.info(f'Building bus model: {num_rows} rows x {interface.num_columns} columns')

    # Build bus model
    builder = BusModelBuilder(interface, num_rows, viz_config)
    bus_model = builder.build()

    # Save JSON output
    logger.info(f'Saving bus model to {args.output_file}')
    with open(args.output_file, 'w') as f:
        json.dump(bus_model, f, cls=BusModelJSONEncoder, indent=2)

    # Save CSV if requested (for round-trip testing)
    if args.save_csv_file:
        logger.info(f'Saving CSV to {args.save_csv_file}')
        CSVHandler.save_csv(args.save_csv_file, interface, viz_config)

    # Report results
    print(f"Bus model saved to: {args.output_file}")
    print(f"  Rows: {bus_model.num_rows}")
    print(f"  Columns: {bus_model.num_columns}")
    print(f"  Total bits: {len(bus_model.bits)}")

    # Report all warnings and issues (matching GUI Notifications panel)
    has_critical = False

    # Critical: Bus clashes (physical bus collisions)
    if bus_model.bus_clashes:
        print(f"\nWARNING: {len(bus_model.bus_clashes)} bus clash(es) detected")
        has_critical = True

    # Warning: Device clashes (internal conflicts)
    if bus_model.device_clashes:
        print(f"\nWARNING: {len(bus_model.device_clashes)} internal device clash(es) detected")

    # Warning: Flow control issues (TxP/DRQ mismatches)
    flow_issues = []
    if bus_model.txp_mismatches:
        flow_issues.append(f"{len(bus_model.txp_mismatches)} TxP source(s) without sink")
    if bus_model.txp_orphan_sinks:
        flow_issues.append(f"{len(bus_model.txp_orphan_sinks)} TxP sink(s) without source")
    if bus_model.drq_mismatches:
        flow_issues.append(f"{len(bus_model.drq_mismatches)} DRQ source(s) without sink")
    if bus_model.drq_orphan_sinks:
        flow_issues.append(f"{len(bus_model.drq_orphan_sinks)} DRQ sink(s) without source")
    if flow_issues:
        print(f"\nWARNING: Flow control issues: {', '.join(flow_issues)}")

    # Warning: Scrambler mismatches
    if bus_model.scrambler_mismatches:
        print(f"\nWARNING: {len(bus_model.scrambler_mismatches)} scrambler mismatch(es) between source and sink")

    # Warning: Test mode mismatches
    if bus_model.test_mode_mismatches:
        print(f"\nWARNING: {len(bus_model.test_mode_mismatches)} test mode mismatch(es) between data ports")

    # Warning: Truncation (interval overflow - configuration error)
    if bus_model.interval_overflow_warnings:
        print(f"\nWARNING: {len(bus_model.interval_overflow_warnings)} data port(s) with interval overflow (bits don't fit)")
        for dp_name, bits_needed, bits_available in bus_model.interval_overflow_warnings:
            print(f"  {dp_name}: needs {bits_needed} bits, only {bits_available} fit in interval")

    # Warning: DRQ truncation (wide DRQ can't complete within a row)
    if bus_model.drq_truncation_warnings:
        print(f"\nWARNING: {len(bus_model.drq_truncation_warnings)} DRQ configuration(s) truncated (wide DRQ doesn't fit in row)")
        for dp_name, last_ui_column, num_columns in bus_model.drq_truncation_warnings:
            print(f"  {dp_name}: wide DRQ last UI at column {last_ui_column}, row ends at {num_columns}")
        has_critical = True

    # Warning: Sample/bit mismatches
    if bus_model.sample_bit_mismatches:
        print(f"\nWARNING: {len(bus_model.sample_bit_mismatches)} sample/bit mismatch(es) between source and sink")

    # Info: Display truncation (user can increase RowsToDraw)
    if bus_model.display_truncation_warnings:
        print(f"\nINFO: {len(bus_model.display_truncation_warnings)} data port(s) truncated (increase RowsToDraw to see full interval)")
        for dp_name, interval_rows, displayed_rows in bus_model.display_truncation_warnings:
            print(f"  {dp_name}: interval is {interval_rows} rows, only {displayed_rows} displayed")

    # Warning: Sink handover (informational)
    if bus_model.sink_handover_warnings:
        print(f"\nINFO: {len(bus_model.sink_handover_warnings)} sink dataport(s) with handover enabled (no effect without DRQ/TxP)")

    # Info: Read overlaps (expected on bus, just visual limitation)
    if bus_model.read_overlaps:
        print(f"\nINFO: {len(bus_model.read_overlaps)} read overlap(s) (visual only, expected on bus)")

    return 2 if has_critical else 0


def run_gui(args: argparse.Namespace) -> None:
    """Run in GUI mode with tkinter.

    Args:
        args: Command-line arguments
    """
    # Check Tk version - 8.6 on macOS has button event issues (fixed in Tk 9.0)
    import tkinter
    tk_version = float(tkinter.TkVersion)
    if tk_version < 9.0 and sys.platform == 'darwin':
        print(f"WARNING: Python {sys.version.split()[0]} has known GUI issues on macOS.", file=sys.stderr)
        print("         Please upgrade to Python 3.13+ from python.org", file=sys.stderr)

    from src.utils.logging_config import setup_logging
    from src.ui.minimal_app import run_app

    # Setup logging (quiet by default)
    setup_logging(verbose=args.verbose)

    run_app(args, APP_VERSION)


def main() -> int:
    """Main entry point.

    Returns:
        Exit code (0 for success, non-zero for errors)
    """
    args = parse_args()

    if args.output_file:
        # -o implies headless mode
        return run_headless(args)
    else:
        # GUI mode
        run_gui(args)
        return 0


if __name__ == '__main__':
    sys.exit(main())
