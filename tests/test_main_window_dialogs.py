"""MainWindow's dialog handlers: Devices (names, register maps, hub depths), Dataport
Scramblers and Export Capture, each driven through its dialog with the dialog's exec
replaced, on the PHY2 demo. What each applies to the session, what it re-decodes, the
cursor it keeps, the status it reports, and what it does with no capture, on Cancel and on
a failed write. Offscreen Qt."""
import os
import pathlib

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from conftest import pump_loads
from PySide6.QtWidgets import QApplication, QComboBox, QDialog, QMessageBox

pytestmark = pytest.mark.gui

_AMP = pathlib.Path(__file__).resolve().parent.parent / "data" / "regmaps" / "example_amp.json"


def _win(load: bool = True):
    QApplication.instance() or QApplication([])
    from swi3s_studio.ui.main_window import MainWindow
    win = MainWindow()
    if load:
        win.load_demo()
    return win


def _boxes(monkeypatch) -> list:
    """(kind, title, text) of every information / warning / critical box asked for."""
    shown: list = []
    for kind in ("information", "warning", "critical"):
        monkeypatch.setattr(QMessageBox, kind,
                            lambda *a, k=kind, **_kw: shown.append((k, a[1], a[2]))
                            or QMessageBox.Ok)
    return shown


def _no_redecode(win) -> bool:
    return win._load_thread is None and not win._load_pending


# ---- Devices ----------------------------------------------------------------------------

@pytest.mark.parametrize("open_it", ["manage_device_names", "manage_register_maps",
                                     "manage_hub_depths"])
def test_each_devices_item_with_no_capture_asks_for_one(monkeypatch, open_it):
    from swi3s_studio.ui.devices_dialog import DevicesDialog
    shown = _boxes(monkeypatch)
    opened: list = []
    monkeypatch.setattr(DevicesDialog, "exec", lambda dlg: opened.append(dlg) or 0)
    win = _win(load=False)
    getattr(win, open_it)()
    assert shown == [("information", "No session", "Load a capture first.")]
    assert opened == []


@pytest.mark.parametrize("open_it, sections", [
    ("manage_device_names", ("names",)),
    ("manage_register_maps", ("maps",)),
    ("manage_hub_depths", ("depths",)),
])
def test_each_devices_item_opens_the_dialog_on_its_own_section(monkeypatch, open_it, sections):
    from swi3s_studio.ui.devices_dialog import DevicesDialog
    built: list = []
    real_init = DevicesDialog.__init__

    def init(dlg, *a, sections=("names", "maps", "depths"), **kw):
        built.append(tuple(sections))
        real_init(dlg, *a, sections=sections, **kw)

    monkeypatch.setattr(DevicesDialog, "__init__", init)
    monkeypatch.setattr(DevicesDialog, "exec", lambda dlg: QDialog.Rejected)
    win = _win()
    getattr(win, open_it)()
    assert built == [sections]


def test_a_renamed_device_is_named_in_the_session_and_the_registers_pane(monkeypatch):
    """A name typed in Device Names is the session's and the Registers pane's device
    selector's at once, with no re-decode."""
    from swi3s_studio.ui.devices_dialog import DevicesDialog
    win = _win()
    selector = win._reg_view._device
    dev = int(selector.itemData(selector.count() - 1))

    def exec_(dlg):
        dlg._name_edits[dev].setText("MicL")
        dlg.accept()
        return QDialog.Accepted

    monkeypatch.setattr(DevicesDialog, "exec", exec_)
    keep_maps = dict(win._session.peripheral_maps)
    win.manage_device_names()
    assert win._session.device_names.get(dev) == "MicL"
    assert win._session.peripheral_maps == keep_maps
    assert selector.itemText(selector.count() - 1) == f"Device {dev} — MicL"
    assert _no_redecode(win)
    assert win._status.text() == "Devices updated"


