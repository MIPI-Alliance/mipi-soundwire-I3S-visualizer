# v1.74 → current CSV converter

A standalone CLI that reads CSV configurations saved by the legacy SWI3S
visualizer v1.74 and writes them in the format used by the current app.

The output is produced by the current `CSVHandler.save_csv`, so a
successful conversion always yields a file the current app can load
without format errors. The target version is read from
`src.version.APP_VERSION`, so the converter stays aligned with the rest of
the repo on version bumps.

## Usage

```bash
# Single file — writes <stem>_converted.csv alongside the input
python3 csv_converter/convert_174.py path/to/old.csv

# Directory — converts every *.csv; output goes to <dir>_converted/
# with each file also suffixed _converted.csv
python3 csv_converter/convert_174.py path/to/old_configs/

# Explicit output location
python3 csv_converter/convert_174.py path/to/old.csv -o new.csv
python3 csv_converter/convert_174.py old_configs/ -o new_configs/

# Verbose: print each file path as it's converted
python3 csv_converter/convert_174.py old_configs/ -v
```

## What the converter does

- Writes `AppVersion,<current>` at the top of every output file.
- Sets the `Description` field to `<source filename stem> — converted from
  SWI3S v1.74 to v<current>` so the per-file origin is obvious.
- Applies the field mapping below: some names change, some values are
  excess-1 encoded, some are inverted, and some are lost because the new
  format represents them differently.
- Unknown v1.74 fields (anything not in the mapping or the silent-skip
  list) are warned to stderr and skipped. Output is still produced.

## Field mapping

### Interface (scalar) fields

| v1.74 field | New field | Transform |
|---|---|---|
| `Columns per Row` | `NumColumns_REG` | subtract 1 (excess-1) |
| `S0 S1 Enabled` | `PHY3Enabled` | direct |
| `S0 Width` | `S0Width` | direct |
| `CDS Guard Enabled` | `CDS_GuardEnabled_REG` | direct |
| `CDS Tail Width` | `CDS_TailWidth_REG` | direct |
| `Interval Denominator` | `SkippingDenominator_REG` | direct |
| `CDS/S0 Handover Width` | `EnforceCDSHandover` + `S1TailWidth_REG` | split: `EnforceCDSHandover = (width >= 1)`; `S1TailWidth_REG = 0` if width≤1, `1` if width=2, `2` if width≥3 |
| `Draw S0 Handover` | `EnforceS1Handover` | direct |
| `Row Rate [kHz]` | `RowRate` | direct |
| `Rows to Draw` | `RowsToDraw` | direct (to VizConfig) |

### Data Port (per-port) fields

Four fields — **Data Port Channels**, **Data Port Sample Width**, **Data Port
Sample Grouping**, **Data Port Interval Integer** — were written by v1.74 in
one of two on-disk encodings. If the file begins with
`Save file using excess one,True` the values are raw REGs and copy across
directly (for the three sample/interval fields) or with a `+1` before
bitmask expansion (for Channels). Otherwise the values are 1-based counts
and the converter subtracts 1 to get the REG. The mapping below lists the
default (`Save file using excess one,False`) case; files with the flag set
to True are handled automatically.

| v1.74 field | New field | Transform (default file) |
|---|---|---|
| `Data Port Name` | `Name` (viz) | direct |
| `Data Port Device Number` | device assignment | direct |
| `Data Port In Manager` | manager assignment | if True, override device with `MANAGER` |
| `Data Port Channels` | `EnableCh_REG` | `N → (1 << N) - 1`; with excess-one flag, count is `N+1` |
| `Data Port Channel Grouping` | `ChannelGrouping_REG` | direct |
| `Data Port Channel Group Spacing` | `Spacing_REG` | direct |
| `Data Port Sample Width` | `SampleSize_REG` | subtract 1 (excess-1); direct when excess-one flag set |
| `Data Port Sample Grouping` | `SampleGrouping_REG` | subtract 1 (excess-1); direct when excess-one flag set |
| `Data Port Interval Integer` | `Interval_REG` | subtract 1 (excess-1); direct when excess-one flag set |
| `Data Port Interval Numerator` | `SkippingNumerator_REG` | direct |
| `Data Port Offset` | `Offset_REG` | direct |
| `Data Port Horizontal Start` | `HorizontalStart_REG` | direct |
| `Data Port Horizontal Count` | `HorizontalCount_REG` | direct |
| `Data Port Tail Width` | `TailWidth_REG` | direct |
| `Data Port Bit Width` | `BitWidth_REG` | direct |
| `Source` | `PortDirection_REG` | **inverted** (v1.74 True = source; `PortDirection_REG` 0 = source) |
| `Data Port Guard Enabled` | `GuardEnable_REG` | direct |
| `Data Port Enabled` | `Enabled` (viz) | direct |
| `Draw Data Port Handover` | `EnableHandover` (viz) | direct |
| `Data Port DRI` | `SubRowInterval_REG` | direct |

## Defaults for fields with no v1.74 source

New fields that didn't exist in v1.74 are written with the defaults below.
Review them after conversion if you need non-default behavior.

**Interface:**
- `CDS_BitWidth_REG = 0`, `CDS_GuardPolarity_REG = False`
- `S1TailWidth_REG = 0`
- `Description` = conversion note (overwrites any legacy description)

**Data Port (per-port):**
- `FlowMode_REG = 0` (Normal)
- `PortMode_REG = 0` (Normal)
- `ScramblerEn_REG = False`
- `GuardPolarity_REG = False`

**Flow Control Port (per-port, all zero — only meaningful when `FlowMode_REG` > 1):**
- `FCP_HorizontalStart_REG = 0`, `FCP_BitWidth_REG = 0`
- `FCP_TailWidth_REG = 0`, `FCP_Offset_REG = 0`
- `FCP_GuardEnable_REG = False`, `FCP_GuardPolarity_REG = False`

**Viz (per-port):**
- `DisplayFields = "sc"` (sample + channel)

## Legacy encoding flag

- `Save file using excess one` controls the on-disk encoding of four DP
  fields (see above). The converter reads it but does not warn; it's a
  normal part of v1.74 output.

## Limitations

- The `Description` field is overwritten with the conversion note. If the
  original file had a description, copy it over manually post-conversion.
