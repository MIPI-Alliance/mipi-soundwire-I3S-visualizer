"""Decoder.run() framing, resync and windowed-replay rules, and the native demo fixtures.

- A same-width resync (a comma re-hunt after the command-confidence limit) keeps the
  payload engine in step with the bus: the audio after it equals the undisturbed decode.
- A resync whose new column count configures nothing records no config epoch, so the
  windowed bit-sample replay agrees with the live decode.
- A commit that changes no data port rebuilds no port, scrambler overrides included: the
  other port's sample indices run on, and the overridden port decodes as without the commit.
- A clock edge at the largest sample value decodes without a crash.
- A known column count framed with no CRC-valid command in the window is reported as a
  guessed framing (guessed_framing_samples); one validated by a single command is not, and
  an empty leading window that stops at a restart point records none.
- A resync or a clock resume into a pinned section searches the pinned count's Column-0
  phase (one CRC-valid command places it); a pinned width the window does not carry is
  reported as a guessed framing.
- A DLV section-0 column pin frames the leading window too (uniform Row Sync spacing).
- A DLV CDS column outside 1..columns-2 reads at the register's reset value; one inside it
  is read there.
- The blind detector finds every even column count up to 32; a command whose CRC fails
  does not place Column 0, so a known count framed over such commands is a guess; on a tie
  between Column-0 phases the first even offset is taken; a config CSV keeps its column
  count across a clock resume.
- run() runs once per Decoder, also when the first run read no UI; a non-positive grid
  row count lays out no rows.
- Decoder.cancel() from another thread ends an in-flight run() promptly, and
  cancelled says so; request_stop() ends it the same way and sets stopped instead. A
  cancel before run() makes it return without reading the source; a completed run is
  not cancelled, whatever is asked after it.
- The single-event fixtures keep Pinging and decode clean, both on a long run and at the
  smallest samples_per_channel that holds their event block; a DSCR fixture too short to
  place its DSCR before its commit point
  raises, and from the smallest that holds it each further sample moves the DSCR one
  interval; the demo sine is correct at SampleSize 31; the PHY3 demo rejects a CDS column on
  a port.

Every input is synthetic.
"""
import math
import os
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import numpy as np
import pytest
import swi3score
from test_native_command_parser import _bus

from swi3s_studio.ingest import transitions
from swi3s_studio.session import Session

_RATE, _SPU = 98_304_000, 4


def _decode_levels(levels, settings=None):
    d = swi3score.Decoder(swi3score.MemorySampleSource(levels, _RATE, _SPU),
                          settings or swi3score.DecoderSettings())
    d.run()
    return d


def _sine(channel: int, index: int, size: int) -> int:
    """Demo.cpp sineSample(channel, index, size): `size` is the SampleSize field (bits - 1)."""
    x = (1 << size) * 0.45 * math.sin(2.0 * math.pi * (channel + 1) * index / 64.0)
    v = math.floor(x + 0.5) if x >= 0 else -math.floor(-x + 0.5)
    return v & ((1 << (size + 1)) - 1)


# ---- resync ---------------------------------------------------------------------------

def test_same_width_resync_keeps_the_payload_engine_in_step():
    levels = list(swi3score.make_demo_levels(3000))
    clean = _decode_levels(levels)
    seg = clean.segments()[-1]
    assert seg["column_count"] == 16
    # Hold the Column-0 CDS level flat for 15000 rows of the 16-column region, payload
    # untouched: more than 8192 rows pass with no CRC-valid command.
    held = list(levels)
    for row in range(seg["row_base"] + 18000, seg["row_base"] + 33000):
        ui = seg["start_ui"] + (row - seg["row_base"]) * 16
        held[ui - 1] = held[ui - 2]
    d = _decode_levels(held)
    resyncs = [g for g in d.segments() if g["start_ui"] > seg["start_ui"]]
    assert resyncs and all(g["column_count"] == 16 for g in resyncs), d.segments()
    after = resyncs[0]["start_sample"]

    def keyed(dec):
        return {(a["dp"], a["channel"], a["start_sample"]): a["value"]
                for a in dec.audio() if a["start_sample"] > after}

    want, got = keyed(clean), keyed(d)
    assert len(want) > 1000
    assert got == want


