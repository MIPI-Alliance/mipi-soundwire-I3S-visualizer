"""Reader for the Saleae Logic 2 ``.sal`` project file.

A ``.sal`` is a ZIP containing ``meta.json`` + one ``digital-N.bin`` per captured
digital channel (and analog files). ``meta.json`` gives the digital sample rate
and the channel <-> file mapping. Each ``digital-N.bin`` begins with the
``<SALEAE>`` header; we decode both **version 0** (the documented export layout,
absolute transition times) and **versions 3 and 4** (Logic 2's compressed internal
format, per-block sample deltas — see ``saleae_binary.parse_channel_v3``; 4 is the
same format with only the version field bumped). Other versions raise
UnsupportedSaleaeVersion with guidance to export Binary/CSV.
"""
from __future__ import annotations

import contextlib
import json
import logging
import math
import sys
import zipfile
import zlib
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

import numpy as np

from .. import import_notes
from ..nputil import searchsorted
from . import saleae_binary
from .capture import Capture
from .timebase import (
    MAX_RATE_HZ,
    TimeRangeError,
    check_rate,
    drop_nonfinite,
    repeats,
    require_distinct,
    require_finite,
    same_sample_note,
    to_samples,
)

logger = logging.getLogger(__name__)


@dataclass
class SalInfo:
    sample_rate_hz: int
    # device-channel -> "digital-N.bin" name
    digital_files: Dict[int, str] = field(default_factory=dict)
    # device-channel numbers of enabled digital channels, in order
    channels: List[int] = field(default_factory=list)
    # what reading meta.json passed over or assumed, as import warning texts
    notes: List[str] = field(default_factory=list)


@dataclass
class SalCost:
    """Predicted memory cost of opening a .sal, WITHOUT decoding it.

    A .sal is a zip of delta-coded blobs, so its decoded edge arrays can be many
    times the file size. Every figure here comes from the zip central directory
    plus a few KB of header, so the estimate itself costs nothing.
    """
    file_bytes: int                    # the .sal on disk
    uncompressed_bytes: int            # blobs once inflated (what z.read() materialises)
    est_transitions: int               # summed across the channels that will be loaded
    est_edge_bytes: int                # uint64 edge arrays built from them
    sample_rate_hz: int
    per_channel: Dict[int, int] = field(default_factory=dict)   # channel -> est transitions
    # Every priced channel is a v3/v4 block chain, so a window streams instead of
    # inflating the blob (see window_peak_bytes).
    streamable: bool = True

    @property
    def est_peak_bytes(self) -> int:
        """Rough peak RSS for a whole-file open: the inflated blobs and the edge
        arrays are both live at once (the blob is only released after the last
        channel is decoded), plus the decoder's own working set.

        THE EDGE TERM CARRIES THE DECODE TRANSIENT (`_WHOLE_FILE_TRANSIENT`) and the
        blob term does not: the inflated blob is one flat allocation. A whole-channel
        decode validates the chain a block at a time and decodes each block straight into
        the one uint64 output (saleae_binary.parse_channel_v3), so what is live is the
        output plus ONE block's int64 run, and a block can be the whole channel. A
        windowed open is priced by :meth:`window_peak_bytes` instead.
        """
        return int(self.uncompressed_bytes + self.est_edge_bytes * _WHOLE_FILE_TRANSIENT)

    def window_peak_bytes(self, model: "SalWindowModel", window: tuple) -> int:
        """Rough peak for the window ``(start, end)`` samples: the streaming model of
        `model` (:meth:`SalWindowModel.peak_bytes`, priced by the blocks the window
        decodes, plus the whole read of any channel whose chain did not walk) when
        every channel is a v3/v4 blob, else the whole-file peak (a v0 blob is read
        whole whatever the window)."""
        if not self.streamable:
            return self.est_peak_bytes
        return model.peak_bytes(int(window[0]), int(window[1]))

    def summary(self) -> str:
        def gb(n: int) -> str:
            return f"{n / 1e9:.1f} GB" if n >= 1e9 else f"{n / 1e6:.0f} MB"
        return (f"{gb(self.file_bytes)} on disk -> {gb(self.uncompressed_bytes)} "
                f"uncompressed, ~{self.est_transitions:,} transitions "
                f"(~{gb(self.est_edge_bytes)} of edge arrays); "
                f"estimated peak ~{gb(self.est_peak_bytes)}")


# One transition costs 8 bytes in the uint64 edge array the decoder walks.
_EDGE_BYTES = 8
# A v3 transition is one base-128 varint: >= 1 byte, and on a fast bus the great
# majority are exactly 1 (small deltas). Counting payload bytes therefore gives an
# UPPER bound on the transition count, which is the safe direction for a guard.
_V3_BYTES_PER_TRANSITION = 1.0
# A v0 blob is a fixed-width table of f64 transition times.
_V0_BYTES_PER_TRANSITION = 8.0


def estimate_cost(path: str, channels: Optional[List[int]] = None) -> SalCost:
    """Predict the memory cost of opening `path` from its zip directory alone.

    `channels` limits the estimate to the device-channels that will actually be
    loaded (default: all digital channels). Never inflates or decodes a blob.
    """
    import os

    info = read_info(path)
    want = set(info.channels if channels is None else channels)
    uncompressed = 0
    per_channel: Dict[int, int] = {}
    streamable = True
    with _zip_errors(path), zipfile.ZipFile(path) as z:
        sizes = {i.filename: i.file_size for i in z.infolist()}
        for ch, name in info.digital_files.items():
            if ch not in want:
                continue
            blob = int(sizes.get(name, 0))
            uncompressed += blob
            # Probe just the header to tell v0 (f64 table) from v3 (varint stream).
            with z.open(name) as fh:
                head = fh.read(16)
            per_tr = _V3_BYTES_PER_TRANSITION
            version = (int.from_bytes(head[8:12], "little")
                       if len(head) >= 12 and head[:8] == saleae_binary.SALEAE_MAGIC else -1)
            if version == 0:
                per_tr = _V0_BYTES_PER_TRANSITION
            if version not in saleae_binary.V3_LIKE_VERSIONS:
                streamable = False
            per_channel[ch] = int(blob / per_tr) if per_tr else 0
    est_tr = sum(per_channel.values())
    return SalCost(file_bytes=int(os.path.getsize(path)),
                   uncompressed_bytes=uncompressed,
                   est_transitions=est_tr,
                   est_edge_bytes=est_tr * _EDGE_BYTES,
                   sample_rate_hz=info.sample_rate_hz,
                   per_channel=per_channel,
                   streamable=streamable)


def _avail_from_proc_meminfo() -> int:
    """`MemAvailable` from /proc/meminfo (Linux), or 0. The kernel's own estimate of what a
    new allocation can have without swapping."""
    try:
        with open("/proc/meminfo", encoding="ascii") as fh:
            for line in fh:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) * 1024        # reported in kB
    except Exception:
        pass
    return 0


def _avail_from_global_memory_status() -> int:
    """`ullAvailPhys` from GlobalMemoryStatusEx (Windows), or 0.

    Windows has no `sysconf`; this is the stdlib route there when psutil is absent (see
    available_memory_bytes).
    """
    if sys.platform != "win32":                     # mypy narrows on this, and so must we:
        return 0                                    # ctypes.windll does not exist elsewhere
    try:
        import ctypes
        from ctypes import wintypes

        class _MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [("dwLength", wintypes.DWORD),
                        ("dwMemoryLoad", wintypes.DWORD),
                        ("ullTotalPhys", ctypes.c_ulonglong),
                        ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong),
                        ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong),
                        ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

        stat = _MEMORYSTATUSEX()
        stat.dwLength = ctypes.sizeof(_MEMORYSTATUSEX)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
            return int(stat.ullAvailPhys)
    except Exception:
        pass
    return 0


def _avail_from_vm_stat() -> int:
    """Free + inactive + speculative pages from `vm_stat` (macOS), or 0.

    macOS publishes no `SC_AVPHYS_PAGES`, so this is the stdlib route. INACTIVE pages are
    counted because Darwin will hand them to a new allocation rather than swap.
    Excludes purgeable/compressed accounting — this is a floor, not a model.
    """
    try:
        import re
        import subprocess
        out = subprocess.run(["/usr/bin/vm_stat"], capture_output=True, text=True,
                             timeout=5).stdout
        m = re.search(r"page size of (\d+) bytes", out)
        page = int(m.group(1)) if m else 4096
        total = 0
        for key in ("Pages free", "Pages inactive", "Pages speculative"):
            m = re.search(rf"^{key}:\s+(\d+)\.", out, re.M)
            if m:
                total += int(m.group(1))
        return total * page
    except Exception:
        return 0


def available_memory_bytes() -> int:
    """Best-effort physical memory available for a load, or 0 if unknown.

    Prefers genuinely free memory over total RAM: a 24 GB box with 20 GB in use
    cannot absorb an 18 GB load.

    EVERY PLATFORM HAS A STDLIB ROUTE, not just psutil (a declared dependency and the
    accurate path): sysconf `SC_AVPHYS_PAGES` or /proc/meminfo on Linux,
    GlobalMemoryStatusEx on Windows (no `sysconf`), and vm_stat on macOS (no
    `SC_AVPHYS_PAGES`).
    """
    try:                                        # psutil is the accurate path
        import psutil
        return int(psutil.virtual_memory().available)
    except Exception:
        pass
    try:                                        # POSIX: free pages * page size (Linux only)
        import os
        if sys.platform != "win32":             # Windows has no sysconf
            names = os.sysconf_names
            if "SC_AVPHYS_PAGES" in names and "SC_PAGE_SIZE" in names:
                avail = os.sysconf("SC_AVPHYS_PAGES") * os.sysconf("SC_PAGE_SIZE")
                if avail > 0:
                    return int(avail)
    except Exception:
        pass
    for probe in (_avail_from_proc_meminfo,          # Linux, if sysconf was unavailable
                  _avail_from_global_memory_status,  # Windows
                  _avail_from_vm_stat):              # macOS
        got = probe()
        if got > 0:
            return int(got)
    return 0


