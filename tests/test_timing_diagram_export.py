"""The Timing view's term diagram export (`TimingDiagram.export`, `TimingView.export_diagram`).

Pinned here:
- `paint_diagram` onto an image draws what the widget's own paint draws, pixel for pixel, and
  onto an SVG or a PDF the same text boxes and lines, its labels replayed or placed anew;
  every text takes its design widths;
- a PNG decodes at the diagram's size times the scale, an SVG parses as XML holding the
  diagram's text, and a PDF is one page of the diagram's size;
- the export actions are in the diagram's context menu, disabled with no diagram drawn and
  while a recompute runs, enabled with one drawn; the light background one is shown on the
  dark palette only;
- light background on the dark palette paints the light palette, and leaves the dark one
  active;
- the dialog's filter picks the format and a PNG's scale, and the file takes its extension;
  a write that fails shows a message naming the file;
- the offered file name is the leg and the term, and `suffixed_save_path` puts the suffix in
  place of a `replaced` extension, confirming a replacement of an existing file.

Run: QT_QPA_PLATFORM=offscreen PYTHONPATH=. python3 -m pytest tests/test_timing_diagram_export.py
"""
import os
import re
import xml.etree.ElementTree as ET

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
import timing_diagram_support as support

from swi3s_studio.timing import CalcInputs

pytestmark = [pytest.mark.gui, pytest.mark.usefixtures("shared_compute")]


def _app():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def _diagram(leg="MP_setup", key="Man_t_DD", width=700):
    """A diagram of `key` in `leg` at the default inputs, `width` px wide at its minimum
    height, in the page's diagram font."""
    _app()
    from swi3s_studio.ui.timing_diagram import TimingDiagram
    from swi3s_studio.ui.timing_view import _FONT_DIAGRAM, _base_font, _subscript, _term_key
    d = TimingDiagram(_subscript, _term_key)
    font = _base_font()
    font.setPixelSize(_FONT_DIAGRAM)
    d.setFont(font)
    d.resize(width, 600)
    d.show_term(f"1) {leg}", key, support.sections(CalcInputs(), leg))
    d.resize(width, d.minimumHeight())
    return d


def _view():
    from PySide6.QtWidgets import QApplication
    _app()
    from swi3s_studio.ui.timing_view import TimingView
    tv = TimingView()
    tv.resize(1800, 1100)
    tv.show()
    tv.flush_recompute()
    QApplication.processEvents()
    return tv


class _Texts:
    """A painter that passes every call through and records each static text's box and
    text, and each line drawn."""

    def __init__(self, painter, texts, lines):
        self._painter, self._texts, self._lines = painter, texts, lines

    def drawStaticText(self, pos, text):   # noqa: N802 - QPainter's name
        from PySide6.QtCore import QRectF
        self._texts.append((QRectF(pos, text.size()).getRect(), text.text()))
        return self._painter.drawStaticText(pos, text)

    def drawLine(self, *a):   # noqa: N802 - QPainter's name
        self._lines.append(tuple((q.x(), q.y()) for q in a) if len(a) == 2 else a)
        return self._painter.drawLine(*a)

    def __getattr__(self, name):
        return getattr(self._painter, name)


def _recorded(monkeypatch, d, draw):
    """The text boxes and lines `draw()` paints through `paint_diagram`."""
    from swi3s_studio.ui.timing_diagram import TimingDiagram
    texts, lines = [], []
    real = TimingDiagram.paint_diagram
    with monkeypatch.context() as patch:
        patch.setattr(TimingDiagram, "paint_diagram",
                      lambda self, p: real(self, _Texts(p, texts, lines)))
        draw()
    return texts, lines


@pytest.mark.parametrize("leg,key", [("MP_setup", "Man_t_DD"), ("PM_setup", "Per_t_DD"),
                                     ("PP_setup", "bus length (cm)")])
