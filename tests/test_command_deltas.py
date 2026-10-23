"""Commands ΔRows / ΔTime: counted from the row above as the table shows it.

Hidden by default and shown from the header's right-click menu. Because they count from the row
ABOVE AS SHOWN, a filter changes them: filtered to one command kind they read that kind's
spacing. In All Links, ΔRows is blank between two Links' rows (rows are counted per Link),
while ΔTime is on the one shared timeline.
"""
import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from swi3s_studio.ui.command_table import DEFAULT_HIDDEN, DEFAULT_WIDTHS, UNSORTABLE

_DELTAS = ("ΔRows", "ΔTime (µs)")


def _win():
    from swi3s_studio.ui.main_window import MainWindow
    QApplication.instance() or QApplication([])
    w = MainWindow()
    w.load_demo()
    return w


def _col(view, name):
    m = view.model()
    return next(c for c in range(m.columnCount())
                if str(m.headerData(c, Qt.Horizontal)).replace("\n", " ") == name)


def _cells(view, name, n=6):
    m, c = view.model(), _col(view, name)
    return [m.index(r, c).data() for r in range(min(n, m.rowCount()))]


def _num(text):
    return float(str(text).replace(",", ""))


def test_the_delta_columns_are_hidden_until_shown():
    win = _win()
    view = win._cmd_view
    for name in _DELTAS:
        assert view.isColumnHidden(_col(view, name)), name
    view.set_column_shown("ΔRows", True)
    assert not view.isColumnHidden(_col(view, "ΔRows"))
    assert view.isColumnHidden(_col(view, "ΔTime (µs)"))


def test_each_delta_is_from_the_row_above_as_shown():
    win = _win()
    view = win._cmd_view
    rows = [_num(t) for t in _cells(view, "Row")]
    times = [_num(t) for t in _cells(view, "Time (µs)")]
    d_rows, d_time = _cells(view, "ΔRows"), _cells(view, "ΔTime (µs)")
    assert d_rows[0] == "" and d_time[0] == ""                  # nothing above the first
    for i in range(1, len(rows)):
        assert _num(d_rows[i]) == rows[i] - rows[i - 1]
        assert abs(_num(d_time[i]) - (times[i] - times[i - 1])) < 0.02


def _source_rows(view, n):
    m = view.model()
    return [m.mapToSource(m.index(r, 0)).row() for r in range(min(n, m.rowCount()))]


def test_a_filter_changes_what_the_row_above_is():
    """Filtered to Pings, the deltas are the spacing of the Pings, not the gap to the
    writes between them: a shown row whose source row above is hidden reads from the
    shown row above."""
    win = _win()
    view = win._cmd_view
    unfiltered = {}
    m = view.model()
    for r in range(m.rowCount()):
        unfiltered[m.mapToSource(m.index(r, 0)).row()] = m.index(r, _col(view, "ΔRows")).data()
    win._cmd_proxy.set_kinds({"Ping"})
    n = view.model().rowCount()
    rows = [_num(t) for t in _cells(view, "Row", n)]
    times = [_num(t) for t in _cells(view, "Time (µs)", n)]
    src = _source_rows(view, n)
    assert len(rows) >= 3, "the demo pings several times"
    gaps = [i for i in range(1, len(src)) if src[i] - src[i - 1] > 1]
    assert gaps, "the filter should hide rows between two shown Pings"
    d_rows, d_time = _cells(view, "ΔRows", n), _cells(view, "ΔTime (µs)", n)
    for i in range(1, len(rows)):
        assert _num(d_rows[i]) == rows[i] - rows[i - 1], i
        assert abs(_num(d_time[i]) - (times[i] - times[i - 1])) < 0.02, i
    assert any(d_rows[i] != unfiltered[src[i]] for i in gaps), (
        "every filtered delta equals the unfiltered one: they read the row above in "
        "capture order, not the row above as shown")


def test_under_a_descending_time_sort_the_deltas_are_negative():
    win = _win()
    view = win._cmd_view
    view.sortByColumn(_col(view, "Time (µs)"), Qt.DescendingOrder)
    rows = [_num(t) for t in _cells(view, "Row", 8)]
    times = [_num(t) for t in _cells(view, "Time (µs)", 8)]
    d_rows, d_time = _cells(view, "ΔRows", 8), _cells(view, "ΔTime (µs)", 8)
    assert d_rows[0] == "" and d_time[0] == ""
    for i in range(1, len(rows)):
        assert _num(d_rows[i]) == rows[i] - rows[i - 1] < 0, i
        assert abs(_num(d_time[i]) - (times[i] - times[i - 1])) < 0.02, i
        assert _num(d_time[i]) < 0, i


