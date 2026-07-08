"""A configuration issue surfaced by Visualization-mode authoring.

The first-party Visualizer engine's (`swi3s_studio/swviz`) clashes + warnings are
mapped to `Issue`s (`model/viz_engine.py::issues_from_model`), which the authoring
notifications panel groups by severity and (where applicable) highlights on the bus grid.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Tuple

# Severity ordering for sorting/colour.
ERROR = "error"
WARNING = "warning"
INFO = "info"
_RANK = {ERROR: 0, WARNING: 1, INFO: 2}


@dataclass
class Issue:
    severity: str                 # ERROR | WARNING | INFO
    source: str                   # e.g. "DP3", "Interface", "DP0 vs DP2"
    message: str
    cells: List[Tuple[int, int]] = field(default_factory=list)  # (row, col) to highlight

    @property
    def rank(self) -> int:
        return _RANK.get(self.severity, 3)


def sort_issues(issues: List[Issue]) -> List[Issue]:
    """Errors first, then warnings, then info; stable within a severity."""
    return sorted(issues, key=lambda i: i.rank)