def test_the_export_paints_what_the_widget_paints(monkeypatch, leg, key):
    """A PNG at the screen's pixel ratio is the widget's own paint pixel for pixel, its text
    antialiased in grey as on every image (a widget's text may be antialiased by subpixel,
    which only a screen shows), and a PNG at another scale, an SVG and a PDF paint the same
    text boxes and lines, a panel with a strip of points on no drawn ramp included, whether
    the export replays the widget's label spots or places its labels anew; an export leaves
    the widget's paint as it was."""
    from PySide6.QtGui import QFont, QImage
    d = _diagram(leg, key, width=900)
    font = d.font()
    font.setStyleStrategy(QFont.NoSubpixelAntialias)
    d.setFont(font)
    shown = _recorded(monkeypatch, d, lambda: d.grab())
    assert shown[0], "the widget painted text"
    grab = d.grab().toImage().convertToFormat(QImage.Format_ARGB32)
    png = QImage.fromData(d.export("png", grab.devicePixelRatio()))
    png = png.convertToFormat(QImage.Format_ARGB32)
    png.setDevicePixelRatio(grab.devicePixelRatio())
    assert png.size() == grab.size()
    assert png == grab
    for fmt in ("png", "svg", "pdf"):
        got = _recorded(monkeypatch, d, lambda f=fmt: d.export(f, 2.0))
        assert got == shown, fmt
        d._spots.clear()                 # the labels placed anew, on the export's device
        got = _recorded(monkeypatch, d, lambda f=fmt: d.export(f, 2.0))
        assert got == shown, f"{fmt}, its labels placed anew"
    assert _recorded(monkeypatch, d, lambda: d.grab()) == shown, "an export changes no paint"


def test_every_text_takes_its_design_widths(monkeypatch):
    """Every text the diagram draws, and every font it measures, is hinted vertically at most,
    so its glyphs advance by their design widths on the screen and on any device."""
    from PySide6.QtGui import QFont
    d = _diagram("PP_setup", "bus length (cm)")
    fonts = []

    class Fonts:
        def __init__(self, painter):
            self._painter = painter

        def drawStaticText(self, *a):   # noqa: N802 - QPainter's name
            fonts.append(self._painter.font())
            return self._painter.drawStaticText(*a)

        def __getattr__(self, name):
            return getattr(self._painter, name)

    from swi3s_studio.ui import diagram_style as ds
    from swi3s_studio.ui.timing_diagram import TimingDiagram
    real = TimingDiagram.paint_diagram
    monkeypatch.setattr(TimingDiagram, "paint_diagram", lambda self, p: real(self, Fonts(p)))
    for draw in (d.grab, lambda: d.export("svg"), lambda: d.export("pdf")):
        draw()
    measured = [d._body_font()] + [d._font(k) for k in ds.FONT_KEYS]
    assert fonts
    scalable = (QFont.PreferVerticalHinting, QFont.PreferNoHinting)
    assert {f.hintingPreference() for f in fonts + measured} <= set(scalable)


def test_a_png_decodes_at_the_size_times_the_scale():
    from PySide6.QtGui import QImage
    d = _diagram()
    for scale in (1.0, 2.0, 3.0):
        img = QImage.fromData(d.export("png", scale))
        assert not img.isNull()
        assert (img.width(), img.height()) == (round(d.width() * scale),
                                               round(d.height() * scale)), scale


def test_an_svg_is_xml_holding_the_diagrams_text():
    d = _diagram()
    root = ET.fromstring(d.export("svg"))
    assert root.tag.endswith("svg")
    assert root.get("viewBox") == f"0 0 {d.width()} {d.height()}"
    text = " ".join(t.strip() for el in root.iter() if el.tag.endswith("text")
                    for t in el.itertext())
    for want in ("1) MP_setup", "Specification", "Clk @ Manager", "Data @ Manager",
                 "Man gnd", "18.00 ns"):
        assert want in text, (want, text)
    families = {el.get("font-family") for el in root.iter() if el.get("font-family")}
    assert families and all(f.endswith(", sans-serif") for f in families), families


def test_a_pdf_is_one_page_of_the_diagrams_size():
    d = _diagram()
    data = d.export("pdf")
    assert data.startswith(b"%PDF")
    assert len(re.findall(rb"/Type\s*/Page\b(?!s)", data)) == 1
    box = re.search(rb"/MediaBox\s*\[\s*([\d.\s]+)\]", data)
    assert box, "the page has a MediaBox"
    x0, y0, x1, y1 = (float(v) for v in box[1].split())
    pt = 72.0 / d.logicalDpiY()
    assert (x1 - x0, y1 - y0) == pytest.approx((d.width() * pt, d.height() * pt), abs=0.5)


