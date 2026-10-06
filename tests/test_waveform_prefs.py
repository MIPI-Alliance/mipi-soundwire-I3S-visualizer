"""Waveform colours and weights: the user's choices, and one Link's stream colours.

line_style resolves every line colour as the Link's per-stream override → the user's
preference (QSettings, per theme) → the theme's default, and every pane asks it rather
than keeping a copy. These pin that order, per theme; that the settings persist and that
a malformed one falls back; the Preferences dialog; the Audio pane's per-stream Color…
menu (one Link's, shared by Audio, Capture and Samples, saved in the workspace); and that
every pane redraws when a choice changes. conftest.py resets the settings around each
test, since they persist across the run.
"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import json

import pytest
from PySide6.QtCore import QPoint, QSettings
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QApplication, QColorDialog, QMenu

from swi3s_studio.ui import line_style
from swi3s_studio.ui.grid_view import _dp_index
from swi3s_studio.ui.theme import _PALETTES, VizTheme, apply_palette

_PICK = "#123456"


@pytest.fixture(autouse=True)
def _dark():
    apply_palette("dark")
    yield
    apply_palette("dark")


# ---- the resolver ------------------------------------------------------------------------

def test_defaults_are_the_theme_palettes():
    for mode in ("dark", "light"):
        pal = _PALETTES[mode]
        assert line_style.palette("dp", mode) == pal["DP_LINE_PALETTE"]
        assert line_style.palette("bookmark", mode) == pal["DP_LINE_PALETTE"]
        assert line_style.palette("capture", mode) == [pal["RAW_DP"], pal["RAW_DN"]]
        t = pal["TRACE_PALETTE"]
        assert line_style.palette("timing", mode) == [t[0], t[5], t[3], t[2]]
    assert line_style.weight("audio") == 1 and line_style.weight("capture") == 2


def test_override_then_preference_then_default_per_theme():
    line_style.set_preferences({"colors": {"light": {"dp": [None, None, "#aa0000"]}}})
    ov = {(0, 2, "light"): "#00bb00"}
    # dark: no preference, no dark override -> the default
    assert line_style.stream_color(2, 0, 2, ov) == _PALETTES["dark"]["DP_LINE_PALETTE"][2]
    apply_palette("light")
    assert line_style.stream_color(2, 0, 2, ov) == "#00bb00"             # the override wins
    assert line_style.stream_color(2, 0, 2) == "#aa0000"                 # then the preference
    assert line_style.stream_color(3, 0, 3, ov) == _PALETTES["light"]["DP_LINE_PALETTE"][3]


def test_preferences_persist_and_a_default_is_stored_as_unset():
    dark_dp = list(_PALETTES["dark"]["DP_LINE_PALETTE"])
    dark_dp[0] = "#010203"
    line_style.set_preferences({"colors": {"dark": {"dp": dark_dp,
                                                     "timing": line_style.defaults("timing", "dark")}},
                                "weights": {"audio": 3, "capture": 2}})
    stored = json.loads(QSettings().value(line_style.COLOR_KEY))
    assert stored == {"dark": {"dp": ["#010203"] + [None] * 11}}       # timing = default: unset
    assert json.loads(QSettings().value(line_style.WEIGHT_KEY)) == {"audio": 3}
    line_style.reset_cache()                                           # as on the next launch
    assert line_style.palette("dp")[0] == "#010203"
    assert line_style.palette("dp")[1:] == dark_dp[1:]
    assert line_style.weight("audio") == 3 and line_style.weight("capture") == 2


@pytest.mark.parametrize("colors, weights", [
    ("not json", "{"),
    ('{"dark": {"dp": ["red", 7, "#12345"]}, "sepia": {"dp": ["#000000"]}}',
     '{"audio": 9, "capture": "2", "grid": 3}'),
])
def test_a_malformed_setting_falls_back_to_the_default(colors, weights):
    QSettings().setValue(line_style.COLOR_KEY, colors)
    QSettings().setValue(line_style.WEIGHT_KEY, weights)
    line_style.reset_cache()
    assert line_style.palette("dp") == _PALETTES["dark"]["DP_LINE_PALETTE"]
    assert line_style.weight("audio") == 1 and line_style.weight("capture") == 2


def test_overrides_round_trip_through_json():
    ov = {(1, 4, "dark"): "#abcdef", (0, 2, "light"): "#000001"}
    assert line_style.overrides_from_json(line_style.overrides_to_json(ov)) == ov
    assert line_style.overrides_from_json(
        [[1, 4, "sepia", "#abcdef"], [1, 4, "dark", "nope"], "x", [1, 2]]) == {}


# ---- the window --------------------------------------------------------------------------

@pytest.fixture
def win(monkeypatch):
    QApplication.instance() or QApplication([])
    from swi3s_studio.ui.main_window import MainWindow
    monkeypatch.setattr("swi3s_studio.ui.main_window.save_preference", lambda _p: None)
    w = MainWindow()
    w.load_demo()
    s0 = w._session.commands[2]["start_sample"]
    w._bookmarks.add(s0, link=0)
    w._bookmarks.add(s0 + 50_000, link=0)
    w._push_bookmarks()
    return w


def _audio_pens(win):
    return {(dev, dp, ch): (curve.opts["pen"].color().name(), curve.opts["pen"].width())
            for _p, curve, dev, dp, ch, _n in win._audio_view._plots}


def _stream(win):
    """The first audio stream, (device, dp)."""
    _p, _c, dev, dp, _ch, _n = win._audio_view._plots[0]
    return dev, dp


def test_a_preference_change_redraws_every_pane(win, monkeypatch):
    """OK in Preferences stores the settings and recolours Audio, Capture (traces and bit
    overlay), Samples, Timing and the bookmark lines, with the new weights."""
    from swi3s_studio.ui import eye_view
    from swi3s_studio.ui.preferences_dialog import PreferencesDialog
    dev, dp = _stream(win)
    slot = _dp_index(dev, dp)

    def accept(dlg):
        dlg.set_value("dark", "dp", slot, "#ff0001")
        dlg.set_value("dark", "capture", 1, "#ff0002")
        dlg.set_value("dark", "timing", 2, "#ff0003")
        dlg.set_value("dark", "bookmark", 0, "#ff0004")
        dlg._weight_spins["audio"].setValue(3)
        dlg._weight_spins["capture"].setValue(4)
        return 1
    monkeypatch.setattr(PreferencesDialog, "exec", accept)
    win.show_preferences()

    pens = {k: v for k, v in _audio_pens(win).items() if k[:2] == (dev, dp)}
    assert pens and all(v == ("#ff0001", 3) for v in pens.values())
    raw = win._raw_view
    assert raw._dn_curve.opts["pen"].color().name() == "#ff0002"
    assert raw._dn_curve.opts["pen"].width() == 4 == raw._dp_curve.opts["pen"].width()
    lanes = [k for k in raw._port_colors if k[:2] == (dev, dp)]
    assert lanes and all(raw._port_colors[k].name() == "#ff0001" for k in lanes)
    assert all(c.name() == "#ff0001" for k, c in win._sample_view._lane_colors.items()
               if k[:2] == (dev, dp))
    assert [c for *_x, c in eye_view._cat_specs()][2] == "#ff0003"
    assert win._raw_view._bm_lines["A1"].pen.color().name() == "#ff0004"
    # stored, and only for the theme it was set in
    win.apply_theme("light")
    assert raw._dn_curve.opts["pen"].color().name() == _PALETTES["light"]["RAW_DN"]
    assert raw._dn_curve.opts["pen"].width() == 4                     # weights: both themes


def test_cancel_changes_nothing(win, monkeypatch):
    from swi3s_studio.ui.preferences_dialog import PreferencesDialog

    def cancel(dlg):
        dlg.set_value("dark", "capture", 0, "#ff0002")
        return 0
    monkeypatch.setattr(PreferencesDialog, "exec", cancel)
    win.show_preferences()
    assert line_style.preferences() == {"colors": {}, "weights": {}}
    assert win._raw_view._dp_curve.opts["pen"].color().name() == _PALETTES["dark"]["RAW_DP"]


def test_the_dialog_flags_a_low_contrast_choice_and_resets():
    QApplication.instance() or QApplication([])
    from swi3s_studio.ui.preferences_dialog import PreferencesDialog
    dlg = PreferencesDialog(line_style.preferences(), mode="light")
    assert dlg._tabs.currentIndex() == 1
    assert not any(dlg.low_contrast(*key) for key in dlg._swatches)  # the defaults clear 3:1
    dlg.set_value("light", "dp", 2, "#ffff80")                       # the old pastel
    assert dlg.low_contrast("light", "dp", 2)
    assert dlg._warnings[("light", "dp", 2)].text().startswith("⚠")
    assert not dlg.low_contrast("dark", "dp", 2)                     # fine on dark
    dlg._weight_spins["audio"].setValue(4)
    dlg.reset_to_defaults()
    assert dlg.value("light", "dp", 2) == _PALETTES["light"]["DP_LINE_PALETTE"][2]
    assert dlg._warnings[("light", "dp", 2)].text() == ""
    line_style.set_preferences(dlg.preferences())
    assert line_style.preferences() == {"colors": {}, "weights": {}}


def test_a_swatch_click_picks_a_colour(monkeypatch):
    QApplication.instance() or QApplication([])
    from swi3s_studio.ui.preferences_dialog import PreferencesDialog
    dlg = PreferencesDialog(line_style.preferences())
    monkeypatch.setattr(QColorDialog, "getColor", lambda *a, **k: QColor(_PICK))
    dlg._swatches[("dark", "timing", 1)].click()
    assert dlg.preferences()["colors"]["dark"]["timing"] == [None, _PICK, None, None]
    monkeypatch.setattr(QColorDialog, "getColor", lambda *a, **k: QColor())   # cancelled
    dlg._swatches[("dark", "timing", 1)].click()
    assert dlg.value("dark", "timing", 1) == _PICK


def _choose(win, monkeypatch, which, dev, dp):
    """Right-click (dev, dp)'s first checkbox and pick `which` ("Color…" / "Reset Color")."""
    cb = next(c for (d, p, _ch), c in win._audio_view._checks.items() if (d, p) == (dev, dp))
    monkeypatch.setattr(QMenu, "exec", lambda m, *_a: next(
        a for a in m.actions() if a.text() == which))
    monkeypatch.setattr(QColorDialog, "getColor", lambda *a, **k: QColor(_PICK))
    win._audio_view._stream_menu(cb, QPoint(0, 0), dev, dp)


