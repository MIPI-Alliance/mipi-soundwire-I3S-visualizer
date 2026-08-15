# SWI3S Studio — Architecture

A desktop **MIPI SoundWire I3S (SWI3S)** bus analyzer. It decodes captured PHY2
traffic (clock + bidirectional data), reconstructs the control protocol and audio
payload, and visualizes the 2D bus structure, the per-device register maps, and the
decoded audio, out-of-core over large captures.

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

The target end-state is for the C++ core to drive *both* grids (the placement is
already golden-verified there), leaving `swviz` as the authoring **validator** and
the test oracle only. That migration is deferred — it must also move the authoring
validators and the baked-in S0/S1/CDS/handover system-slot handling — and is tracked
in the maintainers' tech-debt register.

---

## 1. Decisions of record

| Area | Decision |
|---|---|
| GUI / rendering | **PySide6 (Qt 6)** + **pyqtgraph**; `QGraphicsView` for the 2D grid; Qt model/view for tables |
| Decode engine | **Reuse the verified C++ decode** via a **pybind11** module (`swi3score`); the per-UI hot loop stays in C++ |
| Column-count / CDS alignment | **Not signalled on the wire** — recovered by *hypothesize-and-validate* (`CColumnDetector`): score each candidate (even count 2–32 × Column-0 phase) by CRC-valid CDS command phases; best wins. **Commit-independent** — needs only some CRC-valid CDS traffic. |
| Inputs | **Logic 2 `.sal` project**, **per-channel `<SALEAE>` binary** pair, **Saleae digital CSV** (`Time,Clock,Data`), **visualizer CSV/JSON config**. (`.sal` is wired to Open via `ingest/saleae_sal` — pick the clock/data channels; v0 and single-/multi-chunk v3 digital blobs decode, with v0 pre-trigger times rebased so they don't wrap.) |
| Capture scale | **Minutes / multi-GB** — out-of-core: memory-map inputs, streaming decode, on-disk indexed results, render from pyramids |
| Live capture | **Offline first**, architected live-ready (streaming decode, bounded buffers, partial results) |
| Platforms | **macOS-first, fully cross-platform** (Qt + portable pybind11 build); package later (PyInstaller/briefcase) |
| Register map | **Extracted from the SWI3S spec** → structured JSON; single source of truth for the register view *and* address→field-name resolution |
| Results store | **Apache Arrow/Parquet** (commands) + **`np.memmap`** arrays with **min/max pyramids** (audio) + an **event log with checkpoints** (register state) |
| Saleae plugin | **Kept**, as a separate build target sharing the same core lib |

---

## 2. Layered architecture

```
┌────────────┐   ┌─────────────────────────┐   ┌──────────────────┐   ┌────────────────┐
│  ingest    │──►│  swi3score (C++/pybind11)│──►│  results store   │──►│  UI (PySide6)  │
│  (python)  │   │  decode loop (verbatim,  │   │  (python)        │   │  dockable      │
│  .sal      │   │   Saleae deps removed)   │   │  Arrow + memmap  │   │  panels +      │
│  CSV       │   │  register model, SSP,    │   │  pyramids +      │   │  one shared    │
│  config    │   │  descramble, audio       │   │  time index +    │   │  time cursor   │
│            │   │  via ISampleSource       │   │  reg-event log   │   │                │
└────────────┘   └─────────────────────────┘   └──────────────────┘   └────────────────┘
```

Module map (proposed package `swi3s_studio/`):

```
swi3s_studio/
  core/            # thin python wrapper over the swi3score pybind11 module
  ingest/          # SalReader, CsvReader, ConfigReader -> ISampleSource
  store/           # CommandStore (Arrow), AudioStore (memmap+pyramid), RegisterTimeline, TimeIndex
  model/           # Interface, DataPort, FlowControlPort, Device, BusModel (adapted from visualizer)
                   # RegisterMap (driven by data/registers.json), provenance tracking
  analysis/        # link_control, cds_meaning, bus_timing (measured setup/hold eye),
                   # compare, responses (incl. per-device ping), errors
  dsp/             # band-limited polyphase resampler + PDM→PCM decimation (resample.py)
  timing/          # ported SWI3S PHY timing calculator (compute + worst-corner)
  ui/
    main_window.py # dockable layout, menu, session
    grid_view.py   # 2D bus grid (QGraphicsView)
    symbol_view.py # color-coded 8b/10b CDS symbols
    command_table.py
    register_view.py
    audio_view.py  # pyqtgraph waveforms + WAV export + QAudioSink playback
    eye_view.py    # measured setup/hold + eye histograms
    timeline.py    # whole-capture overview ribbon
    cursor.py      # shared TimeCursor + VisibleRange (synchronized navigation)
  export/          # WAV, CSV/Arrow, SVG/PNG
  data/
    registers.json # register map from the SWI3S spec (source of truth)
  app.py           # entry point
native/
  swi3score/       # pybind11 bindings + ISampleSource/Decoder
    core/          # vendored SWI3S decode core (was SwI3sAnalyzer/source) — standalone
  CMakeLists.txt   # builds the python module (scikit-build-core)
tests/
```

---

## 3. Shared decode core

The decode classes are **vendored** in `native/swi3score/core/` (so the repo builds
standalone; see that folder's README for provenance + the file list). They are
already almost SDK-free — they use `LogicPublicTypes.h` only for `U8`/`U16`/`BitState`.
The *single* coupling to Saleae is `CBitstreamDecoder`, which pulls bits from
`AnalyzerChannelData` (not vendored; replaced by `ISampleSource`).

**Refactor (done):** `CBitstreamDecoder` was rewritten against an abstract sample
source, `ISampleSource` (`native/swi3score/ISampleSource.h`). The interface has
grown, backward-compatibly, past the original two methods as real features landed —
its current surface is:

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
    // Absolute index of the next UI (UIs consumed) — stamps segment boundaries so
    // windowed re-decodes align row numbering.
    virtual uint64_t UiIndex() const { return 0; }
    // Total UIs if known up front, else 0 — decode PROGRESS only, never correctness.
    virtual uint64_t TotalUiCount() const { return 0; }
    virtual ~ISampleSource() = default;
};
```

The three seekable/columns/index methods are all defaulted, so a pure streaming
reader still satisfies the interface with only the two core methods.

`CBitstreamDecoder` runs against `ISampleSource` (it already exposed the exact
edge/level/rewind semantics the decoder needs). **Everything downstream is reused
verbatim**: `CNrzsDecoder`, `C8b10bDecoder`, `CColumnDetector`,
`CCommandTransportParser`, `CRegisterModel`, `CDataPort`, `CFlowControlPort`,
`CPayloadEngine`, `CDescrambler`, `CCrc16`, `SwI3sProtocolDefs`.

The core is built **twice** from one source tree:
- **`swi3score`** — pybind11 python module for Studio (no Saleae SDK).
- **`SwI3sAnalyzer`** — the Saleae plugin (adds the `AnalyzerChannelData` sample
  source + FrameV2 emission). Unchanged behaviour.

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
same detection (resync watchdog), producing one segment per width.

### pybind11 surface

The per-UI loop must **not** cross the Python boundary. The core runs the whole
decode and emits results in **batches** (or writes the results store directly
from C++ via Arrow C-data / memmap buffers). Sketch:

```python
import swi3score
dec = swi3score.Decoder(source, settings)     # source = ingest.ISampleSource impl
dec.on_commands(callback_or_arrow_sink)        # batched
dec.on_register_events(sink)
dec.on_audio(sink)                             # per-(dp,channel) arrays
dec.run()                                      # streaming, single pass
```

Settings mirror the plugin: column-count (auto/forced), config CSV (mid-stream),
decode-audio, PHY mode (PHY1/2 now, PHY3 stub).

### Performance & language choice

**One scalar loop in C++; everything else Python.** The C++/Python boundary is drawn
at exactly one place — the **per-UI decode loop** — and that placement *is* the
performance story. The loop runs once per unit interval (tens to hundreds of millions
of times on a multi-GB capture) doing branchy, stateful, bit-level work: data-line
sampling, NRZS, 8b/10b + CRC-16, the command-transport parser, the register model, the
`CDataPort` placement cascade, the LFSR descrambler, and audio reconstruction. Measured,
the C++ core decodes **~35M UIs/s** (≈1.9 s for a 65M-UI / 31M-audio-sample capture), and
it **releases the GIL**, so decode runs on a worker thread with a responsive UI.

Everything *around* the loop is already Python and stays there — none of it is on the
hot path: ingest (edge arrays), the results store (NumPy `lexsort`/grouping), all
analysis (`bus_timing` etc., already NumPy-vectorized), PDM decimation (FFT), and the UI.

**Why not all-Python.** A pure-CPython port of the per-UI loop is bytecode- and
method-call-bound (~2–10 µs/UI vs ~30 ns in C++) → roughly **50–200× slower**. That turns
a ~2 s decode into minutes, and a "minutes of audio" multi-GB capture into many minutes to
hours — which breaks the core design point (interactive over multi-GB) — and, holding the
GIL, it would freeze the UI unless carefully chunked. NumPy only partly helps: the
*stateless, streaming* stages (level sampling, NRZS, 8b/10b table-lookups over the whole
symbol array) vectorize well, but the parts that dominate are inherently **sequential and
stateful** — the self-syncing descrambler, CDS framing / comma re-sync, dual-ranked
register commit + SSP anchoring, the placement cascade — and don't vectorize. A heavy
NumPy rewrite might reach ~10–30× slower, at the cost of a large rewrite *and* losing the
single C++ core shared with the Saleae Logic 2 plugin. So the boundary sits where it does
deliberately: **C++ for the one scalar state machine NumPy can't touch, Python for
everything that's either vectorizable or off the hot path.**

---

## 4. Memory architecture (multi-GB)

The design principle: **no view cost scales with capture size.**

1. **Inputs are edge-based, not per-sample.** A `.sal` stores per-channel
   *transition* lists; the decoder consumes clock edges + data level, which *is*
   the transition data. We memory-map the transition arrays and stream them — a
   full raw-sample buffer never exists. (CSV is parsed streaming into the same
   edge form.)

2. **Decode is single-pass and streaming**, writing typed event streams to an
   **out-of-core results store**:

   > **Status (3.0.8) — partially realized; the rest is the design target.** What
   > ships today: the single-pass streaming decode; the CDS-symbol and data-bit
   > windowed re-decode (rows 4–5 below, fully realized — see §6/§7); and audio
   > min/max pyramids for the timeline. What is *scaffolded but not on the hot
   > path*: `store/audio_store.py` has a working `np.memmap` backend, but every
   > production caller uses the in-RAM branch (`mmap_dir` is only passed in tests),
   > because the decoder currently hands its results to `Session` as in-memory
   > Python/NumPy structures rather than to a persistent store. `store/command_store.py`
   > (Arrow/Parquet) is a display/export cache, not fed back into replay. The
   > `TimeIndex` / `RegisterTimeline` / register event-log-with-checkpoints named
   > below are **not yet built** — register-at-cursor is replayed from the in-memory
   > command list. This is the right shape for the demo-/typical-scale captures the
   > app is exercised on; wiring the persistent store is deferred until a concrete
   > multi-GB workload needs it (tracked in the maintainers' tech-debt register).

   | Stream | Volume | Storage |
   |---|---|---|
   | Commands (phases) | sparse (thousands–millions) | **Arrow/Parquet**, memory-mapped, filterable |
   | Audio samples | large | per-`(dp,channel)` **`np.memmap`** + **min/max pyramid** |
   | Register events (writes/commits) | sparse | event log + **periodic state checkpoints** |
   | CDS symbols | enormous | **not persisted** — re-decoded per visible window on demand, cached |
   | Data-bit sample points | enormous | **not persisted** — re-decoded per visible window from **transport-phase checkpoints** (see §6) |

3. **Time index.** A `sample → file offset` index per stream gives O(log n) range
   queries, so every panel pulls only what's on screen.

4. **Waveform pyramids (mip-maps).** For audio, precompute min/max per bin at
   several zoom levels; pan/zoom renders from pre-binned data and never touches
   the full array (the technique pro waveform viewers use to stay smooth).

5. **Register state at time T.** Reconstructed by replaying register events from
   the nearest checkpoint up to T — powering the "register map at the cursor"
   view and dual-rank staged/committed display in O(checkpoint interval).

6. **Symbol viewer.** Persisting every 8b/10b symbol is infeasible at multi-GB.
   Instead, the core can **seek the sample source and re-decode a window** on
   demand (cheap, edge-based); recently viewed windows are cached.

7. **Port sample points** (the Raw Capture overlay: where each data bit is latched).
   Also infeasible to persist whole — but a bit's *sample position* is a pure function
   of the transport **schedule** (config + SSP phase), independent of the descrambler.
   So the streaming decode records a light **transport event log** (each payload
   (re)Configure / SSP re-anchor) plus **periodic transport-phase checkpoints**; a
   windowed request (`Decoder::bitSamplesInWindow`) restores the nearest checkpoint,
   fast-forwards the schedule to the window (bounded by the checkpoint interval), then
   ticks the window reading the wire to stamp each sampled bit. No re-decode of the
   whole capture, no descramble — enabling the overlay and navigating are cheap and
   every region resolves. (A `collect_bit_samples` whole-capture path still exists as
   the test oracle the windowed path is checked bit-for-bit against.)

---

## 5. UI

Dockable panels (IDE / Saleae style) bound by **one shared time cursor +
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
  Field tooltips decode bit ranges. (The earlier in-view "what-if" register editing
  was dropped; the register map is now read-only — the Visualization mode is where
  configs are authored.)
- **Audio viewer** (pyqtgraph): multi-track waveforms per `(dp,channel)`, smooth
  zoom/scroll from the pyramid; region-select → **WAV export**; in-app **playback**
  via `QAudioSink`. Sample rate derived from RowRate + DP params (existing
  `SampleRateHz`). A **PDM** data port (1-bit `sample_size`) is a bipolar *density*
  code, not a 1-bit two's-complement sample, so it is decoded to PCM at store-build
  time: `{0,1}→±1`, band-limited polyphase decimation to ~48 kHz (`dsp/resample.py`),
  then DC-blocked (mic density bias) and scaled — see `store.audio_store.decode_pdm`.
- **Eye Diagram** (pyqtgraph): measured **setup/hold** timing straight off the
  capture's clock/data edges (`analysis/bus_timing.py`) — per-polarity setup/hold
  histograms + a data-edge "eye", gated on real data transitions, with a margin
  verdict. Aggregate over the capture (no time cursor); rendered lazily on first show.
- **Statistics** panel: derived measurements (row/clock rate, per-DP SSP interval &
  bandwidth, bus-config segments, link `PM_Action` events).
- **Timeline overview ribbon**: whole-capture minimap with markers for commits,
  SSPs, PHY/link events, and errors; click to seek.

---

## 6. Link bring-up, PHY, and modes

- **From t=0**: decode link bring-up & timing (PHY selection, calibration,
  Announce/SSPA, ExitDormant) and learn the full configuration by snoop. The core
  already parses Announce/Commit/CalibratePhy phases; a small **link/PHY state
  machine** tracks PHY selection + timings on top.
- **Mid-stream**: column auto-detect + per-DP config from an imported visualizer
  CSV (already supported by the core).
- **PHY1/PHY2 selectable now; PHY3 stubbed** (DLV / S0-S1 / recovered clock,
  multi-lane) — the `ISampleSource` + PHY state machine leave room for it.

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
stable keys**: r08 moved the register content from Chapter 15 to Chapter 14 and renumbered
tables 163–171 → 165–182 *without moving a single register*. The map is keyed on register/field
**name + block + offset + bit range**, which have been stable, and `_meta.conventions` lists
the r06 → r08 field deltas that were applied.

---

## 8. Reuse map

| From | Reused as |
|---|---|
| C++ analyzer decode (all of `source/*` except the Saleae glue) | the `swi3score` core, verbatim behind `ISampleSource` |
| `CWavWriter`, `SampleRateHz` | audio export + sample-rate math |
| Visualizer `models/` (DataPort, FCP, Device, BusModel, Interface, bit_slot) | `model/` for grid + register state |
| Visualizer CSV/JSON schema (`config/constants.py` CSVFields) | `ingest` config reader (mid-stream config) |
| Visualizer `drawing/clash_detector`, slot/color semantics, `theme.py` | grid rendering on QGraphics |
| Plugin test vectors / `model_dump.py` cross-checks | golden tests |

The DataPort placement algorithm has **one** implementation (the C++ core); the
visualizer's Python version remains the cross-check oracle in tests.

---

## 9. Additional features (from other bus tools)

> Status: all of the below are **implemented** except the dark/light theme toggle
> (the app is dark-themed). See the README for how each is surfaced in the UI.

- Wireshark-style **filter expressions** + bookmarks on the command table.
- **Measurements**: row rate, SSP intervals, derived sample rate, per-DP
  bandwidth / slot utilization.
- A dedicated **error lane**: CRC, disparity, unexpected-token, device-mask
  cardinality violations — flagged on the timeline + table.
- **Config-vs-decoded overlay** and **capture diff**.
- **Session/workspace save** (loaded capture source + view state) as JSON.
- **Exports**: WAV (audio), CSV/Arrow (commands), SVG/PNG (grid).
- A **synthetic `.sal` generator** (extending the plugin's simulation work) to
  test the whole pipeline, including large files.
- Dark/light theme.

---

## 10. Build & packaging

- `native/CMakeLists.txt` builds `swi3score` as a pybind11 module via
  **scikit-build-core**, producing a wheel. Reuses the existing CMake patterns but
  drops the Saleae SDK dependency for the core. The Saleae plugin stays a separate
  CMake target over the same sources.
- App packaged later with PyInstaller / briefcase; macOS-first, cross-platform CI
  builds the native module per OS.

---

## 11. Testing / headless

See [`TESTING.md`](TESTING.md) for the layers, coverage matrix, and
conventions. In brief:

- **Headless `pytest`.** Golden tests decode a known `.sal` / synthetic stream and
  assert commands + audio. GUI suites run under the Qt offscreen platform.
- **Placement cross-check**: the C++ grid output is compared against the Python
  placement model across the directed-test configs.
- **Performance**: decode throughput, engine-build time, and digital-CSV import
  ceilings on synthetic captures (`tests/test_perf.py`). Pan/zoom latency and a
  memory ceiling are design targets, not yet asserted by a test.

---

## 12. Modes

A top **mode switcher** (`ui/mode_controller.py`) swaps the central page and dock set
per mode; all three share one workspace file.

- **Visualization** — the authoring editor: `model/bus_config.py` (Interface + 12
  DataPort/FCP, the `_REG` config vocabulary, serialised to v2.0 CSV) and
  `ui/authoring/`. The CDS carries four **per-source** settings (guard, tail width, drive
  type, end-drive-early — 13 entries each: Manager + Device 0–11, since each register lives
  in a device's own CDS block), edited through one *CDS Settings* dialog and written as
  13-wide CSV rows. Placement, clash detection, and validation come from the SWI3S
  Visualizer engine under `swi3s_studio/swviz/`, driven by `model/viz_engine.py`: it
  builds a merged `BusModel`, and `GridView.set_bus_model` renders its bits
  (CDS/S0/S1/guards/tails/handovers + data) and clash markers. An authored config can
  be pushed into Analysis ▸ Compare as the expected config. The engine is covered by a
  JSON parity testsuite (`tests/test_visualizer_engine.py`).
- **Timing** — the PHY margin calculator: `swi3s_studio/timing/` (`calculator.py`,
  `delta_tpd.py`, `spec_source.py`), surfaced by `ui/timing_view.py` as a text margin
  readout. **17 inequalities**, numbered in display order: setup and hold for MP, PM and
  PP, each in both launch forms (`t_DD` and the handover's `t_ZD`); the three handover
  non-contention legs; and the bus keeper once per releasing device (one keeper, two legs —
  a min over the pair is not affine and defeated the corner search). Every leg is evaluated
  at **its own** worst PVT corner by `find_worst_corner_rows`, which is why each row can
  read a different end of the same input row. `spec_source.py` holds the per-side spec
  presets — SWI3S PHY1/PHY2, two proposed PHY2 revisions, SoundWire 1.3 — plus the
  normalisation that puts a mixed bus on one timing graph, and the divergences it declines
  to reconcile. Terms are ordered as a physical time trace and each is clickable: the view
  keys a term to the **input row** it reads (`_term_key`) and washes every occurrence.
  No plots.
- **Analysis** — the protocol analyzer.

The bus-grid renderer (`ui/grid_view.py`) is shared between the authored (planned) and
decoded views, and its placement is regression-tested against the Visualizer's config
corpus (`tests/visualizer/`, `test_visualizer_placement.py`).

The renderer has two input paths. **Analysis mode** feeds it the C++ core's grid via
`GridView.set_cells`: each data row split into a **Source half (top)** / **Sink half
(bottom)**, multi-emit `GridCell`s (`isSource`), samples merged across bit columns and
labelled per display fields (`C<ch>B<bit>` default; `S<sample>C<channel>` when
Sample+Channel; `T1`/`T0` in PortMode test modes), TxPresent `TxP<ch>`, DRQ, guards
`G0`/`G1`, tail squiggles, scrambler corner squares, full-height CDS, the Source/Sink
key column, and handover arrows. **Visualization mode** feeds it the Visualizer
engine's `BusModel` via `GridView.set_bus_model`: the model supplies the
CDS/S0/S1/guard/tail/handover system bits with device, and the renderer draws those and
overlays clash X-markers (bus red / device yellow / read blue) from the model's clash
lists. Analysis-mode decode keeps the C++ core for throughput.

One label does NOT come through the bit model. The CDS cell reads **`CDS` over a flags line**
carrying the two per-source settings the grid can show — `SP`/`SPx` for a drive type of Special
(a CDS bit of 1 left high-Z for the Manager's bus keeper, which NRZS decode sees) and
`EDE`/`EDEx` for end-drive-early, the trailing `x` meaning the sources disagree, as `Gx` already
does for the guard. The flags line is omitted when there is nothing to say, which is every
example config. It moves no cell, so `set_bus_model` takes it as a `cds_symbol` argument, and it
is part of the render **content key** for that reason: nothing else in the key changes when only
the drive type does, so without it the redraw would be skipped as unchanged.

The grid is otherwise a uniform `_CW` per column, and the CDS run is the **one exception**:
`_size_cds_columns` gives it the extra width its label needs, measured from the rendered text,
and `_cx`/`_colw`/`_span_w` make every drawing site width-aware. Widening only that column keeps
the horizontal room a 32-column view needs; `_col_extra` is empty on every path that draws no
flags, so those render byte-identically to before.

### The CDS across the CSV ↔ register boundary

The CDS crosses in **both** directions (`score_abi` 10):

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
rather than the register *reset* (0 = Special). Those differ deliberately, and the choice here
is to report only what the capture actually programmed; reading the reset instead would relabel
every capture that ignores the CDS as passively driven.