def total_memory_bytes() -> int:
    """Total physical RAM, or 0 if unknown.

    A weaker figure than `available_memory_bytes` and used only when that one fails: it says
    nothing about what is already in use. It is readable from the stdlib on every
    platform. See memory_budget.
    """
    try:
        import os
        if sys.platform != "win32":                     # Linux and macOS both publish these
            names = os.sysconf_names
            if "SC_PHYS_PAGES" in names and "SC_PAGE_SIZE" in names:
                total = os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE")
                if total > 0:
                    return int(total)
    except Exception:
        pass
    if sys.platform != "win32":                         # see _avail_from_global_memory_status
        return 0
    try:                                                # Windows
        import ctypes
        from ctypes import wintypes

        class _MEMSTAT(ctypes.Structure):
            _fields_ = [("dwLength", wintypes.DWORD), ("dwMemoryLoad", wintypes.DWORD),
                        ("ullTotalPhys", ctypes.c_ulonglong),
                        ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong),
                        ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong),
                        ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

        stat = _MEMSTAT()
        stat.dwLength = ctypes.sizeof(_MEMSTAT)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
            return int(stat.ullTotalPhys)
    except Exception:
        pass
    return 0


# Fraction of TOTAL RAM allowed when total is known but free is not. Lower than
# _MEM_HEADROOM because it is a weaker figure: some unknown share of that total is already
# spoken for, and this has to be safe on a machine that is mostly full.
_UNKNOWN_TOTAL_FRACTION = 0.25
# The total this ASSUMES when even total RAM is unreadable — a deliberately small machine.
# Budget when NOTHING about memory can be read is then the same rule applied to that
# assumption, which is what keeps the tiers consistent: a weaker reading can never buy a
# BIGGER load. An explicit max_bytes (or max_bytes=0 to opt out) overrides it.
_ASSUMED_TOTAL_WHEN_UNKNOWN = 4 << 30
_UNKNOWN_BUDGET = int(_ASSUMED_TOTAL_WHEN_UNKNOWN * _UNKNOWN_TOTAL_FRACTION)
# Predicted peak past which the Open Capture dialog calls a load LARGE (large_load_bytes).
# It informs and does not refuse; memory_budget refuses, and a running decode is watched
# (decode_memory_floor).
_LARGE_LOAD = 8 << 30


def large_load_bytes() -> int:
    """The predicted peak above which an open is labelled large (and may be slow) rather
    than refused; memory_budget() is what refuses."""
    return _LARGE_LOAD

# How the last budget was derived, for callers that report a guard running in a degraded
# mode. Set by memory_budget.
_BUDGET_SOURCE = "not computed yet"


def budget_source() -> str:
    """A short phrase describing how the last `memory_budget()` figure was obtained.

    The UI uses it to tell the user when the memory guard is working from a weaker
    reading than free RAM.
    """
    return _BUDGET_SOURCE


# Warn ONCE per process, not per open: the guard is consulted on every capture open and on
# every window prompt.
_WARNED_DEGRADED = False


def _warn_degraded_budget(source: str) -> None:
    global _WARNED_DEGRADED
    if _WARNED_DEGRADED:
        return
    _WARNED_DEGRADED = True
    logger.warning(
        "large-capture memory guard is using %s. Free memory could not be read, so the "
        "guard is working from a weaker figure and may allow a load this machine cannot "
        "hold. Installing psutil (a declared dependency: pip install -r requirements.txt) "
        "restores the accurate reading.", source)


def memory_budget(max_bytes: Optional[int] = None) -> int:
    """The peak-memory budget a capture open is allowed, in bytes.

    ONE definition, shared by the loader's guard and the UI's window prompt.

    A positive `max_bytes` overrides the computed budget. `0`/`None` mean "no explicit
    cap", and this returns the computed budget for them — opting OUT of the guard
    entirely is the caller's job (load_capture short-circuits on `max_bytes == 0`
    before asking), because a budget function has no way to express "unlimited"
    without handing back a number that silently disables the check.

    IT IS A QUESTION OF CAPACITY ONLY. large_load_bytes() is a label the dialog shows,
    not a refusal (see _LARGE_LOAD).

    WHEN FREE MEMORY CANNOT BE READ IT DEGRADES IN STEPS, and says which one it took
    (`budget_source`). Free RAM, then a quarter of TOTAL RAM, then a small constant, each a
    weaker reading than the last, and each used only when the one before it cannot be read.
    They are not ordered by size: a quarter of total RAM can exceed 60% of what is free, and
    the constant exceeds a quarter of a 2 GB machine.
    """
    global _BUDGET_SOURCE
    if max_bytes:
        _BUDGET_SOURCE = "caller-specified max_bytes"
        return int(max_bytes)
    avail = available_memory_bytes()
    if avail > 0:
        _BUDGET_SOURCE = "free memory"
        return int(avail * _MEM_HEADROOM)
    total = total_memory_bytes()
    if total > 0:
        _BUDGET_SOURCE = (f"{_UNKNOWN_TOTAL_FRACTION:.0%} of total RAM "
                          "(free memory unreadable — is psutil installed?)")
        _warn_degraded_budget(_BUDGET_SOURCE)
        return int(total * _UNKNOWN_TOTAL_FRACTION)
    # Fail CLOSED when memory is unknown: a guard that silently disables itself is worse
    # than none, because the caller believes it is protected.
    _BUDGET_SOURCE = "a conservative fixed budget (no memory reading available at all)"
    _warn_degraded_budget(_BUDGET_SOURCE)
    return _UNKNOWN_BUDGET


# The free memory a running decode must leave: past this the watchdog stops it (Session).
# A decode's own growth (the audio it produces) cannot be predicted before it runs, so it is
# watched instead: 10% of total RAM and at least 1 GiB, the point where macOS starts
# compressing memory well before it swaps.
_DECODE_FLOOR_FRACTION = 0.10
_DECODE_FLOOR_MIN = 1 << 30


def decode_memory_floor() -> int:
    """Free memory, in bytes, a running decode is stopped to keep, or 0 if free memory cannot
    be read (then there is nothing to watch with)."""
    if available_memory_bytes() <= 0:
        return 0
    total = total_memory_bytes()
    return max(_DECODE_FLOOR_MIN, int(total * _DECODE_FLOOR_FRACTION)) if total > 0 \
        else _DECODE_FLOOR_MIN


# Never plan to consume ALL free memory: the decode, the audio store and Qt itself
# still need room, and an allocation that exactly fills RAM thrashes rather than fails.
_MEM_HEADROOM = 0.6
# Peak/final ratio for a streaming windowed decode: a channel's in-window int64 edges
# and their concatenation are live at once, while the channel decoded first is kept.
# The rebase to the window start is done in place. An upper bound, enforced by
# test_a_window_of_a_one_block_channel_peaks_within_the_guard.
_STREAM_TRANSIENT = 2.0
# Peak/final ratio for a whole-file decode: the output plus one block's int64 deltas, and
# one block can be the whole channel. See SalCost.est_peak_bytes.
_WHOLE_FILE_TRANSIENT = 2.0
# Delta-run bytes a streaming decode inflates and decodes at a time, whatever the
# block's size: a block can be the whole channel.
_STREAM_CHUNK = 1 << 18
# Peak working set of decoding one chunk, per byte of delta run in it: the int64 deltas,
# their positions and masks, and the over-long-code scan. An upper bound for each decoder
# (the NumPy one holds more int64 and boolean temporaries), enforced by
# test_a_window_of_a_one_block_channel_peaks_within_the_guard. A constant whatever the
# window.
_STREAM_DECODE_BYTES_PER_BYTE_NATIVE = 24
_STREAM_DECODE_BYTES_PER_BYTE_NUMPY = 100
# Fixed working set of a streaming decode whatever the window: the zip member's reader
# and inflater, one block header at a time and the small per-call arrays. An upper bound,
# enforced by test_a_window_of_a_one_block_channel_peaks_within_the_guard.
_STREAM_FIXED_BYTES = 1 << 20


def _stream_decode_bytes_per_byte() -> int:
    return (_STREAM_DECODE_BYTES_PER_BYTE_NATIVE if saleae_binary._native_v3 is not None
            else _STREAM_DECODE_BYTES_PER_BYTE_NUMPY)


def window_peak_bytes(edge_bytes: int, max_block_bytes: int = _STREAM_CHUNK) -> int:
    """Rough peak of a streaming windowed decode whose in-window edge arrays are at most
    `edge_bytes`: those edges times `_STREAM_TRANSIENT`, plus the decode of one chunk,
    the smaller of `_STREAM_CHUNK` and the largest block (`max_block_bytes`, its
    delta-run bytes), plus `_STREAM_FIXED_BYTES`. `edge_bytes` comes from
    :meth:`SalWindowModel.edge_bytes`."""
    chunk = min(max(0, int(max_block_bytes)), _STREAM_CHUNK)
    return (int(max(0, int(edge_bytes)) * _STREAM_TRANSIENT)
            + chunk * _stream_decode_bytes_per_byte() + _STREAM_FIXED_BYTES)


