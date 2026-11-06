"""Digital-CSV ingest is BOUNDED and BATCH-INVARIANT.

1. **Bounded.** The loader reads in batches and refuses a file predicted over the memory
   budget before reading it, fail-closed like the `.sal` path's guard.

2. **Batch-invariant.** A batch size that forces many boundaries produces byte-identical
   output to one that forces none, so a batch edge landing mid-capture changes nothing.

3. **Limits at their boundary.** A first row of exactly `MAX_LINE_CHARS` characters and
   one of exactly `_MAX_HEADER_CELLS` cells are read, and one more of either is refused; a
   sample rate of 1 Hz and of `MAX_RATE_HZ` is accepted, and one just past either is
   refused; timestamps that give a rate beyond the maximum are refused naming the file; a
   clock column that is also the data column is refused naming the file.

4. **Same-sample warning.** The count includes a data line whose transitions alone merge,
   and one merged transition is reported in the singular.

Run: PYTHONPATH=. python3 -m pytest tests/test_digital_csv_bounded.py
"""
import csv
import os
import tempfile

import numpy as np
import pytest

from swi3s_studio import import_notes
from swi3s_studio.ingest import digital_csv, timebase


def _write_csv(path: str, rows: int, *, bad_time_at: int = -1) -> str:
    """A Logic-style digital export: Time, clock (toggles every row), data (every 2)."""
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["Time [s]", "Channel 0", "Channel 1"])
        t = 0.0
        for i in range(rows):
            stamp = "not-a-time" if i == bad_time_at else f"{t:.9f}"
            w.writerow([stamp, i & 1, (i >> 1) & 1])
            t += 2e-8
    return path


def _load(path, **kw):
    return digital_csv.load_capture(path, 1, 2, **kw)


def test_batching_does_not_change_the_capture(monkeypatch):
    """The property that makes the batched read safe: identical output whatever the batch
    size, including sizes that put a boundary between a level change and its neighbour.

    Chunk sizes chosen to be coprime-ish with the 1-row clock period and the 2-row data
    period, so boundaries land at every phase of both.
    """
    with tempfile.TemporaryDirectory() as d:
        path = _write_csv(os.path.join(d, "c.csv"), 5_000)
        monkeypatch.setattr(digital_csv, "_ROW_CHUNK", 10_000)   # one batch, no boundary
        whole = _load(path)
        for chunk in (1, 2, 3, 7, 999, 4_999, 5_000, 5_001):
            monkeypatch.setattr(digital_csv, "_ROW_CHUNK", chunk)
            got = _load(path)
            assert np.array_equal(got.clock_edges, whole.clock_edges), f"chunk={chunk}"
            assert np.array_equal(got.data_edges, whole.data_edges), f"chunk={chunk}"
            assert got.initial_clock == whole.initial_clock, f"chunk={chunk}"
            assert got.initial_data == whole.initial_data, f"chunk={chunk}"
            assert got.sample_rate_hz == whole.sample_rate_hz, f"chunk={chunk}"


