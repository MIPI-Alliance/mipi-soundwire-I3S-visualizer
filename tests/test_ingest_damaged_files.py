"""Truncated, partly written or damaged capture files are refused by the loaders' own
errors, and priced by what a load of them actually reads.

Each test builds a synthetic file in a temporary directory and damages it the way an
interrupted export or a partial copy does: a lost tail, a cut last row, a block chain
that no longer tiles its blob.
"""
import io
import struct
import zipfile
from pathlib import Path

import numpy as np
import pytest

from swi3s_studio.ingest import analog_csv, probe, sal_export, saleae_binary, saleae_sal
from swi3s_studio.ingest.capture import Capture

# ---- .sal: a channel whose block chain does not walk -------------------------------------


def _capture(n_clock=200_000, n_data=30_000, seed=3):
    rng = np.random.default_rng(seed)
    ce = np.cumsum(rng.integers(10, 13, n_clock)).astype(np.uint64)
    de = np.sort(rng.choice(ce + 1, size=n_data, replace=False)).astype(np.uint64)
    return Capture(ce, de, False, False, 500_000_000)


def _rewrite(path, member, change):
    """Rewrite the zip at `path` with `member`'s bytes replaced by `change(bytes)`."""
    with zipfile.ZipFile(path) as z:
        items = [(i, z.read(i.filename)) for i in z.infolist()]
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for info, data in items:
            z.writestr(info.filename, change(data) if info.filename == member else data)


@pytest.fixture
def sal_with_a_damaged_channel(tmp_path):
    path = str(tmp_path / "damaged.sal")
    sal_export.export_sal(_capture(), path)
    _rewrite(path, "digital-1.bin", lambda b: b + b"\x01\x02\x03")
    return path


def test_a_channel_that_does_not_walk_is_priced_whole_in_a_window(
        sal_with_a_damaged_channel, monkeypatch):
    path = sal_with_a_damaged_channel
    chains = saleae_sal._v3_chains(path, saleae_sal.read_info(path), [0, 1])
    assert chains[0][1] >= 0 and chains[1][1] < 0
    with zipfile.ZipFile(path) as z:
        blob = z.getinfo("digital-1.bin").file_size
    model = saleae_sal.window_model(path, [0, 1])
    assert model.whole_channels == {1: blob}
    w = (0, 1_000)
    streamed_only = saleae_sal.window_peak_bytes(model.edge_bytes(*w), model.max_block_bytes)
    assert model.peak_bytes(*w) == streamed_only + saleae_sal.whole_channel_peak_bytes(blob)

    reads = []
    real_read = zipfile.ZipFile.read
    monkeypatch.setattr(zipfile.ZipFile, "read",
                        lambda self, name, *a, **k: reads.append(name) or
                        real_read(self, name, *a, **k))
    # A budget the streamed channel alone fits in, and the damaged one read whole does not.
    budget = streamed_only + saleae_sal.whole_channel_peak_bytes(blob) // 2
    with pytest.raises(saleae_sal.SalTooLargeError) as caught:
        saleae_sal.load_capture(path, 0, 1, window=w, max_bytes=budget)
    err = caught.value
    assert err.whole_channels == (1,) and err.streamed
    assert err.peak == model.peak_bytes(*w)
    assert "Channel 1 is read whole" in str(err)
    assert "digital-1.bin" not in reads, "the damaged channel was inflated before the guard"


def test_the_probe_prices_a_window_as_the_loader_does_when_a_channel_does_not_walk(
        sal_with_a_damaged_channel):
    path = sal_with_a_damaged_channel
    pr = probe.probe([path])
    model = saleae_sal.window_model(path, [0, 1])
    rate = pr.sample_rate_hz
    t0, t1 = 0.0, 999 / rate
    with zipfile.ZipFile(path) as z:
        blob = z.getinfo("digital-1.bin").file_size
    assert pr.cost_bytes(t0, t1) == model.peak_bytes(0, 1_000)
    assert pr.cost_bytes(t0, t1) > saleae_sal.whole_channel_peak_bytes(blob)