def test_a_resync_that_configures_nothing_records_no_epoch():
    """Cold restart at Safe-Lock 2 on a 16-column wire: the registers reset, the ports
    stop, and the watchdog later re-detects 16 columns with nothing to configure. The
    windowed replay shows no port bits where the live decode decoded none."""
    full = Session.from_demo(1500, phy=2, cold_start=False)
    cap = full.capture
    ce = np.asarray(cap.clock_edges, dtype=np.int64)
    ui0 = int(full.segments[-1]["start_ui"]) + 16 * 5000
    restart = int(ce[ui0])
    st = swi3score.DecoderSettings()
    st.restart_points = [(restart, 2, True, int(ce[ui0 - 100]))]
    st.collect_bit_samples = True
    d = swi3score.Decoder(cap.sample_source(), st)
    d.run()
    last = d.segments()[-1]
    assert last["column_count"] == 16 and not last["restart"], d.segments()
    live = np.asarray(d.bit_samples()["sample"], dtype=np.int64)
    assert not np.any(live > restart)
    window = d.bit_samples_window(int(last["start_ui"]) + 1000, int(last["start_ui"]) + 5000)
    assert len(window["sample"]) == 0


def test_a_commit_that_changes_no_port_leaves_the_ports_running_under_an_override():
    """A second SSCR (the setup SSCR's CDS bits replayed mid-audio, interval-aligned)
    commits nothing new. Forcing dp0's scrambler off must not rebuild dp1 there: dp1
    decodes the same with and without the dp0 override."""
    levels = list(swi3score.make_dscr_disable_levels(64))
    clean = _decode_levels(levels)
    s0, s1 = clean.segments()
    sscr = next(c for c in clean.commands() if c["command"] == "SSCR")
    first, ssp = sscr["bus_row"], sscr["effective_row"]
    length = ssp - sscr["row_delay"] - first          # through the CONFIRM_COMMIT bit
    start = ssp + 2400
    start += (first - start) % 64                     # its SSP lands on an interval start
    spliced = list(levels)
    for r in range(length):
        src = s0["start_ui"] + (first + r - s0["row_base"]) * 2
        bit = levels[src - 1] == levels[src - 2]      # NRZS: same level -> 1
        dst = s1["start_ui"] + (start + r - s1["row_base"]) * 8
        spliced[dst - 1] = spliced[dst - 2] if bit else not spliced[dst - 2]

    def port(dp, overrides, lv=spliced):
        st = swi3score.DecoderSettings()
        st.scrambler_overrides = overrides
        d = _decode_levels(lv, st)
        if lv is spliced:
            assert [c["bus_row"] for c in d.commands() if c["command"] == "SSCR"] == \
                [first, start]
        return [(a["index"], a["start_sample"], a["value"])
                for a in sorted(d.audio(), key=lambda a: a["end_sample"]) if a["dp"] == dp]

    plain = port(1, [])
    assert [i for i, _s, _v in plain] == list(range(64))   # no restart at the second SSCR
    assert port(1, [(0, 0, 0)]) == plain
    assert port(0, [(0, 0, 0)]) == port(0, [(0, 0, 0)], levels)


# ---- a pinned section's Column-0 phase ---------------------------------------------------

