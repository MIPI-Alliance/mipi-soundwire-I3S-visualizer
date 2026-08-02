"""Visualization-mode authoring tests (pure model, no Qt):

- BusConfig serialises to the v2.0 CSV the engine/core read, and CSV / dict
  round-trip.
- An authored config is placed by the verified C++ cascade (`grid_from_csv`), with
  wide-bit held columns carrying the bit identity.
- Every data port gets a name in the CSV (defaults like the Visualizer).

Clash detection / validation parity is covered end-to-end by
`test_visualizer_engine.py` (the Visualizer's own 89-config JSON testsuite), since
Bus-Visualizer mode is now driven by the vendored Visualizer engine.

Run: PYTHONPATH=. python3 -m pytest tests/test_authoring.py
"""
import os
import tempfile

import swi3score

from swi3s_studio.model import viz_engine
from swi3s_studio.model.bus_config import BusConfig, demo_config


def test_notifications_use_renamed_data_port():
    """A per-DP notification must identify the port by device + slot + user-assigned name
    (e.g. 'Dev0 DP3 (LeftMic)'), not the bare default 'DP{index}'. And the no-channel case
    must read 'drawn but no channel enabled' — the port is DRAWN, not enabled (enabled
    means >=1 channel-enable bit set). Regression for both."""
    cfg = demo_config()
    dp = cfg.dataports[3]
    dp.enabled = True                     # drawn on the grid...
    dp.enable_ch = 0                      # ...but no channel enabled -> the notification
    dp.device_number = 0
    dp.name = "LeftMic"
    with tempfile.TemporaryDirectory() as d:
        path = cfg.to_csv_file(os.path.join(d, "renamed.csv"))
        _cells, _clashes, issues, _ncols, _nrows = viz_engine.render_payload(path)
    hits = [i for i in issues if "no channel enabled" in i.message]
    assert hits, [(i.source, i.message) for i in issues]
    src, msg = hits[0].source, hits[0].message
    assert "DP3" in src and "(LeftMic)" in src and "Dev0" in src, src
    assert msg == "drawn but no channel enabled", msg


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


def test_cds_guard_polarity_roundtrip():
    """Regression: to_csv() used to hardcode CDS_GuardPolarity_REG=False on export
    (Studio didn't model polarity), silently dropping Guard 1 on every save. Both
    cds_guard_enabled and cds_guard_polarity must now survive CSV and dict
    round-trips, with the polarity row written exactly once. cds_guard_enabled/polarity
    are now derived (read-only) from the per-source cds_guard list — drive them via the
    Manager's slot (index 0), which is what the legacy singular properties reflect."""
    from swi3s_studio.model.bus_config import CDS_GUARD_G1

    cfg = BusConfig()
    cfg.cds_guard[0] = CDS_GUARD_G1
    with tempfile.TemporaryDirectory() as d:
        path = cfg.to_csv_file(os.path.join(d, "c.csv"))
        text = open(path).read()
        back = BusConfig.from_csv(path)
    assert sum(1 for l in text.splitlines()
               if l.startswith("CDS_GuardPolarityPerSource")) == 1
    assert back.cds_guard_enabled is True
    assert back.cds_guard_polarity is True
    # dict round-trip (workspace persistence)
    back2 = BusConfig.from_dict(cfg.to_dict())
    assert back2.cds_guard_enabled is True
    assert back2.cds_guard_polarity is True


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


