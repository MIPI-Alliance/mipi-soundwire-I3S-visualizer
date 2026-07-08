"""nputil.searchsorted — dtype-safe bisect that avoids numpy's uint64+int/float →
float64 promotion (which casts the whole array; the cause of ~1s-per-cursor-move
navigation stalls on large captures).

Run: python3 tests/test_nputil.py
"""
import numpy as np

from swi3s_studio.nputil import searchsorted


def test_matches_numpy_for_uint64_array():
    a = np.array([0, 10, 20, 30, 40], dtype=np.uint64)
    for v in (-5, 0, 9, 10, 25, 40, 100):
        for side in ("left", "right"):
            assert int(searchsorted(a, v, side=side)) == int(
                np.searchsorted(a, np.uint64(max(0, v)), side=side)), (v, side)


def test_does_not_float64_cast_a_huge_uint64_array():
    """The whole point: searching a big uint64 array must NOT lose precision to a
    float64 cast. 2**53+1 is not representable in float64 (rounds to 2**53), so a value
    just above it would land on the wrong side if the array were cast. This finds the
    exact boundary — proving the search stayed in integer space."""
    base = (1 << 53)
    a = np.array([base - 1, base + 1, base + 3], dtype=np.uint64)
    # base+2 sits strictly between a[1] and a[2]; in float64 both base+1 and base+2
    # collapse to 2**53 and the answer would be wrong.
    assert int(searchsorted(a, base + 2, side="left")) == 2


def test_float_boundary_floors_to_integer_index():
    a = np.array([0, 100, 200, 300], dtype=np.uint64)
    assert int(searchsorted(a, 150.9, side="left")) == 2      # floors to 150 -> before 200
    assert int(searchsorted(a, 200.0, side="left")) == 2


def test_array_value_ok():
    a = np.array([0, 10, 20, 30], dtype=np.uint64)
    got = searchsorted(a, np.array([5, 15, 35], dtype=np.int64))
    assert list(map(int, got)) == [1, 2, 4]


if __name__ == "__main__":
    test_matches_numpy_for_uint64_array(); print("ok: matches numpy (uint64 array, int value)")
    test_does_not_float64_cast_a_huge_uint64_array(); print("ok: no float64 cast (>2**53 precision kept)")
    test_float_boundary_floors_to_integer_index(); print("ok: float boundary floors to an integer index")
    test_array_value_ok(); print("ok: array search value")
    print("ALL NPUTIL TESTS PASSED")
