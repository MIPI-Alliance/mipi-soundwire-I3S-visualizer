# SWI3S Studio — Testing Approach

> Status: living document. Update it whenever the test strategy changes — it is the
> reference for **auditing** test coverage as the app grows. Last reviewed 2026-09-30
> (3.0.19). Every `tests/test_*.py` is named here: `tests/test_testing_doc.py` fails the
> build on a suite this document does not mention, or on one it names that no longer exists.

## 1. Scope

SWI3S Studio decodes a non-trivial wire protocol (NRZS → 8b/10b → command transport
→ register snoop → SSP-anchored audio placement → descramble) and renders it through
a stateful Qt UI bound by one shared cursor. This document describes how it is tested,
so that coverage can be **audited** rather than assumed.

The guiding rule: **every behaviour a user could rely on should be pinned by a test,
and every fixed bug should leave a regression test behind.** When in doubt, prefer a
small deterministic test over a manual check.

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

The **C++ decode core is authoritative**. The same decode classes also ship in the
Saleae plugin (`SwI3sAnalyzer`); they are **vendored in-repo** at
`native/swi3score/core/` (so Studio builds standalone), reused behind an
`ISampleSource`/`Decoder` shim and exposed to Python via pybind11 (`native/swi3score`).
Two consequences for testing:

1. Protocol correctness (8b/10b, CRC16, descrambler, placement, register packing) is
   tested **at the C++ layer** with spec vectors, and **again from Python** through the
   binding so we know the binding faithfully exposes it.
2. The Python app layer is tested against the core's output, not against a re-implemented
   oracle — so the app can't silently disagree with the decode.

> **Hard rule:** after editing anything under `native/` (including the vendored
> `native/swi3score/core/*`), you MUST rebuild the module before the Python
> tests mean anything:
> ```
> cd swi3s-studio/native && \
>   PYBIND11_INCLUDE="$(python3 -c 'import pybind11; print(pybind11.get_include())')" \
>   bash build_local.sh
> ```
> A stale `.so` tests the previous build.

## 3. Test layers

### 3.1 Native C++ primitive + round-trip suite
Location: `protocol-analyzer/SwI3sAnalyzer/test/`, runner `run_tests.sh` (compiles each
with `clang++ -std=c++17` against the bundled AnalyzerSDK and runs it).

| Test | Pins |
|------|------|
| `test_cds_primitives.cpp` | 8b/10b decode, K.28.7 comma, CRC16, Robust Tokens |
| `test_descrambler.cpp` | descrambler against spec §14.1.3.2 Test Cases 1–3 |
| `test_roundtrip.cpp` | CDS command encode → parse |
| `test_simulation_roundtrip.cpp` | full synthetic stream: commands + scrambled audio end-to-end |
| `test_register_snoop.cpp` | WriteA32/Commit → register model → config |
| `test_column_detect.cpp` | hypothesize-and-validate column count + phase offset |
| `test_sample_rate.cpp` | per-DP audio sample-rate formula |
| `test_wav_export.cpp` | WAV writer |
| `compare_placement.py` / `model_dump.py` | **cross-check**: core grid vs the visualizer reference model across directed-test configs |

These are the ground truth for the protocol. They use spec-published vectors where they
exist (descrambler, 8b/10b) so they're not self-referential.

### 3.2 Python suites
Location: `swi3s-studio/tests/`, one concern per file, run by **pytest** (the only supported
entry point: see §6). Shared setup is `tests/conftest.py`: a short demo, the Qt
offscreen platform, the two-Link fixture, the guards against an unpatched modal and an
exception in a Qt slot, a throwaway settings file, and closing each test's windows (§8).

They split into three bands:

**Core-through-binding (does the C++ reach Python intact?)**
- `test_swi3score.py` — decode the synthetic demo (config + commit + scrambled stereo)
  and assert the control phases, snooped geometry, and reconstructed audio sine. Mirrors
  `test_simulation_roundtrip.cpp` but from Python.
- `test_symbols.py` — 8b/10b symbol framing (comma → robust-token header → D-code packet;
  `max_symbols` honoured; monotonic samples).
- `test_seek_window.py` — windowed re-decode equals the full decode from the first
  re-acquired comma; symbol rows match command bus-rows on one continuous scale with a
  clean 10-row cadence.
- `test_cds_meaning.py` — per-symbol Command Transport role labels (PhaseID / Device Mask
  / Opcode / Address / CRC / responses) for Ping / WriteA32 / Commit.
- `test_8b10b_disparity.py` — the 8b/10b symbol decoder over both running-disparity forms
  (RD−/RD+ coverage), commas, and control vs data codes.

**App data path (ingest → store → analysis → export)**
- `test_capture_pipeline.py` — demo levels → `Capture` → write Saleae binary → read back →
  `TransitionSampleSource` → `Decoder`; asserts the capture-backed source decodes
  **identically** to the in-memory source, the binary reader/writer round-trips, and the
  Arrow command store preserves the decode.
- `test_ingest_formats.py` — synthesises a v0 `.sal`, a v3 `.sal`, and a digital CSV from
  the demo and checks each decodes to the same commands/columns; confirms a real Logic 2
  `.sal` (if present) is handled rather than producing garbage; single- and multi-chunk v3
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
- `test_bus_model_compaction.py` — bit removal is deferred but exact.
  `remove_bits_matching` updates the position bucket at once and defers the rest to one
  `compact()`, which the builder calls when placement finishes. Pins that the bucket the
  clash logic reads is exact immediately, `bits` is exact after compaction, and a row read
  compacts for itself.
- `test_register_model.py` — spec load, address → register/field resolution, provenance,
  and a write → Commit → read stream whose Commit promotes NumColumns_NEXT into _CURR,
  checked against the apply_commands oracle.
- `test_responses.py` — response token → name by phase; labelled `response_summary`,
  including the per-device **Ping** breakdown collapsed into device ranges
  (`0: PING_ATTACHED, 1-11: NO_RESPONSE`).
