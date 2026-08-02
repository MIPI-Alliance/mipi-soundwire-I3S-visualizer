"""Analyzer ▸ Import Visualizer CSV dialog.

Picks a Visualizer data-port config and what to do with it against the open
capture. The config comes from either a CSV file or the current Visualizer
authoring (in-memory), and the action is one of:

  * Update grid & registers — impose the config on the decode from row 0
    (Provenance.CSV), so a post-commit capture matches the config.
  * Compare against decoded — overlay the config's expected registers in the
    Register Map (blue) beside the snooped values (green) and report the diff.

This folds together the old File ▸ Apply Config CSV, Compare ▸ CSV Config, and
Compare ▸ Visualizer into one place (see MainWindow.open_visualizer_config).
"""
from __future__ import annotations

from PySide6.QtWidgets import (
    QButtonGroup,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QLineEdit,
    QPushButton,
    QRadioButton,
    QVBoxLayout,
)


class OpenVisualizerConfigDialog(QDialog):
    """Result attributes (valid after exec() returns Accepted):
        use_authoring : bool  — True → use the current Visualizer authoring, else the file
        path          : str   — chosen CSV path (empty when use_authoring)
        do_update     : bool  — impose the config on the decode
        do_compare    : bool  — compare the config against the decode
    """

    def __init__(self, parent, *, has_authoring: bool, start_dir: str = "") -> None:
        super().__init__(parent)
        self.setWindowTitle("Import Visualizer CSV")
        self.setMinimumWidth(460)
        self.use_authoring = False
        self.path = ""
        self.do_update = True
        self.do_compare = False

        root = QVBoxLayout(self)

        # -- source: a CSV file, or the current authoring --------------------
        src = QGroupBox("Config source")
        sl = QVBoxLayout(src)
        self._from_file = QRadioButton("From CSV file:")
        self._from_auth = QRadioButton("From Visualizer")
        self._from_file.setChecked(True)
        self._from_auth.setEnabled(has_authoring)
        if not has_authoring:
            self._from_auth.setToolTip("Author a config in Bus Visualizer first.")
        srcgrp = QButtonGroup(self)
        srcgrp.addButton(self._from_file)
        srcgrp.addButton(self._from_auth)
        sl.addWidget(self._from_file)
        row = QHBoxLayout()
        self._path_edit = QLineEdit()
        self._path_edit.setPlaceholderText("Visualizer config CSV…")
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse)
        row.addWidget(self._path_edit, 1)
        row.addWidget(browse)
        sl.addLayout(row)
        sl.addWidget(self._from_auth)
        root.addWidget(src)

        # -- action: update the decode, or compare against it ----------------
        act = QGroupBox("Action")
        al = QVBoxLayout(act)
        self._act_update = QRadioButton("Update grid && registers from config")
        self._act_compare = QRadioButton("Compare against decoded (Register Map overlay)")
        self._act_update.setChecked(True)
        actgrp = QButtonGroup(self)
        actgrp.addButton(self._act_update)
        actgrp.addButton(self._act_compare)
        al.addWidget(self._act_update)
        al.addWidget(self._act_compare)
        root.addWidget(act)

        # Typing/browsing a path selects the file source; picking authoring is explicit.
        self._path_edit.textEdited.connect(lambda _t: self._from_file.setChecked(True))
        # "Update" writes a persistent config path into the session/workspace, so it's
        # only offered for a real file. In-memory authoring is transient → Compare only
        # (this is the old "Compare with Visualizer"). Toggling the source enforces that.
        self._from_auth.toggled.connect(self._sync_action_enabled)
        self._sync_action_enabled()
        self._start_dir = start_dir

        self._buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self._buttons.accepted.connect(self._accept)
        self._buttons.rejected.connect(self.reject)
        root.addWidget(self._buttons)

    def _sync_action_enabled(self) -> None:
        authoring = self._from_auth.isChecked()
        self._act_update.setEnabled(not authoring)
        if authoring:
            self._act_compare.setChecked(True)       # authoring can only be compared
            self._act_update.setToolTip("Update needs a CSV file (authoring is in-memory).")
        else:
            self._act_update.setToolTip("")

    def _browse(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Visualizer config CSV", self._start_dir,
            "Visualizer CSV (*.csv);;All files (*)")
        if path:
            self._path_edit.setText(path)
            self._from_file.setChecked(True)

    def _accept(self) -> None:
        self.use_authoring = self._from_auth.isChecked()
        self.path = self._path_edit.text().strip()
        self.do_update = self._act_update.isChecked()
        self.do_compare = self._act_compare.isChecked()
        if not self.use_authoring and not self.path:
            self._path_edit.setPlaceholderText("Choose a CSV file or pick authoring…")
            return                                   # keep the dialog open — no source
        self.accept()
