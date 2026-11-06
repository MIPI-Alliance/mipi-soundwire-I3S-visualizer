"""Saleae .sal checks: meta.json bounds and malformed fields, a damaged archive or
meta.json, windows that start before the capture, end at or before their start or end past
any sample count, malformed, non-finite and fractional windows, the sample rate argument,
the limits of `export_sal`, and the fallback of a
failed streamed window read (its error says the channels are read whole and why), and
a clock channel that is also the data channel."""
import json
import logging
import warnings
import zipfile

import numpy as np
import pytest

from swi3s_studio.ingest import sal_export
from swi3s_studio.ingest import saleae_binary as sb
from swi3s_studio.ingest import saleae_sal as ss
from swi3s_studio.ingest.capture import Capture

_RATE = 500_000_000


def _blob(edges, end=2000):
    return sb.build_logic2_channel_v3(False, np.asarray(edges, dtype=np.uint64),
                                      sample_rate_hz=_RATE, unix_ms=1, frac_ms=0.5,
                                      capture_end=end)


def _sal(tmp_path, meta, blobs=None):
    blobs = blobs or {"digital-0.bin": _blob(range(1, 200, 3)),
                      "digital-1.bin": _blob(range(2, 200, 5))}
    path = tmp_path / "c.sal"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("meta.json", meta if isinstance(meta, str) else json.dumps(meta))
        for name, blob in blobs.items():
            z.writestr(name, blob)
    return str(path)


def _meta(rate=_RATE, bin_data=None):
    if bin_data is None:
        bin_data = [{"type": "Digital", "file": f"digital-{i}.bin", "deviceChannel": i}
                    for i in range(2)]
    return {"data": {"legacySettings": {"sampleRate": {"digital": rate}}},
            "binData": bin_data}


def test_an_oversized_meta_json_is_refused_unread(tmp_path, monkeypatch):
    monkeypatch.setattr(ss, "_META_MAX_BYTES", 1000)
    path = _sal(tmp_path, json.dumps(_meta()) + " " * 5000)
    with pytest.raises(ValueError, match="meta.json is larger than 1,000 bytes"):
        ss.read_info(path)


def test_a_repeated_device_channel_keeps_the_first_file_and_says_so(tmp_path):
    bd = [{"type": "Digital", "file": "digital-0.bin", "deviceChannel": 0},
          {"type": "Digital", "file": "digital-1.bin", "deviceChannel": 0}]
    info = ss.read_info(_sal(tmp_path, _meta(bin_data=bd)))
    assert info.digital_files == {0: "digital-0.bin"}
    assert any("digital-1.bin (channel 0)" in n and "repeat" in n for n in info.notes)


@pytest.mark.parametrize("rate", ["abc", -5, float("nan"), 1.5, [3]])
def test_a_malformed_sample_rate_is_noted_and_taken_as_absent(tmp_path, rate):
    meta = json.dumps(_meta(rate))
    info = ss.read_info(_sal(tmp_path, meta))
    assert info.sample_rate_hz == 0
    assert any("sampleRate" in n for n in info.notes)


def test_a_malformed_device_channel_is_skipped_with_a_note(tmp_path):
    bd = [{"type": "Digital", "file": "digital-0.bin", "deviceChannel": "x"},
          {"type": "Digital", "file": "digital-1.bin", "deviceChannel": 1}]
    info = ss.read_info(_sal(tmp_path, _meta(bin_data=bd)))
    assert info.digital_files == {1: "digital-1.bin"}
    assert any("digital-0.bin" in n for n in info.notes)


def test_bin_data_that_is_not_a_list_falls_back_to_the_file_names(tmp_path):
    meta = _meta()
    meta["binData"] = 5
    info = ss.read_info(_sal(tmp_path, meta))
    assert info.channels == [0, 1]
    assert any("binData is not a list" in n for n in info.notes)