def whole_channel_peak_bytes(blob_bytes: int) -> int:
    """Rough peak of reading one v3 channel's blob whole: the inflated blob plus its
    edges, at most one per byte, times `_WHOLE_FILE_TRANSIENT`. The per-channel term
    of :attr:`SalCost.est_peak_bytes`."""
    blob = max(0, int(blob_bytes))
    return int(blob + int(blob / _V3_BYTES_PER_TRANSITION) * _EDGE_BYTES
               * _WHOLE_FILE_TRANSIENT)


# Buckets a channel's block index keeps: on reaching twice this, adjacent buckets are
# merged in pairs, so an index is under 2 x 4096 buckets of 16 bytes whatever the chain's
# length.
_INDEX_BUCKETS = 1 << 12


class _BlockIndex:
    """A channel's v3 block chain as consecutive buckets of whole blocks: bucket `i`
    covers samples ``[ends[i-1], ends[i]]``, bucket 0 from sample 0, and holds
    ``cum[i] - cum[i-1]`` delta-run bytes. Built one block header at a time
    (:meth:`add`); a bucket merges blocks, so the bytes it charges a window are never
    fewer than the blocks it covers."""

    __slots__ = ("ends", "cum", "_e", "_b", "_per", "_n", "_acc", "_end")

    def __init__(self) -> None:
        self._e: List[int] = []
        self._b: List[int] = []
        self._per = 1                     # blocks per bucket
        self._n = 0                       # blocks in the open bucket
        self._acc = 0
        self._end = 0
        self.ends = np.zeros(0, dtype=np.uint64)
        self.cum = np.zeros(0, dtype=np.int64)

    def add(self, b_end: int, nbytes: int) -> None:
        self._acc += int(nbytes)
        self._end = int(b_end)
        self._n += 1
        if self._n == self._per:
            self._close()

    def _close(self) -> None:
        self._e.append(self._end)
        self._b.append(self._acc)
        self._n = self._acc = 0
        if len(self._e) >= 2 * _INDEX_BUCKETS:
            self._e = self._e[1::2]
            self._b = [x + y for x, y in zip(self._b[0::2], self._b[1::2])]
            self._per *= 2

    def finish(self) -> "_BlockIndex":
        if self._n:
            self._close()
        self.ends = np.asarray(self._e, dtype=np.uint64)
        self.cum = np.cumsum(np.asarray(self._b, dtype=np.int64))
        self._e, self._b = [], []
        return self

    def bytes_overlapping(self, s0: int, s1: int) -> int:
        """Delta-run bytes of the buckets a window decode reads for ``[s0, s1)``: every
        one ending after `s0` and starting before `s1`, as
        :func:`_decode_v3_window_streaming` skips and stops."""
        if self.ends.size == 0 or s1 <= s0:
            return 0
        first = int(np.searchsorted(self.ends, np.uint64(max(0, s0)), side="right"))
        # Bucket j starts at ends[j-1] (bucket 0 at 0), so buckets [0, stop) start
        # before s1.
        stop = 1 + int(np.searchsorted(self.ends[:-1], np.uint64(s1), side="left"))
        if stop <= first:
            return 0
        return int(self.cum[stop - 1]) - (int(self.cum[first - 1]) if first else 0)


@dataclass
class SalWindowModel:
    """What a windowed open of a .sal costs, from one header walk of each channel:
    the capture's length in samples, the largest block's delta-run bytes, and each
    channel's :class:`_BlockIndex`.

    A window is priced by the blocks it decodes, not by its share of the capture: a
    v3 transition is at least one byte of delta run and at least one sample, so a
    channel holds at most ``min(bytes of the blocks overlapping the window, samples in
    the window)`` transitions in it. Shared by the loader's guard and the Open Capture
    probe.

    A channel whose blob does not walk as a block chain (damaged, not v3, or empty) is
    read whole by a windowed load: `whole_channels` maps it to its blob's bytes, and
    :meth:`peak_bytes` charges it :func:`whole_channel_peak_bytes` whatever the window."""
    span_samples: int = 0
    max_block_bytes: int = 0
    channels: Dict[int, _BlockIndex] = field(default_factory=dict)
    whole_channels: Dict[int, int] = field(default_factory=dict)
    block_bytes: Dict[int, int] = field(default_factory=dict)   # each streamed channel's largest

    def edge_bytes(self, s0: int, s1: int) -> int:
        """Upper bound on the in-window edge arrays of ``[s0, s1)``, summed over the
        channels."""
        s0, s1 = int(s0), int(s1)
        total = 0
        for idx in self.channels.values():
            end = int(idx.ends[-1]) if idx.ends.size else 0
            samples = max(0, min(s1, end) - s0)
            total += min(idx.bytes_overlapping(s0, s1), samples)
        return total * _EDGE_BYTES

    def of(self, channels: Sequence[int]) -> "SalWindowModel":
        """The model of `channels` alone (the two a Link loads), as window_model of them
        gives it."""
        keep = set(channels)
        idx = {ch: i for ch, i in self.channels.items() if ch in keep}
        big = {ch: b for ch, b in self.block_bytes.items() if ch in keep}
        return SalWindowModel(span_samples=self.span_samples,
                              max_block_bytes=max(big.values(), default=0),
                              channels=idx,
                              whole_channels={ch: b for ch, b in self.whole_channels.items()
                                              if ch in keep},
                              block_bytes=big)

    def peak_bytes(self, s0: int, s1: int) -> int:
        """Rough peak of a windowed decode of ``[s0, s1)``: the streamed channels'
        :func:`window_peak_bytes` plus :func:`whole_channel_peak_bytes` of each channel
        in `whole_channels`."""
        return (window_peak_bytes(self.edge_bytes(s0, s1), self.max_block_bytes)
                + sum(whole_channel_peak_bytes(b) for b in self.whole_channels.values()))


# What reading an inflating zip member raises when its stream is damaged.
_STREAM_ERRORS = (zipfile.BadZipFile, zlib.error, EOFError, OSError)


def _stream_reason(exc: BaseException) -> str:
    """The import-note wording for a zip member read that failed with `exc`."""
    return f"the archive member cannot be read ({type(exc).__name__}: {exc})"


def _read_head(fh, n: int, step: int = 1 << 12):
    """``(bytes, exc)``: up to `n` bytes from the start of `fh`, read `step` bytes at a
    time, and the stream error that ended the read early (None when it did not)."""
    parts: List[bytes] = []
    got = 0
    try:
        while got < n:
            chunk = fh.read(min(step, n - got))
            if not chunk:
                break
            parts.append(chunk)
            got += len(chunk)
    except _STREAM_ERRORS as exc:
        return b"".join(parts), exc
    return b"".join(parts), None


