"""tools/screenshots.py: its fault checks, on synthetic images and widgets.

Each check is run on a case it must flag and on the healthy case next to it: a window drawn in
the wrong theme, text too faint or not drawn at all, a widget narrower than its text, and a
widget left in the old theme after a live switch. The renders themselves run in the gate's
screenshots check, on each platform's own style.
"""
import importlib.util
import os

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


@pytest.mark.parametrize("state", ["start-light", "start-dark", "switched-to-dark",
                                   "switched-to-light"])
def test_every_state_is_rendered(state):
    assert state in S._STATES
