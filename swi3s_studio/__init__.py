"""SWI3S Studio — desktop MIPI SoundWire I3S bus analyzer.

A PySide6 UI over the C++ decode core (`swi3score`, built from `native/`), with capture
ingestion, an out-of-core results store, audio decode and timing analysis. See
docs/architecture.md.

`__version__` is shown in the window title and kept in sync with pyproject.toml. Release
notes, one file per version, are in docs/releases/.
"""
__version__ = "3.1.0"

from .api import DecodeResult, decode, decode_capture
from .ingest import saleae_binary
from .ingest.capture import Capture
from .ingest.transitions import build_capture_from_levels, demo_capture
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