def test_a_delta_cell_on_an_error_row_explains_the_error():
    """An error row's tooltip is the reason it is flagged on every cell, the delta cells
    and the first row's included; a row without an error gets the column's help there."""
    win = _win()
    view = win._cmd_view
    m = view.model()
    for r in (0, 2):                                # rows 0 and 2 fail their CRC
        cmd = m.sourceModel().command_at(m.mapToSource(m.index(r, 0)).row())
        cmd["has_manager_packet"], cmd["crc_valid"] = True, False
    for r in (0, 2):
        want = m.index(r, _col(view, "Opcode")).data(Qt.ToolTipRole)
        assert isinstance(want, str) and want.startswith("⚠"), r
        for name in _DELTAS:
            assert m.index(r, _col(view, name)).data(Qt.ToolTipRole) == want, (r, name)
    for name in _DELTAS:
        help_tip = m.index(1, _col(view, name)).data(Qt.ToolTipRole)
        assert isinstance(help_tip, str) and "row above" in help_tip, name


def _two_links(offset_ps=3_000_000):
    """The demo, and a second decoded demo Link `offset_ps` later (3 µs by default)."""
    from swi3s_studio.session import Session
    win = _win()
    win.load_session(Session.from_demo(300, register_map=win._rmap), add_link=True)
    win.set_link_offset(1, offset_ps)
    win.switch_link(0)
    return win


def test_all_links_counts_time_across_links_but_not_rows():
    win = _two_links()
    win.set_commands_scope_all(True)
    view, m = win._cmd_view, win._cmd_view.model()
    src = m.sourceModel()
    link_col, time_col = 0, _col(view, "Time (µs)")
    crossings = 0
    for r in range(1, m.rowCount()):
        same = m.index(r, link_col).data() == m.index(r - 1, link_col).data()
        d_rows = m.index(r, _col(view, "ΔRows")).data()
        d_time = m.index(r, _col(view, "ΔTime (µs)")).data()
        assert (d_rows != "") == same, (r, d_rows)
        assert d_time != ""
        if not same:
            crossings += 1
            # The global times of the two rows, the second Link's offset included.
            a = m.mapToSource(m.index(r - 1, 0)).row()
            b = m.mapToSource(m.index(r, 0)).row()
            want = src.delta_value(b, "time") - src.delta_value(a, "time")
            assert _num(d_time) == round(want, 2), (r, d_time, want)
            shown = _num(m.index(r, time_col).data()) - _num(m.index(r - 1, time_col).data())
            assert abs(_num(d_time) - shown) < 0.02, (r, d_time, shown)
    assert crossings > 0, "the table should interleave the two Links' rows"
    # Shown or hidden is kept by NAME across the Link column coming and going.
    view.set_column_shown("ΔTime (µs)", True)
    win.set_commands_scope_all(False)
    assert not view.isColumnHidden(_col(view, "ΔTime (µs)"))
    assert view.isColumnHidden(_col(view, "ΔRows"))


def test_all_links_time_delta_includes_the_link_offset():
    """Across two Links' rows, ΔTime is the difference of the rows' local times with each
    Link's offset added: here 0 and 3 µs."""
    win = _two_links(offset_ps=3_000_000)
    win.set_commands_scope_all(True)
    view, m = win._cmd_view, win._cmd_view.model()
    src = m.sourceModel()
    offset_us = [0.0, 3.0]

    def local_plus_offset(r):
        li, row = src.link_at(r), src.source_row_at(r)
        return win.links[li].view.cmd_model.delta_value(row, "time") + offset_us[li]

    checked = 0
    for r in range(1, m.rowCount()):
        a = m.mapToSource(m.index(r - 1, 0)).row()
        b = m.mapToSource(m.index(r, 0)).row()
        if src.link_at(a) == src.link_at(b):
            continue
        d_time = _num(m.index(r, _col(view, "ΔTime (µs)")).data())
        assert d_time == pytest.approx(local_plus_offset(b) - local_plus_offset(a), abs=0.006)
        checked += 1
    assert checked > 0


