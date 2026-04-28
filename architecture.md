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

- **GUI Mode**: Interactive tkinter / customtkinter interface for real-time visualization
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
│   ├── frame.py            # FrameModel (legacy grid representation, still used by renderer/json_handler)
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
    │                       ├── dp.clock_tick()   # DP advances one UI, drives device.write_*/read_*/held_*
    │                       ├── fcp.clock_tick()  # FCP advances one UI, drives device.write_drq/read_drq/held_*
    │                       │
    │                       │   Before each tick, engine sets device._active_port and clears
    │                       │   device._current_slot; each write_*/read_*/held_* hook records
    │                       │   a BitSlotState into _current_slot. None after the tick = EMPTY.
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

The `DataPort` class (`src/models/dataport.py`) implements the hardware state machine that advances one UI per call. The engine invokes `dp.clock_tick()` for every column; the DP drives `device.write_*`/`read_*`/`held_*` hooks which record the `BitSlotState` the visualizer renders into `Device._current_slot`.

### Class Structure

```
DataPort
    │
    ├── config: DataPortConfig    # Register values + derived properties
    ├── state: DataPortState      # Runtime state (column, row, phase, counters)
    │
    └── Public API:
            initialize()         → hardware init / arm first interval
            clock_tick()         → advance one UI; drives device.write_*/read_*/held_* for the current slot

Consumers that need row-within-interval or interval-skipped status read `dp.state.row_in_interval` / `dp.state.interval_skipped` directly — there are no pass-through properties on `DataPort`.
```

There is no separate algorithm class — the state machine methods live directly on `DataPort`. Bus I/O is delegated to the parent `Device` via no-arg methods (`device.write_data_bit_from_fifo()`, `device.held_write_bit()`, `device.held_read_bit()`, `device.read_data_bit_to_fifo()`, `device.write_txp()`, `device.read_txp()`, `device.write_guard0/1()`, `device.write_tail()`). Each hook records a `BitSlotState` into `Device._current_slot` describing the slot the DP just drove; the default held hooks extend the most recent slot recorded for the active port (stored per-port in `Device._last_slot_per_port`). The engine sets `Device._active_port` before each tick and reads `_current_slot` after — `None` means EMPTY. Hardware-realistic harnesses can subclass `Device` to additionally drive a real bus.

### DataPortConfig

Holds register values and derived properties. Derived values (like enabled channel count, effective channel grouping) live as `@property` methods so they always reflect the current register state:

```python
class DataPortConfig:
    # Hardware registers (from CSV / UI)
    EnableCh_REG: int            # 16-bit channel enable bitmask (plain int)
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

    # `EnableCh_REG` is a plain int bitmask; no cache.

    @property
    def _num_channels(self) -> int:
        """Count of enabled channels (popcount of EnableCh_REG)."""
        return bin(self.EnableCh_REG).count('1')

    @property
    def _effective_channel_grouping(self) -> int:
        """ChannelGrouping_REG clamped to num_channels when register is 0 or oversized."""
        if self.ChannelGrouping_REG == 0 or self.ChannelGrouping_REG > self._num_channels:
            return self._num_channels
        return self.ChannelGrouping_REG

    @property
    def _is_source(self) -> bool:
        """True iff this DP drives data onto the bus (PortDirection_REG: False=source, True=sink)."""
        return not self.PortDirection_REG

    @property
    def _txp_enabled(self) -> bool:
        """True iff FlowMode prepends a TX_PRESENT slot before each (channel, sample)'s DATA bits."""
        return self.FlowMode_REG in (FlowMode.TX_CONTROLLED, FlowMode.ASYNC)

    @property
    def _drq_enabled(self) -> bool:
        """True iff FlowMode activates the FCP's DRQ path."""
        return self.FlowMode_REG in (FlowMode.RX_CONTROLLED, FlowMode.ASYNC)
```

### DataPortState

Mutable runtime state. `DataPortState.initialize(config)` seeds every field from the current register values (delegating transport-scope fields to `initialize_transport(config)`); `DataPort.initialize()` chains it with the first interval-start (`_start_interval`) so the DP is ready to emit:

