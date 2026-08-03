"""Per-port cell-label fields in the Analyzer Bus Grid.

The renderer always supported this — `GridView.set_cells` takes a `dp_display` map
and `_dp_disp` reads `display_fields` from it — but nothing ever passed one, so the
Analyzer was hard-wired to the Channel|Bit default. On a port carrying several
samples per row that hides the distinguishing field: every cell drew "C0B0".

These tests cover the three pieces that make the choice reachable and durable: the
swatch is a click target, the choice reaches the rendered label, and it survives a
workspace reopen.
"""
import json

import pytest

from swi3s_studio.session import Session

SAMPLE, CHANNEL, BIT = 1, 2, 4
DEFAULT = 6                      # Channel|Bit — the Visualizer default


@pytest.fixture
def qapp():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


@pytest.fixture
def session():
    return Session.from_demo(48, phy=2, cold_start=True)


def _sample(session):
    return int(session.segments[-1]["start_sample"]) + 2000


def _grid(session, qapp):
    from swi3s_studio.ui.grid_view import GridView

    smp = _sample(session)
    gv = GridView()
    gv.resize(1200, 300)
    gv.set_cells(session.grid_cells_at(smp, 6), session.column_count_at(smp),
                 dp_display=session.dp_display_map())
    return gv


# --------------------------------------------------------------------------
# Session state
# --------------------------------------------------------------------------

def test_default_is_channel_bit(session):
    """Unset ports keep the Visualizer default, and nothing is written to disk."""
    assert session.label_fields_for(0, 0) == DEFAULT
    assert session.dp_display_map() == {}
    assert session.source.get("label_fields") is None


def test_set_label_fields_round_trips(session):
    session.set_label_fields(0, 2, SAMPLE | CHANNEL)
    assert session.label_fields_for(0, 2) == 3
    assert session.dp_display_map()[(0, 2)] == {"display_fields": 3}


def test_label_fields_are_per_port(session):
    """The point of per-port: changing one must not move the others."""
    session.set_label_fields(0, 2, SAMPLE)
    assert session.label_fields_for(0, 2) == SAMPLE
    assert session.label_fields_for(0, 0) == DEFAULT
    assert session.label_fields_for(0, 3) == DEFAULT


def test_setting_back_to_default_drops_the_entry(session):
    """A workspace should record real choices only — otherwise every port opened
    once is pinned to today's default, and changing that default later silently
    doesn't apply to them."""
    session.set_label_fields(0, 2, SAMPLE)
    assert session.source.get("label_fields") == {"0.2": 1}
    session.set_label_fields(0, 2, DEFAULT)
    assert session.source.get("label_fields") is None


def test_label_fields_survive_a_workspace_round_trip(session):
    """Keys are tuples in memory and "dev.dp" strings on disk (JSON has no tuple
    keys), and each from_* factory rebuilds `source` from its own args — so this
    only works if from_source forwards them explicitly."""
    session.set_label_fields(0, 2, SAMPLE | CHANNEL)
    session.set_label_fields(0, 3, BIT)

    reopened = Session.from_source(json.loads(json.dumps(session.source)))

    assert reopened.label_fields == {(0, 2): 3, (0, 3): 4}


def test_corrupt_persisted_entries_are_ignored():
    """A hand-edited or future workspace must still open."""
    parsed = Session._load_label_fields({"0.2": 3, "bogus": 1, "1.x": 2, "3.4": "nope"})
    assert parsed == {(0, 2): 3}


def test_label_fields_do_not_re_decode(session):
    """Display-only: this must not pay for a decode. The Bus Grid re-renders from
    cells the session already has."""
    before = session.decoder
    session.set_label_fields(0, 2, SAMPLE)
    assert session.decoder is before


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------

def test_default_label_shows_channel_and_bit(session, qapp):
    from swi3s_studio.ui.grid_view import GridView

    cell = {"slot": 1, "sample": 0, "channel": 0, "bit": 15, "port_mode": 0}
    assert GridView._data_label(cell, DEFAULT, 1) == "C0B15"


