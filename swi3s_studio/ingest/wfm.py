"""Reader for a Tektronix ``.wfm`` waveform file (one analog channel per file).

Tek scopes (e.g. the MSO5 series) export each analog channel as a self-
describing binary waveform: a fixed-layout header (endianness + version tag,
then a "static" description block and a "frame" curve-object block whose *field
offsets* depend on the file version) followed by the raw sample codes. Voltages
are ``code * v_scale + v_offset``; the time axis is ``t0 + arange(n) * dt``. Only
version 3 (the format Tek scopes currently write) is implemented — see
`read_wfm` for the guard.

Layout reference (little-endian byte offsets, version 3)::

    0x000  u16      byte-order tag (0x0F0F == this file's endianness)
    0x002  char[8]  ":WFM#00x"  (x = version digit)
    0x00F  u8       bytes_per_point (curve sample size)
    0x010  i32      curve_buffer_start (offset of the curve *object*, not data)
    0x07A  i32      data_type (2 == this reader's expectation: single-valued)
    0x0A8  f64      v_scale
    0x0B0  f64      v_offset
    0x0F0  i32      v_format (numeric code of the curve's element type)
    0x1E8  f64      dt (horizontal sample interval)
    0x1F0  f64      t0 (horizontal time of the first sample)
    0x336  u32      data_start   (frame-0 curve: offset of sample data, within
                                  the curve buffer)
    0x33A  u32      postcharge_start (offset just past the sample data)

``curve_buffer_start + data_start`` is the absolute file offset of sample 0;
``(postcharge_start - data_start) / bytes_per_point`` is the sample count.
"""
from __future__ import annotations

import struct
import weakref
from typing import Dict, Optional, Tuple

import numpy as np

from . import analog

_BOM_LE = 0x0F0F
_BOM_BE = 0xF0F0

_VERSION_OFF = 0x002
_VERSION_LEN = 8                    # ":WFM#00x"

_BYTES_PER_POINT_OFF = 0x00F
_CURVE_BUFFER_START_OFF = 0x010
_DATA_TYPE_OFF = 0x07A
_EXPECTED_DATA_TYPE = 2              # "single-valued" curve (one sample per point)

_V_SCALE_OFF = 0x0A8
_V_OFFSET_OFF = 0x0B0
_V_FORMAT_OFF = 0x0F0

_DT_OFF_V3 = 0x1E8
_T0_OFF_V3 = 0x1F0

_DATA_START_OFF_V3 = 0x336
_POSTCHARGE_START_OFF_V3 = 0x33A

# v_format code -> (struct format char, byte size)
_V_FORMAT_CODES = {
    0: ("h", 2),   # int16
    1: ("i", 4),   # int32
    2: ("I", 4),   # uint32
    3: ("Q", 8),   # uint64
    4: ("f", 4),   # float32
    5: ("d", 8),   # float64
    6: ("B", 1),   # uint8
    7: ("b", 1),   # int8
}


def _endianness(data: bytes) -> str:
    tag = struct.unpack_from("<H", data, 0x000)[0]
    if tag == _BOM_LE:
        return "<"
    if tag == _BOM_BE:
        return ">"
    raise ValueError(f".wfm: unrecognised byte-order tag 0x{tag:04x} at offset 0x000")


def _version(data: bytes, endian: str) -> int:
    tag = data[_VERSION_OFF:_VERSION_OFF + _VERSION_LEN]
    # ":WFM#00x" — the version digit is the last of the 8 ASCII bytes.
    try:
        text = tag.decode("ascii")
        return int(text[5:8])
    except (UnicodeDecodeError, ValueError) as exc:
        raise ValueError(f".wfm: unrecognised version tag {tag!r} at offset 0x{_VERSION_OFF:03x}") from exc


# Bytes the header fields read_wfm parses span: every one sits below 0x340.
_HEADER_BYTES = 1024

# Bytes per point held while reading a .wfm pair (read_wfm twice): one shared float64
# time axis and two float64 volts arrays (24), and one file's raw codes while they are
# converted (at most 8). The analog front end comes on top (analog.front_end_bytes).
_PAIR_READ_BYTES_PER_POINT = 32