- `test_link_control.py` — §5.1.2 link bring-up detection from the raw edges
  (cold/warm start, the PHY-number clock burst, PhyStart), self-orienting when the
  bring-up rides the data line. And the Session overruling a detection the decode
  contradicts (CRC-valid commands inside it): every capture without a bring-up (the PHY1,
  PHY2 and flow-control demos without a cold start, a cold-start demo joined after audio
  began) has none, though the edges alone read one into each; every genuine bring-up is
  kept; a phantom that named a PHY does not steer the decode, including one that steered it
  into no valid command at all; traffic before a Bus Reset does not count.
- `test_bus_timing.py` — measured setup/hold/eye (`analysis/bus_timing`) on synthetic
  captures of known geometry: the margins match the offset, gating drops the
  no-transition (1-UI) pileup, and a tight margin flags as such.
- `test_pdm.py` — PDM decode (`dsp.pdm_to_pcm` + `store.decode_pdm`): a sigma-delta
  tone recovers the right pitch, the 1-bit code is **bipolar** (`{0,1}→±1`, not 2's
  complement), and a DC-biased mic stream is DC-blocked yet keeps its tone.
- `test_measurements.py` — derived metrics (columns, SSP count, per-DP sample rate &
  bandwidth); each region's clock is the mean edge spacing, checked region by region.
- `test_analysis_filter.py` — error classification + command filter proxy.
- `test_compare.py` — grid-diff classes; `grid_diff_report` summary/detail; the
  **`registers_from_csv` round-trip** (encode a CSV config to register writes, feed through
  `grid_from_registers`, assert the grid equals `grid_from_csv`); `register_diff`; old
  visualizer-CSV (v1.73) → v2.0 conversion + placement equivalence.
- `test_audio_export.py` — `AudioStore` → per-(device,DP) WAV → read back → compare to the
  generated sine.
- `test_audio_export_visible_range.py` — exporting the Audio pane's visible range writes the
  same capture span for every stream: the store maps a capture-sample window to each
  stream's own sample indices (`AudioStore.stream_index_range`), the export dialog maps its
  visible window per stream (`index_range(dev, dp)`) and refuses to map it without a stream,
  an index window is used as given, and `AudioView.visible_sample_range` reports the shared
  X range in capture samples.
- `test_audio_pyramid.py` — render-pyramid envelope equals raw samples when zoomed in;
  memmap-backed channel.
- `test_resample.py` — arbitrary-rate band-limited resampler (`dsp/resample`): identity,
  predicted output length, in-band tone preserved, out-of-band rejected, up/down-sample,
  unity DC gain, PDM→PCM decimation.
- `test_filters.py` — the audio high-pass (`dsp/filters`) against SciPy: the Butterworth
  design, the steady-state initial conditions, `sosfiltfilt` with its default padding and with
  the longer padding `highpass` uses, over corners from 1 Hz to just under Nyquist
  (references in `tests/fixtures/highpass_reference.json`, regenerated with SciPy by
  `python3 tests/test_filters.py --regen`); the -6 dB corner and 48 dB/octave stopband; a click
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
  −90 dB beyond 5 bins and no DC term in the bottom bins; a full-scale tone's window skirt
  stays above the −120 dB colour floor out to about 70 bins and is below it beyond 80; a
  click lit from half a frame before it and no earlier, at every frame size; short, empty and silent streams) and the Audio
  pane: each channel waveform or spectrogram, its own frame size, the shared zoom, the menu,
  Vertical Zoom leaving a spectrogram alone.
- `test_maximize_pane.py` — View ▸ Maximize Pane (⌘⇧M), the key itself (no auto-repeat), the
  title-bar button and a double-click: the focused pane alone in the window (a tab behind
  another, the Commands table, a floating pane), and the arrangement restored exactly but for
  that pane staying in front of its tab group; in the Timing Calculator, the ⤢, the key and
  a double-click on the title hide the top bar, the parameter sections still scrolled to;
  Commands, Registers, Bus Grid, CDS and Samples keep the Timeline at its height through a resize of the window (a side-area pane is moved
  below it meanwhile), the others take the whole window, a closed Timeline stays closed, and
  no cap is left behind; the key pressed again and again keeps to the same pane, for every
  pane, and a title-bar button moves the focus to its pane's view, not a toolbar control; the
  Commands header's button and a double-click on its title; the Timeline is never maximized
  nor floated by a double-click; with nothing selected nothing changes; another pane shown,
  hidden from the View menu or closed by its ✕, a Link added or removed, Timing's jump to
  Capture, the Bus Analyzer left, All Links turned off or a workspace saved meanwhile all keep
  the arrangement to return to (a workspace saved after All Links was turned off reopens
  with the one-Link height); outside the Bus Analyzer the item is hidden; the key is in
  the shortcuts help.
- `test_audio_stems.py` — zoomed in to a few pixels a sample, every Audio waveform track is
  drawn as its samples (a stem from zero at each sample's capture position, a dot on its
  value), and zoomed out the line through them again; the switch follows the pane's width at
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
  snapshot of all of it must match (each restore removed in turn fails it); All Links'
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
- `test_export.py` — command CSV (headless) + bus-grid SVG/PNG (offscreen Qt).
- `test_workspace.py` — workspace JSON round-trip + full GUI save → open restores
  bookmarks and cursor; the v3 → v4 migration (a v3 file loads as one Link at offset 0
  and re-saves as a true v4); a broken Link list is rejected. A saved what-if register
  overlay is re-applied on open, and each Link's device register maps reach its session,
  its Commands table and the Registers pane's Peripheral block.
- `test_demo_workspaces.py` — each File ▸ Open Demo Capture entry opens its capture with its
  bundled workspace (`data/demos/*.swi3s`): data-port names such as 0.0 "AMP1" on PHY1, PHY2
  and PHY3; two Links on PHY2, Link 2's data ports named as in the flow-control demo; and the
  shipped files carrying no window geometry, no Commands sort and no paths.
- `test_links.py` — `LinkSet` (headless): ordering, the active Link across add/remove,
  default "Link n" names, rename; the time mapping — sample → ps → sample is the identity
  at every rate (the single-Link invariant), signed offsets, two rates meeting at one
  instant.