def _one_command_window():
    """make_demo_levels(3000) with the 16-column segment's CDS held flat over its rows
    18000..52000 except one Ping's: a window there holds one CRC-valid command, too few for
    the blind detector (two) and enough for a known count's phase search (one). Returns the
    held levels, the clean decode's segments and the kept Ping's row in its segment."""
    levels = list(swi3score.make_demo_levels(3000))
    clean = _decode_levels(levels)
    segs = clean.segments()
    seg = segs[-1]
    assert seg["column_count"] == 16
    keep = next(c for c in clean.commands() if c["command"] == "Ping" and c["crc_valid"]
                and 32_000 < c["bus_row"] - seg["row_base"] < 34_000)
    k0 = keep["bus_row"] - seg["row_base"]
    rows = (keep["end_sample"] - keep["start_sample"]) // (16 * _SPU) + 2
    held = list(levels)
    for rel in range(18_000, 52_000):
        if not k0 - 2 <= rel <= k0 + rows:
            ui = seg["start_ui"] + rel * 16
            held[ui - 1] = held[ui - 2]
    return held, segs, k0


def _pings_after(d, sample):
    return [c["start_sample"] for c in d.commands()
            if c["command"] == "Ping" and c["crc_valid"] and c["start_sample"] >= sample]


def test_a_resync_into_a_pinned_section_searches_the_pinned_count_s_phase():
    """The command-confidence resync opens a section pinned to 16 columns over a window
    with one CRC-valid command: the pinned count's Column-0 phase is searched there, so
    the command decodes and the framing is not a guess. Unpinned, the blind detector reads
    nothing and the command is lost; pinned to a width the wire does not carry, the
    framing is reported as guessed."""
    held, segs, k0 = _one_command_window()
    seg = segs[-1]
    kept_from = (seg["start_ui"] + (k0 - 2) * 16) * _SPU

    def decode(pins):
        st = swi3score.DecoderSettings()
        st.forced_column_sections = pins
        return _decode_levels(held, st)

    def kept(d):
        return [x for x in _pings_after(d, kept_from) if x < kept_from + 300 * 16 * _SPU]

    def guessed(d):
        return [x for x in d.guessed_framing_samples() if x >= seg["start_sample"]]

    assert kept(decode([])) == []
    pinned = decode([(len(segs), 16)])
    assert len(kept(pinned)) == 1 and guessed(pinned) == []
    assert guessed(decode([(len(segs), 8)]))


def test_a_resume_into_a_pinned_section_searches_the_pinned_count_s_phase():
    """A clock pause inside the 2-column cold-start segment, after which a 16-column wire
    resumes, a window holding one CRC-valid command: with the section the resume opens
    pinned to 16 the pinned count's Column-0 phase is searched and the command decodes;
    unpinned nothing decodes; pinned to a width the wire does not carry, the framing is
    reported as guessed."""
    held, segs, k0 = _one_command_window()
    join = 6_000                                   # inside the 2-column segment
    src0 = segs[-1]["start_ui"] - 1 + (k0 - 3_000) * 16 + 6
    levels = list(swi3score.make_demo_levels(3000))[:join] + held[src0:src0 + 20_000 * 16]

    def decode(pins):
        st = swi3score.DecoderSettings()
        st.resume_points = [join * _SPU]
        st.forced_column_sections = pins
        return _decode_levels(levels, st)

    assert _pings_after(decode([]), join * _SPU) == []
    assert len(_pings_after(decode([(1, 16)]), join * _SPU)) == 1
    wrong = decode([(1, 8)])
    assert _pings_after(wrong, join * _SPU) == []
    assert [x for x in wrong.guessed_framing_samples() if x >= join * _SPU]


def test_an_empty_leading_window_at_a_restart_point_records_no_guessed_framing():
    """A restart point on the first UI after the alignment edge, with a forced count: the
    leading window holds no UI, so the decode begins at the restart and no framing guess
    is recorded for it."""
    st = swi3score.DecoderSettings()
    st.forced_column_count = 2
    st.restart_points = [(6, 2, True, 0)]
    d = _decode_levels(list(swi3score.make_demo_levels(32)), st)
    assert d.segments()[0]["restart"]
    assert list(d.guessed_framing_samples()) == []


# ---- source edge values ---------------------------------------------------------------

