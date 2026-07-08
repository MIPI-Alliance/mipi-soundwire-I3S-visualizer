# SWI3S Studio — User Guide

A pane-by-pane reference. For setup and build see the [README](../README.md);
for design, [architecture.md](../architecture.md).

## Modes

A switcher at the top of the window selects one of three modes. They share one
window, one bus-grid renderer, and one workspace file.

- **Visualization** — plan a bus. Author an Interface + up to 12 data ports in a
  table; the Rows×Columns grid re-places live and a notifications list flags clashes
  and spec violations. Load/Save the config as CSV; export the placed frame as JSON;
  push it into Analysis as the *expected* config (**Compare ▸ Compare with Visualizer**).
- **Timing** — dial in the PHY. Enter bus length, per-lane slew, supply/noise, and
  output/input/Z-handover timing; read the four MP/PM setup/hold inequalities term by
  term with margins, F_max, and the binding constraint (optionally at each
  inequality's worst PVT corner).
- **Analysis** — confirm it works. Decode a captured PHY2 bus and explore it in the
  linked panes below.

All Analysis panes share **one time cursor**: selecting a command, clicking the grid
or a waveform, scrubbing the timeline, or picking a symbol moves the cursor, and every
pane follows.

## Analysis panes

### Command Table
One row per decoded Command Transport command. Columns: **Row** (the bus row of the
command's opening SPM comma), **Time**, PhaseID, Devices, Packet Length, Opcode,
Address, **Register** (address resolved to register/field names, e.g.
`SLC.NumColumns`, with symbolic enums), Data, Group Mask, CRC, **Response**. Responses
decode to symbolic names labelled peripheral vs manager (`Periph: WRITE_OK`,
`Mgr: CONFIRM_COMMIT`); a Ping breaks out per device, collapsing runs into ranges.
CRC errors, empty masks, and error responses are highlighted. Filtering is in the
**Filter menu** (expression search with `and`/`or`/parens; multi-select Commands /
Devices / Groups; Errors Only). Right-click the header to show/hide columns.

### Register Map
Per-device register state as-of the cursor: SLC, CDS, each active data-port block, the
active PHY, and any imported peripheral (vendor) registers. Values are coloured by
**provenance** (inline key): Cold Reset / Bus Write / Bus Read / CSV Import / Manual
Edit. Dual-ranked registers show a staged **(NEXT)** row and a committed **(CURR)**
peer. **What-if editing:** right-click (or double-click) a register → *Edit fields…*
(dropdowns for enums, bounded number boxes otherwise) or *Force value…* (raw byte).
The edit re-decodes so grid, registers, and audio all reflect it; *Clear all manual
edits* reverts. A dual-ranked write takes effect at its **commit**, not where it was seen.

### Bus Grid
The 2D Rows×Columns bus laid out like the Visualizer, as-of the cursor; it fills in as
config is written, and per-section geometry follows a mid-stream reconfiguration. Data
cells are coloured per **(device, data-port)** with channel labels and sample-start
markers; guard/tail/TxPresent/DRQ slots are labelled; a legend keys the ports and
slots. Rows are 0-based. **Rows To Draw** sets the grid height.

**Show Toggles** replaces the layout with the **TX map** — a raster marking every UI
that carried a data-line transition (green `TX`). It needs no decoded config or CSV,
so it shows *where data is on the wire* on a raw capture: idle columns stay blank,
transporting columns fill, an interval/skipping port shows row-periodic gaps. Scroll
(bar or wheel) moves the window across the whole capture. **Persistence On** collapses
the drawn rows into one slice — a column fills if it *ever* toggled within them — so
active columns read as solid bars.

### Decoded Audio
One stacked waveform per **(device, data-port, channel)**, from a min/max pyramid so
pan/zoom stays fast over millions of samples. A **PDM** port (1-bit sample size) is
decoded from its bipolar density stream to PCM. Channel checkboxes (colour-matched to
each waveform) show/hide tracks without resetting zoom; a cursor line tracks the shared
cursor. **Click a waveform to seek**; **▶ Play** streams the first selected port to the
system output; **Jog < / >** page one screen. Output device, bit depth, and per-port
decimation are in the **Audio menu**.

### CDS Symbols
The Control Data Stream as classified 8b/10b symbols: **Row**, Time, codeword, **RD**
(running disparity; `!` = violation), Kind (comma / robust-token / D-code / K-code /
NO_RESPONSE, colour-coded), decoded value, and **Meaning** (the symbol's role in its
Command Transport phase — `PhaseID: WRITE`, `Address[31:24]`, `Manager_CRC16_H`).
Follows the cursor via windowed re-decode; selecting a command scrolls its comma to the
top. **Scroll up** to decode-and-prepend earlier history across config sections.

### Decoded Samples
Each reconstructed audio sample as a row: its **MSB** bus row, Time, the port that
carried it, and the value in binary / hex / signed decimal. Windowed around the cursor;
click a row to seek. Filter by **data port** and **channel** (multi-select) and by a
**value predicate** (e.g. `Decimal ≤ 0`).

### Raw Capture
The two physical link lines (DP, DN) as digital waveforms, straight from the capture's
edges — no protocol interpretation. Overlays (toggleable): **CDS Row-Sync-Point**
markers (amber — the rising clock edge opening each Column 0), **Commit** RSPs (white),
and colour-coded **per-bit sample points** per active data-port channel. **Hide Clock**
drops the forwarded-clock trace and rescales the data to fill. **Jog < / >** page one
screen. **1 Row / 2 Rows** zoom snaps to Row-Sync edges; max zoom-in is ~1 UI.

### Timing
Measured bus **setup/hold** from the raw clock/data edges: per-polarity histograms (DDR
samples both edges), an **eye** of data-edge phase within the UI, a margin verdict, and
the **UI period per config section** (median / min / max, since the UI can change
mid-capture). Aggregate over the capture — no time cursor.

### Statistics
Derived metrics: clock / row rate, per-dataport SSP intervals and bandwidth, the
bus-config segment summary, and link `PM_Action` events.

### Timeline ribbon
A strip across the whole capture: command marks coloured by kind, translucent
**bus-config bands** by column count (so a `2col → 8col` cold-start reconfiguration is
visible at a glance), and the cursor. Click to seek; scroll up/down to zoom, left/right
to pan. **Bookmarks** (Ctrl+B / Ctrl+] / Ctrl+[) mark and step through points.

## Partial captures & finding the SSP

A **partial capture** starts *after* the bus was configured — the setup
`WriteA32`/commit sequence and the Stream Sync Point (SSPA/SSCR) are not on the wire.
Two things are then missing:

1. **Port geometry** — the decoder can't snoop it. Supply it with **File ▸ Open Capture
   with Config CSV…** (or **Apply Config CSV to Open Capture…** for a capture already
   open). The CSV's data-port config drives the decode from row 0, so audio
   reconstructs. Use the **TX map** (Bus Grid ▸ Show Toggles) to see which columns
   actually carry data and confirm the CSV matches the wire.

2. **The SSP** — a data port with **interval > 1** transports on 1 of N rows; the SSP
   marks which (`row_in_interval == 0`). Without it, the port decodes at an arbitrary
   phase — only 1/N rows land right and the audio is garbled. Pick it by ear:

   - **Audio ▸ Manual SSP Move ▸ Set SSP at Cursor Row**, then **Move SSP +1 / −1 Row**
     (Ctrl+. / Ctrl+,) to step the phase until the audio is clean. The phase repeats
     every interval, so stepping by 1 cycles all N.
   - **Clear Manual SSP** returns to the default anchor.
   - Manual SSP only re-phases audio when a config CSV is applied (it needs the port
     geometry); on a capture with no decodable audio the status bar says so.

The applied CSV and the chosen SSP row are saved in the workspace and restored on reopen.

## Menus

- **File** — Load Demo Capture; Open Capture (`.sal` / digital `.csv` / a `.bin`
  channel pair); Open Capture with Config CSV…; Apply Config CSV to Open Capture…;
  Open/Save Workspace; Export Audio as WAV; Export Commands as CSV; Export Bus Grid
  (SVG/PNG); Export Bus Grid as Visualizer Settings (CSV). The loaded file shows in the
  window title; the open dialog reopens in the last-used folder.
- **Compare** — diff an expected config (CSV, or the in-app authored config) against the
  decode: outlines differing grid cells, a per-cell report, and expected register values
  overlaid in the register map (CSV-Import colour).
- **Devices** — Manage Devices (per-device display names, vendor register maps, hub depth).
- **Audio** — Per-Dataport Scrambler override (Auto / On / Off); **Manual SSP Move**
  (see above); Block PDM DC Bias; Export Audio as WAV; Playback Output Device / Bit
  Depth / Decimation.
- **Bookmarks** — Toggle / Next / Previous / Clear (Ctrl+B / Ctrl+] / Ctrl+[).
- **View** — show/raise any pane; Appearance (Dark / Light / follow System).
- **Filter** — expression search, multi-select Commands / Devices / Groups, Errors Only.
- **Help** — Timeline Marks Legend.

## Row / Time reference points

Table Row/Time columns mark the **start** of an entity, so a cursor sample lines the
panes up: a Command Table row is the command's opening SPM comma (the row's rising-edge
Row Sync Point); a CDS Symbols row is the first of the symbol's 10 bits; a Decoded
Samples row is the sample's MSB. Bus rows are **0-based** throughout (the first row is
0). The register map reflects the committed state as-of the cursor.
