"""Analog CSV rows: a line ending in a comma, row numbers in messages, the inferred rate, and
the preamble's Sample Interval (reported when unusable, silent at the limits of its range,
a subnormal one refused by file name)."""
import numpy as np
import pytest

from swi3s_studio.ingest import analog_csv, probe

PRE = "Model,SCOPE1\nSample Interval,2e-9\n\n"


def _write(tmp_path, text):
    p = tmp_path / "scope.csv"
    p.write_text(text)
    return str(p)


def test_lines_ending_in_a_comma_read_as_analog(tmp_path):
    rows = "".join(f"{i * 2e-9:.7e},{0.1 * i:.4e},{0.2 * i:.4e},\n" for i in range(-3, 40))
    path = _write(tmp_path, PRE + "TIME,CH1,CH2,\n" + rows)
    assert analog_csv.is_analog_csv(path)
    assert analog_csv.channel_names(path) == ["CH1", "CH2"]
    out = analog_csv.read_analog_csv(path)
    assert list(out["channels"]) == ["CH1", "CH2"]
    assert out["time"].size == 43
    assert probe.probe([path]).kind == "analog_csv"


def test_a_bad_row_is_numbered_among_all_rows_after_the_header(tmp_path):
    body = "0,1.5,2.5\n1e-9,1.5\n2e-9,1.5\n3e-9,abc,0.3\n4e-9,1.5,2.5\n"
    path = _write(tmp_path, PRE + "TIME,CH1,CH2\n" + body)
    with pytest.raises(ValueError, match="data row 4 after the TIME header"):
        analog_csv.read_analog_csv(path)


def test_the_rate_is_rounded_to_a_whole_hertz():
    time = np.arange(100) * 2.0000000000001503e-09
    assert analog_csv._infer_rate(time, {})[0] == 500_000_000.0


def _scope_file(tmp_path, interval, time_cells, name="scope.csv"):
    rows = ["Model,SCOPE1", f"Sample Interval,{interval}", "", "TIME,CH1,CH2"]
    rows += [f"{t},{0.1 * k},{0.2 * k}" for k, t in enumerate(time_cells)]
    p = tmp_path / name
    p.write_text("\n".join(rows) + "\n")
    return str(p)


@pytest.mark.parametrize("interval", ["nan", "inf", "0", "-1", "fast", "1e-320", "1e-17", "5"])
def test_a_preamble_interval_that_is_no_usable_time_is_reported_and_not_used(tmp_path, interval):
    from swi3s_studio import import_notes
    path = _scope_file(tmp_path, interval, ["0", "0", "0"])        # no TIME step to fall back on
    with import_notes.collect() as notes:
        out = analog_csv.read_analog_csv(path)
    assert out["sample_rate_hz"] == 0.0
    assert [n.message for n in notes if "Sample Interval" in n.message] == [
        f"the preamble's Sample Interval {interval!r} is not a positive time that gives a "
        f"rate from 1 Hz to 1,000,000,000,000,000 Hz; it is not used"]


@pytest.mark.parametrize("interval, rate", [("2e-9", 500_000_000.0), ("1", 1.0),
                                            ("1e-15", 1e15)])
def test_a_usable_preamble_interval_is_not_reported(tmp_path, interval, rate):
    """The limits of the usable range, 1 Hz and 1e15 Hz, are inside it."""
    from swi3s_studio import import_notes
    path = _scope_file(tmp_path, interval, ["0", "0", "0"])
    with import_notes.collect() as notes:
        out = analog_csv.read_analog_csv(path)
    assert out["sample_rate_hz"] == rate
    assert not [n for n in notes if "Sample Interval" in n.message]


def test_a_subnormal_preamble_interval_or_time_step_is_not_a_bare_overflow(tmp_path):
    from swi3s_studio import import_notes
    from swi3s_studio.session import Session
    path = _scope_file(tmp_path, "1e-320", [f"{k * 1e-9:.3e}" for k in range(5)])
    with import_notes.collect():
        assert analog_csv.read_analog_csv(path)["sample_rate_hz"] == 1_000_000_000.0
    path = _scope_file(tmp_path, "1e-9", ["0", "5e-324", "1e-323", "1.5e-323"], "tiny.csv")
    with import_notes.collect():
        with pytest.raises(ValueError, match=r"tiny\.csv"):
            Session.from_analog_csv(path)


def test_a_file_of_non_numeric_rows_is_scanned_within_a_bound(tmp_path, monkeypatch):
    path = _write(tmp_path, "".join(f"t{i},hello world,INFO\n" for i in range(5000)))
    parsed = []
    real = analog_csv._row_of_line
    monkeypatch.setattr(analog_csv, "_row_of_line",
                        lambda line: parsed.append(line) or real(line))
    assert analog_csv.is_analog_csv(path, max_scan_rows=50) is False
    assert len(parsed) <= 2 * 50 + analog_csv._PREAMBLE_MAX_ROWS


def test_a_row_with_no_time_cell_is_reported_as_such_not_as_short(tmp_path):
    from swi3s_studio import import_notes
    rows = "".join(f"{i * 2e-9:.7e},{0.1 * i:.4e},{0.2 * i:.4e}\n" for i in range(20))
    path = _write(tmp_path, PRE + "TIME,CH1,CH2\n" + rows + ",0.3,0.4\n")
    with import_notes.collect() as notes:
        out = analog_csv.read_analog_csv(path)
    assert out["time"].size == 20
    assert [n.message for n in notes] == [
        "1 row with no TIME value was skipped (first: row 21 after the TIME header)"]
