"""Timing Diagrams drawing settings (`ui/diagram_style.py`), the Preferences dialog's Timing
Diagrams tab (`ui/diagram_style_page.py`) and the not-maximized scale.

Pinned here:
- every default is the value of record the diagrams drew with, and with the defaults every
  pen the diagrams draw is one of the pens of record;
- each setting changes what is drawn: every colour, weight, dash, size and font
  setting changes the rendered image of one of a set of diagrams that draws every element;
- a colour is per theme: the dark theme's choice draws in dark and not on a light export;
- the settings persist, a malformed stored value falls back, and an entry equal to its
  default is stored as unset;
- the dialog: View ▸ Diagram Colors, Lines and Fonts… (Timing Calculator only) opens it on the
  Timing Diagrams tab; an edit redraws the open diagram at once; Cancel puts the stored
  settings back and OK stores them; Reset to Defaults resets the tab shown; a colour below
  3:1 on its theme's diagram background is flagged, and no default is;
- Export Settings… writes every setting and Import Settings… reads it back to the same
  settings; a malformed entry is an import warning, shown in an Import warnings window, and
  a file that is not settings is refused with a message;
- the not-maximized scale applies only while the diagram is not maximized;
- with larger fonts and heavier lines no text overlaps another or a line, in the term
  diagram and the whole-inequality view.

Run: QT_QPA_PLATFORM=offscreen PYTHONPATH=. python3 -m pytest tests/test_diagram_style.py
"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import hashlib
import json

import pytest
import timing_diagram_support as support

from swi3s_studio.timing import CalcInputs

pytestmark = [pytest.mark.gui, pytest.mark.usefixtures("shared_compute")]

_KEEPER = CalcInputs(Man_ede=True, t_keeper_ns=8.0)

# The values of record, held by hand: the user's settings, in line heights or body font sizes,
# most of them whole px at the Timing page's diagram font (18 px, its line height 21.1875 px),
# the dash styles and the theme tokens.
_LH, _PX = 21.1875, 18
_W3, _W2 = 3 / _LH, 2 / _LH         # 3 px and 2 px pens
_RECORD_VALUES = {
    **{f"{k}.weight": _W3 for k in ("clock", "data", "flight", "span", "release", "interval",
                                    "axis", "margin")},
    **{f"{k}.weight": _W2 for k in ("vdd", "gnd", "gnd_offset", "reference", "valid",
                                    "threshold", "guide_dot", "guide_dash")},
    "leader.weight": 0.06,
    "high_z.dash": "dot", "vdd.dash": "dot", "gnd.dash": "dot", "gnd_offset.dash": "dash",
    "reference.dash": "dash", "valid.dash": "dash", "threshold.dash": "solid",
    "guide_dot.dash": "dot", "guide_dash.dash": "dash",
    "threshold.head": 0.32, "span.head": 6 / _LH, "interval.head": 6 / _LH,
    "flight.head": 6 / _LH, "margin.head": 10 / _LH, "dot.radius": 5 / _LH,
    "release.length": 0.6, "axis.tick": 5 / _LH,
    "label.narrow": 0.85,
    "font.title.size": 1.12, "font.note.size": 0.85,
    **{f"font.{k}.size": 15 / _PX for k in ("heading", "row", "span", "level", "axis")},
    **{f"font.{k}.weight": "normal" for k in ("title", "heading", "row", "span", "level",
                                              "axis", "note")},
    **{f"font.{k}.family": "" for k in ("title", "heading", "row", "span", "level", "axis",
                                        "note")},
}
_RECORD_COLORS = {
    "clock": "DIAG_CLOCK", "data": "DIAG_DATA", "data_per": "DIAG_DATA_PER",
    "data_per_b": "DIAG_DATA_PER_B", "vdd": "DIAG_DIM", "gnd": "DIAG_DIM",
    "gnd_offset": "DIAG_GROUND", "reference": "DIAG_OK", "valid": "DIAG_VALID",
    "threshold": "DIAG_LEVEL", "guide_dot": "DIAG_GUIDE", "guide_dash": "DIAG_GUIDE",
    "span": "DIAG_TEXT", "interval": "DIAG_DIM", "margin_ok": "DIAG_OK",
    "margin_bad": "DIAG_BAD", "release": "DIAG_TEXT", "keeper": "DIAG_KEEPER",
    "axis": "DIAG_TEXT", "text": "DIAG_TEXT", "dim_text": "DIAG_DIM",
}

# Diagrams that between them draw every element: (inputs, leg, term key or WHOLE, width).
_WHOLE = "WHOLE"
_DRAWS_ALL = (
    (CalcInputs(), "MP_hold", _WHOLE, 900),            # a short margin, drawn whole
    (CalcInputs(), "MP_setup", _WHOLE, 900),           # a gap
    (CalcInputs(), "MP_setup", "Per_t_IS", 900),       # a threshold off an offset ground
    (CalcInputs(), "MP_setup", "t_PD,mis fraction", 900),    # flights
    (_KEEPER, "keeper_Per", "Per_t_DZ", 900),          # a release, the keeper, high-Z
    (_KEEPER, "keeper_Man", "Man_t_Keeper_Response", 900),   # a short keeper margin
    (CalcInputs(), "MP_contention", "Man_t_DZ", 900),
    (CalcInputs(), "MP_setup", "Man_t_DD", 380),       # a narrow column
    (CalcInputs(), "PP_contention", _WHOLE, 900),      # Peripherals A and B driving
    (_KEEPER, "keeper_Man", "Man_t_EDE", 900),         # an EDE release's 80 % point
    (_KEEPER, "keeper_Per", "t_RF Per DATA", 900),     # a guide other than the margin
)


def _app():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def _diagram():
    _app()
    from swi3s_studio.ui.inequality_diagram import InequalityDiagram
    from swi3s_studio.ui.timing_view import _FONT_DIAGRAM, _base_font, _subscript, _term_key
    d = InequalityDiagram(_subscript, _term_key)
    font = _base_font()
    font.setPixelSize(_FONT_DIAGRAM)
    d.setFont(font)
    return d


def _show(d, inp, leg, key, width):
    secs = support.sections(inp, leg)
    d.resize(width, 600)
    if key == _WHOLE:
        d.show_inequality(leg, secs)
    else:
        assert key in support.keys(secs), (leg, key)
        d.show_term(leg, key, secs)
    d.resize(width, d.minimumHeight())


def _image(d) -> str:
    """A hash of the diagram's rendered pixels. The image is held while its buffer is read:
    `constBits` does not keep a temporary image alive."""
    img = d.grab().toImage()
    return hashlib.sha1(bytes(img.constBits())).hexdigest()


@pytest.fixture
def dark():
    from swi3s_studio.ui.theme import apply_palette
    apply_palette("dark")
    yield
    apply_palette("dark")


# ---- the defaults ------------------------------------------------------------------------

def test_the_defaults_are_the_values_of_record():
    """Every setting defaults to its value of record, the not-maximized scale to 0.7, and
    every colour to its theme token, in both themes."""
    from swi3s_studio.ui import diagram_style as ds
    from swi3s_studio.ui.theme import _PALETTES
    assert {k: ds.default(k) for k in ds.VALUES} == _RECORD_VALUES
    for mode in ("dark", "light"):
        assert {k: ds.color(k, mode) for k in ds.COLOR_KEYS} == {
            k: _PALETTES[mode][t].lower() for k, t in _RECORD_COLORS.items()}


class _Pens:
    """A painter that records each pen set on it, its colour, width and style."""

    def __init__(self, painter, pens):
        self._painter, self.pens = painter, pens

    def setPen(self, pen):         # noqa: N802 - QPainter's name
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QPen
        if isinstance(pen, QPen) and pen.style() != Qt.NoPen:
            self.pens.append((pen.color().name(), pen.widthF(), pen.style()))
        return self._painter.setPen(pen)

    def __getattr__(self, name):
        return getattr(self._painter, name)


def test_the_defaults_draw_only_pens_of_record(monkeypatch, dark):
    """With the default settings every line the diagrams draw is a pen of record: a theme
    token's colour, a width of record in line heights and the style of record; text and
    leader lines in the label's colour take the leader's width."""
    from PySide6.QtCore import Qt

    from swi3s_studio.ui.theme import VizTheme
    from swi3s_studio.ui.timing_diagram import TimingDiagram
    t = {k: getattr(VizTheme, k).lower() for k in set(_RECORD_COLORS.values())}
    solid, dot, dash = Qt.SolidLine, Qt.DotLine, Qt.DashLine
    w3, w2 = round(_W3, 6), round(_W2, 6)
    record = {
        (t["DIAG_CLOCK"], w3, solid), (t["DIAG_CLOCK"], w3, dot),
        (t["DIAG_DATA"], w3, solid), (t["DIAG_DATA"], w3, dot),
        (t["DIAG_DATA_PER"], w3, solid), (t["DIAG_DATA_PER"], w3, dot),
        (t["DIAG_DATA_PER_B"], w3, solid), (t["DIAG_DATA_PER_B"], w3, dot),
        (t["DIAG_DIM"], w2, dot), (t["DIAG_GROUND"], w2, dash), (t["DIAG_GROUND"], w2, solid),
        (t["DIAG_OK"], w2, dash), (t["DIAG_VALID"], w2, dash),
        (t["DIAG_LEVEL"], w2, solid), (t["DIAG_GUIDE"], w2, dot),
        (t["DIAG_GUIDE"], w2, dash), (t["DIAG_GUIDE"], w2, solid), (t["DIAG_TEXT"], w3, solid),
        (t["DIAG_DIM"], w3, solid), (t["DIAG_OK"], w3, solid), (t["DIAG_BAD"], w3, solid),
    }
    pens: list = []
    real = TimingDiagram._paint_panel
    monkeypatch.setattr(TimingDiagram, "_paint_panel", lambda self, p, *a: real(
        self, _Pens(p, pens) if not isinstance(p, _Pens) else p, *a))
    d = _diagram()
    for case in _DRAWS_ALL:
        _show(d, *case)
        pens.clear()
        d.grab()
        lh = d._lh()
        wrong = set()
        for colour, width, style in pens:
            w = round(width / lh, 6)
            if width == 0.0 or w == 0.06:          # text, and a label's leader line
                continue
            if (colour, w, style) not in record:
                wrong.add((colour, w, style))
        assert pens and not wrong, (case[1:3], wrong)


def _driver_key(r):
    """The colour key of the device driving data ramp `r`: the Manager, Peripheral B, or a
    Peripheral or Peripheral A."""
    if r.driver == "Man":
        return "data"
    return "data_per_b" if r.device == "Per B" else "data_per"


@pytest.mark.parametrize("leg", ["PM_contention", "PP_contention", "MP_contention",
                                 "PM_setup_ho", "MP_setup_ho", "keeper_Per"])
def test_a_data_edge_is_drawn_in_its_driving_devices_colour(monkeypatch, dark, leg):
    """In the term diagram of every term and in the whole view, each data edge is drawn in
    the colour of the device driving it, at every pin it is seen: the Manager's in the data
    colour, a Peripheral's or Peripheral A's in data_per, Peripheral B's in data_per_b; the
    keeper-held stretch after a release in the releasing device's colour, dotted. Each
    contention leg draws the two devices' data in two colours."""
    from PySide6.QtCore import Qt

    from swi3s_studio.ui import diagram_style as ds
    from swi3s_studio.ui.timing_diagram import TimingDiagram
    traced, pens = [], []
    real_trace = TimingDiagram._trace

    def trace(self, p, pn, level, t0, t1, knee, level_y, pen):
        traced.append((level.__self__, pen.color().name()))
        return real_trace(self, p, pn, level, t0, t1, knee, level_y, pen)
    monkeypatch.setattr(TimingDiagram, "_trace", trace)
    real = TimingDiagram._paint_panel
    monkeypatch.setattr(TimingDiagram, "_paint_panel", lambda self, p, *a: real(
        self, _Pens(p, pens) if not isinstance(p, _Pens) else p, *a))
    d = _diagram()
    secs = support.sections(CalcInputs(), leg)
    for key in (_WHOLE, *support.keys(secs)):
        _show(d, CalcInputs(), leg, key, 900)
        d.grab()
    data = [(r, c) for r, c in traced if r.signal == "data"]
    assert data
    for r, colour in data:
        assert colour == ds.color(_driver_key(r)), (leg, r.key, r.pin, colour)
    drivers = {_driver_key(r) for r, _ in data}
    if leg.endswith("contention"):
        assert len(drivers) == 2, (leg, drivers)
        # The acquirer's drive is drawn in its own colour at the releaser's pin too.
        rel_pin = {"PM_contention": "Peripheral", "PP_contention": "Peripheral A",
                   "MP_contention": "Manager"}[leg]
        assert len({_driver_key(r) for r, _ in data if r.pin == rel_pin}) == 2, leg
    released = {_driver_key(r) for r, _ in data if r.off_ns is not None}
    dotted = {c for c, _w, style in pens if style == Qt.DotLine}
    for key in released:
        assert ds.color(key) in dotted, (leg, key)


