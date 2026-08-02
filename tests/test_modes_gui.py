"""Three-mode shell test: the top mode switcher swaps the central page and the
visible dock set per mode, Analysis-only menus gate by mode, and the active mode
round-trips through the workspace JSON.

Run: QT_QPA_PLATFORM=offscreen PYTHONPATH=. python3 -m pytest tests/test_modes_gui.py
"""
import os
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from swi3s_studio.ui.main_window import MainWindow
from swi3s_studio.ui.mode_controller import ANALYSIS, MODES, TIMING, VISUALIZATION
from swi3s_studio.workspace import Workspace, session_from_source


def _win():
    QApplication.instance() or QApplication([])
    win = MainWindow()
    win.load_demo()
    return win


def test_commands_menu_filter_sscr_and_shortcuts():
    """Commands ▸ Filter has an 'All excl. Ping' quick action; the SSCR/DSCR jump
    actions move the cursor; the shortcuts help exists."""
    win = _win()
    # 'All excl. Ping' checks every kind except Ping.
    kinds = {a.text() for a in win._kind_menu.actions()}
    assert "All excl. Ping" in kinds
    excl = next(a for a in win._kind_menu.actions() if a.text() == "All excl. Ping")
    excl.trigger()
    checked = {a.data() for a in win._kind_menu._actions if a.isChecked()}
    assert "Ping" not in checked and checked, checked
    # SSCR/DSCR jump moves the cursor to a commit sample.
    pts = win._sscr_samples()
    assert pts, "demo has an SSCR commit"
    win.cursor.set_sample(0); win.next_sscr()
    assert win.cursor.sample in pts
    assert hasattr(win, "show_keyboard_shortcuts")
    # ⌘/Ctrl + Right in the timeline jumps to a commit (plain Right steps commands).
    from PySide6.QtCore import QEvent, Qt
    from PySide6.QtGui import QKeyEvent
    tl = win._timeline
    assert tl._commit_starts == sorted(pts)
    seen = {}
    tl.seeked.connect(lambda s: seen.update(t=s))
    tl._cursor = 0
    tl.keyPressEvent(QKeyEvent(QEvent.KeyPress, Qt.Key_Right, Qt.ControlModifier))
    assert seen.get("t") in pts, (seen, pts)


def test_starts_in_visualization():
    QApplication.instance() or QApplication([])
    win = MainWindow()                       # no capture loaded yet
    # Bus Visualizer is the default mode on open (before any capture).
    assert win._mode_mgr.current() == VISUALIZATION
    assert win._central_stack.currentIndex() == MODES.index(VISUALIZATION)
    # Opening a capture (even the demo) lands in Analysis, where the decoded,
    # cursor-gated bus grid lives — and shows all its docks + enables its menus.
    win.load_demo()
    assert win._mode_mgr.current() == ANALYSIS
    for dock in (win._timeline_dock, win._reg_dock, win._meas_dock,
                 win._grid_dock, win._audio_dock, win._symbol_dock):
        assert not dock.isHidden()
    assert all(m.isEnabled() for m in win._analysis_menus)


def test_title_shows_capture_only_in_analyzer():
    """The window title carries the capture name only in Analyzer mode; Visualizer and
    Timing just show the app name (the capture is irrelevant to those views)."""
    QApplication.instance() or QApplication([])
    win = MainWindow()
    win.load_demo()                                  # Analyzer
    assert "Demo Capture" in win.windowTitle()
    win._mode_mgr.switch_to(VISUALIZATION)
    assert "Demo Capture" not in win.windowTitle() and "SWI3S Studio" in win.windowTitle()
    win._mode_mgr.switch_to(TIMING)
    assert "Demo Capture" not in win.windowTitle()
    win._mode_mgr.switch_to(ANALYSIS)
    assert "Demo Capture" in win.windowTitle()
    # A two-file Saleae binary shows BOTH channel basenames.
    win._session.source = {"type": "bin", "clock": "/x/digital_1.bin", "data": "/x/digital_0.bin"}
    win._update_title()
    assert "digital_1.bin & digital_0.bin" in win.windowTitle()


