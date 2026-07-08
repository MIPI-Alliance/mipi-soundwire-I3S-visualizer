"""Visualization-mode authoring tests (pure model, no Qt):

- BusConfig serialises to the v2.0 CSV the engine/core read, and CSV / dict
  round-trip.
- An authored config is placed by the verified C++ cascade (`grid_from_csv`), with
  wide-bit held columns carrying the bit identity.
- Every data port gets a name in the CSV (defaults like the Visualizer).

Clash detection / validation parity is covered end-to-end by
`test_visualizer_engine.py` (the Visualizer's own 89-config JSON testsuite), since
Bus-Visualizer mode is now driven by the vendored Visualizer engine.

Run: PYTHONPATH=. python3 tests/test_authoring.py
"""
import os
import tempfile

import swi3score

from swi3s_studio.model.bus_config import BusConfig, demo_config


def test_csv_and_dict_roundtrip():
    cfg = demo_config()
    cfg.dataports[2].enabled = True
    cfg.dataports[2].enable_ch = 0b1111
    cfg.dataports[2].device_number = 3
    with tempfile.TemporaryDirectory() as d:
        path = cfg.to_csv_file(os.path.join(d, "c.csv"))
        back = BusConfig.from_csv(path)
    assert back.num_columns == cfg.num_columns
    assert back.column_count() == cfg.column_count()
    assert back.dataports[0].enable_ch == cfg.dataports[0].enable_ch
    assert back.dataports[2].device_number == 3
    assert back.dataports[2].enable_ch == 0b1111
    # dict round-trip (workspace persistence)
    back2 = BusConfig.from_dict(cfg.to_dict())
    assert back2.dataports[2].enable_ch == 0b1111
    assert back2.dataports[0].scrambler_en == cfg.dataports[0].scrambler_en


def test_authored_config_places_via_core():
    cfg = demo_config()
    with tempfile.TemporaryDirectory() as d:
        path = cfg.to_csv_file(os.path.join(d, "c.csv"))
        cells = swi3score.grid_from_csv(path, 48)
        writes = swi3score.registers_from_csv(path)
    data = [c for c in cells if c["slot"] == 1 and c["dp"] == 0]
    assert data, "DP0 should place data cells"
    # stereo: both channels present; data in columns 2-9 (CDS col 0, handover col 1).
    assert {c["channel"] for c in data} == {0, 1}
    assert min(c["col"] for c in data) >= 2 and max(c["col"] for c in data) <= 9
    assert writes, "config should yield register writes"


def test_wide_bit_held_columns_carry_identity():
    # Wide bits (BitWidth>0) replay the held bit across columns; every column must
    # carry the same channel/sample/bit (no channel = -1 holes) so the renderer can
    # merge + label them like the Visualizer.
    cfg = demo_config()
    dp = cfg.dataports[0]
    dp.enable_ch = 0b1; dp.sample_size = 3; dp.bit_width = 1
    dp.horizontal_start = 2; dp.horizontal_count = 15
    with tempfile.TemporaryDirectory() as d:
        path = cfg.to_csv_file(os.path.join(d, "c.csv"))
        cells = swi3score.grid_from_csv(path, 2)
    data = sorted((c for c in cells if c["slot"] == 1 and c["dp"] == 0),
                  key=lambda c: c["col"])
    assert data and all(c["channel"] == 0 for c in data), [c["channel"] for c in data]
    # the two columns of each wide bit share the same bit number
    assert data[0]["bit"] == data[1]["bit"] == 3


def test_csv_writes_all_dataport_names():
    # Every DP gets a name in the CSV (default "DP{i}"), like the Visualizer.
    cfg = demo_config()
    cfg.dataports[1].name = "Spk"
    name_row = next(l for l in cfg.to_csv().splitlines() if l.startswith("Name"))
    assert name_row == "Name,DP0,Spk,DP2,DP3,DP4,DP5,DP6,DP7,DP8,DP9,DP10,DP11"


