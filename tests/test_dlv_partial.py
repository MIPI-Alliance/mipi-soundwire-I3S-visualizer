"""Partial DLV (PHY3) capture detection: a mid-stream differential capture with no
cold-start bring-up is recognised as DLV, loaded at the true sample rate, and its
column count blind-detected — so RSPs, CDS and the recovered clock come out without
the §5.1.2 PHY-select ever being on the wire. Complements the FBCSE partial path. A pair
sampled too slowly for one sample per DLV UI is not decoded as DLV and says so, and a DLV
TX-map region ends at the last recovered Row Sync Point. Detection needs both min_valid
CRC-valid commands and a valid ratio of min_valid_ratio; the rate reader raises ValueError
naming the file for a CSV it cannot read as text, reads the times in the header's Time unit,
and stops at the first blank row. A pair is complementary while at most 0.5 % of its edges
are missing or sit on another sample.

Run: python3 -m pytest tests/test_dlv_partial.py -q
"""
import os
import tempfile

import numpy as np
import pytest

from swi3s_studio import import_notes
from swi3s_studio.analysis import dlv_detect
from swi3s_studio.ingest import digital_csv, raw_export, transitions
from swi3s_studio.ingest.capture import Capture
from swi3s_studio.session import Session


def test_is_complementary_pair():
    dlv = transitions.demo_capture(200, phy=3)             # differential pair
    fbcse = transitions.demo_capture(200, phy=2)           # forwarded clock + NRZS data
    assert dlv_detect.is_complementary(dlv)
    assert not dlv_detect.is_complementary(fbcse)


def test_true_sample_rate_from_edge_csv():
    """A Logic edge (per-transition) CSV: the finest spacing is one UI, not one sample.
    true_sample_rate_csv and digital_csv.infer_sample_rate (the grid the timestamps sit on)
    both recover the real rate."""
    cap = transitions.demo_capture(200, phy=3)             # 500 MHz demo
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "dlv.csv")
        raw_export.export_csv(cap, p, clock_name="SW_CLK", data_name="SW_DATA")
        assert dlv_detect.true_sample_rate_csv(p) == cap.sample_rate_hz
        assert digital_csv.looks_complementary(p, 1, 2)
        assert digital_csv.infer_sample_rate(p) == cap.sample_rate_hz   # the timestamp grid


def test_fbcse_csv_not_complementary():
    """A forwarded-clock capture's channels are independent (~half the rows differ),
    so it is NOT flagged complementary and stays on the normal (FBCSE) load path."""
    cap = transitions.demo_capture(200, phy=2)
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "fbcse.csv")
        raw_export.export_csv(cap, p, clock_name="CLK", data_name="DATA")
        assert not digital_csv.looks_complementary(p, 1, 2)


def test_detect_columns_on_partial_dlv():
    cap = transitions.demo_capture(400, phy=3)
    det = dlv_detect.detect_columns(cap)
    assert det is not None
    cols, valid, total, ratio, row_rate, inverted = det
    assert cols == 4 and valid >= 4 and ratio >= 0.5       # the demo starts in Safe-Lock-4
    assert abs(row_rate - 3072.0) < 60.0                   # ~3.072 MRows/s reference
    assert inverted is False                               # not DP/DN-swapped


def test_detect_handles_swapped_dp_dn():
    """A DP/DN-swapped capture inverts every logical level, so the CDS/8b10b fail at the
    default polarity; detection must try the flipped polarity and report inverted=True
    (still recovering the same column count)."""
    cap = transitions.demo_capture(400, phy=3)
    swapped = Capture(cap.clock_edges, cap.data_edges,
                      not cap.initial_clock, not cap.initial_data, cap.sample_rate_hz)
    det = dlv_detect.detect_columns(swapped)
    assert det is not None and det[5] is True and det[3] >= 0.5   # inverted, decodes


