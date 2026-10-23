"""Bit removal in the bus model is DEFERRED but EXACT.

`remove_bits_matching` updates the position bucket immediately and records the rest of the
removal for `compact()`. This file pins the contract:

  * the bucket (what the clash logic reads) is exact AT ONCE — a deferred removal never lets
    a suppressed guard take part in a later clash decision;
  * `bits` is exact after `compact()`, which `BusModelBuilder.build()` calls once placement
    finishes, before anything reads the list;
  * `get_bits_in_row` compacts for itself, so a direct BusModel user cannot see a stale row.

The vendored configs in test_visualizer_engine cover the values themselves: several exercise
guard/tail suppression, and their goldens compare bits exactly.

Run: PYTHONPATH=. python3 -m pytest tests/test_bus_model_compaction.py
"""
from swi3s_studio.swviz.models.bus_model import BitInfo, BusModel
from swi3s_studio.swviz.models.enums import DirectionType, SlotType


def _model() -> BusModel:
    """A 2x4 frame carrying a guard and a data bit from device 3 at index 5, plus a guard
    from device 7 at the same index (a different device must NOT be swept up)."""
    m = BusModel(num_rows=2, num_columns=4)
    for device, slot in ((3, SlotType.GUARD_0), (3, SlotType.DATA), (7, SlotType.GUARD_0)):
        m.add_bit(BitInfo(bit_index=5, dp=0, device=device, slot=slot,
                          direction=DirectionType.SOURCE))
    m.add_bit(BitInfo(bit_index=1, dp=0, device=3, slot=SlotType.GUARD_0,
                      direction=DirectionType.SOURCE))   # another index, must be untouched
    return m


def test_the_position_bucket_is_exact_immediately():
    """No deferral for the bucket: the clash logic reads it while placing, so a suppressed
    guard must be gone from it before the next bit at that position is considered."""
    m = _model()
    assert m.remove_bits_matching(5, 3, SlotType.GUARD_0) == 1
    at5 = m.get_bits_at(5)
    assert not [b for b in at5 if b.device == 3 and b.slot == SlotType.GUARD_0], \
        "the suppressed guard is still in the position bucket"
    assert [b for b in at5 if b.device == 7], "another device's guard was swept up"
    assert [b for b in at5 if b.slot == SlotType.DATA], "the data bit was removed"


def test_bits_is_exact_after_compact_and_compact_is_idempotent():
    m = _model()
    before = len(m.bits)
    m.remove_bits_matching(5, 3, SlotType.GUARD_0)
    m.compact()
    assert len(m.bits) == before - 1
    assert not [b for b in m.bits
                if b.bit_index == 5 and b.device == 3 and b.slot == SlotType.GUARD_0]
    again = list(m.bits)
    m.compact()
    m.compact()
    assert m.bits == again, "compact() must be idempotent"


def test_compact_is_free_when_nothing_was_removed():
    """The common path: a build with no suppressions must not pay a rebuild. Pinned by
    identity — the list object is not replaced when there is nothing pending."""
    m = _model()
    same = m.bits
    m.compact()
    assert m.bits is same, "compact() rebuilt the list with nothing to remove"


def test_a_removal_that_matches_nothing_records_nothing():
    m = _model()
    same = m.bits
    assert m.remove_bits_matching(5, 9, SlotType.GUARD_0) == 0      # no such device
    assert m.remove_bits_matching(5, 3, SlotType.TAIL) == 0         # no such slot
    assert m.remove_bits_matching(99, 3, SlotType.GUARD_0) == 0     # empty position
    m.compact()
    assert m.bits is same, "a no-op removal still triggered a rebuild"


def test_get_bits_in_row_cannot_return_a_removed_bit():
    """A direct BusModel user who removes and then reads a row must not see the removal
    pending — get_bits_in_row compacts for itself rather than trusting the caller."""
    m = _model()
    m.remove_bits_matching(5, 3, SlotType.GUARD_0)
    row1 = m.get_bits_in_row(1)          # index 5 is row 1 of a 4-column frame
    assert not [b for b in row1 if b.device == 3 and b.slot == SlotType.GUARD_0], \
        "a deferred removal leaked into a row read"
