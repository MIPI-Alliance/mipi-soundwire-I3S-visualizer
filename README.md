# SWI3S Visualizer v2.0

MIPI SoundWire I3S Frame Visualizer - A tool for visualizing and validating SoundWire I3S frame configurations.

## Overview

This application provides both a graphical user interface (GUI) and command-line batch mode for:

- Visualizing SoundWire I3S frame layouts
- Configuring interface parameters (columns, PHY settings, CDS/S0/S1 timing)
- Configuring up to 12 data ports with PCM or PDM streams
- Supporting Flow Control modes with TxP and DRQ bits
- Detecting bus clashes and read/write conflicts
- Exporting frame models to JSON for further analysis
- Loading and saving configurations via CSV files

## Requirements

- **Python 3.13+** (recommended for macOS - required for GUI to work properly)
- **Python 3.10+** (minimum for all platforms - uses `int.bit_count()`)

### macOS Installation

Python 3.11/3.12 on macOS has a known issue where button clicks don't work in the GUI. **Python 3.13+ is required.**

1. **Install Python 3.13+** from [python.org](https://www.python.org/downloads/)
   - Download the macOS installer
   - Run the installer (this installs Python system-wide)

2. **Verify installation:**
   ```bash
   python3.13 --version
   ```

3. **Setup the app** (one time, from the app folder):
   ```bash
   python3.13 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```

4. **Run the app:**
   ```bash
   source .venv/bin/activate
   python swi3s_visualizer.py
   ```

### Linux/Windows Installation

```bash
python3 -m venv .venv
source .venv/bin/activate   # On Windows: .venv\Scripts\activate
pip install -r requirements.txt
python swi3s_visualizer.py
```

### Dependencies

The app requires these Python packages (installed via requirements.txt):
- **customtkinter** - Modern UI widgets
- **canvasvg** - SVG export support

## Quick Start

### GUI Mode

After completing the installation above:

```bash
source .venv/bin/activate
python swi3s_visualizer.py
```

This opens the graphical interface where you can:
- Adjust interface parameters in the top panel
- Configure data ports in the middle panel
- View the frame visualization in the canvas area
- Save/load configurations via File menu

### Batch Mode (Headless)

```bash
source .venv/bin/activate

# Generate JSON frame model from CSV configuration
python swi3s_visualizer.py -c config.csv -o output.json

# With verbose logging
python swi3s_visualizer.py -c config.csv -o output.json -v
```

## Command Line Options

| Option | Description |
|--------|-------------|
| `-h, --help` | Show help message |
| `-c FILE, --config FILE` | Load configuration from CSV file |
| `-o FILE, --output FILE` | Output frame model to JSON file (implies headless mode) |
| `-s FILE, --save-csv FILE` | Save configuration to CSV file |
| `-r N, --rows N` | Number of rows to draw (0 = use CSV value) |
| `-t MODE, --theme MODE` | UI theme: light, dark, or system |
| `-v, --verbose` | Enable verbose debug logging |

## Configuration Files

### CSV Format

Configuration files use CSV format with interface parameters followed by data port parameters. Example structure:

```csv
Save file using excess one,True
NumColumns_REG,15
SkippingDenominator_REG,16
PHY3Enabled,True
S0Width,1
...
Name,DP0,DP1,DP2,...,DP11
DeviceNumber_REG,0,0,0,...
EnableCh_REG,0b11111,0b11,0b0,...
FlowMode_REG,0,1,2,...
...
```

See `examples/` for example configurations.

### JSON Output

The JSON frame model output contains:
- Frame dimensions and timing parameters
- Per-slot ownership and state information
- TxP and DRQ bit locations (for Flow Control modes)
- Clash detection results
- Data port configuration summary

## Project Structure

```
mipi-soundwire-I3S-visualizer/
├── swi3s_visualizer.py    # Main entry point (CLI + GUI launcher)
├── src/
│   ├── config/            # Constants and configuration
│   │   └── constants.py   # SpecialDevices, CSVFields, ranges
│   ├── core/              # Core engine (BusModelBuilder)
│   │   └── engine.py      # Builds BusModel from configuration
│   ├── drawing/           # Canvas rendering and clash detection
│   │   ├── canvas_renderer.py
│   │   └── clash_detector.py
│   ├── io/                # CSV and JSON handlers
│   │   ├── csv_handler.py
│   │   └── json_handler.py
│   ├── models/            # Data models (Interface, DataPort, BusModel)
│   │   ├── dataport.py    # DataPort state machine
│   │   ├── interface.py   # Top-level configuration
│   │   ├── bus_model.py   # Sequential bit representation
│   │   ├── device.py      # Device abstraction
│   │   └── enums.py       # SlotType, DirectionType, FlowMode
│   ├── ui/                # UI components
│   │   ├── minimal_app.py # Main application window
│   │   ├── frame_renderer.py
│   │   ├── parameter_panel.py
│   │   ├── error_panel.py
│   │   └── dialogs/       # Modal dialogs
│   ├── utils/             # Validators, logging, platform utilities
│   │   └── validators.py
│   └── viz/               # Visualization configuration
│       └── dataport_viz.py
├── examples/              # Example CSV configurations (also used as tests)
│   ├── directed_tests/    # Targeted feature tests
│   ├── spec_figures/      # Configurations from spec figures
│   └── use_cases/         # Real-world use case examples
├── test/
│   ├── testsuite.py       # Test runner with summary generation
│   ├── summary.md         # Test statistics (generated after each run)
│   └── test_json_outputs/ # Expected JSON outputs for tests
├── docs/                  # Additional documentation
├── requirements.txt       # Python dependencies
├── architecture.md        # Detailed architecture documentation
├── LICENSE.md             # BSD 3-Clause License
└── README.md              # This file
```

## Running Tests

```bash
source .venv/bin/activate
cd test
rm -rf temp && python testsuite.py
```

The test suite:
1. Runs each CSV configuration through the visualizer in batch mode
2. Compares generated JSON output against reference files
3. Generates a detailed `summary.md` with test statistics
4. Cleans up temporary files after completion

To regenerate all reference files after intentional changes:

```bash
source .venv/bin/activate
cd test
python testsuite.py --regenerate
```

## Key Concepts

### Interface Parameters

| Parameter | Description |
|-----------|-------------|
| `NumColumns_REG` | Number of columns per row (excess-1 encoded) |
| `PHY3Enabled` | Enable S0/S1 PHY layer bits |
| `SkippingDenominator_REG` | Frame skipping denominator |
| `CDS_BitWidth_REG` | Control Data Stream bit width |
| `S0Width` | S0 synchronization width |
| `S1TailWidth_REG` | S1 tail width |

### Data Port Parameters

| Parameter | Description |
|-----------|-------------|
| `EnableCh_REG` | Channel enable bitmap (e.g., 0b11111 for 5 channels) |
| `SampleSize_REG` | Sample width in bits (excess-1 encoded) |
| `SampleGrouping_REG` | Samples grouped per transport (excess-1 encoded) |
| `ChannelGrouping_REG` | Channels grouped before spacing |
| `Spacing_REG` | Slots between channel groups |
| `Interval_REG` | Rows per transport pattern (excess-1 encoded) |
| `Offset_REG` | Row offset for first transport |
| `HorizontalStart_REG` | Starting column |
| `HorizontalCount_REG` | Columns owned per row |
| `PortDirection_REG` | True = Sink (read), False = Source (write) |
| `SubRowInterval_REG` | Enable sub-row interval mode |
| `FlowMode_REG` | Flow control mode |

### Flow Control Modes

| Mode | Value | Description |
|------|-------|-------------|
| Normal | 0 | Standard data transfer without flow control bits |
| Tx Controlled | 1 | Source sends TxPresent bit per channel indicating valid data |
| Rx Controlled | 2 | Sink sends DRQ bit indicating readiness to receive |
| Asynchronous | 3 | Both TxP and DRQ bits for bidirectional flow control |

### Clash Detection

The visualizer detects several types of issues:

| Issue Type | Severity | Description |
|------------|----------|-------------|
| Bus Clashes | Critical | Multiple sources writing to the same bit slot |
| Device Clashes | Warning | Same device writing to same slot from different DPs |
| Read Overlaps | Info | Multiple sinks reading the same bit slot |
| TxP Mismatches | Warning | TxP source bits without matching sink bits |
| DRQ Mismatches | Warning | DRQ source/sink validation errors |
| Scrambler Mismatches | Warning | Source and sink have different scrambler settings |
| Test Mode Mismatches | Warning | Different test modes at same bit position |
| Interval Overflow | Warning | Data port bits don't fit in configured interval |
| Display Truncation | Info | Data port interval extends beyond displayed rows |

## Architecture

See [architecture.md](architecture.md) for detailed documentation of the codebase structure, including:

- Module organization and dependencies
- Data flow from CSV to rendered frame
- DataPort state machine implementation
- Performance optimizations
- Key design patterns

## License

BSD 3-Clause License. See LICENSE.md for details.

Copyright (c) 2020-2026, MIPI Alliance and other contributors.