def test_a_damaged_channel_is_refused_as_malformed_once_admitted(sal_with_a_damaged_channel):
    with pytest.raises(saleae_binary.MalformedSaleaeV3):
        saleae_sal.load_capture(sal_with_a_damaged_channel, 0, 1, window=(0, 1_000),
                                max_bytes=0)


# ---- v3: the chain start is searched for in one bounded prefix by every reader -----------


def test_every_v3_reader_looks_for_the_chain_start_in_the_same_prefix():
    edges = np.arange(5, 50_000, 7, dtype=np.uint64)
    blob = saleae_binary.build_channel_v3(False, edges)
    head = 16                                       # magic, version, type
    pad = b"\xff" * (saleae_binary.V3_CHAIN_START_SCAN + 100)
    late = blob[:head] + pad + blob[head:]          # chain starts past the prefix
    for reader in (saleae_binary.parse_channel_v3,
                   saleae_binary._find_v3_chain_start,
                   lambda d: saleae_binary.parse_channel_v3_window(d, 0, 1_000)):
        with pytest.raises(saleae_binary.MalformedSaleaeV3):
            reader(late)
    assert saleae_sal._v3_span_streaming(io.BytesIO(late), len(late)) == 0

    early = blob[:head] + pad[:1_000] + blob[head:]  # chain starts inside it
    got = saleae_binary.parse_channel_v3(early)
    assert np.array_equal(got.transition_samples, edges)
    assert saleae_binary._find_v3_chain_start(early)[0] == head + 1_000
    assert saleae_sal._v3_span_streaming(io.BytesIO(early), len(early), want_start=True)[1] \
        == head + 1_000


# ---- v0 .bin: a lost tail or a damaged count ----------------------------------------------


def _bin(path, n=20_000, rate=500e6):
    e = np.arange(1, n + 1, dtype=np.uint64) * 10
    saleae_binary.write_channel(str(path), False, e / rate)
    return str(path)


def _bin_pair(tmp_path):
    return _bin(tmp_path / "clk.bin"), _bin(tmp_path / "dat.bin", n=5_000)