def test_cds_guard_tail_dialogs_update_panel_checkboxes():
    """AuthoringPanel's "CDS Guard Enabled" / "CDS Tail Enabled" rows are checkboxes
    whose click opens a per-source (Manager + Device 0-11) dialog; the checkbox
    itself just mirrors the derived cds_guard_enabled/cds_tail_enabled summary and
    must never be setattr'd directly (both are read-only properties on BusConfig).
    Simulates an accepted dialog result — exec() blocks headless — by driving the
    dialog's own toggle handler, then feeding the result through the panel's
    post-dialog path."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication, QCheckBox

    from swi3s_studio.model.bus_config import CDS_GUARD_G0, CDS_GUARD_G1
    from swi3s_studio.ui.authoring.authoring_panel import AuthoringPanel
    from swi3s_studio.ui.authoring.dialogs import CdsGuardDialog, CdsTailDialog

    QApplication.instance() or QApplication([])
    panel = AuthoringPanel()
    guard_cb = panel._iface["cds_guard_enabled"][1]
    tail_cb = panel._iface["cds_tail_enabled"][1]
    assert isinstance(guard_cb, QCheckBox) and isinstance(tail_cb, QCheckBox)
    assert guard_cb.isChecked() is False and tail_cb.isChecked() is False

    # Simulate accepting a CdsGuardDialog: Device 0 (index 1) -> G1.
    dlg = CdsGuardDialog(panel, list(panel._cfg.cds_guard))
    dlg._pick(1, CDS_GUARD_G1)
    panel._cfg.cds_guard = list(dlg.guard)
    panel._update_cds_guard_checkbox()
    assert guard_cb.isChecked() is True
    assert panel._cfg.cds_guard[1] == CDS_GUARD_G1
    assert "device" in guard_cb.toolTip()

    # Simulate accepting a CdsTailDialog: Manager (index 0) -> width 2.
    dlg2 = CdsTailDialog(panel, list(panel._cfg.cds_tail))
    dlg2._pick(0, 2)
    panel._cfg.cds_tail = list(dlg2.tail)
    panel._update_cds_tail_checkbox()
    assert tail_cb.isChecked() is True
    assert panel._cfg.cds_tail[0] == 2
    assert "Manager" in tail_cb.toolTip()

    # _on_iface_edit must never touch the derived attrs (both raise on setattr).
    panel._on_iface_edit()
    assert panel._cfg.cds_guard[1] == CDS_GUARD_G1
    assert panel._cfg.cds_tail[0] == 2

    # _populate() re-derives both checkboxes from the model without reopening a
    # dialog (setChecked doesn't emit clicked).
    panel._cfg.cds_guard = [CDS_GUARD_G0] + [0] * 12
    panel._cfg.cds_tail = [0] * 13
    panel._populate()
    assert guard_cb.isChecked() is True   # Manager now G0
    assert tail_cb.isChecked() is False   # all tails cleared


def test_per_source_cds_handover_placement_and_clash():
    """Each CDS source hands the bus over right AFTER its own last-driven CDS column,
    not uniformly at the region end. A guard-only source (no tail) therefore hands
    over immediately after the guard, where a *different* source may still be driving
    its (longer) tail — that coexists (time-multiplex, no clash), while the source
    that drove the tail hands over at the region end, reconciling with a same-device
    data port there.

    Regression: a Device-0 guard's handover was dumped at the region-final column,
    spuriously clashing with a Manager data port that legitimately owns that column
    (its own tail + handover reconcile). See the four cases below."""
    import tempfile as _tf

    from swi3s_studio.model import viz_engine

    def _clash_cols(guard, tail, dp_dev, dp_col):
        cfg = BusConfig(num_columns=15, row_rate_khz=3072.0)
        cfg.cds_bit_width = 0                       # CDS col0, guard col1, tails col2..
        for i, g in guard.items():
            cfg.cds_guard[i] = g
        for i, t in tail.items():
            cfg.cds_tail[i] = t
        dp = cfg.dataports[0]
        dp.enabled = True
        dp.device_number = dp_dev
        dp.enable_ch = 0b1
        dp.sample_size = 0
        dp.horizontal_start = dp_col
        dp.horizontal_count = 0
        with _tf.TemporaryDirectory() as d:
            path = cfg.to_csv_file(os.path.join(d, "o.csv"))
            _cells, clashes, _i, _c, _r = viz_engine.render_payload(path)
        return sorted({col for (_row, col) in (clashes or {})})

    # THE reported case: Manager G1 + Device-0 G0 guards, Manager tail width 3, and a
    # MANAGER data port at the region-end column (col5). Same device as the Manager's
    # own tail+handover there => no clash. Device-0 (guard only) hands over at col2.
    assert _clash_cols({0: 2, 1: 1}, {0: 3}, dp_dev=-1, dp_col=5) == []
    # Manager guard-only (no tail) => Manager handover at col2; a DEVICE-0 data port
    # there is a different device => clash.
    assert _clash_cols({0: 2}, {}, dp_dev=0, dp_col=2) == [2]
    # Device-0 guard-only => Device-0 handover at col2; a DEVICE-0 data port there is
    # the same device => reconciles, no clash.
    assert _clash_cols({1: 1}, {}, dp_dev=0, dp_col=2) == []
    # A DEVICE-0 data port landing on the Manager's tail columns is a different-device
    # write => still a real clash (the CDS exemption is for turnarounds, not writes).
    assert _clash_cols({0: 2}, {0: 3}, dp_dev=0, dp_col=3) == [3, 4]
