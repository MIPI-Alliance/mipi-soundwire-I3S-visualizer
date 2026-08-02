# Saleae capture file formats — investigation & conclusions

Reverse-engineering notes for the Saleae digital capture formats that SWI3S Studio
ingests, why the version-3 `.sal` parser was failing, and the recovered v3 delta
encoding — now fully decoded and implemented. Written against Logic 2 captures
(project `meta.json` version 22) used in the SWI3S PHY2 analyzer work.

Relevant code: `swi3s_studio/ingest/saleae_binary.py` (v0 + v3 blob parsing) and
`swi3s_studio/ingest/saleae_sal.py` (`.sal` project reader).

---

## 1. Scope

Two distinct Saleae formats matter here:

| Name | Where it comes from | `type` field | Payload |
|------|---------------------|--------------|---------|
| **v0 "Binary export"** | Logic 2 -> *Export -> Raw Data -> Binary* (one file per channel) | `0` | absolute transition **times** (float64 seconds) |
| **v3 "internal"** | the `digital-N.bin` blobs **inside** a `.sal` project ZIP | `100` | per-transition sample **deltas**, compressed |

Both blobs begin with the 8-byte identifier `"<SALEAE>"` followed by `u32 version`,
`u32 type`. The documented, stable format is **v0**; **v3** is Logic's undocumented
internal representation and differs between Logic versions.

---

## 2. v0 export — verified byte-exact

`saleae_binary.parse_channel` reads:

```
offset  type      field
0       char[8]   "<SALEAE>"
8       i32       version        (== 0)
12      i32       type           (== 0, digital)
16      i32/u32   initial_state  (0=low, 1=high)
20      f64       begin_time     (seconds; negative = pre-trigger)
28      f64       end_time
36      i64/u64   num_transitions
44      f64[num]  transition_times (absolute seconds)
```

The state before the first transition is `initial_state`; each subsequent time is a
level flip.

**Verification.** Ran Saleae's own reference parser (`struct '<ii'` for version/type,
`'<iddq'` for `initial_state, begin, end, num`, then `array('d')` of times) against
our reader on a real 8.66 M-transition capture:

```
reference: initial=0 begin=-2.456743680 end=5.093003520 num=8664552
ours     : initial=0 begin=-2.456743680 end=5.093003520 num=8664552
first-5 transition times: identical
```

**Conclusion: the v0 importer is byte-exact correct.** The only difference from the
reference is that we read `initial_state`/`num` as unsigned (`I`/`Q`) vs the
reference's signed (`i`/`q`) — immaterial for valid files (both are non-negative).
Since our production `.bin` captures are v0, capture *import* is not a source of any
decode discrepancy.

---

## 3. v3 `.sal` internal — the problem, and the solution

The previous `parse_channel_v3` was reverse-engineered from an older Logic (Logic 8)
and assumed:
- the delta block starts at a **fixed offset 61**, and
- deltas are **single bytes** (1-255).

On Logic 2 (meta v22) files it failed immediately (`bad chunk header at offset 82`),
because **both assumptions are wrong**: there is a variable-length metadata region
before the data, and the deltas are a multi-byte code.

### 3.1 Method: a ground-truth verification loop

