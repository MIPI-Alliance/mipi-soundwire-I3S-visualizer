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

import json
import zipfile
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

import numpy as np

from . import saleae_binary
from .capture import Capture


@dataclass
class SalInfo:
    sample_rate_hz: int
    # device-channel -> "digital-N.bin" name
    digital_files: Dict[int, str] = field(default_factory=dict)
    # device-channel numbers of enabled digital channels, in order
    channels: List[int] = field(default_factory=list)


@dataclass
class SalCost:
    """Predicted memory cost of opening a .sal, WITHOUT decoding it.

    A .sal is a zip of delta-coded blobs: a 278 MB file can hold 2.25 GB of
    uncompressed payload and decode to 18 GB of uint64 edge arrays, so a naive
    open can exhaust RAM and wedge the machine before any error surfaces. Every
    figure here comes from the zip central directory plus a few KB of header, so
    the estimate itself costs nothing.
    """
    file_bytes: int                    # the .sal on disk
    uncompressed_bytes: int            # blobs once inflated (what z.read() materialises)
    est_transitions: int               # summed across the channels that will be loaded
    est_edge_bytes: int                # uint64 edge arrays built from them
    sample_rate_hz: int
    per_channel: Dict[int, int] = field(default_factory=dict)   # channel -> est transitions

    @property
    def est_peak_bytes(self) -> int:
        """Rough peak RSS for a whole-file open: the inflated blobs and the edge
        arrays are both live at once (the blob is only released after the last
        channel is decoded), plus the decoder's own working set.

        THE EDGE TERM CARRIES THE DECODE TRANSIENT and the blob term does not. Building
        the edge arrays holds the per-block int64 delta run, its cumsum, the boolean mask
        and the final concatenate simultaneously (`_STREAM_TRANSIENT`), while the inflated
        blob is one flat allocation. This used to return the bare sum, so it under-predicted
        the real peak by 1.9x-3.6x on measured captures — and because the transient was
        applied ONLY on the windowed branch of `load_capture`, the path that allocates LEAST
        was the only one estimated honestly. On a 103 GB machine that let a capture predicted
        at 20.29 GB load to a measured ~60 GB and put macOS into continuous memory
        compression, which is a minute-long stall that no amount of per-move Python tuning
        reaches. Measured against this model: 2.134 vs 2.125 GB actual, 0.948 vs 0.921, and
        0.733 vs 0.598 (over-predicting, which is the safe direction for a guard).
        """
        return int(self.uncompressed_bytes + self.est_edge_bytes * _STREAM_TRANSIENT)

    def fits_in(self, budget_bytes: int) -> bool:
        return self.est_peak_bytes <= int(budget_bytes)

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
# Sampling the real terminator density would mean inflating the blob — the very
# thing this estimate exists to avoid.
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
    with zipfile.ZipFile(path) as z:
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
            if len(head) >= 12 and head[:8] == saleae_binary.SALEAE_MAGIC:
                if int.from_bytes(head[8:12], "little") == 0:
                    per_tr = _V0_BYTES_PER_TRANSITION
            per_channel[ch] = int(blob / per_tr) if per_tr else 0
    est_tr = sum(per_channel.values())
    return SalCost(file_bytes=int(os.path.getsize(path)),
                   uncompressed_bytes=uncompressed,
                   est_transitions=est_tr,
                   est_edge_bytes=est_tr * _EDGE_BYTES,
                   sample_rate_hz=info.sample_rate_hz,
                   per_channel=per_channel)


def available_memory_bytes() -> int:
    """Best-effort physical memory available for a load, or 0 if unknown.

    Prefers genuinely free memory over total RAM: a 24 GB box with 20 GB in use
    cannot absorb an 18 GB load, and the point of the guard is to refuse before
    the machine starts swapping itself to death.
    """
    try:                                        # psutil is the accurate path
        import psutil
        return int(psutil.virtual_memory().available)
    except Exception:
        pass
    try:                                        # POSIX: free pages * page size
        import os
        if hasattr(os, "sysconf"):
            names = os.sysconf_names
            if "SC_AVPHYS_PAGES" in names and "SC_PAGE_SIZE" in names:
                avail = os.sysconf("SC_AVPHYS_PAGES") * os.sysconf("SC_PAGE_SIZE")
                if avail > 0:
                    return int(avail)
    except Exception:
        pass
    return 0


