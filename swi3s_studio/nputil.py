"""Small numpy helpers shared across ingest / analysis / session / UI (no deps, so it
can't create an import cycle)."""
from __future__ import annotations

import numpy as np


def searchsorted(arr: np.ndarray, value, side: str = "left"):
    """np.searchsorted WITHOUT numpy's dtype-promotion trap. Searching a uint64 array
    (the clock/data edge lists) with a Python int / int64 / float value promotes to
    float64 and casts the WHOLE array on every call. Casting the search value to the
    array's own dtype first keeps it O(log n).

    A fractional boundary becomes the integer that gives the same answer on an integer
    array: its ceiling for side="left" (elements below it are exactly those < ceil(v)),
    its floor for side="right". A key below the array dtype's range (or -inf) gives 0
    and a key above it (a Python int beyond it, +inf, or NaN, which sorts last as in
    numpy) gives arr.size, on both sides, whatever boundary values the array holds."""
    key, below, above = _in_dtype_of(arr, value, side)
    out = np.searchsorted(arr, key, side=side)
    return _clamp_result(out, below, above, arr.size)


def searchsorted_many(arr: np.ndarray, keys) -> np.ndarray:
    """searchsorted(arr, keys, side="left") for many keys at once, dtype-safe as above.

    Every key takes its next bisection step together, so the cache misses of one key's
    search overlap the others'."""
    keys, below, above = _in_dtype_of(arr, keys, "left")
    lo = np.zeros(keys.shape, dtype=np.intp)
    hi = np.full(keys.shape, arr.size, dtype=np.intp)
    if arr.size == 0:
        return lo
    last = arr.size - 1
    for _ in range(int(arr.size).bit_length()):    # halves every [lo, hi) to empty
        mid = (lo + hi) >> 1
        less = arr[np.minimum(mid, last)] < keys
        lo = np.where(less & (lo < hi), mid + 1, lo)
        hi = np.where(less, hi, mid)
    return _clamp_result(lo, below, above, arr.size)


def _clamp_result(out, below, above, size: int):
    """`out` with 0 where `below` and `size` where `above` (either mask may be None)."""
    if below is None or not (np.any(below) or np.any(above)):
        return out
    res = np.where(below, 0, np.where(above, size, out)).astype(np.intp, copy=False)
    return res[()] if res.ndim == 0 else res


def _in_dtype_of(arr: np.ndarray, value, side: str = "left"):
    """(`value` cast to `arr`'s dtype so a search compares in it, mask of keys below
    the dtype's range, mask of keys above it). The cast key of an out-of-range entry
    is clipped to the range's end and the masks say where the answer is 0 or arr.size
    instead. The masks are None for an array that is not integer."""
    v = np.asarray(value)
    if v.dtype == arr.dtype or not np.issubdtype(arr.dtype, np.integer):
        return v, None, None
    info = np.iinfo(arr.dtype)
    lo, hi = int(info.min), int(info.max)
    if v.dtype == object:                               # a Python int beyond uint64
        ints = [int(x) for x in v.ravel()]
        below = np.array([x < lo for x in ints], dtype=bool).reshape(v.shape)
        above = np.array([x > hi for x in ints], dtype=bool).reshape(v.shape)
        flat = [min(max(x, lo), hi) for x in ints]
        return np.array(flat, dtype=arr.dtype).reshape(v.shape), below, above
    if np.issubdtype(v.dtype, np.floating):
        v = np.ceil(v) if side == "left" else np.floor(v)
        above = ~(v < float(hi + 1))      # hi + 1 is a power of two, exact as a float; catches NaN
        below = v < float(lo)
        out = np.where(above | below, 0, v).astype(arr.dtype)
        key = np.where(above, hi, np.where(below, lo, out)).astype(arr.dtype)
        return key, below, above
    below = above = np.zeros(v.shape, dtype=bool)
    if np.issubdtype(v.dtype, np.integer):
        vinfo = np.iinfo(v.dtype)
        if int(vinfo.min) < lo:
            below = v < np.asarray(lo, dtype=v.dtype)
            v = np.maximum(v, np.asarray(lo, dtype=v.dtype))     # no unsigned wrap
        if int(vinfo.max) > hi:
            above = v > np.asarray(hi, dtype=v.dtype)
            v = np.minimum(v, np.asarray(hi, dtype=v.dtype))
    return v.astype(arr.dtype, copy=False), below, above