def test_a_window_starting_before_the_capture_is_read_from_zero_with_a_note(tmp_path):
    path = _sal(tmp_path, _meta())
    whole = ss.load_capture(path, 0, 1, max_bytes=0, window=(0, 300))
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        cap = ss.load_capture(path, 0, 1, max_bytes=0, window=(-100, 300))
    assert np.array_equal(cap.clock_edges, whole.clock_edges)
    assert any("before the capture" in str(x.message) for x in w)


def test_a_window_ending_before_it_starts_is_refused(tmp_path):
    path = _sal(tmp_path, _meta())
    with pytest.raises(ValueError, match="must be past start"):
        ss.load_capture(path, 0, 1, max_bytes=0, window=(2000, 100))


def test_a_window_end_beyond_a_sample_count_is_read_to_the_end(tmp_path):
    path = _sal(tmp_path, _meta())
    whole = ss.load_capture(path, 0, 1, max_bytes=0)
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        cap = ss.load_capture(path, 0, 1, max_bytes=0, window=(0, 2 ** 70))
    assert np.array_equal(cap.clock_edges, whole.clock_edges)
    assert np.array_equal(cap.data_edges, whole.data_edges)
    assert any("past any capture" in str(x.message) for x in w)


def test_a_window_end_beyond_float64_is_read_to_the_end_with_a_note(tmp_path):
    path = _sal(tmp_path, _meta())
    whole = ss.load_capture(path, 0, 1, max_bytes=0)
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        cap = ss.load_capture(path, 0, 1, max_bytes=0, window=(0, 10 ** 400))
    assert np.array_equal(cap.clock_edges, whole.clock_edges)
    assert any("past any capture" in str(x.message) for x in w)


@pytest.mark.parametrize("window, text", [
    ((0, float("inf")), r"c\.sal: window: the end sample \(inf\) is not a finite number"),
    ((float("nan"), 5), r"c\.sal: window: the start sample \(nan\) is not a finite number"),
    ((None, 5), r"c\.sal: window: the start sample \(None\) is not a number"),
    ((0, "x"), r"c\.sal: window: the end sample \('x'\) is not a number"),
    ((5,), r"c\.sal: window must be a \(start, end\) pair"),
    ((), r"c\.sal: window must be a \(start, end\) pair"),
    (7, r"c\.sal: window must be a \(start, end\) pair"),
])
def test_a_malformed_window_raises_a_value_error_naming_the_file(tmp_path, window, text):
    path = _sal(tmp_path, _meta())
    with pytest.raises(ValueError, match=text):
        ss.load_capture(path, 0, 1, max_bytes=0, window=window)


def test_a_fractional_window_bound_is_rounded_and_noted(tmp_path):
    path = _sal(tmp_path, _meta())
    want = ss.load_capture(path, 0, 1, max_bytes=0, window=(2, 20))
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        cap = ss.load_capture(path, 0, 1, max_bytes=0, window=(1.6, 20.5))
    assert np.array_equal(cap.clock_edges, want.clock_edges)
    msgs = [str(x.message) for x in w]
    assert any("start sample 1.6 is not a whole sample; it is read as 2" in m for m in msgs)
    assert any("end sample 20.5 is not a whole sample; it is read as 20" in m for m in msgs)


def test_a_fractional_window_end_is_cut_at_the_sample_it_is_rounded_to(tmp_path):
    """The clock edge at sample 22 separates an end read as 22 (cut before it) from one
    read as 23 (kept)."""
    path = _sal(tmp_path, _meta())
    below = ss.load_capture(path, 0, 1, max_bytes=0, window=(2, 22))
    above = ss.load_capture(path, 0, 1, max_bytes=0, window=(2, 23))
    assert below.clock_edges.size + 1 == above.clock_edges.size
    with warnings.catch_warnings(record=True):
        warnings.simplefilter("always")
        low = ss.load_capture(path, 0, 1, max_bytes=0, window=(1.6, 22.4))
        high = ss.load_capture(path, 0, 1, max_bytes=0, window=(1.6, 22.6))
    assert np.array_equal(low.clock_edges, below.clock_edges)
    assert np.array_equal(high.clock_edges, above.clock_edges)


