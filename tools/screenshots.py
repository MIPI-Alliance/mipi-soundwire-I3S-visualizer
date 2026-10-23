#!/usr/bin/env python3
"""Screenshots of every mode in both themes, checked for theme and text-contrast faults.

    python3 tools/screenshots.py                       # render + check, into build/screenshots
    python3 tools/screenshots.py --out DIR             # somewhere else

For each mode (Bus Visualizer, Timing Calculator, Bus Analyzer with the PHY1 demo) it renders
four states: started in light, started in dark, and each switched live to the other theme. Each
image is written to OUT/<mode>_<state>.png, with OUT/contact_sheet.png holding all of them and
OUT/report.txt listing every finding. tools/gate.py runs it as the "screenshots" check; the
contact sheets from the three platforms are the gate's visual review.

Four checks, and a finding in any fails the run (exit 1):
  * THEME: the share of the window drawn in the other theme's background — dark tiles in light
    mode, light tiles in dark mode — stays under THEME_LIMIT. A pane left in the wrong palette
    fails it.
  * CONTRAST: every visible, enabled label, button, check box, radio button, line edit and
    combo box has text whose contrast with the pixels behind it is at least MIN_CONTRAST (WCAG
    relative luminance). Text that is not drawn at all has a contrast of 1.
  * CLIPPED: no button, check box, single-line label or line edit is narrower than its text
    by more than CLIP_SLACK px.
  * THEME not re-applied: after a live switch to a theme, every entry and button has the
    background tone it has in a window started in that theme. A widget whose own style is set
    once, at creation, and missed by the switch fails it.

The native windowing platform is used where there is one, with the windows kept off screen, so
the platform's own style is what is checked; with none (a headless Linux shell) it falls back to
the offscreen platform and says so in the report.
"""
from __future__ import annotations

import os
import sys
import tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

THEME_LIMIT = 0.10        # share of 16 px tiles allowed in the other theme's background
MIN_CONTRAST = 3.0        # text against what is behind it (WCAG large-text AA)
CLIP_SLACK = 2            # px of a widget's text it may not show before that is a finding
_TILE = 16
_SIZE = (1400, 900)
_STATES = ("start-light", "start-dark", "switched-to-dark", "switched-to-light")


def _luminance(rgb):
    """WCAG relative luminance of an (..., 3) uint8 array."""
    import numpy as np
    c = rgb.astype(np.float64) / 255.0
    lin = np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)
    return lin @ np.array([0.2126, 0.7152, 0.0722])


def _contrast(a, b):
    import numpy as np
    hi, lo = np.maximum(a, b), np.minimum(a, b)
    return (hi + 0.05) / (lo + 0.05)


def image_array(img):
    """A QImage as an (h, w, 3) uint8 array."""
    import numpy as np
    from PySide6.QtGui import QImage
    img = img.convertToFormat(QImage.Format_RGB888)
    w, h, bpl = img.width(), img.height(), img.bytesPerLine()
    buf = np.frombuffer(img.constBits(), dtype=np.uint8, count=h * bpl).reshape(h, bpl)
    return buf[:, : w * 3].reshape(h, w, 3).copy()


