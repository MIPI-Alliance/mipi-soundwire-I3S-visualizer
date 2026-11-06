"""The audio rate the core reports for a port (Decoder.audio_sample_rates, from
SwI3sConfig::SampleRateHz) is the rate its transport placement carries.

A sub-row-interval (SRI) port repeats its transport within the horizontal window of every
row, so its rate is the samples it completes per row times the row rate. Each case here is
a config the reference validator accepts in SRI mode (Interval 0, Spacing >= 2, the
transport cycle dividing the row, the gap outside the window equal to the gap inside it),
and the reported rate is checked against the samples the grid layout completes per row.

Every input is synthetic: the demo config with cells edited, decoded over a silent capture.
"""
import csv
import io
from types import SimpleNamespace

import pytest
import swi3score

from swi3s_studio.model.bus_config import demo_config
from swi3s_studio.swviz.utils.validators import DataPortValidator, ValidationResult

_ROW_RATE_HZ = 3_072_000          # the demo config's RowRate (kHz) x 1000
_COLUMNS = 16                     # the demo config's NumColumns_REG + 1
_ROWS = 8


def _csv(tmp_path, cells: dict[str, str]) -> str:
    rows = list(csv.reader(io.StringIO(demo_config().to_csv())))
    for key, value in cells.items():
        next(r for r in rows if r and r[0] == key)[1] = value     # DP0's column
    path = tmp_path / "sri.csv"
    path.write_text("\n".join(",".join(r) for r in rows) + "\n", encoding="utf-8")
    return str(path)


def _reported_rate(path: str) -> float:
    settings = swi3score.DecoderSettings()
    settings.config_csv_path = path
    d = swi3score.Decoder(swi3score.MemorySampleSource([False] * 20_000, 98_304_000, 4),
                          settings)
    d.run()
    rates = d.audio_sample_rates()
    assert list(rates) == [(0, 0)], rates
    return rates[(0, 0)]


def _completed_per_row(path: str) -> list[int]:
    """Samples of DP0's first channel whose LSB the grid places, per row."""
    per_row = [0] * _ROWS
    for c in swi3score.grid_from_csv(path, _ROWS):
        if (c["dp"] == 0 and c["channel"] == 0 and c["slot"] == 1 and c["sample_here"]
                and c["bit"] == 0):
            per_row[c["row"]] += 1
    return per_row


def _sri_valid(cells: dict[str, str]) -> ValidationResult:
    cfg = SimpleNamespace(SubRowInterval_REG=True, Interval_REG=0, SkippingNumerator_REG=0,
                          Spacing_REG=int(cells["Spacing_REG"]),
                          HorizontalCount_REG=int(cells["HorizontalCount_REG"]))
    channels = bin(int(cells["EnableCh_REG"])).count("1")
    drive = ((int(cells["SampleSize_REG"]) + 1) * (int(cells.get("SampleGrouping_REG", "0")) + 1)
             * channels)
    v = DataPortValidator(SimpleNamespace(num_columns=_COLUMNS))
    result = ValidationResult()
    v._check_sri_interval_zero(result, cfg)
    v._check_sri_skipping_disabled(result, cfg)
    v._check_sri_equal_spacing(result, cfg, drive)
    v._check_sri_pattern_fits(result, cfg, drive)
    return result


_SRI = {"SubRowInterval_REG": "True", "Interval_REG": "0", "HorizontalStart_REG": "2"}


