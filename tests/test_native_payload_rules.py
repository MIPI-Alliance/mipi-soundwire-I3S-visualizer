"""Rules of the payload engine's per-channel state across a re-anchoring reconfigure, and of
the flow-control handshake check.

- A port that stays enabled across an SSCR keeps its running sample index per channel.
- The {ASW5205} handshake check reads the recovered TX_PRESENT bit, the one that gates the
  sample, so a port with ScramblerEn set is checked on its descrambled bit.
- The RX_CONTROLLED handshake is bijective: a skipped transport after a DRQ of 1 fails it,
  while ASYNC allows one. A TX_PRESENT slot carries bit index 0.
- A wide bit (BitWidth 1) is sampled on its last UI: its first UI does not change the audio.
  A sink port's wide bit occupies every one of its UIs in the grid, as a source port's
  does, with its sample marker on the last.
- A source data port places TailWidth Tail slots after its data in each row, with one
  Guard slot first when GuardEnable is set.
- A sink data port places no Guard or Tail slot, whatever its GuardEnable and TailWidth,
  in the grid and in the decode: its bit sample points and audio are those of the same
  port with neither.
- decode_saleae_v3_deltas decodes a code up to the int64 limit and raises ValueError for
  one past it.
"""
import csv
import io
from collections import defaultdict

import numpy as np
import pytest
import swi3score


def _decode(levels, **settings):
    s = swi3score.DecoderSettings()
    for k, v in settings.items():
        setattr(s, k, v)
    d = swi3score.Decoder(swi3score.MemorySampleSource(levels, 98_304_000, 4), s)
    d.run()
    return d


def test_sample_index_continues_for_a_port_that_survives_an_sscr():
    """The demo's second SSCR (8 -> 16 columns) keeps DP0..DP3 enabled: each channel's
    index counts 0, 1, 2, ... without restarting."""
    d = _decode(swi3score.make_demo_levels(32))
    assert sum(1 for c in d.commands() if c["command"] == "SSCR" and c["is_commit"]) == 2
    by_key = defaultdict(list)
    for a in d.audio():
        by_key[(a["device"], a["dp"], a["channel"])].append(a)
    assert by_key
    for key, samples in by_key.items():
        samples.sort(key=lambda a: a["end_sample"])
        idx = [a["index"] for a in samples]
        assert idx == list(range(len(idx))), (key, [i for k, i in enumerate(idx) if i != k][:4])


def _flow_fails(overrides):
    d = _decode(swi3score.make_demo_levels_flow_control(200), scrambler_overrides=overrides)
    return d.flow_control_stats()["fails"]


def test_the_handshake_check_reads_the_descrambled_tx_present():
    """The demo's wire is unscrambled. Forcing ScramblerEn on a flow-controlled port makes
    the engine descramble TX_PRESENT, so the check sees a different bit than the wire
    carries and reports violations; read raw, it would report none."""
    assert _flow_fails([]) == 0
    assert _flow_fails([(2, 0, True)]) > 0


_SPU = 4


def _level_index(sample):
    """The index into a MemorySampleSource's levels of the UI at capture `sample`: the
    source emits one alignment UI first and numbers UI i's sample (i + 1) * samples per UI."""
    return int(sample) // _SPU - 2


def _drq_points(levels, device):
    """Every DRQ observe point of `device`'s port in the flow-control demo, as level indices:
    the DRQs the decoded samples name (drq_sample) are one per interval, so their spacing's
    GCD is the interval and every interval between the first and last has one."""
    a = _decode(levels).audio_columns()
    named = sorted(set(a["drq_sample"][a["device"] == device].tolist()) - {0})
    period = int(np.gcd.reduce(np.diff(named)))
    return [_level_index(s) for s in range(named[0], named[-1] + 1, period)]