# Budget used when available memory can't be determined (no psutil, and no
# SC_AVPHYS_PAGES — which is the case on stock macOS). The guard exists to fail
# CLOSED: treating "unknown" as "unlimited" silently disables it on exactly the
# machines it was written for. 8 GiB of predicted peak is generous for any ordinary
# capture while still refusing the multi-GB ones that wedge a machine; an explicit
# max_bytes (or max_bytes=0 to opt out) overrides it.
_FALLBACK_BUDGET = 8 << 30
# Absolute ceiling on the COMPUTED budget, however much memory is free. Past this a load
# stops being a question about capacity and becomes one about interactivity, which is a
# person's call — so the app asks for a time window instead of deciding for them. Set equal
# to _FALLBACK_BUDGET so "memory unknown" and "memory plentiful" agree on the largest load
# taken without asking; an explicit max_bytes overrides both. See memory_budget.
_MAX_AUTO_BUDGET = 8 << 30


def memory_budget(max_bytes: Optional[int] = None) -> int:
    """The peak-memory budget a capture open is allowed, in bytes.

    ONE definition, shared by the loader's guard and the UI's window prompt. They had
    separate copies and only the loader's was fixed to fail closed, so on a machine
    with no readable free-memory figure the prompt still concluded "it fits" and the
    user hit a raw SalTooLargeError from inside the async worker instead of being
    offered a window. Same class of bug as the spacing fix landing in one of two
    placement engines — see the maintainers' spacing row-boundary write-up §6.

    A positive `max_bytes` overrides the computed budget. `0`/`None` mean "no explicit
    cap", and this returns the computed budget for them — opting OUT of the guard
    entirely is the caller's job (load_capture short-circuits on `max_bytes == 0`
    before asking), because a budget function has no way to express "unlimited"
    without handing back a number that silently disables the check.

    THE COMPUTED BUDGET IS CAPPED ABSOLUTELY, not just as a fraction of free memory.
    "Will it fit" and "will it stay interactive" are different questions, and only the
    first scales with RAM: 0.6 x free means the more capable the machine, the more the app
    loads without asking, so the failure mode grows with the hardware. That is how a
    103 GB box loaded a capture to ~60 GB resident — no prompt, continuous macOS memory
    compression, and a minute-long stall — while 16-32 GB machines were offered a time
    window for the same file and never reached that state. The complaint that started this
    was that the BETTER machine was slower. `_MAX_AUTO_BUDGET` is what a load may consume
    before the caller has to ask a human; an explicit `max_bytes` still overrides it,
    because a caller naming a number has made the decision itself.
    """
    if max_bytes:
        return int(max_bytes)
    avail = available_memory_bytes()
    # Fail CLOSED when memory is unknown (0): a guard that silently disables itself is
    # worse than none, because the caller believes it is protected.
    if avail <= 0:
        return _FALLBACK_BUDGET
    return min(int(avail * _MEM_HEADROOM), _MAX_AUTO_BUDGET)


# Never plan to consume ALL free memory: the decode, the audio store and Qt itself
# still need room, and an allocation that exactly fills RAM thrashes rather than fails.
_MEM_HEADROOM = 0.6
# Peak/final ratio for a streaming windowed decode: the per-block int64 delta run,
# its cumsum, the boolean mask and the final concatenate are all live at once.
# Calibrated against the reported capture — a 2 s window whose final edge arrays are
# 0.31 GB peaked at ~1.29 GB above baseline, i.e. ~4x.
_STREAM_TRANSIENT = 4.0


