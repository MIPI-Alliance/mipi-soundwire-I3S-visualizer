"""Small pane behaviours, one section each: the view-state restores of every pane (Audio,
Capture, Commands header and filter, Samples, Registers, Statistics, Timing) and what they
report; the Registers pane (Force value, the Compare tooltip, Addr tooltips, column widths);
the timeline ruler (step, labels, clamping, view minimum, bookmark grab, axis colour); the
authoring entries (comma locale, ranges against register widths, the Flow dialog's error
colour); and the smaller helpers of the Commands table, Samples value box, Filter & Gain,
Statistics, CDS table, TSV copy, line styles, register-map import, history summary,
notifications, exit sweep and Open Capture dialog. Offscreen Qt."""
import os
import subprocess
import sys
import textwrap
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QLabel,
    QTableWidget,
    QTableWidgetItem,
)

from swi3s_studio import import_notes
from swi3s_studio.ui import view_state

pytestmark = pytest.mark.gui

_ROOT = Path(__file__).resolve().parent.parent


def _app():
    return QApplication.instance() or QApplication([])


# The parentless widgets the tests build, deleted after each test.
_BUILT_CLASSES = (
    ("swi3s_studio.ui.open_capture_dialog", "OpenCaptureDialog"),
    ("swi3s_studio.ui.regmap_import_dialog", "RegisterMapImportDialog"),
    ("swi3s_studio.ui.register_history_dialog", "RegisterHistoryDialog"),
    ("swi3s_studio.ui.audio_process_dialog", "StreamProcessingDialog"),
    ("swi3s_studio.ui.symbol_view", "SymbolView"),
    ("swi3s_studio.ui.measurements_view", "MeasurementsView"),
    ("swi3s_studio.ui.decoded_sample_view", "DecodedSampleView"),
    ("swi3s_studio.ui.command_table", "CommandTableView"),
    ("swi3s_studio.ui.authoring.notifications", "_Section"))


@pytest.fixture(autouse=True)
def _delete_the_widgets_built():
    """Record each widget of _BUILT_CLASSES a test builds, and delete the parentless ones
    after it. The constructors are patched through a private MonkeyPatch, undone in this
    fixture's own teardown whatever part of the setup ran."""
    import importlib

    from PySide6.QtCore import QCoreApplication, QEvent
    from shiboken6 import isValid
    _app()
    made = []
    patch = pytest.MonkeyPatch()
    try:
        for module, name in _BUILT_CLASSES:
            cls = getattr(importlib.import_module(module), name)
            build = cls.__init__

            def recording_init(self, *args, _build=build, **kwargs):
                _build(self, *args, **kwargs)
                made.append(self)
            patch.setattr(cls, "__init__", recording_init)
        yield
    finally:
        patch.undo()
        for w in made:
            if isValid(w) and w.parent() is None:
                w.close()
                w.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)


# ------------------------------------------------------- tolerant view-state restores
class _Btn:
    def isChecked(self):
        return False

    def setChecked(self, _on):
        pass


class _Splitter:
    def sizes(self):
        return [100, 300]

    def setSizes(self, _s):
        pass


class _AudioStub:
    def __init__(self):
        self._vzoom_btn, self._splitter = _Btn(), _Splitter()
        self.ranges, self.rates = [], []

    def set_channel_selection(self, _c):
        pass

    def set_spectrogram_frames(self, _f):
        pass

    def set_spectrogram_channels(self, _c):
        pass

    def streams(self):
        return [(1, 2)]

    def set_play_rate_target(self, dev, dp, hz):
        self.rates.append((dev, dp, hz))

    def set_x_range_seconds(self, a, b):
        self.ranges.append((a, b))


@pytest.mark.parametrize("entry,field", [
    ({"decimation": 5}, "decimation"),
    ({"decimation": [{"a": 1}]}, "decimation"),
    ({"decimation": [[1, 2, 0]]}, "decimation"),
    ({"x_range_s": {"0": 0}}, "x_range_s"),
    ({"x_range_s": [0, float("inf")]}, "x_range_s"),
    ({"x_range_s": [0, 10 ** 400]}, "x_range_s"),
    ({"x_range_s": [1, 1]}, "x_range_s"),
    ({"channels": [[float("inf"), 1, 1]]}, "channels"),
    ({"channels": [[1, 1, 10 ** 30]]}, "channels"),
    ({"channels": [[1, 1]]}, "channels"),
    ({"spectrogram": [[1, 1, float("inf")]]}, "spectrogram"),
    ({"spectrogram_frames": [[1, 1, 1, float("inf")]]}, "spectrogram_frames"),
    ({"decimation": [[1, 2, float("inf")]]}, "decimation"),
    ({"decimation": [[1, 2, 10 ** 30]]}, "decimation"),
    ({"channel_list_width": float("inf")}, "channel_list_width"),
    ({"channel_list_width": 10 ** 30}, "channel_list_width"),
    ({"channel_list_width": True}, "channel_list_width"),
    ({"channel_list_width": "wide"}, "channel_list_width"),
    ({"channels": [[0, "1", 2]]}, "channels"),
    ({"channels": [[0, 1, 2.7]]}, "channels"),
    ({"channels": [[True, 1, 2]]}, "channels"),
    ({"spectrogram": [[0, "1", 2]]}, "spectrogram"),
    ({"spectrogram": [[0, 1.5, 2]]}, "spectrogram"),
    ({"decimation": [[1, 2, "48000"]]}, "decimation"),
    ({"decimation": [[1, 2, 48000.5]]}, "decimation"),
    ({"decimation": [[1, False, 48000]]}, "decimation"),
    ({"x_range_s": ["1", "2"]}, "x_range_s"),
    ({"x_range_s": [True, 2]}, "x_range_s"),
    ({"x_range_s": [0.0, 1e300]}, "x_range_s"),
    ({"x_range_s": [0, 1, 2]}, "x_range_s"),
    ({"vertical_zoom": "false"}, "vertical_zoom"),
    ({"vertical_zoom": 1}, "vertical_zoom"),
])
def test_a_malformed_audio_entry_is_reported_and_nothing_raises(entry, field):
    view = _AudioStub()
    with import_notes.collect() as notes:
        view_state.restore_audio(view, entry, source="ws.json")
    assert len(notes) == 1 and field in notes[0].message, [n.message for n in notes]
    assert view.ranges == [] and view.rates == []


def test_an_audio_range_and_rate_that_read_are_applied_without_a_note():
    view = _AudioStub()
    with import_notes.collect() as notes:
        view_state.restore_audio(view, {"x_range_s": [0, 1.5], "decimation": [[1, 2, 48000]],
                                        "channel_list_width": view_state.MAX_PX}, source="w")
    assert notes == [] and view.ranges == [(0.0, 1.5)] and view.rates == [(1, 2, 48000)]


@pytest.mark.parametrize("entry", [{"ports": [[float("inf"), 1]]}, {"ports": [["1", 2]]},
                                   {"ports": [[1, 2.5]]}, {"ports": [[True, 2]]},
                                   {"channels": ["3"]}, {"channels": [1.5]}])
def test_samples_ports_and_channels_that_are_not_whole_numbers_are_reported_once(entry):
    _app()
    view = _SamplesStub()
    with import_notes.collect() as notes:
        view_state.restore_samples(view, entry, source="w")
    assert len(notes) == 1 and "ports or channels" in notes[0].message