@pytest.mark.parametrize("device,fails", [(2, 1), (3, 0)], ids=["rx_controlled", "async"])
def test_a_skipped_transport_after_a_drq_of_1_fails_only_rx_controlled(device, fails):
    """The demo's device 2 runs RX_CONTROLLED and device 3 ASYNC, each with a TX_PRESENT of 0
    after every DRQ of 0. A DRQ turned from 0 to 1 on the wire leaves a TX_PRESENT of 0
    after a DRQ of 1: RX_CONTROLLED requires TX_PRESENT to equal the DRQ, so that is one
    failure; ASYNC sends data only when requested and may skip a request, so it is none."""
    levels = list(swi3score.make_demo_levels_flow_control(200))
    zeros = [u for u in _drq_points(levels, device) if not levels[u]]
    assert len(zeros) > 10
    for u in zeros[2:5]:
        changed = list(levels)
        changed[u] = True
        assert _decode(changed).flow_control_stats()["fails"] == fails, u


def test_a_tx_present_slot_carries_bit_index_0():
    cells = _decode(swi3score.make_demo_levels_flow_control(32)).grid_cells(64)
    txp = [c for c in cells if c["slot"] == 2]                  # SwI3sSlot::TxPresent
    assert {c["device"] for c in txp} == {1, 2, 3}
    assert {c["bit"] for c in txp} == {0}


def _config(tmp_path, name, **fields):
    """demo_config() as a config CSV with DP0's cells of `fields` replaced."""
    from swi3s_studio.model.bus_config import demo_config
    rows = list(csv.reader(io.StringIO(demo_config().to_csv())))
    for r in rows:
        if r and r[0] in fields:
            r[1] = fields[r[0]]
    path = tmp_path / name
    path.write_text("\n".join(",".join(r) for r in rows) + "\n", encoding="utf-8")
    assert swi3score.csv_warnings(str(path)) == []
    return str(path)


def test_a_wide_bit_is_sampled_on_its_last_ui(tmp_path):
    """DP0 at BitWidth 1 (two UIs per bit) under a config CSV. In the grid each bit spans two
    columns of a row and its sample marker is on the second; the decode samples each bit on
    that column (counted from the section's Column 0); changing the UI each bit was
    sampled on changes the audio, and changing the first UI of every bit does not."""
    path = _config(tmp_path, "wide.csv", BitWidth_REG="1")
    pairs = defaultdict(list)
    for c in swi3score.grid_from_csv(path, 64):
        if c["slot"] == 1 and c["dp"] == 0:
            pairs[(c["row"], c["channel"], c["sample"], c["bit"])].append(c)
    assert pairs and all(len(p) == 2 for p in pairs.values())
    marked = set()
    for first, last in (sorted(p, key=lambda c: c["col"]) for p in pairs.values()):
        assert last["col"] == first["col"] + 1
        assert (first["sample_here"], last["sample_here"]) == (False, True)
        marked.add(last["col"])
    levels = list(swi3score.make_demo_levels(64))

    def decode(lv):
        return _decode(lv, config_csv_path=path, collect_bit_samples=True)
    d = decode(levels)
    values = d.audio_columns()["value"]
    sampled = [_level_index(s) for s in d.bit_samples()["sample"]]
    assert len(values) > 100 and len(sampled) > 1000
    [seg] = d.segments()
    column0 = seg["start_ui"] - 1                  # the level index of the first Column 0
    cols = {(u - column0) % seg["column_count"] for u in sampled}
    assert cols <= marked, sorted(cols - marked)

    def flipped(offset):
        lv = list(levels)
        for u in sampled:
            lv[u + offset] = not lv[u + offset]
        return decode(lv).audio_columns()["value"]
    assert np.array_equal(flipped(-1), values)
    assert not np.array_equal(flipped(0), values)


def test_a_sink_port_places_no_guard_or_tail(tmp_path):
    def slots(direction, tail):
        path = _config(tmp_path, f"dir_{direction}_{tail}.csv", GuardEnable_REG="True",
                       TailWidth_REG=tail, PortDirection_REG=direction)
        return {c["slot"] for c in swi3score.grid_from_csv(path, 64)
                if not c["is_cds"] and c["dp"] == 0}
    for tail in ("1", "2"):
        assert slots("False", tail) == {1, 3, 5}     # source: Data, Guard0, Tail
        assert slots("True", tail) == {1}            # sink: Data only
    levels = swi3score.make_demo_levels(32)

    def decode(**fields):
        d = _decode(levels, config_csv_path=_config(tmp_path, "sink.csv", **fields),
                    collect_bit_samples=True)
        return d.bit_samples()["sample"].tolist(), d.audio_columns()["value"].tolist()
    plain = decode(PortDirection_REG="True", GuardEnable_REG="False", TailWidth_REG="0")
    assert len(plain[0]) > 1000 and len(plain[1]) > 50
    for guard in ("False", "True"):
        for tail in ("1", "2", "3"):
            assert decode(PortDirection_REG="True", GuardEnable_REG=guard,
                          TailWidth_REG=tail) == plain, (guard, tail)


