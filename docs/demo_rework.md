# Demo capture rework — scoping & implementation notes

Status: **done** — native `MakeDemoLevels` rewritten; 1 s @ 48 kHz decodes bit-exactly;
the demo synthesises a valid §5.1.2 Cold Start (PHY2); full test suite green.

## Progress / validation (N=300, via Session)
- Decodes to geometry segments **[2, 8, 16]** (safe-lock-2 → 8-col → 16-col) with **2
  confirmed commits** — the multi-geometry transition machinery works.
- Four streams: dp0/dp1 = **16-bit PCM** (single channel), dp2/dp3 = **1-bit PDM**.
- **PCM round-trips bit-exactly** across the 8→16 commit (all 300 dp0/dp1 samples ==
  the generated sine) — audio continuity across the geometry change is proven.
- At the **1 s** default (N=48000): 48000 samples/PCM-port, a **constant 48 kHz** sample
  period (delta 2048 clocks) across all intervals, and a bit-exact round-trip.

## RESOLVED BUGS
1. **PCM Interval → 48 kHz** (was: capture ~16× too short). Fixed: per-geometry PCM
   Interval (63 @ 8-col / 31 @ 16-col) lands 48 kHz; levels ≈ 24.576 M UIs for N=48000.
2. **Resync watchdog corrupted audio at 1 s scale.** The demo idled the CDS during the
   long audio phases, so no command reset the watchdog; the old 200000-**UI** heuristic
   fired ~94×, and each `resync()` re-hunt shifted the payload engine's phase, corrupting
   ~98 % of the audio. Fixed per spec (command confidence = **8192 Rows**, {ASW2601}):
   - Decoder (`Decoder.cpp`): watchdog now counts **Rows** without a CRC-valid command
     (`mRowsSinceCommand`), threshold `kCommandConfidenceRows = 8192` — geometry-independent.
   - Demo (`Demo.cpp` `fillIdleWithPings`): the Manager Pings every **2048 Rows** through
     the audio phases (half the 4096-Row {ASW2601} limit), so confidence never lapses.
3. **Demo bring-up.** The demo synthesises a valid Cold Start selecting **PHY2** with the
   two-part Bus Reset — `Man_tReset00` 212–236 µs then `Man_tReset10` ≥1569 µs — plus
   reset-recovery, the PHY-number clock burst, and PhyStart; all §5.2.3 timings pass.
4. **PDM holds 3.072 MHz across the 8→16 commit.** Widening a port's columns makes ONE
   wide sample, not two — so at 16-col the PDM ports instead set **SampleGrouping=2**
   (register `[7:5]=1`) with **Spacing=1** (adjacent columns, 0 gap): two 1-bit samples
   per row. At the halved 16-col row rate that yields 2 samples/row = **3.072 MHz**
   (verified 3,072,000 samples/s over the 1 s demo), matching the 8-col rate (1 sample/row
   at 3.072 MHz row rate).

## Transport variety
To show scrambled and unscrambled transport side by side, **DP0 (PCM) and DP2 (PDM) run
unscrambled** (`PortControl.ScramblerEn=0`, single-rank) while DP1/DP3 keep the reset
default (scrambling ON). The generator emits the raw clear bit for the unscrambled ports;
the decode reads `ScramblerEn` and skips descrambling for them. All four still round-trip.

## Goal (from product)
A more representative demo capture that:
- starts in **safe-lock-2** (2-column PHY2 geometry, control only),
- commits to **8-column** mode — **audio starts here**,
- commits to **16-column** mode — **audio continues uninterrupted** across the commit,
- carries **1 s** of audio,
- with **two 1-channel 16-bit PCM ports @ 48 kHz** and **two 1-channel PDM ports @ 3.072 MHz**.

## Fixed timing facts
UI rate = 24.576 MHz (98.304 MHz capture ÷ 4 samples/UI). Row rate = UI rate ÷ columns.

| | 2-col (safe-lock) | 8-col | 16-col |
|---|---|---|---|
| Row rate | 12.288 MHz | 3.072 MHz | 1.536 MHz |
| PCM 48 kHz → `Interval` (rows/interval − 1) | — | **63** (64 rows) | **31** (32 rows) |
| PDM 3.072 MHz → samples/row | — | **1** | **2** (SampleGrouping=2) |

