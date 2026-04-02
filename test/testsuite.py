#!/usr/bin/env python3

"""
Copyright (c) 2023 MIPI Alliance and other contributors. All Rights Reserved.

Redistribution and use in source and binary forms, with or without modification, are permitted provided that the following conditions are met:

1. Redistributions of source code must retain the above copyright notice, this list of conditions and the following disclaimer.

2. Redistributions in binary form must reproduce the above copyright notice, this list of conditions and the following disclaimer in the documentation and/or other materials provided with the distribution.

3. Neither the name of the copyright holder nor the names of its contributors may be used to endorse or promote products derived from this software without specific prior written permission.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

Optimized Test Suite for SWI3S Visualizer
=========================================

Uses headless batch mode (swi3s_visualizer.py) for testing.

Test flow per configuration (single batch invocation):
1. Load CSV config
2. Write temp CSV (round-trip test)
3. Write temp JSON (bus model output)
4. Diff temp JSON vs reference JSON

Usage:
    python3 testsuite.py                 # Run all tests
    python3 testsuite.py --regenerate    # Regenerate reference JSON files
"""

import unittest
import os
import subprocess
import sys
import shutil
import json
import time
from datetime import datetime
from collections import defaultdict
from typing import Dict, Any, Optional


# File to store previous run statistics for comparison
PREVIOUS_STATS_FILE = 'previous_stats.json'


def parse_json_stats(json_path: str) -> dict:
    """Parse JSON output file and extract statistics for summary.

    Args:
        json_path: Path to the JSON output file

    Returns:
        Dictionary with statistics about bits, slots, clashes, etc.
    """
    try:
        with open(json_path, 'r') as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}

    stats = {
        'num_rows': data.get('num_rows', 0),
        'num_columns': data.get('num_columns', 0),
        'row_rate': data.get('row_rate', 0),
        'total_bit_positions': 0,
        'total_slots': 0,
        'slot_types': defaultdict(int),
        'direction_counts': defaultdict(int),
        'data_port_bits': defaultdict(int),
        'device_bits': defaultdict(int),
        'bus_clashes': len(data.get('bus_clashes', [])),
        'device_clashes': len(data.get('device_clashes', [])),
        'read_overlaps': len(data.get('read_overlaps', [])),
        'clash_positions': data.get('bus_clashes', []) + data.get('device_clashes', []),
        'handover_count': 0,
    }

    bits = data.get('bits', {})
    stats['total_bit_positions'] = len(bits)

    for bit_index, bit_data in bits.items():
        slots = bit_data.get('slots', [])
        stats['total_slots'] += len(slots)

        for slot in slots:
            slot_type = slot.get('slot', 'UNKNOWN')
            stats['slot_types'][slot_type] += 1

            if slot_type == 'HANDOVER':
                stats['handover_count'] += 1

            direction = slot.get('direction', 'UNKNOWN')
            stats['direction_counts'][direction] += 1

            dp = slot.get('dp')
            if dp is not None:
                stats['data_port_bits'][dp] += 1

            device = slot.get('device')
            if device is not None:
                stats['device_bits'][device] += 1

    return stats


def load_previous_stats(tests_dir: str) -> Optional[Dict[str, Any]]:
    """Load previous run statistics for comparison.

    Args:
        tests_dir: Directory containing the previous_stats.json file

    Returns:
        Dictionary with previous stats, or None if not found
    """
    stats_path = os.path.join(tests_dir, PREVIOUS_STATS_FILE)
    try:
        with open(stats_path, 'r') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def save_current_stats(tests_dir: str, stats: Dict[str, Any]) -> None:
    """Save current run statistics for future comparison.

    Args:
        tests_dir: Directory to save the previous_stats.json file
        stats: Dictionary with current aggregate statistics
    """
    stats_path = os.path.join(tests_dir, PREVIOUS_STATS_FILE)
    stats['timestamp'] = datetime.now().isoformat()
    with open(stats_path, 'w') as f:
        json.dump(stats, f, indent=2)


def format_delta(current: int, previous: Optional[int]) -> str:
    """Format a delta value for display.

    Args:
        current: Current value
        previous: Previous value (or None if no previous)

    Returns:
        String like "+5" or "-3" or "0" if no change, "" if no previous
    """
    if previous is None:
        return ""
    delta = current - previous
    if delta == 0:
        return "0"
    elif delta > 0:
        return f"+{delta:,}"
    else:
        return f"{delta:,}"


