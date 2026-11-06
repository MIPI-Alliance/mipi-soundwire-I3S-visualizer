# SWI3S Studio — User Guide

A pane-by-pane reference. For setup and build see `README.md` in the source tree;
for design, `docs/architecture.md` there. Neither ships with an installed or packaged
application.

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
  The **⤢** at the grid's top right, **Maximize Frame** or **View ▸ Maximize Pane** (⌘⇧M)
  gives the grid the whole page, with the data ports' colour key; again to restore. Dragging
  the divider above the grid to the top does the same, and the grid stays maximized across a
  switch to another mode and back.
  The Bus Grid and Save Output (JSON) build at most 1024 rows. A larger **Rows To Draw**
  is drawn to that limit and a Rows To Draw warning appears in the notifications.
  Load/Save the config as CSV; export the placed bus model as JSON (**File ▸ Export Bus Model (JSON)…**, or **Save Output (JSON)**); push it into Analysis as
  the *expected* config (in the Bus Analyzer, **Decode ▸ Import Visualizer CSV**, choosing the
  current authoring, to compare it with the decode).
  **Load Init (CSV)** opens a config CSV named `init.csv`. It looks for the file named by the
  environment variable `SWI3S_INIT_CSV`, then for `init.csv` next to the application (the
  repository root in a source checkout), then for `init.csv` in the working directory, and
  opens the first of these that exists. If none exists, or the file cannot be read, it opens
  the built-in demo config, and the status bar says why.
- **Timing** — dial in the PHY. Pick a spec source **per side** (Manager and Peripheral
  independently): **SWI3S PHY1**, **SWI3S PHY2**, **SWI3S PHY2 (EDE proposed)** (the proposed
  PHY2 revision with EndDriveEarly), or **SoundWire 1.3 (1.2 V)** — so a mixed SWI3S ↔
  SoundWire bus is expressible. The plain proposed PHY2 revision is not a selector choice:
  it seeds the editable **Example** column. A SoundWire
  side is seeded with its SWI3S equivalents (each value re-measured between the SWI3S anchors
  for the side it is on), and the
  cross-spec divergences that are not converted are surfaced as caveats rather than hidden.
  The **Specification** column is read-only; only the **Example** column is editable. Enter
  bus length, per-lane slew, supply/noise, and output/input/Z-handover timing, and choose
  the Manager's **data launch** (analog delay, or a 2× / 4× clock grid), whether a
  **handover UI** is allocated, whether the **Manager forces keeper** (it loads the keeper
  with the level it drives, so the Manager-releasing keeper inequality does not apply and reads
  "does not apply" with "—" for its margin and F_max; Reset clears it), and where the two
  peripherals sit for the **P→P** legs. A side
  on the **EDE** source releases its **EDE Delay** (Handover Timing, per side) after its ramp
  reaches 80 %, in place of its t_DZ. A contention inequality shows that release as its parts,
  from the edge that opens the driven UI: the releaser's t_DD, its ramp to 80 %, the EDE
  Delay, and the UI back to the closing edge.
  **Save Example** writes JSON (`example.json`). **Load Example** reads JSON, and also YAML
  where PyYAML is installed; it reads files up to 1 MiB and refuses YAML aliases. For a clocked launch the hint beneath the selector gives the rate
  up to which the Example column's Manager-to-Peripheral setup and hold legs pass, and marks
  a rate above it with a warning. Where the two columns launch differently, the hint names the
  column the selector drives and the column that keeps its analog delays by their headings. A saved value that is not a number is ignored and the box
  keeps its value; a range saved with its minimum above its maximum is restored with the two
  exchanged; both are reported in the import warnings. A range typed with its minimum above its
  maximum is checked once focus leaves both boxes of its row, so typing a range upward, minimum
  first, does not exchange the boxes while you type.
  Read **17 numbered inequalities** term by term with margins, F_max and the binding
  constraint, each evaluated at its own worst PVT corner, with each device at one V_SEOS
  for its outputs and inputs alike: setup and hold for
  Manager→Peripheral, Peripheral→Manager and Peripheral A→Peripheral B (each in both launch
  forms, `t_DD` and the handover's `t_ZD`), the three **Handover Contention** inequalities
  (Manager to Peripheral, Peripheral to Manager, Peripheral to Peripheral), and the bus keeper per releasing device, its response counted from the releasing data's crossing of the Manager's V_IH / V_IL at the Manager as the Bus Keeper table measures it, a Peripheral's release counted from its own detections of the two clock edges that bound its UI. Titles name who owes what to whom — *Manager Holding for
  Peripheral*, *Peripheral A Setup for Peripheral B* — and the terms read in physical time
  order, so a row can be followed from the clock edge to the receiver's window. F_max is
  shown for every leg whose margin scales with the UI: setup legs, and hold, handover-hold,
  contention and keeper legs through a clocked Manager timing, the allocated handover UIs or
  a conventional release's closing UI. F_max is the first failure: the lowest rate at which the
  leg fails as F_CLK rises from zero. It reads — where the leg never fails and FAIL where it
  fails at the lowest rates. A leg whose margin scales with the UI never reads above the toggle ceiling, 1000 × 0.45 / t_RF,max (54.2
  MHz on SWI3S, from its 8.3 ns slowest edge), 0.45 being the shortest half period as a fraction of the period: there that half period holds just one
  complete edge, and above it no edge reaches V_OH / V_OL. A leg that passes up to the ceiling
  reads the ceiling, and its tooltip says so. Under a 2× or 4× Manager launch, F_max places each clocked Manager
  timing where the rows go when that rate is typed in: at its fraction of the UI, raised to a
  further grid point where that falls below the row's tabulated minimum. A leg can then fail,
  pass again and fail again as the rate rises; its F_max is still the first failure, and the
  F_max cell's tooltip names the higher rates at which it passes again. On the Example column,
  PHY2/PHY2 at 12.288 MHz with 4x CLK, MP_hold fails from 12.167 MHz, its F_max and the binding
  rate, and passes again from 12.50 MHz, where its Man_t_DD,min moves from 0.25 to 0.50 UI.
  Inputs the model cannot evaluate, such as a threshold at or beyond the transmitter's rail,
  show no margins: the advisory line under the inputs names the column and the reason, and
  with the page or the diagram maximized, where the top bar is hidden, the same 'No margins.
  <column>: ...' reason is shown above the results. The term diagram closes when no margins can
  be computed. The binding constraint is the lowest F_max over the legs that
  bound the bus.
  Where the two columns price different terms, as when one releases by t_DZ and the other by
  an EDE Delay, the inequality shows an equation row per column, headed by the column's name
  and followed by that column's numbers; where they price the same terms the columns share one
  equation row. Every term of every row is clickable. The copied summary lists the same rows
  after its margin table, which ends at the first blank line. The summary names the
  specification column 'Spec' throughout, and its last column is the right column's F_max less
  the left's ('—' where either is not a rate).
  **Click any term** — a symbol or a substituted number — to highlight that parameter
  everywhere it acts: every inequality that reads it, both substitution rows, and its input
  row above. Click it again in the inequality drawn, or any blank space among the inequalities, to clear;
  a click on a Margin or Max Frequency value does nothing, so the traced term and its diagram
  stay. Several terms can be traced at once. A click on an input row's name highlights its parameter, or
  clears it, the same way. Clicking an EDE Delay row highlights the EDE release terms it sets,
  and an EDE release term highlights the EDE Delay row.
  A click on a term in an inequality also opens the **term diagram** on the right, which
  draws that one term to scale at each column's worst corner for its inequality: the
  signals it connects at the pin it is measured at, each row named for its signal and pin (*Clk @ Peripheral*) in the
  edge's colour (a launch shows the launcher's clock and data, a flight one signal at both
  pins), each edge as its ramp model's curve, the level each end is measured at, and the
  term as a dimension in ns. A SoundWire launch and its anchor correction share one drawing,
  each on its own dimension line, headed by their total; a correction that shortens the
  launch is subtracted (−). A flight mismatch, t_PD,mis, is drawn as the clock's and the
  data's propagation from the launch, and the difference between their ends. A 20 % point is
  labelled as a percentage of the driver's V_DD. A threshold is drawn from the receiver's own
  ground, with an arrow giving it as a percentage of the receiver's V_DD; its percentage of
  the driver's is on the left. The driver's V_DD runs across the left half and the
  receiver's across the right, each labelled on its line as a percentage of nominal (to a
  tenth where it is not whole), as is a receiver's offset ground; the
  Corner line names the rows off typical, a slew row inside its range by its value (*t_RF Man CLK at 5.70 ns*) and a V_SEOS tolerance, noise or per-edge slew row inside its range by its value in % (*V_SEOS tol (δ) at 3.58 %*), and each device's V_SEOS. The diagram follows the
  inputs: an edit redraws it at the new worst corner. The
  **⤢** at the diagram's top right, or a double-click on the diagram, gives it the whole
  page with the two columns side by side; again to restore. While it has the page, the
  buttons beside the ⤢ draw the Specification column alone, the Example column alone, or Both;
  the choice is kept the next time the diagram is maximized, and restoring stacks both columns
  again. The diagram draws in heavy strokes and demi-bold labels, the labels a little smaller in a
  narrow pane. The rows grow with the pane.
  No label overlaps another or a line: each sits on its line where there is room, and
  further along it, off it, or joined to it by a thin leader line where there is not.
  A click on a highlighted term in another inequality draws it there; a click on the term
  drawn, in the inequality it is drawn for, clears its highlight, and clearing the term's
  highlight clears the diagram. The diagram draws every term of every inequality. A driver's trace is solid while it drives and dotted while it is high-Z: after a release, and before a turn-on (t_ZD), which leaves high-Z. An EDE Delay runs from its ramp's 80 % point, drawn as a level line. A keeper's response runs from the data's crossing of the keeper's threshold at the Manager, drawn as a threshold line: the Manager's V_IH,max / V_IL,min, or a SoundWire Manager's V_TH_Keeper,max / V_TL_Keeper,min. A Peripheral's data is drawn at both pins, the flight between them a guide. A contention inequality is drawn at the releaser's pin. P→M and P→P contention are drawn with both devices at the Manager, because their bus terms net to zero, and the Corner line says so. Contention carries no data crossing, so the releaser's last bit is held at the level opposite the acquirer's first edge (low with the acquirer rising, the usual case; high where that edge falls); an EndDriveEarly releaser is drawn on the edge its ramp is priced on, which on an exponential ramp with V_OH + V_OL other than 1 is the slower of the two. The margin is green where it is a gap and red where it is short, named overlap on a contention inequality and short on a keeper one.
  The bar at the top, which does not scroll, holds the spec sources, the Manager and
  Peripheral **ramp models**, the data launch, the handover and P→P choices and F_CLK. Below
  it scroll the **Timing Inequalities** and the parameter sections, each titled inside its
  box: RX Thresholds, RX Timing, TX Timing, Handover Timing, **Rise/Fall Times** (the t_RF
  lanes and the per-edge slew) and **System Properties** (V_SEOS tolerance, V_noise, bus
  length and length mismatch). The **⤢** beside *Timing Inequalities*, a double-click on that
  title or **View ▸ Maximize Pane** (⌘⇧M) hides the top bar, so the inequalities, and the
  parameter sections below them, scroll in the whole page; the same again brings it back.
  While the term diagram has the page, **View ▸ Maximize Pane** puts the diagram back.
