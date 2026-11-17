"""The three guard controls of the authoring panel: the data port guard (GuardSelectorDialog),
the FCP guard (FlowModeSelectorDialog) and the per-source CDS guard (CdsGuardDialog, opened
from CdsSettingsDialog). Each is an Enable checkbox beside a Polarity choice (0 / 1):

- the polarity stays shown and editable with Enable off, and is kept across Enable off and
  on;
- OK writes both fields to the config, Cancel writes neither;
- the panel's guard cell shows the port's polarity whether or not the guard is enabled.

exec() blocks headless, so it is replaced by the edits a user makes and an OK or Cancel."""
import os

import pytest

pytestmark = pytest.mark.gui

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from swi3s_studio.ui.authoring import dialogs as D  # noqa: E402
from swi3s_studio.ui.authoring.authoring_panel import AuthoringPanel  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def _app():
    yield QApplication.instance() or QApplication([])


def _selected(btns) -> list:
    return [v for v, b in btns.items() if b.styleSheet() == D._SEL_CSS]


def _drive(monkeypatch, cls, edits, ok: bool):
    """Replace `cls.exec` by `edits(dlg)` then OK (accept) or Cancel (reject)."""
    def _exec(dlg):
        edits(dlg)
        (dlg.accept if ok else dlg.reject)()
        return dlg.result()
    monkeypatch.setattr(cls, "exec", _exec)


# ---- the data port guard ----

def test_the_port_guard_dialog_keeps_its_polarity_across_enable_off_and_on():
    dlg = D.GuardSelectorDialog(None, True, 1, 0)
    assert dlg._enable.isChecked() and _selected(dlg._pol_btns) == [1]
    dlg._enable.setChecked(False)
    assert _selected(dlg._pol_btns) == [1]
    assert all(b.isEnabled() for b in dlg._pol_btns.values()), "polarity not editable"
    dlg._pick(0)
    dlg._enable.setChecked(True)
    assert _selected(dlg._pol_btns) == [0]
    dlg.accept()
    assert (dlg.guard_enabled, dlg.guard_polarity) == (True, 0)


@pytest.mark.parametrize("ok", [True, False])
def test_the_port_guard_dialog_writes_both_fields_on_ok_and_neither_on_cancel(
        monkeypatch, ok):
    panel = AuthoringPanel()
    dp = panel._cfg.dataports[3]
    dp.guard_enable, dp.guard_polarity = True, False

    def edits(dlg):
        dlg._enable.setChecked(False)
        dlg._pick(1)
    _drive(monkeypatch, D.GuardSelectorDialog, edits, ok)
    panel._open_dp_dialog("guard", 3)
    expected = (False, True) if ok else (True, False)
    assert (dp.guard_enable, dp.guard_polarity) == expected
    # The guard cell shows the polarity beside the checkbox, the guard on or off.
    assert panel._dp_check["dialog:guard"][3].isChecked() is expected[0]
    assert panel._dp_guard_pol[3].text() == str(int(expected[1]))
    assert f"polarity {int(expected[1])}" in panel._dp_guard_pol[3].toolTip()


def test_the_panel_s_guard_row_names_the_polarity():
    from swi3s_studio.ui.authoring.authoring_panel import _CHECK_ROWS
    assert ("Guard Enable / Polarity", "dialog:guard") in _CHECK_ROWS


# ---- the FCP guard ----

def _fcp(enable, polarity):
    return D.FlowModeSelectorDialog(None, 2, 0, 4, 0, 0, 0, enable, polarity)


def test_the_fcp_dialog_keeps_its_polarity_across_enable_off_and_on():
    dlg = _fcp(True, 1)
    assert _selected(dlg._gp) == [1]
    dlg._ge.setChecked(False)
    assert _selected(dlg._gp) == [1]
    dlg._ge.setChecked(True)
    assert _selected(dlg._gp) == [1]
    dlg._ge.setChecked(False)
    dlg.accept()
    assert (dlg.fcp_guard_enable, dlg.fcp_guard_polarity) == (False, 1)