def test_label_honours_each_field_combination(session, qapp):
    from swi3s_studio.ui.grid_view import GridView

    cell = {"slot": 1, "sample": 1, "channel": 2, "bit": 3, "port_mode": 0}
    assert GridView._data_label(cell, SAMPLE, 1) == "S1"
    assert GridView._data_label(cell, CHANNEL, 1) == "C2"
    assert GridView._data_label(cell, BIT, 1) == "B3"
    assert GridView._data_label(cell, SAMPLE | CHANNEL, 1) == "S1C2"
    assert GridView._data_label(cell, SAMPLE | CHANNEL | BIT, 1) == "S1C2B3"
    assert GridView._data_label(cell, 0, 1) == ""


def test_chosen_fields_reach_the_rendered_label(session, qapp):
    """End to end through the renderer: the map the session hands GridView must
    change what _dp_disp returns for that port, and only that port.
    """
    session.set_label_fields(0, 2, SAMPLE | CHANNEL)
    gv = _grid(session, qapp)

    dp2 = {"device": 0, "dp": 2}
    dp0 = {"device": 0, "dp": 0}
    assert gv._dp_disp(dp2)[0] == SAMPLE | CHANNEL
    assert gv._dp_disp(dp0)[0] == DEFAULT


def test_sample_field_distinguishes_multi_sample_cells(session, qapp):
    """The actual user-visible win. A port with two samples per row draws the same
    "C0B0" in every cell under the default; adding Sample separates them.
    """
    from swi3s_studio.ui.grid_view import GridView

    smp = _sample(session)
    cells = [c for c in session.grid_cells_at(smp, 6)
             if not c["is_cds"] and c.get("dp") == 2 and c.get("slot") == 1]
    assert len({c["sample"] for c in cells}) > 1, "need a multi-sample port"

    default_labels = {GridView._data_label(c, DEFAULT, 1) for c in cells}
    with_sample = {GridView._data_label(c, SAMPLE | CHANNEL, 1) for c in cells}
    assert len(default_labels) == 1, "precondition: default collapses them"
    assert len(with_sample) > 1, "Sample field must separate them"


# --------------------------------------------------------------------------
# The swatch as a click target
# --------------------------------------------------------------------------

def test_key_swatches_are_hit_testable(session, qapp):
    gv = _grid(session, qapp)
    ports = {key for _rect, key in gv._key_hits}
    assert ports == {(0, 0), (0, 1), (0, 2), (0, 3)}


def test_clicking_a_swatch_emits_its_port(session, qapp):
    from PySide6.QtCore import QEvent, QPointF, Qt
    from PySide6.QtGui import QMouseEvent

    gv = _grid(session, qapp)
    got = []
    gv.dpKeyClicked.connect(lambda d, p: got.append((d, p)))

    rect, key = gv._key_hits[2]
    pos = QPointF(gv.mapFromScene(rect.center()))
    gv.mousePressEvent(QMouseEvent(QEvent.MouseButtonPress, pos,
                                   Qt.LeftButton, Qt.LeftButton, Qt.NoModifier))
    assert got == [key]


def test_clicking_the_grid_body_emits_nothing(session, qapp):
    """Only the swatch is a target — a click in the grid must fall through to the
    base class (panning/scrolling must keep working)."""
    from PySide6.QtCore import QEvent, QPointF, Qt
    from PySide6.QtGui import QMouseEvent

    gv = _grid(session, qapp)
    got = []
    gv.dpKeyClicked.connect(lambda d, p: got.append((d, p)))

    rect, _ = gv._key_hits[0]
    below = QPointF(gv.mapFromScene(rect.center().x(), rect.center().y() + 120))
    gv.mousePressEvent(QMouseEvent(QEvent.MouseButtonPress, below,
                                   Qt.LeftButton, Qt.LeftButton, Qt.NoModifier))
    assert got == []