def test_a_samples_entry_with_bad_ports_still_applies_its_op_and_value():
    _app()
    view = _SamplesStub()
    with import_notes.collect() as notes:
        view_state.restore_samples(view, {"ports": [["1", 2]], "op": ">", "value": "0x10"},
                                   source="w")
    assert view._op_cb.currentText() == ">" and view._val_edit.text() == "0x10"
    assert view.filtersChanged.emits == 1
    assert [n.message for n in notes] == [
        "the saved Samples view has entries that do not read, left as the pane has them: "
        "ports or channels"]


class _ToggleBtn:
    def __init__(self):
        self.on = False

    def isEnabled(self):
        return True

    def isChecked(self):
        return self.on

    def setChecked(self, on):
        self.on = on


class _CaptureStub:
    def __init__(self):
        self._cds_btn, self._samp_btn, self._legend_btn = _ToggleBtn(), _ToggleBtn(), _ToggleBtn()
        self.ranges = []

    def set_x_range_seconds(self, a, b):
        self.ranges.append((a, b))


@pytest.mark.parametrize("entry,field", [({"samples": "false"}, "samples"),
                                         ({"rsp_markers": 1}, "rsp_markers"),
                                         ({"x_range_s": ["0", "1"]}, "x_range_s")])
def test_a_capture_toggle_or_range_that_does_not_read_is_reported_and_left(entry, field):
    view = _CaptureStub()
    with import_notes.collect() as notes:
        view_state.restore_capture(view, entry, source="w")
    assert len(notes) == 1 and field in notes[0].message
    assert not view._samp_btn.on and not view._cds_btn.on and view.ranges == []


def test_capture_toggles_that_read_are_applied_without_a_note():
    view = _CaptureStub()
    with import_notes.collect() as notes:
        view_state.restore_capture(view, {"samples": True, "rsp_markers": False,
                                          "x_range_s": [0, 2]}, source="w")
    assert notes == [] and view._samp_btn.on and view.ranges == [(0.0, 2.0)]


def test_a_huge_range_is_not_applied_to_a_loaded_audio_pane():
    """A range whose sample numbers overflow is left out, not passed on to the plot."""
    _app()
    from swi3s_studio.ui.main_window import MainWindow
    win = MainWindow()
    win.load_demo()
    audio = win._audio_view
    before = audio.x_range_seconds()
    audio.set_x_range_seconds(0.0, 1e300)
    assert audio.x_range_seconds() == before


class _RegView:
    def __init__(self):
        self._device = type("D", (), {"findData": lambda s, d: -1})()
        self.hidden = None

    def hidden_provenances(self):
        return set(self.hidden or ())

    def set_hidden_provenances(self, v):
        self.hidden = list(v)

    def set_expanded_blocks(self, _b):
        pass

    def column_titles(self):
        return []

    def set_column_widths(self, _w):
        pass


def test_a_registers_filter_with_an_unhashable_entry_is_reported_and_nothing_raises():
    view = _RegView()
    with import_notes.collect() as notes:
        view_state.restore_registers(view, {"hidden": [["x"], 3]}, source="ws.json")
    assert view.hidden == [] and any("filter" in n.message for n in notes)


@pytest.mark.parametrize("expanded,kept", [([[1]], []), ([{}], []), ([1, "SLC"], ["SLC"]),
                                           ("SLC", None), ({"a": 1}, None)])
def test_registers_open_blocks_that_are_not_text_are_reported_and_only_text_is_kept(
        expanded, kept):
    view = _RegView()
    opened = []
    view.set_expanded_blocks = opened.append
    with import_notes.collect() as notes:
        view_state.restore_registers(view, {"expanded": expanded}, source="ws.json")
    assert len(notes) == 1
    assert opened == ([] if kept is None else [kept])


def test_a_registers_view_with_several_bad_entries_makes_one_note():
    view = _RegView()
    bad = {"device": 10 ** 30, "hidden": ["nope", 5], "expanded": [[1]],
           "col_widths": {"Name": 10 ** 30}}
    with import_notes.collect() as notes:
        view_state.restore_registers(view, bad, source="ws.json")
    assert len(notes) == 1
    for part in ("device", "filter", "open block", "width of"):
        assert part in notes[0].message, notes[0].message


@pytest.mark.parametrize("width", [10 ** 30, 1.5, 0, -4, True, "9", view_state.MAX_PX + 1])
def test_a_registers_column_width_outside_the_range_is_reported(width):
    class Titled(_RegView):
        def column_titles(self):
            return ["Name"]
    view = Titled()
    with import_notes.collect() as notes:
        view_state.restore_registers(view, {"col_widths": {"Name": width}}, source="w")
    assert len(notes) == 1 and "width of 'Name'" in notes[0].message


def test_a_whole_valued_fraction_reads_as_a_registers_width_and_device():
    _app()
    from swi3s_studio.model import DeviceRegisterFile, RegisterMap
    from swi3s_studio.ui.register_view import RegisterView
    rmap = RegisterMap.load()
    view = RegisterView(rmap)
    try:
        view.set_files({0: DeviceRegisterFile(rmap, 0), 1: DeviceRegisterFile(rmap, 1)})
        title = view.column_titles()[0]
        with import_notes.collect() as notes:
            view_state.restore_registers(view, {"col_widths": {title: 120.0}, "device": 1.0},
                                         source="w")
        assert notes == []
        assert view.column_widths()[title] == 120 and view.current_device() == 1
    finally:
        view.deleteLater()


@pytest.mark.parametrize("hidden", [None, "x", 5])
def test_a_registers_filter_missing_or_malformed_leaves_the_panes_filter(hidden):
    _app()
    from swi3s_studio.model import DeviceRegisterFile, Provenance, RegisterMap
    from swi3s_studio.ui.register_view import RegisterView
    rmap = RegisterMap.load()
    view = RegisterView(rmap)
    try:
        view.set_files({0: DeviceRegisterFile(rmap, 0)})
        view.set_hidden_provenances({Provenance.DEFAULT})
        d = {"device": 0, "expanded": ["SLC"]}
        if hidden is not None:
            d["hidden"] = hidden
        with import_notes.collect() as notes:
            view_state.restore_registers(view, d, source="w")
        assert view.hidden_provenances() == {Provenance.DEFAULT}
        assert view.expanded_blocks() == {"SLC"}
        assert len(notes) == (0 if hidden is None else 1)
    finally:
        view.deleteLater()


class _StatsView:
    _collapsed_titles: set = set()
    _expanded_groups: set = set()

    def _apply_folds(self):
        pass


@pytest.mark.parametrize("entry", [{"collapsed": "Header"}, {"expanded": 5}])
def test_statistics_folds_that_are_not_a_list_are_reported(entry):
    with import_notes.collect() as notes:
        view_state.restore_statistics(_StatsView(), entry, source="w")
    assert len(notes) == 1 and "Statistics" in notes[0].message


class _TimingView:
    """The Timing pane's controls restore_timing reads and sets; `regions` are the region
    names its picker holds."""

    class _Region:
        def __init__(self, regions):
            self._names, self.current = list(regions), -1

        def findText(self, text):
            return self._names.index(text) if text in self._names else -1

        def currentIndex(self):
            return self.current

        def setCurrentIndex(self, i):
            self.current = i

    def __init__(self, regions=()):
        self._region = self._Region(regions)
        self._filter_items = []
        self._cat_hidden = set()

    def _update_legend(self):
        pass

    def _redraw(self):
        pass

    def _on_filter_changed(self, redraw=True):
        pass


@pytest.mark.parametrize("entry", [{"filter": 5}, {"filter": [1]}, {"hidden": 7},
                                   {"hidden": [[1, 2, 3]]}, {"hidden": [5]},
                                   {"region": "No such region"}])
