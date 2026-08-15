# SWI3S Studio — User Guide

A pane-by-pane reference. For setup and build see the [README](../README.md);
for design, [architecture.md](architecture.md).

## Modes

A switcher at the top of the window selects one of three modes. They share one
window, one bus-grid renderer, and one workspace file.

- **Visualization** — plan a bus. Author an Interface + up to 12 data ports in a
  table; the Rows×Columns grid re-places live and a notifications list flags clashes
  and spec violations. The **Control Data Stream** (Column 0) is configured **per
  source** — an independent guard (Guard 0 / Guard 1 / Off), tail width, **drive type**
  (Normal / Special) and **end-drive-early** (Full / Early) for the Manager and each
  device, all reached through one **CDS Settings** dialog — rendered as one full-height
  universal symbol when every source shares it and split into labelled **M/P** glyphs when
  they diverge. Where a source departs from the ordinary case the CDS cell annotates it on a
  second line (`SP` for a Special drive type, `EDE` for an early release; a trailing `x`
  means the sources disagree).
  Load/Save the config as CSV; export the placed frame as JSON; push it into Analysis as
  the *expected* config (**File ▸ Analyzer ▸ Import Visualizer CSV**, choosing the
  current authoring, to compare and/or impose it).
- **Timing** — dial in the PHY. Pick a spec source **per side** (Manager and Peripheral
  independently): SWI3S **PHY1** or **PHY2**, the two **proposed** PHY2 revisions, or
  **SoundWire 1.3** at 1.8 V / 1.2 V — so a mixed SWI3S ↔ SoundWire bus is expressible, and
  the cross-spec divergences it cannot reconcile are surfaced as caveats rather than hidden.
  The **Specification** column is read-only; only the **Example** column is editable. Enter
  bus length, per-lane slew, supply/noise, and output/input/Z-handover timing, and choose
  the Manager's **data launch** (analog delay, or a 2× / 4× clock grid), whether a
  **handover UI** is allocated, and where the two peripherals sit for the **P→P** legs.
  Read **17 numbered inequalities** term by term with margins, F_max and the binding
  constraint, each evaluated at its own worst PVT corner: setup and hold for
  Manager→Peripheral, Peripheral→Manager and Peripheral A→Peripheral B (each in both launch
  forms, `t_DD` and the handover's `t_ZD`), the three handover non-contention legs, and the
  bus keeper per releasing device. Titles name who owes what to whom — *Manager Holding for
  Peripheral*, *Peripheral A Setup for Peripheral B* — and the terms read in physical time
  order, so a row can be followed from the clock edge to the receiver's window.
  **Click any term** — a symbol or a substituted number — to highlight that parameter
  everywhere it acts: every inequality that reads it, both substitution rows, and its input
  row above. Click it again, or any blank space, to clear; several can be traced at once.
- **Analysis** — confirm it works. Decode a captured PHY1/PHY2 (forwarded-clock) or
  PHY3/DLV (recovered-clock) bus and explore it in the linked panes below. Entering this
  mode with nothing open decodes the synthetic demo capture (a progress dialog, a second
  or two) so there is something to explore; open a real capture and it is replaced.

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
CRC errors, empty masks, error responses, and an **EnableCh _CURR write while the
port's Interval ≠ 1 Row** (a protocol violation — a direct write to the committed
channel-enable rank is only safe when every row transports) are highlighted; the
status-bar error count and the **Errors Only** filter agree with the shading.
Filtering is in **Commands ▸ Filter** (expression search with `and`/`or`/parens;
multi-select Commands / Devices / Groups; Errors Only). Right-click the header to
show/hide columns. **Commands ▸ Next / Previous SSCR/DSCR Commit** (Ctrl+> / Ctrl+<)
jump the cursor between the sync-point commits where the config takes effect.

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

**Click a data-port swatch** in the colour key to choose what that port's cell labels
show — **Sample**, **Channel**, **Bit**, in any combination (the same fields, and the
same flags, as the Visualizer's port Display Options). The default is Channel|Bit, which
hides the sample index: a port carrying several samples per row draws every cell as
`C0B0`. Turning Sample on separates them (`S0C0`, `S1C0`). Tick **Apply to every data
port** to set them all at once. It only changes labels — no re-decode — and the choice
is saved in the workspace.

**Show Toggles** replaces the layout with the **TX map** — a raster marking every UI
that carried a data-line transition (green `TX`). It needs no decoded config or CSV,
so it shows *where data is on the wire* on a raw capture: idle columns stay blank,
transporting columns fill, an interval/skipping port shows row-periodic gaps. Scroll
(bar or wheel) moves the window across the whole capture. **Persistence On** collapses
the drawn rows into one slice — a column fills if it *ever* toggled within them — so
active columns read as solid bars. In a cold-start's pre-audio bring-up region (before a
PHY is selected) the TX map, like the config layout, shows **No PHY Selected** — there is
no audio-mode column structure on the wire there yet.

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
Measured bus **setup/hold** margins straight from the raw clock/data edges (no decode).
Two log-scaled histograms meet at the sample point — **setup** on the left, **hold** on
the right — with every data transition split into the four **clock-edge polarity × data
direction** flavours (DDR samples both edges); click a colour-key entry to hide/show a
flavour. A **filter** narrows the transitions to any mix of drivers (Manager /
peripheral), data ports, or columns (CDS / Column 0 is off by default). When a commit
changes the bus clock rate mid-capture (e.g. `2col → 16col`) a **region** selector scopes
the measurement to one rate. **View Worst UI** jumps the shared cursor to the tightest
transitions so you can inspect them in Raw Capture. Aggregate over the capture — no time
cursor of its own.

