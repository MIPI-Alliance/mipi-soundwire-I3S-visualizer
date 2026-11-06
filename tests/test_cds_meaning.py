"""CDS symbol meaning annotator test.

Verifies the per-symbol Command Transport labels (PhaseID, Device Mask, Opcode,
Address, CRC, responses) against the demo capture's Ping / WriteA32 / Commit
phases. With hub depths set, decode_symbols frames each delayed device's response token
after its gap bits, so the labels read the responses the Commands table reports, and
every Session symbol window passes the session's hub depths.

Run: python3 -m pytest tests/test_cds_meaning.py
"""
from swi3s_studio.analysis.cds_meaning import annotate
from swi3s_studio.session import Session


def _labels_for(sess, command_name):
    c = next(c for c in sess.commands if c["command"] == command_name)
    syms = sess.symbols_around(c["start_sample"])
    labels = annotate(syms)
    # The symbol window retains earlier history (so the user can scroll back), so the
    # target command's phase starts at ITS OWN comma, not index 0. Slice from that
    # comma (annotate re-syncs per comma, so each phase is labelled from its SPM).
    start = next(i for i, s in enumerate(syms)
                 if s["kind"] == 1 and int(s["row"]) == int(c["bus_row"]))
    return list(zip(syms[start:], labels[start:]))


def test_ping_header_and_status():
    sess = Session.from_demo(8)
    seq = _labels_for(sess, "Ping")
    labels = [l for _s, l in seq]
    assert labels[0] == "SPM (Start of Phase)"
    assert labels[1] == "PhaseID: GET_STATUS"
    assert [l.split(" = ")[0] for l in labels[2:5]] == ["Device Mask_H", "Device Mask_M", "Device Mask_L"]
    assert [l.split(" = ")[0] for l in labels[5:7]] == ["Packet Length_H", "Packet Length_L"]
    assert labels[7] == "Opcode: PING"
    assert any(l.startswith("Manager_CRC16_H") for l in labels)
    assert any(l.startswith("Manager_CRC16_L") for l in labels)
    assert any(l.startswith("Ping Status Dev 0:") for l in labels)
    # Data-field labels carry the decoded value (e.g. "Device Mask_H = 0x1").
    assert " = 0x" in labels[2]


def test_write_address_and_response():
    sess = Session.from_demo(8)
    labels = [l for _s, l in _labels_for(sess, "WriteA32")]
    assert labels[1] == "PhaseID: WRITE"
    assert labels[7] == "Opcode: WRITEA32"
    # The four address bytes appear in order right after the opcode (with values).
    i = labels.index("Opcode: WRITEA32")
    assert [l.split(" = ")[0] for l in labels[i + 1:i + 5]] == [
        "Address[31:24]", "Address[23:16]", "Address[15:8]", "Address[7:0]"]
    assert any(l.startswith("Write Data[0]") for l in labels)
    assert any(l == "Peripheral Response: WRITE_OK" for l in labels)


def test_idle_pattern_between_commands():
    """Between commands the Manager drives the alternating idle filler (…101010…),
    codewords 0x155/0x2AA. In the hunt/idle states that reads "Idle Pattern"; the SAME
    codeword mid-command is a legitimate MP/PM spacer and must stay "Spacer"."""
    sess = Session.from_demo(64, cold_start=True)
    syms = sess.symbols()
    labels = annotate(syms)
    idle = [i for i, l in enumerate(labels) if l == "Idle Pattern"]
    assert idle, "no between-command idle filler was labelled"
    for i in idle:
        assert (int(syms[i].get("raw", -1)) & 0x3FF) in (0x155, 0x2AA)
    # The alternating codeword also occurs as a within-command spacer — those stay "Spacer".
    spacers = [i for i, l in enumerate(labels) if l == "Spacer"]
    assert any((int(syms[i].get("raw", -1)) & 0x3FF) in (0x155, 0x2AA) for i in spacers), \
        "expected at least one 0x155/0x2AA spacer that is NOT relabelled as idle"