@pytest.mark.parametrize("edges", ["[M] * 3", "list(range(10, 1010, 10)) + [M]"])
def test_an_edge_at_the_largest_sample_decodes_without_a_crash(edges):
    code = textwrap.dedent(f"""
        import numpy as np, swi3score
        M = 2**64 - 1
        clk = np.array({edges}, dtype=np.uint64)
        src = swi3score.TransitionSampleSource(clk, np.array([], dtype=np.uint64),
                                               True, False, 1_000_000_000)
        swi3score.Decoder(src, swi3score.DecoderSettings()).run()
    """)
    root = Path(swi3score.__file__).resolve().parent     # the module under test
    env = dict(os.environ, PYTHONPATH=os.pathsep.join(
        p for p in (str(root), os.environ.get("PYTHONPATH", "")) if p))
    try:
        r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                           cwd=root, env=env, timeout=60)
    except subprocess.TimeoutExpired:
        pytest.fail("the decode of an edge at the largest sample did not end within 60 s")
    assert r.returncode == 0, (r.returncode, r.stderr[-500:])


# ---- DLV ------------------------------------------------------------------------------

def test_a_dlv_section_zero_pin_frames_the_leading_window():
    s = Session.from_demo(48, phy=3, cold_start=True)
    s.force_column_count_at(int(s.segments[0]["start_sample"]) + 100, 8)
    rs = np.asarray(s.decoder.row_sync_samples(), dtype=np.int64)
    period = np.diff(rs)
    assert period.size > 300
    assert period.max() < 1.5 * np.median(period), period[:8]


def _dlv_valid_commands(cds_column):
    """The CRC-valid commands of the 4-column PHY3 demo decoded with CDS_HorizontalStart
    `cds_column`."""
    cap = transitions.demo_capture(100, phy=3)
    nominal = cap.sample_rate_hz / (3_072_000.0 * 4)
    src = swi3score.DlvSampleSource(cap.clock_edges.astype(np.uint64),
                                    bool(cap.initial_clock), int(cap.sample_rate_hz),
                                    4, nominal)
    st = swi3score.DecoderSettings()
    st.dlv = True
    st.cds_horizontal_start = cds_column
    st.forced_column_count = 4
    d = swi3score.Decoder(src, st)
    d.run()
    return [c for c in d.commands() if c["crc_valid"]]


@pytest.mark.parametrize("cds", [0, 3, 20, -1])
def test_a_dlv_cds_column_off_the_row_reads_at_the_reset_column(cds):
    def commands(cds_column):
        return [(c["command"], c["bus_row"]) for c in _dlv_valid_commands(cds_column)]

    reset = commands(2)
    assert reset
    assert commands(cds) == reset


def test_a_dlv_cds_column_in_the_row_is_read_there():
    """The contrast to the reset rule: an in-range CDS column other than the reset value
    moves the CDS read, so the demo (CDS at column 2) decodes no CRC-valid command at 1."""
    assert _dlv_valid_commands(2)
    assert _dlv_valid_commands(1) == []


# ---- API edges ------------------------------------------------------------------------

def test_run_runs_once_per_decoder():
    d = _decode_levels(swi3score.make_demo_levels(32))
    n = len(d.commands())
    with pytest.raises(RuntimeError, match="runs once"):
        d.run()
    assert len(d.commands()) == n
    assert d.audio_sample_rates()


@pytest.mark.parametrize("first", ["empty", "cancelled"])
def test_run_runs_once_when_the_first_run_fed_nothing(first):
    """A first run that read no UI (an empty source, or a cancel before run()) still
    counts: a second call raises."""
    if first == "empty":
        d = swi3score.Decoder(swi3score.MemorySampleSource([], _RATE, _SPU),
                              swi3score.DecoderSettings())
    else:
        d = _endless_dlv_decoder()
        d.cancel()
    d.run()
    assert d.progress_uis == 0
    with pytest.raises(RuntimeError, match="runs once"):
        d.run()


