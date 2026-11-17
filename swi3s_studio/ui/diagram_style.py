"""Timing Diagrams drawing settings: the one place the term diagram and the whole-inequality
view ask for a colour, a line weight or dash, a size or a font.

A colour is resolved as: the user's preference for the active theme (QSettings, per theme)
→ the theme's `DIAG_` token. Every other setting holds for both themes. The diagrams read
the settings at paint time, so a theme switch needs only a repaint and a settings change
a `restyle` (the line height and the placed labels follow the fonts).

What can be set:

| kind     | entries (`VALUES`, `COLORS`)                                  | unit                    |
|----------|---------------------------------------------------------------|-------------------------|
| colour   | each line class, the data per driving device, the margin's two colours, the text and dim text | "#rrggbb", per theme |
| weight   | each line class's pen width                                   | line heights            |
| dash     | the dashed and dotted line classes                            | `DASHES`                |
| length   | arrowheads, the dot radius, the tick lengths                  | line heights            |
| font     | family, size and weight of each text class (`FONTS`)          | size: × the body font   |
| scale    | labels in a narrow column                                     | × their size            |

A line height is the height of the diagram's body font (its widget font, scaled by
`timing_diagram.REDUCED_SCALE` while the diagram is not maximized), so every stroke and size
keeps its weight against the text at any font size.

`preferences` / `set_preferences` read and store the user's settings, an entry equal to
its default stored as unset. `set_preview` puts an unsaved set in force (the Preferences
dialog's live preview) until it is cleared. `to_json` / `from_json` write and read a file
holding every setting; `from_json` reports anything it skips or clamps through
`import_notes`, but drops a `RETIRED` setting silently.
"""
from __future__ import annotations

import json
import math
from typing import Any, Dict, Optional

from .. import import_notes
from .theme import _PALETTES, VizTheme

STYLE_KEY = "timing_diagrams/style"   # QSettings: JSON {"colors": {mode: {key: hex}}, "values": {}}
FILE_FORMAT = "SWI3S Studio timing diagram settings"
FILE_VERSION = 1

# (key, label, the theme token it defaults to).
COLORS: tuple[tuple[str, str, str], ...] = (
    ("clock", "Clock waveform", "DIAG_CLOCK"),
    ("data", "Data, the Manager driving", "DIAG_DATA"),
    ("data_per", "Data, a Peripheral (Peripheral A) driving", "DIAG_DATA_PER"),
    ("data_per_b", "Data, Peripheral B driving", "DIAG_DATA_PER_B"),
    ("vdd", "V_DD rails", "DIAG_DIM"),
    ("gnd", "GND rails", "DIAG_DIM"),
    ("gnd_offset", "Receiver's offset GND", "DIAG_GROUND"),
    ("reference", "20 % levels (references, anchors)", "DIAG_OK"),
    ("valid", "80 % levels", "DIAG_VALID"),
    ("threshold", "Thresholds (V_IH, V_IL)", "DIAG_LEVEL"),
    ("guide_dot", "Guides from a point, dotted", "DIAG_GUIDE"),
    ("guide_dash", "Guides from a point on no ramp, dashed", "DIAG_GUIDE"),
    ("span", "Span arrows", "DIAG_TEXT"),
    ("interval", "Interval arrows", "DIAG_DIM"),
    ("margin_ok", "Margin, a gap", "DIAG_OK"),
    ("margin_bad", "Margin, short", "DIAG_BAD"),
    ("release", "Release tick", "DIAG_TEXT"),
    ("keeper", "Keeper's response", "DIAG_KEEPER"),
    ("axis", "Time axis", "DIAG_TEXT"),
    ("text", "Title and headings", "DIAG_TEXT"),
    ("dim_text", "Notes, high-Z and point labels", "DIAG_DIM"),
)
COLOR_KEYS = tuple(k for k, _, _ in COLORS)

# The dash styles a dashed line class takes, by name.
DASHES = ("solid", "dash", "dot", "dashdot")
DASH_LABELS = {"solid": "Solid", "dash": "Dashed", "dot": "Dotted", "dashdot": "Dash-dot"}
FONT_WEIGHTS = ("normal", "demibold", "bold")
FONT_WEIGHT_LABELS = {"normal": "Normal", "demibold": "Demi-bold", "bold": "Bold"}

