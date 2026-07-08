#!/usr/bin/env python3
"""Render SWI3S Studio views to PNGs for quick visual review — "render and look".

Iterating on layout is much faster when you can see the rendered page instead of
guessing from the code. This dumps one PNG per view using Qt's offscreen
platform, so it runs headless (local, CI, or an agent) with no display.

    python tools/shots.py                     # all views -> ./shots/*.png
    python tools/shots.py timing analysis     # only those views
    python tools/shots.py --out /tmp/shots    # choose the output directory
    python tools/shots.py --size 1600x1000    # override the window size
    python tools/shots.py --list              # list the view names

Caveat: the offscreen platform uses a fallback font, so pixel widths are
~faithful but not identical to a real display. Use these shots to judge
structure, alignment, overflow and clipping — not final typography.
"""
from __future__ import annotations

import argparse
import os
import sys

# Must be set before QApplication so this runs without a display.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


def _timing():
    from swi3s_studio.ui.timing_view import TimingView
    return TimingView(), (1300, 1200)


def _window(mode: str, size):
    """A full MainWindow with the demo loaded, switched to `mode`."""
    def make():
        from swi3s_studio.ui.main_window import MainWindow
        from swi3s_studio.ui import mode_controller as mc
        w = MainWindow()
        w.load_demo()
        w._mode_mgr.switch_to({"visualizer": mc.VISUALIZATION,
                               "timing": mc.TIMING,
                               "analysis": mc.ANALYSIS}[mode])
        return w, size
    return make


# view name -> zero-arg factory returning (widget, (width, height)).
# Add an entry here to make a new view shootable.
TARGETS = {
    "timing": _timing,                                   # Timing calculator, standalone
    "visualizer": _window("visualizer", (1700, 1050)),   # Bus Visualizer (authoring)
    "analysis": _window("analysis", (1700, 1050)),       # Analysis (demo capture loaded)
}


def _shot(app, factory, path, size_override=None):
    widget, size = factory()
    w, h = size_override or size
    widget.resize(w, h)
    widget.show()
    for _ in range(5):        # let layouts settle (and demo panes populate)
        app.processEvents()
    ok = bool(widget.grab().save(path))
    widget.close()
    return ok


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Render SWI3S Studio views to PNGs.")
    ap.add_argument("views", nargs="*", help="view names to render (default: all)")
    ap.add_argument("--out", default=os.path.join(_ROOT, "shots"),
                    help="output directory (default: ./shots)")
    ap.add_argument("--size", help="override size as WxH, e.g. 1600x1000")
    ap.add_argument("--list", action="store_true", help="list view names and exit")
    args = ap.parse_args(argv)

    if args.list:
        print("\n".join(sorted(TARGETS)))
        return 0

    names = args.views or sorted(TARGETS)
    unknown = [n for n in names if n not in TARGETS]
    if unknown:
        ap.error(f"unknown view(s): {', '.join(unknown)}; "
                 f"choose from: {', '.join(sorted(TARGETS))}")

    size_override = None
    if args.size:
        try:
            w, h = (int(x) for x in args.size.lower().split("x"))
            size_override = (w, h)
        except ValueError:
            ap.error("--size must look like 1600x1000")

    from PySide6.QtWidgets import QApplication
    from swi3s_studio.ui.theme import apply_palette, resolve_mode, saved_preference
    app = QApplication.instance() or QApplication([])
    apply_palette(resolve_mode(saved_preference()))    # match the app's appearance

    os.makedirs(args.out, exist_ok=True)
    failures = 0
    for name in names:
        path = os.path.join(args.out, f"{name}.png")
        ok = _shot(app, TARGETS[name], path, size_override)
        failures += not ok
        print(f"{'ok  ' if ok else 'FAIL'} {path}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
