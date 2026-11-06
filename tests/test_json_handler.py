"""BusModel JSON round-trip (swviz.io.json_handler).

Non-payload slots (guard/tail/CDS/…) carry sample/bit = None, and the save->load
round trip must preserve that — coercing None to 0 corrupts the scrambler/test-mode
mismatch checks (0 is a valid payload sample).

Run: PYTHONPATH=. python3 -m pytest tests/test_json_handler.py
"""
import json
import os
import tempfile

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from swi3s_studio.swviz.io.json_handler import JSONHandler
from swi3s_studio.swviz.models.bus_model import BitInfo, BusModel
from swi3s_studio.swviz.models.enums import DirectionType, SlotType


def test_json_roundtrip_preserves_none_sample_bit():
    m = BusModel(num_rows=1, num_columns=4, row_rate=3072.0)
    # A payload DATA bit (sample/bit set) and a non-payload GUARD bit (sample/bit None).
    m.add_bit(BitInfo(bit_index=0, slot=SlotType.DATA, direction=DirectionType.SOURCE,
                      device=0, dp=0, channel=1, sample=0, bit=3))
    m.add_bit(BitInfo(bit_index=1, slot=SlotType.GUARD_0, direction=DirectionType.SOURCE,
                      device=0, dp=0))
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "model.json")
        JSONHandler.save_bus_model(p, m)
        m2 = JSONHandler.load_bus_model(p)
    by_idx = {b.bit_index: b for b in m2.bits}
    data, guard = by_idx[0], by_idx[1]
    assert (data.sample, data.bit) == (0, 3), (data.sample, data.bit)
    # The guard's sample/bit must come back None, not 0.
    assert guard.sample is None and guard.bit is None, (guard.sample, guard.bit)


def _model_with_findings():
    from swi3s_studio.swviz.utils.validators import ValidationResult
    m = BusModel(num_rows=2, num_columns=4, row_rate=6144.0)
    m.add_bit(BitInfo(bit_index=5, slot=SlotType.DATA, direction=DirectionType.SINK,
                      device=2, dp=1, channel=0, sample=1, bit=0))
    m.bus_clashes, m.device_clashes, m.read_overlaps = [5], [6], [7]
    m.warnings.interval_overflow_warnings += [("DP3", 40, 32), ("DP1", 9, 8), ("DP3", 1, 0)]
    m.warnings.validation_issues += [("Interface", ValidationResult()),
                                     ("DP2", ValidationResult())]
    return m


def test_json_roundtrip_keeps_the_clash_lists_and_which_ports_have_warnings(tmp_path):
    p = str(tmp_path / "deeper" / "model.json")
    JSONHandler.save_bus_model(p, _model_with_findings(), batch_mode=True)   # makes the dirs
    m2 = JSONHandler.load_bus_model(p)
    assert (m2.num_rows, m2.num_columns, m2.row_rate) == (2, 4, 6144.0)
    assert (m2.bus_clashes, m2.device_clashes, m2.read_overlaps) == ([5], [6], [7])
    # Only the port of each warning is kept: one truncation entry per port, sorted by port number.
    assert [w[0] for w in m2.warnings.interval_overflow_warnings] == ["DP1", "DP3"]
    assert [v[0] for v in m2.warnings.validation_issues] == ["DP2"]
    [b] = m2.bits
    assert (b.bit_index, b.slot, b.direction, b.device, b.dp, b.sample, b.bit) == (
        5, SlotType.DATA, DirectionType.SINK, 2, 1, 1, 0)


def test_loading_a_file_that_is_not_a_bus_model_says_why(tmp_path):
    missing = tmp_path / "missing.json"
    missing.write_text(json.dumps({"num_rows": 1, "bits": {}}), encoding="utf-8")
    with pytest.raises(ValueError, match="Missing required field: num_columns"):
        JSONHandler.load_bus_model(str(missing))
    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError, match="Invalid bus model JSON"):
        JSONHandler.load_bus_model(str(broken))
    with pytest.raises(OSError):
        JSONHandler.load_bus_model(str(tmp_path / "absent.json"))
    with pytest.raises(OSError):
        JSONHandler.save_bus_model(str(tmp_path / "no" / "dir.json"), BusModel(1, 4, 3072.0))


_GOOD = {"num_rows": 1, "num_columns": 4, "bits": {}}
_SLOT = {"slot": "DATA", "direction": "SOURCE", "device": 0, "dp": 0, "sample": 0, "bit": 0,
         "channel": 0}