def test_a_workspace_keeps_them_and_an_older_one_leaves_them_hidden():
    from swi3s_studio.ui import view_state
    win = _win()
    view = win._cmd_view
    view.set_column_shown("ΔRows", True)
    view.set_column_shown("Opcode", False)
    saved = view_state.commands_header_state(view)
    view.set_hidden_names(DEFAULT_HIDDEN)
    view_state.restore_commands_header(view, saved)
    assert not view.isColumnHidden(_col(view, "ΔRows"))
    assert view.isColumnHidden(_col(view, "Opcode"))
    # A workspace saved before the delta columns existed hides Opcode, shows the rest it has,
    # and knows nothing of the deltas: they stay hidden, as they are by default.
    old = {"hidden": ["Opcode"], "sort": None}
    view.set_hidden_names(DEFAULT_HIDDEN)
    view_state.restore_commands_header(view, old)
    assert view.isColumnHidden(_col(view, "Opcode"))
    assert all(view.isColumnHidden(_col(view, n)) for n in _DELTAS)
    assert not view.isColumnHidden(_col(view, "Row")) and not view.isColumnHidden(
        _col(view, "CRC"))


def test_each_delta_column_sits_after_the_column_it_counts():
    win = _win()
    view = win._cmd_view
    for name in _DELTAS:
        view.set_column_shown(name, True)
    assert _col(view, "ΔRows") == _col(view, "Row") + 1
    assert _col(view, "Time (µs)") == _col(view, "ΔRows") + 1
    assert _col(view, "ΔTime (µs)") == _col(view, "Time (µs)") + 1


# ---------------------------------------------------------------- not a sort key
def test_a_delta_header_click_leaves_the_sort_as_it_was():
    from swi3s_studio.ui import view_state
    win = _win()
    view, proxy = win._cmd_view, win._cmd_proxy
    hdr = view.horizontalHeader()
    rows = _cells(view, "Row", 8)
    for name in UNSORTABLE:                          # from capture order
        hdr.setSortIndicator(_col(view, name), Qt.DescendingOrder)     # what a click does
        assert hdr.sortIndicatorSection() == -1 and proxy.sortColumn() == -1, name
        assert _cells(view, "Row", 8) == rows
        assert view_state.commands_header_state(view)["sort"] is None
    time_col = _col(view, "Time (µs)")
    view.sortByColumn(time_col, Qt.DescendingOrder)
    by_time = _cells(view, "Row", 8)
    assert by_time != rows
    for name in UNSORTABLE:                          # from a sort by Time
        view.sortByColumn(_col(view, name), Qt.AscendingOrder)
        assert hdr.sortIndicatorSection() == time_col, name
        assert hdr.sortIndicatorOrder() == Qt.DescendingOrder
        assert proxy.sortColumn() == time_col and _cells(view, "Row", 8) == by_time
        assert view_state.commands_header_state(view)["sort"] == ["Time (µs)", 1]


def test_a_saved_delta_sort_is_not_applied_or_saved():
    from swi3s_studio.ui import view_state
    win = _win()
    view = win._cmd_view
    rows = _cells(view, "Row", 8)
    view_state.restore_commands_header(view, {"hidden": [], "sort": ["ΔTime (µs)", 1]})
    assert view.horizontalHeader().sortIndicatorSection() == -1
    assert _cells(view, "Row", 8) == rows
    assert view_state.commands_header_state(view)["sort"] is None


@pytest.mark.parametrize("saved", [None, ["ΔRows", 0], ["No such column", 0],
                                   ["Opcode", "bad"]])
def test_a_restored_capture_order_clears_the_sort_made_before(saved):
    """A saved view in capture order (or naming a sort the table cannot apply) restores
    capture order over a sort made since, and the header says so."""
    from swi3s_studio.ui import view_state
    win = _win()
    view = win._cmd_view
    rows = _cells(view, "Row", 8)
    view.sortByColumn(_col(view, "Opcode"), Qt.DescendingOrder)
    assert _cells(view, "Row", 8) != rows
    view_state.restore_commands_header(view, {"hidden": [], "sort": saved})
    assert view.horizontalHeader().sortIndicatorSection() == -1
    assert win._cmd_proxy.sortColumn() == -1
    assert _cells(view, "Row", 8) == rows
    assert view_state.commands_header_state(view)["sort"] is None


