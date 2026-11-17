"""Line colours are readable in both themes, and follow a theme switch.

The Audio waveforms, the Capture pane's lane overlays and the bookmark pairs take the
theme's DP_LINE_PALETTE: the Bus Grid's data-port PASTELS (cell fills made for dark ink)
with the same hues darkened for light mode. The grid keeps its fills. These pin:

- every line colour clears 3:1 (WCAG 1.4.11, graphical objects) on its theme's PLOT_BG;
- the line palette keeps the fill palette's hues, in order, so a stream is recognisably one
  colour in the grid and the waveforms, and no two line colours are closer than the fills;
- the panes read the palette at use: a pane built in light mode draws light, and a live
  switch recolours pens and the cached bookmark lines;
- the audio channel labels, in the legible shade of their trace's colour, clear 3:1 on the
  channel list in both themes and after a live switch, and the traces keep the colour;
- the plot gridlines are at least as visible in light mode as in dark. They are the axis
  grey at the theme's PLOT_GRID_ALPHA opacity.
"""
import colorsys
import itertools
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication

from swi3s_studio.ui import theme as t
from swi3s_studio.ui.grid_view import (
    _DP_PALETTE,
    bookmark_pair_color,
    dp_line_color,
    dp_stream_color,
)
from swi3s_studio.ui.theme import apply_palette

pytestmark = pytest.mark.gui

# Palette keys drawn as lines or marks on PLOT_BG. A list key's entries are all checked, so
# a colour added to one is covered without editing this.
_LINE_KEYS = ("DP_LINE_PALETTE", "TRACE_PALETTE", "RAW_DP", "RAW_DN", "CURSOR", "SEM_SYNC")
_MIN_CONTRAST = 3.0


def _rgb(hex_):
    h = hex_.lstrip("#")
    return tuple(int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))


def _lin(c):
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def _luminance(hex_):
    r, g, b = map(_lin, _rgb(hex_))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _contrast(a, b):
    hi, lo = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def _lab(hex_):
    r, g, b = map(_lin, _rgb(hex_))
    x = (0.4124 * r + 0.3576 * g + 0.1805 * b) / 0.95047
    y = 0.2126 * r + 0.7152 * g + 0.0722 * b
    z = (0.0193 * r + 0.1192 * g + 0.9505 * b) / 1.08883

    def f(v):
        return v ** (1 / 3) if v > 0.008856 else 7.787 * v + 16 / 116
    return 116 * f(y) - 16, 500 * (f(x) - f(y)), 200 * (f(y) - f(z))


def _delta_e(a, b):
    return sum((p - q) ** 2 for p, q in zip(_lab(a), _lab(b))) ** 0.5


def _min_delta_e(palette):
    return min(_delta_e(a, b) for a, b in itertools.combinations(palette, 2))


def _hue_deg(hex_):
    return colorsys.rgb_to_hls(*_rgb(hex_))[0] * 360


def _line_colours(pal):
    for key in _LINE_KEYS:
        val = pal[key]
        for i, c in enumerate(val if isinstance(val, list) else [val]):
            yield f"{key}[{i}]" if isinstance(val, list) else key, c


@pytest.mark.parametrize("mode", ["dark", "light"])
def test_every_line_colour_clears_3_to_1_on_the_plot(mode):
    pal = t._PALETTES[mode]
    low = [(name, c, round(_contrast(c, pal["PLOT_BG"]), 2))
           for name, c in _line_colours(pal) if _contrast(c, pal["PLOT_BG"]) < _MIN_CONTRAST]
    assert not low, f"{mode}: below {_MIN_CONTRAST}:1 on PLOT_BG {pal['PLOT_BG']}: {low}"


def test_the_fills_themselves_fail_on_white():
    """The premise: the pastels are not line colours in light mode, which is why lines
    have their own palette. If this ever passes, the split is no longer needed."""
    worst = min(_contrast(c, t._LIGHT["PLOT_BG"]) for c in _DP_PALETTE)
    assert worst < 1.1


@pytest.mark.parametrize("mode", ["dark", "light"])
def test_the_line_palette_keeps_the_fill_hues_in_order(mode):
    line = t._PALETTES[mode]["DP_LINE_PALETTE"]
    assert len(line) == len(_DP_PALETTE)
    for i, (fill, ln) in enumerate(zip(_DP_PALETTE, line)):
        d = abs(_hue_deg(fill) - _hue_deg(ln)) % 360
        assert min(d, 360 - d) <= 3.0, f"{mode} entry {i}: {fill} -> {ln} changes hue"


@pytest.mark.parametrize("mode", ["dark", "light"])
def test_no_two_line_colours_are_closer_than_the_fills(mode):
    """Darkening keeps hue, and three pastels share a hue with a paler twin, so a uniform
    darkening would collapse each twin pair onto one colour. The line palette must tell its
    entries apart at least as well as the grid's fills do."""
    line = t._PALETTES[mode]["DP_LINE_PALETTE"]
    assert _min_delta_e(line) >= _min_delta_e(_DP_PALETTE) - 0.5


