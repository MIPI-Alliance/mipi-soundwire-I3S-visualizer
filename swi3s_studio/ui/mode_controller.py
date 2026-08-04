"""Three-mode shell for SWI3S Studio.

Studio is one tool with three macro-modes covering the SWI3S deployment workflow:

- **Visualization** — plan a bus setup by authoring a config from scratch.
- **Timing** — dial in PHY timing settings (setup/hold/Z-handover margins, F_max).
- **Analysis** — the protocol analyzer: confirm a real capture behaves as planned.

`ModeManager` owns a "Mode" menu (mutually-exclusive, checkable actions) and,
for each mode, (1) which central page is shown and (2) which docks are visible and
how they're arranged. It snapshots each mode's dock arrangement with
`QMainWindow.saveState()` on the way out and restores it on the way back, so each
mode keeps its own layout. The host window builds the docks and central pages; this
class only sequences visibility/arrangement and emits the active mode.
"""
from __future__ import annotations

from typing import Callable, Dict, List, Optional

from PySide6.QtGui import QAction, QActionGroup
from PySide6.QtWidgets import QDockWidget, QMainWindow, QMenu

VISUALIZATION = "Visualization"
TIMING = "Timing"
ANALYSIS = "Analysis"
MODES = (VISUALIZATION, TIMING, ANALYSIS)

# Display labels for the mode switcher (the internal mode keys above stay stable
# so workspace files and dock maps keyed by mode don't churn).
MODE_LABELS = {
    VISUALIZATION: "Bus Visualizer",
    TIMING: "Timing Calculator",
    ANALYSIS: "Bus Analyzer",
}


class ModeManager:
    """Drives mode switching for a `QMainWindow` with a stacked central widget.

    Args:
        window: the host `QMainWindow`.
        set_central_index: callback that shows the central page for a mode index
            (0=Visualization, 1=Timing, 2=Analysis).
        mode_docks: mode name → the docks that belong to that mode.
        on_change: optional callback invoked with the new mode name after a switch.
    """

    def __init__(self, window: QMainWindow,
                 set_central_index: Callable[[int], None],
                 mode_docks: Dict[str, List[QDockWidget]],
                 on_change: Optional[Callable[[str], None]] = None) -> None:
        self._window = window
        self._set_central = set_central_index
        self._mode_docks = mode_docks
        self._on_change = on_change
        self._states: Dict[str, bytes] = {}     # mode → saved QMainWindow state
        self._current: Optional[str] = None
        self._actions: Dict[str, QAction] = {}
        self._all_docks = {d for docks in mode_docks.values() for d in docks}
        self._build_mode_menu()

    def _build_mode_menu(self) -> None:
        """Mode switching lives in a 'Mode' menu rather than a toolbar. On macOS the
        menu bar is the system bar (zero in-window height), so this reclaims the
        vertical space the toolbar used. The current mode is checkmarked; Ctrl+1/2/3
        switch directly."""
        mb = self._window.menuBar()
        menu = QMenu("&Mode", self._window)
        group = QActionGroup(self._window)
        group.setExclusive(True)
        for i, m in enumerate(MODES, start=1):
            act = QAction(MODE_LABELS.get(m, m), self._window)
            act.setCheckable(True)
            act.setShortcut(f"Ctrl+{i}")
            group.addAction(act)
            menu.addAction(act)
            act.triggered.connect(lambda _checked=False, mm=m: self.switch_to(mm))
            self._actions[m] = act
        # Insert just after the File menu so it's prominent without displacing File.
        actions = mb.actions()
        if len(actions) > 1:
            mb.insertMenu(actions[1], menu)
        else:
            mb.addMenu(menu)
        self._mode_menu = menu

    def current(self) -> Optional[str]:
        return self._current

    def switch_to(self, mode: str) -> None:
        if mode not in self._mode_docks:
            return
        if mode == self._current:
            self._actions[mode].setChecked(True)
            return
        # Snapshot the arrangement of the mode we're leaving so we can restore it.
        if self._current is not None:
            self._states[self._current] = bytes(self._window.saveState())

        self._current = mode
        self._actions[mode].setChecked(True)
        self._set_central(MODES.index(mode))

        # Show this mode's docks, hide the rest.
        wanted = set(self._mode_docks.get(mode, []))
        for dock in self._all_docks:
            dock.setVisible(dock in wanted)

        # If we've been here before, restore that mode's exact arrangement
        # (positions, tab groups, visibility). First visit keeps the as-built layout.
        saved = self._states.get(mode)
        if saved is not None:
            self._window.restoreState(saved)

        if self._on_change is not None:
            self._on_change(mode)