def test_commit_fields_and_responses():
    """A Commit's Manager Packet reads Opcode, Group Mask (byte 1), Row Delay (byte 2) and
    its CRC-16, each byte the value the core's parser reads, whether or not its codeword
    is also a Robust Token."""
    from test_native_reference_vectors import _crc16
    sess = Session.from_demo(8)
    sc = next((c for c in sess.commands if c["command"] in ("SSCR", "DSCR")), None)
    assert sc is not None, "the demo carries a commit"
    seq = _labels_for(sess, sc["command"])
    labels = [l for _s, l in seq]
    assert "PhaseID: COMMIT" in labels
    i = labels.index(f"Opcode: {sc['command']}")
    packet = [sc["opcode"], sc["group_mask"], sc["row_delay"]]
    crc = _crc16(packet)
    assert labels[i + 1:i + 5] == [f"Group Mask = 0x{sc['group_mask']:02X}",
                                   f"Row Delay = 0x{sc['row_delay']:02X}",
                                   f"Manager_CRC16_H = 0x{crc >> 8:02X}",
                                   f"Manager_CRC16_L = 0x{crc & 0xFF:02X}"]
    assert any(s["kind"] == 2 for s, _l in seq[i + 1:i + 5]), \
        "test premise: a packet byte whose codeword is also a Robust Token"
    assert any(l.startswith("Commit Resp Dev 0:") for l in labels)


def test_write_packet_bytes_are_the_parsers():
    sess = Session.from_demo(8)
    for c in [c for c in sess.commands if c["command"] == "WriteA32" and c["crc_valid"]][:6]:
        syms = sess.symbols_around(c["start_sample"])
        labels = annotate(syms)
        start = next(i for i, s in enumerate(syms)
                     if s["kind"] == 1 and int(s["row"]) == int(c["bus_row"]))
        labels = labels[start:]
        i = labels.index("Opcode: WRITEA32")
        address = c["address"].to_bytes(4, "big")
        assert labels[i + 1:i + 5] == [f"Address[{hi}:{lo}] = 0x{b:02X}" for (hi, lo), b in
                                       zip(((31, 24), (23, 16), (15, 8), (7, 0)), address)]
        assert labels[i + 5:i + 5 + len(c["data"])] == [
            f"Write Data[{k}] = 0x{b:02X}" for k, b in enumerate(c["data"])]


def test_the_load_time_symbol_window_ends_at_the_next_segment():
    """symbols() frames the first segment only: no symbol reaches past the second
    segment's start, so none is numbered on the first segment's column count beyond it."""
    sess = Session.from_demo(300, cold_start=True, phy=2)
    assert len(sess.segments) > 1
    nxt = int(sess.segments[1]["start_sample"])
    syms = sess.symbols()
    assert syms and max(int(s["start_sample"]) for s in syms) < nxt
    assert max(int(s["row"]) for s in syms) <= sess.bus_row_for_sample(nxt)


def test_response_names_come_from_the_shared_table():
    from swi3s_studio.analysis import cds_meaning, responses
    phases = {0: "GetStatus", 1: "Write", 2: "ReadSetup", 3: "ReadData", 4: "Commit",
              5: "Announce", 6: "CalibratePhy"}
    for phase, name in phases.items():
        for token in range(16):
            assert cds_meaning._peripheral_response(phase, token) == \
                responses.peripheral_response_name(name, token), (phase, token)
    assert cds_meaning._peripheral_response(0, 1) == "RESERVED"
    assert cds_meaning._peripheral_response(0, -1) == "?"
    assert cds_meaning._manager_response(4, 0) == "CONFIRM_COMMIT"
    assert cds_meaning._manager_response(3, 15) == "READ_DATA_ERROR"
    assert cds_meaning._manager_response(3, 7) == "RT7"      # unnamed token: its number


def test_the_phase_names_and_the_response_phases_are_one_table():
    from swi3s_studio.analysis import cds_meaning
    assert set(cds_meaning._PHASE_NAMES) == set(cds_meaning._RESPONSE_PHASE) == set(range(7))
    assert cds_meaning._PHASE_NAMES == {
        0: "GET_STATUS", 1: "WRITE", 2: "READ_SETUP", 3: "READ_DATA", 4: "COMMIT",
        5: "ANNOUNCE", 6: "CALIBRATE_PHY"}
    # The opcode names match the core's command names up to case and underscores.
    core = {(0, 0): "Ping", (1, 0): "WriteA32", (1, 1): "Unblock", (2, 0): "ReadA32",
            (4, 0): "SSCR", (4, 1): "DSCR", (5, 0): "SSPA", (5, 1): "ExitDormant",
            (6, 0): "InitCal", (6, 1): "TrimCal"}
    for (phase, op), name in core.items():
        assert cds_meaning._opcode_name(phase, op).replace("_", "") == name.upper()
    assert cds_meaning._opcode_name(1, 0x0B) == "Op0x0B"          # the core's reserved-opcode label


