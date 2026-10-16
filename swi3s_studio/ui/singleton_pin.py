"""Keep None, True and False alive on Python 3.11 under PySide's Linux builds.

On Linux, PySide (every release from 6.9 to 6.12 at least) releases references it does not
hold: one to None each time it hands Python an empty value (an item or index read for a role
it does not hold, an invalid index, an unset QAction.data()), and one to True each time a
Python-defined signal is emitted (emit() returns True without taking a reference). Views
that read roles on every rebuild and signals emitted on every cursor move run through those
counts, some tens of thousands, within one session, and when one reaches zero the
interpreter aborts ("deallocating None", "deallocating True or False"). Python 3.12 made
these objects immortal, so the same releases do nothing there.

pin_singletons() gives 3.11 the same protection: once, before any of that, it adds 2**40 to
the reference count of None, True and False, which no session can release. It writes
CPython's reference-count field, the first field of every object in a release build; a build
whose objects start with other fields (Py_TRACE_REFS, which adds sys.getobjects) is left
alone, and a write is undone if the count did not move as expected.
tests/test_singleton_pin.py checks it.
"""
from __future__ import annotations

import sys

_PIN = 1 << 40
_PINNED = (None, True, False)
_pinned = False


def pin_singletons() -> bool:
    """Pin None, True and False on CPython 3.11, once. Whether they are protected
    afterwards: always on 3.12 and later, where they are immortal; on 3.11, once pinned."""
    global _pinned
    if sys.version_info >= (3, 12) or _pinned:
        return True
    if sys.implementation.name != "cpython" or hasattr(sys, "getobjects"):
        return False
    import ctypes

    done = []
    for obj in _PINNED:
        refcnt = ctypes.c_ssize_t.from_address(id(obj))
        before = sys.getrefcount(obj)
        refcnt.value += _PIN
        if sys.getrefcount(obj) - before != _PIN:   # not the field it should be: undo all
            refcnt.value -= _PIN
            for prev in done:
                ctypes.c_ssize_t.from_address(id(prev)).value -= _PIN
            return False
        done.append(obj)
    _pinned = True
    return True