# The device that last drove the wire before each turn-on: on a handover leg the other
# device, on contention the releaser.
_LAST_DRIVER = {**{f"MP_{k}_ho": "Per" for k in ("setup", "hold")},
                **{f"PM_{k}_ho": "Man" for k in ("setup", "hold")},
                **{f"PP_{k}_ho": "Per B" for k in ("setup", "hold")},
                "MP_contention": "Man", "PM_contention": "Per", "PP_contention": "Per A"}


@pytest.mark.parametrize("leg", sorted(_LAST_DRIVER))
def test_a_turn_ons_undriven_stretch_is_in_the_last_drivers_colour(monkeypatch, dark, leg):
    """Every turn-on names the device that last drove the wire (`Ramp.before`), and the
    undriven stretch before it, in the term diagram of every term and the whole view, is
    dotted in that device's colour: MP_setup_ho's in the Peripheral's, PM_setup_ho's in the
    Manager's, a contention leg's acquirer's in the releaser's."""
    from swi3s_studio.timing.timeline import leg_timeline
    from swi3s_studio.ui import diagram_style as ds
    from swi3s_studio.ui.timing_diagram import TimingDiagram
    keys = {"Man": "data", "Per": "data_per", "Per A": "data_per", "Per B": "data_per_b"}
    turn_ons = [r for r in leg_timeline(CalcInputs(), leg).ramps if r.from_hiz]
    assert turn_ons and all(r.before == _LAST_DRIVER[leg] for r in turn_ons), leg
    drawn = []
    real = TimingDiagram._high_z

    def high_z(self, p, pn, level, t0, t1, level_y, pen, at_end=False):
        if at_end:
            drawn.append(pen.color().name())
        return real(self, p, pn, level, t0, t1, level_y, pen, at_end)
    monkeypatch.setattr(TimingDiagram, "_high_z", high_z)
    d = _diagram()
    secs = support.sections(CalcInputs(), leg)
    for key in (_WHOLE, *support.keys(secs)):
        _show(d, CalcInputs(), leg, key, 900)
        d.grab()
    assert drawn and set(drawn) == {ds.color(keys[_LAST_DRIVER[leg]])}, (leg, set(drawn))


