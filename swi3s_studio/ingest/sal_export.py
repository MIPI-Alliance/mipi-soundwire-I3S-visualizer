"""Writer for the Saleae Logic 2 ``.sal`` project file (the export half of
``saleae_sal``).

Turns a :class:`~swi3s_studio.ingest.capture.Capture` (clock + data edge lists)
into a ``.sal`` — a ZIP of ``meta.json`` + one ``digital-N.bin`` per channel —
that ``saleae_sal.load_capture`` reads back unchanged, **and that Logic 2 opens
as a native project.**

Logic 2 will not open a ``.sal`` whose blobs are the documented version-0 binary
export (it reports "an older version … could not be opened") and validates
``meta.json`` against a schema before opening; a native project uses the
**version-3** compressed internal blob (``type=100``) and a ``meta.json`` with
``version: 22`` carrying a full set of ``data`` keys. This writer emits both,
reverse-engineered from several real Logic Pro 16 captures (see
docs/saleae_sal_format.md and ``saleae_binary.parse_channel_v3``).

Version-3 blob header, exactly as Logic writes it (little-endian)::

    0   char[8]  "<SALEAE>"
    8   u32      version (== 3)
    12  u32      type    (== 100, digital)
    16  u8       == 1                       (constant flag)
    17  f64      sample_rate
    25  u64      capture unix-time ms
    33  f64      capture fractional ms
    41  u8       preamble flag (0 for a simple single-region capture)
    42  u64      block_count × 256  (Logic reads block_count = u64 // 256)
    50  u8       == 0
    51  block:   u64 A_start | u64 B_end | u16 level | u64 byte_count | codec…
    ..  block:   … (chains: A_start == previous B_end)

The preamble at offset 41 is variable-length in real captures: ``flag`` then
``N × u64`` records then a ``0`` byte. For a simple (untriggered) capture flag is 0
and there is one record — the **block count encoded as ``nblocks × 256``** (Logic
reads ``block_count = u64 // 256``); writing 0, or one giant block, makes Logic
stall on "Preparing session". A real busy channel chunks into many blocks (800+),
so the exporter chunks too (``V3_LOGIC_BLOCK_DELTAS`` transitions per block) and sets the
count to match. ``X``/``Y``-style values in the multi-record (triggered) form are
Logic's trigger bookkeeping; the reader locates the block chain by scanning, so it
round-trips regardless. The blocks are the base-128 delta codec written by
:func:`saleae_binary.encode_v3_delta`; both channels fill their trailing gap to a
**shared** capture-end sample so Logic sees one timeline length. The full-header
blob itself is built by :func:`saleae_binary.build_logic2_channel_v3` (the codec +
block-chain layout lives in one place).

The project ZIP must also contain a ``trigger-store.bin`` member (a fixed 32-byte
``<SALEAE>`` v3 type-103 blob, ``_TRIGGER_STORE``) or Logic fails to open the
project with "Failed to load file", even with a valid ``meta.json`` and blobs.
"""
from __future__ import annotations

import json
import struct
import uuid
import zipfile

import numpy as np

from . import saleae_binary
from .capture import Capture

# Deterministic namespace so a given (channel, name) always yields the same row id
# (reproducible exports / golden tests) while still being a valid UUID for Logic.
_ROW_ID_NS = uuid.UUID("5731335f-5354-5544-494f-5f73616c6578")   # "SWI3S_STUDIO_salex"-ish

# Logic 2 requires a trigger-store member in the project ZIP; without it the project
# fails to load ("Failed to load file") even with a valid meta.json + digital blobs.
# It's a fixed 32-byte <SALEAE> v3 blob (type 103): identifier, version 3, type 103,
# a 1 byte, then 15 zero bytes — byte-identical across every real capture inspected
# (independent of trigger settings / channels / rate), so it's emitted as a constant.
_TRIGGER_STORE = saleae_binary.SALEAE_MAGIC + struct.pack("<II", saleae_binary.V3_VERSION, 103) \
    + struct.pack("<B", 1) + b"\x00" * 15


def _row_id(device_channel: int, name: str) -> str:
    return str(uuid.uuid5(_ROW_ID_NS, f"{device_channel}:{name}"))


def _channel_ref(device_channel: int) -> dict:
    return {"category": "legacy", "type": "Digital", "deviceChannel": int(device_channel)}


# Standard Logic Pro 16 digital sample-rate menu (Hz), highest first. The capture's
# own rate is merged in (§_sample_rate_options) so Logic's schema, which expects
# legacySettings.sampleRate.digital to be one of capabilities.sampleRateOptions,
# always finds it — our synthetic rates (e.g. 786.432 MHz) aren't in the stock menu.
_STD_SAMPLE_RATES_HZ = [500_000_000, 250_000_000, 125_000_000, 100_000_000, 50_000_000,
                        25_000_000, 20_000_000, 12_500_000, 10_000_000, 6_250_000,
                        5_000_000, 4_000_000, 2_500_000, 2_000_000, 1_000_000]


