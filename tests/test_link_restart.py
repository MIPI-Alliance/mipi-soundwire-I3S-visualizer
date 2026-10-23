"""A Link that goes inactive and is brought up again: the decode restarts at each bring-up.

decode_link_controls finds every bring-up after a stopped clock, whether or not the capture
began with one. The core restarts the decode at each (DecoderSettings.restart_points): from
the clock stop to the restart nothing is decoded; at the restart the decode re-aligns, frames
at the Safe-Lock count, reconfigures the payload engine on the new Column-0 phase and opens a
segment flagged `restart` (and `cold_start` for a Cold Start, which also resets the register
model). The register and grid replays start from the last Cold Start's Bus Reset. On DLV the
recovered clock locks again at each bring-up's first Row Sync (DlvSampleSource::Reacquire)
and is not run through the stretch.

Every capture here is synthetic: demos joined with the clock stopped between them, so each
later part decodes exactly as the same demo alone. The first part is shorter than the
decoder's 262144-UI leading window, so the restart has to be taken inside a window read.
"""
import numpy as np
import pytest
import swi3score

from swi3s_studio.analysis import link_control
from swi3s_studio.analysis.link_control import (
    LinkControlResult,
    clock_resumes,
    decode_link_control,
    decode_link_controls,
)
from swi3s_studio.ingest import transitions
from swi3s_studio.ingest.capture import Capture
from swi3s_studio.model import Provenance
from swi3s_studio.session import Session

_GAP_S = 0.005


def _level_after(edges, initial) -> bool:
    return bool(initial) ^ bool(edges.size & 1)


def _chain(parts, gaps):
    """The captures in `parts`, each after the one before with the clock stopped (both lines
    held at the next part's idle levels) for the matching entry of `gaps` samples. Returns
    the capture and the sample each part starts at."""
    a = parts[0]
    clk, dat = [a.clock_edges.astype(np.int64)], [a.data_edges.astype(np.int64)]
    lvc = _level_after(a.clock_edges, a.initial_clock)
    lvd = _level_after(a.data_edges, a.initial_data)
    end = int(max(a.clock_edges[-1], a.data_edges[-1]))
    shifts = [0]
    for b, gap in zip(parts[1:], gaps):
        if lvc != b.initial_clock:
            clk.append(np.array([end + 10]))
        if lvd != b.initial_data:
            dat.append(np.array([end + 20]))
        shift = end + 20 + int(gap)
        clk.append(b.clock_edges.astype(np.int64) + shift)
        dat.append(b.data_edges.astype(np.int64) + shift)
        shifts.append(shift)
        lvc = _level_after(b.clock_edges, b.initial_clock)
        lvd = _level_after(b.data_edges, b.initial_data)
        end = int(max(b.clock_edges[-1], b.data_edges[-1])) + shift
    cap = Capture(np.concatenate(clk).astype(np.uint64), np.concatenate(dat).astype(np.uint64),
                  a.initial_clock, a.initial_data, a.sample_rate_hz)
    return cap, shifts


def _relinked(a, b, gap_samples):
    """`a`, then the clock stopped at `b`'s idle levels for `gap_samples`, then `b`."""
    cap, shifts = _chain([a, b], [gap_samples])
    return cap, shifts[1]


def _cold(phy=2, variant=""):
    return transitions.demo_capture(300, cold_start=True, phy=phy, variant=variant)


def _plain(phy=2, variant=""):
    return transitions.demo_capture(300, cold_start=False, phy=phy, variant=variant)


def _us(cap, us):
    return int(us * cap.sample_rate_hz / 1e6)


def _warm_prefixed(b):
    """`b` (no bring-up of its own) after a Warm Start: a 460 µs DP-high pulse, then
    Man_tPhyStart low."""
    rise = _us(b, 50)
    fall = rise + _us(b, 460)
    start = fall + _us(b, 80)
    assert not b.initial_clock
    clk = np.concatenate([[rise, fall], b.clock_edges.astype(np.int64) + start])
    dat = b.data_edges.astype(np.int64) + start
    return Capture(clk.astype(np.uint64), dat.astype(np.uint64), False, b.initial_data,
                   b.sample_rate_hz)


def _lc_on_the_data_line(b, phy=2):
    """`b` after a Cold Start whose link control rides the capture's DATA line, as a real
    analyzer records FBCSE: its clock channel is DN, which carries the forwarded clock in
    audio mode and the PHY number during link control; its data channel is DP, the
    link-control clock."""
    dp, dn, end, dn_end = transitions.cold_start_edges(phy, b.sample_rate_hz)
    clk, dat = [dn.astype(np.int64)], [dp.astype(np.int64)]
    if dn_end != bool(b.initial_clock):
        clk.append(np.array([end]))
    if bool(b.initial_data):
        dat.append(np.array([end]))
    clk.append(b.clock_edges.astype(np.int64) + end)
    dat.append(b.data_edges.astype(np.int64) + end)
    return Capture(np.concatenate(clk).astype(np.uint64), np.concatenate(dat).astype(np.uint64),
                   False, False, b.sample_rate_hz)


def _session(cap):
    return Session(cap, source={"type": "test"})


