"""Reading and re-imposing the user-adjustable state of the Analysis panes, as JSON-able
dicts, so a Link keeps its view while another is shown and a workspace brings the whole
view back.

One function pair per pane: `<pane>_state(view) -> dict` and `restore_<pane>(view, d)`.
A restore tolerates a missing or malformed entry (a hand-edited workspace, or one saved by
an older version) by leaving that part as the pane has it, and it touches only what is
still there: a channel that a re-decode dropped is skipped, not invented.

A restore given a `source` (a workspace file's name) reports each malformed entry it
leaves out through import_notes, once per pane; without one it is silent, as when a Link's
own kept view is put back on a switch.
"""
from __future__ import annotations

from typing import Any, List, Optional

from .. import import_notes


def _report(source: Optional[str], pane: str, bad: List[str]) -> None:
    if source is not None and bad:
        import_notes.warn(source, f"the saved {pane} view has entries that do not read, left "
                                  f"as the pane has them: {', '.join(bad)}")


def _pair(v) -> Optional[tuple]:
    try:
        a, b = float(v[0]), float(v[1])
    except (TypeError, ValueError, IndexError):
        return None
    return (a, b) if b > a else None


# ---- Audio (one view per lane) ----
def audio_state(view) -> dict:
    return {
        "channels": sorted([int(d), int(p), int(c)] for d, p, c in view.channel_selection()),
        "x_range_s": list(view.x_range_seconds() or ()),
        "vertical_zoom": bool(view._vzoom_btn.isChecked()),
        "decimation": sorted([int(d), int(p), int(hz)]
                             for (d, p), hz in view.play_rate_targets.items()),
        "channel_list_width": int(view._splitter.sizes()[0]),   # the rest is the tracks
        "spectrogram": sorted([int(d), int(p), int(c)] for d, p, c in view.spectrogram_channels()),
        "spectrogram_frames": sorted([int(d), int(p), int(c), int(n)]
                                     for (d, p, c), n in view.spectrogram_frames().items()),
    }


def restore_audio(view, d: Any, zoom: bool = True, source: Optional[str] = None) -> None:
    """`zoom=False` leaves the X range alone: All Links lanes share one window."""
    if not isinstance(d, dict):
        _report(source, "Audio", ["the whole entry"] if d is not None else [])
        return
    bad: List[str] = []
    try:
        chans = {(int(a), int(b), int(c)) for a, b, c in d.get("channels") or ()}
    except (TypeError, ValueError):
        chans = set()
        bad.append("channels")
    view.set_channel_selection(chans)
    try:
        spectro = {(int(a), int(b), int(c)) for a, b, c in d.get("spectrogram") or ()}
    except (TypeError, ValueError):
        spectro = None
        bad.append("spectrogram")
    try:
        frames = {(int(a), int(b), int(c)): int(n)
                  for a, b, c, n in d.get("spectrogram_frames") or ()}
    except (TypeError, ValueError):
        frames = None
        bad.append("spectrogram_frames")
    if frames is not None:
        view.set_spectrogram_frames(frames)          # before the channels: one redraw
    if spectro is not None:
        view.set_spectrogram_channels(spectro)
    have = set(view.streams())
    for row in d.get("decimation") or ():
        try:
            dev, dp, hz = int(row[0]), int(row[1]), int(row[2])
        except (TypeError, ValueError, IndexError):
            if "decimation" not in bad:
                bad.append("decimation")
            continue
        if (dev, dp) in have:
            view.set_play_rate_target(dev, dp, hz)
    if bool(d.get("vertical_zoom")) != view._vzoom_btn.isChecked():
        view._vzoom_btn.setChecked(bool(d.get("vertical_zoom")))
    width = d.get("channel_list_width")
    if isinstance(width, int) and width >= 0:
        total = sum(view._splitter.sizes())
        view._splitter.setSizes([width, max(1, total - width)])
    rng = _pair(d.get("x_range_s"))
    if rng is None and d.get("x_range_s"):
        bad.append("x_range_s")
    _report(source, "Audio", bad)
    if zoom and rng is not None:
        view.set_x_range_seconds(*rng)              # last: a rebuild above refits the zoom


# ---- Capture (one view per lane) ----
_CAPTURE_TOGGLES = (("rsp_markers", "_cds_btn"), ("samples", "_samp_btn"),
                    ("hide_legend", "_legend_btn"))


def capture_state(view) -> dict:
    out: dict = {"x_range_s": list(view.x_range_seconds())}
    for key, attr in _CAPTURE_TOGGLES:
        out[key] = bool(getattr(view, attr).isChecked())
    return out


def restore_capture(view, d: Any, zoom: bool = True, source: Optional[str] = None) -> None:
    if not isinstance(d, dict):
        _report(source, "Capture", ["the whole entry"] if d is not None else [])
        return
    for key, attr in _CAPTURE_TOGGLES:
        if key in d:
            btn = getattr(view, attr)
            if btn.isEnabled() and bool(d[key]) != btn.isChecked():
                btn.setChecked(bool(d[key]))
    rng = _pair(d.get("x_range_s"))
    _report(source, "Capture", ["x_range_s"] if rng is None and d.get("x_range_s") else [])
    if zoom and rng is not None:
        view.set_x_range_seconds(*rng)


