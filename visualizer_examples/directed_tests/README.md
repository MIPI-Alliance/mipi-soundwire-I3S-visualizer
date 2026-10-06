# Directed tests

Targeted configurations for the Bus Visualizer, one feature or edge case each, from the
SWI3S Visualizer's own test suite. The test suite builds every one of them and compares the
result against a golden (`tests/test_visualizer_engine.py`, `tests/test_visualizer_placement.py`,
`tests/test_authoring_render_golden.py`).

## Configurations that report errors or warnings

Several exist to exercise the Visualizer's checks, so opening one shows issues in the
notifications list, and `swi3s-studio -c <file> -o <out>` exits 2 for a bus clash. For these
files that is the expected result, not a fault in the file or the tool. The lists below are
checked against what the engine reports (`tests/test_cli.py`).

**Bus clash** (an error: two devices drive the same slot; the command line exits 2):

- `CDS_split_guard_1.csv`, `CDS_split_guard_2.csv`, `CDS_split_guard_3.csv`
- `CDS_split_tail_1.csv`, `CDS_split_tail_2.csv`
- `clash_logic_1.csv` (also a same-device clash)
- `handover_logic.csv`
- `partial_channel_group_sample_grouping.csv`

**Placement rule errors** (the grid is drawn; the configuration breaks a placement rule):

- `guards_and_tails_overflow_row.csv`: a guard and a tail overflow the row; a horizontal
  span exceeds NumColumns
- `SRI_at_row_boundary.csv`, `sri_horizontal_end_at_row_boundary.csv`: a group or sample
  incomplete when HorizontalCount expires, and SRI rules
- `sri_sample_grouping.csv`, `sri_transport_truncated_at_horizontal_end.csv`: SRI column-count
  and spacing rules
- `PDM_SRI_with_channel_grouping.csv`, `PDM_SRI_with_sample_grouping_and_channel_grouping.csv`:
  SRI column-count rules

**Warnings only:**

- `same_device_clash_logic_1.csv`: one device drives a slot from two data ports
- `test_mode_mismatch.csv`: a port's test mode differs between source and sink
- `asynchronous_flow_control_mismatched_DRQ_TxP.csv`: DRQ and TxPresent sources without
  sinks, and sinks without sources

Every other file in this folder builds with no issue.
