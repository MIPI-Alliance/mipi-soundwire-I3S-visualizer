"""Workspace (session) save/load.

A workspace is a small JSON sidecar that records how to reconstruct an analysis:
for each Link, the capture *source* (demo parameters or a Saleae clock/data file pair)
and its decode inputs (what-if register overlay, device settings), plus the user's
view state — bookmarks, the cursor, the mode.
The decoded results themselves are not stored; they are re-derived by re-running
the decode on load (fast, and keeps workspaces tiny + portable).
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field, fields, replace
from typing import Any, Dict, List, Optional, Tuple

from . import import_notes
from .fsutil import write_whole
from .model.registers import bundled_data_path
from .session import CSV_SECTION_KEY, MAX_HUB_DEPTH, Session

# The format a save writes: per-Link state in `links` (one entry per Link, each with its
# source and time offset) and bookmarks that carry the Link they are on. A file of version 3
# or lower reads as one Link at offset 0, and a bookmark list of bare samples reads as the
# pairs A1, A2, ...
WORKSPACE_VERSION = 4

# The per-Link keys a file of version 3 or lower holds at its top level.
_LINK_KEYS = ("overlay", "device_regmaps", "device_names", "device_hub_depths",
              "device_scramblers")


@dataclass
class LinkSpec:
    """How to rebuild ONE Link: its capture source, its decode inputs and its name/offset."""
    source: dict                                   # {"type": "demo"|"saleae_binary", ...}
    name: str = "Link 1"
    offset_ps: int = 0                             # this Link's sample 0 in global time
    overlay: List[list] = field(default_factory=list)   # what-if register overrides,
    #                                    [[section, device, address, value], ...]
    # Per-device peripheral register maps, embedded as normalized JSON keyed by
    # device number (as a string for JSON). Self-contained so a shared workspace
    # carries its vendor maps — no external regmap file needed on reload.
    device_regmaps: dict = field(default_factory=dict)
    # Per-device display names {str(device): name} and hub depths
    # {str(device): 0..5}. Hub depth feeds the decoder (responses delayed 2 rows
    # per level), so it is re-applied on load.
    device_names: dict = field(default_factory=dict)
    device_hub_depths: dict = field(default_factory=dict)
    # Per-(device, dp) scrambler overrides as [device, dp, on] triples (tuple keys
    # aren't JSON-able). Like hub depths, this is a DECODE input — it changes the
    # reconstructed audio — so it must be restored on reopen or the audio silently
    # re-decodes with the snooped/CSV scrambler state.
    device_scramblers: list = field(default_factory=list)
    # Per-stream line colours picked in the Audio pane, per theme, as
    # [device, dp, "dark"|"light", "#rrggbb"] (line_style.overrides_to_json). A view
    # preference, not a decode input; optional, a Link without it reads as empty.
    stream_colors: list = field(default_factory=list)
    # Per-stream Filter & Gain as [device, dp, {"highpass_hz", "gain_db",
    # "allow_clipping", "dc_block"}] (StreamProcessing.to_json). Changes what the waveform,
    # playback and export carry but not the decode; optional, a Link without it reads as
    # empty.
    stream_processing: list = field(default_factory=list)
    # Streams the Audio pane draws evenly spaced rather than at their transport times, as
    # [device, dp]; optional, a Link without it reads as empty.
    even_timing: list = field(default_factory=list)
    # The Link's own view (ui/view_state.py dicts): its Commands filter and column widths,
    # and its Audio and Capture panes' channels, zoom and toggles; optional, a Link without
    # it reads as empty.
    view: dict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d, source: str = "", index: int = 0) -> "LinkSpec":
        """Read one saved Link. A key it does not know, a value of the wrong type and a row
        that does not read are left out and reported through import_notes under `source`
        (the workspace file's name)."""
        if not isinstance(d, dict) or not isinstance(d.get("source"), dict):
            raise ValueError("not a valid workspace file (a Link is missing its 'source')")
        default_name = f"Link {index + 1}"
        saved_name = d.get("name", default_name)
        name_reads = isinstance(saved_name, str) and bool(saved_name.strip())
        name = saved_name if name_reads else default_name
        lead = link_lead(index, name)

        def note(msg: str) -> None:
            import_notes.warn(source, f"{lead}: {msg}")

        if not name_reads:
            note(f"its saved name is not text or is blank; {default_name} is used")

        unknown = sorted(str(k) for k in d if k not in _LINK_FIELDS)
        if unknown:
            note(f"{_plural(len(unknown), 'saved setting')} this version does not read, "
                 f"ignored: {import_notes.names_listed(unknown)}")
        got: Dict[str, Any] = {}
        for key, kind in _LINK_SHAPES.items():
            if key not in d:
                continue
            v = d[key]
            if not isinstance(v, kind):
                note(f"'{key}' is not a {'list' if kind is list else 'mapping'}; not read")
                continue
            got[key] = v
        rows, bad = _rows(got.get("overlay", []), _overlay_row)
        if bad:
            note(f"{_plural(bad, 'register override')} not read (not [region, device "
                 f"0..{_DEVICES - 1}, 32-bit address, value 0..255] whole numbers)")
        got["overlay"] = rows
        rows, bad = _rows(got.get("device_scramblers", []), _scrambler_row)
        if bad:
            note(f"{_plural(bad, 'scrambler override')} not read (not [device "
                 f"-1..{_DEVICES - 1}, data port, true/false])")
        got["device_scramblers"] = rows
        rows, bad = _rows(got.get("even_timing", []), _pair_row)
        if bad:
            note(f"{_plural(bad, 'Evenly Spaced stream')} not read (not [device "
                 f"0..{_DEVICES - 1}, data port])")
        got["even_timing"] = rows
        for key, what, check, why in (
                ("device_names", "device name", lambda v: isinstance(v, str), "is not text"),
                ("device_hub_depths", "hub depth", _hub_depth,
                 f"is not 0..{MAX_HUB_DEPTH}"),
                ("device_regmaps", "register map", lambda v: isinstance(v, dict),
                 "is not a map")):
            mapping = got.get(key, {})
            kept, dropped = {}, 0
            # A key the file names twice is collapsed by the parse, which counts it.
            repeated = sum(1 for k in _repeats(mapping) if _device(k) is not None)
            for k, v in mapping.items():
                dev = _device(k)
                if dev is None or not check(v):
                    dropped += 1
                    continue
                if str(dev) in kept:
                    repeated += 1
                kept[str(dev)] = int(v) if key == "device_hub_depths" else v
            if dropped:
                note(f"{_plural(dropped, what)} not read: the device is not "
                     f"0..{_DEVICES - 1} or the value {why}")
            if repeated:
                note(f"{_plural(repeated, what)} for a device named twice not read; "
                     f"the last is used")
            got[key] = kept
        view = d.get("view")
        if view is not None and not isinstance(view, dict):
            note("its saved view is not a mapping; not read")
        offset = _whole(d.get("offset_ps", 0))
        if offset is None:
            note("its saved time offset is not a whole number; 0 is used")
            offset = 0
        elif abs(offset) > _MAX_OFFSET_PS:
            note(f"its saved time offset is outside ±{_MAX_OFFSET_PS} ps; 0 is used")
            offset = 0
        return cls(source=d["source"], name=name,
                   offset_ps=offset,
                   overlay=got["overlay"],
                   device_regmaps=got["device_regmaps"],
                   device_names=got["device_names"],
                   device_hub_depths=got["device_hub_depths"],
                   device_scramblers=got["device_scramblers"],
                   stream_colors=got.get("stream_colors", []),
                   stream_processing=got.get("stream_processing", []),
                   even_timing=got["even_timing"],
                   view=_dict_or_empty(view))


def link_lead(index: int, name: str) -> str:
    """How a note names the Link at `index` (0-based) called `name`: "Link 2 (Amp)", or
    "Link 2" alone when `name` is that position's default name."""
    default = f"Link {index + 1}"
    return default if name == default else f"{default} ({name})"


# What LinkSpec.from_dict checks a saved Link's values against.
_LINK_SHAPES = {"overlay": list, "device_regmaps": dict, "device_names": dict,
                "device_hub_depths": dict, "device_scramblers": list, "stream_colors": list,
                "stream_processing": list, "even_timing": list}
_DEVICES = 12                    # devices 0..11 (a command's Device Mask)


def _plural(n: int, noun: str) -> str:
    return f"{n} {noun}{'s' if n != 1 else ''}"


def _was(n: int) -> str:
    """The verb for `n` things: "was" for one, else "were"."""
    return "was" if n == 1 else "were"


def _whole(v) -> Optional[int]:
    """`v` as an integer when it is a JSON number with no fraction, else None. Text and
    a fractional number are not read as integers."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    if isinstance(v, float) and (v != v or v in (float("inf"), float("-inf"))
                                 or v != int(v)):
        return None
    return int(v)


# The largest workspace file read, in characters: a workspace is a few KB of settings.
MAX_WORKSPACE_CHARS = 8 << 20
# The most Links a workspace opens: each is a decode and a set of panes.
MAX_WORKSPACE_LINKS = 64
_MAX_SAMPLE = 2 ** 63 - 1        # the cursor signal carries a signed 64-bit sample
_MAX_ADDRESS = 2 ** 32 - 1       # a register address is 32 bits
_MAX_SECTION = 2 ** 31 - 1       # a config section index is a signed 32-bit value
_MAX_PORT = 2 ** 31 - 1          # a data-port number is a non-negative 32-bit value
_MAX_BOOKMARK_INDEX = 2 ** 31 - 1
_MAX_OFFSET_PS = 2 ** 63 - 1     # a Link's time offset is a signed 64-bit count of ps


def _sample(v) -> Optional[int]:
    """`v` as a sample number (0 .. 2**63 - 1), or None."""
    n = _whole(v)
    return n if n is not None and 0 <= n <= _MAX_SAMPLE else None


def _device(k) -> Optional[int]:
    """A device-keyed mapping's key as a device 0..11, or None. A key is the device's
    decimal text with no sign, space, separator or leading zero (a JSON object key is
    text), or a whole number."""
    if isinstance(k, str):
        if not (k.isascii() and k.isdigit()) or (len(k) > 1 and k[0] == "0"):
            return None
        d: Optional[int] = int(k)
    else:
        d = _whole(k)
    return d if d is not None and 0 <= d < _DEVICES else None


def _hub_depth(v) -> bool:
    d = _whole(v)
    return d is not None and 0 <= d <= MAX_HUB_DEPTH


def _in(v, lo: int, hi: int) -> Optional[int]:
    """`v` as a whole JSON number lo..hi, or None."""
    n = _whole(v)
    return n if n is not None and lo <= n <= hi else None


def _overlay_row(row) -> Optional[list]:
    """[section, device, address, value] as whole JSON numbers: a section >= 0, a device
    0..11, an address of 32 bits and a register value 0..255. The row as integers, or None."""
    if not (isinstance(row, list) and len(row) == 4):
        return None
    out = [_in(row[0], 0, _MAX_SECTION), _in(row[1], 0, _DEVICES - 1),
           _in(row[2], 0, _MAX_ADDRESS), _in(row[3], 0, 0xFF)]
    return None if None in out else out


def _scrambler_row(row) -> Optional[list]:
    """[device, data port, on]: a device -1..11 (-1 is the Manager, the core's key for a
    Manager data port) and a non-negative 32-bit data port as whole JSON numbers, and an
    on/off flag as JSON true/false. The row as [int, int, bool], or None."""
    if not (isinstance(row, list) and len(row) == 3 and isinstance(row[2], bool)):
        return None
    device, port = _in(row[0], -1, _DEVICES - 1), _in(row[1], 0, _MAX_PORT)
    return None if device is None or port is None else [device, port, row[2]]


def _pair_row(row) -> Optional[list]:
    """[device, data port] as whole JSON numbers: a device 0..11 and a non-negative 32-bit
    data port. The row as integers, or None."""
    if not (isinstance(row, list) and len(row) == 2):
        return None
    device, port = _in(row[0], 0, _DEVICES - 1), _in(row[1], 0, _MAX_PORT)
    return None if device is None or port is None else [device, port]


def _rows(rows: list, read) -> Tuple[list, int]:
    """(each row as `read` gives it, for the rows it reads; how many it did not)."""
    kept = [r for r in (read(x) for x in rows) if r is not None]
    return kept, len(rows) - len(kept)


def _dict_or_empty(v) -> dict:
    return v if isinstance(v, dict) else {}


class _Parsed(dict):
    """A JSON object as _parse_json reads it: `repeated` lists each key the object named
    more than once (the last value is kept), and `reported` is set once a note names them."""
    repeated: List[str]
    reported: bool


def _parse_json(text: str) -> Tuple[Any, List[_Parsed]]:
    """`text` parsed as JSON, and every object in it that names a key twice."""
    with_repeats: List[_Parsed] = []

    def pairs(items):
        out = _Parsed()
        out.repeated, out.reported = [], False
        for k, v in items:
            if k in out:
                out.repeated.append(k)
            out[k] = v
        if out.repeated:
            with_repeats.append(out)
        return out
    return json.loads(text, object_pairs_hook=pairs), with_repeats


def _repeats(mapping) -> List[str]:
    """The keys `mapping` named twice in its file, marked as reported."""
    if not isinstance(mapping, _Parsed) or not mapping.repeated:
        return []
    mapping.reported = True
    return mapping.repeated


def _note_repeats(with_repeats: List[_Parsed], source: str) -> None:
    """One note for the keys named twice that no more specific note named."""
    keys = sorted({str(k) for m in with_repeats if not m.reported for k in m.repeated})
    n = sum(len(m.repeated) for m in with_repeats if not m.reported)
    if n:
        shown = import_notes.names_listed(keys)
        import_notes.warn(source, f"{_plural(n, 'saved setting')} named twice in one place; "
                                  f"the last of each is used: {shown}")


@dataclass
class Workspace:
    links: List[LinkSpec]                          # one per Link, in LinkSet order
    active_link: int = 0
    bookmarks: List = field(default_factory=list)      # paired bookmarks:
    #   [{"sample":int,"group":"A","index":1,"label":"","link":0}, ...]  (a bare [int] reads too)
    cursor: int = 0                                # in the ACTIVE Link's samples
    mode: str = "Analysis"                         # active macro-mode on save
    authoring: Optional[dict] = None               # BusConfig.to_dict() (Visualization mode)
    timing: Optional[dict] = None                  # TimingView inputs (Timing mode)
    # Window-wide view state, re-applied after the decode; a Link's own view is on its
    # LinkSpec. The keys, by writer (MainWindow):
    #   save_workspace: Bus Grid and Raw Capture prefs {tx_map, tx_persist, grid_rows,
    #     tx_start_row, show_clock}
    #   _link_view_prefs: the multi-Link layout, present only with two or more Links
    #     {pane_links, commands_all, bottom_all, timeline_height}
    #   _window_view_state: {window (geometry, docks, bottom_settle), timeline_ps,
    #     commands_header, samples, registers, statistics, timing_pane, and with
    #     All Links Commands on, all_links_commands}
    # A new key is named once across these writers.
    view: dict = field(default_factory=dict)
    version: int = WORKSPACE_VERSION

    @classmethod
    def single(cls, source: dict, **kw) -> "Workspace":
        """A one-Link workspace: `source`, the Link's name and offset and its decode inputs
        (`_LINK_KEYS`) go to the Link, every other keyword to the workspace. A Link's `view`
        and its stream settings are set on the LinkSpec itself."""
        link_kw = {k: kw.pop(k) for k in list(kw) if k in _LINK_KEYS + ("name", "offset_ps")}
        return cls(links=[LinkSpec(source=source, **link_kw)], **kw)

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2)

    @classmethod
    def from_json(cls, text: str, source: str = "") -> "Workspace":
        """Read a workspace. A key this version does not know, a value of the wrong type,
        and a file from a newer version are reported through import_notes under `source`
        (the file's name); a file that is not a workspace raises ValueError. A key named
        twice in one place keeps its last value, with a note."""
        d, with_repeats = _parse_json(text)
        ws = cls.from_data(d, source)
        _note_repeats(with_repeats, source)
        return ws

    @classmethod
    def from_data(cls, d, source: str = "") -> "Workspace":
        """Read a workspace from its parsed JSON `d`, as from_json does. Links past the
        first MAX_WORKSPACE_LINKS are not read, with a note."""
        if not isinstance(d, dict):
            raise ValueError("not a valid workspace file (missing 'source')")
        n_saved = 1
        if "links" in d:
            raw_links = d["links"]
            if not isinstance(raw_links, list) or not raw_links:
                raise ValueError("not a valid workspace file (no Links)")
            n_saved = len(raw_links)
            if n_saved > MAX_WORKSPACE_LINKS:
                import_notes.warn(source, f"{_plural(n_saved - MAX_WORKSPACE_LINKS, 'Link')} "
                                          f"past the first {MAX_WORKSPACE_LINKS} not opened")
                raw_links = raw_links[:MAX_WORKSPACE_LINKS]
            links = [LinkSpec.from_dict(x, source, i) for i, x in enumerate(raw_links)]
            known = _WORKSPACE_FIELDS
        else:
            # v3 and earlier: one Link, its state at the top level.
            if not isinstance(d.get("source"), dict):
                raise ValueError("not a valid workspace file (missing 'source')")
            links = [LinkSpec.from_dict({k: d[k] for k in ("source",) + _LINK_KEYS
                                         if k in d}, source)]
            known = _WORKSPACE_FIELDS | {"source", *_LINK_KEYS}
        active = _whole(d.get("active_link", 0))
        if active is None:
            import_notes.warn(source, "the saved active Link is not a number; the first is "
                                      "used")
            active = 0
        if len(links) <= active < n_saved:
            import_notes.warn(source, "the saved active Link is one not opened; the first is "
                                      "used")
            active = 0
        if not 0 <= active < len(links):
            raise ValueError(f"not a valid workspace file (active Link {active} of "
                             f"{len(links)})")
        unknown = sorted(str(k) for k in d if k not in known)
        if unknown:
            import_notes.warn(source, f"{_plural(len(unknown), 'saved setting')} this "
                                      f"version does not read, ignored: "
                                      f"{import_notes.names_listed(unknown)}")
        version = _whole(d.get("version", WORKSPACE_VERSION))
        if version is None:
            import_notes.warn(source, "the saved format version is not a whole number; the "
                                      "file is read as the current format")
        elif version > WORKSPACE_VERSION:
            import_notes.warn(source, f"saved in workspace format {version}, newer than the "
                                      f"{WORKSPACE_VERSION} this version reads: what that "
                                      f"format adds is not restored")
        view = d.get("view", {})
        if not isinstance(view, dict):
            import_notes.warn(source, "the saved window view is not a mapping; not read")
            view = {}
        out: dict = {}
        for key in ("authoring", "timing"):
            v = d.get(key)
            if v is not None and not isinstance(v, dict):
                import_notes.warn(source, f"the saved '{key}' settings are not a mapping; "
                                          f"not read")
                v = None
            out[key] = v
        bookmarks = d.get("bookmarks", [])
        if not isinstance(bookmarks, list):
            import_notes.warn(source, "the saved cursors are not a list; not read")
            bookmarks = []
        kept, unread, off_link = _readable_bookmarks(bookmarks, len(links))
        if unread:
            import_notes.warn(source, f"{_plural(unread, 'cursor')} with no readable sample, "
                                      f"index or Link {_was(unread)} not read")
        if off_link:
            import_notes.warn(source, f"{_plural(off_link, 'cursor')} on a Link the workspace "
                                      f"does not have {_was(off_link)} not read")
        cursor = _sample(d.get("cursor", 0))
        if cursor is None:
            import_notes.warn(source, "the saved cursor is not a sample; 0 is used")
            cursor = 0
        mode = d.get("mode", "Analysis")
        if not isinstance(mode, str):
            import_notes.warn(source, "the saved mode is not text; Analysis is used")
            mode = "Analysis"
        return cls(links=links, active_link=active,
                   bookmarks=kept, cursor=cursor,
                   mode=mode, authoring=out["authoring"],
                   timing=out["timing"],
                   view=view)    # a save writes WORKSPACE_VERSION

    def save(self, path: str) -> None:
        """Write the workspace, each capture file named relative to the workspace's folder
        (and absolute as a fallback), so a folder holding both can be moved or shared. The
        file is written to a uniquely named file beside the file `path` resolves to and
        moved over that file, so a failed write leaves the previous workspace intact and a
        symbolic link at `path` stays a link; the new file is synced to disk before it
        replaces the old (`fsutil.write_whole`). An existing file keeps its permission bits;
        a new one gets the mode a plain file created there gets. A file that is not writable
        is refused with PermissionError, and a writable file in a folder that cannot hold
        the temporary file is written in place."""
        base = os.path.dirname(os.path.abspath(path))
        out = replace(self, links=[replace(link, source=relative_source(link.source, base))
                                   for link in self.links])
        text = out.to_json()
        write_whole(path, lambda f: f.write(text), mode="w", fsync=True, encoding="utf-8")

    @classmethod
    def load(cls, path: str) -> "Workspace":
        """Read a workspace and find its capture files: relative to the workspace's folder
        first, then where they were when it was saved. `missing_files` lists any not found.
        A file that cannot be read, is not UTF-8, is over `MAX_WORKSPACE_CHARS` characters, is
        not JSON or is nested too deeply raises ValueError naming `path`, and a device or pipe
        is read no further than the limit; one that is JSON but not a workspace raises
        ValueError. A leading byte-order mark is skipped, and a key named twice in one place
        keeps its last value, with a note."""
        try:
            with open(path, encoding="utf-8-sig") as f:
                text = f.read(MAX_WORKSPACE_CHARS + 1)
            if len(text) > MAX_WORKSPACE_CHARS:
                raise ValueError(f"over {MAX_WORKSPACE_CHARS:,} characters, too large for a "
                                 f"workspace")
            d, with_repeats = _parse_json(text)
        except (ValueError, RecursionError, OSError) as e:
            raise ValueError(f"{path}: not a readable workspace ({e})") from None
        name = import_notes.source_name(path)
        try:
            ws = cls.from_data(d, name)
        except RecursionError as e:
            raise ValueError(f"{path}: not a readable workspace ({e})") from None
        _note_repeats(with_repeats, name)
        base = os.path.dirname(os.path.abspath(path))
        for link in ws.links:
            link.source = resolve_source(link.source, base)
        return ws

    def missing_files(self) -> List[Tuple[int, str, str]]:
        """(Link index, source key, path as saved) for each capture file not found."""
        return [(i, key, str(link.source[key])) for i, link in enumerate(self.links)
                for key in source_files(link.source) if not os.path.isfile(link.source[key])]

    def locate(self, link: int, key: str, path: str) -> None:
        """The user found a missing file at `path`. Any other missing file with the same
        name in that folder is taken from there too: a capture folder moves as a whole."""
        self.links[link].source = {**self.links[link].source, key: os.path.abspath(path)}
        folder = os.path.dirname(os.path.abspath(path))
        for i, k, saved in self.missing_files():
            candidate = os.path.join(folder, os.path.basename(saved))
            if os.path.isfile(candidate):
                self.links[i].source = {**self.links[i].source, k: candidate}


# The keys a saved workspace and a saved Link may carry.
_WORKSPACE_FIELDS = frozenset(f.name for f in fields(Workspace))
_LINK_FIELDS = frozenset(f.name for f in fields(LinkSpec))


# What _bookmark_state finds a saved paired bookmark to be.
_BOOKMARK_READS, _BOOKMARK_UNREAD, _BOOKMARK_OFF_LINK = "reads", "unread", "off_link"


def _bookmark_state(b, n_links: int) -> str:
    """_BOOKMARK_READS when `b` is a paired bookmark BookmarkSet.from_json reads: a mapping
    whose sample is a whole sample number, whose index is a whole number 0..2**31-1 and
    whose Link is one of 0..`n_links` - 1. _BOOKMARK_OFF_LINK when only its Link fails (it
    is not one of those, or not a whole number); _BOOKMARK_UNREAD otherwise."""
    if not (isinstance(b, dict) and _sample(b.get("sample")) is not None):
        return _BOOKMARK_UNREAD
    if _in(b.get("index", 1), 0, _MAX_BOOKMARK_INDEX) is None:
        return _BOOKMARK_UNREAD
    link = _whole(b.get("link", 0))
    return _BOOKMARK_READS if link is not None and 0 <= link < n_links else _BOOKMARK_OFF_LINK


def _readable_bookmarks(bookmarks: list, n_links: int) -> Tuple[list, int, int]:
    """(the entries kept, how many do not read, how many name a Link that does not exist).
    Each entry is read by its own form, as BookmarkSet.from_json does: a mapping is a paired
    bookmark and a bare whole sample number is the list form; the workspace rule is
    stricter in taking whole JSON numbers only, never text or a fraction."""
    kept, unread, off_link = [], 0, 0
    for b in bookmarks:
        if isinstance(b, dict):
            state = _bookmark_state(b, n_links)
            if state == _BOOKMARK_READS:
                kept.append(b)
            elif state == _BOOKMARK_OFF_LINK:
                off_link += 1
            else:
                unread += 1
        elif _sample(b) is not None:
            kept.append(b)
        else:
            unread += 1
    return kept, unread, off_link


# A workspace file's extension. A "*.json" workspace file opens too.
WORKSPACE_SUFFIX = ".swi3s"

# Which keys of a source name files, by source type: an analog CSV's "clock" and "data" are
# channel names, a .bin pair's are its two files. "config_csv" is a file for every type,
# and so is each region's "config_csv@<n>" (Session.apply_config_csv_at).
_FILE_KEYS = {"sal": ("path",), "digital_csv": ("path",), "vcd": ("path",),
              "analog_csv": ("path",), "saleae_binary": ("clock", "data"),
              "wfm": ("clock_path", "data_path")}
_SAVED_AT = "saved_at"         # the absolute paths at save time, under this source key


def source_files(source: dict) -> List[str]:
    """The keys of `source` that hold a file path."""
    keys = list(_FILE_KEYS.get(str(source.get("type")), ()))
    if source.get("config_csv"):
        keys.append("config_csv")
    keys += sorted(k for k in source if str(k).startswith(CSV_SECTION_KEY))
    return [k for k in keys if isinstance(source.get(k), str) and source.get(k)]


def relative_source(source: dict, base: str) -> dict:
    """`source` with each file relative to `base`, the absolute paths kept under
    "saved_at". A file on another drive (Windows) stays absolute."""
    out = dict(source)
    saved = {}
    for key in source_files(source):
        full = os.path.abspath(source[key])
        saved[key] = full
        try:
            out[key] = os.path.relpath(full, base).replace(os.sep, "/")
        except ValueError:
            out[key] = full
    if saved:
        out[_SAVED_AT] = saved
    return out


def resolve_source(source: dict, base: str) -> dict:
    """`source` with each file as an absolute path that exists if one can be found: a
    relative path against `base`, then the path it had when saved. A file found nowhere
    keeps the path it was saved with, for `Workspace.missing_files` to report."""
    out = {k: v for k, v in source.items() if k != _SAVED_AT}
    saved = _dict_or_empty(source.get(_SAVED_AT))
    for key in source_files(out):
        p = out[key]
        tries = [p] if os.path.isabs(p) else [os.path.join(base, p)]
        if isinstance(saved.get(key), str):
            tries.append(saved[key])
        found = next((os.path.normpath(t) for t in tries if os.path.isfile(t)), None)
        if found is not None:
            out[key] = found
    return out


# The workspace each File ▸ Open Demo Capture entry opens with, by entry, in data/demos.
DEMO_WORKSPACES = {"phy1": "phy1.swi3s", "phy2": "phy2.swi3s",
                   "phy2_flow_control": "phy2_flow_control.swi3s", "phy3": "phy3.swi3s"}


def demo_dir() -> str:
    """Path to the bundled demo workspaces (data/demos), across dev, installed (pip
    data-files) and PyInstaller layouts."""
    return bundled_data_path("demos")


def demo_workspace(key: str, samples: int) -> Workspace:
    """The bundled workspace of demo `key` (a DEMO_WORKSPACES key), each Link's demo
    source set to `samples` audio samples per channel. Raises OSError when the file is
    missing and ValueError when it does not read as a workspace."""
    name = DEMO_WORKSPACES[key]
    with open(os.path.join(demo_dir(), name), encoding="utf-8") as f:
        ws = Workspace.from_json(f.read(), name)
    for link in ws.links:
        if link.source.get("type") != "demo":
            raise ValueError(f"{name}: Link {link.name!r} is not a demo source")
        link.source = {**link.source, "audio_samples_per_channel": int(samples)}
    return ws


def session_from_source(source: dict, register_map=None) -> Session:
    """Rebuild a Session from a workspace `source` descriptor. The type→factory
    dispatch lives on Session (co-located with the from_* factories); this is the
    workspace-layer entry point."""
    return Session.from_source(source, register_map=register_map)