def test_the_driver_colours_have_swatches_in_both_themes_and_round_trip(tmp_path):
    """The Preferences tab sets each driving device's data colour per theme; a change is
    stored, read back after a restart, and written to and read from a settings file."""
    _app()
    from swi3s_studio.ui import diagram_style as ds
    from swi3s_studio.ui.diagram_style_page import DiagramStylePage
    keys = ("data", "data_per", "data_per_b")
    page = DiagramStylePage(ds.preferences())
    for mode in ("dark", "light"):
        for key in keys:
            assert (mode, key) in page._swatches, (mode, key)
    page.set_color("light", "data_per", "#123456")
    page.set_color("dark", "data_per_b", "#654321")
    ds.set_preferences(page.preferences())
    ds.reset_cache()
    assert ds.color("data_per", "light") == "#123456"
    assert ds.color("data_per_b", "dark") == "#654321"
    assert ds.color("data_per", "dark") == ds.default_color("data_per", "dark")
    path = tmp_path / "style.json"
    page.export_settings(str(path))
    fresh = DiagramStylePage({"colors": {}, "values": {}})
    assert fresh.import_settings(str(path)) == []
    assert fresh.color("light", "data_per") == "#123456"
    assert fresh.color("dark", "data_per_b") == "#654321"
    distinct = {ds.default_color(k, m) for m in ("dark", "light") for k in keys}
    assert len(distinct) == 6