# The units most defaults are written in: px at the Timing page's diagram font (its pixel size
# `_REF_PX`) and that font's line height (`_REF_LH`). A default of `n / _REF_LH` is n px at the
# diagram's full scale with that font, and scales with any other.
_REF_PX = 18
_REF_LH = 21.1875

# (key, label, body-font scale, weight).
FONTS: tuple[tuple[str, str, float, str], ...] = (
    ("title", "Title", 1.12, "normal"),
    ("heading", "Panel headings and captions", 15 / _REF_PX, "normal"),
    ("row", "Row names", 15 / _REF_PX, "normal"),
    ("span", "Span and interval labels", 15 / _REF_PX, "normal"),
    ("level", "Level and point labels", 15 / _REF_PX, "normal"),
    ("axis", "Axis numbers", 15 / _REF_PX, "normal"),
    ("note", "Footnote and corner notes", 0.85, "normal"),
)
FONT_KEYS = tuple(k for k, _, _, _ in FONTS)
FONT_SIZE_RANGE = (0.5, 2.5)
FAMILY_MAX = 100          # characters in a font family name

# Settings an earlier version held, colours and values: a stored or imported entry of one is
# dropped without a warning.
RETIRED = frozenset({"margin.band", "unmax_scale", "connector", "connector.weight",
                     "connector.dash"})

# The numeric and named settings: key -> (default, kind, low, high). A weight, a length
# and a font size are in line heights or body-font sizes; low and high bound them.
VALUES: Dict[str, tuple[Any, str, float, float]] = {
    # line weights
    "clock.weight": (3 / _REF_LH, "weight", 0.02, 0.6),
    "data.weight": (3 / _REF_LH, "weight", 0.02, 0.6),
    "vdd.weight": (2 / _REF_LH, "weight", 0.02, 0.6),
    "gnd.weight": (2 / _REF_LH, "weight", 0.02, 0.6),
    "gnd_offset.weight": (2 / _REF_LH, "weight", 0.02, 0.6),
    "reference.weight": (2 / _REF_LH, "weight", 0.02, 0.6),
    "valid.weight": (2 / _REF_LH, "weight", 0.02, 0.6),
    "threshold.weight": (2 / _REF_LH, "weight", 0.02, 0.6),
    "guide_dot.weight": (2 / _REF_LH, "weight", 0.02, 0.6),
    "guide_dash.weight": (2 / _REF_LH, "weight", 0.02, 0.6),
    "span.weight": (3 / _REF_LH, "weight", 0.02, 0.6),
    "interval.weight": (3 / _REF_LH, "weight", 0.02, 0.6),
    "flight.weight": (3 / _REF_LH, "weight", 0.02, 0.6),
    "margin.weight": (3 / _REF_LH, "weight", 0.02, 0.6),
    "release.weight": (3 / _REF_LH, "weight", 0.02, 0.6),
    "axis.weight": (3 / _REF_LH, "weight", 0.02, 0.6),
    "leader.weight": (0.06, "weight", 0.02, 0.6),
    # dash styles
    "high_z.dash": ("dot", "dash", 0, 0),
    "vdd.dash": ("dot", "dash", 0, 0),
    "gnd.dash": ("dot", "dash", 0, 0),
    "gnd_offset.dash": ("dash", "dash", 0, 0),
    "reference.dash": ("dash", "dash", 0, 0),
    "valid.dash": ("dash", "dash", 0, 0),
    "threshold.dash": ("solid", "dash", 0, 0),
    "guide_dot.dash": ("dot", "dash", 0, 0),
    "guide_dash.dash": ("dash", "dash", 0, 0),
    # sizes
    "threshold.head": (0.32, "length", 0.05, 2.0),
    "span.head": (6 / _REF_LH, "length", 0.05, 2.0),
    "interval.head": (6 / _REF_LH, "length", 0.05, 2.0),
    "flight.head": (6 / _REF_LH, "length", 0.05, 2.0),
    "margin.head": (10 / _REF_LH, "length", 0.05, 2.0),
    "dot.radius": (5 / _REF_LH, "length", 0.05, 1.0),
    "release.length": (0.6, "length", 0.1, 2.0),
    "axis.tick": (5 / _REF_LH, "length", 0.05, 0.5),
    # fonts: the narrow-column labels
    "label.narrow": (0.85, "scale", 0.5, 1.0),
}
for _key, _label, _size, _weight in FONTS:
    VALUES[f"font.{_key}.family"] = ("", "family", 0, 0)
    VALUES[f"font.{_key}.size"] = (_size, "font_size", *FONT_SIZE_RANGE)
    VALUES[f"font.{_key}.weight"] = (_weight, "font_weight", 0, 0)

