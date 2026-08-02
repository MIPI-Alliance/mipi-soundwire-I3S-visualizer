"""Decode API: run the swi3score core over a sample source / capture."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List

import swi3score

from .ingest.capture import Capture


@dataclass
class DecodeResult:
    commands: List[Dict[str, Any]] = field(default_factory=list)
    audio: List[Dict[str, Any]] = field(default_factory=list)
    column_count: int = 0
    row_rate_khz: float = 0.0
    ui_rate_hz: float = 0.0


def decode(source: "swi3score.ISampleSource", *, forced_column_count: int = 0,
           decode_audio: bool = True, config_csv: str = "",
           scrambler_overrides=None) -> DecodeResult:
    settings = swi3score.DecoderSettings()
    settings.forced_column_count = int(forced_column_count)
    settings.decode_audio = bool(decode_audio)
    if config_csv:
        from .ingest import visualizer_csv
        config_csv = visualizer_csv.normalized_path(config_csv)
    settings.config_csv_path = config_csv or ""
    if scrambler_overrides:
        settings.scrambler_overrides = [(int(d), int(p), 1 if on else 0)
                                        for (d, p), on in dict(scrambler_overrides).items()]
    dec = swi3score.Decoder(source, settings)
    dec.run()
    return DecodeResult(
        commands=dec.commands(),
        audio=dec.audio(),
        column_count=dec.column_count,
        row_rate_khz=dec.row_rate_khz,
        ui_rate_hz=dec.measured_ui_rate_hz,
    )


def decode_capture(capture: Capture, **kw) -> DecodeResult:
    return decode(capture.sample_source(), **kw)
