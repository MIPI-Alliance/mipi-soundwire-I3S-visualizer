"""DataPort and Interface validator rules (swviz.utils.validators).

Pins:
- the FCP guard/tail is only placed by a SINK DP's (source) FCP, so a source DP's
  FCP row-fit check must not reserve guard/tail columns;
- the C07 wide-bit straddle check sizes the trailing channel group by its actual
  channel count, not the full grouping, and counts the TxPresent UI of a flow mode that
  sends one;
- the range checks on EnableCh_REG, BitWidth_REG, TailWidth_REG, FlowMode_REG and
  PortMode_REG;
- the TxPresent UI counts in the drive length that the tail must fit after;
- an odd column count is an Interface error unless PHY3 is enabled.

Run: PYTHONPATH=. python3 -m pytest tests/test_validators.py
"""
import os
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from swi3s_studio.swviz.utils.validators import DataPortValidator, ValidationResult


def test_fcp_fits_row_is_direction_aware():
    v = DataPortValidator(SimpleNamespace(num_columns=8))
    # DRQ at column 6 (BitWidth 0 -> 1 UI), guard on, 1 tail. The FCP runs opposite
    # its DP: a source DP has a SINK FCP that places ONLY the DRQ (1 col, fits); a
    # sink DP has a SOURCE FCP that also places guard+tail (6 + 1 + 1 + 1 = 9 > 8).
    fcp = SimpleNamespace(FCP_BitWidth_REG=0, FCP_GuardEnable_REG=True,
                          FCP_TailWidth_REG=1, FCP_HorizontalStart_REG=6)
    src = ValidationResult()
    v._check_fcp_fits_row(src, fcp, dp_is_sink=False)
    assert src.is_valid, src.get_summary()          # source DP: no overflow

    snk = ValidationResult()
    v._check_fcp_fits_row(snk, fcp, dp_is_sink=True)
    assert not snk.is_valid                          # sink DP: guard+tail overflow


def test_wide_bit_straddle_uses_partial_trailing_group():
    v = DataPortValidator(SimpleNamespace(num_columns=16))
    # 1 channel but grouping 4 -> a single PARTIAL group of 1 channel = one 2-UI wide
    # bit, which fits in a window ending at col 2. Sizing the group by the full
    # grouping (4) would simulate 4 bits and spuriously flag the 2nd as straddling.
    cfg = SimpleNamespace(SubRowInterval_REG=False, BitWidth_REG=1,
                          HorizontalStart_REG=0, HorizontalCount_REG=2,
                          Spacing_REG=0, SampleSize_REG=0, SampleGrouping_REG=0)
    r = ValidationResult()
    v._check_no_wide_bit_straddles_window(r, cfg, num_channels=1,
                                          effective_channel_grouping=4)
    assert r.is_valid, r.get_summary()              # no spurious straddle

    # Sanity: 2 real channels (2 bits) genuinely overflow the same window — bit 2
    # starts at col 2 and its 2-UI span crosses row_end=2.
    r2 = ValidationResult()
    v._check_no_wide_bit_straddles_window(r2, cfg, num_channels=2,
                                          effective_channel_grouping=4)
    assert not r2.is_valid


def test_engine_builds_partial_and_oversized_channel_groups():
    # The engine does not run the validator, so a channel-group config that is
    # valid-passing OR merely plausible must not crash BusModelBuilder.build() with
    # an IndexError from walking channel_index past the enabled channels.
    from swi3s_studio.swviz.core.engine import BusModelBuilder
    from swi3s_studio.swviz.models.interface import Interface
    from swi3s_studio.swviz.viz import VizConfig

    def _build(enable_mask, grouping):
        iface, viz = Interface(), VizConfig()
        iface.NumColumns_REG = 15
        dp = iface.data_ports[0].config
        dp.EnableCh_REG = enable_mask
        dp.ChannelGrouping_REG = grouping
        dp.SampleGrouping_REG = 2          # >0 and Spacing>0 are needed to surface it
        dp.Spacing_REG = 2
        dp.SampleSize_REG = 0
        dp.HorizontalStart_REG = 2
        dp.HorizontalCount_REG = 12
        viz.data_ports[0].enabled = True
        iface.set_dp_device(0, 0)
        return BusModelBuilder(iface, 64, viz).build()

    assert _build(0b111, 2).bits      # 3 channels, grouping 2 -> partial trailing group [2,1]
    assert _build(0b1, 4).bits        # grouping > channels (invalid but engine must not crash)
    assert _build(0b1111, 2).bits     # even grouping still works


def _port_validation(**regs):
    from swi3s_studio.swviz.models.interface import Interface
    iface = Interface()
    iface.NumColumns_REG = regs.pop("columns", 7)
    cfg = iface.data_ports[0].config
    cfg.EnableCh_REG = 1
    for k, v in regs.items():
        setattr(cfg, k, v)
    iface.set_dp_device(0, 0)
    return DataPortValidator(iface).validate(iface.data_ports[0], 0)


