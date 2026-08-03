"""Bus-grid slot vocabulary — the single source of truth for the integer ``slot``
kind carried on every grid cell.

Three producers historically named these kinds independently; this enum unifies them:

- **C++ decode** (`native/swi3score/core/CDataPort.h` ``SwI3sSlot``): emits
  ``GridCell.slot`` for a decoded capture. Its ints for ``Empty..Drq`` (0..6) are
  IDENTICAL to ``GridSlot`` here, so ``swi3score.grid_from_*`` cells render directly.
- **GridView** (`ui/grid_view.py`): reads ``cell["slot"]`` to colour/label each cell.
- **Visualizer engine adapter** (`model/viz_engine.py`): maps the authoring engine's
  ``swviz.models.enums.SlotType`` onto these ints for the live authoring grid.

Ints 7–9 (S0/S1/Handover) are placement-only kinds the C++ decode never emits as
data cells but the Visualizer authoring engine does. ``tests/test_grid_slots.py``
pins 1–6 to ``swi3score.SLOT_NAMES`` so a C++ enum reorder is caught at test time
instead of silently mis-colouring the grid.
"""
from __future__ import annotations

from enum import IntEnum


class GridSlot(IntEnum):
    # 0–6: identical to the C++ SwI3sSlot enum (order pinned by test_grid_slots).
    EMPTY = 0        # no cell placed
    DATA = 1
    TX_PRESENT = 2
    GUARD_0 = 3
    GUARD_1 = 4
    TAIL = 5
    DRQ = 6
    # 7–9: Visualizer-engine-only (absent from the C++ decode slot vocabulary).
    S0 = 7
    S1 = 8
    HANDOVER = 9
