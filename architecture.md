# SWI3S Visualizer Architecture

This document describes the architecture of the SWI3S Visualizer, a tool for visualizing MIPI SoundWire I3S data port configurations.

## Table of Contents

1. [Overview](#overview)
2. [Module Organization](#module-organization)
3. [Data Flow](#data-flow)
4. [Core Components](#core-components)
   - [Model Layer](#model-layer)
   - [Core Engine](#core-engine)
   - [UI Layer](#ui-layer)
5. [DataPort Architecture (Detailed)](#dataport-architecture-detailed)
6. [FlowControlPort Architecture](#flowcontrolport-architecture)
7. [Validators](#validators)
8. [Key Design Patterns](#key-design-patterns)
9. [Performance Optimizations](#performance-optimizations)
10. [Appendix: SlotType Reference](#appendix-slottype-reference)

---

## Overview

The SWI3S Visualizer is a Python application that renders SoundWire I3S data port configurations. It supports:

- **GUI Mode**: Interactive tkinter-based interface for real-time visualization
- **Headless Mode**: Command-line batch processing for automated testing

The architecture follows a clean separation between:
- **Model Layer** (`src/models/`): Pure hardware models and algorithms, no UI dependencies
- **Core Engine** (`src/core/`): Business logic that transforms configuration into bus model
- **UI Layer** (`src/ui/`): Presentation and user interaction

```
┌─────────────────────────────────────────────────────────────────┐
│                         Entry Point                              │
│                   swi3s_visualizer.py                           │
│            (CLI parsing, mode selection)                        │
└─────────────────────────────────────────────────────────────────┘
                              │
              ┌───────────────┴───────────────┐
              ▼                               ▼
    ┌─────────────────┐             ┌─────────────────┐
    │   Headless Mode │             │    GUI Mode     │
    │  (batch JSON)   │             │   (tkinter)     │
    └────────┬────────┘             └────────┬────────┘
             │                               │
             └───────────────┬───────────────┘
                             ▼
    ┌─────────────────────────────────────────────────────────────┐
    │                    Core Engine                               │
    │               BusModelBuilder                                │
    │    (Transforms Interface + VizConfig → BusModel)            │
    └─────────────────────────────────────────────────────────────┘
                             │
                             ▼
    ┌─────────────────────────────────────────────────────────────┐
    │                    Model Layer                               │
    │   Interface, DataPort, FlowControlPort, Device, BusModel    │
    │          (Pure data, no UI dependencies)                     │
    └─────────────────────────────────────────────────────────────┘
```

---

## Module Organization

```
src/
├── __init__.py
├── config/                 # Constants and configuration
│   ├── __init__.py
│   └── constants.py        # SpecialDevices, CSVFields, ranges
│
├── core/                   # Core engine (UI-independent)
│   ├── __init__.py
│   └── engine.py           # BusModelBuilder
│
├── drawing/                # Rendering support
│   ├── __init__.py
│   ├── canvas_renderer.py  # Tkinter canvas drawing
│   └── clash_detector.py   # Bus clash detection (uses SlotOccupancyType)
│
├── io/                     # File I/O
│   ├── __init__.py
│   ├── csv_handler.py      # CSV load/save
│   └── json_handler.py     # JSON export
│
├── models/                 # Data models (NO UI CODE — see CLAUDE.md policy)
│   ├── __init__.py
│   ├── bit_slot.py         # BitSlotData, BitSlotState
│   ├── bus_model.py        # BusModel, BitInfo, ClashType
│   ├── dataport.py         # DataPort, DataPortConfig, DataPortState
│   ├── flow_control_port.py # FlowControlPort, FlowControlPortConfig, FlowControlPortState
│   ├── device.py           # Device abstraction
│   ├── enums.py            # SlotType, DirectionType, FlowMode, TransportPhase, PortMode, DisplayField
│   ├── frame.py            # Frame_model (legacy grid representation, still used by renderer/json_handler)
│   ├── interface.py        # Interface configuration (owns data_ports + flow_control_ports)
│   └── manager.py          # System slot layout
│
├── ui/                     # User interface
│   ├── __init__.py
│   ├── minimal_app.py      # Main application window
│   ├── app_ui.py           # UI composition
│   ├── frame_renderer.py   # Frame visualization
│   ├── parameter_panel.py  # Configuration widgets
│   ├── error_panel.py      # Notifications/warnings
│   ├── helpers.py          # UI utilities, tooltips
│   ├── constants.py        # UI-specific constants
│   ├── theme.py            # Color themes
│   ├── dialogs/            # Modal dialogs
│   │   ├── channel_selector.py
│   │   ├── device_selector.py
│   │   ├── flow_mode.py
│   │   ├── guard_selector.py
│   │   ├── port_mode.py
│   │   └── display_options.py
│   └── widgets/            # Reusable widgets
│       └── tooltip.py
│
├── utils/                  # Utilities
│   ├── __init__.py
│   ├── descriptors.py      # ValidatedInt, ValidatedBool
│   ├── logging_config.py   # Logging setup
│   ├── platform.py         # Platform detection
│   └── validators.py       # Ranges + settings validators (see Validators section)
│
└── viz/                    # Visualization config
    ├── __init__.py
    └── dataport_viz.py     # VizConfig, DataPortVizConfig
```

---

## Data Flow

### Configuration Loading

```
CSV File
    │
    ▼
CSVHandler.load_csv()
    │
    ├──► Interface (hardware registers)
    │        ├── NumColumns_REG, PHY3Enabled, etc.
    │        ├── data_ports: List[DataPort]
    │        └── flow_control_ports: List[FlowControlPort]  (parallel to data_ports)
    │
    └──► VizConfig (visualization settings)
             ├── rows_to_draw
             └── data_ports: List[DataPortVizConfig]  (enable_handover defaults True)
```

### Frame Building

```
Interface + VizConfig
    │
    ▼
BusModelBuilder.build()
    │
    ├── 1. Validate interface configuration (InterfaceValidator)
    │
    ├── 2. Add system slots (S0, S1, CDS, tails, handovers)
    │
    ├── 3. Process data ports by device priority
    │       │
    │       └── For each enabled data port:
    │               │
    │               ├── DataPortValidator.validate() — ranges + settings
    │               │
    │               ├── dp.initialize()   # Seed DP state, advance into interval 0
    │               ├── fcp.initialize()  # Seed FCP state
    │               │
    │               └── For each bit position (row, column):
    │                       │
    │                       ├── dp.fetch_bit_slot()   # DP emits DATA/TX_PRESENT/guard/tail/EMPTY
    │                       ├── fcp.fetch_bit_slot()  # FCP emits DRQ/guard/tail/EMPTY
    │                       │
    │                       └── Both written to BusModel; bus model's SAME_DEVICE clash
    │                           detector surfaces any (DP, FCP) overlap
    │
    ├── 4. Generate handover indicators (post-processing pass)
    │
    ├── 5. Validate TxP/DRQ pairs
    │
    └── 6. Finalize clashes and warnings
            │
            ▼
        BusModel
            ├── bits: List[BitInfo]
            ├── bus_clashes, device_clashes
            ├── txp_mismatches, drq_mismatches
            └── validation_issues
```

### Rendering (GUI Mode)

```
BusModel
    │
    ▼
FrameRenderer.render()
    │
    ├── Iterate rows
    │       │
    │       └── Get bits for row from BusModel
    │               │
    │               └── Merge consecutive same-slot bits
    │                       │
    │                       └── Draw on canvas
    │
    └── Apply clash highlighting
```

---

## Core Components

### Model Layer

The model layer is **strictly UI-independent**. No tkinter imports, no dialogs, no message boxes. This allows:
- Headless batch processing
- Unit testing without GUI
- Future web or CLI interfaces

Additionally, `dataport.py` and `flow_control_port.py` are **hardware models** (see `CLAUDE.md` — Hardware Model Policy). They hold only state a real chip would hold — registers, transport/row counters, phase, channel/bit pointers. No engine-helper state (cross-interval counters, transport-index tracking, or flags that exist solely to signal the engine) lives in these modules.

#### Interface (`src/models/interface.py`)

Top-level configuration container:

```python
class Interface:
    # Frame structure
    NumColumns_REG: int          # Columns per row (excess-1)
    phy3_enabled: bool           # PHY3 mode (S0/S1 enabled)

    # System slot timing
    s0_width, s1_width: int
    CDS_BitWidth_REG: int
    tail_width: int

    # Device management
    devices: Dict[int, Device]   # Device number → Device
    data_ports: List[DataPort]              # 12 DPs (derived property)
    flow_control_ports: List[FlowControlPort]  # 12 FCPs, parallel index
```

The parallel `flow_control_ports` list is owned by `Interface` (not `DataPort`) so that the DP stays a pure hardware model. Each FCP stores a back-reference to its parent DP to read `FlowMode_REG`, `PortDirection_REG`, and `Interval_REG`.

#### Device (`src/models/device.py`)

Device abstraction for grouping data ports:

```python
class Device:
    device_num: int              # -1=Manager, -2=Universal, 0-11=Peripheral
    _data_ports: List[DataPort]

    @property
    def priority(self) -> int:   # Processing order (Manager=0, then peripherals)
```

Special device numbers:
- `-1` (MANAGER): System manager, processes first
- `-2` (UNIVERSAL): CDS system slots
- `-3` (VISUALIZER): Handover indicators (post-processing)
- `0-11`: Peripheral devices

#### BusModel (`src/models/bus_model.py`)

Sequential bus representation:

```python
@dataclass
class BitInfo:
    bit_index: int               # Global index (row * num_columns + column)
    slot: SlotType               # DATA, GUARD_0/1, TAIL, CDS, DRQ, TX_PRESENT, etc.
    direction: DirectionType     # SOURCE (write) or SINK (read)
    device: int                  # Device number
    dp: Optional[int]            # Data port index (None for system slots)
    sample, channel, bit: int    # Data position within stream
    clash: ClashType             # NONE, SAME_DEVICE, DIFFERENT_DEVICE

@dataclass
class BusModel:
    num_rows, num_columns: int
    bits: List[BitInfo]
    bus_clashes: List[int]
    device_clashes: List[int]
    validation_issues: List[Tuple[str, ValidationResult]]
    # ... other validation categories
```

### Core Engine

#### BusModelBuilder (`src/core/engine.py`)

Transforms configuration into bus model. Composes DP and FCP emissions — both are iterated once per column:

```python
class BusModelBuilder:
    def __init__(self, interface, num_rows, viz_config):
        self.interface = interface
        self.num_rows = num_rows
        self.viz_config = viz_config
        self.clash_detector = ClashDetector(interface.num_columns)
        self.bus_model = BusModel(num_rows, interface.num_columns)
        self._dp_validator = DataPortValidator(interface)  # Reused across DPs

    def build(self) -> BusModel:
        self._validate_interface()
        self._add_system_slots()
        for device in priority_order:
            for dp in device.data_ports:
                self._process_data_port(dp)        # Iterates DP + FCP in lock-step
        self._generate_viz_handovers()
        self._finalize_clashes()
        return self.bus_model
```

The engine maintains a small amount of state per DP for visualization-only concerns that the hardware model intentionally does NOT track — most notably a transport index used to reconstruct absolute sample ordinals, and the bits-emitted counter used to detect SRI row-cut resumes (see [DataPort Architecture](#dataport-architecture-detailed)).

### UI Layer

#### FrameRenderer (`src/ui/frame_renderer.py`)

Canvas-based frame visualization:

```python
class FrameRenderer:
    def render(self, bus_model: BusModel, canvas: tk.Canvas):
        for row in range(bus_model.num_rows):
            bits = bus_model.get_bits_in_row(row)
            merged = self._merge_consecutive_bits(bits)
            for merged_bit in merged:
                self._draw_slot(canvas, merged_bit)
```

---

## DataPort Architecture (Detailed)

The `DataPort` class (`src/models/dataport.py`) implements the hardware state machine that emits one bit slot per bus column. The engine calls `dp.fetch_bit_slot()` for every frame position.

### Class Structure

```
DataPort
    │
    ├── config: DataPortConfig    # Register values + derived properties
    ├── _state: DataPortState     # Runtime state (column, row, phase, counters)
    │
    └── Public API:
            initialize()         → hardware init / arm first interval
            fetch_bit_slot()     → BitSlotState at current position, auto-advance
            row_in_interval      → current row within this DP's interval
```

There is no separate algorithm class — the state machine methods live directly on `DataPort`.

### DataPortConfig

Holds register values and derived properties. Derived values (like enabled channel count, effective channel grouping) live as `@property` methods so they always reflect the current register state:

```python
class DataPortConfig:
    # Hardware registers (from CSV / UI)
    _EnableCh_REG: int           # 16-bit channel enable bitmask (property with cache invalidation)
    SampleSize_REG: int          # Bits per sample (excess-1)
    Interval_REG: int            # Rows per interval
    HorizontalStart_REG: int
    HorizontalCount_REG: int     # Excess-1: window = [HStart, HStart + HCount]
    ChannelGrouping_REG: int
    Spacing_REG: int
    SubRowInterval_REG: bool
    FlowMode_REG: int            # 0=Normal, 1=TxCtrl, 2=RxCtrl, 3=Async
    PortDirection_REG: bool      # True=Sink, False=Source
    # ... plus BitWidth_REG, Offset_REG, TailWidth_REG, GuardEnable_REG,
    #         GuardPolarity_REG, SkippingNumerator_REG, PortMode_REG, ScramblerEn_REG

    # Cached (enabled_channels_tuple, num_channels). None = dirty.
    _channel_cache: Optional[tuple[tuple[int, ...], int]]

    @property
    def _num_channels(self) -> int:
        """Count of enabled channels (derived from EnableCh_REG)."""
        return (self._channel_cache or self._compute_channel_cache())[1]

    @property
    def _enabled_channels(self) -> tuple[int, ...]:
        """Tuple of enabled channel indices (derived from EnableCh_REG)."""
        return (self._channel_cache or self._compute_channel_cache())[0]

    @property
    def _effective_channel_grouping(self) -> int:
        """ChannelGrouping_REG clamped to num_channels when register is 0 or oversized."""
        if self.ChannelGrouping_REG == 0 or self.ChannelGrouping_REG > self._num_channels:
            return self._num_channels
        return self.ChannelGrouping_REG

    @property
    def _emits_txp(self) -> bool:
        """True iff FlowMode prepends a TX_PRESENT slot before each (channel, sample)'s DATA bits."""
        return self.FlowMode_REG in (FlowMode.TX_CONTROLLED, FlowMode.ASYNC)

    @property
    def _emits_drq(self) -> bool:
        """True iff FlowMode activates the FCP's DRQ path."""
        return self.FlowMode_REG in (FlowMode.RX_CONTROLLED, FlowMode.ASYNC)
```

### DataPortState

Mutable runtime state. `DataPortState.initialize(config)` seeds every field from the current register values; `DataPort.initialize()` chains it with the first interval-start (`_advance_interval`) so the DP is ready to emit:

```python
class DataPortState:
    # Position (column is row-local; row_in_interval is interval-local)
    column: int
    row_in_interval: int

    # Transport lifecycle
    phase: TransportPhase           # ACTIVE | SPACING | ROW_DONE | PATTERN_DONE
    interval_skipped: bool          # Latched by _advance_skipping at interval start

    # Skipping accumulator persists across intervals (not reset per interval)
    skipping_accumulator: int

    # Transport-scope counters (set by _reset_transport)
    sample_in_group: int            # 0..SampleGrouping_REG within current transport
    samples_in_group_remaining: int
    channel_index: int
    channel_group_base: int
    channels_in_group_remaining: int
    bit: int                        # Current bit within (channel, sample)
    wide_bit_remaining: int         # Innermost cascade counter
    txp_pending: bool               # True → next emission is TX_PRESENT (not DATA)
    spacing_slots_remaining: int

    # Row-scope queue (cleared at row wrap / primed after owned DP emissions)
    post_data_queue: deque[SlotType]   # Deferred GUARD/TAIL tokens
```

There is no `channel_group_size` state field — the current group size comes from `config._effective_channel_grouping`, computed on demand.

**Sample tracking is external to DataPort.** `DataPortState` holds only `sample_in_group` (transport-scoped, 0..SG). The engine maintains a per-DP transport counter by observing DP state transitions — it detects a fresh transport by matching the unique post-`_reset_transport` state signature (every counter at its fresh-transport value + `phase == ACTIVE`), and distinguishes SRI row-cut resumptions from genuine new transports by comparing bits-emitted to the full transport bit count. The absolute/global sample ordinal in labels is reconstructed externally:

```
bits_per_transport = _num_channels
                   × (SampleSize_REG + 1 + (1 if _emits_txp else 0))
                   × (SampleGrouping_REG + 1)
                   × (BitWidth_REG + 1)

global_sample = max(0, transport_index_at_emit - 1) × (SampleGrouping_REG + 1)
              + sample_in_group
```

This matches real hardware, where the DP tracks only its position within the current transport pattern and has no knowledge of cross-interval sample ordinals (those are a DMA/source-side concept).

### Counter Cascade

The state machine is a nested counter cascade. Each `_advance_*` method decrements its counter; on exhaustion, the counter rolls over and the next-outer counter advances:

```
wide_bit → bit → channel → sample → channel_group → transport completion
```

```python
def _advance_wide_bit(self) -> None:
    self._state.wide_bit_remaining -= 1
    if self._state.wide_bit_remaining < 0:
        self._state.wide_bit_remaining = self.config.BitWidth_REG
        self._advance_bit()

def _advance_bit(self) -> None:
    if self._state.txp_pending:
        self._state.txp_pending = False
        return                       # TxP fires once per (channel, sample); no bit decrement
    self._state.bit -= 1
    if self._state.bit < 0:
        self._state.bit = self.config.SampleSize_REG
        self._advance_channel()

def _advance_channel(self) -> None:
    self._state.channel_index += 1
    self._state.channels_in_group_remaining -= 1
    self._state.txp_pending = self.config._emits_txp   # Re-arm for next (channel, sample)
    if self._state.channels_in_group_remaining < 0:
        self._state.channel_index = self._state.channel_group_base
        self._state.channels_in_group_remaining = self.config._effective_channel_grouping - 1
        self._advance_sample()

def _advance_sample(self) -> None:
    self._state.sample_in_group += 1
    self._state.samples_in_group_remaining -= 1
    if self._state.samples_in_group_remaining < 0:
        self._state.sample_in_group = 0
        self._state.samples_in_group_remaining = self.config.SampleGrouping_REG
        self._advance_channel_group()
```

`_advance_channel_group` either starts the next CG in the same transport, or — if the pattern is complete — ends the transport (non-SRI) or resets for the next transport in the same row (SRI). Inter-group spacing is set up via `spacing_slots_remaining = Spacing_REG - 1` (the counter decrements to 0 before phase returns to ACTIVE).

Every new interval triggers `_advance_interval`, which latches the skipping decision (via `_advance_skipping`) and runs `_reset_transport` to seed the transport-scope fields from config.

### Transport Phase Lifecycle

`TransportPhase` has four values:

| Phase | Meaning |
|---|---|
| `ACTIVE` | Emitting data inside the horizontal window |
| `SPACING` | Inter-channel-group / SRI inter-transport gap (counter > 0) |
| `ROW_DONE` | Horizontal window closed on this row; transport still alive |
| `PATTERN_DONE` | Transport complete or interval skipped |

`ROW_DONE` means the window closed mid-pattern — on row wrap it flips back to `ACTIVE` so the fresh row resumes emission. This covers both SRI row-cuts and non-SRI multi-row transports. `PATTERN_DONE` persists until the next row-counter rollover, where `_advance_interval` arms a fresh transport (or latches `interval_skipped` if the skipping accumulator says this interval is skipped).

**Normal Mode** (one transport per interval):

```
Row N:     [ACTIVE] ─(all CGs done)─> [PATTERN_DONE]
              │
              v
         [ROW_DONE]    (column > _horizontal_end on this row)
              │
         (row wrap, still mid-interval)
              v
          [ACTIVE]     (next row resumes window)

Row wrap:  [PATTERN_DONE] ─(row-counter rollover → _advance_interval)─> [ACTIVE]
```

**SRI Mode** (multiple transports per row):

```
Col C:   [ACTIVE] ─(CG done)─> [SPACING] ─(counter == 0)─> [ACTIVE] ...
                                    │
                                    └──(column > _horizontal_end)──> [ROW_DONE]
                                                                         │
                                                          (row wrap)     v
                                                                    [ACTIVE]
```

### fetch_bit_slot() Flow

`fetch_bit_slot()` is the single public entry point for emission. Four layers:

1. **Passive gate** — if `num_channels == 0`, `interval_skipped`, `phase ∈ {ROW_DONE, PATTERN_DONE}`, `row_in_interval < Offset_REG`, or `column < HorizontalStart_REG`, return an EMPTY slot (no fresh data this tick).
2. **Data probe** — otherwise call `_data_slot()`, which handles the horizontal-end cut (sets `ROW_DONE`), the SPACING decrement, and the DATA / TX_PRESENT emission with the counter cascade.
3. **Post-data queue** — on an EMPTY result, drain any pending GUARD/TAIL tokens queued by the previous owned emission. Primed by `_prime_post_data_queue()` (only for SOURCE-direction DPs — sinks don't drive guards/tails).
4. **Column advance** — always step the column cursor, wrapping to the next row at the right edge (which may trigger row-counter rollover into `_advance_interval`).

The wide-bit hold is handled by the innermost cascade counter (`wide_bit_remaining`): the same bit emits for `BitWidth_REG + 1` columns before the bit cursor advances. No cached `BitSlotState` — the slot is recomputed from current state on every emission.

### Channel Grouping

Channel grouping creates a burst/space pattern within the transport:

```
ChannelGrouping=2, NumChannels=4, Spacing=2

[Ch0][Ch1] [--] [--] [Ch2][Ch3] [--] [--]
└─group 1─┘ └spacing─┘ └─group 2─┘ └spacing─┘
```

With sample grouping, each channel group processes multiple samples before spacing:

```
ChannelGrouping=2, SampleGrouping=1 (2 samples), Spacing=2

[Ch0.S0][Ch1.S0][Ch0.S1][Ch1.S1] [--][--] [Ch2.S0][Ch3.S0][Ch2.S1][Ch3.S1]
└────────────── group 1 ───────────────┘ └────── group 2 ─────────────────┘
```

---

## FlowControlPort Architecture

The `FlowControlPort` class (`src/models/flow_control_port.py`) emits DRQ + optional guards/tails in `RX_CONTROLLED` / `ASYNC` flow modes. It is an **independent peer** of the DataPort on the bus — both are iterated by the engine in lock-step, and any overlap is surfaced by the bus model's SAME_DEVICE clash detector (no arbitration lives in the core loop).

The FCP mirrors the DataPort's hardware-model structure: it owns its own `column` / `row_in_interval` tracking, exposes a single `initialize()` / `fetch_bit_slot()` public API, and uses the same counter-cascade idiom.

### Class Structure

```
FlowControlPort
    │
    ├── config: FlowControlPortConfig   # FCP-specific registers (no FCP_ prefix inside class)
    ├── _state: FlowControlPortState    # column, row_in_interval, drq_sent, queues
    ├── _dataport: DataPort             # Back-ref for FlowMode / PortDirection / Interval
    │
    └── Public API:
            initialize()         → reset FCP state
            fetch_bit_slot()     → BitSlotState at current position, auto-advance
```

### Emission Priority

`_data_slot()` evaluates slot sources in strict priority order:

1. **Wide-bit replay** — if a prior DRQ's replay is still active, emit the stored slot
2. **DRQ trigger** — if `_emits_drq` and we're at `(Offset_REG, HorizontalStart_REG)` and `drq_sent` is not yet latched, emit `DRQ` with direction opposite to the DP's data direction (Sink DP → SOURCE DRQ, Source DP → SINK DRQ). Prime the post-data queue with guard + tails (SOURCE DRQs only), set up wide-bit replay, and latch `drq_sent`
3. **EMPTY** otherwise

Guards and tails drain via the same `post_data_queue` pattern as DataPort, making the two modules architecturally parallel. The queue is primed inside `_data_slot`'s DRQ-trigger branch (not from `fetch_bit_slot`) because wide-bit replays are continuations of the same DRQ and must NOT re-prime.

### Lifecycle

- **`_advance_column`** → wraps to `_advance_row` at the right edge
- **`_advance_row`** → clears wide-bit replay state; increments `row_in_interval`; wraps to `_advance_interval` when `row_in_interval > Interval_REG`
- **`_advance_interval`** → clears `drq_sent` so the next interval's DRQ can fire
- **`_advance_wide_bit`** → decrements replay counter; clears stored slot on exhaustion (terminal; unlike DP's, does NOT cascade to `_advance_bit`)

Because FCP owns its own row/interval counters, the engine no longer passes `column` / `row_in_interval` into `fetch_bit_slot()` and no longer orchestrates per-row or per-interval reset callbacks.

---

## Validators

`src/utils/validators.py` splits validation into two deliberately separated categories:

### Range checks — hardware register bit-field bounds

Each register's value is checked against its declared min/max (e.g., `SampleSize_REG` in `[0, 31]`, `ChannelGrouping_REG` in `[0, 15]`). In real hardware these bounds are enforced by the register bit widths themselves; we check them because the visualizer lets users type arbitrary values via UI / CSV.

Range checks are shallow — one `_check_range()` call per register, no cross-field logic.

### Settings checks — spec-level semantic requirements

Each rule is a single method with a docstring written as a SHALL-statement, suitable for lifting into written requirements. There are 17 rules today (14 DataPort + 2 FCP + 1 Interface):

- `_check_offset_within_interval`, `_check_sri_interval_zero`, `_check_sri_skipping_disabled`, `_check_sri_pattern_fits`
- `_check_horizontal_start_within_columns`, `_check_horizontal_count_within_columns`, `_check_horizontal_window_within_columns`
- `_check_tail_fits_row`, `_check_bitwidth_fits_remaining_columns`, `_check_bitwidth_fits_horizontal_count`, `_check_horizontal_count_divisible_by_bitwidth`, `_check_guard_fits_row`
- `_check_sink_no_guard`, `_check_sink_no_tail`
- `_check_fcp_offset_within_interval`, `_check_fcp_fits_row`
- `_check_phy3_requires_even_columns`

All settings checks produce `ErrorSeverity.ERROR`. Shared computations (`_effective_channel_grouping`, `_drive_in_group`, `_last_data_column`) are hoisted into helpers so each rule method stays atomic.

Each validator has a single `validate()` entry point that runs `_validate_ranges()` then `_validate_settings()` and returns a combined `ValidationResult`. Validation does not gate engine emission — results are stored on `bus_model.validation_issues` for UI display.

---

## Key Design Patterns

### 1. Hardware-Model Purity

`DataPort` and `FlowControlPort` hold only state real hardware would hold. Anything the engine or renderer needs that isn't hardware-natural (cross-interval sample counters, transport indices, SRI-resume flags) is computed externally in the engine, NOT added to the model. This keeps the model layer independently testable against spec behavior and makes it safe to reuse in headless tools.

### 2. Counter Cascade

Both DataPort and FlowControlPort use the same pattern: each `_advance_*` method owns one counter, decrements it, and cascades to the next outer counter on rollover. State seeds live on the config (e.g., `SampleSize_REG`, `BitWidth_REG`, `_effective_channel_grouping`) — the `_advance_*` method resets its counter from config on rollover rather than relying on an external "reset" pass.

### 3. Property Caching with Invalidation

Expensive derived values (currently just the enabled-channels tuple + count) are cached in a single `Optional` tuple on `DataPortConfig`. The `EnableCh_REG` setter invalidates the cache:

```python
@EnableCh_REG.setter
def EnableCh_REG(self, value: int) -> None:
    if self._EnableCh_REG != value:
        self._EnableCh_REG = value
        self._channel_cache = None   # Invalidate
```

Lightweight derived properties (`_effective_channel_grouping`, `_emits_txp`, `_emits_drq`, `_horizontal_end`) are computed on each access — cheap enough that caching would be premature.

### 4. Enum-Based Type Safety

Enums replace string comparisons for type safety and performance. `clash_detector.py` uses `SlotOccupancyType` internally:

```python
class SlotOccupancyType(Enum):
    WRITE = "write"
    READ = "read"
    GUARD = "guard"
    TAIL = "tail"
    HANDOVER = "handover"
    TXP_SOURCE = "txp_source"
    TXP_SINK = "txp_sink"
    DRQ_SOURCE = "drq_source"
    DRQ_SINK = "drq_sink"
```

The bus slot-type enum is `SlotType` in `src/models/enums.py`.

### 5. Separation of Concerns

- **Configuration** (`DataPortConfig`, `FlowControlPortConfig`): What the hardware registers say
- **State** (`DataPortState`, `FlowControlPortState`): Where the state machine is now
- **Algorithm** (methods on `DataPort` / `FlowControlPort`): How to compute the next slot
- **Validation** (`src/utils/validators.py`): Whether the configuration is sane — split into ranges and settings
- **Visualization** (`DataPortVizConfig`): How to display it (enable_handover, display fields, etc.)

---

## Performance Optimizations

### 1. Cached Enabled Channels

`DataPortConfig._channel(index)` is called thousands of times per frame. The tuple of enabled channel indices is computed once and cached; `EnableCh_REG`'s setter invalidates the cache:

```python
def _compute_channel_cache(self) -> tuple[tuple[int, ...], int]:
    enabled = tuple(i for i in range(16) if self._EnableCh_REG & (1 << i))
    self._channel_cache = (enabled, len(enabled))
    return self._channel_cache
```

### 2. Reusable Validator

`DataPortValidator` is created once in `BusModelBuilder.__init__()` and reused across all data ports in a build:

```python
# Created once
self._dp_validator = DataPortValidator(interface)

# Reused per DP
for dp in data_ports:
    self._dp_validator.validate(dp, dp_index)
```

### 3. Inline Counter Cascade

The `_advance_*` cascade avoids method-call overhead by cascading only on rollover. In steady state a single emission costs one decrement and one compare (the innermost `_advance_wide_bit`).

---

## Appendix: SlotType Reference

| SlotType | Value | Description |
|----------|-------|-------------|
| EMPTY | -1 | Position not owned |
| DATA | 0 | Regular data bit |
| GUARD_0 | 1 | Guard bit (polarity 0) |
| TAIL | 2 | Tail bit |
| HANDOVER | 3 | Direction change indicator |
| CDS | 4 | Control Data Stream |
| S0 | 5 | S0 synchronization (PHY3) |
| S1 | 6 | S1 synchronization (PHY3) |
| GUARD_1 | 7 | Guard bit (polarity 1) |
| CLASH | 8 | Bus clash marker |
| TX_PRESENT | 9 | TxP flow control bit |
| DRQ | 10 | Data request flow control bit |
