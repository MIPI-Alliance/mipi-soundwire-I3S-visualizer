"""The Timing page's Export Results (`TimingView.results_rows`, `TimingView.export_results`).

Pinned here:
- the CSV reads back with the csv module: a title row, the inputs as name/value rows, a blank
  row, the table header and a row per inequality per column, 17 x 2, in the tree's order;
- each row's margin is the margin the page shows for that column, its F_MAX the F_max the
  page shows (FAIL, or empty where none is shown), its substitution ends in "=" and the
  margin, and its equation in ">= 0";
- a column an inequality does not apply to keeps its row, reading "does not apply:" and the
  reason, its results empty;
- every cell is ASCII, a subscript after "_" and a Greek letter spelt out, and a text cell
  starting with "-" is quoted;
- the dialog starts in the folder the Timing page last used and remembers the chosen one; a
  write that fails, or a page with no margins, shows a message naming the file;
- the buttons read Save Example Settings, Load Example Settings and Export Results…;
- Copy Summary puts `summary_text` on the clipboard, from its action, the page's context menu
  and the Copy key with the focus in the page, while a focused number box keeps the Copy key.
"""
import csv
import math
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox, QPushButton

from swi3s_studio.timing import INEQUALITIES
from swi3s_studio.ui.timing_view import RESULTS_HEADER

pytestmark = [pytest.mark.gui, pytest.mark.usefixtures("shared_compute")]


def _tv():
    QApplication.instance() or QApplication([])
    from swi3s_studio.ui.timing_view import TimingView
    tv = TimingView()
    tv.flush_recompute()
    return tv


def _export(tv, monkeypatch, path):
    seen = {}

    def dialog(_parent, _title, start, _filters, *_a):
        seen["start"] = start
        return str(path), ""
    monkeypatch.setattr(QFileDialog, "getSaveFileName", staticmethod(dialog))
    tv.export_results()
    return seen


def _read(path):
    with open(path, newline="", encoding="utf-8") as f:
        rows = list(csv.reader(f))
    at = rows.index(list(RESULTS_HEADER))
    inputs = dict(r for r in rows[1:at] if r)
    return rows[0], inputs, rows[at + 1:]


def _page_margins(tv):
    """The margins the page's own summary shows, by (number, column), to the hundredth."""
    from swi3s_studio.ui.timing_view import _INEQ_NUMBER
    left, right = tv._column_labels()
    table = tv.summary_text().split("\n\n")[0].splitlines()[1:]
    out = {}
    for ineq, line in zip(INEQUALITIES, table):
        cells = [c.strip() for c in line.split("|")]
        n = str(_INEQ_NUMBER[ineq])
        assert cells[0].startswith(f"{n})")
        for label, cell in ((left, cells[1]), (right, cells[2])):
            out[(n, label)] = "" if cell == "—" else f"{float(cell):.2f}"
    return out


def test_the_csv_holds_the_inputs_and_a_row_per_inequality_per_column(tmp_path, monkeypatch):
    tv = _tv()
    path = tmp_path / "results.csv"
    _export(tv, monkeypatch, path)
    title, inputs, table = _read(path)
    assert title == ["SWI3S Studio Timing Calculator results"]
    from swi3s_studio.build_id import display_version
    assert inputs == {
        "Manager": "SWI3S PHY2", "Peripheral": "SWI3S PHY2",
        "Manager ramp model": tv._man_shape.currentText(),
        "Peripheral ramp model": tv._per_shape.currentText(),
        "Manager data launch": tv._launch_combo.currentText(),
        "Manager forces keeper": "no", "Handover UI": "yes",
        "Peripheral Placement": "Worst Per Inequality",
        "Clock Frequency Target (MHz)": f"{tv._fclk.value():.3f}",
        "Application version": display_version()}
    assert len(table) == 2 * len(INEQUALITIES) == 34
    assert all(len(r) == len(RESULTS_HEADER) for r in table)
    left, right = tv._column_labels()
    assert [r[2] for r in table] == [left, right] * len(INEQUALITIES)
    assert [r[0] for r in table[::2]] == [str(n) for n in range(1, 18)]
    assert table[0][1] == table[1][1] == "Manager Setup for Peripheral, t_DD"
    assert table[2][1] == "Manager Setup for Peripheral, t_ZD"
    assert table[-1][1] == "Bus Keeper - Peripheral Releasing"        # a lone leg: no launch
    margins = _page_margins(tv)
    for r in table:
        assert r[5] == margins[(r[0], r[2])], r
        assert r[4].endswith(f"= {r[5]}") and r[3].endswith(">= 0"), r
        assert r[7].startswith("Corner: "), r
        # The four P->P setup and hold inequalities and P->P contention carry the trace mismatch
        # over A and B's separation, and no other does.
        assert ("t_PD,mis,AB" in r[3]) is (r[0] in ("9", "10", "11", "12", "15")), r


