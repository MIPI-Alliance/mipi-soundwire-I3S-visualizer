"""Session decode inputs and the sources that set them.

- A re-decode the memory watchdog stops or the user cancels, or an input the binding
  refuses, or a source or decoder that fails to build, leaves the decode inputs, the source
  and the decode as they were, and a later valid input decodes; the SSP row and whether it
  was derived from an SSPA are restored too (set_ssp_row, lock_ssp_from_sspa);
  apply_config_csv also undoes a CSV that does not read, a decode that fails and a stop of
  the SSP re-phase that follows the CSV's decode, and removes the upgrade temp copy it
  made; a constructor that fails, a cancelled one included, removes the upgrade temp copy
  it made.
- A later bring-up of the other PHY family costs no second decode.
- Two identical .wfm channels reach the analog front end whatever their rate (negative,
  zero or positive).
- sample_at_ui inverts ui_index; bus_rows_for_samples with no segments is the scalar
  bus_row_for_sample (row 0); run_watched raises DecodeCancelled for a cancelled decode.
- The window reads the decode inputs through Session.config_csv and tx_persist_key, whose
  key follows an applied config CSV.
- A window cut from a capture read whole follows the .sal window rule (one function): a
  start before the capture and an end past any sample count are noted and clamped, a
  fractional bound is rounded and noted, an integer bound is taken as it is however large,
  an empty or non-finite one raises ValueError, and a window with no transition is noted;
  .sal records the window as read, as the cut does; a window in seconds whose sample
  number overflows a float is clamped to the end the same way, and a window_s that is not a
  pair of numbers raises ValueError naming window_s.
- A malformed workspace source field (a numeric field that does not read or is outside its
  range, a window that is not a pair of numbers, a file path that is not text, a map field
  that is not a mapping) raises ValueError naming the field; a demo phy or variant the demo
  does not build is noted and the PHY2 or plain demo built.
- A column pin or a forced column count outside 2..32 (the core's range) is left out with a
  note; an analog CSV channel the file lacks raises naming the file and its channels.
- last_sample takes the last edge on either line; total_bus_rows covers the row of the last
  sample; a region is rated over its whole rows only and needs two between its ends; the
  regions start at the audio start; a sample on a Row Sync Point is the row it opens to
  bus_row_for_sample and the row before to bus_rows_for_samples; a DLV row past the last
  Row Sync Point opens at the last; with no recovered Row Sync Point a DLV sample's row is
  counted from Column 0, the segment's CDS column added back.
- The signed sample value is two's complement at every width, the most negative value
  included, and a 1-bit PDM value reads as -1 or +1.
- A workspace demo's samples per channel is clamped to 1..DEMO_SAMPLES_MAX with a note;
  from_demo refuses one outside it. DEMO_SAMPLES_MAX is swi3score.max_demo_samples, and
  every native stream generator refuses a length above it.
- A decode-input key outside the core's range (a config section index outside
  0..2**31-1, a hub-depth device outside 0..11, a scrambler override key that is not a
  (device -1..11, data port 0..2**31-1) pair) is left out with a note naming the field.
- A DLV region exports the row rate rate_regions measures for it.
- A re-decode that fails in any step after its decoder ran (audio, CSV seed, register
  files) keeps every attribute of the previous decode.
- An analog CSV's clock and data on one channel, or a .wfm pair naming one file, raises
  ValueError before the file is read.
- A source's signal identifier that is not text, a flag that is not true or false, or a
  threshold that is not a (high, low) pair of finite numbers with high at least low raises
  ValueError naming the field.
- A forced-DLV grid replays the configuration from the last Cold Start.
- A value filter operator other than <=, <, ==, !=, >=, > raises ValueError; each keeps
  the samples its comparison passes.
- section_ui_stats leaves out idle gaps over 4x the section's median spacing.
- A DLV sample belongs to the UI whose mid-UI sample point is nearest.
- Source-descriptor entries that do not parse (label fields, port and region names,
  column pins, region config CSV keys and region config CSV values that are not a path)
  are left out with one note per field; a label-field value that is not an integer 0..7 does
  not parse.
"""
import copy
import os
from types import SimpleNamespace

import numpy as np
import pytest

import swi3s_studio.session as S
from swi3s_studio import import_notes
from swi3s_studio.analysis.link_control import LinkControlResult
from swi3s_studio.ingest import transitions
from swi3s_studio.model.bus_config import demo_config
from swi3s_studio.session import DecodeCancelled, DecodeMemoryError, Session


def _state(s):
    return (copy.deepcopy(s.source), dict(s.forced_column_sections),
            dict(s.scrambler_overrides), dict(s.hub_depths), dict(s.register_overrides),
            dict(s.csv_sections), s._config_csv, s._csv_seed_key, s._ssp_row)


def _stopped(monkeypatch, exc=None):
    def stop(decoder):
        raise exc or DecodeMemoryError(1, 2, 0.5)
    monkeypatch.setattr(S, "run_watched", stop)


@pytest.fixture
def csv_path(tmp_path):
    return demo_config().to_csv_file(str(tmp_path / "cfg.csv"))


@pytest.mark.parametrize("call", [
    lambda s, p: s.set_scrambler_overrides({(1, 0): False}),
    lambda s, p: s.set_hub_depths({1: 2}),
    lambda s, p: s.set_register_override(0, 1, 0x2090, 3),
    lambda s, p: s.set_register_overrides({(0, 1, 0x2090): 3}),
    lambda s, p: s.force_column_count_at(10, 12),
    lambda s, p: s.apply_config_csv(p),
    lambda s, p: s.apply_config_csv_at(10, p),
])
@pytest.mark.parametrize("exc", [DecodeMemoryError(1, 2, 0.5), DecodeCancelled()],
                         ids=["memory", "cancelled"])
