# SWI3S Studio — Testing Approach

> Status: living document. Update it whenever the test strategy changes — it is the
> reference for **auditing** test coverage as the app grows. Last reviewed against
> 65 Python suites + the native C++ suite.

## 1. Why this document exists

SWI3S Studio decodes a non-trivial wire protocol (NRZS → 8b/10b → command transport
→ register snoop → SSP-anchored audio placement → descramble) and renders it through
a stateful Qt UI bound by one shared cursor. Bugs are easy to introduce and hard to
eyeball: an off-by-one in row framing, a shifted register bit-field, or a mis-mapped
sample→UI index all *look* plausible on screen. The test suite is the thing that
keeps the decode honest and the UI wired up correctly. This document describes how we
test, so that coverage can be **audited** rather than assumed.

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
> A stale `.so` is the #1 cause of "the test passed but the fix isn't there" / "the
> test fails but my code is right" confusion.

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
Location: `swi3s-studio/tests/`, 65 files, one concern per file. Each is a plain script
with `test_*` functions and a `__main__` block that runs them, prints `ok: …` lines per
check, and ends with `ALL … TESTS PASSED`. They are also `pytest`-discoverable.

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
  `.sal` (if present) is handled (single-chunk v3 decodes; multi-chunk raises a clear
  error) rather than producing garbage.
- `test_register_model.py` — spec load, address → register/field resolution, provenance.
- `test_responses.py` — response token → name by phase; labelled `response_summary`,
  including the per-device **Ping** breakdown collapsed into device ranges
  (`0: PING_ATTACHED, 1-11: NO_RESPONSE`).
- `test_link_control.py` — §5.1.2 link bring-up detection from the raw edges
  (cold/warm start, the PHY-number clock burst, PhyStart), self-orienting when the
  bring-up rides the data line.
- `test_bus_timing.py` — measured setup/hold/eye (`analysis/bus_timing`) on synthetic
  captures of known geometry: the margins match the offset, gating drops the
  no-transition (1-UI) pileup, and a tight margin flags as such.
- `test_pdm.py` — PDM decode (`dsp.pdm_to_pcm` + `store.decode_pdm`): a sigma-delta
  tone recovers the right pitch, the 1-bit code is **bipolar** (`{0,1}→±1`, not 2's
  complement), and a DC-biased mic stream is DC-blocked yet keeps its tone.
- `test_measurements.py` — derived metrics (columns, SSP count, per-DP sample rate &
  bandwidth).
- `test_analysis_filter.py` — error classification + command filter proxy.
- `test_compare.py` — grid-diff classes; `grid_diff_report` summary/detail; the
  **`registers_from_csv` round-trip** (encode a CSV config to register writes, feed through
  `grid_from_registers`, assert the grid equals `grid_from_csv`); `register_diff`; old
  visualizer-CSV (v1.73) → v2.0 conversion + placement equivalence.
- `test_audio_export.py` — `AudioStore` → per-(device,DP) WAV → read back → compare to the
  generated sine.
- `test_audio_pyramid.py` — render-pyramid envelope equals raw samples when zoomed in;
  memmap-backed channel.
- `test_resample.py` — arbitrary-rate band-limited resampler (`dsp/resample`): identity,
  predicted output length, in-band tone preserved, out-of-band rejected, up/down-sample,
  unity DC gain, PDM→PCM decimation.
- `test_audio_source.py` — the playback `QIODevice` (`_PcmSource`): never short-reads
  (PCM then silence), the software fade-out ramp, and frame/byte math.
- `test_export.py` — command CSV (headless) + bus-grid SVG/PNG (offscreen Qt).
- `test_workspace.py` — workspace JSON round-trip + full GUI save → open restores
  bookmarks and cursor. (The legacy what-if `overlay` field is still parsed for
  backward compatibility but no longer applied.)

**Headless GUI**
- `test_gui_smoke.py` — constructs the real `MainWindow` under the Qt **offscreen**
  platform, loads the demo, and exercises: every pane populates; the shared cursor drives
  the register view; two-way command ↔ timeline ↔ CDS-symbol ↔ audio sync (a command's
  start_sample equals its SPM-comma Row Sync Point); the **Filter menu** (multi-select
  kinds, boolean and/or text, errors-only) and the title's filter indicator;
  bookmarks; **64-bit sample signals** (>2³¹); the Compare expected-config (CSV-Import)
  overlay; the audio cursor line + click-to-seek mapping.
