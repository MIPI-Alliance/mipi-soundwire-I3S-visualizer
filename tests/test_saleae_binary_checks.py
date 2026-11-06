"""Saleae binary checks: the first block's level field, the version-in-wrong-container
message, the export of a capture with two edges on one sample, the fold of an edge at sample 0
into the initial level, the sample-rate range a bin pair needs (at most the maximum, at
least 1 Hz), and a clock file that is also the data file."""
import os
import struct

import numpy as np
import pytest
from conftest import symlink_or_skip

from swi3s_studio.ingest import saleae_binary as sb

_RATE = 500_000_000


def _blob(init=False, edges=(5, 9)):
    return sb.build_logic2_channel_v3(init, np.array(edges, dtype=np.uint64),
                                      sample_rate_hz=_RATE, unix_ms=1, frac_ms=0.5,
                                      capture_end=100)


def test_the_whole_and_windowed_decodes_read_the_level_field_alike():
    blob = bytearray(_blob(False))
    first_block = sb._find_v3_chain_start(bytes(blob))[0]
    struct.pack_into("<H", blob, first_block + sb._V3_LEVEL_OFF, 2)      # bit 0 clear
    whole = sb.parse_channel_v3(bytes(blob))
    window = sb.parse_channel_v3_window(bytes(blob), 0, 100)
    assert whole.initial_state is False
    assert window.initial_state == whole.initial_state


def test_a_version_3_blob_as_a_bin_pair_says_where_it_is_read():
    with pytest.raises(sb.UnsupportedSaleaeVersion) as e:
        sb.parse_channel(_blob(), "digital-0.bin")
    assert "a .bin pair holds version 0" in str(e.value)
    assert "a .sal holds versions 0, 3 and 4" in str(e.value)


def test_two_edges_on_one_sample_are_refused_with_the_reason():
    with pytest.raises(ValueError, match="at most one transition per sample") as e:
        _blob(edges=(10, 20, 20, 30))
    assert "build_logic2_channel_v3" not in str(e.value)


@pytest.mark.parametrize("edges, first, second, sample", [
    ((0, 0, 5), 1, 2, 0),                    # two edges at sample 0
    ((0, 4, 4), 2, 3, 4),                    # numbered from the channel, not past the fold
    ((10, 20, 20, 30), 2, 3, 20),
    ((7, 7), 1, 2, 7)])
def test_the_transitions_named_are_numbered_as_the_channel_numbers_them(
        edges, first, second, sample):
    with pytest.raises(ValueError) as e:
        _blob(edges=edges)
    assert f"transitions {first:,} and {second:,} of a channel" in str(e.value)
    assert f"around sample {sample:,}" in str(e.value)
    assert "v3 delta" not in str(e.value)


def test_an_edge_at_sample_zero_is_folded_into_the_initial_level():
    got = sb.parse_channel_v3(_blob(init=False, edges=(0, 5, 9)))
    assert got.initial_state is True
    assert got.transition_samples.tolist() == [5, 9]


@pytest.mark.parametrize("rate", [2 ** 64, float("inf"), 1e300])
def test_a_bin_pair_needs_a_sample_rate_of_at_most_the_maximum(tmp_path, rate):
    from swi3s_studio.ingest.capture import Capture
    cap = Capture(np.array([10, 20, 30], dtype=np.uint64), np.array([15], dtype=np.uint64),
                  False, False, 1_000_000)
    clk, dat = str(tmp_path / "clk.bin"), str(tmp_path / "dat.bin")
    sb.write_capture(cap, clk, dat)
    with pytest.raises(ValueError, match=r"clk\.bin: the sample rate must be at least 1 Hz "
                                        r"and at most"):
        sb.load_capture(clk, dat, rate)


@pytest.mark.parametrize("rate", [0, -5, float("nan")])
def test_a_bin_pair_needs_a_sample_rate_of_at_least_one_hertz(tmp_path, rate):
    from swi3s_studio.ingest.capture import Capture
    cap = Capture(np.array([10, 20, 30], dtype=np.uint64), np.array([15], dtype=np.uint64),
                  False, False, 1_000_000)
    clk, dat = str(tmp_path / "clk.bin"), str(tmp_path / "dat.bin")
    sb.write_capture(cap, clk, dat)
    with pytest.raises(ValueError, match="at least 1 Hz"):
        sb.load_capture(clk, dat, rate)
    assert sb.load_capture(clk, dat, 1_000_000).clock_edges.size == 3


def test_a_clock_file_that_is_also_the_data_file_is_refused_by_name(tmp_path):
    clk = tmp_path / "clk.bin"
    sb.write_channel(str(clk), False, np.array([1e-6, 2e-6]), 0.0, 1e-5)
    with pytest.raises(ValueError, match=r"clk\.bin.*the clock and the data are the same "
                                         r"file"):
        sb.load_capture(str(clk), str(clk), _RATE)
    spelled = str(tmp_path) + os.sep + "." + os.sep + "clk.bin"       # pathlib would collapse '.'
    assert spelled != str(clk)
    with pytest.raises(ValueError, match="the same file"):
        sb.load_capture(str(clk), spelled, _RATE)


def test_a_symlink_to_the_clock_file_is_the_same_file(tmp_path):
    clk = tmp_path / "clk.bin"
    sb.write_channel(str(clk), False, np.array([1e-6, 2e-6]), 0.0, 1e-5)
    alias = tmp_path / "alias.bin"
    symlink_or_skip(clk, alias)
    with pytest.raises(ValueError, match="the same file"):
        sb.load_capture(str(clk), str(alias), _RATE)