def test_a_failed_streamed_read_is_logged_and_read_whole(tmp_path, monkeypatch, caplog):
    path = _sal(tmp_path, _meta())
    ref = ss.load_capture(path, 0, 1, max_bytes=0, window=(0, 300))

    def boom(*a, **k):
        raise IndexError("streaming bug")
    monkeypatch.setattr(ss, "_decode_v3_window_streaming", boom)
    with caplog.at_level(logging.WARNING, logger=ss.logger.name):
        cap = ss.load_capture(path, 0, 1, max_bytes=0, window=(0, 300))
    assert np.array_equal(cap.clock_edges, ref.clock_edges)
    assert any("streamed window read" in r.getMessage() for r in caplog.records)


def test_a_failed_streamed_read_does_not_escape_the_memory_guard(tmp_path, monkeypatch):
    path = _sal(tmp_path, _meta())
    cost = ss.estimate_cost(path, channels=[0, 1])
    budget = cost.est_peak_bytes // 2
    assert budget > 1
    monkeypatch.setattr(ss, "memory_budget", lambda max_bytes=None: budget)
    monkeypatch.setattr(ss.SalCost, "window_peak_bytes", lambda self, model, window: 1)
    assert ss.load_capture(path, 0, 1, window=(0, 300)).clock_edges.size   # window fits

    def boom(*a, **k):
        raise IndexError("streaming bug")
    monkeypatch.setattr(ss, "_decode_v3_window_streaming", boom)
    with pytest.raises(ss.SalTooLargeError) as caught:
        ss.load_capture(path, 0, 1, window=(0, 300))
    err = caught.value
    assert err.window == (0, 300) and not err.streamed
    assert "choose a window" not in str(err).lower()
    assert "whatever the window" in str(err)
    assert "IndexError: streaming bug" in str(err)


# ---------------------------------------------------- huge numbers in meta.json

@pytest.mark.parametrize("rate", [10 ** 400, "1" + "0" * 400, 1e300, 2 ** 64,
                                  int(ss.MAX_RATE_HZ) + 1])
def test_a_sample_rate_beyond_the_maximum_is_noted_and_taken_as_absent(tmp_path, rate):
    info = ss.read_info(_sal(tmp_path, json.dumps(_meta(rate))))
    assert info.sample_rate_hz == 0
    assert any("sampleRate" in n and "from 1 to" in n for n in info.notes)
    assert all(len(n) < 300 for n in info.notes)


def test_the_largest_accepted_sample_rate_and_the_smallest_are_read(tmp_path):
    assert ss.read_info(_sal(tmp_path, _meta(int(ss.MAX_RATE_HZ)))).sample_rate_hz == int(
        ss.MAX_RATE_HZ)
    assert ss.read_info(_sal(tmp_path, _meta(1))).sample_rate_hz == 1


def test_a_huge_device_channel_is_kept_without_an_error(tmp_path):
    bd = [{"type": "Digital", "file": "digital-0.bin", "deviceChannel": 10 ** 400},
          {"type": "Digital", "file": "digital-1.bin", "deviceChannel": "9" * 400}]
    info = ss.read_info(_sal(tmp_path, _meta(bin_data=bd)))
    assert sorted(info.digital_files.values()) == ["digital-0.bin", "digital-1.bin"]


@pytest.mark.parametrize("value, want", [
    (10 ** 400, 10 ** 400), ("7", 7), (7.0, 7), (" 7 ", 7), ("1e3", 1000), (7.5, None),
    (True, None), (float("nan"), None), (float("inf"), None), ("x", None), (None, None),
    ([1], None)])
def test_whole_number(value, want):
    assert ss._whole_number(value) == want


# ---------------------------------------------------- a damaged archive or meta.json