_prefs: Optional[dict] = None        # the stored settings, cleaned
_preview: Optional[dict] = None      # an unsaved set in force, or None


def default_color(key: str, mode: Optional[str] = None) -> str:
    """`key`'s default colour in the given (default: active) theme."""
    token = next(t for k, _, t in COLORS if k == key)
    return str(_PALETTES[mode or VizTheme.MODE][token]).lower()


def default(key: str) -> Any:
    """A `VALUES` setting's default."""
    return VALUES[key][0]


def _valid_hex(v) -> bool:
    return (isinstance(v, str) and len(v) == 7 and v[0] == "#"
            and all(c in "0123456789abcdefABCDEF" for c in v[1:]))


def _value(key: str, v, report) -> Any:
    """`v` as `key`'s setting, clamped to its range; None where it cannot be one. `report`
    is told of each value skipped or clamped."""
    dflt, kind, lo, hi = VALUES[key]
    if kind == "dash":
        ok = v in DASHES
    elif kind == "font_weight":
        ok = v in FONT_WEIGHTS
    elif kind == "family":
        ok = isinstance(v, str) and len(v) <= FAMILY_MAX
    else:
        ok = (isinstance(v, (int, float)) and not isinstance(v, bool)
              and math.isfinite(float(v)))
        if ok:
            got = min(max(float(v), lo), hi)
            if got != float(v):
                report(f"{key} = {v:g} is outside {lo:g} to {hi:g}; read as {got:g}")
            return got
    if not ok:
        report(f"{key} = {json.dumps(v)[:40]} is not a {kind.replace('_', ' ')} setting; "
               f"the default is kept")
        return None
    return v


def _clean(raw, report=None) -> dict:
    """A stored or imported set, with anything malformed dropped and a number outside its
    range clamped; `report`, where given, is told of each."""
    def say(msg: str) -> None:
        if report is not None:
            report(msg)

    raw = raw if isinstance(raw, dict) else {}
    colors: dict = {}
    stored = raw.get("colors", {})
    if not isinstance(stored, dict):
        say("colors is not a mapping of theme to colours; the defaults are kept")
        stored = {}
    for mode, entries in stored.items():
        if mode not in _PALETTES or not isinstance(entries, dict):
            say(f"colors: {str(mode)[:40]} is not a theme (dark or light); skipped")
            continue
        for key, v in entries.items():
            if key in RETIRED:
                continue
            if key not in COLOR_KEYS:
                say(f"colors.{mode}: {str(key)[:40]} is not a diagram colour; skipped")
            elif not _valid_hex(v):
                say(f"colors.{mode}.{key} = {json.dumps(v)[:40]} is not a #rrggbb colour; "
                    f"the default is kept")
            else:
                colors.setdefault(mode, {})[key] = v.lower()
    values: dict = {}
    stored = raw.get("values", {})
    if not isinstance(stored, dict):
        say("values is not a mapping of setting to value; the defaults are kept")
        stored = {}
    for key, v in stored.items():
        if key in RETIRED:
            continue
        if key not in VALUES:
            say(f"values: {str(key)[:40]} is not a diagram setting; skipped")
            continue
        got = _value(key, v, say)
        if got is not None:
            values[key] = got
    return {"colors": colors, "values": values}


def _strip_defaults(clean: dict) -> dict:
    """`clean` without the entries equal to their default."""
    colors = {}
    for mode, entries in clean["colors"].items():
        kept = {k: v for k, v in entries.items() if v != default_color(k, mode)}
        if kept:
            colors[mode] = kept
    values = {k: v for k, v in clean["values"].items() if v != default(k)}
    return {"colors": colors, "values": values}


