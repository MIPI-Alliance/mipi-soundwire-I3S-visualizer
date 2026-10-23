"""A register's history: every write and read of it, with the attempts that did not land.

Synthetic commands, shaped as the decoder emits them, one failure kind each: a CRC-failed
packet, a peripheral that answered WRITE_FAILED, one that did not answer, a read whose data
failed its CRC, a deferred read delivered by a later ReadData, and a deferred read whose
ReadData failed or got no answer. A dual-ranked register's _CURR alias lists its _NEXT
accesses and the Commits that promote them.
"""
from swi3s_studio.analysis.register_history import register_history
from swi3s_studio.session import Session


def _write(addr, data, *, mask=0b1, crc=True, pr=0, sample=0, row=0):
    return {"command": "WriteA32", "phase": "Write", "has_address": True, "address": addr,
            "data": bytes(data), "device_mask": mask, "has_manager_packet": True,
            "crc_valid": crc, "peripheral_response": pr, "manager_response": -1,
            "has_read_data": False, "read_data": b"", "read_data_crc_valid": False,
            "start_sample": sample, "bus_row": row}


def _read(addr, data=b"", *, op="ReadA32", data_crc=True, pr=0, sample=0, row=0, mask=0b1,
          has_address=True):
    return {"command": op, "phase": "ReadSetup" if op == "ReadA32" else "ReadData",
            "has_address": has_address, "address": addr if has_address else 0, "data": b"",
            "device_mask": mask,
            "has_manager_packet": op == "ReadA32", "crc_valid": op == "ReadA32",
            "peripheral_response": pr, "manager_response": 0 if data else -1,
            "has_read_data": bool(data), "read_data": bytes(data),
            "read_data_crc_valid": data_crc, "start_sample": sample, "bus_row": row}


def test_writes_and_reads_of_the_byte_in_capture_order():
    cmds = [_write(0x10, [0x11, 0x22], sample=10, row=1),       # covers 0x10 and 0x11
            _write(0x12, [0x33], sample=20, row=2),               # not 0x11
            _read(0x11, [0x22], sample=30, row=3),
            _write(0x11, [0x44], mask=0b10, sample=40, row=4)]    # device 1, not device 0
    ev = register_history(cmds, 0, 0x11)
    assert [(e.access, e.value, e.bus_row, e.status) for e in ev] == [
        ("Write", 0x22, 1, "OK"), ("Read", 0x22, 3, "OK")]
    assert all(e.applied for e in ev) and [e.command_index for e in ev] == [0, 2]


def test_attempts_that_did_not_land_are_listed_and_say_why():
    no_answer = _write(0x20, [3], pr=-1)
    cmds = [_write(0x20, [1], crc=False), _write(0x20, [2], pr=4), no_answer,
            _read(0x20, [9], data_crc=False), _read(0x20, pr=4)]
    ev = register_history(cmds, 0, 0x20)
    status = [e.status for e in ev]
    assert status[0].startswith("CRC error") and not ev[0].applied
    # The bus carried the value, so the model took it; the device said it did not.
    assert status[1] == "WRITE_FAILED" and ev[1].applied and ev[1].value == 2
    assert status[2] == "no response"
    assert "read data CRC error" in status[3] and not ev[3].applied
    assert status[4] == "READ_FAILED" and ev[4].value is None and not ev[4].applied
    assert all(e.failed for e in ev)


def test_a_deferred_read_is_its_setup_then_its_delivery():
    cmds = [_read(0x30, pr=5, sample=1), _read(0x30, [0x7E], op="ReadData", sample=2)]
    ev = register_history(cmds, 0, 0x30)
    assert [(e.access, e.value, e.status) for e in ev] == [
        ("Read", None, "OK"), ("Read (deferred delivery)", 0x7E, "OK")]


def test_a_deferred_read_that_fails_or_gets_no_answer_is_listed_at_its_setup_address():
    """The decoder gives a ReadData an address only when data follows. One answered
    READ_FAILED, or not at all, belongs to the device's pending deferred ReadA32."""
    cmds = [_read(0x30, pr=5, sample=1),                                       # deferred
            _read(0x30, op="ReadData", pr=4, has_address=False, sample=2),     # READ_FAILED
            _read(0x30, op="ReadData", pr=-1, has_address=False, sample=3),    # no answer
            _read(0x30, op="ReadData", pr=4, has_address=False, mask=0b10),    # device 1
            _read(0x30, [0x7E], op="ReadData", sample=4),                      # delivered
            _read(0x30, op="ReadData", pr=4, has_address=False, sample=5)]     # none pending
    ev = register_history(cmds, 0, 0x30)
    assert [(e.command_index, e.access, e.value, e.status, e.applied) for e in ev] == [
        (0, "Read", None, "OK", False),
        (1, "Read (deferred delivery)", None, "READ_FAILED", False),
        (2, "Read (deferred delivery)", None, "no response", False),
        (4, "Read (deferred delivery)", 0x7E, "OK", True)]
    assert register_history(cmds, 0, 0x31) == []


def _commit(group_mask, *, mask=0b1, confirmed=True, sample=0):
    return {"command": "SSCR", "phase": "Commit", "device_mask": mask, "is_commit": True,
            "commit_confirmed": confirmed, "group_mask": group_mask,
            "has_manager_packet": True, "crc_valid": True, "has_address": False,
            "address": 0, "data": b"", "peripheral_response": 0 if confirmed else 2,
            "manager_response": 0, "has_read_data": False, "read_data": b"",
            "read_data_crc_valid": False, "start_sample": sample, "bus_row": 0}