def test_timing_pane_entries_that_do_not_read_are_reported(entry):
    with import_notes.collect() as notes:
        view_state.restore_timing(_TimingView(["Whole capture"]), entry, source="w")
    assert len(notes) == 1 and "Timing" in notes[0].message


def test_a_timing_pane_that_reads_makes_no_note():
    view = _TimingView(["Whole capture", "Region 2"])
    with import_notes.collect() as notes:
        view_state.restore_timing(view, {"filter": ["a"], "hidden": [[True, False]],
                                         "region": "Region 2"}, source="w")
    assert notes == [] and view._cat_hidden == {(True, False)}
    assert view._region.current == 1


def test_a_saved_timing_region_the_pane_lacks_is_named_in_the_note():
    with import_notes.collect() as notes:
        view_state.restore_timing(_TimingView(["Whole capture"]), {"region": "Gone"},
                                  source="w")
    assert [n.message for n in notes] == [
        "the saved Timing view has entries that do not read, left as the pane has them: "
        "region 'Gone'"]


@pytest.mark.parametrize("value,whole", [(5, 5), (5.0, 5), (10 ** 30, 10 ** 30), (1.5, None),
                                         (float("inf"), None), (float("nan"), None),
                                         (True, None), ("5", None), (None, None)])
def test_whole_number_reads_a_json_number_with_no_fraction(value, whole):
    assert view_state.whole_number(value) == whole


def test_bounded_number_and_valid_width_include_both_ends():
    assert view_state.bounded_number(0, 0, 3) == 0 and view_state.bounded_number(3, 0, 3) == 3
    assert view_state.bounded_number(-1, 0, 3) is None
    assert view_state.bounded_number(4, 0, 3) is None
    assert view_state.valid_width(1) and view_state.valid_width(view_state.MAX_PX)
    assert not view_state.valid_width(0) and not view_state.valid_width(view_state.MAX_PX + 1)


@pytest.mark.parametrize("entry, bad", [({"errors_only": "false"}, "errors_only"),
                                        ({"kinds_none": 1}, "kinds_none"),
                                        ({"text": 5}, "text")])
def test_a_saved_commands_filter_reads_on_off_only_as_true_or_false(entry, bad):
    """An on/off entry that is not a JSON true or false is off, a text that is not text is
    empty, and each is reported; the rest of the filter reads."""
    with import_notes.collect() as notes:
        snap = view_state.filters_from_json({"kinds": ["Ping"], **entry}, source="w")
    assert snap["kinds"] == {"Ping"}
    assert (snap["errors_only"], snap["kinds_none"], snap["text"]) == (False, False, "")
    assert [n.message for n in notes] == [
        f"the saved Commands filter view has entries that do not read, left as the pane has "
        f"them: {bad}"]
    with import_notes.collect() as notes:
        snap = view_state.filters_from_json({"errors_only": True, "text": "Ping"}, source="w")
    assert notes == [] and snap["errors_only"] and snap["text"] == "Ping"


@pytest.mark.parametrize("order", [float("inf"), 1.5, "1", True, 2, -1])
def test_a_commands_sort_order_that_is_not_0_or_1_is_reported_in_capture_order(order):
    _app()
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QStandardItemModel

    from swi3s_studio.ui.command_table import CommandTableView
    table = CommandTableView()
    model = QStandardItemModel(3, 2)
    model.setHorizontalHeaderLabels(["Row", "Name"])
    table.setModel(model)
    table.sortByColumn(1, Qt.DescendingOrder)
    with import_notes.collect() as notes:
        view_state.restore_commands_header(table, {"sort": ["Name", order]}, source="w")
    assert [n.message for n in notes] == [
        f"the saved Commands sort order {order!r} is not 0 or 1; the rows are in capture "
        f"order"]
    assert table.horizontalHeader().sortIndicatorSection() == -1


def test_a_commands_sort_that_reads_is_applied():
    _app()
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QStandardItemModel

    from swi3s_studio.ui.command_table import CommandTableView
    table = CommandTableView()
    model = QStandardItemModel(3, 2)
    model.setHorizontalHeaderLabels(["Row", "Name"])
    table.setModel(model)
    with import_notes.collect() as notes:
        view_state.restore_commands_header(table, {"sort": ["Name", 1]}, source="w")
    head = table.horizontalHeader()
    assert notes == [] and head.sortIndicatorSection() == 1
    assert head.sortIndicatorOrder() == Qt.DescendingOrder


@pytest.mark.parametrize("bad, field", [({"hidden": [1, ["x"]]}, "hidden"),
                                        ({"columns": [1]}, "columns"),
                                        ({"columns": "x"}, "columns"),
                                        ({"hidden": [1], "columns": [2]}, "hidden, columns")])
def test_a_commands_header_with_non_string_entries_is_reported_and_nothing_raises(bad, field):
    _app()
    from swi3s_studio.ui.command_table import CommandTableView
    table = CommandTableView()
    with import_notes.collect() as notes:
        view_state.restore_commands_header(table, bad, source="ws.json")
    assert [n.message for n in notes] == [
        f"the saved Commands view has entries that do not read, applied where they read, "
        f"the rest ignored: {field}"]


def test_a_commands_header_note_says_which_entries_were_left_and_which_applied():
    """A 'hidden' that is not a list is left as the pane has it; a 'columns' with an entry
    that is not text is applied where it reads. One note says both."""
    _app()
    from PySide6.QtGui import QStandardItemModel

    from swi3s_studio.ui.command_table import CommandTableView
    table = CommandTableView()
    model = QStandardItemModel(3, 2)
    model.setHorizontalHeaderLabels(["Row", "Name"])
    table.setModel(model)
    table.set_hidden_names({"Row"})
    with import_notes.collect() as notes:
        view_state.restore_commands_header(table, {"hidden": "Name", "columns": ["Row", 5]},
                                           source="ws.json")
    assert table.hidden_names() == {"Row"}
    assert [n.message for n in notes] == [
        "the saved Commands view has entries that do not read, left as the pane has them: "
        "hidden; applied where they read, the rest ignored: columns"]


def test_the_text_entries_of_a_partly_bad_hidden_list_are_applied():
    _app()
    from PySide6.QtGui import QStandardItemModel

    from swi3s_studio.ui.command_table import CommandTableView
    table = CommandTableView()
    model = QStandardItemModel(3, 2)
    model.setHorizontalHeaderLabels(["Row", "Name"])
    table.setModel(model)
    with import_notes.collect() as notes:
        view_state.restore_commands_header(table, {"hidden": ["Name", 1]}, source="ws.json")
    assert len(notes) == 1 and notes[0].message.endswith(": hidden")
    assert table.hidden_names() == {"Name"} and table.isColumnHidden(1)


@pytest.mark.parametrize("hidden", ["Row", 5, {"Row": True}])
def test_a_commands_hidden_entry_that_is_not_a_list_leaves_the_columns_as_they_are(hidden):
    _app()
    from PySide6.QtGui import QStandardItemModel

    from swi3s_studio.ui.command_table import CommandTableView
    table = CommandTableView()
    model = QStandardItemModel(3, 3)
    model.setHorizontalHeaderLabels(["Row", "Name", "Data"])
    table.setModel(model)
    table.set_hidden_names({"Row"})
    with import_notes.collect() as notes:
        view_state.restore_commands_header(
            table, {"hidden": hidden, "columns": ["Row", "Name", "Data"]}, source="ws.json")
    assert table.hidden_names() == {"Row"}
    assert [n.message for n in notes] == [
        "the saved Commands view has entries that do not read, left as the pane has them: "
        "hidden"]


