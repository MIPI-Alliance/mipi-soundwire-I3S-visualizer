"""Shared 0-based row-number display for the analysis views.

A mid-row-start capture anchors its first complete bus row at an internal
``row_base`` > 0 (the leading partial row bumps the decoder's counter). Every
row shown to the user is 0-based: the first row reads as 0. This mixin holds the
per-view offset (``Session.row_origin``) and formats a display row through the
single source of truth, :func:`swi3s_studio.session.zero_based_row`, so a new
row-showing view can't silently drift back to internal rows.
"""
from __future__ import annotations

from ..session import zero_based_row


class RowOriginMixin:
    """Mixin for views that display bus rows 0-based. `set_row_origin` is called
    once per capture (from MainWindow.load_session); `display_row` formats an
    internal bus row for display."""

    _row_origin = 0

    def set_row_origin(self, origin: int) -> None:
        """Set the 0-based row display offset (Session.row_origin)."""
        self._row_origin = int(origin)

    def display_row(self, bus_row) -> int:
        """0-based display row for an internal bus row."""
        return zero_based_row(bus_row, self._row_origin)