@pytest.mark.parametrize("content,message", [
    ({**_GOOD, "bits": []}, "bits must be an object"),
    ({**_GOOD, "num_columns": "16"}, "num_columns must be a positive integer"),
    ({**_GOOD, "num_columns": 0}, "num_columns must be a positive integer"),
    ({**_GOOD, "num_rows": 0}, "num_rows must be a positive integer"),
    ({**_GOOD, "num_rows": True}, "num_rows must be a positive integer"),
    ({**_GOOD, "bits": {"0": {"slots": [5]}}}, "each slot must be an object"),
    ({**_GOOD, "bits": {"0": {"slots": 5}}}, "slots must be a list"),
    ({**_GOOD, "bits": {"0": []}}, "slots must be a list"),
    ({**_GOOD, "bits": {"9": {"slots": []}}}, "outside the 4-bit frame"),
    ({**_GOOD, "bits": {"-1": {"slots": []}}}, "outside the 4-bit frame"),
    ({**_GOOD, "bus_clashes": None}, "bus_clashes must be a list"),
    ({**_GOOD, "device_clashes": "1"}, "device_clashes must be a list"),
    ({**_GOOD, "read_overlaps": [9]}, "read_overlaps holds 9"),
    ({**_GOOD, "bus_clashes": [True]}, "bus_clashes holds True"),
    ({**_GOOD, "row_rate": "fast"}, "row_rate must be a finite positive number"),
    ({**_GOOD, "row_rate": float("nan")}, "row_rate must be a finite positive number"),
    ({**_GOOD, "row_rate": 0}, "row_rate must be a finite positive number"),
    ([1, 2], "Top-level JSON value must be an object"),
    ({**_GOOD, "bits": {"0": {"slots": [{**_SLOT, "device": "x"}]}}}, "bit 0: device must be an integer"),
    ({**_GOOD, "bits": {"0": {"slots": [{k: v for k, v in _SLOT.items() if k != "device"}]}}},
     "bit 0: device is missing"),
    ({**_GOOD, "bits": {"0": {"slots": [{**_SLOT, "dp": "q"}]}}}, "bit 0: dp must be an integer or null"),
    ({**_GOOD, "bits": {"0": {"slots": [{**_SLOT, "sample": "s"}]}}}, "bit 0: sample must be an integer or null"),
    ({**_GOOD, "bits": {"0": {"slots": [{**_SLOT, "bit": 1.5}]}}}, "bit 0: bit must be an integer or null"),
    ({**_GOOD, "bits": {"0": {"slots": [{**_SLOT, "channel": None}]}}}, "bit 0: channel must be an integer"),
    ({**_GOOD, "bits": {"0": {"slots": [{**_SLOT, "device": True}]}}}, "bit 0: device must be an integer"),
    ({**_GOOD, "bits": {"0": {"slots": [{**_SLOT, "slot": ["x"]}]}}}, "bit 0: slot .* is not a SlotType name"),
    ({**_GOOD, "bits": {"0": {"slots": [{**_SLOT, "direction": "UP"}]}}}, "bit 0: direction 'UP' is not a DirectionType"),
    ({**_GOOD, "bits": {"0": {"slots": [{**_SLOT, "clash": 3}]}}}, "bit 0: clash 3 is not a ClashType name"),
    ({**_GOOD, "warnings": []}, "warnings must be an object"),
    ({**_GOOD, "warnings": {"truncation": 3}}, "warnings.truncation must be a list"),
    ({**_GOOD, "warnings": {"truncation": ["a", None]}}, "warnings.truncation holds 'a'"),
    ({**_GOOD, "warnings": {"validation": [-1]}}, "warnings.validation holds -1"),
    ({**_GOOD, "warnings": {"validation": [True]}}, "warnings.validation holds True"),
])
def test_a_structurally_wrong_bus_model_is_a_value_error(tmp_path, content, message):
    p = tmp_path / "bad.json"
    p.write_text(json.dumps(content), encoding="utf-8")
    with pytest.raises(ValueError, match=message):
        JSONHandler.load_bus_model(str(p))


def test_loading_places_bits_without_the_per_bit_clash_scan(tmp_path, monkeypatch):
    """The clash lists are assigned after the bits, so each bit is added with clash tracking
    off (the membership scan per clashing bit is skipped) and a list the file holds is the
    file's, whatever the slots' flags say."""
    seen = []
    real = BusModel.add_bit

    def spy(self, bit, track_clashes=True):
        seen.append(track_clashes)
        real(self, bit, track_clashes=track_clashes)

    monkeypatch.setattr(BusModel, "add_bit", spy)
    content = {**_GOOD, "bits": {"1": {"slots": [{**_SLOT, "clash": "SAME_DEVICE"}]}},
               "bus_clashes": [2], "device_clashes": []}
    p = tmp_path / "m.json"
    p.write_text(json.dumps(content), encoding="utf-8")
    m = JSONHandler.load_bus_model(str(p))
    assert seen == [False]
    assert m.bus_clashes == [2] and m.device_clashes == []


@pytest.mark.parametrize("text,message", [
    ('{"num_rows": 1, "num_columns": 4, "bits": {}, "row_rate": 1' + "0" * 400 + '}',
     "row_rate must be a finite positive number"),
    ("[" * 100_000, "Invalid bus model JSON"),
], ids=["huge_row_rate", "deep_nesting"])
def test_a_number_beyond_float_range_or_a_deeply_nested_file_is_a_value_error(tmp_path, text,
                                                                              message):
    p = tmp_path / "extreme.json"
    p.write_text(text, encoding="utf-8")
    with pytest.raises(ValueError, match=message):
        JSONHandler.load_bus_model(str(p))