@pytest.mark.parametrize("guard", ["False", "True"])
@pytest.mark.parametrize("tail", [0, 1, 2, 3])
def test_a_source_port_places_its_tail_width_of_tail_slots(tmp_path, guard, tail):
    """The demo config's DP0 carries data in 8 rows of 64; each of those rows ends its data
    with one Guard slot when GuardEnable is set and `tail` Tail slots."""
    path = _config(tmp_path, f"tail_{guard}_{tail}.csv", GuardEnable_REG=guard,
                   TailWidth_REG=str(tail))
    per_row = defaultdict(lambda: defaultdict(int))
    for c in swi3score.grid_from_csv(path, 64):
        if not c["is_cds"] and c["dp"] == 0:
            per_row[c["row"]][c["slot"]] += 1
    rows = [r for r in per_row.values() if r[1]]
    assert len(rows) == 8
    for r in rows:
        assert (r[3], r[5]) == ((1 if guard == "True" else 0), tail), dict(r)


def test_a_sink_wide_bit_occupies_every_ui(tmp_path):
    """A sink port at BitWidth 1 and 2: each bit spans BitWidth + 1 columns of a row, and
    only the last carries the sample marker."""
    for width in (1, 2):
        path = _config(tmp_path, f"sink_wide{width}.csv", BitWidth_REG=str(width),
                       PortDirection_REG="True")
        bits = defaultdict(list)
        for c in swi3score.grid_from_csv(path, 64):
            if c["slot"] == 1 and c["dp"] == 0:
                bits[(c["row"], c["channel"], c["sample"], c["bit"])].append(c)
        whole = [cells for cells in bits.values() if len(cells) == width + 1]
        assert len(whole) >= 48                   # the rest are cut at a row's end
        for cells in whole:
            cells.sort(key=lambda c: c["col"])
            cols = [c["col"] for c in cells]
            assert cols == list(range(cols[0], cols[0] + width + 1))
            assert [c["sample_here"] for c in cells] == [False] * width + [True]


def test_v3_delta_decoder_raises_for_a_code_too_long_for_int64():
    buf = np.array([0x7F] + [0xFF] * 10 + [0x7F], np.uint8)
    with pytest.raises(ValueError, match="too long"):
        swi3score.decode_saleae_v3_deltas(buf, 0, len(buf))


def _v3(code):
    buf = np.array(code, np.uint8)
    return swi3score.decode_saleae_v3_deltas(buf, 0, len(buf)).tolist()


def test_v3_delta_decoder_decodes_the_largest_nine_digit_code():
    # MSB digit 0x3F (byte 0x7F), seven interior digits of 127 (0xFF), terminal 127 (0x7F):
    # 6 + 7 * 7 + 7 = 62 bits of ones, and the delta is the value plus one.
    assert _v3([0x7F] + [0xFF] * 7 + [0x7F]) == [2**62]


def test_v3_delta_decoder_takes_codes_up_to_the_int64_limit_and_no_further():
    # A code is taken while its value plus one fits int64. MSB digit 0 (0x40), eight interior
    # digits of 127 and terminal 126 (0x7E): value 2**63 - 2, the largest code accepted.
    assert _v3([0x40] + [0xFF] * 8 + [0x7E]) == [2**63 - 1]
    assert _v3([0x40] + [0xFF] * 8 + [0x05]) == [2**63 - 122]
    # Terminal 127: value 2**63 - 1, whose delta does not fit, the smallest code refused.
    with pytest.raises(ValueError, match="too long"):
        _v3([0x40] + [0xFF] * 8 + [0x7F])
