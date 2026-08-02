# Multi-threading the decode path — feasibility research

**Logged:** 3.0.8 · **Status:** researched, *not implemented* (single-threaded for now) ·
**Effort:** M–L · **Payoff:** ~1.25× on full `.sal` open (Amdahl-bounded)

Scoping study for parallelizing `swi3score`'s `Decoder::run()`. Written up so the
analysis isn't re-derived. Reference capture throughout: an 89 MB / 420 M-edge PDM
capture — 24 kHz speaker content at 768 kHz PDM, 500 MHz sampling, 49.9 M audio samples,
81,493 commands. All timings are macOS / Python 3.11 / the local `-O3 -flto` build; they
vary ±15 % run-to-run (the file lives on network-backed storage).

## Context

After the 3.0.8 ingest/analysis perf pass (full open 86.9 s → ~17 s), the remaining cost
is dominated by the native decode. Breakdown of a full open now:

| stage | time | nature |
|---|---|---|
| `load_capture` (v3 varint → native, ingest) | ~6.7 s | I/O + native decode + cumsum |
| `Decoder.run()` | ~7.1 s | **the subject of this doc** |
| `audio_store()` (grouping + PDM decimate) | ~3.9 s | Python/numpy |

## Why the decoder is sequential today

`Decoder::run()` (`native/swi3score/Decoder.{h,cpp}`) is a per-UI streaming state
machine. `feed(level, sample, srcUi)` runs once per UI and carries state that depends on
every prior UI:

- NRZS decoder disparity (`CNrzsDecoder`), 8b/10b running disparity,
- the command-transport parser (`CCommandTransportParser`),
- the register/config snoop (`CRegisterModel`) → the *bus config* (column count, active
  dataports, intervals) in effect at each row,
- the payload-engine phase (`CPayloadEngine`), SSP anchoring, the continuous row counter,
- the resync watchdog (column-count changes detected from the stream).

You cannot split the edge array into N chunks and decode them independently: a chunk in
the middle does not know the config that earlier commands/commits established, and sample
placement is a function of that config + SSP phase.

## The measured split — what makes it tractable

`run()` timed with `DecoderSettings.decode_audio` off vs on (both tick every UI):

| | time |
|---|---|
| control plane only (framing + 8b/10b + commands + config/SSP schedule) | **~2.76 s** |
| + payload/audio assembly (per-data-bit; ~50 M samples) | **~7.14 s** |

So **~4.4 s is audio assembly** (parallelizable) and **~2.76 s is the sequential control
plane** (the Amdahl floor).

### Enabling infrastructure that already exists

- **Checkpoint / window replay.** `Decoder` already records `mCheckpoints` (periodic
  `CPayloadEngine` phase snapshots), `mTransportEvents`, and `mConfigEpochs`, and
  `bitSamplesInWindow()` already reconstructs a payload window from the nearest checkpoint
  *without decoding from the start*. That is exactly the primitive parallel chunked
  assembly needs.
- **Seekable, read-only source.** `TransitionSampleSource` seeks by binary-searching the
  (immutable) edge arrays — each worker can hold its own source view over shared data.
- **Order-agnostic output.** `store/audio_store.py::from_audio_columns` sorts by
  `start_sample` and ignores the core's per-`(dp,channel)` `index`, so workers may emit
  blocks out of order; merge is concatenate-then-sort.
- **GIL already released** across `run()` (`bindings.cpp`).

## Proposed design — two-pass (sequential control → parallel audio)

1. **Pass 1 (sequential, ~2.76 s):** run the control plane only; emit the config schedule
   (`mConfigEpochs` + `mTransportEvents` + `mCheckpoints` + segment/row/SSP info). Already
   recorded today.
2. **Pass 2 (parallel, ~4.4 s → ~0.6 s @ 8 cores):** partition the UI range into K
   row-aligned blocks, split at config-epoch boundaries so each block has a single fixed
   config. Each worker restores the nearest checkpoint, fast-forwards a bounded distance
   to its block start, then assembles its block's audio from its own seekable source +
   `CPayloadEngine`. Merge = concatenate the per-block `AudioRec` vectors.

