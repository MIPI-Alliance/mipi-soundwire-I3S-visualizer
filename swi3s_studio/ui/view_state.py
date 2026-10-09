"""Reading and re-imposing the user-adjustable state of the Analysis panes, as JSON-able
dicts, so a Link keeps its view while another is shown and a workspace brings the whole
view back.

One function pair per pane: `<pane>_state(view) -> dict` and `restore_<pane>(view, d)`.
A restore tolerates a missing or malformed entry (a hand-edited workspace, or one saved by
an older version) by leaving that part as the pane has it, and it touches only what is
still there: a channel that a re-decode dropped is skipped, not invented.
"""
from __future__ import annotations

from typing import Any, Optional


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


def restore_audio(view, d: Any, zoom: bool = True) -> None:
    """`zoom=False` leaves the X range alone: All Links lanes share one window."""
    if not isinstance(d, dict):
        return
    try:
        chans = {(int(a), int(b), int(c)) for a, b, c in d.get("channels") or ()}
    except (TypeError, ValueError):
        chans = set()
    view.set_channel_selection(chans)
    try:
        spectro = {(int(a), int(b), int(c)) for a, b, c in d.get("spectrogram") or ()}
    except (TypeError, ValueError):
        spectro = None
    try:
        frames = {(int(a), int(b), int(c)): int(n)
                  for a, b, c, n in d.get("spectrogram_frames") or ()}
    except (TypeError, ValueError):
        frames = None
    if frames is not None:
        view.set_spectrogram_frames(frames)          # before the channels: one redraw
    if spectro is not None:
        view.set_spectrogram_channels(spectro)
    have = set(view.streams())
    for row in d.get("decimation") or ():
        try:
            dev, dp, hz = int(row[0]), int(row[1]), int(row[2])
        except (TypeError, ValueError, IndexError):
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


def restore_capture(view, d: Any, zoom: bool = True) -> None:
    if not isinstance(d, dict):
        return
    for key, attr in _CAPTURE_TOGGLES:
        if key in d:
            btn = getattr(view, attr)
            if btn.isEnabled() and bool(d[key]) != btn.isChecked():
                btn.setChecked(bool(d[key]))
    rng = _pair(d.get("x_range_s"))
    if zoom and rng is not None:
        view.set_x_range_seconds(*rng)


# ---- Commands table: sort and hidden columns, by header name (All Links adds a column) ----
def _headers(table) -> list:
    from PySide6.QtCore import Qt
    model = table.model()
    n = model.columnCount() if model is not None else 0
    return [str(model.headerData(c, Qt.Horizontal)) for c in range(n)]


def commands_header_state(table) -> dict:
    names = _headers(table)
    head = table.horizontalHeader()
    sec = head.sortIndicatorSection()
    sort = None
    if table.isSortingEnabled() and 0 <= sec < len(names):
        sort = [names[sec], int(head.sortIndicatorOrder().value)]
    return {"hidden": [n for c, n in enumerate(names) if table.isColumnHidden(c)],
            "sort": sort}


def restore_commands_header(table, d: Any) -> None:
    from PySide6.QtCore import Qt
    if not isinstance(d, dict):
        return
    names = _headers(table)
    hidden = set(d.get("hidden") or ()) if isinstance(d.get("hidden"), list) else set()
    for c, n in enumerate(names):
        table.setColumnHidden(c, n in hidden)
    sort = d.get("sort")
    if isinstance(sort, list) and len(sort) == 2 and sort[0] in names \
            and table.isSortingEnabled():
        try:
            order = Qt.SortOrder(int(sort[1]))
        except (TypeError, ValueError):
            return
        table.sortByColumn(names.index(sort[0]), order)


# ---- Samples filters ----
def samples_state(view) -> dict:
    return {"ports": sorted([int(d), int(p)] for d, p in view._port_cb.selected()),
            "channels": sorted(int(c) for c in view._chan_cb.selected()),
            "op": view._op_cb.currentText(), "value": view._val_edit.text()}


def restore_samples(view, d: Any) -> None:
    if not isinstance(d, dict):
        return
    try:
        ports = {(int(a), int(b)) for a, b in d.get("ports") or ()}
        chans = {int(c) for c in d.get("channels") or ()}
    except (TypeError, ValueError):
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
    view._op_cb.blockSignals(False)
    view._val_edit.blockSignals(True)
    view._val_edit.setText(str(d.get("value", "")))
    view._val_edit.blockSignals(False)
    view.filtersChanged.emit()


# ---- Registers: the device shown and the blocks opened ----
def registers_state(view) -> dict:
    root = view._tree.invisibleRootItem()
    return {"device": view.current_device(),
            "expanded": sorted(root.child(i).text(0) for i in range(root.childCount())
                               if root.child(i).isExpanded())}


def restore_registers(view, d: Any) -> None:
    if not isinstance(d, dict):
        return
    if d.get("device") is not None:
        i = view._device.findData(d.get("device"))
        if i >= 0 and i != view._device.currentIndex():
            view._device.setCurrentIndex(i)
    expanded = d.get("expanded")
    if isinstance(expanded, list):
        root = view._tree.invisibleRootItem()
        for i in range(root.childCount()):
            it = root.child(i)
            it.setExpanded(it.text(0) in expanded)


# ---- Statistics: folded sections and opened groups, by title ----
def statistics_state(view) -> dict:
    return {"collapsed": sorted(view._collapsed_titles),
            "expanded": sorted(view._expanded_groups)}


def restore_statistics(view, d: Any) -> None:
    if not isinstance(d, dict):
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


def restore_timing(view, d: Any) -> None:
    if not isinstance(d, dict):
        return
    i = view._region.findText(str(d.get("region", "")))
    if i >= 0 and i != view._region.currentIndex():
        view._region.setCurrentIndex(i)               # rebuilds the filter for the region
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
            pass
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
