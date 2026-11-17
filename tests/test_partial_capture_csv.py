"""Join-late partial captures: no config on the wire, geometry supplied by a CSV.

A capture started after the bus was already running has no setup `WriteA32`s or commit
on the wire, so the decoder has nothing to snoop. Port geometry comes from an imported
Visualizer config CSV instead, and the SSP is recovered from an SSPA.

`test_config_csv.py` applies a CSV to a FULL demo, where the config commands are still on
the wire. These tests build the join-late case with `Capture.subcapture`, starting inside
the 16-column audio region, and assert the capture is genuinely configless first.
"""
import os
import tempfile

import numpy as np
import pytest
from conftest import tone_snr
from test_analog_import import _slice_capture
from test_link_restart import _off_nominal, _relinked

from swi3s_studio.model.bus_config import BusConfig
from swi3s_studio.session import Session

_SAMPLES = 6000          # deep enough that the 16-col region holds an SSPA


@pytest.fixture(scope="module")
def full():
    return Session.from_demo(_SAMPLES, phy=2, cold_start=True)


@pytest.fixture(scope="module")
def joined_late(full):
    """The capture as if recording started mid-audio: everything from 20 000 samples
    into the 16-column region onward, rebased to 0. Uses the app's own
    Capture.subcapture — the same code path Export Capture uses for a range."""
    t0 = int(full.segments[-1]["start_sample"]) + 20000
    end = int(full.capture.clock_edges[-1])
    return full.capture.subcapture(t0, end)


@pytest.fixture(scope="module")
def config_csv(full):
    """The demo's own operational config, exported as a Visualizer CSV — exactly what a
    user would hand the Analyzer for a capture whose setup they missed."""
    t0 = int(full.segments[-1]["start_sample"]) + 60000
    cfg, _n = BusConfig.from_decoder_config(full.config_dataports_at(t0))
    fd, path = tempfile.mkstemp(suffix=".csv")
    os.close(fd)
    cfg.to_csv_file(path)
    yield path
    os.remove(path)


def _pcm(store):
    for dev, dp in store.streams():
        for ch in store.channels(dev, dp):
            if store.native_sample_bits(dev, dp, ch) != 1:
                yield dev, dp, ch


def test_the_slice_really_has_no_config(joined_late):
    """Precondition. If the slice still carried the setup writes or the commit, every
    test below would pass with the CSV doing nothing."""
    s = Session(joined_late)
    assert not [c for c in s.commands if c.get("command") == "WriteA32"], \
        "slice still contains config writes — not a join-late capture"
    assert not [c for c in s.commands if c.get("is_commit")], \
        "slice still contains a commit"


def test_without_a_csv_there_is_no_audio(joined_late):
    """Nothing on the wire says which columns carry which port, so no stream decodes.
    This is the state the CSV import exists to rescue."""
    s = Session(joined_late)
    assert s.audio_store().streams() == []


def test_csv_at_open_recovers_every_stream(joined_late, config_csv):
    """Supplying the config at open reconstructs all four data ports."""
    s = Session(joined_late, config_csv=config_csv)
    store = s.audio_store()
    assert len(store.streams()) == 4, store.streams()


def test_csv_applied_later_recovers_every_stream(joined_late, config_csv):
    """The other route: open the capture bare, then Decode ▸ Import Visualizer CSV."""
    s = Session(joined_late)
    assert s.audio_store().streams() == []
    s.apply_config_csv(config_csv)
    assert len(s.audio_store().streams()) == 4


def test_recovered_audio_is_clean_end_to_end(joined_late, config_csv):
    """The point of the whole workflow: the PCM streams decode as clean tones across the
    WHOLE slice, not just the part after the SSPA (the SSP lock is what buys the
    pre-SSPA half — see test_sspa_ssp_lock.py)."""
    s = Session(joined_late, config_csv=config_csv)
    store = s.audio_store()
    checked = 0
    for dev, dp, ch in _pcm(store):
        a = store.samples(dev, dp, ch)
        half = len(a) // 2
        assert tone_snr(a[:half]) > 20.0, f"dev{dev} dp{dp} ch{ch}: first half garbled"
        assert tone_snr(a[half:]) > 20.0, f"dev{dev} dp{dp} ch{ch}: second half garbled"
        checked += 1
    assert checked >= 2, f"expected at least two PCM streams, checked {checked}"


