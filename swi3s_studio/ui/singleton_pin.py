"""Keep None, True and False alive on Python 3.11 under PySide's Linux builds.

On Linux, PySide (6.9 to 6.12 at least) releases references it does not hold: one to None
each time it hands Python an empty value (an item or index read for a role it does not hold,
an invalid index, an unset QAction.data()), and one to True each time a Python-defined signal
is emitted (emit() returns True without taking a reference). When a count reaches zero the
interpreter aborts ("deallocating None", "deallocating True or False"). On Python 3.12 and
later these objects are immortal and the releases do nothing.

pin_singletons() adds 2**40 to the reference count of None, True and False on 3.11. It decides
by the counts: an object whose count is already far above any real one is left alone, so calling
it again (app.main does, once the application exists) pins whatever is not pinned and changes
nothing else. It writes CPython's reference-count field, the first field of every object in
a release build; a build whose objects start with other fields (Py_TRACE_REFS, which adds
sys.getobjects) is left alone, and a write the count does not show is undone. The collector
is off while it runs, and the read-back check allows the count to move by up to _SLACK from
other references. On 3.11 a pin it cannot make warns. tests/test_singleton_pin.py checks it.
"""
from __future__ import annotations

import sys

_PIN = 1 << 40
_PINNED = (None, True, False)
# How far a count may move by other references while a pin is written and read back; a write
# to the wrong field moves it by about nothing, a pin by _PIN.
_SLACK = 1 << 20


def _landed(moved: int) -> bool:
    """Whether a count that moved by `moved` across the write took the pin."""
    return abs(moved - _PIN) <= _SLACK


def pin_singletons() -> bool:
    """Pin None, True and False on CPython 3.11, those not pinned already. Whether all three
    are protected afterwards: always on 3.12 and later, where they are immortal."""
    if sys.version_info >= (3, 12):
        return True
    if sys.implementation.name != "cpython" or hasattr(sys, "getobjects"):
        return _unprotected("this interpreter's objects do not start with their count")
    import ctypes
    import gc

    collecting = gc.isenabled()
    gc.disable()
    try:
        for obj in _PINNED:
            if sys.getrefcount(obj) > _PIN // 2:
                continue                                # pinned already
            refcnt = ctypes.c_ssize_t.from_address(id(obj))
            before = sys.getrefcount(obj)
            refcnt.value += _PIN
            if not _landed(sys.getrefcount(obj) - before):
                refcnt.value -= _PIN                    # not the count: put it back
                return _unprotected("the reference count did not take the pin")
        return True
    finally:
        if collecting:
            gc.enable()


def _unprotected(why: str) -> bool:
    import warnings

    warnings.warn(f"None, True and False are not pinned ({why}): PySide's over-released "
                  "references can abort Python 3.11", RuntimeWarning, stacklevel=3)
    return False
