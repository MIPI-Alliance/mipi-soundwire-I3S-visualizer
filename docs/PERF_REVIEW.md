# Performance Review — 3.0.8 (5-round, 39 verified findings)

Produced by a five-round find → adversarially-verify → dedup workflow (79 agents).
Every finding below survived a skeptic pass; items the skeptics rejected/downgraded are
listed at the end so they aren't re-raised. Excluded up front (already tracked): swviz
per-tick placement, `.sal` v3 *read* varint, deferred Statistics, C++/swviz duplication.

"Hot path" = runs per-UI / per-cell / per-frame / per-cursor-move, or scales with capture
length. All fixes are output-preserving (verify against the goldens after each).

> **Status (3.0.8): 24 of the 39 fixed** — all Tier-1 and Tier-2 items plus the Tier-3
> quick wins (#1–#10, #13b, #14, #15, #17–#25, #27, #43), each output-preserving and
> golden-verified (full suite green). **Deferred as tracked debt** (docs/TECH_DEBT.md,
> with rationale): **#11** VCD + **#12** `.sal` encode vectorize (rare, non-interactive
> I/O on delicate/complex formats — vectorizing risks byte-corruption for marginal gain;
> #12 mirrors the already-accepted read-side varint), **#13a** `Detect` early-exit
> (correctness-sensitive — can change the detected column count; only the safe #13b
> stride landed), **#16** `grid_cells_at` CSV-replay re-marshal (needs C++ register-model
> checkpointing for a ~100–250 µs gain). **#26** (commands remarshal) left as-is — it's
> 0.2–0.6 % of a re-decode. The items below are the review record *as found*.

## Tier 1 — visible UI stalls on routine interaction (fix first)

- **#1 `ui/symbol_view.py::_populate` + `ui/register_view.py::_rebuild` — bulk table fill without `setUpdatesEnabled(False)`.** Every settled cursor move while the CDS Symbols / Registers dock is visible (tabified docks count as visible even when not the raised tab). ~50 ms (symbols) / ~9.5 ms (registers) offscreen; the *same* bug in `decoded_sample_view.py` was ~140× worse live (24 s) before its `bce6113` fix. **Fix:** wrap clear+populate+resize in `setUpdatesEnabled(False)`/try-finally; cache column widths with a one-shot flag reset per capture — copy `decoded_sample_view.py:248-266`. Effort **S** each. Highest-confidence item in the batch.
- **#2 `dsp/resample.py::_fftconvolve_same` — monolithic full-capture FFT for PDM decode.** Time AND peak memory scale with capture length, unbounded: 30–60 s of PDM → multi-second stalls + **3.6–5.8 GB transient RSS**; runs eagerly per PDM channel on every load/re-decode. The batch's only memory-safety finding. **Fix:** overlap-add/save block convolution or polyphase FIR; match resample/PDM goldens. Effort **M–L**.
- **#3 `ui/command_table.py::fit_columns()`/`widen_to_true_max()` — full double-scan of every command row on every re-decode.** ~27 µs/row → 1.7 s @ 50k, 5.5 s @ 200k, on the GUI thread, on load AND all ~6 re-decode sites (SSP nudge / override / CSV apply / hub-depth / scrambler / port-samples). **Fix:** bound the Response-column scan (elide/sample); compute the Row-column width analytically from `session.total_rows`; cache the max per capture. Effort **M**.
- **#4 `ui/main_window.py::_select_command_for_sample` — O(gap) `mapFromSource` scan under an active filter.** Up to **1.2 s** for one cursor settle (long clean capture + one early error + errors-only filter — an ordinary workflow). **Fix:** cache a sorted array of accepted source rows (invalidate on filter/model change), bisect it. Effort **S–M**.

## Tier 2 — real, hot, moderate; fix next