class ColoredTextTestResult(unittest.TextTestResult):
    """Custom test result class with colored output and per-test status."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.test_results = []
        self.test_stats = {}  # Store per-test statistics

    def addSubTest(self, test, subtest, err):
        """Called when a subtest finishes."""
        super().addSubTest(test, subtest, err)

        # Extract test name and JSON path from subTest params
        test_name = "unknown"
        json_path = None
        if hasattr(subtest, 'params'):
            if 'cfg' in subtest.params:
                cfg = subtest.params['cfg']
                test_name = os.path.splitext(os.path.basename(cfg))[0]
            if 'json_path' in subtest.params:
                json_path = subtest.params['json_path']

        if err is None:
            self.test_results.append((test_name, 'PASS', None))
            # Parse and store JSON statistics for passing tests
            if json_path and os.path.exists(json_path):
                self.test_stats[test_name] = parse_json_stats(json_path)
            if self.showAll:
                self.stream.writeln(f"  ✓ {test_name} ... PASS")
        else:
            exc_type = err[0].__name__ if err[0] else 'ERROR'
            status = 'FAIL' if 'Assertion' in exc_type else 'ERROR'
            self.test_results.append((test_name, status, err))
            if self.showAll:
                self.stream.writeln(f"  ✗ {test_name} ... {status}")

    def printSummary(self):
        """Print a summary of all test results."""
        self.stream.writeln("\n" + "="*70)
        self.stream.writeln("TEST SUMMARY")
        self.stream.writeln("="*70)

        passed = sum(1 for _, status, _ in self.test_results if status == 'PASS')
        failed = sum(1 for _, status, _ in self.test_results if status in ('FAIL', 'ERROR'))
        total = len(self.test_results)

        for name, status, _ in self.test_results:
            symbol = "✓" if status == 'PASS' else "✗"
            self.stream.writeln(f"  {symbol} {name:30} ... {status}")

        self.stream.writeln("-"*70)
        self.stream.writeln(f"Total: {total} tests  |  Passed: {passed}  |  Failed: {failed}")
        self.stream.writeln("="*70)

    def generate_summary_md(self, output_dir: str, elapsed_time: float = 0.0) -> str:
        """Generate a detailed summary.md file with test results and statistics.

        Args:
            output_dir: Directory to write summary.md
            elapsed_time: Total test execution time in seconds

        Returns:
            Path to the generated summary file
        """
        summary_path = os.path.join(output_dir, 'summary.md')
        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

        passed = sum(1 for _, status, _ in self.test_results if status == 'PASS')
        failed = sum(1 for _, status, _ in self.test_results if status in ('FAIL', 'ERROR'))
        total = len(self.test_results)

        # Load previous stats for comparison
        prev_stats = load_previous_stats(output_dir)

        # Calculate aggregate statistics
        total_bits = 0
        total_slots = 0
        total_bus_clashes = 0
        total_device_clashes = 0
        total_read_overlaps = 0
        total_handovers = 0
        aggregate_slot_types: Dict[str, int] = defaultdict(int)
        aggregate_directions: Dict[str, int] = defaultdict(int)

        for stats in self.test_stats.values():
            total_bits += stats.get('total_bit_positions', 0)
            total_slots += stats.get('total_slots', 0)
            total_bus_clashes += stats.get('bus_clashes', 0)
            total_device_clashes += stats.get('device_clashes', 0)
            total_read_overlaps += stats.get('read_overlaps', 0)
            total_handovers += stats.get('handover_count', 0)
            for slot_type, count in stats.get('slot_types', {}).items():
                aggregate_slot_types[slot_type] += count
            for direction, count in stats.get('direction_counts', {}).items():
                aggregate_directions[direction] += count

        # Get previous values for delta comparison
        prev_total = prev_stats.get('total_tests') if prev_stats else None
        prev_passed = prev_stats.get('passed') if prev_stats else None
        prev_bits = prev_stats.get('total_bits') if prev_stats else None
        prev_slots = prev_stats.get('total_slots') if prev_stats else None
        prev_handovers = prev_stats.get('total_handovers') if prev_stats else None
        prev_bus_clashes = prev_stats.get('bus_clashes') if prev_stats else None
        prev_device_clashes = prev_stats.get('device_clashes') if prev_stats else None
        prev_read_overlaps = prev_stats.get('read_overlaps') if prev_stats else None
        prev_slot_types = prev_stats.get('slot_types', {}) if prev_stats else {}
        prev_directions = prev_stats.get('directions', {}) if prev_stats else {}
        prev_elapsed = prev_stats.get('elapsed_time') if prev_stats else None

        # Format elapsed time as mm:ss.d
        def format_time(seconds: float) -> str:
            mins = int(seconds // 60)
            secs = seconds % 60
            return f"{mins}:{secs:04.1f}"

        # Format time delta (show difference in seconds)
        def format_time_delta(current: float, previous: Optional[float]) -> str:
            if previous is None:
                return ""
            delta = current - previous
            if abs(delta) < 0.1:
                return " (0.0s)"
            sign = "+" if delta > 0 else ""
            return f" ({sign}{delta:.1f}s)"

        with open(summary_path, 'w') as f:
            f.write(f"# SWI3S Visualizer Test Summary\n\n")
            f.write(f"**Generated:** {timestamp}\n\n")
            f.write(f"**Total Time:** {format_time(elapsed_time)}{format_time_delta(elapsed_time, prev_elapsed)}\n\n")

            # Show previous run timestamp if available
            if prev_stats:
                prev_time = prev_stats.get('timestamp', 'Unknown')
                f.write(f"**Comparing to:** {prev_time}\n\n")

            # Overall Results
            f.write(f"## Test Results\n\n")
            f.write(f"| Metric | Count | Delta |\n")
            f.write(f"|--------|-------|-------|\n")
            f.write(f"| Total Tests | {total} | {format_delta(total, prev_total)} |\n")
            f.write(f"| Passed | {passed} | {format_delta(passed, prev_passed)} |\n")
            f.write(f"| Failed | {failed} | {format_delta(failed, prev_total - prev_passed if prev_total and prev_passed else None)} |\n")
            f.write(f"| Pass Rate | {100*passed/total:.1f}% | |\n\n")

            # Aggregate Statistics
            f.write(f"## Aggregate Statistics (All Tests)\n\n")
            f.write(f"| Metric | Value | Delta |\n")
            f.write(f"|--------|-------|-------|\n")
            f.write(f"| Total Bit Positions | {total_bits:,} | {format_delta(total_bits, prev_bits)} |\n")
            f.write(f"| Total Slots | {total_slots:,} | {format_delta(total_slots, prev_slots)} |\n")
            f.write(f"| Total Handovers | {total_handovers:,} | {format_delta(total_handovers, prev_handovers)} |\n")
            f.write(f"| Bus Clashes | {total_bus_clashes} | {format_delta(total_bus_clashes, prev_bus_clashes)} |\n")
            f.write(f"| Device Clashes | {total_device_clashes} | {format_delta(total_device_clashes, prev_device_clashes)} |\n")
            f.write(f"| Read Overlaps | {total_read_overlaps} | {format_delta(total_read_overlaps, prev_read_overlaps)} |\n\n")

            # Slot Type Distribution
            f.write(f"## Slot Type Distribution\n\n")
            f.write(f"| Slot Type | Count | Percentage | Delta |\n")
            f.write(f"|-----------|-------|------------|-------|\n")
            for slot_type in sorted(aggregate_slot_types.keys()):
                count = aggregate_slot_types[slot_type]
                pct = 100 * count / total_slots if total_slots > 0 else 0
                prev_count = prev_slot_types.get(slot_type)
                f.write(f"| {slot_type} | {count:,} | {pct:.1f}% | {format_delta(count, prev_count)} |\n")
            f.write("\n")

            # Direction Distribution
            f.write(f"## Direction Distribution\n\n")
            f.write(f"| Direction | Count | Percentage | Delta |\n")
            f.write(f"|-----------|-------|------------|-------|\n")
            for direction in sorted(aggregate_directions.keys()):
                count = aggregate_directions[direction]
                pct = 100 * count / total_slots if total_slots > 0 else 0
                prev_count = prev_directions.get(direction)
                f.write(f"| {direction} | {count:,} | {pct:.1f}% | {format_delta(count, prev_count)} |\n")
            f.write("\n")

            # Individual Test Results
            f.write(f"## Individual Test Results\n\n")

            # Failed tests first
            failed_tests = [(name, status, err) for name, status, err in self.test_results
                           if status != 'PASS']
            if failed_tests:
                f.write(f"### Failed Tests\n\n")
                for name, status, err in failed_tests:
                    f.write(f"#### ❌ {name}\n\n")
                    if err:
                        f.write(f"```\n")
                        import traceback
                        f.write(''.join(traceback.format_exception(*err))[:500])
                        f.write(f"\n```\n\n")

            # Passed tests with stats
            f.write(f"### Passed Tests\n\n")
            f.write(f"| Test | Rows | Cols | Bits | Slots | Handovers | Bus Clashes | Device Clashes |\n")
            f.write(f"|------|------|------|------|-------|-----------|-------------|----------------|\n")

            for name, status, _ in self.test_results:
                if status == 'PASS':
                    stats = self.test_stats.get(name, {})
                    rows = stats.get('num_rows', '-')
                    cols = stats.get('num_columns', '-')
                    bits = stats.get('total_bit_positions', '-')
                    slots = stats.get('total_slots', '-')
                    handovers = stats.get('handover_count', '-')
                    bus_cl = stats.get('bus_clashes', '-')
                    dev_cl = stats.get('device_clashes', '-')
                    f.write(f"| {name} | {rows} | {cols} | {bits} | {slots} | {handovers} | {bus_cl} | {dev_cl} |\n")

            f.write("\n")

            # Tests with Clashes (debugging section)
            tests_with_clashes = [(name, self.test_stats.get(name, {}))
                                 for name, status, _ in self.test_results
                                 if status == 'PASS' and
                                 (self.test_stats.get(name, {}).get('bus_clashes', 0) > 0 or
                                  self.test_stats.get(name, {}).get('device_clashes', 0) > 0)]

            if tests_with_clashes:
                f.write(f"## Tests with Clashes (For Debugging)\n\n")
                for name, stats in tests_with_clashes:
                    f.write(f"### {name}\n\n")
                    f.write(f"- Bus clashes: {stats.get('bus_clashes', 0)}\n")
                    f.write(f"- Device clashes: {stats.get('device_clashes', 0)}\n")
                    clash_positions = stats.get('clash_positions', [])
                    if clash_positions:
                        num_cols = stats.get('num_columns', 32)
                        f.write(f"- Clash positions (row, col):\n")
                        for pos in clash_positions[:10]:  # Limit to first 10
                            row = pos // num_cols
                            col = pos % num_cols
                            f.write(f"  - ({row}, {col})\n")
                        if len(clash_positions) > 10:
                            f.write(f"  - ... and {len(clash_positions) - 10} more\n")
                    f.write("\n")

        # Save current stats for next run comparison
        current_stats = {
            'total_tests': total,
            'passed': passed,
            'total_bits': total_bits,
            'total_slots': total_slots,
            'total_handovers': total_handovers,
            'bus_clashes': total_bus_clashes,
            'device_clashes': total_device_clashes,
            'read_overlaps': total_read_overlaps,
            'slot_types': dict(aggregate_slot_types),
            'directions': dict(aggregate_directions),
            'elapsed_time': elapsed_time,
        }
        save_current_stats(output_dir, current_stats)

        return summary_path


class ColoredTextTestRunner(unittest.TextTestRunner):
    """Custom test runner using ColoredTextTestResult."""
    resultclass = ColoredTextTestResult

    def __init__(self, *args, output_dir: str = None, cleanup_temp: bool = True, **kwargs):
        super().__init__(*args, **kwargs)
        self.output_dir = output_dir
        self.cleanup_temp = cleanup_temp

    def run(self, test):
        start_time = time.time()
        result = super().run(test)
        elapsed_time = time.time() - start_time
        if isinstance(result, ColoredTextTestResult):
            result.printSummary()
            # Generate summary.md if output directory is set
            if self.output_dir and os.path.exists(self.output_dir):
                summary_path = result.generate_summary_md(self.output_dir, elapsed_time)
                self.stream.writeln(f"\nSummary written to: {summary_path}")
        return result


class CombinedRegressionTest(unittest.TestCase):
    """Combined regression and round-trip test - single pass per config.

    For each CSV configuration:
    1. Run visualizer ONCE: load CSV, save temp CSV, save temp JSON
    2. Compare temp JSON with reference JSON
    3. Verify temp CSV round-trips correctly (load temp CSV, compare JSON)
    """

    @classmethod
    def setUpClass(cls):
        cur_dir = os.path.abspath(os.path.dirname(__file__))
        cls.cfg_dir = os.path.normpath(os.path.join(cur_dir, "../examples"))
        cls.ref_dir = os.path.join(cur_dir, "test_json_outputs")
        cls.out_dir = os.path.join(cur_dir, "temp")
        cls.script = os.path.normpath(os.path.join(cur_dir, '../swi3s_visualizer.py'))

        # Create/clear output directory
        if os.path.exists(cls.out_dir):
            for item in os.listdir(cls.out_dir):
                item_path = os.path.join(cls.out_dir, item)
                if os.path.isfile(item_path):
                    os.remove(item_path)
                elif os.path.isdir(item_path):
                    shutil.rmtree(item_path)
        else:
            os.makedirs(cls.out_dir)

        # Find all CSV config files
        cls.cfg_list = []
        for root, dirs, files in os.walk(cls.cfg_dir):
            for file in files:
                if file.endswith('.csv'):
                    cls.cfg_list.append(os.path.join(root, file))

        print(f"\nTest Output Directory: {cls.out_dir}")
        print(f"Found {len(cls.cfg_list)} test configurations\n")

    def run_single_test(self, cfg) -> str:
        """Run test for a single configuration - compare JSON output with reference.

        Returns:
            Path to the output JSON file (for statistics collection)
        """

        # Compute paths
        cfg_relpath = os.path.relpath(cfg, self.cfg_dir)
        cfg_basename, _ = os.path.splitext(cfg_relpath)
        safe_basename = cfg_basename.replace(' ', '_').replace('/', '_')

        json_relpath = cfg_basename + '.json'
        ref_json = os.path.join(self.ref_dir, json_relpath)

        # Output paths
        out_json = os.path.join(self.out_dir, safe_basename + '.json')
        out_csv = os.path.join(self.out_dir, safe_basename + '_roundtrip.csv')

        # Ensure output subdirectory exists
        os.makedirs(os.path.dirname(out_json) if os.path.dirname(out_json) else self.out_dir, exist_ok=True)

        test_name = os.path.basename(cfg_basename)

        # === STEP 1: Run batch processor ===
        cmd = f'python3 "{self.script}" -c "{cfg}" -s "{out_csv}" -o "{out_json}"'
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True)

        # swi3s_visualizer.py returns: 0=success, 1=error, 2=bus clashes detected
        # Exit code 2 is valid (bus clashes are recorded in JSON, which we compare)
        if result.returncode == 1:
            self.fail(f"Batch processor failed:\nstderr: {result.stderr}\nstdout: {result.stdout}")

        # === STEP 2: Check for missing fields in CSV (fail test if any) ===
        if "expected field(s) missing from CSV" in result.stderr:
            self.fail(f"CSV has missing fields:\n{result.stderr}")

        # === STEP 3: Compare JSON output with reference ===
        self.assertTrue(os.path.exists(out_json), f"Output JSON not created: {out_json}")
        self.assertTrue(os.path.exists(ref_json), f"Reference JSON missing: {ref_json}")
        self.assertTrue(os.path.exists(out_csv), f"Output CSV not created: {out_csv}")

        # Compare JSON with reference (includes bus_clashes, device_clashes, read_overlaps)
        cmp_result = subprocess.run(['cmp', '-s', ref_json, out_json])
        self.assertEqual(cmp_result.returncode, 0,
            f"JSON mismatch for {test_name}.\nReference: {ref_json}\nOutput: {out_json}")

        # === STEP 4: CSV round-trip test (load saved CSV, verify same JSON) ===
        roundtrip_json = os.path.join(self.out_dir, safe_basename + '_roundtrip.json')
        cmd2 = f'python3 "{self.script}" -c "{out_csv}" -o "{roundtrip_json}"'
        result2 = subprocess.run(cmd2, shell=True, capture_output=True, text=True)

        if result2.returncode == 1:
            self.fail(f"Round-trip CSV load failed:\nstderr: {result2.stderr}")

        # Check for missing fields in round-trip CSV too
        if "expected field(s) missing from CSV" in result2.stderr:
            self.fail(f"Round-trip CSV has missing fields:\n{result2.stderr}")

        # Compare round-trip JSON with original output
        cmp_result2 = subprocess.run(['cmp', '-s', out_json, roundtrip_json])
        self.assertEqual(cmp_result2.returncode, 0,
            f"Round-trip JSON mismatch for {test_name}.\nOriginal: {out_json}\nRound-trip: {roundtrip_json}")

        return out_json

    def test_all_configs(self):
        """Run tests for all configurations."""
        for cfg in self.cfg_list:
            # Compute json_path to pass to subTest for statistics collection
            cfg_relpath = os.path.relpath(cfg, self.cfg_dir)
            cfg_basename, _ = os.path.splitext(cfg_relpath)
            safe_basename = cfg_basename.replace(' ', '_').replace('/', '_')
            json_path = os.path.join(self.out_dir, safe_basename + '.json')

            with self.subTest(cfg=cfg, json_path=json_path):
                self.run_single_test(cfg)


def regenerate_reference_files():
    """Regenerate all reference JSON files using the batch processor."""
    cur_dir = os.path.abspath(os.path.dirname(__file__))
    cfg_dir = os.path.normpath(os.path.join(cur_dir, "../examples"))
    ref_dir = os.path.join(cur_dir, "test_json_outputs")
    script = os.path.normpath(os.path.join(cur_dir, '../swi3s_visualizer.py'))

    # Find all CSV config files
    cfg_list = []
    for root, dirs, files in os.walk(cfg_dir):
        for file in files:
            if file.endswith('.csv'):
                cfg_list.append(os.path.join(root, file))

    print(f"Regenerating {len(cfg_list)} reference JSON files...")
    print(f"Output directory: {ref_dir}\n")

    success_count = 0
    fail_count = 0

    for cfg in cfg_list:
        # Compute paths
        cfg_relpath = os.path.relpath(cfg, cfg_dir)
        cfg_basename, _ = os.path.splitext(cfg_relpath)
        json_relpath = cfg_basename + '.json'
        ref_json = os.path.join(ref_dir, json_relpath)

        # Ensure output subdirectory exists
        ref_json_dir = os.path.dirname(ref_json)
        if ref_json_dir:
            os.makedirs(ref_json_dir, exist_ok=True)

        # Run batch processor
        cmd = f'python3 "{script}" -c "{cfg}" -o "{ref_json}"'
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True)

        test_name = os.path.basename(cfg_basename)
        if result.returncode in (0, 2):  # 0=success, 2=bus clashes (still valid output)
            print(f"  ✓ {test_name}")
            success_count += 1
        else:
            print(f"  ✗ {test_name}: {result.stderr.strip()}")
            fail_count += 1

    print(f"\n{'='*60}")
    print(f"Regeneration complete: {success_count} succeeded, {fail_count} failed")
    print(f"{'='*60}")

    return fail_count == 0


if __name__ == '__main__':
    if '--regenerate' in sys.argv:
        success = regenerate_reference_files()
        sys.exit(0 if success else 1)
    else:
        # Get directory paths
        cur_dir = os.path.abspath(os.path.dirname(__file__))
        temp_dir = os.path.join(cur_dir, "temp")

        # Run tests with summary generation (summary goes to tests/ directory)
        runner = ColoredTextTestRunner(verbosity=2, output_dir=cur_dir)
        loader = unittest.TestLoader()
        suite = loader.loadTestsFromTestCase(CombinedRegressionTest)
        result = runner.run(suite)

        # Clean up temp directory completely
        if os.path.exists(temp_dir):
            shutil.rmtree(temp_dir)
            print(f"\nTemp directory cleaned up.")

        print(f"Summary written to: {os.path.join(cur_dir, 'summary.md')}")

        sys.exit(0 if result.wasSuccessful() else 1)