**Crux:** to hold audio rate constant across the 8→16 commit, that commit must rewrite,
per port, the rate-determining registers alongside NumColumns:
- PCM `Interval` 63 → 31,
- PDM `SampleGrouping` 1 → 2 (`0x09[7:5]`: 0 → 1) with `Spacing`=1 (`0x82[3:0]`) so the
  two 1-bit samples pack into adjacent columns — 2 samples/row at the halved row rate.
The sine phase index / PDM modulator state must **not** reset at the commit (audio
continuity); the line scrambler DOES reset at the SSP (matches the decoder's segment
boundary) but that doesn't change decoded sample values.

## Register encoding (native, verified in SwI3sProtocolDefs.h / CRegisterModel.cpp)
- `NumColumns_Next` = 0x1081, value = columns − 1 (7 → 8 col, 15 → 16 col).
- DP block base 0x2000, stride 0x100; `DpAddr(n, off)`:
  - 0x09 `SampleSizeGrouping` [4:0]=SampleSize ex-1 (**15**=16-bit PCM, **0**=1-bit PDM); single-rank (not _NEXT).
  - 0x80 `BitWidthHStart_N` [7:6]=BitWidth, [4:0]=HorizontalStart.
  - 0x81 `EnableCh0HCount_N` bit7=EnableCh0, [4:0]=HorizontalCount (lastCol = start+count; 1 col → 0, 2 col → 1).
  - 0x83/0x84 `IntLo_N`/`IntHi_N` = Interval (ex-1) split 4/8.
  - 0x87 `ChannelGrouping_N`.
  - 0x90 `EnableCh1_7_N` (bit i → ch i), 0x91 `EnableCh8_15_N`.
- Single channel (ch0 only): EnableCh0 bit in 0x81, 0x90 = 0.

## Column layout (1 device, 4 DPs; col 0 = CDS)
- **8-col:** PCM dp0=col1, PCM dp1=col2, PDM dp2=col3, PDM dp3=col4 (cols 5-7 idle).
- **16-col:** PCM dp0=col1, PCM dp1=col2, PDM dp2=cols3-4, PDM dp3=cols5-6.

## Control (CDS, Column 0) sequence — one bit/row across all geometries
preamble → Ping → [8-col writes: NumColumns=7 + 4 ports full _NEXT] → Ping → **SSCR#1**
→ (idle rows = 8-col audio) → [16-col writes: NumColumns=15 + PCM Interval=31 + PDM
HCount=1/new HStart] → Ping → **SSCR#2** → (idle to end = 16-col audio).
SSP row = (confirm-bit row) + 1 + rowDelay; the decoder derives the same rows, so
generator and decoder agree on the per-row geometry.

## PDM generation
No PDM encoder exists (demo is PCM sine only). Add a 1st-order sigma-delta per PDM
stream producing 1 bit/sample at 3.072 MHz; deterministic so the roundtrip is stable.

## Affected files
- `native/swi3score/Demo.cpp` (rewrite `MakeDemoLevels`), `Demo.h` (doc; signature may stay).
- Python `transitions.py` (`demo_capture` default → 1 s = 48000 samples/ch), preload in `main_window`.
- Tests: `tests/test_swi3score.py` (asserts write/commit counts — will change) and
  `native/.../test/test_simulation_roundtrip.cpp` (the decode-back-to-sine oracle;
  extend to verify continuity across the 8→16 boundary and PDM).

## Decisions (from product)
- **Always 1 s** default (accepts a few extra seconds of startup decode).
- **Widen PDM to 2 cols** in 16-col to preserve 3.072 MHz.

## Risks
- Decode cost of ~24.6 M UIs at startup (demo is preloaded).
- Bit-exact roundtrip across two geometry changes — verify scrambler reset + audio-index
  continuity match the decoder.
- Confirm the core handles a 2-column PDM port (2 samples/row) and 16-bit PCM as emitted.