- `test_phy_bringup_gui.py` — a capture with a §5.1.2 bring-up loads into Analysis with
  the timeline bring-up band + PHY label; a plain demo (no bring-up) stays in
  Visualization with the grid gated at t=0.
- `test_timeline.py` — the timeline ribbon's tick paint order (significant marks win a
  colliding pixel over Pings) and distinct per-kind colours.
- `test_modes_gui.py` — the **three-mode shell**: the top switcher swaps the central page
  and the visible dock set per mode (Visualization | Timing | Analysis); Analysis-only menus
  gate by mode; returning to a mode restores its layout; the active mode round-trips through
  the workspace. Also: Visualization authoring places the grid + flags a seeded bus clash,
  "Compare with Visualizer" feeds Analysis ▸ Compare, and the workspace persists the authored
  config + timing inputs.

**Visualization-mode authoring + Timing-mode (mostly Qt-free)**
- `test_authoring.py` — `BusConfig` ↔ the v2.0 CSV (the engine + C++ core read it) + dict
  round-trips; an authored config is placed by the `grid_from_csv` cascade (and yields register
  writes), including wide-bit held-column identity; every data port gets a name in the CSV.
- `test_timing.py` — the ported SWI3S timing `compute()` matches the source `swtiming`
  exactly (the source validated it against two reference spreadsheets); `find_worst_corner`
  never improves a margin; `TimingView` renders the four inequalities + binding summary and
  round-trips its inputs.
- `test_visualizer_engine.py` — **the authoritative Bus-Visualizer parity test.** Bus-Visualizer
  mode is driven by the *first-party Visualizer engine* (`swi3s_studio/swviz/`, via
  `model/viz_engine.py`). For all **89** example configs this builds the merged `BusModel`
  through that engine and asserts the serialized model equals the Visualizer's golden JSON
  (`tests/visualizer/golden_json/`) — **bits + bus_clashes + device_clashes + read_overlaps +
  warnings**. This is the Visualizer's own testsuite, ported, and is the source of truth for
  placement/clash/validation parity (it replaced the earlier hand-port `analysis/clash`/`validate`,
  now removed).
- `test_visualizer_placement.py` — a fast first-line placement check: for all 89 configs it
  solo-places each data port through `grid_from_csv` and asserts `(dp,row,col,slot)` matches the
  Visualizer's per-DP golden (excluding the reserved CDS column).
- `test_grid_cross_engine.py` — asserts the two LIVE placement engines agree DIRECTLY:
  each solo data port placed through both the C++ core and the swviz authoring engine
  yields the same DP-owned cells. Runs the demo AND the **whole example corpus** (all
  flow modes) on DATA + TX_PRESENT — the sweep that catches a placement rule landing in
  one engine but not the other (e.g. the {ASW5203} TX_PRESENT-in-RX_CONTROLLED fix). Plus
  a per-FlowMode spec check that TX_PRESENT is placed iff flow-controlled, in both engines
  (catches a bug identical in both, which equivalence can't). (The two are otherwise
  pinned only transitively via the shared goldens above.)
- `test_visualizer_roundtrip.py` — whole-corpus CSV round-trip: every example config,
  loaded → re-saved via `CSVHandler` → reloaded, must build a byte-identical model. Restores
  the upstream suite's round-trip coverage (Studio previously round-tripped only a couple of
  targeted cases in `test_config_csv`).
- `test_authoring_render_golden.py` — characterization golden over all 94 example configs:
  pins `viz_engine.render_payload`'s issue list + clash cells + grid dims (the part the
  placement/model goldens don't cover), so a future placement-engine change can't silently
  shift the authoring warnings.
- **`tools/viz_report.py`** — a dedicated one-command visualizer runner + report (not a
  CI gate; the pytest suites above are). Runs the regression + CSV round-trip over every
  example config and writes `tests/visualizer/summary.md`: aggregate stats (bit positions,
  slots, handovers, clashes), slot-type/direction distribution, per-config metrics, and a
  **delta column vs the previous run** (tracked in `previous_stats.json`). Both outputs are
  gitignored — regenerate on demand. This restores (and extends) the upstream
  `test/testsuite.py` summary; `--check` makes it exit non-zero on any failure.
