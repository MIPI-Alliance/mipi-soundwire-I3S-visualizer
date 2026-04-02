# SWI3S Visualizer Test Suite

This directory contains the automated test suite for the MIPI SoundWire I3S Visualizer.

## Directory Structure

```
mipi-soundwire-I3S-visualizer/
├── examples/                     # Test CSV configuration files
│   ├── directed_tests/           # Targeted feature and regression tests
│   ├── spec_figures/             # Configurations matching spec figures
│   └── use_cases/                # Real-world use case examples
├── test/
│   ├── testsuite.py              # Main test runner
│   ├── README.md                 # This file
│   ├── summary.md                # Test statistics (generated after each run)
│   ├── previous_stats.json       # Stats from last run (for delta comparison)
│   └── test_json_outputs/        # Reference (golden) JSON outputs
│       ├── directed_tests/
│       ├── spec_figures/
│       └── use_cases/
```

**Note:** CSV test configurations are stored in `examples/` (outside `test/`) so they can also serve as documentation and user examples. The `test_json_outputs/` directory mirrors the `examples/` structure.

## Running Tests

From the `test/` directory:

```bash
cd test
rm -rf temp && python3 testsuite.py
```

### Regenerating Reference Files

To regenerate all reference JSON files (use after intentional behavior changes):

```bash
cd test
python3 testsuite.py --regenerate
```

### Test Output

After running tests, a detailed `summary.md` file is generated with:
- Total test execution time (with delta vs previous run)
- Test pass/fail counts and percentages
- Aggregate statistics (total bits, slots, handovers, clashes)
- Delta comparison with previous run for all metrics
- Slot type distribution across all tests
- Individual test metrics (rows, columns, bits, clashes)
- Debug information for tests with clashes

Statistics from each run are saved to `previous_stats.json` for comparison. This file is gitignored.

### Prerequisites

- Python 3.10+ (uses `int.bit_count()`)
- Python 3.13+ recommended for macOS (GUI compatibility)
- Dependencies: `pip install -r requirements.txt`

## Test Types

### 1. Regression Tests

Verify that the visualizer produces consistent JSON output:

1. Load a CSV configuration file from `examples/`
2. Run the visualizer in headless mode
3. Compare the generated JSON against a reference file in `test_json_outputs/`

### 2. CSV Round-Trip Tests

Verify CSV import/export consistency:

1. Load original CSV configuration
2. Export to a new CSV file
3. Load the exported CSV
4. Compare the resulting JSON against the reference

This ensures no data is lost during CSV save/load cycles.

## Test Categories

### Directed Tests (54 tests)

Targeted tests for specific features and edge cases:

| Test | Description |
|------|-------------|
| `no_dataports` | Frame with only system slots, no data ports |
| `pcm_1`, `pcm_2`, `pdm_1` | Basic PCM and PDM stream configurations |
| `sri_1`, `sri_sample_grouping` | Sub-row interval (SRI) mode |
| `clash_read_read`, `clash_logic_1` | Read overlap and write clash detection |
| `same_device_clash_logic_1` | Same-device priority rules |
| `handover_logic` | Handover placement and clash detection |
| `cds_guard_0/1` | CDS guard polarity (G0/G1) |
| `dp_guards`, `dp_tails` | Data port guard and tail bits |
| `dp_channels` | Channel configuration and EnableCh_REG |
| `dp_sample_size_*` | Sample size ranges (0-11, 12-23, 24-31) |
| `CDS_width_*_bit_slots` | CDS width configurations (2-8 columns) |
| `asynchronous_flow_control` | Async flow mode with TxP and DRQ bits |
| `tx/rx_synchronous_flow_control` | TX and RX controlled flow modes |
| `PDM_*_grouping*` | PDM with sample and channel grouping |
| `max_sample_size_max_channels_pcm` | Stress test with maximum counts |
| `scrambler` | Scrambler enable/disable |
| `test_mode_*` | Port test modes (ones, zeros, mismatch) |
| `skipping_numerator` | Frame skipping configuration |
| `guards_and_tails_overflow_row` | Guard/tail behavior at row boundaries |

### Spec Figures (29 tests)

Configurations that reproduce figures from the MIPI SoundWire I3S specification:
- Figures 5-13: Basic frame layouts
- Figures 137-160: Advanced configurations (DLV PHY, FBCSE PHY, tail bits)

### Use Cases (1+ tests)

Real-world configuration examples:
- `2_pcm_amplifiers_with_current_and_voltage_sense_and_2_pdm_mics`

## Adding New Tests

### 1. Create the CSV Configuration

Add a new `.csv` file to the appropriate subdirectory under `examples/`:

- `directed_tests/` - For feature tests and regression tests
- `spec_figures/` - For spec figure reproductions
- `use_cases/` - For real-world examples

### 2. Generate the Reference Output

Run the test suite - it will detect the missing reference and fail. Then generate it:

```bash
# Generate just your test
python3 ../swi3s_visualizer.py -c ../examples/directed_tests/my_test.csv \
    -o test_json_outputs/directed_tests/my_test.json
```

Or regenerate all references:

```bash
python3 testsuite.py --regenerate
```

### 3. Run the Test Suite

Verify your new test passes:

```bash
rm -rf temp && python3 testsuite.py
```

## Updating Reference Files

When intentionally changing visualizer behavior:

1. Run the test suite (tests will fail)
2. Review the differences to ensure they're expected
3. Regenerate only the affected reference files
4. Re-run tests to confirm they pass

**Warning:** Be careful with `--regenerate` - it updates ALL references. If behavior changed incorrectly, you'll lose the correct references. Prefer regenerating individual files when possible.

## Console Output

Example output:

```
test_all_configs (__main__.CombinedRegressionTest.test_all_configs) ...
  ✓ no_dataports                   ... PASS
  ✓ pdm_1                          ... PASS
  ✓ pcm_1                          ... PASS
  ✓ clash_logic_1                  ... PASS

======================================================================
TEST SUMMARY
======================================================================
  ✓ no_dataports                   ... PASS
  ✓ pdm_1                          ... PASS
----------------------------------------------------------------------
Total: 84 tests  |  Passed: 84  |  Failed: 0
======================================================================

Summary written to: .../test/summary.md
```

The generated `summary.md` includes delta comparisons:

```markdown
**Generated:** 2026-04-03 14:06:15

**Total Time:** 0:15.0 (-0.5s)

| Metric | Value | Delta |
|--------|-------|-------|
| Total Bit Positions | 11,488 | (-56) |
| Total Slots | 12,581 | (-56) |
```

## Troubleshooting

### Test fails with "file not found"

Ensure the reference JSON file exists in `test_json_outputs/` with the same subdirectory structure as the CSV in `examples/`.

### Test fails with JSON diff

Check the `summary.md` file for details. Common causes:
- Row count mismatch (check `RowsToDraw` in CSV)
- New warning types added
- Bit placement algorithm changes

### "No reference file" for new test

Generate the reference:
```bash
python3 ../swi3s_visualizer.py -c ../examples/directed_tests/new_test.csv \
    -o test_json_outputs/directed_tests/new_test.json
```
