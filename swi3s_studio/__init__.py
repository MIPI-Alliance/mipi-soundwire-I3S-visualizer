"""SWI3S Studio — desktop MIPI SoundWire I3S bus analyzer.

This package wraps the verified C++ decode core (`swi3score`, built from the
Saleae plugin sources) with capture ingestion, an out-of-core results store, and
(later) a PySide6 UI. See ../architecture.md.
"""
# App version, shown in the window title. Kept in sync with pyproject.toml.
# 3.0.3: Windows support (x64 wheels, MSVC build, launchers), CDS disparity column,
#        ping-period stats, v0 .sal load memory fix; MIPI OSS release scaffolding.
# 3.0.2: mode-grouped menus; per-mode File submenus; link-control timeline + timing.
# 3.0.1: workspaces persist the TX-map + Hide-Clock view state.
__version__ = "3.0.3"

from .ingest.capture import Capture
from .ingest.transitions import build_capture_from_levels, demo_capture
from .ingest import saleae_binary
from .api import DecodeResult, decode, decode_capture
from .session import Session
from .workspace import Workspace, session_from_source

__all__ = [
    "Capture",
    "build_capture_from_levels",
    "demo_capture",
    "saleae_binary",
    "DecodeResult",
    "decode",
    "decode_capture",
    "Session",
    "Workspace",
    "session_from_source",
]
