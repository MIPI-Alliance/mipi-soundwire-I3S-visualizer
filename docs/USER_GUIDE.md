# SWI3S Studio — User Guide

A pane-by-pane reference. For setup and build see the [README](../README.md);
for design, [architecture.md](architecture.md).

## Modes

The **Mode** menu, first in the menu bar, selects one of three modes: *Bus Visualizer*
(⌘1), *Timing Calculator* (⌘2) and *Bus Analyzer* (⌘3). They share one window, one
bus-grid renderer, and one workspace file. (Keys are written as on macOS: on Windows and
Linux, ⌘ is Ctrl and ⌥ is Alt.)

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
  the *expected* config (in the Bus Analyzer, **Decode ▸ Import Visualizer CSV**, choosing the
  current authoring, to compare it with the decode).
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
  forms, `t_DD` and the handover's `t_ZD`), the three **Handover Contention** inequalities
  (Manager to Peripheral, Peripheral to Manager, Peripheral to Peripheral), and the bus
  keeper per releasing device. Titles name who owes what to whom — *Manager Holding for
  Peripheral*, *Peripheral A Setup for Peripheral B* — and the terms read in physical time
  order, so a row can be followed from the clock edge to the receiver's window.
  **Click any term** — a symbol or a substituted number — to highlight that parameter
  everywhere it acts: every inequality that reads it, both substitution rows, and its input
  row above. Click it again, or any blank space, to clear; several can be traced at once.
- **Analysis** — confirm it works. Decode a captured PHY1/PHY2 (forwarded-clock) or
  PHY3/DLV (recovered-clock) bus and explore it in the linked panes below. With nothing
  open the panes are empty, and the Commands and signal panes say so; **File ▸ Open Capture…** opens a capture, and
  **File ▸ Open Demo Capture** decodes a synthetic one to explore.

A slim **status bar** along the bottom says what the window just did: a file opened as a
Link, an offset set, a filter that found nothing.

All Analysis panes share **one time cursor**: selecting a command, clicking the grid
or a waveform, scrubbing the timeline, or picking a symbol moves the cursor, and every
pane follows.

## Analysis panes

### Commands
One row per decoded Command Transport command. Columns: **Row** (the bus row of the
command's opening SPM comma), **Time**, PhaseID, Devices, Packet Length, Opcode,
Address, **Register** (address resolved to register/field names, e.g.
`SLC.NumColumns`, with symbolic enums), Data, Group Mask, CRC, **Response**. Responses
decode to symbolic names labelled peripheral vs manager (`Periph: WRITE_OK`,
`Mgr: CONFIRM_COMMIT`); a Ping breaks out per device, collapsing runs into ranges.
CRC errors, empty masks, error responses, and an **EnableCh _CURR write while the
port's Interval ≠ 1 Row** (a protocol violation — a direct write to the committed
channel-enable rank is only safe when every row transports) are highlighted, and the
**Errors Only** filter agrees with the shading.
Filtering is in **Commands ▸ Filter** (expression search with `and`/`or`/parens;
multi-select Commands / Devices / Groups; Errors Only). The Commands list has quick picks
with keys: **All** (⌘4), **None** (⌘5: nothing, to tick kinds one by one), **All excl.
Ping** (⌘6) and **Commit** (⌘7: the SSCR / DSCR sync-point commits and the Commit Point
rows where each takes effect). Right-click the header to
show/hide columns. **Commands ▸ Next / Previous SSCR/DSCR Commit** (⌘> / ⌘<)
jump the cursor between the sync-point commits where the config takes effect.

### Registers
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

Each swatch in the colour key is as wide as its label, so a data port you have named
(Audio ▸ right-click a channel ▸ **Rename…**) reads in full.

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