@pytest.fixture(scope="module")
def two():
    """(the relinked session, the second demo alone, the sample the second half starts)."""
    a, b = _cold(), _cold()
    cap, shift = _relinked(a, b, int(_GAP_S * a.sample_rate_hz))
    return _session(cap), _session(b), shift


def _after(session, shift):
    return [c for c in session.commands if int(c["start_sample"]) >= shift]


def _signature(commands):
    return [(c["command"], c["crc_valid"]) for c in commands]


def _written(session, sample):
    files = session.register_files_at(sample)
    return {(dev, a): f.value(a) for dev, f in files.items() for a, _v in f.items()
            if f.provenance(a) == Provenance.WRITTEN}


def _decode(cap, restarts, *, forced=2, csv="", bits=False, dlv=False, source=None):
    st = swi3score.DecoderSettings()
    st.forced_column_count = forced
    st.config_csv_path = csv
    st.collect_bit_samples = bits
    st.restart_points = restarts
    if dlv:
        st.dlv = True
        st.cds_horizontal_start = 2
    src = source if source is not None else cap.sample_source()
    d = swi3score.Decoder(src, st)
    d.run()
    return d


def _stop_and_restart(cap, shift):
    """(the last clock edge before `shift`, the first one at or after it)."""
    ce = np.asarray(cap.clock_edges, dtype=np.int64)
    i = int(np.searchsorted(ce, shift))
    return int(ce[i - 1]), int(ce[i])


# ---- finding the bring-ups --------------------------------------------------------------

def test_every_bring_up_is_found(two):
    s, alone, shift = two
    found = decode_link_controls(s.capture)
    assert [r.sequence for r in found] == ["cold", "cold"]
    assert found[1].phy_name == "PHY2" and found[1].safe_lock_columns == 2
    assert found[1].audio_start_sample == shift + alone.link_control.audio_start_sample
    assert found[1].bus_reset_sample == shift + alone.link_control.bus_reset_sample
    assert found[1].sections[0]["name"] == "Idle"          # the stretch the clock stopped for
    stop, _restart = _stop_and_restart(s.capture, shift)
    assert found[1].clock_stop_sample == stop == found[1].sections[0]["start"]
    assert found[1].sections[0]["start"] < shift < found[1].sections[0]["end"]


def test_three_bring_ups_each_restart_the_decode():
    parts = [_cold(), _cold(), _cold()]
    cap, shifts = _chain(parts, [int(_GAP_S * parts[0].sample_rate_hz)] * 2)
    s, alone = _session(cap), _session(_cold())
    assert [r.sequence for r in s.link_controls] == ["cold"] * 3
    assert [g["cold_start"] for g in s.segments].count(True) == 2
    for lo, hi in zip(shifts, shifts[1:] + [1 << 62]):
        part = [c for c in s.commands if lo <= int(c["start_sample"]) < hi]
        assert _signature(part) == _signature(alone.commands)


@pytest.mark.parametrize("gap_us", [0, 300])
def test_a_short_idle_before_a_later_cold_start(gap_us):
    """Without an idle of LINK_IDLE_MIN_US the only long clock stop is the Bus Reset's LC:10
    pulse itself; the bring-up is still found where it is, and the Link is inactive from its
    last audio-mode edge, before the LC_Ack and LC:00 edges that precede the LC:10."""
    a, b = _cold(), _cold()
    cap, shift = _relinked(a, b, _us(a, gap_us))
    found = decode_link_controls(cap)
    alone = decode_link_control(b)
    assert [r.sequence for r in found] == ["cold", "cold"]
    assert found[1].bus_reset_sample == shift + alone.bus_reset_sample
    assert found[1].audio_start_sample == shift + alone.audio_start_sample
    ce = np.asarray(cap.clock_edges, dtype=np.int64)
    last_audio = int(ce[ce < shift + int(b.clock_edges[0])][-1])
    assert found[1].clock_stop_sample == last_audio
    assert found[1].sections[0]["name"] == "Idle"
    assert found[1].sections[0]["start"] == last_audio
    assert _signature(_after(_session(cap), shift)) == _signature(_session(b).commands)


def test_link_control_on_the_data_line():
    """The orientation a real analyzer records: the link-control clock on the data line."""
    a, b = _lc_on_the_data_line(_plain()), _lc_on_the_data_line(_plain())
    cap, shift = _relinked(a, b, int(_GAP_S * a.sample_rate_hz))
    found = decode_link_controls(cap)
    alone = decode_link_control(b)
    assert alone.sequence == "cold" and alone.lc_on_data_line
    assert [(r.sequence, r.phy_name, r.lc_on_data_line) for r in found] == [
        ("cold", "PHY2", True)] * 2
    assert found[1].audio_start_sample == shift + alone.audio_start_sample
    after = _after(_session(cap), shift)
    assert sum(c["crc_valid"] for c in after) > 0
    assert _signature(after) == _signature(_session(b).commands)


