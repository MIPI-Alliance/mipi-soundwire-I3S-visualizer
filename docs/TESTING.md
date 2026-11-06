# SWI3S Studio — Testing Approach

> Status: living document. Update it whenever the test strategy changes — it is the
> reference for **auditing** test coverage as the app grows. Every `tests/test_*.py` is named
> here: `tests/test_testing_doc.py` fails the build on a suite this document does not mention,
> or on one it names that no longer exists.

## 1. Scope

SWI3S Studio decodes a non-trivial wire protocol (NRZS → 8b/10b → command transport
→ register snoop → SSP-anchored audio placement → descramble) and renders it through
a stateful Qt UI bound by one shared cursor. This document describes how it is tested,
so that coverage can be **audited** rather than assumed.

The guiding rule: **every behaviour a user could rely on should be pinned by a test,
and every fixed bug should leave a regression test behind.** When in doubt, prefer a
small deterministic test over a manual check.

### 1.1 Strategy: what runs where, and when

A test is run where it can fail. Behaviour that is the same on every platform is checked
once per change on the development machine; behaviour that depends on the platform is
checked on each platform; everything runs everywhere before a tag.

**Every test carries a marker for what it protects** (`pyproject.toml`, `markers`):

| marker | the test depends on | examples |
|---|---|---|
| `core` | nothing platform-specific: decode, timing arithmetic, parsers, goldens, tools | the decode round trips, `test_timing_timeline.py`, `test_timing_fmax_rate.py` |
| `gui` | widgets: it builds a view, a dialog or the main window | `test_links_gui.py`, `test_menus.py` |
| `platform` | the OS, the filesystem, text encodings and line ends, subprocesses, the Qt style or DPI, the Python version | `test_leak_scan.py`, `test_platform_metrics.py`, `test_exit_teardown.py`, `test_publish_sync.py` |
| `perf` | wall-clock ceilings, run alone in their own process | `test_perf.py` |

A test file marks itself with a module-level `pytestmark`; a test may carry `gui` and
`platform` both. One with none of `gui`, `platform` or `perf` is `core` (`tests/conftest.py`
adds the marker, so `-m core` selects it), and `--strict-markers` (`pyproject.toml`) refuses a
marker not in this table.

**The stages, their scope and their budgets.** The gate prints each check's wall time and
warns when the commit gate or the guest gate runs over its budget, so growth is seen when it
happens:

| stage | where | runs | budget |
|---|---|---|---|
| dev loop: `tools/changed_tests.py --run` | development machine | the test files the change touches, in parallel | a 2 min target, not timed |
| commit gate: `tools/gate.py` | development machine | everything, each file in its own process, several at once | 10 min |
| guest gate: `tools/gate.py --guest` | each test guest, per batch | `gui` and `platform`, `_CORE_SMOKE`, perf, the leak scan and the screenshots | 20 min |
| release gate: `tools/gate.py --release` | every platform, before a tag | the commit gate everywhere; on the development machine also the single-process run and the native core's coverage; `tools/test_report.py` is run separately by hand before a tag (`python3 tools/test_report.py`) (a guest runs `--release --on-guest`) | none |
| CI | every push to the branches `ci.yml` names, and every pull request | the full matrix, as `ci.yml` declares | — |

The guest gate leaves out ruff and mypy, which read the same source on every platform.

`tools/gate.py` refuses, with exit 2, an unknown option, `--guest` together with `--release`,
`--quick` with either stage (it trims the commit gate only), and `--on-guest` without a stage
(`--release --on-guest` is the release gate on a guest, `--guest --on-guest` the guest scope
there). A test file that runs
longer than 30 minutes is killed with every process it started and counted as a failure.
Any other process a check runs that outlives two hours (`_STAGE_TIMEOUT`) is killed with every process it started, and its check is a named failure; the gate goes on to the next check.
`native-coverage` reports only the sources under `native/` and fails when the report lists
none. The exit codes of `tools/leak_scan.py` and `tools/name_review.py` are in
`docs/DEVELOPMENT.md`, "Pre-flight before publishing a release".
`tools/changed_tests.py` maps a changed module to the test files importing it, a changed
test file or helper under `tests/` to the test files importing it (directly or through
another test file), a native
change to every file using the core or the session, a changed file under `tools/` (a script,
its data or a launcher) or a `tests/` shell script to the files naming it, and a `tools/`
file that is not Python also to the files naming each `tools/` script that names it, any other changed file under `tests/` (a fixture, a golden file, a helper in a subdirectory) or `data/` to the files naming its file name or its path below `tests/`, and a changed file outside docs/ (example data, package data, workflow and top-level files) also to the files naming its file name or path, a path no test file names being printed as `not mapped`, and a `data/` file also to the files importing each `swi3s_studio` module that names it; a change to `tests/conftest.py`, `pyproject.toml` or a requirements file runs everything.
`tools/package_smoke.py` takes no options and `tools/mypy_gate.py` only `--update` and `--allow-incomparable`; anything else is refused with exit 2 before any work.

The release commit's own exception stands (DEVELOPMENT.md, release checklist step 3).

**Running in parallel.** The gate's suite check runs each test file in a process of its own,
one per CPU at a time (`SWI3S_TEST_WORKERS` overrides the count), which catches a teardown
crash and gives the suite's result in one run. The heaviest files (`_SPLIT` in
`tools/gate.py`) are each spread over several workers of their own (pytest-xdist,
`--dist load`): the run takes as long as its slowest file. A file in `_SPLIT` keeps its tests
independent of order. Run directly, `pytest -n auto --dist loadfile` keeps each file in one
worker. Every test gets its own settings file (`conftest.py`), which is what parallel
workers need. `perf` never runs in parallel. The single-process run, which catches state
leaking from one file to the next, runs only in the release gate, on the development machine:
a leak between files is a property of the tests, not of the platform.

**What a test costs.** Building the main window is the largest single cost in the suite.
A test builds it only when the behaviour crosses panes; otherwise it builds the view under
test alone. Building the window must stay cheap: nothing it builds recomputes what it
already has (a theme switch redraws the Timing Calculator's results rather than recomputing
them).

**What is measured.**
- **Time:** the gate prints each check's wall time, and each suite run writes every file's
  wall time and every test's setup + call + teardown to
  `build/test-durations/<platform>.txt`, or `<platform>-guest.txt` for `--guest`, so one scope's
  run never replaces the other's record; the gate orders the next run of the same scope by it,
  and skips a line that does not parse.
- **Coverage** (`pyproject.toml`, `[tool.coverage.run]`): lines and branches of
  `swi3s_studio` and `tools/`, and of the subprocesses a test starts (`patch = subprocess`).
  CI's floor is the measured figure less a margin. The release gate measures the native core
  from an instrumented build (`tools/native_coverage.py`, clang's `llvm-cov`), where those
  tools exist, and reports it in `build/native-coverage/report.txt` with no floor.
- **The test report** (`tools/test_report.py`, before a tag): the suite with one coverage
  context per test, written to `build/test-report/report.md` — coverage per subsystem, the
  files with the most lines unrun, each test's lines no other test runs, the tests one other
  test covers entirely, the lines only one test reaches, and the slowest files and tests.
  Line coverage cannot see test data, so a sweep over spec values reads as covering nothing
  new; read the redundancy beside the durations, not alone.

## 2. The two-layer system under test

```
   ┌────────────────────────────────────────────┐
   │  PySide6 UI  (swi3s_studio/ui/*)             │  ← headless GUI smoke test
   ├────────────────────────────────────────────┤
   │  Python app  (session, ingest, store,        │  ← Python unit/integration suites
   │  analysis, model, export)                     │
   ├────────────────────────────────────────────┤
   │  swi3score  (pybind11 module)                 │  ← Python end-to-end + C++ suite
   │  = the verified C++ decode core               │
   └────────────────────────────────────────────┘
```

The **C++ decode core is authoritative**. The decode classes live in
`native/swi3score/core/` and are maintained in this repository (so Studio builds
standalone), driven behind an `ISampleSource`/`Decoder` shim and exposed to Python via
pybind11 (`native/swi3score`).
Two consequences for testing:

1. Protocol correctness (8b/10b, CRC16, descrambler, placement, register packing) is
   tested **at the C++ layer** with spec vectors, and **again from Python** through the
   binding so we know the binding faithfully exposes it.
2. The Python app layer is tested against the core's output, not against a re-implemented
   oracle — so the app can't silently disagree with the decode.

> **Hard rule:** after editing anything under `native/` (including
> `native/swi3score/core/*`), you MUST rebuild the module before the Python
> tests mean anything:
> ```
> cd native && \
>   PYBIND11_INCLUDE="$(python3 -c 'import pybind11; print(pybind11.get_include())')" \
>   bash build_local.sh
> ```
> A stale `.so` tests the previous build.

## 3. Test layers

### 3.1 Native C++ primitive + round-trip suite
The primitive and round-trip tests of the C++ core (8b/10b, CRC16, descrambler, command
encode and parse, register snoop, column detection, per-port sample rate, WAV writer) belong
to a separate project that keeps its own copy of the decode classes. They are an external suite
and are not part of this tree. `tools/gate.py --only native-cpp` runs an external suite: set
`SWI3S_NATIVE_CPP_SUITE` to the suite's runner script (a bash script); without the variable
the check reports NOT RUN. In this tree the same rules are pinned through the binding
(`test_swi3score.py`, `test_symbols.py`, `test_native_*.py`), against spec-published vectors
where they exist (descrambler, 8b/10b), so they are not self-referential.

### 3.2 Python suites
Location: `tests/` in the checkout root, one concern per file, run by **pytest** (the only supported
entry point: see §6). Shared setup is `tests/conftest.py`: a short demo, the Qt
offscreen platform, the two-Link fixture, the guards against an unpatched modal and an
exception in a Qt slot, a throwaway settings file, and closing each test's windows (§8).
`tone_snr(a)` and `worst_snr(a, window)` (single-tone SNR of a decoded PCM stream, whole or per
window) live in `tests/conftest.py` and are imported with `from conftest import ...`.
For tests of the tools, `load_tool(name)` imports `tools/<name>.py` by path. A test that builds a
git repository, or runs git in a directory that is not one, requests the `isolated_git` fixture and builds the repository with `git_in` and
`commit_files`. Under `isolated_git` git reads none of the machine's configuration, and GIT_DIR,
GIT_WORK_TREE, GIT_INDEX_FILE and the other repository-locating variables are removed, and git looks for a repository no higher than pytest's base temporary directory (GIT_CEILING_DIRECTORIES). `git_in`
always runs under `isolated_git_env()`. `publish_release_repo(repo, tool, app)` builds the
up/rel/pub repository that the `tools/publish_tree.py` tests read.

They split into three bands:

**Core-through-binding (does the C++ reach Python intact?)**
- `test_swi3score.py` — decode the synthetic demo (config + commit + scrambled stereo)
  and assert the control phases, snooped geometry, and reconstructed audio sine. A DSCR that disables a port drops it; one
  that enables a port (`make_dscr_enable_levels`) decodes it from its first sample while the
  running port keeps its phase, and the windowed re-decode across the commit agrees; one that
  enables a channel on a running port keeps the port's phase, flags no unexpected SSP, and the
  channel sends its first sample in the interval that starts at the commit point. A core whose score_abi is older or newer than the app's fails with a rebuild message (`test_native_abi_guard`).
- `test_symbols.py` — 8b/10b symbol framing (comma → robust-token header → D-code packet;
  `max_symbols` honoured; monotonic samples).
- `test_seek_window.py` — windowed re-decode equals the full decode from the first
  re-acquired comma; symbol rows match command bus-rows on one continuous scale with a
  clean 10-row cadence.
- `test_cds_meaning.py` — per-symbol Command Transport role labels (PhaseID / Device Mask / Opcode / Address / CRC / responses) for Ping / WriteA32 / Commit. Both phases of the alternating idle pattern, 0x155 and 0x2AA, read "Idle Pattern" (`test_both_phases_of_the_alternating_idle_pattern_are_labelled_idle`). The phase names and the response phases are one table; a read setup and its data are labelled field by field; a deferred read is delivered by a zero-length read-data phase; a read setup answered with no data ends after the response; a ping labels twelve statuses and then idles. A Phase Header garbled in one token reads alike in the symbol pane and the core: the core reports a bad header with no packet, and the annotator labels the six header tokens, marks the last "(Bad header)" and labels no packet or response (`test_a_bad_header_reads_alike_in_the_symbol_pane_and_the_commands_table`). A hub-delayed Commit labels each device's response as the Commands table reads it when decode_symbols is given the hub depths (`test_a_hub_delayed_commit_reads_as_the_commands_table_reads_it`). Session.symbols / symbols_around / symbols_before pass the session's hub depths (`test_every_symbol_window_passes_the_session_hub_depths`). A Commit's packet reads Opcode, Group Mask, Row Delay, CRC_H, CRC_L in order with the parser's byte values, also for a byte whose codeword is also a Robust Token (`test_commit_fields_and_responses`); WriteA32 address and data bytes are the parser's (`test_write_packet_bytes_are_the_parsers`); the load-time symbols() window ends at the next segment (`test_the_load_time_symbol_window_ends_at_the_next_segment`). Each of the three symbol windows makes exactly one decode with the hub depths.
- `test_8b10b_disparity.py` — the 8b/10b symbol decoder over both running-disparity forms
  (RD−/RD+ coverage), the rejection of same-sign sub-blocks, and the encoder (all 256 bytes
  legal and round-tripping).

**App data path (ingest → store → analysis → export)**
- `test_capture_pipeline.py` — demo levels → `Capture` → write Saleae binary → read back →
  `TransitionSampleSource` → `Decoder`; asserts the capture-backed source decodes
  **identically** to the in-memory source, the binary reader/writer round-trips, and the
  Arrow command store preserves the decode.
- `test_ingest_formats.py` — synthesises a v0 `.sal`, a v3 `.sal`, and a digital CSV from
  the demo and checks each decodes to the same commands/columns; the real `.sal` test runs only when `SWI3S_REAL_SAL_DIR` names a directory of `.sal` files,
  otherwise it skips, and checks that such a file is handled rather than producing garbage; single- and multi-chunk v3
  both round-trip. Also pins **v4** (the v3 format under a new blob version): the v4 tests
  are parametrised over BOTH versions and assert they decode to the *same* transitions. The
  accepted set is exactly `{3, 4}`, and a v5 blob is refused (see the maintainers' .sal format
  notes). A v3 delta code too long for int64 is REFUSED: 128⁹ is exactly 2⁶³, so an
  11-digit code wraps to 1 and can satisfy the block chain's own sum check against a crafted
  `B_end`. The guard counts interior bytes on the raw block, so it covers the native decoder
  too.
- `test_digital_csv_bounded.py` — digital-CSV ingest is BOUNDED and BATCH-INVARIANT: read in
  batches, each batch's edges kept and its rows released, with the shared `memory_budget()`
  refusing, before reading, a file whose predicted peak (`digital_csv.predict_peak_bytes`, an
  upper bound) would not fit, and a batch size forcing many boundaries gives byte-identical output to one forcing
  none.
  Limits at their boundary (header line 65,536 characters, 1024 header cells, 1 Hz, 1e15 Hz) and the same-sample warning for a data line that alone merged and in the singular (the both-channels count is in `test_import_warnings_captures.py`). A clock column that is also the data column is refused by file (`test_a_clock_column_that_is_also_the_data_column_is_refused_by_file`). A data line is read no further than MAX_LINE_CHARS, so a line past it is refused without being held (`test_a_data_line_is_read_no_further_than_the_longest_line`). The bad-time-cell test compares with the same file without the bad row, and also asserts its import note for every batch size.
- `test_bus_model_compaction.py` — bit removal is deferred but exact.
  `remove_bits_matching` updates the position bucket at once and defers the rest to one
  `compact()`, which the builder calls when placement finishes. Pins that the bucket the
  clash logic reads is exact immediately, `bits` is exact after compaction, and a row read
  compacts for itself.
- `test_register_model.py` — spec load, address → register/field resolution, provenance,
  and a write → Commit → read stream whose Commit promotes NumColumns_NEXT into _CURR,
  checked against the apply_commands oracle; the core's read-only and RW1C tables equal `RegisterMap`'s, and the C++ and Python engines agree on a write to a read-only byte and an RW1C bit.
  A read of a _CURR alias updates _CURR only and a _NEXT read leaves _CURR alone; a data port with no commit-group write is in commit group zero. The replay's rules: an unconfirmed Commit promotes nothing, a read with a bad CRC changes nothing, and the `up_to_sample` boundary. A one-address range is a one-address span (`test_a_one_address_range_is_a_one_address_span`); an enum keeps single values in hex or b tokens and drops ranges (`test_an_enum_keeps_single_values_in_hex_or_b_tokens_and_drops_ranges`); a negative address is labelled by its 32-bit region (`test_a_negative_address_is_labelled_by_its_32_bit_region`); a register reset byte keeps its low eight bits (`test_a_register_reset_byte_keeps_its_low_eight_bits`); a bit range is high bit first within a byte (`test_a_bit_range_must_be_high_bit_first_within_a_byte`).
- `test_responses.py` — response token → name by phase; labelled `response_summary`,
  including the per-device **Ping** breakdown collapsed into device ranges
  (`0: PING_ATTACHED, 1-11: NO_RESPONSE`); token 6 is named per phase, and a read's returned bytes fill the Data column.
- `test_link_control.py` — §5.1.2 link bring-up detection from the raw edges
  (cold/warm start, the PHY-number clock burst, PhyStart), self-orienting when the
  bring-up rides the data line. And the Session overruling a detection the decode
  contradicts (CRC-valid commands inside it): every capture without a bring-up (the PHY1,
  PHY2 and flow-control demos without a cold start, a cold-start demo joined after audio
  began) has none: the edges alone read a bring-up into the PHY1 and PHY2 demos and the
  Session drops it with a note saying why, while the flow-control demo has none even from the
  edges; every genuine bring-up is
  kept; a phantom that named a PHY does not steer the decode, including one that steered it
  into no valid command at all; traffic before a Bus Reset does not count.
  A parameter with no bound is shown and not judged; every `LC_TIMING_LIMITS` entry includes both of its bounds; a clock-cycle count exactly on a limit passes; a limit with no maximum passes however long (`test_a_limit_with_no_maximum_has_none_to_exceed`); a clock row reports the cycle nearest a limit, the longer one on a tie (`test_a_clock_row_reports_the_cycle_nearest_a_limit`); the seven (min, max) pairs of `LC_TIMING_LIMITS` are the specified numbers (`test_the_manager_timing_limits_are_the_specified_numbers`). One out-of-range cycle fails its Man_tClock1 (high) or Man_tClock0 (low) row; the Warm Start limit is 482/483 us; the 150 and 500 us pulse boundaries. A capture ending right after the start pulse takes its falling edge as PhyStart, one ending on the next rising edge has none, and a lone edge is 'none' (`test_a_capture_that_ends_on_or_just_after_the_start_pulse_decodes_without_error`); a DP-low longer than 48 us after a falling edge is PhyStart and exactly 48 us still clocks a PHY-number bit (`test_a_dp_low_longer_than_48_us_after_a_falling_edge_is_phystart_and_exactly_48_is_not`).
- `test_bus_timing.py` — measured setup/hold/eye (`analysis/bus_timing`) on synthetic
  captures of known geometry: the margins match the offset, gating drops the
  no-transition (1-UI) pileup, and a tight margin flags as such. The eye arrays are asymmetric by clock polarity, data direction, FBCSE column and high/low duty; the 99/100 clock-edge and 15/16 data-edge floors; DLV columns and the end_sample boundary; the DLV UI estimate uses at most max_edges rows; `n_clock_edges` counts the clock edges of the region measured, not of the capture (`test_n_clock_edges_counts_the_region_not_the_capture`); DLV margins fall on the mid-UI grid and its columns (`test_dlv_margins_match_the_mid_ui_grid_and_columns`).
- `test_pdm.py` — PDM decode (`dsp.pdm_to_pcm` + `store.decode_pdm`): a sigma-delta
  tone recovers the right pitch, the 1-bit code is **bipolar** (`{0,1}→±1`, not 2's
  complement), and a DC-biased mic stream is DC-blocked yet keeps its tone; a channel whose sample_size changes mid-capture decodes each run at its own size (16-to-24-bit PCM, PDM then 24-bit PCM) and is MSB-aligned to the widest width. The minimum decimation is 2; a 2-bit stream is two's complement and plays back at its own scale; a native rate equal to the target is not decimated (`test_a_native_rate_equal_to_the_target_is_not_decimated`); a PCM channel with a PDM run plays at the decimated rate (`test_a_pcm_channel_with_a_pdm_run_plays_at_the_decimated_rate`).
- `test_measurements.py` — derived metrics (columns, SSP count, per-DP sample rate &
  bandwidth); each region's clock is the mean edge spacing, checked region by region.
  Bring-ups are numbered from one when the first lists nothing; PM actions list the first eight and count them all; regions list reconfigurations from two bus configs; link-control rows for every bring-up, and a single Ping reports no period. Exact Statistics rows: bandwidth, the SSP interval, counter versus flag fault text, a ping period that ignores same-row pings, the region row rate, and the limit order and fail colour. A DLV capture's Regions list its recovered clock at the full UI rate and FBCSE its clock at half, single-region and per region; a region with no measured UI lists 0 MHz and 0 kHz (`test_a_dlv_capture_lists_its_recovered_clock_at_the_ui_rate_and_fbcse_at_half`, `test_a_region_with_no_measured_ui_lists_zero_rates_rather_than_dividing`); the Safe-Lock row is in `test_link_control_lists_every_bring_up`. The per-device Read/Write breakdown covers device 11 and shows an error row only for a device that has errors (`test_the_per_device_write_breakdown_covers_device_11_and_counts_errors_per_device`).
- `test_analysis_filter.py` — error classification + command filter proxy.
- `test_compare.py` — the **`registers_from_csv` round-trip** (encode a CSV config to register writes, feed through
  `grid_from_registers`, assert the grid equals `grid_from_csv`); `_NEXT` staging in `grid_from_commands` (a write places only after a confirmed commit of its group); `force_columns`; `register_diff`; `register_diff_report` (the Compare dialog's text: the match sentence, the count summary, per-device grouping, and a `(+N more)` line only when registers are left past the cap, never `(+0 more)`; the report is capped at max_lines lines with one extra '(+N more)' line, and never ends on a device header with nothing under it, `test_register_diff_report_never_ends_on_a_device_header_with_nothing_under_it`); the bundled directed-test CSV places data ports (`test_grid_from_csv_places_data_ports_for_a_directed_example`); legacy
  visualizer-CSV (v1.73 / v1.74) → v2.0 conversion + placement equivalence, and the v2.0 → current upgrade.
- `test_audio_export.py` — `AudioStore` → per-(device,DP) WAV → read back → compare to the
  generated sine. The WAV is WAVE_FORMAT_EXTENSIBLE integer PCM, wValidBitsPerSample the
  port's sample size, samples left-justified, header fields and values read back through
  `tests/wav_reader.py` (shared helper).
  A heavy decimation keeps its ratio in bounded memory (24.576 MHz to 48 kHz is exactly 1/512 and peaks under 200 MB on 4M samples) and resampling in blocks does not change the samples; the header names no speaker positions (dwChannelMask 0); a WAV at the size limit is written and one byte over is refused; a sample rate above 2**32-1 Hz, or one whose byte rate (rate x channels x container bytes) overflows 32 bits, is refused by `export_wav` with a ValueError naming the stream and writes no file, in six cases including 2**31 mono 16-bit and 2**30 stereo 16-bit, while 2**31-1 mono 16-bit is written (`wav_header` raises the same); a PCM channel with no core rate measures it from the sample spacing, and a mixed 1-bit / PCM channel does not; in a mixed 16/24-bit export read back with wav_reader each channel is scaled by its own size, equal in level to playback, and clips to its own range (`test_a_narrower_channel_is_left_justified_by_its_own_size_in_a_mixed_export`). An export builds the whole file before it replaces the destination, so a failed export (before the header, after the header with the samples unwritten, at the final replace) leaves an existing WAV byte for byte and no temporary file (`test_a_failed_export_leaves_an_existing_wav_untouched_and_no_temporary_file`, marked `platform` with `test_an_export_keeps_the_mode_of_the_file_it_replaces` and `test_write_whole_writes_text_or_bytes_and_falls_back_when_the_folder_refuses`), and the replacement keeps the old file's mode; the shared writer `swi3s_studio/fsutil.write_whole` writes text or bytes and falls back to a direct write when the folder refuses a second file; `pcm_play` interleaves channels by value with a zero-filled tail (`test_pcm_play_interleaves_each_channel_by_value_and_zero_fills_the_tail`); the PDM decimation factor is the rounded ratio (`test_a_pdm_decimation_factor_is_the_rounded_ratio_of_the_bit_rate_to_the_pcm_rate`); Evenly Spaced positions round to the nearest capture sample (`test_evenly_spaced_runs_round_positions_to_the_nearest_capture_sample`); a step is a transport gap only past tolerance x the nominal step, 4x NORMAL and 32x flow (`test_a_transport_step_is_a_gap_only_past_the_tolerance_times_the_nominal_step`); a recovered rate is the median of three probes (`test_a_recovered_rate_is_the_median_of_three_probes_not_their_mean`); a PDM overshoot in a 32-bit mixed channel is held at full scale in a memmap-backed store (`test_a_pdm_overshoot_in_a_wide_mixed_channel_is_held_at_full_scale_in_a_memmap`); playback keeps a 16-bit stream bit-exact and left-justifies a 24-bit one as export_wav does (`test_playback_keeps_a_sixteen_bit_stream_bit_exact_and_left_justifies_the_rest`); `tests/wav_reader.py` refuses a data chunk that runs past the file, a data size that is not a whole number of frames, a missing pad or trailing bytes (`test_the_wav_reader_checks_the_data_chunk_against_what_the_file_holds`). `write_whole` refuses a read-only target, and writes a target that is not a regular file, `os.devnull` included, directly (`test_write_whole_writes_the_null_device_directly`, marked `platform`). `write_whole` refuses a read-only target without touching it (`test_write_whole_refuses_a_target_the_user_cannot_write_and_leaves_it_alone`), and writes in place a name too long for its temporary file (`test_write_whole_writes_a_name_too_long_for_its_temporary_file`).
- `test_audio_export_visible_range.py` — exporting the Audio pane's visible range writes the
  same capture span for every stream: the store maps a capture-sample window to each
  stream's own sample indices (`AudioStore.stream_index_range`), the export dialog maps its
  visible window per stream (`index_range(dev, dp)`) and refuses to map it without a stream,
  and `AudioView.visible_sample_range` reports the shared
  X range in capture samples.
- `test_audio_pyramid.py` — render-pyramid envelope equals raw samples when zoomed in;
  memmap-backed channel.
- `test_resample.py` — arbitrary-rate band-limited resampler (`dsp/resample`): identity,
  predicted output length, in-band tone preserved, out-of-band rejected, up/down-sample,
  unity DC gain, PDM→PCM decimation; a decimation past a denominator of 256 (24.576 MHz to 48 kHz is 512:1, 14.4 MHz to 48 kHz is 300:1) keeps its ratio (`test_a_decimation_beyond_the_denominator_bound_keeps_its_ratio`).
- `test_filters.py` — the audio high-pass (`dsp/filters`) against SciPy: the Butterworth
  design, the steady-state initial conditions, `sosfiltfilt` with its default padding and with
  the longer padding `highpass` uses, over corners from 1 Hz to just under Nyquist
  (references in `tests/fixtures/highpass_reference.json`, regenerated with SciPy by
  `python3 tests/test_filters.py --regenerate`); the -6 dB corner and 48 dB/octave stopband; a click
  left in place; clean stream ends; short streams; and the native `sosfilt` against the
  textbook recursion.
- `test_stream_processing.py` — per-stream DC blocker, high-pass and gain in `AudioStore`: the
  samples are the DC blocker, the filter then the gain, rounded and saturated at full scale;
  the DC blocker removes a drifting bias, leaves a 1 kHz tone untouched and is −6 dB at its
  1 Hz corner; every channel of the stream and no other; Revert returns the decoded samples
  exactly; the waveform, playback and export follow; the peak is measured after the DC
  blocker and the high-pass; memory-mapped stores; the saved form, an older one included.
- `test_stream_processing_gui.py` — the Filter & Gain dialog (opens normalised after a
  20 Hz high-pass, the DC blocker's checkbox and corner, amplification and new peak linked,
  the logarithmic corner slider, Allow clipping, Preview / Cancel / Apply) and the window:
  the stream menu and Revert, the marker and title, a re-decode, two Links, and a saved
  workspace.
- `test_audio_even_timing.py` — Evenly Spaced: offered only for an unevenly transported
  stream, respaced to its average rate within each run between gaps, As Transported restores
  the positions exactly; the Link keeps it through a re-decode and a saved workspace. A
  spectrogram lane follows it: the held (as-transported) spectrum shows the skip spurs, the
  evenly spaced one does not.
- `test_command_deltas.py` — the Commands ΔRows / ΔTime columns: hidden by default, counted
  from the row above as shown (a filter to Ping, which hides the writes between, changes
  them; a descending Time sort makes them negative), blank ΔRows between Links in All
  Links, kept by name across a scope switch, an older workspace leaves them hidden, and each
  sits after the column it counts. Also: All Links with a second decoded Link 3 µs later
  (ΔRows blank exactly at Link crossings, ΔTime on the shared timeline with offsets); the Δ
  columns are unsortable (header click, sortByColumn and a saved sort leave the sort
  unchanged); a restored view in capture order, or with a sort the table cannot apply, clears
  a sort made before it; a Δ cell on an error row has the row's error tooltip; a Δ column
  shown after a width restore, a Link switch or an All Links round trip has a width; widths
  are kept by column name.
- `test_spectrogram.py` — the spectrogram lane's spectrum (a tone at its frequency and
  level, within the window's scalloping between bins; an off-bin tone's leakage under
  −90 dB beyond 5 bins and no DC term in the bottom bins; the worst case, a tone half a bin
  off a bin centre, under −90 dB beyond 5 bins and −98 dB beyond 10; a full-scale tone's
  window skirt stays above the −120 dB colour floor out to about 70 bins and is below it
  beyond 80, for the tone the test uses, a third of a bin off a bin centre; a
  click lit from half a frame before it and no earlier, at every frame size; short, empty and silent streams) and the Audio
  pane: each channel waveform or spectrogram, its own frame size, the shared zoom, the menu,
  Vertical Zoom leaving a spectrogram alone.
- `test_maximize_pane.py` — View ▸ Maximize Pane (⌘⇧M), the key itself (no auto-repeat), the
  title-bar button and a double-click: the focused pane alone in the window (a tab behind
  another, the Commands table, a floating pane), and the arrangement restored exactly but for
  that pane staying in front of its tab group; in the Timing Calculator, the ⤢, the key and
  a double-click on the title hide the top bar (the inequalities or the term diagram take the page and the key restores them), the parameter sections still scrolled to; in
  the Bus Visualizer, the grid's ⤢ at its top right, the key and Maximize Frame collapse the
  parameters and put them back;
  Commands, Registers, Bus Grid, CDS and Samples keep the Timeline at its height through a resize of the window (a side-area pane is moved
  below it meanwhile), the others take the whole window, a closed Timeline stays closed, and
  no cap is left behind; the key pressed again and again keeps to the same pane, for every
  pane, and a title-bar button moves the focus to its pane's view, not a toolbar control; the
  Commands header's button and a double-click on its title; the Timeline is never maximized
  nor floated by a double-click; with nothing selected nothing changes; another pane shown,
  hidden from the View menu or closed by its ✕, a Link added or removed, Timing's jump to
  Capture, the Bus Analyzer left, All Links turned off or a workspace saved meanwhile all keep
  the arrangement to return to (a workspace saved after All Links was turned off reopens
  with the one-Link height); View ▸ Maximize Pane is shown in every mode; the Bus Visualizer's grid is also maximized by a
  splitter drag; both modes' maximized pages stay maximized across a mode switch; the key is in
  the shortcuts help. Maximizing and restoring any pane (every dock and the Commands table), floating and docking the CDS pane, and removing a Link while a pane is maximized leave the shared cursor at its sample, and Qt prints no message. A focus-in on a CDS or Commands table with no current row makes Qt pick a current row, and neither table reports it as a selection (`ui/quiet_focus.py`). The right-click menu on the menu bar or a title bar (createPopupMenu) lists only the View menu's pane items, not the docks' own toggles, and none outside the Bus Analyzer. Hiding the maximized pane there, or showing a hidden one, puts the panes back first. A focus-in on the Commands table with no current row leaves the shared cursor. The maximized pane cannot be dragged out (its floatable feature is off until the restore), and made floating it puts the panes back exactly (`test_the_maximized_pane_cannot_be_dragged_out_and_floated_it_puts_the_panes_back`). The deferred restore after a maximized pane is floated is a timer owned by that pane (`test_the_restore_after_a_floated_maximized_pane_belongs_to_the_pane`). The dock-layout timers belong to the window and the pane (`test_the_dock_layout_timers_belong_to_the_window_and_the_pane`).
- `test_audio_stems.py` — zoomed in to a few pixels a sample, every Audio waveform track is
  drawn as its samples (a stem from zero at each sample's capture position, a dot on its
  value), and zoomed out the line through them again; the switch follows the Audio pane's width (narrowed to 300 px) at
  one zoom; Vertical Zoom fits Y to the samples shown.
- `test_audio_zoom_kept.py` — every change that redraws the Audio pane's tracks (a spectrogram
  on or off, a frame size, a channel shown or hidden, colours, names, Filter & Gain) leaves
  the zoom where it was with the cursor outside the window, a cursor that moves out of it is
  still followed, and in a narrow pane a long title neither widens its track nor puts the
  tracks on different stretches.
- `test_dp_names.py` — a data port's name: kept in the source and reopened, and shown by the
  Audio, Samples and Capture panes, the Bus Grid key (widened to fit), the Audio menus, the
  Filter & Gain and Audio ▸ Export… dialogs and the Timing filter, whose checked items survive a
  rename; cancel and an empty name; a re-decode.
- `test_region_names.py` — a config region's name: kept in the source and the workspace,
  drawn on its timeline band, right-click on a band opening its menu rather than seeking,
  the zoom kept, a re-decode.
