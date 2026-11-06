"""A focus change never moves the shared cursor.

An item view that gains the keyboard focus with no current index makes Qt pick its first
index as current (``QAbstractItemView.focusInEvent``). That happens when a pane is shown,
floated or put back, and it is not a selection. A view that drives the shared cursor from
its current index mixes this in and ignores a current change while ``focus_changing`` is
True; a mouse press, a key press and an explicit select still move the cursor.
"""
from __future__ import annotations


class QuietFocusMixin:
    """Mixin for item views whose current index drives the shared cursor. Put it before
    the Qt view class in the bases."""

    focus_changing = False

    def focusInEvent(self, event) -> None:
        self.focus_changing = True
        try:
            super().focusInEvent(event)  # type: ignore[misc]
        finally:
            self.focus_changing = False