**Headless GUI**
- `test_gui_smoke.py` — constructs the real `MainWindow` under the Qt **offscreen**
  platform, loads the demo, and exercises: every pane populates; the shared cursor drives
  the register view; two-way command ↔ timeline ↔ CDS-symbol ↔ audio sync (a command's
  start_sample equals its SPM-comma Row Sync Point); the **Filter menu** (multi-select
  kinds, boolean and/or text, errors-only) and the title's filter indicator;
  bookmarks; **64-bit sample signals** (>2³¹); the Compare expected-config (CSV-Import)
  overlay; the audio cursor line + click-to-seek mapping.
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
  since a CI-safe wall clock cannot see them.
- `test_link_sequences.py` — seeded random sequences of Link operations (offsets,
  activations, cursor moves, Align, removals and additions, All Links scopes) on three
  Links, with `window_checks.check_window` after every step, a cursor round-trip property,
  and a save and reopen that must bring back what was displayed, band ranges included. A
  failure names its seed and step. `tests/window_checks.py` is the shared helper: it
  recomputes the derived state (band ranges, the remembered cursor instant, every index
  that names a Link) from the Links alone and compares it with what is shown.
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
  helper the app calls is guarded (read from the source, so a new call site cannot slip by).
- `test_modes_gui.py` — the **three-mode shell**: the Mode menu swaps the central page
  and the visible dock set per mode (Visualization | Timing | Analysis); Analysis-only menus
  gate by mode; returning to a mode restores its layout; the active mode round-trips through
  the workspace. Also: Visualization authoring places the grid + flags a seeded bus clash,
  the authored config can be compared in the Analyzer (Decode ▸ Import Visualizer CSV), and
  the workspace persists the authored
  config + timing inputs.

**Visualization-mode authoring + Timing-mode (mostly Qt-free)**
- `test_authoring.py` — `BusConfig` ↔ the v2.0 CSV (the engine + C++ core read it) + dict
  round-trips; an authored config is placed by the `grid_from_csv` cascade (and yields register
  writes), including wide-bit held-column identity; every data port gets a name in the CSV.
- `test_timing.py` — `find_worst_corner_rows` never improves a margin; `TimingView` renders the
  inequality headings + binding summary and round-trips its inputs. No leg's margin moves
  with a t_RF lane outside its declared `_LEGAL_LANES` set (a releasing EDE peripheral's
  contention legs also read Per DATA, through the release); no leg shows a structurally zero
  term (checklist rule 5), and every zero-valued term is a parameter whose row can reach
  zero; the calculator's module docstring states the number of legs in `INEQUALITIES`.
  Also the conventions that carry no number and so would fail silently: every leg's terms read in **physical
  time order** (receiver window last, clock lane before data, a peripheral launcher's
  launch between its two crossings), **A drives and B samples** on all four P→P legs, the
  1)–17) numbering matches the display order, every printed symbol **resolves to an input
  row** (or a declared row-less key), and the term highlight's **click regions agree with
  the painted glyphs** at every sample point on every row — the invariant that catches a
  hit-test laying out at a different font from the paint path. The F_max column (summary
  and tree) prints each leg's own worst-corner F_max, non-setup legs included, and the
  default page shows a finite one on both keeper legs.
- `test_timing_cross_spec.py` — mixed SWI3S ↔ SoundWire 1.3 timing: with both sides SWI3S
  the calculator is identical to single-spec; the four setup legs agree with an independent
  interop model within its tPD convention; and the anchor conversion between the two.
  Every leg's displayed terms sum to its margin, also with Man CLK, Man DATA and Per DATA at
  three different t_RF values; PP_setup / PP_setup_ho follow the Per DATA lane and never Man
  DATA; a peripheral's EDE release is its pure Per_t_DD plus the full swing plus the hold
  interval; and every anchor-conversion note recomputes to the pure value it prints, at the
  spec's reference t_RF. One crossing symbol carries one coefficient on every leg it appears
  on, swept over unequal t_RF in both directions, independent Manager/Peripheral ramp shapes
  and noise/supply/slew tolerance; a releasing peripheral's clock detection on the
  contention legs is `X_MP,late` (MP_hold's late clock crossing) and an acquiring one's
  `X_PM,early` (PM_hold's). On the P→P legs B's Per DATA crossing takes the Peripheral's
  ramp shape and the clock lane the Manager's. Every unratified source (the proposed
  revision and EDE) carries PROPOSAL_NOTE first in the result caveats.
- `test_perf.py` — wall-clock ceilings on the hot paths (marked `perf`, run on their own with
  `pytest -m perf`): decode, engine build, CSV import, cursor move, zoom/pan and Link-switch
  budgets, and a `.sal` load's peak memory against its prediction. Cliff detectors, not
  micro-benchmarks: ceilings are several times the observed time. The cursor-move and
  Link-switch budgets print their median and worst as a fraction of each ceiling at the end
  of the run (`perf margins`), pass or fail, so a runner creeping up on one shows before it
  goes red.
- `test_delta_tpd_swing.py` — the crossing envelope's TX swing is a PARAMETER (`V_OH_frac` /
  `V_OL_frac` from `CalcInputs`, the fields `calculator.py` reads). Pins that every linear
  coefficient moves with the pair, that halving the swing doubles the crossing, that the
  default gives the tabulated 0.60 swing (`0.80 − 0.20`), that an inverted swing raises
  instead of dividing, and that the exponential ramp stays independent of it **by design** —
  its shape is set by a time constant, so there is no slope for the swing to define.
- `test_timing_vs_reference.py` — **the divergence log, executable.** The same timing model
  lives twice: here, and in the `timing-analysis` project's `swtiming/emit_ede.py` (which
  generates the EDE paper's numeric macros). This asserts every leg pair under one matched
  configuration with each delta DECLARED — four legs agree exactly, plus MP_contention at the
  Manager's boundary release and keeper_Man at both release placements; the
  peripheral-launched legs and P→P hold differ by the Peripheral Output Timing figure's anchor
  conversion (1.667 ns); P→P contention with a conventional release differs by the P→P slew split
  this model declines to make; and P→M / P→P contention with a releasing EDE peripheral differ by
  the anchor conversion, P→P with the slew split on top. A delta that MOVES fails. The deltas are
  deliberate; see `docs/anchors.md`. SKIPS when the reference project is not beside this one
  (`SWI3S_TIMING_ANALYSIS` overrides the path). The reference is not vendored. Three traps:
  the two models name legs by OPPOSITE conventions (`emit_ede.setup_mp` is this project's
  `PM_setup_ho`); each reference leg carries its own corner in its default arguments rather
  than a global one; and where the reference HARDCODES a worst corner by omitting a term,
  this project shows the term and lets the search find it, so comparing at the nominal
  invents a divergence.