class _SamplesStub:
    """The Samples pane's filter controls restore_samples reads and sets."""

    class _Box:
        def __init__(self):
            self._menu = type("M", (), {"actions": lambda s: []})()

        def _update_text(self):
            pass

    class _Signal:
        def __init__(self):
            self.emits = 0

        def emit(self):
            self.emits += 1

    def __init__(self):
        from PySide6.QtWidgets import QComboBox, QLineEdit
        self._port_cb, self._chan_cb = self._Box(), self._Box()
        self._op_cb = QComboBox()
        self._op_cb.addItems(["=", ">"])
        self._val_edit = QLineEdit("7")
        self.filtersChanged = self._Signal()

    def check_value(self):
        pass


@pytest.mark.parametrize("entry, field", [({"value": None}, "value"), ({"value": 5}, "value"),
                                          ({"value": ["x"]}, "value"),
                                          ({"op": 5}, "op 5"), ({"op": ">="}, "op '>='")])
def test_a_samples_value_or_op_that_is_not_text_is_reported_and_the_pane_keeps_its_own(
        entry, field):
    _app()
    view = _SamplesStub()
    with import_notes.collect() as notes:
        view_state.restore_samples(view, entry, source="w")
    assert view._val_edit.text() == "7" and view._op_cb.currentText() == "="
    assert [n.message for n in notes] == [
        f"the saved Samples view has entries that do not read, left as the pane has them: "
        f"{field}"]


def test_a_samples_value_and_op_that_read_are_applied_in_one_pass():
    _app()
    view = _SamplesStub()
    with import_notes.collect() as notes:
        view_state.restore_samples(view, {"op": ">", "value": "0x10"}, source="w")
    assert notes == [] and view._val_edit.text() == "0x10" and view._op_cb.currentText() == ">"
    assert view.filtersChanged.emits == 1


@pytest.mark.parametrize("key", ["kinds", "devices", "groups"])
@pytest.mark.parametrize("value", ["Read", {"Read": 1}, 3])
def test_a_commands_filter_set_that_is_not_a_list_is_empty_and_reported(key, value):
    with import_notes.collect() as notes:
        snap = view_state.filters_from_json({key: value}, source="w")
    assert snap[key] == set()
    assert [n.message for n in notes] == [
        f"the saved Commands filter view has entries that do not read, left as the pane has "
        f"them: {key}"]


def test_statistics_folds_keep_their_text_entries_and_report_the_rest():
    view = _StatsView()
    with import_notes.collect() as notes:
        view_state.restore_statistics(view, {"collapsed": ["Regions", 5, None]}, source="w")
    assert view._collapsed_titles == {"Regions"}
    assert len(notes) == 1 and notes[0].message.endswith(": collapsed")


def test_commands_column_widths_outside_the_range_are_dropped_and_reported():
    from swi3s_studio.ui.main_window import _widths_by_name
    said = []
    got = _widths_by_name({"A": 10 ** 30, "B": 40, "C": 1.5, "D": view_state.MAX_PX}, ["A", "B"],
                          said.append)
    assert got == {"B": 40, "D": view_state.MAX_PX}
    assert said == [f"column widths of 'A', 'C' are not widths of 1..{view_state.MAX_PX} "
                    f"pixels"]
    said.clear()
    _widths_by_name({"A": 0, "B": 40}, ["A", "B"], said.append)
    assert said == [f"column width of 'A' is not a width of 1..{view_state.MAX_PX} pixels"]
    said.clear()
    assert _widths_by_name([1], ["A", "B"], said.append) == {}
    assert said == ["column widths are not a mapping, or a list with one width per column"]
    assert _widths_by_name(None, ["A"], said.append) == {} and len(said) == 1


# ------------------------------------------------- command table helpers and the value box
def test_a_device_list_names_only_the_devices_the_mask_holds():
    from swi3s_studio.ui.command_table import _collapse_ranges, _device_list
    assert _collapse_ranges([1, 3]) == "1,3"
    assert _device_list(0b1010) == "1,3"
    assert _device_list(1 << 12) == "—"                  # bit 12 is no device (0..11)
    assert _device_list((1 << 12) | (1 << 11)) == "11"


def test_a_payload_is_the_returned_bytes_only_for_a_read_that_has_them():
    from swi3s_studio.analysis.responses import command_payload as _payload
    assert _payload({"has_read_data": True, "read_data": b"\x01", "data": b"\x02"}) == b"\x01"
    assert _payload({"has_read_data": False, "read_data": b"\x01", "data": b"\x02"}) == b"\x02"
    assert _payload({}) == b""


def test_the_device_and_group_filters_pass_a_command_matching_any_one_selected():
    """Two devices selected pass a command addressing either; two commit groups pass a
    commit in either. A command addressing neither, and a non-commit under a group filter,
    are hidden."""
    _app()
    from test_analysis_filter import _cmd

    from swi3s_studio.model import RegisterMap
    from swi3s_studio.ui.command_table import CommandFilterProxy, CommandTableModel
    cmds = [_cmd(device_mask=0b0001, start_sample=0),               # device 0 only
            _cmd(device_mask=0b0010, start_sample=10),              # device 1 only
            _cmd(device_mask=0b0100, start_sample=20),              # device 2 only
            _cmd(command="SSCR", phase="Commit", is_commit=True, group_mask=0b01,
                 start_sample=30),
            _cmd(command="SSCR", phase="Commit", is_commit=True, group_mask=0b10,
                 start_sample=40),
            _cmd(command="SSCR", phase="Commit", is_commit=True, group_mask=0b100,
                 start_sample=50)]
    proxy = CommandFilterProxy()
    proxy.setSourceModel(CommandTableModel(cmds, RegisterMap.load(), 1_000_000))

    def shown():
        return [proxy.mapToSource(proxy.index(r, 0)).row() for r in range(proxy.rowCount())]
    proxy.set_devices({0, 1})
    assert shown() == [0, 1, 3, 4, 5]                    # the commits address device 0
    proxy.set_devices(set())
    proxy.set_groups({0, 1})
    assert shown() == [3, 4]


def test_a_parenthesised_term_after_a_term_is_an_implicit_and():
    from swi3s_studio.ui.command_table import eval_filter, parse_filter
    node = parse_filter("write (data)")
    assert node[0] == "and"
    assert eval_filter(node, "write data") and not eval_filter(node, "write x")
    assert not eval_filter(node, "read data")


@pytest.mark.parametrize("text,ok", [("12", True), ("0012", True), ("0x1F", True), ("0b101", True),
                                     ("0o17", True), ("-3", True), ("1_000", False),
                                     ("0x_ff", False), ("\u0661\u0662", False), ("1.5", False),
                                     ("abc", False), ("", False)])
def test_the_value_filter_reads_ascii_whole_numbers_only(text, ok):
    from swi3s_studio.ui.decoded_sample_view import _parse_whole
    if ok:
        assert isinstance(_parse_whole(text), int)
    else:
        with pytest.raises(ValueError):
            _parse_whole(text)


def test_the_value_box_is_marked_when_it_does_not_read_and_filters_has_no_side_effect():
    _app()
    from swi3s_studio.ui.decoded_sample_view import DecodedSampleView
    from swi3s_studio.ui.theme import VizTheme
    view = DecodedSampleView()
    view._op_cb.setCurrentIndex(1)                       # an operator other than the blank one
    view._val_edit.setText("1_000")
    assert view._val_edit.toolTip() and VizTheme.SEM_ERROR in view._val_edit.styleSheet()
    view._val_edit.setStyleSheet("")
    view._val_edit.setToolTip("")
    assert view.filters()[2] is None
    assert view._val_edit.styleSheet() == "" and view._val_edit.toolTip() == ""
    view._val_edit.setText("1000")
    assert not view._val_edit.toolTip() and view.filters()[2] is not None
    assert view.check_value() is True