- **Analysis** — confirm it works. Decode a captured PHY1/PHY2 (forwarded-clock) or
  PHY3/DLV (recovered-clock) bus and explore it in the linked panes below. With nothing
  open the panes are empty, and the Commands and signal panes say so; **File ▸ Open Capture…** opens a capture, and
  **File ▸ Open Demo Capture** decodes a synthetic one to explore, laid out by the
  workspace bundled with it (see **Menus**).

A slim **status bar** along the bottom says what the window just did: a file opened as a
Link, an offset set, a filter that found nothing.

All Analysis panes share **one playhead**, the current instant: selecting a command, clicking
the grid or a waveform, scrubbing the timeline, or picking a symbol moves the playhead, and
every pane follows. **Cursors** are marks you place at the playhead (⌘B) to measure between
(see **Cursors** below).

## Analysis panes

**Tabbed panes** — Registers, Statistics and Cursors on the right; Bus Grid, Capture, CDS,
Audio, Samples and Timing below — show their group's tabs in the title row, where the pane's
title would be: the selected tab names the pane in front, and a click on another brings that
one forward. A pane alone in its area, docked apart or floating, shows its title instead.

**Maximize a pane** to give it the whole window: click in it and press **⌘⇧M** (Ctrl+Shift+M
on Windows and Linux), or use **View ▸ Maximize Pane**, the **⤢** button at the right of its
title bar, or a double-click on the title bar (the Commands table's title too). Every other
pane and the Commands table are hidden; the Commands table can be the maximized pane too.
The Timeline is not maximized: it has no ⤢, and with it selected the key only says so in the
status bar. The panes read by moving through time (Commands, Registers, Bus Grid, CDS and
Samples) keep the **Timeline** above them, at its height, to navigate with; Audio, Capture,
Timing, Statistics and Cursors take the whole window, since the first three have time axes
of their own. A closed Timeline stays closed. The same key, button or double-click (or
**View ▸ Restore Panes**) puts every pane back as it was: sizes, tab groups and floating
panes, with the pane you maximized in front of its tab group and still holding the keyboard
focus, so the key toggles that same pane. Showing, hiding or closing a pane, adding or
removing a Link, leaving the Bus Analyzer or opening a workspace puts them back first, and a
workspace saved meanwhile keeps the arrangement they return to. A double-click on a title bar
never floats a pane; a pane is floated out of the window by dragging its title bar. The
maximized pane cannot be dragged out of the window; made floating, it puts the panes back.

### Commands
One row per decoded Command Transport command. Columns: **Row** (the bus row of the
command's opening SPM comma), **ΔRows**, **Time (µs)**, **ΔTime (µs)**, PhaseID, Devices,
Packet Length, Opcode, Address, **Register** (address resolved to register/field names, e.g.
`SLC.NumColumns`, with symbolic enums), Data, Group Mask, CRC, **Response**. Time (µs),
PhaseID, Opcode, Address, Register, Data and Response are shown by default, each at a fixed
default width; Row, ΔRows, ΔTime (µs), Devices, Packet Length, Group Mask and CRC are hidden
until shown from the header's right-click menu, and a column shown for the first time is
fitted to its contents. Responses
decode to symbolic names labelled peripheral vs manager (`Periph: WRITE_OK`,
`Mgr: CONFIRM_COMMIT`); a Ping breaks out per device, collapsing runs into ranges.
CRC errors, empty masks, error responses, and an **EnableCh _CURR write while the
port's Interval ≠ 1 Row** (a protocol violation — a direct write to the committed
channel-enable rank is only safe when every row transports) are highlighted, and the
**Errors Only** filter agrees with the shading.
Filtering is in **Commands ▸ Filter** (expression search with `and`/`or`/parens; an operator
with nothing after it (`write or`) is ignored, and a `)` with no matching `(` is ignored; in
All Links the filter matches the global time shown in the Time column;
multi-select Commands / Devices / Groups; Errors Only). The Commands list has quick picks
with keys: **All** (⌘4), **None** (⌘5: nothing, to tick kinds one by one), **All excl.
Ping** (⌘6) and **Commit** (⌘7: the SSCR / DSCR sync-point commits and the Commit Point
rows where each takes effect). Right-click the header to
show/hide columns. **ΔRows** and **ΔTime (µs)** are counted from the row above as the table
shows it, filtered and sorted. Filtered to one command kind
they read that kind's spacing; in All Links, ΔRows is blank between two Links' rows (each
Link counts its own rows) while ΔTime is on the shared timeline. Because they count from the
row above, the two delta columns are not a sort key: clicking either header
leaves the table's sort unchanged. Which columns are shown is
kept across Link switches and saved in the workspace; a column the workspace does not record
takes its default. **Commands ▸ Next / Previous SSCR/DSCR Commit** (⌘> / ⌘<)
jump the playhead between the sync-point commits where the config takes effect.

### Registers
Per-device register state as-of the playhead: SLC, CDS, each active data-port block, the
active PHY, and any imported peripheral (vendor) registers. Values are coloured by
**provenance** (inline key): Cold Reset / Bus Write / Bus Read / CSV Import / Manual
Edit. A field list wider than the Fields column is cut with an ellipsis; hover the cell to
read it in full. Dual-ranked registers show a staged **(NEXT)** row and a committed **(CURR)**
peer. **The key is also a filter:** click an entry (or Tab to it and press Space or
Enter) to hide or show the registers whose value came from it; hidden entries grey out,
and the tooltip says which state an entry is in. Turn **Cold Reset** off to see only the
registers something has set. NEXT and CURR rows are filtered each on its own provenance,
and a block with nothing left to show is hidden; if nothing at all is left, the pane says
so. Starting or changing the filter opens the blocks with registers to show (except
Peripheral Registers, which can be long); after that, blocks stay as you open or close
them, and showing every category again gives back the blocks you had open, with the
changes you made while filtering. The filter stays across playhead moves and is saved in the
workspace, as are the column widths (by column name; a workspace without them opens at the
default widths). **What-if editing:** right-click (or double-click) a register → *Edit fields…*
(dropdowns for enums, bounded number boxes otherwise) or *Force value…* (raw byte).
*Force value…* takes a whole number 0..255 in decimal, or with a 0x, 0o or 0b prefix in hex, octal or binary (0xFF); hex without the prefix (FF), `_` and non-ASCII digits are refused. Anything else is
refused with a message and the prompt opens again with what you typed; nothing is changed
until a value reads or you cancel.
The edit re-decodes so grid, registers, and audio all reflect it; *Clear all manual
edits* reverts. A dual-ranked write takes effect at its **commit**, not where it was seen.
**History:** right-click a register → *History…* lists every write and read of it on the
Link the Registers pane shows, in capture order: the time and bus row, the access, the byte
value it carried, whether the register model applied it, and what went wrong if anything did
(a CRC error, WRITE_FAILED or READ_FAILED, an error token, no response). Failed attempts are
tinted and can be shown alone. Double-clicking a row puts the playhead on that command's Row
Sync Point on the History window's Link, the same place selecting the command in Commands
puts it. An open History window follows a re-decode of its Link and lists the accesses of the
new decode. A CRC-valid write is applied even when the device answered WRITE_FAILED, as the bus
carried the value; both facts are shown. A failed read returns no data, so it is matched at its own start
address. A deferred read's ReadData that failed or got no answer carries no address; it is
listed under the address of the device's deferred ReadA32. On a dual-ranked register's (CURR)
row, History also lists the writes and reads of its (NEXT) register, marked NEXT, and each
Commit to the device that covers the register's commit group, with the staged value it
promotes: a value reaches the CURR register that way or by a write to the CURR address
itself. A write to a read-only register is listed, not applied, with the status "read-only register: no effect", and the command table flags it as a programming error. A write to a register with write-1-to-clear bits clears each bit it writes 1 and keeps each it writes 0; its access reads "Write (cleared by write)". A data
port's commit group is its CommitGroupMemb as the bus left it before that Commit. A Commit
promotes the devices its device mask selects, none when the mask is 0 (Section 8.1.1.5.1,
the "Commit Phase for SSCR and DSCR Commands" table), in History and in the Registers pane
alike. A write must select exactly one device: History lists a write whose device mask
selects more than one, marks it not applied and names the mask error, and the Registers
pane ignores it. A write to a (CURR) register sets its (NEXT) register too, so a (NEXT)
row's History also lists the writes to its (CURR) register, marked CURR. After a later
Cold Start's Bus Reset, staged values and commit-group membership are back at their reset
values, so a Commit after the reset promotes only what was written since it. The window
closes when its Link is removed or a new capture replaces the analysis.

### Bus Grid
The 2D Rows×Columns bus laid out like the Visualizer, as-of the playhead; it fills in as
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
no audio-mode column structure on the wire there yet. Where the Link went inactive before a
later bring-up (its clock stopped), both read **Link Inactive**.

### Audio
One stacked waveform per **(device, data-port, channel)**, from a min/max pyramid so
pan/zoom stays fast over millions of samples. **Zoomed in until the samples are a few pixels
apart, a track shows the samples themselves**: a stem from zero to each value with a dot on
it, at the time the sample arrived on the bus; zoomed out again it is the line through them.
A **PDM** port (1-bit sample size) is
decoded from its bipolar density stream to PCM. Each stream has its Bus Grid hue, in a
deeper shade on the light theme so the line stays readable. Channel checkboxes
(colour-matched to each waveform) show/hide tracks without resetting zoom; the list is as
wide as its longest entry, drag the splitter beside it to resize it, and a workspace's saved
width applies. **Right-click a
checkbox ▸ Color…** to pick that data port's colour, for all its channels, for this Link in
the current theme (it follows into the Capture overlay and the Samples Port column, and is
saved in the workspace), or **Reset Color** to go back to the palette (**View ▸ Waveform Colors and Line Weight…** sets the
palettes and how thick the lines are drawn); the Y axis reads -1, 0, +1 at full scale, and
the extremes of what is shown under **Vertical Zoom In**; a playhead line tracks the shared
playhead. **Click a waveform to seek**; **▶ Play** streams the first selected port to the
system output (selected data ports that start after the playhead start playing at their own
start time); **Jog < / >** page one screen. Output device, bit depth, and per-port
decimation are in the **Audio menu**.

**Right-click a checkbox ▸ Rename…** names that data port, for example `Mic L`. The name
replaces "Dev1 DP2" in the channel list (the numbers stay in its tooltip and in the track
titles), the Samples Port column and filter, the Capture legend, the Bus Grid's colour key,
the Audio menus, the Filter & Gain and **Audio ▸ Export…** dialogs and the Timing pane's driver
filter. An empty name goes back to the numbers. The name is the Link's and is saved in the
workspace; it changes labels only, so nothing is re-decoded.

**Waveform or spectrogram, per channel.** Right-click a checkbox ▸ **Spectrogram** draws
that channel as a spectrogram (time across, frequency up to the stream's Nyquist, level as
colour from −120 to 0 dBFS); **Waveform** goes back. It shares the waveform's zoom, playhead,
cursors and click-to-seek, and the tooltip adds the frequency under the pointer. The
**frame** sizes under Spectrogram (256, 512, 1024, 2048 points; 1024 by default) trade time
for frequency: each column is a Blackman-Harris-windowed FFT of one frame centred on it, so a
full-scale tone's leakage is under −90 dB beyond 5 bins and under −98 dB beyond 10 at any
frequency, and a click shows from half a frame before it, ±2.7 ms at 256 points and 48 kHz, with 188 Hz bins; ±10.7 ms at
1024 with 47 Hz bins. Each menu item says what its size gives at that channel's rate. Zoomed
far out a frame is taken per column, so a click between two frames shows once you zoom in.
The choice and the frame are part of the Link's Audio view.

**As Transported or Evenly Spaced, per stream.** A stream carried by Payload Interval Skipping
(44.1 kHz over 48 kHz rows: 147 samples in every 160 intervals) arrives in steps and skips, and
drawn where each sample arrived a sine looks lumpy. Right-click its checkbox ▸ **Evenly
Spaced** draws each run of samples evenly from its first to its last, the stream's average
rate, as a receiver plays it, and a spectrogram lane shows the clean spectrum of the samples.
As Transported, the spectrogram is of the signal as transported (each sample held until the
next), so the skip pattern shows as spurs. A real gap in transport still breaks the line. **As Transported** goes back. It is offered only for a stream
whose samples arrive unevenly (a flow-controlled port is already evenly spaced within each run between gaps), is kept per
Link through a re-decode, and is saved in the workspace. The Samples pane always shows the
transported times.

**Right-click a checkbox ▸ Filter & Gain…**, or **Audio ▸ Filter & Gain ▸** a stream,
filters and amplifies one stream, all its channels, to make a glitch stand out: a click or a dropout is often lost under a large
low-frequency signal or too quiet to see. The dialog has:

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
tracks' titles name it. Exported files are WAVE_FORMAT_EXTENSIBLE integer PCM in a 16-, 24- or 32-bit container; the stream's sample size is the valid-bits field and each sample is left-justified in the container, so a stream narrower than its container (a 20-bit stream in a 24-bit file) still reads at full scale in a WAV editor. **Export Audio** asks before replacing WAV files that already exist in the
chosen folder. A typed resample rate must be 1 kHz or more and no
higher than the larger of 768 kHz and the stream's own rate. A unit always wins ("32000 Hz",
"22.05 kHz", "48k"). A number with no unit is in kHz below 1000 ("48" is 48 kHz, "44.1" is
44.1 kHz) and in Hz from 1000 up ("32000"). The dialog names the stream whose rate it cannot
use and exports nothing. Playback and **Audio ▸ Export…** use the processed samples; the
**Samples** table keeps the decoded bus values. The setting belongs to the Link, survives a re-decode,
and is saved in the workspace. A filter cannot know the signal beyond the capture, so a
stream that starts or ends far from its own level shows the filter's edge response over the
first and last few periods of the corner frequency; a transport gap, where the stream is
joined across missing samples, shows the same.

