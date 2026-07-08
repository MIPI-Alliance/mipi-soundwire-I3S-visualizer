"""Error classification + command filter proxy tests.

Run: QT_QPA_PLATFORM=offscreen PYTHONPATH=. python3 tests/test_analysis_filter.py
"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from swi3s_studio.analysis import command_error, is_error, count_errors
from swi3s_studio.ui.command_table import CommandTableModel, CommandFilterProxy
from swi3s_studio.model import RegisterMap
from swi3s_studio import decode_capture
from swi3s_studio.ingest import transitions


def _cmd(**kw):
    base = dict(command="WriteA32", phase="Write", device_mask=0x001,
                has_manager_packet=True, opcode=0, has_address=True, address=0x1081,
                data=b"\x03", crc_valid=True, peripheral_response=-1, manager_response=-1,
                is_commit=False, commit_confirmed=False, has_ping_status=False,
                ping_status=None, has_read_data=False, read_data=b"", read_data_crc_valid=False,
                start_sample=0, end_sample=1)
    base.update(kw)
    return base


def test_error_classification():
    assert command_error(_cmd()) is None
    assert command_error(_cmd(crc_valid=False)) == "CRC error"
    # Write must select exactly one device (cardinality is a hard error).
    assert "expected 1" in command_error(_cmd(device_mask=0x003))
    assert "expected 1" in command_error(_cmd(device_mask=0x000))
    # A non-single-device command with no device selected is an error.
    assert "device mask" in command_error(_cmd(command="Ping", phase="GetStatus", device_mask=0))
    assert command_error(_cmd(command="Ping", phase="GetStatus", device_mask=0xFFF)) is None
    # A commit with no commit-group selected is an error.
    assert "group mask" in command_error(
        _cmd(command="SSCR", phase="Commit", is_commit=True, device_mask=0x001, group_mask=0))
    # Peripheral error-response token.
    assert command_error(_cmd(peripheral_response=13)) == "COMMAND_ERROR"


def test_demo_has_no_errors():
    res = decode_capture(transitions.demo_capture(8))
    assert count_errors(res.commands) == 0


def test_filter_proxy():
    app = QApplication.instance() or QApplication([])
    rmap = RegisterMap.load()
    cmds = [_cmd(command="Ping", phase="GetStatus", device_mask=0xFFF, start_sample=0),
            _cmd(command="WriteA32", start_sample=10),
            _cmd(command="WriteA32", crc_valid=False, start_sample=20),   # error
            _cmd(command="SSCR", phase="Commit", is_commit=True, group_mask=0x1, start_sample=30)]
    model = CommandTableModel(cmds, rmap, 1_000_000)
    proxy = CommandFilterProxy()
    proxy.setSourceModel(model)
    assert proxy.rowCount() == 4

    proxy.set_kinds({"WriteA32"})
    assert proxy.rowCount() == 2
    proxy.set_kinds(set())

    proxy.set_errors_only(True)
    assert proxy.rowCount() == 1
    src = proxy.mapToSource(proxy.index(0, 0))
    assert is_error(model.command_at(src.row()))
    proxy.set_errors_only(False)

    # Multi-select kinds: WriteA32 OR Ping.
    proxy.set_kinds({"WriteA32", "Ping"})
    assert proxy.rowCount() == 3
    proxy.set_kinds(set())

    # Boolean free text: AND requires both terms; OR matches either.
    proxy.set_text("write and ping")
    assert proxy.rowCount() == 0          # no row mentions both
    proxy.set_text("write and bad")
    assert proxy.rowCount() == 1          # the CRC-bad WriteA32 (CRC col = "BAD")
    proxy.set_text("ping or sscr")
    assert proxy.rowCount() == 2
    # Parentheses + precedence: (ping or sscr) and getstatus -> only the Ping.
    proxy.set_text("(ping or sscr) and getstatus")
    assert proxy.rowCount() == 1
    # Without parens, AND binds tighter: ping or (sscr and getstatus) -> just Ping.
    proxy.set_text("ping or sscr and getstatus")
    assert proxy.rowCount() == 1
    proxy.set_text("")

    proxy.set_text("ping")
    assert proxy.rowCount() == 1


def test_cursor_selects_nearest_visible_command_under_filter():
    """Moving the timeline cursor must surface the nearest NON-filtered command at the
    top of the table. When the exact nearest command is filtered out,
    _select_command_for_sample falls back to the nearest visible one (preferring the
    closest at/before the cursor), rather than selecting nothing."""
    QApplication.instance() or QApplication([])
    from swi3s_studio.ui.main_window import MainWindow
    win = MainWindow()
    win.load_demo()
    starts = win._starts
    assert len(starts) >= 4, len(starts)

    # Filter to a single command kind so several source rows are hidden.
    win._cmd_proxy.set_kinds({"WriteA32"})
    try:
        visible_src = {win._cmd_proxy.mapToSource(win._cmd_proxy.index(r, 0)).row()
                       for r in range(win._cmd_proxy.rowCount())}
        assert visible_src and len(visible_src) < len(starts), "need some hidden rows"

        def selected_src_row():
            rows = win._cmd_view.selectionModel().selectedRows()
            assert rows, "nothing selected"
            return win._cmd_proxy.mapToSource(rows[0]).row()

        # Cursor exactly on a HIDDEN command -> selection falls back to the nearest
        # visible command at/before it (never nothing, never the raw hidden row).
        hidden = sorted(set(range(len(starts))) - visible_src)
        for h in hidden:
            win._select_command_for_sample(int(starts[h]))
            sel = selected_src_row()
            assert sel in visible_src, (h, sel)
            assert sel <= h or all(v > h for v in visible_src), (h, sel)

        # Cursor on a VISIBLE command still selects exactly that command.
        for v in sorted(visible_src):
            win._select_command_for_sample(int(starts[v]))
            assert selected_src_row() == v, v
    finally:
        win._cmd_proxy.set_kinds(set())


def test_filter_change_repops_nearest_command():
    """Changing a command filter must re-pop the nearest still-visible command to the
    top of the table at the current cursor — the previously-selected command may now
    be hidden. _on_command_filter_changed re-runs the cursor->command selection."""
    QApplication.instance() or QApplication([])
    from swi3s_studio.ui.main_window import MainWindow
    win = MainWindow()
    win.load_demo()
    starts = win._starts
    assert len(starts) >= 4

    def selected_src_row():
        rows = win._cmd_view.selectionModel().selectedRows()
        return win._cmd_proxy.mapToSource(rows[0]).row() if rows else None

    # Park the cursor on a command a WriteA32 filter will HIDE (pick a non-write).
    hidden = next((i for i, c in enumerate(win._session.commands)
                   if c.get("command") != "WriteA32"), None)
    assert hidden is not None, "demo should have a non-WriteA32 command"
    win.cursor.set_sample(int(starts[hidden]))

    # Apply the filter via the real slot; selection must land on a VISIBLE command.
    win._cmd_proxy.set_kinds({"WriteA32"})
    win._on_command_filter_changed()
    visible_src = {win._cmd_proxy.mapToSource(win._cmd_proxy.index(r, 0)).row()
                   for r in range(win._cmd_proxy.rowCount())}
    sel = selected_src_row()
    assert sel in visible_src, (sel, visible_src)
    win._cmd_proxy.set_kinds(set())


def test_command_row_is_zero_based():
    """The Row column displays 0-based bus rows: a mid-row-start capture anchors
    internally at row_base > 0, so the model subtracts row_origin so the first row
    reads as 0 (Session.row_origin)."""
    from PySide6.QtCore import Qt
    QApplication.instance() or QApplication([])
    rmap = RegisterMap.load()
    cmds = [_cmd(bus_row=1, start_sample=0), _cmd(bus_row=82, start_sample=10)]
    # row_origin = 1 (capture's first complete row is internal row 1).
    model = CommandTableModel(cmds, rmap, 1_000_000, row_origin=1)
    assert model.data(model.index(0, 0), Qt.DisplayRole) == "0"     # 1 - 1
    assert model.data(model.index(1, 0), Qt.DisplayRole) == "81"    # 82 - 1
    # Default origin 0 leaves rows unchanged.
    m0 = CommandTableModel(cmds, rmap, 1_000_000)
    assert m0.data(m0.index(0, 0), Qt.DisplayRole) == "1"


if __name__ == "__main__":
    test_error_classification(); print("ok: error classification (CRC / device-mask / response)")
    test_demo_has_no_errors(); print("ok: demo capture has 0 errors")
    test_filter_proxy(); print("ok: filter proxy (kind / errors-only / text)")
    test_cursor_selects_nearest_visible_command_under_filter(); print("ok: cursor selects nearest visible command under a filter")
    test_filter_change_repops_nearest_command(); print("ok: filter change re-pops nearest command to top")
    test_command_row_is_zero_based(); print("ok: command Row column is 0-based")
    print("ALL ANALYSIS/FILTER TESTS PASSED")
