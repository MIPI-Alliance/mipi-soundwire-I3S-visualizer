"""The Registers pane filters its rows by where each value came from.

The legend's five entries (Cold Reset, Bus Write, Bus Read, CSV Import, Manual Edit) are the
filter: click one to hide or show the registers whose value it names, so turning Cold Reset
off leaves only the registers something has set. A dual-ranked register's NEXT and CURR rows
are filtered on their own provenance each, and a block left with nothing to show is hidden.
Starting or changing the filter opens the blocks with rows to show (the Peripheral Registers
block excepted); a rebuild while filtering keeps the blocks as the user left them, and
showing every category again gives back the blocks the user had open, with their opens and
closes made while filtering. A filter that leaves nothing says so in words, and the key's
entries are buttons the keyboard and assistive technology can use. The filter outlives the
rebuild every cursor move does, and a workspace keeps it, with the open blocks by id.
"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from swi3s_studio.model import DeviceRegisterFile, Provenance, RegisterMap
from swi3s_studio.model.registers import SLC_BASE
from swi3s_studio.ui.register_view import _BLOCK_ROLE, _PROV_LABEL, _PROV_ROLE, RegisterView
from swi3s_studio.ui.theme import VizTheme

_NUM_COLUMNS = SLC_BASE + 0x81        # dual-ranked: NEXT here, CURR at its alias
_MANUFACTURER_H = SLC_BASE + 0x01     # single rank
_MANUFACTURER_L = SLC_BASE + 0x02
_SLC = "System & Link Control (SLC)"
_CDS = "Control Data Stream Transport (CDS)"


@pytest.fixture
def view():
    QApplication.instance() or QApplication([])
    rmap = RegisterMap.load()
    f = DeviceRegisterFile(rmap, 0)
    f.apply_write(_NUM_COLUMNS, bytes([0x0F]))         # staged in NEXT, no commit: CURR is reset
    f.apply_read(_MANUFACTURER_H, bytes([0x01]))
    f.force_ui(_MANUFACTURER_L, 0x5A)
    v = RegisterView(rmap)
    v.set_files({0: f})
    v.resize(900, 700)
    v.show()
    QApplication.processEvents()
    return v


def _rows(v):
    """{(block, register): provenance} of the rows on show."""
    out = {}
    root = v._tree.invisibleRootItem()
    for i in range(root.childCount()):
        block = root.child(i)
        if block.isHidden():
            continue
        for j in range(block.childCount()):
            row = block.child(j)
            if not row.isHidden():
                out[(block.text(0), row.text(0))] = row.data(0, _PROV_ROLE)
    return out


def _blocks(v):
    root = v._tree.invisibleRootItem()
    return {root.child(i).text(0) for i in range(root.childCount())
            if not root.child(i).isHidden()}


def _block(v, block_id):
    root = v._tree.invisibleRootItem()
    return next(root.child(i) for i in range(root.childCount())
                if root.child(i).data(0, _BLOCK_ROLE) == block_id)


def _open(v):
    root = v._tree.invisibleRootItem()
    return {root.child(i).data(0, _BLOCK_ROLE) for i in range(root.childCount())
            if root.child(i).isExpanded()}


def test_the_legend_is_the_five_categories_and_shows_everything_at_first(view):
    assert list(view._legend_labels) == [Provenance.DEFAULT, Provenance.WRITTEN,
                                         Provenance.READ, Provenance.CSV, Provenance.UI]
    assert [_PROV_LABEL[p] for p in view._legend_labels] == [
        "Cold Reset", "Bus Write", "Bus Read", "CSV Import", "Manual Edit"]
    assert view.hidden_provenances() == set()
    assert {p for p in _rows(view).values()} >= {Provenance.DEFAULT, Provenance.WRITTEN,
                                                 Provenance.READ, Provenance.UI}


def test_turning_cold_reset_off_leaves_only_what_something_set(view):
    view._legend_labels[Provenance.DEFAULT].click()
    rows = _rows(view)
    assert rows and Provenance.DEFAULT not in rows.values()
    assert set(rows) == {(_SLC, "NumColumns (NEXT)"), (_SLC, "ManufacturerID_H"),
                         (_SLC, "ManufacturerID_L")}
    assert _blocks(view) == {_SLC}, "a block with nothing to show was left on screen"
    key = view._legend_labels[Provenance.DEFAULT]
    assert not key.isChecked() and VizTheme.TEXT_DIM in key.styleSheet()
    assert key.toolTip().startswith("Cold Reset: hidden")


def test_the_key_entries_are_keyboard_and_accessible_toggles(view):
    """Tab reaches each entry, Space, Enter and Return toggle it, and it gives assistive
    technology its name and whether it is checked (its category shown)."""
    key = view._legend_labels[Provenance.WRITTEN]
    assert key.focusPolicy() & Qt.TabFocus
    assert key.accessibleName() == "Bus Write" and key.isCheckable() and key.isChecked()
    assert key.toolTip().startswith("Bus Write: shown")
    for press in (Qt.Key_Space, Qt.Key_Return, Qt.Key_Enter):
        before = view.hidden_provenances()
        QTest.keyClick(key, press)
        assert view.hidden_provenances() == before ^ {Provenance.WRITTEN}, press
        assert key.isChecked() == (Provenance.WRITTEN not in view.hidden_provenances())


def test_a_filter_that_leaves_nothing_says_so(view):
    view.set_hidden_provenances(set(Provenance))
    assert not _rows(view) and not view._empty_note.isHidden()
    assert "Every category" in view._empty_note.text()
    view.set_hidden_provenances(set(Provenance) - {Provenance.CSV})
    assert not _rows(view) and not view._empty_note.isHidden()
    assert "CSV Import" in view._empty_note.text()
    view.set_hidden_provenances({Provenance.DEFAULT})
    assert _rows(view) and view._empty_note.isHidden()
    view.set_hidden_provenances(set())
    assert view._empty_note.isHidden()


@pytest.mark.parametrize("keep, register", [
    (Provenance.WRITTEN, "NumColumns (NEXT)"),
    (Provenance.READ, "ManufacturerID_H"),
    (Provenance.UI, "ManufacturerID_L"),
])
def test_each_category_alone(view, keep, register):
    view.set_hidden_provenances(set(Provenance) - {keep})
    assert set(_rows(view)) == {(_SLC, register)}


def test_next_and_curr_are_filtered_each_on_its_own_provenance(view):
    """The write is staged: NEXT is a Bus Write, CURR still the Cold Reset value."""
    view.set_hidden_provenances({Provenance.WRITTEN})
    rows = _rows(view)
    assert (_SLC, "NumColumns (CURR)") in rows and (_SLC, "NumColumns (NEXT)") not in rows


def test_a_csv_compare_is_its_own_category(view):
    view.set_expected([(0, _MANUFACTURER_L + 1, 0x33)])
    view.set_hidden_provenances(set(Provenance) - {Provenance.CSV})
    assert [p for p in _rows(view).values()] == [Provenance.CSV]


def test_the_filter_outlives_a_rebuild(view):
    view.set_hidden_provenances({Provenance.DEFAULT})
    before = _rows(view)
    view.set_files(dict(view._files))                    # what every cursor move does
    assert _rows(view) == before


def test_showing_everything_again_gives_back_the_blocks_the_user_had_open(view):
    mine = _open(view)
    view.set_hidden_provenances({Provenance.DEFAULT})
    assert view.expanded_blocks() == mine, "a workspace saved now would keep the filter's"
    view.set_hidden_provenances(set())
    assert view.expanded_blocks() == mine and _open(view) == mine
    root = view._tree.invisibleRootItem()
    assert all(not root.child(i).isHidden() for i in range(root.childCount()))


def test_a_block_closed_while_filtering_stays_closed_through_a_rebuild(view):
    """Only starting or changing the filter opens blocks; the rebuild every cursor move does
    leaves them as the user has them."""
    view.set_hidden_provenances({Provenance.WRITTEN})
    assert _block(view, "CDS").isExpanded(), "the filter opens a block with rows to show"
    _block(view, "CDS").setExpanded(False)
    view.set_files(dict(view._files))
    assert not _block(view, "CDS").isExpanded()
    view.set_hidden_provenances({Provenance.WRITTEN, Provenance.READ})
    assert _block(view, "CDS").isExpanded(), "a changed filter opens it again"


def test_opens_and_closes_made_while_filtering_are_given_back(view):
    """Expand and collapse while filtered, a rebuild, then every category shown again: the
    user's own changes are what comes back, and are what a workspace would save meanwhile."""
    mine = _open(view)
    assert "SLC" in mine and "CDS" not in mine
    view.set_hidden_provenances({Provenance.WRITTEN})
    _block(view, "SLC").setExpanded(False)              # open before: closed by the user
    _block(view, "CDS").setExpanded(False)
    _block(view, "CDS").setExpanded(True)               # closed before: opened by the user
    view.set_files(dict(view._files))
    want = (mine - {"SLC"}) | {"CDS"}
    assert view.expanded_blocks() == want
    view.set_hidden_provenances(set())
    assert _open(view) == want and view.expanded_blocks() == want


