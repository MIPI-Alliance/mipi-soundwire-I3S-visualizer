"""nputil.searchsorted — dtype-safe bisect that avoids numpy's uint64+int/float →
float64 promotion (which casts the whole array).

Run: python3 -m pytest tests/test_nputil.py
"""
import numpy as np

from swi3s_studio.nputil import searchsorted, searchsorted_many


def test_matches_numpy_for_uint64_array():
    a = np.array([0, 10, 20, 30, 40], dtype=np.uint64)
    for v in (-5, 0, 9, 10, 25, 40, 100):
        for side in ("left", "right"):
            assert int(searchsorted(a, v, side=side)) == int(
                np.searchsorted(a.astype(np.float64), float(v), side=side)), (v, side)


def test_does_not_float64_cast_a_huge_uint64_array():
    """The whole point: searching a big uint64 array must NOT lose precision to a
    float64 cast. Above 2**53 float64 holds only even integers, so a key that sits past
    the last element lands on the wrong side if the array is cast. This finds the exact
    boundary, proving the search stayed in integer space."""
    base = (1 << 53)
    a = np.array([base - 1, base + 1, base + 3], dtype=np.uint64)
    # base+4 is past a[2]; cast to float64 a[2] becomes base+4 and the answer would be 2.
    assert int(searchsorted(a, base + 4, side="left")) == 3
    assert int(searchsorted(a, base + 2, side="left")) == 2


def test_float_boundary_gives_the_integer_bisect_endpoint():
    a = np.array([0, 100, 200, 300], dtype=np.uint64)
    assert int(searchsorted(a, 150.9, side="left")) == 2      # ceils to 151 for side=left -> before 200
    assert int(searchsorted(a, 200.0, side="left")) == 2


def test_array_value_ok():
    a = np.array([0, 10, 20, 30], dtype=np.uint64)
    got = searchsorted(a, np.array([5, 15, 35], dtype=np.int64))
    assert list(map(int, got)) == [1, 2, 4]


def test_many_keys_match_numpy():
    rng = np.random.default_rng(7)
    for dtype in (np.uint64, np.int64):
        for n in (0, 1, 2, 3, 17, 1000):
            a = np.sort(rng.integers(0, 500, n)).astype(dtype)     # duplicates included
            keys = rng.integers(-20, 520, 300)
            want = np.searchsorted(a, np.maximum(keys, 0) if dtype == np.uint64 else keys)
            assert (searchsorted_many(a, keys) == want).all(), (dtype, n)


def test_many_keys_stay_exact_above_float64_precision():
    base = (1 << 53)
    a = np.array([base - 1, base + 1, base + 3], dtype=np.uint64)
    keys = np.array([base, base + 2, base + 4], dtype=np.uint64)
    assert list(map(int, searchsorted_many(a, keys))) == [1, 2, 3]


def test_a_fractional_key_gives_exact_count_on_both_sides():
    for dtype, vals in ((np.uint64, [1, 2, 3]), (np.int64, [-3, -2, 0, 4])):
        a = np.array(vals, dtype=dtype)
        keys = np.array([-2.5, -0.5, 0.5, 1.5, 2.5, 3.5, 9.5])
        for side in ("left", "right"):
            want = np.searchsorted(a.astype(float), keys, side=side)
            got = [int(searchsorted(a, float(k), side=side)) for k in keys]
            assert got == list(want), (dtype, side)
        assert list(searchsorted_many(a, keys)) == list(np.searchsorted(a.astype(float), keys))


def test_a_key_beyond_the_dtype_range_clips_instead_of_failing():
    a = np.array([1, 2, 3], dtype=np.uint64)
    assert int(searchsorted(a, 2 ** 64 + 7)) == 3          # a Python int above uint64
    assert int(searchsorted(a, 1e30)) == 3
    assert int(searchsorted(a, -1e30)) == 0


def _exact_count(vals, key, side):
    """The elements below (side="left") or at or below (side="right") `key`, counted
    by comparing as Python numbers; a NaN key counts every element."""
    k = float(key) if isinstance(key, float) else int(key)
    if isinstance(k, float) and k != k:
        return len(vals)
    return sum((v < k) if side == "left" else (v <= k) for v in vals)


def test_a_key_outside_the_range_gives_zero_or_size_when_the_array_holds_the_boundary():
    u_max = 2 ** 64 - 1
    cases = (
        (np.uint64, [0, 5, 10]),
        (np.uint64, [0, 5, u_max]),
        (np.uint64, [u_max, u_max]),
        (np.int64, [-2 ** 63, 0, 2 ** 63 - 1]),
    )
    keys = [-1, -0.5, -1e30, float("-inf"), 2 ** 70, 1e30, float("inf"), float("nan"),
            0, 0.5, 2 ** 64, 2 ** 63, -2 ** 63 - 1]
    for dtype, vals in cases:
        a = np.array(vals, dtype=dtype)
        for key in keys:
            for side in ("left", "right"):
                assert int(searchsorted(a, key, side=side)) == _exact_count(vals, key, side), (
                    dtype.__name__, vals, key, side)
        floats = [k for k in keys if isinstance(k, float)]
        want = [_exact_count(vals, k, "left") for k in floats]
        assert list(map(int, searchsorted_many(a, np.array(floats)))) == want, (dtype.__name__, vals)


def test_integer_array_keys_outside_the_range_give_zero_or_size():
    a = np.array([0, 5, 2 ** 64 - 1], dtype=np.uint64)
    neg = np.array([-1, -7, 0], dtype=np.int64)
    assert list(map(int, searchsorted_many(a, neg))) == [0, 0, 0]
    a64 = np.array([-2 ** 63, 3, 2 ** 63 - 1], dtype=np.int64)
    big = np.array([2 ** 63, 2 ** 64 - 1, 5], dtype=np.uint64)
    assert list(map(int, searchsorted_many(a64, big))) == [3, 3, 2]
    for side in ("left", "right"):
        assert int(searchsorted(a, np.int64(-1), side=side)) == 0
    assert int(searchsorted(a64, np.uint64(2 ** 64 - 1), side="right")) == 3


def test_a_python_int_key_beyond_uint64_gives_the_size_in_searchsorted_many():
    a = np.array([0, 5, 2 ** 64 - 1], dtype=np.uint64)
    keys = np.array([2 ** 64, 2 ** 70, -1, 5], dtype=object)
    assert list(map(int, searchsorted_many(a, keys))) == [3, 3, 0, 1]


def test_a_float_key_equal_to_the_dtype_maximum_finds_it_in_narrow_integer_arrays():
    for dtype in (np.uint8, np.uint16, np.uint32, np.int8, np.int16, np.int32):
        info = np.iinfo(dtype)
        vals = [int(info.min), 1, int(info.max)]
        a = np.array(vals, dtype=dtype)
        for key in (float(info.max), float(info.max) + 1.0, float(info.min), float(info.min) - 1.0):
            for side in ("left", "right"):
                assert int(searchsorted(a, key, side=side)) == _exact_count(vals, key, side), (
                    dtype.__name__, key, side)
            assert int(searchsorted_many(a, np.array([key]))[0]) == _exact_count(vals, key, "left"), (
                dtype.__name__, key)
