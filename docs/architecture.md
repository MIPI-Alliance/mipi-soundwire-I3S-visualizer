# SWI3S Studio — Architecture

A desktop **MIPI SoundWire I3S (SWI3S)** bus analyzer. It decodes captured PHY1 /
PHY2 traffic (forwarded clock + bidirectional data) and PHY3 / DLV traffic (recovered
clock), reconstructs the control protocol and audio
payload, and visualizes the 2D bus structure, the per-device register maps, and the
decoded audio, streaming over large captures.

It has two engines, complementary and non-overlapping:

- the **C++ wire decode** (`native/swi3score/core/`): NRZS, 8b/10b, CRC-16, Command
  Transport Protocol, register snoop, SSP anchoring, LFSR descramble, audio sample
  reconstruction, column auto-detect.
- the **2D layout model** (`swi3s_studio/swviz/`): DataPort / FlowControlPort placement
  (the normative §14.2.5 cascade), register/config model, CSV/JSON schema, slot
  colouring, clash detection.

The §14.2.5 placement cascade exists in **two** implementations: the C++
`CDataPort`/`CFlowControlPort` core, and the Python `swviz` engine. They are held
bit-exact by the golden cross-check (`tests/test_visualizer_placement.py`, every
example config; `tests/test_grid_cross_engine.py` compares the two live paths
directly). Each drives one mode:

- **Analysis mode** (a decoded capture) renders from the **C++ core**
  (`swi3score.grid_from_commands`) — the same engine that reconstructs audio.
- **Visualization mode** (live authoring) renders from the **Python `swviz` engine**
  (`viz_engine.render_payload`), which additionally computes the authoring-time
  validation warnings the C++ decode path doesn't (scrambler/test-mode/interval/
  sample-bit mismatches, sink-handover, enabled-no-channel).

The two engines are live by design: they differ on full authoring output, and the golden
suite (`test_visualizer_engine`, `test_visualizer_placement`) holds them together.

---

## 1. Decisions of record

