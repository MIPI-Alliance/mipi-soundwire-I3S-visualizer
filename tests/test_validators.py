"""DataPort validator regressions (swviz.utils.validators).

Targets two false-rejection fixes:
- the FCP guard/tail is only placed by a SINK DP's (source) FCP, so a source DP's
  FCP row-fit check must not reserve guard/tail columns;
- the C07 wide-bit straddle check must size the trailing channel group by its actual
  channel count, not the full grouping.

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
    # The engine intentionally ignores the validator ("real hardware has no
    # validators"), so a channel-group config that is valid-passing OR merely
    # plausible must not crash BusModelBuilder.build() with an IndexError from
    # walking channel_index past the enabled channels.
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