def test_open_blocks_are_kept_by_id_when_a_title_changes(view):
    """A PHY block's title gains " — ACTIVE" or " — inactive" with the decode; it is the
    same block, so it stays open or closed across the change, filtered or not."""
    _block(view, "PHY3").setExpanded(True)
    view.set_phy("PHY2")
    assert _block(view, "PHY3").text(0).endswith("inactive")
    assert _block(view, "PHY3").isExpanded() and "PHY3" in view.expanded_blocks()


def test_the_filter_leaves_the_peripheral_block_closed():
    """It can hold hundreds of registers, so starting a filter does not open it."""
    from swi3s_studio.model.regmap_import import PeripheralRegisterMap, library_dir
    QApplication.instance() or QApplication([])
    path = os.path.join(library_dir(), "example_amp.json")
    with open(path, encoding="utf-8") as fh:
        pmap = PeripheralRegisterMap.from_json(fh.read())
    rmap = RegisterMap.load()
    v = RegisterView(rmap)
    v.set_peripheral_maps({0: pmap})
    v.set_files({0: DeviceRegisterFile(rmap, 0)})
    assert not _block(v, "PERIPH").isExpanded()
    v.set_hidden_provenances({Provenance.WRITTEN})
    assert not _block(v, "PERIPH").isHidden() and not _block(v, "PERIPH").isExpanded()


