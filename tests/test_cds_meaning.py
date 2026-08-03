"""CDS symbol meaning annotator test.

Verifies the per-symbol Command Transport labels (PhaseID, Device Mask, Opcode,
Address, CRC, responses) against the demo capture's Ping / WriteA32 / Commit
phases.

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
    sess = Session.from_demo(8)
    sc = next((c for c in sess.commands if c["command"] in ("SSCR", "DSCR")), None)
    if sc is None:
        return
    syms = sess.symbols_around(sc["start_sample"])
    labels = annotate(syms)
    assert "PhaseID: COMMIT" in labels
    assert f"Opcode: {sc['command']}" in labels
    assert any(l.startswith("Group Mask") for l in labels)
    assert any(l.startswith("Row Delay") for l in labels)
    assert any(l.startswith("Commit Resp Dev 0:") for l in labels)