```python
class DataPortState:
    # Position (column is row-local; row_in_interval is interval-local)
    column: int
    row_in_interval: int

    # Transport lifecycle
    transport_phase: TransportPhase # PENDING | ACTIVE | SPACING | ROW_DONE | PATTERN_DONE
    interval_skipped: bool          # Latched by _advance_skipping at interval start

    # Skipping accumulator persists across intervals (not reset per interval)
    skipping_accumulator: int

    # Transport-scope counters (set by DataPortState.initialize_transport)
    sample_in_group: int            # 0..SampleGrouping_REG within current transport
    samples_in_group_remaining: int
    channel_index: int
    channel_group_base_channel: int
    channels_in_group_remaining: int
    bit_in_channel: int             # Current bit position within (channel, sample)
    wide_bit_remaining: int         # Innermost cascade counter
    txp_pending: bool               # True → next emission is TX_PRESENT (not DATA)
    spacing_slots_remaining: int

    # Post-data emission (primed after each owned source-port slot; drained
    # by _pop_guard_tail on subsequent not-owned columns)
    guard_pending: bool             # One GUARD UI pending
    tail_remaining: int             # Remaining TAIL UIs (fresh tail on first, held on rest)
```

There is no `channel_group_size` state field — the current group size comes from `config._effective_channel_grouping`, computed on demand.

**Sample tracking is external to DataPort.** `DataPortState` holds only `sample_in_group` (transport-scoped, 0..SG). The engine maintains a per-DP transport counter by observing DP state transitions — it detects a fresh transport by matching the unique post-`initialize_transport` state signature (every counter at its fresh-transport value + `phase == ACTIVE`), and distinguishes SRI row-cut resumptions from genuine new transports by comparing bits-emitted to the full transport bit count. The absolute/global sample ordinal in labels is reconstructed externally:

```
bits_per_transport = _num_channels
                   × (SampleSize_REG + 1 + (1 if _txp_enabled else 0))
                   × (SampleGrouping_REG + 1)
                   × (BitWidth_REG + 1)

global_sample = max(0, transport_index_at_emit - 1) × (SampleGrouping_REG + 1)
              + sample_in_group
```

This matches real hardware, where the DP tracks only its position within the current transport pattern and has no knowledge of cross-interval sample ordinals (those are a DMA/source-side concept).

### Counter Cascade

The state machine is a nested counter cascade. Each `_advance_*` method decrements its counter; on exhaustion, the counter rolls over and the next-outer counter advances:

```
wide_bit → bit_in_channel → channel → sample → channel_group → transport completion
```

```python
def _advance_wide_bit(self) -> None:
    self.state.wide_bit_remaining -= 1
    if self.state.wide_bit_remaining < 0:
        self.state.wide_bit_remaining = self.config.BitWidth_REG
        self._advance_bit_in_channel()

def _advance_bit_in_channel(self) -> None:
    if self.state.txp_pending:
        self.state.txp_pending = False
        return                       # TxP fires once per (channel, sample); no bit decrement
    self.state.bit_in_channel -= 1
    if self.state.bit_in_channel < 0:
        self.state.bit_in_channel = self.config.SampleSize_REG
        self._advance_channel()

def _advance_channel(self) -> None:
    self.state.channel_index += 1
    self.state.channels_in_group_remaining -= 1
    self.state.txp_pending = self.config._txp_enabled   # Re-arm for next (channel, sample)
    if self.state.channels_in_group_remaining < 0:
        self.state.channel_index = self.state.channel_group_base_channel
        self.state.channels_in_group_remaining = self.config._effective_channel_grouping - 1
        self._advance_sample()

def _advance_sample(self) -> None:
    self.state.sample_in_group += 1
    self.state.samples_in_group_remaining -= 1
    if self.state.samples_in_group_remaining < 0:
        self.state.sample_in_group = 0
        self.state.samples_in_group_remaining = self.config.SampleGrouping_REG
        self._advance_channel_group()
```

`_advance_channel_group` either starts the next CG in the same transport, or — if the pattern is complete — ends the transport (non-SRI) or resets for the next transport in the same row (SRI). Inter-group spacing is set up via `spacing_slots_remaining = Spacing_REG - 1` (the counter decrements to 0 before phase returns to ACTIVE).

