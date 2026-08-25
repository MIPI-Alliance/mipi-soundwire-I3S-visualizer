"""Digital-CSV ingest is BOUNDED and BATCH-INVARIANT.

Two properties, both of which the loader lacked:

1. **Bounded.** It used to build a Python list of every row plus three more full-length
   Python lists, peaking at ~12x the file size (415 MB for a 34 MB / 2M-row export) with no
   guard at all — while the `.sal` path has had a fail-closed memory guard and tests for
   several releases. A format the architecture doc calls multi-GB cannot cost 12x.

2. **Batch-invariant.** The fix reads in batches, which introduces exactly the failure mode
   the ingest review warned about for every other chunked reader here: a boundary bug that
   only shows when a batch edge lands mid-capture. So the interesting test is not "does it
   load" but "does a batch size that forces many boundaries produce byte-identical output to
   one that forces none".

Run: PYTHONPATH=. python3 -m pytest tests/test_digital_csv_bounded.py
"""
import csv
import os
import tempfile

import numpy as np
import pytest

from swi3s_studio.ingest import digital_csv


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
    """The non-numeric-time fallback moved inside the per-batch conversion, so it now runs
    per batch instead of once over the whole file. A stray cell must still be dropped, and
    dropping it must not depend on where the batch boundaries fall."""
    with tempfile.TemporaryDirectory() as d:
        path = _write_csv(os.path.join(d, "bad.csv"), 400, bad_time_at=201)
        monkeypatch.setattr(digital_csv, "_ROW_CHUNK", 10_000)
        whole = _load(path)
        for chunk in (50, 200, 201, 202):        # boundary before/at/after the bad row
            monkeypatch.setattr(digital_csv, "_ROW_CHUNK", chunk)
            got = _load(path)
            assert np.array_equal(got.clock_edges, whole.clock_edges), f"chunk={chunk}"
            assert np.array_equal(got.data_edges, whole.data_edges), f"chunk={chunk}"


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


def test_the_guard_can_be_opted_out_of_but_not_silently_disabled():
    """`max_bytes=0` is the documented opt-out and must load. A budget that cannot be
    determined must NOT become "unlimited" — that is the .sal guard's fail-closed rule, and
    it is inherited here by sharing `memory_budget()` rather than reimplementing it."""
    with tempfile.TemporaryDirectory() as d:
        path = _write_csv(os.path.join(d, "c.csv"), 2_000)
        got = _load(path, max_bytes=0)                    # explicit opt-out
        assert got.clock_edges.size > 0
        generous = _load(path, max_bytes=1 << 30)         # plenty of room
        assert np.array_equal(got.clock_edges, generous.clock_edges)

    from swi3s_studio.ingest import saleae_sal
    assert digital_csv.load_capture.__doc__ and "memory_budget" in \
        digital_csv.load_capture.__doc__, "the shared budget should be named in the contract"
    assert saleae_sal.memory_budget(None) > 0, \
        "an unknown-memory budget must still be a positive number, not unlimited"


def test_the_row_estimate_is_close_enough_to_be_useful():
    """The guard divides file size by a sampled row width. It only has to be
    order-of-magnitude right, but a systematic error would make it refuse good files or
    admit bad ones, so pin it loosely."""
    with tempfile.TemporaryDirectory() as d:
        rows = 20_000
        path = _write_csv(os.path.join(d, "c.csv"), rows)
        with open(path, "rb") as f:
            sample = f.read(64 << 10)
        est = digital_csv._predict_rows(path, 3, sample)
        assert 0.8 * rows <= est <= 1.25 * rows, f"estimated {est} for {rows} rows"
