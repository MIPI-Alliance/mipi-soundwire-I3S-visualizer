"""Import warnings: what an importer skipped, clamped, masked, defaulted, truncated,
reinterpreted or ignored in a user file.

An importer calls `warn(source, message)` for each such case, `source` being the file's
base name (`source_name(path)`). The caller that owns the import opens a collector:

    with import_notes.collect() as notes:
        session = Session.from_vcd(path, ...)
    show(notes)                     # a list of ImportWarning, in the order raised

The active collector is a `contextvars.ContextVar`, so it belongs to the thread (or
context) that opened it: a worker thread opens its own around the work it runs and hands
the list back with its result. `collect()` blocks nest; an inner one takes the warnings
raised inside it and the outer one does not see them. With no collector open, `warn`
issues an `ImportIssueWarning` through the `warnings` module instead.

A hard failure is still an exception; a warning is for an import that went ahead.
"""
from __future__ import annotations

import contextlib
import contextvars
import os
import warnings
from dataclasses import dataclass
from typing import Iterator, List, Optional, Sequence


class ImportIssueWarning(UserWarning):
    """Issued by `warn` when no collector is open."""


@dataclass(frozen=True)
class ImportWarning:
    """One import warning: `source` is the file's base name, `message` what happened."""
    source: str
    message: str

    def __str__(self) -> str:
        return f"{self.source}: {self.message}" if self.source else self.message


_active: contextvars.ContextVar[Optional[List[ImportWarning]]] = contextvars.ContextVar(
    "swi3s_import_notes", default=None)


def source_name(path) -> str:
    """The base name `warn` expects for a file path (a path-like or a string)."""
    return os.path.basename(os.fspath(path)) if path else ""


@contextlib.contextmanager
def collect() -> Iterator[List[ImportWarning]]:
    """Collect the warnings raised inside the block into the yielded list."""
    notes: List[ImportWarning] = []
    token = _active.set(notes)
    try:
        yield notes
    finally:
        _active.reset(token)


def warn(source: str, message: str) -> None:
    """Record one import warning for `source` (a file's base name). An identical warning
    already in the active collector is not added twice."""
    note = ImportWarning(str(source or ""), str(message))
    notes = _active.get()
    if notes is None:
        warnings.warn(str(note), ImportIssueWarning, stacklevel=2)
        return
    if note not in notes:
        notes.append(note)


def extend(notes: Sequence[ImportWarning]) -> None:
    """Re-raise already collected warnings into the active collector (or `warnings`)."""
    for n in notes:
        warn(n.source, n.message)


def active() -> bool:
    """Whether a collector is open in this context."""
    return _active.get() is not None
