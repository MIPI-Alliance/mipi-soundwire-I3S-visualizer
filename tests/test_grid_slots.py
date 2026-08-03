"""Slot-vocabulary consistency — the one guardrail that keeps three independently
maintained slot representations from drifting apart:

  * C++ ``SwI3sSlot`` (native/swi3score/core/CDataPort.h), surfaced as
    ``swi3score.SLOT_NAMES``;
  * the shared ``GridSlot`` enum (model/grid_slots.py) the GridView renders from;
  * the Visualizer authoring engine's ``SlotType`` (swviz), mapped to GridSlot ints
    by ``viz_engine._SLOT_TO_GRID``.

If a C++ enum reorder, a GridSlot edit, or a SlotType change skews any of these, one
of these assertions fails instead of the grid silently mis-colouring cells.

Run: PYTHONPATH=. python3 -m pytest tests/test_grid_slots.py
"""
import swi3score

from swi3s_studio.model import viz_engine
from swi3s_studio.model.grid_slots import GridSlot
from swi3s_studio.swviz.models.enums import SlotType


def test_gridslot_matches_cpp_slot_names():
    """GridSlot 0–6 must equal the C++ SwI3sSlot ordinals (swi3score.SLOT_NAMES), so a
    decoded capture's GridCell.slot renders under the right GridSlot without translation."""
    names = list(swi3score.SLOT_NAMES)   # ("Empty","Data","TxPresent","Guard0","Guard1","Tail","Drq")
    expected = {
        GridSlot.EMPTY: "Empty", GridSlot.DATA: "Data", GridSlot.TX_PRESENT: "TxPresent",
        GridSlot.GUARD_0: "Guard0", GridSlot.GUARD_1: "Guard1", GridSlot.TAIL: "Tail",
        GridSlot.DRQ: "Drq",
    }
    for slot, name in expected.items():
        assert 0 <= int(slot) < len(names), (slot, names)
        assert names[int(slot)] == name, (slot, names[int(slot)], name)


def test_slot_to_grid_covers_placement_slot_types():
    """viz_engine._SLOT_TO_GRID must map every swviz SlotType that can appear as a placed
    DATA/system cell to a GridSlot int; the non-cell kinds (EMPTY/CLASH) and CDS (handled
    via is_cds) are deliberately absent."""
    m = viz_engine._SLOT_TO_GRID
    must_map = [SlotType.DATA, SlotType.GUARD_0, SlotType.GUARD_1, SlotType.TAIL,
                SlotType.HANDOVER, SlotType.S0, SlotType.S1, SlotType.TX_PRESENT, SlotType.DRQ]
    for st in must_map:
        assert st.value in m, st
        assert m[st.value] in {int(g) for g in GridSlot}, (st, m[st.value])
    # CDS routes through is_cds (viz_engine._CDS_SLOT_VALUE), not _SLOT_TO_GRID.
    assert SlotType.CDS.value == viz_engine._CDS_SLOT_VALUE
    assert SlotType.CDS.value not in m
    # The shared 1–6 kinds map identity (GridSlot ints coincide with the C++ enum).
    assert m[SlotType.DATA.value] == int(GridSlot.DATA)
    assert m[SlotType.DRQ.value] == int(GridSlot.DRQ)