def test_the_f_max_column_is_the_f_max_the_page_shows(tmp_path, monkeypatch):
    """F_MAX reads the tree's F_max without its unit, FAIL where the leg fails at the lowest
    rates, and empty where the tree shows none (MP hold at the analog launch)."""
    from swi3s_studio.ui.timing_view import _FMAX_ROLE, _SUBST_LABEL_ROLE
    tv = _tv()
    path = tmp_path / "results.csv"
    _export(tv, monkeypatch, path)
    _title, _inputs, table = _read(path)
    shown = []
    tree = tv._tree
    for i in range(tree.topLevelItemCount()):
        head = tree.topLevelItem(i)
        for j in range(head.childCount()):
            item = head.child(j)
            if item.data(0, _SUBST_LABEL_ROLE):
                shown.append(item.data(0, _FMAX_ROLE).removesuffix(" MHz"))
    assert [r[6] for r in table] == shown
    assert "" in shown and any(f not in ("", "FAIL") for f in shown)
    assert table[4][6] == "" and math.isfinite(float(table[0][6]))


def test_a_column_an_inequality_does_not_apply_to_keeps_its_row(tmp_path, monkeypatch):
    """With the Manager forcing its keeper, keeper_Man's rows read "does not apply" and the
    reason in both columns, their results empty; the table still holds 34 rows."""
    tv = _tv()
    tv._keeper_forced_check.setChecked(True)
    tv.flush_recompute()
    path = tmp_path / "results.csv"
    _export(tv, monkeypatch, path)
    _title, inputs, table = _read(path)
    assert inputs["Manager forces keeper"] == "yes"
    assert len(table) == 34
    na = [r for r in table if r[4].startswith("does not apply: ")]
    assert len(na) == 2 and {r[2] for r in na} == set(tv._column_labels())
    assert all(r[3] == r[5] == r[6] == r[7] == "" for r in na)


def test_every_cell_is_ascii_and_a_leading_minus_is_quoted(tmp_path, monkeypatch):
    """On a SoundWire Manager with an EDE Peripheral (the most symbols the page prints) every
    cell is ASCII: no minus sign, middle dot, >= sign or Greek letter survives, the system
    size is listed, and a text cell never starts with -, =, + or @."""
    tv = _tv()
    tv._man_spec_combo.setCurrentIndex(tv._man_spec_combo.findData("SW13_1V2"))
    tv._per_spec_combo.setCurrentIndex(tv._per_spec_combo.findData("SWI3S_PHY2_EDE"))
    tv.flush_recompute()
    rows = tv.results_rows()
    text = "\n".join(",".join(r) for r in rows)
    assert text.isascii() and "?" not in text
    assert "tau" in text and "V_SEOS" in text and "<sub>" not in text
    inputs = dict(r for r in rows[1:rows.index(list(RESULTS_HEADER))] if r)
    assert inputs["SoundWire system size"].startswith("small  (<=60 pF")
    table = rows[rows.index(list(RESULTS_HEADER)) + 1:]
    assert len(table) == 34
    for r in table:
        for k in (1, 2, 3, 4, 7):
            assert r[k][:1] not in ("-", "=", "+", "@"), r[k]
    from swi3s_studio.ui.timing_view import _ascii
    assert _ascii("−X_MP · Man_t<sub>RF</sub> ≥ 0 (τ)") == "-X_MP * Man_t_RF >= 0 (tau)"


def test_the_dialog_starts_in_the_last_timing_folder_and_remembers_the_chosen_one(
        tmp_path, monkeypatch):
    tv = _tv()
    QSettings().setValue("capture/last_dir_timing", str(tmp_path))
    (tmp_path / "out").mkdir()
    seen = _export(tv, monkeypatch, tmp_path / "out" / "r")
    assert seen["start"] == os.path.join(str(tmp_path), "timing_results.csv")
    assert (tmp_path / "out" / "r.csv").is_file()
    assert QSettings().value("capture/last_dir_timing") == str(tmp_path / "out")


def test_a_failed_write_or_a_page_with_no_margins_is_reported(tmp_path, monkeypatch):
    tv = _tv()
    shown = []
    monkeypatch.setattr(QMessageBox, "critical",
                        staticmethod(lambda _p, title, text: shown.append((title, text))))
    monkeypatch.setattr(QMessageBox, "warning",
                        staticmethod(lambda _p, title, text: shown.append((title, text))))
    target = tmp_path / "missing" / "results.csv"
    _export(tv, monkeypatch, target)
    assert not target.exists()
    assert shown and shown[-1][0] == "Export Results" and str(target) in shown[-1][1]
    tv._max[("proposed", "τ (per-edge slew)")].setValue(10000.0)
    tv._max[("proposed", "τ (per-edge slew)")].editingFinished.emit()
    tv.flush_recompute()
    assert tv._compute_error
    _export(tv, monkeypatch, tmp_path / "none.csv")
    assert not (tmp_path / "none.csv").exists()
    assert "was not written" in shown[-1][1]
    with pytest.raises(ValueError):
        tv.results_rows()


