"""The decode core's handling of inputs outside the shapes the app itself produces.

- TransitionSampleSource and DlvSampleSource view their edge arrays without a copy; an
  argument that is not already C-contiguous uint64 (a narrower dtype, a strided view, a
  list) is converted, and the converted array must live as long as the source.
- The audio-output reservation run() makes up front is bounded, and absent when audio is
  not decoded.
- A config CSV's SampleSize_REG is a 5-bit field: the audio decode reads it at the width the
  register map and the grid read it, so audio() and audio_columns() carry the same values.
- A config CSV's DeviceNumber_REG outside 0..11 names no device: that port is disabled, so
  the audio decode, the grid and the register map all leave it out.
- DecoderSettings.forced_column_count outside [2, 32] auto-detects, as an out-of-range
  forced_column_sections entry does.
- A recovered-clock (DLV) decode reports a total UI count, so its progress is a fraction.
- Edge arrays of a signed or floating-point dtype are refused with TypeError.
- The recovered clock ends where a UI spans under one sample, takes the cold-start count for
  an initial column count outside 2..32, relocks across a stretch with no rising edge for
  more than 64 rows, and ends when no rising edge remains. These run in a child process
  with a time limit, so a clock that stops advancing fails the test instead of hanging it.
  At the limits, also in the child: a UI of one sample (seeded or guessed) locks and one
  under it ends; a next rising edge 64 row periods past the predicted Sync1 is coasted to,
  one 65 on is relocked to. Decoder.total_uis is documented as a refreshed estimate.
- A grid layout holds its rows to max_grid_rows and its column count to 2..32: rows,
  force_columns or fallback_columns however large lay out at most 32 columns, and a
  force_columns or fallback_columns below 2 (1, 0, negative) lays out the cold-start 2.
  A bit-sample window that starts past the capture's last UI is empty, read in the
  child process under its time limit.
"""
import gc
import json
import os
import subprocess
import sys
import tempfile
import time

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


# ---- DlvSampleSource argument conversion ---------------------------------------------

def _dlv_cap():
    cap = transitions.demo_capture(8, phy=3)
    assert int(cap.clock_edges[-1]) < 2**32          # representable as uint32
    return cap, cap.sample_rate_hz / (3_072_000.0 * 4)


def _dlv_source(cap, nominal, edges):
    return swi3score.DlvSampleSource(edges, bool(cap.initial_clock), int(cap.sample_rate_hz),
                                     4, nominal)


def _dlv_summary(src):
    dec = _decode(src, dlv=True, forced_column_count=4, decode_audio=False)
    return _summary(dec) + (list(src.recovered_row_syncs()),)


@pytest.mark.parametrize("kind", sorted(_CONVERSIONS) + ["uint64"])
def test_dlv_edges_outlive_the_caller(kind):
    """The DP-wire edges decode exactly like the caller's original after the caller's
    array is gone and the heap has been churned, whether viewed or converted."""
    cap, nominal = _dlv_cap()
    edges = cap.clock_edges.astype(np.uint64)
    ref = _dlv_summary(_dlv_source(cap, nominal, edges))
    assert ref[1] > 0

    arg = edges.copy() if kind == "uint64" else _CONVERSIONS[kind](edges)
    src = _dlv_source(cap, nominal, arg)
    del arg
    gc.collect()
    churn = [np.full(edges.size, 0xFFFF_FFFF, dtype=np.uint64) for _ in range(8)]
    got = _dlv_summary(src)
    del churn
    assert got == ref, f"{kind}: {got[:2]} != {ref[:2]}"


