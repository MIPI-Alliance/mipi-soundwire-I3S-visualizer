"""The SWI3S Links of one analysis: an ordered set of decoded Sessions, one of them active.

A Link (spec §4.7) is one Manager and its Peripherals on one bus. Links are independent, so
each is a complete `Session`, unchanged; this module only holds them, names them and says
which one the per-Link panels show. It is not Link-Control (the Cold/Warm Start bring-up in
`analysis.link_control`) — nothing here decodes.

A Link's IDENTITY is its position in the set. The name is display only, so renaming a Link
never has to touch anything that refers to it.

TIME. Each Link counts time in its own capture's samples. The global time the Links share is
int64 picoseconds, and `LinkEntry.to_ps` / `to_sample` are the ONLY conversion — nothing else
does the arithmetic:

    t_ps   = round(sample * 1e12 / rate_hz) + offset_ps
    sample = round((t_ps - offset_ps) * rate_hz / 1e12)

The arithmetic is exact (rationals, not floats), so a round trip returns the same sample for
any rate below 1 THz: the picosecond rounding moves the value by at most rate/2e12 of a
sample. The offset is signed, and changing it moves nothing but this mapping.

`LinkEntry.view` is an opaque slot for the UI's per-Link panel state; this module never
reads it, which keeps it free of Qt.
"""
from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from typing import Any, Iterator, List, Optional

_DEFAULT_PREFIX = "Link "
_PS_PER_S = 10 ** 12


@dataclass
class LinkEntry:
    session: Any                  # a Session (Any: tests stand in placeholder objects)
    name: str
    view: Any = None              # UI-owned per-Link state; opaque here
    offset_ps: int = 0            # where this Link's sample 0 sits in global time

    def _rate(self) -> Fraction:
        return Fraction(self.session.sample_rate_hz)     # exact, even from a float

    def to_ps(self, sample: int) -> int:
        """Global time (ps) of one of this Link's sample numbers."""
        return round(Fraction(int(sample)) * _PS_PER_S / self._rate()) + int(self.offset_ps)

    def to_sample(self, t_ps: int) -> int:
        """This Link's sample number nearest a global time (ps)."""
        return round(Fraction(int(t_ps) - int(self.offset_ps)) * self._rate() / _PS_PER_S)


def _default_number(name: str) -> Optional[int]:
    """n for a default name "Link n", else None (a user-chosen name)."""
    tail = name[len(_DEFAULT_PREFIX):]
    if name.startswith(_DEFAULT_PREFIX) and tail.isdigit():
        return int(tail)
    return None


class LinkSet:
    """Ordered Links with one active. Empty means nothing is loaded; `active` is then None."""

    def __init__(self) -> None:
        self._entries: List[LinkEntry] = []
        self._active = -1

    def __len__(self) -> int:
        return len(self._entries)

    def __iter__(self) -> Iterator[LinkEntry]:
        return iter(self._entries)

    def __getitem__(self, index: int) -> LinkEntry:
        return self._entries[index]

    @property
    def active(self) -> Optional[LinkEntry]:
        return self._entries[self._active] if self._entries else None

    @property
    def active_index(self) -> int:
        """Index of the active Link, or -1 when the set is empty."""
        return self._active

    def _next_default_name(self) -> str:
        """The smallest "Link n" not already taken, so a removed Link's default name is
        reused before a higher one is invented."""
        taken = {_default_number(e.name) for e in self._entries}
        n = 1
        while n in taken:
            n += 1
        return f"{_DEFAULT_PREFIX}{n}"

    def add(self, session: Any, name: Optional[str] = None, view: Any = None) -> LinkEntry:
        """Append a Link and return it. The first Link added becomes active; later ones do
        not, so adding a Link never changes what the panels show."""
        entry = LinkEntry(session, name if name is not None else self._next_default_name(),
                          view)
        self._entries.append(entry)
        if self._active < 0:
            self._active = 0
        return entry

    def replace(self, session: Any, view: Any = None) -> LinkEntry:
        """Make `session` the only Link. Opening a capture does this: it replaces the
        analysis rather than adding to it."""
        self.clear()
        return self.add(session, view=view)

    def remove(self, index: int) -> None:
        """Remove a Link. The active Link stays active if it survives; if it was the one
        removed, its successor (or the new last Link) becomes active."""
        del self._entries[index]
        if not self._entries:
            self._active = -1
        elif index < self._active or self._active >= len(self._entries):
            self._active -= 1

    def clear(self) -> None:
        self._entries.clear()
        self._active = -1

    def set_active(self, index: int) -> None:
        if not 0 <= index < len(self._entries):
            raise IndexError(f"no Link at index {index} (have {len(self._entries)})")
        self._active = index

    def rename(self, index: int, name: str) -> None:
        name = name.strip()
        if not name:
            raise ValueError("a Link name cannot be empty")
        self._entries[index].name = name

    def convert(self, sample: int, src: int, dst: int) -> int:
        """A sample number of Link `src` expressed in Link `dst`'s samples. Within one Link
        it is returned untouched, so nothing single-Link ever goes through the rounding."""
        if src == dst:
            return int(sample)
        return self._entries[dst].to_sample(self._entries[src].to_ps(sample))

    def index_of(self, session: Any) -> int:
        """Index of the Link holding `session` (by identity), or -1."""
        for i, e in enumerate(self._entries):
            if e.session is session:
                return i
        return -1
