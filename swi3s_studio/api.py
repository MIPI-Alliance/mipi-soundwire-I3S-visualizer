"""Decode API: run the swi3score core over a sample source / capture."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Dict, List

try:
    import swi3score
except ImportError as exc:       # the core is built from native/, not installed as a dependency
    raise ImportError(
        f"The swi3score native decode core is missing or failed to load ({exc}). Build and install it:\n"
        "    python3 -m pip install ./native\n"
        "(or, offline:  bash native/build_local.sh)") from exc

from .ingest.capture import Capture


@dataclass
class DecodeResult:
    commands: List[Dict[str, Any]] = field(default_factory=list)
    audio: List[Dict[str, Any]] = field(default_factory=list)
    column_count: int = 0
    row_rate_khz: float = 0.0
    ui_rate_hz: float = 0.0


def _remove(path: str) -> None:
    """Delete the upgraded copy of a config CSV, if this call made one."""
    if path:
        try:
            os.remove(path)
        except OSError:
            pass


def decode(source: "swi3score.ISampleSource", *, forced_column_count: int = 0,
           decode_audio: bool = True, config_csv: str = "",
           scrambler_overrides=None) -> DecodeResult:
    """Decode a bare sample source. A source carries no link bring-up to read, so this
    decodes with neither a Safe-Lock seed nor restart points: for a Capture use
    decode_capture, which decodes as Session does."""
    settings = swi3score.DecoderSettings()
    settings.forced_column_count = int(forced_column_count)
    settings.decode_audio = bool(decode_audio)
    original, upgraded = config_csv, ""
    if config_csv:
        from .ingest import visualizer_csv
        config_csv = visualizer_csv.normalized_path(config_csv)
        if config_csv != original:
            upgraded = config_csv                  # a temp copy this call owns
    try:
        if config_csv:
            visualizer_csv.report_csv_warnings(config_csv, original)
        settings.config_csv_path = config_csv or ""
        if scrambler_overrides:
            settings.scrambler_overrides = [(int(d), int(p), 1 if on else 0)
                                            for (d, p), on in dict(scrambler_overrides).items()]
        dec = swi3score.Decoder(source, settings)
        dec.run()
    finally:
        _remove(upgraded)
    return DecodeResult(
        commands=dec.commands(),
        audio=dec.audio(),
        column_count=dec.column_count,
        row_rate_khz=dec.row_rate_khz,
        ui_rate_hz=dec.measured_ui_rate_hz,
    )


def decode_capture(capture: Capture, *, forced_column_count: int = 0,
                   decode_audio: bool = True, config_csv: str = "",
                   scrambler_overrides=None) -> DecodeResult:
    """Decode a Capture exactly as Session does: the bring-ups read from its edges seed
    the Safe-Lock column count and restart the decode at each later one, and a detected
    bring-up the decode contradicts is dropped."""
    from .session import Session
    s = Session(capture, forced_column_count=forced_column_count, decode_audio=decode_audio,
                config_csv=config_csv, scrambler_overrides=scrambler_overrides)
    return DecodeResult(commands=s.commands, audio=s.audio, column_count=s.column_count,
                        row_rate_khz=s.row_rate_khz, ui_rate_hz=s.ui_rate_hz)