- **#5 `native/DlvSampleSource::levelAt()` — O(n log m) binary search per UI instead of an O(1) forward cursor (entire PHY3/DLV decode).** ~60–75× slower than a forward walk (520 ms vs 6.9 ms at scale). Also add `DlvSampleSource*` to the `Decoder.cpp:969` `dynamic_cast` streamRemainder fast path FBCSE already gets. **Fix:** monotonic `mEdgeCursor` mirroring `TransitionSampleSource::NextUi`; re-derive only on `Seek()`. Effort **M** (native, Seek care). PHY3 is new this cycle.
- **#6 `ui/grid_view.py::_freeze()` — recursive cache-key serialization costs more than the C++ call it guards.** Runs unconditionally *before* the cache-hit check on every grid redraw: ~1 ms @64 rows (3–4× the marshal it gates), ~15 ms @1024, ~150 ms @10240. **Fix:** key off cheap scalars (sample, rows, column_count, a decode-generation counter) like the sibling `set_tx_raster` already does. Effort **S–M**. (Triple-confirmed.)
- **#7 `session.py::_build_register_files` — register replay rebuilt from scratch every cursor move, no caching.** ~340–400× slower than the cached+bisected grid sibling (~48–54 ms/move @60k config commands). **Fix:** mirror `_effective_sorted()`/`_grid_replay_in_effect` — precompute+cache, bisect+slice. Effort **M**.
- **#8 `ui/main_window.py::commit_column_samples` — full config-command scan regardless of viewport (Raw Capture pan/zoom).** O(all config commands), never cheaper when zoomed in; ~19 ms/call @200k, fires continuously during drag. **Fix:** bisect into the cached `_effective_sorted()`. Effort **S–M** (infra exists).
- **#9 `ui/timeline.py` commit-point markers — unbatched full-list scan + linear hover hit-test.** 0.16 → 17 ms/paint @10k points (~60 Hz during playback); `_nearest_commit_point` 8.9 ms/call. **Fix:** copy the file's own `_decimated_ticks()` cache + bisect `_nearest_commit_point` like `_nearest()`. Effort **S**.
- **#10 `native/Decoder.cpp::mAudio` never `reserve()`d — realloc-copy churn on every audio decode.** Measured ~15–30% `run()` reduction from one `reserve()`. **Fix:** `mAudio.reserve(mTotalUis/mColumnCount * headroom)` before the feed loop. Effort **S** (prototyped in-review).
- **#11 `ingest/vcd.py` — pure-Python per-token decode, unlike every sibling reader.** ~0.35–0.6 µs/value-change → ~35–60 s for a 100M-change VCD. **Fix:** NumPy vectorize (bulk tokenize + masks + forward-fill) like `digital_csv`. Effort **L**.
- **#12 `ingest/saleae_binary.py::encode_v3_delta` — pure-Python per-transition `.sal` *write* encoder** (mirror of the known read-side varint, but unlogged). ~13.5 s tottime @31.5M transitions, synchronous on every export. **Fix:** vectorize base-128 encode or move to C++. Effort **M**.
- **#13 `native/CColumnDetector::Detect()` — O(candidates × window) resync, paid on content-triggered resyncs (~every 8192 rows during idle/CRC-degraded stretches).** ~19–23 ms/call. **Fix (b, S):** stride from the first congruent index instead of modulo-per-index (18–25× on the inner loop, bit-exact). **Fix (a, M):** early-exit the candidate sweep on strong confidence.

## Tier 3 — smaller / narrower; batch into a cleanup pass