def _changed(key):
    """A value of `key` other than its default."""
    from PySide6.QtGui import QFontDatabase

    from swi3s_studio.ui import diagram_style as ds
    dflt, kind, lo, hi = ds.VALUES[key]
    if kind == "dash":
        return "dashdot"
    if kind == "font_weight":
        return "normal" if dflt != "normal" else "bold"
    if kind == "family":
        own = _diagram().font().family()
        others = [f for f in QFontDatabase.families() if f != own]
        return others[0] if others else None
    if kind == "scale":
        return 0.6
    return min(dflt * 2.0, hi)


def test_each_setting_changes_what_is_drawn(dark):
    """Every colour (for the theme drawn) and every weight, dash, size and font
    setting changes the rendered image of at least one of the diagrams that draw every
    element; the not-maximized scale is pinned by its own test. A font family is checked
    where the platform has a second family."""
    from swi3s_studio.ui import diagram_style as ds
    d = _diagram()
    base = []
    for case in _DRAWS_ALL:
        _show(d, *case)
        base.append(_image(d))
    settings = ([("colors", k, "#ff00ff") for k in ds.COLOR_KEYS]
                + [("values", k, _changed(k)) for k in ds.VALUES])
    unchanged = []
    for section, key, value in settings:
        if value is None:
            continue
        ds.set_preview({"colors": {"dark": {key: value}}} if section == "colors"
                       else {"values": {key: value}})
        d.restyle()
        hit = False
        for case, before in zip(_DRAWS_ALL, base):
            _show(d, *case)
            if _image(d) != before:
                hit = True
                break
        if not hit:
            unchanged.append(key)
        ds.set_preview(None)
        d.restyle()
    assert not unchanged, unchanged


