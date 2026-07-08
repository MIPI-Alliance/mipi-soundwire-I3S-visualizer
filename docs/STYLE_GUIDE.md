# SWI3S Studio — Visual Style Guide

> Status: living document. The **Visualizer tab** is the reference look; this guide
> captures its design language so the same feel can be applied to the other tabs
> (Analysis, Timing) and any new UI. When you change the look, change it in
> `ui/theme.py` first and update this doc. Last reviewed against `ui/theme.py`,
> `ui/authoring/`, `ui/grid_view.py`, and the Analyzer views (`ui/command_table.py`,
> `register_view.py`, `symbol_view.py`, `audio_view.py`, `raw_view.py`,
> `measurements_view.py`, `timeline.py`).

## 1. Where the style lives

There is **one source of truth**: `swi3s_studio/ui/theme.py` (`VizTheme` +
`authoring_stylesheet()` + `SCROLLBAR_QSS`). The rule that keeps the app coherent:

> **Never scatter hex literals or radii through widgets.** Pull every colour,
> border, radius, and font size from `VizTheme`. If a value isn't there yet, add it
> there and reference it — don't inline it.

The look is a port of the standalone SoundWire I3S Visualizer's customtkinter dark
theme, so the vocabulary (frame / entry / accent / border) mirrors customtkinter.

## 2. Palette

All values are defined in `VizTheme`. Use the **name**, not the hex.

| Token | Hex | Role |
|---|---|---|
| `WINDOW_BG` | `#242424` (gray13) | Outermost window only |
| `FRAME_BG` | `#2b2b2b` (gray17) | **Everything**: panels, sections, fields, grid background |
| `ENTRY_BG` | `#2b2b2b` | Text entries / checkboxes / description — *same as the frame* |
| `BORDER` | `#565b5e` | Light-grey border for entries, DP chips, sub-panels |
| `TEXT` | `#dce4ee` | Primary light text |
| `TEXT_DIM` | `#7a848d` | Disabled / secondary text |
| `ACCENT` | `#1f6aa5` | Action buttons, selection highlight, checked checkbox |
| `ACCENT_HOVER` | `#144870` | Button hover |
| `CHECK_ON` | `#1f6aa5` | Checked checkbox fill |
| `CHECK_BORDER` | `#949ba2` | Unchecked checkbox border |
| `OK_GREEN` | `#388e3c` | Success / "no issues" notification |

### The defining principle — "fields are borders, not fills"

The single most important idea in this theme: **panels, sub-panels, text fields,
and the grid all share one background (`FRAME_BG`).** A text field is not a lighter
box on a darker panel — it's the *same* dark, defined only by its light 1px border
and rounded corners. This is what gives the Visualizer its calm, flat, unified
look. When styling a new widget, default its background to `FRAME_BG` and let a
`BORDER`-coloured outline do the separating.

## 3. Shape language — rounded, 1px, flat

| Token | Value | Applies to |
|---|---|---|
| `CORNER_RADIUS` | `6 px` | Entries, buttons, sub-panel frames, the bus-grid outer frame |
| `BORDER_WIDTH` | `1 px` | All borders |
| Checkbox radius | `4 px` | Checkbox indicator (slightly tighter than 6) |
| Scrollbar handle radius | `5 px` | Themed scrollbar handle |
| Grid corner radius | `_s(6)` | Bus-grid outer frame (scaled with the grid, see §6) |

Rules:
- **Everything rounds at 6px.** Buttons, fields, sub-panel frames, the grid frame.
  Smaller elements (checkbox 4, scrollbar 5) round slightly tighter so the curve
  reads proportionally, not absolutely.
- **Borders are always 1px and `BORDER`-coloured.** No 2px borders except as a
  transient state signal (e.g. the grid's diff/clash outlines, which intentionally
  shout).
- **Flat, no gradients, no drop shadows.** Depth comes from the border + radius,
  never from shading.

## 4. Typography

Helvetica/system sans throughout. Sizes are deliberately compact:

| Element | Size / weight | Source |
|---|---|---|
| Body labels, entries | `12 px` | `authoring_stylesheet()` |
| Panel-name headers | `15 px`, weight `600` | `_HEADER_CSS` |
| Page/tab title | `18 px`, weight `600` | `main_window` |
| Notification header | bold | `notifications.py` |
| Grid in-cell labels | scaled ~`10 px` | `grid_view._f_label` |
| Grid row/col numbers | scaled `12 px`, bold | `grid_view._f_num` |