def test_contiguous_uint64_dlv_edges_are_held_not_copied():
    """The DLV source holds the caller's own array (one reference, released with the
    source)."""
    cap, nominal = _dlv_cap()
    edges = cap.clock_edges.astype(np.uint64)
    before = sys.getrefcount(edges)
    src = _dlv_source(cap, nominal, edges)
    assert sys.getrefcount(edges) == before + 1
    del src
    gc.collect()
    assert sys.getrefcount(edges) == before


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
    # The port is dropped, not masked into another device: against the unedited CSV the
    # grid and the audio lose exactly that port's stream and gain nothing.
    base_grid = ports(swi3score.grid_from_csv(_FLOW_CSV, 32))
    assert ports(grid) < base_grid and len(base_grid - ports(grid)) == 1
    st = swi3score.DecoderSettings()
    st.config_csv_path = _FLOW_CSV
    base = swi3score.Decoder(swi3score.MemorySampleSource(levels, 98_304_000, 4), st)
    base.run()
    streams = lambda dd: {(a["device"], a["dp"]) for a in dd.audio()}
    assert streams(dec) < streams(base) and len(streams(base) - streams(dec)) == 1


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


@pytest.mark.parametrize("dtype", [np.int64, np.float64])
@pytest.mark.parametrize("which", ["transition", "dlv"])
def test_signed_and_float_edge_arrays_are_refused(which, dtype):
    """The edge arrays convert only from what numpy casts to uint64 safely; a signed or
    floating-point array raises TypeError rather than being reinterpreted."""
    edges = np.array([1, 5, 9, 20], dtype=dtype)
    with pytest.raises(TypeError):
        if which == "dlv":
            swi3score.DlvSampleSource(edges, False, 1_000_000, 4, 0.0)
        else:
            swi3score.TransitionSampleSource(edges, edges, False, False, 1_000_000)


# ---- the recovered clock's limits ----------------------------------------------------

_BOUNDED_DLV = r"""
import json, sys
import numpy as np
import swi3score
from swi3s_studio.ingest import transitions

def run(edges, rate, cols, nominal):
    src = swi3score.DlvSampleSource(np.ascontiguousarray(edges, dtype=np.uint64), False,
                                    rate, cols, nominal)
    st = swi3score.DecoderSettings()
    st.dlv = True
    st.decode_audio = False
    st.forced_column_count = cols if 2 <= cols <= 32 else 0
    dec = swi3score.Decoder(src, st)
    dec.run()
    cmds = dec.commands()
    syncs = src.recovered_row_syncs()
    return {"uis": int(dec.progress_uis), "rows": int(len(syncs)),
            "last sync": int(syncs[-1]) if len(syncs) else None,
            "ui": float(src.recovered_ui_samples()),
            "valid": sum(1 for c in cmds if c["crc_valid"]),
            "names": [c["command"] for c in cmds]}

def dlv_rows(edges, nominal, cols=4):
    src = swi3score.DlvSampleSource(np.asarray(edges, dtype=np.uint64), False, 1_000_000,
                                    cols, nominal)
    st = swi3score.DecoderSettings()
    st.dlv, st.decode_audio, st.forced_column_count = True, False, cols
    swi3score.Decoder(src, st).run()
    return len(src.recovered_row_syncs())

def regular_rows(n, period, gap=(0, 0)):
    first, length = gap
    rows = [k for k in range(n) if not first < k < first + length]
    return [t for k in rows for t in (k * period + period, k * period + period + period // 2)]

case = sys.argv[1]
if case == "one sample":
    out = {"1.0": dlv_rows(regular_rows(400, 4), 1.0),
           "0.999": dlv_rows(regular_rows(400, 4), 0.999),
           "0.75": dlv_rows(regular_rows(400, 4), 0.75),
           "guessed 4": dlv_rows(regular_rows(400, 4), 0.0),
           "guessed 2": dlv_rows(regular_rows(400, 2), 0.0)}
elif case == "coast":
    out = {"base": dlv_rows(regular_rows(400, 16), 4.0),
           "64": dlv_rows(regular_rows(400, 16, (100, 65)), 4.0),
           "65": dlv_rows(regular_rows(400, 16, (100, 66)), 4.0)}
elif case == "window":
    from swi3s_studio.session import Session
    d = Session.from_demo(16).decoder
    n = d.total_uis
    out = {"inside": int(d.bit_samples_window(n // 2, n // 2 + 5000)["sample"].size),
           "past": [int(d.bit_samples_window(start, 2**64 - 1)["sample"].size)
                    for start in (n + 1, 2**63)]}
elif case == "slow":
    # A 1 MS/s capture: the 3.072 MRows/s row spans a third of a sample.
    edges = np.arange(1, 2001, dtype=np.uint64) * 3
    out = {"seeded": run(edges, 1_000_000, 4, 1e6 / (3.072e6 * 4))}
    for cols in (-1, 0, 1, 33, 2**31 - 1):
        out[f"cols {cols}"] = run(edges, 1_000_000, cols, 0.0)
else:
    cap = transitions.demo_capture(8, phy=3)
    assert not cap.initial_clock
    e = cap.clock_edges.astype(np.uint64)
    nominal = cap.sample_rate_hz / (3_072_000.0 * 4)
    h = e.size // 2 + (e.size // 2) % 2                  # an even index: a rising edge
    gapped = e.copy()
    gapped[h:] += np.uint64(20_000_000_000)
    assert e.size % 2 == 0                               # the capture ends low
    rising = np.append(e, np.uint64(2**64 - 2))          # a rising edge far past the end
    falling = e.copy()
    falling[-1] = np.uint64(2**64 - 2)                   # the last fall moved far past it
    out = {k: run(v, int(cap.sample_rate_hz), 4, nominal)
           for k, v in (("ref", e), ("gapped", gapped), ("rising", rising),
                        ("falling", falling))}
print(json.dumps(out))
"""