def test_a_stopped_re_decode_restores_every_input(monkeypatch, csv_path, call, exc):
    s = Session.from_demo(48, phy=2, cold_start=True)
    before = _state(s)
    decoder, commands, segments = s.decoder, s.commands, s.segments
    with monkeypatch.context() as m:
        _stopped(m, exc)
        with pytest.raises(type(exc)):
            call(s, csv_path)
    assert _state(s) == before
    assert s.decoder is decoder and s.commands is commands and s.segments is segments
    call(s, csv_path)                           # the same call, retried, now takes effect
    assert _state(s) != before


@pytest.mark.parametrize("exc", [DecodeMemoryError(1, 2, 0.5), DecodeCancelled()],
                         ids=["memory", "cancelled"])
def test_a_stopped_ssp_re_decode_restores_the_ssp_inputs(monkeypatch, csv_path, exc):
    """set_ssp_row and lock_ssp_from_sspa write the SSP row and whether it was derived by
    assignment; a stopped re-decode puts both back, with the decode they were made with."""
    s = Session.from_demo(48, phy=2, cold_start=True)
    s.apply_config_csv(csv_path)
    before = (_state(s), s._ssp_from_sspa)
    decoder, commands, segments = s.decoder, s.commands, s.segments
    assert s._ssp_row == -1 and s._ssp_from_sspa is False
    with monkeypatch.context() as m:
        _stopped(m, exc)
        with pytest.raises(type(exc)):
            s.set_ssp_row(7)
        m.setattr(Session, "_wire_supplies_an_ssp", lambda self: False)
        m.setattr(Session, "sspa_ssp_row", lambda self: 100)
        with pytest.raises(type(exc)):
            s.lock_ssp_from_sspa()
    assert (_state(s), s._ssp_from_sspa) == before
    assert s.decoder is decoder and s.commands is commands and s.segments is segments


def test_a_later_bring_up_of_the_other_family_costs_no_second_decode(monkeypatch):
    s = Session.from_demo(48, phy=2, cold_start=True)
    first = LinkControlResult(sequence="cold")                    # names no PHY: unsteered
    other = LinkControlResult(sequence="cold", phy_name="PHY3", audio_start_sample=100)
    s._link_control, s._later_link_controls = first, [other]
    s._dlv_forced = False
    decodes = []
    monkeypatch.setattr(s, "_decode_with", lambda *a, **k: decodes.append(1))
    monkeypatch.setattr(s, "_valid_commands_inside", lambda lc: 3)
    monkeypatch.setattr(S, "decode_link_controls", lambda capture, first: [first, other])
    settings = SimpleNamespace(forced_column_count=0, dlv=False, cds_horizontal_start=0)
    s._check_first_bringup(settings)
    assert len(decodes) == 1


@pytest.mark.parametrize("rate", [-1e9, 0.0, 1e9])
def test_two_identical_wfm_channels_do_not_disagree_whatever_their_rate(monkeypatch, rate):
    from swi3s_studio.ingest import analog
    from swi3s_studio.ingest import wfm as wfm_ingest

    ch = {"time": np.arange(4) * 1e-9, "volts": np.zeros(4), "sample_rate_hz": rate}
    monkeypatch.setattr(wfm_ingest, "read_wfm_pair", lambda a, b: (ch, dict(ch)))

    class Reached(Exception):
        pass

    def reached(*a, **k):
        raise Reached
    monkeypatch.setattr(analog, "capture_from_analog", reached)
    with pytest.raises(Reached):
        Session.from_wfm("a.wfm", "b.wfm")