### CDS
The Control Data Stream as classified 8b/10b symbols: **Row**, **Time (µs)**, codeword,
**Disp** (the codeword's own disparity: −2, 0 or +2), **RD**
(running disparity; `!` = violation; `?` = not yet known in this window, or not a legal codeword), Kind (comma / robust-token / D-code / K-code /
NO_RESPONSE, colour-coded), decoded value, and **Meaning** (the symbol's role in its
Command Transport phase — `PhaseID: WRITE`, `Address[31:24]`, `Manager_CRC16_H`).
Follows the playhead via windowed re-decode; selecting a command scrolls its comma to the
top. **Scroll up** to decode-and-prepend earlier history across config sections.
The pane frames the CDS with **Devices ▸ Hub Depths…**, as the Commands table does. A
device N hubs deep has its response token read 2·N CDS bits later, so its Ping status, Write
or Commit response slot shows the token the Commands table reports.

### Samples
Each reconstructed audio sample as a row: its **MSB** bus row, Time, the port that
carried it, and the value in binary / hex / signed decimal. Windowed around the playhead;
click a row to seek. Filter by **data port** and **channel** (multi-select) and by a
**value predicate** (e.g. `Decimal ≤ 0`). Binary is grouped in fours from the least
significant bit, matching the Hex digits; the value filter takes decimal (leading zeros
allowed) or a `0x`/`0o`/`0b` prefix, and a value that does not parse is outlined in red and
not applied.

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
peripheral), data ports, or columns (CDS / Column 0 is included by default; **All excl. CDS** removes it). When a commit
changes the bus clock rate mid-capture (e.g. `2col → 16col`) a **region** selector scopes
the measurement to one rate. **View Worst UI** moves the playhead to the tightest
transitions so you can inspect them in Capture. Aggregate over the capture — no
playhead of its own.