- `test_timing_fmax_rate.py` — F_max on legs whose terms scale with the UI. A clocked Manager
  t_DD / t_ZD sits at a fixed fraction of the UI, so the MP setup legs' F_max, evaluated at
  that rate with the placement held, gives a zero margin; under 2x it agrees with
  `launch_mode_max_fclk_MHz`. An EDE release (one UI earlier, on either side), a clocked
  Manager t_DZ / t_ZD, and the EndDriveEarly ceiling on an analog Manager EDE release all enter the
  contention legs' F_max, so a zero-UI EDE deficit reports the rate that closes it instead of
  0.0. Also pins that the MP_setup / MP_hold Man_t_DD notes, under a clocked launch, give the
  grid note and then the snapped value, and that the SoundWire min corner takes only the
  clock-side re-anchor. Hold, handover-hold and keeper legs with a UI-scaled part (a clocked
  Man_t_DD / t_ZD / t_DZ, N_HO·UI, a conventional release's closing UI) report the rate where
  the margin reaches zero, just above it the leg fails, just below it passes; 4x MP_hold
  agrees with `launch_mode_max_fclk_MHz`. `F_max_binding_MHz` reads every bounding leg: at
  0.999 of it every bounding leg passes (cases bound by PM_contention under a peripheral EDE
  release, MP_hold under 4x, and keeper_Man), and a leg failing where its margin grows with
  the rate pins it to 0.0.
- `test_visualizer_engine.py` — **the authoritative Bus-Visualizer parity test.** Bus-Visualizer
  mode is driven by the *first-party Visualizer engine* (`swi3s_studio/swviz/`, via
  `model/viz_engine.py`). For all **96** example configs this builds the merged `BusModel`
  through that engine and asserts the serialized model equals the Visualizer's golden JSON
  (`tests/visualizer/golden_json/`) — **bits + bus_clashes + device_clashes + read_overlaps +
  warnings**. This is the Visualizer's own testsuite, ported, and is the source of truth for
  placement/clash/validation parity.
- `test_visualizer_placement.py` — a fast first-line placement check: for all 90 configs it
  solo-places each data port through `grid_from_csv` and asserts `(dp,row,col,slot)` matches the
  Visualizer's per-DP golden (excluding the reserved CDS column).
- `test_grid_cross_engine.py` — asserts the two LIVE placement engines agree DIRECTLY:
  each solo data port placed through both the C++ core and the swviz authoring engine
  yields the same DP-owned cells. Runs the demo AND the **whole example corpus** (all
  flow modes) on DATA + TX_PRESENT — the sweep that catches a placement rule landing in
  one engine but not the other. Plus
  a per-FlowMode spec check that TX_PRESENT is placed iff flow-controlled, in both engines
  (catches a bug identical in both, which equivalence can't). (The two are otherwise
  pinned only transitively via the shared goldens above.)
- `test_visualizer_roundtrip.py` — whole-corpus CSV round-trip: every example config,
  loaded → re-saved via `CSVHandler` → reloaded, must build a byte-identical model.
- `test_authoring_render_golden.py` — characterization golden over all 96 example configs:
  pins `viz_engine.render_payload`'s issue list + clash cells + grid dims (the part the
  placement/model goldens don't cover), so a future placement-engine change can't silently
  shift the authoring warnings.
- `test_drq_clash.py` — the DRQ clash verdict, hand-built because **no vendored golden carries
  a DRQ collision**. A DRQ's direction is the inverse of its parent port's, so the engine's
  clash branch reads the bit's own direction, not `PortDirection_REG`. Two Sink ports
  driving one DRQ column must be a bus clash; two Source ports sampling it must be a read
  overlap.
- **`tools/viz_report.py`** — a dedicated one-command visualizer runner + report (not a
  CI gate; the pytest suites above are). Runs the regression + CSV round-trip over every
  example config and writes `tests/visualizer/summary.md`: aggregate stats (bit positions,
  slots, handovers, clashes), slot-type/direction distribution, per-config metrics, and a
  **delta column vs the previous run** (tracked in `previous_stats.json`). Both outputs are
  gitignored — regenerate on demand. `--check` makes it exit non-zero on any failure.
- `test_grid_slots.py` — pins the shared `GridSlot` enum (model/grid_slots.py) to the C++
  `SLOT_NAMES` ordinals, so a C++ slot-enum reorder fails a test instead of mis-colouring
  the grid.

**The rest, by area** (each file's docstring has the detail)

*Decode and PHYs*
- `test_phy1_demo.py`, `test_phy3_demo.py` — the PHY1 (slow FBCSE, ports repositioned
  mid-capture) and PHY3 (DLV, recovered clock) demos round-trip; `test_phy_invariants.py` —
  what must hold for EVERY audio-mode PHY.
- `test_dlv_partial.py` — a mid-stream DLV capture with no bring-up is recognised, loaded at
  the true rate and its column count blind-detected.
- `test_flow_control_demo.py` — the four-mode flow-control demo round-trips bit-exact, and the
  mid-phase SPM framing lock.
- `test_payload_skipping.py`, `test_demo_skipping.py` — Section 14.1.10 interval skipping
  through the decode (44.1 kHz on 48 kHz intervals).
- `test_sspa_demo.py`, `test_sspa_ssp_lock.py`, `test_ssp.py` — periodic SSPAs, the
  SSPA-derived SSP lock applied backwards for partial captures, and the manual SSP row.
- `test_config_csv.py` — a decode-driving config CSV: the data-port config it supplies is
  applied from row 0, persisted in the workspace `source` and reloaded, and threaded through
  Open Capture and Decode ▸ Import Visualizer CSV.
- `test_csv_handler_missing_fields.py` — a Visualizer CSV the app saved loads with no
  missing fields: the scalar CDS_GuardEnabled_REG, CDS_GuardPolarity_REG and
  CDS_TailWidth_REG are derived from the per-source rows, and a scalar is reported missing
  only when the per-source row it is derived from is absent too.
- `test_partial_capture_csv.py` — join-late captures whose geometry comes from a CSV.
- `test_forced_columns.py` — region-scoped forced column counts, and the stale Safe-Lock seed.
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
- `test_devices.py` — hub-depth response delay and per-device names / register maps / depths.
- `test_dp_number.py` — the per-slot logical data-port number through model, CSV and core.
- `test_errors.py` — per-command error classification.
- `test_port_status.py` — peripheral-reported status registers and error counters.

*Ingest and export*
- `test_analog_import.py` — `.wfm` and analog-CSV import, sub-capture locate, `.sal` export
  and the progress hook, all on synthetic files.
- `test_vcd.py` — VCD import: the decode matches the demo, `$timescale` sets the rate, only
  1-bit signals are offered, and the bounded edge counts.
- `test_sal_export.py`, `test_raw_export.py` — `.sal`, per-channel `.bin` and digital-CSV
  exports reconstruct the same Capture.
- `test_sal_large_capture.py` — cost estimation, refuse-before-OOM and the windowed load, and
  that a whole-channel v3 decode allocates the output plus one block (tracemalloc), which
  is what the estimate's whole-file transient of 2 assumes. A window is priced by
  `saleae_sal.SalWindowModel` (the edges the blocks the window overlaps can hold, at most one
  per delta-run byte and one per sample, × 2, plus one chunk's decode and a fixed reader
  allowance), which is why the window-budget test uses a 400k-edge fixture.
- `test_ingest_rate_inference.py` — a digital CSV or v0 `.bin` stores times, not a rate: the
  rate is the grid every timestamp sits on (`ingest/timebase.py`), so an export at 500 MHz
  reopens at 500 MHz edge for edge, timestamps printed to the ns keep their rate, and the
  probe, the scan and the loader agree.
- `test_ingest_memory_guards.py` — each loader's guard figure, measured with tracemalloc
  against the peak it reaches (digital CSV ascending and unsorted, analog CSV, `.wfm` pair,
  `.bin` pair (a non-v0 file charged its size), VCD, a window of a one-block `.sal` channel),
  the probe showing the same figure, refusal before reading over budget, and a `.sal` window
  the probe calls fitting being one the loader admits, a `.wfm` pair priced once before
  either curve is read, a busy window of a mostly idle `.sal` priced by its blocks with the
  probe agreeing, and the `.sal` block index never charging fewer bytes than the blocks a
  window overlaps.
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
- `test_ingest_analog_classify.py` — telling an analog CSV from a digital one parses a
  bounded number of lines: the first rows in full, then lines sampled at spread byte
  offsets.
- `test_capture_export.py` — Export Capture: sub-range slicing, signal subsets, the dialog.
- `test_subcapture.py` — the sub-capture locator, sample-rate independent.
- `test_regmap_import.py` — vendor register-map import and per-device decode: both XML
  shapes (bit lists; wide registers with a separate field list, split into bytes), from
  synthetic maps only; defaults shorter than their width (zeros above the written digits;
  unknown above a leading X); an inferred register width rounded up to a power of two (a
  short default or a top field below bit 24 gives a 32-bit register, with no alignment
  warning).
- `test_register_history.py` — a register's history (Registers ▸ History…): each write and
  read of the byte, in order, and the attempts that did not land, each saying why; a deferred
  read's address-less ReadData (READ_FAILED, no answer) listed at the setup's address; a
  _CURR alias listing its _NEXT accesses and the Commits whose group (a data port's
  CommitGroupMemb as written) promotes them.
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
- `test_register_history_dialog.py` — the Registers ▸ History… window: the Failed attempts
  only filter and its shown-row to event mapping (synthetic RegisterEvents); in the window,
  the jump to the Row Sync Point, the jump following its Link by Session after a removal
  renumbers the Links, closing with its Link and on a new capture, a no-op jump from a
  removed Link, re-reading its accesses after a re-decode of its Link and not of another
  Link, set_events updating the summary and the filter, and following a theme switch.
  Offscreen Qt.
- `test_json_handler.py` — BusModel JSON round-trip keeps non-payload slots' None.

*Import warnings* (`swi3s_studio.import_notes`; each malformed or edge-case input gives the
expected warning naming the file, and a well-formed input gives none)
- `test_import_warnings_notes.py` — the collector: warnings in the order raised, an exact
  repeat dropped, nested collectors keeping their own, a collector belonging to the thread
  that opened it, and `ImportIssueWarning` through `warnings` with no collector open.
- `test_import_warnings_gui.py` — the Import warnings window: a load whose worker raised
  warnings opens one non-modal window titled with the file and listing each, with the count
  in the status bar and the list on the Session; a clean load opens none; each import gets
  its own window, deleted on close; a failed import shows its error and no window.
- `test_import_warnings_captures.py` — the capture readers (`.vcd`, digital CSV, the DLV
  rate snap, analog CSV, the analog front end, a `.wfm` pair, a `.bin` pair, a `.sal`
  archive, a window cut before the capture): skipped rows and cells, times sorted, rounded
  or rebased, a rate assumed or snapped, thresholds defaulted, a window clamped; opening a
  malformed digital CSV through the window opens the Import warnings window.
- `test_import_warnings_config_csv.py` — a Visualizer config CSV: the native loader
  (`swi3score.csv_warnings`: a port disabled for its DeviceNumber_REG, a value that does not
  fit its register field, an unreadable cell, a value past the last column), the legacy and
  v2.0 upgrade, `BusConfig`, the CSV handler through `viz_engine` and the CLI, the Session's
  construction, whole-capture and region imports (a plain re-decode reports nothing), and
  the Import warnings window; a config the app writes reports none.
- `test_import_warnings_regmap.py` — register maps: both XML importers' reports, a stored map
  read from JSON and the spec map JSON, the bundled maps reading clean, and binding a map in
  the Devices dialog opening the window titled with the file (a cancelled dialog, none).
- `test_import_warnings_workspace.py` — workspaces: keys, types and rows dropped and
  reported, a clean save and reopen and a format-3 file reporting nothing, and reopening a
  workspace with invalid values opening one non-modal window titled with the workspace file.
- `test_import_warnings_timing_settings.py` — Timing Calculator settings and Load Example:
  unknown keys, unknown choices and out-of-range numbers reported; a save and reload of
  every Manager spec choice, and a saved Example, reporting none.

*Analysis panes*
- `test_command_statistics.py` — the Statistics pane's expandable per-command groups.
- `test_demo_configs.py` — the demo layouts, and a cursor-aware config export.
- `test_tx_map.py` — the data-line transition raster and its Bus-Grid toggle.
- `test_grid_dp_colors.py`, `test_grid_label_fields.py` — grid colours key on the config slot;
  per-port cell-label fields and swatch clicks.
- `test_raw_view.py` — Raw Capture pane regressions, including a stopped line drawing its
  true level at every zoom and scroll (the zoomed-out edge reduction, on uint64 edges) and
  that reduction allocating by its slots, never a float64 copy of the window (tracemalloc).
- `test_sample_view_gui.py` — the Decoded-Samples window: bounded, extends at either end,
  drives the cursor.
- `test_eye_view.py` — the Timing (eye) view's colour-key toggles and its per-Link cache.
- `test_link_restart.py` — a Link brought up again after its clock stopped (synthetic
  captures: demos joined with a stop between, the first shorter than the decoder's leading
  window). Finding them: every bring-up, three in a row, after a short idle (the stop being
  the Bus Reset itself), with the link control on the data line, in a capture that starts in
  audio mode, none anywhere; audio after a stop, and a bursty line, read as none; the gap
  search chunked; a detected Warm Start inheriting the PHY, a later Cold Start with an
  unrecognised PHY naming none. The decode: restarting at the second Cold Start as the
  second demo alone decodes, at the first audio edge after PhyStart; nothing (audio,
  commands, bit samples, the windowed bit-sample replay) decoded while the Link is inactive;
  a Warm Start putting unchanged ports on the new Column-0 phase; restart points merged,
  sorted and range-checked; a restart in the first UIs. DLV: the recovered clock locking
  again so the second part decodes as the demo alone (commands, segments, audio), rows
  numbered by their Row Syncs across the restart, nothing decoded while inactive, the
  stretch skipped and its samples mapping to the restart's UI, the windowed bit-sample
  replay following the restart (exact from the row the stop cut short on, on an analyzer
  clock 3% off nominal), the CDS symbols after it, a Warm Start keeping the registers, the
  Safe-Lock count under a config CSV; a bring-up of the other PHY family (FBCSE to DLV and
  back) followed by no restart. A paused clock's phantom Warm Start dropped by the decode.
  The registers reset at the Bus Reset (the restart row's section agreeing), a config CSV
  keeping its ports through a Cold Start, decode_capture decoding as Session does, and the
  window: Timeline labels and Bus Grid overlays for Cold and Warm restarts and a mid-stream
  capture's, and the Registers PHY block following the cursor. After a short idle the Link
  is inactive from its last audio-mode edge. A clock pause with no bring-up: in phase,
  decoded on as without it (every command, segment and audio sample); out of phase, framed
  afresh and decoded as the joined demo alone; none found inside a start sequence.
- `test_register_filter.py` — the Registers pane's colour key as a filter by provenance
  (Cold Reset, Bus Write, Bus Read, CSV Import, Manual Edit): each category alone, Cold
  Reset off leaving only what something set, NEXT and CURR on their own provenance, a CSV
  compare as its own category, empty blocks hidden, the filter outliving a cursor-move
  rebuild, a block closed while filtering staying closed through one, opens and closes
  made while filtering given back, blocks kept by id across a title change, Peripheral
  Registers left closed, a filter that leaves nothing saying so, the entries as keyboard
  and accessible toggles (Space / Enter / Return, name, checked state, tooltip), and the
  workspace keeping the filter and the open blocks by id (titles still read).
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
  Cursors pane, propagation and drag with edge snapping.
- `test_locate_gui.py` — Locate Sub-Capture progress and teardown.
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
  the picker and menu entry; leaving, one Link left, and a middle Link's removal.
- `test_probe.py` — what Open Capture's probe reports for each format before decoding
  (channels and edge counts, the rate the loader will use, the length, windowed or read
  whole, the cost), what it refuses (one `.wfm`, two captures, a config CSV), an analog CSV
  with no TIME header refused within its preamble's bounds, a digital CSV header ending in a
  comma counted as the loader reads it, and that every
  format decodes a window, records it, and reopens it from the workspace source.
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
  About; no Links menu, with its items where they went (View ▸ Links with its shortcuts,
  Align in Cursors, a Link's label menu acting on that Link).
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
  a glyph button and a word-wrapped label are not clipped.
- `test_grid_group.py` — the Bus Grid's own pane group: every way in to the grid (cursor,
  toolbar, TX map, scroll, raise, theme, compare, its menu actions) draws the grid's Link
  while another is active, and a layout saved before the split puts it on the bottom's.
- `test_waveform_prefs.py` — line colours resolve override → preference → default, per
  theme; settings persist and a malformed one falls back; the Preferences dialog (low-
  contrast flag, reset, swatch pick, cancel); the Audio pane's per-stream Color… reaching
  Audio, Capture and Samples for one Link and one theme, and its workspace round trip.

*Authoring model (swviz)*
- `test_validators.py` — DataPort validator false-rejection fixes.
- `test_reference_model_clean.py` — the vendored reference model (`dataport.py`,
  `flow_control_port.py`) carries no `#` comments and keeps its docstrings.
- `test_delta_tpd_ground_shift.py` — ground-shift handling in the crossing envelope.

*The app, the tree and the gate*
- `test_app_launch.py` — window activation on startup (the macOS no-menu-bar case).
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
  CSV that does not load exits 1 and writes no model.
- `test_nputil.py` — dtype-safe `searchsorted` (the uint64 → float64 promotion stall), and
  `searchsorted_many` equal to numpy's and exact above float64 precision.
- `test_native_build_config.py` — the native build finds Python's headers.
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
- `test_native_reconfigure_survivor.py` — a port that keeps running through an immediate
  single-ranked write keeps its per-channel state: each channel's `index` continues without
  restarting, the sample in progress keeps its start sample, every sample starts after the
  previous one on its channel ends, and the sample size stays constant. Each case runs with
  the write on a sample boundary and halfway through an interval. Also checks that the
  `Decoder.total_uis` docstring describes the total as refreshed during the decode and as an
  estimate for a recovered-clock source. Also an immediate SampleSizeGrouping write that
  lands mid-sample (`make_immediate_sample_size_levels`): the sample in flight finishes at
  its old width, later samples use the new width, all bit-exact.
- `test_native_fcp_start_column.py` — a config-CSV decode of the flow-control demo started at
  each column of a row places every DRQ at the same distance from the samples it gates as
  the wire decode does (the Flow Control Port shares the data port's Column-0 phase).
- `test_release_gate.py` — the gate is one definition, CI and the gate stay in step, the
  wrappers (`gate.sh`, `gate.ps1`, `run_all.sh`) hold no logic, `--only` is never read as
  the gate, the release docs describe reality, and pyproject.toml is configuration
  setuptools accepts, listing every package and every asset a wheel must carry, the CI lint
  job pins the baseline's mypy and compares, the mypy ratchet reads no site-packages, the
  publish leak check fails without site patterns or when the scan read fewer files than the
  tree holds, the gate reports an empty pattern file as NOT RUN, a mypy run stopped by a
  blocking error is refused, publish check 6 compiles the core from the assembled tree's own
  sources, the mypy ratchet compares each platform with its own baseline counts (a rise on
  a platform below another platform's count fails), `--update` records per-platform counts
  and the checked-in baseline holds them for every platform, publish check 5 runs this
  checkout's leak scan on the assembled tree, a symlink to a directory is counted by check 5
  and resolved by check 2, publish_tree's usage says a tree with no sha is built from HEAD
  and uncommitted tracked changes are reported, and the gate's native probe asserts which
  swi3score it imported.
- `test_collection.py` — every test file collects at least one test, and the per-suite
  runner visits every file pytest collects.
- `test_testing_doc.py` — this document names every suite, and none that is gone.
- `test_leak_scan.py` — the structural leak patterns catch real leaks (escaped Windows home
  paths, Linux home paths, macOS volume, iCloud Drive and container paths, and the scanned
  tree's own absolute path) and not prose. A stray NUL does not turn a text file into UTF-16,
  and UTF-16 is read with or without a byte-order mark. The working-tree scan includes
  untracked, non-ignored files. An exported tree is walked whole except what its own
  .gitignore excludes. A pattern file holding no patterns is reported as covering nothing.
  A symlink to a directory is listed as a file, not descended into, and scanned by its
  target path. `--tree=DIR` walks a directory with the scanner's own patterns, including the
  absolute path of the scanner's checkout; `--tree` with a sha, with `--text=`, or naming no
  directory is refused.
- `test_publish_sync.py` — `tools/publish_tree.py`'s check 3b on a throwaway upstream
  repository: no recorded sync point, upstream commits not taken in (listed), a sync point
  that is unknown or rewritten out of upstream's history, a contributed sentence undone beside
  the release's own edits, and a contributed file missing are refused, in every recorded
  window; the contributed text kept, re-wrapped with CRLF line ends, or edited later passes, a
  publish commit is not held against the tree, and the upstream-owned files are not checked.
  `--record-sync` refuses until the contributed changes are in the working tree, appends once,
  and takes only a publish commit as the first point. Also the `docs/releases/next.md` rules:
  a release tree carrying it is refused, a preview describes itself from it, and an open one is
  marked `(in development)`.
- `test_publish_tiers.py` — the three content tiers stay apart. Every published file,
  whatever its type, is checked for references to the internal tier, by path or by a
  relative Markdown or HTML link or a symlink resolved from the citing file, by the tier test
  and by publish_tree's check 2.
- `test_tools_gitignore.py` — every .gitignore rule is a pattern git applies as written (no
  comment after a pattern on the same line), and the root scratch files frame_model.json,
  bus_config.csv and init.csv are ignored.
- `test_name_review.py` — the publish-time name review: a word new since the last published
  tree is listed (inside identifiers and file paths, in UTF-16 and binary files, part numbers
  kept whole, accented and non-Latin names read whole (Unicode letters after NFC
  normalisation, and a run of a script without case such as CJK or kana is one word at two
  characters or more), a symlink read as its target path in the tree and in the baseline,
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
  DIFFERENT row rate use the PHY3 demo (3072 kHz): PHY1 and PHY2 share 1536 kHz.
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
- **Visualizer repos** — `../mipi-soundwire-I3S-visualizer` (v2.0) and `…-1.74` (old) for
  the placement cross-check and CSV-format conversion tests.

## 6. How to run

**Setting up.** `requirements-dev.txt` lists what testing needs: the runtime set, pytest,
pytest-cov, ruff, and mypy at the version `tools/mypy_baseline.json` records. CI installs it.
`bash tools/setup_dev.sh` (macOS / Linux) or `.\tools\setup_dev.ps1` (Windows) installs it and
builds the decode core into the venv `run.sh` / `run.ps1` use, then prints that venv's activate
command; `--test` / `-Test` also runs the functional suite.

**Day to day, plain `pytest`.** `pyproject.toml` defaults it to `-m "not perf"`, so a
bare run is the functional suite; the perf ceilings are wall clocks and only mean something
in their own process, where the gate and CI run them (`pytest -m perf`). An explicit `-m`
on the command line overrides the default.

```bash
cd swi3s-studio
python3 -m pytest                     # the functional suite (-m "not perf" by default)
python3 -m pytest tests/test_links_gui.py -k offset     # one suite / some tests
python3 -m pytest -m perf             # the perf ceilings, in their own process
```

**Per suite, one process each:** the runner, a thin wrapper over the gate's per-suite
check (`tools/gate.py --only per-suite`), so the gate and the runner cannot drift:

```bash
bash tests/run_all.sh                  # every suite in its own process (excludes perf, like CI)
bash tests/run_all.sh --build          # rebuild the core and assert its ABI first
bash tests/run_all.sh --perf           # the perf benchmarks instead
bash tests/run_all.sh --native         # also run the native C++ suite (sibling checkout)
bash tests/run_all.sh --build --native --verbose     # --verbose lists every suite's count
```

**The runner covers exactly what CI is configured to run**: `python -m pytest` per file with
the same `-m "not perf"` marker `ci.yml` declares. The only difference is process
granularity, so you get per-suite results and teardown crashes can't be masked by whichever
suites shared a process. A failing suite prints its last lines. `tests/test_collection.py`
pins that equivalence, and fails the build if any test file collects zero tests. Any
`python3 tools/gate.py --only a,b` run ends in a PASS that says it was partial: only the
full gate is the gate.

Running a suite as a script (`python3 tests/test_x.py`) executes nothing, except in the two
files with a `__main__`, each of which regenerates a golden with `--regen`:
`test_authoring_render_golden.py` (the authoring render golden) and `test_filters.py`
(`tests/fixtures/highpass_reference.json`, with SciPy). Show the diff of the regenerated
fixture first and wait for review before committing it.

By hand, without the runner:

```bash
# 0. (after any C++ edit) rebuild the core — see §2.
cd swi3s-studio/native && PYBIND11_INCLUDE="$(python3 -c 'import pybind11; print(pybind11.get_include())')" bash build_local.sh

# 1. A single suite (conftest sets the offscreen platform; PYTHONPATH as below):
cd swi3s-studio
PYTHONPATH="$PWD" python3 -m pytest tests/test_gui_smoke.py -q

# 2. Native C++ suite:
bash ../protocol-analyzer/SwI3sAnalyzer/test/run_tests.sh
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
| 8b/10b, CRC16, comma, tokens | C++ core | `test_cds_primitives`, `test_symbols` | core |
| Descrambler | C++ core | `test_descrambler` | core (spec vectors) |
| Command transport parse | C++ core | `test_roundtrip`, `test_swi3score`, `test_cds_meaning` | core + binding |
| Register snoop / config build | C++ core | `test_register_snoop`, `test_swi3score` | core |
| Register **encode** (config→writes) | `native/.../Decoder.cpp` | `test_compare` (round-trip) | core + binding |
| Column detect + phase offset / resync | C++ core | `test_column_detect`, (real captures, manual) | core |
| Audio placement (§14.2.5) | C++ core | `test_simulation_roundtrip`, `compare_placement.py` | core + cross-check |
| Sample-rate formula | C++ core | `test_sample_rate` | core |
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
| Link-control events (PM_Action) | `analysis/link_events` | `test_register_model` (link_events) | app |
| Responses / measurements / errors / filter | `analysis/*` | `test_responses`, `test_measurements`, `test_analysis_filter` | app |
| Config compare (grid + report + overlay) | `analysis/compare`, `ui/register_view` | `test_compare`, `test_gui_smoke` | app + GUI |
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
| Placement parity (90 configs, fast check) | `Decoder` `layoutGrid`, `grid_from_csv` | `test_visualizer_placement` | binding vs golden |
| Bus model → JSON export | `model/viz_engine.model_json` | `test_visualizer_engine` | app |
| Authoring panel + Use-as-Expected | `ui/authoring`, `ui/main_window` | `test_modes_gui` | GUI |
| SWI3S timing margins (compute, F_max, corner) | `timing/calculator`, `timing/delta_tpd` | `test_timing`, `test_timing_cross_spec`, `test_timing_fmax_rate` | app |
| Divergence from the reference analysis | `timing/*` vs `timing-analysis/swtiming` | `test_timing_vs_reference` (skips if absent) | app |
| Displayed equation sums to the margin printed beside it | `timing/calculator` | `test_timing_cross_spec` | app |
| Timing view (margins text + round-trip) | `ui/timing_view` | `test_timing` | GUI |
| Workspace (mode + authoring + timing) | `workspace`, `ui/main_window` | `test_modes_gui`, `test_workspace` | app + GUI |

## 8. Conventions for new tests

- **One concern per file**, named `tests/test_<concern>.py`, functions `test_*`, run by
  pytest. No `__main__` block other than a golden's `--regen` (§6). Name the file in §3.2
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
  traceback.
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
  *Risk: neither net is a superset of the other.* CI does not run the process-isolated
  per-suite pass and does not assert a stale native ABI; the hand-run gate runs only where
  it is run, at a release (the development machine, a Windows guest on 3.14 and a Linux
  guest on 3.11–3.13), not on every push. Both are required at a release — see
  DEVELOPMENT.md ▸ Release checklist steps 2 and 3, and `tools/gate.py`, which is the one
  definition of the gate.
- **Performance is pinned by wall-clock cliff detectors and counts.** `tests/test_perf.py`
  holds wall-clock cliff-detectors on a large synthetic capture, run in CI and in the release
  gate (`-m perf`) and locally via `run_all.sh --perf`; see the maintainers' performance notes.
  It holds an **end-to-end per-cursor-move budget** with every dock open, on a fixture big
  enough to expose a per-move scan (`from_demo(20000)`: 10.2 M UIs, 408 commands, 3 config regions),
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
  a synthetic regression where possible. `test_ingest_formats.test_real_sal_if_present`
  reads any present locally but checks only that the edges are ordered.
- **GUI testing is mostly wiring-level.** Wiring and population are pinned everywhere;
  interaction is pinned for the timeline (zoom, pan, seek, bookmark drag) and the main
  dialogs, through real events and the dialogs' own widgets. Pixel rendering, and the
  Dataport Scramblers dialog, are not.
- **Compare's register diff is known-unreliable** when the visualizer and decoder number
  dataports differently; the grid diff is the trusted signal. The register diff is
  `register_diff` in `analysis/compare.py`, which matches registers by device and address.
- **Some cross-checks depend on sibling repos.** The *native* C++ placement
  cross-check and the descrambler vectors use `../mipi-soundwire-I3S-visualizer`;
  absent it, those silently skip. *Mitigated* for placement: the Visualizer's
  96-config corpus + per-DP goldens are **bundled** under `tests/visualizer/`,
  so `test_visualizer_placement` pins placement parity self-contained (no sibling
  repo). Regenerate the goldens from the Visualizer's `model_dump.py` if placement
  intentionally changes.

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

*Cross-references: `architecture.md` §3 (shared core), §11 (testing/headless overview);
the native runner `protocol-analyzer/SwI3sAnalyzer/test/run_tests.sh`.*
