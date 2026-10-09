"""Per-Link panel state: what the main window keeps for ONE Link, derived from its Session.

The per-Link panels (Bus Grid, Capture, CDS, Audio, Samples, Timing, Registers, Statistics,
Commands) are one set of widgets bound to the active Link. What they need from that Link
beyond the Session lives here, one instance per Link, so a later Link switch re-binds the
panels instead of losing the state.

Only state DERIVED from a Link belongs here, plus the Link's stream colour overrides: those
are the user's, but a (device, data port) means something only within one Link's capture,
so they travel with it (and its workspace entry). Display preferences (TX map on/off,
persist, grid rows, the TX-map scroll row) are window-wide, because opening a new capture
keeps them today and one set of panels shows them.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, List, Optional, Tuple


@dataclass
class LinkPanelState:
    audio_store: Any = None                 # the Link's AudioStore; None before it loads
    cmd_model: Any = None                   # the Commands table's source model
    starts: List[int] = field(default_factory=list)   # command cursor samples (ascending)
    meas_dirty: bool = False                # Statistics needs a (re)build (lazy, on show)
    sample_range: Optional[Tuple[int, int]] = None    # Samples: loaded row window
    sample_total: int = 0                   # Samples: total filtered samples
    sample_filters: Any = None              # Samples: filters the window was built with
    symbol_range: Optional[Tuple[int, int]] = None    # CDS: loaded window's start samples
    kinds: List[str] = field(default_factory=list)    # command kinds, for the Filter menu
    sections: Any = None                    # Timing pane's per-section UI stats (cached)
    meas_rows: Any = None                   # Statistics rows once built (cached)
    filters: Any = None                     # Commands filter snapshot, kept while switched away
    col_widths: Any = None                  # Commands column widths, kept while switched away
    eye_cache: dict = field(default_factory=dict)     # Timing pane's measurement (cached)
    stream_colors: dict = field(default_factory=dict)  # (device, dp, theme) -> "#rrggbb"
    # Per-stream Filter & Gain (DC blocker, high-pass, gain), (device, dp) -> StreamProcessing. The Link's choice,
    # re-applied to each new audio store (a re-decode builds one) and saved per Link.
    stream_processing: dict = field(default_factory=dict)
    # The Audio and Capture panes' view of this Link (ui/view_state.py dicts): checked
    # channels, zoom, Vertical Zoom, decimation, toggles. Saved when a pane stops showing
    # the Link and re-applied when one shows it again, so a Link switch or a re-decode keeps
    # them, and saved in the workspace. None: not seen yet, so the panes' defaults.
    audio_view: Any = None
    capture_view: Any = None
    #                                                    (line_style.Overrides)