def test_a_renamed_device_is_named_in_statistics(monkeypatch):
    """Statistics rows built before a Device Names change are rebuilt with the name."""
    from swi3s_studio.ui.devices_dialog import DevicesDialog
    win = _win()
    selector = win._reg_view._device
    dev = int(selector.itemData(selector.count() - 1))
    win._refresh_measurements()
    assert win._link_state().meas_rows is not None

    def text():
        if win._link_state().meas_rows is None:
            win._refresh_measurements()
        m = win._meas_view
        return "\n".join(m.item(r, c).text() for r in range(m.rowCount()) for c in (0, 1)
                         if m.item(r, c))
    assert "MicL" not in text()

    def exec_(dlg):
        dlg._name_edits[dev].setText("MicL")
        dlg.accept()
        return QDialog.Accepted

    monkeypatch.setattr(DevicesDialog, "exec", exec_)
    win.manage_device_names()
    assert _no_redecode(win)
    assert "MicL" in text()


def test_a_cancelled_devices_dialog_changes_nothing(monkeypatch):
    from swi3s_studio.ui.devices_dialog import DevicesDialog

    def exec_(dlg):
        dlg._name_edits[1].setText("MicL")
        return QDialog.Rejected

    monkeypatch.setattr(DevicesDialog, "exec", exec_)
    win = _win()
    names = dict(win._session.device_names)
    status = win._status.text()
    win.manage_device_names()
    assert win._session.device_names == names
    assert _no_redecode(win) and win._status.text() == status


def test_a_register_map_bound_in_the_dialog_reaches_the_session_and_the_commands_table(
        monkeypatch):
    from swi3s_studio.model.regmap_import import PeripheralRegisterMap
    from swi3s_studio.ui.devices_dialog import DevicesDialog
    pm = PeripheralRegisterMap.from_json(_AMP.read_text(encoding="utf-8"))

    def exec_(dlg):
        dlg.regmaps[3] = pm
        dlg.accept()
        return QDialog.Accepted

    monkeypatch.setattr(DevicesDialog, "exec", exec_)
    win = _win()
    bound: list = []
    real = type(win._cmd_model).set_peripheral_maps
    monkeypatch.setattr(type(win._cmd_model), "set_peripheral_maps",
                        lambda model, maps: bound.append(dict(maps)) or real(model, maps))
    win.manage_register_maps()
    assert win._session.peripheral_maps[3] is pm
    assert bound and bound[-1][3] is pm
    assert _no_redecode(win) and win._status.text() == "Devices updated"


def test_a_changed_hub_depth_re_decodes_and_keeps_the_cursor(monkeypatch):
    """A hub depth the dialog changes re-decodes the capture, off the GUI thread, with the
    cursor where it was; the depth is the session's afterwards."""
    from swi3s_studio.ui.devices_dialog import DevicesDialog

    def exec_(dlg):
        dlg._depth_spins[3].setValue(2)
        dlg.accept()
        return QDialog.Accepted

    monkeypatch.setattr(DevicesDialog, "exec", exec_)
    win = _win()
    win.cursor.set_sample(win._session.commands[5]["start_sample"])
    keep = win.cursor.sample
    win.manage_hub_depths()
    pump_loads(win)
    assert win._session.hub_depths.get(3) == 2
    assert win.cursor.sample == keep
    assert win._status.text() == "Hub depths applied; re-decoded."


def test_an_unchanged_hub_depth_does_not_re_decode(monkeypatch):
    from swi3s_studio.ui.devices_dialog import DevicesDialog

    def exec_(dlg):
        dlg.accept()
        return QDialog.Accepted

    monkeypatch.setattr(DevicesDialog, "exec", exec_)
    win = _win()
    win.manage_hub_depths()
    assert _no_redecode(win) and win._status.text() == "Devices updated"


# ---- Dataport Scramblers ----------------------------------------------------------------

def _scrambler_dialog(monkeypatch, choose):
    """Replace QDialog.exec for the scrambler dialog: `choose(combos)` sets its choices
    and returns the dialog's result. Returns the list the dialogs shown are added to."""
    shown: list = []

    def exec_(dlg):
        shown.append(dlg)
        return choose(dlg.findChildren(QComboBox))

    monkeypatch.setattr(QDialog, "exec", exec_)
    return shown