- `test_grid_slots.py` — pins the shared `GridSlot` enum (model/grid_slots.py) to the C++
  `SLOT_NAMES` ordinals, so a C++ slot-enum reorder fails a test instead of mis-colouring
  the grid.

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
  (Python) generate a *known* PHY2 stream — config writes, a commit, and a scrambled
  stereo sine across 2 devices × 2 DPs at 16 columns. Tests assert exact decoded values
  (the reconstructed waveform is a sine of known amplitude/frequency, so audio
  correctness is checkable, not eyeballed). This is the central fixture; most suites build
  on it.
- **Spec vectors.** The descrambler and 8b/10b tests use values published in the spec, so
  they are not self-referential.
- **External cross-check.** The C++ grid placement is compared against the companion
  visualizer's reference model across its directed-test configs.

## 5. Fixtures & test data

- **Synthetic demo** — the workhorse; deterministic, in-repo, no external files. Always
  prefer it for new tests.
- **Real hardware captures** — large Logic 2 exports kept in `~/Downloads` (e.g.
  `ColdStart_Lusk_768k_2col_768k_8col_pb_rec.{sal,csv}` + `_digital_{0,1}.bin`,
  `ColdStart_768kHz_2col_Ping0x3.*`, `768k_8col_pb_rec.csv`). These exercise the messy
  realities the demo can't: multi-GB size, negative pre-trigger origin, 2-col→8-col
  reconfiguration, cold-start-preloaded config, real audio. **Tests reference them only
  when present** (guarded by existence checks) so the suite stays green on a clean
  checkout. They are used heavily for *manual* validation during development; promoting a
  finding from a real capture into a deterministic synthetic regression test is preferred
  whenever feasible.
- **Visualizer repos** — `../mipi-soundwire-I3S-visualizer` (v2.0) and `…-1.74` (old) for
  the placement cross-check and CSV-format conversion tests.

## 6. How to run

The quickest path is the bundled runner, which runs every Python suite in its own
pytest process and prints a per-suite pass/fail summary with test counts:

```bash
cd swi3s-studio
bash tests/run_all.sh                  # all Python suites (excludes perf, like CI)
bash tests/run_all.sh --build          # rebuild the core first (after a C++ edit)
bash tests/run_all.sh --perf           # the perf benchmarks instead
bash tests/run_all.sh --native         # also run the native C++ suite
bash tests/run_all.sh --build --native --verbose
```

