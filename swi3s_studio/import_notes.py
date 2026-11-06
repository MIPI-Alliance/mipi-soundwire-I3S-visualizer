"""Import warnings: what an importer skipped, clamped, masked, defaulted, truncated,
reinterpreted or ignored in a user file.

An importer calls `warn(source, message)` for each such case, `source` being the file's
base name (`source_name(path)`). The caller that owns the import opens a collector:

    with import_notes.collect() as notes:
        session = Session.from_vcd(path, ...)
    show(notes)                     # a list of ImportNote, in the order raised

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
from typing import Iterable, Iterator, List, Optional, Sequence

from .ingest.timebase import clipped


class ImportIssueWarning(UserWarning):
    """Issued by `warn` when no collector is open."""


@dataclass(frozen=True)
class ImportNote:
    """One import warning: `source` is the file's base name, `message` what happened."""
    source: str
    message: str

    def __str__(self) -> str:
        return f"{self.source}: {self.message}" if self.source else self.message


def _edit(name: str):
    """The list method `name`, marking the collector's dedup index stale first."""
    method = getattr(list, name)

    def edit(self, *args, **kwargs):
        self.stale = True
        return method(self, *args, **kwargs)
    edit.__name__ = name
    return edit


class _Collected(list):
    """The list a collector yields. An edit made to it from outside `warn` (an append, item
    assignment, removal, sort or any other change) marks the dedup index stale, so the next
    `warn` rebuilds it from what the list holds."""
    stale = False
    append = _edit("append")
    extend = _edit("extend")
    insert = _edit("insert")
    remove = _edit("remove")
    pop = _edit("pop")
    clear = _edit("clear")
    sort = _edit("sort")
    reverse = _edit("reverse")
    __setitem__ = _edit("__setitem__")
    __delitem__ = _edit("__delitem__")
    __iadd__ = _edit("__iadd__")
    __imul__ = _edit("__imul__")


_active: contextvars.ContextVar[Optional[_Collected]] = contextvars.ContextVar(
    "swi3s_import_notes", default=None)
# The active collector's index: the set of (source, message) pairs the list holds. It is
# rebuilt when the caller has edited the list.
_seen: contextvars.ContextVar[Optional[set]] = contextvars.ContextVar(
    "swi3s_import_notes_seen", default=None)


# Names a warning lists before it says "and N more".
NAMES_SHOWN = 3


def listed(items: Iterable[str], shown: int = NAMES_SHOWN) -> str:
    """The first `shown` of `items` comma-joined, then "and N more" for the rest."""
    items = list(items)
    text = ", ".join(items[:shown])
    return f"{text} and {len(items) - shown} more" if len(items) > shown else text


def names_listed(names: Iterable[object], shown: int = NAMES_SHOWN) -> str:
    """`listed` for names taken from a file: each one is cut as `ingest.timebase.clipped`
    cuts a quoted cell."""
    return listed((clipped(str(n)) for n in names), shown)


def source_name(path) -> str:
    """The base name `warn` expects for a file path (a path-like or a string)."""
    return os.path.basename(os.fspath(path)) if path else ""


@contextlib.contextmanager
def collect() -> Iterator[List[ImportNote]]:
    """Collect the warnings raised inside the block into the yielded list."""
    notes = _Collected()
    token = _active.set(notes)
    seen_token = _seen.set(set())
    try:
        yield notes
    finally:
        _seen.reset(seen_token)
        _active.reset(token)


def warn(source: str, message: str) -> None:
    """Record one import warning for `source` (a file's base name). An identical warning
    already in the active collector is not added twice."""
    note = ImportNote(str(source or ""), str(message))
    notes = _active.get()
    if notes is None:
        warnings.warn(str(note), ImportIssueWarning, stacklevel=2)
        return
    seen = _seen.get()
    if seen is None or notes.stale:
        seen = {(n.source, n.message) for n in notes}                  # edited behind it
        _seen.set(seen)
        notes.stale = False
    key = (note.source, note.message)
    if key not in seen:
        seen.add(key)
        list.append(notes, note)


def extend(notes: Sequence[ImportNote]) -> None:
    """Re-raise already collected warnings into the active collector (or `warnings`)."""
    for n in notes:
        warn(n.source, n.message)


def active() -> bool:
    """Whether a collector is open in this context."""
    return _active.get() is not None
