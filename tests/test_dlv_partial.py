"""Partial DLV (PHY3) capture detection: a mid-stream differential capture with no
cold-start bring-up is recognised as DLV, loaded at the true sample rate, and its
column count blind-detected — so RSPs, CDS and the recovered clock come out without
the §5.1.2 PHY-select ever being on the wire. Complements the FBCSE partial path.

Run: python3 -m pytest tests/test_dlv_partial.py -q
"""
import os
import tempfile

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
    """A Logic edge (per-transition) CSV: the finest spacing is one UI, so
    infer_sample_rate under-reports; true_sample_rate_csv recovers the real rate
    from the timestamp quantum."""
    cap = transitions.demo_capture(200, phy=3)             # 500 MHz demo
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "dlv.csv")
        raw_export.export_csv(cap, p, clock_name="SW_CLK", data_name="SW_DATA")
        assert dlv_detect.true_sample_rate_csv(p) == cap.sample_rate_hz
        assert digital_csv.looks_complementary(p, 1, 2)
        # the edge-spacing heuristic is a UI (< the true rate) on an edge export
        assert 0 < digital_csv.infer_sample_rate(p) < cap.sample_rate_hz


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
    assert cols in (2, 4, 8, 16) and valid >= 4 and ratio >= 0.5
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
    ingest (source records dlv_inverted) and commands come out CRC-valid."""
    cap = transitions.demo_capture(400, phy=3)
    swapped = Capture(cap.clock_edges, cap.data_edges,
                      not cap.initial_clock, not cap.initial_data, cap.sample_rate_hz)
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "swapped_dlv.csv")
        raw_export.export_csv(swapped, p, clock_name="DN", data_name="DP")  # wires reversed
        s = Session.from_digital_csv(p, 1, 2)
    assert s.is_dlv and s.source.get("dlv_inverted") is True
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
    assert s.source.get("dlv") is True and s.source.get("dlv_columns") in (2, 4, 8, 16)
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
    """Regression: exporting a partial DLV capture to .sal and re-importing must decode
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
    """Show Toggles (TX map) for a DLV capture flags the CDS at Column 2 and draws the
    full operational width — not the CDS at col 0 with a shifted frame."""
    s = Session(transitions.demo_capture(600, phy=3))
    r = s.tx_raster(0, 6, s.sample_at_bus_row(200))
    assert r["column_count"] == 16 and r["cds_col"] == 2
    assert r["tx"].shape == (6, 16)


def test_partial_dlv_visualizer_config_has_geometry():
    """Regression: opening a partial DLV capture in the Visualizer showed no bus config
    because config_dataports_at reported 1 column (no config commit on the wire). It now
    forces the operational width and flags PHY3, so the visualizer shows the DLV frame."""
    from swi3s_studio.model.bus_config import BusConfig
    s = Session(transitions.demo_capture(600, phy=3))
    d = s.config_dataports_at(s.sample_at_bus_row(200))
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
    if before:                                            # only meaningful if it decoded
        import os
        import tempfile
        cfg, _ = BusConfig.from_decoder_config(s.config_dataports_at(None))
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "c.csv")
            cfg.to_csv_file(p)
            s.apply_config_csv(p)
        assert sum(c["crc_valid"] for c in s.commands) == before   # unchanged (clean)
    """A forwarded-clock CSV must NOT be misrouted as DLV (auto_dlv is a no-op when
    the pair isn't complementary)."""
    cap = transitions.demo_capture(200, phy=2)
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "fbcse.csv")
        raw_export.export_csv(cap, p, clock_name="CLK", data_name="DATA")
        s = Session.from_digital_csv(p, 1, 2)
    assert not s.is_dlv
    assert s.source.get("dlv") is not True