def _v3_span_streaming(fh, size: int, want_start: bool = False):
    """Last ``B_end`` of a v3 block chain, read by streaming the headers only.

    Walks the chain from the start of the stream, seeking past each block's delta
    payload instead of decoding (or even holding) it, so peak memory is one block
    header regardless of blob size. This is what makes it safe to ask "how long is
    this capture?" about a file too big to open — `z.read()` on the same blob would
    inflate the gigabytes we are trying to avoid.

    Returns 0 if no self-consistent chain tiles the stream exactly (same
    acceptance rule as the in-memory walk, so the two never disagree). With
    ``want_start`` returns ``(chain_end, start_offset)`` instead — a caller that
    then decodes needs the VALIDATED offset, because a real Logic 2 metadata header
    contains several zero qwords that each look like a candidate first block and
    only the full walk tells them apart.
    """
    fail = (0, -1) if want_start else 0
    head = fh.read(65536)
    if len(head) < 16 or head[:8] != saleae_binary.SALEAE_MAGIC:
        return fail
    if int.from_bytes(head[8:12], "little") not in saleae_binary.V3_LIKE_VERSIONS:
        return fail
    hdr_len = saleae_binary._V3_BLOCK_HEADER
    bc_off = saleae_binary._V3_BYTECOUNT_OFF
    for o0 in range(16, len(head) - hdr_len + 1):
        if int.from_bytes(head[o0:o0 + 8], "little") != 0:      # first block A_start == 0
            continue
        pos, prev_b, ok = o0, None, True
        buf = head
        while pos + hdr_len <= size:
            if pos + hdr_len > len(buf):                        # need more bytes
                fh.seek(pos)
                chunk = fh.read(hdr_len)
                if len(chunk) < hdr_len:
                    ok = False
                    break
                a = int.from_bytes(chunk[0:8], "little")
                b = int.from_bytes(chunk[8:16], "little")
                cnt = int.from_bytes(chunk[bc_off:bc_off + 8], "little")
            else:
                a = int.from_bytes(buf[pos:pos + 8], "little")
                b = int.from_bytes(buf[pos + 8:pos + 16], "little")
                cnt = int.from_bytes(buf[pos + bc_off:pos + bc_off + 8], "little")
            body = pos + hdr_len
            if b < a or body + cnt > size or (prev_b is None and a != 0) or \
               (prev_b is not None and a != prev_b):
                ok = False
                break
            if prev_b is None and b == a and body + cnt != size:
                ok = False
                break
            prev_b, pos = b, body + cnt
        if ok and pos == size and prev_b is not None:
            return (int(prev_b), o0) if want_start else int(prev_b)
        fh.seek(0)
        head = fh.read(65536)
    return fail


def capture_span_samples(path: str, channels: Optional[Sequence[int]] = None) -> int:
    """Length of the capture in samples (0 if it can't be determined).

    Read from the v3 block chain's last ``B_end`` by streaming block headers — no delta
    decoding and no whole-blob `read()` — so it is safe to ask about a capture far too large
    to open. v0 blobs have no such index and contribute 0.

    ``channels`` restricts the walk to those device channels. **Pass it whenever the caller
    knows which channels it will load.** Without it every digital channel in the archive is
    walked, and on a compressed archive that is not cheap: the walk seeks with
    ``ZipExtFile.seek()``, which for a DEFLATE member is implemented as read-and-discard (and
    re-reads from zero for each candidate start offset), so an 8-channel capture pays that
    cost six times over for channels nobody asked for. That is what made a pre-flight span
    read block the GUI for ~2 s on a multi-hundred-MB `.sal`.

    Only STORED members get a true seek, which is why a test fixture written with
    ``ZIP_STORED`` shows none of this — see `tests/test_sal_large_capture.py`.
    """
    info = read_info(path)
    want = None if channels is None else {int(c) for c in channels}
    best = 0
    with zipfile.ZipFile(path) as z:
        for ch, name in info.digital_files.items():
            if want is not None and int(ch) not in want:
                continue
            try:
                size = z.getinfo(name).file_size
                with z.open(name) as fh:
                    best = max(best, _v3_span_streaming(fh, size))
            except Exception:
                continue
    return best


def _capture_span_samples(info: "SalInfo", path: str,
                          channels: Optional[Sequence[int]] = None) -> int:
    """Span helper used by the load guard; tolerates any read failure (the guard
    then just falls back to the whole-capture estimate, which is conservative)."""
    try:
        return capture_span_samples(path, channels=channels)
    except Exception:
        return 0



def _dig(meta: dict, *path, default=None):
    cur = meta
    for k in path:
        if isinstance(cur, dict) and k in cur:
            cur = cur[k]
        else:
            return default
    return cur


