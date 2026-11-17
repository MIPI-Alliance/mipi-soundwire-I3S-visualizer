"""The Bus Visualizer's authoring: the authored Bus Grid drawn from the panel's config,
the panel's requests (Load Init, Reset, Load, Save, Export JSON), File ▸ Visualizer, and
Compare ▸ Compare with Visualizer.

`AuthoringController` draws the authored grid on the Visualizer engine and reads and writes
the panel's config as a Visualizer CSV. It reaches the panel, the authored grid, the mode,
the status bar and the file-dialog directories through the window it is given; its own
state is the debounce timer of the redraw.
"""
from __future__ import annotations

import os
import sys
import tempfile
from typing import TYPE_CHECKING

import swi3score
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QFileDialog, QMessageBox

from ..fsutil import write_whole
from ..model import viz_engine
from ..model.bus_config import BusConfig, cds_symbol, demo_config
from ..model.viz_engine import MAX_AUTHORED_ROWS
from . import view_state
from .mode_controller import ANALYSIS, VISUALIZATION

if TYPE_CHECKING:
    from .main_window import MainWindow

# NumColumns_REG's range: the register holds the column count less one, over the core's
# column-count range.
NUM_COLUMNS_REG = (swi3score.min_column_count - 1, swi3score.max_column_count - 1)


def authored_rows(wanted: int) -> tuple:
    """(rows to build, the Issue naming the limit or None) for a Rows To Draw value. The
    authored Bus Grid and the JSON export build at most `viz_engine.MAX_AUTHORED_ROWS` rows,
    the headless export's limit too; the Rows To Draw field accepts more, and a larger value
    is drawn to that limit with an Issue saying so."""
    from ..analysis.issues import WARNING, Issue
    rows = max(1, min(int(wanted), MAX_AUTHORED_ROWS))
    if int(wanted) <= MAX_AUTHORED_ROWS:
        return rows, None
    return rows, Issue(WARNING, "Rows To Draw",
                       f"Rows To Draw is {int(wanted)}; the Bus Grid and the JSON export "
                       f"build the first {rows} rows.")


def _app_home() -> str:
    """The obvious, user-editable directory where init.csv lives: next to the
    executable for a frozen build, else the repo root for a source checkout. This
    is the spot a user drops their own init.csv to replace the default."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    # swi3s_studio/ui/authoring_controller.py -> repo root
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _init_csv_path() -> str:
    """Resolve a user-supplied init.csv (it's a local file, not shipped with the
    app). Order: $SWI3S_INIT_CSV, then init.csv in the app home (repo root / next to
    the executable), then the working directory. Returns the app-home path when none
    exists, so load_init_csv's open fails cleanly and falls back to the demo config."""
    env = os.environ.get("SWI3S_INIT_CSV")
    if env and os.path.exists(env):
        return env
    for d in (_app_home(), os.getcwd()):
        c = os.path.join(d, "init.csv")
        if os.path.exists(c):
            return c
    return os.path.join(_app_home(), "init.csv")


def _authored_slot_names(cfg) -> dict:
    """{slot index: user-assigned port name} for the Bus-Visualizer colour key. Skips the
    default "DP{i}" placeholder so the key falls back to the device-qualified number,
    which identifies the port where a bare "DP0" cannot (several devices may each own a
    DP0). Best-effort: a config shape without names just yields an empty map."""
    names = {}
    try:
        for i, dp in enumerate(cfg.dataports):
            name = (getattr(dp, "name", "") or "").strip()
            if name and name != f"DP{i}":
                names[i] = name
    except Exception:  # noqa: BLE001 — the key degrades to numbers; never break the render
        return {}
    return names