def test_from_digital_csv_auto_dlv_swapped():
    """A swapped DLV edge CSV still loads as DLV: the polarity is auto-corrected at
    ingest (dlv_detected reports it inverted), the swap is the one import note, and
    commands come out CRC-valid."""
    cap = transitions.demo_capture(400, phy=3)
    swapped = Capture(cap.clock_edges, cap.data_edges,
                      not cap.initial_clock, not cap.initial_data, cap.sample_rate_hz)
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "swapped_dlv.csv")
        raw_export.export_csv(swapped, p, clock_name="DN", data_name="DP")  # wires reversed
        with import_notes.collect() as notes:
            s = Session.from_digital_csv(p, 1, 2)
    assert [(n.source, n.message) for n in notes] == [
        ("swapped_dlv.csv", "the differential pair's two channels are swapped (DN on the "
                            "clock channel); they are read the other way round")]
    assert s.is_dlv and s.dlv_detected[1] is True
    assert s.commands and all(c["crc_valid"] for c in s.commands)


def test_from_digital_csv_auto_detects_dlv():
    """The headline path: a partial DLV capture as a Logic edge CSV loads straight
    through Session.from_digital_csv as DLV — RSPs, CDS-driven CRC-valid commands,
    the recovered clock, and the 16-column geometry, with no cold start."""
    cap = transitions.demo_capture(400, phy=3)
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "partial_dlv.csv")
        raw_export.export_csv(cap, p, clock_name="SW_CLK", data_name="SW_DATA")
        s = Session.from_digital_csv(p, 1, 2)
    assert s.is_dlv
    assert s.dlv_detected[0] in (2, 4, 8, 16)
    # The detection is not a decode input: the source descriptor does not carry it.
    assert not {"dlv", "dlv_columns", "dlv_inverted"} & set(s.source)
    assert s.sample_rate_hz == cap.sample_rate_hz           # true rate, not the UI rate
    assert s.column_count == 16                             # commits Safe-Lock -> 16
    assert s.commands and all(c["crc_valid"] for c in s.commands)
    rc = s.recovered_clock()
    assert rc is not None and rc[0].size > 0 and rc[1] > 0.0   # RSP samples + recovered UI


def test_session_autodetects_partial_dlv():
    """Any source (not just the CSV importer) auto-detects a partial DLV capture: a
    complementary pair with no cold-start decodes through the recovered-clock path."""
    s = Session(transitions.demo_capture(600, phy=3))          # no dlv= -> auto-detect
    assert s.is_dlv and s.column_count == 16
    assert s.commands and all(c["crc_valid"] for c in s.commands)
    assert not Session(transitions.demo_capture(200, phy=2)).is_dlv   # FBCSE not misdetected


def test_sal_roundtrip_redetects_dlv():
    """Exporting a partial DLV capture to .sal and re-importing must decode
    (the DLV auto-detection runs in the Session ctor, so it covers .sal / .bin, not just
    the CSV importer)."""
    s = Session(transitions.demo_capture(600, phy=3))
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "rt.sal")
        s.export_sal(p, clock_name="DP", data_name="DN")
        s2 = Session.from_sal(p, 0, 1)
    assert s2.is_dlv and s2.column_count == 16
    assert s2.commands and all(c["crc_valid"] for c in s2.commands)


def test_tx_raster_dlv_cds_at_column_2():
    """Show Toggles (TX map) for a DLV capture's operational region flags the CDS at
    Column 2 and draws the full operational width — not the CDS at col 0 with a shifted
    frame."""
    s = Session(transitions.demo_capture(600, phy=3))
    r = s.tx_raster(0, 6, s.sample_at_bus_row(int(s.segments[-1]["row_base"]) + 200))
    assert r["column_count"] == 16 and r["cds_col"] == 2
    assert r["tx"].shape == (6, 16)


def test_a_dlv_tx_region_ends_at_the_last_recovered_row_sync():
    """A region whose next region starts past the last recovered Row Sync Point ends at that
    point: the TX map counts only rows that have one."""
    s = Session.from_demo(48, phy=3, cold_start=True)
    first, second = s.segments[0], s.segments[1]
    nrsp = int(s._recovered_row_syncs().size)
    second["row_base"] = nrsp + 1000
    origin, cols, end_ui, row_lo, row_hi = s._tx_dlv_region(first)
    assert (row_lo, row_hi) == (int(first["row_base"]), nrsp)
    assert end_ui == origin + (nrsp - row_lo) * cols


