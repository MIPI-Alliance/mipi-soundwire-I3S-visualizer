#!/usr/bin/env python3
"""SVG new-vs-old comparison page for the Bus-Visualizer renderer.

Renders every vendored Visualizer example config TWO ways and lays them side by side
in one HTML page so rendering (not just data) can be eyeballed across all configs:

  - **new** = SWI3S Studio's `GridView`, driven by the vendored Visualizer engine
    (`viz_engine.render_payload` -> `set_bus_model` -> `export_image('.svg')`).
  - **old** = the standalone SWI3S Visualizer's own render (its tkinter
    `FrameRenderer` -> `canvasvg`), from the sibling repo `../mipi-soundwire-I3S-visualizer`.

Qt (new) and Tk (old) can't share one process, so each side runs in its own
subprocess; the parent then writes `svg_compare.html`.

Usage (from swi3s-studio/):
    python3 tools/svg_compare.py                 # all configs -> svg_compare_out/
    python3 tools/svg_compare.py --out /tmp/cmp  # custom output dir
    python3 tools/svg_compare.py --limit 10      # first 10 configs (quick look)
    open svg_compare_out/svg_compare.html

This is a run-on-demand dev tool, not part of the test suite.
"""
from __future__ import annotations

import argparse
import glob
import os
import subprocess
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_STUDIO = os.path.dirname(_HERE)                                   # swi3s-studio/
_EXAMPLES = os.path.join(_STUDIO, "tests", "visualizer", "examples")
_SIBLING = os.path.abspath(os.path.join(_STUDIO, "..", "mipi-soundwire-I3S-visualizer"))


def _configs(limit=None):
    out = []
    for csv in sorted(glob.glob(os.path.join(_EXAMPLES, "**", "*.csv"), recursive=True)):
        rel = os.path.relpath(csv, _EXAMPLES)[:-4]                 # category/name
        out.append((rel, csv))
    return out[:limit] if limit else out


def _safe(rel):
    return rel.replace(os.sep, "__")


# ---------------------------------------------------------------- new (Studio/Qt)
def _render_new(out_dir, limit):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    sys.path.insert(0, _STUDIO)
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    from swi3s_studio.model import viz_engine
    from swi3s_studio.ui.grid_view import GridView

    dest = os.path.join(out_dir, "new")
    os.makedirs(dest, exist_ok=True)
    for rel, csv in _configs(limit):
        try:
            cfg_rows = None
            cells, clashes, _issues, ncols, nrows = viz_engine.render_payload(csv, cfg_rows)
            gv = GridView()
            gv.set_bus_model(cells, clashes, ncols, nrows)
            gv.export_image(os.path.join(dest, _safe(rel) + ".svg"))
        except Exception as exc:  # noqa: BLE001
            print(f"  new FAILED {rel}: {exc}", file=sys.stderr)


# ---------------------------------------------------------------- old (Visualizer/Tk)
def _render_old(out_dir, limit):
    if not os.path.isdir(_SIBLING):
        print(f"  (sibling Visualizer repo not found at {_SIBLING}; skipping 'old')",
              file=sys.stderr)
        return
    sys.path.insert(0, _SIBLING)
    import logging
    import tkinter as tk

    import canvasvg
    from src.core.engine import BusModelBuilder
    from src.io.csv_handler import CSVHandler
    from src.models.interface import Interface
    from src.ui.frame_renderer import FrameRenderer, RenderConfig
    from src.viz import VizConfig
    logging.disable(logging.CRITICAL)
    root = tk.Tk()
    root.withdraw()
    dest = os.path.join(out_dir, "old")
    os.makedirs(dest, exist_ok=True)
    renderer = FrameRenderer()
    for rel, csv in _configs(limit):
        try:
            iface, viz = Interface(), VizConfig()
            CSVHandler.load_csv(csv, iface, viz)
            bm = BusModelBuilder(iface, viz.rows_to_draw, viz).build()
            canvas = tk.Canvas(root, width=4000, height=4000, bg="#e0e0e0")
            renderer.render(bm, canvas, RenderConfig())
            canvas.update_idletasks()
            canvasvg.saveall(os.path.join(dest, _safe(rel) + ".svg"), canvas)
            canvas.destroy()
        except Exception as exc:  # noqa: BLE001
            print(f"  old FAILED {rel}: {exc}", file=sys.stderr)
    root.destroy()