def _stream_colours(win, dev, dp):
    """Every pane's colour for (dev, dp): Audio pens + checkbox, Capture overlay, Samples."""
    out = {c for (d, p, _ch), (c, _w) in _audio_pens(win).items() if (d, p) == (dev, dp)}
    out |= {c.name() for k, c in win._raw_view._port_colors.items() if k[:2] == (dev, dp)}
    out |= {c.name() for k, c in win._sample_view._lane_colors.items() if k[:2] == (dev, dp)}
    for (d, p, _ch), cb in win._audio_view._checks.items():
        if (d, p) == (dev, dp):
            out.add(cb.styleSheet().split("color:")[1].split(";")[0].strip())
    return out


def test_a_stream_colour_reaches_audio_capture_and_samples_for_its_theme(win, monkeypatch):
    dev, dp = _stream(win)
    default = _stream_colours(win, dev, dp)
    assert len(default) == 1 and default != {_PICK}
    _choose(win, monkeypatch, "Color…", dev, dp)
    assert win._links[0].view.stream_colors == {(dev, dp, "dark"): _PICK}
    assert _stream_colours(win, dev, dp) == {_PICK}
    win.apply_theme("light")                                   # chosen for dark only
    assert _stream_colours(win, dev, dp) == {line_style.palette("dp")[_dp_index(dev, dp)]}
    win.apply_theme("dark")
    assert _stream_colours(win, dev, dp) == {_PICK}
    _choose(win, monkeypatch, "Reset Color", dev, dp)
    assert win._links[0].view.stream_colors == {}
    assert _stream_colours(win, dev, dp) == default


