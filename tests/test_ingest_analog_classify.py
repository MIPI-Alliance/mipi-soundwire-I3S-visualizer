"""Telling an analog CSV from a digital one reads a bounded number of lines.

Past the first `max_scan_rows` data rows, `is_analog_csv` samples lines at byte offsets
spread through the rest of the file instead of parsing every row, so classifying a
multi-GB digital export costs about as much as a small one.
"""
import csv

from swi3s_studio.ingest import analog_csv


class _CountingCsv:
    """The csv module, counting the rows its readers hand out."""

    def __init__(self):
        self.rows = 0

    def reader(self, source, *a, **k):
        for row in csv.reader(source, *a, **k):
            self.rows += 1
            yield row


def _digital(path, rows, analog_tail=0):
    with open(path, "w") as f:
        f.write("Time [s],Channel 0,Channel 1\n")
        for i in range(rows):
            f.write(f"{i * 2e-9:.9f},{i & 1},{(i >> 3) & 1}\n")
        for i in range(analog_tail):
            f.write(f"{(rows + i) * 2e-9:.9f},0.4{i % 10},0.9\n")


def test_a_long_digital_csv_is_classified_from_a_bounded_number_of_lines(tmp_path,
                                                                        monkeypatch):
    path = str(tmp_path / "d.csv")
    _digital(path, 50_000)
    shim = _CountingCsv()
    monkeypatch.setattr(analog_csv, "csv", shim)
    assert analog_csv.is_analog_csv(path, max_scan_rows=200) is False
    assert shim.rows <= 2 * 200 + 2, f"parsed {shim.rows} rows of 50,000"


def test_analog_rows_past_the_prefix_are_still_found(tmp_path):
    path = str(tmp_path / "late.csv")
    _digital(path, 20_000, analog_tail=2_000)
    assert analog_csv.is_analog_csv(path, max_scan_rows=100) is True


def test_a_short_file_is_read_to_its_end(tmp_path):
    path = str(tmp_path / "short.csv")
    _digital(path, 50, analog_tail=1)
    assert analog_csv.is_analog_csv(path, max_scan_rows=100) is True
    _digital(path, 50)
    assert analog_csv.is_analog_csv(path, max_scan_rows=100) is False
