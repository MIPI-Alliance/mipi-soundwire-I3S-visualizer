# SWI3S Studio — functional diff, v3.0.7 → v3.0.10

What changed for a user of the app between the **v3.0.7** tag and the current
**3.0.10** development head. Features, menu/UI changes and fixed defects only — no code
diff. Two releases landed in between (**3.0.8**, **3.0.9**, **3.0.10**).

Source: the in-repo changelog (`swi3s_studio/__init__.py`), the 75 commits in
`v3.0.7..HEAD`, and the menu definitions at each tag.

Native decode ABI: **`score_abi` 5 → 7**.

---

## 1. New capabilities

### Flow control, end to end (3.0.9)

The headline addition. SWI3S flow control (TX_PRESENT / DRQ / the Flow Control Port) is
now decoded, validated and visualised.

- **Decode gates on TX_PRESENT.** A sample is emitted only when its TX_PRESENT bit read
  1, so a jittered/gated transport de-jitters back to a bit-exact audio stream. The
  descrambler LFSR still advances on skipped opportunities.
- **Handshake validation.** The FCP is re-driven during decode to read the DRQ bit off
  the wire and check the DRQ↔TxPresent relationship (§14.2.2 {ASW5205}: RX bijective,
  ASYNC data-only-if-requested) through the `d = FlowControlDelay + 1` pipeline. Results
  surface via `Decoder.flow_control_stats()`.
- **New demo capture** — four peripheral data ports, one per flow mode (NORMAL /
  TX_CONTROLLED / RX_CONTROLLED / ASYNC), with audio sampled uniformly but transport
  jittered on a 2×-oversampled opportunity grid.
- **Grid rendering** for DRQ / FCP guard-tail cells (a wide SOURCE DRQ drives every UI;
  a wide SINK DRQ occupies only its last-UI sample point).
- **Audio view** plots flow-controlled streams at the *measured average* rate — what a
  receiver FIFO clocks out — rather than at bus transport time or the oversampled
  opportunity rate.

### Partial DLV (PHY3) captures (3.0.8)

A mid-stream differential capture with **no §5.1.2 cold start** is now auto-detected and
decoded, complementing the existing FBCSE partial-capture support:

- complementary-pair detection (data == NOT clock) routes the capture through the
  recovered-clock DLV path;
- true sample rate taken from the timestamp *quantum* (an edge export's finest spacing
  is one UI, not one sample — the old min-spacing guess under-reported it);
- Safe-Lock column count blind-detected by which framing yields CRC-valid commands;
- **DP/DN probe swap auto-corrected** — both polarities are tried and the pair flipped
  when the swapped orientation is the one producing CRC-valid commands;
- detection runs for **every** source (.sal / .bin / CSV / workspace reopen), so
  re-importing an exported partial-DLV capture decodes like the original.

### Timing Calculator covers PHY1 (3.0.8)

