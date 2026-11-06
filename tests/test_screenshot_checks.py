"""tools/screenshots.py: its fault checks, on synthetic images and widgets.

Each check is run on a case it must flag and on the healthy case next to it: a window drawn in
the wrong theme, text too faint or not drawn at all, a widget narrower than its text, and a
widget left in the old theme after a live switch. The renders themselves run in the gate's
screenshots check, on each platform's own style.
"""
import importlib.util
import os
import types

import conftest
import numpy as np
import pytest
from PySide6.QtWidgets import (
    QApplication,
    QLabel,
    QLineEdit,
    QPushButton,
    QToolButton,
    QWidget,
)

pytestmark = [pytest.mark.gui, pytest.mark.platform]

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_app = QApplication.instance() or QApplication([])


def _tool():
    path = os.path.join(_ROOT, "tools", "screenshots.py")
    spec = importlib.util.spec_from_file_location("screenshots", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


S = _tool()


def _solid(h, w, rgb):
    return np.full((h, w, 3), rgb, dtype=np.uint8)


def test_a_dark_pane_in_light_mode_is_the_wrong_theme():
    img = _solid(320, 320, (250, 250, 250))
    assert S.wrong_theme_share(img, "light") == 0.0
    img[:, :200] = (40, 40, 40)                       # a pane left in the dark palette
    assert S.wrong_theme_share(img, "light") > S.THEME_LIMIT


def test_a_light_pane_in_dark_mode_is_the_wrong_theme():
    img = _solid(320, 320, (43, 43, 43))
    assert S.wrong_theme_share(img, "dark") == 0.0
    img[:200] = (255, 255, 255)
    assert S.wrong_theme_share(img, "dark") > S.THEME_LIMIT


def _text_crop(bg, ink):
    crop = _solid(20, 80, bg)
    crop[6:14, 10:70:3] = ink                         # glyph strokes
    return crop


def test_readable_text_passes_the_contrast_check():
    assert S.text_contrast(_text_crop((255, 255, 255), (30, 30, 30))) > 10
    assert S.text_contrast(_text_crop((43, 43, 43), (220, 220, 220))) > S.MIN_CONTRAST


def test_faint_text_fails_the_contrast_check():
    """Grey on a near grey: the dim text of a pane drawn in the wrong palette."""
    assert S.text_contrast(_text_crop((43, 43, 43), (70, 70, 70))) < S.MIN_CONTRAST
    assert S.text_contrast(_text_crop((250, 250, 250), (215, 215, 215))) < S.MIN_CONTRAST


def test_text_that_is_not_drawn_has_no_contrast():
    assert S.text_contrast(_solid(20, 80, (255, 255, 255))) == 1.0


def test_a_button_narrower_than_its_label_is_clipped():
    b = QPushButton("Save Output (JSON)")
    b.setFixedWidth(40)
    assert S.clipped_by(b, b.text()) > S.CLIP_SLACK
    b.setFixedWidth(b.sizeHint().width())
    assert S.clipped_by(b, b.text()) == 0


def test_a_line_edit_squeezed_to_nothing_is_clipped():
    """A styled spin box can lay its edit field out 1 px wide on Windows: the value is there
    and draws nothing."""
    e = QLineEdit("12.288")
    e.setFixedWidth(1)
    assert S.clipped_by(e, e.text()) > S.CLIP_SLACK
    e.setFixedWidth(200)
    assert S.clipped_by(e, e.text()) == 0


def test_a_glyph_button_is_measured_by_its_glyph():
    t = QToolButton()
    t.setText("✕")
    t.setFixedSize(18, 18)
    assert S.clipped_by(t, t.text()) == 0


def test_a_wrapped_label_is_not_clipped():
    lab = QLabel("a long sentence that wraps over several lines in a narrow column")
    lab.setWordWrap(True)
    lab.setFixedWidth(60)
    assert S.clipped_by(lab, lab.text()) == 0
    lab.setWordWrap(False)
    assert S.clipped_by(lab, lab.text()) > S.CLIP_SLACK


def test_a_widget_left_in_the_old_theme_after_a_switch_is_found():
    """Tones keyed by widget: the one whose switched tone is the other theme's is reported;
    a fixed-colour widget, the same in both, and a slight shift are not."""
    started = {"open": 0.03, "chip": 0.7, "entry": 0.03, "only-started": 0.03}
    switched = {"open": 0.9, "chip": 0.7, "entry": 0.035, "only-switched": 0.9}
    assert S.unthemed(started, switched) == ["open"]


def test_the_tone_of_a_widget_is_read_from_its_inside():
    window = QWidget()
    window.setFixedSize(120, 40)
    window.setStyleSheet("background: white;")
    w = QPushButton("Open…", window)
    w.setGeometry(20, 8, 80, 24)
    w.setStyleSheet("QPushButton { background: black; color: white; border: none; }")
    window.show()
    _app.processEvents()
    (tone,) = S.widget_tones(window, window.grab().toImage()).values()
    assert tone < 0.05
    window.close()


def test_the_contact_sheet_orders_every_state():
    """Each state render() names sorts by its place in _STATES, within its mode."""
    for rank, state in enumerate(S._STATES):
        assert S._sheet_order(f"/out/Bus_Grid_{state}.png") == ("Bus_Grid", rank), state


def test_a_contact_sheet_that_cannot_be_written_or_an_image_it_cannot_read_is_a_finding(
        tmp_path):
    shot = tmp_path / "Mode_start-light.png"
    window = QWidget()
    window.setFixedSize(40, 30)
    assert window.grab().toImage().save(str(shot))
    window.close()
    missing = tmp_path / "Mode_start-dark.png"
    assert S._contact_sheet([str(shot)], str(tmp_path / "sheet.png")) == []
    sheet = tmp_path / "absent" / "sheet.png"
    assert S._contact_sheet([str(shot), str(missing)], str(sheet)) == [
        f"could not read {missing} for the contact sheet",
        f"could not write the contact sheet {sheet}"]


@pytest.mark.parametrize("native, asked, note", [
    (False, "minimal", "is not used"),
    (True, "minimal", "is cleared"),
    (False, "same", None),
    (True, "same", None),
    (False, None, None),
])
def test_render_asks_for_every_state_of_every_mode(tmp_path, monkeypatch, native, asked, note):
    """render() visits each mode in exactly the four named states, and the contact sheet
    orders by those names, so a state renamed in one place and not the other fails here.
    A QT_QPA_PLATFORM that differs from the platform the renders run on is named, as
    "is not used" when a platform was forced and "is cleared" when the native one ran; one
    equal to it, or none, is not named."""
    from PySide6.QtCore import QCoreApplication, QSettings

    from swi3s_studio.ui.mode_controller import MODE_LABELS, MODES
    org, name = QCoreApplication.organizationName(), QCoreApplication.applicationName()
    fmt = QSettings.defaultFormat()
    from swi3s_studio.ui.theme import VizTheme, apply_palette
    mode, palette, sheet = VizTheme.MODE, _app.palette(), _app.styleSheet()
    seen = []

    def fake_state(win, theme, state_name, out):
        seen.append(state_name)
        return os.path.join(out, state_name + ".png"), [], "stub", {}

    monkeypatch.setattr(S, "render_state", fake_state)
    monkeypatch.setattr(S, "_contact_sheet", lambda paths, out: [])
    monkeypatch.setattr(S, "_platform", lambda: "" if native else "offscreen")
    if asked is None:
        monkeypatch.delenv("QT_QPA_PLATFORM", raising=False)
    else:
        monkeypatch.setenv("QT_QPA_PLATFORM", _app.platformName() if asked == "same" else asked)
    made = []

    def mkdtemp(prefix):
        made.append(tmp_path / f"{prefix}settings")
        made[-1].mkdir()
        return str(made[-1])

    monkeypatch.setattr(S, "tempfile", types.SimpleNamespace(mkdtemp=mkdtemp))
    out = tmp_path / "out"
    try:
        assert S.render(str(out)) == 0
    finally:
        QCoreApplication.setOrganizationName(org)
        QCoreApplication.setApplicationName(name)
        QSettings.setDefaultFormat(fmt)
        QSettings.setPath(QSettings.IniFormat, QSettings.UserScope, conftest._SETTINGS_DIR)
        apply_palette(mode)
        _app.setPalette(palette)
        _app.setStyleSheet(sheet)
    assert (VizTheme.MODE, _app.palette(), _app.styleSheet()) == (mode, palette, sheet)
    # The settings directory is removed, and the platform the caller asked for is named.
    assert len(made) == 1 and not made[0].exists()
    report = (out / "report.txt").read_text(encoding="utf-8")
    if note:
        assert (f"NOTE: QT_QPA_PLATFORM=minimal was set and {note}: the renders run on "
                f"{_app.platformName()}") in report
    else:
        assert "QT_QPA_PLATFORM" not in report
    labels = [MODE_LABELS[m].replace(" ", "_") for m in MODES]
    states = {st for n in seen for st in S._STATES if n.endswith("_" + st)}
    assert states == set(S._STATES)
    assert sorted(seen) == sorted(f"{lb}_{st}" for lb in labels for st in S._STATES)