def test_all_links_delta_header_click_keeps_the_global_time_sort():
    win = _two_links()
    win.set_commands_scope_all(True)
    view = win._cmd_view
    hdr = view.horizontalHeader()
    before = (hdr.sortIndicatorSection(), hdr.sortIndicatorOrder())
    hdr.setSortIndicator(_col(view, "ΔRows"), Qt.DescendingOrder)
    assert (hdr.sortIndicatorSection(), hdr.sortIndicatorOrder()) == before


# ---------------------------------------------------------------- a shown column has a width
def _shown_width(view, name):
    view.set_column_shown(name, True)
    return view.columnWidth(_col(view, name))


def test_a_delta_column_shown_after_a_width_restore_has_a_width():
    win = _win()
    widths = win._command_column_widths()
    assert not set(widths) & set(DEFAULT_HIDDEN)     # a hidden column is not saved at 0
    win._apply_column_widths(widths)
    assert _shown_width(win._cmd_view, "ΔRows") > 0


def test_a_delta_column_shown_after_a_link_switch_or_all_links_has_a_width():
    win = _two_links()
    win.set_group_link("commands", 1)
    win.set_group_link("commands", 0)
    assert _shown_width(win._cmd_view, "ΔRows") > 0
    win.set_commands_scope_all(True)
    win.set_commands_scope_all(False)
    assert _shown_width(win._cmd_view, "ΔTime (µs)") > 0


def test_a_column_shown_at_zero_width_is_fitted():
    win = _win()
    view = win._cmd_view
    c = _col(view, "ΔTime (µs)")
    view.setColumnWidth(c, 0)                        # stored as its width when shown
    view.set_column_shown("ΔTime (µs)", True)
    assert view.columnWidth(c) > 0


def test_the_default_columns_and_widths():
    """Every capture's table shows Time, PhaseID, Opcode, Address, Register, Data and
    Response at DEFAULT_WIDTHS, and hides the rest. A hidden column shown from the header
    menu is fitted to its contents."""
    win = _win()
    view = win._cmd_view
    shown = [str(view.model().headerData(c, Qt.Horizontal)).replace("\n", " ")
             for c in range(view.model().columnCount()) if not view.isColumnHidden(c)]
    assert shown == ["Time (µs)", "PhaseID", "Opcode", "Address", "Register", "Data",
                     "Response"]
    assert DEFAULT_HIDDEN == {"Row", "ΔRows", "ΔTime (µs)", "Devices", "Packet Length",
                              "Group Mask", "CRC"}
    assert {n: view.columnWidth(_col(view, n)) for n in shown} == DEFAULT_WIDTHS == {
        "Time (µs)": 100, "PhaseID": 86, "Opcode": 98, "Address": 96, "Register": 257,
        "Data": 209, "Response": 313}
    for name in sorted(DEFAULT_HIDDEN):
        c = _col(view, name)
        fit = max(view.sizeHintForColumn(c), view.horizontalHeader().sectionSizeHint(c))
        view.set_column_shown(name, True)
        assert view.columnWidth(c) >= fit > 0, name


def test_column_widths_are_kept_by_name():
    from swi3s_studio.ui.command_table import COLUMN_NAMES
    from swi3s_studio.ui.main_window import _widths_by_name
    names = list(COLUMN_NAMES)
    assert _widths_by_name({"Time (µs)": 90, "Row": 0, "CRC": True, "Data": "x"}, names) == \
        {"Time (µs)": 90}
    assert _widths_by_name(list(range(1, len(names) + 1)), names)["Time (µs)"] == 3
    assert _widths_by_name(list(range(1, len(names) - 1)), names) == {}   # another layout
    assert _widths_by_name(None, names) == {} and _widths_by_name("x", names) == {}
    win = _win()
    view = win._cmd_view
    assert win._apply_column_widths({"Time (µs)": 177, "Opcode": 133})
    assert view.columnWidth(_col(view, "Time (µs)")) == 177
    assert view.columnWidth(_col(view, "Opcode")) == 133
    assert not win._apply_column_widths(list(range(1, len(names) - 1)))
    assert view.columnWidth(_col(view, "Time (µs)")) == 177