### Statistics
Derived metrics: clock / row rate, per-dataport SSP intervals and bandwidth, the
bus-config segment summary, and link `PM_Action` events.

Rows are grouped into collapsible sections (Link Control / Regions / Commands / Data
Ports) — click a section title to fold it. In **Commands**, each command kind is itself
expandable: click it for the breakdown.

- **Ping** — per peripheral, how many of each response state: `PING_ATTACHED`,
  `PING_ATTACHED_BUSY`, `PING_ALERT`, `PING_ALERT_BUSY`, `NO_RESPONSE`. A device that
  never answered collapses to a single `NO_RESPONSE` line.
- **Reads / Writes** — per device: how many commands, how many **bytes** read or
  written (counted from the payload actually on the wire, so a failed or deferred read
  contributes none), and an error count where there are any.

Folds are remembered, so a cursor move or a re-decode doesn't collapse what you opened.

### Bookmarks
The user's **paired-bookmark measurements** (distinct from Statistics, which is
whole-capture facts). One row per bookmark pair (A, B, …) — the two members and their
**delta** (rows / time) — with each row's text coloured to match its timeline marker.
Click a row to seek the cursor to that pair's left member. Place and step bookmarks with
Ctrl+B / Ctrl+] / Ctrl+[ (Bookmarks menu); pairs form as you add the second mark of a
letter. **Locate Sub-Capture** (below) also drops bookmarks, one per match.

### Sub-capture search
**File ▸ Analyzer ▸ Locate Sub-Capture…** finds every place a smaller reference capture
recurs in the open one and drops a bookmark at each. Pick the reference file(s) — any
format Open Capture accepts, selecting **both** files together for a `.bin` or `.wfm`
pair. Matching is **sample-rate independent** (it compares the transition *pattern* — the
data bits at each clock edge and the clock's gap shape — not absolute sample counts) and
requires a **near-exact** match of *both* the data and the clock pattern, so only genuine
occurrences are reported; it also retries with clock/data swapped in case the reference
was captured with the opposite orientation. When nothing clears the bar, it still
bookmarks the single best candidate and reports its score, so you can judge how close it
came. The search runs off the UI thread with a progress dialog.

### Timeline ribbon
A strip across the whole capture: command marks coloured by kind, translucent
**bus-config bands** by column count (so a `2col → 8col` cold-start reconfiguration is
visible at a glance), and the cursor. Click to seek; scroll up/down to zoom, left/right
to pan. **Bookmarks** (Ctrl+B / Ctrl+] / Ctrl+[) mark and step through points.

## Partial captures & finding the SSP

A **partial capture** starts *after* the bus was configured — the setup
`WriteA32`/commit sequence and the Stream Sync Point (SSPA/SSCR) are not on the wire.
Two things are then missing:

1. **Port geometry** — the decoder can't snoop it. Supply it with **File ▸ Analyzer ▸
   Import Visualizer CSV…** — pick a config CSV (or the current Visualizer authoring)
   and choose **Update** to impose its data-port config on the open capture, and/or
   **Compare** to overlay it in the Register Map. The config drives the decode from row 0,
   so audio reconstructs. Use the **TX map** (Bus Grid ▸ Show Toggles) to see which
   columns actually carry data and confirm the config matches the wire.

   If the grid still shows the wrong number of columns — the wire never carried a
   `NumColumns` commit to snoop, or the blind column detector mis-locked — pin the width
   with **File ▸ Analyzer ▸ Force Column Count…**. It applies to the config region under
   the cursor **only**, so a capture that genuinely changes geometry mid-stream keeps
   decoding its other regions as the wire says. A pinned region is labelled
   `16col (forced)` and outlined in the timeline, so a forced width never looks like a
   snooped one. Enter `0` to remove the pin. Pins are saved in the workspace.

2. **The SSP** — a data port with **interval > 1** transports on 1 of N rows; the SSP
   marks which (`row_in_interval == 0`). Without it, the port decodes at an arbitrary
   phase — only 1/N rows land right and the audio is garbled.

   **If the capture contains an SSPA this is handled for you.** An SSPA announces a
   Stream Sync Point without committing anything, so it pins the phase; Studio adopts the
   first one automatically and applies it **backwards as well as forwards**, so the rows
   *before* the SSPA decode correctly too (the decoder on its own only re-anchors from the
   row it reaches). Nothing to configure — it applies when a config CSV has supplied the
   geometry and no setup commit is on the wire. Setting an SSP row by hand overrides it.

   With no SSPA, pick the phase by ear:

   - **Audio ▸ Manual SSP Move ▸ Set SSP at Cursor Row**, then **Move SSP +1 / −1 Row**
     (Ctrl+. / Ctrl+,) to step the phase until the audio is clean. The phase repeats
     every interval, so stepping by 1 cycles all N.
   - **Clear Manual SSP** returns to the default anchor.
   - Manual SSP only re-phases audio when a config CSV is applied (it needs the port
     geometry); on a capture with no decodable audio the status bar says so.

The applied CSV and the chosen SSP row are saved in the workspace and restored on reopen.

## Menus

- **File** — grouped by the mode each item acts on (opens work from any mode and switch
  to it):
  - **Analyzer** — *Open Capture…* (a Logic 2 `.sal`, a digital `.csv`, a Tektronix
    analog `.csv`, or **both** channel files of a `.bin` or `.wfm` pair, multi-selected);
    *Open Demo Capture ▸ PHY1/2/3*; *Export Capture…* (one dialog — pick the format
    `.sal` / `.bin` / CSV, which signals + their names, the range (whole capture or a
    time / bus-row / UI window), and the output file; handy when the source was a large
    `.bin`/CSV). Then, in their own sections: *Import Visualizer CSV…* (impose a config
    CSV / the authored config on the capture from row 0, and/or compare it in the Register
    Map — clear the comparison with the **Clear Compare** button there) and *Export
    Visualizer CSV…*; *Force Column Count…* (pin the bus column count for the config
    region under the cursor, when the wire's own width is missing or mis-detected);
    *View Bus Grid in Visualizer* and *Save Bus Grid Image…* (SVG/PNG);
    *Locate Sub-Capture…*; *Open / Save Workspace…*.
  - **Visualizer** — *Open / Save Settings…* (the authoring CSV); *Save Image…*; *Save
    Bus Model…* (placed frame as JSON).
  - **Timing** — *Open / Save Settings…* (the PHY timing-calculator inputs, incl. the
    selected PHY).
- **Commands** — *Filter* (expression search; multi-select Commands / Devices / Groups;
  Errors Only); *Next / Previous SSCR/DSCR Commit* (Ctrl+> / Ctrl+<); *Export as CSV…*.
- **Devices** — *Names…* (per-device display names); *Register Maps…* (import vendor maps);
  *Hub Depth…*.
- **Audio** — *Per-Dataport Scrambler…* (Auto / On / Off); **Manual SSP Move** (see above);
  *Block PDM DC Bias*; *Export Audio as WAV…*; *Playback Output Device / Bit Depth /
  Decimation*.
- **Bookmarks** — *Toggle / Next / Previous / Clear* (Ctrl+B / Ctrl+] / Ctrl+[).
- **View** — show/raise any pane (Timeline, Registers, Statistics, Bookmarks, Bus Grid,
  Capture, CDS, Audio, Samples, Timing); **Appearance** (Dark / Light / follow System).
- **Help** — *Keyboard Shortcuts…*; *Timeline Marks Legend…*.

**Imports at a glance.** Digital sources (`.sal`, digital `.csv`, `.vcd`, `.bin`) and
analog scope sources (`.wfm`, analog `.csv`) all open through *Open Capture*. Analog
volts are thresholded to logic with an automatic mid-rail + 10% hysteresis, and — with
exactly two channels — the forwarded clock is auto-assigned by transition count (a `.csv`
with more than two channels prompts for which is clock vs data). Digital-vs-analog `.csv`
is decided by the data values (channels that are strictly 0/1 read as digital). The open
dialog reopens in the last-used folder, tracked separately per mode (the Visualizer
defaults to `./visualizer_examples`); a large capture shows an n/N decode-progress bar.

## Row / Time reference points

Table Row/Time columns mark the **start** of an entity, so a cursor sample lines the
panes up: a Command Table row is the command's opening SPM comma (the row's rising-edge
Row Sync Point); a CDS Symbols row is the first of the symbol's 10 bits; a Decoded
Samples row is the sample's MSB. Bus rows are **0-based** throughout (the first row is
0). The register map reflects the committed state as-of the cursor.