**The runner covers exactly what CI is configured to run.** `run_all.sh` delegates to
`python -m pytest` per file with the same `-m "not perf"` marker `ci.yml` declares (CI runs
on the public repo only, and not the perf gate or Windows — see §9); the only
difference is process granularity (one pytest process per suite, so you get per-suite
results and teardown crashes can't be masked by whichever suites shared a process).
`tests/test_collection.py` pins that equivalence, and fails the build if any test file
collects zero tests.

> **History.** Until 3.0.10 the runner executed each file as a *script*
> (`python tests/test_x.py`), so it ran only what that file's `__main__` block happened
> to call. Sixty tests were invisible to it, and six files with no `__main__` block ran
> nothing, exited 0, and were reported `ok`. If you see a `__main__` block in a suite,
> it is a vestige of that runner — pytest is the only supported entry point.

To run things by hand instead:

```bash
# 0. (after any C++ edit) rebuild the core — see §2.
cd swi3s-studio/native && PYBIND11_INCLUDE="$(python3 -c 'import pybind11; print(pybind11.get_include())')" bash build_local.sh

# 1. Python suites — from the repo root, with the package on the path.
#    GUI/offscreen suites also need the Qt offscreen platform.
cd swi3s-studio
for t in tests/test_*.py; do
  PYTHONPATH="$PWD" QT_QPA_PLATFORM=offscreen python3 "$t" || echo "FAILED: $t"
done

# 2. A single suite:
PYTHONPATH="$PWD" QT_QPA_PLATFORM=offscreen python3 tests/test_gui_smoke.py

# 3. Native C++ suite:
bash ../protocol-analyzer/SwI3sAnalyzer/test/run_tests.sh
```

Notes / gotchas:
- `PYTHONPATH=.` is required so `import swi3s_studio` resolves; the `swi3score` `.so`
  sits at the package root and imports once it's on the path.
- `QT_QPA_PLATFORM=offscreen` is required for any suite that touches Qt
  (`test_gui_smoke`, `test_export`, `test_workspace`, `test_analysis_filter`); harmless
  for the others.
- Each suite exits non-zero on failure and prints `ALL … PASSED` on success — that exit
  code is the reliable pass/fail signal. **Do not** grep stdout for words like "error"
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
| Capture ↔ source equivalence | `ingest/capture`, `TransitionSampleSource` | `test_capture_pipeline` | app + binding |
| Audio store / pyramid / WAV | `store/audio_store`, `export` | `test_audio_pyramid`, `test_audio_export` | app |
| Arbitrary-rate resampler / PDM | `dsp/resample` | `test_resample`, `test_audio_export` (export-at-rate) | app |
| Command store (Arrow) | `store/command_store` | `test_capture_pipeline` | app |
| Register model + provenance | `model/registers` | `test_register_model` | app |
| Register enums / PHY+CDS+link blocks | `model/registers`, `data/registers.json` | `test_register_model` (enum decode, PHY/CDS resolve) | app |
| Link-control events (PM_Action) | `analysis/link_events` | `test_register_model` (link_events) | app |
| Responses / measurements / errors / filter | `analysis/*` | `test_responses`, `test_measurements`, `test_analysis_filter` | app |
| Config compare (grid + report + overlay) | `analysis/compare`, `ui/register_view` | `test_compare`, `test_gui_smoke` | app + GUI |
| Visualizer CSV v1→v2 conversion | `ingest/visualizer_csv` | `test_compare` | app |
| Visualizer placement parity (89 configs) | `Decoder` `layoutGrid`, `grid_from_csv` | `test_visualizer_placement` | binding (vs Visualizer goldens) |
| CDS symbol meaning | `analysis/cds_meaning` | `test_cds_meaning` | app |
| Exports (CSV / SVG / PNG) | `export/*`, `ui/grid_view` | `test_export` | app + GUI |
| Bus grid render (per-device/DP colour, legend) | `ui/grid_view`, `Decoder` (GridCell.device) | `test_gui_smoke` (stream colours) | GUI |
| Workspace save/load | `workspace`, `ui/main_window` | `test_workspace` | app + GUI |
| Window assembly, all panes populate | `ui/*` | `test_gui_smoke` | GUI |
| Cross-panel cursor sync | `ui/main_window`, cursor/timeline/symbol/audio | `test_gui_smoke` | GUI |
| Timeline config bands (per-segment colour) | `ui/timeline`, `decoder.segments()` | `test_gui_smoke` (segments/bands) | GUI |
| Command filtering (Filter menu, boolean text, title indicator) | `ui/command_table`, `ui/main_window` | `test_analysis_filter`, `test_gui_smoke` | app + GUI |
| 64-bit (>2³¹) sample signals | `ui/cursor`, `ui/timeline` | `test_gui_smoke` | GUI |
| Three-mode shell (switcher, per-mode docks) | `ui/mode_controller`, `ui/main_window` | `test_modes_gui` | GUI |
| Authored config model + CSV/dict serialise | `model/bus_config` | `test_authoring` | app |
| Authored config placement (via core) | `model/bus_config`, `grid_from_csv` | `test_authoring` | app + binding |
| Bus-Visualizer parity (bits+clashes+warnings, 89 configs) | `swviz`, `model/viz_engine` | `test_visualizer_engine` | engine vs golden JSON |
| Placement parity (89 configs, fast check) | `Decoder` `layoutGrid`, `grid_from_csv` | `test_visualizer_placement` | binding vs golden |
| Bus model → JSON export | `model/viz_engine.model_json` | `test_visualizer_engine` | app |
| Authoring panel + Use-as-Expected | `ui/authoring`, `ui/main_window` | `test_modes_gui` | GUI |
| SWI3S timing margins (compute, F_max, corner) | `timing/calculator`, `timing/delta_tpd` | `test_timing` (parity to source) | app |
| Timing view (margins text + round-trip) | `ui/timing_view` | `test_timing` | GUI |
| Workspace (mode + authoring + timing) | `workspace`, `ui/main_window` | `test_modes_gui`, `test_workspace` | app + GUI |

## 8. Conventions for new tests

- **One concern per file**, named `tests/test_<concern>.py`. Functions `test_*`. End the
  `__main__` block with a single `ALL … PASSED` print and let exceptions fail loudly.
- **Build on the synthetic demo** unless you specifically need a real capture; if you do,
  guard on file existence and `print("(skipped …)")` so a clean checkout stays green.
- **Add a regression test with every bug fix.** This is not optional — it's how the suite
  grew its sharpest checks (e.g. `test_symbol_rows_match_command_rows` after the
  row-numbering bugs; the `registers_from_csv` round-trip after the encoder was written;
  the audio-cursor-line check after the sync work). Name the behaviour, not the bug.
- **Prefer an inverse/equivalence assertion** (§4) over a hand-computed expected value
  when one is available — it's harder to fool and survives refactors.
- **C++ vs Python:** put protocol-correctness checks in the C++ suite (spec vectors), and
  re-assert the *observable result* from Python through the binding. Don't re-implement
  protocol logic in Python just to test it.
- **UI tests reference table columns by index** (e.g. command-table column 2 = Command).
  This is brittle: adding/removing a column shifts every later index and silently breaks
  unrelated assertions. When you change a table's columns, grep the GUI test for the old
  indices and fix them in the same commit. (Consider migrating hot assertions to
  column-by-header lookups if this keeps biting.)
- **Watch for state bleed in `test_gui_smoke`:** it's one long script over a single
  window; if your new check moves the cursor or mutates a view, restore the baseline
  (e.g. `win.cursor.set_sample(0)`) before the next section, or you'll break a later
  assertion that assumed the prior state.
- **Don't shadow module-level imports** inside a test function (a local `from … import X`
  makes `X` local for the *whole* function and `UnboundLocalError`s earlier uses).