Rule: **one step down from "comfortable".** Parameter/label/entry text is 12px (not
the OS default 13–14), which lets dense parameter grids breathe without scrolling.
Headers earn their size by weight (600), not by going large.

## 5. Component recipes

Copy these patterns rather than reinventing per-widget CSS.

### Action button (`_BTN_CSS`)
```
background: ACCENT; color: white; border: none;
border-radius: 6px; padding: 5px;
:hover  background: ACCENT_HOVER;
```
Blue, borderless, rounded, white text. Hover darkens to `ACCENT_HOVER`.

### Text entry / multi-line edit (`authoring_stylesheet`)
```
background: ENTRY_BG; color: TEXT;
border: 1px solid BORDER; border-radius: 6px;
padding: 1px 4px; font-size: 12px;
selection-background-color: ACCENT;
:disabled  color: TEXT_DIM;
```

### Checkbox — centred, text-less indicator
The indicator is the whole control (no trailing label). Centre it with
`spacing:0` + `subcontrol-position:center`, 16×16, 4px radius, `ENTRY_BG` fill with
a `CHECK_BORDER` outline. Checked → `CHECK_ON` fill + the bundled `assets/check.svg`
as `image:`. (See the comment block in `authoring_stylesheet()` — getting a Qt
checkbox truly centred is fiddly and that comment explains why.)

### Sub-panel frame (Description / Notifications)
Give the frame an `objectName` and scope the style to it so children don't inherit
the border:
```
#notifFrame { border: 1px solid BORDER; border-radius: 6px; background: FRAME_BG; }
```

### Scrollbars (`SCROLLBAR_QSS`)
Transparent track (no groove), rounded `#5a6068` handle (radius 5, min 32px),
hover `#6f7680`, **no arrow buttons** (`add-line`/`sub-line` sized to 0). Append
`SCROLLBAR_QSS` to any scrollable widget's stylesheet.

### Header label (`HEADER_CSS`)
`font-size:15px; font-weight:600;` — used for panel names, centred where it titles
a sub-panel. Page/mode titles use `TITLE_CSS` (18px/600). Both live in `VizTheme`.

### Tables & trees / item-views (`analyzer_stylesheet`)
`QTableView`/`QTreeView`/`QTableWidget`/`QTreeWidget` get **no theme by default** —
left alone they render with the platform's native (often light) chrome, which
clashes with the dark Visualizer. Apply `analyzer_stylesheet()` to the view (the
same way `authoring_stylesheet()` is applied to the AuthoringPanel) for:
- `FRAME_BG` body, `WINDOW_BG` alternating rows, `BORDER` 1px gridlines + a 6px
  rounded frame, `ACCENT` selection (white text), 12px text.
- `QHeaderView::section` on `WINDOW_BG` with `BORDER` separators — a flat dark header.
- `QPushButton` (the ACCENT recipe), themed `QComboBox`, centred `QCheckBox`, and the
  shared `SCROLLBAR_QSS` — so a whole view themes from one call.

Per-cell colour (error rows, provenance, symbol kinds) is set via the model/items
on top of this — see §8.

## 6. The bus grid (`grid_view.py`)

The grid is a `QGraphicsScene`, not styled widgets, but it follows the same palette
via `VizTheme.GRID_*`:

| Token | Hex | Role |
|---|---|---|
| `GRID_BG` | = `FRAME_BG` | Canvas / empty cells — same as the panels |
| `GRID_LINE` | `#8c9196` | Grid lines (a touch lighter than pure dark) |
| `GRID_TEXT` | `#c8ccd0` | Row/column numbers, key text |
| `GRID_INK` | `#0f0f12` | In-cell text (it sits on light pastel DP colours) |
| `GRID_CDS_FILL` | `#34373c` | CDS / full-height system fill (blends, but bordered) |

Conventions worth carrying elsewhere:
- **Scale, don't hardcode sizes.** Cell metrics derive from the Visualizer's base
  (`COLUMN_SIZE=39`, `ROW_SIZE=30`) through `_s(v)` with `_SCALE = 1.125` and
  round-half-up. Want a bigger grid? Change `_SCALE`, not 30 literals.
- **Rounded outer frame, square cells.** The grid frame rounds at `_s(6)` to match
  the design language, while interior cells stay square. The corners are produced by
  masking the square corner nubs with `FRAME_BG` rectangles (z=50) then stroking a
  rounded-rect path on top (z=51). **The masks extend ~2px *outward* past the frame
  edge** so they swallow the outer half of the 1px cell-border pen — otherwise a
  square nub pokes out beyond the arc. (This was a real bug; see the comment at
  `grid_view._draw_frame`.)