def read_info(path: str) -> SalInfo:
    with zipfile.ZipFile(path) as z:
        meta = json.loads(z.read("meta.json"))
        names = set(z.namelist())
    sample_rate = int(_dig(meta, "data", "legacySettings", "sampleRate", "digital",
                           default=0) or 0)
    # binData[i] maps a per-channel file to its device channel & type. It mixes
    # Digital and Analog entries (and the file name isn't positional), so key off
    # entry["type"]=="Digital" and entry["file"].
    digital_files: Dict[int, str] = {}
    bin_data = _dig(meta, "binData", default=[]) or []
    for entry in bin_data:
        if not isinstance(entry, dict) or entry.get("type") != "Digital":
            continue
        name = str(entry.get("file", "")).lstrip("./")
        if name in names and "deviceChannel" in entry:
            digital_files[int(entry["deviceChannel"])] = name
    # Fall back to positional digital-N.bin if binData is absent/uninformative.
    if not digital_files:
        i = 0
        while f"digital-{i}.bin" in names:
            digital_files[i] = f"digital-{i}.bin"
            i += 1
    channels = sorted(digital_files)
    return SalInfo(sample_rate_hz=sample_rate, digital_files=digital_files, channels=channels)


# Offset of the first transition block in a MINIMAL v3 blob: `<SALEAE>` + version + type
# and nothing else (saleae_binary.build_channel_v3). Every Logic-shaped writer puts a
# metadata header before the chain, so a first-block offset past this one is the signal
# that the blob's per-block `level` fields were actually filled in — 51 bytes for
# build_logic2_channel_v3, 67 on the reported capture. See _decode_v3_window_streaming.
_V3_MINIMAL_FIRST_BLOCK = len(saleae_binary.SALEAE_MAGIC) + 8


class _UnstampedBlockLevels(Exception):
    """A v3 blob whose per-block `level` fields can't be trusted, so a window's opening
    line level has to come from the decoded parity of every preceding block instead.

    Raised by :func:`_decode_v3_window_streaming` under ``trust_levels=True`` when the
    skipped blocks show no evidence of being stamped. The caller retries the same window
    with ``trust_levels=False``. See that function for why the fast path exists."""