Every new interval triggers `_start_interval` on `DataPort`, which latches the skipping decision (via `_advance_skipping`), calls `state.initialize_transport(config)` to seed the transport-scope fields from config, and sets `transport_phase = ACTIVE` (promoting the transient `PENDING` seeded by `initialize_transport`).

### Transport Phase Lifecycle

`TransportPhase` has five values:

| Phase | Meaning |
|---|---|
| `PENDING` | Transport-scope counters seeded; awaiting `_start_interval` to promote to ACTIVE |
| `ACTIVE` | Emitting data inside the horizontal window |
| `SPACING` | Inter-channel-group / SRI inter-transport gap (counter > 0) |
| `ROW_DONE` | Horizontal window closed on this row; transport still alive |
| `PATTERN_DONE` | Transport complete or interval skipped |

`PENDING` is transient: `DataPortState.initialize_transport` sets it whenever the transport-scope fields are (re-)seeded, and `DataPort._start_interval` immediately promotes it to `ACTIVE`. Under correct caller discipline it is never visible to the engine at `clock_tick` time.

`ROW_DONE` means the window closed mid-pattern — on row wrap it flips back to `ACTIVE` so the fresh row resumes emission. This covers both SRI row-cuts and non-SRI multi-row transports. `PATTERN_DONE` persists until the next row-counter rollover, where `_start_interval` arms a fresh transport (or latches `interval_skipped` if the skipping accumulator says this interval is skipped).

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

Row wrap:  [PATTERN_DONE] ─(row-counter rollover → _start_interval)─> [ACTIVE]
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

### clock_tick() Flow

`clock_tick()` is the single public entry point for UI advance. It has three paths, gated by a positive `in_transport_window` check (`num_channels > 0` AND `not interval_skipped` AND `transport_phase ∉ {ROW_DONE, PATTERN_DONE}` AND `row_in_interval >= Offset_REG` AND `column >= HorizontalStart_REG`):

1. **Window-exhausted / SPACING** — in transport window but past `_horizontal_end` or in SPACING: flip phase (→ROW_DONE / decrement spacing_slots / →ACTIVE), fall through to drain.
2. **Owned slot** — DATA or TX_PRESENT. Source drives `device.write_txp()`/`write_data_bit_from_fifo()` on the first UI of the wide bit and `device.held_write_bit()` on subsequent UIs; sink calls `device.read_txp()`/`device.read_data_bit_to_fifo()` on the first UI and `device.held_read_bit()` on subsequent UIs (the visualizer records the slot for the whole wide-bit window; real hardware samples on the last UI and may override). Then `_advance_wide_bit()`, `_arm_guard_tail()`, advance column, return.
3. **Drain** — not owned (gated, window-exhausted, or SPACING): `_pop_guard_tail()` emits any pending GUARD (one UI) then TAIL (`TailWidth_REG` UIs; fresh `write_tail()` on first, `held_write_bit()` on rest). Always advance column.

Each hook records a `BitSlotState` into `Device._current_slot`; `held_write_bit` / `held_read_bit` reuse the most recent slot recorded for the active port (stored in `Device._last_slot_per_port`). The engine reads `_current_slot` after each tick and treats `None` as `EMPTY`. Hardware-realistic harnesses subclass `Device` to additionally drive a real bus.

The wide-bit hold is handled by the innermost cascade counter (`wide_bit_remaining`): the same bit emits for `BitWidth_REG + 1` UIs before the bit cursor advances.

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

The FCP mirrors the DataPort's hardware-model structure: it owns its own `column` / `row_in_interval` tracking, exposes a single `initialize()` / `clock_tick()` public API, and uses a DRQ replay sentinel (`drq_sent and wide_bit_remaining >= 0`). Its emissions are recorded into `Device._current_slot` through the same hooks the DP uses (DRQ-specific `write_drq`/`read_drq`, and shared `held_write_bit`/`held_read_bit`/`write_guard0/1`/`write_tail`) — the engine sets `Device._active_port = fcp` before each FCP tick so the hooks know whose slot they are building.

### Class Structure