Do **not** attempt to parallelize the control plane (speculative comma-resync chunking):
Amdahl caps the whole thing at the 2.76 s floor regardless, so it isn't worth the
complexity.

## The one real gap (linchpin)

The checkpoint **does not capture the descrambler LFSR state.**
`CPayloadEngine::RestoreState` (CPayloadEngine.cpp:129) explicitly leaves the per-channel
descramblers reset, because `bitSamplesInWindow` only needs sample *positions*, never
assembled *values*:

```
// Descrambler LFSRs are intentionally left reset: a windowed re-decode using this
// path reads only bit SAMPLE POINTS (schedule), never assembled values.
```

So a mid-stream restart would produce **wrong audio for scrambled ports**. Fortunately
`CDescrambler`'s entire state is `mQ` (a 9-bit LFSR) + `mNtState` (a small int) —
~4 bytes/channel — so adding `SaveState`/`RestoreState` and folding it into `Checkpoint`
is trivial. This is the linchpin: with it, checkpoint-restart reproduces audio *values*,
not just positions.

## Work breakdown

| item | size | notes |
|---|---|---|
| `CDescrambler` save/restore; include in `Checkpoint` | **S** | ~4 B/channel; the linchpin |
| Prove *windowed audio* (values) decode bit-matches sequential | **M** | reuses checkpoint machinery; the correctness oracle |
| Refactor `run()` into control-pass + audio-pass; block partitioner (row-aligned, epoch-bounded) | **M–L** | the real structural work |
| Thread pool + per-thread source/engine; merge | **M** | `std::thread`; per-block vectors |
| Bit-exact parallel-vs-sequential tests across all demos + real captures | **M** | the derisker |

## Payoff (Amdahl)

Control plane stays sequential → floor. `run()` 7.1 s → **~3.5 s @ 8 threads**
(2.76 + 4.4/8 + merge). Full open **~17 s → ~13.5 s (~1.25×)**. Modest vs the earlier
pure-Python wins precisely because the 2.76 s control plane caps it.

## Risks & how they're derisked

- **Descrambler / hidden cross-row state** → wrong parallel audio. Caught by the bit-exact
  oracle below; the descrambler gap is already identified and cheap to close.
- **DLV (PHY3)** uses a stateful virtual-PLL source (`DlvSampleSource`) that may not
  seek/restart cleanly per thread. **Scope v1 to FBCSE**; keep DLV sequential.
- **Thread-safety** relies on the edge arrays / config epochs being truly read-only during
  pass 2 (they are).

Derisking (the reason significant work here is safe):

- Keep the **sequential path as the oracle**; gate parallel behind a flag + size threshold
  with sequential as the default fallback.
- Property test **"parallel output == sequential output, bit-for-bit"** on every demo
  (all 3 PHYs) + the real captures. **K=1 must be identical to sequential** — that alone
  catches the descrambler gap and any hidden state.
- Land incrementally & revertably: (1) descrambler checkpoint state + windowed-audio
  equivalence proof, then (2) threading.

## Cheaper adjacent win (no C++ / no ABI change)

The PDM decimation in `audio_store` (~1.3 s over 5 channels on the reference capture)
parallelizes with plain Python threads *today* — numpy's FFT (`dsp/resample.py`) releases
the GIL, so a `ThreadPoolExecutor` over channels needs no native changes. Do this first if
a decode speedup is wanted; it's a fraction of the effort.

## Recommendation

Real, feasible, well-derisked — but **M–L effort for ~1.25× on total open**, bounded by
the sequential control plane. Grab the free PDM-threads win first; attempt the FBCSE
audio-parallel pass only if the ~2× on `run()` specifically is worth the structural
change. A good first, self-contained step that proves the whole approach before any
threading: **descrambler checkpoint state + a windowed-audio bit-exactness test.**
