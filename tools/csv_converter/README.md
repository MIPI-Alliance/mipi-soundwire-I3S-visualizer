# v1.73 / v1.74 → SWI3S Studio CSV converter

A standalone CLI that reads CSV configurations saved by the legacy SWI3S
Visualizer v1.73 or v1.74 and writes them in the current format used by SWI3S Studio.

The output is produced by the current `CSVHandler.save_csv`, so a
successful conversion always yields a file the current app can load
without format errors. The target version is read from
`swi3s_studio.swviz.version.app_version()` (the package `__version__`), so the converter stays aligned
with the rest of the repo on version bumps.

## Usage

```bash
# Single file — writes <stem>_converted.csv alongside the input
python3 tools/csv_converter/convert_174.py path/to/old.csv

# Directory — converts every *.csv; output goes to <dir>_converted/
# with each file also suffixed _converted.csv
python3 tools/csv_converter/convert_174.py path/to/old_configs/

# Explicit output location
python3 tools/csv_converter/convert_174.py path/to/old.csv -o new.csv
python3 tools/csv_converter/convert_174.py old_configs/ -o new_configs/

# Verbose: print each file path as it's converted
python3 tools/csv_converter/convert_174.py old_configs/ -v
```

## What the converter does

- Writes `AppVersion,<current>` at the top of every output file.
- Sets the `Description` field to `<source filename stem> — converted from
  SWI3S legacy CSV to v<current>` so the per-file origin is obvious.
- Applies the field mapping below: some names change, some values are
  excess-1 encoded, some are inverted, and some are lost because the new
  format represents them differently.
- Unknown v1.74 fields (anything not in the mapping or the silent-skip
  list) are warned to stderr and skipped. Output is still produced.
- A value in the scalar or Channels/Sample fields listed under "Values changed to fit" is
  changed to fit and warned to stderr. Per-port register values (Offset, Horizontal Start,
  Horizontal Count, Tail Width, Bit Width, Channel Grouping, Channel Group Spacing, Interval
  Numerator, Device Number, and a Sample Width, Sample Grouping or Interval above its field) are
  copied as read, with no warning; the application range-checks them when the file is opened.
- A file that cannot be converted (unreadable, not UTF-8 text, over the
  config size limit, a cell that is not a number, or a `Columns per Row`, `S0 Width`,
  `CDS Tail Width` or `Skipping Denominator` the format cannot hold) is named on stderr as
  `error: <file>: <reason>`. In directory mode the remaining files are
  still converted. The exit status is 1 when any file failed.
- An input with no field at all converts to the default config with a warning on stderr.
- When a directory is converted, a `.csv` extension in any case (`.CSV`) is picked up.

## Field mapping

### Interface (scalar) fields

| v1.74 field | New field | Transform |
|---|---|---|
| `Columns per Row` | `NumColumns_REG` | subtract 1 (excess-1) |
| `S0 S1 Enabled` | `PHY3Enabled` | direct |
| `S0 Width` | `S0Width` | direct |
| `CDS Guard Enabled` | `CDS_GuardEnabledPerSource` | written to every CDS source |
| `CDS Tail Width` | `CDS_TailWidthPerSource` | written to every CDS source |
| `Skipping Denominator` (or `Interval Denominator`; the first wins) | `SkippingDenominator_REG` | direct |
| `CDS Guard Polarity` | `CDS_GuardPolarityPerSource` | written to every CDS source with its guard, when present; kept only while the guard is enabled |
| `CDS/S0 Handover Width` | `EnforceCDSHandover` + `S1TailWidth_REG` | split: `EnforceCDSHandover = (width >= 1)`; `S1TailWidth_REG = 0` if width≤1, `1` if width=2, `2` if width≥3 |
| `Draw S0 Handover` | `EnforceS1Handover` | direct |
| `Row Rate [kHz]` | `RowRate` | direct, clamped to 1–48000 |
| `Rows to Draw` | `RowsToDraw` | direct (to VizConfig), clamped to 1–10240 |

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
| `Data Port Channels` | `EnableCh_REG` | `N → (1 << N) - 1`, N capped at 16; with excess-one flag, count is `N+1` |
| `Data Port Channel Grouping` | `ChannelGrouping_REG` | direct |
| `Data Port Channel Group Spacing` | `Spacing_REG` | direct |
| `Data Port Sample Width` | `SampleSize_REG` | subtract 1 (excess-1); direct when excess-one flag set |
| `Data Port Sample Grouping` | `SampleGrouping_REG` | subtract 1 (excess-1); direct when excess-one flag set |
| `Data Port Interval Integer` (v1.73: `Data Port Interval`) | `Interval_REG` | subtract 1 (excess-1); direct when excess-one flag set |
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

## Values changed to fit

Each change below is warned to stderr, naming the field and the data port.

- `Row Rate [kHz]` outside 1–48000 is clamped to that range.
- `Rows to Draw` outside 1–10240 is clamped to that range.
- `CDS Guard Polarity` true while `CDS Guard Enabled` is false is read as false (Guard 0).
- `CDS/S0 Handover Width` outside 0–3 is read as the nearest width: a handover with an S1 tail
  width of 2 above 3, none below 0.
- `Data Port Channels` above 16 enables 16 channels; below 1 enables none.
- `Data Port Sample Width`, `Data Port Sample Grouping` and
  `Data Port Interval Integer` below 1 (default file) are read as 1.
- A flag that is not `true`/`false`, `1`/`0` or `yes`/`no` (any case) is
  read as false.

## Defaults for fields with no v1.74 source

New fields that didn't exist in v1.74 are written with the defaults below.
Review them after conversion if you need non-default behavior.

**Interface:**
- `CDS_BitWidth_REG = 0`
- `CDS_GuardPolarityPerSource` = Guard 0 when the file has no `CDS Guard Polarity`
- `S1TailWidth_REG = 0` when the file has no `CDS/S0 Handover Width`
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
- `DataPortNumber` = column index (0-11). v1.74 has no logical DP-number
  concept, so each port defaults to its slot index; edit per-port in
  SWI3S Studio (click the DP name chip) to assign a 0-31 number.

## Legacy encoding flag

- `Save file using excess one` controls the on-disk encoding of four DP
  fields (see above). The converter reads it but does not warn; it's a
  normal part of v1.74 output.

## Limitations

- The `Description` field is overwritten with the conversion note. If the
  original file had a description, copy it over manually post-conversion.