| Area | Decision |
|---|---|
| GUI / rendering | **PySide6 (Qt 6)** + **pyqtgraph**; `QGraphicsView` for the 2D grid; Qt model/view for tables |
| Decode engine | **Reuse the verified C++ decode** via a **pybind11** module (`swi3score`); the per-UI hot loop stays in C++ |
| Column-count / CDS alignment | **Not signalled on the wire** — recovered by *hypothesize-and-validate* (`CColumnDetector`): score each candidate (even count 2–32 × Column-0 phase) by CRC-valid CDS command phases; best wins. **Commit-independent** — needs only some CRC-valid CDS traffic. |
| Inputs | **Logic 2 `.sal` project**, **per-channel `<SALEAE>` binary** pair (`.bin`), **Saleae digital CSV** (`Time,Clock,Data`), **VCD**, **`.wfm` analog pair**, **analog scope CSV**, **visualizer CSV/JSON config**. (`.sal` is wired to Open via `ingest/saleae_sal` — pick the clock/data channels; v0, single-/multi-chunk v3 and v4 digital blobs decode, with v0 pre-trigger times rebased so they don't wrap. Analog inputs are thresholded to edges in `ingest/analog`.) |
| Capture scale | **Minutes / multi-GB** — streaming decode, audio rendered from pyramids, results held in memory (the status note in §4 says what is built and what is the design target) |
| Live capture | **Offline first**, architected live-ready (streaming decode, bounded buffers, partial results) |
| Platforms | **macOS-first, fully cross-platform** (Qt + portable pybind11 build); packaged with PyInstaller |
| Register map | **Extracted from the SWI3S spec** → structured JSON; single source of truth for the register view *and* address→field-name resolution |
| Results store | In-memory results; an **Apache Arrow** command cache for display and export; audio with **min/max pyramids** (an optional `np.memmap` backend that tests use). Register state at the cursor is replayed from the in-memory command list |
| Analyzer plugin | Not part of this repository: it builds only the `swi3score` module |

---

## 2. Layered architecture

```
┌────────────┐   ┌─────────────────────────┐   ┌──────────────────┐   ┌────────────────┐
│  ingest    │──►│  swi3score (C++/pybind11)│──►│  results store   │──►│  UI (PySide6)  │
│  (python)  │   │  decode loop, one pass   │   │  (python)        │   │  dockable      │
│  .sal      │   │   no SDK dependency      │   │  Arrow cache     │   │  panels +      │
│  CSV       │   │  register model, SSP,    │   │  audio min/max   │   │  one shared    │
│  config    │   │  descramble, audio       │   │  pyramids        │   │  time cursor   │
│            │   │  via ISampleSource       │   │                  │   │                │
└────────────┘   └─────────────────────────┘   └──────────────────┘   └────────────────┘
```

Module map (the repository root; the Python package is `swi3s_studio/`):

```
swi3s_studio/
  session.py       # Session: one Link's decode (source, config CSV, what-if overlay, results)
  api.py           # decode() / decode_capture(): headless decode through swi3score
  links.py         # LinkSet: the open Links, the active one, offsets and the time mapping
  workspace.py     # the .swi3s workspace: read, write, migrate; capture paths kept relative
  cli.py, app.py   # command line (headless -o model dump) and the GUI entry point
  fsutil.py        # write_whole: the atomic file writer the exporters share (a new file beside the target, moved into place once complete)
  ingest/          # capture files -> Capture (clock/data edge arrays) -> TransitionSampleSource:
                   #   saleae_sal (.sal), saleae_binary (.bin pair), digital_csv, vcd,
                   #   wfm / analog_csv / analog (scope exports, thresholded to logic),
                   #   timebase (the rate of a capture that stores times), transitions
                   #   (the synthetic demos); sal_export / raw_export write captures out;
                   #   visualizer_csv upgrades older config CSVs; probe says what a file is
                   #   before decoding (the Open Capture dialog's Source)
  store/           # audio_store (AudioStore: per-stream samples, memmap backend, render
                   #   pyramid, WAV export); command_store (functions: commands to an Arrow
                   #   table, save/load Parquet, register writes)
  model/           # registers (RegisterMap from data/registers.json, DeviceRegisterFile,
                   #   provenance), regmap_import (peripheral register maps), bus_config
                   #   (the authored BusConfig and its v2.0 CSV), viz_engine (the BusModel
                   #   adapter over swviz), bookmarks, grid_slots
  swviz/           # the Visualizer engine: models/ (Interface, DataPort, FlowControlPort,
                   #   Device, BusModel, ...; dataport.py and flow_control_port.py are the
                   #   vendored reference model), core/engine.py, drawing/clash_detector.py,
                   #   io/ (CSV and JSON handlers), config/, utils/
  analysis/        # link_control, link_events, cds_meaning, bus_timing (measured setup/hold
                   #   eye), compare, responses (incl. per-device ping), errors, issues,
                   #   measurements, port_status, register_history, subcapture, dlv_detect
  dsp/             # band-limited polyphase resampler + PDM→PCM decimation (resample.py);
                   # zero-phase Butterworth high-pass and DC blocker for per-stream processing
                   # (filters.py); the spectrogram lane's short-time spectrum (spectrogram.py)
  timing/          # SWI3S PHY timing calculator (calculator.py: compute + worst-corner),
                   #   the crossing envelope (delta_tpd.py), per-spec presets (spec_source.py),
                   #   per-leg event timelines (timeline.py)
  export/          # commands_csv: the decoded commands as CSV
  ui/
    main_window.py # dockable layout, menus, the load queue, the Links' views
    mode_controller.py # the three modes and each one's docks
    grid_view.py   # 2D bus grid (QGraphicsView), SVG/PNG export
    symbol_view.py # color-coded 8b/10b CDS symbols
    command_table.py
    register_view.py, register_history_dialog.py
    audio_view.py  # pyqtgraph waveforms / spectrograms + QAudioSink playback
    save_path.py   # a save dialog's result: the format's suffix added, a replacement confirmed
    port_names.py  # how a data port is labelled: the user's name, else Dev/DP numbers
    eye_view.py    # measured setup/hold + eye histograms
    timeline.py    # whole-capture overview ribbon
    timing_view.py, timing_diagram.py  # the Timing Calculator and its term diagram
    cursor.py      # shared TimeCursor (synchronized navigation)
    theme.py       # VizTheme palettes (dark / light) and the stylesheet helpers
    authoring/     # the Visualization mode's authoring panel and notifications
    ...            # the dialogs (*_dialog.py) and the other panes
data/
  registers.json   # register map from the SWI3S spec (source of truth)
  regmaps/         # bundled example peripheral register map
native/
  swi3score/       # pybind11 bindings + ISampleSource/Decoder
    core/          # the SWI3S decode core, maintained here — standalone
  CMakeLists.txt   # builds the python module (scikit-build-core)
  build_local.sh   # offline build: the module at the repository root
tests/
```

---

## 3. Shared decode core

The decode classes are in `native/swi3score/core/` (see that folder's README for the file
list), so the repository builds standalone. They are SDK-free: they use `LogicPublicTypes.h`
only for `U8`/`U16`/`BitState`, which `native/swi3score/compat/` supplies. The `Decoder`
reads its input from an abstract sample source.

The sample source is `ISampleSource` (`native/swi3score/ISampleSource.h`); the Python ingest
readers hand it edge arrays through `TransitionSampleSource` or `DlvSampleSource`, and
`MemorySampleSource` is the in-memory source of the tests and the demos:

```cpp
class ISampleSource {
public:
    // Core (required): advance one UI; report the bounding edge's direction, the
    // data-line level for the UI, and the absolute sample. False at end of capture.
    virtual bool NextUi(bool& rising, bool& dataHigh, uint64_t& sampleNumber) = 0;
    virtual uint64_t SampleRateHz() const = 0;
    // Random access (default: not seekable) — powers windowed re-decode (symbol viewer).
    virtual bool CanSeek() const { return false; }
    virtual void Seek(uint64_t uiIndex) {}
    // Committed NumColumns pushed down by the decoder — a recovered-clock (DLV) source
    // uses it to subdivide each row; a forwarded-clock source ignores it.
    virtual void SetColumns(int columnCount) {}
    // A later link bring-up: a recovered-clock (DLV) source re-acquires its clock at the
    // first row edge at or after `sample`; a forwarded-clock source ignores it.
    virtual void Reacquire(uint64_t sample, int columns) {}
    // Absolute index of the next UI (UIs consumed) — stamps segment boundaries so
    // windowed re-decodes align row numbering.
    virtual uint64_t UiIndex() const { return 0; }
    // Total UIs: exact for a forwarded-clock capture, an estimate the decoder re-reads for a
    // recovered-clock (DLV) source, else 0 — decode PROGRESS only, never correctness.
    virtual uint64_t TotalUiCount() const { return 0; }
    virtual ~ISampleSource() = default;
};
```

Every method but the two core ones is defaulted, so a pure streaming reader satisfies the
interface with only `NextUi` and `SampleRateHz`.

The `Decoder` drives these classes from the sample source: `CNrzsDecoder`, `C8b10bDecoder`,
`CColumnDetector`, `CCommandTransportParser`, `CRegisterModel`, `CDataPort`,
`CFlowControlPort`, `CPayloadEngine`, `CDescrambler`, `CCrc16`, `SwI3sProtocolDefs`. This
repository builds one target, the `swi3score` pybind11 module.

### Column-count / CDS-column detection

SWI3S does **not** signal the column count on the wire — the spec (`{ASW3713}`)
says a receiver must "try all possible Column Counts until it reattaches" — so
`CColumnDetector` recovers it by **hypothesize-and-validate**. This is
**commit-independent**: commits are just one CDS command; detection keys off *any*
CRC-valid Control-Data-Stream traffic (pings, register ops, config, …).

From a bounded leading window of raw data-line levels (one per UI, starting on the
first rising clock edge; `kDetectWindow` UIs), it tries every legal PHY2 count
(even, 2–32) × every Column-0 **phase offset** (even offsets only — Column 0 lands
on a rising edge, so it falls on an even window index; the offset disambiguates
*which* rising-edge UI opens the row when >2 columns fall on rising edges). For each
`(count, offset)` it lifts the Column-0 bit at indices where `(i − offset) % count
== 0`, **NRZS-decodes** it against the preceding UI (same level → 1, toggle → 0),
and feeds the reconstructed CDS bit stream to `CCommandTransportParser`; the score
is the number of **CRC-16-valid** command phases. Highest score wins (a wrong
count/phase almost never validates a CRC); below ~2 valid phases it returns nothing
and the decoder falls back to the cold-start column count (so a *truly silent* CDS
also falls back). Column 0 then recurs every `column_count` UIs from that anchor;
the Raw-Capture view marks each Row-Sync-Point rising edge from the same segment
geometry (`session.cds_column_samples`). A mid-stream geometry change re-runs the
same detection (resync watchdog), producing one segment per width; a later link bring-up
opens one too (§6), so there is one segment per column count or link restart.

### pybind11 surface

The per-UI loop must **not** cross the Python boundary. The core runs the whole decode in
one call and the results are read back in bulk:

```python
import swi3score
dec = swi3score.Decoder(source, settings)     # source = a swi3score.ISampleSource
                                               # (TransitionSampleSource, DlvSampleSource;
                                               # MemorySampleSource for tests and demos)
dec.run()                                      # streaming, single pass, GIL released
dec.commands()                                 # the decoded commands
dec.audio_columns()                            # per-(dp,channel) arrays
dec.segments()                                 # one per column count or link restart
dec.bit_samples_window(...)                    # windowed bit-sample replay
```

Settings: column-count (auto/forced), config CSV (mid-stream), decode-audio, `dlv` (a
recovered-clock PHY3 source), `cds_horizontal_start`. Some are scoped to one config section (an index
into `segments()`): what-if register overrides, a forced column count, and
`imported_config_sections`, whose port config is built from that section's overrides
alone rather than over the snoop. A config CSV imported into one region of a multi-region
capture is those three together (`Session.apply_config_csv_at`); the global config CSV
would frame every region at its width.

The native config-CSV loader reads cells as Python's csv reader with newline='' does: a line
ends at LF, CR LF or a lone CR outside a quoted cell, a quoted cell is
unquoted, a doubled quote reads as one, and a ',' or line break inside it is part of the cell.
RowRate is decimal only, with a '.' decimal point whatever the process locale. A path that is
a directory, or a read that fails, reports 'Cannot open config CSV' and loads nothing. A row the
decode reads that the file does not carry takes its default and is reported by name (one
warning). A CDS per-source row given by its legacy scalar spelling counts as present. A warning
lists at most 3 items and counts the rest, and quotes a cell or key of up to 60 characters
whole, a longer one as its first 59 and an ellipsis.

### Performance & language choice

**One scalar loop in C++; everything else Python.** The C++/Python boundary is drawn
at exactly one place — the **per-UI decode loop**. The loop runs once per unit interval
(tens to hundreds of millions of times on a multi-GB capture) doing branchy, stateful,
bit-level work: data-line
sampling, NRZS, 8b/10b + CRC-16, the command-transport parser, the register model, the
`CDataPort` placement cascade, the LFSR descrambler, and audio reconstruction. Measured,
the C++ core decodes **~35M UIs/s** (≈1.9 s for a 65M-UI / 31M-audio-sample capture), and
it **releases the GIL**, so decode runs on a worker thread with a responsive UI.

Everything *around* the loop is Python — none of it is on the
hot path: ingest (edge arrays), the results store (NumPy `lexsort`/grouping), all
analysis (`bus_timing` etc., NumPy-vectorized), PDM decimation (FFT), and the UI.

Keep the per-UI loop in C++: its dominant stages (the self-syncing descrambler, CDS
framing / comma re-sync, dual-ranked register commit + SSP anchoring, the placement
cascade) are sequential and stateful and do not vectorize.

---

## 4. Memory architecture (multi-GB)

The design principle: **no view cost scales with capture size.**

1. **Inputs are edge-based, not per-sample.** A `.sal` stores per-channel
   *transition* lists; the decoder consumes clock edges + data level, which *is*
   the transition data. The decoder streams the transition arrays — a
   full raw-sample buffer never exists. (CSV is parsed streaming into the same
   edge form.)

2. **Decode is single-pass and streaming**, writing typed event streams to the
   results store:

   > **Status — partially realized; the rest is the design target.** What
   > ships: the single-pass streaming decode; the CDS-symbol and data-bit
   > windowed re-decode (items 6–7 below, fully realized); and audio
   > min/max pyramids for the timeline. What is *scaffolded but not on the hot
   > path*: `store/audio_store.py` has a working `np.memmap` backend, but every
   > production caller uses the in-RAM branch (`mmap_dir` is only passed in tests),
   > because the decoder hands its results to `Session` as in-memory
   > Python/NumPy structures rather than to a persistent store. `store/command_store.py`
   > (Arrow/Parquet) is a display/export cache, not fed back into replay. Register state
   > at the cursor is replayed from the in-memory command list.

   | Stream | Volume | Storage |
   |---|---|---|
   | Commands (phases) | sparse (thousands–millions) | in-memory arrays; an **Arrow/Parquet** cache for display and export |
   | Audio samples | large | per-`(dp,channel)` arrays in RAM (an optional **`np.memmap`** backend) + **min/max pyramid** |
   | Register events (writes/commits) | sparse | the in-memory command list; register state at a sample is replayed from it |
   | CDS symbols | enormous | **not persisted** — re-decoded per visible window on demand, cached |
   | Data-bit sample points | enormous | **not persisted** — re-decoded per visible window from **transport-phase checkpoints** (item 7 below) |

3. **Range queries.** Every panel pulls only what is on screen: commands by binary search
   on start sample over the in-memory command list, audio from the pyramids.

4. **Waveform pyramids (mip-maps).** For audio, precompute min/max per bin at
   several zoom levels; pan/zoom renders from pre-binned data and never touches
   the full array.

5. **Register state at time T.** Reconstructed by replaying the register events of the
   command list up to T — powering the "register map at the cursor" view and dual-rank
   staged/committed display.

6. **Symbol viewer.** Persisting every 8b/10b symbol is infeasible at multi-GB.
   Instead, the core can **seek the sample source and re-decode a window** on
   demand (cheap, edge-based); recently viewed windows are cached.

7. **Port sample points** (the Capture pane's overlay: where each data bit is latched).
   Also infeasible to persist whole — but a bit's *sample position* is a pure function
   of the transport **schedule** (config + SSP phase), independent of the descrambler.
   So the streaming decode records a light **transport event log** (each payload
   (re)Configure / SSP re-anchor) plus **periodic checkpoints** of each port's transport
   phase (the window needs no descrambler state); a windowed request
   (`Decoder::bitSamplesInWindow`) restores the nearest checkpoint,
   fast-forwards the schedule to the window (bounded by the checkpoint interval), then
   ticks the window reading the wire to stamp each sampled bit. No re-decode of the
   whole capture, no descramble — enabling the overlay and navigating are cheap and
   every region resolves. (A `collect_bit_samples` whole-capture path still exists as
   the test oracle the windowed path is checked bit-for-bit against.)

---

## 5. UI

Dockable panels (IDE style) bound by **one shared time cursor +
visible-range model** (`ui/cursor.py`). Selecting an item in any panel drives the
others — Wireshark-style linked navigation.

- **2D Bus Grid** (`QGraphicsView`): the Rows×Columns layout *at the cursor time*,
  color-coded slots (Data / TxPresent / Guard / Tail / DRQ / CDS / empty). Reuses
  the visualizer's placement output (from the core) + clash detector, rendered on
  a `QGraphicsScene` (zoom/pan; SVG/PNG export).
- **CDS Symbol viewer**: 8b/10b symbols color-coded **per spec** — K.28.7 comma,
  Robust Tokens, D-codes, K-codes, disparity — time-aligned, click-to-inspect
  (raw 10b, decoded byte/token, running disparity).
- **Command table** (virtual `QAbstractTableModel`): timestamp, phase, device(s),
  opcode, **address resolved to register + field name** (from `registers.json`),
  data, CRC ok/bad, peripheral/manager response. Filter + search + bookmarks.
- **Register-map view** (per device): every register; **color-coded by
  provenance** —
  - *Cold Reset* (reset value, untouched) — neutral
  - *Bus Write* (CRC-valid WriteA32) — with value + write timestamp;
    dual-rank shows staged `_NEXT` vs committed `_CURR`
  - *Bus Read* (CRC-valid Read's returned data) — the timeline's read colour; a
    read updates the rank it addresses (`_NEXT` alias → `_NEXT`, `_CURR` → `_CURR`)
  - *CSV Import* (expected config overlaid for Compare) — distinct color
  - *Manual Edit* (a what-if override, below) — its own color
  Field tooltips decode bit ranges. **What-if editing**: right-click (or double-click) a
  register for *Edit fields…* or *Force value…*, which re-decodes so every pane follows;
  *Clear all manual edits* reverts (shown as *Manual Edit*). **The colour key is a filter
  by provenance**: each entry (`click_label.KeyToggle`, a checkable button the keyboard
  reaches) hides or shows the rows whose value came from it, each row filtered on its own
  provenance (`_PROV_ROLE`, so `_NEXT` and `_CURR` apart) and a block left empty hidden.
  Starting or changing the filter opens the blocks with rows to show (not Peripheral
  Registers); rebuilds keep the user's open blocks, which are tracked by block id
  (`_BLOCK_ROLE`, stable when a PHY or device changes a title) and given back when every
  category shows again. A filter that leaves nothing says so; the workspace keeps the
  filter and the open blocks (`ui/view_state.py`).
- **Audio viewer** (pyqtgraph): multi-track waveforms per `(dp,channel)`, smooth
  zoom/scroll from the pyramid; region-select → **WAV export**; in-app **playback**
  via `QAudioSink`. Sample rate derived from RowRate + DP params (existing
  `SampleRateHz`). A **PDM** data port (1-bit `sample_size`) is a bipolar *density*
  code, not a 1-bit two's-complement sample, so it is decoded to PCM at store-build
  time: `{0,1}→±1`, band-limited polyphase decimation to ~48 kHz (`dsp/resample.py`),
  then scaled, keeping the density's bias — see `store.audio_store.decode_pdm`.
  **Per-stream DC blocker, high-pass and gain** (`StreamProcessing`, the Audio pane's
  Filter & Gain dialog) is applied INSIDE the store, in that order: `AudioStore.set_processing` replaces the stream's
  channel arrays with processed ones and keeps the decoded arrays aside for Revert, so the
  waveform, playback and WAV export all read the same samples with no change of their own.
  The filter is `dsp/filters.py`: SciPy's Butterworth design and `sosfiltfilt` algorithm in
  numpy, padded by three periods of the corner, with the per-sample recursion in the native
  core (`swi3score.sosfilt`) because numpy cannot vectorise it. The setting is the Link's
  (`LinkPanelState.stream_processing`), re-applied to every new store a re-decode builds,
  and saved per Link in the workspace.
- **Timing** pane (pyqtgraph): measured **setup/hold** timing straight off the
  capture's clock/data edges (`analysis/bus_timing.py`) — per-polarity setup/hold
  histograms + a data-edge "eye", gated on real data transitions, with a margin
  verdict. Aggregate over the capture (no time cursor); rendered lazily on first show.
- **Statistics** panel: derived measurements (row/clock rate, per-DP SSP interval &
  bandwidth, bus-config segments, link `PM_Action` events).
- **Timeline overview ribbon**: whole-capture minimap with markers for commits,
  SSPs, PHY/link events, and errors; click to seek.

### Multiple Links

An analysis holds one or more SWI3S **Links** (spec §4.7: one Manager and its Peripherals on
one bus). Links are independent buses, so nothing is decoded across them. Each is a complete
`Session`.

- **Model.** `links.LinkSet` holds the Links in order, with one **active**. A Link's identity
  is its index; its name is display only.
- **Time.** Each Link counts time in its own capture's samples. The Links share a global
  time in int64 picoseconds, and `LinkEntry.to_ps` / `to_sample` are the only conversion:
  exact rational arithmetic, with a signed `offset_ps` placing the Link's sample 0.
  - The offset is a mapping. Changing it re-decodes nothing and forbids nothing, including
    a negative offset.
  - `LinkSet.convert` moves a sample between Links, and returns it untouched within one
    Link. So a single Link never passes through the rounding.
- **Per-Link window state.** `ui.link_state.LinkPanelState` holds what the window derives
  from one Link, built once per decode (`_build_link_state`) and kept across switches:
  - the command model, cursor samples and command kinds
  - the audio store
  - the Timing measurement
  - the Statistics rows
  - the Commands filter and column widths
- **Pane groups.** The panes form four groups, each showing a Link of its own:
  - Commands
  - Registers and Statistics
  - the Bus Grid (its own group, since it stays one Link at a time; its handlers run
    under `_on_grid_link`)
  - Capture, CDS, Audio, Samples and Timing, which can also show every Link: Capture,
    Audio, CDS and Samples as stacked lanes (`ui/link_lanes.py`, one single-Link view per
    Link on global time); Timing stacks the same way

  The Timeline spans every Link, one band each. Bookmarks belong to a Link and are drawn
  only on it (its band, and the bottom panes when they show it); the Cursors pane lists
  every Link's, and times a pair across two.
- **How the panes stay Link-unaware.** A group's bind, cursor redraw and refresh run under
  `_as_link(i)`, which makes Link i active and re-counts the cursor in its samples for the
  duration. One cursor therefore drives every group at the same instant on each group's
  Link.
- **The active Link** is what menus act on, and what the cursor counts in. It is the Link
  of the group last worked in, set by an application event filter that is installed only
  while some window has two or more Links. A menu that belongs to a group acts on that
  group's Link.
- **Commands, All Links.** `AllLinksCommandModel` merges every Link's command models in
  global time, behind a Link column, and draws each cell through its own Link's model.
  The one exception is Time, which is global.
- **Timeline.** One ribbon per Link, stacked. Each covers the union of every Link's span in
  its own samples, and they follow each other's zoom and pan through `viewChanged` /
  `set_view`.
- **Loading.** Loads are queued, one at a time, by kind:
  - `open` replaces the analysis.
  - `add` appends a Link.
  - `redecode` reloads one session in place.

  A request is superseded only by one that makes it pointless. A re-decode finds its Link
  by session identity, and rebinds only the groups that show that Link.
- **Workspace v4** (a `.swi3s` file; older `.json` ones still open). One
  `workspace.LinkSpec` per Link holds its source (with the window, in samples, when only
  part of the capture was decoded), name, offset, decode inputs, per-stream colours
  (`stream_colors`) and Filter & Gain (`stream_processing`), and its own `view`: the
  Commands filter and column widths and the Audio and Capture panes' state
  (`ui/view_state.py`). The workspace `view` holds the window's: each group's Link, the
  All Links choices, the Timeline height and zoom, the dock layout and geometry, the
  Commands sort and hidden columns, and the Samples, Registers, Statistics and Timing
  panes' settings, applied after the mode switch and again once the deferred layout
  passes have run. `Workspace.save` writes each capture file relative to the workspace
  (`source_files` knows which keys of each source type are files) with the absolute path
  under `saved_at`; `Workspace.load` resolves them, and `missing_files` / `locate` let the
  window ask for any it cannot find. A v3 workspace loads as one Link at offset 0.
  Commands column widths (each Link's `view.commands.col_widths`, and the window's
  `all_links_commands.col_widths`) are a JSON object, `{column name: width px}`, holding the
  shown columns only. A list is still read when it has exactly one entry per column. A
  Commands column the workspace does not record takes its default visibility and width. The
  Registers pane's column widths are kept by column name too; without them the pane
  opens at its default widths. The Audio pane's `channel_list_width` sizes its channel list.
  The `bookmarks` key holds the cursors, and `cursor` the playhead's sample.
- **Bookmarks.** A bookmark stores its own Link's sample and that Link's index, so it moves
  with its Link's offset. Cursors ▸ Align Links on Cursor Pair sets one Link's offset from a pair that
  marks the same event on two Links.

---

## 6. Link bring-up, PHY, and modes

- **From t=0**: decode link bring-up & timing (PHY selection, calibration,
  Announce/SSPA, ExitDormant) and learn the full configuration by snoop. The core
  already parses Announce/Commit/CalibratePhy phases; a small **link/PHY state
  machine** tracks PHY selection + timings on top.
- **Mid-stream**: column auto-detect + per-DP config from an imported visualizer
  CSV (already supported by the core).
- **The decode can overrule the bring-up detector.** `analysis/link_control` reads only
  the raw edges, and a capture with no bring-up can satisfy it (a mid-stream recording reads
  as a Warm Start). A real bring-up precedes audio mode, so the Session drops any detection
  with CRC-valid commands decoded inside it (`Session._decode_checking_bringup`), and
  decodes again when the dropped one had steered the decode (a PHY3 path, a Safe-Lock seed).
- **A Link can be brought up more than once.** After a clock stop longer than 1 ms past the
  previous bring-up's audio start (or anywhere, in a capture that starts in audio mode),
  `link_control.decode_link_controls` decodes the next start sequence from the stop on. A
  detection counts only if it reaches audio mode, its start pulse rises within a few clock
  edges of the clock moving again, and the clock then runs as audio mode does. The Session
  hands each later one of the decode's PHY family to the core as a restart point
  (`DecoderSettings.restart_points`: restart sample, Safe-Lock columns, cold, inactive
  from). The restart sample is the first clock edge after PhyStart (on DLV, the DP wire's:
  the first Row Sync), not the nominal audio start. From the clock stop to it nothing is
  decoded and every port stops. There the decode re-aligns, frames at the Safe-Lock count,
  reconfigures the payload engine on the new Column-0 phase and opens a segment flagged
  `restart`; only a Cold Start also flags it `cold_start` and resets the register model. A
  config CSV keeps its ports through both, and on FBCSE its column count. Every window read
  and edge hunt stops at the next restart point (the leading window is 262144 UIs, which a
  short first session fits inside), and the register and grid replays start from the Bus
  Reset of the last Cold Start before the cursor.
- **The phantom check applies to every bring-up.** The decode does not run inside a later
  one, so the evidence is what follows it: one after which no CRC-valid command decodes is
  checked by decoding once without it, and dropped when that decode finds CRC-valid
  commands from its clock stop on (`Session._check_later_bringups`).
- **A clock pause is not a bring-up.** Audio mode never pauses its clock, so a stop longer
  than 1 ms with audio mode's clock after it and no bring-up there means samples were lost
  or the capture was joined (`link_control.clock_resumes`, handed to the core as
  `DecoderSettings.resume_points` on FBCSE). There the decode reads a fresh window: a bus
  that carried on in phase is decoded on through as if nothing happened; one that did not is
  framed afresh, its column count and Column-0 phase detected as at the start of a capture
  that begins mid-stream, in a new segment that is no restart (registers kept). A later
  bring-up's Link is inactive from its last audio-mode clock edge, found back over any
  link-control edges before the stop (after a short idle the stop is the Bus Reset's LC:10
  itself, with the LC_Ack and LC:00 edges before it).
- **A DLV restart locks the recovered clock again.** Where the Link goes inactive the
  decoder calls `ISampleSource::Reacquire`, and `DlvSampleSource` skips to the bring-up's
  first Row Sync, the first rising DP edge at or after the restart sample: the stretch has
  no rows, and its link-control pulses are single-ended edges the DLL would snap to. At the
  restart it is called again with the restart's column count, and the DLL locks there as at
  the first bring-up, at the seeded Safe-Lock row period over that count (a bring-up does
  not change the row rate). Column 0 is the row it locked on (no phase search), the CDS is
  at CDS_HorizontalStart (Column 2, its reset value, after a Cold Start; a Warm Start keeps
  the registers and their column), and a config CSV does not set the count, as at the first
  bring-up. A row the
  stop cut short keeps its bus row, so the restart opens the next: DLV rows are numbered by
  their Row Sync Points (`Session.bus_row_for_sample`). The stretch has no UIs, so a sample
  in it maps to the restart's (`Session._ui_for_sample`). The row log stays in UI order: the
  UIs consumed after the last row edge belong to the row the stop cut short, and each
  re-acquisition records its row, its UI and column count, and that cut row's UI. A seek
  into the log (`bit_samples_window`, the CDS pane) replays each logged row from its edge,
  with the column count the log gives it (a commit's `SetColumns` reaches no replay); from a
  re-acquired row on the replay is the live decode exactly, while before it a seek restores
  a row's UI from its logged span rather than the DLL's running estimate, which can place a
  bit a sample away.
- **Later bring-ups keep the decode's PHY family.** `Session.link_controls` lists PHY3 ones
  on a DLV decode and PHY1 and PHY2 ones on an FBCSE one (and one naming no PHY on either).
  The sample source, the UI index every sample maps through, the CDS pane's source and the
  Capture pane's drawing are each fixed per capture, so the decode runs on through a
  bring-up that switches between the forwarded clock and the recovered one: the Timeline,
  the Bus Grid and the register replay present one bring-up
  per decode restart. The bring-up in force at the cursor names the Registers pane's PHY
  block (`Session.phy_at`).
- **DLV takes no resume points.** Its clock is the data line, so a pause is a stretch with
  no row edges, and after it the first rising edge need not open a row: framing it afresh
  needs the row phase found from the edges, with no bring-up to anchor it. The DLL coasts
  through a pause and the decode runs on.
- **PHY1, PHY2 and PHY3 (DLV)** all decode: PHY3's bit clock is recovered from the row
  edges by a virtual PLL, through the same `ISampleSource` + PHY state machine.
- **The PHY3 CDS column is CDS_HorizontalStart[4:0]** (reset value 2; Column 0 is Sync1,
  the last column Sync0). The decode reads the CDS at Column 2 until a commit makes a
  CDS_HorizontalStart current, and at that column from the row the commit takes effect. A
  move opens a new segment, which records its `cds_column`; the Session's symbol viewer, TX
  raster and bus rows read each region's column from its segment. An authored PHY3 config
  places the CDS as the visualizer does: S1 at Column 0, then S1's tail, its handover, then
  the CDS (`SwI3sConfig::CdsHorizontalStart`).
- **A write that changes a running port's single-ranked control register is flagged** as a
  programming error, not a protocol error (`running_port_write_error`): SampleSizeGrouping,
  PortControl, SkippingNumerator and FlowControl take effect at once, so they are set while
  the port's channels are disabled. The decode follows the write.

---

## 7. Register map

`data/registers.json` is extracted from the SWI3S specification: every block (SLC base
`0x1000`, Data Port `0x2000 +
0x100·n`, FCP), each register's offset/abs-address/name/reset/access, dual-rank
flag, and per-field bit-range/reset/access/description (excess-1 noted where the
hardware uses it). It is the **single source of truth** for:

- the register-map view (names, fields, reset values, layout), and
- command-table **address → field-name** resolution.

The target device for any access is selected by the **Phase Header Device Mask**,
not the address; the 32-bit address is an offset within that peripheral's
identical register space (so one map describes all devices).

Extracted at **v1.1 r08**. `_meta` records the spec revision, the source tables, and the
conventions — read it before trusting a citation, because **table and section numbers are not
stable keys** across spec revisions. The map is keyed on register/field
**name + block + offset + bit range**, and `_meta.conventions` lists
the r06 → r08 field deltas that were applied.

---

## 8. Reuse map

| From | Reused as |
|---|---|
| the C++ decode classes | `native/swi3score/core/`, the `swi3score` core behind `ISampleSource` |
| `SampleRateHz` (native), `store/audio_store.py` (`export_wav` writes the WAVE_FORMAT_EXTENSIBLE integer-PCM header and data) | sample-rate math, audio export |
| Visualizer `models/` (DataPort, FCP, Device, BusModel, Interface, bit_slot) | `swviz/models/`, read by `model/viz_engine` for the authored grid |
| Visualizer CSV/JSON schema (`config/constants.py` CSVFields) | `swviz/io/csv_handler`, `model/bus_config` and the core's config-CSV reader (mid-stream config) |
| Visualizer `drawing/clash_detector`, slot/color semantics, `theme.py` | grid rendering on QGraphics |
| Reference model output (golden JSON) | golden tests (`tests/visualizer/golden_json`) |

The DataPort placement cascade runs in the C++ core (Analysis mode) and in the Python `swviz`
engine (Visualization mode, and the cross-check oracle in tests); see the introduction.

---

## 9. Additional features

> Status: all of the below are **implemented**, as is a dark / light / follow-system theme
> (View ▸ Appearance). See the README for how each is surfaced in the UI.

- Wireshark-style **filter expressions** + bookmarks on the command table.
- **Measurements**: row rate, SSP intervals, derived sample rate, per-DP
  bandwidth / slot utilization.
- A dedicated **error lane**: CRC, disparity, unexpected-token, device-mask
  cardinality violations — flagged on the timeline + table.
- **Config-vs-decoded overlay** and **register diff**.
- **Session/workspace save** (loaded capture source + view state) as a `.swi3s` JSON file.
- **Exports**: WAV (audio), CSV/Arrow (commands), SVG/PNG (grid).
- A **synthetic `.sal` generator** to test the whole pipeline, including large files.
- Dark/light theme.

---

## 10. Build & packaging

- `native/CMakeLists.txt` builds `swi3score` as a pybind11 module via
  **scikit-build-core**, producing a wheel, with no SDK dependency for the core.
  It is the only target this repository builds.
- The app is packaged with PyInstaller (see [`PACKAGING.md`](PACKAGING.md));
  macOS-first, cross-platform CI builds the native module per OS.

---

## 11. Testing / headless

See [`TESTING.md`](TESTING.md) for the layers, coverage matrix, and
conventions. In brief:

- **Headless `pytest`.** Golden tests decode a known `.sal` / synthetic stream and
  assert commands + audio. GUI suites run under the Qt offscreen platform.
- **Placement cross-check**: the C++ grid output is compared against the Python
  placement model across the directed-test configs.
- **Performance**: decode throughput, engine-build time, and digital-CSV import
  ceilings on synthetic captures (`tests/test_perf.py`), including cursor-move, zoom/pan,
  Link-switch and Timing-recompute budgets, and a `.sal` load's peak memory against the
  guard's prediction.

---

## 12. Modes

The **Mode** menu (`ui/mode_controller.py`), first in the menu bar, swaps the central page
and dock set per mode; all three share one workspace file.

- **Visualization** — the authoring editor: `model/bus_config.py` (Interface + 12
  DataPort/FCP, the `_REG` config vocabulary, serialised to v2.0 CSV) and
  `ui/authoring/`. The CDS carries four **per-source** settings (guard, tail width, drive
  type, end-drive-early — 13 entries each: Manager + Device 0–11, since each register lives
  in a device's own CDS block), edited through one *CDS Settings* dialog and written as
  13-wide CSV rows. Placement, clash detection, and validation come from the SWI3S
  Visualizer engine under `swi3s_studio/swviz/`, driven by `model/viz_engine.py`: it
  builds a merged `BusModel`, and `GridView.set_bus_model` renders its bits
  (CDS/S0/S1/guards/tails/handovers + data) and clash markers. An authored config can
  be compared in the Bus Analyzer as the expected config (Decode ▸ Import Visualizer CSV,
  Compare). The engine is covered by a
  JSON parity testsuite (`tests/test_visualizer_engine.py`).
- **Timing** — the PHY margin calculator: `swi3s_studio/timing/` (`calculator.py`,
  `delta_tpd.py`, `spec_source.py`), surfaced by `ui/timing_view.py` as a text margin
  readout. **17 inequalities**, numbered in display order: setup and hold for MP, PM and
  PP, each in both launch forms (`t_DD` and the handover's `t_ZD`); the three Handover
  Contention legs (`MP/PM/PP_contention`); and the bus keeper once per releasing device (one keeper, two legs —
  a min over the pair does not move in one direction per row, which the corner search needs:
  `calculator.py` checklist rule 4), each counting the keeper's response at the Manager from the
  releasing data's crossing of the Manager's V_IH,max / V_IL,min (`calculator.keeper_corners`). Every leg is evaluated
  at **its own** worst PVT corner by `find_worst_corner_rows`, which is why each row can
  read a different end of the same input row. `spec_source.py` holds the per-side spec
  presets — SWI3S PHY1/PHY2, two proposed PHY2 revisions, SoundWire 1.3 — plus the
  normalisation that puts a mixed bus on one timing graph, and the divergences it declines
  to reconcile. Terms are ordered as a physical time trace and each is clickable: the view
  keys a term to the **input row** it reads (`_term_key`) and washes every occurrence.
  `timeline.py` places every leg's events (setup, hold, contention and keeper) on one time axis at its corner, each on
  its ramp with its threshold corner (`delta_tpd.crossing_corners`), and one span per term,
  each naming the ramps it is drawn with. `ui/timing_diagram.py` draws the clicked term from
  it, in each column.
- **Analysis** — the protocol analyzer.

The bus-grid renderer (`ui/grid_view.py`) is shared between the authored (planned) and
decoded views, and its placement is regression-tested against the Visualizer's config
corpus (`tests/visualizer/`, `test_visualizer_placement.py`).

The renderer has two input paths. **Analysis mode** feeds it the C++ core's grid via
`GridView.set_cells`: each data row split into a **Source half (top)** / **Sink half
(bottom)**, multi-emit `GridCell`s (`isSource`), samples merged across bit columns and
labelled per display fields (`C<ch>B<bit>` default; `S<sample>C<channel>` when
Sample+Channel; the Bit field reads `T0` in PortMode 2 (Test0) and `T1` in PortMode 3
(Test1)), TxPresent `TxP<ch>`, DRQ, guards `G0`/`G1`, tail squiggles, scrambler corner squares, full-height CDS, the Source/Sink
key column, and handover arrows. **Visualization mode** feeds it the Visualizer
engine's `BusModel` via `GridView.set_bus_model`: the model supplies the
CDS/S0/S1/guard/tail/handover system bits with device, and the renderer draws those and
overlays clash X-markers (bus red / device yellow / read blue) from the model's clash
lists. Analysis-mode decode keeps the C++ core for throughput.

A wide data bit (BitWidth > 0) occupies the bus on every one of its UIs, whichever side
drives it, and is sampled on its last UI. The analyzer's grid (native `layoutGrid`) draws a
sink port's wide bit on every UI, with the sample marker on the last. The vendored swviz model
draws a sink port's wide bit only on its last UI. That difference between the two engines is
declared, not a defect.

One label does NOT come through the bit model. The CDS cell reads **`CDS` over a flags line**
carrying the two per-source settings the grid can show — `SP`/`SPx` for a drive type of Special
(a CDS bit of 1 left high-Z for the Manager's bus keeper, which NRZS decode sees) and
`EDE`/`EDEx` for end-drive-early, the trailing `x` meaning the sources disagree, as `Gx` already
does for the guard. The flags line is omitted when there is nothing to say, which is every
example config. It moves no cell, so `set_bus_model` takes it as a `cds_symbol` argument, and it
is part of the render **content key**, since nothing else in the key changes when only the
drive type does.

The grid is otherwise a uniform `_CW` per column, and the CDS run is the **one exception**:
`_size_cds_columns` gives it the extra width its label needs, measured from the rendered text,
and `_cx`/`_colw`/`_span_w` make every drawing site width-aware. Only that column widens, so a
32-column view keeps its horizontal room; `_col_extra` is empty on every path that draws no
flags, so those keep the uniform width.

### The CDS across the CSV ↔ register boundary

The CDS crosses in **both** directions:

| direction | path | what carries |
|---|---|---|
| config → registers | `SwI3sConfig::LoadCsv` → `registersFromConfig` | `0x1186` drive type, `0x1187` bit width / end-drive-early / guard enable+polarity / tail, per device |
| registers → config | `CRegisterModel::BuildConfig` → `configToDict` → `BusConfig.from_decoder_config` | the same fields back into the per-source lists, so a decoded capture exports an authoring CSV with its CDS intact |

Each source drives the time-multiplexed CDS under **its own** copy of the registers, so
per-source slot `d+1` is device `d`'s. **The Manager's slot (index 0) has no register and never
will:** only peripherals are addressable, so nothing on the wire says how the Manager drives
the CDS. It round-trips through the CSV and stays at its default on any decoded path — a limit
of the encoding, not an omission, and asserted as such.

A device that never wrote its CDS registers keeps the **config** default (drive type Normal)
rather than the register *reset* (0 = Special): the decode reports only what the capture
programmed.