@pytest.mark.parametrize("phy", [2, 3])
def test_sample_at_ui_inverts_ui_index(phy):
    s = Session.from_demo(600, phy=phy, cold_start=True)
    total = s.total_uis()
    assert total == s.ui_index(s.last_sample()) + 1
    for ui in sorted({0, 1, 7, total // 3, total // 2, total - 1}):
        first = s.sample_at_ui(ui)
        assert s.ui_index(first) >= ui
        assert first == 0 or s.ui_index(first - 1) < ui
    assert s.sample_at_ui(total + 10) == s.last_sample()


def test_rows_for_samples_with_no_segments_is_the_scalar_zero_row_scale():
    s = Session.from_demo(48, phy=2, cold_start=True)
    s.segments = []
    samples = [0, 50, 5000]
    rows = s.bus_rows_for_samples(np.array(samples, dtype=np.int64))
    assert list(rows) == [s.bus_row_for_sample(x) for x in samples] == [0, 0, 0]


@pytest.mark.parametrize("floor", [0, 1 << 20], ids=["no_floor", "floor"])
def test_run_watched_raises_when_the_decode_was_cancelled(monkeypatch, floor):
    """A cancelled decode's result is incomplete: run_watched raises rather than return it,
    and a decode that ran to the end returns. A decoder with no `cancelled` (a binding older
    than the ABI the Session requires) raises rather than read as not cancelled."""
    from swi3s_studio.ingest import saleae_sal
    monkeypatch.setattr(saleae_sal, "decode_memory_floor", lambda: floor)
    monkeypatch.setattr(saleae_sal, "available_memory_bytes", lambda: 1 << 40)

    def decoder(cancelled):
        return SimpleNamespace(run=lambda: None, cancelled=cancelled, stopped=False,
                               progress=1.0, audio_count=0, request_stop=lambda: None)
    with pytest.raises(DecodeCancelled):
        S.run_watched(decoder(True))
    S.run_watched(decoder(False))
    with pytest.raises(AttributeError, match="cancelled"):
        S.run_watched(SimpleNamespace(run=lambda: None, stopped=False, progress=1.0,
                                      audio_count=0, request_stop=lambda: None))


def test_the_window_reads_the_decode_inputs_through_public_accessors(csv_path):
    s = Session.from_demo(48, phy=2, cold_start=True)
    assert s.config_csv == ""
    before = s.tx_persist_key(None)
    s.apply_config_csv(csv_path)
    assert s.config_csv and s.config_csv == s._config_csv
    origin, cols, end_ui = s.tx_persist_key(None)
    assert 0 <= origin < end_ui and cols >= 2
    assert (origin, cols, end_ui) != before      # the CSV frames the capture from row 0


# ---- apply_config_csv on a failure that is not a stop -----------------------------------

def test_a_config_csv_that_does_not_read_changes_nothing(tmp_path):
    s = Session.from_demo(48, phy=2, cold_start=True)
    before = _state(s)
    bad = tmp_path / "bad.csv"
    bad.write_text("AppVersion,3\nDescription," + "x" * 200_000 + "\n", encoding="utf-8")
    with pytest.raises(ValueError):
        s.apply_config_csv(str(bad))
    assert _state(s) == before


def open_bytes(path):
    with open(path, "rb") as f:
        return f.read()


@pytest.mark.platform
def test_a_failed_decode_undoes_the_csv_and_removes_its_temp(monkeypatch, csv_path, tmp_path):
    from swi3s_studio.ingest import visualizer_csv
    s = Session.from_demo(48, phy=2, cold_start=True)
    before = _state(s)
    decoder = s.decoder
    tmp = tmp_path / "upgraded.cur.csv"

    def upgraded(path):
        tmp.write_bytes(open_bytes(path))
        return str(tmp)
    monkeypatch.setattr(visualizer_csv, "normalized_path", upgraded)

    def fail(decoder):
        raise RuntimeError("decode failed")
    monkeypatch.setattr(S, "run_watched", fail)
    with pytest.raises(RuntimeError):
        s.apply_config_csv(csv_path)
    assert _state(s) == before
    assert s.decoder is decoder
    assert not os.path.exists(tmp)


@pytest.mark.platform
@pytest.mark.parametrize("exc", [DecodeCancelled(), DecodeMemoryError(1, 2, 0.5),
                                 RuntimeError("decode failed")],
                         ids=["cancelled", "memory", "error"])
def test_a_failed_constructor_removes_its_config_csv_temp(monkeypatch, csv_path, tmp_path,
                                                          exc):
    from swi3s_studio.ingest import visualizer_csv
    cap = Session.from_demo(48, phy=2, cold_start=True).capture
    tmp = tmp_path / "upgraded.cur.csv"

    def upgraded(path):
        tmp.write_bytes(open_bytes(path))
        return str(tmp)
    monkeypatch.setattr(visualizer_csv, "normalized_path", upgraded)
    _stopped(monkeypatch, exc)
    with pytest.raises(type(exc)):
        Session(cap, config_csv=csv_path)
    assert not os.path.exists(tmp)


# ---- a window cut from a capture read whole ---------------------------------------------

def _cut(window=None, window_s=None):
    cap = transitions.demo_capture(8)
    src = {"type": "digital_csv", "path": "cut.csv"}
    with import_notes.collect() as notes:
        out = Session._cut(cap, src, window, window_s)
    return cap, out, src, [n.message for n in notes]


def test_a_window_end_past_any_sample_count_is_read_to_the_end():
    cap, out, src, notes = _cut(window=[0, 2**64])
    assert out.clock_edges.size == cap.clock_edges.size
    assert any("past any capture" in n for n in notes)
    assert src["window"][1] < 2**63


def test_a_fractional_window_bound_is_rounded_and_noted():
    _, out, src, notes = _cut(window=[1.5, 4000.7])
    assert src["window"] == [2, 4001]
    assert sum("is not a whole sample" in n for n in notes) == 2


@pytest.mark.parametrize("window,window_s,match", [
    ([100, 100], None, "must be past start"), (None, (100.0, 50.0), "before the start"),
    (None, (float("nan"), 3.0), "not a pair of finite times"),
    ([float("inf"), 3], None, "not a finite number"),
    ([float("nan"), 3], None, "not a finite number")])
def test_an_empty_or_non_finite_window_raises(window, window_s, match):
    with pytest.raises(ValueError, match=match):
        _cut(window=window, window_s=window_s)


def test_an_integer_window_bound_is_taken_as_it_is_however_large():
    """A whole-sample bound past 2**53 is no fraction, and one past what a float holds is no
    overflow: each is an end past any capture, read to its end."""
    for end in ((1 << 62) + 5, 10 ** 400):
        cap, out, src, notes = _cut(window=[0, end])
        assert out.clock_edges.size == cap.clock_edges.size
        assert not any("is not a whole sample" in n for n in notes), end
        assert any("past any capture" in n for n in notes), end


def test_sal_and_a_cut_read_one_window_alike(tmp_path):
    """The .sal reader and the cut of a capture read whole take a window by one rule: a
    fractional bound is rounded and noted, the source records the window as read, and a
    bound that is not finite raises ValueError."""
    demo = Session.from_demo(8)
    path = str(tmp_path / "w.sal")
    demo.export_sal(path)
    with import_notes.collect() as notes:
        sal = Session.from_sal(path, 0, 1, window=(1.5, 4000.7))
    assert sal.source["window"] == [2, 4001]
    assert sum("is not a whole sample" in n.message for n in notes) == 2
    _, _, src, cut_notes = _cut(window=[1.5, 4000.7])
    assert src["window"] == sal.source["window"]
    assert sum("is not a whole sample" in n for n in cut_notes) == 2
    for bad in ((0, float("inf")), (float("nan"), 10)):
        with pytest.raises(ValueError, match="not a finite number"):
            Session.from_sal(path, 0, 1, window=bad)
        with pytest.raises(ValueError, match="not a finite number"):
            _cut(window=list(bad))


def test_a_window_in_seconds_past_the_float_range_is_read_to_the_end(tmp_path):
    """A finite time whose sample number overflows a float is that sample number, so the
    window rule clamps it to the capture's end with a note; a window_s that is not a pair of
    numbers raises ValueError naming window_s."""
    from fractions import Fraction
    assert Session.window_samples((0.0, 1e308), 1e6) == (0, int(Fraction(1e308) * 10**6) + 1)
    assert Session.window_samples((0.0, 1e-6), 1e9) == (0, 1001)
    demo = Session.from_demo(8)
    path = str(tmp_path / "w.sal")
    demo.export_sal(path)
    with import_notes.collect() as notes:
        sal = Session.from_sal(path, 0, 1, window_s=(0.0, 1e300))
    assert sal.capture.clock_edges.size == demo.capture.clock_edges.size
    assert any("past any capture" in n.message for n in notes)
    cap, out, _, cut_notes = _cut(window_s=(0.0, 1e300))
    assert any("past any capture" in n for n in cut_notes)
    assert out.clock_edges.size == cap.clock_edges.size
    assert out.data_edges.size == cap.data_edges.size
    for bad in (("a", 1.0), (0.0,), None, (0.0, [1])):
        with pytest.raises(ValueError, match="window_s: .* is not a pair of numbers"):
            Session.window_samples(bad, 1e6)
    with pytest.raises(ValueError, match="window_s: the sample rate inf Hz is not finite"):
        Session.window_samples((0.0, 1.0), float("inf"))


def test_a_window_with_no_transition_is_noted():
    cap = transitions.demo_capture(8)
    last = max(int(cap.clock_edges[-1]), int(cap.data_edges[-1]))
    _, out, _, notes = _cut(window=[last + 10, last + 20])
    assert out.clock_edges.size == 0 and out.data_edges.size == 0
    assert any("holds no transition" in n for n in notes)


# ---- demo sources -----------------------------------------------------------------------

@pytest.mark.parametrize("n", [0, -5, Session.DEMO_SAMPLES_MAX + 1])
def test_from_demo_refuses_a_sample_count_outside_its_range(n):
    with pytest.raises(ValueError, match="audio_samples_per_channel"):
        Session.from_demo(n)


@pytest.mark.parametrize("given,kept", [(10_000_000, Session.DEMO_SAMPLES_MAX), (0, 1),
                                        (48, 48)])
def test_a_workspace_demo_sample_count_is_clamped_with_a_note(monkeypatch, given, kept):
    built = []
    monkeypatch.setattr(Session, "from_demo",
                        classmethod(lambda cls, n, **kw: built.append(n)))
    with import_notes.collect() as notes:
        Session.from_source({"type": "demo", "audio_samples_per_channel": given, "phy": 2})
    assert built == [kept]
    assert bool([n for n in notes if "audio_samples_per_channel" in n.message]) == (given != kept)


@pytest.mark.parametrize("given", [32.7, True, "12.5", None, [8]])
def test_a_workspace_demo_sample_count_that_is_not_a_whole_number_is_refused(given):
    with pytest.raises(ValueError, match="audio_samples_per_channel"):
        Session.from_source({"type": "demo", "audio_samples_per_channel": given, "phy": 2})


# ---- source-descriptor entries that do not parse ----------------------------------------

def test_unreadable_source_entries_are_left_out_with_a_note():
    cap = Session.from_demo(8).capture
    source = {"type": "capture",
              "label_fields": {"0.0": "abc", "1.0": 2, "2.0": 9, "3.0": 2.5, "4.0": -1},
              "dp_names": {"x": "Mic", "1.2": "Amp"}, "region_names": {"r": "A", "1": "B"},
              "forced_column_sections": {"abc": 8, "1": "x", "2": float("inf"), "3": 8},
              "config_csv@zz": "a.csv", "config_csv@3": 42, "config_csv@4": ""}
    with import_notes.collect() as notes:
        s = Session(cap, source=source)
    assert s.label_fields == {(1, 0): 2}
    assert s.dp_names == {(1, 2): "Amp"}
    assert s.region_names == {1: "B"}
    assert s.forced_column_sections == {3: 8}
    assert s.csv_sections == {}
    msgs = [n.message for n in notes]
    for field in ("label_fields", "dp_names", "region_names", "forced_column_sections",
                  "csv_sections"):
        assert sum(m.startswith(f"{field}:") for m in msgs) == 1, field
    assert any("forced_column_sections: 3 entries" in m for m in msgs)
    assert any("label_fields: 4 entries" in m for m in msgs)
    assert any("csv_sections: 3 entries" in m for m in msgs)


@pytest.mark.parametrize("source,field", [
    ({"type": "demo", "ssp_row": "abc"}, "ssp_row"),
    ({"type": "demo", "phy": None}, "phy"),
    ({"type": "demo", "delay_samples": 2.5}, "delay_samples"),
    ({"type": "sal", "path": "x.sal", "clock_channel": 0, "data_channel": 1,
      "window": [5]}, "window"),
    ({"type": "sal", "path": "x.sal", "clock_channel": 0, "data_channel": 1,
      "window": [None, 5]}, "window"),
    ({"type": "sal", "path": "x.sal", "clock_channel": 0, "data_channel": 1,
      "window": ["0", "7"]}, "window"),
    ({"type": "sal", "path": "x.sal", "clock_channel": True, "data_channel": 1},
     "clock_channel"),
    ({"type": "digital_csv", "path": "x.csv", "clock_col": "a", "data_col": 2},
     "clock_col"),
    ({"type": "demo", "delay_samples": -5}, "delay_samples"),
    ({"type": "demo", "delay_samples": 2**64}, "delay_samples"),
    ({"type": "demo", "ssp_row": 2**63}, "ssp_row"),
    ({"type": "demo", "ssp_row": -2}, "ssp_row"),
    ({"type": "demo", "config_csv": 5}, "config_csv"),
    ({"type": "demo", "dp_names": [1]}, "dp_names"),
    ({"type": "demo", "label_fields": "abc"}, "label_fields"),
    ({"type": "demo", "region_names": [["1", "A"]]}, "region_names"),
    ({"type": "demo", "forced_column_sections": 8}, "forced_column_sections"),
    ({"type": "sal", "path": 5, "clock_channel": 0, "data_channel": 1}, "path"),
    ({"type": "sal", "path": None, "clock_channel": 0, "data_channel": 1}, "path"),
    ({"type": "saleae_binary", "clock": "c.bin", "data": 3, "sample_rate_hz": 1}, "data"),
    ({"type": "wfm", "clock_path": ["a"], "data_path": "b.wfm"}, "clock_path"),
])
def test_a_malformed_source_field_raises_naming_it(source, field):
    with pytest.raises(ValueError, match=f"source {field}:"):
        Session.from_source(source)


# ---- inputs that a decode would refuse or ignore ----------------------------------------

@pytest.mark.parametrize("call", [
    lambda s: s.set_register_override(0, 1, 2**40, 3),
    lambda s: s.set_register_overrides({(0, 1, 2**40): 3}),
])
def test_an_input_the_binding_refuses_leaves_the_session_as_it_was(call):
    s = Session.from_demo(48, phy=2, cold_start=True)
    before, decoder = _state(s), s.decoder
    with pytest.raises(TypeError):
        call(s)
    assert _state(s) == before and s.decoder is decoder
    s.set_hub_depths({1: 2})                     # a later valid input decodes
    assert s.hub_depths == {1: 2} and s.decoder is not decoder


@pytest.mark.parametrize("call", [
    lambda s: s.set_hub_depths({10**12: 1}),
    lambda s: s.set_scrambler_overrides({(10**12, 0): True}),
])
def test_a_key_past_the_cores_range_is_left_out_before_the_decode(call):
    """A hub-depth device or scrambler override key past the core's int range never reaches
    the binding: it is left out with a note, and the session keeps its decode."""
    s = Session.from_demo(48, phy=2, cold_start=True)
    before, decoder = _state(s), s.decoder
    with import_notes.collect() as notes:
        call(s)
    assert notes and "left out" in notes[0].message
    assert _state(s) == before and s.decoder is decoder


@pytest.mark.platform
def test_a_stopped_ssp_re_phase_after_a_config_csv_undoes_the_csv(monkeypatch, csv_path,
                                                                    tmp_path):
    """apply_config_csv's second decode, the SSP re-phase, is stopped: the CSV, its decode
    and its upgrade temp copy go too, and the Session keeps its decode from before."""
    from swi3s_studio.ingest import visualizer_csv
    s = Session.from_demo(48, phy=2, cold_start=True)
    before, decoder, commands = _state(s), s.decoder, s.commands
    tmp = tmp_path / "upgraded.cur.csv"

    def upgraded(path):
        tmp.write_bytes(open_bytes(path))
        return str(tmp)
    monkeypatch.setattr(visualizer_csv, "normalized_path", upgraded)
    monkeypatch.setattr(Session, "_wire_supplies_an_ssp", lambda self: False)
    monkeypatch.setattr(Session, "sspa_ssp_row", lambda self: 100)
    real = S.run_watched
    runs = []

    def second_stops(decoder):
        runs.append(decoder)
        if len(runs) == 2:
            raise DecodeMemoryError(1, 2, 0.5)
        real(decoder)
    monkeypatch.setattr(S, "run_watched", second_stops)
    with pytest.raises(DecodeMemoryError):
        s.apply_config_csv(csv_path)
    assert len(runs) == 2
    assert _state(s) == before
    assert s.decoder is decoder and s.commands is commands and s.config_csv == ""
    assert not os.path.exists(tmp)


def test_a_column_pin_outside_the_range_is_left_out_with_a_note():
    with import_notes.collect() as notes:
        s = Session.from_demo(32, phy=2, forced_column_sections={"0": 1000, "1": 8})
    assert s.forced_column_sections == {1: 8}
    assert s.source["forced_column_sections"] == {"1": 8}
    assert any(n.message.startswith("forced_column_sections: 1 entry") for n in notes)


def test_a_forced_column_count_outside_the_range_is_detected_with_a_note():
    cap = Session.from_demo(32).capture
    with import_notes.collect() as notes:
        s = Session(cap, forced_column_count=33)
    assert s._forced_column_count != 33
    assert [g["column_count"] for g in s.segments] == \
        [g["column_count"] for g in Session(cap).segments]
    assert any("forced column count 33 is outside 2..32" in n.message for n in notes)


def test_the_column_range_repeats_the_cores():
    import swi3score
    assert (S._MIN_COLUMNS, S._MAX_COLUMNS) == (swi3score.min_column_count,
                                                 swi3score.max_column_count)


def test_an_analog_csv_channel_the_file_lacks_names_the_file_and_its_channels(monkeypatch):
    from swi3s_studio.ingest import analog_csv
    parsed = {"time": np.arange(4) * 1e-9, "sample_rate_hz": 1e9,
              "channels": {"A": np.zeros(4), "B": np.zeros(4)}}
    monkeypatch.setattr(analog_csv, "read_analog_csv", lambda path: parsed)
    for sel in ("CH1", 5, -1):
        with pytest.raises(ValueError, match=r"scope\.csv: no channel .*; the file has A, B"):
            Session.from_analog_csv("scope.csv", clock=sel, data="B")


# ---- geometry accessors at their edges -------------------------------------------------

def test_the_last_sample_is_the_last_edge_on_either_line():
    cap = SimpleNamespace(clock_edges=np.array([10, 20, 30]), data_edges=np.array([5, 45]))
    assert Session.last_sample(SimpleNamespace(capture=cap)) == 45
    cap.data_edges = np.array([5])
    assert Session.last_sample(SimpleNamespace(capture=cap)) == 30


def test_the_row_count_covers_the_row_of_the_last_sample():
    s = Session.from_demo(48, phy=2, cold_start=True)
    assert s.total_bus_rows() == s.bus_row_for_sample(s.last_sample()) + 1


def _rate_region_fake(row_opens, segments, audio_start=0):
    """A Session stand-in for rate_regions: rows opening at the samples `row_opens`, a
    1 MHz capture ending at the last one."""
    opens = np.asarray(row_opens)

    def row_for(sample):
        return int(np.searchsorted(opens, sample, side="right")) - 1

    return SimpleNamespace(
        last_sample=lambda: int(opens[-1]), sample_rate_hz=1_000_000, segments=segments,
        link_control=SimpleNamespace(audio_start_sample=audio_start),
        bus_row_for_sample=row_for, sample_at_bus_row=lambda r: int(opens[r]))


def test_a_region_is_rated_over_its_whole_rows_only():
    """Row 0 opens 130 samples before row 1 and the rest 100 apart: the rate is read from
    row 1 on, over whole rows, so the short opening row does not skew it."""
    fake = _rate_region_fake([0, 130, 230, 330, 430, 530],
                             [{"start_sample": 0, "column_count": 4}])
    [(start, end, ui, row)] = Session.rate_regions(fake)
    assert (start, end) == (0, 530)
    assert row == pytest.approx(10_000.0) and ui == pytest.approx(40_000.0)


@pytest.mark.parametrize("rows,rated", [(3, False), (4, True)])
def test_a_region_needs_two_whole_rows_between_its_ends(rows, rated):
    opens = [100 * k for k in range(rows + 1)]
    fake = _rate_region_fake(opens, [{"start_sample": 0, "column_count": 2}])
    [(_s, _e, ui, row)] = Session.rate_regions(fake)
    assert (row is not None) is rated
    if rated:
        assert row == pytest.approx(10_000.0)


def test_the_regions_start_at_the_audio_start():
    fake = _rate_region_fake([100 * k for k in range(30)],
                             [{"start_sample": 0, "column_count": 2}], audio_start=1000)
    regions = Session.rate_regions(fake)
    assert regions[0] == (0, 1000, None, None)
    assert regions[1][0] == 1000


def test_a_sample_on_a_row_sync_point_is_numbered_by_each_convention():
    """On a Row Sync Point edge the scalar row (cursor, commit anchoring) is the row it
    opens; the vectorised row (a data bit's closing edge) is the row before it."""
    s = Session.from_demo(48, phy=2, cold_start=True)
    row = int(s.segments[-1]["row_base"]) + 10
    edge = s.sample_at_bus_row(row)
    assert s.bus_row_for_sample(edge) == row
    assert list(s.bus_rows_for_samples(np.array([edge - 1, edge, edge + 1]))) == \
        [row - 1, row - 1, row]


def test_a_dlv_row_past_the_last_row_sync_opens_at_the_last():
    s = Session.from_demo(48, phy=3, cold_start=True)
    rsp = s._recovered_row_syncs()
    assert s.sample_at_bus_row(10 ** 9) == int(rsp[-1])
    assert s.sample_at_bus_row(int(rsp.size) - 1) == int(rsp[-1])


@pytest.mark.parametrize("field,value,note", [
    ("phy", 7, "demo phy 7 is not 1, 2 or 3"),
    ("variant", "bogus", "demo variant 'bogus' is not known"),
])
def test_a_demo_phy_or_variant_it_does_not_build_is_noted(field, value, note):
    with import_notes.collect() as notes:
        s = Session.from_source({"type": "demo", "audio_samples_per_channel": 8, field: value})
    assert any(note in n.message for n in notes), [n.message for n in notes]
    assert (s.source["phy"], s.source["variant"]) == (2, "")


def test_a_source_or_decoder_that_fails_to_build_leaves_the_decode(monkeypatch):
    s = Session.from_demo(16, phy=3, cold_start=True)
    before = (s._source, s.decoder, s._cds_horizontal_start, s._forced_column_count,
              list(s._replay_resets), dict(s.hub_depths))

    def fail(*a, **k):
        raise MemoryError("decoder")
    monkeypatch.setattr(S.swi3score, "Decoder", fail)
    with pytest.raises(MemoryError):
        s.set_hub_depths({0: 1})
    assert (s._source, s.decoder, s._cds_horizontal_start, s._forced_column_count,
            list(s._replay_resets), dict(s.hub_depths)) == before
    monkeypatch.undo()
    s.set_hub_depths({0: 1})
    assert s.hub_depths == {0: 1}


def test_a_dlv_row_with_no_recovered_row_sync_counts_from_column_0():
    """With the recovered Row Sync Points empty, bus_row_for_sample counts a DLV sample's
    row from its segment's anchor UI with the CDS column added back: a sample in Column 0
    or 1 of a row (before the CDS column) is that row, as the Row Sync Points give it."""
    s = Session.from_demo(32, phy=3, cold_start=True)
    rsp = s._recovered_row_syncs()
    seg = s.segments[-1]
    assert s.is_dlv and s._seg_cds(seg) == 2
    k0 = int(np.searchsorted(rsp, seg["start_sample"])) + 5
    ui = (int(rsp[k0 + 1]) - int(rsp[k0])) / seg["column_count"]
    samples = [int(rsp[k] + ui * c) for k in range(k0, k0 + 40, 3) for c in (0.5, 1.5, 8.5)]
    want = [s.bus_row_for_sample(x) for x in samples]
    s._rec_syncs_cache = np.zeros(0, dtype=np.int64)
    assert [s.bus_row_for_sample(x) for x in samples] == want


@pytest.mark.parametrize("bits", [2, 8, 16, 24])
def test_the_signed_value_is_twos_complement_at_the_most_negative(bits):
    half = 1 << (bits - 1)
    values = np.array([0, 1, half - 1, half, half + 1, 2 * half - 1])
    got = Session._signed(values, np.full(values.size, bits)).tolist()
    assert got == [0, 1, half - 1, -half, -half + 1, -1]


def test_a_one_bit_pdm_value_reads_as_minus_or_plus_one():
    assert Session._signed(np.array([0, 1]), np.array([1, 1])).tolist() == [-1, 1]


# ---- decode-input keys outside the core's range ------------------------------------------

@pytest.mark.parametrize("kw,field,left", [
    ({"forced_column_sections": {"99999999999999999999": 8, "1": 8}},
     "forced_column_sections", {"forced_column_sections": {1: 8}}),
    ({"forced_column_sections": {"-3": 8}}, "forced_column_sections",
     {"forced_column_sections": {}}),
    ({"forced_column_sections": {"0": 10 ** 30}}, "forced_column_sections",
     {"forced_column_sections": {}}),
    ({"csv_sections": {"-1": "a.csv"}}, "csv_sections", {"csv_sections": {}}),
    ({"hub_depths": {10 ** 30: 2, 3: 2}}, "hub_depths", {"hub_depths": {3: 2}}),
    ({"hub_depths": {-5: 2, 12: 1, "x": 1}}, "hub_depths", {"hub_depths": {}}),
    ({"scrambler_overrides": {(10 ** 30, 0): True, (-1, 0): False}}, "scrambler_overrides",
     {"scrambler_overrides": {(-1, 0): False}}),
    ({"scrambler_overrides": {(0, -1): True, (12, 0): True, (0, 2 ** 40): True, 5: True}},
     "scrambler_overrides", {"scrambler_overrides": {}}),
    ({"hub_depths": {1: 2.5, 2: 2.0, 3: 1}}, "hub_depths", {"hub_depths": {2: 2, 3: 1}}),
    ({"scrambler_overrides": {(1.5, 2): True, (1, 2.7): False, (0, 3.0): True}},
     "scrambler_overrides", {"scrambler_overrides": {(0, 3): True}}),
    ({"scrambler_overrides": {"12": True, (0, 1, 2): True, (0,): True, (0, 4): True}},
     "scrambler_overrides", {"scrambler_overrides": {(0, 4): True}}),
])
def test_a_decode_input_key_outside_the_cores_range_is_left_out_with_a_note(kw, field, left):
    """A section index outside 0..2**31-1, a hub-depth device outside 0..11 or a depth that is
    not a whole number, or a scrambler override key that is not a (device -1..11, data port
    0..2**31-1) pair of whole numbers is left out with one note naming the field, and the
    Session builds."""
    with import_notes.collect() as notes:
        s = Session.from_demo(16, **kw)
    for attr, want in left.items():
        assert getattr(s, attr) == want
    assert [n for n in notes if n.message.startswith(f"{field}: ")], \
        [n.message for n in notes]


def test_a_bad_key_given_to_a_setter_is_left_out_with_a_note():
    s = Session.from_demo(16)
    with import_notes.collect() as notes:
        s.set_hub_depths({2: 1, -1: 3})
        s.set_scrambler_overrides({(0, 0): False, (2 ** 40, 0): True})
    assert s.hub_depths == {2: 1} and s.scrambler_overrides == {(0, 0): False}
    assert {n.message.split(":")[0] for n in notes} >= {"hub_depths", "scrambler_overrides"}


# ---- the demo's size ---------------------------------------------------------------------

def test_the_demo_cap_is_the_native_generators_bound():
    import swi3score
    assert Session.DEMO_SAMPLES_MAX == swi3score.max_demo_samples == 48_000
    makers = [n for n in dir(swi3score) if n.startswith("make_")]
    assert len(makers) >= 16
    for name in makers:
        args = (Session.DEMO_SAMPLES_MAX + 1,)
        if name == "make_running_port_write_levels":
            args += (0, 0)                     # offset and value have no default
        with pytest.raises(ValueError, match="is above 48000"):
            getattr(swi3score, name)(*args)


# ---- a DLV config's row rate -------------------------------------------------------------

def test_a_dlv_regions_exported_row_rate_is_that_regions():
    """On DLV the row rate holds while the UI rate follows the columns: the Safe-Lock-4
    region and the 16-column one each export the row rate rate_regions measures for them,
    not the final UI rate over the region's columns."""
    s = Session.from_demo(64, cold_start=True, phy=3)
    regions = [r for r in s.rate_regions() if r[3]]
    assert len(regions) == 2
    for start, end, _ui, row in regions:
        cfg = s.config_dataports_at((start + end) // 2)
        assert cfg["row_rate_khz"] == pytest.approx(row / 1000.0)
        assert cfg["row_rate_khz"] == pytest.approx(3072.0, rel=1e-4)


def test_a_dlv_sample_before_any_rated_region_exports_the_decoders_row_rate():
    s = Session.from_demo(64, cold_start=True, phy=3)
    unrated = [r for r in s.rate_regions() if not r[3]]
    assert unrated and unrated[0][0] == 0 and unrated[0][1] > 1000
    cfg = s.config_dataports_at(1000)
    assert cfg["row_rate_khz"] == pytest.approx(s.row_rate_khz)


# ---- a re-decode that fails after its decoder is installed -------------------------------

@pytest.mark.parametrize("step", ["_refresh_audio", "_reload_csv_seed", "_build_register_files"])
def test_a_failure_after_the_decode_ran_keeps_the_previous_decode(monkeypatch, step):
    s = Session.from_demo(48, phy=2, cold_start=True)
    before = _state(s)
    kept = (s.decoder, s.commands, s.segments, s._audio_cols, s.register_files, s._source)

    def fail(*a, **k):
        raise MemoryError(step)
    with monkeypatch.context() as m:
        m.setattr(Session, step, fail)
        with pytest.raises(MemoryError):
            s.set_hub_depths({1: 2})
    assert _state(s) == before
    assert (s.decoder, s.commands, s.segments, s._audio_cols, s.register_files,
            s._source) == kept
    s.set_hub_depths({1: 2})
    assert s.hub_depths == {1: 2} and s.decoder is not kept[0]


# ---- clock and data must be two lines ------------------------------------------------------

@pytest.mark.parametrize("clock,data", [("CH1", "CH1"), (0, 0), ("CH1", 0), (1, "CH2")])
def test_an_analog_csv_clock_and_data_on_one_channel_is_refused(monkeypatch, clock, data):
    from swi3s_studio.ingest import analog, analog_csv
    parsed = {"time": np.arange(8) * 1e-9, "sample_rate_hz": 1e9,
              "channels": {"CH1": np.zeros(8), "CH2": np.ones(8)}}
    monkeypatch.setattr(analog_csv, "read_analog_csv", lambda path: parsed)
    monkeypatch.setattr(analog, "capture_from_analog",
                        lambda *a, **k: pytest.fail("the pair was read"))
    with pytest.raises(ValueError, match="the clock and the data are the same channel"):
        Session.from_analog_csv("scope.csv", clock=clock, data=data)


def test_a_wfm_clock_and_data_in_one_file_is_refused(monkeypatch, tmp_path):
    from swi3s_studio.ingest import wfm as wfm_ingest
    monkeypatch.setattr(wfm_ingest, "read_wfm_pair",
                        lambda a, b: pytest.fail("the pair was read"))
    one = str(tmp_path / "ch1.wfm")
    with pytest.raises(ValueError, match="the clock and the data are the same file"):
        Session.from_wfm(one, one)
    with pytest.raises(ValueError, match="the same file"):
        Session.from_wfm(one, str(tmp_path / "." / "ch1.wfm"))


# ---- source fields read strictly -----------------------------------------------------------

@pytest.mark.parametrize("source,field", [
    ({"type": "vcd", "path": "a.vcd", "data_ident": "!"}, "clock_ident"),
    ({"type": "vcd", "path": "a.vcd", "clock_ident": None, "data_ident": "!"}, "clock_ident"),
    ({"type": "vcd", "path": "a.vcd", "clock_ident": "!", "data_ident": 5}, "data_ident"),
    ({"type": "vcd", "path": "a.vcd", "clock_ident": "!", "data_ident": "#",
      "auto_clock": "false"}, "auto_clock"),
    ({"type": "wfm", "clock_path": "a.wfm", "data_path": "b.wfm", "clock_thresh": "abc"},
     "clock_thresh"),
    ({"type": "wfm", "clock_path": "a.wfm", "data_path": "b.wfm", "data_thresh": [0.2, 0.8]},
     "data_thresh"),
    ({"type": "wfm", "clock_path": "a.wfm", "data_path": "b.wfm",
      "clock_thresh": [float("nan"), 0.1]}, "clock_thresh"),
    ({"type": "wfm", "clock_path": "a.wfm", "data_path": "b.wfm", "auto_clock": 1},
     "auto_clock"),
    ({"type": "analog_csv", "path": "s.csv", "clock_thresh": [1, True]}, "clock_thresh"),
    ({"type": "demo", "cold_start": "yes"}, "cold_start"),
])
def test_a_source_field_that_does_not_read_raises_naming_it(source, field):
    with pytest.raises(ValueError, match=f"source {field}:"):
        Session.from_source(source)


def test_a_threshold_pair_reads_as_given(monkeypatch):
    got = {}
    monkeypatch.setattr(Session, "from_wfm", classmethod(lambda cls, *a, **k: got.update(k)))
    Session.from_source({"type": "wfm", "clock_path": "a.wfm", "data_path": "b.wfm",
                         "clock_thresh": [0.8, 0.2], "auto_clock": False})
    assert got["clock_thresh"] == [0.8, 0.2] and got["data_thresh"] is None
    assert got["auto_clock"] is False


# ---- the forced-DLV grid follows the last Cold Start ---------------------------------------

def test_the_forced_dlv_grid_replays_the_config_since_the_last_cold_start():
    import swi3score
    s = Session.from_demo(16, cold_start=True, phy=3)
    cmds = s._config_commands()
    after = max(int(c["start_sample"]) for c in cmds) + 1
    s._dlv_forced = True
    s._replay_start = lambda sample: after          # a Cold Start after every config write

    def grid(replay):
        return swi3score.grid_from_commands(replay, 32, force_columns=int(s.column_count),
                                            dlv=True,
                                            cds_horizontal_start=s._cds_horizontal_start)
    assert s.grid_cells() == grid([])
    assert s.grid_cells() != grid(s._grid_replay(cmds)), "test premise: the config shows"


# ---- the value filter ---------------------------------------------------------------------

def test_an_unknown_value_filter_operator_raises():
    s = Session.from_demo(16)
    for op in ("<=", "<", "==", "!=", ">=", ">"):
        s._filtered_positions(value_pred=(op, 0))
    with pytest.raises(ValueError, match="value_pred"):
        s._filtered_positions(value_pred=("=", 5))


def test_the_value_filter_keeps_the_samples_its_comparison_passes():
    s = Session.from_demo(16)
    m, _ = s._filtered_positions()
    signed = m["signed"]
    for op, want in (("<=", signed <= 3), ("<", signed < 3), ("==", signed == 3),
                     ("!=", signed != 3), (">=", signed >= 3), (">", signed > 3)):
        s._filtered_cache = None
        _m, pos = s._filtered_positions(value_pred=(op, 3))
        assert np.array_equal(pos, np.flatnonzero(want)), op


# ---- per-section UI statistics -------------------------------------------------------------

@pytest.mark.parametrize("idle", [5, 12, 40])
def test_section_ui_stats_leaves_out_idle_gaps(idle):
    """Bursts of 10-sample UIs (10 ns at 1 GS/s) separated by idle gaps `idle` UIs long:
    the UI is the burst UI, its min and max too."""
    from swi3s_studio.ingest.capture import Capture
    s = Session.from_demo(16)
    burst = np.arange(200) * 10
    period = burst[-1] + idle * 10
    edges = np.concatenate([burst + k * period for k in range(6)]).astype(np.uint64)
    s.capture = Capture(edges, edges.copy(), False, False, 1_000_000_000)
    s.segments = [{"start_sample": 0, "column_count": 4}]
    s._dlv_forced = False
    [row] = s.section_ui_stats()
    assert (row["ui_ns"], row["ui_min_ns"], row["ui_max_ns"]) == (10.0, 10.0, 10.0)


# ---- a DLV sample's UI ---------------------------------------------------------------------

def test_a_dlv_sample_belongs_to_the_ui_whose_sample_point_is_nearest():
    """A DLV segment's start_sample is its first UI's mid-UI sample point: a sample within
    0.3 UI either side of UI k's sample point is UI k, on each segment's own UI."""
    s = Session.from_demo(64, cold_start=True, phy=3)
    ui = s.recovered_clock()[1]
    for seg in s.segments:
        u = ui * s.column_count / int(seg["column_count"])
        start, first = int(seg["start_sample"]), int(seg["start_ui"])
        for k in range(1, 60, 7):
            assert s.ui_index(round(start + (k - 0.3) * u)) == first + k, (seg, k)
            assert s.ui_index(round(start + (k + 0.3) * u)) == first + k, (seg, k)