def test_a_capture_that_starts_in_audio_mode_still_restarts():
    """A plain demo (no bring-up), the clock stopped, then a cold-start demo: the decode
    restarts at the Cold Start, and the cursor, the Timeline and the decode agree."""
    a, b = _plain(), _cold()
    cap, shift = _relinked(a, b, int(_GAP_S * a.sample_rate_hz))
    s = _session(cap)
    assert s.link_control.sequence == "none"
    assert [r.sequence for r in s.link_controls] == ["none", "cold"]
    assert len(s._restart_points()) == 1
    assert [g["cold_start"] for g in s.segments].count(True) == 1
    assert _signature(_after(s, shift)) == _signature(_session(b).commands)
    assert s.bringup_at(shift - 100) is s.link_controls[1]
    assert s.bringup_at(0) is None
    assert s.phy_at(0) is None and s.phy_at(int(cap.clock_edges[-1])) == "PHY2"


def test_no_bring_up_anywhere_restarts_nothing():
    plain = _session(_plain())
    assert [r.sequence for r in plain.link_controls] == ["none"]
    assert plain._restart_points() == []
    assert not any(g["restart"] or g["cold_start"] for g in plain.segments)


@pytest.mark.parametrize("phy,variant", [(1, ""), (2, "flow_control")])
def test_audio_after_a_stop_is_not_a_bring_up(phy, variant):
    """A cold-start demo, the clock stopped, then a demo with no bring-up of its own: the
    second part's long audio pulses do not read as a later Cold or Warm Start."""
    a, b = _cold(phy, variant), _plain(phy, variant)
    cap, shift = _relinked(a, b, int(_GAP_S * a.sample_rate_hz))
    assert len(decode_link_controls(cap)) == 1
    s = _session(cap)
    assert len(s.link_controls) == 1 and s._restart_points() == []
    assert not any(g["restart"] for g in s.segments)
    assert s.bringup_at(shift + 1000) is None


@pytest.mark.parametrize("phy,variant", [(1, ""), (2, ""), (2, "flow_control")])
def test_audio_resumed_out_of_phase_after_a_pause_is_framed_afresh(phy, variant):
    """Audio after a clock pause with no bring-up, on another phase and geometry (here a
    second demo joined on, as a dropout or an exported join leaves it): the decode detects
    its column count and Column-0 phase there, as at the start of a capture that begins
    mid-stream, in a segment that is no restart, and decodes it as that demo alone."""
    a, b = _cold(phy, variant), _plain(phy, variant)
    cap, shift = _relinked(a, b, int(_GAP_S * a.sample_rate_hz))
    s = _session(cap)
    assert clock_resumes(cap, s.link_controls) == [shift + int(b.clock_edges[0])]
    assert _signature(_after(s, shift)) == _signature(_session(b).commands)
    later = [g for g in s.segments if int(g["start_sample"]) >= shift]
    assert [g["column_count"] for g in later] == [
        g["column_count"] for g in _session(b).segments]
    assert not any(g["restart"] or g["cold_start"] for g in s.segments)


def test_a_clean_pause_decodes_on_as_if_nothing_happened():
    """A clock that only paused: the bus carries on in phase, so the decode goes on through
    the pause in the same framing, every command, segment and audio sample as without it."""
    a = _cold()
    ce = np.asarray(a.clock_edges, dtype=np.int64)
    de = np.asarray(a.data_edges, dtype=np.int64)
    whole = _session(a)
    cut = int(ce[np.searchsorted(ce, int(whole.segments[-1]["start_sample"]) + 200_000)])
    pause = int(_GAP_S * a.sample_rate_hz)
    cap = Capture(np.where(ce > cut, ce + pause, ce).astype(np.uint64),
                  np.where(de > cut, de + pause, de).astype(np.uint64),
                  a.initial_clock, a.initial_data, a.sample_rate_hz)
    s = _session(cap)
    assert len(clock_resumes(cap, s.link_controls)) == 1
    assert _signature(s.commands) == _signature(whole.commands)
    assert [g["column_count"] for g in s.segments] == [
        g["column_count"] for g in whole.segments]
    assert len(s.audio_columns()["start_sample"]) == len(whole.audio_columns()["start_sample"])


def test_no_resume_inside_a_start_sequence():
    """The stops a bring-up makes itself (its Bus Reset's LC:10, the stretch the clock
    stopped for before a later one) are not pauses."""
    a, b = _cold(), _cold()
    for gap_us in (0, 5000):
        cap, _shift = _relinked(a, b, _us(a, gap_us))
        s = _session(cap)
        assert len(s.link_controls) == 2 and clock_resumes(cap, s.link_controls) == []


def test_a_bursty_line_is_not_a_link():
    """Bursts of edges with millisecond pauses between: every pause reads like a stop, but
    no audio-mode clock follows any of them."""
    rate = 500_000_000
    bursts = np.arange(0, 2 * rate, int(1.5e-3 * rate))
    clk = (bursts[:, None] + np.arange(50)[None, :] * 100).ravel().astype(np.uint64)
    cap = Capture(clk, (bursts + 7).astype(np.uint64), False, False, rate)
    assert len(decode_link_controls(cap)) == 1