# ---- Commands table: sort and hidden columns, by header name (All Links adds a column) ----
def _headers(table) -> list:
    from PySide6.QtCore import Qt
    model = table.model()
    n = model.columnCount() if model is not None else 0
    return [str(model.headerData(c, Qt.Horizontal)) for c in range(n)]


def commands_header_state(table) -> dict:
    from .command_table import UNSORTABLE
    names = _headers(table)
    head = table.horizontalHeader()
    sec = head.sortIndicatorSection()
    sort = None
    if table.isSortingEnabled() and 0 <= sec < len(names) and names[sec] not in UNSORTABLE:
        sort = [names[sec], int(head.sortIndicatorOrder().value)]
    # "columns" names every column the table had when saved, so a column added since (the
    # delta columns, 3.1.0) keeps its default on reopen rather than counting as shown.
    return {"hidden": [n for c, n in enumerate(names) if table.isColumnHidden(c)],
            "columns": names, "sort": sort}


def restore_commands_header(table, d: Any, source: Optional[str] = None) -> None:
    from PySide6.QtCore import Qt
    if not isinstance(d, dict):
        _report(source, "Commands", ["the column header"] if d is not None else [])
        return
    names = _headers(table)
    if d.get("hidden") is not None and not isinstance(d.get("hidden"), list):
        _report(source, "Commands", ["hidden"])
    hidden = set(d.get("hidden") or ()) if isinstance(d.get("hidden"), list) else set()
    if hasattr(table, "set_hidden_names"):
        from .command_table import UNRECORDED_COLUMNS
        flat = [n.replace("\n", " ") for n in names]
        saved = {h.replace("\n", " ") for h in hidden}
        # Columns the workspace knew follow it; one it has no setting for keeps the table's
        # default. A workspace without "columns" has a setting for every column but
        # UNRECORDED_COLUMNS.
        known = ({c.replace("\n", " ") for c in d["columns"]}
                 if isinstance(d.get("columns"), list) else set(flat) - UNRECORDED_COLUMNS)
        table.set_hidden_names({n for n in flat if n in saved and n in known}
                               | {n for n in flat if n not in known
                                  and n in table.hidden_names()})
    else:
        for c, n in enumerate(names):
            table.setColumnHidden(c, n in hidden)
    from .command_table import UNSORTABLE
    if not table.isSortingEnabled():
        return
    # A saved sort the table can apply is applied; anything else (None, the capture-order
    # state, or a column that is UNSORTABLE, unknown or malformed) puts the rows back in
    # capture order, so a sort made before the restore does not stay on.
    sort = d.get("sort")
    column, order = -1, Qt.AscendingOrder
    if isinstance(sort, list) and len(sort) == 2 and sort[0] in names \
            and sort[0] not in UNSORTABLE:
        try:
            column, order = names.index(sort[0]), Qt.SortOrder(int(sort[1]))
        except (TypeError, ValueError):
            column, order = -1, Qt.AscendingOrder
    if sort is not None and column < 0 and source is not None:
        what = (repr(sort[0]) if isinstance(sort, list) and len(sort) == 2
                and isinstance(sort[0], str) else repr(sort))
        import_notes.warn(source, f"the saved Commands sort {what} is not a sortable column "
                                  f"of the table; the rows are in capture order")
    table.sortByColumn(column, order)


# ---- Samples filters ----
def samples_state(view) -> dict:
    return {"ports": sorted([int(d), int(p)] for d, p in view._port_cb.selected()),
            "channels": sorted(int(c) for c in view._chan_cb.selected()),
            "op": view._op_cb.currentText(), "value": view._val_edit.text()}


def restore_samples(view, d: Any, source: Optional[str] = None) -> None:
    if not isinstance(d, dict):
        _report(source, "Samples", ["the whole entry"] if d is not None else [])
        return
    try:
        ports = {(int(a), int(b)) for a, b in d.get("ports") or ()}
        chans = {int(c) for c in d.get("channels") or ()}
    except (TypeError, ValueError):
        _report(source, "Samples", ["ports or channels"])
        return
    for box, want in ((view._port_cb, ports), (view._chan_cb, chans)):
        for act in box._menu.actions():
            act.blockSignals(True)
            act.setChecked(tuple(act.data()) in want if isinstance(act.data(), (tuple, list))
                           else act.data() in want)
            act.blockSignals(False)
        box._update_text()
    view._op_cb.blockSignals(True)
    i = view._op_cb.findText(str(d.get("op", "")))
    if i >= 0:
        view._op_cb.setCurrentIndex(i)
    elif d.get("op"):
        _report(source, "Samples", [f"op {d.get('op')!r}"])
    view._op_cb.blockSignals(False)
    view._val_edit.blockSignals(True)
    view._val_edit.setText(str(d.get("value", "")))
    view._val_edit.blockSignals(False)
    view.filtersChanged.emit()