@pytest.mark.parametrize("cells,per_row", [
    # 4-bit samples, gap 4: two transports in a 12-column window
    ({"EnableCh_REG": "1", "SampleSize_REG": "3", "Spacing_REG": "5",
      "HorizontalCount_REG": "11"}, 2),
    # two 2-bit channels in each transport: two samples per channel per row
    ({"EnableCh_REG": "3", "SampleSize_REG": "1", "Spacing_REG": "5",
      "HorizontalCount_REG": "11"}, 2),
    # a group of two 4-bit samples, gap 8: one transport, two samples per row
    ({"EnableCh_REG": "1", "SampleSize_REG": "3", "SampleGrouping_REG": "1",
      "Spacing_REG": "9", "HorizontalCount_REG": "7"}, 2),
    # 2-bit samples, gap 2: four transports in a 14-column window
    ({"EnableCh_REG": "1", "SampleSize_REG": "1", "Spacing_REG": "3",
      "HorizontalCount_REG": "13"}, 4),
])
def test_a_sub_row_interval_port_reports_the_samples_it_places_per_row(tmp_path, cells, per_row):
    result = _sri_valid(cells)
    assert result.is_valid, result.get_summary()
    path = _csv(tmp_path, {**_SRI, **cells})
    assert _completed_per_row(path) == [per_row] * _ROWS
    assert _reported_rate(path) == per_row * _ROW_RATE_HZ


def test_a_sub_row_interval_transport_wider_than_its_window_reports_no_audio(tmp_path):
    cells = {"EnableCh_REG": "1", "SampleSize_REG": "15", "Spacing_REG": "5",
             "HorizontalCount_REG": "11"}
    assert not _sri_valid(cells).is_valid
    path = _csv(tmp_path, {**_SRI, **cells})
    assert _completed_per_row(path) == [0] * _ROWS
    assert _reported_rate(path) == 0.0


def test_a_skipping_numerator_at_or_above_the_denominator_reports_no_audio(tmp_path):
    # The demo's SkippingDenominator_REG is 16: a numerator of 16 skips every interval.
    assert _reported_rate(_csv(tmp_path, {"SkippingNumerator_REG": "16"})) == 0.0
    assert _reported_rate(_csv(tmp_path, {"SkippingNumerator_REG": "4"})) == pytest.approx(
        _ROW_RATE_HZ / 8 * 12 / 16)


@pytest.mark.parametrize("cells,ratio", [
    ({"SampleGrouping_REG": "2147483647"}, 2**31),
    ({"Interval_REG": "2147483647"}, 8 / 2**31),
    ({"SampleGrouping_REG": "2147483647", "Interval_REG": "2147483647"}, 8),
])
def test_a_field_near_the_int_limit_gives_the_rate_it_writes(tmp_path, cells, ratio):
    """The rate is (SampleGrouping + 1) x RowRate / (Interval + 1) for the values as the
    file writes them, however large: the demo port's rate (SampleGrouping 0, Interval 7)
    times the change those values make, never a wrapped or negative one."""
    base = _reported_rate(_csv(tmp_path, {}))
    assert base == _ROW_RATE_HZ / 8
    assert _reported_rate(_csv(tmp_path, cells)) == pytest.approx(base * ratio)


@pytest.mark.parametrize("grouping", ["-2", "-2147483648"])
def test_a_rate_that_is_not_positive_reads_as_no_audio(tmp_path, grouping):
    assert _reported_rate(_csv(tmp_path, {"SampleGrouping_REG": grouping})) == 0.0


@pytest.mark.parametrize("field", ["SampleSize_REG", "HorizontalCount_REG", "Spacing_REG"])
def test_a_sub_row_interval_field_near_the_int_limit_gives_no_wrapped_rate(tmp_path, field):
    """An SRI transport whose width or window is near the int limit gives the rate its
    values carry: a transport wider than its window reports no audio, and a window that
    holds it reports a positive rate."""
    cells = {**_SRI, "EnableCh_REG": "1", "SampleSize_REG": "3", "Spacing_REG": "5",
             "HorizontalCount_REG": "11", field: "2147483647"}
    rate = _reported_rate(_csv(tmp_path, cells))
    if field == "SampleSize_REG":
        assert rate == 0.0
    elif field == "HorizontalCount_REG":
        # A 4-bit transport plus its 4-column gap repeats every 8 columns of the window.
        assert rate == (2**31 + 4) // 8 * _ROW_RATE_HZ
    else:
        assert rate == _ROW_RATE_HZ