def test_the_gap_search_reads_the_edges_in_chunks(monkeypatch):
    rng = np.random.default_rng(7)
    edges = np.cumsum(rng.integers(1, 100, 1000)).astype(np.uint64)
    want = np.flatnonzero(np.diff(edges.astype(np.int64)) > 90)
    monkeypatch.setattr(link_control, "_GAP_CHUNK", 7)
    assert np.array_equal(link_control._clock_stops(edges, 90), want)
    assert link_control._clock_stops(edges[:1], 90).size == 0


def test_a_detected_warm_start_keeps_the_registers():
    a, b = _cold(), _warm_prefixed(_plain())
    cap, shift = _relinked(a, b, int(_GAP_S * a.sample_rate_hz))
    s = _session(cap)
    later = s.link_controls[1]
    assert later.sequence == "warm" and later.phy_name == "PHY2"
    assert "reused from the bring-up before it" in later.note
    restart = next(g for g in s.segments if g["restart"])
    assert not any(g["cold_start"] for g in s.segments)
    stop = int(later.clock_stop_sample)
    assert _written(s, int(restart["start_sample"]) + 1) == _written(s, stop) != {}
    assert _signature(_after(s, shift)) == _signature(_session(_plain()).commands)


def test_an_unrecognised_phy_names_none():
    """A later Cold Start does not inherit the PHY before it: it selected one."""
    a, b = _cold(), transitions.prepend_cold_start(_plain(), phy_number=5)
    cap, _shift = _relinked(a, b, int(_GAP_S * a.sample_rate_hz))
    later = decode_link_controls(cap)[1]
    assert later.sequence == "cold" and later.phy_number == 5
    assert later.phy_name is None and later.safe_lock_columns is None


# ---- the decode at a restart -------------------------------------------------------------

def test_the_decode_restarts_at_the_second_cold_start(two):
    s, alone, shift = two
    later = [g for g in s.segments if int(g["start_sample"]) >= shift]
    assert later and later[0]["restart"] and later[0]["cold_start"]
    assert later[0]["column_count"] == 2
    assert [g["column_count"] for g in later] == [g["column_count"] for g in alone.segments]
    assert [g["cold_start"] for g in s.segments].count(True) == 1
    after = _after(s, shift)
    assert _signature(after) == _signature(alone.commands), (
        "the second half did not decode as the same demo alone")
    assert sum(c["crc_valid"] for c in after) > 0


def test_the_restart_is_the_first_audio_edge_after_phystart(two):
    """Man_tPhyStart is 64–96 µs: the restart is where audio mode is seen to begin."""
    s, _alone, _shift = two
    later = s.link_controls[1]
    ce = np.asarray(s.capture.clock_edges, dtype=np.int64)
    first = int(ce[np.searchsorted(ce, int(later.phystart_sample), side="right")])
    [(restart, cols, cold, inactive_from)] = s._restart_points()
    assert restart == first != later.audio_start_sample
    assert (cols, cold, inactive_from) == (2, True, int(later.clock_stop_sample) + 1)


def test_nothing_decodes_while_the_link_is_inactive(two):
    s, _alone, _shift = two
    [(restart, _c, _k, inactive_from)] = s._restart_points()
    st = np.asarray(s.audio_columns()["start_sample"], dtype=np.int64)
    assert not np.any((st >= inactive_from) & (st < restart))
    assert not [c for c in s.commands if inactive_from <= int(c["start_sample"]) < restart]
    d = _decode(s.capture, s._restart_points(), bits=True)
    bits = np.asarray(d.bit_samples()["sample"], dtype=np.int64)
    assert bits.size and not np.any((bits >= inactive_from) & (bits < restart))
    # The windowed reconstruction agrees: the ports stop where the Link went inactive.
    ce = np.asarray(s.capture.clock_edges, dtype=np.int64)
    lo, hi = int(np.searchsorted(ce, inactive_from)), int(np.searchsorted(ce, restart))
    window = np.asarray(d.bit_samples_window(lo - 2000, hi + 2000)["sample"], dtype=np.int64)
    live = bits[(bits >= ce[lo - 2000]) & (bits <= ce[hi + 2000])]
    assert np.array_equal(np.sort(window), np.sort(live))


def test_a_warm_restart_puts_unchanged_ports_on_the_new_phase():
    """The same configuration on both sides of a Warm Start, the second part cut at a
    different row phase: its ports must move to the new Column-0 phase with the framing."""
    full = Session.from_demo(300, phy=2, cold_start=True)
    a = full.capture
    t0 = int(full.segments[-1]["start_sample"]) + 20000         # inside the 16-column region
    b = a.subcapture(t0, int(a.clock_edges[-1]))
    cap, shift = _relinked(a, b, int(_GAP_S * a.sample_rate_hz))
    stop, restart = _stop_and_restart(cap, shift)
    d = _decode(cap, [(restart, 16, False, stop + 1)], bits=True)
    ce = np.asarray(cap.clock_edges, dtype=np.int64)
    bits = d.bit_samples()
    smp, dp = np.asarray(bits["sample"], dtype=np.int64), np.asarray(bits["dp"])
    ui = np.searchsorted(ce, smp)

    def columns(seg, hi):
        m = (smp >= int(seg["start_sample"])) & (smp < hi) & (dp == 2)
        return sorted(set(((ui[m] - int(seg["start_ui"])) % 16).tolist()))
    segs = d.segments()
    before, after = segs[-2], segs[-1]
    assert after["restart"] and not after["cold_start"] and after["column_count"] == 16
    assert columns(after, 1 << 62) == columns(before, int(after["start_sample"])) != []


