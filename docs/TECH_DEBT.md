# Tech Debt Register

Findings consciously **deferred** from a review/release cycle — tracked here so they
aren't lost between releases. Pick roughly one strategic item per cycle (see
[DEVELOPMENT.md](DEVELOPMENT.md)). Add the cycle that logged each item, an effort/impact
read, and enough context to act without re-deriving it.

Severity/effort: **H**igh / **M**edium / **L**ow.

---

## Architectural

### Multi-threading the decode path — researched, deferred → [decode_multithreading.md](decode_multithreading.md)
- **Logged:** 3.0.8 · **Impact:** M (~1.25× on full `.sal` open, Amdahl-bounded) · **Effort:** M–L
- Feasibility study for parallelizing `swi3score::Decoder::run()`. Measured split: control
  plane ~2.76 s (sequential floor) vs audio assembly ~4.4 s (parallelizable). Proposed
  two-pass design (sequential control → parallel checkpoint-restart audio) reusing the
  existing `Checkpoint`/`bitSamplesInWindow` machinery. Linchpin: the checkpoint omits the
  `CDescrambler` LFSR state (fine for positions, wrong for values) — cheap to add. Scope
  v1 to FBCSE (DLV's virtual-PLL source doesn't seek cleanly). Cheaper adjacent win noted:
  PDM decimation parallelizes with plain Python threads today (numpy FFT releases the GIL).
  Full design, work breakdown, risks, and derisking in the doc.

### Performance review (3.0.8) — 39 verified findings in [PERF_REVIEW.md](PERF_REVIEW.md)
- **Logged:** 3.0.8 · **Impact:** mixed (H→L) · **Effort:** mixed
- A five-round find→adversarially-verify→dedup review (79 agents) produced a ranked,
  measurement-backed list of hot-path perf issues — see [PERF_REVIEW.md](PERF_REVIEW.md)
  for the full tiers, quick-wins vs bigger-bets, themes, and the adversarially-downgraded
  items (do NOT re-raise those). Headliners: Tier-1 UI stalls (#1 table-fill without
  `setUpdatesEnabled`, #2 unbounded PDM-resample memory 3.6–5.8 GB, #3 command-table
  column double-scan, #4 filtered command-select up to 1.2 s). Recurring theme: "the fix
  already exists in a sibling file — apply it here." Not yet actioned.

### `MainWindow` (3935 lines) and `Session` (2351 lines) are god-modules
- **Logged:** 3.0.8 review · **Impact:** M (maintainability) · **Effort:** H
- Both are large by line/method count. The 3.0.8 review confirmed the cohesion is
  largely **real** (one live decoder + segment table for Session; one shared time
  cursor + all panes for MainWindow), so a generic split is *not* recommended — it
  would move coupling sideways, not remove it. Only targeted extractions are
  defensible, and closer inspection found each more entangled than a line-range
  suggests — do them as focused efforts with the full suite as oracle, not in a sweep:
  - **Session → `DlvClockAnalysis`**: `recovered_clock`, `missing_rsp_samples`,
    `pll_unlocked`, `_rsp_analysis`, `_recovered_row_syncs`, `_make_dlv_source`,
    `_configure_source_for_phy`, `_autodetect_partial_dlv`. Reads `self.decoder`/
    `self.capture`/`self.settings`; would take those in a ctor. Most defensible (already
    conceptually modular per the PHY3-DLV architecture note).
  - **Session → `ConfigCsvImporter`**: `apply_config_csv` + `_reload_csv_seed`,
    `_csv_writes_replay`, `_discard_config_tmp`. Self-contained *except* it mutates
    `self.source` and `apply_config_csv` is a public, tested API — keep it as a
    forwarder.
  - **MainWindow — the re-decode-preserving-state pattern is duplicated ~6×**
    (`sess, keep_cursor, keep_bms = …; def done(...): self._bookmarks = bms;
    _push_bookmarks(); restore cursor`) across locate / register-edit / SSP / scrambler
    / hub-depths / config-apply. Factor into one `_redecode_preserving(build_fn)` helper
    — the single highest-value MainWindow cleanup, and lower risk than moving state out.
  - **MainWindow controllers** (bookmarks / workspace / theme / authoring / locate):
    genuinely coupled — `self._bookmarks` alone is read/written at ~20 sites threaded
    through the re-decode machinery, and `apply_theme` reaches ~18 view attrs. Extract
    only behind characterization tests; expect rewiring, not a clean lift.

### Persistent out-of-core results store is scaffolded but not wired
- **Logged:** 3.0.8 review · **Impact:** M (only multi-GB captures) · **Effort:** H
- `docs/architecture.md` §"out-of-core results store" is the design target; today the
  decoder hands results to `Session` as in-memory Python/NumPy structures. `store/audio_store.py`
  has a working `np.memmap` backend but every production caller uses the in-RAM branch
  (`mmap_dir` only passed in tests); `store/command_store.py` (Arrow/Parquet) is a
  display/export cache, not fed back into replay; `TimeIndex`/`RegisterTimeline`/the
  register event-log-with-checkpoints don't exist (register-at-cursor replays the
  in-memory command list). Fine at demo/typical scale.
