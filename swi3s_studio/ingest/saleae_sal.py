"""Reader for the Saleae Logic 2 ``.sal`` project file.

A ``.sal`` is a ZIP containing ``meta.json`` + one ``digital-N.bin`` per captured
digital channel (and analog files). ``meta.json`` gives the digital sample rate
and the channel <-> file mapping. Each ``digital-N.bin`` begins with the
``<SALEAE>`` header; we decode both **version 0** (the documented export layout,
absolute transition times) and **version 3** (Logic 2's compressed internal
format, per-block sample deltas — see ``saleae_binary.parse_channel_v3``). Other
versions raise UnsupportedSaleaeVersion with guidance to export Binary/CSV.
"""
from __future__ import annotations

import json
import zipfile
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import numpy as np

from .capture import Capture
from . import saleae_binary


@dataclass
class SalInfo:
    sample_rate_hz: int
    # device-channel -> "digital-N.bin" name
    digital_files: Dict[int, str] = field(default_factory=dict)
    # device-channel numbers of enabled digital channels, in order
    channels: List[int] = field(default_factory=list)


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


def _decode_channel(data: bytes, label: str):
    """Decode one digital-N blob to (initial_state, times_s, samples, begin_time).

    For version 0 `times_s` is the transition times in seconds and `begin_time` is
    the recorded capture-start time (`samples` is None); for version 3 `samples` is
    the transition sample numbers, already measured from capture start (`times_s` is
    None, `begin_time` 0.0). The v0 seconds→samples conversion is deferred to
    load_capture so v0 and v3 channels can share ONE capture-start origin: a v3
    channel's sample 0 IS the v0 begin_time, and pre-trigger begin_times are negative
    and would wrap through the uint64 cast. Raises UnsupportedSaleaeVersion for any
    other version."""
    version = saleae_binary.peek_version(data)
    if version == 0:
        ch = saleae_binary.parse_channel(data, label)
        return (ch.initial_state, np.asarray(ch.times_s, dtype=np.float64),
                None, float(ch.begin_time))
    if version == saleae_binary._V3_VERSION:
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


def load_capture(path: str, clock_channel: int, data_channel: int,
                 sample_rate_hz: Optional[int] = None,
                 auto_clock: bool = False) -> Capture:
    """Build a Capture from a .sal, using the chosen clock & data channels.

    Decodes <SALEAE> version 0 and 3 digital blobs; raises
    saleae_binary.UnsupportedSaleaeVersion for any other version, and ValueError
    if a channel isn't present. With ``auto_clock`` the forwarded clock is picked
    as whichever of the two decoded channels has more transitions (it toggles every
    UI), so the caller needn't know the probe order.
    """
    info = read_info(path)
    rate = int(sample_rate_hz or info.sample_rate_hz)
    if rate <= 0:
        raise ValueError(f"{path}: no digital sample rate in meta.json")
    for ch, role in ((clock_channel, "clock"), (data_channel, "data")):
        if ch not in info.digital_files:
            raise ValueError(f"{path}: {role} channel {ch} not in capture "
                             f"(available: {info.channels})")
    with zipfile.ZipFile(path) as z:
        clk_init, clk_times, clk_samp, clk_begin = _decode_channel(
            z.read(info.digital_files[clock_channel]), f"digital ch{clock_channel}")
        dat_init, dat_times, dat_samp, dat_begin = _decode_channel(
            z.read(info.digital_files[data_channel]), f"digital ch{data_channel}")
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
    return Capture(
        clock_edges=clk_edges,
        data_edges=dat_edges,
        initial_clock=clk_init,
        initial_data=dat_init,
        sample_rate_hz=rate,
    )
