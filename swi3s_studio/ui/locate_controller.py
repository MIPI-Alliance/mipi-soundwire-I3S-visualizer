"""File ▸ Locate Sub-Capture: a second capture read and searched for in the open one off
the GUI thread, behind a progress dialog, and each match marked with a cursor.

`LocateController` is a QObject child of the window: its search worker's signals connect to
its bound methods, so they run queued on the GUI thread. It holds the one search in flight
(its thread, worker, progress dialog, the session searched and the reference's path) and
reaches the open capture, the Links, the cursors and the status bar through the window.
"""
from __future__ import annotations

import functools
import os
from typing import TYPE_CHECKING, Any, Optional

from PySide6.QtCore import QObject, Qt, QThread, Signal
from PySide6.QtWidgets import QDialog, QFileDialog, QMessageBox, QProgressDialog

from .. import import_notes
from ..ingest import saleae_binary
from ..session import Session

if TYPE_CHECKING:
    from .main_window import MainWindow

# The file filter of every dialog that opens a capture; the format labels are the ones the
# export dialog and the user guide use.
CAPTURE_FILTER = ("Captures (*.sal *.csv *.bin *.vcd *.wfm);;Logic 2 project (*.sal);;"
                  "Digital CSV (*.csv);;Saleae binary (*.bin);;VCD (*.vcd);;"
                  "Tektronix waveform (*.wfm);;All files (*)")


def failure_text(exc: BaseException) -> str:
    """A worker's exception as the message its `failed` signal carries: the exception's
    text, led by its type name when the text is empty or is a KeyError's bare key."""
    return (f"{type(exc).__name__}: {exc}" if not str(exc) or isinstance(exc, KeyError)
            else str(exc))


def reference_capture(pr, req):
    """A bare Capture (no Session, no decode) of what Locate's dialog picked: the
    reference is searched for, so only its edges are needed. Same readers as Open
    Capture, cut to the window when one was asked for."""
    from ..ingest import analog, analog_csv, digital_csv, saleae_sal, vcd
    from ..ingest import wfm as wfm_ingest
    link = req.links[0]
    rate = req.sample_rate_hz or int(pr.sample_rate_hz or 0)
    window = None
    if req.window_s is not None and pr.kind == "sal" and rate:
        window = Session.window_samples(req.window_s, rate)
    k = pr.kind
    if k == "sal":
        return saleae_sal.load_capture(pr.paths[0], link.clock, link.data,
                                       auto_clock=link.auto_clock, window=window)
    if k == "vcd":
        cap = vcd.load_capture(pr.paths[0], link.clock, link.data,
                               auto_clock=link.auto_clock)
    elif k == "digital_csv":
        cap = digital_csv.load_capture(pr.paths[0], link.clock, link.data)
    elif k == "saleae_binary":
        cap = saleae_binary.load_capture(link.clock, link.data, rate)
    elif k == "analog_csv":
        parsed = analog_csv.read_analog_csv(pr.paths[0])
        cap = analog.capture_from_analog(
            parsed["time"], parsed["channels"][link.clock], parsed["channels"][link.data],
            parsed["sample_rate_hz"] or None, link.clock_thresh, link.data_thresh,
            auto_clock=link.auto_clock, source=pr.name)
    elif k == "wfm":
        a, b = wfm_ingest.read_wfm_pair(link.clock, link.data)
        cap = analog.capture_from_analog(a["time"], a["volts"], b["volts"], None,
                                         link.clock_thresh, link.data_thresh,
                                         auto_clock=link.auto_clock, source=pr.name)
    else:
        raise ValueError(f"{pr.name}: unsupported sub-capture format")
    if req.window_s is None:
        return cap
    # At the rate the reference loaded at, as Open Capture's windows are.
    return cap.subcapture(*Session.window_samples(req.window_s, cap.sample_rate_hz))