def _write(tmp_path, name, data: bytes):
    p = tmp_path / name
    p.write_bytes(data)
    return str(p)


def test_a_file_that_is_not_a_zip_is_refused_by_name(tmp_path):
    p = _write(tmp_path, "junk.sal", b"not a zip at all" * 50)
    with pytest.raises(ValueError, match=r"junk\.sal: cannot read as a \.sal capture"):
        ss.read_info(p)


def test_a_zip_without_meta_json_is_refused_by_name(tmp_path):
    p = tmp_path / "nometa.sal"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("digital-0.bin", b"x")
    with pytest.raises(ValueError, match=r"nometa\.sal: cannot read as a \.sal capture"):
        ss.read_info(str(p))


@pytest.mark.parametrize("meta", [b"", b"{not json", b"\xff\xfe\x80 broken",
                                  b"[" * 100_000, b"1" * 5000],
                         ids=["empty", "not_json", "not_utf8", "deep_nesting", "long_number"])
def test_a_meta_json_that_is_not_readable_json_is_refused_by_name(tmp_path, meta):
    p = tmp_path / "badmeta.sal"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("meta.json", meta)
    with pytest.raises(ValueError, match=r"badmeta\.sal: (meta\.json is not readable JSON|"
                                        r"cannot read as a \.sal capture)"):
        ss.read_info(str(p))


def test_a_missing_file_still_raises_file_not_found(tmp_path):
    with pytest.raises(FileNotFoundError):
        ss.read_info(str(tmp_path / "absent.sal"))


def test_a_flip_of_any_byte_of_a_sal_loads_or_raises_a_value_error(tmp_path):
    path = _sal(tmp_path, _meta())
    with open(path, "rb") as f:
        raw = bytearray(f.read())
    for pos in range(len(raw)):
        mutated = bytearray(raw)
        mutated[pos] ^= 0xFF
        p = _write(tmp_path, "m.sal", bytes(mutated))
        try:
            ss.load_capture(p, 0, 1, max_bytes=0)
        except sb.UnsupportedSaleaeVersion as e:
            assert "unsupported <SALEAE> digital format" in str(e), pos
        except ValueError as e:
            assert "m.sal" in str(e), (pos, str(e))


def test_a_blob_whose_deflate_stream_is_damaged_is_refused_by_name(tmp_path):
    path = _sal(tmp_path, _meta(), blobs={
        "digital-0.bin": _blob(range(1, 4000, 3), end=5000),
        "digital-1.bin": _blob(range(2, 4000, 5), end=5000)})
    with open(path, "rb") as f:
        raw = bytearray(f.read())
    with zipfile.ZipFile(path) as z:
        info = z.getinfo("digital-0.bin")
    mid = info.header_offset + 30 + len(info.filename) + info.compress_size // 2
    for k in range(8):
        raw[mid + k] ^= 0xA5
    p = _write(tmp_path, "dmg.sal", bytes(raw))
    with pytest.raises(ValueError, match=r"dmg\.sal"):
        ss.load_capture(p, 0, 1, max_bytes=0)


# ---------------------------------------------------- the sample rate argument

@pytest.mark.parametrize("rate", [2 ** 64, float("inf"), 1e300, -1.0, float("nan")])
def test_a_given_sample_rate_outside_the_range_is_refused_by_name(tmp_path, rate):
    path = _sal(tmp_path, _meta())
    with pytest.raises(ValueError, match=r"c\.sal: the sample rate must be at least 1 Hz"):
        ss.load_capture(path, 0, 1, sample_rate_hz=rate, max_bytes=0)


@pytest.mark.parametrize("top", [(1 << 62) - 1, 1 << 40])
def test_an_exported_sal_below_the_sample_limit_reads_back_exactly(tmp_path, top):
    cap = Capture(np.array([top - 3, top], dtype=np.uint64),
                  np.array([top - 2], dtype=np.uint64), False, True, _RATE)
    path = str(tmp_path / "e.sal")
    sal_export.export_sal(cap, path)
    got = ss.load_capture(path, 0, 1, max_bytes=0)
    assert np.array_equal(got.clock_edges, cap.clock_edges)
    assert np.array_equal(got.data_edges, cap.data_edges)