def wrong_theme_share(rgb, mode: str) -> float:
    """The share of tiles whose mean luminance is the OTHER theme's background: dark
    (< 0.05) in light mode, light (> 0.6) in dark mode."""
    lum = _luminance(rgb)
    h, w = lum.shape
    h, w = h - h % _TILE, w - w % _TILE
    tiles = lum[:h, :w].reshape(h // _TILE, _TILE, w // _TILE, _TILE).mean(axis=(1, 3))
    wrong = tiles < 0.05 if mode == "light" else tiles > 0.6
    return float(wrong.mean()) if tiles.size else 0.0


def text_contrast(rgb) -> float:
    """The contrast of a text crop's ink with its background. The background is the median
    luminance of the crop's edge; ink is every pixel at least 1.15:1 from it, and the ink's
    contrast is its 99th percentile: the cores of the glyphs, which carry the text colour even
    where thin strokes at 1x scale are mostly anti-aliased edge. No ink at all is a contrast
    of 1.0: the text cannot be seen."""
    import numpy as np
    lum = _luminance(rgb)
    if lum.shape[0] < 3 or lum.shape[1] < 3:
        return float("inf")
    edge = np.concatenate([lum[0], lum[-1], lum[:, 0], lum[:, -1]])
    bg = float(np.median(edge))
    ratio = _contrast(lum, bg)
    ink = ratio[ratio >= 1.15]
    return float(np.percentile(ink, 99)) if ink.size else 1.0


def _text_widgets(window):
    """(widget, its text, the inset that leaves out its frame) for every widget the text
    checks read: wholly on screen, enabled, showing text."""
    from PySide6.QtWidgets import QAbstractButton, QComboBox, QLabel, QLineEdit
    out = []
    for wdg in window.findChildren(QWidget_types()):
        if isinstance(wdg, QComboBox):
            text, inset = wdg.currentText(), 3
        elif isinstance(wdg, QLineEdit):
            text, inset = wdg.text() or wdg.placeholderText(), 3
        elif isinstance(wdg, QAbstractButton):
            text, inset = wdg.text(), (0 if wdg.metaObject().className() in
                                       ("QCheckBox", "QRadioButton") else 3)
        elif isinstance(wdg, QLabel):
            text, inset = wdg.text(), 0
        else:
            continue
        text = (text or "").strip()
        if not text or not wdg.isVisible() or not wdg.isEnabled():
            continue
        # Partly scrolled out of view, its text may be in the part that is not shown.
        if wdg.visibleRegion().boundingRect().size() != wdg.size():
            continue
        out.append((wdg, text, inset))
    return out


def QWidget_types():      # noqa: N802 - names the classes findChildren matches
    from PySide6.QtWidgets import QWidget
    return QWidget


def clipped_by(wdg, text: str) -> int:
    """How many px of `text` `wdg` cannot show: a button, check box or single-line label
    narrower than its size hint, or a glyph button or line edit narrower than its text. 0 when it fits, or
    when the widget wraps or elides by design (a word-wrapped label, a combo box)."""
    from PySide6.QtWidgets import QAbstractButton, QLabel, QLineEdit, QToolButton
    if isinstance(wdg, QToolButton):        # a glyph button: the glyph itself must fit
        return max(0, wdg.fontMetrics().horizontalAdvance(text) - wdg.width())
    if isinstance(wdg, QLineEdit):
        need = wdg.fontMetrics().horizontalAdvance(text) + 2 * wdg.style().pixelMetric(
            wdg.style().PixelMetric.PM_DefaultFrameWidth, None, wdg) + 4
        return max(0, need - wdg.width())
    if isinstance(wdg, QLabel):
        if wdg.wordWrap() or "\n" in text or "<br" in text.lower() or not wdg.pixmap().isNull():
            return 0
        return max(0, wdg.sizeHint().width() - wdg.width())
    if isinstance(wdg, QAbstractButton):
        return max(0, wdg.sizeHint().width() - wdg.width())
    return 0


def _crop(rgb, window, wdg, inset: int = 0):
    """The pixels of `wdg`'s visible part in the window's image, `inset` px in from each
    side (to leave out a frame)."""
    r = wdg.visibleRegion().boundingRect().adjusted(inset, inset, -inset, -inset)
    tl = wdg.mapTo(window, r.topLeft())
    scale = rgb.shape[1] / max(1, window.width())
    x0, y0 = int(tl.x() * scale), int(tl.y() * scale)
    x1, y1 = int((tl.x() + r.width()) * scale), int((tl.y() + r.height()) * scale)
    return rgb[max(0, y0):max(0, y1), max(0, x0):max(0, x1)]


def widget_tones(window, img) -> dict:
    """The background tone of each entry and button in `window`'s image: the median luminance
    of its inside, keyed by class, name, text and place in the window, so the same widget of
    another window built the same way has the same key."""
    import numpy as np
    from PySide6.QtCore import QPoint
    from PySide6.QtWidgets import (
        QAbstractButton,
        QAbstractSpinBox,
        QCheckBox,
        QComboBox,
        QLineEdit,
        QRadioButton,
    )
    rgb = image_array(img)
    tones = {}
    for wdg in window.findChildren(QWidget_types()):
        if not isinstance(wdg, (QLineEdit, QComboBox, QAbstractButton, QAbstractSpinBox)):
            continue
        if isinstance(wdg, (QCheckBox, QRadioButton)) or not wdg.isVisible():
            continue
        if wdg.visibleRegion().boundingRect().size() != wdg.size():
            continue
        crop = _crop(rgb, window, wdg, 3)
        if crop.shape[0] < 4 or crop.shape[1] < 4:
            continue
        at = wdg.mapTo(window, QPoint(0, 0))
        text = wdg.currentText() if isinstance(wdg, QComboBox) else getattr(wdg, "text", str)()
        key = (type(wdg).__name__, wdg.objectName(), text, at.x(), at.y(), wdg.width(),
               wdg.height())
        tones[key] = float(np.median(_luminance(crop)))
    return tones


def unthemed(started: dict, switched: dict) -> list[tuple]:
    """The widgets that look different after a switch to a theme than when the window
    started in it: their own style was not re-applied. Tones at least 1.5:1 apart differ."""
    return sorted(k for k in started.keys() & switched.keys()
                  if _contrast(started[k], switched[k]) >= 1.5)


def check_window(window, img, mode: str) -> tuple[list[str], str]:
    """The findings for one rendered state of `window` in theme `mode`, and a one-line
    summary of the margins: the wrong-theme share and the lowest text contrast."""
    import re
    rgb = image_array(img)
    found = []
    lowest = (float("inf"), "")
    share = wrong_theme_share(rgb, mode)
    if share > THEME_LIMIT:
        found.append(f"THEME: {share:.0%} of the window is drawn in the "
                     f"{'dark' if mode == 'light' else 'light'} theme's background "
                     f"(limit {THEME_LIMIT:.0%})")
    for wdg, text, inset in _text_widgets(window):
        plain = re.sub(r"<[^>]+>", "", text).replace("\n", " ")
        what = f"{type(wdg).__name__} {wdg.objectName() or ''} \"{plain[:60]}\"".replace("  ", " ")
        short = clipped_by(wdg, text)
        if short > CLIP_SLACK:
            found.append(f"CLIPPED by {short} px — {what}")
        crop = _crop(rgb, window, wdg, inset)
        if crop.shape[0] < 4 or crop.shape[1] < 4:
            continue
        c = text_contrast(crop)
        lowest = min(lowest, (c, what))
        if c < MIN_CONTRAST:
            found.append(f"CONTRAST {c:.2f}:1 < {MIN_CONTRAST}:1 — {what}")
    summary = f"wrong theme {share:.1%}, lowest contrast {lowest[0]:.2f}:1 {lowest[1]}"
    return found, summary


def _contact_sheet(paths: list[str], out: str) -> None:
    from PySide6.QtGui import QColor, QFont, QImage, QPainter
    cols, w, h, label = 4, 700, 450, 24
    rows = (len(paths) + cols - 1) // cols
    sheet = QImage(cols * w, rows * (h + label), QImage.Format_RGB32)
    sheet.fill(QColor("#808080"))
    p = QPainter(sheet)
    p.setFont(QFont("Helvetica", 14))
    for i, path in enumerate(paths):
        x, y = (i % cols) * w, (i // cols) * (h + label)
        p.drawText(x + 4, y + 18, os.path.basename(path))
        img = QImage(path)
        p.drawImage(x + 3, y + label, img.scaled(w - 6, h - 6))
    p.end()
    sheet.save(out)


def _platform() -> str:
    """The windowing platform to render on: the native one where the session has one, the
    offscreen one otherwise (a Linux shell with no display)."""
    if sys.platform.startswith("linux") and not (os.environ.get("DISPLAY")
                                                 or os.environ.get("WAYLAND_DISPLAY")):
        return "offscreen"
    return ""


def render(out: str) -> int:
    """Render every mode in every state into `out` and check each; 0 when nothing is found."""
    plat = _platform()
    if plat:
        os.environ["QT_QPA_PLATFORM"] = plat
    else:
        os.environ.pop("QT_QPA_PLATFORM", None)
    sys.path.insert(0, _ROOT)
    from PySide6.QtCore import QCoreApplication, QSettings, Qt
    QCoreApplication.setOrganizationName("MIPI SWI3S Screenshots")
    QCoreApplication.setApplicationName("SWI3S Studio Screenshots")
    QSettings.setDefaultFormat(QSettings.IniFormat)
    QSettings.setPath(QSettings.IniFormat, QSettings.UserScope,
                      tempfile.mkdtemp(prefix="swi3s_screenshots_"))
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    from swi3s_studio.ui.singleton_pin import pin_singletons
    pin_singletons()
    from swi3s_studio.ui.main_window import MainWindow
    from swi3s_studio.ui.mode_controller import ANALYSIS, MODE_LABELS, MODES
    from swi3s_studio.ui.theme import apply_palette, save_preference

    def settle(n: int = 30) -> None:
        for _ in range(n):
            app.processEvents()

    os.makedirs(out, exist_ok=True)
    paths, report = [], [f"platform: {app.platformName()}, style: {app.style().name()}, "
                         f"python {sys.version.split()[0]}"]
    if plat:
        report.append("NOTE: no display, so the offscreen platform and its style were used")
    failed = 0
    tones = {}
    for start in ("light", "dark"):
        other = "dark" if start == "light" else "light"
        save_preference(start)
        apply_palette(start)
        win = MainWindow()
        win.setAttribute(Qt.WA_DontShowOnScreen, True)     # rendered, never on the screen
        win.resize(*_SIZE)
        win.show()
        settle()
        win._mode_mgr.switch_to(ANALYSIS)
        win.open_demo("phy1")
        settle()
        for state, theme in ((f"start-{start}", start), (f"switched-to-{other}", other)):
            if theme != start:
                win.apply_theme(theme)
                settle()
            for mode in MODES:
                win._mode_mgr.switch_to(mode)
                settle()
                img = win.grab().toImage()
                name = f"{MODE_LABELS[mode].replace(' ', '_')}_{state}"
                path = os.path.join(out, name + ".png")
                img.save(path)
                paths.append(path)
                found, summary = check_window(win, img, theme)
                tones[name] = widget_tones(win, img)
                failed += len(found)
                verdict = "ok" if not found else f"{len(found)} finding(s)"
                report.append(f"{name}: {verdict} ({summary})")
                report.extend(f"    {f}" for f in found)
        win.close()
        win.deleteLater()
        settle()
    # A switch to a theme must leave every widget as a window started in it draws it.
    for mode in MODES:
        label = MODE_LABELS[mode].replace(' ', '_')
        for theme in ("light", "dark"):
            name = f"{label}_switched-to-{theme}"
            stale = unthemed(tones[f"{label}_start-{theme}"], tones[name])
            failed += len(stale)
            if stale:
                report.append(f"{name}: {len(stale)} widget(s) not re-themed")
                report.extend(f"    THEME not re-applied — {k[0]} {k[1]} \"{k[2][:40]}\" "
                              f"at ({k[3]}, {k[4]})".replace("  ", " ") for k in stale)
    paths.sort(key=lambda p: (os.path.basename(p).split("_start")[0].split("_switched")[0],
                              [s in p for s in _STATES].index(True)))
    sheet = os.path.join(out, "contact_sheet.png")
    _contact_sheet(paths, sheet)
    report.append(f"contact sheet: {sheet}")
    with open(os.path.join(out, "report.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(report) + "\n")
    print("\n".join(report), flush=True)
    return 1 if failed else 0


def main(argv: list[str]) -> int:
    out = os.path.join(_ROOT, "build", "screenshots")
    if "--out" in argv:
        out = os.path.abspath(argv[argv.index("--out") + 1])
    return render(out)


if __name__ == "__main__":
    code = main(sys.argv[1:])
    sys.stdout.flush()
    os._exit(code)        # skip Qt's teardown of the windows already closed