def test_the_scrambler_dialog_with_no_capture_asks_for_one(monkeypatch):
    shown = _boxes(monkeypatch)
    opened = _scrambler_dialog(monkeypatch, lambda combos: QDialog.Rejected)
    _win(load=False).scrambler_overrides_dialog()
    assert shown == [("information", "No session", "Load a capture first.")]
    assert opened == []


def test_the_scrambler_dialog_with_no_audio_ports_says_so(monkeypatch):
    shown = _boxes(monkeypatch)
    opened = _scrambler_dialog(monkeypatch, lambda combos: QDialog.Rejected)
    win = _win()
    monkeypatch.setattr(win._session, "audio_streams", lambda: [])
    win.scrambler_overrides_dialog()
    assert shown == [("information", "Scrambler",
                      "No decoded audio dataports in this capture.")]
    assert opened == []


def test_the_scrambler_dialog_lists_every_audio_port_on_auto(monkeypatch):
    """One Auto / On / Off choice per decoded audio data port, each on Auto while no
    override is set."""
    seen: list = []

    def choose(combos):
        seen.extend((c.count(), c.currentText()) for c in combos)
        return QDialog.Rejected

    _scrambler_dialog(monkeypatch, choose)
    win = _win()
    streams = win._session.audio_streams()
    assert streams
    win.scrambler_overrides_dialog()
    assert seen == [(3, "Auto")] * len(streams)


def test_a_cancelled_scrambler_dialog_re_decodes_nothing(monkeypatch):
    def choose(combos):
        combos[0].setCurrentIndex(2)
        return QDialog.Rejected

    _scrambler_dialog(monkeypatch, choose)
    win = _win()
    win.scrambler_overrides_dialog()
    assert win._session.scrambler_overrides == {}
    assert _no_redecode(win)


def test_scrambler_overrides_re_decode_and_reopen_as_chosen(monkeypatch):
    """On forces the descrambler on and Off forces it off for that port, re-decoded with the
    cursor kept; the dialog opened again shows each port's override, and Auto removes it."""
    win = _win()
    streams = win._session.audio_streams()
    first = streams[0]
    choices = {"first": 2, "rest": 1}

    def choose(combos):
        combos[0].setCurrentIndex(choices["first"])
        for c in combos[1:]:
            c.setCurrentIndex(choices["rest"])
        return QDialog.Accepted

    _scrambler_dialog(monkeypatch, choose)
    win.cursor.set_sample(win._session.commands[5]["start_sample"])
    keep = win.cursor.sample
    win.scrambler_overrides_dialog()
    pump_loads(win)
    want = {first: False, **{s: True for s in streams[1:]}}
    assert win._session.scrambler_overrides == want
    assert win.cursor.sample == keep
    assert win._status.text() == (f"Scrambler override applied to {len(streams)} "
                                  f"dataport(s); re-decoded.")

    reopened: list = []

    def back_to_auto(combos):
        reopened.extend(c.currentIndex() for c in combos)
        for c in combos:
            c.setCurrentIndex(0)
        return QDialog.Accepted

    _scrambler_dialog(monkeypatch, back_to_auto)
    win.scrambler_overrides_dialog()
    pump_loads(win)
    assert reopened == [2] + [1] * (len(streams) - 1)
    assert win._session.scrambler_overrides == {}
    assert win._status.text() == "Scrambler override applied to 0 dataport(s); re-decoded."


# ---- Export Capture ---------------------------------------------------------------------