- `test_workspace_view.py` — the whole Analysis view comes back: each Link's Audio and Capture
  view survives a Link switch and a re-decode, and a new capture starts from the defaults;
  a two-Link window with everything changed (Commands filter, widths, hidden column and
  sort; both Links' Audio and Capture, a spectrogram channel included; Filter & Gain;
  bookmarks; the timeline's zoom;
  Samples, Registers, Statistics and Timing; the dock layout) is saved and reopened, and a
  snapshot of all of it must match (the Registers part covers the hidden-provenance filter, the expanded blocks and the column
  widths; the Registers device is covered by its own round trip on a multi-device capture); All Links'
  shared filter and lanes; and the window geometry handed back to Qt unchanged (Qt fits it
  to the screen, so the offscreen screen cannot hold a size). Also: a hidden column shown
  after reopening a workspace has a width (widths saved by name, shown columns only); a
  positional col_widths list is applied only when it has one entry per column, and a
  12-column list from an older layout is dropped.
- `test_workspace_paths.py` — captures named relative to the workspace (and where they
  were saved): a moved folder opens, a workspace moved alone finds them, an older
  absolute workspace opens, an analog CSV's channel names stay names, a lost `.bin` pair
  is found from one file; the window's Locate / Skip This Link / Cancel (skipping the first
  Link opens the others with their saved names; skipping every Link opens nothing and shows
  no error) and the `.swi3s` extension.
- `test_audio_source.py` — the playback `QIODevice` (`_PcmSource`): never short-reads
  (PCM then silence), the software fade-out ramp, and frame/byte math.
- `test_export.py` — command CSV (headless) with its read-data column and bus-grid SVG/PNG (offscreen Qt). Every leading formula character (= + - @ tab CR) in a name is quoted and a name without one is left alone; the commands CSV and the table show the same payload (the Data column rule is `analysis.responses.command_payload`); the commands CSV has device, CRC and register-rank columns; it labels a data-port register `DP.DP<n>.<name>` (`test_a_data_port_register_label_names_its_port`). A commands export that fails keeps the previous file (`test_a_failed_commands_export_keeps_the_previous_file`).
- `test_workspace.py` — workspace JSON round-trip + full GUI save → open restores
  bookmarks and cursor; the v3 → v4 migration (a v3 file loads as one Link at offset 0
  and re-saves as a true v4); a broken Link list is rejected; a file that is not UTF-8, not JSON or nested too deeply to parse
  raises a ValueError naming it. A saved what-if register
  overlay is re-applied on open, and each Link's device register maps reach its session,
  its Commands table and the Registers pane's Peripheral block. A grid_rows that is not a number is one import note and DEFAULT_GRID_ROWS, and the workspace's mode switch after it still runs (Timing to Analysis) (`test_view_prefs_no_stale_state_in_reused_window`). The file tests round-trip the scrambler overrides and the view preferences. A missing workspace file raises ValueError naming its path, and one over `MAX_WORKSPACE_CHARS` is refused the same way while a file at the limit loads (`test_load_reads_no_more_than_the_workspace_limit`). An endless stream is read once for at most the limit plus one character (`test_load_reads_an_endless_stream_no_further_than_the_limit`), and /dev/zero raises 'too large' (`test_load_of_a_device_that_never_ends_raises`).
- `test_demo_workspaces.py` — each File ▸ Open Demo Capture entry opens its capture with its
  bundled workspace (`data/demos/*.swi3s`): data-port names such as 0.0 "AMP1" on PHY1, PHY2
  and PHY3; two Links on PHY2, Link 2's data ports named as in the flow-control demo; and the
  shipped files carrying no window geometry, no Commands sort and no paths.
- `test_links.py` — `LinkSet` (headless): ordering, the active Link across add/remove,
  default "Link n" names, rename; the time mapping — sample → ps → sample is the identity
  at every rate (the single-Link invariant), signed offsets, two rates meeting at one
  instant; a name that only looks like a default number (superscript, Arabic-Indic or full-width digit, underscore, a 5000-digit tail) is a user-chosen name and never breaks the next default name (`test_a_name_that_only_looks_like_a_default_number_does_not_break_naming`).

**Headless GUI**
- `test_gui_smoke.py` — constructs the real `MainWindow` under the Qt **offscreen**
  platform, loads the demo, and exercises: every pane populates; the shared cursor drives
  the register view; two-way command ↔ timeline ↔ CDS-symbol ↔ audio sync (a command's
  start_sample equals its SPM-comma Row Sync Point); the **Filter menu** (multi-select
  kinds, boolean and/or text, errors-only) and the title's filter indicator;
  bookmarks; **64-bit sample signals** (>2³¹); the Compare expected-config (CSV-Import)
  overlay; the audio cursor line + click-to-seek mapping. An audio view with an empty decimation list restores without a warning under a workspace file name (`test_an_audio_view_with_no_decimation_restores_without_a_warning`). A workspace's saved cursor past the capture opens at its end, and a per-source authoring list of the wrong length is reported under the workspace file's name (`test_a_workspace_cursor_past_the_capture_opens_at_its_end_and_authoring_notes_name_the_file`). Rebuilding the eye filter menu deletes the previous Columns submenu and its actions (`test_rebuilding_the_eye_filter_menu_does_not_accumulate_submenus`). The register-map dialog names the encoding it read a non-UTF-8 XML file as, and a byte-order-marked file reads clean (`test_the_register_map_dialog_names_the_encoding_it_read_an_xml_file_as`). A field's tooltip says whether an excess-1 value is the whole stored value or one assembled from parts (`test_an_excess_one_field_tooltip_says_whether_the_value_is_the_whole_stored_value`).
- `test_links_gui.py` — multi-Link analysis in the real `MainWindow` (offscreen):
  per-Link state follows the active Link; add / switch / remove / rename; each Link keeps
  its command filter and column widths; background re-decodes; the load queue's kinds;
  cross-Link bookmarks, offsets and Align; the stacked timeline; All Links; the three
  independent pane groups, each at the same instant on its own Link; a pane's menu acting
  on its group's Link; the saved multi-Link layout; and regressions for a removed Link's
  re-decode, a workspace Link that fails, a Link added while a workspace loads (each saved
  Link's state lands on its own capture, matched by Session), floating docks, Locate
  Sub-Capture results landing on the searched capture's Link or dropped when it is gone, a
  background Bus Grid persistence result keeping its Link's CDS column, measured rates
  within a tolerance. A switch's caches are pinned by COUNTS (no re-measure, no refit),
  since a CI-safe wall clock cannot see them. Picking a Link in the Commands picker, when the clamp moves the cursor's instant, moves every playhead to the new instant. A Commands filter change never moves the cursor: the table re-selects the nearest visible command (asserted after every change, at least one of which hides the current row), and a workspace saved with a filter reopens at its cursor. A reopened workspace shows its active Link and cursor on every band, also when the saved cursor equals the live one, and a lane's filter restore does not make that lane's Link active. Removing the Link that reached furthest fits the timeline to the Links left. A reopened workspace restores a saved cursor of 5000 in both bottom-scope arms. A row picked in the Commands table while another pane group made a different Link active is read from the Link the table shows: the cursor moves to that Link's command, on that Link. A real click on each All Links row leaves that row current and alone selected, its Link active and the cursor on its command (`test_a_click_on_any_all_links_row_leaves_that_row_highlighted`). PageDown after a click on another Link's band highlights the row at the cursor (`test_page_down_after_a_click_on_another_links_band_highlights_the_row_at_the_cursor`). A Link added while All Links is on keeps the row at the cursor current (`test_a_link_added_while_all_links_is_on_keeps_the_row_at_the_cursor_current`).
- `test_link_sequences.py` — seeded random sequences of Link operations (offsets,
  activations, cursor moves, Align, removals and additions, All Links scopes) on three
  Links, with `window_checks.check_window` after every step, a cursor round-trip property,
  and a save and reopen that must bring back what was displayed, band ranges included. A
  failure names its seed and step. `tests/window_checks.py` is the shared helper: it
  recomputes the derived state (band ranges, the remembered cursor instant, every index
  that names a Link) from the Links' sessions and offsets and compares it with what is shown.
- `test_load_queue_settles.py` — every load settles exactly once (its `after` or its
  `on_fail`) whichever stage fails: the decode, building the views before or after the
  session became a Link, or the caller's follow-on, for an open, an added Link and a
  re-decode; a failure does not stall the requests queued behind it; and a re-decode
  discarded because its Link was removed, queued or running, settles as failed.
- `test_two_links.py` — two Links end to end on the `conftest.two_link_captures` fixture:
  one `.sal` carrying both, each Link decoding exactly as it does alone, one file-wide
  timeline, File ▸ Open taking both through the dialog and the real load queue, one row
  opening one Link, adding a Link from a second file, and Align recovering a known offset
  between two files to within one sample. Also the in-app **two-Link demo** (Open Demo Capture ▸
  PHY2): it IS the fixture, edge for edge; it loads synchronously and through the async
  queue; and a saved workspace rebuilds its delayed second Link.
- `test_cursor_cost.py` — what a cursor move is ALLOWED TO DO, asserted as **counts, not
  wall-clock times**: a move inside the Decoded-Samples window builds zero rows, a move
  outside builds at most one window's worth, a config region above
  `_TX_PERSIST_SYNC_MAX_UIS` goes to the worker thread while one below it stays inline, and
  a hidden dock costs nothing. Counts are deterministic and identical on a loaded runner, so
  they fail on drift where a ceiling only fails on a cliff, and they can say "and not on the
  GUI thread", which a ceiling on a *function* cannot.
- `test_phy_bringup_gui.py` — a capture with a §5.1.2 bring-up loads into Analysis with
  the timeline bring-up band + PHY label, with the grid gated at t=0; a capture joined mid-stream draws no bring-up
  band and keeps the Capture pane's DP/DN line names.
- `test_timeline.py` — the timeline ribbon's tick paint order (significant marks win a
  colliding pixel over Pings) and distinct per-kind colours, and that no legend entry whose
  colour flips between near-white and near-black across the palettes names a shade.
- `test_timeline_interaction.py` — the ribbon driven by **real Qt input events**
  (`QApplication.sendEvent`): wheel zoom about the pointer, the zoom limits (the whole
  capture; 16 samples), horizontal and Shift-wheel pan, a middle-drag keeping the grabbed
  sample under the pointer, click-to-seek, a left drag that coalesces its seeks and ends
  where the button was released, bookmark drag, double-click reset, and that a TOLD view
  (`set_view`) does not re-announce itself. In a stack of Links: zooming or panning one band
  shows the same instants on the other at any offset, a band click makes its Link active at
  the clicked sample, and a zoom or pan moves no cursor and rebuilds no pane (counted).
- `test_bookmark_deltas.py` — bookmark ΔRows / ΔUI: on one Link exact COUNTS (a DLV pair
  across the Safe-Lock-4 to 16-column commit);
  across two Links shown only where they SHARE that timing over the interval, judged per
  geometry region: rows withheld for an 8- vs 16-column pair whose capture-wide rates agree,
  withheld across a column change, nothing claimed where a Link was not recording or is in
  its bring-up, both shown where both Links share both rates.
- `test_timeline_decimation.py` — the ribbon's tick and commit-point decimation (one mark
  per pixel column, the most significant) equals a per-command loop exactly, kept in the
  test as the oracle: every column's winner and the list's order, over
  random commands of every rank listed out of time order, colliding and missing starts, a
  filter, many zooms, several widths, and a negative domain (a stacked Link), and every
  set_draw_filter, set_events and set_commit_points call is drawn even when several land
  between two paints (the caches are keyed by generation, not by object identity).
- `test_dialogs_gui.py` — the dialogs driven as a user would, through `_drive` (§8): Export
  Audio (per-stream selection, the playback rate seeding the export rate, a typed rate, the
  WAV written at that rate, nothing ticked, cancel), Import Visualizer CSV (a source required, authoring compare-only, a
  capture CSV refused), Import Peripheral Register Map (preview, coalesce toggle, normalized
  JSON, a broken file), and Set Time Offset (unit conversion to the picosecond, cancel).
- `test_modal_guard.py` — the conftest guard itself: an unpatched modal answers "cancel" and
  is recorded, a test's own patch wins, every guarded name exists, and every static dialog
  helper the app calls is guarded (read from the source: every static helper called on a Q...Dialog class or QMessageBox is checked).
  A static helper with a parent is recorded under its caption; a menu opened on an instance is cancelled.
- `test_modes_gui.py` — the **three-mode shell**: the mode controller (`_mode_mgr.switch_to`, which the Mode menu drives) swaps the central page
  and the visible dock set per mode (Visualization | Timing | Analysis); Analysis-only menus
  gate by mode; returning to a mode restores its layout; the active mode round-trips through
  the workspace. Also: Visualization authoring places the grid + flags a seeded bus clash,
  the authored config can be compared in the Analyzer (Decode ▸ Import Visualizer CSV), and
  the workspace persists the authored
  config + timing inputs; the Keyboard Shortcuts help opens and names the pane-maximize and cursor keys; the arrow keys step through the authoring cells.

**Visualization-mode authoring + Timing-mode (mostly Qt-free)**
- `test_authoring.py` (includes `test_a_group_separator_cannot_be_typed_into_a_numeric_cell_and_never_breaks_commit`) — `BusConfig` ↔ the v2.0 CSV (the engine + C++ core read it) + dict
  round-trips; an authored config is placed by the `grid_from_csv` cascade (and yields register
  writes), including wide-bit held-column identity; every data port gets a name in the CSV.
