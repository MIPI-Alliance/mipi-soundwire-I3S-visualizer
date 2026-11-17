"""A CRC-valid SSPA, or confirmed SSCR or DSCR, whose Row_Delay is outside 14..15 (Section
9.1.13) names no SSP row: the decode gives it no effect and flags it as a programming error
(CommandRec::rowDelayError, analysis.errors.command_error).

The streaming decode schedules no SSP for it, so a DSCR that would enable a port commits
nothing and the port carries no audio; the register replay behind the Bus Grid, the
as-of-cursor config and the Registers pane does not commit it either. A Row_Delay of 14 or
15 is not flagged.

Every input is synthetic: make_dscr_enable_levels (dp0 runs from an SSCR; a DSCR mid-stream
enables dp1) with the DSCR's or the SSCR's Row_Delay byte as given, and make_skipping_levels
(a port with periodic SSPAs) with the SSPAs' Row_Delay byte as given.
"""

import pytest
import swi3score

from swi3s_studio.analysis.errors import command_error
from swi3s_studio.session import Session

N = 32


def _decode(row_delay):
    levels = swi3score.make_dscr_enable_levels(N, dscr_row_delay=row_delay)
    d = swi3score.Decoder(swi3score.MemorySampleSource(levels, 98_304_000, 4),
                          swi3score.DecoderSettings())
    d.run()
    return d


def _commits(d):
    return [c for c in d.commands() if c["is_commit"] and c["crc_valid"]]


@pytest.mark.parametrize("row_delay", [0, 13, 16, 255])
def test_a_dscr_with_a_row_delay_outside_14_15_is_flagged_and_has_no_effect(row_delay):
    d = _decode(row_delay)
    sscr, dscr = _commits(d)
    assert dscr["commit_confirmed"] and dscr["row_delay"] == row_delay
    assert dscr["row_delay_error"]
    assert dscr["effective_row"] == -1
    assert command_error(dscr) == (f"Programming error: Row_Delay {row_delay} is outside "
                                   "14..15; the command had no effect")
    assert not sscr["row_delay_error"]
    assert not any(a["dp"] == 1 for a in d.audio())
    cfg = swi3score.config_from_commands(Session._grid_replay(d.commands()))
    assert [p["dp_number"] for p in cfg["dataports"] if p["enabled"]] == [0]
    regs = [r for c in d.commands() if (r := Session._reg_replay_entry(c)) is not None]
    assert [r["commit_confirmed"] for r in regs if r.get("is_commit")] == [True, False]


@pytest.mark.parametrize("row_delay", [14, 15])
def test_a_dscr_with_a_row_delay_of_14_or_15_is_not_flagged(row_delay):
    d = _decode(row_delay)
    assert not any(c["row_delay_error"] for c in d.commands())
    _sscr, dscr = _commits(d)
    assert dscr["effective_row"] >= 0
    assert command_error(dscr) is None
    assert any(a["dp"] == 1 for a in d.audio())
    cfg = swi3score.config_from_commands(Session._grid_replay(d.commands()))
    assert sorted(p["dp_number"] for p in cfg["dataports"] if p["enabled"]) == [0, 1]


@pytest.mark.parametrize("row_delay", [0, 13, 16, 255])
def test_an_sscr_with_a_row_delay_outside_14_15_is_flagged_and_starts_nothing(row_delay):
    levels = swi3score.make_dscr_enable_levels(N, sscr_row_delay=row_delay)
    d = swi3score.Decoder(swi3score.MemorySampleSource(levels, 98_304_000, 4),
                          swi3score.DecoderSettings())
    d.run()
    sscr = next(c for c in d.commands() if c["command"] == "SSCR" and c["crc_valid"])
    assert sscr["commit_confirmed"] and sscr["row_delay"] == row_delay
    assert sscr["row_delay_error"]
    assert sscr["effective_row"] == -1
    assert command_error(sscr) == (f"Programming error: Row_Delay {row_delay} is outside "
                                   "14..15; the command had no effect")
    assert d.audio() == []


def _ramp_unbroken(d):
    values = [a["value"] for a in sorted(d.audio(), key=lambda a: a["start_sample"])]
    return bool(values) and values == list(range(len(values)))


@pytest.mark.parametrize("row_delay", [0, 13, 16, 255])
def test_an_sspa_with_a_row_delay_outside_14_15_is_flagged_and_does_not_re_anchor(row_delay):
    """The SSPAs sit one Interval off the skip pattern, where one that re-anchored would be
    an unexpected SSP and break the ramp; with its Row_Delay rejected neither happens."""
    levels = swi3score.make_skipping_levels(400, misaligned_sspa=True, sspa_row_delay=row_delay)
    d = swi3score.Decoder(swi3score.MemorySampleSource(levels, 98_304_000, 4),
                          swi3score.DecoderSettings())
    d.run()
    sspas = [c for c in d.commands() if c["command"] == "SSPA" and c["crc_valid"]]
    assert sspas
    for sspa in sspas:
        assert sspa["row_delay"] == row_delay and sspa["row_delay_error"]
        assert not sspa["unexpected_ssp_error"]
        assert command_error(sspa) == (f"Programming error: Row_Delay {row_delay} is "
                                       "outside 14..15; the command had no effect")
    assert _ramp_unbroken(d)