def _decode_v3_window_streaming(fh, size: int, s0: int, s1: int, label: str,
                                trust_levels: bool = True):
    """Decode a v3 window straight from the zip stream, never holding the blob.

    :func:`saleae_binary.parse_channel_v3_window` already skips non-overlapping
    blocks, but it needs the whole inflated blob in memory first — 2.25 GB on the
    reported capture, which dominates the cost of a 2 s window and is most of what
    makes a large .sal unopenable. Here the entry is inflated sequentially and each
    block is either **skipped** as it streams past or buffered and decoded when it
    overlaps the window. Peak memory is therefore one block plus the window's own edges.

    WHAT A SKIPPED BLOCK COSTS IS THE WHOLE POINT. All a skipped block contributes is
    the LINE LEVEL at the window's start, one bit. Deriving that bit by counting
    transitions means decoding every delta before the window, which is O(START OFFSET)
    and made a windowed open of a long capture unusable: on a 176 s / 2.25 G-transition
    capture, a 0.5 s window cost 3.7 s at the start, 15.3 s at 88 s and 43.3 s at
    175.5 s — the same half-second of data, and the beach ball users reported. Terminator
    counting can't replace the decode either: a multi-byte code whose MSB digit is < 0x40
    encodes as [digit+0x40, ..., terminal] (``encode_v3_delta(65) == b"\\x40\\x40"``), so
    both bytes are < 0x80 and an odd over-count opens the window with the level INVERTED —
    samples right, polarity wrong, silently.

    So take the bit from the block header instead. Each header carries `level`, the line
    state at that block's START, and the block containing s0 is decoded ANYWAY because it
    overlaps the window — so its own transitions give the level at s0 exactly, and no
    earlier block needs decoding at all. Verified against the reported capture: the header
    level agreed with the decoded parity on 400/400 blocks of both channels, with about
    half the fields nonzero (so they carry real data, not a constant).

    ``trust_levels=False`` restores the parity walk. It exists because
    :func:`saleae_binary.build_channel_v3` — the minimal internal test writer — stamps 0
    for every non-first block, and trusting that would invert the level. The two are told
    apart by whether a METADATA HEADER precedes the chain, which is a property of the
    writer rather than of the data: a minimal blob puts its first block at offset
    ``_V3_MINIMAL_FIRST_BLOCK`` (magic + version + type, nothing else), while every
    Logic-shaped blob carries the header Logic needs first — 51 bytes for
    :func:`saleae_binary.build_logic2_channel_v3`, 67 on the reported capture. The level
    VALUES cannot be used as the signal: with an even block size and a low initial state
    every block legitimately starts at 0, which is indistinguishable from unstamped.
    An unrecognised blob therefore raises :class:`_UnstampedBlockLevels` and the caller
    retries by parity — correct, just O(start offset), which is the safe direction.

    Returns ``(initial_state_at_s0, transition_samples)`` on the ORIGINAL timeline,
    matching parse_channel_v3_window; ``None`` if the blob isn't a v3 chain (the
    caller then falls back to the in-memory path).
    """
    hdr_len = saleae_binary._V3_BLOCK_HEADER
    bc_off = saleae_binary._V3_BYTECOUNT_OFF
    lvl_off = saleae_binary._V3_LEVEL_OFF
    # Take the VALIDATED chain start from the header walk. Picking the first zero
    # qword instead silently lands mid-metadata on a real Logic 2 blob (several
    # candidates there look plausible) and decodes an empty window.
    chain_end, start = _v3_span_streaming(fh, size, want_start=True)
    if chain_end <= 0 or start < 0:
        return None
    # A metadata header before the chain means a Logic-shaped writer, which stamps every
    # block's level. Nothing else here may assume the levels are real.
    stamped = start > _V3_MINIMAL_FIRST_BLOCK
    fh.seek(0)
    head = fh.read(start + hdr_len)
    if len(head) < start + hdr_len:
        return None
    initial = int.from_bytes(head[start + lvl_off:start + lvl_off + 2], "little") & 1
    limit = min(int(s1), chain_end)
    fh.seek(0)
    _skip(fh, start)                                  # discard the metadata header
    pos, parity, kept = start, 0, []
    level = None            # line level AT s0, from the header of the block containing it
    skipped = 0             # blocks that ended at or before s0
    while pos + hdr_len <= size:
        h = fh.read(hdr_len)
        if len(h) < hdr_len:
            break
        a = int.from_bytes(h[0:8], "little")
        b = int.from_bytes(h[8:16], "little")
        lvl = int.from_bytes(h[lvl_off:lvl_off + 2], "little") & 1
        cnt = int.from_bytes(h[bc_off:bc_off + 8], "little")
        body = pos + hdr_len
        if b < a or body + cnt > size:
            break
        if b <= s0:
            skipped += 1
            if trust_levels and stamped:
                _skip(fh, cnt)                        # no decode: the level is in a header
            else:
                parity ^= _count_and_discard(fh, cnt, drop_last=(b >= chain_end)) & 1
        elif a >= limit:
            break                                     # past the window; stop inflating
        else:
            run = saleae_binary._v3_decode_deltas_np(fh.read(cnt), 0, cnt)
            abs_s = np.cumsum(run, dtype=np.int64) + int(a)
            if b >= chain_end and abs_s.size:
                abs_s = abs_s[:-1]                    # drop the phantom end gap
            crossed = int(np.count_nonzero(abs_s <= s0)) & 1
            parity ^= crossed
            if level is None and trust_levels and stamped:
                # This block spans s0, so its own header level plus the transitions it
                # holds before s0 give the level at s0 -- no earlier block involved.
                level = lvl ^ crossed
            sel = abs_s[(abs_s > s0) & (abs_s < limit)]
            if sel.size:
                kept.append(sel)
        pos = body + cnt
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
        level = initial ^ parity
    samples = (np.concatenate(kept) if len(kept) > 1
               else (kept[0] if kept else np.zeros(0, dtype=np.int64)))
    return bool(level), np.ascontiguousarray(samples, dtype=np.uint64)


def _skip(fh, n: int, chunk: int = 1 << 20) -> None:
    """Consume `n` bytes from a (possibly non-seekable) stream without keeping them."""
    while n > 0:
        got = fh.read(min(n, chunk))
        if not got:
            return
        n -= len(got)


def _count_and_discard(fh, nbytes: int, drop_last: bool) -> int:
    """Number of transitions in the next `nbytes` of delta run, discarding the bytes.

    A skipped block contributes only its level PARITY, so all we need is the delta
    count — but it has to be the real one. Counting bytes < 0x80 as terminators
    over-reports: a multi-byte code whose MSB digit is < 0x40 encodes as
    [digit+0x40, ..., terminal] (e.g. encode_v3_delta(65) == b"\\x40\\x40"), so both
    bytes are < 0x80. An odd over-count flips the parity and the window opens with the
    line level INVERTED — samples right, polarity wrong, silently. So decode the run.

    Read whole (a block body, not the blob) and released immediately; blocks are the
    codec's own chunking, which is what keeps this bounded. `drop_last` excludes the
    chain's trailing gap-to-capture-end delta, which is not a real transition."""
    buf = fh.read(nbytes)
    if not buf:
        return 0
    n = int(saleae_binary._v3_decode_deltas_np(buf, 0, len(buf)).size)
    return n - 1 if (drop_last and n) else n


