"""Reading and re-imposing the user-adjustable state of the Analysis panes, as JSON-able
dicts, so a Link keeps its view while another is shown and a workspace brings the whole
view back.

One function pair per pane: `<pane>_state(view) -> dict` and `restore_<pane>(view, d)`.
A restore tolerates a missing or malformed entry (a hand-edited workspace, or one written by
another version) by leaving that part as the pane has it, and it touches only what is
still there: a channel that a re-decode dropped is skipped, not invented. A Commands column
list or a Timing filter list some of whose entries do not read applies the entries that do,
and the note says so.

A restore given a `source` (a workspace file's name) reports the malformed entries it
leaves out through import_notes in one note per pane, and a Commands sort it replaces with
capture order in a note of its own (the Commands filter reports as the pane "Commands
filter"); without a source it is silent, as when a Link's own kept view is put back on a
switch. A number is read only as a JSON number: an
Infinity, a fraction where a whole number is meant, text, a bool or a value outside the range
the pane can hold is malformed. An on/off setting is read only as a JSON true or false.
"""
from __future__ import annotations

import math
from typing import Any, List, Optional

from .. import import_notes

MAX_PX = 1 << 16                 # the widest column or pane a saved width may name
_MAX_INT = 2 ** 31 - 1           # a device, port, channel or rate number is a 32-bit value
MAX_TIME_S = 1e9                 # a saved time range lies within ±MAX_TIME_S seconds
# What a malformed saved number raises out of int() / float() / unpacking.
_BAD_VALUE = (TypeError, ValueError, IndexError, KeyError, OverflowError)