- `test_authoring_readouts.py` — the authoring panel's System SSP interval is the model's
  `Interface.interval_lcm` (a skipping port's period is Interval x SkippingDenominator) and
  each port's rate is the core's, TX_PRESENT counted in every flow mode that has one.
- `test_timing.py` — `TimingView` renders the
  inequality headings + the summary table and round-trips its inputs. A Manager that forces its
  keeper leaves keeper_Man not applicable (no terms, infinite margin and F_max) and moves no
  other leg, and at the default, EDE, SoundWire and 4x inputs, where keeper_Man does not bind, nor the binding rate
  (`test_a_forced_keeper_leaves_keeper_man_not_applicable_and_moves_no_other_leg`); where keeper_Man binds (about 15.55 MHz) a forced keeper moves the binding to MP_setup at about 16.09 MHz, and a leg that does not apply never bounds F_max_binding_MHz or names binding_inequality, whatever its rate bound reads (`test_a_forced_keeper_moves_the_binding_rate_off_keeper_man_where_it_binds`).
  F_max stops at the toggle ceiling, 1000 × duty_min / t_RF,max, one slowest edge per shortest half period (`test_the_toggle_ceiling_is_one_slowest_edge_per_shortest_half_period`, with the SoundWire 5.0 and 6.0 ns cases); a leg at the ceiling names it in its F_max note with the numbers of its formula, 1000 × duty_min / t_RF,max (`test_no_ui_scaled_leg_reports_an_fmax_past_the_toggle_ceiling`); a RowPoint copies, deep-copies and pickles as a RowPoint with its text and position (`test_a_row_point_survives_copy_deepcopy_and_pickle`). No leg's margin moves
  with a t_RF lane outside its declared `_LEGAL_LANES` set (a releasing EDE side's contention legs also read its DATA lane, through the release's ramp term; the sweeps include SoundWire sides, `_LEGAL_LANES_SW_MAN` / `_LEGAL_LANES_SW_PER`); no leg shows a structurally zero
  term (checklist rule 5), and every zero-valued term is a parameter whose row can reach
  zero; the calculator's module docstring states the number of legs in `INEQUALITIES`.
  Also the conventions that carry no number and so would fail silently: every leg's terms read in **physical
  time order** (receiver window last, clock lane before data, a peripheral launcher's
  launch between its two crossings; a keeper leg's launch, its crossing of the keeper's threshold, closing edge (on keeper_Per then the spacing of the Peripheral's two detections), release, then the keeper's response; a contention leg's releaser (an EndDriveEarly release as its t_DD, ramp, EDE Delay and the UI back to the closing edge), handover UIs, then acquirer; on P→P the two detections stay gathered ahead of the release), **A drives and B samples** on all four P→P legs (also on buses with SoundWire Peripherals, whose `,anchor` term follows A's launch row and reads no row), the
  1)–17) numbering matches the display order, every printed symbol **resolves to an input
  row** (or a declared row-less key), and the term highlight's **click regions agree with
  the painted glyphs** at every sample point on every row — the invariant that catches a
  hit-test laying out at a different font from the paint path; a click on an input row's
  name toggles its highlight as a term's does; the naming-scheme test reads each crossing as `X_<pair>[,<device>],<edge>[,slow|fast],<role>` and also asserts the sweep yields every qualifier it names (X_MP/X_PM/X_PP/X_K, A/B, rise/fall, rr/rf/fr/ff, slow/fast, late/early) and the ramp to V_OH / V_OL as `<dev>_t_RF,DATA,rise` and `,fall`, then `,fast`, each of which keys on its lane's row, and an anchor correction as a Peripheral's `,anchor,rise` / `,fall` and a Manager's `,anchor,rr` / `,rf` / `,fr` / `,ff`, then `,fast` on the fast data edge, each keyed on its launch row; every crossing names its edge, `rise` / `fall` or a Sigma_cross,PM sum's case letters, every ramp to V_OH / V_OL ends `,rise` / `,fall` and every anchor term names its edges, and no clock detection is `X_PP`, over every pairing of the spec sources with EndDriveEarly and zero to two handover UIs, both ramp shapes at asymmetric V_OH / V_OL, each P→P placement, zero noise, every supply assignment on both data-edge sets and the view's two columns (`test_every_crossing_and_ramp_symbol_names_its_edge`); the click test pins which leg and term the diagram draws, and the time-order rules 1-4 also run under EDE and SoundWire sides. One device has one supply: each crossing's
  rail and threshold come from its two devices, and each leg reports its worst assignment, and no supply inside the tolerance (each device's nominal, ends and halfway points) leaves less.
  The F_max column (summary and tree) prints each leg's own worst-corner F_max, non-setup
  legs included, and on the default page MP_hold_ho, PP_hold_ho, the three contention legs and both keeper legs have a finite one in both columns. `compute` refuses with a ValueError naming the input any number that is not finite, a per-edge slew
  tolerance or supply tolerance δ outside [0, 1), a clock rate, trace velocity or nominal supply
  that is not positive, a duty floor outside (0, 0.5], a negative handover UI count and a negative slew, bus length, trace mismatch fraction, noise fraction α or keeper response (zero values of these are evaluated)
  (`test_an_input_no_leg_can_be_evaluated_at_is_refused_with_a_value_error`). A clocked Manager launch placed off its row's value carries its placement note on every leg that shows it (`test_a_clocked_manager_launch_carries_its_placement_note_on_every_leg`); a crossing term shows its coefficient positive under its operator, which is `-` exactly where the value subtracts (`test_a_crossing_term_shows_a_positive_coefficient_under_its_operator`); the lane-decomposition check is relative to the values, so a slew far past the spin boxes' range is evaluated (`test_the_lane_decomposition_check_scales_with_the_slews`). A tie between supply
  assignments is one to 1e-12 ns and keeps a leg's first case (`_worse_case`) where there is one
  (`test_a_tie_between_supply_assignments_is_one_to_the_rounding_of_its_sums`). The
  contention-order rule also runs at two handover UIs. PHY1 is chosen per side by the spec selector, reseeds the page with PHY1's rows and persists through `to_dict` (`test_phy1_selector_reseeds_spec_and_persists`). No P→P leg reads one `Per` row for both devices, also on buses with SoundWire Peripherals, whose `,anchor` term follows A's launch row and reads no row (`test_no_pp_leg_reads_one_per_row_for_both_devices`). A gathered crossing's symbol, factors and note state one sum: the factors add to the value in force, the bracketed symbol carries one crossing name per coefficient, and the note says the same sum (`test_a_gathered_crossing_states_arithmetic_that_actually_adds_up`). Rise/Fall Times holds the t_RF rows and the per-edge slew, System Properties holds the V_SEOS tolerance, noise fraction, bus length and length mismatch, each card carries its title in its own top-left cell, and the two ramp-model pickers sit in the top bar (`test_the_parameter_sections_and_where_the_ramp_models_live`). The highlight's wash is a class the document carries on exactly the active keys (`test_the_wash_is_applied_by_class_to_exactly_the_active_keys`), an anchored term keeps its palette ink in both modes, compared by render, and that test skips where no glyph rasterises (`test_anchored_terms_keep_the_palette_ink_in_both_modes`), a click on an operator between terms is inert and only empty space clears the highlight (`test_a_click_between_terms_is_inert_and_only_empty_space_clears`), and a toggle, or a click on a parameter row's name, is a repaint that runs neither the corner search nor `compute` and does not change the item text (`test_a_highlight_is_a_repaint_and_not_a_recompute`).
- `test_timing_cross_spec.py` — mixed SWI3S ↔ SoundWire 1.3 timing, by section of the file:
  - **Non-regression.** With both sides SWI3S the calculator is identical to single-spec;
    every leg's displayed terms sum to its margin, also with Man CLK, Man DATA and Per DATA at
    three different t_RF values; PP_setup / PP_setup_ho follow the Per DATA lane and never Man
    DATA; the P→P data crossing takes the Peripheral's ramp shape and the clock lane the
    Manager's.
  - **Anchor placement.** A launch sits at its anchor at every slew and each crossing is
    charged from it; the Manager's times are measured from its clock reference; no edge's slew
    leaves its spec's range under the per-edge slew tolerance, which scales the anchor and the
    reference with the edge they are on, and no edge of any lane, Manager lanes included, leaves its spec's t_RF range; on a linear ramp a keeper leg waits for t_DD and the data's
    crossing of the Manager's V_IH,max / V_IL,min at the Manager, a Peripheral's offset by the
    ground shift
    (`test_the_keeper_leg_charges_the_launch_at_its_anchor_and_its_crossing_at_the_manager`), and an
    EndDriveEarly release is t_DD + t_RF + EDE Delay − UI, on either side; on an exponential ramp an
    EDE release's ramp to V_OH / V_OL is the ramp's closed-form crossing time, on the slower
    data edge on a contention leg, so its margin drops by (c − 1)·t_RF on a late edge at
    0.10 / 0.90 and at asymmetric pairs and by nothing at 0.20 / 0.80, and on the edge its
    keeper crossing is priced on as the EDE keeper leg's credit
    (`test_an_exponential_ramp_to_v_oh_is_its_true_crossing_on_keeper_and_ede_legs`), the Manager's
    EDE release on MP_contention charges c·t_RF on a late edge, read from its own term
    (`test_a_manager_ede_release_charges_its_exponential_ramp_to_v_oh`), and where the two
    polarities' keeper crossings tie a keeper leg takes the rising edge, and where the two
    ramps tie an EDE release the falling one, while values a few parts in a million apart are
    not a tie (`test_the_edge_tie_goes_rising_on_a_keeper_and_falling_on_an_ede_release`), and
    the EDE keeper leg takes its keeper crossing and its ramp on one data edge, against a SWI3S
    or a SoundWire Manager
    (`test_a_keepers_ede_leg_takes_its_crossing_and_ramp_on_one_data_edge`); the
    corner search stays exact there
    (`test_the_corner_search_is_exact_where_the_ramp_to_v_oh_is_not_one_t_rf`). The term names
    the edge it is priced on, `<dev>_t_RF,DATA,rise` / `,fall`, and one
    ramp symbol carries one value on every leg at every supply assignment, its coefficient the
    closed form of that edge, at four V_OH / V_OL pairs over SoundWire and SWI3S sides,
    EndDriveEarly and handover UIs
    (`test_one_ramp_symbol_has_one_value_on_every_leg_at_every_supply_assignment`). A ramp to
    V_OH / V_OL priced on the fast data edge carries `,fast` and its note names the fast edge,
    so one ramp symbol has one value across every leg of a result
    (`test_a_ramp_priced_on_the_fast_edge_names_it`). An EndDriveEarly
    release is shown on each contention leg as its parts in time order from the edge that opens the driven UI and summing to the
    release at 0, 1 and 2 handover UIs, SoundWire on either side and both ramp shapes.
  - **The SoundWire equivalents.** A SoundWire side's presets are its SWI3S equivalents
    (`sw13_equivalents`, Peripheral and Manager, small and large), each the derivation
    rounded outward to 0.1 ns; a SoundWire side evaluates exactly as a SWI3S side with the same
    rows but for its data-input window, its anchor corrections and a SoundWire Manager's keeper
    threshold; a SoundWire Peripheral takes no clock-side offset; an anchor correction is zero
    at the equivalents' own slews for both system sizes and its note states the slews that
    reproduce its move; a SoundWire anchor takes its side's ramp shape and follows the V_OH /
    V_OL in force (`test_a_soundwire_anchor_follows_the_v_oh_and_v_ol_the_calculator_reads`),
    an exponential ramp's 80 % point being (ln(1−V_OL) − ln 0.2)/ln 4 of a slew after its anchor;
    every `,anchor` term is its launch's closed-form placement on the data edge and the Manager
    clock edge the leg's timeline draws it on, a falling launch on the falling ramp, at
    0.20 / 0.80 and three pairs with V_OH + V_OL ≠ 1, on both shapes, with EndDriveEarly and
    handover UIs, and the correction matches the closed form at every pair of polarities
    (`test_every_soundwire_anchor_is_its_launchs_placement_at_the_edges_it_is_drawn_on`);
    an `,anchor` term names its edges, a Peripheral's data edge
    `,anchor,rise` / `,anchor,fall` and a Manager's clock then data edge `,anchor,rr` / `,rf` /
    `,fr` / `,ff`, then `,fast` on the max corner where the side's DATA lane is in the leg's
    fast data-edge set, its value is `sw_anchor_ns` on those edges at its row's corner, and one
    symbol has one value on every leg of both data-edge sets at each supply assignment and
    corner (`test_one_anchor_symbol_has_one_value_on_every_leg_at_every_supply_assignment`);
    a SoundWire t_OV launch and an EndDriveEarly keeper leg are reported at the worse of
    the slow and fast data edge; a SoundWire Manager's margins do not depend on the launch mode; the SoundWire hold bound is t_ZD_Data,Min, not t_OH_Data (`test_soundwire_hold_bound_is_t_ZD_not_t_OH`); an edge anchored at its 80 % point is also evaluated on its fast factor where its anchor and a crossing of it both enter, and the leg reports the worse (`test_an_eighty_percent_anchored_edge_is_evaluated_on_its_fast_factor_too`).
  - **Advisories and sources.** SoundWire caps the clock below the SWI3S rate, and the ceiling is
    the lower side's maximum, PHY1's 6.6 MHz against SoundWire; a SoundWire data
    input's crossings take a 35–65 % window on both edges while every clock input keeps the
    V_IH / V_IL rows; a SoundWire Manager seeds its 2.5 ns keeper response and a negative
    t_IS; the keeper legs count the response from the Manager's threshold at its supply, a
    SWI3S Manager's V_IH,max / V_IL,min and a SoundWire Manager's V_TH_Keeper,max /
    V_TL_Keeper,min, a Peripheral's data offset by the ground shift, and each term's note names
    the source, the threshold and the ground shift
    (`test_a_keeper_counts_its_response_from_the_managers_threshold`); a conventional release's
    keeper crossing is its closed form on both ramp shapes at asymmetric V_OH / V_OL, on the
    edge whose crossing is the later after the clock reference with a SoundWire launch's
    anchor placed on that edge, with either Manager and a SWI3S or a SoundWire Peripheral
    (`test_a_keepers_crossing_is_its_closed_form_on_either_shape`), keeper_Man opens on the clock edge that places a SoundWire Manager's launch the later; at V_OL 0.30 that is the falling edge, which leaves the keeper 1.0 ns less than the rising one (`test_keeper_man_opens_on_the_clock_edge_that_places_its_launch_later`), and the keeper term's note names the Manager's source; an EndDriveEarly release
    counts its keeper leg from the keeper's threshold crossing, against either Manager
    (`test_a_keeper_counts_an_ede_release_from_its_threshold_crossing`);
    converted terms carry provenance; every unratified source (the proposed revision and EDE)
    carries PROPOSAL_NOTE first in the result caveats; a SoundWire pair's caveats end with
    `HANDOVER_SW_PAIR_NOTE`, the handover-UI count taken as given, and a mixed bus's end with
    `HANDOVER_CROSS_SPEC_NOTE` (`test_a_soundwire_side_flags_the_handover_assumption`); the EDE column's PVT spreads are not
    tighter than 2:1, t_DZ having a minimum of 0.
  - **The page.** A spec change on either side reseeds the whole page from the selected pair, Manager rows and F_CLK included (`test_a_spec_change_reseeds_the_page_from_the_selected_pair`); spec selection round-trips
    and a legacy dict defaults to SWI3S; a legacy PHY1 workspace restores as a PHY1 bus; the
    PHY1 / PHY2 Example columns follow the table structure; the two SWI3S PHYs are
    independently selectable per side; the size row hides and returns with the SoundWire
    selection; the launch hint names the right-hand column by its heading as the one the selector
    drives (`test_the_launch_hint_names_only_the_column_the_selector_drives`).
  - **The proposed revision** closes every failing inequality at the reference corner, leaves
    a sub-ns deficit at the design target, carries its unratified flag, and reaches only one of
    two hold terms on a mixed bus.
  - **Manager launch mode** (analog, 2x, 4x): clock launch beats analog on both MP legs at the
    design rate, the two clock modes are bounded from opposite ends, the mode governs every
    Manager output including the EDE t_DZ, all three Manager timings are clocked or all analog
    and a 4x clock places them independently, the selector preserves the user's rate and its
    hint tracks the rate and flags the ceiling, a clocked launch never snaps below the tabulated
    minimum and never governs the Specification column, the launch hint names only the column the selector drives (`test_the_launch_hint_names_only_the_column_the_selector_drives`), and the mode round-trips with legacy
    dicts defaulting to analog; clocking the Manager's release costs MP contention exactly its
    grid-snapped excess over the tabulated t_DZ.
  - **Handover.** Non-contention (the level is owned by the hold_ho legs, contention is an
    overlap bounded by one flight, the out-of-scope split matches the legs' own terms), the
    P→P placement selector and its 2x2 launch methods, PHY1 handing over in zero UIs, PHY2
    needing its UI, a mixed PHY pair taking the larger allocation, every spec source having its own
    allocation, EDE 0 UIs, with an unknown source refused
    (`test_every_spec_source_has_a_handover_allocation`), the handover checkbox, and
    the whole-count rule for a fractional count. Each allocated handover UI adds exactly one UI
    to MP_hold_ho, at two clock rates; under a clocked launch MP_hold_ho is at least the same
    column's analog margin and PM_hold_ho is unchanged; one allocated UI clears PP_hold_ho at
    its own worst corner.
  - **Crossing symbols.** One crossing symbol carries one coefficient on every leg it appears
    on, swept over unequal t_RF in both directions, independent Manager/Peripheral ramp shapes
    and noise / supply / slew tolerance, and checked at every supply assignment. A single
    crossing is named for its direction and its edge, so every Peripheral's lone detection of
    the Manager's clock is `X_MP`: a releasing peripheral's on the contention legs is
    `X_MP,rise,late` (`X_MP,fall,late` on a falling edge), an acquiring one's `X_MP,rise,early`
    (`X_MP,fall,early` falling), the names of MP_setup's early clock crossings, which
    Sigma_cross,PM,hold's rf and fr clock lanes and keeper_Per's closing detection carry too,
    and on a P→P leg Peripheral A's `X_MP,A,…` and B's `X_MP,B,…`, B's early detection on
    PP_setup sharing its symbol with P→P contention's acquirer; `X_PM` on the clock lane is
    only a Sigma_cross,PM sum's clock addend beside its data addend, with the sum's case
    letters, PM_setup's `X_PM,rr,late` / `,rf` / `,fr` and Sigma_cross,PM,hold's rr case
    `X_PM,rr,early` and ff case `X_PM,ff,early`, over both spec
    families, EndDriveEarly, zero to two handover UIs, exponential ramps and a pinned P→P
    placement, with each `X_MP` clock symbol at one value per supply assignment
    (`test_every_lone_clock_detection_is_an_x_mp_crossing`). Each crossing term's factors are its
    coefficients and its named lane's t_RF, and its value scales with the slews
    (`test_crossing_terms_are_dimensionless_coefficients_times_a_named_lane_trf`).
  - **Shared edges and polarities.** Where a P→P leg's two clock detections are of one Manager
    edge (PP_hold; PP_hold_ho and a conventional PP_contention at zero handover UIs), both take
    that edge's one slew factor, the worse for the leg, each with its own coefficient, and
    detections of two edges take their corners' own, at every supply assignment and inside the
    slew range. PP_hold_ho at one, two and three handover UIs equals a hand derivation (A's
    early detection is at V_IH, `X_MP,A,rise,early`, on the rising edge an even count gives and
    at V_IL, `X_MP,A,fall,early`, on the falling edge an odd count gives); at an odd count the
    acquirer's turn-on and PM_hold_ho's launch after a rising sampling edge follow a falling
    edge. Every setup and hold leg is the worse of its sampling edge rising and falling, each
    by hand in a window below mid-rail where the other polarity binds, and the two tie in the
    default window at equal supplies. Every contention leg is the worse of its closing edge
    rising and falling, the same way. Sigma_cross,PM,setup is the worst of its same-polarity
    and cross cases, by hand in both windows (the independent recomputation is in
    `test_delta_tpd_ground_shift.py`).
  - **EndDriveEarly.** The EDE release replaces the conventional one on the side that has it;
    the EDE pair is proposal versus EDE; EDE helps every contention leg it owns, and the keeper
    legs read the EDE Delay,min and the ramp to V_OH less the crossing of the keeper's
    threshold and the keeper demand (`test_ede_helps_every_contention_leg_it_owns`);
    one Man_t_DZ is read by the keeper and the contention legs; every EDE value is absolute, so no
    EDE row moves with the rate (`test_no_ede_row_moves_with_the_rate`).
  - **The value on screen is the value in force.** Every governed row agrees with the term it
    feeds; the keeper legs are split so the corner search stays exact; the clock-reference
    test also pins each clock term's sign.
  - **The corner search.** The slews, τ, α and δ are searched jointly on every bus: τ, α and δ over every
    combination of their min, typ and max, and each slew row over its whole range. The corner
    search leaves no leg more margin than brute force over the min/typ/max grid. That holds with a SoundWire side, over the slews,
    τ, α, δ and the bus length in both system sizes, and over the slews, τ, α and δ with the
    Manager's lanes unlinked. It also holds on a SWI3S-only bus, with a linked Man CLK or Man
    DATA t_RF row whose range is not its master's and with α up to 20 %
    (`test_the_corner_search_is_exact_on_a_swi3s_bus_whatever_the_slew_and_noise_ranges`). The
    Timing view's Specification column with SoundWire on both sides at 12.288 MHz shows
    MP_hold, PM_setup and keeper_Per no better than that worst. The search is exact at V_OH / V_OL pairs
    with V_OH + V_OL ≠ 1 (0.80 / 0.15, 0.85 / 0.25, 0.90 / 0.30) with a SoundWire side, on
    both shapes, with EndDriveEarly and handover UIs
    (`test_the_corner_search_is_exact_at_asymmetric_soundwire_levels`). It holds with the
    V_IH / V_IL windows set low, where the direction α and δ move a P->M margin depends on the
    slews: PHY2/PHY2 linear, PHY1 / PHY2 EDE under 4x and 2x, V_OH / V_OL 0.80 / 0.30, and
    exponential ramps (`test_the_corner_search_is_exact_with_the_input_windows_set_low`). It
    also holds where δ leaves P->M hold least inside its range, below both ends and no worse
    than typ, a positive control on the Specification column with a SWI3S PHY2 Manager on an
    exponential ramp and a SoundWire Peripheral
    (`test_the_corner_search_is_exact_where_delta_binds_at_typ`). Where a margin turns inside
    δ's range and rises to another supply assignment's plateau, the search's δ pick is a point
    inside the range below the min/typ/max grid's margin, within 0.2 ps of a fine sweep, and
    the search without the sampling misses it
    (`test_the_corner_search_finds_an_interior_delta_minimum_past_a_supply_case_boundary`). Every leg's least combination is refined along each of τ, α and δ once, and a point found for one is reported for that row and no other (`test_the_corner_search_refines_each_fraction_row_and_reports_its_inside_point`); the refinement reports an interior minimum of any of the three as a RowPoint there and leaves a combination least at an end as it was (`test_the_refinement_keeps_an_inside_point_only_where_its_margin_is_smaller`). With a SoundWire side or
    without, and at those pairs on exponential ramps, every other row moves each margin by the
    same amount at every corner of the slews, τ, α and δ
    (`test_the_other_rows_move_a_margin_independently_of_the_coupled_rows`). A tie in the joint search goes to the combination with the most rows at typ, so a joint row a leg does not read is reported at typ (`test_a_joint_row_a_leg_does_not_read_is_reported_at_typ`). keeper_Per's search leaves no more margin than the worst of
    the rows its two detections read
    (`test_the_keeper_per_corner_search_is_exact_over_the_rows_its_detections_read`), and on
    both keeper legs, against a SWI3S and a SoundWire Manager, with a conventional and an
    EndDriveEarly release, on both shapes and at the default and a low asymmetric input
    window, the search leaves no more margin than the worst of the slews, τ, α and δ, nor
    than any point of a slew row's range
    (`test_the_corner_search_is_exact_on_both_keeper_legs`).
    Each edge's t_RF is affine in its row between two consecutive kinks of `edge_kinks_ns`, and turns
    at each one, at every τ on SWI3S and SoundWire lanes
    (`test_each_edge_is_affine_in_its_row_between_two_kinks`). No point of a slew row's range leaves
    any leg less margin than the search's corner. Each slew row is scanned at 25 positions, every
    other row at the leg's picks, in four configurations: a Man DATA row linked to a Man CLK row of
    another range, whose kink falls with CLK mid-range and puts MP_setup's corner inside the range; a
    SoundWire Manager (MP_hold); a SoundWire Peripheral on exponential ramps (MP_setup); and the
    Manager's lanes unlinked with τ up to 10 %
    (`test_the_corner_search_is_exact_over_each_slew_rows_whole_range`).
- `test_perf.py` — wall-clock ceilings on the hot paths (marked `perf`, run on their own with
  `pytest -m perf`): decode, engine build, CSV import, cursor move, zoom/pan, Link-switch and
  Timing-recompute budgets, and a `.sal` load's peak memory against its prediction. Cliff detectors, not
  micro-benchmarks: the Timing-recompute ceilings are about twice the median. The cursor-move,
  Link-switch and Timing-recompute budgets print their median and worst as a fraction of each
  ceiling at the end of the run (`perf margins`), pass or fail, so a runner creeping up on one
  shows before it goes red.
  The Timing-recompute and Link-switch budgets have slower ceilings on Windows and on a shared runner and checks that the SoundWire Peripheral page is selected before timing it; each timed recompute is asserted to have computed margins (no compute error, a rendered result); the cursor-move and Link-switch budgets assert that every dock is visible; the low-entropy locate asserts that the exact-match path returns hits and the no-match path returns none.
- `test_delta_tpd_swing.py` — the crossing envelope's TX swing is a PARAMETER (the crossing envelope takes
  `V_OH_frac` / `V_OL_frac`, which `CalcInputs.envelope()` threads in from `V_OH_min_frac` /
  `V_OL_max_frac`). The ramp from an anchor to V_OH / V_OL (`ramp_to_valid_frac`) is 1 on a
  linear ramp and the closed-form exponential crossing at six level pairs, both polarities, where
  `ramp_level` reads V_OH / V_OL. Unanchored (counted from
  the start of the ramp) a linear crossing scales as 1/swing and an exponential ramp is
  independent of it; anchored (counted from the 20 % point, as `CalcInputs.envelope()` reads
  it) a linear crossing is the unanchored one less V_OL/swing on the crossing's own edge, (1 + τ) on a slow
  one, and an exponential crossing moves with V_OL. Every linear coefficient moves with the pair, the default gives the
  tabulated 0.60 swing (`0.80 − 0.20`), every unanchored linear coefficient scales as 1/swing, an
  inverted swing raises instead of dividing in the envelope and in the anchor and ramp-to-valid
  fractions, and on an exponential ramp those fractions also refuse V_OL ≤ 0 and V_OH ≥ 1 by name;
  a ramp sampled before its start is at its start rail
  (`test_a_ramp_sampled_before_it_starts_is_at_its_start_rail`). A falling edge's 20 % point is its V_OH crossing on both shapes, and a linear falling crossing counts from it, differing from the rising fraction where V_OH + V_OL ≠ 1 (`test_a_falling_edge_counts_from_its_v_oh_level`). The
  calculator path is tested through `CalcInputs.envelope()`. The `rising_*` / `falling_*` deviations carry no per-edge slew tolerance and no anchor, and
  `uncorrelated_diff` is `rising_max − falling_min`.
- `test_timing_vs_reference.py` — **the divergence log, executable.** The same timing model
  exists twice: here, and in a reference analysis vendored as
  `tests/fixtures/timing_reference.json`, which holds every value the file reads from it and
  names the module and revision it came from. Every reference leg except those `_UNCOMPARED_LEGS`
  declares with their reasons (keeper_per_ede, keeper_response_man,
  keeper_man_retired_launch, keeper_window_opens) is evaluated against its counterpart under
  one matched configuration, each delta DECLARED. This model's slew-range limit is lifted for
  the comparison, since the reference applies a plain ±τ.
  `test_every_reference_leg_is_compared_or_declared` fails for a reference leg that is neither
  compared nor declared. `docs/anchors.md` ("The cross-check against the reference") is
  the table of record for what each delta is, its size and the legs it applies to; this file
  holds the same constants. A placement delta takes its edge's slew factor, (1 ± τ). The
  deltas that are a number rather than a formula (the shared-edge slew factor, the supply
  split, the polarity case, the keeper_Per detection spacing and the P→P slew split) are
  pinned per leg and also checked against what the model computes. The P→P contention test also
  pins the three margins the slew-split note quotes: −18.48 in the reference, −14.96 with the
  slew-range limit lifted and −14.63 with it in force.

  A delta that MOVES fails. The deltas are deliberate. The fixture
  is read always; `test_the_fixture_covers_every_read_and_nothing_else` ties it to the reads
  in the file, and a constant missing from the fixture raises KeyError naming the regeneration
  (`test_a_constant_missing_from_the_fixture_fails_naming_the_regeneration`), and, when the reference checkout is present (`SWI3S_TIMING_ANALYSIS` names it),
  `test_the_fixture_matches_the_live_reference` fails naming each value that differs, and each public function the reference has gained or dropped against the fixture (it skips
  only where the checkout has no swtiming directory, and is the only test in the file that skips; a checkout whose `swtiming.emit_ede` does not import fails it, `test_a_reference_checkout_that_does_not_import_fails_rather_than_skips`, whose ImportError names the checkout's path and the import's own error). The fixture is rewritten
  only by hand, with `PYTHONPATH=. python3 tests/test_timing_vs_reference.py --regenerate`,
  which refuses a reference checkout with uncommitted changes under `swtiming` unless
  `--allow-dirty` is given, and a fixture may record as uncommitted only files the module does not load (`_UNLOADED_DIRTY`), after its difference has been read. Three traps: the two models
  name legs by OPPOSITE conventions (the reference's `setup_mp` is this project's
  `PM_setup_ho`); each reference leg carries its own corner in its default arguments rather
  than a global one; and where the reference HARDCODES a worst corner by omitting a term, this
  project shows the term and lets the search find it, so comparing at the nominal invents a
  divergence.
- `test_timing_timeline.py` — per-leg timelines (also the linear ramp's rail clamp): every term of every leg has one span
  as long as its value and the margin span is the margin, over specs, shapes, slews, handover
  UIs, placements and launch modes; every crossing and detection lies on its ramp at its
  threshold and every anchor and clock reference at its ramp's 20 % point; each threshold is
  the receiver's level plus the ground offset, P→M setup's two offsets opposite; the crossing
  corners reproduce the envelope's coefficients; each term names the ramps it is drawn with, and an end on a ramp is on one of them,
  and a launch is drawn at its launcher's pin, from its clock reference to its data's anchor;
  a flight mismatch is the clock's and the data's flights from the launch, a window both
  signals at the receiver beside the margin; one device has one supply in a leg; a clock edge
  Peripherals A and B both detect has one t_RF at both, the calculator's edge factor, and
  detections of two edges take their corners' own; a handover's launching edge is of the
  polarity its count gives, and on P→P hold A's clock ramp and threshold are that polarity's
  early corner, V_IH rising and V_IL falling; every leg has a timeline, over EDE on either side, handover UIs 0/1/2, SoundWire on either side and both shapes; a leg that does not apply (a forced keeper's keeper_Man) is refused with a ValueError naming the reason (`test_a_leg_that_does_not_apply_has_no_timeline`); a release is at its ramp's off_ns and an EDE Delay starts at its ramp's 80 % point, where the drawn ramp reaches V_OH / V_OL on either shape; the to-V_OH span of an EDE release, on a contention or a keeper leg, is as long as its term, the closed-form crossing on an exponential ramp at 0.10 / 0.90 and asymmetric pairs, on the polarity the term is priced at, which its symbol names (`,rise` / `,fall`) where the two polarities' crossings differ; a t_ZD launch leaves high-Z at the launcher's pin and a t_DD launch does not; a contention leg is drawn at the releaser's pin (M→P) or co-located (P→M, P→P), at the closing polarity it reports; MP_contention's two flights are drawn, the second from the acquirer's 20 % point to the Manager, and P→M / P→P contention have none; a keeper's response starts at the data's crossing of the keeper's threshold at the Manager, V_IH,max / V_IL,min on a SWI3S Manager and V_TH_Keeper,max / V_TL_Keeper,min on a SoundWire one; every setup and hold leg is drawn at the
  sampling, launching and data polarities of the case it reports, P→M hold's data from its Sigma_cross,PM,hold case; a P→P data leg's clock ramps take the slew factors its gathered clock symbol names (`test_the_pp_clock_ramps_are_drawn_at_the_slew_factors_the_symbols_name`).
  A missing event or ramp key raises KeyError naming the leg and the key; the mismatch/window test asserts t_PD,mis on
  every MP setup/hold leg and a window on every setup/hold leg. On an edge of zero t_RF (an ideal step) every such event lies at the step, the ramp at its first rail before it and its far rail from it. A conventional contention releaser's last bit is held at the other level to the acquirer's first edge (high where that edge falls, as on a SoundWire side whose V_OH + V_OL is not 1). `LegTimeline.event` and `.ramp` raise a KeyError naming the leg and the key for a key the timeline does not hold.
- `test_timing_diagram.py` — the term diagram: a click on a term opens the pane and draws only
  that term from each column, and clearing its highlight clears it; a launch shows its
  launcher's clock and data and a crossing only its crossing ramp; a ramp named for its
  edge (`<dev>_t_RF,DATA,rise` / `,fall`) is drawn from its lane's row with the data ramp of
  that polarity; a repeated term
  gets a panel each and a SoundWire launch and its anchor correction share one; a flight
  mismatch is the two flights with no signal rows, and a row is named Clk or Data @ its pin;
  a column without the term says so; the ⤢ puts the columns side by side, and while maximized
  a choice draws either column alone or both, kept across restoring, which stacks both and
  hides it; a wide column's labels are the diagram's body size in demi-bold, a narrow one's
  smaller, and a pen's width scales with the line height; every term of
  every leg paints, and a click on every term of every leg draws the term in each column holding it and says 'not in this column' in each that does not (`test_a_click_on_every_term_of_every_leg_draws_a_panel`); that test also asserts that each spec selector took its value, and that Man_t_EDE and Per_t_EDE appear exactly where the pairing has them. A compute error closes the diagram and clears the highlight of the term it drew (`test_a_compute_error_clears_the_drawn_term_with_its_highlight`). Also pinned: a recompute with no term
  drawn builds no timeline, a click in another inequality redraws there, a contention or
  keeper term is drawn in every column, a high-Z stretch is dotted, and a short margin is red (overlap / short), a percentage reads to a tenth, the rail label
  sits away from the resting edge, chained spans sit on separate dimension lines, labels are
  subscripted, and a shown diagram follows an input change. A chained panel's heading total equals the spans' unsigned lengths combined with the operators shown, and is negative (U+2212) where the span against the lead is longer; the click-on-every-term test clicks each term of every equation row, a column's own row where the columns differ, and draws it in each column that holds it. A chained heading with a zero lead span takes its direction from the first non-zero span (`test_a_heading_with_a_zero_lead_span_reads_along_the_first_non_zero_span`), and a margin guide's value uses the true minus and no sign at zero (`test_a_margin_guide_value_uses_the_true_minus_and_no_sign_at_zero`).
- `test_timing_view_settings.py` — the Example column's Manager launch rows (Man_t_DD/ZD/DZ) keep edited values across an
  F_CLK change, a to_dict/set_from_dict round trip and Load Example, analog and 4x; an off-grid clocked value that is
  restored is snapped and reported; a saved selector choice that is not a string is reported, not passed to Qt, the selector keeps what it held and the file's other keys are still applied; a launch-mode change reseeds. The view-level F_max: a clocked F_max is where the
  shown margin reaches zero, at a placement step it passes just below the rate and fails just above it, and a hold failing at the target reads its
  first failure. A clocked launch box capped at 10000 ns at a very low F_CLK returns to its placement when the rate
  returns (`test_a_launch_row_keeps_its_ui_fraction_through_a_very_low_rate`). Load Example refuses a JSON list with a
  message naming the expected mapping and leaves the margins unchanged, reads a file with a byte-order mark and a plain YAML mapping (`test_load_example_reads_a_plain_yaml_mapping`), and refuses a YAML alias and a file over 1 MiB (`test_load_example_refuses_yaml_that_expands_or_is_oversized`). A rate step that raises a clocked launch row to its floor is undone by the step back to the earlier rate (`test_a_rate_step_that_raises_a_launch_row_to_its_floor_is_undone_by_the_step_back`). An
  F_max typed at the rate box's 3-decimal precision leaves a margin within 0.005 ns of zero. Typed edits are debounced: a burst
  (five single-step edits, each a real change, plus a rate edit) costs one recompute, a discrete action recomputes at once and drops the pending one, `flush_recompute` (and so
  `summary_text`) shows an edit's margins, and closing or deleting the view fires nothing pending (the delete test asserts that no recompute ran).
- `test_timing_fmax_rate.py` — F_max on legs whose terms scale with the UI. A clocked Manager
  t_DD / t_ZD sits at a fixed fraction of the UI, so the MP setup legs' F_max, evaluated at
  that rate with the placement held, gives a zero margin; under 2x it agrees with
  `launch_mode_max_fclk_MHz`. With the Timing Calculator's floors (`CalcInputs.Man_t_DD_floor_ns` and its siblings) each clocked row is re-placed at every rate, raised to a further grid point where its fraction falls below its tabulated minimum. F_max is the first failure as the rate rises: each leg passes just below its F_max and fails just above it, with a zero margin there unless a placement step takes it through zero, and a leg that fails, passes again and fails again across placement steps names its later passing windows in its F_max note (4x MP_hold on the Example column at 12.288 MHz is the binding rate and passes again at a higher one; that note, the example `_fmax_from_windows` quotes, is pinned in `test_timing_view_settings.py`). This is checked by hand in `test_timing_fmax_rate.py` and through the view's own recompute in `test_timing_view_settings.py`. `test_timing_fmax_rate.py` pins the note's forms on synthetic windows: up to three later windows each named, more counted and spanned, an open last window reading "MHz up" in the span, and the "passes in N windows" form after an F_max of 0.0. Every test in the file solves without the toggle ceiling, through the `no_toggle_ceiling` fixture of `tests/conftest.py` (which `test_timing_cross_spec.py` also requests where it needs the solver's own answer); a test of the F_max solver requests it, or the window clips at the ceiling. It also covers the zero rate bound of a failing rate-growing leg and the rate independence of EDE keeper legs. An EDE release (referenced to the closing edge, on either side,
  its launch scaling with the UI under a clocked Manager launch) and a clocked Manager t_DZ /
  t_ZD enter the contention legs' F_max, so a zero-UI EDE deficit of a Peripheral release is closed by a slower clock and reads the
  first failure below the target (`test_a_zero_ui_ede_deficit_is_closed_by_a_slower_clock`); only a
  leg that also fails at every lower rate reads 0.0, with a note naming the rate from which it passes. Also pins that the MP_setup / MP_hold Man_t_DD terms, under a clocked launch, carry the
  snapped value and the grid note, and that a SoundWire Peripheral's min corner starts its ramp
  at SoundWire's own low-impedance instant at every data slew. Hold, handover-hold and keeper legs with a UI-scaled part (a clocked
  Man_t_DD / t_ZD / t_DZ, N_HO·UI, a conventional release's closing UI) report the rate where
  the margin reaches zero, just above it the leg fails, just below it passes; 4x MP_hold
  agrees with `launch_mode_max_fclk_MHz`. `F_max_binding_MHz` reads every bounding leg: at
  0.999 of it every bounding leg passes (cases bound by PM_contention under a peripheral EDE
  release, MP_hold under 4x, and keeper_Man), and a leg failing where its margin grows with
  the rate pins it to 0.0. A clocked Manager row whose nearest grid point is below its floor is placed at the lowest grid point at or above
  the floor at the target rate as well, with a 'raised to' note, and the target lies inside its passing rates
  (`test_a_clocked_row_below_its_floor_is_placed_at_the_target_as_at_every_rate`, t_DD, t_ZD and t_DZ). The floored
  parametrisation holds every setup, contention and keeper leg in both modes
  (`test_the_floored_cases_hold_every_rate_bounded_leg_in_both_modes`). The 2x setup and 4x hold hints give a zero margin at
  their own rate. A leg failing where its margin grows with the rate binds at zero, clocked and EDE releases
  (`test_a_leg_failing_where_its_margin_grows_with_the_rate_binds_at_zero`). The window helpers are pinned directly: the
  note names up to three later windows and counts more; a piece with B = 0 passes by its slope's sign; a zero-UI margin
  passes by its sign; a floored leg failing at about 3.6 GHz or 9 GHz reports that rate under the 10 GHz search cap (`_FLOORED_FMAX_CAP_MHZ`); and a floored leg passing at the cap, whether its first failure lies above it or its floor exceeds its deficit, reads F_max = inf (`test_a_floored_leg_passing_up_to_the_search_cap_reads_inf`). A floored leg passes at every rate below its F_max, checked at both ends of each span between the floored rows' placement steps, where the margin is A·UI + B (`test_a_floored_clocked_fmax_is_the_first_rate_the_re_placed_leg_fails`).
- `test_visualizer_engine.py` — **the authoritative Bus-Visualizer parity test.** Bus-Visualizer
  mode is driven by the *Visualizer engine* (`swi3s_studio/swviz/`, via
  `model/viz_engine.py`). For all **96** example configs this builds the merged `BusModel`
  through that engine and asserts the serialized model equals the Visualizer's golden JSON
  (`tests/visualizer/golden_json/`) — **bits + bus_clashes + device_clashes + read_overlaps +
  warnings**. This is the Visualizer's own testsuite, ported, and is the source of truth for
  placement/clash/validation parity.
  A build masks EnableCh_REG to 16 bits for placement only: the caller's value is restored and the width error is reported on every build (and the placed channels are asserted); a build leaves every register, setting and port enable as given; a port on device 11 is drawn and not reported unplaced; the PHY3 slot-layout error starts below the columns the slots need; a TX-controlled port expects its TxP bit in every channel sample; a wide DRQ past the row end is reported only for a drawn port. The PHY3 slot-layout error starts one column below the literal column count each of five explicit configurations needs (`test_the_phy3_slot_layout_error_starts_below_the_columns_the_slots_need`).
- `test_visualizer_placement.py` — a fast first-line placement check: for all 90 configs it
  solo-places each data port through `grid_from_csv` and asserts `(dp,row,col,slot)` matches the
  Visualizer's per-DP golden (excluding the reserved CDS column). Separately, the swviz engine
  alone (no C++ core, no golden) places a Source port's TailWidth_REG 1..3 Tail cells per data
  row, with and without a guard (`test_a_source_port_places_its_tail_width_of_tail_cells_per_row`).
- `test_grid_cross_engine.py` — asserts the two LIVE placement engines agree DIRECTLY:
  each solo data port placed through both the C++ core and the swviz authoring engine
  yields the same DP-owned cells. Runs the demo AND the **whole example corpus** (all
  flow modes) on DATA + TX_PRESENT — the sweep that catches a placement rule landing in
  one engine but not the other. Every enabled demo port as a solo Sink at BitWidth_REG 1 pins the declared
  difference: the core places every UI of a wide bit, swviz only the last
  (`test_a_sink_wide_bit_differs_by_declaration`). Plus
  a per-FlowMode spec check that TX_PRESENT is placed iff flow-controlled, in both engines
  (catches a bug identical in both, which equivalence can't). (The two are otherwise
  pinned only transitively via the shared goldens above.)
- `test_visualizer_roundtrip.py` — whole-corpus CSV round-trip: every example config,
  loaded → re-saved via `CSVHandler` → reloaded, must build a byte-identical model, with the same description and each data port's drawing fields; a second test checks every
  example is well-formed CSV.
- `test_authoring_render_golden.py` — characterization golden over all 96 example configs:
  pins `viz_engine.render_payload`'s issue list + clash cells + grid dims (the part the
  placement/model goldens don't cover), so a future placement-engine change can't silently
  shift the authoring warnings.
- `test_drq_clash.py` — the DRQ clash verdict, hand-built because **no vendored golden carries
  a DRQ collision**. A DRQ's direction is the inverse of its parent port's, so the engine's
  clash branch reads the bit's own direction, not `PortDirection_REG`. Two Sink ports
  driving one DRQ column must be a bus clash; two Source ports sampling it must be a read
  overlap.
- `test_grid_slots.py` — pins the shared `GridSlot` enum (model/grid_slots.py) to the C++
  `SLOT_NAMES` ordinals, so a C++ slot-enum reorder fails a test instead of mis-colouring
  the grid, and pins `viz_engine._SLOT_TO_GRID`: every swviz `SlotType` that can appear as a placed data or system cell maps to a `GridSlot`, and the non-cell kinds (EMPTY, CLASH) and CDS are absent (`test_slot_to_grid_covers_placement_slot_types`), and that each mapped kind lands on the GridSlot of its own name (the shared 1-6 are the C++ ordinals, S0/S1/HANDOVER are 7-9).

**The rest, by area** (each file's docstring has the detail)

*Decode and PHYs*
- `test_phy1_demo.py`, `test_phy3_demo.py` — the PHY1 (slow FBCSE, ports repositioned
  mid-capture) and PHY3 (DLV, recovered clock) demos round-trip; `test_phy_invariants.py` —
  what must hold for EVERY audio-mode PHY.
- `test_dlv_partial.py` — a mid-stream DLV capture with no bring-up is recognised, loaded at
  the true rate and its column count blind-detected: the width the capture starts at (a
  Safe-Lock-4 start is 4, though a decode forced to 16 is clean after the commit), so every
  command is read and the decode follows the commit; an operational-only slice is 16; a pair sampled too slowly for one sample per DLV UI is not decoded as DLV and is
  reported; a DLV TX-map region ends at the last recovered Row Sync Point. The DLV CDS column and the fewest samples per UI are the core's (`swi3score.dlv_cds_column`, `swi3score.dlv_min_ui_samples`) (`test_dlv_detect_repeats_the_cores_dlv_constants`); a nan, inf or too-large time cell is left out of the rate (`test_a_non_finite_or_huge_time_is_left_out_of_the_rate`); a tie between widths goes to the narrowest (`test_a_tie_between_widths_goes_to_the_narrowest`); edge counts differing past 0.5 % of the larger are not a pair (`test_a_pair_whose_edge_counts_differ_past_the_tolerance_is_not_complementary`); edges that sit on another sample past 0.5 % are not a pair (`test_a_pair_whose_edges_differ_in_place_past_the_tolerance_is_not_complementary`); the rate reads the header's Time unit (`test_the_rate_reads_the_header_time_unit`) and stops at the first blank row (`test_the_rate_is_read_up_to_the_first_blank_row`); a rate is snapped only within 2 % of a standard one (`test_a_rate_is_snapped_only_within_two_percent_of_a_standard_one`); a spacing under 500 Hz names no rate (`test_a_spacing_under_500_hz_names_no_rate`); the blind detection is not written into the source descriptor. Detection needs both min_valid CRC-valid commands and the valid ratio (`test_detection_needs_both_the_valid_count_and_the_valid_ratio`). A CSV cell past the csv field limit is a ValueError naming the file (`test_a_csv_cell_past_the_field_limit_is_a_value_error_naming_the_file`). The DLV row rate has one definition, shared by the detector and the ingest (`test_the_dlv_row_rate_has_one_definition`).
- `test_flow_control_demo.py` — the four-mode flow-control demo round-trips bit-exact, a wide DRQ bit and one cut short by the row edge are read on their last UI, the mid-phase SPM framing lock, and FCP_ScramblerEn: 0x0E bit 3 decodes into the config, a config CSV keeps its reset value (1), and the `fcp_scrambled` demo's DRQ bits descramble with a per-port LFSR that persists across intervals and SSPAs. Each flow-controlled sample carries the position of the DRQ that permitted it; the exact unset set is the first sample of device 2 channels 7 and 8, and none on the ASYNC port (`test_flow_drq_sample_annotated_on_samples`).
- `test_payload_skipping.py`, `test_demo_skipping.py` — Section 14.1.10 interval skipping
  through the decode (44.1 kHz on 48 kHz intervals).
- `test_sspa_demo.py`, `test_sspa_ssp_lock.py`, `test_ssp.py` — periodic SSPAs (every audio region holds an SSPA, compared with the audio regions), the SSPA-derived SSP lock applied backwards for partial captures (every 512-sample window of each half checked: the decoder is locked by sample 160, so a window from there is clean, and the first half is checked from sample 192), and the manual SSP row. The GUI's set/step/clear SSP controls are exercised from a mid-capture cursor row (>= 3), not the capture start.
- `test_config_csv.py` — a decode-driving config CSV: the data-port config it supplies is
  applied from row 0, persisted in the workspace `source` and reloaded, and threaded through
  Open Capture and Decode ▸ Import Visualizer CSV.
- `test_csv_handler_missing_fields.py` — a Visualizer CSV the app saved loads with no
  missing fields: the scalar CDS_GuardEnabled_REG, CDS_GuardPolarity_REG and
  CDS_TailWidth_REG are derived from the per-source rows, and a scalar is reported missing
  only when the per-source row it is derived from is absent too.
- `test_partial_capture_csv.py` — join-late captures whose geometry comes from a CSV.
- `test_forced_columns.py` — region-scoped forced column counts, and the stale Safe-Lock seed. A config CSV import decodes at the CSV's width (with and without a cold start), draws the grid at the CSV's columns, keeps every command at that width decodable, and clears a stale Safe-Lock seed, while a cold start without a CSV still seeds it and a DLV decode forces its own recovered width. Force Column Count pins only the cursor's region, reports the pin back, removes it at 0, rejects a width outside 2..32, and is a no-op when repinned to the same width; Clear Forced Column Sections restores the wire geometry. A pin survives a workspace round trip, applies to an imported CSV region, labels its timeline band '(forced)' with an outline confined to the pinned region, and is reached through the Analyzer menu action.
- `test_decode_memory_watchdog.py` — a decode stopped when free memory falls below the floor
  (early, with a message saying what to do), the audio copy sized before it is made, a
  stopped re-decode keeping the previous decode, and no watchdog when memory is unreadable.
- `test_region_csv_import.py` — a config CSV imported into one region of the 2 → 8 → 16 column
  demo changes that region's ports and nothing else (other regions, every command), draws
  its grid and Register Map from the CSV under any what-if, reopens from the source and the
  workspace (relative, and Locate-able), reaches a first region with no config on the wire,
  and is the route the window takes for a multi-region capture.
- `test_spacing_row_boundary.py`, `test_transport_slot_budget.py` — channel-group spacing
  does not cross a row; every interval carries channels x samples x bits slots.
- `test_devices.py` — hub-depth response delay and per-device names / register maps / depths; a hub depth outside 0..MAX_HUB_DEPTH is clamped to it and reported, naming the device and the value. A saved workspace reads only hub depths in 0..5 for devices 0..11 and reports how many it dropped, with the two rules ('3 hub depths not read: the device is not 0..11 or the value is not 0..5').
- `test_dp_number.py` — the per-slot logical data-port number through model, CSV and core.
- `test_errors.py` — per-command error classification, and the running-port write message in its three forms (a de-duplicated SkippingNumerator, the fallback text, below DP_BASE, a two-port list), and the device-mask cardinality by phase, CalibratePhy included. A running-port write covering several control registers names all of them (the multi-register `one of ...` message), and a write to a read-only register is a programming error (`read_only_write_error` gives 'Programming error: write to a read-only register'). A command with `header_valid` false is labelled 'header unreadable', not an empty device mask. The device mask counts devices 0..11 (0x800 is device 11, 0xFFF is twelve, 0x1000 is none) (`test_the_device_mask_counts_every_device_from_0_to_11`).
- `test_port_status.py` — peripheral-reported status registers and error counters: a calibration request is a lifecycle event, COUNT_ counters decode, the highest counter value is kept, and the label folds state beyond 4 fields.

*Ingest and export*
- `test_analog_import.py` — `.wfm` and analog-CSV import, sub-capture locate, `.sal` export
  and the progress hook, all on synthetic files. `capture_from_analog` takes time, clock and data arrays of one length and refuses any other combination. An analog CSV with a byte-order mark reads as one without (marked `platform`), a first line over the line limit is not an analog CSV, and a preamble cell over the csv field limit is no header row. A given threshold pair that is not two finite numbers with high at least low is refused by field name (`test_a_threshold_pair_that_is_not_two_finite_numbers_high_over_low_is_refused`); a pair no sample reaches is reported (`test_a_threshold_pair_that_no_sample_reaches_is_reported`); a flat channel is not reported as never crossing (`test_a_flat_channel_is_not_reported_as_never_crossing`); a channel holding only noise (a swing under 1 microvolt) is flat and takes the fallback thresholds, not its own band (`test_a_channel_that_holds_only_noise_is_flat_not_thresholded_on_its_own_band`). The `Sample Interval` of its fixtures equals the TIME spacing. A repeated header name is numbered ('CH1 (2)') and a blank one reads 'Column N', with two import notes and channel_names in agreement (`test_duplicate_and_blank_header_names_keep_every_column_under_unique_names`); a header of unique names makes no header note (`test_a_header_of_unique_names_makes_no_header_note`); `from_analog_csv` selects a repeated name by position (`test_from_analog_csv_selects_a_repeated_name_by_position`); a threshold pair with high equal to low is accepted (`test_a_threshold_pair_with_high_equal_to_low_is_accepted`). The csv-field-limit test sets and restores `csv.field_size_limit` itself, and its over-long first-line case is a numeric analog row. A CSV with no header names the file in its error.
- `test_vcd.py` — VCD import: the decode matches the demo, `$timescale` sets the rate, only
  1-bit signals are offered, the auto-clock choice, `$dumpoff`, `$comment` and truncated
  time stamps, several changes at one time stamp, a malformed time stamp, an unterminated
  comment, workspace round trip and the bounded edge counts.
  A dump on one line is read in steps, a token across a read step, an over-long token, a timescale's rate is its reciprocal rounded to a whole hertz (1500 ms gives 1 Hz, 1 fs gives 1e15 Hz) (`test_a_timescale_gives_its_reciprocal_rounded_to_a_whole_hertz`), a timescale whose rate rounds to under 1 Hz (2 s, 3 s, 10 s, 100 s) refused, long and non-ASCII time stamps, the 2**63 time stamp boundary (2**63-1 read, 2**63 and above refused: `test_a_time_stamp_of_2_pow_63_minus_1_is_read`, `test_a_time_stamp_of_2_pow_63_or_more_is_refused`), data-signal same-time collapse. Three changes in one time step keep the edge before it (`test_three_changes_in_one_time_step_keep_the_edge_before_it`); the busier signal takes its initial level with it as the clock, and equal counts keep the clock as given (`test_the_busier_signal_takes_its_initial_level_with_it_as_the_clock`, `test_equal_counts_keep_the_clock_as_given`); a real change whose ident starts with a level digit is not a scalar change (`test_a_real_change_whose_ident_starts_with_a_level_digit_is_not_a_scalar_change`); an ident declared twice keeps its first name (`test_an_ident_declared_twice_keeps_its_first_name`); a signal has the name and bit select without its scope (`test_a_signal_has_the_name_and_bit_select_without_its_scope`); x values after a $dumpoff block are reported again (`test_x_values_after_a_dumpoff_block_are_reported_again`); a bounded count stops after the given number of changes, and the edge budget is checked every so many edges and refuses only when exceeded (`test_a_bounded_count_stops_after_the_given_number_of_changes`, `test_the_edge_budget_is_checked_every_so_many_edges_and_refuses_only_when_exceeded`); a clock signal that is also the data signal is refused by file (`test_a_clock_signal_that_is_also_the_data_signal_is_refused_by_file`). Every fixture is written as UTF-8. A one-bit signal dumped as a binary vector (`b1 !`) is read as that level (`test_a_one_bit_vector_value_change_is_read_as_that_level`), an unknown vector value is reported as x/z (`test_a_one_bit_vector_unknown_value_is_reported_as_x_z`), and a vector or real change of a chosen signal that cannot be read is reported, not dropped (`test_a_vector_or_real_change_of_a_watched_signal_is_reported_not_dropped`). The one-line dump test spies on the read calls: many, each at most `_READ_CHARS`. The bounded-count tests call `transition_counts_until`. A `$timescale` with no `$end` reads a few words and names the missing `$end` (`test_a_timescale_with_no_end_reads_a_few_words_and_names_the_missing_end`); a long unterminated one is refused with a short message (`test_a_long_unterminated_timescale_is_refused_with_a_short_message`), and a long timescale text is cut in the error (`test_a_long_timescale_text_is_cut_in_the_error`). A `$var` with no `$end` holds a bounded declaration (`test_a_var_with_no_end_holds_a_bounded_declaration`), and a bit-select `$var` is still a declaration (`test_a_bit_select_var_is_still_a_declaration`). A dump with no `$enddefinitions` says the header is unterminated (`test_a_dump_with_no_enddefinitions_says_the_header_is_unterminated`), and one with it makes no header note (`test_a_dump_with_its_enddefinitions_makes_no_header_note`). `test_truncated_time_token_survives` asserts its import note.
- `test_sal_export.py`, `test_raw_export.py` — `.sal`, per-channel `.bin` and digital-CSV
  exports reconstruct the same Capture; a digital-CSV export refuses two edges of one line at one sample, writes no file, and writes the data alone cleanly (`test_csv_export_refuses_two_edges_of_one_line_at_one_sample`). The same-channel .bin export raises ValueError naming the channel. A raw capture export that fails keeps the previous files (`test_a_failed_raw_export_keeps_the_previous_files`).
- `test_sal_large_capture.py` — cost estimation, refuse-before-OOM and the windowed load, and
  that a whole-channel v3 decode allocates the output plus one block (tracemalloc), which
  is what the estimate's whole-file transient of 2 assumes. The capture span comes from the
  block headers alone: no delta decoder runs, and the walk of both channels peaks below half
  of one channel's blob (tracemalloc). A window is priced by
  `saleae_sal.SalWindowModel` (the edges the blocks the window overlaps can hold, at most one
  per delta-run byte and one per sample, × 2, plus one chunk's decode and a fixed reader
  allowance), which is why the window-budget test uses a 400k-edge fixture.
- `test_ingest_rate_inference.py` — a digital CSV or v0 `.bin` stores times, not a rate: the
  rate is the grid every timestamp sits on (`ingest/timebase.py`), so an export at 500 MHz
  reopens at 500 MHz edge for edge, timestamps printed to the ns keep their rate, and the
  probe, the scan and the loader agree; times on no grid give 1/finest spacing to 4 significant
  figures, at least 1 Hz (fewer than two distinct times give no rate). The probe and the loader take the rate from the same first rows (`test_the_probe_and_the_loader_take_the_rate_from_the_same_first_rows`); a pair is DLV only when nearly every row is opposite (`test_a_pair_is_dlv_only_when_nearly_every_row_is_opposite`); the memory prediction prices the join when the rows outweigh a batch (`test_the_memory_prediction_prices_the_join_when_the_rows_outweigh_a_batch`); the row estimate counts each row's newline (`test_the_row_estimate_counts_each_rows_newline`); a time out of range names the first one that is (`test_a_time_out_of_range_names_the_first_one_that_is`). A subnormal spacing gives a rate above the limit, not an overflow, in the probe, the scan and the loader (`test_a_subnormal_spacing_gives_a_rate_above_the_limit_not_an_overflow`); a time whose product with the rate overflows raises the range error quietly (`test_a_time_whose_product_with_the_rate_overflows_raises_the_range_error_quietly`). The DLV pair test pins 0.99 (True) and 0.985 (False).
- `test_ingest_memory_guards.py` — each loader's guard figure, measured with tracemalloc
  against the peak it reaches (digital CSV ascending and unsorted, analog CSV, `.wfm` pair,
  `.bin` pair (a non-v0 file charged its size), VCD, a window of a one-block `.sal` channel),
  the probe showing the same figure, refusal before reading over budget, and a `.sal` window
  the probe calls fitting being one the loader admits, a `.wfm` pair priced once before
  either curve is read, a busy window of a mostly idle `.sal` priced by its blocks with the
  probe agreeing, and the `.sal` block index never charging fewer bytes than the blocks a
  window overlaps; an analog time axis's NaN steps are not counted as positive.
- `test_ingest_v3_window_checks.py` — a windowed v3 decode, in memory or streamed from the
  ZIP, refuses a block whose delta code is too long for int64 or whose deltas do not sum to
  its header's span, as the whole-file decode does; the streamed decode reads big blocks in
  chunks and its output does not depend on the chunk size.
- `test_ingest_damaged_files.py` — truncated, partly written or damaged files: a `.sal`
  channel whose block chain does not walk is priced whole in every window by the loader's
  guard and the probe alike, and refused before it is inflated; every v3 reader searches the
  same `V3_CHAIN_START_SCAN` prefix for the chain start; a v0 `.bin` (or `.sal` v0 channel)
  that lost its tail, or whose count field is damaged, is refused as truncated, not as too
  large; an analog CSV's partly written last row is dropped, and any other unreadable row is
  refused by number and text; a multi-channel scope preamble is keyed per channel block.
  A time beyond the sample range, in a digital CSV, a bin, a v0 .sal or an analog source, is refused by file; a bin or v0 .sal begin time that is not finite is refused; a wfm curve offset below zero, a config CSV cell over the csv field limit and an analog CSV with a non-UTF-8 byte or an over-long cell are refused by file; a byte flip anywhere in an analog CSV loads or raises a ValueError; a long absolute time axis takes the preamble's interval and says so; a wfm with a bad byte-order or version tag is refused by file (`test_a_wfm_with_a_bad_byte_order_or_version_tag_is_refused_by_file`); a .sal channel that is not a Saleae binary is refused by channel (`test_a_sal_channel_that_is_not_a_saleae_binary_is_refused_by_channel`); a time axis that resolves its interval keeps the time column's rate (`test_a_time_axis_that_resolves_its_interval_keeps_the_time_columns_rate`). The `_v0_sal` helper takes a `name`. Digital CSV: a stray double quote costs its own row, not the rows after it (`test_a_stray_double_quote_costs_its_own_row_not_the_rows_after_it`); every line with an unmatched quote is counted and the first named (`test_every_line_with_an_unmatched_quote_is_counted_and_the_first_named`); a fully quoted row is still read (`test_a_fully_quoted_row_is_still_read`); a file without a header row is refused by name (`test_a_digital_csv_without_a_header_row_is_refused_by_name`); a bracketed unit in the Time header scales the times to seconds, and an unrecognised unit is read as seconds and reported (`test_the_time_header_unit_scales_the_time_column_to_seconds`, `test_a_time_header_unit_that_is_not_recognised_is_reported_and_read_as_seconds`); a nameless trailing header column is not one to choose (`test_a_nameless_trailing_column_is_not_a_column_to_choose`); the Open Capture probe reads a row shorter than the header as the loader does (`test_the_probe_reads_a_row_shorter_than_the_header_as_the_loader_does`); a row with cells past the header is reported (`test_a_digital_csv_row_with_cells_past_the_header_is_reported`); a file whose rows are all too short says so (`test_a_digital_csv_whose_rows_are_all_too_short_says_so`); an empty file is refused as empty, not as a column out of range (`test_an_empty_digital_csv_is_refused_as_empty_not_as_a_column_out_of_range`); and a byte-order mark reads as a file without one (`test_a_digital_csv_with_a_byte_order_mark_reads_as_one_without`). A line with an even number of quotes cannot swallow the lines after it (`test_a_line_with_an_even_number_of_quotes_cannot_swallow_the_lines_after_it`).
- `test_ingest_analog_classify.py` — telling an analog CSV from a digital one parses a
  bounded number of lines: the first rows in full, then lines sampled at spread byte
  offsets.
- `test_capture_export.py` — Export Capture: sub-range slicing, signal subsets, the dialog.
- `test_subcapture.py` — the sub-capture locator, sample-rate independent: the data edge on a clock-edge sample is read before it (the decoder's rule) and agrees with the same signal at 4x rate; hits exactly m apart collapse (tie, order); the exact-search cap and overlap; the exact search finds separated occurrences (`test_the_exact_search_path_finds_separated_occurrences`) and collapses overlapping hits to one match (`test_the_exact_search_path_collapses_overlapping_hits_to_one_match`); each diagnostics test forces its search path with `_CONVOLVE_BUDGET`: on the convolve path a damaged-tail sub reports the offset with the most agreeing symbols (0.92 at the slice origin) and a no-match sub reports its count peak (`test_diagnostics_locates_the_offset_with_the_most_agreeing_symbols`, `test_diagnostics_on_no_match_in_a_convolved_search_reports_the_count_peak`); on the exact path the best location is the longest intact head among the offsets sharing the sub's first 32 symbols (240 of 400), and a sub sharing no seed has none (`test_diagnostics_locates_the_longest_intact_head_on_the_exact_path`, `test_diagnostics_on_no_match_without_a_shared_seed_has_no_best_location`); the exact search stops at its hit limit and says it was cut, and the convolve path's hit list is capped the same way (`test_the_exact_search_stops_at_its_hit_limit_and_says_it_was_cut`, `test_a_constant_sub_in_a_convolved_search_is_capped_to_its_first_hits`); the low-entropy sub matches exactly (score 1.0) at a capped number of occurrences; a PHY3 (DLV) main or sub capture is refused with a ValueError, since its data line only alternates: a DLV main with a DLV sub, a normal main with a DLV sub ('sub capture is a PHY3') and a DLV main with a normal sub ('main capture is a PHY3') (`test_a_dlv_capture_is_refused_not_matched_everywhere`); a sub as long as the main; back-to-back occurrences and end_sample; max_matches keeps the best-scoring and reports them in start_sample order (`test_a_capped_result_is_reported_in_time_order_not_score_order`); the score threshold is inclusive; the exact-search path.
- `test_regmap_import.py` — vendor register-map import and per-device decode: both XML
  shapes (bit lists; wide registers with a separate field list, split into bytes), from
  synthetic maps only; defaults shorter than their width (zeros above the written digits;
  unknown above a leading X); an inferred register width rounded up to a power of two (a
  short default or a top field below bit 24 gives a 32-bit register, with no alignment
  warning). A field with a huge bit index is skipped and reported, not allocated (`test_a_field_with_a_huge_bit_index_is_skipped_and_reported_not_allocated`); a lone bit and a later run of a base name keep their index in the name (`test_runs_of_one_bit_and_later_runs_of_a_base_keep_their_index_in_the_name`); a byte-order mark is stripped and the importer source holds none (`test_a_byte_order_mark_is_stripped_and_the_importer_source_holds_none`). A default of a million hex or binary digits imports, masked to the register, and a sign, a non-ASCII digit or a non-hex character reads as nothing known while X digits stay unknown (`test_a_very_long_default_imports_in_linear_time_masked_to_the_register`, `test_x_digits_are_unknown_and_other_non_digits_read_as_nothing_known`); a register-map file over 64 MiB is refused naming the file (`test_a_register_map_file_over_the_size_limit_is_refused_naming_it`). Registers sharing a name do not each take the fields of that name: the fields go to the first, and the repeat is reported (`test_registers_sharing_a_name_do_not_each_take_all_the_fields_of_that_name`); a name or access in a stored map that is not text is reported and read as `?` or blank (`test_a_name_or_access_that_is_not_text_in_a_stored_map_is_reported_not_kept`); a file of thousands of distinct oddities gives a few short warnings (`test_register_map_warnings_stay_short_for_a_file_of_distinct_oddities`).
- `test_register_history.py` — a register's history (Registers ▸ History…): each write and
  read of the byte, in order, and the attempts that did not land, each saying why; a deferred
  read's address-less ReadData (READ_FAILED, no answer) listed at the setup's address; a
  _CURR alias listing its _NEXT accesses and the Commits whose group (a data port's
  CommitGroupMemb as written) promotes them; a write to a read-only (Dual-CURR-RO) _CURR
  alias listed unapplied in both histories, with nothing for the Commit to promote.
  A write spanning a writable and a read-only byte marks only the read-only one. A write to a read-only register is listed unapplied, and a write to an RW1C register reads 'Write (cleared by write)'. A multi-device write is listed and not staged; a command starting on a bus-reset sample sees the reset state; a data port with CommitGroupMemb unwritten commits with group 0 only; a _CURR read is not in the _NEXT alias's history while a _CURR write is (`test_a_write_selecting_several_devices_is_listed_and_not_staged`, `test_a_command_starting_on_a_bus_reset_sample_sees_only_what_followed_the_reset`, `test_a_data_port_not_written_to_commits_with_group_0_only`, `test_a_curr_read_is_not_in_the_next_aliases_history_but_a_curr_write_is`).
- `test_registers_commit_scope.py` — which devices a Commit promotes and what a bus reset
  clears, compared across the Registers pane (Session over the C++ register model), Register
  History and `model.registers.apply_commands`. Covers a Commit whose device mask leaves a
  device out, a mask-0 Commit, the per-device build keeping the C++ write and read rules and
  what-ifs on their device, a bus reset restoring CommitGroupMemb, dropping staged values and
  dropping a pending deferred read, a multicast WriteA32 ignored by all three, a _CURR-alias
  write setting _NEXT too in all three (and listed on the _NEXT row's History as "Write
  (CURR)"), and, on two Cold Start demos joined with the clock stopped, the Session listing
  the later Bus Reset and the Registers ▸ History… window (opened, and re-read after a
  re-decode) starting again at it.
  A command starting exactly at a bus reset sees the bus since it, and the grid replay since a bus reset keeps a write starting at the reset.
- `test_register_history_dialog.py` — the Registers ▸ History… window: the Failed attempts
  only filter and its shown-row to event mapping (synthetic RegisterEvents); in the window,
  the jump to the Row Sync Point, the jump following its Link by Session after a removal
  renumbers the Links, closing with its Link and on a new capture, a no-op jump from a
  removed Link, re-reading its accesses after a re-decode of its Link and not of another
  Link, set_events updating the summary and the filter, and following a theme switch.
  Offscreen Qt.
- `test_json_handler.py` — BusModel JSON round-trip keeps non-payload slots' None, the clash lists and which ports carry a
  truncation or validation warning; batch mode makes the directories; a file missing a field, one that is not JSON, and an
  unreadable path each raise; a structurally wrong bus model is a ValueError naming the field (bit-index range, slots shape, slot field types for device/dp/channel/sample/bit, slot/direction/clash names, the warnings object and its port lists, clash-list shape and row_rate, each with its message pinned); loading adds bits with clash tracking off because the clash lists come from the file (`test_loading_places_bits_without_the_per_bit_clash_scan`); an integer row_rate of 400 digits and a file of 100,000 opening brackets each raise ValueError, not OverflowError or RecursionError (`test_a_number_beyond_float_range_or_a_deeply_nested_file_is_a_value_error`); `save_bus_model` of a model with no rows or no columns raises ValueError and writes no file, so every saved model reloads (`test_a_model_the_loader_would_refuse_is_not_saved`). A save that fails, in serialization or in moving the file into place, leaves an existing file as it was with no temporary file beside it (`test_a_failed_save_leaves_the_existing_file_and_no_temporary_file`), and a save keeps the replaced file's mode (`test_a_save_keeps_the_mode_of_the_file_it_replaces`). The JSON export applies deferred bit removals, so a model with a deferred removal is saved without the removed bit (`test_saving_a_model_with_a_deferred_removal_omits_the_removed_bit`). The filesystem save tests (`test_a_failed_save_leaves_the_existing_file_and_no_temporary_file`, `test_a_save_replaces_an_existing_file`, `test_a_save_keeps_the_mode_of_the_file_it_replaces`) are marked `platform` (the mode test is skipped on Windows). A clash list the file omits is derived from the slots' clash flags, and a list it holds is read as written (`test_a_clash_list_the_file_omits_is_derived_from_the_slots_clash_flags`, `test_a_clash_list_the_file_holds_is_read_as_written`).

*Import warnings* (`swi3s_studio.import_notes`; each malformed or edge-case input gives the
expected warning naming the file, and a well-formed input gives none)
- `test_import_warnings_notes.py` — the collector: warnings in the order raised, an exact
  repeat dropped, nested collectors keeping their own, a collector belonging to the thread
  that opened it, and `ImportIssueWarning` through `warnings` with no collector open. An item replaced or removed in the middle of the list is re-indexed (`test_an_item_replaced_or_removed_in_the_middle_is_re_indexed`). `names_listed` lists three names, counts the rest, and cuts each name to the quote limit (`test_names_are_listed_a_few_at_a_time_and_cut_to_the_quote_limit`).
- `test_import_warnings_gui.py` — the Import warnings window: a load whose worker raised
  warnings opens one non-modal window titled with the file and listing each, with the count
  in the status bar and the list on the Session; a clean load opens none; each import gets
  its own window, deleted on close; a failed import propagates its exception and opens no warnings window.
- `test_import_warnings_captures.py` — the capture readers (`.vcd`, digital CSV, the DLV
  rate snap, analog CSV, the analog front end, a `.wfm` pair, a `.bin` pair, a `.sal`
  archive, a window cut before the capture): skipped rows and cells, times sorted, rounded
  or rebased, a rate assumed or snapped, thresholds defaulted, a window clamped; opening a
  malformed digital CSV through the window opens the Import warnings window. Times that are not finite are skipped and reported in a digital CSV, a `.bin` pair, a v0 `.sal` and an analog capture; a digital CSV rate coarser than its transitions reports the merged edges; a rate below 1 Hz is refused by file. A version-0 channel inside a .sal gets the order, grid, same-sample and begin-time checks of a .bin pair (`test_a_sal_v0_channel_reports_what_the_bin_loader_reports_for_the_same_times`, `test_a_sal_v0_window_cuts_a_channel_whose_times_were_out_of_order`, `test_a_sal_v0_reports_transitions_on_one_sample`). The v0 `.sal` test compares its notes with the `.bin` loader's, and the window-after-sort test swaps two times inside the window. A digital or analog CSV cell a note quotes is quoted whole up to 60 characters and as its first 59 and an ellipsis past that, as the native config CSV loader quotes one (`test_a_csv_cell_in_a_note_is_quoted_to_sixty_characters`).
- `test_import_warnings_config_csv.py` — a Visualizer config CSV: the native loader
  (`swi3score.csv_warnings`: a port disabled for its DeviceNumber_REG, a value that does not
  fit its register field — interface, per-source CDS and per-port —, an EnableCh_REG wider
  than 16 channels, a DataPortNumber outside 0..31, an unreadable cell, hex and binary cells
  read as their values, a row rate with trailing text, a value past the last column), the legacy and
  v2.0 upgrade, `BusConfig`, the CSV handler through `viz_engine` and the CLI, the Session's
  construction, whole-capture and region imports (a plain re-decode reports nothing), and
  the Import warnings window; a config the app writes has no native warnings (`test_a_config_the_app_writes_has_no_native_warnings`); the native loader names the rows a file lacks on a synthetic CSV (`test_the_native_loader_names_the_rows_a_file_lacks`); text with no config
  field (empty, blank, or only AppVersion and Description) gives one note that every setting
  defaults, from `BusConfig` as from the upgrade. A CDS_TailWidth of 3 (Reserved) is
  kept and reported once, naming the source, by each reader of the per-source tail row —
  `BusConfig.from_csv`, the CSV handler (per-source and scalar `CDS_TailWidth_REG` rows) and
  the native loader (`swi3score.csv_warnings`, per-source and scalar rows), which words it
  character for character as `BusConfig` so one file reports it once — and a legacy `CDS Tail
  Width` of 3 carries to every source and, read through the native route and `BusConfig`, is
  reported once; widths 0..2 report nothing and 4 and above (or below 0) report only the
  register-width note (`test_bus_config_keeps_a_reserved_tail_width_and_reports_it_once`,
  `test_the_csv_handler_keeps_a_reserved_tail_width_and_reports_it_once`,
  `test_the_engine_side_report_names_a_reserved_tail_width_as_bus_config_words_it`,
  `test_the_native_loader_words_a_reserved_tail_width_as_bus_config_does`,
  `test_the_native_loader_reports_a_wide_tail_width_only_as_wide`,
  `test_a_legacy_reserved_tail_width_read_by_the_native_route_is_reported_once`). A legacy
  `CDS Guard Enabled` and `CDS Guard Polarity` reach every source in the upgraded rows,
  `BusConfig`, the CSV handler and the native expected registers; a polarity on a disabled
  guard is reported and written as Guard 0; a legacy file without guard rows upgrades with every
  source off, and the guard rows change nothing else in the upgraded file; a legacy `CDS/S0
  Handover Width` outside 0..3 is reported (`test_a_legacy_guard_and_polarity_are_carried_to_every_source`,
  `test_a_legacy_guard_reaches_the_native_expected_registers`,
  `test_a_legacy_polarity_on_a_disabled_guard_is_reported_and_written_as_guard_0`,
  `test_a_legacy_file_without_guard_rows_upgrades_as_before`,
  `test_a_legacy_handover_width_outside_0_to_3_is_reported`).
- `test_import_warnings_regmap.py` — register maps: both XML importers' reports, a stored map
  read from JSON and the spec map JSON, the bundled maps reading clean, and binding a map in
  the Devices dialog opening the window titled with the file (a cancelled dialog, none). The damaged spec map case also covers an inverted `[3:7]` and an out-of-byte `[9:8]` bit range (3 fields reported). An unrecognised Access is warned about and kept as written; access spellings and a blank access; a DOCTYPE is tolerated and malformed XML raises ValueError naming file and line. A DOCTYPE strip reads a hundred-thousand-declaration input of unterminated DOCTYPEs and has no time bound (`test_a_file_full_of_unterminated_doctypes_is_read_in_linear_time`). A register beyond 32 bits is skipped with a warning in both XML importers. A bit-list `<reset hex>` over a byte or negative is reported and read as no reset, and survives the JSON round trip. A field default wider than its field is reported. A stored map's huge bit index, reset or enum key is reported, not raised, and a huge `name[index]` is a plain name. Per-item warnings list three per kind and count the rest (`test_importers_list_a_few_warnings_of_a_kind_and_count_the_rest`). import_notes de-duplicates per nested collector (`test_a_note_raised_twice_is_collected_once_in_constant_time`). A mixed-access warning clips the access text it echoes (`test_a_mixed_access_warning_clips_the_access_text_it_echoes`).
- `test_import_warnings_workspace.py` — workspaces: keys, types and rows dropped and
  reported, a clean save and reopen and a format-3 file reporting nothing, a cursor that is
  not a sample opening at 0 and a mode that is not text opening in Analysis, each with one
  note, and reopening a workspace with invalid values opening one non-modal window titled
  with the workspace file. A saved authoring config's CDS tail width of 3 (Reserved) is kept
  and reported once, naming the source, for a per-source list and for the scalar key; 0..2
  report nothing and 4 and above report only the register-width note
  (`test_a_workspace_authoring_config_keeps_a_reserved_cds_tail_width_and_reports_it_once`).
  Bookmarks are read by their own form and range-checked (sample 0..2**63-1, index, Link < the Links present); scrambler rows are bounded to device -1..11 (-1 the Manager) and a 32-bit port; the inclusive limits of the sample, register-override and scrambler rows and of a register map's field bits (7:0) and 32-bit address; a workspace full of Infinity / huge view values opens with each bad entry skipped. Each window-wide entry is noted once: grid rows, TX start row, timeline height and range, the pane Links, Samples, Registers, Statistics, Timing and Audio. A Link's own Commands column widths are noted once per Link. Inclusive limits for the bookmark index (2**31-1), device 11 and the three-entry scrambler row. Row entries that are text or a fraction are dropped, and the rows kept are integers. A time offset outside the signed 64-bit range is 0, with a note, and one at the limit is kept. A format version that is not a whole number is reported. Notes about dropped cursors are full sentences. A saved show_clock, tx_map or tx_persist that is not true or false takes its default, with one note each (`test_saved_window_on_off_settings_read_only_as_true_or_false`). A device key that is not plain decimal text ('01', '+3', '1_0', ' 3', a non-ASCII digit, '-1', '1.0') is dropped with a note, and a device named twice in a file's map, or as a number and as text in memory, keeps the last with a note (`test_a_device_named_twice_keeps_the_last_with_a_note`, `test_a_device_named_as_a_number_and_as_text_keeps_the_last_with_a_note`). Any other setting named twice keeps the last with one note (`test_any_other_setting_named_twice_keeps_the_last_with_one_note`). A scrambler flag reads only as true or false. A nameless Link is named by its position ('Link 2'), and a note names a Link by position alone when its name is that position's default. The rows kept are integers and the scrambler flag a bool, whole-valued fractions included. A save through a symbolic link writes the linked file, keeps its permission bits and leaves a same-named .tmp file alone. An integer literal too long to read raises ValueError naming the file. A save refuses a workspace file that is not writable and leaves it (`test_a_save_refuses_a_read_only_workspace_and_leaves_it`). A new file gets the mode the umask gives and the umask is never changed (`test_a_new_workspace_gets_the_mode_the_umask_gives_and_the_umask_is_untouched`). A writable file in a read-only folder is written in place (`test_a_writable_workspace_in_a_read_only_folder_is_written_in_place`). These three are POSIX-only. A Link name that is not text or is blank reads as the position's default with one note (`test_a_link_name_that_is_not_text_or_is_blank_reads_as_the_default_with_a_note`). Links past MAX_WORKSPACE_LINKS (64) are not opened, with a note, and a saved active Link among them falls back to the first (`test_links_past_the_workspace_limit_are_not_opened_and_noted`, `test_the_workspace_link_limit_is_beyond_any_capture`). A save that fails while serializing leaves the previous workspace and no temporary file (`test_a_failed_save_leaves_the_previous_workspace`). A save that fails after its temporary file exists (chmod or replace) leaves the old file and no .tmp (`test_a_save_that_fails_after_its_temporary_file_exists_removes_it`). A save fsyncs before it replaces (`test_a_save_syncs_the_new_file_before_it_replaces_the_old`), and a file system that refuses fsync still saves (`test_a_file_system_that_refuses_to_sync_still_saves`). A new workspace in a read-only folder is refused and leaves nothing; POSIX-only (`test_a_new_workspace_in_a_read_only_folder_is_refused_and_leaves_nothing`). A workspace with a UTF-8 byte-order mark opens (`test_a_workspace_with_a_byte_order_mark_opens`). Unread workspace keys are listed three at a time and cut to the quote limit (`test_unread_keys_are_listed_three_at_a_time_and_cut_to_the_quote_limit`).
- `test_import_warnings_timing_settings.py` — Timing Calculator settings and Load Example:
  unknown keys, unknown choices and out-of-range numbers reported; a save and reload of
  every Manager spec choice, and a saved Example, reporting none; settings the model cannot evaluate (a threshold beyond the rail) show no margins and the reason, and warn; a clocked launch_mode saved with a SoundWire Manager warns that the analog launch is used.

*Analysis panes*
- `test_command_statistics.py` — the Statistics pane's expandable per-command groups.
- `test_demo_configs.py` — the demo layouts, and a cursor-aware config export.
- `test_tx_map.py` — the data-line transition raster and its Bus-Grid toggle; on a PHY3 (DLV) capture the raster and the
  persistence summary count rows by the recovered Row Sync Points in each region and light the columns the decoded grid
  occupies plus the CDS column, and a region whose next segment starts past the last clock edge is scanned to the capture end; an FBCSE region's raster lights no cell from the next region's first UI on (`test_tx_raster_stops_at_its_regions_end`).
- `test_grid_dp_colors.py`, `test_grid_label_fields.py` — grid colours key on the config slot;
  per-port cell-label fields, the same labels as the Visualizer's own for every field
  combination, and swatch clicks.
- `test_grid_scrambler.py` — the scrambler square: one per scrambled bit column, flush in its own top-left corner and uncovered, for single and wide bits; only the square's interior is sampled, at an exact 3x scale; the source, wide-bit and sink cases.
- `test_port_mode_encoding.py` — DPn_PortMode 0 Normal, 1 Reserved, 2 Test0 (every Channel, TxPresentCh and FCP DRQ bit raw 0), 3 Test1 (raw 1): the register map, the example configs `test_mode_zeros.csv` and `test_mode_ones.csv` carrying PortMode_REG 2 and 3, the swviz `PortMode` enum and `port_mode_bit`, and the native core carrying the mode on every DATA cell.
- `test_port_mode_encoding_gui.py` — DPn_PortMode in the widgets. The Bus Grid draws every channel bit of a PortMode 2 port as `T0` and of a PortMode 3 port as `T1`, for native and swviz cells (`test_grid_draws_every_channel_bit_at_the_test_mode_level`), and a Normal port's bits by weight (`test_grid_draws_normal_mode_bits_by_weight`). The Port Test Mode dialog writes 2 for Test0 and 3 for Test1; it opens on a different mode so the click must change the value (`test_authoring_dialog_writes_the_spec_value`). `test_authoring_dialog_reopens_with_the_stored_mode_highlighted` asserts the highlighted button per stored mode, with Reserved (1) shown as Normal.
- `test_grid_view_merge.py` — the Bus Grid merge rule and cell labels. Two adjacent cells merge only with the same slot, data port and device. Past those, a guard, tail, S0/S1 or handover ignores channel, sample and bit (`test_system_slots_ignore_channel_sample_and_bit_but_need_the_same_slot_port_and_device`, which also refuses another data port); a data-ish cell also needs the same channel and sample, and the same bit when the Bit field is shown. A data label shows its Sample/Channel/Bit fields, with T0 for a port in PortMode 2 (Test0) and T1 in PortMode 3 (Test1), and ' xN' for a merged run only when the Bit field is shown. TxP and DRQ labels are fixed, and a merged S0/S1 run reads 'S0 xN' / 'S1 xN'. The channel/sample and bit rules hold for data, TxPresent and DRQ cells alike. A clash X over a CDS column widened for its flags line spans that column's width (`test_a_clash_over_a_widened_cds_column_spans_the_columns_width`).
- `test_raw_view.py` — Raw Capture pane regressions, including a stopped line drawing its
  true level at every zoom and scroll (the zoomed-out edge reduction, on uint64 edges) and
  that reduction allocating by its slots, never a float64 copy of the window (tracemalloc).
- `test_sample_view_gui.py` — the Decoded-Samples window: bounded, extends at either end,
  drives the cursor.
- `test_eye_view.py` — the Timing (eye) view's colour-key toggles and its per-Link cache. The View Worst UI list ranks by the smaller of setup and hold, then by time, keeping the 500 smallest. The x ticks step by 1/2/5·10ⁿ from 0 to the end, and the count ticks are decades up to the one above the count (k/M labels).
- `test_link_restart.py` — a Link brought up again after its clock stopped (synthetic
  captures: demos joined with a stop between, the first shorter than the decoder's leading
  window). Finding them: every bring-up, three in a row, after a short idle (the stop being
  the Bus Reset itself), with the link control on the data line, in a capture that starts in
  audio mode, none anywhere; the later search taking the caller's validated first bring-up
  (searching from its audio start, or the capture's start when it has none), as the Session
  does; a first bring-up rejected as a phantom reopens the later search from the capture's start (`test_a_rejected_first_bring_up_reopens_the_later_search`); audio after a stop, and a bursty line, read as none; the gap
  search chunked; a detected Warm Start inheriting the PHY, a later Cold Start with an
  unrecognised PHY naming none. The decode: restarting at the second Cold Start as the
  second demo alone decodes, at the first audio edge after PhyStart; nothing (audio,
  commands, bit samples, the windowed bit-sample replay) decoded while the Link is inactive;
  a Warm Start putting unchanged ports on the new Column-0 phase; restart points merged,
  sorted and range-checked; a restart in the first UIs. DLV: the recovered clock locking
  again so the second part decodes as the demo alone (commands, segments, audio), rows
  numbered by their Row Syncs across the restart, nothing decoded while inactive, the
  stretch skipped and its samples mapping to the restart's UI, the windowed bit-sample
  replay following the restart (exact from the row the stop cut short on and from further back, on an analyzer
  clock 3% off nominal), the CDS symbols after it, a Warm Start keeping the registers, the
  Safe-Lock count under a config CSV; a bring-up of the other PHY family (FBCSE to DLV and
  back) followed by no restart. A paused clock's phantom Warm Start dropped by the decode.
  The registers reset at the Bus Reset (the restart row's section agreeing), a config CSV
  keeping its ports through a Cold Start, decode_capture decoding as Session does, and the
  window: Timeline labels and Bus Grid overlays for Cold and Warm restarts and a mid-stream
  capture's, and the Registers PHY block following the cursor. After a short idle the Link
  is inactive from its last audio-mode edge. A clock pause with no bring-up: in phase,
  decoded on as without it (every command, segment and audio sample); a pause with no CRC-valid command after it keeps the phase on any column of the row, at column steps 0, 2 and 4 (`test_a_clean_pause_with_no_command_after_it_keeps_the_phase`); out of phase, framed
  afresh and decoded as the joined demo alone; none found inside a start sequence.
- `test_register_filter.py` — the Registers pane's colour key as a filter by provenance
  (Cold Reset, Bus Write, Bus Read, CSV Import, Manual Edit): each category alone, Cold
  Reset off leaving only what something set, NEXT and CURR on their own provenance, a CSV
  compare as its own category, empty blocks hidden, the filter outliving a cursor-move
  rebuild, a block closed while filtering staying closed through one, opens and closes
  made while filtering given back, blocks kept by id across a title change, Peripheral
  Registers left closed, a filter that leaves nothing saying so, the entries as keyboard
  and accessible toggles (Space / Enter / Return, name, checked state, tooltip), and the
  workspace keeping the filter and the open blocks by id (titles still read). The Registers column widths are saved by title, put back over the defaults, kept through a rebuild, and bad entries are ignored (`test_a_workspace_keeps_the_column_widths_by_title`); with none saved they are `RegisterView.DEFAULT_WIDTHS`, 258/63/52/407 (`test_the_default_column_widths`).
- `test_audio_gap_render.py`, `test_audio_playhead.py` — no waveform drawn across undecoded
  gaps; the play-head sweep over streams of different lengths.
- `test_audio_flow_gaps.py` — transport gaps on flow-controlled (TX/RX/ASYNC) ports: the
  flow-control demo has none and its envelope is not split; a coin-jittered transport's
  declined opportunities are not gaps but a 30 s dropout is, in every flow mode; a NORMAL
  port keeps the lower-quartile rule; each run either side of a dropout is respaced on its
  own and the rate is measured over the runs; the default-tolerance gaps are computed once
  per channel, are read-only, and are what the waveform's connect mask reads.
- `test_audio_grouped_gaps.py` — transport gaps on a NORMAL port whose samples are grouped
  onto rows (SampleGrouping). The step to the next row is not a gap, for 2 and 8 samples per
  row and for rows several intervals apart; a dropout on such a port is a gap; with a
  reported rate, a mostly idle port is still gapped. Also checks `timing_is_irregular` and
  `transport_step` on a grouped port.
- `test_bookmarks.py`, `test_bookmarks_gui.py` — A1/A2 pairing and deltas; Ctrl+B, the
  Cursors pane, propagation and drag with edge snapping; the earliest incomplete group is the one paired, the `remove_at` tolerance boundary, and a JSON boolean is not a sample; each bookmark item is read by its own form, numeric fields with int() and other item forms skipped (`test_bookmarks_read_each_item_by_its_own_form`); a pair lists its members left to right whichever was placed first, and after a drag (`test_a_pair_lists_its_members_left_to_right_whichever_was_placed_first`).
- `test_locate_gui.py` — Locate Sub-Capture progress and teardown, the reference read off the GUI thread, and an
  unreadable reference reported.
- `test_copyable.py` — ⌘/Ctrl+C copies the read-only views as TSV; rows of two tree blocks
  sharing a row number are a line each, in tree order.
- `test_theme.py` — the dark/light palette and a live theme switch.
- `test_line_palette.py` — every line colour clears 3:1 on its theme's plot background;
  the line palette keeps the grid fills' hues and stays as distinct; panes read the palette
  at use, so a light launch and a live switch recolour waveforms and bookmark lines; the
  light theme's plot gridlines are at least as visible as the dark theme's.
- `test_bottom_all_links.py` — All Links in the bottom group: Capture, Audio, CDS and
  Samples lanes each bound to their own Link (capture, audio, symbol and sample windows,
  bookmarks, stream colours); one global time across the lanes (ranges, axis labels and
  Time columns moved by each Link's offset); seeks, drags, picks and touches in a lane act
  on its Link, and a pick does not re-window its own lane; one lane plays at a time; the
  Timing lanes (each its own Link's, in flavour colours, the single pane's measurement, a
  worst-UI jump to its Link); lanes of different lengths share one window and its ticks;
  the picker and menu entry; leaving, one Link left, and a middle Link's removal. A row clicked in Link 1's CDS or Samples lane while Commands shows Link 2 selects Link 2's command in effect at that instant through Commands' own model, with no Qt 'wrong model' warning (`test_a_lane_click_selects_the_command_in_effect_on_the_commands_link`).
- `test_probe.py` — what Open Capture's probe reports for each format before decoding
  (channels and edge counts, the rate the loader will use, the length, windowed or read
  whole, the cost), what it refuses (one `.wfm`, two captures, a config CSV), an analog CSV
  with no TIME header refused within its preamble's bounds, a digital CSV header ending in a
  comma counted as the loader reads it, and that every
  format decodes a window, records it, and reopens it from the workspace source.
  A VCD signal with no change counts 0 edges; a csv that is not a capture is refused with a ValueError naming the file. A digital CSV's duration is in seconds whatever its time unit: Time [s], [ms], [us], [ns] and [ps] each give the loaded capture's (`test_a_digital_csv_duration_is_in_seconds_whatever_its_time_unit`), and a byte-order mark probes as a file without one (`test_a_digital_csv_with_a_byte_order_mark_probes_as_one_without`).
- `test_open_capture_dialog.py` — the Open Capture dialog: defaults (busiest clock with a
  quiet data line, Link names continuing the open ones when adding), rows, what keeps Open
  greyed out, the window's live length and cost and the fitting default, the request it
  returns (names, pairs, window, offset, thresholds, auto-orientation), a `.bin` without a
  rate; Open Capture loading what it asked for (a named window, an added Link at its offset,
  analog channels and thresholds, cancel, a non-capture); Locate Sub-Capture's reference
  form and its search.
- `test_menus.py` — Mode first; each mode's menu bar and File items exactly; File survives
  mode switches (QMenu.clear() deletes borrowed actions); ⌘O / ⌘S on the current mode's
  pair and no key on two live actions; the Analyzer's menus hidden and disabled elsewhere;
  capture items wait for a capture, submenus and their items included, at the start and
  after a mode switch (enabling a menu re-enables its submenus), and with no capture only
  what opens files, sets preferences or arranges the window is offered; with one, every
  capture item is, but those with a rule of their own; Decode / Devices / Audio contents; Help's guide and
  About; no Add Link item in any menu of any mode, no Links menu, with its items where they went (View ▸ Links with its shortcuts,
  Align in Cursors, a Link's label menu acting on that Link).
  The User Guide is found in an installed layout and closes with the window; every menu is walked in every mode. In each mode, the Keyboard Shortcuts help lists every shortcut that works there: each reachable menu item's key, and each enabled pane key on a shown pane. The all-menu walk fails if it reaches no Quit item or fewer than 100 items, and leaves the window in the mode it was in.
- `test_pane_chrome.py` — no Capture/Audio axis titles; Audio's -1/0/+1 and zoomed-extreme
  Y labels survive pyqtgraph's culling with the grid on; every Link picker's chevron (and
  that the spec ships it); a tree section's arrow in its heading's colour, on the selection
  fill when selected; the Link-name column's width; View's way to the waveform
  preferences; no stale channel checkboxes after a rebind.
- `test_pane_tabs.py` — tabbed panes show their group's tabs in the title row, in the dock
  area's order with the front pane selected; the dock area's own tab bars take no height; a
  click on a tab brings its pane forward; a closed pane leaves the tabs, and a pane alone
  shows its title.
- `test_platform_metrics.py` — widgets sized from the platform's own metrics: a buttonless
  spin box keeps an edit field wide enough for its value under the Timing stylesheet, the Bus
  Visualizer's buttons are as wide as their labels (one width for the column), and bus grid
  text is centred vertically on its capitals.
- `test_screenshot_checks.py` — `tools/screenshots.py`'s fault checks on synthetic cases: a
  pane in the wrong theme's background, faint text and text not drawn at all, a button or line
  edit narrower than its text, a widget whose tone after a switch is not a started window's;
  a glyph button and a word-wrapped label are not clipped; render() removes its temporary settings directory and names an explicit QT_QPA_PLATFORM it does not use (the render test is parametrized over forced and native platforms, each with a differing, equal or unset QT_QPA_PLATFORM, asserting the note, the clearing, or no note). The contact sheet orders each state by its place in `_STATES` (`test_the_contact_sheet_orders_every_state`); an image the sheet cannot read and a sheet that cannot be written are findings (`test_a_contact_sheet_that_cannot_be_written_or_an_image_it_cannot_read_is_a_finding`); the render test restores the theme, application palette and stylesheet it changed.
- `test_grid_group.py` — the Bus Grid's own pane group: every way in to the grid (cursor,
  toolbar, TX map, scroll, raise, theme, compare, its menu actions) draws the grid's Link
  while another is active, and a layout saved before the split puts it on the bottom's.
- `test_waveform_prefs.py` — line colours resolve override → preference → default, per
  theme; settings persist and a malformed one falls back; the Preferences dialog (low-
  contrast flag, reset, swatch pick, cancel); the Audio pane's per-stream Color… reaching
  Audio, Capture and Samples for one Link and one theme, and its workspace round trip.

*Authoring model (swviz)*
- `test_validators.py` — the DataPort validator's flow-control-port row fit (direction-aware),
  wide-bit straddle (partial trailing group), range checks and TxPresent drive length, and
  `BusModelBuilder` building partial and oversized channel groups.
  A TxP UI counts in the wide-bit straddle check; an odd column count is an interface error unless PHY3 is enabled. The range checks also assert the lowest and highest valid value of EnableCh, BitWidth, TailWidth, FlowMode and PortMode give no error. A drawn port on a device number outside -1..11 gets only the DeviceNumber validation error; no interval-overflow, display-truncation, no-channels or sink-handover entry (`test_a_port_on_a_device_number_that_names_no_device_gets_only_the_device_error`).
- `test_clash_rules.py` — the clash detector's rules, slot by slot: a guard, tail, read or
  handover against each occupant already there, same device and different, in and outside
  the CDS; the clash record (where, between whom, same or different device), the summary and
  clear.
  A handover and a read agree in either order, and a read is never an overlap with a handover; a write by the same device removes its guard or tail from the slot.
- `test_swviz_interface.py` — the Interface model: an out-of-range or wrongly typed value is
  refused and the field keeps its value, boolean fields read ints and their spellings, a port
  moved between devices, an unknown port index refused, and the PHY3 CDS start after S1, its
  tail and its handover.
  An integer too large for a float is a ValueError; a bool is refused where a number is expected; the VizConfig holds one slot per interface port; a moved port's `_device` is the target device object; a NaN or infinite row rate is a ValueError naming the finite requirement; a Visualizer config saved with RowRate at `InterfaceRanges.MIN_ROW_RATE` / `MAX_ROW_RATE` loads in `swi3score.csv_warnings` without a RowRate warning, and one just below the minimum or just above the maximum is reported as outside 1..48000 kHz (`test_the_row_rate_limits_are_the_config_csv_readers`).
- `test_reference_model_clean.py` — the vendored reference model (`dataport.py`,
  `flow_control_port.py`) carries no `#` comments and keeps its docstrings.
- `test_delta_tpd_ground_shift.py` — ground-shift handling in the crossing envelope. The
  cancellation, exponential residue, unequal-t_RF, independent-oracle and rr-dominance checks
  run counted from the ramp's start and from the 20 % point; the independent oracle covers both
  launching-edge polarities and the default and below-mid-rail windows; ∂margin/∂α ≤ 0 holds over
  SWI3S and SoundWire on either side, both shapes, 0 and 2 handover UIs and δ 0 and 5 %; the worked cross case is
  14.0197 ns from the start and 10.5197 ns from the 20 % point. A threshold at or beyond the
  transmitter's rail is refused on both ramp shapes. The reported-PM_hold sweep detects rr and
  ff, covers a window below mid-rail, and must see the same-polarity case at α = 0. Which polarity pairing binds depends on the window (`test_which_polarity_pairing_binds_depends_on_the_window`); in a window symmetric about mid-rail each falling case mirrors its rising one (`test_each_falling_case_mirrors_its_rising_one_in_a_symmetric_window`); each default corner puts its supplies and bound at the end it names (`test_each_default_corner_puts_its_supplies_and_bound_at_the_end_it_names`). Σ_hold selects its case with each data edge's own offset: an offset on the selected case's data edge large enough to reverse the order flips the selection, and on the other edge it does not; `sigma_pm_hold_ns` returns the selected case's sum (`test_sigma_pm_hold_selects_its_case_with_each_data_edges_offset`).

*The app, the tree and the gate*
- `test_app_launch.py` — window activation on startup (the macOS no-menu-bar case).
- `test_app_entry.py` — the PyInstaller spec's entry script starts when run by path as `__main__` with no parent package (`--help` exits 0 before any Qt is built); marked `platform` (it starts a subprocess, as each OS's frozen app does).
- `test_singleton_pin.py` — on Python 3.11, None, True and False survive far more
  over-released references than their own counts (100000 empty item and index reads; 100000
  emits of a Python signal), as PySide's Linux builds release them, and they are pinned once
  and only below 3.12. Each case runs in its own interpreter, through the app's ui package.
- `test_exit_teardown.py` — quitting the app leaves no Python-owned QObject for PySide's
  sweep at interpreter exit, a tree reparented through a
  layout is deleted from its root, a running QThread is left alone, and Ctrl+C: from the
  terminal it quits the idle app (POSIX: Popen cannot send SIGINT on Windows); pressed
  before the event loop it quits once the loop runs; the close runs from the loop, never
  inside the callback it interrupted; a refused close leaves Ctrl+C asking again; and a
  second press ends a quit that hangs, as one after the loop does (POSIX: the default
  action). Closing the window quits the app with a parentless top-level window still
  open. Each case runs in its own interpreter.
- `test_cli.py` — CLI dispatch and the headless model dump (no Qt imported, exit codes, the
  batch JSON equal to the UI export); the launcher scripts forward their arguments; a config
  CSV that does not load exits 1 and writes no model; a wide DRQ truncated past the row end, with no bus clash, exits 2 and still writes the model (`test_a_truncated_drq_without_a_bus_clash_exits_two`).
  With argv[0] = .../__main__.py, program_name() is '<python> -m swi3s_studio'; `python -m swi3s_studio -o` exits 1 and prints a usage line that names the command, not __main__.py; RowsToDraw 5000 builds the ceiling's rows (1024) and says so on stderr; a RowsToDraw of 0 or below builds one row (`test_headless_builds_at_least_one_row_as_the_ui_export_does`); the UI's `_authored_rows` and the headless build give the same row count for RowsToDraw -5, 0, 1, 1024 and 5000 (`test_the_headless_and_ui_row_clamps_agree`); a CSV missing required fields has the handler's message on stderr once, in the error line (`test_a_csv_missing_required_fields_reports_them_once`); importing the package with swi3score blocked fails with the `pip install ./native` / `build_local.sh` message (`test_a_missing_decode_core_names_how_to_install_it`). Every subprocess is bounded by a timeout; --help names what each flag opens.
- `test_nputil.py` — dtype-safe `searchsorted` (the uint64 → float64 promotion stall), and
  `searchsorted_many` equal to numpy's and exact above float64 precision.
  A key below or above the dtype range gives 0 / arr.size on both sides even when the array holds 0 or the dtype maximum (uint64 and int64), for negative, +-inf, NaN and huge keys; a fractional key gives numpy's answer on both sides. A float key equal to the dtype maximum, one above it, the minimum and one below it find their exact count in the narrow integer dtypes (uint8/16/32, int8/16/32; `test_a_float_key_equal_to_the_dtype_maximum_finds_it_in_narrow_integer_arrays`); `searchsorted_many` with Python-int (object) keys beyond uint64 gives the size above and 0 below (`test_a_python_int_key_beyond_uint64_gives_the_size_in_searchsorted_many`).
- `test_native_build_config.py` — the native build finds Python's headers; native/pyproject.toml's minor version equals
  score_abi in bindings.cpp and session._REQUIRED_SCORE_ABI; native/ carries LICENSE.md byte for byte and names it
  (license, license-files). CMakeLists sets CMake 3.18 as its floor, stops on a missing interpreter with a message, and passes -O3 to every compiler but MSVC (`test_cmake_sets_its_floor_and_guards`); CMakeLists and build_local.sh compile the same sources, every .cpp of the core (`test_both_builds_compile_the_same_sources`).
- `test_native_input_hardening.py` — the decode core on inputs the app itself does not
  build. TransitionSampleSource edge arrays that are converted (uint32, a strided view, a
  list) decode like the contiguous uint64 original after the caller's objects are freed, and
  a contiguous uint64 array is held (one reference) rather than copied. No audio capacity is
  reserved when audio is not decoded, and the up-front reservation is capped at 64 MiB of
  records. A config CSV SampleSize_REG above 31 or negative decodes at (value & 0x1F) + 1
  bits, with audio() and audio_columns() agreeing. forced_column_count outside 2..32
  auto-detects. A PHY3 (DLV) decode reports total_uis and a progress in [0.95, 1]. A config
  CSV DeviceNumber_REG outside 0..11 drops that port from the audio decode, the grid and the
  register map alike; grid_from_registers ignores a write to a device outside 0..11; the
  forced_column_count bounds test runs on a mid-capture slice whose auto-detected count is
  neither 2 nor 32, so a clamp to either bound fails it.
  DlvSampleSource edge arrays are converted and held the same way. A port on a device outside 0..11 is dropped, not masked into another device: the grid and the audio lose exactly that port's stream and gain nothing. Edge arrays of a signed or floating-point dtype raise TypeError. The recovered clock ends where a UI spans under one sample, takes the cold-start count for an initial column count outside 2..32 (-1, 0, 1, 33 and the largest int are tried), relocks across a 2e10-sample stretch with no edge to the same decode, and ends when no rising edge remains; these run in a child process with a 60 s limit. total_uis read while a PHY3 run is in flight is the estimate after its 4 -> 16 column commit. A DLV UI of exactly one sample locks and one under it ends, seeded or guessed (`test_a_dlv_ui_of_one_sample_locks_and_one_under_it_ends`); a gap of up to 64 row periods past the predicted Sync1 is coasted row by row and a longer one relocked (`test_a_dlv_gap_of_up_to_64_row_periods_coasts_and_a_longer_one_relocks`); these two limit tests also run in the 60 s child process; the total_uis docstring is checked on its own (`test_total_uis_is_documented_as_a_refreshed_estimate`). Grid layouts hold rows to `max_grid_rows` and columns to 2..32: a force_columns or fallback_columns of 1, 0 or negative lays out the cold-start 2 columns (checked on the DLV layout) (`test_a_grid_layout_holds_its_rows_and_columns`). A bit-sample window that starts past the capture's last UI is empty, read in the bounded child process (`test_a_bit_sample_window_past_the_capture_is_empty`). The DLV child processes import the core under test, and the five child-process DLV tests (`test_a_dlv_clock_under_one_sample_per_ui_ends`, `test_a_dlv_gap_with_no_rising_edge_is_relocked_and_a_trailing_one_ends`, `test_a_dlv_ui_of_one_sample_locks_and_one_under_it_ends`, `test_a_dlv_gap_of_up_to_64_row_periods_coasts_and_a_longer_one_relocks`, `test_a_bit_sample_window_past_the_capture_is_empty`) are marked `platform`. A skipping ratio that needs more than the interval bound raises, make_skipping_levels being bounded to 4 * max_demo_samples intervals (`test_a_skipping_ratio_that_needs_more_than_the_interval_bound_raises`). `make_skipping_levels` refuses a numerator or denominator above 4095, the width of their 12-bit registers (`test_a_skipping_ratio_wider_than_its_registers_raises`). `make_enablech_curr_write_levels` refuses an interval above 4095, the width of the Interval register (`test_an_enablech_curr_interval_wider_than_its_register_raises`). `make_enablech_curr_write_levels` refuses a samples_per_channel x (interval + 1) above 3,072,000 Rows per phase, 48000 samples at a 64-row Interval (`test_an_enablech_curr_stream_longer_than_its_bound_raises`).
- `test_native_sample_rate.py` — the rate the core reports for a port is the rate its placement
  carries: for sub-row-interval configs the reference validator accepts, the samples the grid
  completes per row times the row rate; a transport wider than its window, and a skipping
  numerator at or above the denominator, report 0 Hz. A SampleGrouping_REG or Interval_REG at the largest int gives the rate those values write, scaled from the demo port's rate (`test_a_field_near_the_int_limit_gives_the_rate_it_writes`). A SampleGrouping_REG of -2 or the smallest int reports 0 Hz (`test_a_rate_that_is_not_positive_reads_as_no_audio`). An SRI SampleSize_REG, HorizontalCount_REG or Spacing_REG at the largest int gives no wrapped rate: 0 Hz for a transport wider than its window, otherwise the transports the window holds times the row rate (`test_a_sub_row_interval_field_near_the_int_limit_gives_no_wrapped_rate`).
- `test_running_port_write.py` — a Write that changes SampleSizeGrouping, PortControl,
  SkippingNumerator or FlowControl of a running port is flagged as a programming error and
  labelled with the register and port, and the decode before it is untouched; a write of the
  value already there, a register outside the set, and the demos' writes before their ports
  run are not flagged. A write covering a read-only data-port byte (PortStatus, ReadyCh_L/H, EC_TestFailCh0, EC_FlowControlFail, COUNT_ReceivedSSP) carries `read_only_write_error` whether or not the port runs; IntStat (RW1C) and a same-value RW write (CommitGroupMemb) do not. A read-only SLC byte (ID0, an EC_* counter) and a CDS register's _CURR alias carry it, the same CDS register's _NEXT address does not (`test_a_write_to_a_read_only_slc_or_cds_curr_register_is_flagged`), and a read-only write among the setup writes, before the port runs, carries it (`test_a_read_only_write_before_the_port_runs_is_flagged`), on `make_running_port_write_levels` and its `base` and `before_run`.
- `test_phy3_cds_column.py` — the PHY3 CDS column: a capture decode reads it at Column 2 and,
  after a commit of CDS_HorizontalStart, at the new column, in a new segment whose
  `cds_column` the symbol viewer, TX raster, grid and bus rows follow; an authored PHY3
  config places it after S1's tail and handover, as the visualizer does, and the expected
  registers carry it when it is not the reset value.
- `test_native_payload_diagnostics.py` — `SWI3S_SRC_SHIFT` reads each source-port bit that
  many UIs later and leaves sink ports alone, 0 is the default decode, and the windowed
  re-decode keeps the shift; `SWI3S_DUMP_PATH` lists the assembled bits in order, up to
  `SWI3S_DUMP_MAX`, for the port `SWI3S_DUMP_DP` names, with the recovered levels.
- `test_native_reconfigure_survivor.py` — a port that keeps running through an immediate
  single-ranked write keeps its per-channel state: each channel's `index` continues without
  restarting, the sample in progress keeps its start sample, every sample starts after the
  previous one on its channel ends, and the sample size stays constant. Each case runs with
  the write on a sample boundary and halfway through an interval. Also an immediate SampleSizeGrouping write that
  lands mid-sample (`make_immediate_sample_size_levels`): the sample in flight finishes at
  its old width, later samples use the new width, all bit-exact.
- `test_native_payload_rules.py` — payload-engine rules: the running sample index across an
  SSCR and the descrambled TX_PRESENT bit in the flow-control handshake check; the RX_CONTROLLED handshake is bijective, so a skipped transport after a DRQ of 1 fails it, while ASYNC allows it (`test_a_skipped_transport_after_a_drq_of_1_fails_only_rx_controlled`); a TX_PRESENT slot carries bit index 0 (`test_a_tx_present_slot_carries_bit_index_0`); a wide bit is sampled on its last UI (`test_a_wide_bit_is_sampled_on_its_last_ui`); a sink port places no Guard or Tail (`test_a_sink_port_places_no_guard_or_tail`). It also pins the
  capture-format delta decoder at both sides of the int64 limit of `decode_saleae_v3_deltas`
  (a code is taken while its delta fits int64), the same guard `test_ingest_formats.py` describes. A source port places TailWidth Tail slots, and one Guard when enabled, in each data row (`test_a_source_port_places_its_tail_width_of_tail_slots`). A sink port's wide bit occupies every UI, with the marker on the last (`test_a_sink_wide_bit_occupies_every_ui`). A sink port with Guard/Tail set decodes the same bit points and audio as without.
- `test_native_dscr_channel_set.py` — a DSCR that changes a running port's channel set
  (`make_dscr_channel_set_levels`) keeps the port's phase, and every interval carries
  exactly one channel set, each channel once, in channel order. A commit point on an
  interval boundary applies the new set to that interval: a disabled channel sends nothing
  in it, an enabled one (ch1 of {0}, the middle ch1 of {0,2}) sends its first sample, index
  0, in it, and disabling ch0 of {0,1} sends ch1 once. A commit point after the interval's
  transport has begun (during it, or after it ends) leaves that interval on its old set and
  applies the new one from the next. A channel the DSCR enables decodes from a seeded
  descrambler and channel 0 from its running one; a windowed re-decode from inside the
  commit interval equals the full decode's sample points and channels.
- `test_native_scrambler_reseed.py` — a scrambler is seeded when its channel or FCP is
  enabled or its scrambler enable bit turns on, and continues otherwise.
  `make_channel_reseed_levels`: an independent descrambler over the wire bits (decoded with
  ScramblerEn forced off), seeded at each channel enable and where ScramblerEn turns on,
  gives every decoded value; a channel re-enabled by an SSCR or a DSCR, or whose ScramblerEn
  an immediate 0x0B write sets again, decodes from the seed; a channel surviving an SSCR or
  DSCR decodes from its running LFSR; with ScramblerEn cleared it decodes as sent; both 0x0B
  writes are flagged as programming errors. `make_fcp_reseed_levels`: an FCP across an
  SSCR, a DSCR, and 0x0E writes that remove and re-enable it and then clear and set
  FCP_ScramblerEn passes the RX_CONTROLLED handshake check when the wire's DRQ scrambler is
  seeded at each FCP enable and where FCP_ScramblerEn turns on, and fails it when seeded at
  the SSCR, the DSCR or every interval, or not where FCP_ScramblerEn turns on, or not at
  the re-enable. ScramblerEn cleared and set again across two full re-anchors (Warm Start restarts) seeds the channel at the second rather than resuming its earlier LFSR (`test_scrambler_en_off_then_on_across_two_re_anchors_seeds_the_channel`).
- `test_native_register_rules.py` — C++ register-model rules: `WriteIsImmediate` by rank and
  Commit Group 0 membership of the SLC, CDS and PHY Control registers (a Commit without
  group 0 leaves a staged _NEXT write unpromoted, one with it promotes it). A bus write of 0 to SkippingDenominator_L/H reads as its reset value 1, and the skipping decode follows a denominator of 1 (`test_a_bus_write_of_zero_to_the_skipping_denominator_reads_as_one`). A write to a read-only CDS _CURR alias changes no register, with or without a Commit (`test_a_write_to_a_read_only_curr_alias_changes_nothing`). A changed SkippingNumerator[11:8] write on a running port is flagged (`test_changing_the_skipping_numerator_high_byte_of_a_running_port_is_flagged`). NumColumns reads its five bits through config_from_commands (`test_num_columns_reads_its_five_bits`). SyncPointOffset 1..5 moves an SSCR's commit row by that many rows (`test_the_sync_point_offset_moves_the_commit_row_by_its_three_bits`).
- `test_native_reference_vectors.py` — wire-level reference checks the core's own encoder
  cannot make: an independent CRC-16 (0xA2EB) and descrambler (seed 0b101010100, taps Q4^Q0,
  No-Toggle) are compared with the demo's planned wire levels, a scrambled channel's LFSR continues across an SSCR that keeps it enabled, and a windowed DLV replay must
  equal the full decode's bit sample points exactly, over exactly the source-UI range asked for (every bit of a row inside it, none of a row outside it); the No-Toggle rule at 16 bits on a port decoded with ScramblerEn
  forced, and the demo's 8b/10b encoder equal to the standard code tables for every byte (A7 included).
- `test_link_events.py` — PM_Action writes, including a write starting below 0x1011; a write that stops short of PM_Action or starts after it is no event (`test_writes_that_stop_short_of_or_start_after_pm_action_are_not_events`); the action reads as the register's label ('Force_Resync' for value 2), and as its hex value when there is no register map (`test_without_a_register_map_an_action_reads_as_its_hex_value`).
- `test_timebase.py` — the sample rate of a capture that stores times: the no-grid fallback rate to 4 significant figures, a spacing finer than float64 can invert (a rate above the limit that every reader refuses), a huge time that neither warns nor hides the others' grid, and a grid fit that stays on the nearest sample. It also covers `timebase.repeats` counted on every pair across chunk sizes, and the sample-rate range rule (`rate_problem` / `check_rate`, 1 Hz to 1e15 Hz, message pinned) (`test_a_spacing_finer_than_float64_can_invert_gives_a_rate_every_reader_refuses`, `test_one_huge_time_neither_warns_nor_hides_the_grid_of_the_others`). It also covers the direct behaviour of the shared helpers: `to_samples` (rounding, the 0 and 2**63 limits, nan/inf, origin), `drop_nonfinite` across chunk edges, `require_finite`, `same_sample_note` singular/plural and `require_distinct` (`test_to_samples_rounds_to_the_nearest_sample_inside_the_range`, `test_to_samples_refuses_a_time_outside_the_sample_range`, `test_to_samples_counts_from_the_origin_and_leaves_its_input_alone`, `test_drop_nonfinite_reports_the_first_bad_position_across_chunk_edges`, `test_drop_nonfinite_returns_a_finite_array_as_given`, `test_require_finite_names_the_source_and_field`, `test_the_same_sample_note_agrees_with_its_count`, `test_require_distinct_refuses_only_a_repeated_selection`).
- `test_demo_capture_pinning.py` — `demo_capture` pins both the rate and the samples per UI
  when either is given.
- `test_config_csv_bom.py` — a config CSV with a UTF-8 byte-order mark keeps its first field; a current-format file with a BOM reads as without one in the native loader and the Visualizer engine.
- `test_visualizer_handover_request.py` — a handover is a device's request: the requesting device's own data, port guard or port tail in its handover column is not a clash and keeps the marker, and another device's write clashes with the request.
  Another device's guard or tail clashes with the handover request, each case having a control with the guard or tail off; an override key must name a config row. The own-device cases also assert the bit is not in the model's bus_clashes; the other-device cases assert it is.
- `test_session_guessed_framing_warning.py` — a known column count framed with no CRC-valid command raises an import warning naming the first samples (not for a generated demo).
  A step that decodes more than once warns once, for the decode it keeps. Two decodes guessing at the same samples give one note in one collector (`test_two_decodes_with_the_same_guess_report_it_once`).
- `test_dock_showing_gui.py` — the cursor tick refreshes a dock only while it is on screen; a tab behind another is not showing until it is raised, and raising it refreshes it at once.
  Raising the Registers tab shows the PHY block in force at the cursor (`test_raising_the_registers_tab_shows_the_phy_block_in_force_at_the_cursor`); the CDS test has a positive control (the tick refreshes the CDS dock while it is raised). A CDS or Samples pane floated from behind another tab is windowed at the cursor once the layout settles (`test_floating_a_tab_behind_another_refreshes_it_at_the_cursor`).
- `test_load_cancel_gui.py` — a decode in flight is cancelled through `Decoder.cancel()` when the window closes or a newer capture is opened, and the window shows the closing notice; a re-decode is not cancelled by opening another capture; a real decode stops promptly on close.
  The close tests close the window (closeEvent) rather than calling join_worker_threads directly. A Locate Sub-Capture done or failed delivered after the close opens no message box and no import warnings, and places no cursors. The real-decode close test checks that the decode stopped before the end of the capture, within half the wait bound. A load emits exactly one of done, failed and cancelled: a decode that fails before a cancel is seen reports failed alone (`test_a_decode_that_fails_before_a_cancel_reports_failed_alone`).
- `test_open_config_csv_errors_gui.py` — a config CSV the reader cannot parse is reported naming the file by Open Capture and by the authoring panel, and the error does not escape the Qt slot.
- `test_window_state_declared.py` — MainWindow declares its load and locate state in `__init__`. Clear All Filters, after the kind, device and group menus, Errors Only and the text each set a criterion, shows every row with no criterion active and the title plain, tests each row once and refreshes the timeline ticks once (`test_clearing_every_filter_clears_each_criterion_and_tests_each_command_once`). A batch that changes the criteria tests nothing inside it and each row once at its end (`test_a_batch_that_changes_the_criteria_tests_each_row_once_at_its_end`); an empty batch, setters writing the current values, or a change undone inside the batch test none. Row tests are counted at the source model's command_at when called from filterAcceptsRow. The ribbon, the Audio, Capture and Samples views and the dock title bars answer through public accessors that agree with what they hold, and there is at least one title bar; that main_window.py uses them is test_main_window_source.py's.
- `test_run_scripts.py` — win32 only, marked `platform` so the guest scope runs it; runs run.ps1 under PowerShell against a stub venv and a stub swi3s_studio.app, and checks the headless/GUI launch choice for -o, -o=, --output, the --o..--outpu prefixes and -h/--help.
- `test_wfm_header_checks.py` — a `.wfm` sample interval must be positive and give at most 1e15 Hz, in `read_wfm` and
  `read_header` alike (the probe offers no such pair); the label is the base name under either separator.
- `test_api_decode_temp.py` — `api.decode` removes its upgraded config temp copy after a decode and when reporting the
  CSV's warnings, building the Decoder or run() raises (core, no widgets); the success-path test also checks that an upgraded copy was made in the watched temp directory.
- `test_core_model_store_rules.py` — (marked `platform`: text encodings and a subprocess import) flow-controlled PDM native rate: a PDM port keeps the core's bit rate in flow modes 0-3, a normal port reports the rate the core supplied, and a flow-controlled port (modes 1-3) measures its bit rate on the bits' own positions of a jittered, gapped schedule (`test_a_flow_controlled_pdm_port_measures_its_bit_rate_on_the_bits_own_positions`), register-map JSON and importer rules, store package imports, the command store's register-write feed (`test_register_writes_yields_only_crc_valid_addressed_writes`).
- `test_tooling_config.py` — `pyproject.toml`, the requirements files, CI and the launchers
  agree.
  The runtime requirements of pyproject.toml and requirements.txt are one set, compared both ways by name, specifier, extras and marker, and the setuptools build requirement is the one requirements-dev.txt installs; swi3score is no package-index dependency; the headless mode has a console script; every installed data file exists, the user guide among them; .gitignore repeats no rule and its comments tell no story; CI pins every action of every workflow (ci.yml and cla.yml) to a full commit, declares read-only repository permissions at the top level with no job setting its own, runs the leak scan in the lint job itself, and installs the wheel into a clean environment and runs `tools/package_smoke.py` on Linux and Windows; the CLA job's one condition is a pull_request_target event, or a comment on a pull request (not an issue) that is `recheck` or the configured sign phrase, and every bot is exempt through the one `*[bot]` allowlist pattern; requirements-dev floors coverage 7.10 and pytest-cov 7.0 for the subprocess option; package_smoke installs from a copy of the files a commit can carry, without an earlier build's build/ or *.egg-info, a tracked file deleted from the working tree, or anything ignored (`test_the_smoke_tool_installs_from_the_files_a_commit_can_carry`), names the installed data files and checks where the installed lookups resolve and that the icons shipped, reads the CLI's model file as a bus model (a JSON object with positive row and column counts and a non-empty bit map; `test_the_smoke_tool_reads_the_cli_model_file_as_a_bus_model`), copies the tree when git is missing or fails, leaving out build/, *.egg-info, .git and __pycache__ (`test_the_smoke_tool_copies_the_tree_when_git_is_not_usable`) and names an interpreter that cannot report its prefix (`test_the_smoke_tool_names_an_interpreter_that_cannot_report_its_prefix`); three of the package_smoke tests are marked `platform`; CI sets a time limit on every job; run.sh reads every spelling of -o and -h the parser accepts as headless and opens a window for everything else (marked `platform`, needs bash).
- `test_tooling_conftest.py` — the suite's own fixtures in `tests/conftest.py` (marked `gui`): the per-test window teardown undoes the test's monkeypatches while the windows still hold them, so a pytest that undoes an instance patch by deleting the attribute passes (`test_a_patch_on_a_window_is_undone_before_the_window_is_emptied`; the test runs the teardown itself and asserts after it); the modal guard and the slot-exception guard keep their own patches, so both stay in place when the test's `monkeypatch` is undone, by the test or by the window teardown before it empties and collects the windows (`test_the_guards_stay_in_place_when_the_tests_patches_are_undone`). The window teardown requests both guards, so in a scratch run under tests/conftest.py both are in place while the teardown closes a window, and a test's patch of a guarded entry point leaves the original on the Qt class (`test_the_guards_hold_through_the_window_teardown_and_leave_no_stub_behind`). A test id longer than `conftest.MAX_TEST_ID` (1000 characters) stops the run at collection, since Windows refuses the `PYTEST_CURRENT_TEST` variable above 32767; a parametrized case holding large data names itself with `ids=`. Every test reads `conftest.AMPLE_FREE_MEMORY` (64 GiB) as the free memory, so the decode's memory watchdog and the load guards act the same on every machine and under any number of parallel test processes; a test of those rules sets its own reading, and a test of the reading itself requests the `real_free_memory` fixture (`test_every_test_reads_ample_free_memory_and_a_decode_runs_below_the_machines_floor`, `test_a_test_requesting_real_free_memory_reads_the_machine`).
- `test_tooling_snr.py` — the SNR helpers of `tests/conftest.py` (`core`, no widgets): `worst_snr` reads every sample of a stream, a slip in the partial last window or in the tail of a window longer than the 4096 samples `tone_snr` reads included.
- `test_publish_tree_build.py` — the `publish_tree` export, diff parsing and command line; a graft that shadows a file (a release commit carrying an upstream-owned file) fails `--check-published`. The throwaway repositories ignore the machine's global and system git configuration; the exec-bit tests assert git's recorded modes on Windows. A blob git cannot return stops the export naming its path (the read-only loose object is made writable before it is removed, so it runs on Windows); the tree listing is from the repository root whatever directory git runs in; check 7 reads a repository that names its objects by sha256; an assembled file or directory that cannot be read is named, not reported missing; the hunks read the same under any diff configuration (core.quotePath=false included) and read a path git quotes (a tab, a double quote), and a revert of such a file is caught; --check-published refuses --message-out, --approve-names, --preview and --record-sync, and --record-sync refuses anything but --upstream.
- `test_analog_csv_rows.py` — analog CSV rows: a line ending in a comma, row numbers in
  messages, the inferred rate. The preamble's Sample Interval is reported when it is no usable time (nan, inf, 0, negative, text, below 1e-15 s or above 1 s), is silent at the limits of its range, 1 s and 1e-15 s (`test_a_usable_preamble_interval_is_not_reported`, parametrized), and a subnormal interval or time step is refused by file name through the Session (`test_a_subnormal_preamble_interval_or_time_step_is_not_a_bare_overflow`). A file of non-numeric rows is scanned within a bound (`test_a_file_of_non_numeric_rows_is_scanned_within_a_bound`), and a row with a blank TIME cell is reported as having no TIME value, not as short (`test_a_row_with_no_time_cell_is_reported_as_such_not_as_short`).
- `test_audio_view_mixed_playback.py` — a mixed playback starts each data port at its own start time, the cursor sweep waits for the swept stream's first sample, and the channel menu shows the tooltip of its disabled Evenly Spaced entry. play() sweeps against the data port reaching furthest, from that port's own first sample, and the cursor does not move until the sink has played that lead. The capture rate is twice the play rate, so a lead is converted from capture samples to frames at the play rate (checked at two play rates). The channel menu's Evenly Spaced entry is disabled with its explanation for an evenly arriving stream, and offered with no tooltip of its own for an irregular one.
- `test_authoring_dialog_entries.py` — the Authoring dialogs commit only entries inside their
  ranges (the Flow Control dialog stays open with the entry marked), and CDS Settings opens for
  any stored bit width.
- `test_grid_view_export.py` — a Bus Grid whose PNG raster would exceed `_MAX_PNG_BYTES` (1 GiB) raises an OSError naming 'too large for a PNG' and SVG, writes no file, and still exports as SVG (`test_a_grid_too_large_for_a_png_says_so_and_still_exports_as_svg`). A grid image that fails to write leaves the previous file (`test_a_grid_image_that_fails_to_write_leaves_the_previous_file`).
- `test_authoring_entry_commit.py` — the Authoring panel commits a typed cell only when it is a
  valid entry; a blanked cell commits its row minimum. A blanked interface field shows the minimum it commits, and the test pins each attribute's minimum, and a blanked data-port cell shows 0 (`test_a_blanked_interface_field_shows_the_minimum_it_commits`, `test_a_blanked_data_port_cell_shows_the_0_it_commits`). The CDS Tail Width dialog offers 0, 1 and 2 only (3 is Reserved) (`test_the_cds_tail_dialog_offers_only_the_widths_the_register_defines`). The data-port identity dialog keeps a number outside 0..31 and the name unless edited (`test_the_identity_dialog_keeps_what_was_not_edited`), and stores a typed 'DP<n>' as auto-following (`test_the_identity_dialog_stores_a_typed_default_name_as_auto_following`). A row rate the box rounds is not rewritten by an unrelated commit (`test_a_row_rate_the_box_rounds_is_not_rewritten_by_an_unrelated_commit`). Typing the rounded Row Rate text over a finer model value commits the typed value, while a box that was not typed in is never the source of a commit (`test_typing_the_rounded_row_rate_over_a_finer_one_commits_it`). A populate forgets what was typed into the row-rate box (`test_a_populate_forgets_what_was_typed_into_the_row_rate_box`).
- `test_bus_config_reading.py` — BusConfig reading: what a short or blank config-CSV cell takes
  and reports, the per-source lists of a saved config, and the device encoding the writer uses.
  An integer cell with trailing characters reads as its leading digits, as the decode reads it; an explicit RowRate of 0 is reported. A blank cell in a per-port or per-source row counts as missing: the default is applied and it is reported as 'KEY (k of n)' (`test_a_blank_port_cell_takes_the_field_default_and_is_reported`); a saved config with unknown keys or data ports past the 12th reports them (`test_a_saved_config_with_unknown_keys_or_extra_data_ports_reports_them`).
  An absent skipping denominator reads as the register reset; a short source list is padded with the field's own default; the CDS bit-width register range is 0..7; the column count, S0 width, S1 tail width, skipping denominator and rows to draw are clamped into their ranges and reported, from a CSV and from a saved config (`test_the_interface_fields_are_held_in_their_ranges_and_the_clamp_is_reported`); a saved config with wrong-typed values reads the rest and reports them; a saved config whose dataports are not a list reads as the defaults. A RowRate outside 1..48000 kHz reads as 0 and is reported with the native loader's sentence, from a CSV and from a saved config; a negative CSV cell is a bad cell (`test_the_row_rate_is_held_in_range_on_the_csv_and_the_authoring_dict`); every per-port field wider than its register (16 fields, SampleSize 5 bits, EnableCh 16 bits, DataPortNumber 0..31) and every CDS per-source row wider than its field is reported on both paths, kept as written, with the native loader's exact wording, checked against `swi3score.csv_warnings` (`test_a_per_port_field_wider_than_its_register_is_reported_on_both_paths`, `test_a_wide_cds_source_value_is_reported_by_source`); a disabled port or one with no channel is not checked; a row the file lacks is reported by name in one note, a CDS row counting as present under its legacy scalar spelling (`test_a_row_missing_from_the_file_is_reported_by_name`, `test_a_truncated_file_names_the_first_missing_rows_and_counts_the_rest`); a warning lists its first three names then 'and N more' (`test_a_warning_lists_a_few_names_and_counts_the_rest`); every boolean spelling reads silently (`test_every_boolean_spelling_reads_without_a_warning`). The 'absent skipping denominator' test also expects the missing-row note. A whole-number cell reads as the native loader reads it: ASCII digits only, an underscore or another script's digit ends the number, with the same note text (`test_a_whole_number_cell_reads_the_way_the_native_loader_reads_it`). A RowRate cell reads its leading number (`test_a_row_rate_cell_reads_its_leading_number_as_the_native_loader_does`). A blank or absent RowRate reads as 0, the measured rate, and the 'RowRate is 0' note fires only for an explicit value (`test_a_blank_or_absent_row_rate_reads_as_the_measured_rate_the_native_loader_takes`). A cds_guard outside 0/1/2 in a saved config reads as the default (`test_a_cds_guard_outside_off_g0_g1_in_a_saved_config_reads_as_the_default`). Hostile cell text and key counts give short warnings (`test_a_saved_config_with_hostile_text_gives_short_warnings`). The scalar CDS_DriveType (blank leaves the default, a value broadcasts) and the UTF-8 BOM first field are read here (`test_a_blank_scalar_cds_drive_type_leaves_the_default`, `test_a_scalar_cds_drive_type_broadcasts_to_every_source`, `test_a_config_csv_with_a_bom_reads_its_first_field`). A negative legacy scalar CDS_TailWidth_REG or CDS_DriveType row is ignored with a warning, as the native loader ignores it (`test_a_negative_legacy_scalar_cds_row_is_ignored_as_the_native_loader_ignores_it`); `to_csv_file` keeps the old file when the write fails (`test_saving_a_bus_config_over_a_file_keeps_the_file_when_the_write_fails`). A bad cell is worded and cut as the native loader words it (`test_a_bad_cell_is_worded_and_cut_as_the_native_loader_words_it`). A config CSV with lone-CR line endings reads as the same file with LF endings (`test_a_config_csv_with_lone_cr_line_endings_reads_as_one_with_lf_endings`).
- `test_command_filter_edge_cases.py` — the Commands free-text filter: a trailing operator and
  an unmatched `)`, and All Links matching the global time shown.
- `test_csv_handler_row_checks.py` — `CSVHandler.load_csv` row checks (marked `platform`: the unix line ends and the byte-order mark run on the test guests): repeated field rows,
  DisplayFields characters, a field row without a value, a UTF-8 byte-order mark, and the
  VizConfig a load returns. A repeated ManagerDataport row decides the port like any other
  row. A saved file has unix line endings. Stray DisplayFields characters in several ports give one message for the row naming each stray set and its data ports (`test_stray_display_fields_characters_in_several_ports_are_one_message`). A message's line number is the physical line the record starts on when a quoted Description spans lines (`test_a_reported_line_is_the_physical_line_when_a_description_spans_lines`). `load_csv` names the line, the field and, on a per-port row, the data port in value-parse and range errors (`test_an_interface_cell_that_is_refused_names_its_line_and_field`, `test_a_per_port_cell_that_is_refused_names_its_line_field_and_port`), and an over-range per-source CDS cell (tail width, drive type, end-drive-early) or scalar CDS_DriveType carries its low bits and warns with the line, field, source and value (`test_a_per_source_cds_cell_wider_than_its_register_carries_its_low_bits`, `test_a_scalar_cds_drive_type_wider_than_its_register_carries_its_low_bit`); `test_a_config_with_over_range_cds_values_builds_the_grid_of_the_carried_values` pins it end to end (BusConfig CSV -> swviz engine and native core build the same grid as the low-bits config, with an import note); a negative legacy scalar CDS_TailWidth_REG or CDS_DriveType row is ignored and reported with its line and field, so the sources keep the defaults of a file without the row, and it is silent beside a per-source row; the last row of the key decides (`test_a_negative_legacy_scalar_cds_row_is_ignored_and_reported`, `test_a_negative_legacy_scalar_is_silent_beside_its_per_source_row`, `test_the_last_row_of_a_legacy_scalar_decides`); a byte that is not UTF-8 is reported without a line, early or past the first read chunk (`test_a_byte_that_is_not_utf8_is_reported_without_a_line`); and `save_csv` is atomic (`test_a_failed_save_leaves_the_existing_file_and_no_temporary_file`). A flag, a whole-number and a RowRate cell read alike in `load_csv` and in BusConfig, and a cell neither accepts is refused by `load_csv` and noted by BusConfig (`test_a_flag_cell_reads_the_same_in_the_engine_and_in_bus_config`, `test_a_whole_number_cell_reads_the_same_in_the_engine_and_in_bus_config`, `test_a_cell_that_is_not_a_whole_number_is_refused_by_the_engine_and_noted_elsewhere`, `test_a_row_rate_cell_reads_the_same_in_the_engine_and_in_bus_config`, `test_a_row_rate_cell_that_is_not_a_number_is_refused_by_the_engine_and_noted_elsewhere`, `test_a_flag_cell_that_is_not_a_flag_is_refused_by_the_engine_and_noted_elsewhere`). A refused cell is quoted cut to 60 characters (`test_a_refused_cell_is_quoted_cut_to_sixty_characters`). Unrecognised fields are listed a few at a time and the rest counted (`test_unrecognised_fields_are_listed_a_few_at_a_time_and_the_rest_counted`). A legacy scalar CDS_TailWidth_REG above its register carries its low bits (`test_a_legacy_scalar_cds_tail_width_above_its_register_carries_its_low_bits`).
- `test_decoded_sample_view.py` — the Decoded Samples pane: Binary grouped in fours from
  the LSB, the value filter's parsing, the port selection across a rename, and the window across
  a capped append. 0o, 0b, 0X and negative hex values filter. A value with digit-group underscores, non-ASCII digits, a bare or invalid prefix, an exponent or a doubled sign applies no filter and marks the field.
- `test_export_dialog_validation.py` — Export dialogs: ranges (To inclusive) and the exports refused (a rows or UI range ending before its start (both range kinds, `test_an_end_before_the_start_is_refused`), a time range ending before its start, no output file, no signal), signal names, typed resample rates: a unit always wins and a number with no unit is kHz below 1000 (`test_a_typed_rate_with_no_unit_is_khz_below_1000_and_a_unit_always_wins`), a rate below the 1 kHz minimum is refused and not exported (`test_a_typed_rate_below_the_minimum_is_refused_and_not_exported`), and the ceiling (768 kHz, or a faster stream's own rate), and the grid image write. The over-limit message names the typed rate in plain decimal kHz, never exponent form ('1000000 kHz', '768.001 kHz').
- `test_eye_view_edges.py` — the Timing (eye) view: the histogram extent for a one-bin side, which spans 0 to that side's largest margin under the filter, and
  the driver-filter button label.
- `test_ingest_viz_edge_cases.py` — the analog front end (thresholds from numeric samples, a
  sample rate of at least 1 Hz), the digital CSV reader and the Visualizer engine's validation
  of data port devices and PHY3 system slots.
  A NaN sample leaves a channel's detected thresholds and a channel of only NaN takes the fallback; the median step of an even count is the mean of the middle two; the analog warnings name the channel by its role after the swap; the digital CSV reader refuses, naming the file, an empty file, a first row with no line break or with too many columns, and bytes that are not UTF-8 text; a wide DRQ truncation is reported only for a drawn port; a legacy flag that is not true or false reads false and is reported, naming its port for a per-port flag; the mismatch lists hold each mismatch once. A time axis with subnormal or sub-femtosecond steps and a given rate beyond the maximum; analog transitions landing on one sample are counted for both channels; the legacy visualizer CSV converter clamps and reports channel counts above 16 or below 1, counts below 1 and Rows to Draw outside 1..10240, and names the field and port of a cell that is not a number; the demo capture builder centres data edges in the UI and sets cold-start DN 2 us before its rising clock edge; the interval check counts TxP in every flow mode but NORMAL. A time axis with no step does not give a sample rate (`test_a_time_axis_with_no_step_does_not_give_a_sample_rate`); a given rate that is not a float is warned about and replaced (`test_a_given_rate_that_is_not_a_float_is_warned_about_and_replaced`); a RowRate outside its range is clamped and reported, one inside it is not changed, and one that is not finite is refused by field (`test_row_rate_outside_its_range_is_clamped_and_reported`, `test_row_rate_inside_its_range_is_not_changed`, `test_a_row_rate_that_is_not_finite_is_refused_by_field`); PHY3 system slots are reported only when they overflow the frame, REG being columns minus one (`test_phy3_system_slots_are_reported_only_when_they_overflow_the_frame`); the config-CSV file checks (non-UTF-8 refused by file, the size limit, device read bounded) and the exact-fit PHY3 boundary are pinned beside the slot truncation, named for the slot; the /dev/zero test is a platform test skipped on Windows; the analog below-1-Hz text names the rate ("gives a sample rate of X Hz, below 1 Hz"). A config CSV with no content is reported as having no field, and a zero-byte file is reported when it is opened (`test_a_config_csv_with_no_content_is_reported_as_having_no_field`, `test_a_zero_byte_config_csv_file_is_reported_when_it_is_opened`); the rate-replacement tests pin each message; the non-integer centring bound is one sample. The 'never crosses its thresholds' warning pairs each channel with its own span after the swap (`test_the_never_crosses_warning_pairs_each_channel_with_its_own_span_after_the_swap`), and the demo capture centres every data edge in its UI (`test_the_demo_capture_centres_every_data_edge_in_its_ui`).
- `test_main_window_rules.py` — `MainWindow` rules not tied to one pane: which table a position
  indexes, how a worker's failure settles, which Link a diff is read from, what a limit tells
  the user, what a damaged workspace reports, and what a finished load leaves behind: Compare reads the active Link and `win.cursor.sample` inside both boxes, with the other Link counting the instant differently, a `.wfm` capture opens with the supplied config, Rows To Draw over the authored limit is named in the notifications, a maximized window is not resized to fit the screen, the SSP step leaves a lane that is gone alone, and closing takes down a search dialog still up.
  A failed persistence scan names its error in the status bar; a failed locate still shows the read's warnings; a new capture starts with cursor and playhead at 0. Export Audio asks before it replaces an existing file (default No; No writes nothing, Yes replaces it), and the wait cursor is removed after a failed write. Open Capture offers every format Export Capture writes, under the same name and pattern.
  A failed persist scan held running until a second request is pending still lets that request start. An unreadable window layout gives one exact note when reporting and none when not. A Locate failure names the exception's type for a KeyError or an exception with no text. A workspace's or settings CSV's NumColumns_REG outside 1..31 is clamped with one note, and the authored-grid fallback draws at most 32 columns. Export Bus Model (JSON) offers bus_model.json, writes at most MAX_AUTHORED_ROWS (1024) rows, and says '(limited to N rows)' in the status bar and the file status only when limited. An interrupted audio export takes the wait cursor down. Force Column Count offers 0..max and refuses outside the core's column range, its message naming swi3score's limits (`test_force_column_count_offers_and_accepts_the_cores_column_range`). A failed re-decode is titled 'Re-decode failed' and a failed open 'Open failed', each naming the error, and a failing after-step is reported as that step failing and not as the views failing (`test_a_failed_re_decode_is_titled_as_one_and_names_the_error`, `test_a_failed_open_keeps_its_title`, `test_a_failing_after_step_is_not_reported_as_the_views_failing`). Three loads and Link-menu rebuilds leave no more progress dialogs or menu actions than before (`test_loads_and_menus_do_not_accumulate_children`). Locate Sub-Capture during a running decode shows the 'Decode running' box and opens no file picker (`test_locate_waits_for_a_decode_that_is_running`). A close joins the persist scan's thread, and the persist slot stays held until it stops (`test_the_persist_slot_is_held_until_its_thread_has_finished`); a failed persist scan's done signal carries the error and the token, and reports no result (`test_a_failed_persist_scan_reports_no_result`). The Compare overlay survives a re-decode of its Link (`test_the_compare_overlay_survives_a_re_decode_of_its_link`). It is removed when another capture opens (`test_the_compare_overlay_goes_with_the_capture_it_was_made_for`) or when the Registers pane shows another Link through the Links menu, its picker or an added Link (`test_the_compare_overlay_goes_when_the_registers_pane_shows_another_link`). Clear Compare removes it with no capture loaded (`test_clear_compare_removes_the_overlay_with_no_capture_loaded`). A bus-model export that fails leaves the previous file and no temporary file (`test_a_bus_model_export_that_fails_leaves_the_previous_file`). Force Column Count accepts the core's minimum, its maximum and 0, and pins that count (`test_force_column_count_accepts_a_legal_count_and_0_to_remove_the_pin`). A num_columns of 32 (workspace) and a NumColumns_REG of 32 (settings CSV) are clamped to 31 with a note. Compare shows its boxes with the active Link and the cursor restored (`test_compare_shows_its_boxes_with_the_active_link_and_cursor_restored`). A saved window entry that is not a mapping is reported (`test_a_saved_window_entry_that_is_not_a_mapping_is_reported`). The Registers column range is the core's less one (`test_the_register_column_range_is_the_cores_less_one`). Export Bus Grid gives a name typed without a suffix the chosen filter's (`test_export_grid_gives_a_name_typed_without_a_suffix_the_chosen_filters`). When the added suffix names an existing file, Export Bus Grid and Save Workspace ask before replacing it, and a No writes nothing (`test_export_grid_asks_before_an_added_suffix_replaces_an_existing_file`, `test_save_workspace_asks_before_an_added_suffix_replaces_an_existing_file`). The Rows To Draw box rejects a group separator in en_US and de_DE (`test_the_rows_to_draw_box_rejects_a_group_separator`). Every save dialog (workspace, bus config CSV, bus model JSON, timing settings JSON, commands CSV, Visualizer settings CSV) adds its format's suffix to a name typed without one, and asks before an added suffix replaces an existing file (`test_every_save_dialog_adds_its_formats_suffix_to_a_name_typed_without_one`, `test_every_save_dialog_asks_before_an_added_suffix_replaces_an_existing_file`); a workspace named with a dot inside keeps no false extension and a `.json` name is kept (`test_save_workspace_adds_its_suffix_to_a_dotted_name_and_keeps_a_json_one`). A Save timing settings that fails leaves the previous file (`test_save_timing_settings_that_fails_leaves_the_previous_file`). The authoring box, the Bus Grid box and the config share one rows limit (`test_the_authoring_box_the_bus_grid_box_and_the_config_share_one_rows_limit`). The Export Capture Browse button gives a name the format's suffix and asks before an added suffix replaces a file (`test_the_export_capture_browse_button_gives_a_name_the_formats_suffix`, `test_the_export_capture_browse_button_asks_before_an_added_suffix_replaces_a_file`).
- `test_main_window_source.py` — main_window.py read with ast; core (builds no widget). No getattr or hasattr call takes `self` as its object (`test_no_getattr_or_hasattr_reads_an_attribute_of_self`), and `_store`, `_dock`, `_table`, `_cursor`, `_total`, `_view_lo` and `_view_hi` are read only on `self`, another object's through its accessor (`test_main_window_reads_no_accessor_backed_attribute_of_another_object`).
- `test_native_config_csv_rules.py` — rules of the native config CSV loader (`csv_warnings` /
  `registers_from_csv`): wide hex and binary cells, non-ASCII paths, repeated fields, non-finite
  RowRate, the scalar CDS_DriveType row, unknown fields, a byte-order mark, a multi-line Description, an unopenable file, a '"' inside an unquoted cell, a quoted cell left open at the end of the
  file, a RowRate of 0, S1TailWidth_REG at the largest int. A blank SkippingDenominator_REG reads as its reset value 1 and is reported as blank (`test_a_blank_skipping_denominator_reads_as_its_reset_value`); a RowRate outside 1..48000 kHz is reported and left unset (`test_a_row_rate_outside_its_range_is_reported_and_left_unset`); every field the Visualizer reader skips is silent (`test_the_fields_the_visualizer_reader_skips_are_silent`); a quoted cell left open on line 1 of a 100,000-line file is read in linear time (`test_an_unclosed_quote_reads_the_rest_of_a_long_file_in_linear_time`); a repeated field's last row gives the registers, a negative S1TailWidth_REG places the CDS as a tail of 0 and 255 or the largest int gives tail 31's registers, while 5 is not reported; a RowRate that is not a number is reported once (`test_a_row_rate_that_is_not_a_number_is_reported_once`); a repeated RowRate takes its last row, a blank one included (`test_a_repeated_row_rate_takes_its_last_row_a_blank_one_included`); a CDS guard cell other than 0 or 1 is reported and read as 1 (`test_a_cds_guard_cell_other_than_0_or_1_is_reported_and_read_as_1`); an EnableCh_REG that wraps to no channel is reported, on every enabled port and not on a disabled one (`test_an_enable_ch_that_wraps_to_no_channel_is_reported`). Each missing row the decode reads is reported by name, and the others (Description, visualizer-only rows) are not (`test_each_missing_row_the_decode_reads_is_reported`). A file cut at a row boundary names every missing row, 3 listed and the rest counted (`test_a_file_cut_at_a_row_boundary_names_every_missing_row`). A legacy CDS scalar stands in for its per-source row (`test_a_legacy_cds_scalar_stands_in_for_its_per_source_row`). A negative legacy scalar CDS_TailWidth_REG or CDS_DriveType is ignored, gives the registers of the file without the row, and is reported once in BusConfig's words, also when BusConfig and the engine report both read the file; beside a per-source row it is silent (`test_a_negative_legacy_scalar_cds_row_is_ignored_and_reported_once`, `test_a_negative_legacy_scalar_under_a_per_source_row_is_not_reported`). 160,000 distinct unknown keys load and the warning lists the first 3 by name (`test_many_distinct_unknown_fields_are_read_in_linear_time`). A warning listing more than three items reads as BusConfig's, word for word (`test_a_warning_listing_more_than_three_items_reads_as_the_python_importers`); a cell or key that is not UTF-8 is echoed with U+FFFD for each bad byte, and csv_warnings does not raise (`test_a_cell_or_key_that_is_not_utf8_is_echoed_with_replacement_characters`). A quoted cell reads as its text (`test_a_quoted_cell_reads_as_its_text`), and a comma or doubled quote inside one is part of it (`test_a_comma_or_doubled_quote_inside_a_quoted_cell_is_part_of_it`). A hexadecimal RowRate is not a number (`test_a_hexadecimal_row_rate_is_not_a_number`). A RowRate reads with a '.' decimal point whatever LC_NUMERIC, and its warning writes the value read the same way, as BusConfig writes it (`test_a_row_rate_reads_with_a_decimal_point_under_a_comma_locale`, skipped when no comma-decimal locale is installed). Each bad RowRate gives exactly one RowRate warning, with its exact text (`test_a_bad_row_rate_gives_exactly_one_row_rate_warning`). A directory path reports 'Cannot open config CSV' and loads nothing (`test_a_directory_is_not_read_as_an_empty_config`). A warning quotes a cell or key of up to 60 characters whole, and a longer one as its first 59 and an ellipsis, cut on a character boundary (`test_a_long_cell_or_key_is_quoted_cut_to_60_characters`). A SkippingDenominator below 1 (0, a negative one, the smallest int) is reported and reads as 1 (`test_a_skipping_denominator_below_one_is_reported_and_read_as_one`). A boolean cell holding a non-ASCII character (é, É, TRÜE, üno) gives one "not a valid value" warning and reads as false; ASCII case variants read with no warning (`test_a_non_ascii_boolean_cell_is_reported_once_and_read_as_false`). A line ends at LF, CR LF or a lone CR outside a quoted cell: LF, CRLF and CR-only copies of one file give the LF copy's registers and BusConfig's warnings word for word (`test_a_config_csv_reads_alike_with_lf_cr_lf_or_lone_cr_line_endings`). After 0x or 0b the next character is a digit of that base; a sign or space there is reported and reads as 0, as BusConfig reads it (`test_a_radix_prefix_without_a_digit_of_its_base_is_not_a_number`). A RowRate warning writes the value as BusConfig does, the shortest decimal for an unreadable cell and %g for an out-of-range rate (`test_a_row_rate_warning_reads_as_busconfigs`). A 1-bit CDS field over its width carries its low bit (`test_a_one_bit_field_that_does_not_fit_names_its_low_bit`). pattern_period, the manual-SSP period, is the LCM of the enabled ports' pattern periods, capped rather than overflowing (`test_the_pattern_period_is_capped_rather_than_overflowing`).
- `test_native_command_parser.py` — the native command parser on synthetic CDS streams (`_parse_cds_test`, and `Decoder.commands()` on a 4-column bus carrying the stream): a garbled header is reported and the next phase decodes, a garbled DevMask selects no device, commit responses follow the hub depths, each device's commit response is read after its own hub gap (non-uniform depths, device 0 included), a multicast commit shows the device that is not ready, a reserved opcode is named by its number; a phase whose header did not read is "Bad header" in phase "?" whatever its PhaseID token held, and every decoded command carries header_valid (`test_a_bad_header_is_named_as_one_and_flagged_in_the_decode`), and a garbled WriteA32 header reads as 'Bad header' through Decoder.commands() too; a WriteA32 decodes its device mask, packet length, address and data (`test_a_write_a32_header_and_packet_decode`); one byte changed in a Manager Packet or its CRC makes crc_valid False and register_writes skips it, and one changed in a ReadData delivery makes read_data_crc_valid False (`test_a_corrupted_packet_or_read_data_fails_its_crc`); a completed ReadData clears the device's pending read (`test_a_completed_read_data_clears_the_pending_read`); the Device Mask's first token carries devices 8..11 (`test_the_first_device_mask_token_carries_devices_8_to_11`). Each Ping status is read after its own hub gap (`test_each_ping_status_is_read_after_its_own_hub_gap`). A hub depth above `max_hub_depth` (== `session.MAX_HUB_DEPTH`) reads as the limit on Ping and Commit, and a negative one as 0 (`test_a_hub_depth_above_the_limit_reads_as_the_limit`). The Bad-header test garbles each of the six header tokens. Only an SSCR or DSCR commits: a confirmed Commit with a reserved opcode is named by its number, carries is_commit False, commits no staged register and has no effective row (`test_only_an_sscr_or_dscr_commits_the_staged_registers`). `_bus` is the shared NRZS bus-level builder. `decode_symbols` frames hub-aware only when one of Devices 0..11 has a depth above 0: a depth at index 12 or later, or a negative one, frames as no depth (`test_symbol_framing_reads_only_the_depths_of_the_bus_devices`).
- `test_native_decoder_run.py` — `Decoder.run()` framing, resync and windowed-replay rules, and
  the native demo fixtures. `run()` raises on a second call also when the first read no UI; one CRC-valid command confirms a
  forced framing, none is a guess; cancel() and request_stop() from another thread end a long recovered-clock run promptly. A resync or a clock resume into a pinned section searches the pinned count's Column-0 phase and records no guess, and a pinned width the wire does not carry is reported as guessed (`test_a_resync_into_a_pinned_section_searches_the_pinned_count_s_phase`, `test_a_resume_into_a_pinned_section_searches_the_pinned_count_s_phase`); an empty leading window at a restart point records no guessed framing (`test_an_empty_leading_window_at_a_restart_point_records_no_guessed_framing`); an in-range DLV CDS column other than 2 moves the CDS read (`test_a_dlv_cds_column_in_the_row_is_read_there`); a DSCR fixture too short for its block raises a ValueError naming the samples_per_channel passed (0, 3 and 8 are tried), and from the smallest count that holds it each further sample moves the DSCR one interval later (`test_a_dscr_fixture_too_short_for_its_block_raises`, `test_the_dscr_lands_one_interval_later_per_sample`); a commit that changes no port keeps dp1's indices 0..63 and dp0 under an override equal to the unspliced decode; the single-event fixtures decode clean on a long run and at their smallest count (`test_a_long_fixture_decodes_clean`); the blind detector finds every even column count up to 32 (`test_the_column_count_is_detected_up_to_the_widest`); a command whose CRC fails does not place Column 0 (`test_a_command_whose_crc_fails_does_not_place_column_0`); on a tie between Column-0 phases the first even offset is taken (`test_on_a_tie_between_phases_the_first_even_offset_is_column_0`); a config CSV keeps its column count across a clock resume (`test_a_config_csv_keeps_its_column_count_across_a_clock_resume`); the largest-sample-edge decode runs in a child process bounded to 60 s; the PHY3 demo rejects a CDS column off the row or on a port and accepts a free one (`test_the_phy3_demo_rejects_a_cds_column_off_the_row_or_on_a_port`, `test_the_phy3_demo_accepts_a_free_cds_column`); a non-positive grid row count lays out no rows (`test_a_non_positive_grid_row_count_lays_out_no_rows`); a cancel before run() returns without reading the source (`test_a_cancel_before_run_returns_without_reading_the_source`).
- `test_open_capture_dialog_inputs.py` — the Open Capture dialog: figures round before they pick
  a unit, thresholds must be finite, and a typed Link name is compared the way it is validated.
- `test_register_view_fixes.py` — the Registers pane: a context-menu action after a rebuild, the
  Compare overlay's expected value and tooltip, a long Fields list, an action for a register no longer shown does nothing (`test_an_action_for_a_register_no_longer_shown_does_nothing`), and the device dropdown keeps its selection across a rename (`test_the_device_dropdown_keeps_its_selection_across_a_rename`).
- `test_saleae_binary_checks.py` — binary-capture checks: the first block's level field, the
  version-in-wrong-container message, and the export of a capture with two edges on one sample.
  The transitions named in the one-transition-per-sample error are numbered as the channel numbers them (including sample-0 edges), and a bin pair is refused above the maximum sample rate and below 1 Hz. An edge at sample 0 is folded into the initial level. A clock file that is also the data file is refused by name (`test_a_clock_file_that_is_also_the_data_file_is_refused_by_name`); the wrong-container message reads "a .bin pair holds version 0, a .sal holds versions 0, 3 and 4"; the level-field test finds the first block through `_find_v3_chain_start`. A symlink to the clock file is the same file (`test_a_symlink_to_the_clock_file_is_the_same_file`); the same-file test's alias is a string pathlib does not normalise.
- `test_saleae_sal_checks.py` — `.sal` checks: `meta.json` bounds and malformed fields, windows
  (a window that starts before the capture, ends at or before its start or ends past any sample count, malformed, non-finite and fractional windows, the sample-rate argument, and the `export_sal` limits), and the fallback of a failed streamed window
  read.
  A huge sampleRate is noted and taken as absent, and a huge deviceChannel is kept without an error (`test_a_huge_device_channel_is_kept_without_an_error`). A sample rate beyond 1e15 Hz, a non-zip file, a zip with no meta.json or a meta.json that is not readable JSON, a damaged deflate stream and a flip of every byte of a .sal all raise ValueError naming the file (or load); a given sample rate outside the range is refused by file. A clock channel that is also the data channel is refused by file (`test_a_clock_channel_that_is_also_the_data_channel_is_refused_by_file`). The failed-streamed-read test pins the error too: the window is kept, the message says the capture was read whole whatever the window, and the original exception text is named. `export_sal` stays within the sample limit: an exported .sal below it reads back exactly, and an edge past it is refused before anything is written (`test_an_exported_sal_below_the_sample_limit_reads_back_exactly`, `test_export_sal_refuses_an_edge_past_the_sample_limit_before_writing`). The flip-every-byte test asserts the message names the file. A failed `export_sal` leaves an existing file as it was and no temporary one, and creates nothing at a new path (`test_a_failed_export_leaves_the_existing_file_and_no_temporary_one`, `test_a_failed_export_to_a_new_path_creates_nothing`); its limit is the loader's window limit (`test_the_export_limit_is_the_loaders_window_limit`); a window beyond 2**53 cuts on exact sample numbers (`test_a_window_beyond_2_pow_53_cuts_on_exact_sample_numbers`); a fractional window end is cut at the sample it is rounded to (`test_a_fractional_window_end_is_cut_at_the_sample_it_is_rounded_to`).
- `test_session_decode_inputs.py` — Session decode inputs: a re-decode the memory watchdog stops or the user cancels
  leaves the inputs as they were, and apply_config_csv also undoes a CSV that does not read or a decode that fails and
  removes its upgrade temp; a stopped set_ssp_row or lock_ssp_from_sspa restores the SSP row and whether it was derived, and keeps the decode (`test_a_stopped_ssp_re_decode_restores_the_ssp_inputs`); a constructor that fails, a cancelled open included, removes its config CSV upgrade temp (`test_a_failed_constructor_removes_its_config_csv_temp`; it, `test_a_failed_decode_undoes_the_csv_and_removes_its_temp` and `test_a_stopped_ssp_re_phase_after_a_config_csv_undoes_the_csv` remove a file and are marked `platform`); a stopped re-decode keeps the decoder, commands and segments; run_watched raises DecodeCancelled for a cancelled decode and a decoder with no `cancelled` raises; a later bring-up of the other PHY
  family costs no second decode; two identical .wfm channels reach the analog front end; sample_at_ui inverts ui_index;
  bus_rows_for_samples with no segments is row 0; the window reads the inputs through Session.config_csv and
  tx_persist_key, whose key changes when a config CSV is applied; a window cut from a capture read whole follows the .sal window rules; a workspace demo's sample count
  is clamped with a note and from_demo refuses one outside 1..DEMO_SAMPLES_MAX; unreadable source-descriptor entries are
  left out with one note per field (a label-field value that is not an integer 0..7 included). A register override the binding refuses leaves the Session as it was (`test_an_input_the_binding_refuses_leaves_the_session_as_it_was`); a stopped SSP re-phase after a config CSV undoes the CSV (`test_a_stopped_ssp_re_phase_after_a_config_csv_undoes_the_csv`); .sal and a cut read one window alike, a fractional bound is rounded and noted, an integer bound is taken as it is however large, an end past any sample count reads to the end, and a window with no transition is noted (`test_sal_and_a_cut_read_one_window_alike`, `test_a_fractional_window_bound_is_rounded_and_noted`, `test_an_integer_window_bound_is_taken_as_it_is_however_large`, `test_a_window_end_past_any_sample_count_is_read_to_the_end`, `test_a_window_with_no_transition_is_noted`); a window in seconds whose sample number overflows a float reads to the end with a note, and a window_s that is not a pair of numbers raises ValueError naming window_s (`test_a_window_in_seconds_past_the_float_range_is_read_to_the_end`); a malformed source field raises naming it (`test_a_malformed_source_field_raises_naming_it`); a column pin or forced count outside 2..32 is left out with a note, and the range is the core's (`test_a_column_pin_outside_the_range_is_left_out_with_a_note`, `test_a_forced_column_count_outside_the_range_is_detected_with_a_note`, `test_the_column_range_repeats_the_cores`); an analog CSV channel the file lacks names the file and its channels (`test_an_analog_csv_channel_the_file_lacks_names_the_file_and_its_channels`); exact values of last_sample, total_bus_rows, rate_regions, the two row-numbering conventions on a Row Sync Point edge, and the DLV row clamp (`test_the_last_sample_is_the_last_edge_on_either_line`, `test_the_row_count_covers_the_row_of_the_last_sample`, `test_a_region_is_rated_over_its_whole_rows_only`, `test_a_region_needs_two_whole_rows_between_its_ends`, `test_the_regions_start_at_the_audio_start`, `test_a_sample_on_a_row_sync_point_is_numbered_by_each_convention`, `test_a_dlv_row_past_the_last_row_sync_opens_at_the_last`). A source or decoder that fails to build leaves the decode (`test_a_source_or_decoder_that_fails_to_build_leaves_the_decode`). A demo phy or variant the demo does not build is noted (`test_a_demo_phy_or_variant_it_does_not_build_is_noted`). With no recovered Row Sync Point, a DLV row counts from Column 0 (`test_a_dlv_row_with_no_recovered_row_sync_counts_from_column_0`). The signed value is two's complement at the most negative value (`test_the_signed_value_is_twos_complement_at_the_most_negative`), and 1-bit PDM reads ±1 (`test_a_one_bit_pdm_value_reads_as_minus_or_plus_one`). Out-of-range delay_samples/ssp_row, non-text paths and non-mapping map fields raise naming the field. Non-path region CSV entries are left out with a note. A decode-input key outside the core's range (a section index outside 0..2**31-1, a hub-depth device outside 0..11, a scrambler key that is not a (device -1..11, port 0..2**31-1) pair) is left out with a note, at construction and in the setters (`test_a_decode_input_key_outside_the_cores_range_is_left_out_with_a_note`, `test_a_bad_key_given_to_a_setter_is_left_out_with_a_note`, `test_a_key_past_the_cores_range_is_left_out_before_the_decode`); a hub depth that is a fraction and a scrambler-override key with a fractional part are left out with the note, whole-valued floats kept. DEMO_SAMPLES_MAX is `swi3score.max_demo_samples`, and every `make_*` generator of the core refuses a longer stream (`test_the_demo_cap_is_the_native_generators_bound`). A DLV region exports the row rate rate_regions measures for it (`test_a_dlv_regions_exported_row_rate_is_that_regions`); a sample in an unrated DLV region exports the decoder's row rate (`test_a_dlv_sample_before_any_rated_region_exports_the_decoders_row_rate`). A failure after the decode ran keeps the previous decode whole (`test_a_failure_after_the_decode_ran_keeps_the_previous_decode`). An analog CSV clock and data on one channel, and a .wfm pair naming one file, are refused before reading (`test_an_analog_csv_clock_and_data_on_one_channel_is_refused`, `test_a_wfm_clock_and_data_in_one_file_is_refused`). A source identifier, flag or threshold that does not read raises naming the field (`test_a_source_field_that_does_not_read_raises_naming_it`, `test_a_threshold_pair_reads_as_given`). The forced-DLV grid replays from the last Cold Start (`test_the_forced_dlv_grid_replays_the_config_since_the_last_cold_start`). An unknown value-filter operator raises, and each known one keeps what its comparison passes (`test_an_unknown_value_filter_operator_raises`, `test_the_value_filter_keeps_the_samples_its_comparison_passes`). section_ui_stats leaves out idle gaps (`test_section_ui_stats_leaves_out_idle_gaps`). A DLV sample belongs to the UI whose sample point is nearest (`test_a_dlv_sample_belongs_to_the_ui_whose_sample_point_is_nearest`). A workspace demo sample count that is not a whole number is refused (`test_a_workspace_demo_sample_count_that_is_not_a_whole_number_is_refused`).
- `test_timing_diagram_layout.py` — the term diagram's layout rules: a row holds the edges of one signal at one pin
  with alternating edges, over every leg at six inputs, a Peripheral-to-Peripheral panel names its driver "Per A", a reference label sits away from
  the resting edge, every panel paints within the height reserved for it (the maximum over all panels), and the labels of points on no drawn ramp stand on one line each (two at most, the second one `strip_pitch` lower), end `_MARK_MARGIN` lines short of the first row and above that row's level labels. The same check holds with every glyph widened to 0.82 line heights, as on a platform whose fonts run wide: a label wider than its row's gaps takes a taller row, one wider than the room beside its point wraps, and a heading wider than the plot wraps within it (`test_no_text_overlaps_another_or_a_line_with_wide_glyphs`). A box whose padded edge meets a placed box's edge or lies along a line is free at every panel top, however the top and the offsets round (`test_a_box_tangent_to_a_placed_box_or_line_is_free_wherever_it_sits`). No text overlaps another or a line, and all stays in its plot (`test_no_text_overlaps_another_or_a_line_and_all_stays_in_its_plot`): a recording painter collects every text box, line segment and dot each panel paints, over every leg and term at the six row inputs, the EDE pairing and a SoundWire Manager (the page's two columns at their worst corners), stacked at 380 and 900 px and side by side at 1580 px at the minimum height and 1000 px tall; no two text boxes intersect, no line or dot lies inside a text box (a leader line starts off its label's box), and every box lies within its panel's plot. A panel laid out from tops a fraction of a pixel apart places every label at the same spot relative to its top (texts equal, corners within 1e-5 px), a label whose padded box is tangent to its own line and a label on the fallback grid included, over every leg and term stacked at 380 and 900 px and side by side at 1580 px (`test_a_panels_labels_take_the_same_spots_wherever_the_panel_sits`, one case per layout). A signal row is `_row_floor` at the minimum height, grows with the pane and stops at `_ROW_ASPECT` of the column's width (`test_a_row_is_the_floor_grows_with_the_pane_and_stops_at_the_aspect_cap`). A shown diagram lays nothing out unseen while width steps follow each other and finds the floor at the new width `_FLOOR_DELAY_MS` after the last; a show_term drawing the same panels keeps the floor (`test_a_shown_diagram_finds_its_floor_once_a_width_change_settles`); the width steps run with the timer's interval held long, so the check depends on event order, not on how fast a step paints.
- `tests/timing_diagram_support.py` — `sections` and `keys`, shared by the two diagram suites (a helper, not a test file).
- `test_timing_view_page.py` — the Timing page: the Manager-forces-keeper switch (keeper_Man's
  rows, summary line and diagram panels say it does not apply, keeper_Per is unchanged, the
  flag round-trips, a non-boolean one is reported and read as false, a missing one is false,
  and Reset clears it); the spec selectors (both start on PHY2 and leave out only the proposed
  revision), term keys (every printed EDE Delay and t_DZ term keys on its own input row, `test_an_ede_release_term_keys_on_the_row_it_reads`; a SoundWire anchor with a trailing `,fast` keys on its row, `test_a_soundwire_anchor_priced_on_the_fast_edge_keys_on_its_row`) and the washes of the rows an EDE release reads (a click on each of the releaser's t_DD, t_RF DATA and t_EDE terms washes the input row of that name, and UI washes none; `test_each_row_an_ede_release_reads_washes_its_term`), restored and loaded settings (a value that is
  not a number is kept and reported, a handover_ui that is not true or false is reported and the box follows the selected pair (`test_a_saved_handover_flag_that_is_not_a_boolean_is_reported_and_follows_the_pair`), a number saved as text is kept as it was and reported (`test_a_saved_number_given_as_text_is_reported_as_not_a_number`), a range with its minimum above its maximum is exchanged and reported, Save and Load
  Example as JSON, a non-JSON file refused without a YAML reader), the hints (trace, handover, launch limit), the
  diagram pane (closes when its term is cleared, a maximized diagram is put back, neither splitter side collapses, the
  Corner line), an F_max below the rate in red, and one that never fails ('—') unmarked on setup and hold legs (`test_an_f_max_below_the_rate_is_red_and_one_that_never_fails_is_not`), a parenthesised negative coefficient in the substitution
  row, and the summary text; the summary's header names the columns the page shows: Spec/Example on a plain bus, PHY2 Proposed/EDE Proposed with both sides on the EDE source, and 'Spec / PHY2 Proposed'/'Example / EDE Proposed' with one (`test_the_summary_header_names_the_columns_the_page_shows`). A column heading names only what its column holds on every row: over the four pairings of plain and EDE sides, a side off the EDE source holds the spec as it stands and its Example, a side on it the PHY2 proposal and EDE (`test_a_column_heading_names_only_what_the_column_holds_on_every_row`). A per-edge slew tolerance of 100 % shows "No margins" in the tree, the advisory line and
  summary_text, is reported as an unevaluable-settings import note on load, and the margins return when the value is set
  back (`test_a_slew_tolerance_of_100_percent_shows_no_margins_and_recovers`). An Example range typed with its minimum
  above its maximum is exchanged on editingFinished, the advisory line says so, the margins equal those of the ordered
  range, and the note clears on the next range edit
  (`test_a_typed_minimum_above_the_maximum_is_exchanged_when_editing_finishes`); an inverted range gives the exchanged range's margins before editing finishes (`test_an_inverted_range_gives_the_margins_of_the_exchanged_one_before_editing_finishes`). The handover hint names the EDE source
  for a zero-UI handover (`test_the_handover_hint_names_the_ede_source_for_a_zero_ui_handover`). Where the columns' terms differ the inequality shows an equation row per column, headed by its label, each followed by its own substitution row, over the four pairings of plain and EDE buses (`test_a_leg_whose_columns_differ_shows_an_equation_row_per_column`); across every inequality and every pairing of the spec selectors no row holds a device's t_DZ and its EDE Delay together, and every term of every row is an anchor (`test_no_equation_row_holds_a_release_by_t_dz_and_by_an_ede_delay_of_one_device`); a plain bus has one row per inequality (`test_a_plain_bus_shows_one_equation_row_per_inequality`); each substitution sums to its column's margin within the rounding bound of the row's own printed numbers (`test_each_rows_substitution_sums_to_its_columns_margin`); the summary's Terms block follows the same rule after the margin table's blank line (`test_the_summary_prints_an_equation_line_per_column_where_the_columns_differ`). A range typed upward, minimum first, is not exchanged between the boxes while it is typed, and is checked once focus leaves both boxes of its row (`test_a_range_typed_upward_box_by_box_is_not_exchanged_between_the_boxes`). With the page or the diagram maximized, an input the model cannot evaluate shows its 'No margins' reason above the results, and the term diagram closes even when it is maximized (`test_an_unevaluable_edit_on_a_maximized_page_is_explained_above_the_results`, `test_an_unevaluable_edit_closes_the_term_diagram_even_when_it_is_maximized`); maximizing the diagram with no term drawn leaves the page as it is (`test_maximizing_the_diagram_with_no_term_drawn_leaves_the_page_as_it_is`); a click on a Margin or a Max Frequency value keeps the traced term (`test_a_click_on_a_margin_or_a_max_frequency_value_does_not_clear_the_term`); the summary names the specification column 'Spec' throughout and fills its F_max difference column (`test_the_summary_names_a_column_one_way_throughout_and_fills_its_f_max_difference`); a clocked mode's launch hint names the driven column and the one keeping its analog delays by their headings; in analog mode on an EDE bus it says both columns use the tabulated ranges, and its seeding sentence appears only where the Manager is on the EDE source (`test_the_launch_hint_names_the_columns_the_page_shows`). With every MP leg holding to the toggle ceiling under a 2x launch the hint reads bound by t_RF (`test_the_launch_hint_is_bound_by_t_rf_where_every_mp_leg_holds_to_the_ceiling`); the limiting leg is judged against the toggle ceiling of its own worst-corner inputs (`test_the_launch_limit_reads_each_legs_own_toggle_ceiling`). Save Example writes through a temporary sibling file: a failed write leaves the previous file whole, leaves no temporary file behind and shows the error (`test_save_example_leaves_the_earlier_file_whole_when_a_write_fails`). Save Example stages in a new, uniquely named sibling file, so a file of the user's named '<target>.tmp' is neither overwritten nor removed, on success or failure (`test_save_example_stages_in_a_new_file_and_leaves_the_users_own_files_alone`). Load Example's size cap counts bytes on disk, so a file of multibyte characters over the cap is refused (`test_load_example_caps_the_file_by_its_bytes`). An import warning cuts a saved value's repr to 60 characters, so an oversized value in a settings file cannot make the warning oversized (`test_an_import_warning_shows_a_saved_value_at_a_bounded_length`). A negative coefficient in the substitution row prints with a true minus in the two-factor and gathered forms, and a coefficient that rounds to zero carries no sign (`test_the_substitution_row_signs_every_coefficient_the_same_way`). Names this version does not read are cut to 60 characters in the import warnings, setting names and row names alike (`test_names_this_version_does_not_read_are_cut_in_the_warning`), and list the first three names and count the rest (`test_names_this_version_does_not_read_are_listed_three_at_a_time`). Save Example asks before a `.json` it adds replaces an existing file (`test_save_example_asks_before_an_added_suffix_replaces_an_existing_file`). A switch to a SoundWire Manager spec with a clocked launch selected runs exactly one recompute (`test_switching_to_a_soundwire_manager_with_a_clocked_launch_recomputes_once`). The launch grid's duty is `CalcInputs().duty_min` (`test_the_launch_grid_is_placed_at_the_models_duty`).
- `test_tool_command_lines.py` — the command lines and listings of the small tools: a mistyped
  option is refused, a non-ASCII path is read, a bad tree is an error, and the launchers and the
  bundle spec carry what they promise.
  The gate refuses an unknown option, --guest with --release, --quick with a stage and --on-guest without one before any check runs, and accepts every option it documents; the native C++ suite's runner comes from $SWI3S_NATIVE_CPP_SUITE, is run with bash, and its exit status is the check's, while a variable naming no file is NOT RUN (`test_the_native_cpp_suite_runs_the_script_the_variable_names`); leak_scan finds a leak in a file with a non-ASCII name (git configuration isolated) and fails on a listed file it cannot read and scans a revision from a root below the repository's top level (`test_a_revision_is_scanned_from_a_root_below_the_repositorys_top_level`), and its private-address patterns separate addresses (10/8, 192.168/16, 172.16/12) from section citations, a four-part citation by its section sign; native_coverage reports only this checkout's sources, refuses a bad command line (an empty or dash-led --out value included), names a failed llvm-cov report or show step with its diagnostic (`test_a_failed_llvm_cov_step_is_named_with_its_diagnostic`), names its report by its absolute path where it is on another drive than the checkout (`test_a_report_on_another_drive_than_the_checkout_is_named_by_its_absolute_path`), shows the suite's stderr when the suite cannot start against the instrumented core (`test_a_suite_that_cannot_start_against_the_instrumented_core_shows_its_stderr`) and removes its working directory whether the build fails, passes or the measurement raises, and the gate reports its exit 3 as NOT RUN and its exit 0 as neither, and does not blame a failure on the build alone; changed_tests, on a synthetic checkout, maps a module imported by name to its tests, a changed test file or `tests/` helper to the test files importing it, directly or through another test file (`test_a_changed_test_file_or_helper_maps_to_the_test_files_importing_it`, `test_the_importing_tests_follow_a_chain_and_every_spelling_of_an_import`), and a `tools/` script, data file or `tests/` shell script to the test files naming it, a data file also through the tool naming it (`test_a_changed_tool_script_data_file_or_launcher_maps_to_the_tests_naming_it`), and a fixture, golden or `data/` file to the test files naming it, a `data/` file also through the package module naming it (`test_a_changed_fixture_or_data_file_maps_to_the_tests_naming_it_or_its_reader`), refuses a bad command line, and an unresolvable --base is an error; a test file past its time limit is a named failure and is killed with every process it started (the grandchild checked on every platform, through the task list on Windows), a check whose process outlives `_STAGE_TIMEOUT` is a named failure and the gate goes on (`test_a_check_whose_process_outlives_the_stage_limit_is_a_named_failure`), and an interrupted suite kills the files still running (a zombie grandchild that no init process reaped counts as gone); the gate decodes every child's output as UTF-8 by name, not by the locale, with an undecodable byte replaced (`test_a_files_output_is_decoded_as_utf8_and_a_bad_byte_is_replaced`); the durations record is kept per scope (`<platform>.txt`, `<platform>-guest.txt`) and a line that does not parse is skipped (a file whose bad line is skipped ranks by its size); screenshots, test_report, package_smoke and mypy_gate refuse an unknown option or an option without a value (an empty one included) on stderr before doing any work, test_report refuses a missing --data file, and a --data file that is not a coverage database or holds no per-test branch data, and closes its database when a query fails, a screenshot that cannot be written is a finding, and the contact sheet orders by file name; name_review, on an empty baseline so it runs in a plain copy, refuses a missing, empty or unlistable tree; the bundle spec ships the licence of the interpreter it freezes under licenses/Python/ and stops without one (`test_the_bundle_ships_the_licence_of_the_interpreter_it_freezes`), takes its runtime requirements from requirements.txt, stops when one is not installed or a distribution has no notice, ships every numpy notice at its own path, and every same-named notice of one distribution under its own path (a synthetic distribution), maps every Qt for Python wheel of PySide6 6.11 and 6.12 (PySide6_WebEngine and PySide6_Pdf included) to data/licenses/Qt-for-Python/, the FFmpeg notice and LGPL-2.1 text ship with the QtMultimedia import and not without it (`test_the_bundle_ships_the_ffmpeg_notice_and_lgpl_2_1_text_with_qt_multimedia`, `test_the_bundle_ships_no_ffmpeg_notice_without_a_qt_multimedia_import`), and the installed runtime closure resolves; pybind11 has one floor; run.ps1 stops on a failed install step, setup_dev.ps1 stops when the virtual environment is not created (`test_setup_dev_ps1_stops_when_the_virtual_environment_is_not_created`), and run.ps1 opens no window for the spellings run.sh treats as headless; the setup scripts name the commit gate; data/registers.json flags excess-1 only on a split field's least significant part (`test_a_split_register_field_is_excess_one_on_its_least_significant_part_only`). convert_174 names a legacy CSV it cannot convert on stderr, converts the rest of a directory and exits 1 (`test_a_legacy_csv_that_cannot_be_converted_is_named_and_the_rest_are_converted`). Any other changed file outside docs/ (example data, package data, workflow and top-level files) maps to the test files naming its file name or path, and a path no test names is listed as not mapped (`test_any_other_changed_file_outside_docs_maps_to_the_tests_naming_it`). `changed_files` reads a path with non-ASCII characters unquoted (`test_changed_files_reads_a_path_with_non_ascii_characters_unquoted`), and `changed_tests` reads each test file once however many paths changed (`test_changed_tests_reads_the_test_files_once_however_many_paths_changed`). The legacy converter names a file it cannot convert once and reports each failure as an error line, converting the rest (`test_a_legacy_csv_that_cannot_be_converted_is_named_and_the_rest_are_converted`, `test_the_converter_names_a_file_once_and_reports_each_failure_as_an_error_line`); it warns about a file with no field and reads an upper-case `.CSV` extension (`test_the_converter_warns_about_a_file_with_no_field_and_reads_an_upper_case_extension`). A tool terminated by SIGTERM or SIGHUP is interrupted so its children are killed (`test_a_terminated_tool_is_interrupted_so_its_children_are_killed`). A failed native build prints the end of its output (`test_a_failed_native_build_prints_the_end_of_its_output`). The kill tests assert that the processes are gone, not how long it took.
- `test_ui_pane_robustness.py` — small pane behaviours: tolerant view-state restores, the Filter
  & Gain opening gain, the Statistics refill, TSV copy of a ragged selection, the CDS running
  disparity of a window, line-style preferences, the register-map import report, the register-
  history summary, the notification list, the timeline axis colour and the exit sweep's threads (the subprocess test is marked `platform`).
  View-state restores report one note per pane (a Commands sort replaced by capture order has its own) and never raise on Infinity, huge integers or wrong types (audio, capture, samples, registers incl. open blocks and widths, statistics, timing pane); whole_number / bounded_number / valid_width; Commands column-width reporting; command-table helpers (device list, payload, implicit AND before '('); the Decimal value box parses ASCII whole numbers and marks itself without filters() changing it; the Open Capture dialog shows a file name as text. A Commands sort order that is not the whole number 0 or 1 (Infinity, 1.5, '1', True) is reported and the rows go back to capture order. A saved Commands filter reads on/off entries only as true or false and its text only as text, and reports anything else. The register-map JSON summary counts fields (example_amp.json). The authoring entries accept a dot and refuse a comma decimal mark and a group separator under a German default locale. A Registers column the user widened keeps its width through rebuilds. The light-palette axis test reads the pen each ruler label is drawn with (dark against the light plot) and restores the palette it started with.
  A Commands header with non-text hidden or columns entries gives one note naming the field, and the text entries of a partly bad hidden list are applied. The register-history summary escapes '&' as well as '<' and '>'. The Notifications 'No issues detected' line takes each theme's green. The Compare tooltip names the bus only for a value the bus wrote or read back; a register the bus never wrote is described as never written, with its reset value. Timeline ruler: the step is the next 1/2/5×10ⁿ; a label takes the capture's s/ms/µs/ns unit (exactly 1 s, 1 ms or 1 µs long reads in that unit) and decimals from the step, at most 6; an off-view pixel is clamped to just past the widget; the view keeps its 16-sample minimum at either end of the capture; a domain is at least one sample wide; a bookmark line is grabbed up to its pixel tolerance. A whole-valued fraction (120.0, 1.0) reads as a Registers column width and device. A missing or malformed saved Registers filter leaves the pane's filter. The parentless widgets each test builds are deleted after it. Every authoring row's range fits the data/registers.json field it sets, Spacing 0..15 and Skipping Denominator 1..4095 (`test_every_authoring_range_fits_the_register_field_it_sets`). The Flow dialog marks a bad entry in the theme's error colour (`test_the_flow_dialogs_bad_entry_is_marked_in_the_themes_error_colour`). Force value refuses what is not a byte 0..0xFF with a message and asks again, and a cancel after a refusal emits nothing (`test_force_value_refuses_what_is_not_a_byte_and_asks_again`, `test_force_value_cancelled_after_a_refusal_emits_nothing`). Every Registers Addr cell tips its full address (`test_every_registers_address_cell_tips_its_full_address`). The Compare tooltip says 'the bus read back' for a read-back register. A Commands 'hidden' that is not a list leaves the columns as they are (`test_a_commands_hidden_entry_that_is_not_a_list_leaves_the_columns_as_they_are`). A Samples value or op that is not text is reported and the pane keeps its own, while one that reads is applied in one pass (`test_a_samples_value_or_op_that_is_not_text_is_reported_and_the_pane_keeps_its_own`, `test_a_samples_value_and_op_that_read_are_applied_in_one_pass`). A Commands filter kinds/devices/groups entry that is not a list is empty and reported (`test_a_commands_filter_set_that_is_not_a_list_is_empty_and_reported`). Statistics folds keep their text entries and report the rest (`test_statistics_folds_keep_their_text_entries_and_report_the_rest`). A Commands header note says which entries were left as the pane has them and which were applied where they read (`test_a_commands_header_note_says_which_entries_were_left_and_which_applied`). A Samples entry with bad ports or channels still applies its op and value (`test_a_samples_entry_with_bad_ports_still_applies_its_op_and_value`). A saved Timing region the pane lacks is named in the note, and one it has is applied (`test_a_saved_timing_region_the_pane_lacks_is_named_in_the_note`). Force value reads decimal with leading zeros ('08', '010') and 0x/0o/0b prefixes, and refuses '_' and non-ASCII digits (`test_a_register_byte_reads_decimal_with_leading_zeros_and_prefixed_bases`), and survives a tree rebuild while its prompt is open (`test_force_value_survives_a_rebuild_while_its_prompt_is_open`). The Commands device and commit-group filters pass a command matching any one selected (`test_the_device_and_group_filters_pass_a_command_matching_any_one_selected`). The timeline's view slice takes both 5 % margin edges and nothing past them (`test_the_view_slice_takes_both_margin_edges_and_nothing_past_them`); ruler labels read ms at 5 ms and 10 ms and µs under 1 ms. The neutral-first CDS window checks both rows. A refused Force value says what reads and names the 0x, 0o and 0b prefixes (`test_a_refused_force_value_says_what_reads_and_names_the_prefix`). An imported config value without a Compare overlay tips no expected config (`test_an_imported_config_value_without_a_compare_overlay_tips_no_expected_config`). The constructor patching here uses a private MonkeyPatch.
- `test_native_fcp_start_column.py` — a config-CSV decode of the flow-control demo started at
  each column of a row places every DRQ at the same distance from the samples it gates as
  the wire decode does (the Flow Control Port shares the data port's Column-0 phase). A commit (SSCR or DSCR) that changes only FCP_HorizontalStart, FCP_Offset and FCP_BitWidth (0..3, a wide sink DRQ sampled on its last UI), with TX_PRESENT following the DRQ history from the port's enable across the commit, moves the DRQ the decoder reads: every handshake check passes, the audio is the sine, and the DRQ-to-sample distance moves by the placement change (`test_a_committed_fcp_placement_moves_the_drq_read`, on `make_fcp_move_levels` and its `fcp_bit_width`). Interval T's TX_PRESENT is checked against DRQ T−d for d = 1, 2, with the DRQ before or after the data window (`test_the_drq_history_reaches_back_d_intervals`). On a scrambled flow-controlled port TX_PRESENT passes through the channel's descrambler (`test_a_scrambled_port_descrambles_its_tx_present`). A DRQ whose fcp_bit_width + 1 UIs reach Columns 2..14 of Rows 2..4 is rejected (`test_a_drq_reaching_into_the_data_window_raises`); a count too small for the setup (65 with the default arguments, 64 with a commit_row_offset of 6 or more, 75 with reenable or columns_after 32) reports the count passed (`test_a_count_too_small_for_the_setup_reports_the_count_passed`).
- `test_native_fcp_drq_history.py` — a flow-controlled port's DRQ history runs from the port's
  enable ({ASW5204}, Table 160 (v1.1 r10)), on `make_fcp_move_levels`, whose d DRQs before the
  commit are 0 at its default length (read off the wire as the test premise). An SSCR or a DSCR
  that moves the FCP while the port stays enabled carries the history: the first d intervals after
  it carry TX_PRESENT 0, the decoder passes every handshake check and the audio is the sine, for d
  = 1, 2 and the DRQ before or after the data window
  (`test_a_config_changing_commit_carries_the_drq_history`; a decoder that cleared the history at
  the SSCR fails every SSCR case). With `reenable` a commit disables the port and the next enables
  it again: its first d intervals carry TX_PRESENT 1, every handshake check passes, and the audio
  before and after starts from the sine's first sample
  (`test_re_enabling_the_port_restarts_the_drq_history`). An SSCR or DSCR that also commits 32 columns rebuilds the port and carries the history (TX_PRESENT 0 for the first d intervals, every check passing, the audio the sine) (`test_a_commit_that_changes_the_column_count_carries_the_drq_history`). An SSCR landing 1, 8 or 15 rows into the interval (`commit_row_offset`) restarts the history: TX_PRESENT 1 for the first d intervals, every check passing (`test_an_sscr_part_way_through_an_interval_restarts_the_drq_history`). With FlowMode overridden to TX_CONTROLLED in the 16-column region, the commit to 32 columns gives the port its DRQ and the decoder fails exactly the first d checks against the carried wire values (`test_a_port_whose_flow_mode_gains_the_drq_at_a_commit_restarts_its_history`). A commit_row_offset outside 0..15 or with dscr/reenable, and a columns_after other than 16 or 32 or with reenable, raise ValueError (`test_a_commit_row_offset_or_column_count_outside_the_fixtures_range_raises`).
- `test_native_fcp_guard_tail.py` — the guard and tail of a source DRQ (the FCP of a sink
  data port), on `make_fcp_guard_tail_levels`. An SSCR or DSCR commits FCP_TailWidth (each of 0..3), FCP_GuardEnable and FCP_GuardPolarity with or without a new FCP placement, on an RX_CONTROLLED and an ASYNC port: the decoder's grid places the DRQ cells, then one guard cell only when FCP_GuardEnable is set, Guard1 or Guard0 as FCP_GuardPolarity selects, then FCP_TailWidth Tail cells, all at the committed placement, every handshake check passes and the audio is the sine; the capture drives each guard UI to its polarity (`test_the_guard_and_tail_follow_the_committed_fields`). With the fixture's first config's guard enabled (`first_guard_enable`, `first_guard_polarity`), a commit that changes FCP_GuardPolarity alone turns the guard cell from Guard0 to Guard1 or back and the guard UI's wire level with it, by SSCR and by DSCR, every handshake check passing and the audio the sine (`test_a_polarity_only_commit_with_the_guard_on_flips_the_guard`); the two arguments default to the guard-off fixture. The commit that changes none of FCP_TailWidth, FCP_GuardEnable and FCP_GuardPolarity at the unmoved placement is included: the fixture's TX_PRESENT follows the DRQ history from the port's enable across every commit, as the decode reads it. A width outside 0..3, or a cell outside the fixture's columns or on the data window, raises ValueError, each case at a length that builds and with its own message, 'fcp_bit_width and fcp_tail_width must be 0..3' or 'must lie in Columns 1..15 and Rows 0..15' (`test_a_field_or_cell_outside_the_fixtures_range_raises`). A count too small for the setup (the minimum is 86) reports the count passed (`test_a_count_too_small_for_the_setup_reports_the_count_passed`). The out-of-range cases include an fcp_h_start at or near the largest int.
- `test_release_gate.py` — the gate is one definition, CI and the gate stay in step, the
  wrappers (`gate.sh`, `gate.ps1`, `run_all.sh`) hold no logic, `--only` is never read as
  the gate, the release docs describe reality, and pyproject.toml is configuration
  setuptools accepts, listing every package and every asset a wheel must carry, the CI lint
  job pins the baseline's mypy and compares, the mypy ratchet reads no site-packages, the
  publish leak check fails without site patterns or when the scan read fewer files than the
  tree holds, the gate reports an empty pattern file as NOT RUN, a mypy run stopped by a
  blocking error is refused, publish check 6 compiles the core from the assembled tree's own
  sources, with no $SWI3S_NATIVE_OUT or $SWI3S_NATIVE_FLAGS from the caller, every ci.yml job sets its own timeout-minutes (`test_every_ci_job_has_a_time_limit`), the mypy ratchet compares each platform with its own baseline counts (a rise on
  a platform below another platform's count fails), `--update` records per-platform counts
  and the checked-in baseline holds them for every platform, publish check 5 runs this
  checkout's leak scan on the assembled tree, a symlink to a directory is counted by check 5
  and resolved by check 2, publish_tree's usage says a tree with no sha is built from HEAD
  and uncommitted tracked changes are reported, and the gate's native probe asserts which
  swi3score it imported; every released version kept its notes: v3.0.17.md always, and the notes of every v3 tag from 3.0.17 on that is in HEAD's history and whose own tree carries them, where git and the tags are present; a tag on a side branch or on a commit without its notes adds none (`test_only_a_released_tag_in_the_checkouts_history_needs_its_notes`); the aggregation test requires every release-stage check to run exactly once. The gate builds the core without the caller's build overrides (`test_the_gate_builds_the_core_without_the_callers_build_overrides`); check 6 names the failing tests and bounds the suite (`test_check_6_names_the_failing_tests_and_bounds_the_suite`). A timed-out assembled-tree suite takes its process group with it (`test_check_6_timeout_kills_the_processes_the_suite_started`). `tools/build_overrides.py` names every variable `native/build_local.sh` reads that changes the binary, and `gate.py` and `publish_tree.py` remove them (`test_the_build_overrides_name_every_variable_build_local_reads`). The type ratchet asks mypy for plain output with FORCE_COLOR removed from its environment, so a coloured pipe still parses (`test_the_ratchet_reads_plain_output_and_refuses_output_it_cannot_read`). The gate and the publish tool print a character their console cannot encode (a cp1252 log, '▸') as '?' and carry on (`test_a_character_the_console_cannot_encode_is_printed_not_raised`).
- `test_collection.py` — every test file collects at least one test, and the suite check
  visits every file pytest collects.
- `test_test_strategy.py` — the tooling of §1.1: a test file that builds widgets is marked
  `gui`; the guest scope runs `gui` and `platform` tests and the `core` smoke files, with the
  spread files first; `tools/changed_tests.py` maps a change to the test files it touches;
  `tools/test_report.py` reads per-test lines from a coverage data file; and
  `tools/native_coverage.py` reports it cannot run where clang's tools are missing.
- `test_testing_doc.py` — this document names every suite, and none that is gone.
- `test_leak_scan.py` — the structural leak patterns catch real leaks (escaped Windows home
  paths, Linux home paths, macOS volume, iCloud Drive and container paths, and the scanned
  tree's own absolute path) and not prose. A stray NUL does not turn a text file into UTF-16,
  and UTF-16 is read with or without a byte-order mark. The working-tree scan includes
  untracked, non-ignored files. An exported tree is walked whole except what its own
  .gitignore excludes. A pattern file holding no patterns is reported as covering nothing.
  A symlink to a directory is listed as a file, not descended into, and scanned by its
  target path (skipped where the platform or account refuses symlinks, through conftest's `symlink_or_skip`). `--tree=DIR` walks a directory with the scanner's own patterns, including the
  absolute path of the scanner's checkout; `--tree` with a sha, with `--text=`, or naming no
  directory is refused. Every test runs under `isolated_git`, so git works on the scratch tree whatever GIT_DIR or an enclosing repository says.
  An empty tree, a second positional argument and a <sha> given with --text= are refused; a directory the scan cannot list fails it; the BOM-less UTF-16 detection thresholds (40 % of pairs, 5 % on the other side, BOMs, 90 % printable) and the 4-byte printable-run minimum are pinned.
- `test_publish_sync.py` — `tools/publish_tree.py`'s check 3b on a throwaway upstream
  repository: no recorded sync point, upstream commits not taken in (listed), a sync point
  that is unknown or rewritten out of upstream's history, a contributed sentence undone beside
  the release's own edits, a contributed deletion whose removed line is back (re-wrapped included), unless the commit itself kept that line elsewhere, a contributed file missing, and a file a contributor deleted or renamed away that the tree still carries with its deleted text (`test_a_file_the_contributor_deleted_or_renamed_and_the_tree_carries_is_a_revert`) are refused, in every recorded
  window; a path reused for other text passes; the contributed text kept, re-wrapped with CRLF line ends, or edited later passes, a
  publish commit is not held against the tree, and none of the upstream-owned files is named as a dangling reference.
  `--record-sync` refuses until the contributed changes are in the working tree, appends once,
  and takes only a publish commit as the first point, and refuses a last point that is not on the upstream branch or not a commit here (`test_record_sync_refuses_a_last_point_that_is_not_on_the_upstream_branch`); a point appended to a file without a final newline starts a line of its own. Also the `docs/releases/next.md` rules:
  a release tree carrying it is refused, an empty notes file is a named problem, a preview describes itself from it, and an open one is
  marked `(in development)`. And check 7 (`--check-published`): a pushed branch that is the
  assembled tree passes, a hand-edited, missing or extra file is named, a file whose mode
  differs is named, a symlink is compared by its target whatever the files it points to hold (skipped where the platform or account refuses symlinks, through conftest's `symlink_or_skip`),
  a tree built where git converts line ends (core.autocrlf=true) holds the bytes git stores,
  and an unresolved ref is refused. `--check-published=<ref>` runs check 7 alone and exits
  with its status, compares the modes git records (a grafted file takes the upstream ref's),
  lists 20 differences and counts the rest, and a value option given without `=<value>`, a
  flag given a value and an unknown option are refused before anything is built.
  The throwaway repositories ignore the machine's global and system git configuration; the exec-bit tests assert git's recorded modes on Windows. A file grafted from upstream is exempt from check 2's dangling-reference test. A publish build that stops midway removes the assembled tree (unless --keep) and the native core's work directory. The mode-from-git test is skipped on Windows, where files carry no execute bit.
- `test_publish_tiers.py` — the three content tiers stay apart. Every published file,
  whatever its type, is checked for references to the internal tier, by path or by a
  relative Markdown or HTML link or a symlink resolved from the citing file (skipped where the platform or account refuses symlinks, through conftest's `symlink_or_skip`), by the tier test
  and by publish_tree's check 2. No tracked file is a document format the application does
  not need (PDF, LaTeX, office, notebook).
  Check 2 names a file or directory it cannot read.
- `test_tools_gitignore.py` — every .gitignore rule is a pattern git applies as written (no
  comment after a pattern on the same line), and the root scratch files bus_model.json,
  bus_config.csv and init.csv are ignored.
- `test_name_review.py` — the publish-time name review: a word new since the last published
  tree is listed (inside identifiers and file paths, in UTF-16 and binary files, part numbers
  kept whole, accented and non-Latin names read whole (Unicode letters after NFC
  normalisation, and a run of a script without case such as CJK or kana is one word at two
  characters or more), a symlink read as its target path in the tree and in the baseline (skipped where the platform or account refuses symlinks, through conftest's `symlink_or_skip`),
  hex-shaped part numbers included, -er/-al/-ly derivations of published words
  listed, English words included), a file too large to read is listed by name and its
  content is in the digest, an
  unknown option is refused, and approval is for exactly the list that was read, with the
  flag the output names.

## 4. Validation patterns we rely on

These are the recurring shapes that make the suite trustworthy rather than circular:

- **Round-trip / inverse pairs.** Encode then decode (or write then read) and assert
  equality. Used for: Saleae binary writer↔reader, Arrow command store, workspace JSON,
  CDS command encode↔parse, and `registers_from_csv` (the register encoder is the *exact
  inverse* of `CRegisterModel`'s decode — validated by feeding its output through the
  independent `grid_from_registers` path and matching `grid_from_csv`). An inverse pair
  catches a bug in either direction without a hand-built oracle.
- **Equivalence between two code paths.** The windowed symbol decode must equal the full
  decode from the first shared comma; the capture-backed `TransitionSampleSource` must
  decode identically to the in-memory `MemorySampleSource`; the v1 CSV converted to v2
  must place dataports identically. If two independent paths agree, both are probably
  right.
- **Golden synthetic stream.** `make_demo_levels` (C++) / `transitions.demo_capture`
  (Python) generate a *known* PHY2 stream — config writes, commits, and a sine on four
  mono data ports of one device, the bus stepping 2 → 8 → 16 columns. Tests assert exact decoded values
  (the reconstructed waveform is a sine of known amplitude/frequency, so audio
  correctness is checkable, not eyeballed). This is the central fixture; most suites build
  on it.
- **Spec vectors.** The descrambler and 8b/10b tests use values published in the spec, so
  they are not self-referential.
- **External cross-check.** The C++ grid placement is compared against the companion
  visualizer's reference model across its directed-test configs.

## 5. Fixtures & test data

- **Synthetic demo** — the workhorse; deterministic, in-repo, no external files. Always
  prefer it for new tests. PHY1/2/3 each carry DP0 at **44.1 kHz by payload interval
  skipping** (13 of every 160 intervals, Section 14.1.10) against DP1 at 48 kHz, so mixed
  rates and skipping are exercised by default — see `tests/test_demo_skipping.py` and the
  `demo_skipped_rate_hz` / `demo_transported_samples` helpers in `conftest.py`. A skipping
  port's count is a BOUND, not a constant: the accumulator restarts at every SSP.
- **Two Links** — the app ships a two-Link demo (File ▸ Open Demo Capture ▸ PHY2,
  `MainWindow.load_demo_links`) and `conftest.two_link_captures` IS that demo: the PHY2
  FBCSE demo and the flow-control demo delayed by `transitions.DEMO_LINK2_DELAY_SAMPLES` (`TWO_LINK_SHIFT_SAMPLES` in the tests),
  at the shared 500 MHz rate. `test_two_links` pins the two edge for edge. The delay is a
  demo `source` key (`delay_samples`), so a workspace rebuilds it; a plain demo's source is
  unchanged. `conftest.export_sal_links` writes several Links into one `.sal`. The demo
  generators themselves are untouched, since exact-value tests depend on them. For Links at a
  DIFFERENT row rate use the PHY3 demo (3072 kHz): PHY1 and PHY2 end at 1536 kHz (PHY2 passes through 12288 and 3072 kHz first).
- **Real hardware captures** — large Logic 2 exports kept outside the repository: a
  cold-start capture that reconfigures 2-col to 8-col (as `.sal`, `.csv` and per-channel
  `.bin`), a 2-column cold start carrying a Ping, and an 8-column playback/record CSV. These
  exercise the messy realities the demo can't: multi-GB size, negative pre-trigger origin,
  2-col→8-col reconfiguration, cold-start-preloaded config, real audio. **Tests reference
  them only when present** (guarded by existence checks) so the suite stays green on a clean
  checkout. They are used heavily for *manual* validation during development; promoting a
  finding from a real capture into a deterministic synthetic regression test is preferred
  whenever feasible. **They cannot be committed, not even as small slices:** they are
  confidential. The only route into the suite is a synthetic reproduction of the behaviour.
- **Visualizer inputs** — the example configs under `visualizer_examples/` and the goldens under
  `tests/visualizer/`, for the placement and CSV-format conversion tests; the old-format tests use
  pinned inputs.

## 6. How to run

**Setting up.** `requirements-dev.txt` lists what testing needs: the runtime set, pytest,
pytest-cov, pytest-xdist, ruff, and mypy at the version `tools/mypy_baseline.json` records.
CI installs it.
`bash tools/setup_dev.sh` (macOS / Linux) or `.\tools\setup_dev.ps1` (Windows) installs it and
builds the decode core into the venv `run.sh` / `run.ps1` use, then prints that venv's activate
command; `--test` / `-Test` also runs the functional suite.

**Day to day, plain `pytest`.** `pyproject.toml` defaults it to `-m "not perf"`, so a
bare run is the functional suite; the perf ceilings are wall clocks and only mean something
in their own process, where the gate and CI run them (`pytest -m perf`). An explicit `-m`
on the command line overrides the default.

```bash
# from the checkout root
python3 tools/changed_tests.py --run  # the test files your change touches, in parallel
python3 -m pytest -n auto             # the functional suite, one worker per CPU
python3 -m pytest -m "gui or platform"   # one scope (core, gui, platform; §1.1)
python3 -m pytest tests/test_links_gui.py -k offset     # one suite / some tests
python3 -m pytest -m perf             # the perf ceilings, in their own process
```

**Per suite, one process each, several at once:** the runner, a thin wrapper over the gate's
suite check (`tools/gate.py --only suite`), so the gate and the runner cannot drift:

```bash
bash tests/run_all.sh                  # every suite in its own process (excludes perf, like CI)
bash tests/run_all.sh --build          # rebuild the core and assert its ABI first
bash tests/run_all.sh --perf           # the perf benchmarks instead
bash tests/run_all.sh --native         # also run the external native C++ suite ($SWI3S_NATIVE_CPP_SUITE names its runner)
bash tests/run_all.sh --build --native --verbose     # --verbose lists every suite's count
```

**The runner covers exactly what CI is configured to run**: `python -m pytest` per file with
the same `-m "not perf"` marker `ci.yml` declares. The only difference is process
granularity, so you get per-suite results and teardown crashes can't be masked by whichever
suites shared a process. A failing suite prints its last lines. `tests/test_collection.py`
pins that equivalence, and fails the build if any test file collects zero tests. Any
`python3 tools/gate.py --only a,b` run ends in a PASS that says it was partial: only a whole
stage is the gate.

Running a suite as a script (`python3 tests/test_x.py`) executes nothing, except in the three
files with a `__main__`, which regenerate a golden or fixture with `--regenerate`:
`test_authoring_render_golden.py` (the authoring render golden), `test_filters.py`
(`tests/fixtures/highpass_reference.json`, with SciPy) and `test_timing_vs_reference.py`
(`tests/fixtures/timing_reference.json`, from the reference checkout; it also accepts
`--allow-dirty`, see its entry). Show the diff of the regenerated fixture first and wait for
review before committing it.

By hand, without the runner:

```bash
# 0. (after any C++ edit) rebuild the core — see §2.
cd native && PYBIND11_INCLUDE="$(python3 -c 'import pybind11; print(pybind11.get_include())')" bash build_local.sh

# 1. A single suite, from the checkout root (conftest sets the offscreen platform; PYTHONPATH as below):
cd ..
PYTHONPATH="$PWD" python3 -m pytest tests/test_gui_smoke.py -q

# 2. External native C++ suite (see §3.1):
SWI3S_NATIVE_CPP_SUITE=<runner script> python3 tools/gate.py --only native-cpp
```

Notes / gotchas:
- `PYTHONPATH=.` is required so `import swi3s_studio` resolves; the `swi3score` `.so`
  sits at the package root and imports once it's on the path.
- `QT_QPA_PLATFORM=offscreen` is set by `conftest.py`, so suites that touch Qt run headless
  without it on the command line.
- pytest's exit code is the reliable pass/fail signal. **Do not** grep stdout for words like "error"
  to judge pass/fail: several suites legitimately print "0 errors", which trips naive
  greps. Use the exit code.

## 7. Coverage matrix (audit table)

Use this to spot gaps. "Layer" indicates where the assertion bites.

| Production area | Module(s) | Test(s) | Layer |
|---|---|---|---|
| 8b/10b, CRC16, comma, tokens | C++ core | `test_symbols` | core |
| Descrambler | C++ core | `test_native_reference_vectors` | core (spec vectors) |
| Command transport parse | C++ core | `test_swi3score`, `test_cds_meaning` | core + binding |
| Register snoop / config build | C++ core | `test_swi3score`, `test_native_register_rules` | core |
| Register **encode** (config→writes) | `native/.../Decoder.cpp` | `test_compare` (round-trip) | core + binding |
| Column detect + phase offset / resync | C++ core | real captures, manual | core |
| Audio placement (§14.2.5) | C++ core | the external native suite (§3.1) | core + cross-check |
| Sample-rate formula | C++ core | `test_native_sample_rate` | core |
| Segments / continuous bus rows | `Decoder`, `session` | `test_seek_window` | binding + app |
| `.sal` / `.bin` / CSV ingest | `ingest/*` | `test_ingest_formats`, `test_capture_pipeline` | app |
| Capture ↔ source equivalence | `ingest/capture`, `TransitionSampleSource` | `test_capture_pipeline`, `test_native_input_hardening` | app + binding |
| Audio store / pyramid / WAV | `store/audio_store`, `export` | `test_audio_pyramid`, `test_audio_export` | app |
| Arbitrary-rate resampler / PDM | `dsp/resample` | `test_resample`, `test_audio_export` (export-at-rate) | app |
| Per-stream DC blocker, high-pass and gain | `dsp/filters`, `store/audio_store`, `ui/audio_process_dialog`, native `sosfilt` | `test_filters`, `test_stream_processing`, `test_stream_processing_gui` | core + app + GUI |
| Spectrogram lanes | `dsp/spectrogram`, `ui/audio_view` | `test_spectrogram` | app + GUI |
| Audio zoom kept across a track rebuild; titles in a narrow pane | `ui/audio_view` | `test_audio_zoom_kept` | GUI |
| Audio samples drawn as stems when zoomed in to them | `ui/audio_view` | `test_audio_stems` | GUI |
| Maximize one pane and restore the arrangement | `ui/main_window`, `ui/mode_controller` | `test_maximize_pane` | GUI |
| Data-port and region names | `session`, `ui/port_names`, the panes, `ui/timeline` | `test_dp_names`, `test_region_names` | app + GUI |
| Multi-Link model + time mapping | `links` | `test_links` | app (headless) |
| Multi-Link UI (switch, groups, All Links, offsets, workspace v4) | `ui/main_window`, `ui/link_state`, `workspace` | `test_links_gui`, `test_two_links`, `test_workspace`, `test_link_sequences` | app (offscreen GUI) |
| Load queue: every request settles, whichever stage fails | `ui/main_window._load_async`, `_on_load_done` | `test_load_queue_settles` | app (offscreen GUI) |
| Later link bring-ups: restart the decode (DLV's recovered clock locking again), reset the registers | `analysis/link_control`, `session`, `api`, native `Decoder::restartAt`, `DlvSampleSource::Reacquire`, `ui/timeline`, `ui/main_window` | `test_link_restart` | core + app + GUI |
| Registers filtered by value provenance (the colour key) | `ui/register_view`, `ui/view_state` | `test_register_filter` | GUI |
| Exit teardown: nothing Python-owned left for PySide's exit sweep | `ui/teardown`, `app.main`, `conftest.pytest_sessionfinish` | `test_exit_teardown` | app (subprocess, offscreen) |
| None / True / False kept alive on Python 3.11 under PySide's Linux builds | `ui/singleton_pin`, `ui/__init__`, `conftest` | `test_singleton_pin` | app (subprocess) |
| Ctrl+C quits as File ▸ Quit, from the loop; a second one ends a hung quit | `app._QuitOnInterrupt` | `test_exit_teardown` | app (subprocess, offscreen) |
| Two-Link demo (= the test fixture) | `ui/main_window.load_demo_links`, `ingest/transitions` | `test_two_links` | app + GUI |
| Open Capture (probe, the one-page dialog, windows in every format, Locate's reference form) | `ingest/probe`, `ui/open_capture_dialog`, `session`, `ui/main_window` | `test_probe`, `test_open_capture_dialog`, `test_two_links` | app + GUI |
| Timeline zoom / pan / seek / drag (real input events), Links in step | `ui/timeline`, `ui/main_window` | `test_timeline_interaction`, `test_perf` (zoom/pan ceiling) | GUI |
| Timeline tick decimation (vectorised = the per-command loop) | `ui/timeline` | `test_timeline_decimation` | GUI (no paint) |
| Dialogs (audio export, Visualizer CSV, register map, Link offset) | `ui/*_dialog`, `ui/main_window` | `test_dialogs_gui` | GUI |
| No test can hang on a modal | `tests/conftest.py` | `test_modal_guard` | harness |
| Command store (Arrow) | `store/command_store` | `test_capture_pipeline` | app |
| Register model + provenance | `model/registers` | `test_register_model` | app |
| Register enums / PHY+CDS+link blocks | `model/registers`, `data/registers.json` | `test_register_model` (enum decode, PHY/CDS resolve) | app |
| Link-control events (PM_Action) | `analysis/link_events` | `test_link_events`, `test_register_model` (link_events) | app |
| Responses / measurements / errors / filter | `analysis/*` | `test_responses`, `test_measurements`, `test_analysis_filter` | app |
| Config compare (register diff + report + overlay) | `analysis/compare`, `ui/register_view` | `test_compare`, `test_gui_smoke` | app + GUI |
| Visualizer CSV v1→v2 conversion | `ingest/visualizer_csv` | `test_compare` | app |
| Visualizer placement parity (90 configs) | `Decoder` `layoutGrid`, `grid_from_csv` | `test_visualizer_placement` | binding (vs Visualizer goldens) |
| CDS symbol meaning | `analysis/cds_meaning` | `test_cds_meaning` | app |
| Exports (CSV / SVG / PNG) | `export/*`, `ui/grid_view` | `test_export` | app + GUI |
| Bus grid render (per-device/DP colour, legend) | `ui/grid_view`, `Decoder` (GridCell.device) | `test_gui_smoke` (stream colours) | GUI |
| Workspace save/load | `workspace`, `ui/main_window` | `test_workspace` | app + GUI |
| Window assembly, all panes populate | `ui/*` | `test_gui_smoke` | GUI |
| Cross-panel cursor sync | `ui/main_window`, cursor/timeline/symbol/audio | `test_gui_smoke` | GUI |
| Timeline config bands (per-segment colour) | `ui/timeline`, `decoder.segments()` | `test_gui_smoke` (segments/bands) | GUI |
| Command filtering (Filter menu, boolean text, title indicator) | `ui/command_table`, `ui/main_window` | `test_analysis_filter`, `test_gui_smoke` | app + GUI |
| 64-bit (>2³¹) sample signals | `ui/cursor`, `ui/timeline` | `test_gui_smoke` | GUI |
| Three-mode shell (Mode menu, per-mode docks) | `ui/mode_controller`, `ui/main_window` | `test_modes_gui` | GUI |
| Authored config model + CSV/dict serialise | `model/bus_config` | `test_authoring` | app |
| Authored config placement (via core) | `model/bus_config`, `grid_from_csv` | `test_authoring` | app + binding |
| Bus-Visualizer parity (bits+clashes+warnings, 96 configs) | `swviz`, `model/viz_engine` | `test_visualizer_engine` | engine vs golden JSON |
| Bus model → JSON export | `model/viz_engine.model_json` | `test_visualizer_engine` | app |
| Authoring panel + Use-as-Expected | `ui/authoring`, `ui/main_window` | `test_modes_gui` | GUI |
| SWI3S timing margins (compute, F_max, corner) | `timing/calculator`, `timing/delta_tpd` | `test_timing`, `test_timing_cross_spec`, `test_timing_fmax_rate` | app |
| Divergence from the reference analysis | `timing/*` vs the vendored reference fixture | `test_timing_vs_reference` (fixture; live check skips if absent) | app |
| Displayed equation sums to the margin printed beside it | `timing/calculator` | `test_timing_cross_spec` | app |
| Timing view (margins text + round-trip, the page and its settings) | `ui/timing_view` | `test_timing`, `test_timing_view_page`, `test_timing_view_settings` | GUI |
| Timing term diagram (per-leg timelines, one term drawn) | `timing/timeline`, `ui/timing_diagram` | `test_timing_timeline`, `test_timing_diagram`, `test_timing_diagram_layout` | app (core) for `test_timing_timeline`, GUI for `test_timing_diagram` and `test_timing_diagram_layout` |
| Workspace (mode + authoring + timing) | `workspace`, `ui/main_window` | `test_modes_gui`, `test_workspace` | app + GUI |

## 8. Conventions for new tests

- **One concern per file**, named `tests/test_<concern>.py`, functions `test_*`, run by
  pytest. No `__main__` block other than a golden's or fixture's `--regenerate` (§6). Name the file in §3.2
  here: `test_testing_doc` fails otherwise.
- **No test can open a modal.** Offscreen nobody dismisses one. The conftest guard replaces
  every modal entry point the app uses
  (`QMessageBox.*`, `QFileDialog.get*`, `QInputDialog.get*`, `QColorDialog.getColor`,
  `QDialog.exec`, `QMenu.exec`) with one that answers "cancel", and a test that opened one
  it did not patch FAILS at teardown naming it. `QMenu.exec` also has a static overload, so
  PySide answers `menu.exec(pos)` on an instance with the built-in method whatever the class
  attribute holds; the guard routes QMenu's instance lookup through the class attribute. To
  drive a dialog, patch its
  entry point yourself: a static helper with `monkeypatch.setattr(QFileDialog,
  "getSaveFileName", ...)`, a dialog class with `test_dialogs_gui._drive`, whose function
  receives the live dialog, fills it in through its widgets and returns Accepted or
  Rejected. If the app starts calling a new static helper, add it to
  `conftest._MODAL_CANCEL` (`test_modal_guard` notices).
- **An exception in a Qt slot fails the test.** PySide prints one and carries on. The
  conftest guard records `sys.excepthook` and fails the test at teardown with the
  traceback. Both guards hold their patches apart from the test's `monkeypatch`, so an undo of it does not remove them.
- **Settings are a throwaway file, empty for every test.** conftest points every
  `QSettings()` of the run (the app's included) at a temporary ini and clears it before
  and after each test, so nothing reaches the developer's own settings and no test's saved
  choice (a colour, a folder) reaches the next.
- **The windows a test builds are deleted after it**, workers joined. A closed window is
  kept alive by its own Qt connections to its methods, with every Link it decoded. Deleted,
  the suite stays near 1 GB (one test that reads a real capture, where one is present, peaks
  higher). Windows are recorded as they are built (listing the top-level widgets segfaults),
  and a window's own `QTimer.singleShot`s take it as their context, so none fires on a
  deleted window. A `TimingView` a test built with no parent is deleted the same way (left
  to the collector after a click, it segfaults Python 3.13).
- **A key press lets go of its modifiers.** `QTest.keySequence` leaves Ctrl held after the
  test and its windows, so a later test's row click reads as a Ctrl-click; a test that
  leaves a modifier held fails. Follow a sequence with `QTest.keyRelease(w, Qt.Key_Control)`.
- **Wait for a decode with `conftest.pump_loads(win)`**, which fails if the loads do not
  finish in time, so a stuck load is reported as itself.
- **Patch with `monkeypatch`, never by assignment.** `module.Thing = fake` outlives the test
  and breaks whichever test later in the same process uses the real one.
- **Our own warnings are errors** (`pyproject.toml` `filterwarnings`): a `DeprecationWarning`
  from `swi3s_studio` or a test, and any `ResourceWarning` (an unclosed file). Read and
  write files with `pathlib.Path.read_text` / `write_text` or a `with` block. A
  `ResourceWarning` fires when the garbage collector finalizes the file, so it can surface
  in a LATER test than the one that leaked: search for the file name in the message, not
  the test that reported it.
- **A wall clock goes in `test_perf.py`**, marked `perf`; everywhere else, pin cost by
  counts (`test_cursor_cost.py`, the switch and zoom counts in `test_links_gui.py` and
  `test_timeline_interaction.py`). The perf file pauses the cyclic GC around each test.
- **Build on the synthetic demo** unless you specifically need a real capture; if you do,
  guard on file existence and `pytest.skip(…)` so a clean checkout stays green (and says
  it skipped).
- **Add a regression test with every bug fix.** This is not optional. Name the behaviour,
  not the bug.
- **Prefer an inverse/equivalence assertion** (§4) over a hand-computed expected value
  when one is available — it's harder to fool and survives refactors.
- **C++ vs Python:** put protocol-correctness checks in the C++ suite (spec vectors), and
  re-assert the *observable result* from Python through the binding. Don't re-implement
  protocol logic in Python just to test it.
- **Address table columns by name, not by number.** The Commands table's columns are
  `command_table._COLUMNS`, and `_COL[name]` gives a column's index (`_COL["Opcode"]`), as
  `test_gui_smoke` does. A literal index breaks when a column is added or removed, and the
  All Links view leads with a Link column, so its indices are one higher. Where a test
  still uses a literal index, grep for it when you change a table's columns and fix it in
  the same commit.
- **Watch for state bleed in `test_gui_smoke`:** it's one long script over a single
  window; if your new check moves the cursor or mutates a view, restore the baseline
  (e.g. `win.cursor.set_sample(0)`) before the next section, or you'll break a later
  assertion that assumed the prior state.
- **Don't shadow module-level imports** inside a test function (a local `from … import X`
  makes `X` local for the *whole* function and `UnboundLocalError`s earlier uses).

## 9. Known gaps & risks (the audit's standing TODO list)

These are deliberately listed so an audit starts from an honest baseline:

- **CI covers less than the release gate, and vice versa.** `.github/workflows/ci.yml`
  runs on this repository: ubuntu 3.11/3.12/3.13 + windows 3.12/3.14 + macOS, a `-m perf`
  gate, coverage, and a **blocking** lint/type job.
  *Risk: neither net is a superset of the other.* CI runs no test file in a process of its
  own and does not assert a stale native ABI; the hand-run gate runs only where it is run (the
  development machine at each commit, the guests per batch, and every platform before a tag:
  a Windows guest on 3.14 and a Linux guest on 3.11–3.13), not on every push. Both are required at a release — see
  DEVELOPMENT.md ▸ Release checklist steps 2 and 3, and `tools/gate.py`, which is the one
  definition of the gate.
- **Performance is pinned by wall-clock cliff detectors and counts.** `tests/test_perf.py`
  holds wall-clock cliff-detectors on a large synthetic capture, run in CI and in the release
  gate (`-m perf`) and locally via `run_all.sh --perf`; see the maintainers' performance notes.
  It holds an **end-to-end per-cursor-move budget** with every dock open, on a fixture big
  enough to expose a per-move scan (`from_demo(20000)`: 10.2 M UIs, ~410 commands, 3 config regions),
  and a **memory prediction-vs-measurement** check that loads a `.sal` in a clean subprocess
  and compares `est_peak_bytes` against that process's peak RSS in both directions. The
  subprocess is load-bearing: `ru_maxrss` is a high-water mark, so building the fixture in
  the same process would hide most of the load. `test_timeline_interaction.py` drives
  wheel zoom and drag pan with real input events and counts that they touch no pane, and
  `test_zoom_and_pan_budget` holds the paint's ceiling. *Residual gap:* a wall clock loose
  enough for a shared runner catches ~+100 ms additions, not smaller drift; that is what the
  counts are for.
- **Real-capture decode is not pinned by CI**, and it will not be: real captures are never
  committed, even as small slices; only synthetic reproductions enter the suite. The hardest
  decode paths — 2-col→8-col reconfiguration, cold-start-preloaded config, negative
  pre-trigger origin — are exercised manually. *Mitigation:* capture the essence of each as
  a synthetic regression where possible. `test_ingest_formats`'s real `.sal` test
  reads the `.sal` files in `$SWI3S_REAL_SAL_DIR` (§3.2) but checks only that the edges are ordered.
- **GUI testing is mostly wiring-level.** Wiring and population are pinned everywhere;
  interaction is pinned for the timeline (zoom, pan, seek, bookmark drag) and the main
  dialogs, through real events and the dialogs' own widgets. Pixel rendering, and the
  Dataport Scramblers dialog, are not.
- **Compare is a register diff** (`register_diff` in `analysis/compare.py`), which matches
  registers by device and address. It is unreliable when the visualizer and decoder number
  dataports differently; it is the only comparison Compare makes.
- **Placement parity is self-contained.** The Visualizer's 96-config JSON corpus
  (`test_visualizer_engine`) and 90 per-DP goldens (`test_visualizer_placement`) are
  **bundled** under `tests/visualizer/`, so no sibling checkout is needed. Regenerate the
  goldens from the Visualizer's `model_dump.py` if placement intentionally changes, and show the
  diff first. The native C++ placement cross-check belongs to the external suite (§3.1).

## 10. Auditing checklist

When reviewing whether testing has kept up with the app:

1. **Build is current:** rebuilt the core after the last C++ change? (§2)
2. **All suites green by exit code**, both layers (§6). Not by stdout grep.
3. **Coverage matrix (§7) still complete:** does every new production module/feature have
   a row and a test? New `analysis/` or `ingest/` module without a suite = a gap.
4. **Every bug fixed since the last audit has a regression test** (§8). Cross-reference
   the roadmap / memory notes for recent fixes.
5. **New user-facing behaviour is pinned**, not just the happy path — error/edge cases
   (malformed file, empty window, missing device) where cheap.
6. **Brittle index-based UI assertions** were updated alongside any table-column change.
7. **Gaps list (§9) reviewed:** anything graduated from "manual only" to "automated"?
   Anything new that belongs on the list?

---

*Cross-references: `architecture.md` §3 (shared core), §11 (testing/headless overview).*
