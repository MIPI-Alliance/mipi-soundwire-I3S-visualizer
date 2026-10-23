"""The decode core's handling of inputs outside the shapes the app itself produces.

- TransitionSampleSource views its edge arrays without a copy; an argument that is not
  already C-contiguous uint64 (a narrower dtype, a strided view, a list) is converted, and
  the converted array must live as long as the source.
- The audio-output reservation run() makes up front is bounded, and absent when audio is
  not decoded.
- A config CSV's SampleSize_REG is a 5-bit field: the audio decode reads it at the width the
  register map and the grid read it, so audio() and audio_columns() carry the same values.
- A config CSV's DeviceNumber_REG outside 0..11 names no device: that port is disabled, so
  the audio decode, the grid and the register map all leave it out.
- DecoderSettings.forced_column_count outside [2, 32] auto-detects, as an out-of-range
  forced_column_sections entry does.
- A recovered-clock (DLV) decode reports a total UI count, so its progress is a fraction.
"""
import gc
import os
import sys
import tempfile

import numpy as np
import pytest
import swi3score

from swi3s_studio.ingest import transitions
from swi3s_studio.session import Session


def _decode(src, **settings):
    s = swi3score.DecoderSettings()
    for k, v in settings.items():
        setattr(s, k, v)
    dec = swi3score.Decoder(src, s)
    dec.run()
    return dec


def _summary(dec):
    cmds = dec.commands()
    return (dec.column_count, sum(1 for c in cmds if c["crc_valid"]),
            [c["command"] for c in cmds])


def _source(cap, clock, data):
    return swi3score.TransitionSampleSource(
        clock, data, bool(cap.initial_clock), bool(cap.initial_data), cap.sample_rate_hz)


# ---- TransitionSampleSource argument conversion -------------------------------------

_CONVERSIONS = {
    "uint32": lambda e: e.astype(np.uint32),
    "strided": lambda e: np.stack([e, e], axis=1)[:, 0],
    "list": lambda e: [int(x) for x in e],
}


@pytest.mark.parametrize("kind", sorted(_CONVERSIONS))
def test_converted_edge_arrays_outlive_the_constructor(kind):
    """A converted argument decodes exactly like the contiguous uint64 original, after the
    caller's objects are gone and the heap has been churned."""
    cap = transitions.demo_capture(8)
    assert int(cap.clock_edges[-1]) < 2**32          # representable as uint32
    ref = _summary(_decode(_source(cap, cap.clock_edges, cap.data_edges),
                           decode_audio=False))
    assert ref[1] > 0

    conv = _CONVERSIONS[kind]
    clock, data = conv(cap.clock_edges), conv(cap.data_edges)
    src = _source(cap, clock, data)
    del clock, data
    gc.collect()
    churn = [np.full(n, 0xFFFF_FFFF, dtype=np.uint64)       # reuses freed buffers
             for _ in range(8) for n in (cap.clock_edges.size, cap.data_edges.size)]
    got = _summary(_decode(src, decode_audio=False))
    del churn
    assert got == ref, f"{kind}: {got[:2]} != {ref[:2]}"


def test_contiguous_uint64_edges_are_held_not_copied():
    """The zero-copy path holds the caller's own arrays (one reference each, released with
    the source)."""
    cap = transitions.demo_capture(8)
    clock, data = cap.clock_edges, cap.data_edges
    before = (sys.getrefcount(clock), sys.getrefcount(data))
    src = _source(cap, clock, data)
    assert (sys.getrefcount(clock), sys.getrefcount(data)) == (before[0] + 1, before[1] + 1)
    del src
    gc.collect()
    assert (sys.getrefcount(clock), sys.getrefcount(data)) == before


# ---- audio reservation ---------------------------------------------------------------

def test_no_audio_reservation_without_audio_decode():
    cap = transitions.demo_capture(8)
    dec = _decode(_source(cap, cap.clock_edges, cap.data_edges), decode_audio=False)
    assert dec.audio_count == 0
    assert dec.audio_capacity == 0


