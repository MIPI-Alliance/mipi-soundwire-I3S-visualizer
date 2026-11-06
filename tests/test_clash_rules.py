"""The Visualizer's clash rules (swviz.drawing.ClashDetector), slot by slot.

A bit slot holds occupants — a data write or read, a guard, a tail, a handover — and each
new guard, tail, read or handover is judged against the occupants already there:

* a different device's write, handover, guard or tail is a bus clash;
* the same device's data write outranks its own guard or tail (no clash, not drawn), its
  guard outranks its own tail, and its handover gives way to its guard or tail;
* outside the CDS two guards, or two tails, in one slot clash even for one device; inside
  the CDS they are time-multiplexed per source and never clash;
* two reads of one slot are an overlap (informational, not a bus clash), whoever reads;
  a handover is not a reader, so a read over any device's handover is no overlap.

Each case checks the verdict, whether the slot is a write clash, and the same-device /
different-device record.
"""
import pytest

from swi3s_studio.swviz.drawing.clash_detector import (
    ClashDetector,
    SlotClashCategory,
    SlotOccupancyType,
)

ROW, COL, D, OTHER = 3, 5, 1, 2
SLOT = ROW * 16 + COL


def _with(prior, device):
    """A 16-column detector whose slot (ROW, COL) holds `prior` by `device`."""
    c = ClashDetector(16)
    if prior == "write":
        c.add_write(ROW, COL, device)
    elif prior == "guard":
        c.check_guard_clash(ROW, COL, device)
    elif prior == "tail":
        c.check_tail_clash(ROW, COL, device)
    elif prior == "handover":
        c.add_handover(ROW, COL, device)
    elif prior == "read":
        c.check_read_clash(ROW, COL, device)
    return c


def _occupants(c):
    return sorted((o.slot_type.value, o.device) for o in c.occupancies.get(SLOT, []))


def _record(c):
    """(same-device, different-device) write clash at the slot."""
    return SLOT in c.same_device_clashes, SLOT in c.different_device_clashes


# (prior, prior's device, is_cds) -> (has_clash, draw, suppressed same-device tail)
GUARD_RULES = [
    (None, D, False, (False, 1, False)),
    ("handover", D, False, (False, 1, False)),
    ("handover", OTHER, False, (True, 0, False)),
    ("handover", OTHER, True, (True, 0, False)),
    ("write", D, False, (False, 0, False)),
    ("write", OTHER, False, (True, 0, False)),
    ("write", OTHER, True, (True, 0, False)),
    ("guard", D, False, (True, 0, False)),
    ("guard", OTHER, False, (True, 0, False)),
    ("guard", D, True, (False, 1, False)),
    ("guard", OTHER, True, (False, 1, False)),
    ("tail", D, False, (False, 1, True)),
    ("tail", OTHER, False, (True, 0, False)),
    ("tail", OTHER, True, (True, 0, False)),
]


@pytest.mark.parametrize("prior,owner,cds,verdict", GUARD_RULES)
def test_a_guard_against_what_the_slot_already_holds(prior, owner, cds, verdict):
    c = _with(prior, owner)
    assert c.check_guard_clash(ROW, COL, D, is_cds=cds) == verdict
    clash = verdict[0]
    assert (SLOT in c.write_clashes) == clash
    assert _record(c) == ((clash and owner == D), (clash and owner != D))
    before = 1 if (prior == "guard" and owner == D) else 0
    assert _occupants(c).count(("guard", D)) == before + verdict[1]
    if verdict[2]:
        assert ("tail", D) not in _occupants(c)          # the guard replaced it


TAIL_RULES = [
    (None, D, False, (False, 1)),
    ("handover", D, False, (False, 1)),
    ("handover", OTHER, False, (True, 0)),
    ("handover", OTHER, True, (True, 0)),
    ("write", D, False, (False, 0)),
    ("write", OTHER, False, (True, 0)),
    ("write", OTHER, True, (True, 0)),
    ("tail", D, False, (True, 0)),
    ("tail", OTHER, False, (True, 0)),
    ("tail", D, True, (False, 1)),
    ("tail", OTHER, True, (False, 1)),
    ("guard", D, False, (False, 0)),
    ("guard", OTHER, False, (True, 0)),
    ("guard", OTHER, True, (True, 0)),
]