def test_the_open_capture_dialog_shows_a_file_name_as_text():
    _app()
    from test_open_capture_dialog import _probe

    from swi3s_studio.ui.open_capture_dialog import OpenCaptureDialog
    pr = _probe()
    pr.paths = ["/x/run<i>&x.sal"]
    dlg = OpenCaptureDialog(pr, [])
    text = next(lab.text() for lab in dlg.findChildren(QLabel) if "channels" in lab.text())
    assert "run&lt;i&gt;&amp;x.sal" in text and "<i>" not in text


# ------------------------------------------------------------- Filter & Gain opening gain
@pytest.mark.parametrize("peak", [-3.16, -0.06, -3.14, -6.02])
def test_the_normalising_gain_never_pushes_the_peak_above_zero(peak):
    _app()
    from test_stream_processing import _store, _tone

    from swi3s_studio.ui.audio_process_dialog import StreamProcessingDialog
    store = _store([_tone(dc=0.0, amp=1000.0)])
    store.peak_db = lambda *a, **k: peak
    dlg = StreamProcessingDialog(store, 1, 2, preview=lambda _p: None)
    assert not dlg.clips() and dlg._apply_btn.isEnabled()
    assert dlg._gain.value() == pytest.approx(-peak, abs=0.1)
    assert dlg._gain.value() + peak <= 1e-6


# --------------------------------------------------------------------- Statistics refill
def test_a_refill_does_not_keep_the_old_layouts_hidden_rows():
    _app()
    from swi3s_studio.ui.measurements_view import MeasurementsView
    v = MeasurementsView()
    v.set_rows([("Sec", "", "header"), ("Ping", "1", "group"),
                ("d1", "x", "detail"), ("d2", "y", "detail")])
    assert v.isRowHidden(2) and v.isRowHidden(3)
    v.set_rows([("Sec", "", "header"), ("Ping", "1", "group"),
                ("Sec2", "", "header"), ("plain", "x")])
    assert [v.isRowHidden(r) for r in range(4)] == [False] * 4


# ---------------------------------------------------------------------- ragged TSV copy
def test_a_ragged_cell_selection_copies_aligned_columns():
    _app()
    from swi3s_studio.ui.copyable import copy_selection
    t = QTableWidget(3, 3)
    for r in range(3):
        for c in range(3):
            t.setItem(r, c, QTableWidgetItem(f"{r}{c}"))
    t.setSelectionMode(QAbstractItemView.ExtendedSelection)
    t.setSelectionBehavior(QAbstractItemView.SelectItems)
    t.selectRow(0)
    t.item(1, 2).setSelected(True)
    try:
        copy_selection(t)
        assert QGuiApplication.clipboard().text() == "00\t01\t02\n\t\t12"
    finally:
        t.deleteLater()


# ---------------------------------------------------------------- CDS running disparity
def _sym(raw):
    return {"raw": raw, "kind": 3, "value": 0, "start_sample": 0, "row": 0}


def test_a_windows_first_double_disparity_symbol_is_not_flagged():
    _app()
    from swi3s_studio.ui.symbol_view import SymbolView
    v = SymbolView()
    v.set_symbols([_sym(0b0001110100), _sym(0b1101110100)])        # 4 ones, then 6 ones
    assert [v.item(r, 4).text() for r in range(2)] == ["-1", "+1"]
    v.set_symbols([_sym(0b0001110100), _sym(0b0001110100)])        # two -2: the second is real
    assert [v.item(r, 4).text() for r in range(2)] == ["-1", "-1!"]
    v.set_symbols([_sym(0b0101010101), _sym(0b0001110100)])        # neutral first: RD unknown
    assert [v.item(r, 4).text() for r in range(2)] == ["?", "-1"]   # the next is not flagged
    v.set_symbols([_sym(0b0101010101), _sym(0b1101110100)])
    assert [v.item(r, 4).text() for r in range(2)] == ["?", "+1"]


# ------------------------------------------------------------- stored line-style prefs
def test_a_malformed_colours_entry_keeps_the_stored_weights():
    from swi3s_studio.ui import line_style
    kind = next(iter(line_style.WEIGHT_KINDS))
    for bad in ([1], 5, "x"):
        got = line_style._clean({"colors": bad, "weights": {kind: line_style.MIN_WEIGHT}})
        assert got["weights"] == {kind: line_style.MIN_WEIGHT} and got["colors"] == {}
        got = line_style._clean({"weights": bad})
        assert got["weights"] == {}


# ------------------------------------------------------------ register-map import dialog
def test_a_json_map_summary_counts_fields_not_bits():
    from swi3s_studio.model.regmap_import import PeripheralRegisterMap
    from swi3s_studio.ui.regmap_import_dialog import _report_for_loaded
    pmap = PeripheralRegisterMap.from_json(
        (_ROOT / "data" / "regmaps" / "example_amp.json").read_text("utf-8"))
    fields = sum(len(r.fields) for r in pmap.registers)
    bits = sum(f.hi - f.lo + 1 for r in pmap.registers for f in r.fields)
    assert 0 < fields < bits, "every field one bit wide: fields and bits cannot be told apart"
    rep = _report_for_loaded(pmap)
    assert rep.raw_unit == "fields" and rep.raw_bit_count == rep.field_count == fields


def test_a_file_that_is_not_utf8_is_imported_with_a_warning(tmp_path):
    _app()
    from swi3s_studio.ui.regmap_import_dialog import RegisterMapImportDialog
    path = tmp_path / "map.json"
    path.write_bytes(b'{"name": "m\xb5", "registers": []}')
    dlg = RegisterMapImportDialog()
    dlg._path = str(path)
    dlg._reimport()
    assert dlg.report is not None, dlg._summary.text()
    assert any("not UTF-8" in w for w in dlg.report.warnings)


# ------------------------------------------------------------- register-history summary
def test_the_history_summary_shows_a_link_name_as_text():
    _app()
    from swi3s_studio.ui.register_history_dialog import RegisterHistoryDialog
    dlg = RegisterHistoryDialog("Amp <L> & R", [], 1e6, lambda r: r)
    text = dlg._summary.text()
    assert "Amp &lt;L&gt; &amp; R" in text and "<L>" not in text and "& R" not in text


# ------------------------------------------------------------------ notification list
def test_replacing_notification_items_leaves_only_the_new_labels():
    _app()
    from swi3s_studio.ui.authoring.notifications import _Section
    sec = _Section("Issues", "#ff0000")
    sec.set_items([("old 1", []), ("old 2", [])])
    sec.set_items([("new", [])])
    assert [lab.text() for lab in sec.findChildren(QLabel)] == ["new"]


def test_the_no_issues_line_takes_each_themes_green():
    _app()
    from swi3s_studio.ui import theme
    from swi3s_studio.ui.authoring.notifications import NotificationsPanel
    from swi3s_studio.ui.theme import VizTheme
    panel = NotificationsPanel()
    try:
        theme.apply_palette("light")
        panel.retheme()
        light = VizTheme.OK_GREEN
        assert panel._ok.styleSheet() == f"color:{light};"
        theme.apply_palette("dark")
        panel.retheme()
        assert panel._ok.styleSheet() == f"color:{VizTheme.OK_GREEN};" != f"color:{light};"
    finally:
        theme.apply_palette("dark")
        panel.deleteLater()