# ---- Registers: the device shown, the blocks opened (by id), the filter, the column widths ----
def registers_state(view) -> dict:
    return {"device": view.current_device(),
            "expanded": sorted(view.expanded_blocks()),
            "hidden": sorted(p.value for p in view.hidden_provenances()),
            "col_widths": view.column_widths()}


def restore_registers(view, d: Any, source: Optional[str] = None) -> None:
    if not isinstance(d, dict):
        _report(source, "Registers", ["the whole entry"] if d is not None else [])
        return
    if d.get("device") is not None:
        i = view._device.findData(d.get("device"))
        if i >= 0 and i != view._device.currentIndex():
            view._device.setCurrentIndex(i)
        elif i < 0:
            _report(source, "Registers", [f"device {d.get('device')!r}"])
    view.set_hidden_provenances(())           # the open blocks are the unfiltered ones
    expanded = d.get("expanded")
    if isinstance(expanded, list):
        view.set_expanded_blocks(expanded)    # block ids; titles, as older workspaces saved
    hidden = d.get("hidden")
    if isinstance(hidden, list):
        from ..model import Provenance
        known = {p.value: p for p in Provenance}
        view.set_hidden_provenances(known[v] for v in hidden if v in known)
        _report(source, "Registers", [f"filter {v!r}" for v in hidden if v not in known])
    # Column widths by title; without them (a workspace that does not record them) the
    # columns are fitted to their content.
    widths = d.get("col_widths")
    if widths is not None and not isinstance(widths, dict):
        _report(source, "Registers", ["col_widths"])
    widths = widths if isinstance(widths, dict) else {}
    titles = set(view.column_titles())
    _report(source, "Registers", [f"width of {t!r}" for t, w in widths.items()
                                  if str(t) not in titles or not isinstance(w, int)
                                  or isinstance(w, bool) or w <= 0])
    view.set_column_widths(widths)


# ---- Statistics: folded sections and opened groups, by title ----
def statistics_state(view) -> dict:
    return {"collapsed": sorted(view._collapsed_titles),
            "expanded": sorted(view._expanded_groups)}


def restore_statistics(view, d: Any, source: Optional[str] = None) -> None:
    if not isinstance(d, dict):
        _report(source, "Statistics", ["the whole entry"] if d is not None else [])
        return
    for attr, key in (("_collapsed_titles", "collapsed"), ("_expanded_groups", "expanded")):
        if isinstance(d.get(key), list):
            setattr(view, attr, {str(t) for t in d[key]})
    view._apply_folds()


# ---- Timing (eye): the region, the driver filter, the hidden edge kinds ----
def timing_state(view) -> dict:
    return {"region": view._region.currentText(),
            "filter": [item["action"].text() for item in view._filter_items
                       if item["action"].isChecked()],
            "hidden": sorted([bool(a), bool(b)] for a, b in view._cat_hidden)}


def restore_timing(view, d: Any, source: Optional[str] = None) -> None:
    if not isinstance(d, dict):
        _report(source, "Timing", ["the whole entry"] if d is not None else [])
        return
    i = view._region.findText(str(d.get("region", "")))
    if i >= 0 and i != view._region.currentIndex():
        view._region.setCurrentIndex(i)               # rebuilds the filter for the region
    elif i < 0 and d.get("region"):
        _report(source, "Timing", [f"region {d.get('region')!r}"])
    want = d.get("filter")
    if isinstance(want, list) and view._filter_items:
        for item in view._filter_items:
            act = item["action"]
            act.blockSignals(True)
            act.setChecked(act.text() in want)
            act.blockSignals(False)
        view._on_filter_changed(redraw=False)
    if isinstance(d.get("hidden"), list):
        try:
            view._cat_hidden = {(bool(a), bool(b)) for a, b in d["hidden"]}
        except (TypeError, ValueError):
            _report(source, "Timing", ["hidden"])
        view._update_legend()
    view._redraw()


# ---- Commands filter (MainWindow._filter_snapshot) in JSON: sets become sorted lists ----
def filters_to_json(snap: Any) -> Optional[dict]:
    if not isinstance(snap, dict):
        return None
    out = dict(snap)
    for key in ("kinds", "devices", "groups"):
        out[key] = sorted((list(v) if isinstance(v, tuple) else v for v in snap.get(key) or ()),
                          key=repr)
    return out


def filters_from_json(d: Any) -> Optional[dict]:
    if not isinstance(d, dict):
        return None
    try:
        out: dict = {key: {tuple(v) if isinstance(v, list) else v for v in d.get(key) or ()}
                     for key in ("kinds", "devices", "groups")}
    except TypeError:
        return None
    out.update(kinds_none=bool(d.get("kinds_none")), errors_only=bool(d.get("errors_only")),
               text=str(d.get("text") or ""))
    return out