def test_stream_colours_belong_to_their_link(win, monkeypatch):
    """Two Links: an override on Link 1 is not Link 2's, and comes back with Link 1."""
    from swi3s_studio.session import Session
    dev, dp = _stream(win)
    _choose(win, monkeypatch, "Color…", dev, dp)
    other = Session.from_demo(300, cold_start=True, phy=2, register_map=win._rmap)
    win.load_session(other, add_link=True)
    win.set_group_link("bottom", 1)
    assert win._links[1].view.stream_colors == {}
    assert _PICK not in _stream_colours(win, dev, dp)
    win.set_group_link("bottom", 0)
    assert _stream_colours(win, dev, dp) == {_PICK}


def test_stream_colours_round_trip_through_the_workspace(win, monkeypatch):
    from swi3s_studio.ui.main_window import MainWindow
    from swi3s_studio.workspace import Workspace, session_from_source
    dev, dp = _stream(win)
    _choose(win, monkeypatch, "Color…", dev, dp)
    spec = win._link_spec(win._links[0])
    assert spec.stream_colors == [[dev, dp, "dark", _PICK]]
    ws = Workspace.from_json(Workspace(links=[spec]).to_json())
    win2 = MainWindow()
    win2.load_session(session_from_source(ws.links[0].source, register_map=win2._rmap))
    win2.apply_workspace(ws)
    assert win2._links[0].view.stream_colors == {(dev, dp, "dark"): _PICK}
    assert _stream_colours(win2, dev, dp) == {_PICK}


def test_a_workspace_saved_before_stream_colours_opens_with_none(win):
    from swi3s_studio.workspace import Workspace
    d = json.loads(Workspace(links=[win._link_spec(win._links[0])]).to_json())
    del d["links"][0]["stream_colors"]
    ws = Workspace.from_json(json.dumps(d))
    assert ws.links[0].stream_colors == []
    win._links[0].view.stream_colors = {(0, 0, "dark"): _PICK}
    win.apply_workspace(ws)                                   # a reopen replaces, not merges
    assert win._links[0].view.stream_colors == {}


def test_preferences_is_in_the_menu(win):
    import sys
    act = win._prefs_action
    # macOS lifts it into the application menu, so View also has a named item; elsewhere
    # the named item is Preferences itself.
    want = "Preferences…" if sys.platform == "darwin" else "Waveform Colors and Line Weight…"
    assert act.text().replace("&", "") == want
    assert act.menuRole() == act.MenuRole.PreferencesRole      # macOS: the app menu, ⌘,
    assert any(act in m.menu().actions() for m in win.menuBar().actions() if m.menu())
    assert VizTheme.MODE == "dark"