@pytest.mark.parametrize("ok", [True, False])
def test_the_fcp_dialog_writes_both_fields_on_ok_and_neither_on_cancel(monkeypatch, ok):
    panel = AuthoringPanel()
    dp = panel._cfg.dataports[2]
    dp.fcp_guard_enable, dp.fcp_guard_polarity = True, False

    def edits(dlg):
        dlg._select(2)
        dlg._ge.setChecked(False)
        dlg._pick_polarity(1)
    _drive(monkeypatch, D.FlowModeSelectorDialog, edits, ok)
    panel._open_dp_dialog("flow", 2)
    expected = (False, True) if ok else (True, False)
    assert (dp.fcp_guard_enable, dp.fcp_guard_polarity) == expected


# ---- the per-source CDS guard ----

def test_the_cds_guard_dialog_keeps_each_polarity_across_enable_off_and_on():
    dlg = D.CdsGuardDialog(None, [True] * 13, [1, 0] * 6 + [1])
    dlg._checks[0].setChecked(False)
    assert _selected(dlg._rows[0]) == [1]
    assert all(b.isEnabled() for b in dlg._rows[0].values()), "polarity not editable"
    dlg._enable_all(False)
    assert [_selected(r) for r in dlg._rows] == [[p] for p in [1, 0] * 6 + [1]]
    dlg._enable_all(True)
    dlg._checks[1].setChecked(False)
    dlg._pick(1, 1)
    dlg.accept()
    assert dlg.enable == [True, False] + [True] * 11
    assert dlg.polarity == [1, 1] + [1, 0] * 5 + [1]


def test_the_cds_guard_dialog_s_all_sources_row_sets_every_polarity():
    dlg = D.CdsGuardDialog(None, [False] * 13, [0] * 13)
    dlg._set_all_polarity(1)
    assert all(not cb.isChecked() for cb in dlg._checks), "a polarity changed an enable"
    dlg.accept()
    assert (dlg.enable, dlg.polarity) == ([False] * 13, [1] * 13)


@pytest.mark.parametrize("inner_ok,outer_ok", [(True, True), (False, True), (True, False)])
def test_the_cds_guard_writes_both_fields_only_through_two_oks(monkeypatch, inner_ok,
                                                              outer_ok):
    panel = AuthoringPanel()
    panel._cfg.cds_guard_enable = [True] * 13
    panel._cfg.cds_guard_polarity = [0] * 13

    def inner(dlg):
        dlg._checks[4].setChecked(False)
        dlg._pick(4, 1)
    _drive(monkeypatch, D.CdsGuardDialog, inner, inner_ok)
    _drive(monkeypatch, D.CdsSettingsDialog, lambda dlg: dlg._open_per_source("guard"),
           outer_ok)
    panel._open_cds_settings_dialog()
    written = inner_ok and outer_ok
    assert panel._cfg.cds_guard_enable[4] is (not written)
    assert panel._cfg.cds_guard_polarity[4] == int(written)
    assert panel._cfg.cds_guard_enable[:4] == [True] * 4
    tip = panel._iface["cds_settings"][1].toolTip()
    assert ("1 device off, polarity 1" in tip) is written, tip


def test_the_cds_guard_summary_names_a_polarity_on_a_disabled_guard():
    assert D.cds_guard_summary([False] * 13, [0] * 13) == "off"
    assert D.cds_guard_summary([True] + [False] * 12, [1] + [0] * 12) == "Manager G1"
    assert D.cds_guard_summary([False] * 13, [1] * 13) == (
        "Manager off, polarity 1; 12 devices off, polarity 1")
    assert D.cds_guard_summary([False, True, True] + [False] * 10, [0, 0, 1, 1] + [0] * 9) == (
        "1 device G0; 1 device G1; 1 device off, polarity 1")