def _v3_span_streaming(fh, size: int, want_start: bool = False,
                       want_max_block: bool = False, want_index: bool = False,
                       unreadable: Optional[list] = None):
    """Last ``B_end`` of a v3 block chain, read by streaming the headers only.

    Walks the chain from the start of the stream, seeking past each block's delta
    payload instead of decoding (or even holding) it, so peak memory is one block
    header and one candidate search step regardless of blob size, so it can measure a
    file too big to open.

    Returns 0 if no self-consistent chain tiles the stream exactly. The candidate
    first-block offsets are the zero qwords inside the first
    `saleae_binary.V3_CHAIN_START_SCAN` bytes, then those in the rest of the stream,
    read `saleae_binary.V3_CANDIDATE_STEP` bytes at a time, and a candidate is accepted
    by the same header checks as :func:`saleae_binary._find_v3_chain_start`, so the two
    accept the same chains. With
    ``want_start`` returns ``(chain_end, start_offset)`` instead — a caller that
    then decodes needs the VALIDATED offset, because a real Logic 2 metadata header
    contains several zero qwords that each look like a candidate first block and
    only the full walk tells them apart. With ``want_max_block`` as well, returns
    ``(chain_end, start_offset, largest block's delta-run bytes)``; with
    ``want_index``, ``(chain_end, start_offset, largest block's delta-run bytes,
    _BlockIndex)``.

    `unreadable`, when a list, lets a stream that cannot be read to its end (a deflate
    or CRC failure) end the chain rather than the walk. When no candidate tiles the
    stream and the stream fails to read to its end, the candidate that walked the most
    block headers before its chain broke is the chain: its end is its last header's
    ``B_end``, and ``(readable_end, reason)`` is appended to `unreadable`, where
    `readable_end` is that last block's ``A_start`` (the blocks ending by then were read
    and passed the header checks). A chain that breaks in a stream that reads whole is
    no chain. Without `unreadable` a stream error propagates.
    """
    fail = ((0, -1, 0, _BlockIndex().finish()) if want_index
            else (0, -1, 0) if want_max_block else ((0, -1) if want_start else 0))
    if unreadable is None:
        head, head_exc = fh.read(saleae_binary.V3_CHAIN_START_SCAN), None
    else:
        head, head_exc = _read_head(fh, saleae_binary.V3_CHAIN_START_SCAN)
    if len(head) < 16 or head[:8] != saleae_binary.SALEAE_MAGIC:
        return fail
    if int.from_bytes(head[8:12], "little") not in saleae_binary.V3_LIKE_VERSIONS:
        return fail
    hdr_len = saleae_binary._V3_BLOCK_HEADER
    bc_off = saleae_binary._V3_BYTECOUNT_OFF

    def result(o0, chain_end, big, index):
        if index is not None:
            return int(chain_end), o0, int(big), index.finish()
        if want_max_block:
            return int(chain_end), o0, int(big)
        return (int(chain_end), o0) if want_start else int(chain_end)

    # With `unreadable`: the walk that read the most blocks before its chain broke, as
    # (blocks, readable_end, start, last B_end, largest run, index).
    best: list = []

    def broke(o0, n, prev_a, prev_b, big, index):
        if unreadable is not None and n and (not best or n > best[0]):
            best[:] = [n, int(prev_a), o0, prev_b, big, index]

    def walk(o0, buf, base):
        """The result for a chain starting at `o0`, or None; `buf` holds the stream's
        bytes from `base`, and a header outside it is read from the stream."""
        pos, prev_a, prev_b, big, n = o0, 0, None, 0, 0
        index = _BlockIndex() if want_index else None
        while pos + hdr_len <= size:
            r = pos - base
            if r + hdr_len > len(buf):                          # need more bytes
                try:
                    fh.seek(pos)
                    chunk = fh.read(hdr_len)
                except _STREAM_ERRORS:
                    if unreadable is None:
                        raise
                    broke(o0, n, prev_a, prev_b, big, index)
                    return None
                if len(chunk) < hdr_len:
                    broke(o0, n, prev_a, prev_b, big, index)
                    return None
                r, hdr = 0, chunk
            else:
                hdr = buf
            a = int.from_bytes(hdr[r:r + 8], "little")
            b = int.from_bytes(hdr[r + 8:r + 16], "little")
            cnt = int.from_bytes(hdr[r + bc_off:r + bc_off + 8], "little")
            body = pos + hdr_len
            if b < a or body + cnt > size or (prev_b is None and a != 0) or \
               (prev_b is not None and a != prev_b):
                broke(o0, n, prev_a, prev_b, big, index)
                return None
            if prev_b is None and b == a and body + cnt != size:
                return None
            prev_a, prev_b, pos, big, n = a, b, body + cnt, max(big, cnt), n + 1
            if index is not None:
                index.add(b, cnt)
        if pos != size or prev_b is None:
            broke(o0, n, prev_a, prev_b, big, index)
            return None
        return result(o0, prev_b, big, index)

    def damaged():
        """The longest broken walk when the stream cannot be read to its end, else
        `fail`: a chain that breaks in a stream that reads whole is malformed."""
        exc = head_exc
        if exc is None and best:
            try:
                fh.seek(0)
                while fh.read(1 << 20):
                    pass
            except _STREAM_ERRORS as e:
                exc = e
        if exc is None or not best:
            if exc is not None and unreadable is None:
                raise exc
            return fail
        assert unreadable is not None
        _n, readable_end, o0, chain_end, big, index = best
        unreadable.append((readable_end, _stream_reason(exc)))
        return result(o0, chain_end, big, index)

    split = len(head) - hdr_len + 1
    if head_exc is not None:
        fh.seek(0)                          # a header past `head` is read afresh
    for o0 in saleae_binary.v3_zero_qwords(head, 16, split):
        got = walk(o0, head, 0)
        if got is not None:
            return got
    if head_exc is not None:
        return damaged()
    step = saleae_binary.V3_CANDIDATE_STEP
    pos = max(16, split)
    while pos + hdr_len <= size:                     # past the prefix, a step at a time
        try:
            fh.seek(pos)
            chunk = fh.read(step + hdr_len - 1)
        except _STREAM_ERRORS:
            if unreadable is None:
                raise
            return damaged()
        if len(chunk) < hdr_len:
            break
        for o in saleae_binary.v3_zero_qwords(chunk, 0, min(step, len(chunk) - hdr_len + 1)):
            got = walk(pos + o, chunk, pos)
            if got is not None:
                return got
        pos += step
    return damaged() if unreadable is not None else fail

def _v3_chains(path: str, info: "SalInfo", channels: Sequence[int]) -> Dict[int, tuple]:
    """``{channel: (chain_end, start_offset, largest block's run bytes, _BlockIndex,
    blob bytes, unreadable)}`` from one header walk of each channel's blob
    (:func:`_v3_span_streaming`); ``(0, -1, 0, empty index, blob bytes, None)`` for a
    blob that is not a v3 chain or cannot be read, with blob bytes 0 when its size cannot
    be read either. `unreadable` is None, or ``(readable_end, reason)`` for a member whose
    stream cannot be read past sample `readable_end`; its chain ends at the last block
    header read. A windowed load walks each channel once and hands the result to both
    the guard and the decode."""
    out: Dict[int, tuple] = {}
    with zipfile.ZipFile(path) as z:
        for ch in channels:
            size = 0
            try:
                name = info.digital_files[ch]
                size = int(z.getinfo(name).file_size)
                cut: List[tuple] = []
                with z.open(name) as fh:
                    walked = tuple(_v3_span_streaming(fh, size, want_start=True,
                                                      want_index=True, unreadable=cut))
                out[ch] = walked + (size, cut[0] if cut else None)
            except Exception:
                out[ch] = (0, -1, 0, _BlockIndex().finish(), size, None)
    return out


def _streams(chain: tuple) -> bool:
    """True when :func:`_decode_v3_window_streaming` decodes the channel from its
    walked `chain`; otherwise a windowed load reads its blob whole."""
    return chain[0] > 0 and chain[1] >= 0


def _model_from_chains(chains: Dict[int, tuple]) -> SalWindowModel:
    walked = {ch: c for ch, c in chains.items() if _streams(c)}
    return SalWindowModel(span_samples=max((c[0] for c in chains.values()), default=0),
                          max_block_bytes=max((c[2] for c in walked.values()), default=0),
                          channels={ch: c[3] for ch, c in walked.items()},
                          whole_channels={ch: int(c[4]) for ch, c in chains.items()
                                          if not _streams(c)},
                          block_bytes={ch: int(c[2]) for ch, c in walked.items()})


def window_model(path: str, channels: Optional[Sequence[int]] = None) -> SalWindowModel:
    """The :class:`SalWindowModel` of `channels` (default: all digital channels), from
    one header walk of each. Its `span_samples` is 0 when no channel is a v3 chain."""
    info = read_info(path)
    return _model_from_chains(
        _v3_chains(path, info, list(info.channels if channels is None else channels)))


def _dig(meta: dict, *path, default=None):
    cur = meta
    for k in path:
        if isinstance(cur, dict) and k in cur:
            cur = cur[k]
        else:
            return default
    return cur


# The largest meta.json read: a project's is a few hundred KB, and reading it is
# bounded before anything is priced.
_META_MAX_BYTES = 8 << 20