def test_range_checks_cover_enable_ch_widths_and_modes():
    for field, value in (("EnableCh_REG", 0x10000), ("BitWidth_REG", 3),
                         ("TailWidth_REG", 3), ("FlowMode_REG", 4), ("PortMode_REG", 4)):
        r = _port_validation(**{field: value})
        assert any(e.field == field for e in r.errors), (field, r.get_summary())
    for field, value in (("EnableCh_REG", 0xFFFF), ("EnableCh_REG", 0), ("BitWidth_REG", 0),
                         ("BitWidth_REG", 2), ("TailWidth_REG", 0), ("TailWidth_REG", 2),
                         ("FlowMode_REG", 0), ("FlowMode_REG", 3), ("PortMode_REG", 0),
                         ("PortMode_REG", 3)):
        r = _port_validation(**{field: value})
        assert not any(e.field == field for e in r.errors), (field, value, r.get_summary())


def test_txp_ui_counts_in_the_drive_length():
    from swi3s_studio.swviz.models import FlowMode
    # 8 columns (NumColumns_REG 7), one channel of 4 data bits from column 2: data alone ends at column 5
    # and leaves room for two tail columns; the TxPresent UI pushes the data to column 6,
    # leaving one.
    regs = dict(columns=7, SampleSize_REG=3, HorizontalStart_REG=2, HorizontalCount_REG=4,
                TailWidth_REG=2)
    plain = _port_validation(FlowMode_REG=FlowMode.NORMAL, **regs)
    assert not any(e.field == "TailWidth" or "tail" in e.message.lower()
                   for e in plain.errors), plain.get_summary()
    flow = _port_validation(FlowMode_REG=FlowMode.TX_CONTROLLED, **regs)
    assert any("tail" in e.message.lower() for e in flow.errors), flow.get_summary()


def test_a_txp_ui_counts_in_the_wide_bit_straddle_check():
    v = DataPortValidator(SimpleNamespace(num_columns=16))
    # One channel of one 2-UI wide bit in a 3-column window fits; with a TxPresent UI
    # ahead of the data, the second bit starts at column 2 and crosses the window end.
    cfg = SimpleNamespace(SubRowInterval_REG=False, BitWidth_REG=1,
                          HorizontalStart_REG=0, HorizontalCount_REG=2,
                          Spacing_REG=0, SampleSize_REG=0, SampleGrouping_REG=0,
                          _txp_enabled=False)
    r = ValidationResult()
    v._check_no_wide_bit_straddles_window(r, cfg, num_channels=1, effective_channel_grouping=1)
    assert r.is_valid, r.get_summary()
    cfg._txp_enabled = True
    r2 = ValidationResult()
    v._check_no_wide_bit_straddles_window(r2, cfg, num_channels=1, effective_channel_grouping=1)
    assert not r2.is_valid


def _interface_errors(columns_reg, phy3):
    from swi3s_studio.swviz.models.interface import Interface
    from swi3s_studio.swviz.utils.validators import InterfaceValidator
    iface = Interface()
    iface.phy3_enabled = phy3
    iface.NumColumns_REG = columns_reg
    return [e for e in InterfaceValidator(iface).validate().errors if e.field == "NumColumns"]


def test_an_odd_column_count_is_an_interface_error_unless_phy3_is_enabled():
    errors = _interface_errors(columns_reg=14, phy3=False)       # 15 columns
    assert len(errors) == 1 and "must be even" in errors[0].message
    assert _interface_errors(columns_reg=14, phy3=True) == []
    assert _interface_errors(columns_reg=15, phy3=False) == []     # 16 columns


def _build_on_device(device, rows=64, **regs):
    from swi3s_studio.swviz.core.engine import BusModelBuilder
    from swi3s_studio.swviz.models.interface import Interface
    from swi3s_studio.swviz.viz import VizConfig
    iface, viz = Interface(), VizConfig()
    iface.NumColumns_REG = 15
    cfg = iface.data_ports[0].config
    cfg.HorizontalStart_REG, cfg.HorizontalCount_REG = 2, 12
    for name, value in regs.items():
        setattr(cfg, name, value)
    viz.data_ports[0].enabled = True
    iface.set_dp_device(0, device)
    return BusModelBuilder(iface, rows, viz).build()


def test_a_port_on_a_device_number_that_names_no_device_gets_only_the_device_error():
    """The port is never placed, so the placement-derived detectors (interval overflow,
    sink handover, enabled with no channels) report nothing for it; its validation result
    carries the DeviceNumber range error. The same port on device 11 reports none of them."""
    for regs in ({"EnableCh_REG": 0x1FFFF}, {"EnableCh_REG": 1},
                 {"EnableCh_REG": 0}, {"EnableCh_REG": 1, "PortDirection_REG": True}):
        w = _build_on_device(12, **regs).warnings
        assert [where for where, _ in w.validation_issues] == ["DP0"], regs
        assert any("DeviceNumber" in e.message for e in w.validation_issues[0][1].errors), regs
        assert w.interval_overflow_warnings == [], regs
        assert w.display_truncation_warnings == [], regs
        assert w.enabled_no_channels_warnings == [], regs
        assert w.sink_handover_warnings == [], regs
    assert _build_on_device(11, EnableCh_REG=0).warnings.enabled_no_channels_warnings == [
        ("DP0", 0)]