def test_restart_points_merge_and_validate(two):
    s, _alone, _shift = two
    [(r, _c, _k, inactive)] = s._restart_points()

    def segs(points):
        return [(g["column_count"], g["restart"], g["cold_start"])
                for g in _decode(s.capture, points).segments()]
    base = segs([(r, 2, True, inactive)])
    assert base[3] == (2, True, True)
    # One sample, two points: one bring-up, a Cold Start if either is.
    assert segs([(r, 2, True, inactive), (r, 2, False, inactive)]) == base
    assert segs([(r, 2, False, inactive), (r, 2, True, inactive)]) == base
    # Within one UI: the first restart's window is empty, and its Cold Start still counts.
    assert segs([(r, 2, True, inactive), (r + 1, 2, False, inactive)]) == base
    # Any order.
    assert segs([(r + 1_000_000, 2, True, 0), (r, 2, True, inactive)]) == segs(
        [(r, 2, True, inactive), (r + 1_000_000, 2, True, 0)])
    # A column count outside 2..32: a Cold Start frames at the cold-start count, a Warm
    # Start keeps the current one.
    for cols in (64, 33, 1, 0, -5):
        assert segs([(r, cols, True, inactive)]) == base
    assert segs([(r, 0, False, inactive)])[3] == (16, True, False)


def test_a_restart_just_after_the_first_edge_still_decodes():
    """A restart point within the first window's first UIs: the decode begins there."""
    a = _cold()
    ce = np.asarray(a.clock_edges, dtype=np.int64)
    rising = [int(e) for i, e in enumerate(ce) if bool(a.initial_clock) ^ bool((i + 1) & 1)]
    d = _decode(a, [(rising[0] + 1, 2, True, 0)])
    assert d.segments() and sum(c["crc_valid"] for c in d.commands()) > 0


# ---- DLV: the recovered clock locks again -----------------------------------------------

@pytest.fixture(scope="module")
def two_dlv():
    """`two` on PHY3: the DLL locks again at the second bring-up's first Row Sync."""
    a, b = _cold(3), _cold(3)
    cap, shift = _relinked(a, b, int(_GAP_S * a.sample_rate_hz))
    return _session(cap), _session(b), shift


def test_a_dlv_decode_restarts_at_the_second_cold_start(two_dlv):
    s, alone, shift = two_dlv
    assert s.is_dlv and [r.phy_name for r in s.link_controls] == ["PHY3", "PHY3"]
    [(restart, cols, cold, _inactive)] = s._restart_points()
    assert (cols, cold) == (4, True)
    later = [g for g in s.segments if int(g["start_sample"]) >= shift]
    assert later[0]["restart"] and later[0]["cold_start"]
    assert [(g["column_count"], int(g["start_sample"]) - shift) for g in later] == [
        (g["column_count"], int(g["start_sample"])) for g in alone.segments]
    after = _after(s, shift)
    assert _signature(after) == _signature(alone.commands), (
        "the second half did not decode as the same demo alone")
    assert sum(c["crc_valid"] for c in after) > 0
    mine, theirs = s.audio_columns(), alone.audio_columns()
    m = np.asarray(mine["start_sample"], dtype=np.int64) >= shift
    assert np.array_equal(np.asarray(mine["value"])[m], np.asarray(theirs["value"]))
    assert np.array_equal(np.asarray(mine["start_sample"], dtype=np.int64)[m] - shift,
                          np.asarray(theirs["start_sample"], dtype=np.int64))


def test_a_dlv_restart_numbers_its_rows_by_their_row_syncs(two_dlv):
    """A DLV bus row is the index of its Row Sync Point, so the row the stop cut short keeps
    its number: every command after the restart is on the row the cursor finds it in, and
    the restart's first row opens on the bring-up's first Row Sync."""
    s, alone, shift = two_dlv
    after = _after(s, shift)
    assert [s.bus_row_for_sample(int(c["start_sample"])) for c in after] == [
        c["bus_row"] for c in after]
    seg = next(g for g in s.segments if g["restart"])
    assert s._sample_at_bus_row(int(seg["row_base"])) == s._restart_points()[0][0]
    offset = int(seg["row_base"]) - int(alone.segments[0]["row_base"])
    assert [c["bus_row"] - offset for c in after] == [c["bus_row"] for c in alone.commands]


def test_nothing_decodes_while_a_dlv_link_is_inactive(two_dlv):
    s, _alone, _shift = two_dlv
    [(restart, _c, _k, inactive_from)] = s._restart_points()
    st = np.asarray(s.audio_columns()["start_sample"], dtype=np.int64)
    assert not np.any((st >= inactive_from) & (st < restart))
    assert not [c for c in s.commands if inactive_from <= int(c["start_sample"]) < restart]
    d = _decode(s.capture, s._restart_points(), forced=4, bits=True, dlv=True,
                source=s._make_dlv_source())
    bits = np.asarray(d.bit_samples()["sample"], dtype=np.int64)
    assert bits.size and not np.any((bits >= inactive_from) & (bits < restart))


