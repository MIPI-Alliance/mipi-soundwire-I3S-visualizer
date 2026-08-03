"""Bookmarks: user-placed markers on the capture timeline, created in PAIRS so a
region can be measured (Δtime / Δrows / ΔUI between the two members of a pair).

Each bookmark carries a stable `group` (A, B, C, …) and `index` (1 or 2) assigned at
creation, NOT derived from sample order — so dragging one member past another does not
re-pair them. `add()` fills the earliest incomplete group before opening a new one, so
the natural sequence is A1, A2, B1, B2, … and deleting a member reopens its group for
the next add. Labels default to f"{group}{index}" (e.g. "A1") but can be overridden.

Persisted in the workspace; the legacy `[sample, ...]` format loads as ungrouped
singles paired up in order (see `BookmarkSet.from_json`).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple


def _group_name(n: int) -> str:
    """0->A, 1->B, … 25->Z, 26->AA, 27->AB, … (spreadsheet-style, unbounded)."""
    name = ""
    n += 1
    while n > 0:
        n, rem = divmod(n - 1, 26)
        name = chr(ord("A") + rem) + name
    return name


@dataclass
class Bookmark:
    sample: int
    group: str            # pair identity: 'A', 'B', ...
    index: int            # 1 or 2 within the group
    label: str = ""       # display override; empty => f"{group}{index}"

    def display(self) -> str:
        return self.label or f"{self.group}{self.index}"

    def to_dict(self) -> dict:
        return {"sample": int(self.sample), "group": self.group,
                "index": int(self.index), "label": self.label}


class BookmarkSet:
    """Ordered collection of paired bookmarks with stable group identity."""

    def __init__(self, bookmarks: Optional[List[Bookmark]] = None) -> None:
        self._items: List[Bookmark] = list(bookmarks or [])

    # ---- queries ----
    def __len__(self) -> int:
        return len(self._items)

    def __iter__(self):
        return iter(self._items)

    @property
    def items(self) -> List[Bookmark]:
        return list(self._items)

    def samples(self) -> List[int]:
        return [b.sample for b in self._items]

    def _members(self, group: str) -> List[Bookmark]:
        return [b for b in self._items if b.group == group]

    def _incomplete_group(self) -> Optional[str]:
        """The earliest group (by creation letter order) with only one member, so the
        next add completes a pair before starting a new one."""
        seen: Dict[str, int] = {}
        for b in self._items:
            seen[b.group] = seen.get(b.group, 0) + 1
        singles = [g for g, c in seen.items() if c == 1]
        if not singles:
            return None
        return min(singles, key=_group_index)

    def _next_new_group(self) -> str:
        used = {b.group for b in self._items}
        n = 0
        while _group_name(n) in used:
            n += 1
        return _group_name(n)

    # ---- mutations ----
    def add(self, sample: int) -> Bookmark:
        """Add a bookmark at `sample`, filling the earliest incomplete pair (index 2)
        or opening a new group (index 1). Returns the new Bookmark."""
        sample = int(sample)
        g = self._incomplete_group()
        if g is not None:
            bm = Bookmark(sample=sample, group=g, index=2)
        else:
            bm = Bookmark(sample=sample, group=self._next_new_group(), index=1)
        self._items.append(bm)
        return bm

    def remove_at(self, sample: int, tol: int = 0) -> bool:
        """Remove the bookmark nearest `sample` within `tol` samples. True if removed."""
        if not self._items:
            return False
        nearest = min(self._items, key=lambda b: abs(b.sample - sample))
        if tol and abs(nearest.sample - sample) > tol:
            return False
        self._items.remove(nearest)
        return True

    def remove(self, bm: Bookmark) -> None:
        if bm in self._items:
            self._items.remove(bm)

    def move(self, bm: Bookmark, sample: int) -> None:
        bm.sample = int(sample)

    def clear(self) -> None:
        self._items.clear()

    def copy(self) -> "BookmarkSet":
        """Deep-ish copy (new Bookmark objects) so a snapshot survives re-decodes and
        edits to the live set don't leak into it."""
        return BookmarkSet([Bookmark(b.sample, b.group, b.index, b.label) for b in self._items])

    # ---- pairs / measurement ----
    def pairs(self) -> List[Tuple[str, Bookmark, Bookmark]]:
        """(group, first, second) for every COMPLETE pair, ordered by group letter.
        Members are returned in sample order (left, right) for a positive delta."""
        by_group: Dict[str, List[Bookmark]] = {}
        for b in self._items:
            by_group.setdefault(b.group, []).append(b)
        out: List[Tuple[str, Bookmark, Bookmark]] = []
        for g in sorted(by_group, key=_group_index):
            members = by_group[g]
            if len(members) == 2:
                lo, hi = sorted(members, key=lambda b: b.sample)
                out.append((g, lo, hi))
        return out

    # ---- serialization ----
    def to_json(self) -> List[dict]:
        return [b.to_dict() for b in self._items]

    @classmethod
    def from_json(cls, data) -> "BookmarkSet":
        """Load from the workspace. Accepts the current list-of-dicts form OR the legacy
        list-of-ints (bare samples), which is paired up in order (A1, A2, B1, …)."""
        s = cls()
        if not data:
            return s
        if all(isinstance(x, (int, float)) for x in data):
            for x in data:                       # legacy: bare samples, pair in order
                s.add(int(x))
            return s
        for d in data:
            s._items.append(Bookmark(sample=int(d["sample"]), group=str(d.get("group", "A")),
                                     index=int(d.get("index", 1)), label=str(d.get("label", ""))))
        return s


def _group_index(name: str) -> int:
    """Inverse of _group_name: 'A'->0, 'Z'->25, 'AA'->26 — for ordering groups."""
    n = 0
    for ch in name:
        n = n * 26 + (ord(ch) - ord("A") + 1)
    return n - 1