def test_a_curr_alias_lists_its_next_writes_and_the_commits_that_promote_them():
    """SLC NumColumns: _NEXT 0x1081, _CURR 0x10C1, Commit Group 0. The value reaches _CURR
    by a write to _NEXT and a Commit, neither of which addresses 0x10C1."""
    cmds = [_write(0x1081, [3], sample=1),
            _commit(0b0001, confirmed=False, sample=2),
            _commit(0b0010, sample=3),                      # another group: not listed
            _commit(0b0001, mask=0b10, sample=4),           # another device: not listed
            _commit(0b0001, sample=5),
            _commit(0b0001, mask=0, sample=6)]              # no device selected: not listed
    ev = register_history(cmds, 0, 0x10C1)
    assert [(e.command_index, e.access, e.value, e.applied, e.status) for e in ev] == [
        (0, "Write (NEXT)", 3, True, "OK"),
        (1, "Commit", 3, False, "COMMIT_NOT_READY"),
        (4, "Commit", 3, True, "OK")]
    # The _NEXT row's own history is its write alone.
    assert [e.command_index for e in register_history(cmds, 0, 0x1081)] == [0]


def test_a_data_ports_commit_group_is_its_membership_as_the_bus_left_it():
    """DP1 BitWidth_HorizontalStart: _NEXT 0x2180, _CURR 0x21C0. DP1 CommitGroupMemb
    (0x210A) resets to Commit Group 0; once written 0b0010 only a group-1 Commit
    promotes it."""
    cmds = [_write(0x2180, [7]), _commit(0b0001),
            _write(0x210A, [0b0010]), _commit(0b0001), _commit(0b0010)]
    ev = register_history(cmds, 0, 0x21C0)
    assert [(e.command_index, e.access) for e in ev] == [
        (0, "Write (NEXT)"), (1, "Commit"), (4, "Commit")]


def test_a_commit_with_nothing_staged_is_listed_but_not_applied():
    ev = register_history([_commit(0b0001)], 0, 0x10C1)
    assert [(e.access, e.value, e.applied, e.status) for e in ev] == [
        ("Commit", None, False, "OK")]


def test_the_demo_has_history_for_a_register_it_writes():
    s = Session.from_demo(300)
    w = next(c for c in s.commands if c.get("command") == "WriteA32" and c.get("crc_valid"))
    dev = (int(w["device_mask"]) & -int(w["device_mask"])).bit_length() - 1
    ev = register_history(s.commands, dev, int(w["address"]))
    assert ev and ev[0].access == "Write" and ev[0].value == w["data"][0]


# ---- the Registers pane: right-click a register ▸ History… ----

def _win():
    from PySide6.QtWidgets import QApplication

    from swi3s_studio.ui.main_window import MainWindow
    QApplication.instance() or QApplication([])
    w = MainWindow()
    w.load_demo()
    return w


def _first_write(win):
    w = next(c for c in win._session.commands
             if c.get("command") == "WriteA32" and c.get("crc_valid"))
    dev = (int(w["device_mask"]) & -int(w["device_mask"])).bit_length() - 1
    return dev, int(w["address"]), w


def test_the_registers_menu_offers_history_for_a_register_row():
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QTreeWidgetItemIterator
    win = _win()
    tree = win._reg_view._tree
    it = QTreeWidgetItemIterator(tree)
    row = None
    while it.value():
        if it.value().data(0, Qt.UserRole):
            row = it.value()
            break
        it += 1
    assert row is not None
    asked = []
    win._reg_view.historyRequested.connect(lambda d, a, label: asked.append((d, a, label)))
    menu = win._reg_view._context_menu_for(row)
    history = next(a for a in menu.actions() if a.text() == "History…")
    assert history.isEnabled()
    history.trigger()
    dev, addr = row.data(0, Qt.UserRole)
    assert asked and asked[0][:2] == (int(dev), int(addr)) and f"0x{int(addr):08X}" in asked[0][2]
    assert not next(a for a in win._reg_view._context_menu_for(None).actions()
                    if a.text() == "History…").isEnabled()


def test_a_curr_rows_history_names_its_rank_and_lists_the_commits():
    """In the demo every committed DP0 value arrives by a _NEXT write and a Commit."""
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QTreeWidgetItemIterator
    win = _win()
    tree = win._reg_view._tree
    it = QTreeWidgetItemIterator(tree)
    row = None
    while it.value():
        key = it.value().data(0, Qt.UserRole)
        if key and int(key[1]) == 0x20C0 and it.value().text(0).endswith("(CURR)"):
            row = it.value()
            break
        it += 1
    assert row is not None
    asked = []
    win._reg_view.historyRequested.connect(lambda d, a, label: asked.append((d, a, label)))
    win._reg_view._request_history(row)
    assert asked and asked[0][1] == 0x20C0 and asked[0][2].endswith("(CURR)"), asked
    dlg = win.show_register_history(*asked[0])
    try:
        accesses = [dlg._table.item(r, 2).text() for r in range(dlg._table.rowCount())]
        assert "Write (NEXT)" in accesses and "Commit" in accesses, accesses
    finally:
        dlg.close()


def test_the_history_window_lists_the_accesses_and_jumps_to_one():
    win = _win()
    dev, addr, w = _first_write(win)
    dlg = win.show_register_history(dev, addr, "label")
    try:
        t = dlg._table
        assert t.rowCount() >= 1
        assert t.item(0, 2).text() == "Write" and t.item(0, 3).text() == f"0x{w['data'][0]:02X}"
        assert t.item(0, 5).text() == "OK"
        target = win._session.command_cursor_sample(w)          # its Row Sync Point
        win.cursor.set_sample(0)
        dlg._on_double_click(0, 0)
        assert win.cursor.sample == target
    finally:
        dlg.close()