def _bounded_dlv(case: str) -> dict:
    """The results of _BOUNDED_DLV's `case`, in a child process given 60 s. The child
    runs in the directory of the core under test (swi3score.__file__), which it imports
    first, with the repository root after it for swi3s_studio."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    core = os.path.dirname(os.path.abspath(swi3score.__file__))
    env = dict(os.environ, PYTHONPATH=os.pathsep.join(
        p for p in (core, root, os.environ.get("PYTHONPATH", "")) if p))
    try:
        r = subprocess.run([sys.executable, "-c", _BOUNDED_DLV, case], capture_output=True,
                           text=True, timeout=60, env=env, cwd=core)
    except subprocess.TimeoutExpired:
        pytest.fail(f"the {case} case did not finish in 60 s")
    assert r.returncode == 0, r.stderr[-2000:]
    return json.loads(r.stdout.strip().splitlines()[-1])


@pytest.mark.platform
def test_a_dlv_clock_under_one_sample_per_ui_ends():
    """A seeded UI under one sample decodes nothing, and an initial column count outside
    2..32 (below 2 or above 32) takes the cold-start count (2), whose UI is half the first
    rising-edge spacing."""
    out = _bounded_dlv("slow")
    assert out["seeded"]["uis"] == 0 and out["seeded"]["rows"] == 0
    for cols in (-1, 0, 1, 33, 2**31 - 1):
        assert out[f"cols {cols}"]["ui"] == 3.0, cols
        assert out[f"cols {cols}"]["rows"] > 0, cols


@pytest.mark.platform
def test_a_dlv_gap_with_no_rising_edge_is_relocked_and_a_trailing_one_ends():
    """A 2e10-sample stretch with no edge before a Row Sync is relocked at that Row Sync, so
    the decode matches the capture without the stretch. A rising edge far past the end is
    relocked to as one more row, logged at that edge exactly, whose UIs lie past 2^64 and
    are not produced; a falling edge far past it, with no rising edge left, ends the source
    within 64 rows of the last rising edge."""
    out = _bounded_dlv("gap")
    ref = out["ref"]
    assert ref["valid"] > 0
    for k in ("gapped", "rising", "falling"):
        assert (out[k]["valid"], out[k]["names"]) == (ref["valid"], ref["names"]), k
    assert out["gapped"]["rows"] == ref["rows"]
    assert out["rising"]["rows"] == ref["rows"] + 1
    assert out["rising"]["last sync"] == 2**64 - 2
    assert out["rising"]["uis"] - ref["uis"] <= 4
    assert out["falling"]["rows"] <= ref["rows"]
    assert out["falling"]["uis"] <= ref["uis"] + 64 * 4


@pytest.mark.platform
def test_a_dlv_ui_of_one_sample_locks_and_one_under_it_ends():
    """Seeded and guessed from the first rising-edge spacing alike: a UI of one sample locks,
    one under it ends the source (400 rows of 4 samples, a rising edge opening each)."""
    assert _bounded_dlv("one sample") == {"1.0": 400, "0.999": 0, "0.75": 0,
                                          "guessed 4": 400,     # 4 samples / 4 columns
                                          "guessed 2": 0}       # 2 samples / 4 columns


@pytest.mark.platform
def test_a_dlv_gap_of_up_to_64_row_periods_coasts_and_a_longer_one_relocks():
    """Past the predicted Sync1, a next rising edge at most 64 row periods on is coasted to
    row by row (every row of the gap counted); one further on is relocked to directly, the
    gap holding no rows."""
    out = _bounded_dlv("coast")
    assert out["base"] == 400
    assert out["64"] == out["base"]                 # 64 periods on
    assert out["65"] == out["base"] - 65            # 65 periods on


# ---- DLV progress --------------------------------------------------------------------

def test_dlv_decode_reports_progress():
    s = Session.from_demo(48, phy=3)
    assert isinstance(s._source, swi3score.DlvSampleSource)
    dec = s.decoder
    assert dec.total_uis > 0
    assert dec.progress_uis <= dec.total_uis
    assert 0.95 <= dec.progress <= 1.0


def test_total_uis_is_refreshed_through_the_run():
    """`Decoder.total_uis` is re-read from the source during run() and at its end. The PHY3
    demo commits 4 -> 16 columns early, which quarters the recovered UI: read while the run
    is in flight, the total is the 16-column estimate, not the 4-column one run() started
    with. After the run it covers every UI counted."""
    import threading
    cap = transitions.demo_capture(16_000, phy=3)
    nominal = cap.sample_rate_hz / (3_072_000.0 * 4)
    src = _dlv_source(cap, nominal, cap.clock_edges)
    st = swi3score.DecoderSettings()
    st.dlv, st.decode_audio, st.forced_column_count = True, False, 4
    dec = swi3score.Decoder(src, st)
    at_start = int(cap.clock_edges[-1]) / nominal          # the 4-column estimate
    worker = threading.Thread(target=dec.run, daemon=True)
    worker.start()
    in_flight = []
    deadline = time.monotonic() + 120.0
    while worker.is_alive():
        if time.monotonic() > deadline:
            dec.cancel()
            worker.join(10.0)
            pytest.fail("the decode did not finish within 120 s")
        progress, total = dec.progress_uis, dec.total_uis
        if 200_000 < progress:
            in_flight.append(total)
        time.sleep(0.0005)
    worker.join()
    assert in_flight, "the run ended before a read in flight"
    assert min(in_flight) > 2 * at_start, (min(in_flight), at_start)
    assert dec.total_uis >= dec.progress_uis > 0
    demo = Session.from_demo(48, phy=3).decoder
    assert demo.total_uis >= demo.progress_uis > 0


def test_total_uis_is_documented_as_a_refreshed_estimate():
    doc = swi3score.Decoder.total_uis.__doc__ or ""
    assert "refreshed" in doc and "estimate" in doc
    assert "treat 0 as unknown" in doc


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


def test_a_grid_layout_holds_its_rows_and_columns():
    from swi3s_studio.model.bus_config import demo_config
    huge = 10**9
    limit = swi3score.max_grid_rows
    for cells in (swi3score.grid_from_registers([(0, 0x10, 1)], huge, huge, 0),
                  swi3score.grid_from_registers([(0, 0x10, 1)], 4, huge, huge),
                  swi3score.grid_from_commands([], huge, huge)):
        assert cells and max(c["row"] for c in cells) < limit
        assert max(c["col"] for c in cells) < swi3score.max_column_count
    with tempfile.TemporaryDirectory() as d:
        path = demo_config().to_csv_file(os.path.join(d, "demo.csv"))
        cells = swi3score.grid_from_csv(path, huge)
        writes = [w for w in swi3score.registers_from_csv(path)
                  if w[1] not in (0x1081, 0x10C1)]          # NumColumns left unwritten
    assert max(c["row"] for c in cells) == limit - 1

    # The DLV layout places S0 in the last column, so its width shows in the cells.
    def regs(fallback, force):
        return swi3score.grid_from_registers(writes, fallback_columns=fallback, rows=4,
                                             force_columns=force, dlv=True,
                                             cds_horizontal_start=1)

    def cmds(force):
        return swi3score.grid_from_commands([], 4, force, dlv=True, cds_horizontal_start=1)

    two = regs(2, 0)
    assert max(c["col"] for c in two) == 1
    assert max(c["col"] for c in regs(3, 0)) == 2
    for low in (1, 0, -5):
        assert regs(low, 0) == two, low
        assert regs(0, low) == two, low
        assert regs(low, low) == two, low
        assert cmds(low) == cmds(2), low
    assert max(c["col"] for c in cmds(2)) == 1


@pytest.mark.platform
def test_a_bit_sample_window_past_the_capture_is_empty():
    out = _bounded_dlv("window")
    assert out["inside"] > 0
    assert out["past"] == [0, 0]


def test_a_skipping_ratio_that_needs_more_than_the_interval_bound_raises():
    """The skipping fixture's stream is bounded in intervals, not only in samples: a ratio
    that multiplies the intervals a sample count needs past 4 * max_demo_samples throws
    before any level is built."""
    with pytest.raises(ValueError, match="make_skipping_levels: 48000 samples .* above 192000"):
        swi3score.make_skipping_levels(48_000, 4094, 4095)
    assert swi3score.make_skipping_levels(200, 13, 160)


@pytest.mark.parametrize("numerator,denominator", [(4096 + 13, 160), (13, 4096 + 160),
                                                   (4096, 4095), (2**31 - 1, 160)])
def test_a_skipping_ratio_wider_than_its_registers_raises(numerator, denominator):
    """The skipping fixture writes SkippingNumerator and SkippingDenominator to 12-bit
    registers, so a value above 4095 is refused rather than sized for and sent truncated."""
    with pytest.raises(ValueError, match="make_skipping_levels: numerator .* must fit their "
                                         "12-bit registers"):
        swi3score.make_skipping_levels(200, numerator, denominator)
    assert swi3score.make_skipping_levels(200, 4095 if numerator > denominator else 13,
                                          min(denominator, 4095))


@pytest.mark.parametrize("interval", [4096, 5000])
def test_an_enablech_curr_interval_wider_than_its_register_raises(interval):
    """The interval is written to the 12-bit Interval register, so the stream it sizes is
    bounded by 4095."""
    with pytest.raises(ValueError, match=f"interval {interval} does not fit the 12-bit "
                                         "Interval register"):
        swi3score.make_enablech_curr_write_levels(8, interval)
    assert swi3score.make_enablech_curr_write_levels(8, 4095)


@pytest.mark.parametrize("samples,interval", [(751, 4095), (48000, 4095), (48000, 64)])
def test_an_enablech_curr_stream_longer_than_its_bound_raises(samples, interval):
    """samples_per_channel x (interval + 1) Rows per phase is bounded by 48000 samples at a
    64-row Interval, so a long stream at a wide interval is refused rather than built."""
    with pytest.raises(ValueError, match=f"samples_per_channel {samples} at interval "
                                         f"{interval} needs .* Rows per phase, above 3072000"):
        swi3score.make_enablech_curr_write_levels(samples, interval)