def test_partial_dlv_visualizer_config_has_geometry():
    """A DLV capture with no bring-up: config_dataports_at in its operational region has
    the 16-column width and flags PHY3, so the Visualizer shows the DLV frame."""
    from swi3s_studio.model.bus_config import BusConfig
    s = Session(transitions.demo_capture(600, phy=3))
    d = s.config_dataports_at(s.sample_at_bus_row(int(s.segments[-1]["row_base"]) + 200))
    assert int(d["num_columns"]) + 1 == 16 and d["phy3_enabled"] is True
    cfg, _n = BusConfig.from_decoder_config(d)
    assert cfg.column_count() == 16 and cfg.phy3_enabled is True
    # the whole-capture (sample=None) config must flag PHY3 too (Export Visualizer CSV)
    assert s.config_dataports_at(None).get("phy3_enabled") is True


def test_single_region_dlv_config_roundtrips_clean():
    """Applying a DLV capture's own saved config re-decodes cleanly when the capture is a
    single region (no reconfiguration) — the multi-region case is the one that misframes
    (a single config imposed from row 0 can't represent Safe-Lock-4 -> 16-col), which the
    UI warns about before imposing."""
    from swi3s_studio.model.bus_config import BusConfig
    cap = transitions.demo_capture(700, phy=3)
    op = cap.subcapture(cap.clock_edges[-1] // 2, int(cap.clock_edges[-1]))   # operational-only
    s = Session(op)
    assert s.is_dlv and len({seg["column_count"] for seg in s.segments}) == 1  # one region
    before = sum(c["crc_valid"] for c in s.commands)
    assert before > 0, "the operational slice decodes no valid command"
    cfg, _ = BusConfig.from_decoder_config(s.config_dataports_at(None))
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "c.csv")
        cfg.to_csv_file(p)
        s.apply_config_csv(p)
    assert sum(c["crc_valid"] for c in s.commands) == before   # unchanged (clean)


def test_from_digital_csv_fbcse_stays_fbcse():
    """A forwarded-clock CSV must NOT be misrouted as DLV (auto_dlv is a no-op when
    the pair isn't complementary)."""
    cap = transitions.demo_capture(200, phy=2)
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "fbcse.csv")
        raw_export.export_csv(cap, p, clock_name="CLK", data_name="DATA")
        s = Session.from_digital_csv(p, 1, 2)
    assert not s.is_dlv
    assert s.dlv_detected is None


@pytest.mark.parametrize("samples", [160, 600])
def test_detection_keeps_the_width_the_capture_starts_at(samples):
    """A capture that starts in Safe-Lock-4 and commits to 16 columns is detected at 4: a
    decode forced to 16 also decodes cleanly, but only the commands after the commit, so it
    must not win. The Session then reads every command, the Safe-Lock config included, and
    follows the commit to 16 columns."""
    cap = transitions.demo_capture(samples, phy=3)
    at = {cols: dlv_detect.detect_columns(cap, candidates=(cols,)) for cols in (4, 16)}
    assert at[16] is not None and at[16][3] == 1.0          # clean at 16 ...
    assert at[4][1] > at[16][1]                             # ... but 4 reads more
    assert dlv_detect.detect_columns(cap)[0] == 4
    s = Session(cap)
    assert [seg["column_count"] for seg in s.segments] == [4, 16]
    assert len(s.commands) == at[4][2] and all(c["crc_valid"] for c in s.commands)
    first_write = next(c for c in s.commands if c["command"] == "WriteA32")
    assert first_write["address"] == 0x1081                 # NumColumns: the Safe-Lock setup