### Audio
One stacked waveform per **(device, data-port, channel)**, from a min/max pyramid so
pan/zoom stays fast over millions of samples. A **PDM** port (1-bit sample size) is
decoded from its bipolar density stream to PCM. Each stream has its Bus Grid hue, in a
deeper shade on the light theme so the line stays readable. Channel checkboxes
(colour-matched to each waveform) show/hide tracks without resetting zoom; **right-click a
checkbox ▸ Color…** to pick that data port's colour, for all its channels, for this Link in
the current theme (it follows into the Capture overlay and the Samples Port column, and is
saved in the workspace), or **Reset Color** to go back to the palette (**View ▸ Waveform Colors and Line Weight…** sets the
palettes and how thick the lines are drawn); the Y axis reads -1, 0, +1 at full scale, and
the extremes of what is shown under **Vertical Zoom In**; a cursor line tracks the shared
cursor. **Click a waveform to seek**; **▶ Play** streams the first selected port to the
system output; **Jog < / >** page one screen. Output device, bit depth, and per-port
decimation are in the **Audio menu**.

**Right-click a checkbox ▸ Rename…** names that data port, for example `Mic L`. The name
replaces "Dev1 DP2" in the channel list (the numbers stay in its tooltip and in the track
titles), the Samples Port column and filter, the Capture legend, the Bus Grid's colour key,
the Audio menus, the Filter & Gain and Export Audio dialogs and the Timing pane's driver
filter. An empty name goes back to the numbers. The name is the Link's and is saved in the
workspace; it changes labels only, so nothing is re-decoded.

**Waveform or spectrogram, per channel.** Right-click a checkbox ▸ **Spectrogram** draws
that channel as a spectrogram (time across, frequency up to the stream's Nyquist, level as
colour from −120 to 0 dBFS); **Waveform** goes back. It shares the waveform's zoom, cursor,
bookmarks and click-to-seek, and the tooltip adds the frequency under the pointer. The
**frame** sizes under Spectrogram (256, 512, 1024, 2048 points; 1024 by default) trade time
for frequency: each column is an FFT of one frame centred on it, so a click shows from
half a frame before it, ±2.7 ms at 256 points and 48 kHz, with 188 Hz bins; ±10.7 ms at
1024 with 47 Hz bins. Each menu item says what its size gives at that channel's rate. Zoomed
far out a frame is taken per column, so a click between two frames shows once you zoom in.
The choice and the frame are part of the Link's Audio view.

**Right-click a checkbox ▸ Filter & Gain…**, or **Audio ▸ Filter & Gain ▸** a stream,
filters and amplifies one stream, all its channels, to make a glitch stand out: a click or a dropout is often lost under a large
low-frequency signal or too quiet to see. The dialog follows Audacity's Amplify:

- **DC blocker**: a 2nd-order Butterworth high-pass at a fixed 1 Hz corner, zero phase. It
  removes a PDM mic's density bias or a PCM stream's offset, including one that drifts. Off,
  the waveform shows what is on the wire: a constant all-ones PDM stream reads full scale.
