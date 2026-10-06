"""One-shot timers that belong to a widget.

`after(ms, owner, fn)` runs `fn` once, `ms` from now, unless `owner` has been destroyed by
then. A plain `QTimer.singleShot(ms, fn)` outlives its window: a pending one fired on a
deleted window and crashed whatever ran the event loop next. The context form
`singleShot(ms, owner, fn)` cancels with the owner; where a PySide build lacks that
overload, the fallback checks the owner is still alive before calling.
"""
from __future__ import annotations

from typing import Callable

from PySide6.QtCore import QObject, QTimer
from shiboken6 import isValid


def after(ms: int, owner: QObject, fn: Callable[[], object]) -> None:
    try:
        QTimer.singleShot(int(ms), owner, fn)
    except TypeError:                     # no (msec, context, functor) overload
        QTimer.singleShot(int(ms), lambda: fn() if isValid(owner) else None)