def test_the_buttons_read_save_and_load_example_settings_and_export_results():
    """The page's buttons sit at the right of the title's row, on the title's line, and none
    overlaps the title, at 1700 and at 1100 px."""
    from PySide6.QtCore import QPoint, QRect
    tv = _tv()
    buttons = tv._tb_buttons.findChildren(QPushButton)
    assert [b.text() for b in buttons] == ["Reset", "Save Example Settings",
                                           "Load Example Settings", "Export Results…"]
    assert tv.results_export_action().text() == "Export Results…"
    tv.show()
    for width in (1700, 1100):
        tv.resize(width, 800)
        QApplication.processEvents()
        row = tv._title_row

        def rect(w):
            return QRect(w.mapTo(row, QPoint(0, 0)), w.size())
        title = rect(tv._title)
        for b in buttons:
            assert row.rect().contains(rect(b)) and rect(b).left() > title.right(), width
            assert rect(b).top() < title.bottom() and rect(b).bottom() > title.top(), width
        assert rect(buttons[-1]).right() >= row.width() - 2, width
    tv._diagram_max_btn.click()
    QApplication.processEvents()
    assert not tv._title_row.isVisible() and not buttons[0].isVisible()


def test_copy_summary_puts_the_summary_on_the_clipboard():
    """Copy Summary, as the File menu and the context menu trigger it, puts the page's
    plain-text summary on the clipboard; the context menu holds it and Export Results…."""
    from PySide6.QtGui import QGuiApplication
    tv = _tv()
    clip = QGuiApplication.clipboard()
    clip.setText("before")
    act = tv.copy_summary_action()
    assert act.text() == "Copy Summary"
    act.trigger()
    assert clip.text() == tv.summary_text() and clip.text().startswith("Inequality | ")
    assert "\nTerms\n" in clip.text()
    menu = tv.context_menu()
    assert [a.text() for a in menu.actions()] == ["Copy Summary", "Export Results…"]
    clip.setText("before")
    menu.actions()[0].trigger()
    assert clip.text() == tv.summary_text()
    menu.deleteLater()
    tv.close()


def test_the_copy_key_copies_the_summary_in_the_page_and_a_number_box_keeps_it():
    """The Copy key (Cmd+C / Ctrl+C) with the focus on the page's inequalities copies the
    summary; with the focus in a number box holding a selection, it copies that selection."""
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QGuiApplication, QKeySequence
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QDoubleSpinBox
    tv = _tv()
    act = tv.copy_summary_action()
    assert act.shortcut() == QKeySequence(QKeySequence.StandardKey.Copy)
    assert act.shortcutContext() == Qt.WidgetWithChildrenShortcut
    tv.resize(1700, 1050)
    tv.show()
    tv.activateWindow()
    QTest.qWaitForWindowActive(tv)
    clip = QGuiApplication.clipboard()
    tree = tv._tree
    tree.setFocus()
    QApplication.processEvents()
    clip.setText("before")
    QTest.keySequence(tree, QKeySequence(QKeySequence.StandardKey.Copy))
    QTest.keyRelease(tree, Qt.Key_Control)          # QTest keeps a sequence's modifier held
    QApplication.processEvents()
    assert clip.text() == tv.summary_text()
    box = next(b for b in tv.findChildren(QDoubleSpinBox) if b.isVisible() and b.isEnabled())
    box.setFocus()
    box.selectAll()
    QApplication.processEvents()
    clip.setText("before")
    QTest.keySequence(box, QKeySequence(QKeySequence.StandardKey.Copy))
    QTest.keyRelease(box, Qt.Key_Control)
    QApplication.processEvents()
    assert clip.text() == box.lineEdit().selectedText() != tv.summary_text()
    tv.close()


def test_example_settings_dialogs_share_the_last_timing_folder(tmp_path, monkeypatch):
    """Save and Load Example Settings start in the folder the Timing page last wrote or read
    a file in and remember the one they used, which Export Results then starts in."""
    tv = _tv()
    QSettings().setValue("capture/last_dir_timing", str(tmp_path))
    (tmp_path / "ex").mkdir()
    seen = {}

    def save(_parent, _title, start, *_a):
        seen["save"] = start
        return str(tmp_path / "ex" / "example.json"), ""

    def load(_parent, _title, start, *_a):
        seen["load"] = start
        return str(tmp_path / "ex" / "example.json"), ""
    monkeypatch.setattr(QFileDialog, "getSaveFileName", staticmethod(save))
    monkeypatch.setattr(QFileDialog, "getOpenFileName", staticmethod(load))
    tv._save_proposed()
    assert seen["save"] == os.path.join(str(tmp_path), "example.json")
    assert QSettings().value("capture/last_dir_timing") == str(tmp_path / "ex")
    QSettings().setValue("capture/last_dir_timing", str(tmp_path))
    tv._load_proposed()
    assert seen["load"] == str(tmp_path)
    assert QSettings().value("capture/last_dir_timing") == str(tmp_path / "ex")
    assert _export(tv, monkeypatch, tmp_path / "ex" / "r")["start"] == os.path.join(
        str(tmp_path / "ex"), "timing_results.csv")
