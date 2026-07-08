"""BusModel JSON round-trip (swviz.io.json_handler).

Regression: non-payload slots (guard/tail/CDS/…) carry sample/bit = None, and the
save->load round trip must preserve that — coercing None to 0 corrupts the
scrambler/test-mode mismatch checks (0 is a valid payload sample).

Run: PYTHONPATH=. python3 tests/test_json_handler.py
"""
import os
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from swi3s_studio.swviz.io.json_handler import JSONHandler
from swi3s_studio.swviz.models.bus_model import BitInfo, BusModel
from swi3s_studio.swviz.models.enums import SlotType, DirectionType


def test_json_roundtrip_preserves_none_sample_bit():
    m = BusModel(num_rows=1, num_columns=4, row_rate=3072.0)
    # A payload DATA bit (sample/bit set) and a non-payload GUARD bit (sample/bit None).
    m.add_bit(BitInfo(bit_index=0, slot=SlotType.DATA, direction=DirectionType.SOURCE,
                      device=0, dp=0, channel=1, sample=0, bit=3))
    m.add_bit(BitInfo(bit_index=1, slot=SlotType.GUARD_0, direction=DirectionType.SOURCE,
                      device=0, dp=0))
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "model.json")
        JSONHandler.save_bus_model(p, m)
        m2 = JSONHandler.load_bus_model(p)
    by_idx = {b.bit_index: b for b in m2.bits}
    data, guard = by_idx[0], by_idx[1]
    assert (data.sample, data.bit) == (0, 3), (data.sample, data.bit)
    # The guard's sample/bit must come back None, not 0.
    assert guard.sample is None and guard.bit is None, (guard.sample, guard.bit)


if __name__ == "__main__":
    test_json_roundtrip_preserves_none_sample_bit()
    print("ok: JSON round-trip preserves None sample/bit for non-payload slots")
    print("ALL JSON-HANDLER TESTS PASSED")