- **Options:** when a concrete multi-GB workload lands, thread `mmap_dir` from a real
  large-capture path, persist commands via the existing `CommandStore`, and add the
  register checkpoint log. Behaviour-neutral if the in-RAM path stays the default.

### Engine per-tick placement is O(rows × columns × DPs) in Python — and is a *second* live placement engine
- **Logged:** 3.0.6 review · **Sharpened:** 3.0.8 review · **Impact:** L–M · **Effort:** H
- Two implementations of the §14.2.5 placement cascade are *live*: the C++ core
  (`swi3score.grid_from_commands/_csv`, drives Analysis-mode + audio) and the Python
  `swviz` engine (`swviz/core/engine.py::BusModelBuilder`, drives Visualization-mode
  authoring via `model/viz_engine.py::render_payload`). Their placement is held
  bit-exact by the golden suite (`test_visualizer_placement` pins C++ to the goldens;
  `test_visualizer_engine` pins the swviz engine to the *same* goldens), and the
  authoring render output (issues + clashes) is pinned by `test_authoring_render_golden`.
- **Measured live cost (3.0.8):** the authoring rebuild is capped at 1024 rows
  (`_refresh_authored_grid`), so the worst case is ~330 ms on the *heaviest* example
  config (dense PDM + channel-grouping) and <100 ms typical — debounced, off the
  paint path. The "~14.5 s" figure is the 10 240-row *engine max*, which the UI never
  reaches; the live experience is acceptable. 3.0.8 hoisted the build-invariant
  `num_columns` / `_num_channels` off the per-UI hot path (byte-identical, ~6% faster);
  further safe micro-opts hit diminishing returns because the cost is inherent per-tick
  method-call overhead in the state machine.
- **Do NOT merge the two engines** (investigated 3.0.8): C++ `grid_from_csv` and swviz
  agree on solo-DP placement but diverge on FULL authoring output — sample/device/
  channel *metadata* (70/94 configs) and actual placement (13/94: flow-control cells,
  clash suppression). Routing authoring through C++ would change authoring output, and
  reconciling it re-introduces the complexity the merge aimed to remove. The remaining
  option — vectorize/rewrite swviz placement — is high effort/risk for the bounded live
  payoff above; not recommended unless a real large-config workload appears.