def test_a_colour_is_per_theme(dark):
    """The dark theme's clock colour draws in the dark theme and not on a light export,
    which draws the light theme's."""
    from PySide6.QtGui import QImage

    from swi3s_studio.ui import diagram_style as ds
    from swi3s_studio.ui.theme import _PALETTES
    ds.set_preferences({"colors": {"dark": {"clock": "#ff00ff"}}})
    d = _diagram()
    _show(d, CalcInputs(), "MP_setup", "Man_t_DD", 900)

    def colours(img):
        return {img.pixelColor(x, y).name() for x in range(0, img.width(), 2)
                for y in range(0, img.height(), 2)}
    dark_img, light_img = (QImage.fromData(d.export("png", 1.0, light=lt))
                           for lt in (False, True))
    assert "#ff00ff" in colours(dark_img)
    assert "#ff00ff" not in colours(light_img)
    assert _PALETTES["light"]["DIAG_CLOCK"].lower() in colours(light_img)
    assert ds.color("clock", "light") == _PALETTES["light"]["DIAG_CLOCK"].lower()


# ---- persistence -------------------------------------------------------------------------

def test_the_settings_persist_and_a_default_is_stored_as_unset():
    from PySide6.QtCore import QSettings

    from swi3s_studio.ui import diagram_style as ds
    ds.set_preferences({"colors": {"light": {"axis": "#AA0000",
                                             "clock": ds.default_color("clock", "light")}},
                        "values": {"clock.weight": 0.3, "vdd.dash": "dot"}})
    stored = json.loads(QSettings().value(ds.STYLE_KEY))
    assert stored == {"colors": {"light": {"axis": "#aa0000"}},
                      "values": {"clock.weight": 0.3}}
    ds.reset_cache()
    assert ds.color("axis", "light") == "#aa0000" and ds.weight("clock") == 0.3


def test_a_malformed_stored_setting_falls_back_to_its_default():
    from PySide6.QtCore import QSettings

    from swi3s_studio.ui import diagram_style as ds
    QSettings().setValue(ds.STYLE_KEY, json.dumps(
        {"colors": {"dark": {"clock": "blue"}}, "values": {"vdd.dash": "wavy",
                                                          "axis.weight": "x"}}))
    ds.reset_cache()
    assert ds.color("clock", "dark") == ds.default_color("clock", "dark")
    assert ds.dash("vdd") == "dot" and ds.weight("axis") == ds.default("axis.weight")
    QSettings().setValue(ds.STYLE_KEY, "not json")
    ds.reset_cache()
    assert ds.preferences() == {"colors": {}, "values": {}}


# ---- the dialog --------------------------------------------------------------------------

@pytest.fixture
def win(monkeypatch):
    _app()
    from swi3s_studio.ui.main_window import MainWindow
    from swi3s_studio.ui.mode_controller import TIMING
    monkeypatch.setattr("swi3s_studio.ui.theme_controller.save_preference", lambda _p: None)
    w = MainWindow()
    w._mode_mgr.switch_to(TIMING)
    w._timing_page.flush_recompute()
    yield w
    w.close()


def _diagram_action(w):
    return next(a for a in w._view_menu.actions()
                if a.text().replace("&", "") == "Diagram Colors, Lines and Fonts…")


def test_the_view_menu_item_is_the_timing_calculators_and_opens_the_diagram_tab(
        win, monkeypatch):
    from swi3s_studio.ui.mode_controller import ANALYSIS
    from swi3s_studio.ui.preferences_dialog import PreferencesDialog
    opened = []
    monkeypatch.setattr(PreferencesDialog, "exec",
                        lambda dlg: opened.append(dlg._tabs.currentIndex()) or 0)
    act = _diagram_action(win)
    assert act.isVisible() and act.isEnabled()
    act.trigger()
    assert opened == [PreferencesDialog.DIAGRAM_TAB]
    win._mode_mgr.switch_to(ANALYSIS)
    assert not act.isVisible() and not act.isEnabled()