def _sample_rate_options(rate_hz: int) -> list[dict]:
    rates = sorted({int(rate_hz), *_STD_SAMPLE_RATES_HZ}, reverse=True)
    return [{"digital": r} for r in rates]


def _channel_capabilities(max_channel: int) -> list[dict]:
    """Interleaved Digital/Analog per index (0..max_channel), matching a real Logic
    Pro 16 `capabilities.channelCapabilities` array."""
    caps = []
    for i in range(max(15, int(max_channel)) + 1):
        caps.append({"type": "Digital", "index": i, "capability": "Toggleable"})
        caps.append({"type": "Analog", "index": i, "capability": "Toggleable"})
    return caps


def _data_table_columns() -> dict:
    """The analyzer data-table column view-state Logic 2 writes (copied verbatim from
    a real v22 capture; required by the meta.json schema)."""
    return {"columns": {
        "analyzerIdentifier": {"isActive": True, "width": 18, "isDefault": True,
                               "baseKey": "analyzerIdentifier", "excludeFromSearch": True},
        "frameType": {"isActive": True, "width": 75, "isDefault": True,
                      "baseKey": "frameType", "excludeFromSearch": False},
        "start": {"isActive": True, "width": 110, "isDefault": True,
                  "baseKey": "start", "excludeFromSearch": True},
        "duration": {"isActive": True, "width": 80, "isDefault": True,
                     "baseKey": "duration", "excludeFromSearch": True},
        "data_data": {"width": 167, "baseKey": "data", "isActive": False},
        "data_channel": {"width": 157, "baseKey": "channel", "isActive": False},
    }}


def _meta_json(sample_rate_hz: int, channels: list[tuple[int, str]],
               unix_ms: int, frac_ms: float, total_samples: int) -> str:
    """Build a Logic-2 ``version: 22`` ``meta.json`` for the exported channels.

    `channels` is an ordered list of (device_channel, display_name). Logic 2
    validates meta.json against a schema before it opens the project, so this emits
    the full set of ``data`` keys a real v22 capture has (verified across several
    real Logic Pro 16 ``.sal`` files): renderViewState, captureStartTime,
    timingMarkers, measurements, highLevelAnalyzers, analyzers, rowsSettings,
    captureSettings, legacyDevice (+capabilities), legacySettings, digitalTriggerTime,
    name, dataTable, analyzerTrigger, timeManager, captureNotes — plus top-level
    binData. Our own `saleae_sal.read_info` keys off `data.legacySettings.sampleRate.
    digital` and `binData`, both present, so the export still round-trips in-package.
    (legacyDeviceCalibration is omitted — it's absent from some real files, i.e.
    optional.)"""
    view_scale = (total_samples / sample_rate_hz) if sample_rate_hz and total_samples else 1.0
    max_channel = max((ch for ch, _ in channels), default=0)
    rows = [{
        "id": _row_id(ch, name),
        "height": 72,
        "isMarkedHidden": False,
        "type": "channel",
        "name": name,
        "channel": _channel_ref(ch),
    } for ch, name in channels]
    data = {
        "renderViewState": {
            "type": "PanAndZoom",
            "leftEdgeTimeSec": 0.0,
            "timeScaleSeconds": view_scale,
        },
        "captureStartTime": {
            "unixTimeMilliseconds": int(unix_ms),
            "fractionalMilliseconds": float(frac_ms),
        },
        "timingMarkers": {"markers": {}, "pairs": {}},
        "measurements": [],
        "highLevelAnalyzers": [],
        "analyzers": [],
        "rowsSettings": rows,
        "captureSettings": {
            "bufferSizeMb": 2048,
            "timerModeSettings": {"stopAfterSeconds": 100},
            "commonCaptureSettings": {"trimAfterCapture": False, "trimTimeSeconds": 5},
            "triggerSettings": {
                "eventChannel": _channel_ref(channels[0][0] if channels else 0),
                "triggerSourceGeneration": 0,
                "scopeEventType": "Rising", "scopeThreshold": 1,
                "scopeHysteresisPercentage": 0.02,
                "digitalEventType": "Rising", "digitalLinkedChannels": [],
                "digitalLegacyPostTriggerBufferSeconds": 10, "mode": "Auto",
                "holdOffSeconds": 0.001, "pulseDuration": {"min": 0.001, "max": 0.01},
                "realTriggerTimeoutViewRatio": 4, "minRealTriggerTimeoutSeconds": 1,
                "autoTriggerTimeoutViewRatio": 2,
            },
            "captureMode": "Trigger", "captureTriggerType": "Signal",
        },
        "legacyDevice": {
            "deviceId": "0",
            "name": "SWI3S Studio",
            "deviceType": "LogicPro16",
            # isSimulation False (+ isPhysicalDevice True below) matches a real capture
            # so Logic doesn't pop the "This Capture Contains Simulated Data!" banner —
            # the exporter usually re-emits real captured edges (from .bin/.csv), not a
            # simulation.
            "isSimulation": False,
            "capabilities": {
                "channelCapabilities": _channel_capabilities(max_channel),
                "sampleRateOptions": _sample_rate_options(sample_rate_hz),
                "digitalThresholdOptions": [{"description": "1.2 Volts"},
                                            {"description": "1.8 Volts"},
                                            {"description": "3.3+ Volts"}],
                "isPhysicalDevice": True,
            },
        },
        "legacySettings": {
            "enabledChannels": [{"type": "Digital", "index": int(ch)} for ch, _ in channels],
            "sampleRate": {"digital": int(sample_rate_hz)},
            "digitalThreshold": {"description": "1.2 Volts"},
            "glitchFilter": {"enabled": False, "channels": []},
        },
        "digitalTriggerTime": 0.0,
        "name": "SWI3S Studio Export",
        "dataTable": _data_table_columns(),
        "analyzerTrigger": {"settings": {"enabled": False, "searchQuery": "",
                                         "holdoffSeconds": 0}},
        "timeManager": {"t0": {"type": "trigger"}},
        "captureNotes": "",
    }
    return json.dumps({
        "version": 22,
        "data": data,
        # binData maps each per-channel blob to its device channel (read back by
        # saleae_sal.read_info). "category":"legacy" mirrors a real Logic project.
        "binData": [
            {"category": "legacy", "type": "Digital", "deviceChannel": int(ch),
             "file": f"./digital-{int(ch)}.bin"}
            for ch, _ in channels
        ],
    })