def test_switch_swaps_central_and_docks():
    win = _win()

    win._mode_mgr.switch_to(VISUALIZATION)
    assert win._central_stack.currentIndex() == MODES.index(VISUALIZATION)
    # Visualization is full-width params + embedded grid (visualizer layout): all
    # Analysis docks (incl. the shared Bus Grid + Register Map) are hidden.
    for dock in (win._grid_dock, win._reg_dock, win._audio_dock,
                 win._timeline_dock, win._symbol_dock, win._meas_dock):
        assert dock.isHidden()
    assert not any(m.isEnabled() for m in win._analysis_menus)

    win._mode_mgr.switch_to(TIMING)
    assert win._central_stack.currentIndex() == MODES.index(TIMING)
    for dock in (win._grid_dock, win._reg_dock, win._audio_dock,
                 win._timeline_dock, win._symbol_dock, win._meas_dock):
        assert dock.isHidden()
    # Analysis-only menus (Commands/Audio/Bookmarks) are grayed outside Analysis; the
    # File submenus stay enabled (their opens work from any mode + guard at call time).
    assert not any(m.isEnabled() for m in win._analysis_menus)
    win._mode_mgr.switch_to(VISUALIZATION)
    assert not any(m.isEnabled() for m in win._analysis_menus)
    assert win._file_analyzer_menu.isEnabled()


def test_return_restores_layout():
    win = _win()
    win._mode_mgr.switch_to(VISUALIZATION)
    win._mode_mgr.switch_to(ANALYSIS)
    # Coming back restores the full Analysis dock set + re-enables its menus.
    for dock in (win._timeline_dock, win._reg_dock, win._meas_dock,
                 win._grid_dock, win._audio_dock, win._symbol_dock):
        assert not dock.isHidden()
    assert all(m.isEnabled() for m in win._analysis_menus)
    assert win._file_analyzer_menu.isEnabled()           # File submenus are always enabled


def test_mode_persists_in_workspace():
    win = _win()
    win._mode_mgr.switch_to(VISUALIZATION)
    ws = Workspace(source=win._session.source, mode=win._mode_mgr.current())
    assert Workspace.from_json(ws.to_json()).mode == VISUALIZATION

    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "ws.json")
        ws.save(path)
        win2 = MainWindow()
        loaded = Workspace.load(path)
        win2.load_session(session_from_source(loaded.source, register_map=win2._rmap))
        win2.apply_workspace(loaded)
    assert win2._mode_mgr.current() == VISUALIZATION
    assert win2._central_stack.currentIndex() == MODES.index(VISUALIZATION)


def test_authoring_places_and_checks():
    win = _win()
    win._mode_mgr.switch_to(VISUALIZATION)
    ap = win._authoring
    # The authoring panel (12 colored DP columns) + the embedded authored grid
    # are populated, demo is clean (the Notifications list shows the OK line).
    assert len(ap._dp_name) == 12
    assert win._viz_grid.item_count > 0
    assert ap._notifications.item_texts() == ["No issues detected"]

    # Edit the config into a bus clash → the Notifications panel flags an error.
    dp1 = ap.config().dataports[1]
    dp1.enabled = True; dp1.device_number = 1; dp1.enable_ch = 0b1
    dp1.sample_size = 1; dp1.interval = 7
    dp1.horizontal_start = 1; dp1.horizontal_count = 1
    ap._populate()
    win._refresh_authored_grid()
    labels = ap._notifications.item_texts()
    assert any("clash" in t.lower() for t in labels), labels


def test_use_authoring_as_expected():
    from PySide6.QtWidgets import QMessageBox

    from swi3s_studio.model.bus_config import demo_config
    # Neutralise every modal this flow can raise so a headless run can never block:
    # box.exec() on the summary dialog, plus the static warning/info shown when the
    # config yields no registers — which must surface as a failed assertion below,
    # NOT a 10-minute hang on an un-dismissable dialog.
    saved = {n: getattr(QMessageBox, n)
             for n in ("exec", "warning", "information", "critical")}
    QMessageBox.exec = lambda self: None
    for n in ("warning", "information", "critical"):
        setattr(QMessageBox, n, lambda *a, **k: None)
    try:
        win = _win()
        win._mode_mgr.switch_to(VISUALIZATION)
        # The authoring panel opens empty (reset state); author a valid config first,
        # as a user would, before pushing it in as the expected config.
        win._authoring.set_config(demo_config())
        win._authoring._populate()
        win.use_authoring_as_expected()
        # Pushes the authored config into Analysis ▸ Compare: mode flips, expected set.
        assert win._mode_mgr.current() == ANALYSIS
        assert win._reg_view.has_expected()
    finally:
        for n, fn in saved.items():
            setattr(QMessageBox, n, fn)


