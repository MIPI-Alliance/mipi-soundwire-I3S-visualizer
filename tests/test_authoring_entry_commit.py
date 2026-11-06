"""The Authoring panel commits a typed cell only when it is a valid entry.

A numeric cell holding text outside its range (which the validator leaves in the field
without firing editingFinished) is never written to the model: the cell shows the model's
value again, and a blanked cell commits its row minimum (0 for a data-port cell) and shows
it.

A box is the source of a commit only while it holds text typed in since the last commit or
populate; a populate (Load Settings, Reset) forgets what was typed, so an unrelated commit
never writes a displayed, rounded value over the model's.

The CDS tail dialog offers only the widths the register defines (CdsTailDialog.OPTIONS),
and the data-port identity dialog keeps what was not edited and stores a typed default name
as following the automatic one.
"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication

pytestmark = pytest.mark.gui


def _panel():
    QApplication.instance() or QApplication([])
    from swi3s_studio.ui.authoring.authoring_panel import AuthoringPanel
    return AuthoringPanel()


def test_an_out_of_range_interface_value_is_not_committed():
    panel = _panel()
    cols = panel._cfg.num_columns
    rate = panel._cfg.row_rate_khz
    panel._iface["num_columns"][1].setText("0")           # below 1
    panel._iface["row_rate_khz"][1].setText("99999")      # above 48000
    panel._on_iface_edit()
    assert panel._cfg.num_columns == cols
    assert panel._cfg.row_rate_khz == rate
    assert panel._iface["num_columns"][1].text() == str(cols)     # the field shows the model
    assert panel._iface["row_rate_khz"][1].text() == f"{rate:g}"


def test_an_out_of_range_cell_is_not_committed_when_another_field_is_edited():
    panel = _panel()
    before = panel._cfg.dataports[0].sample_size
    panel._dp_num["sample_size"][0].setText("40")          # above 31
    panel._iface["row_rate_khz"][1].setText("12.5")        # a valid edit elsewhere
    panel._on_iface_edit()
    panel._on_edit()
    assert panel._cfg.dataports[0].sample_size == before
    assert panel._dp_num["sample_size"][0].text() == str(before)
    assert panel._cfg.row_rate_khz == 12.5


def test_a_blank_interface_field_commits_its_minimum():
    panel = _panel()
    panel._iface["num_columns"][1].setText("")
    panel._on_iface_edit()
    assert panel._cfg.num_columns == 1


@pytest.mark.parametrize("attr, minimum, text", [
    ("num_columns", 1, "1"),
    ("skipping_denominator", 1, "1"),
    ("row_rate_khz", 1.0, "1"),
    ("rows_to_draw", 1, "1"),
])
def test_a_blanked_interface_field_shows_the_minimum_it_commits(attr, minimum, text):
    panel = _panel()
    panel._iface[attr][1].setText("")
    panel._on_iface_edit()
    assert getattr(panel._cfg, attr) == minimum
    assert panel._iface[attr][1].text() == text


def test_a_row_rate_the_box_rounds_is_not_rewritten_by_an_unrelated_commit():
    """The box shows 6 significant digits; committing without editing it leaves the
    model's full-precision rate alone."""
    panel = _panel()
    panel._cfg.row_rate_khz = 3072.002
    panel._populate()
    assert panel._iface["row_rate_khz"][1].text() == "3072"
    panel._iface["num_columns"][1].setText("9")
    panel.commit_edits()
    assert panel._cfg.num_columns == 9
    assert panel._cfg.row_rate_khz == 3072.002


def test_typing_the_rounded_row_rate_over_a_finer_one_commits_it():
    from PySide6.QtTest import QTest
    panel = _panel()
    panel._cfg.row_rate_khz = 3072.002
    panel._populate()
    box = panel._iface["row_rate_khz"][1]
    box.selectAll()
    QTest.keyClicks(box, "3072")                      # what the box already shows
    panel._on_iface_edit()
    assert panel._cfg.row_rate_khz == 3072.0
    panel._cfg.row_rate_khz = 3072.002                # a later commit elsewhere leaves it be
    panel._populate()
    panel._on_iface_edit()
    assert panel._cfg.row_rate_khz == 3072.002


def test_a_populate_forgets_what_was_typed_into_the_row_rate_box():
    from PySide6.QtTest import QTest
    panel = _panel()
    box = panel._iface["row_rate_khz"][1]
    box.selectAll()
    QTest.keyClicks(box, "3072")                      # typed, with no commit before the load
    assert panel._iface_typed == {"row_rate_khz"}
    panel._cfg.row_rate_khz = 3072.002
    panel._populate()
    assert panel._iface_typed == set()
    assert box.text() == "3072"
    panel._iface["num_columns"][1].setText("9")       # an unrelated commit
    panel.commit_edits()
    assert panel._cfg.num_columns == 9
    assert panel._cfg.row_rate_khz == 3072.002


@pytest.mark.parametrize("attr", ["sample_size", "skipping_numerator"])
def test_a_blanked_data_port_cell_shows_the_0_it_commits(attr):
    panel = _panel()
    panel._dp_num[attr][3].setText("")
    panel._on_edit()
    assert getattr(panel._cfg.dataports[3], attr) == 0
    assert panel._dp_num[attr][3].text() == "0"


def test_the_cds_tail_dialog_offers_only_the_widths_the_register_defines():
    """CDS_TailWidth is 0, 1 or 2 UI; 3 is Reserved and is not offered."""
    QApplication.instance() or QApplication([])
    from swi3s_studio.ui.authoring.dialogs import CdsTailDialog
    dlg = CdsTailDialog(None, [0] * 13)
    assert [v for v, _label in dlg.OPTIONS] == [0, 1, 2]
    assert all(sorted(row) == [0, 1, 2] for row in dlg._rows)


@pytest.mark.parametrize("edit, want", [
    (None, (40, "")),                     # OK with nothing edited keeps both
    ("name", (40, "Amp")),                # a new name, the number kept
    ("number", (7, "")),                  # a new number; the default name follows it
])
def test_the_identity_dialog_keeps_what_was_not_edited(edit, want):
    """A data port numbered outside 0..31 opens clamped into the spin box; OK keeps the
    number unless the spin box was changed, and keeps a name left as it opened."""
    QApplication.instance() or QApplication([])
    from swi3s_studio.ui.authoring.dialogs import DataPortIdentityDialog
    dlg = DataPortIdentityDialog(None, 40, "", 4)
    assert dlg._num.value() == 31 and dlg._name.text() == "DP40"
    if edit == "name":
        dlg._name.setText("Amp")
    elif edit == "number":
        dlg._num.setValue(7)
    dlg.accept()
    assert (dlg.dp_number, dlg.name) == want


def test_the_identity_dialog_stores_a_typed_default_name_as_auto_following():
    QApplication.instance() or QApplication([])
    from swi3s_studio.ui.authoring.dialogs import DataPortIdentityDialog
    dlg = DataPortIdentityDialog(None, 3, "Amp", 3)
    dlg._num.setValue(5)
    dlg._name.setText("DP5")
    dlg.accept()
    assert (dlg.dp_number, dlg.name) == (5, "")
