# Vendored SWI3S Visualizer placement tests

These fixtures port the standalone **SWI3S Visualizer**'s test suite
(`mipi-soundwire-I3S-visualizer/test/`) into SWI3S Studio so the placement the
Studio shares with the Visualizer (the verified C++ cascade) is regression-tested
here, with no dependency on the sibling repo.

- `examples/` — the Visualizer's example configs, copied verbatim:
  - `directed_tests/` (58) — targeted feature/edge-case configs
  - `spec_figures/` (30) — configs reproducing MIPI SoundWire I3S spec figures
  - `use_cases/` (1) — a real-world example
- `golden/` — the Visualizer's **raw per-data-port placement** for each config,
  generated from its `test/model_dump.py` (which drives the Visualizer's own
  `DataPort` model). One `dp row col SLOT` line per owned slot; a `# rows N`
  header records the row count (the config's `RowsToDraw`).

`tests/test_visualizer_placement.py` solo-places every enabled data port through
`swi3score.grid_from_csv` and asserts the `(dp, row, col, slot)` set matches the
golden for all 89 configs. Column 0 (CDS) is excluded — Studio reserves it for the
control stream.

To regenerate the goldens after an intentional placement change, re-run
`model_dump.py` from the visualizer repo for each example at its `RowsToDraw`
(see the generator used in the commit that added these).