def _load() -> dict:
    global _prefs
    if _prefs is None:
        try:
            from PySide6.QtCore import QSettings
            _prefs = _clean(json.loads(str(QSettings().value(STYLE_KEY, "{}"))))
        except Exception:
            _prefs = _clean({})
    return _prefs


def _active() -> dict:
    return _preview if _preview is not None else _load()


def preferences() -> dict:
    """A copy of the user's stored settings: {"colors": {mode: {key: hex}}, "values":
    {key: value}}. Absent entries use the default."""
    return json.loads(json.dumps(_load()))


def set_preferences(prefs: dict) -> None:
    """Replace the user's settings and persist them. An entry equal to its default is
    stored as unset, so a later change to the default still reaches it."""
    global _prefs
    _prefs = _strip_defaults(_clean(prefs))
    try:
        from PySide6.QtCore import QSettings
        QSettings().setValue(STYLE_KEY, json.dumps(_prefs))
    except Exception:
        pass


def set_preview(prefs: Optional[dict]) -> None:
    """Put `prefs` in force without storing it, or with None the stored settings again."""
    global _preview
    _preview = None if prefs is None else _strip_defaults(_clean(prefs))


def reset_cache() -> None:
    """Forget the loaded settings and any preview (the next read goes to QSettings). For
    tests."""
    global _prefs, _preview
    _prefs = _preview = None


# ---- reading a setting ----------------------------------------------------------------
def color(key: str, mode: Optional[str] = None) -> str:
    """`key`'s colour in the given (default: active) theme: the preference, else the
    default."""
    mode = mode or VizTheme.MODE
    return _active()["colors"].get(mode, {}).get(key) or default_color(key, mode)


def value(key: str) -> Any:
    """A `VALUES` setting: the preference, else the default."""
    return _active()["values"].get(key, VALUES[key][0])


def weight(key: str) -> float:
    """A line class's pen width, in line heights."""
    return float(value(f"{key}.weight"))


def dash(key: str) -> str:
    """A line class's dash style, a `DASHES` name."""
    return str(value(f"{key}.dash"))


def font(key: str) -> tuple[str, float, str]:
    """A text class's family ("" for the diagram's own), size (× the body font) and weight
    (a `FONT_WEIGHTS` name)."""
    return (str(value(f"font.{key}.family")), float(value(f"font.{key}.size")),
            str(value(f"font.{key}.weight")))


def widest_weight() -> float:
    """The widest pen of every line class, in line heights."""
    return max(float(value(k)) for k, spec in VALUES.items() if spec[1] == "weight")


# ---- files ------------------------------------------------------------------------------
def full(prefs: dict) -> dict:
    """Every setting `prefs` gives, defaults included: both themes' colours and every
    value."""
    clean = _clean(prefs)
    colors = {mode: {k: clean["colors"].get(mode, {}).get(k) or default_color(k, mode)
                     for k in COLOR_KEYS} for mode in _PALETTES}
    values = {k: clean["values"].get(k, spec[0]) for k, spec in VALUES.items()}
    return {"colors": colors, "values": values}


def to_json(prefs: dict) -> str:
    """A settings file holding every setting `prefs` gives, defaults included."""
    body = {"format": FILE_FORMAT, "version": FILE_VERSION, **full(prefs)}
    return json.dumps(body, indent=2, sort_keys=False) + "\n"


def from_json(text: str, source: str = "") -> dict:
    """The settings a file written by `to_json` holds, as `set_preferences` takes them.
    Each entry skipped or clamped is an import warning from `source`. Raises ValueError
    where the text is not a JSON object."""
    try:
        raw = json.loads(text)
    except ValueError as exc:
        raise ValueError(f"the file is not valid JSON ({exc})") from None
    if not isinstance(raw, dict):
        raise ValueError("the file does not hold a settings object")
    for key in raw:
        if key not in ("format", "version", "colors", "values"):
            import_notes.warn(source, f"{str(key)[:40]} is not a section of a diagram "
                                      f"settings file; skipped")
    return _strip_defaults(_clean(raw, lambda msg: import_notes.warn(source, msg)))