@pytest.mark.parametrize("row_delay", [14, 15])
def test_an_sscr_or_sspa_with_a_row_delay_of_14_or_15_is_not_flagged(row_delay):
    d = swi3score.Decoder(swi3score.MemorySampleSource(
        swi3score.make_dscr_enable_levels(N, sscr_row_delay=row_delay), 98_304_000, 4),
        swi3score.DecoderSettings())
    d.run()
    sscr = next(c for c in d.commands() if c["command"] == "SSCR")
    assert not sscr["row_delay_error"] and sscr["effective_row"] >= 0
    assert any(a["dp"] == 0 for a in d.audio())
    d = swi3score.Decoder(swi3score.MemorySampleSource(
        swi3score.make_skipping_levels(400, misaligned_sspa=True, sspa_row_delay=row_delay),
        98_304_000, 4), swi3score.DecoderSettings())
    d.run()
    sspas = [c for c in d.commands() if c["command"] == "SSPA"]
    assert sspas and not any(c["row_delay_error"] for c in sspas)
    assert any(c["unexpected_ssp_error"] for c in sspas) and not _ramp_unbroken(d)


def test_a_row_delay_that_is_not_a_byte_raises():
    with pytest.raises(ValueError, match="dscr_row_delay"):
        swi3score.make_dscr_enable_levels(N, dscr_row_delay=256)
    with pytest.raises(ValueError, match="dscr_row_delay"):
        swi3score.make_dscr_enable_levels(N, dscr_row_delay=-1)
    with pytest.raises(ValueError, match="sscr_row_delay"):
        swi3score.make_dscr_enable_levels(N, sscr_row_delay=256)
    with pytest.raises(ValueError, match="sspa_row_delay"):
        swi3score.make_skipping_levels(200, sspa_row_delay=-1)


# ---- a rejected Row_Delay takes no effect in the Session's views either ----------------

def _rejected(index_of_commit=None):
    """A cold-start demo Session whose confirmed SSCRs carry row_delay_error, as the decode
    marks one whose Row_Delay is outside 14..15 (effective_row -1); all of them, or only the
    `index_of_commit`-th."""
    s = Session.from_demo(300, cold_start=True, phy=2)
    sscrs = [c for c in s.commands if c.get("is_commit") and c.get("has_sync_point")
             and c.get("commit_confirmed")]
    assert len(sscrs) >= 2
    for i, c in enumerate(sscrs):
        if index_of_commit is None or i == index_of_commit:
            c["row_delay_error"], c["effective_row"], c["row_delay"] = True, -1, 0
    s._rel_cmds = s._commit_rsp_cache = None
    return s, sscrs


def test_a_rejected_commit_has_no_commit_point_marker_or_row():
    s, sscrs = _rejected(1)
    kept = Session.from_demo(300, cold_start=True, phy=2)
    assert len(kept.commit_point_samples()) == len(sscrs)
    assert s.commit_point_samples() == kept.commit_point_samples()[:1]
    assert [r["start_sample"] for r in s.commit_point_rows()] == s.commit_point_samples()
    assert len(s._commit_rsp_samples_all()) == len(kept._commit_rsp_samples_all()) - 1


def test_a_rejected_commit_does_not_supply_the_ports_ssp():
    s, _ = _rejected()
    assert Session.from_demo(300, cold_start=True, phy=2)._wire_supplies_an_ssp()
    assert not s._wire_supplies_an_ssp()


def test_an_sspa_with_a_rejected_row_delay_gives_no_ssp_row():
    s = Session.from_demo(300, cold_start=True, phy=2)
    end = int(s.commands[-1]["end_sample"])
    sspa = {"command": "SSPA", "has_sync_point": True, "is_commit": False,
            "crc_valid": True, "start_sample": end - 100, "end_sample": end, "row_delay": 0}
    s.commands = s.commands + [sspa]
    assert s.sspa_ssp_row() == s.bus_row_for_sample(end) + 1
    sspa["row_delay_error"] = True
    assert s.sspa_ssp_row() is None


def test_the_register_oracle_and_history_do_not_commit_a_rejected_commit():
    import copy

    from swi3s_studio.analysis.register_history import _curr_alias, register_history
    from swi3s_studio.model import apply_commands
    s, sscrs = _rejected()
    unconfirmed = copy.deepcopy(s.commands)
    for c in unconfirmed:
        if c.get("row_delay_error"):
            c["commit_confirmed"] = False

    def state(files):
        return {d: (sorted(f.items()), {a: c.value for a, c in f._curr.items()})
                for d, f in files.items()}
    kept = Session.from_demo(300, cold_start=True, phy=2)
    assert state(apply_commands(s.register_map, s.commands)) == state(
        apply_commands(s.register_map, unconfirmed))
    assert state(apply_commands(s.register_map, s.commands)) != state(
        apply_commands(s.register_map, kept.commands))
    index = s.commands.index(sscrs[0])
    curr = {}                                          # (device, _CURR address) written
    for c in s.commands[:index]:
        if c.get("command") == "WriteA32" and c.get("has_address"):
            for i in range(len(c.get("data") or b"")):
                alias = _curr_alias(s.register_map, int(c["address"]) + i)
                for d in range(12):
                    if alias is not None and int(c.get("device_mask", 0)) >> d & 1:
                        curr[(d, alias)] = True
    writes = set(curr)
    commits = [(e, d, a) for d, a in sorted(writes)
               for e in register_history(kept.commands, d, a, kept.register_map)
               if e.access == "Commit" and e.command_index == index]
    assert commits and all(e.applied for e, _d, _a in commits)
    for _e, d, a in commits:
        for e in register_history(s.commands, d, a, s.register_map):
            if e.command_index == index:
                assert not e.applied