def test_audio_reservation_is_capped():
    """A long capture whose first segment is two columns wide: the reservation is bounded
    by the cap (64 MiB of records), not by the row count."""
    n = 4_000_000                                    # 2M rows at 2 columns
    clock = np.arange(10, 10 * (n + 1), 10, dtype=np.uint64)
    data = np.empty(0, dtype=np.uint64)
    src = swi3score.TransitionSampleSource(clock, data, False, False, 100_000_000)
    dec = _decode(src, forced_column_count=2)
    rows = n // 2
    assert dec.audio_count == 0
    assert dec.audio_capacity < rows
    assert dec.audio_capacity * 72 <= 64 << 20


# ---- config CSV SampleSize width -----------------------------------------------------

def _csv(path, sample_size):
    """One enabled peripheral port (device 0, DP0, two channels) on a 16-column bus."""
    n = 12

    def row(k, v, rest="0"):
        return f"{k}," + ",".join([str(v)] + [rest] * (n - 1))

    lines = [
        "AppVersion,3.0.10", "NumColumns_REG,15", "SkippingDenominator_REG,1",
        "PHY3Enabled,False", "RowRate,3072.0",
        row("DeviceNumber_REG", 0), row("ManagerDataport", "False", "False"),
        row("EnableCh_REG", "0b11"), row("SampleSize_REG", sample_size),
        row("SampleGrouping_REG", 0), row("ChannelGrouping_REG", 0),
        row("Spacing_REG", 0), row("Interval_REG", 3), row("Offset_REG", 0),
        row("HorizontalStart_REG", 1), row("HorizontalCount_REG", 13),
        row("TailWidth_REG", 0), row("BitWidth_REG", 0), row("SkippingNumerator_REG", 0),
        row("PortDirection_REG", "False", "False"), row("SubRowInterval_REG", "False", "False"),
        row("FlowMode_REG", 0), row("PortMode_REG", 0),
        row("ScramblerEn_REG", "False", "False"), row("Enabled", "True", "False"),
    ]
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


@pytest.mark.parametrize("sample_size", [39, 70, -1])
def test_csv_sample_size_reads_at_register_width(sample_size):
    """SampleSize_REG past the 5-bit field decodes at (value & 0x1F) + 1 bits, the width
    registers_from_csv reports, and audio() / audio_columns() agree sample for sample."""
    width = (sample_size & 0x1F) + 1
    cap = transitions.demo_capture(64)
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "cfg.csv")
        _csv(path, sample_size)
        dec = _decode(_source(cap, cap.clock_edges, cap.data_edges),
                      config_csv_path=path, decode_audio=True)
    port = dec.config_dataports()["dataports"][0]
    assert port["sample_size"] + 1 == width
    audio = dec.audio()
    assert len(audio) > 20, "the port must decode samples"
    assert {a["sample_size"] for a in audio} == {width}
    cols = dec.audio_columns()
    assert list(cols["value"]) == [a["value"] for a in audio]
    assert all(a["value"] < (1 << width) for a in audio)


# ---- config CSV DeviceNumber bounds --------------------------------------------------

_FLOW_CSV = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures",
                         "flow_control_demo.csv")


def _csv_with_device(path, slot, device):
    """The flow-control demo CSV with data port `slot`'s DeviceNumber_REG set to `device`."""
    with open(_FLOW_CSV, encoding="utf-8") as f:
        lines = f.read().splitlines()
    for i, line in enumerate(lines):
        cells = line.split(",")
        if cells[0] == "DeviceNumber_REG":
            cells[slot + 1] = str(device)
            lines[i] = ",".join(cells)
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