def _endless_dlv_decoder():
    """A recovered-clock decode of ~4e8 UIs: a Row Sync pulse every 60 rows of 32 UIs, which
    the DLL coasts between, so the run lasts seconds unless ended."""
    row = 32 * 10
    starts = np.arange(200_000, dtype=np.uint64) * np.uint64(60 * row) + np.uint64(100)
    edges = np.empty(2 * starts.size, dtype=np.uint64)
    edges[0::2], edges[1::2] = starts, starts + np.uint64(10)
    src = swi3score.DlvSampleSource(edges, False, 100_000_000, 32, 10.0)
    st = swi3score.DecoderSettings()
    st.dlv = True
    st.forced_column_count = 32
    st.decode_audio = False
    return swi3score.Decoder(src, st)


@pytest.mark.parametrize("how", ["cancel", "request_stop"])
def test_a_run_ended_from_another_thread_returns_promptly(how):
    """The run polls the request every few thousand UIs, so it returns long before its
    end; the join is bounded generously for a loaded machine."""
    d = _endless_dlv_decoder()
    worker = threading.Thread(target=d.run, daemon=True)
    worker.start()
    deadline = time.monotonic() + 5.0
    while d.progress_uis == 0 and time.monotonic() < deadline:
        time.sleep(0.001)
    assert d.progress_uis > 0
    getattr(d, how)()
    worker.join(5.0)
    assert not worker.is_alive()
    assert (d.cancelled, d.stopped) == ((True, False) if how == "cancel" else (False, True))
    assert 0 < d.progress_uis < d.total_uis // 2


def test_a_cancel_before_run_returns_without_reading_the_source():
    d = _endless_dlv_decoder()
    d.cancel()
    d.run()
    assert d.cancelled and not d.stopped
    assert d.progress_uis == 0
    assert d.commands() == []


def test_a_completed_run_is_not_cancelled():
    d = _decode_levels(swi3score.make_demo_levels(32))
    assert not d.cancelled and not d.stopped
    d.cancel()
    assert not d.cancelled


@pytest.mark.parametrize("rows", [0, -1])
def test_a_non_positive_grid_row_count_lays_out_no_rows(rows):
    assert swi3score.grid_from_registers([], 8, rows) == []


# ---- demo fixtures --------------------------------------------------------------------

_FIXTURES = {
    "dscr_disable": lambda n: swi3score.make_dscr_disable_levels(n),
    "dscr_disable_all": lambda n: swi3score.make_dscr_disable_levels(n, disable_all=True),
    "dscr_enable": lambda n: swi3score.make_dscr_enable_levels(n),
    "dscr_enable_channel": lambda n: swi3score.make_dscr_enable_levels(n, enable_channel=True),
    "immediate_scrambler": lambda n: swi3score.make_immediate_scrambler_levels(n),
    "immediate_scrambler_mid": lambda n: swi3score.make_immediate_scrambler_levels(
        n, mid_interval=True),
    "immediate_sample_size": lambda n: swi3score.make_immediate_sample_size_levels(n),
    "enablech_curr": lambda n: swi3score.make_enablech_curr_write_levels(n, 63),
    "running_port_write": lambda n: swi3score.make_running_port_write_levels(n, 0x0B, 0),
}


def _smallest(make, most=64):
    """The smallest samples_per_channel up to `most` that `make` builds a fixture at (a
    DSCR fixture raises below the count that holds its block); none fails the test."""
    for n in range(1, most + 1):
        try:
            make(n)
            return n
        except ValueError:
            pass
    pytest.fail(f"no samples_per_channel up to {most} builds the fixture")


@pytest.mark.parametrize("length", ["long", "smallest"])
@pytest.mark.parametrize("name", sorted(_FIXTURES))
def test_a_long_fixture_decodes_clean(name, length):
    make = _FIXTURES[name]
    d = _decode_levels(make(400 if length == "long" else _smallest(make)))
    assert [g["column_count"] for g in d.segments()] == [2, 8]
    cmds = d.commands()
    assert all(c["crc_valid"] for c in cmds)
    assert sum(c["command"] == "Ping" for c in cmds) > 10   # keep-alive Pings throughout