- **Pastel data colours ("Eddie's colours").** The 12-entry `_DP_PALETTE` (also in
  `authoring_panel._DP_COLORS`) colours data streams; dark `GRID_INK` text sits on
  top. Keep these two lists in sync.
- **State via colour, loudly.** Diffs and clashes use 2px outlines in saturated
  semantic colours (yellow=changed/device, red=removed/bus, green=decoded-only,
  blue=read-overlap). These are the *only* place we break the 1px / desaturated
  rule, precisely so they stand out.

## 7. Data-visualization palette (Analyzer plots, tables, timeline)

The Analyzer needs colours the base palette doesn't cover — waveform traces, symbol
classes, timeline marks, register provenance. These are *meaning-bearing*, so they're
legitimate; the rule is they live in `VizTheme` (one source of truth), **not** re-typed
as local literals per view. They were the main audit finding: the same value (plot
`#1e2023`, cursor `#f5f5f5`, axis grey) was retyped in three+ files.

| Group | Tokens | Used by |
|---|---|---|
| Canvas | `PLOT_BG` (`#1e2023`, slightly darker than panels), `CURSOR` (`#f5f5f5`), `AXIS` (`#787878`) | pyqtgraph plots, timeline, greyed text |
| Traces | `TRACE_PALETTE` (6 pens), `RAW_DP`, `RAW_DN` | audio waveforms, raw DP/DN lines |
| Semantic marks | `SEM_ERROR` / `SEM_ERROR_BG` / `SEM_SSP` / `SEM_COMMIT` / `SEM_READ` / `SEM_OTHER` / `SEM_PING` / `SEM_BOOKMARK` / `SEM_BRINGUP` | command table, symbol viewer, timeline ticks |
| Provenance | `PROV_DEFAULT` / `PROV_WRITTEN` / `PROV_CSV` / `PROV_UI` | register-map value column |
| Symbol kinds | `SYM_INVALID` / `SYM_COMMA` / `SYM_ROBUST` / `SYM_DCODE` / `SYM_KCODE` / `SYM_UNKNOWN` | CDS symbol viewer |

Rules:
- **Tokens are hex strings**, so the same value works in a Qt stylesheet *and* as
  `QColor(VizTheme.X)` / a pyqtgraph `mkPen` / `background=`.
- **Reuse, don't re-shade.** `SYM_COMMA` *is* `SEM_SSP`; trace cyan doubles as `RAW_DP`.
  If two views mean the same thing, they share the token.
- **One genuinely local exception is allowed**: a colour used in exactly one place and
  nowhere else (e.g. timeline's translucent `_CONFIG_BANDS`, its internal axis-label
  greys) may stay local — over-centralizing single-use values is its own anti-pattern.
  Anything shared or duplicated goes in `VizTheme`.

## 8. Layout & spacing

- **Tight, content-sized.** Spacings are small (`setSpacing(2–4)`), margins often
  zeroed (`setContentsMargins(0,0,0,0)`), and panels are fixed-width to their content
  (`setFixedWidth(320)` / `300` / button `180` / value cells `64`). The aim is a
  dense instrument panel, not an airy web page.
- **Reclaim vertical space.** Prefer a menu over a toolbar, drop the status bar where
  a transient label suffices, and size panes to content on first load. (Several recent
  Visualizer commits exist purely to claw back vertical room.)
- **Centre status/feedback text**, dim it (`TEXT` or `TEXT_DIM`), and word-wrap it —
  e.g. the "Loaded: init.csv" file-status label under the Notifications panel.

## 9. Applying this elsewhere — checklist

When styling a new tab or widget so it matches the Visualizer:

1. Set the widget background to `FRAME_BG`; don't introduce a new shade.
2. Define fields by a **1px `BORDER` outline + 6px radius**, not a fill.
3. Pull every colour/size from `VizTheme`; add new tokens there if needed.
4. Buttons → the ACCENT/hover recipe. Don't invent button colours.
5. Body text 12px; headers `HEADER_CSS` (15/600); page titles `TITLE_CSS` (18/600).
6. Tables/trees → `analyzer_stylesheet()`. Any other scroll area → append `SCROLLBAR_QSS`.
7. Meaning-bearing colours (traces, marks, provenance, kinds) → the §7 tokens, not literals.
8. Reserve 2px borders and saturated colours for **state signals** only.
9. Keep spacing tight and panels content-sized; reclaim vertical space.