def test_hits_are_dropped_when_the_scene_is_rebuilt(session, qapp):
    """Stale swatch rects would map a click onto the wrong port. Every scene
    teardown must drop them — set_tx_raster replaces the grid entirely."""
    gv = _grid(session, qapp)
    assert gv._key_hits
    gv.set_tx_raster({"column_count": 4, "cds_col": 0, "rows": [[0, 0, 0, 0]],
                      "row_labels": [0]})
    assert gv._key_hits == []


def test_engine_mode_has_no_dp_hits(session, qapp):
    """The Visualizer grid colours by dp NUMBER with no device, so a swatch there
    can't identify a (device, dp) port — it must not become clickable."""
    from swi3s_studio.ui.grid_view import GridView

    gv = GridView()
    gv.resize(900, 300)
    cells = [{"row": 0, "col": 1, "is_cds": False, "slot": 1, "device": 0, "dp": 0,
              "channel": 0, "bit": 0, "sample": 0, "is_source": True, "port_mode": 0,
              "display_fields": 6}]
    gv.set_bus_model(cells, {}, 4, 1)
    assert gv._key_hits == []


# --------------------------------------------------------------------------
# The dialog + main-window wiring
# --------------------------------------------------------------------------

def test_dialog_previews_the_real_label(qapp):
    from swi3s_studio.ui.authoring.dialogs import GridLabelFieldsDialog

    dlg = GridLabelFieldsDialog(None, "D0·DP2 Cell Labels", SAMPLE | CHANNEL,
                                sample=1, channel=2, bit=15)
    assert dlg._preview.text() == "Preview: S1C2"
    dlg._toggle(BIT)
    assert dlg._preview.text() == "Preview: S1C2B15"
    assert dlg.fields == SAMPLE | CHANNEL | BIT


def test_dialog_with_no_fields_says_so(qapp):
    from swi3s_studio.ui.authoring.dialogs import GridLabelFieldsDialog

    dlg = GridLabelFieldsDialog(None, "t", 0)
    assert dlg._preview.text() == "Preview: (no label)"


def test_clicking_a_swatch_applies_the_choice(qapp, monkeypatch):
    """The whole path: main_window opens the dialog for the clicked port and the
    accepted fields land in the session."""
    import swi3s_studio.ui.authoring.dialogs as dialogs
    from swi3s_studio.ui.main_window import MainWindow

    w = MainWindow()
    try:
        w.load_session(Session.from_demo(48, phy=2, cold_start=True))
        qapp.processEvents()
        w.cursor.set_sample(_sample(w._session))
        qapp.processEvents()

        class _Accept(dialogs.GridLabelFieldsDialog):
            def __init__(self, *a, **k):
                super().__init__(*a, **k)
                self.fields = SAMPLE | CHANNEL

            def exec(self):
                return 1

        monkeypatch.setattr(dialogs, "GridLabelFieldsDialog", _Accept)
        w._on_dp_key_clicked(0, 2)
        assert w._session.label_fields == {(0, 2): 3}
    finally:
        w.join_worker_threads()


def test_cancelling_the_dialog_changes_nothing(qapp, monkeypatch):
    import swi3s_studio.ui.authoring.dialogs as dialogs
    from swi3s_studio.ui.main_window import MainWindow

    w = MainWindow()
    try:
        w.load_session(Session.from_demo(48, phy=2, cold_start=True))
        qapp.processEvents()

        class _Reject(dialogs.GridLabelFieldsDialog):
            def exec(self):
                return 0

        monkeypatch.setattr(dialogs, "GridLabelFieldsDialog", _Reject)
        w._on_dp_key_clicked(0, 2)
        assert w._session.label_fields == {}
    finally:
        w.join_worker_threads()