@pytest.mark.parametrize("make", [
    lambda n: swi3score.make_dscr_disable_levels(n),
    lambda n: swi3score.make_dscr_disable_levels(n, disable_all=True),
    lambda n: swi3score.make_dscr_enable_levels(n),
    lambda n: swi3score.make_dscr_enable_levels(n, enable_channel=True),
    lambda n: swi3score.make_dscr_channel_set_levels(n, 0x3, 0x1, 0),
])
@pytest.mark.parametrize("count", [0, 3, 8])
def test_a_dscr_fixture_too_short_for_its_block_raises(make, count):
    """The message names the samples_per_channel the caller passed."""
    with pytest.raises(ValueError,
                       match=f"samples_per_channel {count} leaves no room for the event block"):
        make(count)


def test_the_dscr_lands_one_interval_later_per_sample():
    """From the smallest sample count that holds the DSCR block, each further sample moves
    the DSCR one 64-row interval later, as its documented commit point does."""
    rows = []
    for n in (14, 15, 16):
        d = _decode_levels(swi3score.make_dscr_channel_set_levels(n, 0x3, 0x1, 0))
        [row] = [c["bus_row"] for c in d.commands() if c["command"] == "DSCR"]
        rows.append(row)
    assert np.diff(rows).tolist() == [64, 64]
    with pytest.raises(ValueError):
        swi3score.make_dscr_channel_set_levels(13, 0x3, 0x1, 0)


def test_a_long_dscr_disable_keeps_dp0_uninterrupted():
    n = 400
    d = _decode_levels(swi3score.make_dscr_disable_levels(n))
    dp0 = sorted((a for a in d.audio() if a["dp"] == 0), key=lambda a: a["index"])
    assert len(dp0) == 2 * n
    wrong = [a["index"] for a in dp0 if a["value"] != _sine(0, a["index"], 15)]
    assert not wrong, wrong[:10]


def test_the_demo_sine_is_correct_at_sample_size_31():
    d = _decode_levels(swi3score.make_immediate_sample_size_levels(16, 31, 31, 8))
    dp0 = [a for a in d.audio() if a["dp"] == 0]
    assert len(dp0) == 32
    wrong = [(a["index"], hex(a["value"])) for a in dp0 if a["value"] != _sine(0, a["index"], 31)]
    assert not wrong, wrong[:5]


@pytest.mark.parametrize("cds", [0, 4, 7, 9, 15])
def test_the_phy3_demo_rejects_a_cds_column_off_the_row_or_on_a_port(cds):
    with pytest.raises(ValueError, match="cds_column"):
        swi3score.make_demo_levels_phy3(20, cds_column=cds)


@pytest.mark.parametrize("cds", [2, 3, 12])
def test_the_phy3_demo_accepts_a_free_cds_column(cds):
    assert len(swi3score.make_demo_levels_phy3(20, cds_column=cds)["levels"]) > 0


def test_a_known_count_framed_without_a_valid_command_is_reported_as_guessed():
    def framed(levels, forced):
        st = swi3score.DecoderSettings()
        st.forced_column_count = forced
        return list(_decode_levels(levels, st).guessed_framing_samples())

    assert framed(list(swi3score.make_demo_levels(8)), 2) == []
    rng = np.random.default_rng(1)
    noise = [bool(b) for b in rng.integers(0, 2, 50_000)]
    guessed = framed(noise, 8)
    assert len(guessed) == 1 and guessed[0] < 8 * _SPU


def test_one_valid_command_in_the_window_confirms_the_framing():
    """A 4-column stream whose Column-0 CDS carries `n` Pings and idle spacers, the other
    columns holding the level: one CRC-valid command is enough to place Column 0, none is
    a guess."""
    from test_native_command_parser import _idle, _ping

    def framed(n):
        cds = _idle(4)
        for _ in range(n):
            cds += _ping() + _idle(2)
        levels = _bus(4, {0: cds + _idle(4)})
        st = swi3score.DecoderSettings()
        st.forced_column_count, st.decode_audio = 4, False
        d = _decode_levels(levels, st)
        return sum(c["crc_valid"] for c in d.commands()), len(d.guessed_framing_samples())

    assert framed(0) == (0, 1)
    assert framed(1) == (1, 0)