def test_export_refuses_with_no_diagram_while_computing_and_an_unknown_format():
    d = _diagram()
    assert d.can_export()
    with pytest.raises(ValueError):
        d.export("gif")
    with pytest.raises(ValueError):
        d.export("png", 0.0)
    d.set_pending(True)
    assert not d.can_export()
    with pytest.raises(ValueError):
        d.export("png")
    d.set_pending(False)
    d.clear()
    assert not d.can_export()
    with pytest.raises(ValueError):
        d.export("svg")


def test_light_background_on_the_dark_palette_paints_the_light_palette():
    from PySide6.QtGui import QColor, QImage

    from swi3s_studio.ui import theme
    from swi3s_studio.ui.theme import VizTheme, apply_palette
    before = VizTheme.MODE
    apply_palette("dark")
    try:
        d = _diagram()
        light = QImage.fromData(d.export("png", 1.0, light=True))
        assert VizTheme.MODE == "dark" and VizTheme.FRAME_BG == theme._DARK["FRAME_BG"]
        dark = QImage.fromData(d.export("png", 1.0))

        def colours(img):
            return {img.pixelColor(x, y).name() for x in range(0, img.width(), 2)
                    for y in range(0, img.height(), 2)}

        lit, dim = colours(light), colours(dark)
        assert light.pixelColor(1, 1) == QColor(theme._LIGHT["FRAME_BG"])
        assert dark.pixelColor(1, 1) == QColor(theme._DARK["FRAME_BG"])
        for key in ("DIAG_CLOCK", "DIAG_DATA"):
            assert theme._LIGHT[key].lower() in lit, key
            assert theme._DARK[key].lower() not in lit, key
            assert theme._DARK[key].lower() in dim, key
    finally:
        apply_palette(before)


def test_the_actions_follow_the_diagram_and_a_recompute(monkeypatch):
    """Disabled with no diagram drawn and while a recompute runs; enabled with one drawn. Both
    are in the diagram's context menu, and the light background one shows on the dark palette
    only."""
    from PySide6.QtCore import Qt

    from swi3s_studio.ui.theme import VizTheme, apply_palette
    tv = _view()
    plain, light = tv.diagram_export_actions()
    d = tv._diagram
    assert d.contextMenuPolicy() == Qt.ActionsContextMenu
    assert {plain, light} <= set(d.actions())
    assert plain.isEnabled() and light.isEnabled()          # the default view of 1)
    tv._hide_diagram()
    assert not plain.isEnabled() and not light.isEnabled()
    tv._hl = {"Man_t_DD"}
    tv._draw_term("Man_t_DD", "MP_setup")
    assert plain.isEnabled() and light.isEnabled()
    tv._recompute()
    assert d.pending and not plain.isEnabled() and not light.isEnabled()
    tv.flush_recompute()
    assert plain.isEnabled() and light.isEnabled()
    tv._hide_diagram()
    assert not plain.isEnabled()
    before = VizTheme.MODE
    try:
        apply_palette("light")
        tv.retheme()
        assert plain.isVisible() and not light.isVisible()
        apply_palette("dark")
        tv.retheme()
        assert light.isVisible()
    finally:
        apply_palette(before)
        tv.retheme()


def _export_through_the_dialog(monkeypatch, tv, path, chosen, light=False):
    """`export_diagram` with the dialog answering `path` and the filter `chosen`; returns
    the messages shown, each (kind, text)."""
    from PySide6.QtWidgets import QFileDialog, QMessageBox
    shown = []
    offered = {}

    def ask(_parent, _title, name, filters, default):
        offered.update(name=name, filters=filters.split(";;"), default=default)
        return path, chosen

    monkeypatch.setattr(QFileDialog, "getSaveFileName", staticmethod(ask))
    for kind in ("critical", "warning"):
        monkeypatch.setattr(QMessageBox, kind, staticmethod(
            lambda _p, _t, text, *a, k=kind: shown.append((k, text))))
    tv.export_diagram(light=light)
    return shown, offered