def test_a_bin_that_lost_its_tail_is_refused_as_truncated(tmp_path):
    clk, dat = _bin_pair(tmp_path)
    raw = Path(dat).read_bytes()
    Path(dat).write_bytes(raw[:len(raw) // 2])
    with pytest.raises(ValueError, match="truncated") as caught:
        saleae_binary.load_capture(clk, dat, 500_000_000)
    assert dat in str(caught.value)
    held = (len(raw) // 2 - saleae_binary._HEADER.size) // 8
    assert saleae_binary.transition_count(dat) == held


def test_a_bin_with_a_damaged_count_is_refused_as_truncated_not_as_too_large(tmp_path):
    clk, dat = _bin_pair(tmp_path)
    raw = bytearray(Path(dat).read_bytes())
    struct.pack_into("<Q", raw, saleae_binary._HEADER.size - 8, 10_000_000_000)
    Path(dat).write_bytes(bytes(raw))
    assert saleae_binary.transition_count(dat) == 5_000
    assert saleae_binary.predict_peak_bytes(clk, dat) < 10_000_000
    with pytest.raises(ValueError, match="truncated") as caught:
        saleae_binary.load_capture(clk, dat, 500_000_000)
    assert not isinstance(caught.value, MemoryError)
    assert "10,000,000,000" in str(caught.value)


def test_the_rate_of_a_bin_cut_mid_value_is_still_inferred(tmp_path):
    clk, dat = _bin_pair(tmp_path)
    raw = Path(dat).read_bytes()
    Path(dat).write_bytes(raw[:saleae_binary._HEADER.size + 8 * 1_000 + 3])
    assert saleae_binary.infer_sample_rate([dat]) > 0


def test_a_sal_v0_channel_that_lost_its_tail_is_refused_as_truncated(tmp_path):
    clk, dat = _bin_pair(tmp_path)
    path = str(tmp_path / "v0.sal")
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("meta.json", '{"data": {"legacySettings": {"sampleRate": '
                                '{"digital": 500000000}}}}')
        z.writestr("digital-0.bin", Path(clk).read_bytes())
        z.writestr("digital-1.bin", Path(dat).read_bytes()[:2_000])
    with pytest.raises(ValueError, match="digital ch1: truncated"):
        saleae_sal.load_capture(path, 0, 1, max_bytes=0)


# ---- analog CSV: a partly written last row ------------------------------------------------


def _scope_csv(path, n, tail=""):
    t = np.arange(n) * 1e-9 - 1e-6
    clk = np.where((np.arange(n) // 4) % 2, 1.0, -1.0)
    with open(path, "w") as f:
        f.write("Model,MSO58\nSample Interval,1e-09\n\nTIME,CH1,CH2\n")
        f.write("\n".join(f"{t[i]:.6e},{clk[i]:.4e},{-clk[i]:.4e}" for i in range(n)) + "\n")
        f.write(tail)
    return str(path)


@pytest.mark.parametrize("tail", ["2.000000e-06,0.0,-", "2.000000e-06,0.0,1.2e",
                                  "2.000000e-06,0.0,-\n", "2.000000e-06,0.0,-\n\n"])
@pytest.mark.parametrize("batch", [7, 2_000, 50_000])
def test_a_partly_written_last_row_is_dropped(tmp_path, monkeypatch, tail, batch):
    monkeypatch.setattr(analog_csv, "_BATCH_ROWS", batch)
    whole = analog_csv.read_analog_csv(_scope_csv(tmp_path / "a.csv", 2_000), max_bytes=0)
    got = analog_csv.read_analog_csv(_scope_csv(tmp_path / "b.csv", 2_000, tail),
                                     max_bytes=0)
    assert np.array_equal(got["time"], whole["time"])
    assert all(np.array_equal(got["channels"][k], whole["channels"][k])
               for k in whole["channels"])


@pytest.mark.parametrize("batch", [7, 2_000, 50_000])
@pytest.mark.parametrize("at", [0, 6, 1_000, 1_998])
def test_an_unreadable_row_before_the_last_is_refused_by_name(tmp_path, monkeypatch,
                                                              batch, at):
    monkeypatch.setattr(analog_csv, "_BATCH_ROWS", batch)
    path = _scope_csv(tmp_path / "a.csv", 2_000)
    lines = Path(path).read_text().split("\n")
    first = lines.index("TIME,CH1,CH2") + 1
    lines[first + at] = "1.0e-06,oops,0.0"
    Path(path).write_text("\n".join(lines))
    with pytest.raises(ValueError, match=f"data row {at + 1:,} after the TIME header") \
            as caught:
        analog_csv.read_analog_csv(path, max_bytes=0)
    assert path in str(caught.value) and "oops" in str(caught.value)


def test_a_multi_channel_preamble_is_keyed_per_channel_block():
    rows = [["Model", "MSO58", "", "Model", "MSO58"],
            ["Sample Interval", "1.6e-10", "", "Sample Interval", "3.2e-10"],
            ["Channel", "CH1", "", "Channel", "CH2"],
            ["Vertical Units", "", "", "Vertical Units", "V"],
            ["Label"]]
    assert analog_csv._parse_preamble(rows) == {
        "Model": ["MSO58", "MSO58"],
        "Sample Interval": ["1.6e-10", "3.2e-10"],
        "Channel": ["CH1", "CH2"],
        "Vertical Units": ["", "V"],
        "Label": [""],
    }
    assert analog_csv._parse_preamble([["Model", "MSO58"], [], [""]]) == {"Model": ["MSO58"]}