def _decode_channel(data: bytes, label: str, window=None):
    """Decode one digital-N blob to (initial_state, times_s, samples, begin_time).

    For version 0 `times_s` is the transition times in seconds and `begin_time` is
    the recorded capture-start time (`samples` is None); for version 3 `samples` is
    the transition sample numbers, already measured from capture start (`times_s` is
    None, `begin_time` 0.0). The v0 seconds→samples conversion is deferred to
    load_capture so v0 and v3 channels can share ONE capture-start origin: a v3
    channel's sample 0 IS the v0 begin_time, and pre-trigger begin_times are negative
    and would wrap through the uint64 cast. Raises UnsupportedSaleaeVersion for any
    other version.

    `window` is an optional (start_sample, end_sample) pair. On v3 it is pushed down
    into the block-chain walk so only overlapping blocks are decoded (see
    saleae_binary.parse_channel_v3_window) — the whole point being NOT to
    materialise a multi-GB edge array for a capture you only want 10 s of. v0 is a
    flat table with no block index, so it decodes fully and slices after; that path
    is bounded by the f64 table itself, which is already in memory."""
    version = saleae_binary.peek_version(data)
    if version == 0:
        ch = saleae_binary.parse_channel(data, label)
        times = np.asarray(ch.times_s, dtype=np.float64)
        return ch.initial_state, times, None, float(ch.begin_time)
    if version in saleae_binary.V3_LIKE_VERSIONS:
        if window is not None:
            ch = saleae_binary.parse_channel_v3_window(data, window[0], window[1], label)
        else:
            ch = saleae_binary.parse_channel_v3(data, label)
        return ch.initial_state, None, ch.transition_samples, 0.0
    raise saleae_binary.UnsupportedSaleaeVersion(version, label)


_EDGE_CHUNK = 1 << 23      # v0 seconds->samples conversion block (~8M edges -> 64 MiB float64)


def _times_to_edges(times: np.ndarray, origin: float, rate: float,
                    step: int = _EDGE_CHUNK) -> np.ndarray:
    """Convert v0 transition times (seconds) to uint64 edge samples in chunks.

    The naive ``np.rint((times - origin) * rate).astype(uint64)`` allocates three
    FULL float64 temporaries (8 bytes/edge each) on top of the raw blob — on a 300M+
    edge capture that's ~8 GiB of transients and OOMs a RAM-limited box (e.g. a VM).
    Converting a block at a time keeps the transient to one small chunk while the
    result is written straight into the uint64 output. Bit-identical to the
    whole-array expression: the per-element arithmetic is unchanged, and origin is a
    lower bound on every time (min of begin-times + first edges), so no value is
    negative before the uint64 cast."""
    out = np.empty(times.size, dtype=np.uint64)
    for i in range(0, times.size, step):
        b = times[i:i + step].astype(np.float64)         # writable copy of just this chunk
        b -= origin
        b *= rate
        np.rint(b, out=b)
        out[i:i + step] = b.astype(np.uint64)
    return out


class SalTooLargeError(MemoryError):
    """Opening this .sal is predicted to exhaust memory.

    Raised BEFORE anything is inflated or decoded, so the app can offer a windowed
    load instead of letting the machine swap itself unresponsive. Carries the
    estimate so a caller can show the numbers and suggest a window size.
    """

    def __init__(self, path: str, cost: "SalCost", available: int):
        self.path = path
        self.cost = cost
        self.available = int(available)
        MemoryError.__init__(
            self,
            f"{path}: {cost.summary()}, but only "
            f"{available / 1e9:.1f} GB is available for one load. Open a time window "
            f"instead (Analyzer ▸ Open Capture Time Window…), or free memory / use a "
            f"machine with more RAM.")