class _LocateWorker(QObject):
    """Reads the reference capture (`load`, called with no arguments, when given), then runs
    Session.locate_subcapture (a direct convolution, or an exact substring search on a
    large capture; a second pass with clock and data swapped when the first finds nothing)
    off the GUI thread, like main_window._LoadWorker.

    The read runs inside an import_notes collector opened on this thread; what it gathers
    is `notes` when `done` or `failed` is emitted. A read or a search that raises ends in
    `failed`."""
    done = Signal(list, dict)     # (matches, diagnostics)
    failed = Signal(str)
    progress = Signal(int, str)   # (percent 0..100, phase label) for the progress dialog

    def __init__(self, session, sub=None, load=None) -> None:
        super().__init__()
        self._session = session
        self._sub = sub
        self._load = load
        self.notes: list = []

    def run(self) -> None:
        try:
            if self._load is not None:
                with import_notes.collect() as notes:
                    try:
                        self._sub = self._load()
                    finally:
                        self.notes = list(notes)       # kept when the read raises
            diag: dict = {}
            matches = self._session.locate_subcapture(
                self._sub, diagnostics=diag,
                progress=lambda f, label: self.progress.emit(int(f * 100), label))
        except Exception as exc:  # noqa: BLE001 - surfaced to the user via `failed`
            self.failed.emit(failure_text(exc))
            return
        self.done.emit(matches, diag)


