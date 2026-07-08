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

WORKSPACE_VERSION = 2   # v2 adds `view` (TX-map + Hide-Clock pane state)


@dataclass
class Workspace:
    source: dict                                   # {"type": "demo"|"saleae_binary", ...}
    overlay: List[list] = field(default_factory=list)   # what-if register overrides,
    #                                    [[section, device, address, value], ...]
    bookmarks: List[int] = field(default_factory=list)  # [sample, ...]
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
    """Rebuild a Session from a workspace `source` descriptor."""
    kind = source.get("type")
    cfg = source.get("config_csv", "")               # decode-driving config CSV (optional)
    ssp = int(source.get("ssp_row", -1))             # manual Stream Sync Point row (optional)
    if kind == "demo":
        return Session.from_demo(int(source.get("audio_samples_per_channel", 32)),
                                 cold_start=bool(source.get("cold_start", False)),
                                 register_map=register_map, config_csv=cfg, ssp_row=ssp)
    if kind == "saleae_binary":
        return Session.from_saleae_binary(source["clock"], source["data"],
                                          int(source["sample_rate_hz"]),
                                          register_map=register_map, config_csv=cfg, ssp_row=ssp)
    if kind == "sal":
        return Session.from_sal(source["path"], int(source["clock_channel"]),
                                int(source["data_channel"]),
                                int(source.get("sample_rate_hz", 0)),
                                auto_clock=bool(source.get("auto_clock", False)),
                                register_map=register_map, config_csv=cfg, ssp_row=ssp)
    if kind == "digital_csv":
        return Session.from_digital_csv(source["path"], int(source["clock_col"]),
                                        int(source["data_col"]),
                                        int(source.get("sample_rate_hz", 0)),
                                        register_map=register_map, config_csv=cfg, ssp_row=ssp)
    if kind == "vcd":
        return Session.from_vcd(source["path"], str(source["clock_ident"]),
                                str(source["data_ident"]),
                                auto_clock=bool(source.get("auto_clock", False)),
                                register_map=register_map, config_csv=cfg, ssp_row=ssp)
    raise ValueError(f"unknown workspace source type: {kind!r}")