@pytest.mark.parametrize("prior,owner,cds,verdict", TAIL_RULES)
def test_a_tail_against_what_the_slot_already_holds(prior, owner, cds, verdict):
    c = _with(prior, owner)
    assert c.check_tail_clash(ROW, COL, D, is_cds=cds) == verdict
    clash = verdict[0]
    assert (SLOT in c.write_clashes) == clash
    assert _record(c) == ((clash and owner == D), (clash and owner != D))
    before = 1 if (prior == "tail" and owner == D) else 0
    assert _occupants(c).count(("tail", D)) == before + verdict[1]


@pytest.mark.parametrize("prior,owner,overlap", [
    (None, D, False),
    ("read", D, True),
    ("read", OTHER, True),
    ("handover", D, False),
    ("handover", OTHER, False),
])
def test_a_read_against_what_the_slot_already_holds(prior, owner, overlap):
    c = _with(prior, owner)
    assert c.check_read_clash(ROW, COL, D) is overlap
    assert (SLOT in c.read_clashes) is overlap
    assert _record(c) == (False, False)                   # an overlap is not a bus clash
    assert ("read", D) in _occupants(c)
    details = [i for i in c.get_clash_details() if i.bit_index == SLOT]
    assert [i.category for i in details] == ([SlotClashCategory.READ_CLASH] if overlap else [])


# (prior, prior's device, is_cds) -> (write clash, suppressed)
HANDOVER_RULES = [
    (None, D, False, (False, False)),
    ("read", D, False, (False, False)),
    ("read", OTHER, False, (False, False)),
    ("write", D, False, (False, True)),
    ("write", OTHER, False, (True, False)),
    ("tail", D, False, (False, True)),
    ("tail", OTHER, False, (True, True)),
    ("tail", OTHER, True, (False, False)),
    ("guard", D, True, (False, True)),
    ("guard", OTHER, False, (True, True)),
    ("guard", OTHER, True, (False, False)),
]


@pytest.mark.parametrize("prior,owner,cds,verdict", HANDOVER_RULES)
def test_a_handover_against_what_the_slot_already_holds(prior, owner, cds, verdict):
    c = _with(prior, owner)
    assert c.add_handover(ROW, COL, D, is_cds=cds) == verdict
    assert (SLOT in c.write_clashes) == verdict[0]
    assert SLOT not in c.read_clashes                      # a handover is never a read overlap
    assert ("handover", D) in _occupants(c)                # tracked whatever the verdict


@pytest.mark.parametrize("reader", [D, OTHER])
def test_a_handover_and_a_read_agree_in_either_order(reader):
    """The pair gives one verdict, whichever is recorded first: no overlap, both kept."""
    read_first = _with("read", reader)
    read_first.add_handover(ROW, COL, D)
    handover_first = _with("handover", D)
    assert handover_first.check_read_clash(ROW, COL, reader) is False
    for c in (read_first, handover_first):
        assert SLOT not in c.read_clashes and SLOT not in c.write_clashes
        assert _occupants(c) == sorted([("handover", D), ("read", reader)])


@pytest.mark.parametrize("prior", ["guard", "tail"])
def test_a_write_by_the_same_device_removes_its_guard_or_tail_from_the_slot(prior):
    c = _with(prior, D)
    assert c.check_write_clash(ROW, COL, D) == (False, {prior})
    c.add_write(ROW, COL, D)
    assert _occupants(c) == [("write", D)]


def test_a_clash_records_where_it_is_and_between_whom():
    c = _with("write", OTHER)
    c.check_guard_clash(ROW, COL, D)
    [info] = c.get_clash_details()
    assert (info.row, info.column) == (ROW, COL)
    assert (info.device_a, info.device_b) == (OTHER, D)
    assert (info.slot_type_a, info.slot_type_b) == ("write", "guard")
    assert c.get_write_clashes() == [SLOT]
    assert c.get_different_device_clashes() == [SLOT] and c.get_same_device_clashes() == []


def test_the_summary_counts_and_clear_empties_it():
    c = _with("write", OTHER)
    c.check_guard_clash(ROW, COL, D)
    c.check_read_clash(ROW + 1, COL, D)
    c.check_read_clash(ROW + 1, COL, OTHER)
    s = c.get_summary()
    assert (s["write_clashes"], s["read_clashes"], s["different_device_clashes"],
            s["same_device_clashes"], s["total_clash_events"]) == (1, 1, 1, 0, 2)
    assert s["total_occupancies"] == 3
    c.clear()
    assert set(c.get_summary().values()) == {0}
    assert c.occupancies == {} and c.get_clash_details() == []


def test_occupancy_types_name_what_the_engine_places():
    assert {t.value for t in SlotOccupancyType} >= {"write", "read", "guard", "tail", "handover"}