def load_capture(path: str, clock_channel: int, data_channel: int,
                 sample_rate_hz: Optional[int] = None,
                 auto_clock: bool = False,
                 window: Optional[tuple] = None,
                 max_bytes: Optional[int] = None) -> Capture:
    """Build a Capture from a .sal, using the chosen clock & data channels.

    Decodes <SALEAE> version 0, 3 and 4 digital blobs (3 and 4 are one format); raises
    saleae_binary.UnsupportedSaleaeVersion for any other version, and ValueError
    if a channel isn't present. With ``auto_clock`` the forwarded clock is picked
    as whichever of the two decoded channels has more transitions (it toggles every
    UI), so the caller needn't know the probe order.

    ``window`` is an optional ``(start_sample, end_sample)`` range. Only transitions
    inside it are decoded — on v3 the block chain is seeked, so cost scales with the
    window rather than the capture — and the returned Capture is REBASED so the
    window starts at sample 0 (matching Capture.subcapture, so every downstream
    sample number is window-relative).

    ``max_bytes`` caps the predicted peak memory. When the estimate exceeds it,
    :class:`SalTooLargeError` is raised before any blob is inflated. Pass 0 to
    disable the check; the default consults available system memory. A ``window``
    small enough to fit is always allowed — the guard tests what will actually be
    decoded, not the file size.
    """
    info = read_info(path)
    rate = int(sample_rate_hz or info.sample_rate_hz)
    if rate <= 0:
        raise ValueError(f"{path}: no digital sample rate in meta.json")
    for ch, role in ((clock_channel, "clock"), (data_channel, "data")):
        if ch not in info.digital_files:
            raise ValueError(f"{path}: {role} channel {ch} not in capture "
                             f"(available: {info.channels})")
    # --- pre-flight memory guard -------------------------------------------------
    # Estimate from the zip directory (no inflation) and refuse rather than let the
    # allocator take the machine down. A windowed v3 load streams the blob past
    # instead of holding it, so its cost is the window's own edges — which is what
    # makes a 2 s slice of an otherwise unopenable capture viable.
    if max_bytes != 0:
        cost = estimate_cost(path, channels=[clock_channel, data_channel])
        budget = memory_budget(max_bytes)
        peak = cost.est_peak_bytes
        if window is not None:
            span = max(0, int(window[1]) - int(window[0]))
            total = _capture_span_samples(info, path,
                                         channels=[clock_channel, data_channel])
            if total > 0:
                frac = min(1.0, span / float(total))
                # Streaming: no inflated-blob term, just the window's own edges plus
                # the decode transient (see _STREAM_TRANSIENT).
                peak = int(cost.est_edge_bytes * frac * _STREAM_TRANSIENT)
        if budget > 0 and peak > budget:
            raise SalTooLargeError(path, cost, budget)
    with zipfile.ZipFile(path) as z:
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
                        with z.open(name) as fh:
                            got = _decode_v3_window_streaming(
                                fh, size, int(window[0]), int(window[1]), label,
                                trust_levels=trust)
                        if got is not None:
                            return got[0], None, got[1], 0.0
                        break                      # not a v3 chain: the retry won't help
                    except _UnstampedBlockLevels:
                        continue                   # re-stream, deriving the level by parity
                    except Exception:
                        break                      # fall through to the in-memory path
            return _decode_channel(z.read(name), label, window=window)

        clk_init, clk_times, clk_samp, clk_begin = read_channel(clock_channel)
        dat_init, dat_times, dat_samp, dat_begin = read_channel(data_channel)
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

    def to_edges(times, samp):
        return _times_to_edges(times, origin, rate) if times is not None else samp

    clk_edges = to_edges(clk_times, clk_samp)
    clk_times = clk_samp = None                          # drop the v0 view (frees the blob bytes it aliases)
    dat_edges = to_edges(dat_times, dat_samp)
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
            lo = int(np.searchsorted(edges, s0, side="right"))
            hi = int(np.searchsorted(edges, s1, side="left"))
            return (np.ascontiguousarray(edges[lo:hi].astype(np.int64) - s0,
                                         dtype=np.uint64),
                    bool(initial) ^ (lo % 2 == 1))

        clk_edges, clk_init = clip(clk_edges, clk_init)
        dat_edges, dat_init = clip(dat_edges, dat_init)
    return Capture(
        clock_edges=clk_edges,
        data_edges=dat_edges,
        initial_clock=clk_init,
        initial_data=dat_init,
        sample_rate_hz=rate,
    )
