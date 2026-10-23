"""A surviving port reconfigured by an immediate single-ranked write keeps its channel state.

`make_immediate_scrambler_levels` builds one scrambled PCM port and clears
PortControl.ScramblerEn mid-stream with a single-ranked WriteA32. The engine re-applies
the new config to the surviving port with its transport phase restored; these tests check
that the per-channel sample assembly carries across the write too: the running `index`
continues, the in-flight sample keeps its start sample, and no sample is emitted with a
start sample of 0 after the first.

`make_immediate_sample_size_levels` changes SampleSize with a single-ranked write whose
reconfigure lands while a sample is in flight. That sample finishes at the width it
started with, and the samples after it use the new width.
"""
import math

import pytest
import swi3score


def _decode(n: int, mid_interval: bool):
    levels = swi3score.make_immediate_scrambler_levels(n, mid_interval=mid_interval)
    src = swi3score.MemorySampleSource(levels, 98_304_000, 4)
    d = swi3score.Decoder(src, swi3score.DecoderSettings())
    d.run()
    assert any(c["command"] == "WriteA32" and c["crc_valid"] and c["address"] == 0x200B
               for c in d.commands()), "fixture must carry the single-ranked write"
    return d


@pytest.mark.parametrize("mid_interval", [False, True])
def test_channel_index_continues_across_immediate_write(mid_interval):
    N = 64
    d = _decode(N, mid_interval)
    by_ch: dict[int, list[dict]] = {}
    for a in d.audio():
        if a["dp"] == 0:
            by_ch.setdefault(a["channel"], []).append(a)
    assert by_ch, "no dp0 audio decoded"
    for ch, samples in by_ch.items():
        samples.sort(key=lambda a: a["end_sample"])
        assert len(samples) >= 2 * N - 4, f"ch{ch}: too few samples ({len(samples)})"
        idx = [a["index"] for a in samples]
        assert idx == list(range(len(idx))), (
            f"ch{ch}: per-channel index restarts across the immediate write: "
            f"{[i for k, i in enumerate(idx) if i != k][:4]}")


@pytest.mark.parametrize("mid_interval", [False, True])
def test_in_flight_sample_keeps_its_start_sample(mid_interval):
    N = 64
    d = _decode(N, mid_interval)
    by_ch: dict[int, list[dict]] = {}
    for a in d.audio():
        if a["dp"] == 0:
            by_ch.setdefault(a["channel"], []).append(a)
    for ch, samples in by_ch.items():
        samples.sort(key=lambda a: a["end_sample"])
        for prev, cur in zip(samples, samples[1:]):
            assert cur["start_sample"] > prev["end_sample"], (
                f"ch{ch} index {cur['index']}: start_sample {cur['start_sample']} "
                f"does not follow the previous sample's end {prev['end_sample']}")
        assert all(a["start_sample"] < a["end_sample"] for a in samples)
        assert all(a["sample_size"] == samples[0]["sample_size"] for a in samples)


def _sine(index: int, size: int) -> int:
    """Demo.cpp sineSample(0, index, size): `size` is the SampleSize field (bits - 1)."""
    x = (1 << size) * 0.45 * math.sin(2.0 * math.pi * index / 64.0)
    v = math.floor(x + 0.5) if x >= 0 else -math.floor(-x + 0.5)
    return v & ((1 << (size + 1)) - 1)


@pytest.mark.parametrize("from_size,to_size", [(23, 15), (15, 23)])
@pytest.mark.parametrize("rows_into_sample", [1, 8, 15])
def test_sample_in_flight_keeps_its_width_across_sample_size_write(
        from_size, to_size, rows_into_sample):
    N = 16
    levels = swi3score.make_immediate_sample_size_levels(
        N, from_size, to_size, rows_into_sample)
    src = swi3score.MemorySampleSource(levels, 98_304_000, 4)
    d = swi3score.Decoder(src, swi3score.DecoderSettings())
    d.run()
    assert any(c["command"] == "WriteA32" and c["crc_valid"] and c["address"] == 0x2009
               for c in d.commands()), "fixture must carry the SampleSizeGrouping write"
    samples = sorted((a for a in d.audio() if a["dp"] == 0), key=lambda a: a["end_sample"])
    assert len(samples) == 2 * N
    widths = [a["sample_size"] for a in samples]
    # Samples 0..N started before the reconfigure (N is in flight across it).
    assert widths == [from_size + 1] * (N + 1) + [to_size + 1] * (N - 1)
    assert [a["index"] for a in samples] == list(range(2 * N))
    for prev, cur in zip(samples, samples[1:]):
        assert cur["start_sample"] > prev["end_sample"]
    wrong = [(a["index"], a["sample_size"], hex(a["value"]))
             for a in samples if a["value"] != _sine(a["index"], a["sample_size"] - 1)]
    assert not wrong, f"samples decoded with the wrong width or bits: {wrong}"


def test_total_uis_docstring_describes_the_refreshed_total():
    """`Decoder.total_uis` is re-read from the source during run() and at its end, so its
    Python docstring must not tell a caller the value can be cached once run() starts."""
    doc = swi3score.Decoder.total_uis.__doc__ or ""
    assert "fixed once run() starts" not in doc
    assert "refreshed" in doc and "estimate" in doc
    assert "treat 0 as unknown" in doc