@pytest.mark.parametrize("top", [1 << 62, (1 << 63) + 7, (1 << 64) - 1])
def test_export_sal_refuses_an_edge_past_the_sample_limit_before_writing(tmp_path, top):
    cap = Capture(np.array([5, top], dtype=np.uint64), np.array([6], dtype=np.uint64),
                  False, True, _RATE)
    path = tmp_path / "e.sal"
    with pytest.raises(ValueError, match=r"export_sal: the edge at sample .* is past the "
                                         r"4,611,686,018,427,387,904 samples a \.sal holds"):
        sal_export.export_sal(cap, str(path))
    assert not path.exists()


def test_a_clock_channel_that_is_also_the_data_channel_is_refused_by_file(tmp_path):
    path = _sal(tmp_path, _meta())
    with pytest.raises(ValueError, match=r"c\.sal: the clock and the data are the same "
                                         r"channel \(1\)"):
        ss.load_capture(path, 1, 1, max_bytes=0)


def test_a_failed_export_leaves_the_existing_file_and_no_temporary_one(tmp_path):
    """Two edges of one channel on one sample cannot be written as a .sal; the project that
    was at the path stays as it was and nothing else is left in the directory."""
    good = Capture(np.array([5, 10, 20], dtype=np.uint64), np.array([7], dtype=np.uint64),
                   False, True, 1_000_000)
    path = tmp_path / "x.sal"
    sal_export.export_sal(good, str(path))
    before = path.read_bytes()
    bad = Capture(np.array([5, 10, 10, 20], dtype=np.uint64), np.array([7], dtype=np.uint64),
                  False, True, 1_000_000)
    with pytest.raises(ValueError, match="at most one transition per sample"):
        sal_export.export_sal(bad, str(path))
    assert path.read_bytes() == before
    assert [p.name for p in tmp_path.iterdir()] == ["x.sal"]
    got = ss.load_capture(str(path), 0, 1, max_bytes=0)
    assert got.clock_edges.tolist() == [5, 10, 20]


def test_a_failed_export_to_a_new_path_creates_nothing(tmp_path):
    bad = Capture(np.array([5, 10, 10, 20], dtype=np.uint64), np.array([7], dtype=np.uint64),
                  False, True, 1_000_000)
    with pytest.raises(ValueError):
        sal_export.export_sal(bad, str(tmp_path / "new.sal"))
    assert list(tmp_path.iterdir()) == []


def test_the_export_limit_is_the_loaders_window_limit():
    assert sal_export.MAX_WINDOW_END is ss.MAX_WINDOW_END == 1 << 62


@pytest.mark.parametrize("window, clock, initial_clock, data, initial_data", [
    ((1, 3), [1], True, [1], False),
    ((0, 4), [1, 2, 3], False, [2], False),
    ((2, 4), [1], False, [], True)])
def test_a_window_beyond_2_pow_53_cuts_on_exact_sample_numbers(tmp_path, window, clock,
                                                              initial_clock, data,
                                                              initial_data):
    """Edges and window bounds are compared as integers: neighbouring samples near 2**60
    are not merged by a float conversion."""
    base = 1 << 60
    cap = Capture(np.array([base + 1, base + 2, base + 3], dtype=np.uint64),
                  np.array([base + 2], dtype=np.uint64), False, False, _RATE)
    path = str(tmp_path / "big.sal")
    sal_export.export_sal(cap, path)
    got = ss.load_capture(path, 0, 1, window=(base + window[0], base + window[1]), max_bytes=0)
    assert got.clock_edges.tolist() == clock and got.initial_clock == initial_clock
    assert got.data_edges.tolist() == data and got.initial_data == initial_data