_GRID_GREY = "#969696"          # pyqtgraph's default axis pen, which paints the grid


def _blend(fg, bg, alpha):
    mix = [round(b * 255 + (f - b) * 255 * alpha) for f, b in zip(_rgb(fg), _rgb(bg))]
    return "#%02x%02x%02x" % tuple(mix)


def _grid_contrast(mode, scale=1.0):
    pal = t._PALETTES[mode]
    grid = _blend(_GRID_GREY, pal["PLOT_BG"], scale * pal["PLOT_GRID_ALPHA"])
    return _contrast(grid, pal["PLOT_BG"])


def test_the_light_grid_is_at_least_as_visible_as_the_dark_one():
    """Light-mode gridlines are held to dark's contrast, for the trace panes and the Timing
    histograms (drawn at three quarters of the opacity)."""
    from swi3s_studio.ui.eye_view import _grid_alpha
    for scale in (1.0, _grid_alpha() / t.VizTheme.PLOT_GRID_ALPHA):
        assert _grid_contrast("light", scale) >= _grid_contrast("dark", scale), scale
    assert _grid_contrast("light") < 2.0          # a grid, not a second set of traces


def _grid_alphas(win):
    """The grid opacity (0-255) each plot's axes paint with, after the last switch."""
    out = {}
    for i, (plot, *_r) in enumerate(win._audio_view._plots):
        out[f"audio{i}"] = (plot.getAxis("bottom").grid, plot.getAxis("left").grid)
    out["capture"] = (win._raw_view._plot.getAxis("bottom").grid,)
    eye = win._eye_view
    out["setup"] = (eye._setup_plot.getAxis("bottom").grid, eye._setup_plot.getAxis("left").grid)
    out["hold"] = (eye._hold_plot.getAxis("bottom").grid, eye._hold_plot.getAxis("right").grid)
    return out


def test_the_grid_keeps_the_fill_and_lines_take_the_theme():
    apply_palette("light")
    try:
        for dev, dp in ((0, 0), (1, 2), (3, 31)):
            assert dp_stream_color(dev, dp).name() in [c.lower() for c in _DP_PALETTE]
            assert dp_line_color(dev, dp).name() in [c.lower() for c in t._LIGHT["DP_LINE_PALETTE"]]
            assert dp_line_color(dev, dp) != dp_stream_color(dev, dp)
        assert bookmark_pair_color("A").name() == t._LIGHT["DP_LINE_PALETTE"][0]
    finally:
        apply_palette("dark")
    assert dp_line_color(1, 2).name() == dp_stream_color(1, 2).name()   # dark: the pastels


def test_a_capture_pane_built_in_light_mode_draws_light():
    """app.py imports the UI before it applies the saved theme, so the pane must read its
    colours at construction, not at import: a pane built in light mode draws the DP/DN
    traces and cursor in light-theme colours."""
    QApplication.instance() or QApplication([])
    from swi3s_studio.ui.raw_view import RawCaptureView
    apply_palette("light")
    try:
        view = RawCaptureView()
        assert view._dp_curve.opts["pen"].color().name() == t._LIGHT["RAW_DP"]
        assert view._dn_curve.opts["pen"].color().name() == t._LIGHT["RAW_DN"]
        assert view._cursor.pen.color().name() == t._LIGHT["CURSOR"]
    finally:
        apply_palette("dark")


@pytest.fixture
def win():
    QApplication.instance() or QApplication([])
    from swi3s_studio.ui.main_window import MainWindow
    apply_palette("dark")
    w = MainWindow()
    w.load_demo()
    s0 = w._session.commands[2]["start_sample"]
    w._bookmarks.add(s0, link=0)
    w._bookmarks.add(s0 + 50_000, link=0)
    w._push_bookmarks()
    yield w
    apply_palette("dark")


def _pens(win):
    return {(dev, dp, ch): curve.opts["pen"].color().name()
            for _p, curve, dev, dp, ch, _n in win._audio_view._plots}


def _bookmark_line_colours(lines):
    return {key: (ln.pen.color().name(), ln.label.color.name()) for key, ln in lines.items()}