def test_geometry_matches_the_original_capture(joined_late, config_csv, full):
    """The recovered decode must agree with the full capture it was cut from: same
    column count, same set of streams."""
    s = Session(joined_late, config_csv=config_csv)
    assert s.column_count == full.column_count
    assert set(s.audio_store().streams()) == set(full.audio_store().streams())


def test_recovered_audio_matches_the_full_capture(joined_late, config_csv, full):
    """Strongest form: sample VALUES from the join-late decode must match the same
    stream decoded from the complete capture. Compares a window from the middle of the
    slice against the corresponding window of the original, which pins the phase as well
    as the geometry."""
    s = Session(joined_late, config_csv=config_csv)
    part, whole = s.audio_store(), full.audio_store()
    compared = 0
    for dev, dp, ch in _pcm(part):
        a = np.asarray(part.samples(dev, dp, ch))
        b = np.asarray(whole.samples(dev, dp, ch))
        if a.size < 600 or b.size < a.size:
            continue
        # The slice starts partway through, so align by finding a's mid-window in b.
        probe = a[a.size // 2: a.size // 2 + 256]
        tail = b[-(a.size + 8):]
        hit = None
        for off in range(0, tail.size - probe.size):
            if np.array_equal(tail[off:off + probe.size], probe):
                hit = off
                break
        assert hit is not None, f"dev{dev} dp{dp} ch{ch}: recovered samples not found in the original"
        compared += 1
    assert compared >= 2, f"compared only {compared} streams"


def test_workspace_round_trip_keeps_the_recovery(joined_late, config_csv, tmp_path):
    """A join-late session reopens the same way from its saved source descriptor: the capture
    file and the CSV path are persisted, and the SSP is re-derived from the SSPA on load."""
    import json

    from swi3s_studio.ingest.sal_export import export_sal
    path = str(tmp_path / "late.sal")
    export_sal(joined_late, path)
    s = Session.from_sal(path, 0, 1, config_csv=config_csv)
    assert s.ssp_locked_from_sspa
    before = {k: len(s.audio_store().samples(*k)) for k in s.audio_store()._channels}
    assert before

    src = json.loads(json.dumps(s.source))
    assert src.get("config_csv") == config_csv
    s2 = Session.from_source(src)
    assert s2.ssp_locked_from_sspa
    assert {k: len(s2.audio_store().samples(*k)) for k in s2.audio_store()._channels} == before


# ---------------------------------------------------------------- the audio of a cut

def _audio_at(session, origin=0):
    """{(device, dp, channel): {start sample in the full capture: value}} of `session`'s
    decoded audio, its samples counted from `origin`."""
    cols = session.audio_columns()
    out: dict = {}
    for dev, dp, ch, start, value in zip(cols["device"], cols["dp"], cols["channel"],
                                         cols["start_sample"], cols["value"]):
        out.setdefault((int(dev), int(dp), int(ch)), {})[int(start) + origin] = int(value)
    return out


def _assert_the_full_captures_audio(part, whole):
    """Every stream of `part` is a run of the same stream's consecutive samples in `whole`,
    from its first sample to its last: the same positions and values, none missing."""
    assert part.keys() == whole.keys()
    for key, samples in part.items():
        positions = sorted(whole[key])
        first = positions.index(min(samples)) if min(samples) in whole[key] else None
        assert first is not None, (key, "the first sample is not the full capture's")
        run = positions[first:first + len(samples)]
        assert sorted(samples) == run, (key, "not a run of the full capture's samples")
        assert [t for t in run if samples[t] != whole[key][t]] == [], key


def _cut(session, config_csv, edge):
    """The decode of `session`'s capture from clock edge `edge` on, opened with the CSV,
    and its import notes."""
    from swi3s_studio import import_notes
    sub, origin = _slice_capture(session.capture, edge, session.capture.clock_edges.size)
    with import_notes.collect() as notes:
        cut = Session(sub, config_csv=config_csv)
    return cut, _audio_at(cut, origin), [n.message for n in notes]


@pytest.fixture(scope="module")
def row_start(full):
    """The clock edge 2000 rows into the 16-column region on which a row starts, as the
    decoder counts a segment's start_ui."""
    return int(full.segments[-1]["start_ui"]) + 16 * 2000


@pytest.mark.parametrize("into_row", [3, 7, 11])
def test_a_cut_part_way_into_a_row_decodes_the_audio_of_the_cut_at_its_start(
        full, config_csv, row_start, into_row):
    """A capture cut `into_row` UIs into a row and opened with the config CSV decodes all
    four streams as the capture cut at the row's start does, at every position both hold:
    the payload is sampled at the CDS's Column-0 phase. Both are the full capture's audio."""
    _a, aligned, _n = _cut(full, config_csv, row_start)
    _m, mid_cut, _n = _cut(full, config_csv, row_start + into_row)
    assert len(mid_cut) == 4 and mid_cut.keys() == aligned.keys()
    for key, samples in mid_cut.items():
        both = samples.keys() & aligned[key].keys()
        assert len(both) >= len(samples) - 2, key
        assert [t for t in both if samples[t] != aligned[key][t]] == [], key
    whole = _audio_at(full)
    _assert_the_full_captures_audio(aligned, whole)
    _assert_the_full_captures_audio(mid_cut, whole)


# A cut (rows into the 16-column region, UIs into that row): the first SSP-lock row, the
# SSPA's row modulo the 5120-row skipping pattern of DP0, lies thousands of rows in.
_PHY2_CUTS = [(61, 0), (1000, 5), (3500, 9)]


@pytest.mark.parametrize("rows,into_row", _PHY2_CUTS)
def test_a_cut_is_the_full_captures_audio_from_its_first_sample(full, config_csv, rows,
                                                                into_row):
    """A PHY2 capture cut `rows` rows into the 16-column region, `into_row` UIs into the
    row, and locked to the SSP of its SSPA, decodes every port, the skipping DP0, DP1 and
    the PDM DP2 and DP3, as the full capture does from each stream's first sample: the
    SSPA's row phases every port from the cut, SSPs recurring every pattern period. The
    scrambled DP1 and DP3 start at the first sample their descrambler recovers from the
    wire's bits, and the cut's import notes name them."""
    edge = int(full.segments[-1]["start_ui"]) + 16 * rows + into_row
    cut, audio, notes = _cut(full, config_csv, edge)
    assert cut.ssp_locked_from_sspa
    assert len(audio) == 4
    _assert_the_full_captures_audio(audio, _audio_at(full))
    withheld = {(r["dp"], r["channel"]): r["samples"] for r in cut.decoder.withheld_samples()}
    assert withheld == {(1, 0): 1, (3, 0): 9}
    assert ("the first samples of device 0 DP1 channel 0 (1 sample), device 0 DP3 channel 0 "
            "(9 samples) are not shown: the capture starts with the port running, and its "
            "descrambler recovers them from a state set before the capture.") in notes


@pytest.fixture(scope="module")
def full_phy3():
    from swi3s_studio import import_notes
    with import_notes.collect():
        return Session.from_demo(_SAMPLES, phy=3, cold_start=True)


@pytest.fixture(scope="module")
def config_csv_phy3(full_phy3):
    t0 = int(full_phy3.segments[-1]["start_sample"]) + 60000
    cfg, _n = BusConfig.from_decoder_config(full_phy3.config_dataports_at(t0))
    fd, path = tempfile.mkstemp(suffix=".csv")
    os.close(fd)
    cfg.to_csv_file(path)
    yield path
    os.remove(path)


def _rows_from(session, origin=0):
    """`session`'s recovered Row Sync Points, counted from `origin`."""
    return session._recovered_row_syncs().astype(np.int64) + origin


def _assert_matches(part, whole, slack):
    """Every stream of `part` is a run of the same stream's consecutive samples in `whole`,
    each `part` sample starting within `slack` samples of its `whole` one, with its value."""
    assert part.keys() == whole.keys()
    for key, samples in part.items():
        positions = np.array(sorted(whole[key]), dtype=np.int64)
        mine = np.array(sorted(samples), dtype=np.int64)
        at = np.clip(np.searchsorted(positions, mine - slack), 0, positions.size - 1)
        assert np.all(np.abs(positions[at] - mine) <= slack), (key, "a sample not in the full capture")
        assert np.array_equal(at, at[0] + np.arange(at.size)), (key, "not a run")
        assert [int(t) for t, w in zip(mine, positions[at])
                if samples[int(t)] != whole[key][int(w)]] == [], key


def _assert_only_the_unknowable_is_withheld(cut, audio, whole, first_row, notes, slack=0):
    """Every sample of the full capture that starts at or after the cut's first Row Sync and
    before a stream's first decoded sample is one its descrambler had not settled for: as
    many as the decoder withholds for the stream, a scrambled one (DP1, DP3), named in the
    import note. A sample in flight at the cut is the only other one not decoded."""
    withheld = {(r["device"], r["dp"], r["channel"]): r["samples"]
                for r in cut.decoder.withheld_samples()}
    for key, samples in audio.items():
        positions = np.array(sorted(whole[key]), dtype=np.int64)
        missing = int(np.searchsorted(positions, min(samples) - slack)
                      - np.searchsorted(positions, first_row))
        assert missing == withheld.get(key, 0), (key, missing, withheld)
    assert set(withheld) <= {(0, 1, 0), (0, 3, 0)} and withheld[(0, 3, 0)] == 9
    note = next(n for n in notes if n.startswith("the first samples of "))
    for (dev, dp, ch), n in withheld.items():
        assert f"device {dev} DP{dp} channel {ch} ({n} sample{'s' if n != 1 else ''})" in note
    assert note.count(" DP") == len(withheld)


def _assert_the_full_decode_from_the_cut(full, cut, origin, notes, whole=None, slack=0,
                                         held_from=None):
    """The cut's Row Sync Points are the full decode's from the first sample whose edge the
    cut holds (`held_from`, the sample after `origin` by default) on, and its audio the full
    decode's, from each stream's first sample, with only the unknowable withheld."""
    mine = _rows_from(cut, origin)
    theirs = _rows_from(full)
    theirs = theirs[theirs >= (origin + 1 if held_from is None else held_from)]
    assert np.array_equal(mine, theirs[:mine.size])
    assert mine.size >= theirs.size - 1
    audio = _audio_at(cut, origin)
    whole = whole if whole is not None else _audio_at(full)
    assert cut.ssp_locked_from_sspa and len(audio) == 4
    _assert_matches(audio, whole, slack)
    _assert_only_the_unknowable_is_withheld(cut, audio, whole, int(mine[0]), notes, slack)


@pytest.fixture(scope="module")
def whole_phy3(full_phy3):
    return _audio_at(full_phy3)


@pytest.mark.parametrize("edges", [976, 8000, 48007, 56009])
def test_a_phy3_cut_is_the_full_captures_audio_from_its_first_sample(full_phy3,
                                                                     config_csv_phy3,
                                                                     whole_phy3, edges):
    """The same for the PHY3 demo, cut `edges` data-line transitions into its 16-column
    region: the recovered clock's Row Sync Points are the full decode's from the cut, and
    each stream of every port, DP0 skipping, is the full capture's from its first sample.
    Where the cut's first rising edge opens no row, the clock locks further on and works
    back to the first row the cut holds."""
    clk = full_phy3.capture.clock_edges
    edge = int(np.searchsorted(clk, int(full_phy3.segments[-1]["start_sample"]))) + edges
    cut, _audio, notes = _cut(full_phy3, config_csv_phy3, edge)
    origin = int(clk[edge])
    _assert_the_full_decode_from_the_cut(full_phy3, cut, origin, notes, whole_phy3,
                                         held_from=origin)


def _cut_at(capture, csv, sample):
    """The decode of `capture` from `sample` on, opened with the CSV, and its import notes."""
    from swi3s_studio import import_notes
    sub = capture.subcapture(sample, int(capture.clock_edges[-1]) + 1)
    with import_notes.collect() as notes:
        cut = Session(sub, config_csv=csv)
    return cut, [n.message for n in notes]


def _row_point(full, rows, into_row):
    """The sample `into_row` UIs into the row `rows` rows into `full`'s 16-column region."""
    rsp = _rows_from(full)
    first = int(np.searchsorted(rsp, int(full.segments[-1]["start_sample"])))
    return int(rsp[first + rows] + into_row * full.recovered_clock()[1])


# (rows into the 16-column region, UIs into that row): a cut part-way into a UI, at the
# start of a row and near its end.
_PHY3_POINTS = [(0, 0.25), (61, 3.5), (61, 7.75), (976, 15.6), (3500, 3.5), (24000, 7.75)]


@pytest.mark.parametrize("rows,into_row", _PHY3_POINTS)
def test_a_phy3_cut_anywhere_in_a_row_is_the_full_decode_from_the_cut(
        full_phy3, config_csv_phy3, whole_phy3, rows, into_row):
    """A PHY3 capture cut `into_row` UIs into a row of its 16-column region decodes as the
    full capture does from the cut: the same Row Sync Points and every stream's samples
    from the first, with only a sample in flight at the cut and the descramblers' settling
    samples left out."""
    t0 = _row_point(full_phy3, rows, into_row)
    cut, notes = _cut_at(full_phy3.capture, config_csv_phy3, t0)
    _assert_the_full_decode_from_the_cut(full_phy3, cut, t0, notes, whole_phy3)


@pytest.fixture(scope="module")
def full_phy3_off(full_phy3):
    """The PHY3 demo recorded with the analyzer's clock 3% off nominal: the recovered UI is
    not the one the clock is seeded with."""
    from swi3s_studio import import_notes
    with import_notes.collect():
        return Session(_off_nominal(full_phy3.capture), source={"type": "test"})


@pytest.mark.parametrize("rows,into_row", [(61, 0.25), (976, 7.75), (24000, 15.6)])
def test_a_phy3_cut_off_nominal_rate_is_the_full_decode_from_the_cut(
        full_phy3_off, config_csv_phy3, rows, into_row):
    """With the analyzer's clock 3% off nominal, a PHY3 cut decodes as the full capture does
    from the cut: its first rows are sampled at the UI the clock locked to, not the seeded
    one. A sample's start may lie a sample from the full decode's, whose UI estimate has
    tracked a different stretch of edges."""
    t0 = _row_point(full_phy3_off, rows, into_row)
    cut, notes = _cut_at(full_phy3_off.capture, config_csv_phy3, t0)
    _assert_the_full_decode_from_the_cut(full_phy3_off, cut, t0, notes, slack=2)


def test_a_phy3_cut_then_a_restart_decodes_both_link_sessions(full_phy3_off,
                                                              config_csv_phy3):
    """A PHY3 capture joined mid-stream whose Link stops and is brought up again by a Cold
    Start, recorded 3% off nominal and opened with the config CSV: the stretch before the
    stop is detected as a DLV pair and decodes as the full capture does from the cut, phased
    by its own SSPA (the restarted Link's commit phases only its own ports), and the
    restarted Link decodes as the same demo alone does."""
    from swi3s_studio import import_notes
    from swi3s_studio.ingest import transitions
    full = full_phy3_off
    clk = full.capture.clock_edges
    first = int(np.searchsorted(clk, int(full.segments[-1]["start_sample"])))
    t0 = int(clk[first + 976]) - 1
    sspa = next(int(c["end_sample"]) for c in full.commands
                if c.get("has_sync_point") and not c.get("is_commit"))
    joined = full.capture.subcapture(t0, sspa + 2_000_000)
    again = _off_nominal(transitions.demo_capture(300, cold_start=True, phy=3))
    cap, shift = _relinked(joined, again, int(0.005 * joined.sample_rate_hz))
    with import_notes.collect() as notes:
        s = Session(cap, source={"type": "test"}, config_csv=config_csv_phy3)
        alone = Session(again, source={"type": "test"}, config_csv=config_csv_phy3)
    assert s.is_dlv and [g["restart"] for g in s.segments] == [False, True]
    rows = _rows_from(s)
    before, after = rows[rows < shift] + t0, rows[rows >= shift] - shift
    theirs = _rows_from(full)
    assert np.array_equal(before, theirs[theirs > t0][:before.size])
    assert np.array_equal(after, _rows_from(alone))
    audio = _audio_at(s)
    head = {k: {t + t0: v for t, v in a.items() if t < shift} for k, a in audio.items()}
    tail = {k: {t - shift: v for t, v in a.items() if t >= shift} for k, a in audio.items()}
    whole = _audio_at(full)
    assert s.ssp_locked_from_sspa
    _assert_matches(head, whole, 2)
    _assert_only_the_unknowable_is_withheld(s, head, whole, int(before[0]),
                                            [n.message for n in notes], 2)
    assert tail == _audio_at(alone)
