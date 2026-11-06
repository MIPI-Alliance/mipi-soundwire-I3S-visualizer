"""The authoring panel's derived readouts agree with the core and the reference model.

The panel shows a System SSP interval and each port's sample rate, computed from the config
it holds. The SSP interval is the LCM of the enabled ports' pattern periods — a skipping
port's is Interval x SkippingDenominator (Section 4.2.3.1) — as the visualizer's
Interface.interval_lcm computes it. A port's rate is the one the core reports for the same
config (Decoder.audio_sample_rates), TX_PRESENT counted in every flow mode that has one.

Every input is synthetic: the demo config with cells edited.
"""
import csv
import io

import pytest
import swi3score

from swi3s_studio.model.bus_config import BusConfig, demo_config
from swi3s_studio.model.viz_engine import build_bus_model

pytestmark = pytest.mark.gui


def _csv(tmp_path, cells: dict[str, str]) -> str:
    rows = list(csv.reader(io.StringIO(demo_config().to_csv())))
    for key, value in cells.items():
        next(r for r in rows if r and r[0] == key)[1] = value     # the scalar / DP0 column
    path = tmp_path / "cfg.csv"
    path.write_text("\n".join(",".join(r) for r in rows) + "\n", encoding="utf-8")
    return str(path)


def _core_rate_khz(path: str) -> float:
    settings = swi3score.DecoderSettings()
    settings.config_csv_path = path
    d = swi3score.Decoder(swi3score.MemorySampleSource([False] * 20_000, 98_304_000, 4),
                          settings)
    d.run()
    return d.audio_sample_rates()[(0, 0)] / 1000


@pytest.fixture
def panel():
    from PySide6.QtWidgets import QApplication

    from swi3s_studio.ui.authoring.authoring_panel import AuthoringPanel
    QApplication.instance() or QApplication([])
    p = AuthoringPanel()
    yield p
    p.deleteLater()


_SRI = {"SubRowInterval_REG": "True", "Interval_REG": "0", "HorizontalStart_REG": "1"}


@pytest.mark.parametrize("cells", [
    {},                                                         # the demo as written
    {"SkippingDenominator_REG": "160", "SkippingNumerator_REG": "13", "Interval_REG": "31"},
    {"SkippingNumerator_REG": "16"},                            # skips every interval
    {**_SRI, "EnableCh_REG": "1", "SampleSize_REG": "1", "Spacing_REG": "2",
     "HorizontalCount_REG": "14", "FlowMode_REG": "2"},         # RX controlled: TX_PRESENT
    {**_SRI, "EnableCh_REG": "1", "SampleSize_REG": "1", "Spacing_REG": "2",
     "HorizontalCount_REG": "14", "FlowMode_REG": "0"},
], ids=["demo", "skipping", "all-skipped", "sri-rx-controlled", "sri-normal"])
def test_the_panel_shows_the_core_rate_and_the_model_ssp_interval(panel, tmp_path, cells):
    path = _csv(tmp_path, cells)
    panel.set_config(BusConfig.from_csv(path))
    _bus, iface, _viz = build_bus_model(path)
    assert panel._ssp_value.text() == str(iface.interval_lcm)
    assert float(panel._dp_rate[0].text()) == pytest.approx(_core_rate_khz(path), abs=0.05)


def test_a_skipping_port_lengthens_the_ssp_interval(panel, tmp_path):
    path = _csv(tmp_path, {"SkippingDenominator_REG": "160", "SkippingNumerator_REG": "13",
                           "Interval_REG": "31"})
    panel.set_config(BusConfig.from_csv(path))
    assert panel._ssp_value.text() == str(32 * 160)