def _whole_number(value) -> Optional[int]:
    """`value` as an int when it is a whole number (an int of any size, an integral float,
    or a string of either), else None. A bool, NaN, infinity and a fraction are not."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)                      # exact, whatever the size
        except ValueError:
            pass
    try:
        number = float(value) if isinstance(value, (float, str)) else None
    except (ValueError, OverflowError):
        return None
    if number is None or number != number or number in (float("inf"), float("-inf")):
        return None
    return int(number) if number == int(number) else None


@contextlib.contextmanager
def _zip_errors(path: str, what: str = "a .sal capture"):
    """Raise ValueError naming `path` for what a damaged zip, blob or meta.json raises
    (a bad archive, a corrupt deflate stream, a missing entry, an unreadable or too deeply
    nested meta.json, a read error), so the caller sees the file and not the codec. A
    path that is missing or cannot be opened (FileNotFoundError, PermissionError,
    IsADirectoryError) and a ValueError pass through."""
    try:
        yield
    except (FileNotFoundError, PermissionError, IsADirectoryError):
        raise
    except (zipfile.BadZipFile, zlib.error, OSError, EOFError, KeyError, RuntimeError,
            NotImplementedError, OverflowError) as e:     # RuntimeError: encrypted, too deep
        reason = e.args[0] if isinstance(e, KeyError) and e.args else e
        raise ValueError(f"{path}: cannot read as {what} "
                         f"({type(e).__name__}: {reason})") from None


def read_info(path: str) -> SalInfo:
    """The sample rate, digital channels and import notes of a .sal's meta.json. A file
    that is not a readable zip with a JSON meta.json raises ValueError naming it."""
    with _zip_errors(path):
        with zipfile.ZipFile(path) as z:
            with z.open("meta.json") as fh:
                raw = fh.read(_META_MAX_BYTES + 1)
            names = set(z.namelist())
        if len(raw) > _META_MAX_BYTES:
            raise ValueError(f"{path}: meta.json is larger than {_META_MAX_BYTES:,} bytes")
        try:
            meta = json.loads(raw)
        except ValueError as e:                    # not JSON, not UTF-8, a huge integer
            raise ValueError(f"{path}: meta.json is not readable JSON ({e})") from None
        del raw
    notes: List[str] = []
    rate_raw = _dig(meta, "data", "legacySettings", "sampleRate", "digital", default=0)
    sample_rate = _whole_number(rate_raw or 0)
    if sample_rate is None or not 0 <= sample_rate <= MAX_RATE_HZ:
        notes.append(f"meta.json sampleRate {rate_raw!r:.60} is not a whole number of hertz "
                     f"from 1 to {MAX_RATE_HZ:,.0f}; the file gives no sample rate")
        sample_rate = 0
    # binData[i] maps a per-channel file to its device channel & type. It mixes
    # Digital and Analog entries (and the file name isn't positional), so key off
    # entry["type"]=="Digital" and entry["file"].
    digital_files: Dict[int, str] = {}
    bin_data = _dig(meta, "binData", default=[]) or []
    named = False                          # a note says the file names give the channels
    if not isinstance(bin_data, list):
        notes.append("meta.json binData is not a list; the digital-N.bin file names "
                     "are used")
        bin_data, named = [], True
    unusable: List[str] = []
    repeated: List[str] = []
    for entry in bin_data:
        if not isinstance(entry, dict) or entry.get("type") != "Digital":
            continue
        name = str(entry.get("file", "")).lstrip("./")
        channel = _whole_number(entry.get("deviceChannel"))
        if name in names and channel is not None:
            if channel in digital_files:
                repeated.append(f"{name} (channel {channel})")
            else:
                digital_files[channel] = name
        else:
            unusable.append(name or "(no file)")
    if repeated:
        n = len(repeated)
        notes.append(f"{n} digital channel entr{'ies' if n != 1 else 'y'} in meta.json "
                     f"repeat{'' if n != 1 else 's'} a device channel already listed and "
                     f"{'are' if n != 1 else 'is'} skipped: "
                     f"{', '.join(repeated[:4])}{' …' if n > 4 else ''}")
    if unusable:
        n = len(unusable)
        notes.append(f"{n} digital channel entr{'ies' if n != 1 else 'y'} in meta.json "
                     f"name{'' if n != 1 else 's'} no file in the archive or no device "
                     f"channel and {'are' if n != 1 else 'is'} skipped: "
                     f"{', '.join(unusable[:4])}{' …' if n > 4 else ''}")
    # Fall back to positional digital-N.bin if binData is absent/uninformative, and say so.
    if not digital_files:
        i = 0
        while f"digital-{i}.bin" in names:
            digital_files[i] = f"digital-{i}.bin"
            i += 1
        if digital_files and not named:
            notes.append("meta.json binData lists no usable digital channel; the channel "
                         "numbers are taken from the digital-N.bin file names")
    channels = sorted(digital_files)
    return SalInfo(sample_rate_hz=sample_rate, digital_files=digital_files, channels=channels,
                   notes=notes)


# Offset of the first transition block in a MINIMAL v3 blob: `<SALEAE>` + version + type
# and nothing else (saleae_binary.build_channel_v3). Every Logic-shaped writer puts a
# metadata header before the chain, so a first-block offset past this one is the signal
# that the blob's per-block `level` fields were actually filled in — 51 bytes for
# build_logic2_channel_v3. See _decode_v3_window_streaming.
_V3_MINIMAL_FIRST_BLOCK = len(saleae_binary.SALEAE_MAGIC) + 8


class _UnstampedBlockLevels(Exception):
    """A v3 blob whose per-block `level` fields can't be trusted, so a window's opening
    line level has to come from the decoded parity of every preceding block instead.

    Raised by :func:`_decode_v3_window_streaming` under ``trust_levels=True`` when the
    skipped blocks show no evidence of being stamped. The caller retries the same window
    with ``trust_levels=False``. See that function for the fast path."""


def _decode_v3_window_streaming(fh, size: int, s0: int, s1: int, label: str,
                                trust_levels: bool = True, chain=None,
                                bad_after: Optional[list] = None):
    """Decode a v3 window straight from the zip stream, never holding the blob.

    Unlike :func:`saleae_binary.parse_channel_v3_window`, which needs the whole
    inflated blob in memory, the entry is inflated sequentially and each block is either
    **skipped** as it streams past or decoded when it overlaps the window, a
    `_STREAM_CHUNK` of its delta run at a time. Peak memory is therefore one chunk's
    decode plus the window's own edges, whatever the block size. Inflating is
    sequential, so the time taken grows with the window's END, not its length, and with
    the whole blob when `bad_after` is given.

    A skipped block contributes only the LINE LEVEL at the window's start. With
    ``trust_levels`` that bit comes from the block headers: each header carries `level`,
    the line state at that block's START, and the block containing s0 is decoded anyway
    because it overlaps the window, so its own transitions give the level at s0 and no
    earlier block is decoded. Cost then scales with the window, not its start offset.
    A skipped block's transitions are counted by decoding its run
    (:func:`_count_and_discard`).

    ``trust_levels=False`` derives the level from the decoded parity of every preceding
    block, O(start offset). Levels are trusted only when a METADATA HEADER precedes the
    chain: :func:`saleae_binary.build_channel_v3` — the minimal internal test writer —
    stamps 0 for every non-first block and puts its first block at offset
    ``_V3_MINIMAL_FIRST_BLOCK`` (magic + version + type, nothing else), while every
    Logic-shaped blob carries the header Logic needs first — 51 bytes for
    :func:`saleae_binary.build_logic2_channel_v3`. The level VALUES cannot be the
    signal: with an even block size and a low initial state every block legitimately
    starts at 0. An unrecognised blob raises :class:`_UnstampedBlockLevels` and the
    caller retries by parity.

    Every block that is decoded is checked as the whole-file reader checks it (no delta
    code too long for int64, deltas summing to the header's span) and raises
    :class:`saleae_binary.MalformedSaleaeV3` if it fails. A block that overlaps the
    window is decoded to its end, past the window's end too. A block skipped on the
    strength of its header is not decoded, so it is not checked.

    `bad_after`, when a list, has the blocks after the window checked the same way, one
    at a time (:func:`_check_blocks_after`): each that fails appends
    ``(A_start, B_end, reason)`` to it rather than raising. Without it, decoding stops
    at the first block past the window.

    `chain` is ``(chain_end, start_offset)`` from :func:`_v3_span_streaming` when the
    caller has walked the chain already; otherwise this walks it, then seeks back to 0.

    Returns ``(initial_state_at_s0, transition_samples)`` on the ORIGINAL timeline,
    matching parse_channel_v3_window; ``None`` if the blob isn't a v3 chain (the
    caller then falls back to the in-memory path).
    """
    hdr_len = saleae_binary._V3_BLOCK_HEADER
    bc_off = saleae_binary._V3_BYTECOUNT_OFF
    lvl_off = saleae_binary._V3_LEVEL_OFF
    # Take the VALIDATED chain start from the header walk: a real Logic 2 metadata
    # header holds several zero qwords that look like a first block.
    if chain is None:
        chain = _v3_span_streaming(fh, size, want_start=True)
        fh.seek(0)
    chain_end, start = chain[0], chain[1]
    if chain_end <= 0 or start < 0:
        return None
    # A metadata header before the chain means a Logic-shaped writer, which stamps every
    # block's level. Nothing else here may assume the levels are real.
    stamped = start > _V3_MINIMAL_FIRST_BLOCK
    limit = min(int(s1), chain_end)
    _skip(fh, start)                                  # discard the metadata header
    pos, parity = start, 0
    kept: List[np.ndarray] = []           # in-window positions, int64, per decoded chunk
    initial = None          # line level at capture start, from the first block's header
    level = None            # line level AT s0, from the header of the block containing it
    skipped = 0             # blocks that ended at or before s0
    after = None            # (A_start, B_end, byte count) of a header read past the window
    prev_b = 0              # B_end of the last block read
    while pos + hdr_len <= size:
        try:
            h = fh.read(hdr_len)
        except _STREAM_ERRORS as exc:
            # The previous block's run was read whole: a header past the window that
            # cannot be read is reported with the bad blocks, one inside it is refused.
            if bad_after is None or prev_b < limit:
                raise
            bad_after.append((prev_b, prev_b, _stream_reason(exc)))
            pos = size
            break
        if len(h) < hdr_len:
            break
        a = int.from_bytes(h[0:8], "little")
        b = int.from_bytes(h[8:16], "little")
        lvl = int.from_bytes(h[lvl_off:lvl_off + 2], "little") & 1
        cnt = int.from_bytes(h[bc_off:bc_off + 8], "little")
        if initial is None:
            initial = lvl
        body = pos + hdr_len
        if b < a or body + cnt > size:
            pos = size                                # nothing after it is a block
            break
        if b <= s0:
            skipped += 1
            if trust_levels and stamped:
                _skip(fh, cnt)                        # no decode: the level is in a header
            else:
                parity ^= _count_and_discard(fh, cnt, drop_last=(b >= chain_end),
                                             span=(a, b), label=label) & 1
        elif a >= limit:
            after = (a, b, cnt)                       # the first block past the window
            break
        else:
            crossed = _decode_overlap(fh, cnt, a, b, s0, limit, label, kept)
            parity ^= crossed
            if level is None and trust_levels and stamped:
                # This block spans s0, so its own header level plus the transitions it
                # holds before s0 give the level at s0 -- no earlier block involved.
                level = lvl ^ crossed
        pos, prev_b = body + cnt, b
    if trust_levels and not stamped and skipped:
        # No metadata header, so the per-block levels may be zeros the writer never filled
        # in. Guessing would invert the line level silently; hand it to the parity walk.
        raise _UnstampedBlockLevels(label)
    if level is None:
        # Either the parity walk ran (its accumulated bit IS the answer), or no block
        # overlapped the window at all -- it sits past the last transition. In the latter
        # case the fast path has no bit to offer, so ask for the walk.
        if trust_levels and stamped and skipped:
            raise _UnstampedBlockLevels(label)
        level = (initial or 0) ^ parity
    if bad_after is not None and pos < size:
        _check_blocks_after(fh, size, pos, after, label, bad_after, prev_b)
    samples = (np.concatenate(kept) if len(kept) > 1
               else (kept[0] if kept else np.zeros(0, dtype=np.int64)))
    kept.clear()
    # Every kept position is > s0 >= 0, so the int64 bits ARE the uint64 value: a view,
    # not a copy.
    return bool(level), np.ascontiguousarray(samples).view(np.uint64)


def _skip(fh, n: int, chunk: int = 1 << 20) -> None:
    """Consume `n` bytes from a (possibly non-seekable) stream without keeping them."""
    while n > 0:
        got = fh.read(min(n, chunk))
        if not got:
            return
        n -= len(got)


def _stream_deltas(fh, nbytes: int, label: str):
    """Yield the next `nbytes` of delta run as ``(int64 deltas, last)`` pairs, one per
    `_STREAM_CHUNK` read, so the working set is one chunk whatever the block's size;
    `last` is True on the pair that ends the run.

    Each chunk is cut at a code boundary and the rest carried into the next. With the
    native decoder the cut follows the chunk's last byte below 0x40, which always ends
    a code (a single-byte code or a terminal digit); a chunk with no such byte, and
    every chunk without the native decoder, goes through
    :func:`saleae_binary._v3_decode_block_np`, which finds the last complete code
    itself. A chunk holding a code too long for int64 raises
    :class:`saleae_binary.MalformedSaleaeV3`, as does a stream that ends before
    `nbytes`. The carry holds the start of an unfinished code, so a run of interior
    bytes that crosses a chunk boundary is seen whole.
    """
    native = saleae_binary._native_v3
    left = int(nbytes)
    carry = b""
    while left > 0 or carry:
        take = min(left, _STREAM_CHUNK)
        got = fh.read(take) if take else b""
        if len(got) < take:
            raise saleae_binary.MalformedSaleaeV3(label, "the delta run ends early")
        left -= take
        chunk = carry + got if carry else got
        bb = np.frombuffer(chunk, np.uint8)
        if saleae_binary._v3_has_overlong_code(bb):
            raise saleae_binary.MalformedSaleaeV3(
                label, "a delta code too long for int64")
        used = bb.size
        if native is not None and left:
            ends = bb < 0x40
            used = bb.size - int(np.argmax(ends[::-1])) if ends.any() else 0
            del ends
        if native is not None and used:
            deltas = native(bb, 0, used)
        else:
            deltas, used = saleae_binary._v3_decode_block_np(bb, left == 0)
        carry = chunk[used:]
        if deltas.size:
            yield deltas, (left == 0 and not carry)


def _decode_overlap(fh, cnt: int, a: int, b: int, s0: int, limit: int, label: str,
                    kept: list) -> int:
    """Decode a block that overlaps the window to its end, appending its in-window
    positions (int64, strictly between s0 and `limit`) to `kept`.

    Returns the parity of its transitions at or before s0. The block must sum to its
    header's span, and positions never pass `b`; either failure raises
    :class:`saleae_binary.MalformedSaleaeV3`. The block's last delta may be the gap to
    capture end (the chain's phantom edge at `b`), which `limit` excludes."""
    base = int(a)
    crossed = 0
    for deltas, _last in _stream_deltas(fh, cnt, label):
        p = np.cumsum(deltas)
        del deltas
        p += base
        base = int(p[-1])
        if base > b:
            raise saleae_binary.MalformedSaleaeV3(
                label, f"a block decodes past its header's end sample {b}")
        crossed ^= int(np.count_nonzero(p <= s0)) & 1
        if int(p[0]) < limit:
            sel = p[(p > s0) & (p < limit)]
            if sel.size:
                kept.append(sel)
        del p
    if base != b:
        raise saleae_binary.MalformedSaleaeV3(
            label, f"a block decodes to {base - a} samples, its header says {b - a}")
    return crossed


class _CountingReader:
    """`fh` with `count`, the bytes read through it since `count` was last set."""

    __slots__ = ("_fh", "count")

    def __init__(self, fh) -> None:
        self._fh = fh
        self.count = 0

    def read(self, n: int) -> bytes:
        got = self._fh.read(n)
        self.count += len(got)
        return got


def _check_blocks_after(fh, size: int, pos: int, first, label: str, bad: list,
                        prev_b: int = 0) -> None:
    """Check each block from offset `pos` to the end of the blob as the whole-file reader
    does, one block's run at a time, a `_STREAM_CHUNK` of it in memory
    (:func:`_count_and_discard`). `first` is ``(A_start, B_end, byte count)`` when the
    header at `pos` has already been read, else None; `prev_b` is the ``B_end`` of the
    block before `pos`. A block that fails appends ``(A_start, B_end, reason)`` to `bad`
    and the check goes on with the next block; a header whose run passes the blob's end
    ends it, and so does a stream that cannot be read further, appending the block it
    was reading."""
    hdr_len = saleae_binary._V3_BLOCK_HEADER
    bc_off = saleae_binary._V3_BYTECOUNT_OFF
    reader = _CountingReader(fh)
    a = b = prev_b
    try:
        while True:
            if first is None:
                if pos + hdr_len > size:
                    return
                a = b = prev_b
                h = fh.read(hdr_len)
                if len(h) < hdr_len:
                    return
                a = int.from_bytes(h[0:8], "little")
                b = int.from_bytes(h[8:16], "little")
                cnt = int.from_bytes(h[bc_off:bc_off + 8], "little")
            else:
                (a, b, cnt), first = first, None
            body = pos + hdr_len
            if b < a or body + cnt > size:
                return
            reader.count = 0
            try:
                _count_and_discard(reader, cnt, drop_last=False, span=(a, b), label=label)
            except saleae_binary.MalformedSaleaeV3 as exc:
                bad.append((a, b, exc.detail))
                _skip(fh, cnt - reader.count)
            pos, prev_b = body + cnt, b
    except _STREAM_ERRORS as exc:
        bad.append((a, b, _stream_reason(exc)))


def _count_and_discard(fh, nbytes: int, drop_last: bool, span=None, label: str = "") -> int:
    """Number of transitions in the next `nbytes` of delta run, discarding the bytes.

    A skipped block contributes only its level PARITY, so only the delta count is
    needed. It is the decoded count, a `_STREAM_CHUNK` at a time (`_stream_deltas`): a
    code's last byte is not the only byte below 0x80 (encode_v3_delta(65) ==
    b"\\x40\\x40").

    `drop_last` excludes the chain's trailing gap-to-capture-end delta, which is not a
    real transition. `span` is the block header's ``(A_start, B_end)``: when given, the
    deltas must sum to ``B_end - A_start`` or :class:`saleae_binary.MalformedSaleaeV3`
    is raised."""
    n = 0
    total = 0
    for deltas, _last in _stream_deltas(fh, nbytes, label):
        n += int(deltas.size)
        total += int(deltas.sum())
    if span is not None and total != int(span[1]) - int(span[0]):
        raise saleae_binary.MalformedSaleaeV3(
            label, f"a block decodes to {total} samples, its header says "
                   f"{int(span[1]) - int(span[0])}")
    return n - 1 if (drop_last and n) else n


def _decode_channel(data: bytes, label: str, window=None, src: str = ""):
    """Decode one digital-N blob to (initial_state, times_s, samples, begin_time).

    For version 0 `times_s` is the transition times in seconds and `begin_time` is
    the recorded capture-start time (`samples` is None); for version 3 `samples` is
    the transition sample numbers, already measured from capture start (`times_s` is
    None, `begin_time` 0.0). The v0 seconds→samples conversion is deferred to
    load_capture so v0 and v3 channels can share ONE capture-start origin: a v3
    channel's sample 0 IS the v0 begin_time, and pre-trigger begin_times are negative
    and would wrap through the uint64 cast. Raises UnsupportedSaleaeVersion for any
    other version. With `src`, a v0 blob holding bytes past the transitions its header
    counts is an import warning of `src`, as for a .bin pair.

    `window` is an optional (start_sample, end_sample) pair. On v3 it is pushed down
    into the block-chain walk so only overlapping blocks are decoded (see
    saleae_binary.parse_channel_v3_window). v0 is a flat table with no block
    index, so it decodes fully and slices after; that path
    is bounded by the f64 table itself, which is already in memory."""
    version = saleae_binary.peek_version(data, label)
    if version == 0:
        ch = saleae_binary.parse_channel(data, label)
        note = saleae_binary.trailing_bytes_note(len(data), ch.times_s.size)
        if note and src:
            import_notes.warn(src, f"{label}: {note}")
        times = np.asarray(ch.times_s, dtype=np.float64)
        return ch.initial_state, times, None, float(ch.begin_time)
    if version in saleae_binary.V3_LIKE_VERSIONS:
        if window is not None:
            samples = saleae_binary.parse_channel_v3_window(data, window[0], window[1], label)
        else:
            samples = saleae_binary.parse_channel_v3(data, label)
        return samples.initial_state, None, samples.transition_samples, 0.0
    raise saleae_binary.UnsupportedSaleaeVersion(version, label)


_EDGE_CHUNK = 1 << 23      # v0 seconds->samples conversion block (~8M edges -> 64 MiB float64)


def _times_to_edges(times: np.ndarray, origin: float, rate: float,
                    step: int = _EDGE_CHUNK) -> np.ndarray:
    """Convert v0 transition times (seconds) to uint64 edge samples in chunks.

    Converting a block at a time keeps the float64 transient to one chunk while the
    result is written straight into the uint64 output. Each chunk goes through
    `timebase.to_samples`: ``round((times - origin) * rate)``, with `TimeRangeError`
    raised for a time that is not a sample index at the rate."""
    out = np.empty(times.size, dtype=np.uint64)
    for i in range(0, times.size, step):
        out[i:i + step] = to_samples(times[i:i + step], origin, rate)
    return out


def _gb(n: int) -> str:
    return f"{n / 1e9:.1f} GB" if n >= 1e9 else f"{n / 1e6:.0f} MB"


class SalTooLargeError(MemoryError):
    """Opening this .sal is predicted to exhaust memory.

    Raised BEFORE anything is inflated or decoded, so the app can offer a windowed
    load. Carries the estimate so a caller can show the numbers and suggest a window
    size: `peak` is the figure that was compared with `available`, the whole-file
    peak or, when `window` (start, end samples) was asked for, the window's.
    `streamed` is False when a window was asked for but the channels are read whole
    whatever the window (a v0 blob, or no block chain that could be walked).
    `whole_channels` lists the channels of a streamed window whose blob does not walk
    as a block chain, so they are read whole and `peak` includes them. `cause` is the
    failure that sent a streamed window read to the whole-channel read, when one did.
    """

    def __init__(self, path: str, cost: "SalCost", available: int,
                 peak: Optional[int] = None, window: Optional[tuple] = None,
                 streamed: bool = True, whole_channels: Sequence[int] = (),
                 cause: str = ""):
        self.path = path
        self.cost = cost
        self.available = int(available)
        self.peak = int(cost.est_peak_bytes if peak is None else peak)
        self.window = None if window is None else (int(window[0]), int(window[1]))
        self.streamed = bool(streamed)
        self.whole_channels = tuple(int(c) for c in whole_channels)
        if self.window is None:
            text = (f"{path}: {cost.summary()}, but only "
                    f"{available / 1e9:.1f} GB is available for one load. Open it again "
                    f"and choose a window (Decode: From … to …) in the Open Capture "
                    f"dialog, or free memory / use a machine with more RAM.")
        elif not self.streamed:
            text = (f"{path}: {cost.summary()}, but only "
                    f"{available / 1e9:.1f} GB is available for one load, and these "
                    f"channels are read whole whatever the window. Free memory / use a "
                    f"machine with more RAM, or export a shorter capture.")
            if cause:
                text += f" (The streamed read of the window failed: {cause}.)"
        elif self.whole_channels:
            s0, s1 = self.window
            names = ", ".join(str(c) for c in self.whole_channels)
            text = (f"{path}: the window from sample {s0:,} to {s1:,} is estimated to "
                    f"peak at ~{_gb(self.peak)}, but only {available / 1e9:.1f} GB is "
                    f"available for one load. Channel {names} is read whole whatever "
                    f"the window: its data does not walk as a block chain (the file "
                    f"may be damaged). Export the capture again, or free memory / use "
                    f"a machine with more RAM.")
        else:
            s0, s1 = self.window
            text = (f"{path}: the window from sample {s0:,} to {s1:,} is estimated to "
                    f"peak at ~{_gb(self.peak)}, but only {available / 1e9:.1f} GB is "
                    f"available for one load (whole capture: {cost.summary()}). Choose "
                    f"a shorter window, or free memory / use a machine with more RAM.")
        MemoryError.__init__(self, text)


# The largest end of a window the readers take; an end past it is read to the capture's end.
MAX_WINDOW_END = 1 << 62


def _seconds(sample: int, rate: int) -> float:
    """`sample` / `rate` as seconds; infinite for a sample count beyond float64."""
    if abs(sample) >= 1 << 1000:
        return math.inf if sample > 0 else -math.inf
    return sample / rate


def _window_bound(src: str, name: str, v) -> int:
    """`v`, a window bound, as a whole sample number: an int is taken as it is and any
    other number rounded to the nearest sample, noted. A value that is not a number, or
    not finite, raises ValueError naming `src`."""
    if isinstance(v, (int, np.integer)) and not isinstance(v, bool):
        return int(v)
    try:
        f = float(v)
    except (TypeError, ValueError):
        raise ValueError(f"{src}: window: the {name} sample ({v!r}) is not a number") from None
    if not math.isfinite(f):
        raise ValueError(f"{src}: window: the {name} sample ({v}) is not a finite number")
    n = int(round(f))
    if n != f:
        import_notes.warn(src, f"the window's {name} sample {v} is not a whole sample; "
                               f"it is read as {n}")
    return n


def _normalise_window(src: str, window: tuple, rate: int) -> tuple:
    """`window` as ``(start, end)`` ints a decode can take. `window` must be a pair of
    finite numbers, else ValueError naming `src`; a bound that is not a whole number of
    samples is rounded to the nearest, noted. A start before the capture is read from
    sample 0 (noted), an end beyond what a sample count can hold is read to the end of
    the capture (noted), and an end not past the start raises ValueError.
    Session._cut reads windows the same way."""
    try:
        first, last = window
    except (TypeError, ValueError):
        raise ValueError(f"{src}: window must be a (start, end) pair of sample numbers "
                         f"(got {window!r})") from None
    s0, s1 = _window_bound(src, "start", first), _window_bound(src, "end", last)
    if s0 < 0:
        import_notes.warn(src, f"the window starts at {_seconds(s0, rate):.9g} s, before "
                               f"the capture; it is read from 0 s")
        s0 = 0
    if s1 > MAX_WINDOW_END:
        import_notes.warn(src, f"the window ends at {_seconds(s1, rate):.9g} s, past any "
                               f"capture; it is read to the end of the capture")
        s1 = MAX_WINDOW_END
    if s1 <= s0:
        raise ValueError(f"{src}: window: end_sample ({s1}) must be past start ({s0})")
    return s0, s1


def _check_window(src: str, window: tuple, span: int, rate: int) -> None:
    """Warn when `window` (start, end) samples runs past a capture `span` samples long
    (0: not known). The end is one past the window's last sample, and a window whose
    end is within 0.1 % of the span is taken as one to the end."""
    if span <= 0 or rate <= 0:
        return
    s0, s1 = int(window[0]), int(window[1])
    end_s = span / float(rate)
    if s0 >= span:
        import_notes.warn(src, f"the window starts at {s0 / rate:.9g} s, at or after the "
                               f"capture's end at {end_s:.9g} s; nothing of it is read")
    elif s1 != MAX_WINDOW_END and s1 - 1 > span + max(1, span // 1000):
        import_notes.warn(src, f"the window ends at {(s1 - 1) / rate:.9g} s, past the "
                               f"capture's end at {end_s:.9g} s; it is read to the end of "
                               f"the capture")


def _v0_clean(src: str, label: str, begin: float, times: Optional[np.ndarray]):
    """(begin time, transition times) of a decoded channel with the times that are not
    finite dropped, each noted under `src`. A v0 channel's begin time that is not finite
    raises ValueError naming the channel; a v3 channel (`times` None) is returned as is."""
    if times is None:
        return begin, times
    begin = require_finite(begin, f"{src}: {label}", "the header's begin time")
    times, gone, first = drop_nonfinite(times)
    if gone:
        import_notes.warn(src, f"{label}: {saleae_binary.skipped_times_note(gone, first)}")
    return begin, times


def _v0_ordered(src: str, label: str, times: Optional[np.ndarray]) -> Optional[np.ndarray]:
    """The transition times of a decoded v0 channel in time order, a channel whose times
    step back noted under `src`; a v3 channel (`times` None) is returned as is."""
    if times is None:
        return times
    times, note = saleae_binary.time_order(times)
    if note:
        import_notes.warn(src, f"{label}: {note}")
    return times


def _bad_after_note(label: str, bad: List[tuple], rate: int) -> str:
    """The import warning for the blocks after a window that failed their check: `bad` is
    what :func:`_check_blocks_after` appended."""
    n, (a, _b, reason) = len(bad), bad[0]
    return (f"{label}: {n:,} block{'s' if n != 1 else ''} after the window "
            f"do{'' if n != 1 else 'es'} not decode{', the first' if n != 1 else ''} "
            f"starting at sample {a:,} ({_seconds(a, rate):.9g} s): {reason}. The window "
            f"decodes and is read; the capture after it is damaged")


def _unreadable_after_note(label: str, cut: tuple, rate: int) -> str:
    """The import warning for an archive member that cannot be read past the window:
    `cut` is the ``(readable_end, reason)`` of :func:`_v3_chains`."""
    end, reason = cut
    return (f"{label}: the capture cannot be read past sample {end:,} "
            f"({_seconds(end, rate):.9g} s), after the window: {reason}. The window "
            f"decodes and is read")


def load_capture(path: str, clock_channel: int, data_channel: int,
                 sample_rate_hz: Optional[int] = None,
                 auto_clock: bool = False,
                 window: Optional[tuple] = None,
                 max_bytes: Optional[int] = None) -> Capture:
    """Build a Capture from a .sal, using the chosen clock & data channels.

    Decodes <SALEAE> version 0, 3 and 4 digital blobs (3 and 4 are one format); raises
    saleae_binary.UnsupportedSaleaeVersion for any other version or a v3 blob that does not
    decode (its subclass MalformedSaleaeV3, whose message names the channel), and ValueError
    if a channel isn't present, the clock and data channel are one channel, or the file is
    not a readable .sal (a damaged archive or meta.json raises ValueError naming it). With
    ``auto_clock`` the forwarded clock is picked as whichever of the two decoded channels
    has more transitions (it toggles every UI), so the caller needn't know the probe
    order.

    ``window`` is an optional ``(start_sample, end_sample)`` range. Only transitions
    inside it are kept — on v3 the block chain is streamed, so memory scales with the
    window rather than the capture — and the returned Capture is REBASED so the window
    starts at sample 0 (matching Capture.subcapture, so every downstream sample number
    is window-relative). A streamed v3 channel's blocks after the window are checked one
    at a time as the whole-file reader checks them, so the time taken grows with the
    file; a block that fails is an import warning, not a refusal, and so is an archive
    member that cannot be read past the window (a deflate or CRC failure). A member that
    cannot be read to the window's end raises ValueError naming the channel and the
    sample its stream can be read to.

    ``max_bytes`` caps the predicted peak memory. When the estimate exceeds it,
    :class:`SalTooLargeError` is raised before any blob is inflated. Pass 0 to
    disable the check; the default consults available system memory. A ``window``
    small enough to fit is always allowed — the guard tests what will actually be
    decoded (:meth:`SalCost.window_peak_bytes`: the blocks the window overlaps), not
    the file size.

    A v0 channel whose begin time is not finite raises ValueError naming the channel,
    and so does a time more than 2**63 samples from the capture start. Import warnings
    (import_notes) report meta.json channel entries that were skipped, v0 transition
    times that are not finite (skipped), v0 times out of order (sorted), off the sample
    grid or landing on one sample, v0 channels with different begin times, and a window
    that starts before the capture (read from sample 0) or runs past its end. A window
    whose end is not past its start raises ValueError, and so does a `sample_rate_hz` below
    1 Hz, above `timebase.MAX_RATE_HZ` or not a number. A streamed window read that fails
    unexpectedly is logged and the channel is read whole, after the memory guard has priced
    that read.
    """
    info = read_info(path)
    src = import_notes.source_name(path)
    for note in info.notes:
        import_notes.warn(src, note)
    if sample_rate_hz:
        check_rate(sample_rate_hz, path)
    rate = int(sample_rate_hz or info.sample_rate_hz)
    if rate <= 0:
        raise ValueError(f"{path}: no digital sample rate in meta.json")
    for ch, role in ((clock_channel, "clock"), (data_channel, "data")):
        if ch not in info.digital_files:
            raise ValueError(f"{path}: {role} channel {ch} not in capture "
                             f"(available: {info.channels})")
    require_distinct(path, clock_channel, data_channel, "channel")
    if window is not None:
        window = _normalise_window(src, window, rate)
    # One header walk per channel, shared by the guard (the capture's length) and the
    # streaming decode (the validated chain start).
    chains = (_v3_chains(path, info, [clock_channel, data_channel])
              if window is not None else {})
    if window is not None:
        for ch, c in sorted(chains.items()):
            cut = c[5]
            if cut is not None and int(window[1]) > cut[0]:
                raise ValueError(
                    f"{path}: digital ch{ch} cannot be read past sample {cut[0]:,} "
                    f"({_seconds(cut[0], rate):.9g} s), inside the window: {cut[1]}. "
                    f"A window that ends by then opens")
        _check_window(src, window, max((c[0] for c in chains.values()), default=0), rate)
    # --- pre-flight memory guard -------------------------------------------------
    # Estimate from the zip directory (no inflation) and refuse when over budget. A
    # windowed v3 load streams the blob past instead of holding it, so its cost is
    # the window's own edges, bounded by the blocks it overlaps, plus the whole read
    # of any channel whose chain did not walk.
    if max_bytes != 0:
        cost = estimate_cost(path, channels=[clock_channel, data_channel])
        budget = memory_budget(max_bytes)
        peak = cost.est_peak_bytes
        streamed = False
        unwalked: List[int] = []
        if window is not None:
            model = _model_from_chains(chains)
            streamed = cost.streamable and model.span_samples > 0
            if streamed:
                peak = cost.window_peak_bytes(model, window)
                unwalked = sorted(ch for ch, c in chains.items() if c[1] < 0)
        if budget > 0 and peak > budget:
            raise SalTooLargeError(path, cost, budget, peak=peak, window=window,
                                   streamed=streamed, whole_channels=unwalked)
    with _zip_errors(path), zipfile.ZipFile(path) as z:
        def read_channel(ch: int):
            """Decode one channel, streaming when a window makes it possible.

            The streaming path never materialises the inflated blob, so a window of
            a huge capture costs the window — not the file. It only applies to v3
            (the block chain is what makes seeking possible); v0 and any blob whose
            chain can't be validated fall back to the in-memory decode.

            Two streaming attempts, not one: the first trusts each block header's `level`
            field, which makes the cost the WINDOW rather than its start offset. A blob
            whose headers aren't stamped raises _UnstampedBlockLevels, and the retry
            derives the level from the decoded parity of every preceding block instead —
            correct, and O(start offset). See _decode_v3_window_streaming."""
            name = info.digital_files[ch]
            label = f"digital ch{ch}"
            if window is not None:
                for trust in (True, False):
                    try:
                        size = z.getinfo(name).file_size
                        bad: List[tuple] = []
                        with z.open(name) as fh:
                            got = _decode_v3_window_streaming(
                                fh, size, int(window[0]), int(window[1]), label,
                                trust_levels=trust, chain=chains.get(ch), bad_after=bad)
                        if got is not None:
                            cut = chains[ch][5]
                            if cut is not None:
                                bad = [x for x in bad if x[2] != cut[1]]
                                import_notes.warn(src, _unreadable_after_note(label, cut,
                                                                              rate))
                            if bad:
                                import_notes.warn(src, _bad_after_note(label, bad, rate))
                            return got[0], None, got[1], 0.0
                        break                      # not a v3 chain: the retry won't help
                    except _UnstampedBlockLevels:
                        continue                   # re-stream, deriving the level by parity
                    except (saleae_binary.MalformedSaleaeV3, MemoryError):
                        raise                      # a corrupt block, or no memory: not retried
                    except Exception as exc:       # noqa: BLE001 - read whole, but priced
                        logger.warning("%s: the streamed window read of %s failed; "
                                       "reading the channel whole", src, label,
                                       exc_info=True)
                        if max_bytes != 0 and budget > 0 and cost.est_peak_bytes > budget:
                            raise SalTooLargeError(
                                path, cost, budget, window=window, streamed=False,
                                cause=f"{type(exc).__name__}: {exc}") from None
                        break
            return _decode_channel(z.read(name), label, window=window, src=src)

        clk_init, clk_times, clk_samp, clk_begin = read_channel(clock_channel)
        dat_init, dat_times, dat_samp, dat_begin = read_channel(data_channel)
    clk_begin, clk_times = _v0_clean(src, f"digital ch{clock_channel}", clk_begin, clk_times)
    dat_begin, dat_times = _v0_clean(src, f"digital ch{data_channel}", dat_begin, dat_times)
    clk_times = _v0_ordered(src, f"digital ch{clock_channel}", clk_times)
    dat_times = _v0_ordered(src, f"digital ch{data_channel}", dat_times)
    # Put both channels on one timeline. A v3 channel is already transition SAMPLES
    # from capture start (sample 0 == capture start). A v0 channel is transition TIMES
    # in seconds; convert them with origin = the capture-start time (begin_time),
    # exactly as the standalone binary loader does. Using begin_time -- NOT the
    # first-transition floor -- is what keeps a v0 channel aligned with a v3 one in a
    # mixed .sal: a negative pre-trigger begin_time would otherwise skew the two lines
    # by |begin_time|*rate samples. begin_time is the single capture start shared by
    # both channels, so any two v0 channels also stay mutually aligned; the `firsts`
    # terms keep the uint64 cast non-negative in the pathological case where a
    # begin_time somehow exceeds the channel's first edge.
    begins = [b for b, t in ((clk_begin, clk_times), (dat_begin, dat_times)) if t is not None]
    firsts = [float(t[0]) for t in (clk_times, dat_times) if t is not None and t.size]
    origin = min(begins + firsts) if (begins or firsts) else 0.0
    # The version-0 checks of the .bin loader: times off the sample grid, two begin times,
    # and (after the conversion) transitions on one sample.
    for ch, times in ((clock_channel, clk_times), (data_channel, dat_times)):
        if times is not None:
            note = saleae_binary.grid_note(times, rate, origin)
            if note:
                import_notes.warn(src, f"digital ch{ch}: {note}")
    if clk_times is not None and dat_times is not None:
        note = saleae_binary.begin_times_note(clk_begin, dat_begin, rate)
        if note:
            import_notes.warn(src, f"digital ch{clock_channel} + digital ch{data_channel}: "
                                   f"{note}")

    def to_edges(ch, times, samp):
        if times is None:
            return samp
        try:
            edges = _times_to_edges(times, origin, rate)
        except TimeRangeError as e:
            raise ValueError(f"{path}: {e}") from None
        same = repeats(edges)
        if same:
            import_notes.warn(src, f"digital ch{ch}: {same_sample_note(same, rate)}")
        return edges

    clk_edges = to_edges(clock_channel, clk_times, clk_samp)
    clk_times = clk_samp = None            # drop the v0 view (frees the blob bytes it aliases)
    dat_edges = to_edges(data_channel, dat_times, dat_samp)
    dat_times = dat_samp = None
    # The forwarded clock toggles every UI, so it has the most transitions; when the
    # caller can't say which channel is which, pick the busier one as the clock.
    if auto_clock and dat_edges.size > clk_edges.size:
        clk_edges, dat_edges = dat_edges, clk_edges
        clk_init, dat_init = dat_init, clk_init
    if window is not None:
        # A v0 channel decoded its whole table, so trim it to the window here; a v3
        # channel was already seeked and this is a no-op. Then rebase both to 0 so a
        # windowed capture numbers its samples exactly like Capture.subcapture().
        s0, s1 = int(window[0]), int(window[1])

        def clip(edges, initial):
            # Rebased IN PLACE: every edge array here was built by this call, and a
            # streamed v3 channel is already inside the window, so it is not copied.
            lo = int(searchsorted(edges, s0, side="right"))
            hi = int(searchsorted(edges, s1, side="left"))
            kept = edges if (lo == 0 and hi == edges.size) else edges[lo:hi].copy()
            kept -= np.uint64(s0)                # every kept edge is > s0
            return kept, bool(initial) ^ (lo % 2 == 1)

        clk_edges, clk_init = clip(clk_edges, clk_init)
        dat_edges, dat_init = clip(dat_edges, dat_init)
    return Capture(
        clock_edges=clk_edges,
        data_edges=dat_edges,
        initial_clock=clk_init,
        initial_data=dat_init,
        sample_rate_hz=rate,
    )
