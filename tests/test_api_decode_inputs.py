"""api.decode reports the forced column count and scrambler overrides it defaults or leaves
out, as Session does for decode_capture.

Run: python3 -m pytest tests/test_api_decode_inputs.py
"""
import pytest
import swi3score

from swi3s_studio import decode, import_notes


def _source():
    return swi3score.MemorySampleSource(swi3score.make_demo_levels(4), 98_304_000, 4)


@pytest.mark.parametrize("count", [1, 33, 64, -3])
def test_a_forced_count_outside_the_range_is_detected_with_a_note(count):
    auto = decode(_source())
    with import_notes.collect() as notes:
        res = decode(_source(), forced_column_count=count)
    assert res.column_count == auto.column_count
    assert [n.message for n in notes] == [
        f"forced column count {count} is outside 2..32; the column count is detected"]


def test_a_forced_count_inside_the_range_is_taken_without_a_note():
    with import_notes.collect() as notes:
        decode(_source(), forced_column_count=8)
    assert list(notes) == []


def test_a_scrambler_override_with_a_bad_key_is_left_out_with_a_note():
    kept = {(0, 1): True}
    with import_notes.collect() as clean:
        want = decode(_source(), scrambler_overrides=kept)
    with import_notes.collect() as notes:
        got = decode(_source(), scrambler_overrides={**kept, (99, 0): True, "x": False,
                                                      (0, -1): True})
    assert list(clean) == []
    assert got.commands == want.commands
    [note] = [n for n in notes if n.message.startswith("scrambler_overrides: 3 entries")]
    assert "(99, 0)" in note.message and "'x'" in note.message


def test_a_bare_source_decode_refuses_a_core_of_another_abi(monkeypatch):
    monkeypatch.setattr(swi3score, "score_abi", int(swi3score.score_abi) - 1, raising=False)
    with pytest.raises(RuntimeError, match="out of date"):
        decode(_source())