def test_choice_reaches_the_real_grid_widget(qapp, monkeypatch):
    """main_window must actually hand the map to GridView.

    Building a GridView directly (as the tests above do) proves the renderer honours
    a map, not that the app ever supplies one — that wiring was missing for the whole
    life of the dp_display parameter, which is the bug this feature fixes.
    """
    import swi3s_studio.ui.authoring.dialogs as dialogs
    from swi3s_studio.ui.main_window import MainWindow

    w = MainWindow()
    try:
        w.load_session(Session.from_demo(48, phy=2, cold_start=True))
        qapp.processEvents()
        w._grid_dock.raise_()
        w.cursor.set_sample(_sample(w._session))
        qapp.processEvents()

        class _Accept(dialogs.GridLabelFieldsDialog):
            def __init__(self, *a, **k):
                super().__init__(*a, **k)
                self.fields = SAMPLE | CHANNEL

            def exec(self):
                return 1

        monkeypatch.setattr(dialogs, "GridLabelFieldsDialog", _Accept)
        w._on_dp_key_clicked(0, 2)
        qapp.processEvents()

        # Read it off the WIDGET, not the session.
        assert w._grid_view._dp_disp({"device": 0, "dp": 2})[0] == SAMPLE | CHANNEL
        assert w._grid_view._dp_disp({"device": 0, "dp": 0})[0] == DEFAULT
    finally:
        w.join_worker_threads()


def test_apply_to_all_covers_every_port(qapp, monkeypatch):
    import swi3s_studio.ui.authoring.dialogs as dialogs
    from swi3s_studio.ui.main_window import MainWindow

    w = MainWindow()
    try:
        w.load_session(Session.from_demo(48, phy=2, cold_start=True))
        qapp.processEvents()
        w.cursor.set_sample(_sample(w._session))
        qapp.processEvents()

        class _All(dialogs.GridLabelFieldsDialog):
            def __init__(self, *a, **k):
                super().__init__(*a, **k)
                self.fields = SAMPLE

            def exec(self):
                self.apply_to_all = True
                return 1

        monkeypatch.setattr(dialogs, "GridLabelFieldsDialog", _All)
        w._on_dp_key_clicked(0, 0)
        assert set(w._session.label_fields) == {(0, 0), (0, 1), (0, 2), (0, 3)}
        assert set(w._session.label_fields.values()) == {SAMPLE}
    finally:
        w.join_worker_threads()


def test_apply_to_all_covers_every_port_from_an_EARLY_cursor(qapp, monkeypatch):
    """"Apply to every data port" must mean the capture's ports, not the cursor's.

    Found by the 3.0.11 release review. The port set was scanned from
    grid_cells_at(cursor, _grid_rows) — a small window as-of the cursor. With the cursor
    parked in the FIRST config region, before any commit has taken effect, that window
    yields NO ports on the stock PHY2 demo, so "apply to every data port" silently applied
    to only the port clicked. test_apply_to_all_covers_every_port cannot catch it because
    it parks the cursor at the LAST segment, where every port is live.
    """
    import swi3s_studio.ui.authoring.dialogs as dialogs
    from swi3s_studio.ui.main_window import MainWindow

    w = MainWindow()
    try:
        w.load_session(Session.from_demo(48, phy=2, cold_start=True))
        qapp.processEvents()
        early = int(w._session.segments[0]["start_sample"])
        w.cursor.set_sample(early)
        qapp.processEvents()

        # Precondition: this really is the blind spot — the old window-based scan sees
        # nothing here, so the test would pass vacuously if the cursor were placed later.
        visible = {(int(c["device"]), int(c["dp"]))
                   for c in w._session.grid_cells_at(early, w._grid_rows)
                   if not c["is_cds"] and int(c.get("dp", -1)) >= 0}
        assert visible == set(), f"expected no ports in the window at {early}, saw {visible}"

        class _All(dialogs.GridLabelFieldsDialog):
            def __init__(self, *a, **k):
                super().__init__(*a, **k)
                self.fields = SAMPLE

            def exec(self):
                self.apply_to_all = True
                return 1

        monkeypatch.setattr(dialogs, "GridLabelFieldsDialog", _All)
        w._on_dp_key_clicked(0, 0)
        assert set(w._session.label_fields) == {(0, 0), (0, 1), (0, 2), (0, 3)}, (
            "apply-to-all from an early cursor must still reach every configured port; "
            f"got {sorted(w._session.label_fields)}")
        assert set(w._session.label_fields.values()) == {SAMPLE}
    finally:
        w.join_worker_threads()