# The last time axis built, by (record length, interval, start): the two files of one
# acquisition share one read-only array. Held weakly, so it lives as long as a caller
# keeps it.
_LAST_TIME: Dict[Tuple[int, float, float], "weakref.ref[np.ndarray]"] = {}


def predict_pair_peak_bytes(n_points: int) -> int:
    """Predicted peak of reading a pair of `n_points`-point .wfm files and thresholding
    them with analog.capture_from_analog. Measured with tracemalloc at about 33 bytes
    per point on a 10M-point pair, against 44 here."""
    n = int(n_points)
    return n * _PAIR_READ_BYTES_PER_POINT + analog.front_end_bytes(n)


def _time_axis(n: int, dt: float, t0: float) -> np.ndarray:
    """``t0 + arange(n) * dt`` as a read-only float64 array, shared with the last call
    that asked for the same axis while that array is still alive."""
    key = (int(n), float(dt), float(t0))
    ref = _LAST_TIME.get(key)
    time = ref() if ref is not None else None
    if time is None:
        time = np.arange(n, dtype=np.float64)
        time *= dt
        time += t0
        time.flags.writeable = False
        _LAST_TIME.clear()
        _LAST_TIME[key] = weakref.ref(time)
    return time


def read_header(path: str) -> Dict[str, float]:
    """What a .wfm is, from its first kilobyte: ``{"sample_rate_hz", "n_points", "t0",
    "duration_s"}``. Every field read_wfm takes before the curve sits below 0x340, so the
    Open Capture dialog learns a pair's length without reading either record."""
    with open(path, "rb") as f:
        data = f.read(_HEADER_BYTES)
    if len(data) < 0x340:
        raise ValueError(f"{path}: too short for a .wfm header ({len(data)} bytes)")
    endian = _endianness(data)
    version = _version(data, endian)
    if version != 3:
        raise NotImplementedError(f"{path}: .wfm version {version} is not supported")
    bytes_per_point = struct.unpack_from(f"{endian}B", data, _BYTES_PER_POINT_OFF)[0]
    dt = struct.unpack_from(f"{endian}d", data, _DT_OFF_V3)[0]
    t0 = struct.unpack_from(f"{endian}d", data, _T0_OFF_V3)[0]
    data_start = struct.unpack_from(f"{endian}I", data, _DATA_START_OFF_V3)[0]
    postcharge_start = struct.unpack_from(f"{endian}I", data, _POSTCHARGE_START_OFF_V3)[0]
    n = max(0, (postcharge_start - data_start) // max(1, bytes_per_point))
    return {"sample_rate_hz": (1.0 / dt) if dt else 0.0, "n_points": float(n),
            "t0": float(t0), "duration_s": float(n * dt)}


def read_wfm_pair(clock_path: str, data_path: str, max_bytes: Optional[int] = None
                  ) -> Tuple[Dict[str, object], Dict[str, object]]:
    """``(read_wfm(clock_path), read_wfm(data_path))`` with the pair priced ONCE, from
    both headers, before either curve is read: `predict_pair_peak_bytes` of the longer
    record against `memory_budget(max_bytes)` (the .sal convention: None uses the
    computed budget, a positive value overrides it, 0 opts out), raising
    :class:`analog.AnalogTooLargeError` over budget. The two reads then run unchecked,
    so the second is not priced against memory the first already holds."""
    if max_bytes != 0:
        n = max(int(read_header(p)["n_points"]) for p in (clock_path, data_path))
        analog.check_budget(f"{clock_path} + {data_path}", n, predict_pair_peak_bytes(n),
                            max_bytes)
    return read_wfm(clock_path, max_bytes=0), read_wfm(data_path, max_bytes=0)


def read_wfm(path: str, max_bytes: Optional[int] = None) -> Dict[str, object]:
    """Parse a Tektronix .wfm file into ``{"time", "volts", "sample_rate_hz",
    "label", "version"}``. `time` is seconds (may start negative, pre-trigger),
    READ-ONLY, and the same array for two files with the same record length,
    interval and start (the two channels of one acquisition); `volts` is the decoded
    channel in volts; `label` is the file's base name (scope .wfm files carry no
    channel name field we rely on, so callers should prefer their own
    filename-derived label).

    Only the header and the curve are read; the codes are converted in place. A .wfm
    is one channel of a pair, so the memory guard prices the pair
    (`predict_pair_peak_bytes`) before the curve is read: `max_bytes` follows the
    .sal convention (None uses the shared `memory_budget()`, a positive value
    overrides it, 0 opts out) and :class:`analog.AnalogTooLargeError` is raised over
    budget. To read both files of a pair, use :func:`read_wfm_pair`, which checks the
    pair once before either is read."""
    with open(path, "rb") as f:
        data = f.read(_HEADER_BYTES)
    if len(data) < 0x340:
        raise ValueError(f"{path}: too short for a .wfm header ({len(data)} bytes)")

    endian = _endianness(data)
    version = _version(data, endian)
    if version != 3:
        # Only version 3 field offsets are implemented. Earlier/later versions move
        # the static/horizontal/curve-object field offsets used below, so bail
        # loudly rather than misread the header.
        raise NotImplementedError(
            f"{path}: .wfm version {version} is not supported (only version 3 "
            f"field offsets are implemented); add offsets for this version "
            f"before reading it")

    bytes_per_point = struct.unpack_from(f"{endian}B", data, _BYTES_PER_POINT_OFF)[0]
    curve_buffer_start = struct.unpack_from(f"{endian}i", data, _CURVE_BUFFER_START_OFF)[0]
    data_type = struct.unpack_from(f"{endian}i", data, _DATA_TYPE_OFF)[0]
    if data_type != _EXPECTED_DATA_TYPE:
        raise NotImplementedError(
            f"{path}: .wfm data_type {data_type} is not supported (expected "
            f"{_EXPECTED_DATA_TYPE}, single-valued)")

    v_scale = struct.unpack_from(f"{endian}d", data, _V_SCALE_OFF)[0]
    v_offset = struct.unpack_from(f"{endian}d", data, _V_OFFSET_OFF)[0]
    v_format = struct.unpack_from(f"{endian}i", data, _V_FORMAT_OFF)[0]
    if v_format not in _V_FORMAT_CODES:
        raise NotImplementedError(f"{path}: .wfm v_format code {v_format} is not supported")
    fmt_char, fmt_size = _V_FORMAT_CODES[v_format]
    if fmt_size != bytes_per_point:
        raise ValueError(
            f"{path}: bytes_per_point ({bytes_per_point}) doesn't match v_format "
            f"{v_format} ('{fmt_char}', {fmt_size} bytes) — unrecognised layout")

    dt = struct.unpack_from(f"{endian}d", data, _DT_OFF_V3)[0]
    t0 = struct.unpack_from(f"{endian}d", data, _T0_OFF_V3)[0]

    data_start = struct.unpack_from(f"{endian}I", data, _DATA_START_OFF_V3)[0]
    postcharge_start = struct.unpack_from(f"{endian}I", data, _POSTCHARGE_START_OFF_V3)[0]
    n = (postcharge_start - data_start) // bytes_per_point
    if n <= 0:
        raise ValueError(f"{path}: empty or malformed curve (data_start={data_start}, "
                         f"postcharge_start={postcharge_start})")

    analog.check_budget(path, n, predict_pair_peak_bytes(n), max_bytes)

    curve_off = curve_buffer_start + data_start
    dtype = np.dtype(f"{endian}{fmt_char}")
    if dtype.itemsize != struct.calcsize(f"{endian}{fmt_char}"):
        raise AssertionError(f"{path}: dtype/struct size mismatch for format '{fmt_char}'")
    with open(path, "rb") as f:
        f.seek(curve_off)
        raw = f.read(n * dtype.itemsize)
    if len(raw) < n * dtype.itemsize:
        raise ValueError(f"{path}: truncated curve ({len(raw)} of {n * dtype.itemsize} "
                         f"bytes at offset {curve_off})")
    codes = np.frombuffer(raw, dtype=dtype, count=n)

    # Converted in place: the same per-element arithmetic as codes * v_scale + v_offset.
    volts = codes.astype(np.float64)
    del codes, raw
    volts *= v_scale
    volts += v_offset
    time = _time_axis(n, dt, t0)
    sample_rate_hz = 1.0 / dt if dt else 0.0

    return {
        "time": time,
        "volts": volts,
        "sample_rate_hz": sample_rate_hz,
        "label": path.rsplit("/", 1)[-1],
        "version": version,
    }