# ------------------------------------------------------------------- timeline axis colour
def test_the_axis_labels_are_dark_on_the_light_themes_white_plot(monkeypatch):
    """Each ruler label is drawn with a pen dark against the light theme's plot."""
    _app()
    from PySide6.QtGui import QColor, QPainter

    from swi3s_studio.ui import theme
    from swi3s_studio.ui.theme import VizTheme
    from swi3s_studio.ui.timeline import TimelineRibbon
    drawn = []
    real = QPainter.drawText

    def record(painter, *args):
        drawn.append((painter.pen().color(), str(args[-1])))
        return real(painter, *args)
    monkeypatch.setattr(QPainter, "drawText", record)
    was, tl = VizTheme.MODE, None
    try:
        theme.apply_palette("light")
        tl = TimelineRibbon()
        tl.resize(800, tl.height())
        tl.set_sample_rate(1e6)
        tl.set_events([], 1_000_000)
        tl.set_view(0, 1_000_000)
        tl.retheme()
        tl.grab()
        labels = [(c, t) for c, t in drawn if t.endswith((" ms", " s"))]
        assert labels, drawn
        background = QColor(VizTheme.PLOT_BG).lightness()
        assert background > 200
        assert all(c.lightness() < 120 for c, _t in labels), labels
    finally:
        theme.apply_palette(was)
        if tl is not None:
            tl.deleteLater()


# ------------------------------------------------------------------ timeline ruler
def _ribbon(rate=1e6, total=2_000_000, width=1016):
    _app()
    from swi3s_studio.ui.timeline import TimelineRibbon
    tl = TimelineRibbon()
    tl.resize(width, tl.height())
    tl.set_sample_rate(rate)
    tl.set_events([], total)
    tl.set_view(0, total)
    return tl


@pytest.mark.parametrize("x, step", [(1, 1), (2, 2), (3, 5), (5, 5), (6, 10), (10, 10),
                                     (20, 20), (21, 50), (99, 100), (0.3, 0.5), (0, 0.0),
                                     (-4, 0.0)])
def test_the_ruler_step_is_the_next_1_2_or_5_times_a_power_of_10(x, step):
    from swi3s_studio.ui.timeline import TimelineRibbon
    assert TimelineRibbon._nice(x) == pytest.approx(step)


@pytest.mark.parametrize("rate, total, sample, step_s, text", [
    (1e6, 2_000_000, 1_500_000, 0.5, "1.5 s"),
    (1e6, 1_000_000, 500_000, 0.5, "0.5 s"),              # exactly 1 s long reads in s
    (1e6, 999_999, 250_000, 0.25, "250 ms"),
    (1e6, 1_000, 500, 1e-4, "0.5 ms"),                    # exactly 1 ms long reads in ms
    (1e6, 5_000, 2_500, 5e-4, "2.5 ms"),
    (1e6, 10_000, 5_000, 1e-3, "5 ms"),
    (1e6, 999, 500, 1e-4, "500 µs"),                      # under 1 ms reads in µs
    (1e9, 1_000, 500, 1e-7, "0.5 µs"),                    # exactly 1 µs long reads in µs
    (1e9, 999, 500, 1e-7, "500 ns"),
    (1e12, 999, 500, 1e-10, "0.5 ns"),
    (1e6, 2_000_000, 1_000_000, 1e-12, "1.000000 s"),     # at most 6 decimals
    (0.0, 1234, 1234, 1.0, "1,234")])
def test_a_ruler_label_takes_the_captures_unit_and_the_steps_decimals(rate, total, sample,
                                                                       step_s, text):
    tl = _ribbon(rate=rate, total=total)
    try:
        assert tl._fmt_axis(sample, step_s) == text
    finally:
        tl.deleteLater()


def test_the_view_slice_takes_both_margin_edges_and_nothing_past_them():
    """The view plus a 5 % margin each side, both ends inclusive."""
    import numpy as np
    tl = _ribbon(total=4000)
    try:
        tl.set_view(1000, 2000)
        lo, hi = tl.view_range
        m = (hi - lo) * 0.05
        pts = np.array([lo - m - 1, lo - m, lo, hi, hi + m, hi + m + 1], dtype=np.float64)
        i0, i1, xs = tl._view_slice(pts)
        assert (i0, i1) == (1, 5) and len(xs) == 4
    finally:
        tl.deleteLater()


def test_an_off_view_pixel_is_clamped_to_just_past_the_widget():
    tl = _ribbon()
    try:
        w = float(tl.width())
        assert tl._clampx(1e12) == w + 2.0 and tl._clampx(-1e12) == -2.0
        assert tl._clampx(10.5) == 10.5
        assert tl._sample_for(-1e6) == 0 and tl._sample_for(1e6) == 2_000_000
    finally:
        tl.deleteLater()


def test_the_view_keeps_its_16_sample_minimum_at_either_end_and_a_domain_its_width():
    tl = _ribbon(total=1000)
    try:
        tl.set_view(990, 1000)
        assert tl.view_range == (984.0, 1000.0)
        tl.set_view(0, 10)
        assert tl.view_range == (0.0, 16.0)
        tl.set_domain(50, 50)
        assert tl._bounds() == (50.0, 51.0)
    finally:
        tl.deleteLater()


def test_a_bookmark_line_is_grabbed_up_to_its_pixel_tolerance():
    tl = _ribbon(total=1000, width=1016)
    try:
        tl.set_bookmarks([(500, "A1")])
        x = tl._x_for(500)
        assert x == int(x), "the bookmark is not on a whole pixel: the bound is not exact"
        assert tl._bookmark_at(x + 6.0) == "A1" and tl._bookmark_at(x - 6.0) == "A1"
        assert tl._bookmark_at(x + 6.5) is None
    finally:
        tl.deleteLater()


# ------------------------------------------------------------------- exit sweep threads
@pytest.mark.platform
def test_the_exit_sweep_leaves_a_tree_holding_a_running_thread_alone():
    body = textwrap.dedent("""
        import os
        os.environ["QT_QPA_PLATFORM"] = "offscreen"
        from PySide6.QtCore import QThread
        from PySide6.QtWidgets import QApplication, QWidget
        app = QApplication([])
        from swi3s_studio.ui.teardown import release_python_owned_qobjects
        root = QWidget()
        th = QThread(root)
        th.start()
        left = release_python_owned_qobjects()
        print("LEFT", left, th.isRunning(), flush=True)
        th.quit(); th.wait()
        """)
    path = os.pathsep.join(p for p in (str(_ROOT), os.environ.get("PYTHONPATH", "")) if p)
    proc = subprocess.run([sys.executable, "-c", body], cwd=_ROOT, capture_output=True,
                          text=True, timeout=120, env=dict(os.environ, PYTHONPATH=path))
    line = [ln for ln in proc.stdout.splitlines() if ln.startswith("LEFT")]
    assert line, proc.stderr[-2000:]
    assert line[-1].split()[2] == "True" and int(line[-1].split()[1]) >= 1


# ------------------------------------------------- authoring entries in a comma locale
def test_the_authoring_entries_read_a_dot_and_no_separator_in_a_comma_locale():
    """With a default locale whose decimal mark is a comma, the Interface entries and the
    Flow Control dialog's entries accept '1.5' where a fraction is allowed and refuse a
    comma decimal mark and a group separator."""
    _app()
    from PySide6.QtCore import QLocale
    from PySide6.QtGui import QValidator

    from swi3s_studio.ui.authoring.authoring_panel import AuthoringPanel
    from swi3s_studio.ui.authoring.dialogs import FlowModeSelectorDialog
    before = QLocale()
    QLocale.setDefault(QLocale(QLocale.German, QLocale.Germany))
    panel = flow = None
    try:
        panel = AuthoringPanel()
        flow = FlowModeSelectorDialog(None, 2, 0, fcp_h_start=4, fcp_offset=7)
        rate = panel._iface["row_rate_khz"][1].validator()
        rows = panel._iface["rows_to_draw"][1].validator()
        offset = flow._off.validator()
        ok = QValidator.State.Acceptable
        assert rate.validate("1.5", 3)[0] == ok and rows.validate("1000", 4)[0] == ok
        for v, text in ((rate, "1,5"), (rate, "1.000,5"), (rows, "1.000"), (rows, "1,000"),
                        (offset, "1.000")):
            assert v.validate(text, len(text))[0] != ok, text
    finally:
        QLocale.setDefault(before)
        for w in (panel, flow):
            if w is not None:
                w.deleteLater()