def _diagram_title_px(win):
    return win._timing_page._diagram._title_font().pixelSize()


@pytest.mark.parametrize("accepted", [True, False])
def test_an_edit_redraws_the_diagram_and_ok_keeps_it_cancel_restores(
        win, monkeypatch, accepted):
    """While the dialog is open an edit is drawn at once; OK stores it, Cancel puts the
    stored settings back."""
    from PySide6.QtCore import QSettings

    from swi3s_studio.ui import diagram_style as ds
    from swi3s_studio.ui.preferences_dialog import PreferencesDialog
    before = _diagram_title_px(win)
    seen = {}

    def run(dlg):
        page = dlg.diagram_page
        page.set_color("dark", "clock", "#ff00ff")
        page.set_value("font.title.size", 1.5)
        seen["colour"] = ds.color("clock", "dark")
        seen["title"] = _diagram_title_px(win)
        return 1 if accepted else 0
    monkeypatch.setattr(PreferencesDialog, "exec", run)
    _diagram_action(win).trigger()
    assert seen["colour"] == "#ff00ff"
    assert seen["title"] > before                   # the diagram restyled at once
    if accepted:
        ds.reset_cache()                            # read back from QSettings
        assert ds.color("clock", "dark") == "#ff00ff"
        assert ds.font("title")[1] == 1.5 and _diagram_title_px(win) == seen["title"]
    else:
        assert QSettings().value(ds.STYLE_KEY) is None
        assert ds.color("clock", "dark") == ds.default_color("clock", "dark")
        assert _diagram_title_px(win) == before


def test_reset_to_defaults_resets_the_tab_shown():
    _app()
    from swi3s_studio.ui import diagram_style as ds
    from swi3s_studio.ui import line_style
    from swi3s_studio.ui.preferences_dialog import PreferencesDialog
    ds.set_preferences({"values": {"span.weight": 0.3}})
    dlg = PreferencesDialog(line_style.preferences())
    page = dlg.diagram_page
    assert page.value("span.weight") == 0.3
    page.set_color("light", "axis", "#123456")
    dlg.set_value("dark", "dp", 0, "#654321")
    dlg.show_diagram_tab()
    assert dlg._weights.isHidden()
    dlg._reset_btn.click()
    assert dlg.diagram_preferences() == {"colors": {}, "values": {}}
    assert dlg.preferences()["colors"]["dark"]["dp"][0] == "#654321"     # kept
    spin = page._spins["span.weight"]
    assert spin.value() == pytest.approx(ds.default("span.weight") * page._lh, abs=0.01)
    dlg._tabs.setCurrentIndex(0)
    dlg._reset_btn.click()
    assert dlg.preferences() == {"colors": {}, "weights": {}}


def test_a_low_contrast_colour_is_flagged_and_no_default_is():
    _app()
    from swi3s_studio.ui import diagram_style as ds
    from swi3s_studio.ui.diagram_style_page import DiagramStylePage
    page = DiagramStylePage(ds.preferences())
    assert not any(page.low_contrast(m, k) for m, k in page._swatches)
    page.set_color("light", "guide_dot", "#eeeeee")
    assert page.low_contrast("light", "guide_dot")
    assert page._warnings[("light", "guide_dot")].text().startswith("⚠")
    assert not page.low_contrast("dark", "guide_dot")


def test_a_spin_shows_px_and_stores_a_fraction_of_the_line_height():
    _app()
    from swi3s_studio.ui import diagram_style as ds
    from swi3s_studio.ui.diagram_style_page import DiagramStylePage
    page = DiagramStylePage(ds.preferences(), units=(20.0, 16))
    got = []
    page.changed.connect(got.append)
    assert page._spins["clock.weight"].value() == pytest.approx(round(_W3 * 20.0, 2))
    assert page._spins["font.title.size"].value() == round(1.12 * 16)
    page._spins["clock.weight"].setValue(5.0)
    page._spins["font.row.size"].setValue(20)
    page._spins["label.narrow"].setValue(60)
    page._combos["vdd.dash"].setCurrentIndex(page._combos["vdd.dash"].findData("dashdot"))
    assert got[-1]["values"] == {"clock.weight": 0.25, "font.row.size": 1.25,
                                 "label.narrow": 0.6, "vdd.dash": "dashdot"}