class LocateController(QObject):
    """Locate Sub-Capture for `win`, a MainWindow: at most one search at a time."""

    def __init__(self, win: "MainWindow") -> None:
        super().__init__(win)
        self._win = win
        self.search_thread: Optional[QThread] = None   # the sub-capture search in flight
        self.worker: Any = None
        self.dlg: Any = None
        self._setting_value = 0              # progress setValue() calls on the stack
        self._retired_dlgs: list = []        # torn down under a setValue(), not yet deleted
        self.session: Optional[Session] = None
        self.path = ""

    def start(self) -> None:
        """File ▸ Locate Sub-Capture (Analyzer): open a SECOND capture (any format the Open
        Capture dialog accepts — select BOTH files together for the two-file .bin /
        .wfm formats) and find every place its signal pattern occurs in the
        currently-open capture (Session.locate_subcapture — sample-rate independent).
        Drops a bookmark at each match's start_sample, labeled with its rank + score,
        and reports the count.

        The search itself (a direct convolution, or an exact substring search on a large
        capture; a second pass with clock and data swapped when the first finds nothing)
        runs on a worker thread (see _LocateWorker), with a progress dialog."""
        if self._win._session is None:
            QMessageBox.information(self._win, "No capture", "Open a capture first.")
            return
        if self._win._probing:
            return                          # a file is being read for a dialog already
        # ONE SEARCH AT A TIME, refused BEFORE the file dialog. A second search would
        # overwrite search_thread / worker / dlg / path while the
        # first worker still runs, and the abandoned QThread would never be joined.
        #
        # Not queued, unlike a re-decode: the input is a file the user chose, so the user is
        # told the previous search is still running.
        if self._win._closing:
            return
        if self.search_thread is not None:
            QMessageBox.information(
                self._win, "Search already running",
                "A sub-capture search is already in progress. Wait for it to finish "
                "before starting another.")
            return
        if self._win._load_thread is not None:
            QMessageBox.information(
                self._win, "Decode running",
                "A capture is still being decoded. Wait for it to finish before "
                "searching it.")
            return
        paths, _ = QFileDialog.getOpenFileNames(
            self._win,
            self._win._titled("Locate sub-capture (.sal, .csv, .vcd, or both .bin/.wfm files)"),
            self._win._last_capture_dir(), filter=CAPTURE_FILTER)
        if not paths:
            return
        self._win._remember_capture_dir(paths[0], "analyzer")
        # The reference's signals (and how much of it) are picked in Open Capture's dialog,
        # in its reference form: one row, no name, no Into.
        from .open_capture_dialog import OpenCaptureDialog
        try:
            pr = self._win._probe_files(paths)
        except Exception as exc:  # noqa: BLE001 - surface a file that is not a capture
            QMessageBox.warning(self._win, "Locate Sub-Capture", str(exc))
            return
        if pr is None:
            return                          # the window began closing while it was read
        if len(pr.channels) < 2:
            QMessageBox.warning(self._win, "Locate Sub-Capture",
                                f"{pr.name}: fewer than two channels to use as clock and data.")
            return
        pick = OpenCaptureDialog(pr, parent=self._win, reference=True)
        if pick.exec() != QDialog.Accepted:
            return    # cancelled — silent, no error
        path = pr.paths[0]
        # The reference is read on the worker, behind the progress dialog, then searched for.
        dlg = QProgressDialog("Reading the reference capture…", None, 0, 0, self._win)
        dlg.setWindowTitle(self._win._titled("Locate Sub-Capture"))
        dlg.setWindowModality(Qt.ApplicationModal)
        dlg.setCancelButton(None)         # a read or a search runs to its end
        dlg.setMinimumDuration(0)
        dlg.setAutoClose(False)
        dlg.setAutoReset(False)
        self.dlg = dlg
        self.search_thread = QThread(self._win)
        # The matches are samples of THIS session: they are bookmarked on the Link holding
        # it when the search finishes, whichever Link is active then, and dropped if no
        # Link holds it any more (an open replaced the analysis while the search ran).
        self.session = self._win._session
        self.worker = _LocateWorker(
            self._win._session, load=functools.partial(self._win._reference_capture, pr,
                                                       pick.result_request()))
        self.worker.moveToThread(self.search_thread)
        self.search_thread.started.connect(self.worker.run)
        # Worker signals connect to bound methods of this controller (a QObject on the GUI
        # thread), never to lambdas: a lambda has no thread affinity and runs on the worker
        # thread, where teardown would wait on its own thread. A bound method runs queued
        # on the GUI thread, like _LoadWorker.done. The path is stashed for the slot.
        self.path = path
        self.worker.progress.connect(self._on_progress)
        self.worker.done.connect(self._on_done)
        self.worker.failed.connect(self._on_failed)
        self.search_thread.start()
        dlg.show()

    def _on_progress(self, percent: int, label: str) -> None:
        dlg = self.dlg
        if dlg is None:
            return
        if dlg.maximum() == 0:
            dlg.setRange(0, 100)          # the read has no metric; the search has one
        if label:
            dlg.setLabelText(f"Searching for sub-capture — {label}")
        # A modal QProgressDialog.setValue() re-enters the event loop (processEvents),
        # which can dispatch another progress tick and the queued 'done' signal, tearing the
        # dialog down mid-call. Operate on the LOCAL ref and do setValue LAST, so nothing
        # dereferences self.dlg after it may have been set to None. A dialog torn down while
        # any setValue() is on the stack is deleted only once the outermost one returns,
        # since a nested event loop would otherwise run its deferred delete under that call.
        self._setting_value += 1
        try:
            dlg.setValue(percent)
        finally:
            self._setting_value -= 1
        if self._setting_value == 0:
            retired, self._retired_dlgs = self._retired_dlgs, []
            for d in retired:
                d.deleteLater()

    def _on_done(self, matches: list, diag: dict) -> None:
        if self._win._closing:              # delivered after the window began closing
            self.teardown()
            self.session = None
            return
        worker = self.worker
        notes = list(worker.notes) if worker is not None else []
        self._win.show_import_warnings(notes)
        self.finish(self.path, matches, diag)

    def finish(self, path: str, matches: list, diag: dict) -> None:
        self.teardown()
        searched = self.session
        if searched is None:
            return                          # this search's result was already handled
        self.session = None
        link = self._win._links.index_of(searched)
        if link < 0:
            self._win._status.setText(
                f"Locate Sub-Capture: results for {os.path.basename(path)} discarded; "
                f"the capture searched is no longer open")
            return
        title = self._win._titled("Locate Sub-Capture", link)
        where = self._win._on_link(link)
        if not matches:
            from ..analysis.subcapture import DEFAULT_SCORE_THRESHOLD
            best = float(diag.get("best_score", 0.0))
            best_start = diag.get("best_start_sample")
            placed = ""
            if best_start is not None:
                bm = self._win._bookmarks_ctl.bookmarks.add(int(best_start), link=link)
                bm.label = f"Best (no match)  score={best:.3g}"
                self._win._bookmarks_ctl.push()
                placed = ("\n\nA cursor was placed at the best candidate so you "
                          "can inspect it.")
            QMessageBox.information(
                self._win, title,
                f"No occurrences of {os.path.basename(path)} found{where}.\n\n"
                f"Best candidate scored {best * 100:.1f}% (needs "
                f"≥ {DEFAULT_SCORE_THRESHOLD * 100:.0f}% — only near-exact matches "
                f"count). A low best score means the pattern doesn't recur here; a "
                f"high one that still misses suggests a different acquisition "
                f"(sample rate / analog threshold) rather than a true occurrence."
                f"{placed}")
            self._win._status.setText(
                f"Locate Sub-Capture{where}: no matches (best {best * 100:.1f}%)")
            return
        n = len(matches)
        found = f"{n} match(es)"
        if diag.get("truncated"):
            total = int(diag.get("total_matches", n))
            at_least = "at least " if diag.get("hits_capped") else ""
            found = f"{at_least}{max(total, n)} matches, of which {n}"
        for i, m in enumerate(matches, start=1):
            bm = self._win._bookmarks_ctl.bookmarks.add(int(m["start_sample"]), link=link)
            bm.label = f"Sub {i}/{len(matches)}  score={m.get('score', 0):.3g}"
        self._win._bookmarks_ctl.push()
        self._win._status.setText(
            f"Locate Sub-Capture{where}: {found} marked with cursors "
            f"from {os.path.basename(path)}")
        # No icon on the matches announcement (plain informational text).
        box = QMessageBox(self._win)
        box.setWindowTitle(title)
        box.setIcon(QMessageBox.Icon.NoIcon)
        if diag.get("truncated"):
            box.setText(f"Found {found} are marked{where}; cursors placed at each marked "
                        f"start sample. More matches exist than the list holds.")
        else:
            box.setText(f"Found {found}{where}; cursors placed at each start sample.")
        box.exec()

    def _on_failed(self, message: str) -> None:
        if self._win._closing:              # delivered after the window began closing
            self.teardown()
            self.session = None
            return
        worker = self.worker
        notes = list(worker.notes) if worker is not None else []
        self.teardown()
        self.session = None                 # a failed search keeps no capture alive
        self._win.show_import_warnings(notes)
        QMessageBox.critical(self._win, "Locate failed", message)

    def teardown(self) -> None:
        # Idempotent: the modal progress dialog's setValue() re-enters the event loop, so
        # 'done'/'failed' can fire more than once (or during a progress tick). Bail if the
        # dialog is already gone rather than dereferencing None.
        #
        # Guard the THREAD too, not just the dialog: join_worker_threads() (window close
        # / atexit) clears search_thread while a queued done/failed may still be in
        # flight, and wait() returning doesn't mean that signal was delivered.
        if self.dlg is None or \
           self.search_thread is None:
            self.dlg = None
            self.worker = None
            return
        self.dlg.close()
        if self._setting_value:
            self._retired_dlgs.append(self.dlg)     # deleted when the setValue() returns
        else:
            self.dlg.deleteLater()
        self.search_thread.quit()
        self.search_thread.wait()
        self.search_thread.deleteLater()
        self.worker.deleteLater()
        self.dlg = None
        self.search_thread = None
        self.worker = None
        self._win._close_dropped_sessions()
