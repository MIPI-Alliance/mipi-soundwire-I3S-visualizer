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
6. [Key Design Patterns](#key-design-patterns)
7. [Performance Optimizations](#performance-optimizations)

---

## Overview

The SWI3S Visualizer is a Python application that renders SoundWire I3S data port configurations. It supports:

- **GUI Mode**: Interactive tkinter-based interface for real-time visualization
- **Headless Mode**: Command-line batch processing for automated testing

The architecture follows a clean separation between:
- **Model Layer** (`src/models/`): Pure data structures and algorithms, no UI dependencies
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
    │   Interface, DataPort, Device, BusModel, BitSlotState       │
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
│   └── clash_detector.py   # Bus clash detection
│
├── io/                     # File I/O
│   ├── __init__.py
│   ├── csv_handler.py      # CSV load/save
│   └── json_handler.py     # JSON export
│
├── models/                 # Data models (NO UI CODE)
│   ├── __init__.py
│   ├── bit_slot.py         # BitSlotData, BitSlotState
│   ├── bus_model.py        # BusModel, BitInfo, ClashType
│   ├── dataport.py         # DataPort, DataPortConfig, DataPortAlgorithm
│   ├── device.py           # Device abstraction
│   ├── enums.py            # SlotType, DirectionType, FlowMode
│   ├── frame.py            # Legacy Frame_model (deprecated)
│   ├── interface.py        # Interface configuration
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
│   └── validators.py       # Configuration validation
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
    │        └── data_ports: List[DataPort]
    │
    └──► VizConfig (visualization settings)
             ├── rows_to_draw
             └── data_ports: List[DataPortVizConfig]
```

### Frame Building

```
Interface + VizConfig
    │
    ▼
BusModelBuilder.build()
    │
    ├── 1. Validate interface configuration
    │
    ├── 2. Add system slots (S0, S1, CDS, tails, handovers)
    │
    ├── 3. Process data ports by device priority
    │       │
    │       └── For each enabled data port:
    │               │
    │               ├── dp.reset()           # Hardware reset + arm first interval
    │               │
    │               └── For each bit position:
    │                       │
    │                       ├── dp.fetch_bit_slot()  # Get slot info
    │                       │
    │                       └── Add to BusModel + ClashDetector
    │
    ├── 4. Generate handover indicators
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

#### Interface (`src/models/interface.py`)

Top-level configuration container:

```python
class Interface:
    # Frame structure
    NumColumns_REG: int          # Columns per row (excess-1)
    phy3_enabled: bool           # PHY3 mode (S0/S1 enabled)

    # System slot timing
    s0_width, s1_width: int      # PHY slot widths
    CDS_BitWidth_REG: int        # Control Data Stream width
    tail_width: int              # S1 tail width

    # Device management
    devices: Dict[int, Device]   # Device number → Device
    data_ports: List[DataPort]   # Flat list (derived property)
```

#### Device (`src/models/device.py`)

Device abstraction for grouping data ports:

```python
class Device:
    device_num: int              # -1=Manager, -2=Universal, 0-11=Peripheral
    _data_ports: List[DataPort]  # Data ports owned by this device

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
    slot: SlotType               # DATA, GUARD, TAIL, CDS, etc.
    direction: DirectionType     # SOURCE (write) or SINK (read)
    device: int                  # Device number
    dp: Optional[int]            # Data port index (None for system slots)
    sample, channel, bit: int    # Data position within stream
    clash: ClashType             # NONE, SAME_DEVICE, DIFFERENT_DEVICE

@dataclass
class BusModel:
    num_rows, num_columns: int
    bits: List[BitInfo]          # All bits (multiple per position allowed)
    bus_clashes: List[int]       # Different-device collisions
    device_clashes: List[int]    # Same-device conflicts
    # ... validation warnings
```

### Core Engine

#### BusModelBuilder (`src/core/engine.py`)

Transforms configuration into bus model:

```python
class BusModelBuilder:
    def __init__(self, interface: Interface, num_rows: int, viz_config: VizConfig):
        self.interface = interface
        self.num_rows = num_rows
        self.viz_config = viz_config
        self.clash_detector = ClashDetector(interface.num_columns)
        self.bus_model = BusModel(num_rows, interface.num_columns)
        self._dp_validator = DataPortValidator(interface)  # Reused

    def build(self) -> BusModel:
        self._validate_interface()
        self._add_system_slots()
        for device in priority_order:
            for dp in device.data_ports:
                self._process_data_port(dp)
        self._generate_viz_handovers()
        self._finalize_clashes()
        return self.bus_model
```

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

The `DataPort` class (`src/models/dataport.py`) is the most complex component, implementing a state machine that generates bit slot ownership as the engine iterates through frame positions.

### Class Structure

```
DataPort (Facade)
    │
    ├── config: DataPortConfig    # Register values, cached properties
    │
    ├── _state: DataPortState     # Runtime state (reset per frame)
    │
    └── _algorithm: DataPortAlgorithm  # State machine logic
            │
            └── fetch_bit_slot() → BitSlotState
```

### DataPortConfig

Holds register values and cached derived properties:

```python
class DataPortConfig:
    # Hardware registers (from CSV)
    _EnableCh_REG: int           # 16-bit channel enable bitmask
    SampleSize_REG: int          # Bits per sample (excess-1)
    Interval_REG: int            # Rows per transport (excess-1)
    HorizontalStart_REG: int     # Starting column
    HorizontalCount_REG: int     # Columns owned (excess-1)
    ChannelGrouping_REG: int     # Channels per group
    Spacing_REG: int             # Slots between groups
    SubRowInterval_REG: bool     # SRI mode enabled
    FlowMode_REG: int            # 0=Normal, 1=TxCtrl, 2=RxCtrl, 3=Async
    PortDirection_REG: bool      # True=Sink, False=Source

    # Cached derived values (invalidated when EnableCh_REG changes)
    _cached_enabled_channels: Optional[tuple]
    _cached_num_channels: Optional[int]

    @property
    def _num_channels(self) -> int:
        """Number of enabled channels (cached for performance)."""
        if self._cached_num_channels is None:
            self._cached_num_channels = self._EnableCh_REG.bit_count()
        return self._cached_num_channels

    @property
    def _enabled_channels(self) -> tuple:
        """Tuple of enabled channel indices (cached for performance)."""
        if self._cached_enabled_channels is None:
            self._cached_enabled_channels = tuple(
                i for i in range(16) if self._EnableCh_REG & (1 << i)
            )
        return self._cached_enabled_channels
```

### DataPortState

Mutable runtime state, grouped by reset scope. `DataPortState.reset()` zeros
the whole object; `DataPort.reset()` chains it with the interval-start
sequence. Narrower resets (row / interval / transport) are inlined in or
called from `DataPort._advance_row` and `_reset_transport`.

```python
class DataPortState:
    # Position
    column: int

    # Interval-scope
    row_in_interval: int
    phase: TransportPhase        # ACTIVE | SPACING | ROW_DONE | PATTERN_DONE

    # Cycles with the effective SSP interval (zeroed only at hardware reset)
    skipping_accumulator: int

    # Transport-scope (set by _reset_transport on each interval start and
    # on each SRI mid-row transport rollover)
    sample_in_group: int         # 0..SampleGrouping_REG within current transport
    samples_in_group_remaining: int
    channel_index: int
    channel_group_base: int
    channel_group_size: int
    channels_in_group_remaining: int
    spacing_slots_remaining: int
    bit: int
    wide_bit_remaining: int                # innermost counter; rolls over to _advance_bit
    txp_sent: bool

    # Row-scope (cleared at row wrap)
    post_data_queue: deque[SlotType]       # deferred GUARD/TAIL tokens
```

`phase` is the single source of truth for transport lifecycle. The parallel
`FlowControlPort` lives on `Interface.flow_control_ports` and owns its own
DRQ/guard/tail counters; it is not part of `DataPortState`.

**Sample tracking is external to DataPort.** `DataPortState` holds only
`sample_in_group` (transport-scoped, 0..SG). The engine maintains a per-DP
transport counter by observing DP state transitions — it detects a fresh
transport via the unique post-`_reset_transport` state signature and
distinguishes SRI row-cut resumptions by comparing bits-emitted to
`num_channels × (SampleSize+1) × (SG+1)`. The absolute/global sample
ordinal in labels is reconstructed as:

```
global_sample = max(0, transport_index_at_emit - 1) * (SampleGrouping_REG + 1) + sample_in_group
```

This matches real hardware, where a data port tracks only its position
within the current transport pattern and has no knowledge of cross-interval
sample ordinals (those are a DMA/source-side concept).

### DataPortAlgorithm

State machine that generates bit slot ownership. The engine calls `fetch_bit_slot()` for every frame position.

#### Counter Hierarchy

The algorithm manages nested counters:

```
Interval (rows)
    └── Channel Group
            └── Sample (within group)
                    └── Channel (within sample)
                            └── Bit (within channel)
```

When an inner counter exhausts, it triggers advancement of the outer counter:

```python
def _advance_bit(self):
    self._state.bit -= 1
    if self._state.bit < 0:
        self._advance_channel()

def _advance_channel(self):
    self._state.channel_index += 1
    self._state.channels_in_group_remaining -= 1
    if self._state.channels_in_group_remaining < 0:
        self._advance_sample()
    else:
        self._state.bit = self._config.SampleSize_REG

def _advance_sample(self):
    self._state.sample_in_group += 1
    self._state.samples_in_group_remaining -= 1
    if self._state.samples_in_group_remaining < 0:
        self._advance_channel_group()
    else:
        # Reset for next sample
        self._state.bit = self._config.SampleSize_REG
        self._state.channel_index = self._state.channel_group_base

def _advance_channel_group(self):
    if all_groups_complete:
        if self._config.SubRowInterval_REG:
            # SRI: prepare for next transport within row
            self._reset_transport()
            self._state.spacing_slots_remaining = self._config.Spacing_REG
        else:
            # Normal: transport complete
            self._end_transport_pattern()    # phase = PATTERN_DONE
    else:
        # Move to next group, restart sample counter at 0
        # (all CGs within a transport share the same samples 0..SG)
        self._state.channel_group_base += self._state.channel_group_size
        self._state.sample_in_group = 0
        self._state.spacing_slots_remaining = self._config.Spacing_REG
```

#### Transport Pattern Lifecycle

`TransportPhase` has four values. `ROW_DONE` means "horizontal window closed
on this row; pattern still alive" — on row wrap it flips back to `ACTIVE`
so the next row's fresh window resumes emission. `PATTERN_DONE` persists
until the next row-counter rollover, where `_start_interval` arms a fresh
transport (or keeps `PATTERN_DONE` if this interval is skipped).

**Normal Mode** (one transport per interval):

```
Row N:     [ACTIVE] ─(all CGs done)─> [PATTERN_DONE]
              │
              v
         [ROW_DONE]    (past HorizontalEnd on this row)
              │
         (row wrap, mid-interval)
              v
          [ACTIVE]     (next row resumes window)

Row wrap:  [PATTERN_DONE] ─(row-counter rollover → _start_interval)─> [ACTIVE]
```

**SRI Mode** (multiple transports per row):

```
Col C:   [ACTIVE] ─(CG done)─> [SPACING] ─(counter == 0)─> [ACTIVE] ...
                                    │
                                    └──(past HorizontalEnd)──> [ROW_DONE]
                                                                   │
                                                      (row wrap)   v
                                                              [ACTIVE]
```

#### fetch_bit_slot() Flow

`fetch_bit_slot()` combines four layers:

1. **Passive gate** — if `num_channels == 0`, `phase ∈ {ROW_DONE,
   PATTERN_DONE}`, `row_in_interval < Offset_REG`, or
   `column < HorizontalStart_REG`, return an EMPTY slot (no fresh data
   this tick).
2. **Data probe** — otherwise call `_data_slot()`, which handles the
   horizontal-end cut (sets `ROW_DONE`), the SPACING decrement, and the
   DATA / TX_PRESENT emission with the counter cascade
   `wide_bit → bit → channel → sample → channel_group`.
3. **Post-data queue** — on an EMPTY result, drain any pending
   GUARD/TAIL tokens queued by the previous owned emission.
4. **Column advance** — always step the column cursor, wrapping to the
   next row at the right edge (which may trigger the interval-start
   sequence at row-counter rollover).

The wide-bit hold is handled by the innermost cascade counter
(`wide_bit_remaining`): the same bit emits for `BitWidth_REG + 1`
columns before the bit cursor advances. No cached `BitSlotState` — the
slot is recomputed from current state on every emission.

DRQ/FCP guards/tails are *not* in this path — the parallel `FlowControlPort`
emits them independently and is composed at the engine layer.

### Channel Grouping

Channel grouping creates a burst/space pattern:

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

## Key Design Patterns

### 1. Facade Pattern (DataPort)

`DataPort` presents a simple interface while internally delegating to specialized classes:

```python
class DataPort:
    def __init__(self, device, dp_index):
        self.config = DataPortConfig()       # Configuration
        self._state = DataPortState()        # Runtime state
        self._algorithm = DataPortAlgorithm(self)  # Logic

    def fetch_bit_slot(self) -> BitSlotState:
        return self._algorithm.fetch_bit_slot()  # Delegate
```

### 2. Property Caching with Invalidation

Expensive derived values are cached and invalidated on change:

```python
@property
def EnableCh_REG(self) -> int:
    return self._EnableCh_REG

@EnableCh_REG.setter
def EnableCh_REG(self, value: int) -> None:
    if self._EnableCh_REG != value:
        self._EnableCh_REG = value
        self._cached_enabled_channels = None  # Invalidate
        self._cached_num_channels = None
```

### 3. Enum-Based Type Safety

Enums replace string comparisons for type safety and performance:

```python
class SlotOccupancyType(Enum):
    WRITE = "write"
    READ = "read"
    GUARD = "guard"
    TAIL = "tail"
    HANDOVER = "handover"
    TXP_SOURCE = "txp_source"
    TXP_SINK = "txp_sink"
```

### 4. Separation of Concerns

- **Configuration** (`DataPortConfig`): What the hardware registers say
- **State** (`DataPortState`): Where we are in the render
- **Algorithm** (`DataPortAlgorithm`): How to compute the next slot
- **Visualization** (`DataPortVizConfig`): How to display it

---

## Performance Optimizations

### 1. Cached Enabled Channels

The `_channel()` method is called thousands of times per frame. Caching avoids reconstructing the enabled channels list:

```python
# Before: O(16) per call
def _channel(self, index):
    enabled = [i for i in range(16) if self.EnableCh_REG & (1 << i)]
    return enabled[index]

# After: O(1) lookup with cached tuple
@property
def _enabled_channels(self) -> tuple:
    if self._cached_enabled_channels is None:
        self._cached_enabled_channels = tuple(
            i for i in range(16) if self._EnableCh_REG & (1 << i)
        )
    return self._cached_enabled_channels

def _channel(self, index):
    return self._enabled_channels[index]
```

### 2. Efficient Bit Counting

Using Python 3.10+ `int.bit_count()` instead of string conversion:

```python
# Before
num_channels = bin(EnableCh_REG).count('1')

# After
num_channels = EnableCh_REG.bit_count()
```

### 3. Reusable Validator

The `DataPortValidator` is created once in `BusModelBuilder.__init__()` and reused:

```python
# Before: Created per data port
for dp in data_ports:
    validator = DataPortValidator(interface)  # Expensive
    validator.validate(dp)

# After: Created once, reused
self._dp_validator = DataPortValidator(interface)
for dp in data_ports:
    self._dp_validator.validate(dp)
```

### 4. Enum-Based Comparisons

`SlotOccupancyType` enum enables fast identity comparisons:

```python
# Before: String comparison
if slot_type == "write": ...

# After: Enum identity (faster)
if slot_type == SlotOccupancyType.WRITE: ...
```

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

---

*Last updated: April 2026*