- **High-pass filter**: a 4th-order Butterworth applied forward and backward (zero phase,
  like SciPy's `filtfilt`), so a click stays where it happened. The corner runs from 1 Hz to
  20 kHz, or to just under half the stream's sample rate.
- **Amplification (dB)** and **New Peak Amplitude (dB)** are one number seen two ways. The
  peak is measured after the DC blocker and the high-pass, and the gain opens at the value
  that brings it to 0 dBFS; until you edit the gain it keeps doing that as they change.
- **Allow clipping**: without it, a gain that would take the peak above 0 dB cannot be
  applied. With it, samples past full scale saturate, as a DAC would.
- **Preview** shows the result in the waveform; **Cancel** puts back what was there, and
  **Apply** keeps it. **Revert**, offered when the stream has a setting, removes it and
  returns the decoded samples exactly.

A processed stream's checkboxes carry an asterisk (the setting is in their tooltip) and its
tracks' titles name it. Playback and **Export Audio** use the processed samples; the
**Samples** table keeps the decoded bus values. The setting belongs to the Link, survives a re-decode,
and is saved in the workspace. A filter cannot know the signal beyond the capture, so a
stream that starts or ends far from its own level shows the filter's edge response over the
first and last few periods of the corner frequency; a transport gap, where the stream is
joined across missing samples, shows the same.

### CDS
The Control Data Stream as classified 8b/10b symbols: **Row**, Time, codeword, **RD**
(running disparity; `!` = violation), Kind (comma / robust-token / D-code / K-code /
NO_RESPONSE, colour-coded), decoded value, and **Meaning** (the symbol's role in its
Command Transport phase — `PhaseID: WRITE`, `Address[31:24]`, `Manager_CRC16_H`).
Follows the cursor via windowed re-decode; selecting a command scrolls its comma to the
top. **Scroll up** to decode-and-prepend earlier history across config sections.

### Samples
Each reconstructed audio sample as a row: its **MSB** bus row, Time, the port that
carried it, and the value in binary / hex / signed decimal. Windowed around the cursor;
click a row to seek. Filter by **data port** and **channel** (multi-select) and by a
**value predicate** (e.g. `Decimal ≤ 0`).

### Capture
The two physical link lines (DP, DN) as digital waveforms, straight from the capture's
edges — no protocol interpretation. Overlays: **Row Sync Point** markers (**Show RSP
Markers**; the rising clock edge opening each Column 0), **Commit** sync points (gold), and
colour-coded **per-bit sample points** per active data-port channel (**Show Samples**).
**Hide Clock** drops the forwarded-clock trace and rescales the data to fill; **Hide
Legend** hides the key. **Jog < / >** page one screen. **Row Zoom** snaps to two rows of
Row-Sync edges and **Reset Zoom** shows the whole capture; max zoom-in is ~1 UI.

### Timing
Measured bus **setup/hold** margins straight from the raw clock/data edges (no decode).
Two log-scaled histograms meet at the sample point — **setup** on the left, **hold** on
the right — with every data transition split into the four **clock-edge polarity × data
direction** flavours (DDR samples both edges); click a colour-key entry to hide/show a
flavour. A **filter** narrows the transitions to any mix of drivers (Manager /
peripheral), data ports, or columns (CDS / Column 0 is off by default). When a commit
changes the bus clock rate mid-capture (e.g. `2col → 16col`) a **region** selector scopes
the measurement to one rate. **View Worst UI** jumps the shared cursor to the tightest
transitions so you can inspect them in Capture. Aggregate over the capture — no time
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
⌘B / ⌘] / ⌘[ (Bookmarks menu); pairs form as you add the second mark of a
letter. **Locate Sub-Capture** (below) also drops bookmarks, one per match.

### Sub-capture search
**File ▸ Locate Sub-Capture…** finds every place a smaller reference capture
recurs in the open one and drops a bookmark at each. Pick the reference file(s) — any
format Open Capture accepts, selecting **both** files together for a `.bin` or `.wfm`
pair. A short form of the Open Capture page follows (*Locate Sub-Capture: the reference*):
pick the reference's clock and data, and optionally a window of it, then **Search**.
Matching is **sample-rate independent** (it compares the transition *pattern* — the
data bits at each clock edge and the clock's gap shape — not absolute sample counts) and
requires a **near-exact** match of *both* the data and the clock pattern, so only genuine
occurrences are reported; it also retries with clock/data swapped in case the reference
was captured with the opposite orientation. When nothing clears the bar, it still
bookmarks the single best candidate and reports its score, so you can judge how close it
came. The search runs off the UI thread with a progress dialog.

### Timeline
A strip across the whole capture: command marks coloured by kind, translucent
**bus-config bands** by column count (so a `2col → 8col` cold-start reconfiguration is
visible at a glance), and the cursor. Click to seek; scroll up/down to zoom, left/right
to pan. **Bookmarks** (⌘B / ⌘] / ⌘[) mark and step through points. **Right-click a
bus-config band ▸ Rename…** to name that region, for example `Playback`: the band then
reads `Playback · 8col`. An empty name clears it; the name is the Link's and is saved in the
workspace. Like a pinned column count it belongs to the region's number, so a re-decode that
changes which regions exist can leave it on a different one.

## Opening a capture

**File ▸ Open Capture…** (⌘O) picks the file — one capture, or **both** channel files of a
`.bin` or `.wfm` pair, selected together — and then shows one page, before anything is
decoded:

- **Source** — what the file is: its format, channels and sample rate, its **length**, and
  roughly how much memory loading it all takes for the Links listed (each is loaded and
  kept, so two Links cost twice one), marked *fits* or *does not fit*. For every
  format but `.sal` the line also says the file is read whole (only a `.sal` reads a window
  without the rest). A `.bin` pair whose timestamps do not give its sample rate away asks
  for the rate here.
- **Links** — one row per Link: its **name** (*Link 1*, *Link 2*… by default), its
  **clock** and its **data** channel. A digital channel is listed with its edge count,
  rounded (~9.8M, ~163k); a smaller count marked `~` is an estimate (always for a `.sal`;
  for a `.csv` or `.vcd` past its first two million rows or edges). An analog channel has no
  edges until it is thresholded, so no count. The busiest channel
  is offered as the clock and a quieter one as its data (a forwarded clock toggles every
  UI). **+ Add a Link from this file** adds a row from the channels left over; **–**
  removes one. An analog capture (`.wfm`, a scope `.csv`) also has optional Schmitt
  thresholds, hi and lo, per line; blank detects them from the trace.
- **Decode** — **All of it**, or **From … to …** (in s, ms or µs, by the capture's length),
  with the window's length and
  memory beside it as you type. When all of a `.sal` would not fit, the page opens on the
  largest window predicted to fit.
- **Into** — with Links already open: **Replace the open Links**, or **Add to them**, with
  an **Offset** (ms) placing this file's time zero on theirs.

Open stays greyed out, with the reason shown, until every Link has its own name and two
channels of its own and the window ends after it starts.

## Multiple Links

A SWI3S **Link** is one Manager and its Peripherals on one bus (spec §4.7). A system can run
several Links side by side, and Analysis mode can show them together: open the first
capture as usual, then add each further Link.

**Adding a Link.**
- **File ▸ Open Capture…** with **Add to them** chosen opens another capture, of any
  format, as further Links, named in the dialog.
- A file with spare channels can give several Links at once: **+ Add a Link from this
  file** in the dialog. Links from one file share its timeline, so they are aligned from the
  start.
- Links from separate files each start at their own time zero. Place one with the dialog's
  **Offset**, or later (below).
- Links may differ in PHY, column count and row rate.
- To try it without a capture: **File ▸ Open Demo Capture ▸ PHY2 (Two Links)**. It opens
  the PHY2 demo as Link 1 and the flow-control demo as Link 2, as one analyzer would record
  both buses, with Link 2 starting about 2.47 ms later.

**Which Link a pane shows.** Four pane groups each show a Link of their own, chosen by the
picker in the group:
- **Commands**: the picker in its header, which also offers **All Links**.
- **Registers and Statistics**: the picker in their title bar.
- **Bus Grid**: the picker in its title bar.
- **Capture, CDS, Audio, Samples and Timing**: the picker in their title bar, which also
  offers **All Links** (below).

So Registers can show one Link while the Bus Grid shows another. **View ▸ Links ▸ Link 1,
Link 2, …** (⌘⌥1…9) shows a Link in every group at once. All the groups follow the one
cursor: each shows the same instant on its own Link.

**The active Link** is the one the menus act on, and it is drawn bold on the Timeline. It is
the Link of the pane you last clicked in. A menu that belongs to a pane always acts on that
pane's Link:
- **Audio**, the capture items of **File** (Export Capture, Locate Sub-Capture), and
  Decode's Scrambler and Manual SSP: the signal panes' Link.
- The grid items (**File ▸ Export Visualizer CSV / Export Bus Grid Image / View Bus Grid in
  Visualizer**, **Decode ▸ Import Visualizer CSV / Force Column Count**) and the grid's own
  toolbar: the Bus Grid's Link.
- **Devices**, **Decode ▸ Hub Depths** and Compare: the Registers group's Link.
- **Commands** export and commit jumps: the Commands pane's Link.

**All Links** in the Commands pane (the picker's last entry, or **View ▸ Links ▸ Commands:
All Links**, ⌘⌥0) lists every Link's commands in time order, with a **Link** column. Time
is shared time, with each Link's offset applied. Filters apply across all Links. Selecting a
row shows that command's Link in every pane. **Commands ▸ Export as CSV** from All Links
writes every Link's commands in time order, with a Link column and each command's shared
start time.

**All Links in Capture, CDS, Audio, Samples and Timing** (their picker's last entry, or
**View ▸ Links ▸ Signal Panes: All Links**, ⌘⌥⇧0) shows every
Link at once:
- **Capture, Audio, CDS, Samples and Timing** stack one pane per Link, each named, all at
  the cursor's instant on their own Link. Their time axes and Time columns read shared time
  (each Link's offset applied); the Capture and Audio panes cover the whole span of every
  Link, and zooming or panning one moves the others with it, ticks at the same times.
  Clicking, dragging a bookmark, picking a row or a Timing pane's **View Worst UI** in a
  Link's pane acts on that Link, and makes it the active one. One Audio pane plays at a
  time. Each Timing pane keeps its own region, filter and edge-flavour colours.
- The **Bus Grid** stays one Link at a time: it has its own picker.

Picking a Link in the picker (or View ▸ Links ▸ Link n) goes back to one Link, and the
panes go back to their one-Link height.

**The Timeline** stacks one band per Link on a shared time scale. Zooming or panning any
band moves them all. With two or more Links each band is labelled with its Link: click the
label to show that Link everywhere, or double-click it to rename the Link. Drag the dock's edge to make the
bands taller or shorter.

**Bookmarks** belong to the Link they were placed on, and are drawn only on that Link: on
its Timeline band, and in Audio and Capture when they show it. Clicking a band makes that
Link the active one, so **⌘B** places the next bookmark on the band you clicked.
Next / Previous Bookmark step through the active Link's bookmarks.

A pair can span two Links, which is how an event on one bus is timed against another:
click Link 1's band at the first event and press ⌘B (A1), then click Link 2's band at
the second and press ⌘B (A2). The **Bookmarks** pane has a **Link** column naming each
bookmark's Link ("Link 1 → Link 2" for a pair across two). The pair always shows the time
between them. It shows rows only when both Links run the same row rate for the whole time
between the two bookmarks, and UIs only when they run the same UI rate for that time, so
where either rate changes on either Link in between, that count is not shown. Each Link must also have
been recording for that whole time. On one Link, rows and UIs are counted, so they are
always shown. Clicking a row shows its bookmark on its own Link.

**Offsets.** Each Link's offset places its capture on the shared time.
- When adding, the Open Capture dialog's **Offset** places the new Links.
- **Set Offset…** on a Link's timeline label (right-click) enters an offset in µs, ns, ms, s
  or ps. Positive means that Link's capture started later.
- **Bookmarks ▸ Align Links on Bookmark Pair…** sets it from events. Put one bookmark on each Link at
  the same physical event (⌘B on one, then on the other) and choose the pair. The
  second Link moves so the two coincide.
- Changing an offset moves only the time mapping. Nothing is re-decoded, and each Link's
  bookmarks move with it.

Right-click a Link's timeline label for **Rename Link…** (double-click renames too),
**Set Offset…**, **Remove Link…** and **Show This Link Everywhere**. A removed Link takes its
bookmarks with it, after a confirmation. With two or more Links, exports are named for the
Link they come from, for example `commands_Amp_bus.csv`, and a saved workspace reopens
every Link with its name, offset and pane layout.

## Partial captures & finding the SSP

A **partial capture** starts *after* the bus was configured — the setup
`WriteA32`/commit sequence and the Stream Sync Point (SSPA/SSCR) are not on the wire.
Two things are then missing:

1. **Port geometry** — the decoder can't snoop it. Supply it with **Decode ▸ Import
   Visualizer CSV…** — pick a config CSV and choose **Update** to impose its data-port
   config on the open capture, or **Compare** to overlay it in the Register Map (the current
   Visualizer authoring can be compared, not imposed). The config drives the decode from row 0,
   so audio reconstructs. A capture with **several config regions** (a cold start's
   Safe-Lock-2 → 8 → 16 columns) takes the CSV into the region under the cursor **only**:
   that region decodes with the CSV's ports and width, and every other region, and every
   command, decodes as the wire says. The region is labelled `8col (CSV)` in the timeline,
   its registers show as *CSV* in the Register Map, and the import is saved in the
   workspace. **Decode ▸ Remove … from This Region** takes it out again (the item names the
   CSV, and is enabled only while the cursor's region has one). Use the **TX map** (Bus Grid ▸ Show Toggles) to see which
   columns actually carry data and confirm the config matches the wire.

   If the grid still shows the wrong number of columns — the wire never carried a
   `NumColumns` commit to snoop, or the blind column detector mis-locked — pin the width
   with **Decode ▸ Force Column Count…**. It applies to the config region under
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

   - **Decode ▸ Manual SSP Move ▸ Set SSP at Cursor Row**, then **Move SSP +1 / −1 Row**
     (⌘L / ⌘K) to step the phase until the audio is clean. The phase repeats
     every interval, so stepping by 1 cycles all N.
   - **Clear Manual SSP** returns to the default anchor.
   - Manual SSP only re-phases audio when a config CSV is applied (it needs the port
     geometry); on a capture with no decodable audio it changes nothing.

The applied CSV and the chosen SSP row are saved in the workspace and restored on reopen.

## Large captures

A `.sal` is a ZIP of delta-coded transitions, so it expands enormously on open: a 291 MB
file can hold 2.25 GB of payload and decode to ~18 GB of edge arrays. Before opening
anything, Studio predicts the peak memory from the ZIP directory alone — no inflating, no
decoding — and the Open Capture dialog shows it against the budget; when all of it would
not fit, the dialog opens on a **time window** that does.

- **The prediction is for a whole-file decode as it is now**: the inflated payload plus
  twice the edge arrays (the result, and one block's worth of deltas while it is built). A
  54 MB capture with 302M transitions peaks at 3.7 GB and is predicted at 7.2 GB, which
  opens without asking.
- **The budget is 60% of free memory.** A load predicted past it is not attempted, and the
  dialog opens on a window that fits. A load that fits but is predicted past 8.6 GB (8 GiB)
  is labelled *large, so opening may be slow* and is yours to open whole or not.
- **The decode after the load is watched, not predicted.** How much audio a capture
  decodes to depends on its bus config (from a few hundredths of a sample per UI to nearly
  one for 1-bit PDM), so it cannot be known before decoding. While it runs, free memory is
  read every quarter second; if it falls below a floor (10% of RAM, at least 1 GiB) the
  decode is stopped and says so, before macOS starts compressing memory, and you can open
  a window instead. A re-decode stopped this way keeps the decode you had.
- **You can ask for a window on purpose**, in any format, with the dialog's **From … to
  …**, even for a capture that would fit. For a `.sal` only the overlapping blocks are
  decoded, streamed straight out of the ZIP; other formats are read whole and cut. Either
  way the result is rebased so the window starts at time 0, and a saved workspace reopens
  the same window. Handy when you already know you want 20 s out of 176.
- **For a `.sal`, a window costs the window, not the file** — including near the end of a
  long capture.
- If Studio cannot offer a window (a file with no block index) it says so rather than
  attempting the load; export a shorter range from Logic 2 instead.

Two things help independently of memory: close the panes you are not reading (a hidden pane
does no work on a cursor move), and keep Capture's *Show Samples* overlay off when zoomed
far out.

## Menus

**Mode** comes first, and every other menu shows only what applies to the mode: the Bus
Visualizer and Timing Calculator have Mode, File, View and Help; the Bus Analyzer adds
Commands, Decode, Devices, Audio and Bookmarks. There is no Links menu: a Link is named and
added in **Open Capture**, its own actions are on its timeline label's right-click, and
showing Links is **View ▸ Links**. **⌘O / ⌘S** (Ctrl on Windows and
Linux) open and save the current mode's file. Items that act on a capture are greyed out
until one is open. The Capture, CDS, Audio, Samples and Timing panes are the **signal
panes**: one pane group, with its own Link picker.

- **Mode** — *Bus Visualizer* (⌘1), *Timing Calculator* (⌘2), *Bus Analyzer* (⌘3).
- **File**, Bus Analyzer — *Open Capture…* (⌘O: a Logic 2 `.sal`, a digital `.csv`, a
  `.vcd`, a Tektronix analog `.csv`, or **both** channel files of a `.bin` or `.wfm` pair,
  multi-selected — see **Opening a capture**); *Open Demo Capture ▸ PHY1 (FBCSE), PHY2
  (FBCSE), PHY2 (Flow Control), PHY3 (DLV), or PHY2 (Two Links)*; *Open / Save Workspace…*
  (⌘S, a `.swi3s` file; older `.json` workspaces still open; see **What a workspace keeps**
  below); *Export Capture…* (one dialog — pick the format `.sal` / `.bin` / CSV, which signals + their names, the range (whole capture or a
  time / bus-row / UI window), and the output file); *Locate Sub-Capture…*; *Export
  Visualizer CSV…*, *Export Bus Grid Image…* (SVG/PNG) and *View Bus Grid in Visualizer*.
- **File**, Bus Visualizer — *Open / Save Visualizer CSV…* (⌘O / ⌘S, the authoring CSV);
  *Export Grid Image…*; *Export Bus Model (JSON)…* (the placed bus model).
- **File**, Timing Calculator — *Open / Save Timing Settings…* (⌘O / ⌘S, the calculator's
  inputs as JSON, incl. the selected PHY).
- **Commands** — *Filter* (expression search; multi-select Commands / Devices / Groups,
  with Commands' quick picks All ⌘4, None ⌘5, All excl. Ping ⌘6, Commit ⌘7; Errors Only);
  *Next / Previous SSCR/DSCR Commit* (⌘> / ⌘<); *Export as CSV…*.
- **Decode** — everything that changes what the capture decodes to, so each re-decodes:
  *Import Visualizer CSV…* (impose a config CSV on the capture from row 0, or on the
  region under the cursor when the capture has several, or compare it,
  or the authored config, in the Register Map — clear the comparison with the **Clear
  Compare** button there); *Remove … from This Region* (drop the cursor region's CSV);
  *Force Column Count…* (pin the bus column count for the config
  region under the cursor); *Hub Depths…*; *Per-Dataport Scrambler…* (Auto / On / Off);
  **Manual SSP Move** (see above).
- **Devices** — *Device Names…*; *Peripheral Register Maps…* (import vendor maps).
- **Audio** — *Playback Output Device / Bit Depth / Decimation*; *Filter & Gain* (a
  stream's DC blocker, high-pass and gain); *Export Audio as WAV…*.
- **Bookmarks** — *Toggle / Next / Previous / Clear* (⌘B / ⌘] / ⌘[); *Align
  Links on Bookmark Pair…* (two or more Links).
- **View** — in the Bus Analyzer, show/raise any pane (Timeline, Registers, Statistics,
  Bookmarks, Bus Grid, Capture, CDS, Audio, Samples, Timing); **Links** (two or more Links:
  *Commands: All Links* ⌘⌥0, *Signal Panes: All Links* ⌘⌥⇧0, and one
  entry per Link, ⌘⌥1…9, to show it in every pane); **Appearance** (Follow System /
  Light / Dark); *Waveform Colors and Line Weight…* (Ctrl+, on Windows and Linux; on macOS also
  **Preferences…**, which macOS shows in the application menu as Settings…, ⌘,): the line
  colours, per theme, for data ports, bookmark pairs, the Capture DP/DN traces and the
  Timing edge flavours, and the Audio and Capture line weights (1–4 px). A colour below 3:1 contrast on that theme's plot is marked
  ⚠ and still allowed; *Reset to Defaults* restores all of them.
- **Help** — *SWI3S Studio User Guide* (this guide); *Keyboard Shortcuts…*; *Timeline Marks
  Legend…* (Bus Analyzer); *About SWI3S Studio* (on macOS, in the application menu).

**Imports at a glance.** Digital sources (`.sal`, digital `.csv`, `.vcd`, `.bin`) and
analog scope sources (`.wfm`, analog `.csv`) all open through *Open Capture*. Analog
volts are thresholded to logic with an automatic mid-rail + 10% hysteresis (or the
dialog's thresholds), and — with exactly two channels, left as offered — the forwarded
clock is assigned by transition count. Channels are paired into Links in the Open Capture
page (see **Opening a capture**). Digital-vs-analog `.csv` is decided by the data values (channels that are strictly 0/1 read as digital). The open
dialog reopens in the last-used folder, tracked separately per mode (the Visualizer
defaults to `./visualizer_examples`); a large capture shows an n/N decode-progress bar.

### What a workspace keeps

A saved workspace reopens the window as it was when it was saved:

- **Each Link:** its capture and everything that changes how it decodes (register pins,
  hub depths, scramblers, device names and register maps), its name and offset, its
  Commands filter and column widths, its Audio pane's checked channels, zoom, Vertical
  Zoom, playback decimation and which channels are spectrograms (and their frames), its
  Capture pane's zoom and RSP marker, Samples and legend toggles, its streams' colours and
  Filter & Gain, and its data-port and region names.
- **The window:** bookmarks, the cursor, the mode, which Link each pane group shows and the
  All Links choices, which panes are open, how they are docked and tabbed and which tab is
  in front, their sizes, the window's size and position, the timeline's zoom, the Commands
  sort and hidden columns, the Samples filters, the Registers device and opened blocks,
  the Statistics sections folded, and the Timing pane's region, driver filter and hidden
  edge kinds; and the Bus Visualizer and Timing Calculator inputs.

Each capture file is recorded relative to the workspace file, so a folder holding both can
be moved or shared, and where it was when saved, in case the workspace moves alone. If a
capture is in neither place, you are asked to **Locate…** it (any other missing file of the
same name in the folder you pick is found too), to **Skip This Link** and open the rest, or
to **Cancel**.

The playback output device and bit depth are not in it: they belong to the computer, not
the capture. A window larger than the screen it reopens on is fitted to that screen. Each
Link also keeps its own Audio and Capture view while another Link is shown, and through a
re-decode.

## Row / Time reference points

Table Row/Time columns mark the **start** of an entity, so a cursor sample lines the
panes up: a Commands row is the command's opening SPM comma (the row's rising-edge Row Sync
Point); a CDS row is the first of the symbol's 10 bits; a Samples row is the sample's MSB. Bus rows are **0-based** throughout (the first row is
0). The register map reflects the committed state as-of the cursor.
