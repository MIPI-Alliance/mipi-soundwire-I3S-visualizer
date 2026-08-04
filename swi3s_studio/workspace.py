"""Workspace (session) save/load.

A workspace is a small JSON sidecar that records how to reconstruct an analysis
session: the capture *source* (demo parameters or a Saleae clock/data file pair),
plus the user's view state — what-if register overlay, bookmarks, and the cursor.
The decoded results themselves are not stored; they are re-derived by re-running
the decode on load (fast, and keeps workspaces tiny + portable).
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import List, Optional

from .session import Session

WORKSPACE_VERSION = 3   # v3: bookmarks are paired {sample,group,index,label} dicts
#                         (v2 added `view`; legacy `[sample,...]` still loads, paired A1,A2,…)


@dataclass
class Workspace:
    source: dict                                   # {"type": "demo"|"saleae_binary", ...}
    overlay: List[list] = field(default_factory=list)   # what-if register overrides,
    #                                    [[section, device, address, value], ...]
    bookmarks: List = field(default_factory=list)      # paired bookmarks:
    #   [{"sample":int,"group":"A","index":1,"label":""}, ...]  (legacy [int] also loads)
    cursor: int = 0
    mode: str = "Analysis"                         # active macro-mode on save
    authoring: Optional[dict] = None               # BusConfig.to_dict() (Visualization mode)
    timing: Optional[dict] = None                  # TimingView inputs (Timing mode)
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
    # Transient Bus-Grid / Raw-Capture pane state (v2): {tx_map, tx_persist, grid_rows,
    # tx_start_row, show_clock}. Purely view prefs — re-applied after the decode.
    view: dict = field(default_factory=dict)
    version: int = WORKSPACE_VERSION

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2)

    @classmethod
    def from_json(cls, text: str) -> "Workspace":
        d = json.loads(text)
        if not isinstance(d, dict) or not isinstance(d.get("source"), dict):
            raise ValueError("not a valid workspace file (missing 'source')")
        return cls(source=d["source"], overlay=d.get("overlay", []),
                   bookmarks=d.get("bookmarks", []), cursor=int(d.get("cursor", 0)),
                   mode=d.get("mode", "Analysis"), authoring=d.get("authoring"),
                   timing=d.get("timing"),
                   device_regmaps=d.get("device_regmaps", {}),
                   device_names=d.get("device_names", {}),
                   device_hub_depths=d.get("device_hub_depths", {}),
                   device_scramblers=d.get("device_scramblers", []),
                   view=d.get("view", {}),
                   version=int(d.get("version", WORKSPACE_VERSION)))

    def save(self, path: str) -> None:
        with open(path, "w", encoding="utf-8") as f:
            f.write(self.to_json())

    @classmethod
    def load(cls, path: str) -> "Workspace":
        with open(path, encoding="utf-8") as f:
            return cls.from_json(f.read())


def session_from_source(source: dict, register_map=None) -> Session:
    """Rebuild a Session from a workspace `source` descriptor. The type→factory
    dispatch lives on Session (co-located with the from_* factories); this is the
    workspace-layer entry point."""
    return Session.from_source(source, register_map=register_map)
