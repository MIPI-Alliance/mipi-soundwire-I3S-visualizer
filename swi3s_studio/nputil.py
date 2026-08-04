"""Small numpy helpers shared across ingest / analysis / session / UI (no deps, so it
can't create an import cycle)."""
from __future__ import annotations

import numpy as np


def searchsorted(arr: np.ndarray, value, side: str = "left"):
    """np.searchsorted WITHOUT numpy's dtype-promotion trap. Searching a uint64 array
    (the clock/data edge lists — hundreds of millions of entries) with a Python int /
    int64 / float value promotes to float64 and casts the WHOLE array on every call —
    ~0.5–1.3 s per lookup on a big capture, so cursor navigation crawls. Casting the
    search value to the array's own dtype first keeps it O(log n).

    Edge/sample values are non-negative and within the array's range, so casting a
    non-negative int to the (unsigned) array dtype is exact; a float boundary truncates
    toward zero, which is the correct bisect endpoint for a monotonic integer array."""
    v = np.asarray(value)
    if v.dtype != arr.dtype:
        if v.ndim == 0 and np.issubdtype(v.dtype, np.floating):
            v = np.asarray(np.floor(v))               # float boundary -> integer index
        if np.issubdtype(arr.dtype, np.unsignedinteger):
            v = np.maximum(v, 0)                       # negative -> 0 (avoid unsigned wrap)
        v = v.astype(arr.dtype, copy=False)
    return np.searchsorted(arr, v, side=side)