### Statistics
Derived metrics: clock / row rate, per-dataport SSP intervals and bandwidth, the
bus-config segment summary, and link `PM_Action` events.

Rows are grouped into collapsible sections (Link Control / Regions / Commands / Peripheral
Reports / Data Ports; Peripheral Reports holds what each device reports about itself) — click a section title to fold it. A capture whose Link is brought up again lists
each later bring-up below the first as a `Bring-up N` row (its sequence and the time its
strip begins), followed by that bring-up's Sequence, Selected PHY, Safe-Lock and section
5.2.3 timing rows, indented. In **Commands**, each command kind is itself
expandable: click it for the breakdown.

- **Ping** — per peripheral, how many of each response state: `PING_ATTACHED`,
  `PING_ATTACHED_BUSY`, `PING_ALERT`, `PING_ALERT_BUSY`, `NO_RESPONSE`. A device that
  never answered collapses to a single `NO_RESPONSE` line.
- **Reads / Writes** — per device: how many commands, how many **bytes** read or
  written (counted from the payload actually on the wire, so a failed or deferred read
  contributes none), and an error count where there are any.

Folds are remembered, so a playhead move or a re-decode doesn't collapse what you opened.

### Cursors
The user's **paired-cursor measurements** (distinct from Statistics, which is
whole-capture facts). One row per cursor pair (A, B, …) — the two members and their
**delta** (rows / time) — with each row's text coloured to match its timeline marker.
Click a row to move the playhead to that pair's left member. Place and step cursors with
⌘B / ⌘] / ⌘[ (**Cursors ▸ Toggle Cursor at Playhead / Next Cursor / Previous Cursor**);
pairs form as you add the second mark of a letter. **Locate Sub-Capture** (below) also
places cursors, one per match.

### Sub-capture search
Locate Sub-Capture compares PHY1 and PHY2 captures; a PHY3 (DLV) capture, as main or sub,
is refused with a message, because its data line is the complement of its clock. It is
refused while a capture is still being decoded.

**File ▸ Locate Sub-Capture…** finds every place a smaller reference capture
recurs in the open one and places a cursor at each, labelled `Sub i/N` with its score. It reads the reference file behind the progress dialog ("Reading the reference capture…"), then searches; a file that cannot be read is reported as Locate failed. Pick the reference file(s) — any
format Open Capture accepts, selecting **both** files together for a `.bin` or `.wfm`
pair. A short form of the Open Capture page follows (*Locate Sub-Capture: the reference*):
pick the reference's clock and data, and optionally a window of it, then **Search**.
Matching is **sample-rate independent** (it compares the transition *pattern* — the
data bits at each clock edge and the clock's gap shape — not absolute sample counts) and
requires a **near-exact** match of *both* the data and the clock pattern, so only genuine
occurrences are reported; it also retries with clock/data swapped in case the reference
was captured with the opposite orientation. When nothing clears the bar, it still
places a cursor on the single best candidate and reports its score, so you can judge how close it
came. The search runs off the UI thread with a progress dialog. The matches' cursors are placed
on the Link that holds the searched capture when the search finishes. If that capture was
replaced while the search ran, the results are discarded and the status bar says so.

### Timeline
A strip across the whole capture: command marks coloured by kind, translucent
**bus-config bands** by column count (so a `2col → 8col` cold-start reconfiguration is
visible at a glance), and the playhead. Click to seek; scroll up/down to zoom, left/right
to pan. **Cursors** (⌘B / ⌘] / ⌘[) mark and step through points. **Right-click a
bus-config band ▸ Rename…** to name that region, for example `Playback`: the band then
reads `Playback · 8col`. An empty name clears it; the name is the Link's and is saved in the
workspace. Like a pinned column count it belongs to the region's number, so a re-decode that
changes which regions exist can leave it on a different one.

**A Link that goes inactive and is brought up again.** When the clock stops (for over 1 ms)
and a new start sequence follows, each bring-up is found, in a capture that starts in audio
mode as well, and the decode starts again where its audio begins, as at the start of the
capture: framed at the selected PHY's Safe-Lock column count, in a new config region labelled
by that PHY (e.g. `PHY2 Safe-Lock-2`), whether a Cold or a Warm Start opened it. Nothing is
decoded while the Link is inactive. A **Cold Start** resets the bus: from its Bus Reset the
registers read their reset values, so the Registers and Bus Grid panes show only what is
configured after it. A **Warm Start** keeps the registers, and the data ports carry on. The
Timeline draws the stopped stretch as an **Idle** band followed by the new bring-up's phases;
the Bus Grid reads **Link Inactive** in the stopped stretch, then **No PHY Selected** through a
Cold Start's sequence or **PHY2 Reused** (the PHY named) through a Warm Start's. The Registers
pane marks the PHY of the bring-up at the playhead. An imported config CSV stays the
configuration through every bring-up. On a PHY3/DLV capture the recovered clock locks again
at each bring-up's first Row Sync, so it restarts the same way (e.g. `PHY3 Safe-Lock-4`). A
bring-up that switches between the forwarded clock (PHY1, PHY2) and DLV is not followed:
the decode runs on through it, and it is not drawn.

## Opening a capture

**File ▸ Open Capture…** (⌘O) picks the file — one capture, or **both** channel files of a
`.bin` or `.wfm` pair, selected together — and then shows one page, before anything is
decoded:

- **Source** — what the file is: its format, channels and sample rate, its **length**, and
  roughly how much memory loading it all takes for the Links listed (each is loaded and
  kept, so two Links cost twice one), marked *fits* or *does not fit*. The figure is the one
  the loader checks before it reads, so what the dialog calls fitting is admitted. For every
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
  a **Time Offset** (ms) placing this file's time zero on theirs.

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
  **Time Offset**, or later (below).
- Links may differ in PHY, column count and row rate.
- To try it without a capture: **File ▸ Open Demo Capture ▸ PHY2**. It opens
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
playhead: each shows the same instant on its own Link.

**The active Link** is the one the menus act on, and it is drawn bold on the Timeline. It is
the Link of the pane you last clicked in. A menu that belongs to a pane always acts on that
pane's Link:
- **Audio**, the capture items of **File** (Export Capture, Locate Sub-Capture), and
  Decode's Dataport Scramblers and Manual SSP: the signal panes' Link.
- The grid items (**File ▸ Export Visualizer CSV / Export Bus Grid Image / View Bus Grid in
  Visualizer**, **Decode ▸ Import Visualizer CSV / Force Column Count**) and the grid's own
  toolbar: the Bus Grid's Link.
- **Devices** (Device Names, Register Maps, Hub Depths) and Compare: the Registers group's Link.
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
  the playhead's instant on their own Link. Their time axes and Time columns read shared time
  (each Link's offset applied); the Capture and Audio panes cover the whole span of every
  Link, and zooming or panning one moves the others with it, ticks at the same times.
  Clicking, dragging a cursor, picking a row or a Timing pane's **View Worst UI** in a
  Link's pane acts on that Link, and makes it the active one. One Audio pane plays at a
  time. Each Timing pane keeps its own region, filter and edge-flavour colours.
- The **Bus Grid** stays one Link at a time: it has its own picker.

Picking a Link in the picker (or View ▸ Links ▸ Link n) goes back to one Link, and the
panes go back to their one-Link height.

**The Timeline** stacks one band per Link on a shared time scale. Zooming or panning any
band moves them all. With two or more Links each band is labelled with its Link: click the
label to show that Link everywhere, or double-click it to rename the Link. Drag the dock's edge to make the
bands taller or shorter.

**Cursors** belong to the Link they were placed on, and are drawn only on that Link: on
its Timeline band, and in Audio and Capture when they show it. Clicking a band makes that
Link the active one, so **⌘B** places the next cursor on the band you clicked.
Next / Previous Cursor step through the active Link's cursors.

A pair can span two Links, which is how an event on one bus is timed against another:
click Link 1's band at the first event and press ⌘B (A1), then click Link 2's band at
the second and press ⌘B (A2). The **Cursors** pane has a **Link** column naming each
cursor's Link ("Link 1 → Link 2" for a pair across two). The pair always shows the time
between them. It shows rows only when both Links run the same row rate for the whole time
between the two cursors, and UIs only when they run the same UI rate for that time, so
where either rate changes on either Link in between, that count is not shown. Each Link must also have
been recording for that whole time. On one Link, rows and UIs are counted, so they are
always shown. Clicking a row shows its cursor on its own Link.

**Time offsets.** Each Link's time offset places its capture on the shared time.
- When adding, the Open Capture dialog's **Time Offset** places the new Links.
- **Set Time Offset…** on a Link's timeline label (right-click) enters it in µs, ns, ms, s
  or ps. Positive means that Link's capture started later.
- **Cursors ▸ Align Links on Cursor Pair…** sets it from events. Put one cursor on each Link at
  the same physical event (⌘B on one, then on the other) and choose the pair. The
  second Link moves so the two coincide.
- Changing an offset moves only the time mapping. Nothing is re-decoded, and each Link's
  cursors move with it.

Right-click a Link's timeline label for **Rename Link…** (double-click renames too),
**Set Time Offset…**, **Remove Link…** and **Show This Link Everywhere**. A removed Link takes its
cursors with it, after a confirmation. With two or more Links, exports are named for the
Link they come from, for example `commands_Amp_bus.csv`, and a saved workspace reopens
every Link with its name, offset and pane layout.

## Partial captures & finding the SSP

A **partial capture** starts *after* the bus was configured — the setup
`WriteA32`/commit sequence and the Stream Sync Point (SSPA/SSCR) are not on the wire.
Two things are then missing:

1. **Port geometry** — the decoder can't snoop it. Supply it with **Decode ▸ Import
   Visualizer CSV…** — pick a config CSV and choose **Update** to impose its data-port
   config on the open capture, or **Compare** to overlay it in the Register Map (the current
   Visualizer authoring can be compared, not imposed). Compare shows each register the CSV
   names with its expected value, in the CSV Import colour, in place of the staged value;
   hover the row to see what the register held without the overlay: the value the bus wrote or read back, its reset value when the bus never wrote it, or the value a CSV import or a manual edit set. The config drives the decode from row 0,
   so audio reconstructs. DeviceNumber_REG must be 0-11 for a peripheral port; a port with
   any other device number is ignored. A capture with **several config regions** (a cold start's
   Safe-Lock-2 → 8 → 16 columns) takes the CSV into the region under the playhead **only**:
   that region decodes with the CSV's ports and width, and every other region, and every
   command, decodes as the wire says. The region is labelled `8col (CSV)` in the timeline,
   its registers show as *CSV* in the Register Map, and the import is saved in the
   workspace. **Decode ▸ Remove … from This Region** takes it out again (the item names the
   CSV, and is enabled only while the playhead's region has one). Use the **TX map** (Bus Grid ▸ Show Toggles) to see which
   columns actually carry data and confirm the config matches the wire.

   If the grid still shows the wrong number of columns — the wire never carried a
   `NumColumns` commit to snoop, or the blind column detector mis-locked — pin the width
   with **Decode ▸ Force Column Count…**. It applies to the config region under
   the playhead **only**, so a capture that genuinely changes geometry mid-stream keeps
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

   - **Decode ▸ Manual SSP Move ▸ Set SSP at Playhead Row**, then **Move SSP +1 / −1 Row**
     (⌘L / ⌘K) to step the phase until the audio is clean. The phase repeats
     every interval, so stepping by 1 cycles all N.
   - **Clear Manual SSP** returns to the default anchor.
   - Manual SSP only re-phases audio when a config CSV is applied (it needs the port
     geometry); on a capture with no decodable audio it changes nothing.

The applied CSV and the chosen SSP row are saved in the workspace and restored on reopen.

## Large captures

A `.sal` is a ZIP of delta-coded transitions, so it expands enormously on open: the inflated
payload is several times the file, and the edge arrays it decodes to several times that.
Before opening anything, Studio predicts the peak memory from the ZIP directory alone — no inflating, no
decoding — and the Open Capture dialog shows it against the budget; when all of it would
not fit, the dialog opens on a **time window** that does.

- **The prediction is for a whole-file decode as it is now**: the inflated payload plus
  twice the edge arrays (the result, and one block's worth of deltas while it is built). A load
  predicted to fit the budget below is attempted.
- **The budget is 60% of free memory.** A load predicted past it is not attempted, and the
  dialog opens on a window that fits. A load that fits but is predicted past 8.6 GB (8 GiB)
  is labelled *large, so opening may be slow* and is yours to open whole or not.
- **Every format is checked against the same budget.** A digital or scope CSV, a `.wfm`
  pair and a `.bin` pair are refused before they are read when their predicted peak would
  not fit; a VCD,
  which has no index, stops loading as soon as its edges pass the budget. These formats are
  read whole whatever the window, so the remedy is a shorter export or more free memory.
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
- **For a `.sal`, a window's memory is the window's, not the file's**, including near the
  end of a long capture, and however large Logic 2 made its blocks. A window is priced by
  the blocks it overlaps, at most one edge per byte of their data and one per sample, not by
  its share of the capture's length, so a window over the busy end of a capture that idles
  for most of its length is priced at what it holds. Reading still runs from
  the file's start, so a window near the end takes longer to reach than one near the start.
  A channel whose data does not walk as a block chain (a damaged file) is read whole whatever
  the window, and its whole size counts toward every window's estimate; the refusal message
  names that channel.
- If Studio cannot offer a window (a file with no block index) it says so rather than
  attempting the load; export a shorter range from Logic 2 instead.

Two things help independently of memory: close the panes you are not reading (a hidden pane
does no work on a playhead move), and keep Capture's *Show Samples* overlay off when zoomed
far out.

## Menus

**Mode** comes first, and every other menu shows only what applies to the mode: the Bus
Visualizer and Timing Calculator have Mode, File, View and Help; the Bus Analyzer adds
Commands, Decode, Devices, Audio and Cursors. There is no Links menu: a Link is named and
added in **Open Capture**, its own actions are on its timeline label's right-click, and
showing Links is **View ▸ Links**. **⌘O / ⌘S** (Ctrl on Windows and
Linux) open and save the current mode's file. Items that act on a capture are greyed out
until one is open, submenus included (Commands ▸ Filter, Decode ▸ Manual SSP Move, the
playback device and bit depth, and Audio's per-stream submenus, which also need audio);
opening files, the View menu and Help are always available; **Waveform Colors and Line
Weight…** and **Preferences…** are offered in the Bus Analyzer only, with or without a
capture. The Capture, CDS, Audio, Samples and Timing panes are the **signal
panes**: one pane group, with its own Link picker.

- **Mode** — *Bus Visualizer* (⌘1), *Timing Calculator* (⌘2), *Bus Analyzer* (⌘3).
- **File**, Bus Analyzer — *Open Capture…* (⌘O: a Logic 2 `.sal`, a digital `.csv`, a
  `.vcd`, a Tektronix analog `.csv`, or **both** channel files of a `.bin` or `.wfm` pair,
  multi-selected — see **Opening a capture**); *Open Demo Capture ▸ PHY1, PHY2, PHY2 (Flow
  Control) or PHY3 (DLV)* (see **Demo captures** below); *Open / Save Workspace…*
  (⌘S, a `.swi3s` file; older `.json` workspaces still open; see **What a workspace keeps**
  below); *Export Capture…* (one dialog — pick the format `.sal` / `.bin` / CSV, which signals + their names, the range (whole capture or a
  time / bus-row / UI window), and the output file; in the Bus rows and UI ranges, To is the
  last row (or UI) included, and From and To may be equal to export one row; a Time range runs
  from From up to To, and a To at the end of the capture keeps the last edge); *Locate Sub-Capture…*; *Export
  Visualizer CSV…*, *Export Bus Grid Image…* (SVG/PNG; a name typed without an extension takes
  the chosen format's; a grid whose PNG raster would exceed 1 GiB (about 268 million pixels) is
  refused, so export a very large grid as SVG or draw fewer rows) and *View Bus Grid in Visualizer*.
  Every Save and Export dialog (workspace, bus config CSV, bus model JSON, timing settings JSON,
  commands CSV, Visualizer settings CSV, Export Capture Browse) adds the format's suffix to a
  name typed without it; an extension that is not the format's (a dot inside a name such as
  `run 1.5`) does not count as one. The grid image keeps `.svg` or `.png` as typed, and a
  workspace keeps `.json`. A suffix the app adds to a name that names an existing file asks
  'Replace it?'.
- **File**, Bus Visualizer — *Open / Save Visualizer CSV…* (⌘O / ⌘S, the authoring CSV);
  *Export Grid Image…*; *Export Bus Model (JSON)…* (the placed bus model).
- **File**, Timing Calculator — *Open / Save Timing Settings…* (⌘O / ⌘S, the calculator's
  inputs as JSON, incl. the selected PHY).
- **File** ends with *Quit* (⌘Q) in every mode. Ctrl+C in the terminal that launched the
  app quits it the same way; if that quit hangs, a second Ctrl+C ends the process at once.
- **Commands** — *Filter* (expression search; multi-select Commands / Devices / Groups,
  with Commands' quick picks All ⌘4, None ⌘5, All excl. Ping ⌘6, Commit ⌘7; Errors Only);
  *Next / Previous SSCR/DSCR Commit* (⌘> / ⌘<); *Export as CSV…*.
- **Decode** — everything that changes what the capture decodes to, so each re-decodes:
  *Import Visualizer CSV…* (impose a config CSV on the capture from row 0, or on the
  region under the playhead when the capture has several, or compare it,
  or the authored config, in the Register Map — clear the comparison with the **Clear
  Compare** button there); *Remove … from This Region* (drop the playhead region's CSV);
  *Force Column Count…* (pin the bus column count for the config
  region under the playhead); *Dataport Scramblers…* (Auto / On / Off per device and data
  port); **Manual SSP Move** (see above).
- **Devices** — *Device Names…*; *Register Maps…* (import peripheral vendor maps); *Hub
  Depths…*.
- **Audio** — *Playback Output Device / Bit Depth / Decimation*; *Filter & Gain* (a
  stream's DC blocker, high-pass and gain); *Export…* (WAV).
- **Cursors** — *Toggle Cursor at Playhead / Next Cursor / Previous Cursor / Clear Cursors*
  (⌘B / ⌘] / ⌘[); *Align Links on Cursor Pair…* (two or more Links).
- **View** — in the Bus Analyzer, show/raise any pane (Timeline, Registers, Statistics,
  Cursors, Bus Grid, Capture, CDS, Audio, Samples, Timing); *Maximize Pane* / *Restore
  Panes* (⌘⇧M: the selected pane alone in the window, and back; see **Analysis panes**; in
  the Timing Calculator, the Timing Inequalities given the whole page; in the Bus Visualizer,
  the grid);
  **Links** (two or more Links:
  *Commands: All Links* ⌘⌥0, *Signal Panes: All Links* ⌘⌥⇧0, and one
  entry per Link, ⌘⌥1…9, to show it in every pane); **Appearance** (Follow System /
  Light / Dark); *Waveform Colors and Line Weight…* (Ctrl+, on Windows and Linux; on macOS also
  **Preferences…**, which macOS shows in the application menu as Settings…, ⌘,): the line
  colours, per theme, for data ports, cursor pairs, the Capture DP/DN traces and the
  Timing edge flavours, and the Audio and Capture line weights (1–4 px). A colour below 3:1 contrast on that theme's plot is marked
  ⚠ and still allowed; *Reset to Defaults* restores all of them.
- **Help** — *SWI3S Studio User Guide* (this guide); *Keyboard Shortcuts…* (including
  **Playhead navigation**); *Timeline Marks Legend…* (Bus Analyzer: what each Timeline mark means, the **Playhead** and
  **Cursor** among them); *About SWI3S Studio* (on macOS, in the
  application menu).

**Demo captures.** Each **File ▸ Open Demo Capture** entry decodes a synthetic capture and
opens it with the workspace bundled with it (`data/demos/phy1.swi3s`, `phy2.swi3s`,
`phy2_flow_control.swi3s`, `phy3.swi3s`): named data ports (for example `AMP1`, `AMP2`,
`MIC1` / `MIC2` or `MIC`) and config regions, the Audio pane's channels and zoom, the Commands
filters, the Capture pane's view, the Registers pane's opened blocks and filters, and the
dock layout.
- **PHY1** — a slow 4-column PHY1 bus whose ports are repositioned mid-capture.
- **PHY2** — two Links: a PHY2 bus stepping 2 → 8 → 16 columns as Link 1, and the PHY2 (Flow
  Control) demo as Link 2, starting about 2.47 ms later on the shared timeline (see
  **Multiple Links**).
- **PHY2 (Flow Control)** — a PHY2 bus whose data ports are named for their flow control:
  `No Flow Control`, `TX Synchronous`, `RX Synchronous`, `Asynchronous`.
- **PHY3 (DLV)** — the differential DLV bus, its bit clock recovered.

**Imports at a glance.** Digital sources (`.sal`, digital `.csv`, `.vcd`, `.bin`) and
analog scope sources (`.wfm`, analog `.csv`) all open through *Open Capture*. Analog
volts are thresholded to logic with an automatic mid-rail + 10% hysteresis (or the
dialog's thresholds), and — with exactly two channels, left as offered — the forwarded
clock is assigned by transition count. Channels are paired into Links in the Open Capture
page (see **Opening a capture**). Digital-vs-analog `.csv` is decided by the data values (channels that are strictly 0/1 read as digital). A digital `.csv` starts with a header row (a file whose first row starts with a number is refused), and a bracketed unit in its Time header (`Time [ms]`, `[us]`, `[ns]`, `[ps]`) is honoured; seconds are the default. The open
dialog reopens in the last-used folder, tracked separately per mode (the Visualizer
defaults to `./visualizer_examples`); a large capture shows an n/N decode-progress bar. Closing the window while a capture is decoding, or opening a different capture, cancels the decode in progress; the status bar and the decode dialog read "Closing: cancelling the decode…" until it has stopped (within a fraction of a second). A re-decode of the open capture is not cancelled by opening another capture; it finishes first.

### Import warnings

When an import goes ahead but part of the file was not read as written (rows skipped, times
sorted, a rate assumed, a value masked to its register field, a setting that no longer
applies), an **Import warnings** window opens, titled with the file, listing each warning,
and the status bar gives the count. Each import has its own window; it is not modal, so the
app stays usable while it is open, and its lines can be selected and copied (⌘C). Captures,
Visualizer config CSVs, register maps, workspaces and timing settings all report this way. A
reopened workspace lists its own warnings in one window, and each of its captures in its
own. A file that cannot be read at all is still reported as an error.

### What a workspace keeps

A saved workspace reopens the window as it was when it was saved:

- **Each Link:** its capture and everything that changes how it decodes (register pins,
  hub depths, scramblers, device names and register maps), its name and offset, its
  Commands filter and column widths, its Audio pane's checked channels, channel-list width,
  zoom, Vertical Zoom, playback decimation and which channels are spectrograms (and their
  frames), its
  Capture pane's zoom and RSP marker, Samples and legend toggles, its streams' colours and
  Filter & Gain, and its data-port and region names.
- **The window:** the cursors, the playhead, the mode, which Link each pane group shows and the
  All Links choices, which panes are open, how they are docked and tabbed and which tab is
  in front, their sizes, the window's size and position, the timeline's zoom, the Commands
  sort and hidden columns, the Samples filters, the Registers device, opened blocks,
  provenance filter and column widths, the Statistics sections folded, and the Timing pane's region, driver
  filter and hidden edge kinds; and the Bus Visualizer and Timing Calculator inputs.

Each capture file is recorded relative to the workspace file, so a folder holding both can
be moved or shared, and where it was when saved, in case the workspace moves alone. If a
capture is in neither place, you are asked to **Locate…** it (any other missing file of the
same name in the folder you pick is found too), to **Skip This Link** and open the rest, or
to **Cancel**. A skipped Link is not opened and is not reported as an error; if every Link is
skipped, nothing opens.

The playback output device and bit depth are not in it: they belong to the computer, not
the capture. A window larger than the screen it reopens on is fitted to that screen. Each
Link also keeps its own Audio and Capture view while another Link is shown, and through a
re-decode. A workspace file that is not writable is not overwritten: the save fails with a
message and the file is left as it was.

## Row / Time reference points

Table Row/Time columns mark the **start** of an entity, so a playhead sample lines the
panes up: a Commands row is the command's opening SPM comma (the row's rising-edge Row Sync
Point); a CDS row is the first of the symbol's 10 bits; a Samples row is the sample's MSB. Bus rows are **0-based** throughout (the first row is
0). The register map reflects the committed state as-of the playhead.