# ---- files -------------------------------------------------------------------------------

def test_export_and_import_round_trip(tmp_path):
    _app()
    from swi3s_studio.ui import diagram_style as ds
    from swi3s_studio.ui.diagram_style_page import DiagramStylePage
    page = DiagramStylePage(ds.preferences())
    page.set_color("dark", "margin_bad", "#ff0000")
    page.set_value("threshold.dash", "dashdot")
    page.set_value("font.axis.family", "Courier")
    page.set_value("dot.radius", 0.4)
    path = tmp_path / "style.json"
    page.export_settings(str(path))
    held = json.loads(path.read_text())
    assert held["format"] == ds.FILE_FORMAT and held["version"] == ds.FILE_VERSION
    assert set(held["values"]) == set(ds.VALUES)
    assert all(set(held["colors"][m]) == set(ds.COLOR_KEYS) for m in ("dark", "light"))
    fresh = DiagramStylePage({"colors": {}, "values": {}})
    assert fresh.import_settings(str(path)) == []
    assert fresh.preferences() == ds._strip_defaults(ds._clean(page.preferences()))
    assert fresh.value("dot.radius") == 0.4 and fresh.color("dark", "margin_bad") == "#ff0000"


@pytest.mark.parametrize("section,key,old", [
    ("values", "margin.band", 0.3), ("values", "unmax_scale", 0.6),
    ("values", "connector.weight", 0.2), ("values", "connector.dash", "dot"),
    ("colors", "connector", "#123456")])
def test_a_retired_setting_is_dropped_without_a_warning(tmp_path, section, key, old):
    """The margin band's opacity, the not-maximized scale and the flight connector's colour,
    weight and dash are no settings: the Preferences tab has no row for any, and a stored or
    imported one is dropped with no import warning, the rest of the settings read as
    before."""
    _app()
    from PySide6.QtCore import QSettings

    from swi3s_studio.ui import diagram_style as ds
    from swi3s_studio.ui.diagram_style_page import LINE_ROWS, DiagramStylePage
    assert key in ds.RETIRED and key not in ds.VALUES and key not in ds.COLOR_KEYS
    assert not any(key in row[1:5] or (row[2] and key == f"{row[2]}.weight")
                   or (row[3] and key == f"{row[3]}.dash") for row in LINE_ROWS)
    if key == "margin.band":
        assert not any("band" in row[0].lower() for row in LINE_ROWS)
    if key.startswith("connector"):
        assert not any("connector" in row[0].lower() for row in LINE_ROWS)
    assert key not in DiagramStylePage(ds.preferences())._spins

    def holding(body: dict) -> dict:
        if section == "colors":
            body.setdefault("colors", {}).setdefault("dark", {})[key] = old
        else:
            body.setdefault("values", {})[key] = old
        return body
    QSettings().setValue(ds.STYLE_KEY, json.dumps(holding({"values": {"clock.weight": 0.3}})))
    ds.reset_cache()
    assert ds.preferences() == {"colors": {}, "values": {"clock.weight": 0.3}}
    path = tmp_path / "old.json"
    body = holding(json.loads(ds.to_json({})))
    body["values"]["clock.weight"] = 0.25
    path.write_text(json.dumps(body))
    fresh = DiagramStylePage({"colors": {}, "values": {}})
    assert fresh.import_settings(str(path)) == []
    got = fresh.preferences()
    assert key not in got["values"] and key not in got["colors"].get("dark", {})
    assert got["values"]["clock.weight"] == 0.25


