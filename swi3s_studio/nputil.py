"""Small numpy helpers shared across ingest / analysis / session / UI (no deps, so it
can't create an import cycle)."""
from __future__ import annotations

import numpy as np


def searchsorted(arr: np.ndarray, value, side: str = "left"):
    """np.searchsorted WITHOUT numpy's dtype-promotion trap. Searching a uint64 array
    (the clock/data edge lists) with a Python int / int64 / float value promotes to
    float64 and casts the WHOLE array on every call. Casting the search value to the
    array's own dtype first keeps it O(log n).

    Edge/sample values are non-negative and within the array's range, so casting a
    non-negative int to the (unsigned) array dtype is exact; a float boundary truncates
    toward zero, which is the correct bisect endpoint for a monotonic integer array."""
    return np.searchsorted(arr, _in_dtype_of(arr, value), side=side)


def searchsorted_many(arr: np.ndarray, keys) -> np.ndarray:
    """searchsorted(arr, keys, side="left") for many keys at once, dtype-safe as above.

    Every key takes its next bisection step together, so the cache misses of one key's
    search overlap the others'."""
    keys = _in_dtype_of(arr, keys)
    lo = np.zeros(keys.shape, dtype=np.intp)
    hi = np.full(keys.shape, arr.size, dtype=np.intp)
    if arr.size == 0:
        return lo
    last = arr.size - 1
    for _ in range(int(arr.size).bit_length()):    # halves every [lo, hi) to empty
        mid = (lo + hi) >> 1
        below = arr[np.minimum(mid, last)] < keys
        lo = np.where(below & (lo < hi), mid + 1, lo)
        hi = np.where(below, hi, mid)
    return lo


def _in_dtype_of(arr: np.ndarray, value) -> np.ndarray:
    """`value` cast to `arr`'s dtype, so a search compares in it."""
    v = np.asarray(value)
    if v.dtype != arr.dtype:
        if v.ndim == 0 and np.issubdtype(v.dtype, np.floating):
            v = np.asarray(np.floor(v))               # float boundary -> integer index
        if np.issubdtype(arr.dtype, np.unsignedinteger):
            v = np.maximum(v, 0)                       # negative -> 0 (avoid unsigned wrap)
        v = v.astype(arr.dtype, copy=False)
    return v