@pytest.mark.parametrize("device", [12, 14, 31, 40, -3])
def test_csv_device_number_outside_the_bus_drops_the_port(device):
    slot = 6                                    # peripheral RX_CONTROLLED port, device 2
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "cfg.csv")
        _csv_with_device(path, slot, device)
        regs = swi3score.registers_from_csv(path)
        grid = swi3score.grid_from_csv(path, 32)
        levels = swi3score.make_demo_levels_flow_control(60)
        st = swi3score.DecoderSettings()
        st.config_csv_path = path
        dec = swi3score.Decoder(swi3score.MemorySampleSource(levels, 98_304_000, 4), st)
        dec.run()
    assert regs, "the other ports must still encode"
    assert {dev for dev, _, _ in regs} <= set(range(12))
    replay = swi3score.grid_from_registers([(dev, addr, val) for dev, addr, val in regs],
                                           16, 32, 0, False, 0)
    ports = lambda cells: {(c["device"], c["dp"]) for c in cells if c["dp"] >= 0}
    assert device not in {dev for dev, _ in ports(grid)}
    assert {p for p in ports(grid) if p[0] >= 0} == ports(replay)
    assert all(a["device"] != device for a in dec.audio())
    enabled = [p for p in dec.config_dataports()["dataports"] if p["enabled"]]
    assert all(p["device_number"] != device for p in enabled)


def test_grid_from_registers_ignores_a_write_to_no_device():
    regs = swi3score.registers_from_csv(_FLOW_CSV)
    base = swi3score.grid_from_registers(regs, 16, 32, 0, False, 0)
    stray = [(40, addr, val) for dev, addr, val in regs if dev == 2]
    assert stray
    assert swi3score.grid_from_registers(regs + stray, 16, 32, 0, False, 0) == base


# ---- forced column count bounds ------------------------------------------------------

@pytest.fixture(scope="module")
def mid_capture_levels():
    """A capture that starts past the two-column bring-up, so its auto-detected column
    count is neither bound of the forced range."""
    levels = swi3score.make_demo_levels(64)
    return levels[len(levels) // 2:]


def _summary_levels(levels, **settings):
    return _summary(_decode(swi3score.MemorySampleSource(levels, 98_304_000, 4),
                            decode_audio=False, **settings))


@pytest.mark.parametrize("forced", [-2, -1, 0, 1, 33, 40, 64])
def test_out_of_range_forced_column_count_auto_detects(mid_capture_levels, forced):
    auto = _summary_levels(mid_capture_levels)
    assert auto[0] not in (2, 32) and auto[1] > 0
    # The fixture tells auto-detection from a clamp to either bound.
    for bound in (2, 32):
        assert _summary_levels(mid_capture_levels, forced_column_count=bound) != auto
    got = _summary_levels(mid_capture_levels, forced_column_count=forced)
    assert got == auto


def test_in_range_forced_column_count_is_used():
    cap = transitions.demo_capture(8)
    dec = _decode(_source(cap, cap.clock_edges, cap.data_edges),
                  decode_audio=False, forced_column_count=16)
    assert dec.column_count == 16


# ---- DLV progress --------------------------------------------------------------------

def test_dlv_decode_reports_progress():
    s = Session.from_demo(48, phy=3)
    assert isinstance(s._source, swi3score.DlvSampleSource)
    dec = s.decoder
    assert dec.total_uis > 0
    assert dec.progress_uis <= dec.total_uis
    assert 0.95 <= dec.progress <= 1.0



# ---- a commands-only decode (decode_audio=False) ------------------------------------

def test_a_commands_only_decode_follows_a_numcolumns_commit():
    """The frame geometry follows a committed NumColumns whether or not audio is decoded:
    the commands and the column regions match an audio decode of the same capture."""
    cap = transitions.demo_capture(8)
    with_audio = _decode(_source(cap, cap.clock_edges, cap.data_edges))
    commands_only = _decode(_source(cap, cap.clock_edges, cap.data_edges), decode_audio=False)
    assert _summary(commands_only) == _summary(with_audio)
    assert ([seg["column_count"] for seg in commands_only.segments()]
            == [seg["column_count"] for seg in with_audio.segments()])
    assert len({seg["column_count"] for seg in commands_only.segments()}) > 1