# ---- the Read grammar, from synthetic symbols
def _comma():
    return {"kind": 1, "value": -1, "raw": 0x17C}


def _rt(token, raw=0):
    return {"kind": 2, "value": token, "raw": raw}


def _byte(value):
    return {"kind": 3, "value": value, "raw": 0}


def _spacer(n=1):
    return [_byte(0) for _ in range(n)]


def _header(phase, pktlen, mask=1):
    return [_comma(), _rt(phase), _rt((mask >> 8) & 0xF), _rt((mask >> 4) & 0xF),
            _rt(mask & 0xF), _rt(pktlen >> 4), _rt(pktlen & 0xF)]


def _read_setup(count_minus_1, response):
    addr = [0x00, 0x00, 0x21, 0x23]
    return (_header(2, 6) + [_byte(0x00), _byte(count_minus_1)] + [_byte(b) for b in addr]
            + [_byte(0xAB), _byte(0xCD)] + _spacer() + [_rt(response)])


def test_a_read_setup_and_its_data_are_labelled_field_by_field():
    syms = _read_setup(3, 0)                                  # 4 bytes, READ_DATA_NOW
    syms += _spacer() + [_byte(0x10 + i) for i in range(4)] + [_byte(0xE1), _byte(0xE2)]
    syms += _spacer(2) + [_rt(0)]                             # PM spacer, then READ_DATA_OK
    labels = annotate(syms)
    assert labels[1] == "PhaseID: READ_SETUP"
    i = labels.index("Opcode: READA32")
    assert labels[i + 1] == "Read Byte Count = 0x03"
    assert labels[i + 2:i + 6] == ["Address[31:24] = 0x00", "Address[23:16] = 0x00",
                                   "Address[15:8] = 0x21", "Address[7:0] = 0x23"]
    assert labels[i + 6:i + 8] == ["Manager_CRC16_H = 0xAB", "Manager_CRC16_L = 0xCD"]
    assert labels[i + 8] == "Spacer"
    assert labels[i + 9] == "Peripheral Response: READ_DATA_NOW"
    assert labels[i + 10] == "Spacer"
    assert labels[i + 11:] == [
        "Read Data[0] = 0x10", "Read Data[1] = 0x11", "Read Data[2] = 0x12",
        "Read Data[3] = 0x13", "Peripheral_CRC16_H = 0xE1", "Peripheral_CRC16_L = 0xE2",
        "Spacer", "Spacer", "Manager Response: READ_DATA_OK"]


def test_a_deferred_read_is_delivered_by_a_zero_length_read_data_phase():
    syms = _read_setup(1, 5)                                  # REMOTE_READ_DEFERRED: no data yet
    syms += _header(3, 0) + _spacer() + [_rt(0)]              # the ReadData phase: delivered now
    syms += _spacer() + [_byte(0x44), _byte(0x55), _byte(0x01), _byte(0x02)]
    labels = annotate(syms)
    assert "Peripheral Response: REMOTE_READ_DEFERRED" in labels
    j = labels.index("PhaseID: READ_DATA")
    assert labels[j + 6:] == [
        "Spacer", "Peripheral Response: READ_DATA_NOW", "Spacer", "Read Data[0] = 0x44",
        "Read Data[1] = 0x55", "Peripheral_CRC16_H = 0x01", "Peripheral_CRC16_L = 0x02"]


def test_a_read_setup_answered_with_no_data_ends_after_the_response():
    labels = annotate(_read_setup(3, 4) + _spacer(3))         # READ_FAILED
    assert labels[-4] == "Peripheral Response: READ_FAILED"
    assert labels[-3:] == ["", "", ""]


def test_a_ping_labels_twelve_statuses_and_then_idles():
    syms = (_header(0, 1, mask=0xFFF) + [_byte(0x00), _byte(0x12), _byte(0x34)] + _spacer()
            + [_rt(0)] * 12 + [_rt(0, raw=0x155)])
    labels = annotate(syms)
    status = [l for l in labels if l.startswith("Ping Status Dev ")]
    assert len(status) == 12 and status[-1].startswith("Ping Status Dev 11:")
    assert labels[-1] == "Idle Pattern"


def test_both_phases_of_the_alternating_idle_pattern_are_labelled_idle():
    for raw in (0x155, 0x2AA):
        syms = (_header(0, 1, mask=0xFFF) + [_byte(0x00), _byte(0x12), _byte(0x34)] + _spacer()
                + [_rt(0)] * 12 + [_rt(0, raw=raw)])
        assert annotate(syms)[-1] == "Idle Pattern", hex(raw)