# ------------------------------------------- authoring ranges fit their register fields
# The register field (data/registers.json) each authoring row with a register sets; the
# rows without one (no register holds S0 width, the S1 tail, the row rate or the rows to
# draw) are listed so a new row has to be classified here.
_AUTHORING_FIELDS = {
    "device_number": "DeviceNumber", "sample_size": "SampleSize",
    "sample_grouping": "SampleGrouping", "channel_grouping": "ChannelGrouping",
    "spacing": "Spacing", "interval": "Interval", "offset": "Offset",
    "horizontal_start": "HorizontalStart", "horizontal_count": "HorizontalCount",
    "tail_width": "TailWidth", "bit_width": "BitWidth",
    "skipping_numerator": "SkippingNumerator", "num_columns": "NumColumns",
    "skipping_denominator": "SkippingDenominator"}
_AUTHORING_NO_FIELD = {"num_channels", "s0_width", "s1_tail_width", "row_rate_khz",
                       "rows_to_draw"}


def _register_field_widths() -> dict:
    """{field name: width in bits} from data/registers.json, a field split over several
    registers ("Interval[3:0]", "Interval[11:4]") summed into one."""
    import json
    doc = json.loads((_ROOT / "data" / "registers.json").read_text(encoding="utf-8"))
    widths: dict = {}
    for block in doc["blocks"]:
        seen = set()
        for reg in block["registers"]:
            for f in reg["fields"]:
                name = f["name"].split("[")[0]
                hi, _, lo = f["bits"].strip("[]").partition(":")
                part = int(hi) - int(lo or hi) + 1
                if name in widths and name not in seen:
                    continue                      # the first block naming it holds it
                seen.add(name)
                widths[name] = widths.get(name, 0) + part
    return widths


def test_every_authoring_range_fits_the_register_field_it_sets():
    from swi3s_studio.ui.authoring.authoring_panel import _IFACE_ROWS, _NUMERIC_ROWS
    widths = _register_field_widths()
    rows = [(attr, lo, hi) for _label, attr, lo, hi in _NUMERIC_ROWS]
    rows += [(attr, lo, hi) for _label, attr, kind, lo, hi, _p in _IFACE_ROWS
             if kind in ("int", "float")]
    assert {a for a, _lo, _hi in rows} == set(_AUTHORING_FIELDS) | _AUTHORING_NO_FIELD
    for attr, lo, hi in rows:
        if attr in _AUTHORING_NO_FIELD:
            continue
        bits = widths[_AUTHORING_FIELDS[attr]]
        assert 0 <= lo <= hi <= (1 << bits) - 1, (attr, lo, hi, bits)
    assert dict(((a, (lo, hi)) for a, lo, hi in rows))["spacing"] == (0, 15)
    assert dict(((a, (lo, hi)) for a, lo, hi in rows))["skipping_denominator"] == (1, 4095)


def test_the_flow_dialogs_bad_entry_is_marked_in_the_themes_error_colour():
    _app()
    from swi3s_studio.ui.authoring.dialogs import FlowModeSelectorDialog
    from swi3s_studio.ui.theme import VizTheme
    flow = FlowModeSelectorDialog(None, 2, 0)
    try:
        flow._off.setText("5000")                    # set, not typed: past the validator
        flow.accept()
        assert flow.result() == 0
        assert flow._off.styleSheet() == f"border: 1px solid {VizTheme.SEM_ERROR};"
    finally:
        flow.deleteLater()


# ------------------------------------------------------------- Registers Force value
def _register_rows(view) -> dict:
    """{address: row} of the editable main rows (the _NEXT or single-ranked ones)."""
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QTreeWidgetItemIterator
    rows = {}
    it = QTreeWidgetItemIterator(view._tree)
    while it.value():
        key = it.value().data(0, Qt.UserRole)
        if key and not it.value().text(0).endswith("(CURR)"):
            rows.setdefault(int(key[1]), it.value())
        it += 1
    return rows


@pytest.mark.parametrize("text, want", [("08", 8), ("010", 10), ("255", 255), ("0", 0),
                                        ("0x2A", 0x2A), ("0o17", 0o17), ("0b101", 5),
                                        (" 7 ", 7), ("0x1_0", None), ("1_0", None),
                                        ("٣", None), ("256", None), ("-1", None)])
def test_a_register_byte_reads_decimal_with_leading_zeros_and_prefixed_bases(text, want):
    from swi3s_studio.ui.register_view import _register_byte
    assert _register_byte(text) == want


def test_a_refused_force_value_says_what_reads_and_names_the_prefix(monkeypatch):
    """A bare hex byte is refused, and the refusal and the prompt name the 0x, 0o and 0b
    prefixes the parser reads rather than saying the register cannot hold the value."""
    _app()
    from PySide6.QtWidgets import QInputDialog, QMessageBox

    from swi3s_studio.model import DeviceRegisterFile, RegisterMap
    from swi3s_studio.ui.register_view import RegisterView
    rmap = RegisterMap.load()
    view = RegisterView(rmap)
    try:
        view.set_files({0: DeviceRegisterFile(rmap, 0)})
        item = next(iter(_register_rows(view).values()))
        answers, told, emitted, asked = [("FF", True), ("0xFF", True)], [], [], []
        monkeypatch.setattr(QInputDialog, "getText",
                            lambda *a, **k: asked.append(a[2]) or answers.pop(0))
        monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: told.append(a[2]))
        view.registerEdited.connect(lambda d, a, v: emitted.append(v))
        view._prompt_edit(item)
        assert emitted == [0xFF]
        assert len(told) == 1
        assert "not a value this register holds" not in told[0]
        assert "“FF”" in told[0]
        for text in (told[0], asked[0]):
            assert all(prefix in text for prefix in ("0x", "0o", "0b")), text
    finally:
        view.deleteLater()


def test_force_value_survives_a_rebuild_while_its_prompt_is_open(monkeypatch):
    """A cursor tick may rebuild the register tree, deleting the row, while the refusal or
    the next prompt is open: the prompt goes on and the value is emitted."""
    _app()
    from PySide6.QtWidgets import QInputDialog, QMessageBox

    from swi3s_studio.model import DeviceRegisterFile, RegisterMap
    from swi3s_studio.ui.register_view import RegisterView
    rmap = RegisterMap.load()
    view = RegisterView(rmap)
    try:
        files = {0: DeviceRegisterFile(rmap, 0)}
        view.set_files(files)
        item = next(iter(_register_rows(view).values()))
        name = item.text(0)
        answers = [("999", True), ("0x2A", True)]
        prompts, emitted = [], []

        def ask(*a, **k):
            prompts.append(a[2])
            return answers.pop(0)
        monkeypatch.setattr(QInputDialog, "getText", ask)
        monkeypatch.setattr(QMessageBox, "warning",
                            lambda *a, **k: (view._tree.clear(), view.set_files(files)))
        view.registerEdited.connect(lambda d, a, v: emitted.append(v))
        view._prompt_edit(item)
        assert emitted == [0x2A]
        assert len(prompts) == 2 and all(p.startswith(name) for p in prompts)
    finally:
        view.deleteLater()