- **#14 `ui/grid_view.py::_text()`** uses `addText`/`QGraphicsTextItem` (rich-text) for plain labels — `addSimpleText` is 2.1×, pixel-identical. **S**.
- **#15 `native/Decoder.cpp::layoutGrid()`** heap-allocates a fresh `emits` vector per (row,col) beside three correctly-hoisted siblings — hoist it. **S**.
- **#16 `session.py::grid_cells_at`** re-marshals the invariant `_csv_replay` prefix through C++ every tick (~100–250 µs with a 200–500-register CSV baseline). Note: the finding's "Python concat is the cost" framing was wrong — the C++ re-cast is. `config_dataports_at` is **cold**, not hot. **M**.
- **#17 `ui/audio_view.py::_render`** casts the whole sample-position array to float64 before indexing ~2048 points — index first, cast the slice. Trivial, ~near-zero risk, punches above its weight. **S**.
- **#18 `session.py::_marks_rows`** scalar NumPy indexing in a Python loop vs `.tolist()` once — 2–2.8×, byte-identical (Decoded-Samples pane). **S**.
- **#19 `session.py::cds_column_samples`** FBCSE branch Python append-loop vs `np.arange`-with-cap (the sibling `tx_raster` pattern) — 9× common / 666× dense, verified on 300 windows. **S**.
- **#20 `ingest/raw_export.py::export_csv`** per-row `writerow` vs batched join — ~2.1× that stage, ~35% overall. **S**.
- **#21 `ingest/saleae_binary.py::_times_to_samples`** un-chunked float64 conversion — same OOM pattern already fixed in the sibling `saleae_sal.py::_times_to_edges`, never ported to the `.bin` path. **S**.
- **#22 `store/audio_store.py::export_wav`** allocates an int64 interleave buffer for a 16/24/32-bit container — allocate at final width. 2.7–4×. **S**.
- **#23 `analysis/subcapture.py::_collapse()`** unbounded on the convolve-match path — the exact-byte sibling was hardened with `_MAX_HITS=8192`; the convolve path was missed (short/low-entropy sub-capture vs idle-heavy capture → millions of hits). **M**.
- **#24 `store/audio_store.py::streams()`/`channels()`** full linear scan-and-filter in a nested loop at 6+ sites — O(streams × keys); the grouping index is computed once and discarded. Quadratic (1.29 s @32k keys). **S**.
- **#25 `analysis/subcapture.py::_match_counts`** evaluates all 4 symbol values though the alphabet is binary — ~24–28% wasted convolve time. **S**.
- **#26 `session.py::commands()` remarshal on every re-decode** — real, and the `audio()`→`audio_columns()` SoA pattern applies, but it's 0.2–0.6% of `_redecode()` (C++ decode dominates). **M**, low urgency.
- **#27 `ui/main_window.py::load_session`** re-sorts an already-sorted command list — bisect-merge the sparse commit rows. ~2–47 ms. **S**.
- **#28 `native/Decoder.h::mCheckpoints`** unreserved/unbounded unlike sibling capped structures — moderate at realistic row rates (single-digit MB), not severe. **S** reserve / **M** prune.

## Themes

1. **"The cache guard costs more than the thing it guards."** #6 `_freeze`, #17 full-array cast, #16 (5+ instances). Compute a cheap proxy, not a full-fidelity copy, to decide whether to skip.
2. **"The fix already exists in a sibling file — just not applied here."** ≥7 of the top ~15 (#1, #9, #6, #19, #21, #10/#26). A targeted grep-for-the-unfixed-pattern sweep would be unusually high-yield.
3. **"No cache on the cursor-move hot path, unlike its sibling that got one."** #7 register vs grid replay, #8 commit_column_samples, #4 command filter. `_effective_sorted()`/bisect is the established idiom; several sites never got it.
4. **Full-collection scan instead of windowed/bounded work.** #3, #8, #23.
5. **Native complexity mismatches its own adjacent sibling code.** #5 (forward cursor two structs away), #13 (stride in the same loop), #15 (unhoisted alloc beside three hoisted).
6. **Unvectorized Python loops in ingest/export** (#11 VCD, #12 .sal encode, #20 CSV export) — the stragglers among otherwise-vectorized readers.

## Quick wins (S, high confidence, do first)
#1, #17, #10, #15, #13b, #9, #19, #18, #21, #22, #24, #25, #27, #6/#7.

## Bigger bets (M–L, need design/golden care)
#2 (resample memory — highest user impact for PDM), #3 (fit_columns), #4 (command filter), #5 (DLV forward cursor), #7 (register caching), #8 (commit bisect), #11 (VCD), #12 (.sal encode), #13a, #23 (subcapture bound — needs a partial-results UX decision).

## Adversarially downgraded — do NOT re-raise
- **`_select_symbol_for_sample` missing memoization** — real mechanism but ~30–94 µs, 3 orders below a frame budget. Skip. *(But `select_bus_row`'s linear scan under it, #43, was separately confirmed.)*
- **`set_tx_raster` cache-key `tobytes()`** — real but ~2–30 µs, ~10,000× cheaper than the rebuild it guards; the proposed fix would introduce a **stale-TX-map correctness regression**. **Do not apply.**
- **`subcapture._collapse` redundant `sorted()`** — cold (menu-triggered, capped). Not a hot path.
- **`audio_store.from_audio_columns` per-sibling PDM rate recovery** — ~6 µs, subsample-capped. Cosmetic.

## What's already good (don't touch)
`_effective_sorted()`/bisect caching in `session.py` (the fix template for half of these — the gap is coverage, not design); `decoded_sample_view.py`'s `setUpdatesEnabled` fix and `timeline._decimated_ticks()` (propagate them, don't reinvent); `set_tx_raster`'s cache-key design; the `_MAX_HITS`-hardened exact-match subcapture path; the bounded `mCdsBitRows`/`mBitSamples` structures.