# ---- column detection -------------------------------------------------------------------

def _cds(phases):
    from test_native_command_parser import _idle
    out = _idle(4)
    for p in phases:
        out += p + _idle(2)
    return out + _idle(4)


def _pings(n, *, corrupt=False):
    """`n` Pings; `corrupt` replaces each one's first CRC byte after the CRC is computed."""
    from test_native_command_parser import PING, SPACER, _bits, _header, _ping, _tok
    if not corrupt:
        return [_ping() for _ in range(n)]
    syms = _header(PING, 0xFFF, [0x00])
    syms[8] = swi3score.encode_codeword(0x5A)                  # comma, 6 header, 1 packet
    ping = [b for s in [*syms, SPACER] for b in _bits(s)] + [b for _ in range(12)
                                                             for b in _bits(_tok(0))]
    return [ping] * n


def _framing(levels, forced=0):
    st = swi3score.DecoderSettings()
    st.forced_column_count, st.decode_audio = forced, False
    return _decode_levels(levels, st)


@pytest.mark.parametrize("cols", range(2, 33, 2))
def test_the_column_count_is_detected_up_to_the_widest(cols):
    d = _framing(_bus(cols, {0: _cds(_pings(3))}))
    assert d.column_count == cols
    assert sum(c["crc_valid"] for c in d.commands()) == 3


def test_a_command_whose_crc_fails_does_not_place_column_0():
    """A forced 4-column count over Pings whose CRC fails: no CRC-valid command places
    Column 0, so the framing is a guess; the same Pings intact place it."""
    for corrupt, guesses in ((False, 0), (True, 1)):
        d = _framing(_bus(4, {0: _cds(_pings(3, corrupt=corrupt))}), forced=4)
        assert len(d.guessed_framing_samples()) == guesses, corrupt


def test_on_a_tie_between_phases_the_first_even_offset_is_column_0():
    """Columns 0 and 2 of a 4-column bus each carry three CRC-valid commands, Pings on one
    and WriteA32s on the other: both phases score alike, and the first (offset 0) is taken
    as Column 0, by the blind detector and by a known count's phase search."""
    from test_native_command_parser import _write
    for forced in (0, 4):
        for col0, col2, name in ((_pings(3), [_write()] * 3, "Ping"),
                                 ([_write()] * 3, _pings(3), "WriteA32")):
            d = _framing(_bus(4, {0: _cds(col0), 2: _cds(col2)}), forced)
            assert d.column_count == 4
            assert {c["command"] for c in d.commands() if c["crc_valid"]} == {name}, forced


def test_a_config_csv_keeps_its_column_count_across_a_clock_resume(tmp_path):
    """A clock pause after which a 16-column wire resumes: unpinned, the resume adopts the
    16 it detects and opens a section; with a config CSV of 8 columns the decode keeps 8."""
    import csv
    import io

    from swi3s_studio.model.bus_config import demo_config
    levels = list(swi3score.make_demo_levels(3000))
    segs = _decode_levels(levels).segments()
    join = 6_000                                   # inside the 2-column segment
    src0 = segs[-1]["start_ui"] - 1 + 33_000 * 16 + 6
    resumed = levels[:join] + levels[src0:src0 + 20_000 * 16]
    rows = list(csv.reader(io.StringIO(demo_config().to_csv())))
    for r in rows:
        if r and r[0] == "NumColumns_REG":
            r[1] = "7"
    path = tmp_path / "eight.csv"
    path.write_text("\n".join(",".join(r) for r in rows) + "\n", encoding="utf-8")

    def counts(csv_path):
        st = swi3score.DecoderSettings()
        st.resume_points = [join * _SPU]
        st.config_csv_path = csv_path
        return [g["column_count"] for g in _decode_levels(resumed, st).segments()]
    assert counts("")[-1] == 16
    assert counts(str(path)) == [8]