@pytest.mark.parametrize("typed", ["256", "0x1FF", "-1", "abc", "1.5", "", "0x1_0"])
def test_force_value_refuses_what_is_not_a_byte_and_asks_again(monkeypatch, typed):
    _app()
    from PySide6.QtWidgets import QInputDialog, QMessageBox

    from swi3s_studio.model import DeviceRegisterFile, RegisterMap
    from swi3s_studio.ui.register_view import RegisterView
    rmap = RegisterMap.load()
    view = RegisterView(rmap)
    try:
        view.set_files({0: DeviceRegisterFile(rmap, 0)})
        item = next(iter(_register_rows(view).values()))
        answers = [(typed, True), ("0x2A", True)]
        prompts, told, emitted = [], [], []
        monkeypatch.setattr(QInputDialog, "getText",
                            lambda *a, **k: prompts.append(k.get("text")) or answers.pop(0))
        monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: told.append(a[2]))
        view.registerEdited.connect(lambda d, a, v: emitted.append(v))
        view._prompt_edit(item)
        assert emitted == [0x2A], "an unreadable or out-of-range value was emitted"
        assert len(told) == 1 and "the register holds 0..255" in told[0]
        assert prompts[1] == typed, "the prompt did not open again with the text typed"
    finally:
        view.deleteLater()


def test_force_value_cancelled_after_a_refusal_emits_nothing(monkeypatch):
    _app()
    from PySide6.QtWidgets import QInputDialog, QMessageBox

    from swi3s_studio.model import DeviceRegisterFile, RegisterMap
    from swi3s_studio.ui.register_view import RegisterView
    rmap = RegisterMap.load()
    view = RegisterView(rmap)
    try:
        view.set_files({0: DeviceRegisterFile(rmap, 0)})
        item = next(iter(_register_rows(view).values()))
        answers = [("300", True), ("300", False)]
        emitted = []
        monkeypatch.setattr(QInputDialog, "getText", lambda *a, **k: answers.pop(0))
        monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: None)
        view.registerEdited.connect(lambda d, a, v: emitted.append(v))
        view._prompt_edit(item)
        assert emitted == [] and answers == []
    finally:
        view.deleteLater()


def test_every_registers_address_cell_tips_its_full_address():
    """The Addr column elides an 8-digit address or a filler range; its tooltip gives it."""
    _app()
    from PySide6.QtWidgets import QTreeWidgetItemIterator

    from swi3s_studio.model import DeviceRegisterFile, RegisterMap
    from swi3s_studio.ui.register_view import RegisterView
    rmap = RegisterMap.load()
    view = RegisterView(rmap)
    try:
        view.set_files({0: DeviceRegisterFile(rmap, 0)})
        seen = 0
        it = QTreeWidgetItemIterator(view._tree)
        while it.value():
            item = it.value()
            if item.text(1).startswith("0x"):
                assert item.toolTip(1) == item.text(1), item.text(0)
                seen += 1
            it += 1
        assert seen > 10
    finally:
        view.deleteLater()


# ---------------------------------------------------------- Registers Compare tooltip
def test_the_compare_tooltip_names_the_bus_only_for_a_value_the_bus_carried():
    """A register the bus wrote tips "the bus wrote", one it read back "the bus read back";
    one it never wrote tips its reset value as never written, not as a value the bus
    carried."""
    _app()
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QTreeWidgetItemIterator

    from swi3s_studio.model import DeviceRegisterFile, RegisterMap
    from swi3s_studio.model.registers import SLC_BASE, Provenance
    from swi3s_studio.ui.register_view import RegisterView
    rmap = RegisterMap.load()
    f = DeviceRegisterFile(rmap, 0)
    written = SLC_BASE + 0x81
    f.apply_write(written, bytes([0x05]))
    read = SLC_BASE + 0x82
    f.apply_read(read, bytes([0x09]))
    view = RegisterView(rmap)
    try:
        view.set_files({0: f})
        rows = {}
        it = QTreeWidgetItemIterator(view._tree)
        while it.value():
            key = it.value().data(0, Qt.UserRole)
            if key and not it.value().text(0).endswith("(CURR)"):
                rows.setdefault(int(key[1]), it.value())
            it += 1
        never = next(a for a in sorted(rows) if a not in (written, read)
                     and f.provenance(a) == Provenance.DEFAULT)
        assert f.provenance(read) == Provenance.READ
        view.set_expected([(0, written, 0x07), (0, read, 0x01), (0, never, 0x03)])
        tips = {}
        it = QTreeWidgetItemIterator(view._tree)
        while it.value():
            key = it.value().data(0, Qt.UserRole)
            if key and not it.value().text(0).endswith("(CURR)"):
                tips.setdefault(int(key[1]), it.value().toolTip(0))
            it += 1
        reset = f"0x{f.value(never):02X}"
        assert "from the expected config CSV (the bus wrote 0x05)" in tips[written]
        assert "from the expected config CSV (the bus read back 0x09)" in tips[read]
        assert f"(never written on the bus (reset {reset}))" in tips[never]
        assert "the bus" not in tips[never].replace("never written on the bus", "")
    finally:
        view.deleteLater()


def test_an_imported_config_value_without_a_compare_overlay_tips_no_expected_config():
    """The "expected config" clause is the Compare overlay's: a register an imported config
    set tips its provenance only."""
    _app()

    from swi3s_studio.model import DeviceRegisterFile, RegisterMap
    from swi3s_studio.model.registers import SLC_BASE, Provenance
    from swi3s_studio.ui.register_view import RegisterView
    rmap = RegisterMap.load()
    f = DeviceRegisterFile(rmap, 0)
    imported = SLC_BASE + 0x81
    f.apply_write(imported, bytes([0x05]), Provenance.CSV)
    view = RegisterView(rmap)
    try:
        view.set_files({0: f})
        rows = _register_rows(view)
        assert "provenance: csv" in rows[imported].toolTip(0)
        assert "expected config" not in rows[imported].toolTip(0)
        view.set_expected([(0, imported, 0x07)])
        assert "from the expected config CSV (" in _register_rows(view)[imported].toolTip(0)
    finally:
        view.deleteLater()


# -------------------------------------------------------------- Registers column widths
def test_a_registers_column_the_user_widened_keeps_its_width_through_rebuilds():
    """The Registers columns take their widths when the pane is made and when a workspace
    sets them; a rebuild for a device, a PHY, a register map, a CSV overlay or a theme keeps
    the widths the columns have."""
    _app()
    from swi3s_studio.model import DeviceRegisterFile, RegisterMap
    from swi3s_studio.ui.register_view import RegisterView
    rmap = RegisterMap.load()
    view = RegisterView(rmap)
    try:
        assert view.column_widths() == RegisterView.DEFAULT_WIDTHS
        view.set_files({0: DeviceRegisterFile(rmap, 0), 1: DeviceRegisterFile(rmap, 1)})
        fields = view.column_titles().index("Fields")
        view._tree.setColumnWidth(fields, 500)
        view.set_phy("PHY2")
        view._device.setCurrentIndex(1)
        view.set_peripheral_maps({})
        view.set_expected([(0, 0x40, 1)])
        view.clear_expected()
        view.retheme()
        view.set_files({0: DeviceRegisterFile(rmap, 0)})
        assert view.column_widths() == {**RegisterView.DEFAULT_WIDTHS, "Fields": 500}
    finally:
        view.deleteLater()
