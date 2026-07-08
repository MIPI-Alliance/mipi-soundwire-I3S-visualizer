"""Grouped notifications panel — a Qt port of the SWI3S Visualizer's
`src/ui/error_panel.py`. Issues are grouped by severity into collapsible sections,
each with a coloured header button showing the count (▶ collapsed / ▼ expanded);
clicking an item re-emits its grid cells so the grid can highlight them. When there
are no issues a green "No issues detected" line shows.
"""
from __future__ import annotations

from typing import List

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QFrame, QLabel, QPushButton, QScrollArea,
                               QVBoxLayout, QWidget)

from ...analysis.issues import Issue, ERROR, WARNING, INFO
from ..theme import VizTheme, scrollbar_qss

# Severity -> (section title, header colour) — the Visualizer's COLORS palette.
_GROUPS = [
    (ERROR, "Errors", "#D32F2F"),          # red - critical
    (WARNING, "Warnings", "#F57C00"),      # orange - warning
    (INFO, "Informational", "#1976D2"),    # blue - informational
]
_OK_COLOR = "#388E3C"                       # green - no errors


class _Section(QWidget):
    """A collapsible section with a coloured header button + item list."""
    activated = Signal(list)

    def __init__(self, title: str, color: str) -> None:
        super().__init__()
        self._title = title
        self._expanded = False
        self._items: List[tuple] = []
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 2)
        v.setSpacing(0)
        self._header = QPushButton(f"▶ {title} (0)")
        self._header.setCursor(Qt.PointingHandCursor)
        self._header.setStyleSheet(
            f"QPushButton {{ background:{color}; color:white; font-weight:bold; "
            f"border:none; border-radius:4px; padding:5px; text-align:center; }}")
        self._header.clicked.connect(self._toggle)
        v.addWidget(self._header)
        self._content = QWidget()
        self._cl = QVBoxLayout(self._content)
        self._cl.setContentsMargins(8, 2, 2, 2)
        self._cl.setSpacing(1)
        self._content.setVisible(False)
        v.addWidget(self._content)
        self.setVisible(False)

    def _toggle(self) -> None:
        self._expanded = not self._expanded
        self._content.setVisible(self._expanded)
        self._refresh_header()

    def _refresh_header(self) -> None:
        arrow = "▼" if self._expanded else "▶"
        self._header.setText(f"{arrow} {self._title} ({len(self._items)})")

    def set_items(self, items: List[tuple]) -> None:
        while self._cl.count():
            w = self._cl.takeAt(0).widget()
            if w is not None:
                w.deleteLater()
        self._items = items
        for text, cells in items:
            lab = QLabel(text)
            lab.setWordWrap(True)
            lab.setStyleSheet(f"color:{VizTheme.TEXT};")
            if cells:
                lab.setCursor(Qt.PointingHandCursor)
                lab.mousePressEvent = lambda _e, c=cells: self.activated.emit(list(c))
            self._cl.addWidget(lab)
        self._refresh_header()
        self.setVisible(bool(items))

    def texts(self) -> List[str]:
        return [t for t, _ in self._items]

    def retheme(self) -> None:
        """Re-render the items so each label's colour picks up the active palette."""
        self.set_items(self._items)


class NotificationsPanel(QWidget):
    issueActivated = Signal(list)

    def __init__(self) -> None:
        super().__init__()
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        # Outer rounded/bordered frame (id-scoped so the border doesn't cascade to
        # children); the scroll area sits inset inside it, so the scrollbar and
        # square content can't break the rounded corners.
        frame = QFrame()
        frame.setObjectName("notifFrame")
        frame.setStyleSheet(
            f"#notifFrame {{ border:{VizTheme.BORDER_WIDTH}px solid {VizTheme.BORDER}; "
            f"border-radius:{VizTheme.CORNER_RADIUS}px; background:{VizTheme.FRAME_BG}; }}")
        self._frame = frame
        fl = QVBoxLayout(frame)
        fl.setContentsMargins(4, 4, 4, 4)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        # Only show the (transparent-track, rounded-handle) scrollbar when the issue
        # list overflows — otherwise the mouse/trackpad scrolls and nothing stands out.
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        scroll.setStyleSheet("QScrollArea { border:none; background:transparent; }"
                             + scrollbar_qss())
        self._scroll = scroll
        host = QWidget()
        self._host_l = QVBoxLayout(host)
        self._host_l.setContentsMargins(6, 14, 6, 6)    # nudge "No issues" down from the top
        self._host_l.setSpacing(2)
        self._sections = {}
        for sev, title, color in _GROUPS:
            s = _Section(title, color)
            s.activated.connect(self.issueActivated)
            self._sections[sev] = s
            self._host_l.addWidget(s)
        self._ok = QLabel("No issues detected")
        self._ok.setAlignment(Qt.AlignHCenter | Qt.AlignTop)
        self._ok.setStyleSheet(f"color:{_OK_COLOR};")
        self._host_l.addWidget(self._ok)
        self._host_l.addStretch(1)
        scroll.setWidget(host)
        fl.addWidget(scroll)
        v.addWidget(frame)

    def set_issues(self, issues: List[Issue]) -> None:
        by = {ERROR: [], WARNING: [], INFO: []}
        for iss in issues:
            by.setdefault(iss.severity, by[INFO]).append(
                (f"{iss.source}: {iss.message}", iss.cells))
        any_issue = False
        for sev, _title, _color in _GROUPS:
            self._sections[sev].set_items(by.get(sev, []))
            any_issue = any_issue or bool(by.get(sev))
        self._ok.setVisible(not any_issue)

    def retheme(self) -> None:
        """Re-apply the palette after a theme switch: refresh the frame border /
        background, the scrollbar style, the "no issues" colour, and re-render each
        section's item labels (which carry the theme text colour)."""
        self._frame.setStyleSheet(
            f"#notifFrame {{ border:{VizTheme.BORDER_WIDTH}px solid {VizTheme.BORDER}; "
            f"border-radius:{VizTheme.CORNER_RADIUS}px; background:{VizTheme.FRAME_BG}; }}")
        self._scroll.setStyleSheet("QScrollArea { border:none; background:transparent; }"
                                   + scrollbar_qss())
        for s in self._sections.values():
            s.retheme()

    def item_texts(self) -> List[str]:
        """All issue strings (or ['No issues detected'] when clean) — for tests.
        Independent of widget visibility (works headless)."""
        out: List[str] = []
        for sev, _title, _color in _GROUPS:
            out += self._sections[sev].texts()
        return out or ["No issues detected"]