class AuthoringController:
    """The Bus Visualizer authoring of `win`, a MainWindow, wired to its AuthoringPanel."""

    def __init__(self, win: "MainWindow") -> None:
        self._win = win
        panel = win._authoring
        # Debounce authoring edits: a full engine rebuild (O(rows×columns×DPs)) per field
        # blur makes the panel unresponsive at large row counts. Coalesce rapid edits
        # through a short timer; direct callers still rebuild immediately.
        self._refresh_timer = QTimer(win)
        self._refresh_timer.setSingleShot(True)
        self._refresh_timer.setInterval(150)
        self._refresh_timer.timeout.connect(self.refresh_grid)
        panel.configChanged.connect(self.schedule_refresh)
        panel.issueActivated.connect(self._on_issue_activated)
        panel.loadInitRequested.connect(self.load_init_csv)
        panel.resetRequested.connect(lambda: panel.set_config(BusConfig()))
        panel.loadRequested.connect(self.load_csv)
        panel.saveRequested.connect(self.save_csv)
        panel.exportJsonRequested.connect(self.export_bus_model_json)

    def schedule_refresh(self) -> None:
        """Coalesce a burst of authoring edits into one rebuild (see the debounce timer in
        __init__)."""
        self._refresh_timer.start()

    def refresh_grid(self) -> None:
        """Render Bus-Visualizer from the Visualizer engine (swi3s_studio.swviz,
        exact parity) — the merged bus model gives the grid bits + clashes + the notifications."""
        cfg = self._win._authoring.config()
        rows, rows_note = authored_rows(cfg.rows_to_draw)
        try:
            with tempfile.TemporaryDirectory() as d:
                path = cfg.to_csv_file(os.path.join(d, "authored.csv"))
                cells, clashes, issues, ncols, nrows = viz_engine.render_payload(path, rows)
        except Exception as exc:  # noqa: BLE001 — surface engine errors as an issue
            from ..analysis.issues import ERROR, Issue
            lo, hi = NUM_COLUMNS_REG
            reg = view_state.whole_number(cfg.num_columns)
            columns = min(max(reg if reg is not None else hi, lo), hi) + 1
            self._win._viz_grid.set_bus_model([], {}, columns, 1)
            self._win._authoring.set_issues(
                [Issue(ERROR, "Engine", f"could not build: {exc}")])
            return
        self._win._viz_grid.set_bus_model(cells, clashes, ncols, nrows,
                                          slot_names=_authored_slot_names(cfg),
                                          cds_symbol=cds_symbol(cfg.cds_drive_type,
                                                                cfg.cds_end_drive_early))
        self._win._authoring.set_issues(list(issues) + ([rows_note] if rows_note else []))

    def _on_issue_activated(self, _cells: list) -> None:
        self.refresh_grid()

    def load_init_csv(self) -> None:
        """Load a user-supplied init.csv (from the app home / repo root) into the
        authoring panel, showing "Loaded: init.csv" like Load Settings. Falls back to
        the built-in demo config when no init.csv is present or it can't be read."""
        path = _init_csv_path()
        try:
            with self._win.reporting_import():
                cfg = self.config_from_csv(path)
        except Exception as exc:  # noqa: BLE001 — missing/unreadable init.csv
            self._win._authoring.set_config(demo_config())
            self._win._authoring.set_file_status("Loaded: built-in demo config")
            self._win._status.setText(f"init.csv unavailable ({exc}); loaded demo config")
            return
        self._win._authoring.set_config(cfg)
        self._win._status.setText(f"Loaded init config: {path}")
        self._win._authoring.set_file_status(f"Loaded: {os.path.basename(path)}")

    def load_csv(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self._win, "Load bus config (visualizer CSV)",
            self._win._last_capture_dir("visualizer"), "CSV (*.csv);;All files (*)")
        if path:
            self._win._remember_capture_dir(path, "visualizer")
            self._load_csv(path)

    def open_csv(self) -> None:
        """File ▸ Open Visualizer CSV: load a Visualizer config CSV into the authoring
        panel, switching to Visualizer mode if we're elsewhere. Legacy / v2.0 CSVs are
        upgraded to the current format on the way in (visualizer_csv.normalized_path)."""
        path, _ = QFileDialog.getOpenFileName(
            self._win, "Open Visualizer CSV", self._win._last_capture_dir("visualizer"),
            "Visualizer CSV (*.csv);;All files (*)")
        if not path:
            return
        self._win._remember_capture_dir(path, "visualizer")
        self.open_csv_path(path)

    def open_csv_path(self, path: str) -> None:
        """Open a Visualizer config CSV by PATH — the dialog-free half of
        open_visualizer_csv, so the `-c` command-line option and the menu item take the
        same route (mode switch, then load). A caller passing a path never raises a
        file dialog."""
        if self._win._mode_mgr.current() != VISUALIZATION:
            self._win._mode_mgr.switch_to(VISUALIZATION)
        self._load_csv(path)

    def _load_csv(self, path: str) -> None:
        """Read a Visualizer CSV into the authoring panel (upgrading legacy/v2.0 CSVs),
        showing its import warnings."""
        try:
            with self._win.reporting_import():
                cfg = self.config_from_csv(path)
            self._win._authoring.set_config(cfg)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self._win, "Load config", f"Couldn't read that CSV: {exc}")
            return
        self._win._status.setText(f"Loaded config: {path}")
        self._win._authoring.set_file_status(f"Loaded: {os.path.basename(path)}")

    def config_from_csv(self, path: str) -> "BusConfig":
        """Read a BusConfig from a Visualizer CSV, upgrading any legacy/v2.0 format and
        removing the temp file normalized_path may create, so repeated loads of an old
        config don't leak temps into the OS temp dir. Import warnings name `path`."""
        from ..ingest import visualizer_csv
        cfg_path = visualizer_csv.normalized_path(path)   # upgrade any legacy/v2.0 CSV
        try:
            return BusConfig.from_csv(cfg_path, source=path)   # holds NumColumns_REG in range
        finally:
            if cfg_path != path:
                try:
                    os.remove(cfg_path)
                except OSError:
                    pass

    def save_csv(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self._win, "Save bus config",
            os.path.join(self._win._last_capture_dir("visualizer"), "bus_config.csv"),
            "CSV (*.csv)")
        if not path:
            return
        path = self._win._suffixed_save_path(path, ".csv", "Save bus config")
        if path is None:
            return
        self._win._remember_capture_dir(path, "visualizer")
        self._win._authoring.commit_edits()     # flush a name/field still being edited
        try:
            self._win._authoring.config().to_csv_file(path)
        except OSError as exc:
            QMessageBox.critical(self._win, "Save config", str(exc))
            return
        self._win._status.setText(f"Config saved: {path}")
        self._win._authoring.set_file_status(f"Saved: {os.path.basename(path)}")

    def export_bus_model_json(self) -> None:
        """Write the authored bus model (the Visualizer engine's serialized model —
        bits + clashes + warnings) to JSON. The file is replaced only once the whole model
        has been serialized and written."""
        path, _ = QFileDialog.getSaveFileName(
            self._win, "Export bus model JSON",
            os.path.join(self._win._last_capture_dir("visualizer"), "bus_model.json"),
            "JSON (*.json)")
        if not path:
            return
        path = self._win._suffixed_save_path(path, ".json", "Export bus model JSON")
        if path is None:
            return
        self._win._remember_capture_dir(path, "visualizer")
        self._win._authoring.commit_edits()
        cfg = self._win._authoring.config()
        rows, rows_note = authored_rows(cfg.rows_to_draw)
        try:
            import json
            model = viz_engine.authored_model_json(cfg, rows)
            text = json.dumps(model, indent=2)

            def put(f) -> None:
                f.write(text)
            write_whole(path, put, mode="w", encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self._win, "Export JSON", str(exc))
            return
        limit = f" (limited to {rows} rows)" if rows_note else ""
        self._win._status.setText(f"Bus model exported: {path}{limit}")
        self._win._authoring.set_file_status(f"Exported: {os.path.basename(path)}{limit}")

    def use_as_expected(self) -> None:
        """Compare ▸ Compare with Visualizer: push the Visualizer-authored config
        into Analysis ▸ Compare as the expected config (register-map diff)."""
        if self._win._session is None:
            QMessageBox.information(self._win, "Compare with Visualizer",
                                    "Load a capture in Analysis mode first, then compare "
                                    "the Visualizer config against it.")
            return
        self._win._authoring.commit_edits()
        cfg = self._win._authoring.config()
        with tempfile.TemporaryDirectory() as d:
            path = cfg.to_csv_file(os.path.join(d, "expected.csv"))
            self._win._mode_mgr.switch_to(ANALYSIS)
            self._win._for_group("right", lambda: self._win._run_compare(path))()