A PHY selector in the top bar switches the specification between PHY1 (FBCSE-slow) and
PHY2 (FBCSE-fast). PHY1 gets its slower Peripheral output-hold / high-Z-recovery and
Manager high-Z-recovery times (Table 129) and its lower mandatory F_CLK (Table 126:
6.6 MHz max, 6.4 target). The Specification column is now **read-only** for both PHYs
(it shows the spec's own min/max); only the Example column is editable. The selected PHY
persists in the workspace.

### Capture data-out (3.0.8)

- **Unified Export Capture dialog** — the separate Export .sal / .bin / CSV menu items
  became one dialog: pick format, which signals and their names, the range (whole
  capture, or a time / bus-row / UI window), and the output file in one place. Ranges
  slice a rebased sub-capture.
- **.sal export rewritten for Logic 2 compatibility** — version-3 internal blobs and a
  schema-complete version-22 `meta.json`. Earlier exports failed to open in Logic
  ("an older version" / "file schema is invalid"). Files still round-trip through Studio.
- **Export .bin** (documented version-0 `<SALEAE>` blob per channel) and **Export CSV**
  (Logic-2 digital CSV, a row per transition).

---

## 2. Menu and UI changes

### File ▸ Analyzer

| v3.0.7 | 3.0.9 (released) |
|---|---|
| Load **&Demo Capture** ▸ PHY1 / PHY2 / PHY3 | Open **&Demo Capture** ▸ PHY1 / PHY2 / PHY3 **+ Flow Control (4 modes)** |
| Open **&Visualizer Config…** | **&Import Visualizer CSV…** (renamed) |
| **&Save Visualizer Config…** | **&Export Visualizer CSV…** (renamed) |
| Open Bus Grid in Visuali**z**er | **View** Bus Grid in Visuali**z**er (renamed) |
| Save Bus **&Image…** | Save Bus Grid **&Image…** (renamed, moved under View Bus Grid) |
| Export **&.sal…** | **Export &Capture…** (unified dialog, grouped with Open) |
| &Locate Sub-Capture… (lower in menu) | &Locate Sub-Capture… (grouped with Open) |
| &Clear Comparison | *(moved out of the menu — now a **Clear Compare** button beside Clear Edits in the Register pane)* |

### Keyboard

| Action | v3.0.7 | now |
|---|---|---|
| Move SSP −1 Row | `Ctrl+,` | **`Ctrl+K`** |
| Move SSP +1 Row | `Ctrl+.` | **`Ctrl+L`** |

`⌘,` is macOS-reserved (Preferences), so its glyph never displayed.

### Other UI

- **Dock title bars** are a themed custom widget — close **X** flat and on the left,
  matching macOS; the tabified-dock tab-bar base is flattened so no pale native strip
  shows behind tabs or buttons.
- **Timing Calculator top bar** split to two rows so nothing clips; the Analyzer Timing
  hold-plot regained its grid.
- **Register pane**: wider device picker.
- **TX map / Persistence** show *"No PHY Selected"* in a cold start's pre-audio region
  (matching the config layout) instead of rastering the single-ended bring-up into a
  bogus 2/4-column grid.
- **Bus Grid** draws the full operational DLV frame (Sync1 at Column 0, CDS at Column 2,
  Sync0 last) even with no NumColumns commit on the wire.
- **Visualizer** shows the operational DLV frame for a partial-DLV capture instead of
  "no bus config".

---

## 3. Defects fixed

### Decode correctness

- **Mid-phase SPM re-framing (affects ANY capture).** A detected K.28.7 SPM no longer
  re-frames or abandons the phase in progress; per §7.2.2 {ASW1907} / §8.2.1 {ASW2206}
  an SPM only *arms* header detection, and mid-phase framing is held by counting bits
  mod 10. Previously an uncontrolled Write/Read payload byte — or the CRC, which no
  encoder can constrain — that happened to spell the 10-bit comma at a sub-symbol offset
  would desync the frame and silently drop the command.
- **TX_PRESENT in RX_CONTROLLED.** TX_PRESENT is present in RX_CONTROLLED too, per
  {ASW5203} (previously omitted).
- **Flow-controlled audio reconstruction** corrected, and the Safe-Lock-2 → 16 column
  ladder is presented properly for the flow-control demo.

### Visualizer placement

- **False handover clash.** A handover landing on a different-device SINK bit is no
  longer flagged as a bus clash — a sink drives nothing, so only a SOURCE driver can
  clash.
- Sink data ports in RX_CONTROLLED / ASYNC now draw their DRQ-driven handover.

### UI / interaction

- **Timeline cursor cascade** — the heavy half (register replay, CDS-symbol and
  Decoded-Samples rebuilds, bus-grid render; ~150 ms on a large capture) is coalesced
  behind a single-shot timer, so clicking around the timeline collapses to one rebuild at
  the resting cursor instead of stacking one per click.
- **Visualizer config guards** — applying a config CSV to a capture that reconfigures
  mid-stream now warns first (a single config can only match one region, misframing the
  others); a non-config CSV picked by mistake is rejected with a message instead of
  silently imposing an empty config and a blank grid.

---

## 4. Performance

Measured on user-reported large captures; all changes output-preserving (bit-identical
or within DSP tolerance against the goldens).

| Area | Before | After |
|---|---|---|
| Full open, 89 MB / 420 M-edge PDM `.sal` | 87 s | **~16 s** |
| Cold/Warm-start bring-up detection | ~26 s | **~0** (bounded edge prefix, not a whole-capture scan) |
| TX-map persistence | ~88 s | **~9 s** (scans data edges via bincount, not every UI) |
| Saleae v3 delta decode | Python loop, ~11 GB transient | **C++ core**, ~8× faster, no transient |

Also: TX-map persistence runs on a worker thread for large regions (the grid no longer
freezes; cached regions render instantly); audio resampling is a bounded polyphase
decimator instead of a whole-capture FFT (a minutes-long PDM stream no longer builds
multi-GB temporaries); Symbols/Registers panes bulk-fill without a per-item repaint
storm; register-map, commit-marker, filtered-command and CDS-symbol lookups on a cursor
move are cached and bisected rather than rebuilt or linearly scanned; Statistics
measurements build lazily on first show.

**Process** (3.0.8): the perf/UI regression loop is formalised in `docs/PERFORMANCE.md` —
CI cliff-detectors on a large synthetic capture, release-time interaction profiling, and
a UI shots checklist. Decode multi-threading was scoped and **deferred**
(`docs/decode_multithreading.md`).

---

## 5. 3.0.10

Large-capture openability and three user-reported rendering/placement bugs, plus the
test-infrastructure pass that closed the hole which let one of them ship.

- **Audio waveform gap rendering.** Two regions of decoded audio separated by a stretch
  with no decoded samples are no longer joined by a straight line implying audio that was
  never on the bus; envelope bins that straddle such a gap are split so the waveform
  matches the Samples tab at the boundary.
- **Large-capture memory guard + windowed load.** A `.sal` too large to open is refused
  *before* allocating (the estimate reads the zip directory only), with the option to open
  a time window instead; a windowed open decodes only the requested span by seeking the v3
  block chain. On the reported 291 MB / 2.25 bn-transition capture — which needs ~20 GB to
  open whole — a 2 s window opens in ~3 s at ~1.4 GB.
- **Channel-group spacing fix** — spacing no longer leaks across a row boundary, which
  had shifted the next row's first data UI one column late. Fixed in **both** placement
  engines (the C++ core and the vendored swviz that renders the Visualizer tab); a new
  `spacing_overflow` directed test covers the last-column geometry no existing config
  could reach. See `docs/spacing_bug.md`.
- **Quitting mid-load no longer aborts the app** — `closeEvent` joined only the
  TX-persistence worker, leaving the capture-load and sub-capture-search threads running;
  Qt aborts when a running `QThread` is destroyed.
- **CDS symbol pane: per-cell selection and copy**, matching the Command table — pick one
  cell or drag a range instead of only ever copying whole rows.
- **Demo menu relabel** — PHY1 (FBCSE) / PHY2 (FBCSE) / PHY2 (Flow Control) / PHY3 (DLV),
  with flow control ordered next to its PHY rather than in a separate section.
- **Idle CDS pattern** in the demo synthesiser is the D10.2 0101 sequence per {ASW2201}
  rather than all-ones (which NRZS-held the FBCSE bus flat — a dead line where a real bus
  shows the idle zebra).
- **Test infrastructure.** `tests/run_all.sh` ran each suite as a script, so 60 tests were
  invisible to it and six files ran nothing while reporting `ok`. It now delegates to
  pytest (coverage equal to CI by construction), a new `tests/test_collection.py` fails
  the build on any file that collects zero tests, and 63 vestigial `__main__` blocks are
  gone.

---

## 6. Also worth knowing

- **Developer/CI** (from 3.0.7, extended since): test matrix on ubuntu/windows/macOS with
  goldens, a perf-regression gate, ruff/mypy/coverage, and a tech-debt register
  (`docs/TECH_DEBT.md`).
- **Cross-engine parity.** Studio runs two placement engines (the C++ decode core and the
  swviz authoring engine). 3.0.9 added a full-corpus cross-engine parity test including
  FCP placement, plus a direct placement guardrail so the two can't drift silently.
- **Known deferred:** a `Figure_153` TAIL cross-engine mismatch is tracked rather than
  fixed (`docs/TECH_DEBT.md`).