def test_export_decoder_config_as_visualizer_csv():
    # The analyzer "export bus grid as visualizer CSV" path: the decoder's snooped
    # config (sparse, may exceed 12 slots) packs its ENABLED ports into the 12
    # visualizer columns, preserving device + DP number, and writes a CSV the core
    # can place. (Mirrors swi3score.config_dataports() output.)
    decoded = {
        "num_columns": 15, "skipping_denominator": 16, "phy3_enabled": False,
        "row_rate_khz": 781.25,
        "dataports": (
            [{"enabled": False} for _ in range(12)] +      # 12 reserved, disabled
            [{"enabled": True, "device_number": 4, "dp_number": 0, "enable_ch": 0x1,
              "sample_size": 0, "sample_grouping": 1, "horizontal_start": 1, "horizontal_count": 1},
             {"enabled": True, "device_number": 6, "dp_number": 0, "enable_ch": 0x1,
              "sample_size": 0, "sample_grouping": 3, "horizontal_start": 2, "horizontal_count": 3}]
        ),
    }
    cfg, n = BusConfig.from_decoder_config(decoded)
    assert n == 2
    assert cfg.row_rate_khz == 781.25
    # Enabled ports packed into the first columns, device/DP-number preserved.
    assert (cfg.dataports[0].device_number, cfg.dataports[0].number(0)) == (4, 0)
    assert (cfg.dataports[1].device_number, cfg.dataports[1].number(1)) == (6, 0)
    # Writes a visualizer CSV the C++ cascade can place.
    p = os.path.join(tempfile.mkdtemp(), "exported.csv")
    cfg.to_csv_file(p)
    assert "RowRate,781.25" in open(p).read()
    assert len(swi3score.grid_from_csv(p, 32)) > 0


def test_wide_bit_interval_overflow_detected():
    """A wide-bit DP (BitWidth>0) whose data can't fit its interval must raise an
    interval-overflow warning. Regression: expected_bits omitted the wide-bit span,
    so once wide-bit repeats inflated the placed-bit count past the un-widened
    expected, a real overflow was silently missed."""
    from swi3s_studio.model import viz_engine

    def _build(bit_width, interval):
        cfg = BusConfig(num_columns=7, row_rate_khz=3072.0, rows_to_draw=8)
        dp = cfg.dataports[0]
        dp.enabled = True
        dp.enable_ch = 0b1            # one channel
        dp.sample_size = 7            # 8-bit sample
        dp.sample_grouping = 0
        dp.bit_width = bit_width      # 1 => each bit held for 2 UIs (wide)
        dp.interval = interval        # interval rows = interval + 1
        dp.horizontal_start = 0
        dp.horizontal_count = 6
        with tempfile.TemporaryDirectory() as d:
            bm, _i, _v = viz_engine.build_bus_model(cfg.to_csv_file(os.path.join(d, "o.csv")))
        return bm.warnings.interval_overflow_warnings

    # bit_width=1 over a 2-row interval (14 UIs): an 8-bit sample needs 16 UIs wide.
    # 14 placed bits already meet the OLD un-widened expected (8), so the overflow
    # was hidden; now expected is the widened 16 and the warning fires.
    overflow = _build(bit_width=1, interval=1)
    assert overflow == [("DP0", 16, 14)], overflow
    # The same geometry with narrow bits (BitWidth=0) genuinely fits — no warning
    # (guards against the widening introducing a false positive).
    assert _build(bit_width=0, interval=1) == []


if __name__ == "__main__":
    test_csv_and_dict_roundtrip(); print("ok: BusConfig CSV + dict round-trip")
    test_authored_config_places_via_core(); print("ok: authored config placed by the C++ cascade")
    test_wide_bit_held_columns_carry_identity(); print("ok: wide-bit held columns carry the bit identity")
    test_csv_writes_all_dataport_names(); print("ok: CSV writes all data-port names (defaults included)")
    test_export_decoder_config_as_visualizer_csv(); print("ok: decoder config exports as visualizer CSV")
    test_wide_bit_interval_overflow_detected(); print("ok: wide-bit interval overflow detected")
    print("ALL AUTHORING TESTS PASSED")