def _stopped_mid_row():
    """A PHY3 demo whose Link stops five UIs into a 16-column row, while its ports run."""
    full = Session.from_demo(300, phy=3, cold_start=True)
    rsp = full._recovered_row_syncs()
    ui = float(np.median(np.diff(rsp))) / 16
    ce = np.asarray(full.capture.clock_edges, dtype=np.int64)
    edges = ce[ce > int(full.segments[-1]["start_sample"]) + 200_000]
    opens = rsp[np.searchsorted(rsp, edges, side="right") - 1]
    last = int(edges[np.flatnonzero((edges - opens) / ui >= 5)[0]])
    return full.capture.subcapture(0, last + 1)


def test_the_dlv_bit_sample_replay_follows_the_restart():
    """A Link stopped mid-row while its ports ran, then brought up again: the windowed
    bit-sample replay follows the recovered clock's row log across the stretch and the new
    session's first commit, where its ports begin. From the row the stop cut short on it is
    the live decode exactly (that row keeps the UI it had and ends where the re-acquired one
    begins, which takes the UI it locked with); further back to a sample, where a seek
    restores a row's UI from its logged span rather than the loop's running estimate. The
    analyzer's clock runs 3% off nominal, so the UI the loop tracks is not the one it is
    seeded with."""
    a = _stopped_mid_row()
    joined, _shift = _relinked(a, _cold(3), int(_GAP_S * a.sample_rate_hz))

    def slow(edges):
        return np.round(np.asarray(edges, dtype=np.float64) * 1.03).astype(np.uint64)
    cap = Capture(slow(joined.clock_edges), slow(joined.data_edges), joined.initial_clock,
                  joined.initial_data, joined.sample_rate_hz)
    s = _session(cap)
    [(restart, _c, _k, inactive_from)] = s._restart_points()
    d = _decode(cap, s._restart_points(), forced=4, bits=True, dlv=True,
                source=s._make_dlv_source())
    bits = np.asarray(d.bit_samples()["sample"], dtype=np.int64)
    segs = d.segments()
    i = next(i for i, g in enumerate(segs) if g["restart"])
    old, new = segs[i - 1], segs[i]
    cols = int(old["column_count"])
    # Column 0 of the row the stop cut short: the row before the restart's (start_ui is
    # the Column-2 CDS UI).
    cut = int(old["start_ui"]) - 2 + (int(new["row_base"]) - 1 - int(old["row_base"])) * cols
    hi = int(segs[i + 1]["start_ui"]) + 2000

    def replay(lo):
        window = np.sort(np.asarray(d.bit_samples_window(lo, hi)["sample"], dtype=np.int64))
        return window, np.sort(bits[(bits >= window[0] - 1) & (bits <= window[-1] + 1)])
    assert 0 < int(new["start_ui"]) - 2 - cut < cols          # the stop cut the row short
    window, live = replay(cut)
    assert np.any(window < inactive_from) and np.any(window > restart)
    assert np.array_equal(window, live)
    window, live = replay(cut - 2000)
    assert window.size == live.size
    assert np.array_equal(window[window >= restart], live[live >= restart])
    assert np.abs(window - live).max() <= 1


def test_a_dlv_restart_frames_at_the_safe_lock_count_under_a_config_csv(tmp_path):
    """DLV frames its first bring-up at the Safe-Lock count with a config CSV too, and so
    every later one: the source subdivides each row from there."""
    from swi3s_studio.model.bus_config import BusConfig
    full = Session.from_demo(300, phy=3, cold_start=True)
    cfg, _n = BusConfig.from_decoder_config(
        full.config_dataports_at(int(full.segments[-1]["start_sample"]) + 60000))
    path = str(tmp_path / "config.csv")
    cfg.to_csv_file(path)
    a, b = _cold(3), _cold(3)
    cap, shift = _relinked(a, b, int(_GAP_S * a.sample_rate_hz))
    s = Session(cap, source={"type": "test"}, config_csv=path)
    alone = Session(b, source={"type": "test"}, config_csv=path)
    seg = next(g for g in s.segments if g["restart"])
    assert seg["column_count"] == s.segments[0]["column_count"] == 4
    after = _after(s, shift)
    assert sum(c["crc_valid"] for c in after) > 0
    assert _signature(after) == _signature(alone.commands)


