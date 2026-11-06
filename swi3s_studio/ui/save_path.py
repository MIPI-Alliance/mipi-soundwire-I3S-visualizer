"""The path a save dialog returns, completed and confirmed."""
from __future__ import annotations

import os
from typing import Iterable, Optional

from PySide6.QtWidgets import QMessageBox, QWidget


def suffixed_save_path(parent: QWidget, path: str, suffix: str, title: str,
                       accepted: Iterable[str] = ()) -> Optional[str]:
    """`path` from a save dialog, with `suffix` added unless its extension is `suffix` or
    one of `accepted` (compared without regard to case). A dot inside a name ("run 1.5")
    is not an extension of the format. The dialog confirmed a replacement only for the
    name as typed, so an added suffix that names an existing file is confirmed here.
    None when the user declines the replacement."""
    kept = {suffix.lower(), *(s.lower() for s in accepted)}
    if os.path.splitext(path)[1].lower() in kept:
        return path
    path += suffix
    if os.path.exists(path) and QMessageBox.question(
            parent, title,
            f"{os.path.basename(path)} exists and will be replaced.\n\nReplace it?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
        return None
    return path