Logic can export the *same* capture as **Raw Data -> CSV** (one row per transition,
with every channel's state):

```
Time [s],Channel 0,Channel 1
-3.772315696,1,1        <- initial state
-0.004184948,1,0        <- a transition on Channel 1
-0.004184860,0,0        <- a transition on Channel 0
...
```

Per-channel transition **sample numbers** are then `round(time * sample_rate)` (with
`sample_rate = 250 MHz` here). This gives an exact oracle to validate any proposed
v3 decode against.

Example pair used: `ColdStart_768kHz_2col_Ping0x3.sal` + `ColdStart_768kHz_2col_Ping0x3.csv`.

### 3.2 v3 header layout (recovered)

```
offset  type    field
0       char[8] "<SALEAE>"
8       u32     version         (== 3)
12      u32     type            (== 100, digital)
16      u8      == 1            (constant flag, NOT the initial state)
17      f64     sample_rate     (e.g. 250000000.0)
25      u64     capture_unix_ms (matches meta.json captureStartTime)
33      f64     fractional_ms   (matches meta.json)
41      u8      preamble flag   (0 for a simple single-region capture)
42      u64     block_count × 256   (flag==0 form; Logic reads nblocks = u64 // 256)
..      u8      == 0            (preamble terminator)
..      ...     transition-block chain (see 3.4) — the initial state lives here
```

The preamble at offset 41 is **variable-length**: verified block offsets are 51
(flag 0, one u64: ColdStart, flareoketo, diags), 59 (flag 1, two u64: guard_en) and
67 (flag 1, three u64: the large PDM capture). For the simple **flag==0** form the u64 is
the **block count × 256** — exact across every sample (818×256 = 209408, 412×256 =
105472, 1×256 = 256); Logic reads `nblocks = u64 // 256` to know how many transition
blocks follow. The multi-u64 (flag 1) form carries trigger/trim region sample
positions instead. The fixed metadata (sample rate + capture timestamp) is
capture-global — identical across a capture's channels. Because the preamble length
varies per file, the reader **scans** for the block chain rather than assuming a
fixed offset.

**Writing.** The exporter emits the flag==0 form: it chunks the transition stream
into blocks (real busy channels chunk into 800+; a single giant block or a zero
block count makes Logic stall at "Preparing session") and sets the u64 to
`nblocks × 256`. See §3.5.

### 3.3 Delta codec (the key finding — cracked & validated)

Each transition is stored as a **big-endian base-128 varint of `(delta - 1)`** (delta
in samples), with a per-byte tag identifying position within the code:

- **MSB (first) byte:** `digit + 0x40`
- **interior bytes:** `digit + 0x80`
- **final byte:** raw low digit, `< 0x80` (a byte `< 0x40` on its own is a whole
  small delta)

Decode:

```
val = 0
b = next_byte
if b < 0x40:                 # single-byte code
    delta = b + 1
else:
    val = b - 0x40           # most-significant digit
    loop:
        b = next_byte
        if b >= 0x80: val = val*128 + (b - 0x80)   # interior digit
        else:         val = val*128 + b; break     # final digit (< 0x80)
    delta = val + 1
```

Worked examples (byte sequence -> delta):

| bytes | computation | delta |
|-------|-------------|-------|
| `41 22` | `(0x41-0x40)*128 + 0x22 + 1` | 163 |
| `45 03` | `5*128 + 3 + 1` | 644 |
| `7a 39` | `58*128 + 57 + 1` | 7482 |
| `41 92 20` | `1*128^2 + 18*128 + 32 + 1` | 18721 |

### 3.4 Block / chunk framing (fully decoded)

The transition data is a chain of blocks. Each block:

```
u64 A_start   cumulative sample position (from capture start) at the block's first edge
u64 B_end     cumulative sample position at the block's last edge
u16 level     line level at the block's start
u64 byte_count length of the delta-codec run that follows
<byte_count bytes of the base-128 delta codec>
```

- Blocks **chain**: `A_start == previous B_end`. A block's decoded deltas sum to
  `B_end - A_start`. The first block starts at sample 0.
- **Initial state** = the `level` field of the **first** block. (The earlier
  "byte 16" guess was a coincidence — that byte is `1` regardless; the real initial
  is here.) This matters: an inverted *clock* initial flips every UI's rising/falling
  parity and the decode collapses — e.g. a 16-column audio capture mis-frames to 2
  columns with zero valid commands.
- **Transitions** = `cumsum(deltas)[:-1]`; the last delta is the gap to capture end
  and is dropped.
- Because the metadata header pushes the first block to a file-dependent offset, the
  reader **locates it by scanning** for the self-consistent chain (first
  `A_start == 0`, deltas sum to `B - A`, and the chain tiles the blob to EOF).

### 3.5 Writing a `.sal` Logic 2 will open (reverse-engineered by iteration)

`sal_export.export_sal` writes a project Logic 2 opens natively. Getting there meant
clearing four gates in order — each surfaced as a *different* Logic error/behaviour,
so the error message is the signal for which gate you're on:

| Logic symptom | Cause | Fix |
|---|---|---|
| "an older version … could not be opened" | v0 blobs | write **v3** blobs (`type 100`) |
| "file schema is invalid" | partial `meta.json` | emit all **16** `data` keys of a real `version: 22` (see below) |
| "Failed to load file" | missing member | add **`trigger-store.bin`** |
| stalls at "Preparing session" | block count wrong | chunk into blocks; preamble u64 = **`nblocks × 256`** |
| "This Capture Contains Simulated Data!" banner | `isSimulation: true` | set `legacyDevice.isSimulation = false`, `isPhysicalDevice = true` |

The ZIP must contain: `meta.json`, one `digital-N.bin` per channel, and
`trigger-store.bin` (a fixed 32-byte `<SALEAE>` v3 **type 103** blob: identifier,
version 3, type 103, a `1` byte, 15 zero bytes — byte-identical across every real
capture, independent of trigger settings).

`meta.json` (`version: 22`) needs all 16 `data` keys a real capture has:
`renderViewState`, `captureStartTime`, `timingMarkers`, `measurements`,
`highLevelAnalyzers`, `analyzers`, `rowsSettings`, `captureSettings`, `legacyDevice`
(+`capabilities`), `legacySettings`, `digitalTriggerTime`, `name`, `dataTable`,
`analyzerTrigger`, `timeManager`, `captureNotes`; plus top-level `binData`
(`legacyDeviceCalibration` is optional — one real file omits it). The capture's
sample rate must appear in `legacyDevice.capabilities.sampleRateOptions` (Logic
cross-checks `legacySettings.sampleRate.digital` against it), so the exporter injects
the capture rate into the stock rate menu.

Each `digital-N.bin`: the §3.2 header (flag==0 form) with the preamble u64 =
`nblocks × 256`, then the block chain (§3.4). Blocks are chunked at
`_V3_BLOCK_DELTAS` transitions each (real busy channels chunk into hundreds; one
giant block or a zero count stalls Logic). Every block's `level` is the true line
state at its first delta (`initial ^ (start_index & 1)`); block 0's level is the
channel's initial state. Both channels fill their trailing delta to **one shared
capture-end sample** so Logic sees a single timeline length. The result also
round-trips through `parse_channel_v3` / `saleae_sal.load_capture`; `tests/
test_sal_export.py` walks the chain and asserts `nblocks == u64 // 256`.

---

## 4. Conclusions

1. **v0 export import is correct** (byte-exact vs Saleae's reference) — the stable,
   documented path.
2. **The v3 format is fully decoded and implemented** in `parse_channel_v3`
   (`build_channel_v3` is the matching writer used by the round-trip tests): the
   base-128 delta codec, the block chain, and the initial state from the first
   block's `level`. Validated to the **sample** against a Logic CSV export — 100 %
   on both channels of `ColdStart_768kHz_2col_Ping0x3` (3.4 M + 14 M transitions) —
   and every project in the reference set decodes to a clean, strictly-monotonic
   edge stream that the analyzer frames correctly (the 16-column IV/PDM captures
   decode identically to their `.bin` exports).
3. **`.sal` export opens natively in Logic 2.** `sal_export.export_sal` writes v3
   blobs + a schema-complete `version: 22` `meta.json` + the `trigger-store.bin`
   member, with the preamble block count and chunked blocks Logic needs — see §3.5
   for the full recipe and the four error gates it clears (older-version / schema /
   failed-to-load / preparing-session), plus `isSimulation:false` to drop the
   simulated-data banner. `tests/test_sal_export.py` pins the layout + the required
   key set; the export still round-trips through `saleae_sal` in-package.
   (Round-trippable data-out that doesn't depend on the internal format:
   `raw_export.export_bin` / `export_csv`.)
4. The app now imports `.sal` directly (**File ▸ Open Capture (.sal / .csv / .bin)**);
   the forwarded clock is auto-selected as the busier channel, matching the
   `.bin`/`.csv` paths, and the source round-trips through the workspace.

## 5. Recommendations

- Keep the **CSV verification loop** for testing against new Logic versions (decode a
  `.sal`, compare per-channel transition samples to `round(csv_time * sample_rate)`,
  require an exact match) — the internal format is undocumented and may change.
- **v0 Binary export** remains the most portable ingest path; `.sal` import is now a
  first-class convenience on top of it.
- Pure-Python v3 decode of the largest captures (~100 M transitions) takes ~15-20 s;
  it runs on the async load path (busy dialog), acceptable for a one-time import. A
  C++/vectorised decoder is a possible future optimisation.

## 6. Reference material

- Example captures (local verification set, not distributed):
  `ColdStart_768kHz_2col_Ping0x3.sal` + `.csv` (verification pair), plus the
  local IV/PDM device captures.
- Sample rate for these captures: **250 MHz**; 2-column cold-start UI rate 1.536 MHz
  (row rate 768 kHz x 2), 16-column operational UI rate 12.288 MHz.
- Reference decoded audio (tones): IV sense = 1234 Hz; PDM mics L = 440 Hz, R = 997 Hz
  (from the `swi3s_ivsns_*` / `swi3s_mic_rec_*` wav filenames).
- Tests: `tests/test_ingest_formats.py` (v3 encode/decode round-trip, single- and
  multi-block; malformed -> `MalformedSaleaeV3`; `.sal` version handling).