def test_a_bad_time_cell_is_dropped_whichever_batch_it_lands_in(monkeypatch):
    """The non-numeric-time fallback runs inside the per-batch conversion. A stray cell is
    dropped, and dropping it does not depend on where the batch boundaries fall."""
    with tempfile.TemporaryDirectory() as d:
        path = _write_csv(os.path.join(d, "bad.csv"), 400, bad_time_at=201)
        # The same file with the bad row removed: dropping the row is reading it so.
        with open(path, encoding="utf-8") as f:
            lines = f.read().splitlines()
        del lines[202]                                    # line 1 is the header
        assert "not-a-time" not in "\n".join(lines)
        without = os.path.join(d, "without.csv")
        with open(without, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        monkeypatch.setattr(digital_csv, "_ROW_CHUNK", 10_000)
        want = _load(without)
        bad_note = ["bad.csv: 1 row with a time that is not a finite number was skipped "
                    "(first at line 203: 'not-a-time')"]
        with import_notes.collect() as notes:
            whole = _load(path)
        assert [str(n) for n in notes] == bad_note
        assert np.array_equal(whole.clock_edges, want.clock_edges)
        assert np.array_equal(whole.data_edges, want.data_edges)
        assert whole.initial_clock == want.initial_clock
        for chunk in (50, 200, 201, 202):        # boundary before/at/after the bad row
            monkeypatch.setattr(digital_csv, "_ROW_CHUNK", chunk)
            with import_notes.collect() as notes:
                got = _load(path)
            assert [str(n) for n in notes] == bad_note, f"chunk={chunk}"
            assert np.array_equal(got.clock_edges, want.clock_edges), f"chunk={chunk}"
            assert np.array_equal(got.data_edges, want.data_edges), f"chunk={chunk}"


def test_a_file_predicted_over_budget_is_refused_before_it_is_read():
    """Fail CLOSED, like the .sal guard: refuse with the numbers rather than swapping.

    `max_bytes=1` makes any real file over budget, which is the cheap way to exercise the
    refusal without writing a multi-GB fixture.
    """
    with tempfile.TemporaryDirectory() as d:
        path = _write_csv(os.path.join(d, "c.csv"), 2_000)
        with pytest.raises(digital_csv.CsvTooLargeError) as caught:
            _load(path, max_bytes=1)
        msg = str(caught.value)
        assert "rows" in msg and "GB" in msg, msg
        assert caught.value.rows > 0, "the estimate must name a row count"


def test_the_guard_can_be_opted_out_of():
    """`max_bytes=0` is the documented opt-out and must load."""
    with tempfile.TemporaryDirectory() as d:
        path = _write_csv(os.path.join(d, "c.csv"), 2_000)
        got = _load(path, max_bytes=0)                    # explicit opt-out
        assert got.clock_edges.size > 0
        generous = _load(path, max_bytes=1 << 30)         # plenty of room
        assert np.array_equal(got.clock_edges, generous.clock_edges)


def test_a_budget_that_cannot_be_determined_is_a_fixed_limit_not_unlimited(monkeypatch):
    """With no memory reading at all, the shared `memory_budget()` is the positive fixed
    budget, and a CSV predicted above it is refused; one predicted within it loads."""
    from swi3s_studio.ingest import saleae_sal
    monkeypatch.setattr(saleae_sal, "available_memory_bytes", lambda: 0)
    monkeypatch.setattr(saleae_sal, "total_memory_bytes", lambda: 0)
    monkeypatch.setattr(saleae_sal, "_WARNED_DEGRADED", True)         # one warning per process
    # memory_budget records its source in this global: setting it here makes monkeypatch
    # restore it afterwards.
    monkeypatch.setattr(saleae_sal, "_BUDGET_SOURCE", saleae_sal._BUDGET_SOURCE)
    budget = saleae_sal.memory_budget(None)
    assert budget == saleae_sal._UNKNOWN_BUDGET > 0
    with tempfile.TemporaryDirectory() as d:
        path = _write_csv(os.path.join(d, "c.csv"), 2_000)
        monkeypatch.setattr(digital_csv, "predict_peak_bytes", lambda *a, **k: budget + 1)
        with pytest.raises(digital_csv.CsvTooLargeError):
            _load(path)
        monkeypatch.setattr(digital_csv, "predict_peak_bytes", lambda *a, **k: budget)
        assert _load(path).clock_edges.size > 0


def test_the_row_estimate_is_close_enough_to_be_useful():
    """The guard divides file size by a sampled row width. It only has to be
    order-of-magnitude right, but a systematic error would make it refuse good files or
    admit bad ones, so pin it loosely."""
    with tempfile.TemporaryDirectory() as d:
        rows = 20_000
        path = _write_csv(os.path.join(d, "c.csv"), rows)
        with open(path, "rb") as f:
            sample = f.read(64 << 10)
        est = digital_csv.predict_rows(path, sample)
        assert 0.8 * rows <= est <= 1.25 * rows, f"estimated {est} for {rows} rows"


# ---- limits at their boundary --------------------------------------------------------

def _header_file(tmp_path, header: str, body="0,1,0\n1,0,1\n"):
    p = tmp_path / "h.csv"
    p.write_bytes((header + "\n" + body).encode())
    return str(p)


def test_a_first_row_of_exactly_the_longest_line_is_read_and_one_more_is_refused(tmp_path):
    fits = "Time," + "x" * (timebase.MAX_LINE_CHARS - len("Time,") - 1)      # + newline
    assert len(fits) + 1 == timebase.MAX_LINE_CHARS
    assert digital_csv.read_header(_header_file(tmp_path, fits))[0] == "Time"
    with pytest.raises(ValueError, match="longer than"):
        digital_csv.read_header(_header_file(tmp_path, fits + "x"))


def test_a_first_row_of_exactly_the_most_cells_is_read_and_one_more_is_refused(tmp_path):
    most = digital_csv._MAX_HEADER_CELLS
    ok = ",".join(["Time"] + [f"c{i}" for i in range(most - 1)])
    assert len(digital_csv.read_header(_header_file(tmp_path, ok))) == most
    with pytest.raises(ValueError, match=f"{most + 1:,} columns"):
        digital_csv.read_header(_header_file(tmp_path, ok + ",extra"))


def test_a_sample_rate_of_exactly_one_hertz_is_accepted_and_just_below_it_is_not(tmp_path):
    rows = "Time,A,B\n" + "".join(f"{i},{i % 2},{(i // 2) % 2}\n" for i in range(8))
    p = tmp_path / "slow.csv"
    p.write_text(rows)
    assert digital_csv.load_capture(str(p), 1, 2, sample_rate_hz=1).sample_rate_hz == 1
    with pytest.raises(ValueError, match=r"slow\.csv: the sample rate must be at least 1 Hz"):
        digital_csv.load_capture(str(p), 1, 2, sample_rate_hz=0.999)


def test_the_largest_sample_rate_is_accepted_and_just_above_it_is_not(tmp_path):
    p = tmp_path / "fast.csv"
    p.write_text("Time,A,B\n" + "".join(f"{i * 1e-15:.15f},{i % 2},{(i // 2) % 2}\n"
                                        for i in range(8)))
    top = int(timebase.MAX_RATE_HZ)
    assert digital_csv.load_capture(str(p), 1, 2, sample_rate_hz=top).sample_rate_hz == top
    for rate in (top + 1, 2 ** 64, float("inf")):
        with pytest.raises(ValueError, match=r"fast\.csv: the sample rate must be at least"):
            digital_csv.load_capture(str(p), 1, 2, sample_rate_hz=rate)


def test_timestamps_that_give_a_rate_beyond_the_maximum_are_refused_by_name(tmp_path):
    p = tmp_path / "tiny.csv"
    p.write_text("Time,A,B\n" + "".join(f"{i * 1e-17:.17f},{i % 2},{(i // 2) % 2}\n"
                                        for i in range(8)))
    with pytest.raises(ValueError, match=r"tiny\.csv: the timestamps give a sample rate"):
        digital_csv.load_capture(str(p), 1, 2)


# ---- the same-sample warning ---------------------------------------------------------

def _merge_note(tmp_path, rows, rate):
    from swi3s_studio import import_notes
    p = tmp_path / "m.csv"
    p.write_text("\n".join(rows) + "\n")
    with import_notes.collect() as notes:
        cap = digital_csv.load_capture(str(p), 1, 2, sample_rate_hz=rate)
    return cap, [str(n) for n in notes]


def test_the_same_sample_warning_counts_a_data_line_that_alone_merged(tmp_path):
    rows = ["Time [s],Channel 0,Channel 1"] + [
        f"{i * 1e-3:.9f},{(i // 10) % 2},{i % 2}" for i in range(60)]
    cap, notes = _merge_note(tmp_path, rows, 100)
    assert timebase.repeats(cap.clock_edges) == 0 and timebase.repeats(cap.data_edges) == 52
    assert notes == ["m.csv: 52 transitions land on the same sample as the one before at "
                     "100 Hz (the rate is coarser than the transitions)"]


def test_the_same_sample_warning_for_one_transition_is_in_the_singular(tmp_path):
    rows = ["Time [s],Channel 0,Channel 1", "0.000,0,0", "0.010,1,0", "0.011,0,0",
            "0.020,1,1"]
    cap, notes = _merge_note(tmp_path, rows, 100)
    assert notes == ["m.csv: 1 transition lands on the same sample as the one before at "
                     "100 Hz (the rate is coarser than the transitions)"]


def test_a_clock_column_that_is_also_the_data_column_is_refused_by_file(tmp_path):
    p = _write_csv(str(tmp_path / "small.csv"), 20)
    with pytest.raises(ValueError, match=r"small\.csv: the clock and the data are the same "
                                         r"column \(1\)"):
        digital_csv.load_capture(p, 1, 1)


def test_a_data_line_over_the_longest_line_is_refused_before_it_is_held(tmp_path):
    body = "0,1,0\n" + "1," + "0" * (timebase.MAX_LINE_CHARS + 10) + "\n"
    path = _header_file(tmp_path, "Time,A,B", body)
    with pytest.raises(ValueError, match=r"line 3 is longer than"):
        digital_csv.scan(path)
    with pytest.raises(ValueError, match=r"line 3 is longer than"):
        digital_csv.load_capture(path, 1, 2)


def test_a_data_line_is_read_no_further_than_the_longest_line():
    import io

    sizes = []

    class Recording(io.StringIO):
        def readline(self, size=-1):
            sizes.append(size)
            return super().readline(size)

    f = Recording("0,1\n" + "1," + "0" * (timebase.MAX_LINE_CHARS + 10) + "\n")
    with pytest.raises(ValueError, match=r"line 3 is longer than"):
        list(digital_csv._data_lines(f, "x.csv"))
    assert sizes and set(sizes) == {timebase.MAX_LINE_CHARS + 1}
