"""How a data port is labelled in the panes: the name the user gave it (Session.dp_names,
from Audio ▸ right-click a channel ▸ Rename…), else its device and port numbers."""
from __future__ import annotations

from typing import Mapping, Tuple

Names = Mapping[Tuple[int, int], str]


def port_label(names: Names, dev: int, dp: int) -> str:
    """"Mic L", or "Dev1 DP2" for a port with no name."""
    return (names.get((int(dev), int(dp))) or "").strip() or f"Dev{dev} DP{dp}"


def with_name(names: Names, dev: int, dp: int, plain: str) -> str:
    """`plain` (a menu's or dialog's own "Device 1 · DP2") with the port's name in front,
    "Mic L · Device 1 · DP2", or `plain` alone for a port with no name."""
    name = (names.get((int(dev), int(dp))) or "").strip()
    return f"{name} · {plain}" if name else plain


def port_identity(names: Names, dev: int, dp: int) -> str:
    """"Mic L (Dev1 DP2)" for a named port, where the numbers matter too; else
    "Dev1 DP2"."""
    name = (names.get((int(dev), int(dp))) or "").strip()
    return f"{name} (Dev{dev} DP{dp})" if name else f"Dev{dev} DP{dp}"
