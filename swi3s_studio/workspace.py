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
from dataclasses import asdict, dataclass, field, replace
from typing import List, Optional, Tuple

from .session import CSV_SECTION_KEY, Session

WORKSPACE_VERSION = 4   # v4: per-Link state moves into `links` (one entry per Link, each with
#                         its source and time offset); bookmarks carry the Link they are on.
#                         v3 (paired bookmark dicts) and earlier load as one Link at offset 0.
#                         v3: bookmarks are paired {sample,group,index,label} dicts
#                         (v2 added `view`; legacy `[sample,...]` still loads, paired A1,A2,…)

# The per-Link keys, which v3 and earlier held at the top level.
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
    # preference, not a decode input; absent from workspaces saved before 3.0.19.
    stream_colors: list = field(default_factory=list)
    # Per-stream Filter & Gain as [device, dp, {"highpass_hz", "gain_db",
    # "allow_clipping", "dc_block"}] (StreamProcessing.to_json). Changes what the waveform, playback and
    # export carry but not the decode; absent from workspaces saved before 3.0.20.
    stream_processing: list = field(default_factory=list)
    # The Link's own view (ui/view_state.py dicts): its Commands filter and column widths,
    # and its Audio and Capture panes' channels, zoom and toggles. Absent before 3.0.20.
    view: dict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d) -> "LinkSpec":
        if not isinstance(d, dict) or not isinstance(d.get("source"), dict):
            raise ValueError("not a valid workspace file (a Link is missing its 'source')")
        return cls(source=d["source"], name=str(d.get("name", "Link 1")),
                   offset_ps=int(d.get("offset_ps", 0)),
                   overlay=d.get("overlay", []),
                   device_regmaps=d.get("device_regmaps", {}),
                   device_names=d.get("device_names", {}),
                   device_hub_depths=d.get("device_hub_depths", {}),
                   device_scramblers=d.get("device_scramblers", []),
                   stream_colors=d.get("stream_colors", []),
                   stream_processing=d.get("stream_processing", []),
                   view=_dict_or_empty(d.get("view")))


def _dict_or_empty(v) -> dict:
    return v if isinstance(v, dict) else {}


@dataclass
class Workspace:
    links: List[LinkSpec]                          # one per Link, in LinkSet order
    active_link: int = 0
    bookmarks: List = field(default_factory=list)      # paired bookmarks:
    #   [{"sample":int,"group":"A","index":1,"label":"","link":0}, ...]  (legacy [int] loads)
    cursor: int = 0                                # in the ACTIVE Link's samples
    mode: str = "Analysis"                         # active macro-mode on save
    authoring: Optional[dict] = None               # BusConfig.to_dict() (Visualization mode)
    timing: Optional[dict] = None                  # TimingView inputs (Timing mode)
    # Transient Bus-Grid / Raw-Capture pane state (v2): {tx_map, tx_persist, grid_rows,
    # tx_start_row, show_clock}. Purely view prefs, window-wide rather than per Link —
    # re-applied after the decode.
    view: dict = field(default_factory=dict)
    version: int = WORKSPACE_VERSION

    @classmethod
    def single(cls, source: dict, **kw) -> "Workspace":
        """A one-Link workspace: `source` and any LinkSpec field go to the Link, the rest
        to the workspace."""
        link_kw = {k: kw.pop(k) for k in list(kw) if k in _LINK_KEYS + ("name", "offset_ps")}
        return cls(links=[LinkSpec(source=source, **link_kw)], **kw)

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2)

    @classmethod
    def from_json(cls, text: str) -> "Workspace":
        d = json.loads(text)
        if not isinstance(d, dict):
            raise ValueError("not a valid workspace file (missing 'source')")
        if "links" in d:
            raw_links = d["links"]
            if not isinstance(raw_links, list) or not raw_links:
                raise ValueError("not a valid workspace file (no Links)")
            links = [LinkSpec.from_dict(x) for x in raw_links]
        else:
            # v3 and earlier: one Link, its state at the top level.
            if not isinstance(d.get("source"), dict):
                raise ValueError("not a valid workspace file (missing 'source')")
            links = [LinkSpec.from_dict({k: d[k] for k in ("source",) + _LINK_KEYS
                                         if k in d})]
        active = int(d.get("active_link", 0))
        if not 0 <= active < len(links):
            raise ValueError(f"not a valid workspace file (active Link {active} of "
                             f"{len(links)})")
        return cls(links=links, active_link=active,
                   bookmarks=d.get("bookmarks", []), cursor=int(d.get("cursor", 0)),
                   mode=d.get("mode", "Analysis"), authoring=d.get("authoring"),
                   timing=d.get("timing"),
                   view=d.get("view", {}))    # migrated: it IS the current version now

    def save(self, path: str) -> None:
        """Write the workspace, each capture file named relative to the workspace's folder
        (and absolute as a fallback), so a folder holding both can be moved or shared."""
        base = os.path.dirname(os.path.abspath(path))
        out = replace(self, links=[replace(link, source=relative_source(link.source, base))
                                   for link in self.links])
        with open(path, "w", encoding="utf-8") as f:
            f.write(out.to_json())

    @classmethod
    def load(cls, path: str) -> "Workspace":
        """Read a workspace and find its capture files: relative to the workspace's folder
        first, then where they were when it was saved. `missing_files` lists any not found."""
        with open(path, encoding="utf-8") as f:
            ws = cls.from_json(f.read())
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


# A workspace file's extension. Older workspaces were "*.json" (often "*.swi3s.json"); they
# still open.
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


def session_from_source(source: dict, register_map=None) -> Session:
    """Rebuild a Session from a workspace `source` descriptor. The type→factory
    dispatch lives on Session (co-located with the from_* factories); this is the
    workspace-layer entry point."""
    return Session.from_source(source, register_map=register_map)