def test_the_cds_symbols_after_a_dlv_restart(two_dlv):
    """The CDS pane seeks the recovered clock's row log into the restarted session."""
    s, alone, shift = two_dlv
    after = _after(s, shift)
    offset = int(after[0]["bus_row"]) - int(alone.commands[0]["bus_row"])
    for c in (after[0], after[len(after) // 2]):
        mine = s.symbols_around(int(c["start_sample"]), 64)
        theirs = alone.symbols_around(int(c["start_sample"]) - shift, 64)
        assert mine and [(x["kind"], x["value"], int(x["start_sample"]) - shift,
                          x["row"] - offset) for x in mine] == [
            (x["kind"], x["value"], int(x["start_sample"]), x["row"]) for x in theirs]


def test_a_dlv_warm_start_keeps_the_registers():
    a, b = _cold(3), _warm_prefixed(_plain(3))
    cap, shift = _relinked(a, b, int(_GAP_S * a.sample_rate_hz))
    s = _session(cap)
    later = s.link_controls[1]
    assert later.sequence == "warm" and later.phy_name == "PHY3"
    restart = next(g for g in s.segments if g["restart"])
    assert not restart["cold_start"] and restart["column_count"] == 4
    stop = int(later.clock_stop_sample)
    assert _written(s, int(restart["start_sample"]) + 1) == _written(s, stop) != {}
    assert _signature(_after(s, shift)) == _signature(_session(_cold(3)).commands)


def test_a_sample_in_a_dlv_stretch_maps_to_the_restart(two_dlv):
    """The stretch has no UIs, so every sample in it maps to the restart's: the sample-to-UI
    mapping stays monotonic, and a Capture-pane window reaching into the stretch replays no
    UI past the restart."""
    s, _alone, _shift = two_dlv
    [(restart, _c, _k, inactive_from)] = s._restart_points()
    start = int(next(g for g in s.segments if g["restart"])["start_ui"])
    samples = np.linspace(inactive_from - 10_000, restart + 10_000, 400).astype(np.int64)
    uis = np.array([s._ui_for_sample(int(x)) for x in samples])
    assert np.all(np.diff(uis) >= 0)
    assert uis[(samples >= inactive_from) & (samples < restart)].max() <= start
    assert s._port_bit_window_uis(inactive_from - 1000, inactive_from + 1_000_000)[1] <= start


def test_a_dlv_restart_skips_the_stretch(two_dlv):
    """The recovered clock is not run through the stopped stretch: the decode consumes no
    UI from the stop to the restart, so the restart's Column 0 follows the stop within a
    row, at any gap."""
    s, _alone, _shift = two_dlv
    segs = s.segments
    i = next(i for i, g in enumerate(segs) if g["restart"])
    old, new = segs[i - 1], segs[i]
    rows_before = (int(new["row_base"]) - int(old["row_base"])) * int(old["column_count"])
    assert int(new["start_ui"]) - int(old["start_ui"]) <= rows_before + 2 * int(old["column_count"])


@pytest.mark.parametrize("first,second", [(2, 3), (3, 2)])
def test_a_bring_up_of_the_other_phy_family_is_not_a_restart(first, second):
    """Forwarded clock to DLV or back would switch the sample source mid-decode, and the UI
    index every sample maps through with it: the decode runs on through such a bring-up."""
    a, b = _cold(first), _cold(second)
    cap, _shift = _relinked(a, b, int(_GAP_S * a.sample_rate_hz))
    assert [r.phy_name for r in decode_link_controls(cap)] == [f"PHY{first}", f"PHY{second}"]
    s = _session(cap)
    assert len(s.link_controls) == 1 and s._restart_points() == []
    assert not any(g["restart"] for g in s.segments)


# ---- what the decode contradicts ---------------------------------------------------------

def test_a_paused_clock_is_not_a_restart(monkeypatch):
    """A clock that only paused (the bus resumes on the same framing) and a Warm Start
    detected there: the decode with it finds no CRC-valid command after it, the decode
    without it does, so it is dropped."""
    import swi3s_studio.session as session_mod
    a = _cold()
    ce = np.asarray(a.clock_edges, dtype=np.int64)
    seg16 = int(_session(a).segments[-1]["start_sample"])
    cut = int(ce[np.searchsorted(ce, seg16 + 200_000)])
    pause = int(_GAP_S * a.sample_rate_hz)
    de = np.asarray(a.data_edges, dtype=np.int64)
    clk = np.where(ce > cut, ce + pause, ce).astype(np.uint64)
    dat = np.where(de > cut, de + pause, de).astype(np.uint64)
    cap = Capture(clk, dat, a.initial_clock, a.initial_data, a.sample_rate_hz)
    resume = int(clk[np.searchsorted(clk, cut, side="right")])
    phantom = LinkControlResult(
        sequence="warm", phy_name="PHY2", phy_kind="FBCSE-fast", safe_lock_columns=2,
        phystart_sample=resume + 1000, audio_start_sample=resume + 2000,
        clock_stop_sample=cut,
        sections=[{"name": "Idle", "start": cut, "end": resume},
                  {"name": "Warm Start", "start": resume, "end": resume + 1000},
                  {"name": "PhyStart", "start": resume + 1000, "end": resume + 2000}])
    monkeypatch.setattr(session_mod, "decode_link_controls",
                        lambda c: [decode_link_control(c), phantom])
    s = _session(cap)
    assert len(s.link_controls) == 1 and s._restart_points() == []
    assert not any(g["restart"] for g in s.segments)
    assert sum(c["crc_valid"] for c in s.commands if int(c["start_sample"]) > resume) > 0


# ---- registers, Bus Grid and cursor ------------------------------------------------------

def test_the_registers_reset_at_the_bus_reset(two):
    s, alone, shift = two
    later = s.link_controls[1]
    reset = next(int(sec["start"]) for sec in later.sections if sec["name"] == "Bus Reset")
    assert _written(s, reset - 1), "nothing was written before the reset: nothing to test"
    assert _written(s, reset) == {}, "the first session's writes outlived the bus reset"
    # The restart row: its Row Sync Point is in the new section, whose state is the reset.
    seg = next(g for g in s.segments if g["cold_start"])
    rsp = s._sample_at_bus_row(int(seg["row_base"]))
    assert s.segment_index_for_sample(rsp) == s.segments.index(seg)
    assert _written(s, rsp) == {}
    end = int(s.capture.clock_edges[-1])
    assert _written(s, end) == _written(alone, end - shift)


def test_a_config_csv_keeps_its_ports_through_a_cold_start(tmp_path):
    """A config CSV is the capture's configuration through every bring-up: the restart
    keeps its column count and ports, and its baseline seeds the registers after the
    reset too."""
    from swi3s_studio.model.bus_config import BusConfig
    full = Session.from_demo(3000, phy=2, cold_start=True)
    cfg, _n = BusConfig.from_decoder_config(
        full.config_dataports_at(int(full.segments[-1]["start_sample"]) + 60000))
    path = str(tmp_path / "config.csv")
    cfg.to_csv_file(path)
    b = full.capture
    cap, shift = _relinked(b, b, int(_GAP_S * b.sample_rate_hz))
    s = Session(cap, source={"type": "test"}, config_csv=path)
    alone = Session(b, source={"type": "test"}, config_csv=path)
    seg = next(g for g in s.segments if g["restart"])
    assert seg["cold_start"] and seg["column_count"] == alone.segments[0]["column_count"] == 16
    st = np.asarray(s.audio_columns()["start_sample"], dtype=np.int64)
    assert int((st >= shift).sum()) >= alone.audio_count - 8
    files = s.register_files_at(int(cap.clock_edges[-1]))
    assert any(f.provenance(a) == Provenance.CSV for f in files.values() for a, _v in f.items())


def test_the_bus_grid_and_cursor_know_which_bring_up_they_are_in(two):
    s, _alone, shift = two
    second = s.link_controls[1]
    assert s.bringup_at(shift - 100) is second            # in the stopped stretch
    assert s.bringup_at(int(second.audio_start_sample) - 1) is second
    assert s.bringup_at(int(second.audio_start_sample) + 1000) is None
    assert s.bringup_at(0) is s.link_control


def test_decode_capture_decodes_as_session_does(two):
    from swi3s_studio import decode_capture
    s, _alone, _shift = two
    res = decode_capture(s.capture)
    assert _signature(res.commands) == _signature(s.commands)
    assert len(res.audio) == s.audio_count


# ---- the window --------------------------------------------------------------------------

def _texts(w):
    from PySide6.QtWidgets import QGraphicsSimpleTextItem, QGraphicsTextItem
    return [it.toPlainText() if isinstance(it, QGraphicsTextItem) else it.text()
            for it in w._grid_view._scene.items()
            if isinstance(it, (QGraphicsTextItem, QGraphicsSimpleTextItem))]


def _window(session):
    from PySide6.QtWidgets import QApplication

    from swi3s_studio.ui.main_window import MainWindow
    QApplication.instance() or QApplication([])
    w = MainWindow()
    w.resize(1400, 900)
    w.show()
    w.load_session(session)
    QApplication.processEvents()
    return w


def _at(w, sample):
    from PySide6.QtWidgets import QApplication
    w.cursor.set_sample(sample)
    QApplication.processEvents()


def test_the_timeline_and_grid_show_the_second_bring_up(two):
    s, _alone, shift = two
    w = _window(_session(s.capture))
    ribbon = w._ribbons[0]
    assert len(ribbon._later_bringups) == 1
    labels = [ribbon._band_label(i, g) for i, g in enumerate(ribbon._segments)]
    assert labels.count("PHY2 Safe-Lock-2") == 2, labels
    _at(w, shift - 100)                                    # the clock is stopped here
    assert "Link Inactive" in _texts(w)


def test_a_warm_restart_reads_as_one_in_the_window():
    a, b = _cold(), _warm_prefixed(_plain())
    cap, shift = _relinked(a, b, int(_GAP_S * a.sample_rate_hz))
    s = _session(cap)
    w = _window(s)
    ribbon = w._ribbons[0]
    labels = [ribbon._band_label(i, g) for i, g in enumerate(ribbon._segments)]
    assert labels.count("PHY2 Safe-Lock-2") == 2, labels
    _at(w, shift - 100)
    assert "Link Inactive" in _texts(w)
    _at(w, int(s.link_controls[1].phystart_sample) + 10)
    assert "PHY2 Reused" in _texts(w)


def test_a_mid_stream_capture_draws_its_later_bring_up():
    a, b = _plain(), _cold(phy=1)
    cap, shift = _relinked(a, b, int(_GAP_S * a.sample_rate_hz))
    w = _window(_session(cap))
    ribbon = w._ribbons[0]
    assert not ribbon._bringup and len(ribbon._later_bringups) == 1
    assert w._reg_view._active_phy is None
    _at(w, shift - 100)
    assert "Link Inactive" in _texts(w)
    _at(w, int(cap.clock_edges[-1]))
    assert w._reg_view._active_phy == "PHY1"             # the PHY the later one selected