def test_a_live_switch_recolours_the_waveforms_and_bookmark_lines(win, monkeypatch):
    from swi3s_studio.ui import theme as theme_mod
    monkeypatch.setattr(theme_mod, "save_preference", lambda _p: None)
    monkeypatch.setattr("swi3s_studio.ui.theme_controller.save_preference", lambda _p: None)
    assert win._audio_view._plots and win._raw_view._bm_lines and win._audio_view._bm_lines
    for mode in ("light", "dark"):
        win.apply_theme(mode)
        pal = t._PALETTES[mode]["DP_LINE_PALETTE"]
        for (dev, dp, _ch), colour in _pens(win).items():
            assert colour == dp_line_color(dev, dp).name(), (mode, dev, dp)
            assert colour in [c.lower() for c in pal]
        for (dev, dp, ch), cb in win._audio_view._checks.items():
            assert t.legible_text_color(dp_line_color(dev, dp), t.VizTheme.FRAME_BG) \
                in cb.styleSheet()
        for label, (pen, text) in _bookmark_line_colours(win._raw_view._bm_lines).items():
            assert pen == text == bookmark_pair_color(label).name(), (mode, label)
        for (_pid, label), (pen, text) in _bookmark_line_colours(win._audio_view._bm_lines).items():
            assert pen == text == bookmark_pair_color(label).name(), (mode, label)
        from swi3s_studio.ui.eye_view import _grid_alpha
        trace, hist = t._PALETTES[mode]["PLOT_GRID_ALPHA"], _grid_alpha()
        for name, grids in _grid_alphas(win).items():
            want = hist if name in ("setup", "hold") else trace
            for g in grids:
                assert abs(g - want * 255) <= 1, (mode, name, g, want)
        pv = win._pair_view
        assert pv.rowCount()
        for r in range(pv.rowCount()):
            label = pv.item(r, 0).text()
            assert pv.item(r, 0).foreground().color().name() == t.legible_text_color(
                bookmark_pair_color(label[:1]), t.VizTheme.FRAME_BG), (mode, label)


@pytest.mark.parametrize("start", ["light", "dark"])
def test_the_audio_channel_labels_read_at_3_to_1_and_the_traces_keep_their_colour(
        start, monkeypatch):
    """The Bus Analyzer's audio channel checkboxes, started in either theme and switched live
    to the other: each label, read from the rendered window to the right of its indicator as
    the screenshots check reads it, clears 3:1; its text is the legible shade of its trace's
    colour, and the trace's pen keeps the colour itself."""
    import conftest
    from PySide6.QtCore import Qt

    from swi3s_studio.ui.main_window import MainWindow
    from swi3s_studio.ui.mode_controller import ANALYSIS
    shots = conftest.load_tool("screenshots")
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr("swi3s_studio.ui.theme_controller.save_preference", lambda _p: None)
    monkeypatch.setattr("swi3s_studio.ui.theme_controller.saved_preference", lambda: start)

    def settle():
        for _ in range(30):
            app.processEvents()

    apply_palette(start)
    w = MainWindow()
    try:
        w.setAttribute(Qt.WA_DontShowOnScreen, True)
        w.resize(*shots._SIZE)
        w.show()
        settle()
        w._mode_mgr.switch_to(ANALYSIS)
        w.open_demo("phy1")
        settle()
        for mode in (start, "dark" if start == "light" else "light"):
            if mode != start:
                w.apply_theme(mode)
                settle()
            av = w._audio_view
            assert t.VizTheme.MODE == mode and av._checks
            rgb = shots.image_array(w.grab().toImage())
            shaded = 0
            for (dev, dp, ch), cb in av._checks.items():
                trace = dp_line_color(dev, dp, av._stream_overrides)
                text = t.legible_text_color(trace, t.VizTheme.FRAME_BG)
                assert f"color: {text};" in cb.styleSheet(), (mode, cb.text())
                shaded += text != trace.name()
                assert cb.visibleRegion().boundingRect().size() == cb.size(), cb.text()
                crop = shots._crop(rgb, w, cb, 0, shots._label_left(cb))
                assert shots.text_contrast(crop) >= shots.MIN_CONTRAST, (mode, cb.text())
            for _p, curve, dev, dp, _ch, _n in av._plots:
                pen = curve.opts["pen"].color().name()
                assert pen == dp_line_color(dev, dp, av._stream_overrides).name(), (mode, dp)
            # The demo's light-theme line colours fall short of the floor on the list.
            assert (shaded > 0) == (mode == "light"), (mode, shaded)
    finally:
        w.close()
        w.deleteLater()
        settle()
        apply_palette("dark")


def test_the_authoring_panel_names_a_data_port_in_the_grids_palette():
    """One palette: a DP name chip is the colour of its grid cells, and the Visualizer config
    carries no copy of it."""
    from swi3s_studio.swviz import config
    from swi3s_studio.ui.authoring import authoring_panel
    assert authoring_panel._DP_PALETTE is _DP_PALETTE
    assert not hasattr(authoring_panel, "_DP_COLORS")
    for c in range(len(_DP_PALETTE)):
        assert f"background:{_DP_PALETTE[c]};" in authoring_panel._chip_css(c)
    assert not hasattr(config, "Colors") and not hasattr(config.constants, "Colors")