@pytest.mark.parametrize("rows,columns", [(0, 4), (2, 0), (0, 0)])
def test_a_model_the_loader_would_refuse_is_not_saved(tmp_path, rows, columns):
    """A model with no rows or no columns raises ValueError and leaves no file behind."""
    p = tmp_path / "empty.json"
    with pytest.raises(ValueError, match="at least one row and one column"):
        JSONHandler.save_bus_model(str(p), BusModel(rows, columns, 3072.0))
    assert not p.exists()


def test_saving_a_model_with_a_deferred_removal_omits_the_removed_bit(tmp_path):
    m = BusModel(num_rows=1, num_columns=4, row_rate=3072.0)
    m.add_bit(BitInfo(bit_index=0, slot=SlotType.GUARD_0, direction=DirectionType.SOURCE,
                      device=0, dp=0))
    m.add_bit(BitInfo(bit_index=0, slot=SlotType.DATA, direction=DirectionType.SOURCE,
                      device=0, dp=0, channel=0, sample=0, bit=0))
    assert m.remove_bits_matching(0, 0, SlotType.GUARD_0) == 1
    p = str(tmp_path / "model.json")
    JSONHandler.save_bus_model(p, m)
    saved = JSONHandler.load_bus_model(p)
    assert [b.slot for b in saved.bits] == [SlotType.DATA]


@pytest.mark.platform     # os.replace and the temporary file behave per OS
def test_a_failed_save_leaves_the_existing_file_and_no_temporary_file(tmp_path, monkeypatch):
    """The model is serialized before the target is touched, and the file is moved into place
    whole: a model that cannot be serialized or a write that fails leaves the old file as it
    was, with nothing else in the folder."""
    p = tmp_path / "model.json"
    JSONHandler.save_bus_model(str(p), BusModel(1, 4, 3072.0))
    old = p.read_bytes()

    bad = BusModel(1, 4, 3072.0)
    bad.row_rate = object()                                  # json cannot serialize it
    with pytest.raises(TypeError):
        JSONHandler.save_bus_model(str(p), bad)
    assert p.read_bytes() == old and os.listdir(tmp_path) == ["model.json"]

    def fail(src, dst):
        raise OSError("disk full")
    monkeypatch.setattr(os, "replace", fail)
    with pytest.raises(OSError, match="disk full"):
        JSONHandler.save_bus_model(str(p), BusModel(2, 4, 3072.0))
    assert p.read_bytes() == old and os.listdir(tmp_path) == ["model.json"]


@pytest.mark.platform
def test_a_save_replaces_an_existing_file(tmp_path):
    p = tmp_path / "model.json"
    JSONHandler.save_bus_model(str(p), BusModel(1, 4, 3072.0))
    JSONHandler.save_bus_model(str(p), BusModel(2, 4, 3072.0))
    assert JSONHandler.load_bus_model(str(p)).num_rows == 2
    assert os.listdir(tmp_path) == ["model.json"]


@pytest.mark.platform
@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_a_save_keeps_the_mode_of_the_file_it_replaces(tmp_path):
    p = tmp_path / "model.json"
    JSONHandler.save_bus_model(str(p), BusModel(1, 4, 3072.0))
    os.chmod(p, 0o600)
    JSONHandler.save_bus_model(str(p), BusModel(2, 4, 3072.0))
    assert JSONHandler.load_bus_model(str(p)).num_rows == 2
    assert (p.stat().st_mode & 0o777) == 0o600


def _flagged_file(tmp_path, **lists):
    slots = [{"slot": "DATA", "direction": "SOURCE", "device": 0, "dp": 0, "channel": 0,
              "sample": 0, "bit": 0, "clash": clash}
             for clash in ("DIFFERENT_DEVICE", "SAME_DEVICE")]
    data = {"num_rows": 1, "num_columns": 4, "bits": {
        "1": {"slots": [slots[0]]}, "2": {"slots": [slots[1], slots[1]]}}, **lists}
    p = tmp_path / "flagged.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    return str(p)


def test_a_clash_list_the_file_omits_is_derived_from_the_slots_clash_flags(tmp_path):
    m = JSONHandler.load_bus_model(_flagged_file(tmp_path))
    assert (m.bus_clashes, m.device_clashes, m.read_overlaps) == ([1], [2], [])


def test_a_clash_list_the_file_holds_is_read_as_written(tmp_path):
    m = JSONHandler.load_bus_model(_flagged_file(tmp_path, bus_clashes=[3], device_clashes=[]))
    assert (m.bus_clashes, m.device_clashes) == ([3], [])
    m = JSONHandler.load_bus_model(_flagged_file(tmp_path, bus_clashes=[0]))
    assert (m.bus_clashes, m.device_clashes) == ([0], [2])
