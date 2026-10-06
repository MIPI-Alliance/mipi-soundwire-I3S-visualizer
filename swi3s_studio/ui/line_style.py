"""Waveform line colours and weights: the one place every pane asks.

A colour is resolved as: the Link's per-stream override (saved in the workspace) →
the user's preference (QSettings, per theme) → the theme's default. Panes never keep
their own copy, so a theme switch, a preference change or an override reaches them all.

What can be set, per theme:

| kind       | entries                                             | default                         |
|------------|-----------------------------------------------------|---------------------------------|
| `dp`       | 12 data-port line colours (`grid_view._dp_index`)   | `VizTheme.DP_LINE_PALETTE`      |
| `bookmark` | 12 pair colours, A, B, C, … in rotation             | `VizTheme.DP_LINE_PALETTE`      |
| `capture`  | the Capture pane's DP and DN traces                 | `VizTheme.RAW_DP`, `RAW_DN`     |
| `timing`   | the four edge flavours, in `eye_view._cat_specs` order | entries 0, 5, 3, 2 of `TRACE_PALETTE` |

and, for both themes, a line weight in pixels for the Audio and Capture traces.

Colours are stored per theme because a colour picked for one can vanish on the other's
plot background, which is the bug the per-theme line palette fixed. Bus Grid cells are
not lines and keep their fills (`grid_view.dp_stream_color`).
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Tuple

from .theme import _PALETTES, VizTheme

COLOR_KEY = "waveforms/colors"      # QSettings: JSON {mode: {kind: [hex or null, ...]}}
WEIGHT_KEY = "waveforms/weights"    # QSettings: JSON {kind: px}

KINDS = ("dp", "bookmark", "capture", "timing")
WEIGHT_KINDS = ("audio", "capture")
DEFAULT_WEIGHTS = {"audio": 1, "capture": 2}
MIN_WEIGHT, MAX_WEIGHT = 1, 4

# (device, dp, mode) -> "#rrggbb": one Link's per-stream overrides.
Overrides = Dict[Tuple[int, int, str], str]

_prefs: Optional[dict] = None        # {"colors": {mode: {kind: [...]}}, "weights": {...}}


def defaults(kind: str, mode: Optional[str] = None) -> List[str]:
    """The theme's default colours for `kind`, in order."""
    pal: Dict[str, Any] = _PALETTES[mode or VizTheme.MODE]   # values mix str/list/float
    if kind in ("dp", "bookmark"):
        return list(pal["DP_LINE_PALETTE"])
    if kind == "capture":
        return [pal["RAW_DP"], pal["RAW_DN"]]
    if kind == "timing":
        t = pal["TRACE_PALETTE"]
        return [t[0], t[5], t[3], t[2]]
    raise KeyError(kind)


def _valid_hex(v) -> bool:
    return (isinstance(v, str) and len(v) == 7 and v[0] == "#"
            and all(c in "0123456789abcdefABCDEF" for c in v[1:]))


def _clean(raw) -> dict:
    """A stored preference, with anything malformed dropped (a hand-edited setting must
    fall back to the default, not break every pane)."""
    colors: dict = {}
    raw = raw if isinstance(raw, dict) else {}
    for mode, kinds in (raw.get("colors") or {}).items():
        if mode not in _PALETTES or not isinstance(kinds, dict):
            continue
        for kind, vals in kinds.items():
            if kind not in KINDS or not isinstance(vals, list):
                continue
            n = len(defaults(kind, mode))
            vals = [v.lower() if _valid_hex(v) else None for v in vals[:n]]
            if any(vals):
                colors.setdefault(mode, {})[kind] = vals + [None] * (n - len(vals))
    weights = {}
    for kind, px in (raw.get("weights") or {}).items():
        if kind in WEIGHT_KINDS and isinstance(px, int) and MIN_WEIGHT <= px <= MAX_WEIGHT:
            weights[kind] = px
    return {"colors": colors, "weights": weights}


def _load() -> dict:
    global _prefs
    if _prefs is None:
        try:
            from PySide6.QtCore import QSettings
            s = QSettings()
            _prefs = _clean({"colors": json.loads(str(s.value(COLOR_KEY, "{}"))),
                             "weights": json.loads(str(s.value(WEIGHT_KEY, "{}")))})
        except Exception:
            _prefs = _clean({})
    return _prefs


def preferences() -> dict:
    """A copy of the user's settings: {"colors": {mode: {kind: [hex or None]}},
    "weights": {kind: px}}. Absent entries use the default."""
    return json.loads(json.dumps(_load()))


def set_preferences(prefs: dict) -> None:
    """Replace the user's settings and persist them. An entry equal to its default is
    stored as unset, so a later change to the default palette still reaches it."""
    global _prefs
    clean = _clean(prefs)
    for mode, kinds in list(clean["colors"].items()):
        for kind, vals in list(kinds.items()):
            base = defaults(kind, mode)
            vals = [None if v == base[i].lower() else v for i, v in enumerate(vals)]
            if any(vals):
                kinds[kind] = vals
            else:
                del kinds[kind]
        if not kinds:
            del clean["colors"][mode]
    clean["weights"] = {k: v for k, v in clean["weights"].items() if v != DEFAULT_WEIGHTS[k]}
    _prefs = clean
    try:
        from PySide6.QtCore import QSettings
        s = QSettings()
        s.setValue(COLOR_KEY, json.dumps(clean["colors"]))
        s.setValue(WEIGHT_KEY, json.dumps(clean["weights"]))
    except Exception:
        pass


def reset_cache() -> None:
    """Forget the loaded settings (the next read goes to QSettings). For tests."""
    global _prefs
    _prefs = None


def palette(kind: str, mode: Optional[str] = None) -> List[str]:
    """`kind`'s colours in the given (default: active) theme: preference, else default."""
    mode = mode or VizTheme.MODE
    base = defaults(kind, mode)
    chosen = _load()["colors"].get(mode, {}).get(kind) or []
    return [chosen[i] if i < len(chosen) and chosen[i] else c for i, c in enumerate(base)]


def color(kind: str, index: int) -> str:
    """One `kind` colour; list kinds wrap (the bookmark letters, the data-port slots)."""
    pal = palette(kind)
    return pal[index % len(pal)]


def stream_color(index: int, device: int, dp: int,
                 overrides: Optional[Overrides] = None) -> str:
    """A data-port stream's line colour: the Link's override for (device, dp) in the
    active theme, else data-port slot `index` of the palette."""
    if overrides:
        hit = overrides.get((int(device), int(dp), VizTheme.MODE))
        if hit:
            return hit
    return color("dp", index)


def weight(kind: str) -> int:
    """Line weight in px for the Audio or Capture traces."""
    return int(_load()["weights"].get(kind, DEFAULT_WEIGHTS[kind]))


def overrides_to_json(overrides: Overrides) -> list:
    """A Link's overrides as JSON-able [device, dp, mode, "#hex"] entries, sorted."""
    return [[d, p, m, c] for (d, p, m), c in sorted(overrides.items())]


def overrides_from_json(entries) -> Overrides:
    """The inverse of overrides_to_json; malformed entries are skipped."""
    out: Overrides = {}
    for e in entries if isinstance(entries, list) else []:
        try:
            d, p, m, c = e
            if m in _PALETTES and _valid_hex(c):
                out[(int(d), int(p), str(m))] = c.lower()
        except (TypeError, ValueError):
            continue
    return out
