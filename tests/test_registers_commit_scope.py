"""Which devices a Commit promotes, and what a bus reset clears, agree across the register
views: the Registers pane (Session, over the C++ register authority), Register History
(analysis.register_history) and the Python model (model.registers.apply_commands).

A Commit promotes the devices its device mask selects, none when the mask is 0 (Section
8.1.1.5.1, "Commit Phase for SSCR and DSCR Commands" table). A WriteA32 is applied only when
it selects exactly one device (Section 8.1.2.2). A write to a dual-ranked _CURR alias sets
_NEXT too (Dual-Curr-is-RW, "Register Access Type" table).
SLC NumColumns: _NEXT 0x1081, _CURR 0x10C1, Commit Group 0.
"""
import pytest
from test_link_restart import _GAP_S, _cold, _relinked
from test_link_restart import _session as _test_session
from test_register_history import _commit, _read, _write

from swi3s_studio.analysis.register_history import register_history
from swi3s_studio.model.registers import RegisterMap, apply_commands
from swi3s_studio.session import Session

NEXT, CURR = 0x1081, 0x10C1


def _pane(cmds):
    """The Registers pane's per-device files for `cmds` (whole capture, no what-ifs)."""
    s = Session.from_demo(32)
    return s._build_register_files(cmds, None)


def _commits_in(events):
    return [(e.command_index, e.applied) for e in events if e.access == "Commit"]


def test_a_commit_promotes_only_the_devices_its_mask_selects_in_every_view():
    cmds = [_write(NEXT, [3], mask=0b01, sample=1),
            _write(NEXT, [5], mask=0b10, sample=2),
            _commit(0b0001, mask=0b10, sample=3)]
    pane = _pane(cmds)
    model = apply_commands(RegisterMap.load(), cmds)
    assert pane[0].curr_value(NEXT) == model[0].curr_value(NEXT) == 0
    assert pane[1].curr_value(NEXT) == model[1].curr_value(NEXT) == 5
    assert pane[0].value(NEXT) == 3                       # staged, not promoted
    assert _commits_in(register_history(cmds, 0, CURR)) == []
    assert _commits_in(register_history(cmds, 1, CURR)) == [(2, True)]


def test_the_register_model_takes_unicast_writes_and_multicast_reads():
    """The C++ model takes a WriteA32 only when it selects exactly one device, and a read
    on every device it selects. The Python model and History do the same."""
    cmds = [_write(NEXT, [7], mask=0b11, sample=1),            # multicast write: ignored
            _write(NEXT, [3], mask=0b01, sample=2),
            _read(0x1010, [0x5A], mask=0b11, sample=3),
            _commit(0b0001, mask=0b01, sample=4)]
    pane = _pane(cmds)
    model = apply_commands(RegisterMap.load(), cmds)
    assert pane[0].curr_value(NEXT) == model[0].curr_value(NEXT) == 3
    assert pane[1].curr_value(NEXT) == model[1].curr_value(NEXT) == 0
    assert pane[1].value(NEXT) == model[1].value(NEXT) == 0
    assert pane[0].value(0x1010) == pane[1].value(0x1010) == 0x5A
    assert model[0].value(0x1010) == model[1].value(0x1010) == 0x5A
    ev = register_history(cmds, 0, CURR)
    assert [(e.access, e.value, e.applied, e.status) for e in ev] == [
        ("Write (NEXT)", 7, False, "device mask: expected 1, got 2"),
        ("Write (NEXT)", 3, True, "OK"),
        ("Commit", 3, True, "OK")]


def test_a_multicast_write_alone_gives_a_commit_nothing_to_promote():
    cmds = [_write(NEXT, [7], mask=0b11, sample=1), _commit(0b0001, mask=0b01, sample=2)]
    pane = _pane(cmds)
    model = apply_commands(RegisterMap.load(), cmds)
    assert 0 not in pane and model[0].curr_value(NEXT) == 0
    assert [(e.access, e.value, e.applied) for e in register_history(cmds, 0, CURR)] == [
        ("Write (NEXT)", 7, False), ("Commit", None, False)]


def test_a_write_to_a_curr_alias_sets_next_too_in_every_view():
    """A Commit after a direct _CURR write promotes the written value, and the _NEXT row's
    history lists the _CURR write that set it."""
    cmds = [_write(NEXT, [3], sample=1), _write(CURR, [9], sample=2),
            _commit(0b0001, sample=3)]
    pane = _pane(cmds)
    model = apply_commands(RegisterMap.load(), cmds)
    before = apply_commands(RegisterMap.load(), cmds[:2])
    assert (pane[0].value(NEXT), pane[0].curr_value(NEXT)) == (9, 9)
    assert (model[0].value(NEXT), model[0].curr_value(NEXT)) == (9, 9)
    assert (before[0].value(NEXT), before[0].curr_value(NEXT)) == (9, 9)
    assert sorted(a for a, _v in model[0].items()) == sorted(a for a, _v in pane[0].items())
    assert [(e.access, e.value, e.applied) for e in register_history(cmds, 0, CURR)] == [
        ("Write (NEXT)", 3, True), ("Write", 9, True), ("Commit", 9, True)]
    assert [(e.access, e.value) for e in register_history(cmds, 0, NEXT)] == [
        ("Write", 3), ("Write (CURR)", 9)]


def test_a_read_of_a_curr_alias_is_not_listed_on_its_next_row():
    cmds = [_write(NEXT, [3], sample=1), _read(CURR, [0], sample=2)]
    assert [e.access for e in register_history(cmds, 0, NEXT)] == ["Write"]