# ---------------------------------------------------------------- HTML assembly
_PAGE = """<!doctype html><meta charset="utf-8"><title>SWI3S Bus-Visualizer — SVG new vs old</title>
<style>
 body{{font-family:-apple-system,Helvetica,Arial,sans-serif;background:#1e1f22;color:#ddd;margin:0;padding:16px}}
 h1{{font-size:18px}} h2{{margin:24px 0 6px;color:#9ab;border-bottom:1px solid #444;padding-bottom:4px}}
 .row{{display:flex;gap:12px;align-items:flex-start;border-bottom:1px solid #333;padding:10px 0}}
 .name{{flex:0 0 230px;font:13px monospace;color:#cdd;word-break:break-word}}
 .cell{{flex:1;min-width:0}} .cell .lbl{{font:11px monospace;color:#888;margin-bottom:3px}}
 .cell img{{max-width:100%;background:#2b2d31;border:1px solid #444}}
 .old img{{background:#e0e0e0}} .miss{{color:#a55;font:12px monospace}}
 .toc a{{color:#9cf;margin-right:10px;font:12px monospace}}
</style>
<h1>SWI3S Bus-Visualizer — SVG new (Studio) vs old (Visualizer) · {n} configs</h1>
<div class="toc">{toc}</div>
{body}
"""


def _assemble(out_dir, limit):
    rows_by_cat = {}
    for rel, _csv in _configs(limit):
        cat = rel.split(os.sep)[0] if os.sep in rel else "."
        new = os.path.join("new", _safe(rel) + ".svg")
        old = os.path.join("old", _safe(rel) + ".svg")

        def cell(kind, path, css):
            full = os.path.join(out_dir, path)
            if os.path.exists(full):
                return f'<div class="cell {css}"><div class="lbl">{kind}</div><img src="{path}"></div>'
            return f'<div class="cell {css}"><div class="lbl">{kind}</div><span class="miss">— not rendered —</span></div>'

        rows_by_cat.setdefault(cat, []).append(
            f'<div class="row"><div class="name">{rel}</div>'
            f'{cell("new — Studio", new, "new")}{cell("old — Visualizer", old, "old")}</div>')

    body, toc = [], []
    for cat in sorted(rows_by_cat):
        anchor = cat.replace(".", "root")
        toc.append(f'<a href="#{anchor}">{cat} ({len(rows_by_cat[cat])})</a>')
        body.append(f'<h2 id="{anchor}">{cat}</h2>' + "".join(rows_by_cat[cat]))
    html = _PAGE.format(n=sum(len(v) for v in rows_by_cat.values()),
                        toc=" ".join(toc), body="".join(body))
    path = os.path.join(out_dir, "svg_compare.html")
    with open(path, "w") as f:
        f.write(html)
    return path


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default=os.path.join(_STUDIO, "svg_compare_out"))
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--phase", choices=["new", "old"], help=argparse.SUPPRESS)
    args = ap.parse_args()

    if args.phase == "new":
        _render_new(args.out, args.limit)
        return 0
    if args.phase == "old":
        _render_old(args.out, args.limit)
        return 0

    os.makedirs(args.out, exist_ok=True)
    n = len(_configs(args.limit))
    for phase in ("new", "old"):
        print(f"Rendering {phase} ({n} configs)…")
        cmd = [sys.executable, os.path.abspath(__file__), "--phase", phase,
               "--out", args.out]
        if args.limit:
            cmd += ["--limit", str(args.limit)]
        env = dict(os.environ, QT_QPA_PLATFORM="offscreen")
        subprocess.run(cmd, env=env, cwd=_STUDIO)
    page = _assemble(args.out, args.limit)
    print(f"\nWrote {page}\n  open {page}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