### `.sal` v3 varint decode is a pure-Python byte loop
- **Logged:** 3.0.6 review (#11) · **Impact:** M (only the largest captures) · **Effort:** M
- `ingest/saleae_binary.py::_v3_decode_deltas` decodes the base-128 delta codec one byte
  at a time — ~15–20 s on ~100M-transition `.sal` files (documented + accepted; runs on
  the async import path). **Options:** numpy-vectorize the varint classification, or a
  small C++ helper via pybind11 (consistent with the rest of the decode core). Delicate
  format — fuzz/round-trip heavily before trusting it.

### Unvectorized I/O codecs — deferred from the 3.0.8 perf review (rare, delicate paths)
- **Logged:** 3.0.8 review · **Impact:** M (only huge exports/imports) · **Effort:** M–L
- **#12 `saleae_binary.encode_v3_delta`** — the write/export mirror of the varint decode
  above: a pure-Python per-transition base-128 encoder (~13.5 s tottime at 31.5M
  transitions on a `.sal` export). Same delicate variable-length codec, same fix options
  (bucket-by-digit-count NumPy, or a C++ helper). Deferred for the same reason as the
  read side — a rare, non-interactive export path where a vectorization bug would corrupt
  the format; not worth the risk over the interactive/memory wins already landed.
- **#11 `ingest/vcd.py`** — VCD import decodes every value-change with a pure-Python
  token generator + per-edge append (~35–60 s on a 100M-change RTL dump), unlike every
  other (vectorized) ingest reader. Fix is a full NumPy tokenizer rewrite (L); VCD is a
  niche import path, so deferred behind the higher-traffic work.
- The `#13a CColumnDetector::Detect` early-exit and `#16 grid_cells_at` CSV-replay
  re-marshal were also considered and NOT done: #13a can change WHICH column count is
  detected (correctness-sensitive — only the safe #13b inner-loop stride landed), and #16
  (~100–250 µs/tick) needs C++-side register-model checkpointing to avoid re-parsing the
  invariant CSV prefix — larger risk than its modest gain.

### Watch for "label doubling as a serialization key" couplings
- **Logged:** 3.0.6 (fixed, noted as a pattern) · **Impact:** M · **Effort:** —
- The engine emits `"DP{index}"` warning labels that the Visualizer JSON encoder parses
  back into indices by regex *and* the UI displays. Fixed by relabeling at the display
  boundary (`viz_engine.render_payload`), leaving the serialization contract intact. The
  lesson for the coupling review lens: a string that is simultaneously human-facing and
  machine-parsed is a latent trap — flag others of this shape.

## Quality gates

### ~~Flip `ruff` + `mypy` from report-only to blocking~~ — DONE in 3.0.12
- **Logged:** 3.0.7 · **Closed:** 3.0.12
- Done, and the reason it took five releases is itself the lesson: the `lint` job existed
  but the workflow never executed; the hand-run release gate never included lint either;
  and the checklist that listed the gate was prose that had drifted from `ci.yml`. 445 ruff
  findings and 448 mypy errors surfaced on the **first published PR** of the v3 line, in
  front of reviewers.
- Now: tree swept clean, `continue-on-error` removed, `mypy … || true` replaced by
  `tools/mypy_gate.py` (per-file non-regression vs `tools/mypy_baseline.json`), and the
  gate is a single script — `tests/gate.sh` — that `tests/test_release_gate.py` pins
  against `ci.yml` so the two cannot drift again. See docs/DEVELOPMENT.md for the
  now-explicit statement that neither gate subsumes the other.

### Pay down the mypy errors (205 left of 448; frozen per file)
- **Logged:** 3.0.12 · **Impact:** M (real type bugs are hiding in here) · **Effort:** M
- First measurement ever taken (2026-08-01): **448 errors in 29 files** of 106 checked.
  **Now 205 in 24 files** — see the VizTheme fix below. The gate freezes the count per file,
  so it can only shrink; frozen is not fixed.
- **54% was one root cause.** `apply_palette()` `setattr()`s every palette key onto
  `VizTheme`, so a type checker saw *none* of them: 243 `"type[VizTheme]" has no attribute`
  errors across **15 files**. Declaring the 42 palette keys + 2 derived colours as bare
  class annotations (names and types, no runtime change) removed all 243 at once.
  `tests/test_theme.py` now pins the declarations against the palettes both ways.
  Lesson for the rest: look for a shared dynamic-attribute pattern before grinding
  file-by-file — the per-file totals were a symptom, not the shape of the problem.
- Remaining 205, by class: `assignment` 44, `attr-defined` 32, `var-annotated` 31,
  `union-attr` 27, `has-type` 26, `arg-type` 15, `index` 12, `misc` 5. No single dominant
  cause left.
  - **`var-annotated` (31) is the next cheapest** — "need a type annotation" on empty
    collection initialisers, concentrated in `ui/eye_view.py` (8), `ui/audio_view.py` (8),
    `ui/raw_view.py` (6). Each needs the element type read off its use site, so it is
    mechanical but not blind.
  - `has-type` (26) often cascades from `var-annotated`; expect some to disappear with it.
- `[tool.mypy]` is deliberately lenient (`check_untyped_defs = false`,
  `ignore_missing_imports = true`), so these counts are a FLOOR — tightening either setting
  will reveal more. Tighten only alongside a paydown, or the gate becomes noise.
- Re-baseline after any improvement: `python3 tools/mypy_gate.py --update`.

### Dependencies are unpinned, so the mypy ratchet only applies in one environment
- **Logged:** 3.0.12 · **Impact:** M (a release check that can't run everywhere) · **Effort:** M
- `requirements.txt` uses loose constraints, so environments drift: macOS has numpy **2.4.4**
  and the Windows VM **2.5.1**. mypy reads numpy's stubs, so the *identical* tree measures
  **205** errors on one and **222** on the other (`session.py` alone 22 → 38). Neither is
  wrong; they are not comparable.
- Consequence: `tools/mypy_gate.py` records the environment in its baseline and reports
  **NOT COMPARABLE** (exit 3) elsewhere, which `tools/gate.py` lists as `NOT RUN HERE`. So
  the ratchet is real on the machine that took the baseline and honestly inert elsewhere —
  but the Windows half of the release gate has no type ratchet at all.
- **Options:** pin the data/GUI stack in `requirements.txt` (converges the environments and
  makes one baseline valid everywhere — but pins what users install); or keep per-platform
  baselines; or accept the ratchet as canonical-environment-only and rely on CI's ubuntu
  3.12 leg for the second opinion. Decide before tightening `[tool.mypy]`, since stricter
  settings will widen the divergence.

### Add a coverage threshold once a baseline is known
- **Logged:** 3.0.7 · **Impact:** L · **Effort:** L
- The `coverage` job reports but doesn't gate. After a few cycles, set
  `--cov-fail-under=<baseline>` so coverage can't regress. Reviews have found untested
  areas reactively (locate GUI, per-source CDS round-trip) — a floor makes them visible.

## Deferred micro-optimizations (low priority)

- **Audio store build is eager at load** (`main_window.load_session`) even though the Audio
  and Capture docks are hidden at load (Grid/Registers are raised). The initial-open store
  build already runs off the GUI thread in `_LoadWorker`, so this only costs on the
  synchronous re-decode path (SSP nudge / override / CSV apply). Fully gating it is coupled
  to the eagerly-populated (and heavily tested) Audio view — it would need Audio/Capture
  dock-visibility handlers plus test updates — so it's MED effort, not a cheap win.
  Statistics, the one genuinely un-gated *heavy* consumer, was deferred to its dock in the
  3.0.8 review; the store build itself is left eager. Revisit if a multi-GB re-decode stalls.
- **Eye view** (`ui/eye_view.py`) re-histograms all four flavours on a single legend
  toggle. User-action-only, not per-frame — 3.0.6 review (#10), left as-is.
- **Raw Capture `_refresh_ports`** recomputes per-lane masks each render; already windowed
  and only on genuine range changes — 3.0.6 review (#10), left as-is.
- **Audio auto-follow** re-pans during non-playback drags; coalesced by the timeline
  debounce — 3.0.6 review (#10), left as-is.
- Revisit only if a perf ceiling or a user report implicates them.

## Deferred findings

- **The payload Column-0 phase fix has no automated guard any more.** `b898fa4` seeded
  the payload engine with the CDS's detected Column-0 phase: a truncated / mid-stream
  capture starts partway into a row, CDS re-derives Column 0 from its differential code,
  but the payload is sampled as an ABSOLUTE level, so without the seed every port reads
  `phaseOffset` columns early (garbled sources; a 2-channel source that can sync CH0 or
  CH1 but never both, at any SSP row). `tests/test_column_phase.py` guarded it against a
  local Box capture pair and was **deleted in 3.0.11** — it had skipped on every machine
  in the repo's life, so in practice it was guarding nothing.
  - A synthetic replacement is feasible. **Correction (3.0.11):** an earlier version of
    this note claimed truncating a demo mid-row "mis-locks the column detector to 2
    columns with no audio". That was wrong — my probe passed `Capture(...)` positionally
    with clock/data (and their initial levels) SWAPPED; the real field order is
    `(clock_edges, data_edges, initial_clock, initial_data, sample_rate_hz)`. With the
    correct order a mid-row-truncated demo holds 16 columns and decodes all four streams,
    so no config-CSV pinning is needed and the SSP-sweep complication does not arise
    either. Use `tests/test_analog_import.py::_slice_capture` (keyword args) as the
    known-good slicing helper. The remaining work is just to assert the AUDIO matches the
    aligned decode across the truncation, which is straightforward.
  - `test_capture_pipeline.py::test_column_detect_phase_offset` already covers a mid-row
    start for CDS/command decode, so only the AUDIO half is unguarded.
  - **Narrowed by the 3.0.11 release review (cross-subsystem lens).** The blanket "no
    automated guard" above is no longer accurate — the fix touches THREE sites and they
    are not equally bare:
    - `Decoder.cpp:988` (open-with-CSV / mid-stream start) — **now covered.**
      `tests/test_partial_capture_csv.py`, added this cycle in `e46e594`, catches a
      mutation to `SetStartColumn` there immediately (`test_recovered_audio_is_clean_end_to_end`
      and `test_recovered_audio_matches_the_full_capture` both fail).
    - `Decoder.cpp:1381`/`1408` (commit-driven reconfigure) — safe by construction: it
      always fires at column 0, so there is no phase to seed.
    - `Decoder.cpp:1121` (**mid-capture `resync()` that re-detects a DIFFERENT column
      count**) — **still bare.** Verified by mutation: replacing the seed with
      `SetStartColumn(0)` leaves the whole 734-test suite green.
      Why it is hard to reach: `resync()` needs `mNeedResync`, which requires
      `kCommandConfidenceRows` (8192) rows with **no CRC-valid command** in AUTO mode (no
      forced count, no CSV) — and every demo emits keep-alive Pings specifically to prevent
      that. A test therefore needs a new `Demo.cpp` variant that starves the bus of
      commands for >8192 rows and then changes the column count without a commit, then
      asserts audio phase (tone SNR) across the re-lock. That is a C++ demo addition, not a
      Python-only test, which is why it was deferred rather than fixed at the 3.0.11 tag.
      **Failure it would catch:** a real capture that loses confidence mid-row (bus glitch,
      dropped frames) and re-locks at a different width decodes every payload port
      `phaseOffset` columns early — silent audio corruption, no error raised.
  **Logged:** 3.0.11 · **Impact:** M (a real past regression; the CSV-open path is now
  guarded, the resync path is not) · **Effort:** S for the audio half of the CSV-open
  path; M for the resync path (needs a command-starved demo variant)

- **A deferred Read may split into two unrelated Statistics groups.** Logged from the
  3.0.11 release review (UI lens) as **SUSPECTED — not reproduced.**
  `CCommandTransportParser::CommandName()` emits a *deferred* Read (peripheral answers
  "not ready", per `IsReadDataNow`) as TWO command records: the ReadSetup phase as
  `command: "ReadA32"` carrying no data, and the later ReadData phase as its own
  `command: "ReadData"` carrying the payload. `_command_rows` groups strictly by that raw
  string, so the per-command statistics would show "ReadA32" with a permanent
  "0 bytes read" next to a separate "ReadData" group holding the real byte and error
  counts — which a user is unlikely to connect to "the Reads". An *immediate* Read stays a
  single record and is unaffected.
  - **Why it is unverified:** `Demo.cpp` has no read-append helper at all, so no demo or
    test exercises a deferred Read end to end; confirming it needs either a demo variant
    that emits one or a real capture containing one.
  - **If confirmed, the fix is a grouping key, not a decode change** — group by a logical
    command identity (merge the ReadSetup/ReadData pair) rather than the wire phase name.
  **Logged:** 3.0.11 · **Impact:** L–M (display only; no decode/audio data is wrong)
  · **Effort:** S once a deferred-Read fixture exists

- **PHY1 is excluded from the cross-PHY invariants on a stale premise.**
  `tests/test_phy_invariants.py` has `PHYS = [2, 3]` with a comment saying PHY1 "isn't
  built yet" — but `Session.from_demo(..., phy=1)` has since been implemented and works
  (4-column bus, cold start included). Adding 1 to `PHYS` today gives **26 of 27
  passing**; the single failure is `test_audio_streams_present`, which hardcodes
  `[(0,0), (0,1), (0,2), (0,3)]` while the PHY1 demo synthesizes three ports. That
  assertion is demo-shape, not a PHY invariant, so it should be generalised (assert every
  decoded stream is non-empty, or per-PHY expected sets) rather than left excluding a
  whole PHY from 26 structural checks.
  **Logged:** 3.0.11 · **Impact:** L–M · **Effort:** S

- **Imposing a config silently drops ports outside the capture's decoded width.** A
  config CSV whose ports sit beyond the capture's columns has those ports simply not
  placed — no warning, and the grid narrows. `open_visualizer_config` already warns
  about mid-stream reconfigures and rejects capture CSVs, so this is a gap in the same
  family; pairs naturally with the item above (same import path).
  **Logged:** 3.0.10 · **Impact:** L–M · **Effort:** S

- **`capture_span_samples()` runs on the GUI thread and inflates every channel.** The
  pre-flight span read in `_open_sal` streams block headers for ALL digital channels via
  `ZipExtFile.read()`, so on a compressed multi-hundred-MB `.sal` it blocks the GUI for
  ~2 s before any dialog appears — on exactly the captures the window prompt targets.
  Correctness-clean (the estimate itself is directory-only and instant); this is the
  span read. Restrict to the two selected channels, or move it off-thread with a busy
  indicator. Note `tests/test_sal_large_capture.py::_write_sal` writes `ZIP_STORED`, so
  the "streams without inflating" test never sees a compressed file.
  **Logged:** 3.0.10 review · **Impact:** L–M (hang-feel, no data risk) · **Effort:** S–M

- **Figure_153 (PCM+PDM) DP8 TAIL cross-engine mismatch** — `grid_from_csv` (C++) and
  `viz_engine` (swviz) disagree on one TAIL cell for DP8 in the
  `spec_figures/Figure_153_*` config. Pre-existing (present on `main`, predates 3.0.9),
  unrelated to flow control; surfaced by the 3.0.9 release review's independent cross-engine
  sweep. Low impact (one spec-figure config, a tail glyph). Fix when the FBCSE tail
  placement is next touched.

## Known footguns (documented, not bugs)

- `DataPortConfig` register fields are mutated in place, so `functools.cached_property`
  on derived props (`_num_channels`, `_horizontal_end`) would serve stale values — use
  stateless computation (`int.bit_count()`), not caching.
- Modal `QProgressDialog.setValue()` re-enters the event loop; slots that touch a dialog
  around it must use a local ref and be idempotent (see the 3.0.6 locate-crash fix).