def _export_dialog(monkeypatch, path, fmt="csv", rng="all", start=None, end=None):
    """Replace CaptureExportDialog.exec: choose `fmt`, the range `rng` from `start` to `end`
    (each left at the dialog's default when None) and the file `path`, and accept. Returns
    the list each dialog's sample_range() is added to."""
    from swi3s_studio.ui.capture_export_dialog import CaptureExportDialog
    ranges: list = []

    def exec_(dlg):
        dlg._format.setCurrentIndex(next(i for i in range(dlg._format.count())
                                         if dlg._format.itemData(i)[0] == fmt))
        dlg._range.setCurrentIndex(dlg._range.findData(rng))
        if start is not None:
            dlg._start.setValue(start)
        if end is not None:
            dlg._end.setValue(end)
        dlg._path.setText(str(path))
        ranges.append(dlg.sample_range())
        return QDialog.Accepted

    monkeypatch.setattr(CaptureExportDialog, "exec", exec_)
    return ranges


def test_export_capture_with_no_capture_asks_for_one(monkeypatch, tmp_path):
    shown = _boxes(monkeypatch)
    ranges = _export_dialog(monkeypatch, tmp_path / "out.csv")
    _win(load=False).export_capture()
    assert shown == [("information", "No capture", "Open a capture first.")]
    assert ranges == []


def test_a_cancelled_export_capture_writes_nothing(monkeypatch, tmp_path):
    from swi3s_studio.ui.capture_export_dialog import CaptureExportDialog
    monkeypatch.setattr(CaptureExportDialog, "exec", lambda dlg: QDialog.Rejected)
    win = _win()
    status = win._status.text()
    win.export_capture()
    assert list(tmp_path.iterdir()) == [] and win._status.text() == status


@pytest.mark.parametrize("rng, start, end", [
    ("rows", 2, 5),
    ("ui", 100, 4000),
    ("time", 0.0001, 0.0004),
])
def test_export_capture_writes_the_range_the_dialog_picked(monkeypatch, tmp_path, rng, start,
                                                           end):
    """A Bus rows, UI or Time range picked in the dialog is the sample window the file
    holds, as the session's own writer gives it, and the status names that window."""
    from swi3s_studio.ui.capture_export_dialog import CLOCK_CHANNEL, DATA_CHANNEL
    out = tmp_path / "out.csv"
    ranges = _export_dialog(monkeypatch, out, "csv", rng, start, end)
    win = _win()
    sess = win._session
    win.export_capture()
    (picked,) = ranges
    assert picked is not None and 0 <= picked[0] < picked[1] <= sess.last_sample() + 1
    ref = tmp_path / "ref.csv"
    sess.export_csv(str(ref), clock_channel=CLOCK_CHANNEL, data_channel=DATA_CHANNEL,
                    clock_name="SW_CLK", data_name="SW_DATA", sample_range=picked)
    assert out.read_bytes() == ref.read_bytes()
    assert win._status.text() == (f"Capture exported (csv): {out}  "
                                  f"(samples {picked[0]:,}–{picked[1]:,})")
    assert QApplication.overrideCursor() is None


def test_export_capture_of_the_whole_capture_names_no_window(monkeypatch, tmp_path):
    out = tmp_path / "whole.sal"
    _export_dialog(monkeypatch, out, "sal")
    win = _win()
    win.export_capture()
    assert out.stat().st_size > 0
    assert win._status.text() == f"Capture exported (sal): {out}"


def test_a_bin_export_names_each_file_it_wrote(monkeypatch, tmp_path):
    out = tmp_path / "cap.bin"
    _export_dialog(monkeypatch, out, "bin", "rows", 0, 3)
    win = _win()
    win.export_capture()
    written = sorted(p.name for p in tmp_path.iterdir())
    assert len(written) == 2
    status = win._status.text()
    assert status.startswith("Capture exported (bin): ")
    assert all(name in status for name in written)


def test_a_failed_capture_export_is_reported_and_restores_the_cursor_shape(monkeypatch,
                                                                         tmp_path):
    shown = _boxes(monkeypatch)
    _export_dialog(monkeypatch, tmp_path / "out.csv", "csv")
    win = _win()
    status = win._status.text()

    def fail(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(win._session, "export_csv", fail)
    win.export_capture()
    assert shown == [("critical", "Export failed", "disk full")]
    assert QApplication.overrideCursor() is None
    assert win._status.text() == status