def whole_number(v) -> Optional[int]:
    """`v` as an int when it is a JSON number with no fraction (an int, or a finite float
    that is whole); None for text, a bool, a fraction, NaN and Infinity."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    if isinstance(v, float) and not (math.isfinite(v) and v == int(v)):
        return None
    return int(v)


def bounded_number(v, lo: int, hi: int) -> Optional[int]:
    """`whole_number(v)` when it lies in lo..hi, else None."""
    n = whole_number(v)
    return n if n is not None and lo <= n <= hi else None


def valid_width(w) -> bool:
    """Whether `w` is a saved column or pane width: a whole JSON number of 1..MAX_PX
    pixels (int(w) is the width)."""
    return bounded_number(w, 1, MAX_PX) is not None


def _ints(row, n: int) -> tuple:
    """The `n` entries of `row` as ints; raises one of `_BAD_VALUE` for a row of another
    length or an entry that is not a whole JSON number of 32 bits."""
    if len(row) != n:
        raise IndexError(n)
    out = tuple(bounded_number(row[i], -_MAX_INT - 1, _MAX_INT) for i in range(n))
    if None in out:
        raise ValueError(row)
    return out


def _flag(d: dict, key: str, bad: List[str]) -> Optional[bool]:
    """d[key] when it is a JSON true or false; None when absent, and None with `key` added
    to `bad` when it is anything else."""
    v = d.get(key)
    if v is None or isinstance(v, bool):
        return v
    bad.append(key)
    return None


def _report(source: Optional[str], pane: str, bad: List[str],
            partial: Optional[List[str]] = None) -> None:
    """One note for `pane`: the entries in `bad` are left as the pane has them, and those
    in `partial` are applied where they read, the rest of each ignored."""
    if source is None or not (bad or partial):
        return
    parts = []
    if bad:
        parts.append(f"left as the pane has them: {', '.join(bad)}")
    if partial:
        parts.append(f"applied where they read, the rest ignored: {', '.join(partial)}")
    import_notes.warn(source, f"the saved {pane} view has entries that do not read, "
                              + "; ".join(parts))


def _pair(v) -> Optional[tuple]:
    """A saved (start, end) time range in seconds: two JSON numbers within ±MAX_TIME_S,
    end after start; else None."""
    if not (isinstance(v, (list, tuple)) and len(v) == 2):
        return None
    if any(isinstance(x, bool) or not isinstance(x, (int, float)) for x in v):
        return None
    try:
        a, b = float(v[0]), float(v[1])
    except _BAD_VALUE:
        return None
    return (a, b) if -MAX_TIME_S <= a < b <= MAX_TIME_S else None


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
        chans = {_ints(r, 3) for r in d.get("channels") or ()}
    except _BAD_VALUE:
        chans = set()
        bad.append("channels")
    view.set_channel_selection(chans)
    try:
        spectro = {_ints(r, 3) for r in d.get("spectrogram") or ()}
    except _BAD_VALUE:
        spectro = None
        bad.append("spectrogram")
    try:
        frames = {t[:3]: t[3] for t in (_ints(r, 4) for r in d.get("spectrogram_frames") or ())}
    except _BAD_VALUE:
        frames = None
        bad.append("spectrogram_frames")
    if frames is not None:
        view.set_spectrogram_frames(frames)          # before the channels: one redraw
    if spectro is not None:
        view.set_spectrogram_channels(spectro)
    have = set(view.streams())
    rows = d.get("decimation")
    if rows is None:
        rows = ()
    if not isinstance(rows, list):
        rows = ()
        bad.append("decimation")
    for row in rows:
        try:
            dev, dp, hz = _ints(row, 3)
            if hz <= 0:
                raise ValueError(hz)
        except _BAD_VALUE:
            if "decimation" not in bad:
                bad.append("decimation")
            continue
        if (dev, dp) in have:
            view.set_play_rate_target(dev, dp, hz)
    vzoom = _flag(d, "vertical_zoom", bad)
    if vzoom is not None and vzoom != view._vzoom_btn.isChecked():
        view._vzoom_btn.setChecked(vzoom)
    width = d.get("channel_list_width")
    if width is not None and bounded_number(width, 0, MAX_PX) is None:
        bad.append("channel_list_width")
    elif width is not None:
        width = int(width)
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
    bad: List[str] = []
    for key, attr in _CAPTURE_TOGGLES:
        on = _flag(d, key, bad)
        if on is not None:
            btn = getattr(view, attr)
            if btn.isEnabled() and on != btn.isChecked():
                btn.setChecked(on)
    rng = _pair(d.get("x_range_s"))
    if rng is None and d.get("x_range_s"):
        bad.append("x_range_s")
    _report(source, "Capture", bad)
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
    # "columns" names every column the table had when saved; a column the table has that
    # the list lacks keeps its default on reopen rather than counting as shown.
    return {"hidden": [n for c, n in enumerate(names) if table.isColumnHidden(c)],
            "columns": names, "sort": sort}


def restore_commands_header(table, d: Any, source: Optional[str] = None) -> None:
    from PySide6.QtCore import Qt
    if not isinstance(d, dict):
        _report(source, "Commands", ["the column header"] if d is not None else [])
        return
    names = _headers(table)
    raw_hidden, raw_cols = d.get("hidden"), d.get("columns")
    # A 'hidden' that is not a list leaves the columns hidden as the pane has them; a list
    # hides its text entries. The text entries of 'columns' are the columns the workspace
    # knew, and a 'columns' that is not a list reads as one the file does not have.
    bad: List[str] = []
    partial: List[str] = []
    if raw_hidden is not None and not isinstance(raw_hidden, list):
        bad.append("hidden")
    elif isinstance(raw_hidden, list) and not all(isinstance(h, str) for h in raw_hidden):
        partial.append("hidden")
    if raw_cols is not None and not (isinstance(raw_cols, list)
                                     and all(isinstance(c, str) for c in raw_cols)):
        partial.append("columns")
    _report(source, "Commands", bad, partial)
    if isinstance(raw_hidden, list):
        _restore_hidden_columns(table, names, {h for h in raw_hidden if isinstance(h, str)},
                                raw_cols)
    from .command_table import UNSORTABLE
    if not table.isSortingEnabled():
        return
    # A saved sort the table can apply is applied; anything else (None, the capture-order
    # state, a column that is UNSORTABLE, unknown or malformed, or an order that is not the
    # whole number 0 or 1) puts the rows back in capture order, so a sort made before the
    # restore does not stay on.
    sort = d.get("sort")
    column, order = -1, Qt.AscendingOrder
    pair = sort if isinstance(sort, list) and len(sort) == 2 else None
    sortable = pair is not None and pair[0] in names and pair[0] not in UNSORTABLE
    direction = bounded_number(pair[1], 0, 1) if pair is not None else None
    if pair is not None and sortable and direction is not None:
        column, order = names.index(pair[0]), Qt.SortOrder(direction)
    if sort is not None and column < 0 and source is not None:
        if sortable:
            import_notes.warn(source, f"the saved Commands sort order {sort[1]!r} is not 0 "
                                      f"or 1; the rows are in capture order")
        else:
            what = repr(sort[0]) if pair and isinstance(sort[0], str) else repr(sort)
            import_notes.warn(source, f"the saved Commands sort {what} is not a sortable "
                                      f"column of the table; the rows are in capture order")
    table.sortByColumn(column, order)


def _restore_hidden_columns(table, names: list, hidden: set, raw_cols) -> None:
    """Hide the Commands columns named in `hidden`, of those the workspace knew (its
    `raw_cols`); a column it has no setting for keeps the table's default. A workspace
    without "columns" has a setting for every column but UNRECORDED_COLUMNS."""
    if not hasattr(table, "set_hidden_names"):
        for c, n in enumerate(names):
            table.setColumnHidden(c, n in hidden)
        return
    from .command_table import UNRECORDED_COLUMNS
    flat = [n.replace("\n", " ") for n in names]
    saved = {h.replace("\n", " ") for h in hidden}
    known = ({c.replace("\n", " ") for c in raw_cols if isinstance(c, str)}
             if isinstance(raw_cols, list) else set(flat) - UNRECORDED_COLUMNS)
    table.set_hidden_names({n for n in flat if n in saved and n in known}
                           | {n for n in flat if n not in known
                              and n in table.hidden_names()})


# ---- Samples filters ----
def samples_state(view) -> dict:
    return {"ports": sorted([int(d), int(p)] for d, p in view._port_cb.selected()),
            "channels": sorted(int(c) for c in view._chan_cb.selected()),
            "op": view._op_cb.currentText(), "value": view._val_edit.text()}


def restore_samples(view, d: Any, source: Optional[str] = None) -> None:
    if not isinstance(d, dict):
        _report(source, "Samples", ["the whole entry"] if d is not None else [])
        return
    bad: List[str] = []
    try:
        ports = {_ints(r, 2) for r in d.get("ports") or ()}
        chans = {_ints([c], 1)[0] for c in d.get("channels") or ()}
    except _BAD_VALUE:
        bad.append("ports or channels")       # the boxes stay; op and value still apply
    else:
        for box, want in ((view._port_cb, ports), (view._chan_cb, chans)):
            for act in box._menu.actions():
                act.blockSignals(True)
                act.setChecked(tuple(act.data()) in want
                               if isinstance(act.data(), (tuple, list)) else act.data() in want)
                act.blockSignals(False)
            box._update_text()
    op, value = d.get("op"), d.get("value")
    view._op_cb.blockSignals(True)
    i = view._op_cb.findText(op) if isinstance(op, str) else -1
    if i >= 0:
        view._op_cb.setCurrentIndex(i)
    elif "op" in d and op != "":
        bad.append(f"op {op!r}")
    view._op_cb.blockSignals(False)
    if isinstance(value, str):
        view._val_edit.blockSignals(True)
        view._val_edit.setText(value)
        view._val_edit.blockSignals(False)
    elif "value" in d:
        bad.append("value")
    _report(source, "Samples", bad)
    view.check_value()
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
    bad: List[str] = []
    if d.get("device") is not None:
        dev = bounded_number(d.get("device"), -_MAX_INT - 1, _MAX_INT)
        i = view._device.findData(dev) if dev is not None else -1
        if i >= 0 and i != view._device.currentIndex():
            view._device.setCurrentIndex(i)
        elif i < 0:
            bad.append(f"device {d.get('device')!r}")
    hidden = d.get("hidden")
    # The open blocks are the unfiltered ones: the filter is off while they are opened, then
    # takes the saved setting, or the pane's own when the saved one is missing or malformed.
    kept_filter = () if isinstance(hidden, list) else tuple(view.hidden_provenances())
    view.set_hidden_provenances(())
    expanded = d.get("expanded")
    if isinstance(expanded, list):
        names = [v for v in expanded if isinstance(v, str)]
        view.set_expanded_blocks(names)       # each entry is a block id or a block title
        bad.extend(f"open block {v!r}" for v in expanded if not isinstance(v, str))
    elif expanded is not None:
        bad.append("expanded")
    if isinstance(hidden, list):
        from ..model import Provenance
        known = {p.value: p for p in Provenance}
        names = [v for v in hidden if isinstance(v, str)]
        view.set_hidden_provenances(known[v] for v in names if v in known)
        bad.extend(f"filter {v!r}" for v in hidden if not isinstance(v, str) or v not in known)
    else:
        view.set_hidden_provenances(kept_filter)
        if hidden is not None:
            bad.append("hidden")
    # Column widths by title; without them (a workspace that does not record them) the
    # columns take the pane's default widths.
    widths = d.get("col_widths")
    if widths is not None and not isinstance(widths, dict):
        bad.append("col_widths")
    widths = widths if isinstance(widths, dict) else {}
    titles = set(view.column_titles())
    bad.extend(f"width of {t!r}" for t, w in widths.items()
               if str(t) not in titles or not valid_width(w))
    view.set_column_widths(widths)
    _report(source, "Registers", bad)


# ---- Statistics: folded sections and opened groups, by title ----
def statistics_state(view) -> dict:
    return {"collapsed": sorted(view._collapsed_titles),
            "expanded": sorted(view._expanded_groups)}


def restore_statistics(view, d: Any, source: Optional[str] = None) -> None:
    if not isinstance(d, dict):
        _report(source, "Statistics", ["the whole entry"] if d is not None else [])
        return
    bad: List[str] = []
    for attr, key in (("_collapsed_titles", "collapsed"), ("_expanded_groups", "expanded")):
        if isinstance(d.get(key), list):
            setattr(view, attr, {t for t in d[key] if isinstance(t, str)})
            if not all(isinstance(t, str) for t in d[key]):
                bad.append(key)
        elif d.get(key) is not None:
            bad.append(key)
    _report(source, "Statistics", bad)
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
    bad: List[str] = []
    partial: List[str] = []
    i = view._region.findText(str(d.get("region", "")))
    if i >= 0 and i != view._region.currentIndex():
        view._region.setCurrentIndex(i)               # rebuilds the filter for the region
    elif i < 0 and d.get("region"):
        bad.append(f"region {d.get('region')!r}")
    want = d.get("filter")
    if isinstance(want, list):
        if not all(isinstance(w, str) for w in want):
            partial.append("filter")            # its text entries are applied
        if view._filter_items:
            for item in view._filter_items:
                act = item["action"]
                act.blockSignals(True)
                act.setChecked(act.text() in want)
                act.blockSignals(False)
            view._on_filter_changed(redraw=False)
    elif want is not None:
        bad.append("filter")
    if isinstance(d.get("hidden"), list):
        rows = d["hidden"]
        if all(isinstance(r, list) and len(r) == 2 and all(isinstance(x, bool) for x in r)
               for r in rows):
            view._cat_hidden = {(a, b) for a, b in rows}
        else:
            bad.append("hidden")
        view._update_legend()
    elif d.get("hidden") is not None:
        bad.append("hidden")
    _report(source, "Timing", bad, partial)
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


def filters_from_json(d: Any, source: Optional[str] = None,
                      pane: str = "Commands filter") -> Optional[dict]:
    """A saved Commands filter as a `_filter_snapshot`, or None when it does not read (the
    filter is then left as it is). An on/off entry that is not a JSON true or false is off,
    a text that is not text is empty, and a kinds, devices or groups entry that is not a list
    is empty; each is reported under `source` as `pane`."""
    if not isinstance(d, dict):
        _report(source, pane, ["the whole entry"] if d is not None else [])
        return None
    bad: List[str] = []
    out: dict = {}
    try:
        for key in ("kinds", "devices", "groups"):
            v = d.get(key)
            if v is not None and not isinstance(v, list):
                bad.append(key)
                v = None
            out[key] = {tuple(x) if isinstance(x, list) else x for x in v or ()}
    except TypeError:
        _report(source, pane, ["the whole entry"])
        return None
    text = d.get("text")
    if text is not None and not isinstance(text, str):
        bad.append("text")
        text = None
    out.update(kinds_none=bool(_flag(d, "kinds_none", bad)),
               errors_only=bool(_flag(d, "errors_only", bad)), text=text or "")
    _report(source, pane, bad)
    return out