def test_detection_of_a_mid_stream_operational_capture_is_16():
    cap = transitions.demo_capture(700, phy=3)
    op = cap.subcapture(cap.clock_edges[-1] // 2, int(cap.clock_edges[-1]))   # operational-only
    assert dlv_detect.detect_columns(op)[0] == 16


def test_a_pair_too_slow_for_a_dlv_ui_is_not_decoded_as_dlv(monkeypatch):
    """At 1 MS/s a 3.072 MRows/s row spans a third of a sample, so no width has a sample per
    UI: detection tries none, and the Session loads the pair without the recovered clock and
    says why."""
    e = np.arange(1, 2001, dtype=np.uint64) * 3
    cap = Capture(clock_edges=e, data_edges=e.copy(), initial_clock=False,
                  initial_data=True, sample_rate_hz=1_000_000)
    assert dlv_detect.is_complementary(cap)
    assert dlv_detect.rate_too_low(1_000_000)
    assert not dlv_detect.rate_too_low(cap.sample_rate_hz * 7)      # 2 columns: >1 sample/UI

    def refused(*a, **k):
        raise AssertionError("a DLV decode was attempted")
    monkeypatch.setattr(dlv_detect.swi3score, "DlvSampleSource", refused)
    assert dlv_detect.detect_columns(cap) is None
    with import_notes.collect() as notes:
        sess = Session(cap)
    assert not sess.is_dlv
    [note] = [n for n in notes if "differential pair" in n.message]
    assert "1,000,000 Hz" in note.message and "not decoded as PHY3" in note.message


# ---- detection rules at their limits ---------------------------------------------------

def test_dlv_detect_repeats_the_cores_dlv_constants():
    import swi3score
    assert dlv_detect.DLV_CDS_COLUMN == swi3score.dlv_cds_column
    assert dlv_detect.MIN_UI_SAMPLES == swi3score.dlv_min_ui_samples


def test_the_dlv_row_rate_has_one_definition():
    from swi3s_studio.ingest import transitions
    assert dlv_detect.DLV_ROW_RATE_HZ is transitions.DLV_ROW_RATE_HZ
    assert transitions.DLV_ROW_RATE_HZ == 3_072_000.0


def _fake_core(cmds):
    from types import SimpleNamespace

    class Decoder:
        def __init__(self, src, settings):
            self.row_rate_khz = 3072.0

        def run(self):
            pass

        def commands(self):
            return cmds
    return SimpleNamespace(DlvSampleSource=lambda *a: None, DecoderSettings=SimpleNamespace,
                           Decoder=Decoder)


def test_a_tie_between_widths_goes_to_the_narrowest(monkeypatch):
    """Every candidate width and polarity decodes the same commands: the first tried, the
    narrowest at the capture's own polarity, is kept."""
    cmds = [{"crc_valid": True}] * 6 + [{"crc_valid": False}] * 2
    monkeypatch.setattr(dlv_detect, "swi3score", _fake_core(cmds))
    det = dlv_detect.detect_columns(transitions.demo_capture(200, phy=3))
    assert det is not None and (det[0], det[1], det[5]) == (2, 6, False)


def test_a_pair_whose_edge_counts_differ_past_the_tolerance_is_not_complementary():
    """The edge counts may differ by `tol` (0.5 %) of the larger: 4 of 1000 do, 6 do not."""
    edges = np.arange(1, 1001, dtype=np.uint64) * 10
    for missing, pair in ((4, True), (6, False)):
        cap = Capture(edges, edges[:-missing], False, True, 1_000_000)
        assert dlv_detect.is_complementary(cap) is pair, missing


def test_a_pair_whose_edges_differ_in_place_past_the_tolerance_is_not_complementary():
    """Equal edge counts, and `tol` (0.5 %) of the edges may sit on another sample: 5 of
    1000 do, 6 do not."""
    edges = np.arange(1, 1001, dtype=np.uint64) * 10
    for moved, pair in ((0, True), (5, True), (6, False), (100, False)):
        data = edges.copy()
        data[np.arange(moved) * 7 + 3] += np.uint64(1)
        cap = Capture(edges, data, False, True, 1_000_000)
        assert dlv_detect.is_complementary(cap) is pair, moved


@pytest.mark.parametrize("unit,scale", [("s", 1.0), ("ms", 1e3), ("us", 1e6),
                                        ("\u00b5s", 1e6), ("ns", 1e9), ("ps", 1e12)])
def test_the_rate_reads_the_header_time_unit(tmp_path, unit, scale):
    """Edges on an 8 ns grid (125 MHz) give 125 MHz in any Time unit the header names, as
    digital_csv scales the times, with no note."""
    gaps = np.tile([8, 16, 24, 40], 50) * 1e-9
    times = np.cumsum(gaps) * scale
    p = tmp_path / "dlv.csv"
    p.write_text(f"Time [{unit}],A,B\n" + "".join(
        f"{float(t)!r},{i % 2},{1 - i % 2}\n" for i, t in enumerate(times)), encoding="utf-8")
    with import_notes.collect() as notes:
        assert dlv_detect.true_sample_rate_csv(str(p)) == 125_000_000
    assert notes == []


def test_the_rate_is_read_up_to_the_first_blank_row(tmp_path):
    """Rows after a blank line do not enter the rate: 1 µs edges before it give 1 MHz
    though the rows after it sit on a 10 ns grid."""
    before = [f"{i * 1e-6:.9f},{i % 2},{1 - i % 2}\n" for i in range(20)]
    after = [f"{20e-6 + i * 1e-8:.9f},{i % 2},{1 - i % 2}\n" for i in range(1, 20)]
    p = tmp_path / "blank.csv"
    p.write_text("Time [s],A,B\n" + "".join(before) + "\n" + "".join(after),
                 encoding="utf-8")
    assert dlv_detect.true_sample_rate_csv(str(p)) == 1_000_000
    whole = tmp_path / "whole.csv"
    whole.write_text("Time [s],A,B\n" + "".join(before + after), encoding="utf-8")
    assert dlv_detect.true_sample_rate_csv(str(whole)) == 100_000_000


def test_a_rate_is_snapped_only_within_two_percent_of_a_standard_one():
    assert dlv_detect.snap_to_standard_rate(101_500_000) == 100_000_000      # 1.5 % off
    assert dlv_detect.snap_to_standard_rate(102_500_000) == 102_500_000      # 2.5 %: kept
    assert dlv_detect.snap_to_standard_rate(102_500_400) == 102_500_000      # to the kHz


def test_a_non_finite_or_huge_time_is_left_out_of_the_rate(tmp_path):
    """A nan or inf time cell, or a spacing too large for the picosecond GCD, does not
    enter the rate: 1 µs edges give 1 MHz around any of them."""
    rows = [f"{i * 1e-6:.9f},{i % 2},{1 - i % 2}\n" for i in range(20)]
    for bad in ("inf", "nan", "-inf", "1e10"):
        p = tmp_path / "dlv.csv"
        p.write_text("Time [s],A,B\n" + "".join(rows[:10]) + f"{bad},0,1\n" + "".join(rows[10:]),
                     encoding="utf-8")
        with import_notes.collect() as notes, np.errstate(all="raise"):
            assert dlv_detect.true_sample_rate_csv(str(p)) == 1_000_000, bad
        assert notes == [], bad


def test_a_spacing_under_500_hz_names_no_rate(tmp_path):
    """Timestamps 1 s apart give a 1 Hz sample rate, which rounds to no analyzer rate: the
    rate is not used (0), and the note says so rather than naming a 0 Hz snap."""
    p = tmp_path / "slow.csv"
    p.write_text("Time [s],A,B\n" + "".join(f"{t},{t % 2},{1 - t % 2}\n" for t in range(8)),
                 encoding="utf-8")
    with import_notes.collect() as notes:
        assert dlv_detect.true_sample_rate_csv(str(p)) == 0
    msgs = [n.message for n in notes]
    assert any("rounds to no analyzer rate" in m for m in msgs), msgs
    assert not any("snapped to the standard rate of 0" in m for m in msgs), msgs


@pytest.mark.parametrize("valid,invalid,detected", [
    (4, 4, True),            # ratio 0.5: accepted
    (4, 5, False),           # enough valid commands, ratio under 0.5
    (40, 41, False),
    (3, 0, False),           # ratio 1, too few valid commands
])
def test_detection_needs_both_the_valid_count_and_the_valid_ratio(monkeypatch, valid,
                                                                  invalid, detected):
    cmds = [{"crc_valid": True}] * valid + [{"crc_valid": False}] * invalid
    monkeypatch.setattr(dlv_detect, "swi3score", _fake_core(cmds))
    det = dlv_detect.detect_columns(transitions.demo_capture(200, phy=3))
    assert (det is not None) is detected, det


def test_a_csv_cell_past_the_field_limit_is_a_value_error_naming_the_file(tmp_path):
    p = tmp_path / "wide.csv"
    p.write_text("Time [s],A,B\n0.0,0,1\n" + "x" * 200_000 + ",1,0\n", encoding="utf-8")
    with pytest.raises(ValueError, match="wide.csv"):
        dlv_detect.true_sample_rate_csv(str(p))