def test_a_malformed_import_is_reported_as_import_warnings(tmp_path, monkeypatch):
    _app()
    from PySide6.QtWidgets import QFileDialog, QMessageBox

    from swi3s_studio.ui import diagram_style as ds
    from swi3s_studio.ui import line_style
    from swi3s_studio.ui.import_warnings_dialog import ImportWarningsDialog
    from swi3s_studio.ui.preferences_dialog import PreferencesDialog
    path = tmp_path / "bad.json"
    path.write_text(json.dumps({
        "colors": {"dark": {"clock": "blue", "nope": "#000000", "axis": "#00FF00"},
                   "sepia": {}},
        "values": {"clock.weight": 9, "vdd.dash": "wavy", "bogus": 1, "span.head": True},
        "extra": 1}))
    dlg = PreferencesDialog(line_style.preferences())
    page = dlg.diagram_page
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *a, **k: (str(path), ""))
    page._ask_import()
    shown = [w for w in dlg.findChildren(ImportWarningsDialog)]
    assert len(shown) == 1
    text = [str(n) for n in shown[0].warnings]
    assert len(text) == 8 and all(t.startswith("bad.json: ") for t in text)
    assert any("clock.weight = 9 is outside" in t for t in text)
    assert page.preferences() == {"colors": {"dark": {"axis": "#00ff00"}},
                                  "values": {"clock.weight": ds.VALUES["clock.weight"][3]}}
    failed = []
    monkeypatch.setattr(QMessageBox, "critical", lambda *a: failed.append(a[2]))
    path.write_text("[1, 2]")
    page._ask_import()
    path.write_text("{not json")
    page._ask_import()
    assert len(failed) == 2 and all(f.startswith("Import failed: ") for f in failed)
    assert page.value("clock.weight") == ds.VALUES["clock.weight"][3]     # unchanged


# ---- the not-maximized scale -------------------------------------------------------------

def test_the_scale_applies_only_while_the_diagram_is_not_maximized():
    """Beside the inequalities the diagram draws at `REDUCED_SCALE` (0.7), a fixed value and
    no setting; maximized, at its size."""
    _app()
    from swi3s_studio.ui import diagram_style as ds
    from swi3s_studio.ui.diagram_style_page import DiagramStylePage
    from swi3s_studio.ui.timing_diagram import REDUCED_SCALE
    from swi3s_studio.ui.timing_view import TimingView
    assert REDUCED_SCALE == 0.7 and "unmax_scale" not in ds.VALUES
    assert "unmax_scale" not in DiagramStylePage(ds.preferences())._spins
    tv = TimingView()
    tv.flush_recompute()
    d = tv._diagram
    full = d.units()[0]
    assert d.reduced and d.style_scale() == REDUCED_SCALE
    assert d._lh() < full
    small = d._title_font().pixelSize()
    tv.set_diagram_maximized(True)
    assert not d.reduced and d.style_scale() == 1.0 and d._lh() == full
    assert d._title_font().pixelSize() > small
    tv.set_diagram_maximized(False)
    assert d.style_scale() == REDUCED_SCALE and d._lh() < full
    from swi3s_studio.ui.timing_diagram import TimingDiagram
    assert not TimingDiagram(lambda s: s, lambda s: s).reduced    # a diagram on its own


# ---- layout with other fonts and weights -------------------------------------------------

_LARGER = {"values": {"font.row.size": 1.3, "font.span.size": 1.25, "font.level.size": 1.2,
                      "font.axis.size": 1.2, "font.heading.size": 1.3, "font.title.size": 1.4,
                      "font.note.size": 1.0, "font.level.weight": "bold",
                      "clock.weight": 0.4, "data.weight": 0.4, "margin.weight": 0.6,
                      "threshold.head": 0.6, "dot.radius": 0.4, "release.length": 1.0}}


@pytest.mark.parametrize("width", (380, 900))
def test_larger_fonts_and_heavier_lines_lay_out_without_overlaps(monkeypatch, width):
    """With larger fonts and heavier lines, over the legs and terms of the sweep's `cover`
    at the default inputs, stacked at `width`, no text overlaps another or a line and all
    stays in its plot (`test_timing_diagram_layout._assert_no_clashes`)."""
    from test_timing_diagram_layout import _assert_no_clashes
    from test_timing_diagram_layout import _diagram as layout_diagram

    from swi3s_studio.ui import diagram_style as ds
    ds.set_preview(_LARGER)
    pairs = set(support.part_of(support.pairs(CalcInputs()), "cover"))
    _assert_no_clashes(monkeypatch, layout_diagram(),
                       lambda leg: support.sections(CalcInputs(), leg),
                       (f"stacked {width}", width, False, ()), only=pairs)


def test_larger_fonts_and_heavier_lines_lay_out_without_overlaps_drawn_whole(monkeypatch,
                                                                          request):
    """The same over every leg drawn whole, at the default columns, stacked at 380 px."""
    from test_inequality_diagram import (
        _CASES,
    )
    from test_inequality_diagram import (
        test_no_text_overlaps_another_or_a_line_and_the_footnote_is_under_the_panels as check,
    )

    from swi3s_studio.ui import diagram_style as ds
    ds.set_preview(_LARGER)
    name, spec, example = _CASES[0]
    check(monkeypatch, request, ("stacked 380", 380, False, ()), name, spec, example)