def _phases(labels):
    """The labels split at each SPM, one list per phase."""
    out = []
    for label in labels:
        if label == "SPM (Start of Phase)":
            out.append([])
        elif out:
            out[-1].append(label)
    return out


def test_a_bad_header_reads_alike_in_the_symbol_pane_and_the_commands_table():
    """One CDS bit stream, garbled in one Phase Header token, then a clean Ping: the core
    reports the first phase as a bad header with no packet, and the annotator labels the
    six header tokens, marks the last "(Bad header)" and labels no packet or response."""
    import swi3score
    from test_native_command_parser import SPACER, UNDRIVEN, _idle, _ping, _tok
    for garble in ((0, SPACER), (0, _tok(9)), (2, SPACER), (4, SPACER), (5, UNDRIVEN)):
        stream = _ping(garble=garble) + _idle() + _ping() + _idle()
        cmds = swi3score._parse_cds_test(stream)
        assert [(c["command"], c["phase"], c["header_valid"]) for c in cmds] == [
            ("Bad header", "?", False), ("Ping", "GetStatus", True)], garble
        bad, good = _phases(annotate(swi3score._cds_symbols_test(stream)))
        assert len(bad) >= 6 and bad[5].endswith("(Bad header)"), garble
        assert not any(l.startswith(("Opcode", "Manager_CRC16", "Ping Status"))
                       for l in bad), garble
        assert good[0] == "PhaseID: GET_STATUS" and good[6] == "Opcode: PING", garble
        assert not any("Bad header" in l for l in good), garble


_HUB_DEPTHS = [0, 1, 1, 2, 2, 3, 3, 4, 4, 5, 5, 5]


def _hubbed_commit_levels():
    from test_native_command_parser import _bus, _commit, _idle, _ping
    cds = _idle(4)
    for _ in range(3):
        cds += _ping() + _idle(2)
    cds += _commit([0] * 12, depths=_HUB_DEPTHS, mask=0xFFF) + _idle(2)
    for _ in range(2):
        cds += _ping() + _idle(2)
    return _bus(4, {0: cds + _idle(4)})     # NRZS on Column 0 of a 4-column bus


def test_a_hub_delayed_commit_reads_as_the_commands_table_reads_it():
    import swi3score

    from swi3s_studio.analysis import responses
    levels = _hubbed_commit_levels()
    st = swi3score.DecoderSettings()
    st.forced_column_count, st.decode_audio = 4, False
    st.hub_depth_overrides = [(d, v) for d, v in enumerate(_HUB_DEPTHS) if v]
    dec = swi3score.Decoder(swi3score.MemorySampleSource(levels, 98_304_000, 4), st)
    dec.run()
    [commit] = [c for c in dec.commands() if c["command"] == "SSCR"]
    assert (commit["peripheral_response"], commit["manager_response"]) == (0, 0)
    seg = dec.segments()[0]

    def labels(depths):
        src = swi3score.MemorySampleSource(levels, 98_304_000, 4)
        syms = swi3score.decode_symbols(src, 4, 0, 0, seg["start_ui"], seg["row_base"],
                                        hub_depths=depths)
        return [l for l in annotate(syms) if l.startswith(("Commit Resp", "Manager Resp"))]

    ready = responses.peripheral_response_name("Commit", 0)
    confirm = responses.manager_response_name("Commit", 0)
    assert labels(_HUB_DEPTHS) == [f"Commit Resp Dev {d}: {ready}" for d in range(12)] + \
        [f"Manager Response: {confirm}"]
    assert labels([]) != labels(_HUB_DEPTHS), "test premise: the gaps misframe a blind read"


def test_every_symbol_window_passes_the_session_hub_depths(monkeypatch):
    import swi3score

    from swi3s_studio import session as session_mod
    sess = Session.from_demo(8)
    sess.hub_depths = {1: 2, 11: 5}
    seen = []
    real = swi3score.decode_symbols

    def spy(*a, **k):
        seen.append(k.get("hub_depths"))
        return real(*a, **k)

    monkeypatch.setattr(session_mod.swi3score, "decode_symbols", spy)
    sample = sess.commands[len(sess.commands) // 2]["start_sample"]
    want = [0] * 12
    want[1], want[11] = 2, 5
    for window in (lambda: sess.symbols(64), lambda: sess.symbols_around(sample, 64),
                   lambda: sess.symbols_before(sample, 64)):
        seen.clear()
        window()
        assert seen == [want]