```
FlowControlPort
    │
    ├── config: FlowControlPortConfig   # FCP-specific registers (FCP_*_REG)
    ├── state: FlowControlPortState     # column, row_in_interval, drq_sent,
    │                                   #   wide_bit_remaining, guard_pending, tail_remaining
    ├── _dataport: DataPort             # Back-ref for FlowMode / PortDirection / Interval / _is_source
    │
    └── Public API:
            initialize()         → reset FCP state
            clock_tick()         → advance one UI; drives device.write_drq/read_drq/held_*/write_guard0_1/write_tail
```

### Emission Priority

`clock_tick()` evaluates three paths in strict order:

1. **Wide-bit replay** — `drq_sent and wide_bit_remaining >= 0`: a prior DRQ is still on the bus. Source DRQ drives `device.held_write_bit()`; sink DRQ calls `device.held_read_bit()` every UI (the visualizer records the slot for the entire wide-bit window; real hardware samples on the last UI and subclasses may override). Then `_advance_wide_bit()` (pure decrement) and advance column.
2. **Fresh DRQ trigger** — `_drq_enabled` AND `not dp.state.interval_skipped` AND `not drq_sent` AND column/row match `(FCP_HorizontalStart_REG, FCP_Offset_REG)`. Source DRQ writes `device.write_drq()`; sink DRQ calls `device.read_drq()`. Then `_arm_drq_repeat()` sets `drq_sent`, primes guard/tail pending via `_arm_guard_tail()`, seeds `wide_bit_remaining = FCP_BitWidth_REG`, and calls `_advance_wide_bit()` once for this emission.
3. **Drain** — otherwise, emit one guard via `device.write_guard0()`/`write_guard1()` (flipping `guard_pending`) or one tail via `device.write_tail()` (first) / `device.held_write_bit()` (subsequent), then advance column. Drain is a no-op on sink DRQ (guard/tail only apply to source DRQ).

DRQ direction is inverted relative to DP data direction: Sink DP → DRQ SOURCE (FCP writes onto bus); Source DP → DRQ SINK (FCP samples bus). `Device._drq_direction()` resolves this from the parent DP's `PortDirection_REG` when building the DRQ slot.

### Lifecycle

- **`_advance_column`** → wraps to `_advance_row` at the right edge.
- **`_advance_row`** → clears `guard_pending`/`tail_remaining` and forces `wide_bit_remaining = -1` so any in-progress DRQ replay terminates at the row boundary (row wrap doesn't carry a replay forward). `drq_sent` persists across rows so a DRQ can only fire once per interval.
- **`_start_interval`** → calls `FlowControlPortState.initialize_transport()`, which clears `drq_sent` so the next interval's DRQ can fire and zeros `wide_bit_remaining`.
- **`_advance_wide_bit`** → pure decrement; terminal (unlike DP's cascade). When `wide_bit_remaining` drops below 0, the replay sentinel `(drq_sent and wide_bit_remaining >= 0)` naturally fails on next tick.

Because FCP owns its own row/interval counters, the engine no longer passes `column` / `row_in_interval` into `clock_tick()` and no longer orchestrates per-row or per-interval reset callbacks.

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

### 3. Lightweight Derived Properties

`EnableCh_REG` is a plain `int` bitmask on `DataPortConfig`. `_num_channels` is a popcount (`bin(EnableCh_REG).count('1')`) computed on demand — fast enough that no cache is needed. The channel-index → channel-number mapping (previously a cached tuple on the config) now lives as `Device._channel_from_index(config, index)`, called only when the device builds a DATA/TX_PRESENT slot.

Other lightweight derived properties (`_effective_channel_grouping`, `_is_source`, `_txp_enabled`, `_drq_enabled`, `_horizontal_end`) are also computed on each access — cheap enough that caching would be premature.

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

### 1. Popcount for Enabled-Channel Count

`DataPortConfig._num_channels` is read many times per frame. It is implemented as a single Python `bin(EnableCh_REG).count('1')` — no cache, no invalidation hook, and cheap enough that an explicit cache was measured to not help. The per-emission channel-index → channel-number lookup happens in `Device._channel_from_index()` and only runs when a DATA or TX_PRESENT slot is built (not every UI).

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