def test_a_workspace_keeps_the_filter():
    from swi3s_studio.ui import view_state
    QApplication.instance() or QApplication([])
    rmap = RegisterMap.load()
    a, b = RegisterView(rmap), RegisterView(rmap)
    for v in (a, b):
        v.set_files({0: DeviceRegisterFile(rmap, 0)})
    a.set_hidden_provenances({Provenance.DEFAULT, Provenance.CSV})
    state = view_state.registers_state(a)
    assert state["hidden"] == ["csv", "default"]
    view_state.restore_registers(b, state)
    assert b.hidden_provenances() == {Provenance.DEFAULT, Provenance.CSV}
    assert state["expanded"] == sorted(a.expanded_blocks()) and "SLC" in state["expanded"]
    view_state.restore_registers(b, {"device": 0, "expanded": ["CDS", _SLC], "hidden": []})
    assert b.expanded_blocks() == {"CDS", "SLC"}, "an id, or a title as older workspaces kept"
    view_state.restore_registers(b, {"device": 0, "expanded": [], "hidden": ["nonsense"]})
    assert b.hidden_provenances() == set()


def test_a_workspace_keeps_the_column_widths_by_title():
    """The Registers pane's column widths are saved by column title and put back over the
    default widths, through a rebuild too. A workspace without them (or with entries
    that do not read) leaves the columns at the defaults."""
    from swi3s_studio.ui import view_state
    QApplication.instance() or QApplication([])
    rmap = RegisterMap.load()
    a, b = RegisterView(rmap), RegisterView(rmap)
    for v in (a, b):
        v.set_files({0: DeviceRegisterFile(rmap, 0)})
    fitted = b.column_widths()
    assert set(fitted) == {"Register", "Addr", "Value", "Fields"}
    a._tree.setColumnWidth(0, 311)
    a._tree.setColumnWidth(3, 97)
    state = view_state.registers_state(a)
    assert state["col_widths"]["Register"] == 311 and state["col_widths"]["Fields"] == 97
    view_state.restore_registers(b, state)
    assert b.column_widths() == state["col_widths"]
    b.set_phy("PHY2")                                   # a rebuild that re-fits the columns
    assert b.column_widths() == state["col_widths"]
    view_state.restore_registers(b, {"device": 0})
    assert b.column_widths() == fitted
    view_state.restore_registers(b, {"col_widths": {"Register": 0, "Nope": 50, "Addr": "x",
                                                    "Value": 77}})
    assert b.column_widths() == {**fitted, "Value": 77}


def test_the_default_column_widths():
    """With no workspace widths the columns take RegisterView.DEFAULT_WIDTHS."""
    QApplication.instance() or QApplication([])
    rmap = RegisterMap.load()
    v = RegisterView(rmap)
    v.set_files({0: DeviceRegisterFile(rmap, 0)})
    assert v.column_widths() == {"Register": 258, "Addr": 63, "Value": 52, "Fields": 407}