def test_a_commit_with_no_device_mask_promotes_no_device():
    cmds = [_write(NEXT, [3], mask=0b01, sample=1),
            _write(NEXT, [5], mask=0b10, sample=2),
            _commit(0b0001, mask=0, sample=3)]
    pane = _pane(cmds)
    model = apply_commands(RegisterMap.load(), cmds)
    assert (pane[0].curr_value(NEXT), pane[1].curr_value(NEXT)) == (0, 0)
    assert (model[0].curr_value(NEXT), model[1].curr_value(NEXT)) == (0, 0)
    assert _commits_in(register_history(cmds, 0, CURR)) == []
    assert _commits_in(register_history(cmds, 1, CURR)) == []


def test_what_ifs_stay_on_their_device_when_a_commit_leaves_it_out():
    replay = [Session._reg_replay_entry(c) for c in (
        _write(NEXT, [3], mask=0b01), _write(NEXT, [5], mask=0b10),
        _commit(0b0001, mask=0b10))]
    snap = Session._registers_snapshot(replay, [(0, 0x1082, 0x2A)])
    by_key = {(d, a): (cur, nxt) for d, a, cur, _hc, _cs, nxt, _hn, _ns in snap}
    assert by_key[(0, 0x1082)][1] == 0x2A and (1, 0x1082) not in by_key
    assert by_key[(0, NEXT)] == (0, 3) and by_key[(1, NEXT)][0] == 5


# ---- a bus reset ----
#
# DP1 BitWidth_HorizontalStart: _NEXT 0x2180, _CURR 0x21C0. DP1 CommitGroupMemb (0x210A)
# resets to Commit Group 0.

def test_a_bus_reset_returns_commit_group_membership_to_its_reset():
    cmds = [_write(0x210A, [0b0010], sample=10),
            _write(0x2180, [7], sample=200),
            _commit(0b0001, sample=210)]
    assert _commits_in(register_history(cmds, 0, 0x21C0)) == []
    assert _commits_in(register_history(cmds, 0, 0x21C0, resets=[100])) == [(2, True)]


def test_a_bus_reset_drops_what_was_staged_before_it():
    cmds = [_write(0x2180, [7], sample=10), _commit(0b0001, sample=200)]
    ev = register_history(cmds, 0, 0x21C0, resets=[100])
    assert [(e.access, e.value, e.applied) for e in ev] == [
        ("Write (NEXT)", 7, True), ("Commit", None, False)]


def test_a_bus_reset_drops_the_pending_deferred_read():
    cmds = [_read(0x30, pr=5, sample=10),
            {**_read(0x30, pr=5, sample=200), "command": "ReadData", "phase": "ReadData",
             "has_address": False, "peripheral_response": 4}]
    assert [e.command_index for e in register_history(cmds, 0, 0x30)] == [0, 1]
    assert [e.command_index for e in register_history(cmds, 0, 0x30, resets=[100])] == [0]


@pytest.fixture(scope="module")
def relinked():
    """(a session of two Cold Start demos joined with the clock stopped between them, the
    sample the second Cold Start's Bus Reset begins)."""
    a, b = _cold(), _cold()
    cap, _shift = _relinked(a, b, int(_GAP_S * a.sample_rate_hz))
    s = _test_session(cap)
    later = s.link_controls[1]
    return s, next(int(sec["start"]) for sec in later.sections if sec["name"] == "Bus Reset")


def test_the_session_lists_the_bus_reset_of_its_later_cold_start(relinked):
    s, reset = relinked
    assert s.bus_reset_samples() == [reset]


def _numcolumns_writes(s):
    return [c for c in s.commands if c.get("command") == "WriteA32" and c.get("crc_valid")
            and int(c.get("address", -1)) == NEXT and int(c.get("device_mask", 0)) & 1]


def _first_commit_after(events, sample):
    return next((e.value, e.applied) for e in events
                if e.access == "Commit" and e.start_sample >= sample)


def test_the_history_window_starts_again_at_the_bus_reset(relinked):
    """NumColumns (CURR) on device 0, with the second half's first NumColumns write
    removed: the first Commit after the reset has nothing staged since it, while the first
    half's last staged value is still in _NEXT were the reset ignored. Both the window's
    first read and its re-read after a re-decode start again at the reset."""
    from PySide6.QtWidgets import QApplication

    from swi3s_studio.ui.main_window import MainWindow
    s, reset = relinked
    full = list(s.commands)
    before = [c for c in _numcolumns_writes(s) if int(c["start_sample"]) < reset]
    after = [c for c in _numcolumns_writes(s) if int(c["start_sample"]) >= reset]
    assert before and after, "the demo writes NumColumns in both halves"
    cut = [c for c in full if c is not after[0]]
    assert _first_commit_after(register_history(cut, 0, CURR), reset) == (
        before[-1]["data"][0], True), "with no reset the stale value would be promoted"
    QApplication.instance() or QApplication([])
    win = MainWindow()
    try:
        win.load_session(s)
        dlg = win.show_register_history(0, CURR, "NumColumns (CURR)")
        assert _first_commit_after(dlg._events, reset) == (after[0]["data"][0], True)
        s.commands = cut
        win.load_session(s)                                 # the re-decode path
        assert _first_commit_after(dlg._events, reset) == (None, False)
        fresh = win.show_register_history(0, CURR, "NumColumns (CURR)")
        assert _first_commit_after(fresh._events, reset) == (None, False)
    finally:
        s.commands = full
        for d in win._history_dialogs():
            d.close()
