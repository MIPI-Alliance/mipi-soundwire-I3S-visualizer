"""Delete the Qt objects Python still owns before the interpreter exits.

At interpreter exit PySide destroys every QObject still owned by Python, in the order of
its wrapper map, which is unrelated to the parent tree. A widget built without a parent
and only later reparented through a layout keeps its Python ownership, so the sweep can
destroy a parent and then reach a child the parent has already destroyed. PySide 6.12
crashes on that (SIGSEGV, SIGBUS or "pure virtual method called"). Deleting each tree
from its root first, while Qt can still order the destructors, leaves the sweep nothing
to visit.

Join the worker threads before calling this: a QThread destroyed while running aborts the
process, and a running one is left alone here rather than destroyed.
"""
from __future__ import annotations

from PySide6.QtCore import QCoreApplication, QEvent, QObject, QThread
from shiboken6 import Shiboken


def python_owned_qobjects() -> list:
    """Every live QObject that Python still owns, the application excepted."""
    app = QCoreApplication.instance()
    return [o for o in Shiboken.getAllValidWrappers()
            if isinstance(o, QObject) and o is not app
            and Shiboken.isValid(o) and Shiboken.ownedByPython(o)]


def release_python_owned_qobjects() -> int:
    """Delete the root of every object tree holding a Python-owned QObject, the application
    excepted, and return how many Python-owned objects are left (0 unless a thread was
    still running)."""
    app = QCoreApplication.instance()
    roots = {}
    for o in python_owned_qobjects():
        top = o
        while (up := top.parent()) is not None and up is not app:
            top = up
        roots[Shiboken.getCppPointer(top)[0]] = top
    for top in roots.values():
        if isinstance(top, QThread) and top.isRunning():
            continue
        top.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    return len(python_owned_qobjects())