def test_the_filter_picks_the_format_and_the_file_takes_its_extension(monkeypatch, tmp_path):
    from PySide6.QtGui import QImage

    from swi3s_studio.ui.timing_view import _EXPORT_FILTERS
    tv = _view()
    tv._hl = {"Man_t_DD"}
    tv._draw_term("Man_t_DD", "MP_setup")
    d = tv._diagram
    filters = [f for f, _, _ in _EXPORT_FILTERS]
    shown, offered = _export_through_the_dialog(monkeypatch, tv, str(tmp_path / "a"), filters[0])
    assert offered["name"] == "MP_setup_Man_t_DD.png"
    assert offered["default"] == filters[0] and filters[0].startswith("PNG, 2×")
    assert not shown
    img = QImage(str(tmp_path / "a.png"))
    assert (img.width(), img.height()) == (2 * d.width(), 2 * d.height())
    one = next(f for f in filters if f.startswith("PNG, 1×"))
    _export_through_the_dialog(monkeypatch, tv, str(tmp_path / "b.png"), one)
    assert QImage(str(tmp_path / "b.png")).width() == d.width()
    svg = next(f for f in filters if f.startswith("SVG"))
    _export_through_the_dialog(monkeypatch, tv, str(tmp_path / "c.png"), svg)
    assert not (tmp_path / "c.png").exists()
    ET.parse(str(tmp_path / "c.svg"))
    pdf = next(f for f in filters if f.startswith("PDF"))
    _export_through_the_dialog(monkeypatch, tv, str(tmp_path / "d.svg"), pdf, light=True)
    assert (tmp_path / "d.pdf").read_bytes().startswith(b"%PDF")


def test_a_write_that_fails_names_the_file(monkeypatch, tmp_path):
    tv = _view()
    tv._hl = {"Man_t_DD"}
    tv._draw_term("Man_t_DD", "MP_setup")
    path = str(tmp_path / "missing" / "x.svg")
    shown, _ = _export_through_the_dialog(monkeypatch, tv, path, "SVG (*.svg)")
    assert len(shown) == 1 and shown[0][0] == "critical"
    assert path in shown[0][1], shown


def test_a_diagram_closed_while_the_file_is_chosen_is_not_written(monkeypatch, tmp_path):
    from PySide6.QtWidgets import QFileDialog, QMessageBox
    tv = _view()
    tv._hl = {"Man_t_DD"}
    tv._draw_term("Man_t_DD", "MP_setup")
    path = str(tmp_path / "x.svg")
    shown = []

    def ask(*_a):
        tv._hide_diagram()
        return path, "SVG (*.svg)"

    monkeypatch.setattr(QFileDialog, "getSaveFileName", staticmethod(ask))
    monkeypatch.setattr(QMessageBox, "warning", staticmethod(
        lambda _p, _t, text, *a: shown.append(text)))
    tv.export_diagram()
    assert not os.path.exists(path)
    assert len(shown) == 1 and path in shown[0]


def test_the_offered_name_and_a_replaced_extension(monkeypatch, tmp_path):
    from PySide6.QtWidgets import QMessageBox

    from swi3s_studio.ui.save_path import suffixed_save_path
    from swi3s_studio.ui.timing_view import _export_name
    assert _export_name("PP_setup", "bus length (cm)") == "PP_setup_bus_length_cm"
    assert _export_name("", "") == "diagram"
    _app()
    rep = (".png", ".svg", ".pdf")
    assert suffixed_save_path(None, str(tmp_path / "a.PNG"), ".svg", "t",
                              replaced=rep) == str(tmp_path / "a.svg")
    assert suffixed_save_path(None, str(tmp_path / "run 1.5"), ".pdf", "t",
                              replaced=rep) == str(tmp_path / "run 1.5.pdf")
    assert suffixed_save_path(None, str(tmp_path / "a.pdf"), ".pdf", "t",
                              replaced=rep) == str(tmp_path / "a.pdf")
    (tmp_path / "b.svg").write_text("")
    asked = []
    monkeypatch.setattr(QMessageBox, "question", staticmethod(
        lambda _p, _t, text, *a: asked.append(text) or QMessageBox.No))
    assert suffixed_save_path(None, str(tmp_path / "b.png"), ".svg", "t", replaced=rep) is None
    assert len(asked) == 1 and "b.svg" in asked[0]