def test_workspace_carries_authoring():
    win = _win()
    win._mode_mgr.switch_to(VISUALIZATION)
    win._authoring.config().dataports[5].enabled = True
    win._authoring.config().dataports[5].enable_ch = 0b101
    # Timing page: set the Spec bus-length range (corners are auto-selected now, so
    # pin min == max to make the round-trip assertion exact).
    win._timing_page._min[("spec", "bus length (cm)")].setValue(42.0)
    win._timing_page._max[("spec", "bus length (cm)")].setValue(42.0)
    ws = Workspace(source=win._session.source, mode=VISUALIZATION,
                   authoring=win._authoring.config().to_dict(),
                   timing=win._timing_page.to_dict())
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "ws.json")
        ws.save(path)
        win2 = MainWindow()
        loaded = Workspace.load(path)
        win2.load_session(session_from_source(loaded.source, register_map=win2._rmap))
        win2.apply_workspace(loaded)
    assert win2._authoring.config().dataports[5].enable_ch == 0b101
    assert win2._timing_page.inputs("spec").bus_length_cm == 42.0
    assert win2._mode_mgr.current() == VISUALIZATION


def test_dp_dialogs_apply():
    """The Visualizer's per-DP picker dialogs (Device / Channels / Guard / Flow /
    Port / Display) exist and their results flow back into the config."""
    from swi3s_studio.ui.authoring import dialogs as D
    QApplication.instance() or QApplication([])

    fd = D.FlowModeSelectorDialog(None, 0, 0); fd._select(2); fd._off.setText("9"); fd.accept()
    assert fd.flow_mode == 2 and fd.fcp_offset == 9
    pd = D.PortModeSelectorDialog(None, 0, 0); pd._select(3); pd.accept()
    assert pd.port_mode == 3
    cd = D.ChannelSelectorDialog(None, 0, 0); cd._toggle(0); cd._toggle(2); cd.accept()
    assert cd.result_mask == 0b101
    gd = D.GuardSelectorDialog(None, False, 0, 0); gd._pick("g1"); gd.accept()
    assert gd.guard_enabled and gd.guard_polarity == 1
    dv = D.DeviceSelectorDialog(None, 0, 0); dv._select(D.MANAGER); dv.accept()
    assert dv.device == D.MANAGER

    # Panel wiring: a Flow Control click applies mode + FCP params to the DP.
    win = _win()
    win._mode_mgr.switch_to(VISUALIZATION)
    import swi3s_studio.ui.authoring.authoring_panel as AP

    class _Stub:
        def __init__(self, *a, **k):
            self.flow_mode = 3; self.fcp_h_start = 4; self.fcp_bit_width = 0
            self.fcp_tail_width = 0; self.fcp_offset = 7
            self.fcp_guard_enable = True; self.fcp_guard_polarity = 1
        def exec(self):
            return 1
    orig = AP.FlowModeSelectorDialog
    AP.FlowModeSelectorDialog = _Stub
    try:
        win._authoring._open_dp_dialog("flow", 0)
    finally:
        AP.FlowModeSelectorDialog = orig
    dp = win._authoring.config().dataports[0]
    assert dp.flow_mode == 3 and dp.fcp_offset == 7 and dp.fcp_guard_enable


def test_authoring_arrow_nav():
    win = _win()
    ap = win._authoring
    # all 12 DP columns registered for arrow-key navigation
    assert {c for _, c, _ in ap._nav_list} == set(range(12))
    # Down from DP0's name cell (row 0, col 0) lands on the next col-0 entry below
    name0 = ap._dp_name[0]
    assert ap._nav_move(name0, "v", 1) is True
    foc = ap.focusWidget()
    if foc is not None:
        assert ap._nav_pos[foc] == (1, 0)
    # Left from DP1's name (col 1), cursor at the edge, jumps to col 0
    name1 = ap._dp_name[1]
    name1.setText(""); name1.setCursorPosition(0)
    assert ap._nav_move(name1, "h", -1) is True