def export_sal(capture: Capture, path: str, *,
               clock_channel: int = 0, data_channel: int = 1,
               clock_name: str = "SW_CLK", data_name: str = "SW_DATA") -> None:
    """Write `capture` out as a Logic 2 ``.sal`` project.

    Encodes `clock_edges`/`data_edges` as **version-3** <SALEAE> digital blobs on
    `clock_channel` / `data_channel` (the format Logic 2 opens natively; see the
    module docstring) plus a ``version: 22`` ``meta.json``. Both blobs fill their
    trailing gap to one shared capture-end sample so Logic sees a single timeline
    length. The result also round-trips exactly through
    `saleae_sal.load_capture(path, clock_channel, data_channel)`.
    """
    if clock_channel == data_channel:
        raise ValueError(f"clock_channel and data_channel must differ (both {clock_channel})")
    rate = int(capture.sample_rate_hz)
    if rate <= 0:
        raise ValueError(f"export_sal: capture.sample_rate_hz must be > 0 (got {rate})")

    clk = np.ascontiguousarray(capture.clock_edges, dtype=np.uint64)
    dat = np.ascontiguousarray(capture.data_edges, dtype=np.uint64)
    # One capture length for the whole project: the last edge on either channel + 1
    # (a real .sal stores the same final B_end in every channel).
    last_edge = 0
    if clk.size:
        last_edge = max(last_edge, int(clk[-1]))
    if dat.size:
        last_edge = max(last_edge, int(dat[-1]))
    capture_end = last_edge + 1

    # Synthetic capture: no wall-clock time. Use a fixed epoch so exports are
    # reproducible (byte-identical goldens); Logic only uses it for a display label.
    unix_ms, frac_ms = 0, 0.0

    channels = [(clock_channel, clock_name), (data_channel, data_name)]
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=1) as z:
        z.writestr("meta.json", _meta_json(rate, channels, unix_ms, frac_ms, capture_end))
        z.writestr(f"digital-{clock_channel}.bin",
                   saleae_binary.build_logic2_channel_v3(
                       bool(capture.initial_clock), clk, sample_rate_hz=rate,
                       unix_ms=unix_ms, frac_ms=frac_ms, capture_end=capture_end))
        z.writestr(f"digital-{data_channel}.bin",
                   saleae_binary.build_logic2_channel_v3(
                       bool(capture.initial_data), dat, sample_rate_hz=rate,
                       unix_ms=unix_ms, frac_ms=frac_ms, capture_end=capture_end))
        z.writestr("trigger-store.bin", _TRIGGER_STORE)   # required by Logic to open