## 9. Known gaps & risks (the audit's standing TODO list)

These are deliberately listed so an audit starts from an honest baseline:

- **CI covers less than the release gate, and vice versa.** `.github/workflows/ci.yml`
  (added 3.0.7) runs on this repository: ubuntu 3.11/3.12/3.13 + windows 3.12/3.14 + macOS,
  a `-m perf` gate, coverage, and a lint/type job that became **blocking** in 3.0.12.
  *Risk: neither net is a superset of the other.* CI does not run the process-isolated
  per-suite pass and does not assert a stale native ABI; the hand-run gate covers neither
  Linux nor Python 3.12/3.13. Both are required at a release — see DEVELOPMENT.md ▸ Release
  checklist step 2, and `tools/gate.py`, which is the one definition of the gate.
  *History, because it cost five releases of debt: the workflow existed from 3.0.7 but did
  not execute until 3.0.12, and the hand-run gate never included lint or types — so 445 ruff
  findings and 448 mypy errors accumulated unseen and surfaced on the first published PR.
  This entry previously claimed CI was resolved in 3.0.7, which was true of the file and of
  nothing running.*
- **~~Performance is untested.~~** *(largely resolved in 3.0.8; the perf job now runs in CI
  and in the release gate.)* `tests/test_perf.py` holds wall-clock cliff-detectors on a large
  synthetic capture, defined as their own CI
  gate (`-m perf`) and locally via `run_all.sh --perf`; see docs/PERFORMANCE.md.
  *Residual gap: pan/zoom interaction latency and memory ceilings are still measured by
  hand at release time, not asserted.*
- **Real-capture decode is not pinned by CI** (the files live in `~/Downloads`, not the
  repo). The hardest decode paths — 2-col→8-col reconfiguration, cold-start-preloaded
  config, negative pre-trigger origin — are exercised manually. *Mitigation:* capture the
  essence of each as a synthetic regression where possible.
- **GUI testing is smoke-level**, not interaction-level. It checks wiring and population,
  not pixel rendering, drag/zoom behaviour, or event-loop edge cases.
- **Open correctness item:** the Lusk DP1 audio "cliff" at ~sample 7763 (see roadmap) is
  understood-but-unresolved and has no test; the register-by-register Compare diff is
  known-unreliable when the visualizer and decoder number dataports differently (the grid
  diff is the trusted signal — documented in `compare_config`).
- **Some cross-checks depend on sibling repos.** The *native* C++ placement
  cross-check and the descrambler vectors use `../mipi-soundwire-I3S-visualizer`;
  absent it, those silently skip. *Mitigated* for placement: the Visualizer's
  89-config corpus + per-DP goldens are now **bundled** under `tests/visualizer/`,
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
