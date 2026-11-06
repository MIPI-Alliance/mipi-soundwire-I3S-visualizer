"""The native command-transport parser on synthetic CDS bit streams (swi3score._parse_cds_test,
and Decoder.commands() on a 4-column bus whose Column 0 carries the stream).

Each stream is built symbol by symbol: the K.28.7 comma, the phase header (PhaseID,
DevMask as three tokens, PktLen as two), the Manager Packet bytes and their CRC-16, then the
phase's responses with their protocol spacers, and idle 0x155 spacers after.

- A garbled header is "Bad header" in phase "?", flagged header_valid=False, whatever phase
  (Ping, Write, Commit) or opcode it carried and whichever token was garbled, in the parser
  and in Decoder.commands() alike.
- Only an SSCR or DSCR commits: a confirmed Commit with a reserved opcode is named by its
  number, carries is_commit False and leaves the staged registers uncommitted.
- A WriteA32 decodes its device mask, packet length, address and data; one byte changed in
  its Manager Packet or its CRC makes crc_valid False, and command_store.register_writes
  skips it. One byte changed in a ReadData delivery makes read_data_crc_valid False.
- A completed ReadData clears its device's pending read: the next ReadData with no
  ReadSetup before it reads 1 byte and carries no address.
- The Device Mask's first token carries devices 8..11.
- Each device's Ping status and Commit response token is read 2 CDS bits per hub level
  later than the device before it; a hub depth above max_hub_depth (session.MAX_HUB_DEPTH)
  reads as max_hub_depth.
"""
import swi3score
from test_native_reference_vectors import _crc16

COMMA = 0x0F8          # K.28.7, RD-
SPACER = 0x155
PING, WRITE, READ_SETUP, READ_DATA, COMMIT = 0, 1, 2, 3, 4
COMMIT_NOT_READY = 2
UNDRIVEN = 0x3FF


def _bits(symbol):
    return [bool((symbol >> i) & 1) for i in range(9, -1, -1)]


def _tok(t):
    return swi3score._token_symbol(t)


def _header(phase, mask, packet, *, garble=None):
    """Comma, header and Manager Packet + CRC as symbols; `garble` = (index, symbol)
    replaces one header symbol (0 = PhaseID .. 5 = PktLen low)."""
    n = len(packet)
    hdr = [_tok(phase), _tok((mask >> 8) & 0xF), _tok((mask >> 4) & 0xF), _tok(mask & 0xF),
           _tok(n >> 4), _tok(n & 0xF)]
    if garble is not None:
        hdr[garble[0]] = garble[1]
    crc = _crc16(packet)
    body = [swi3score.encode_codeword(b) for b in [*packet, crc >> 8, crc & 0xFF]]
    return [COMMA, *hdr, *body]


def _ping(*, garble=None):
    syms = _header(PING, 0xFFF, [0x00], garble=garble) + [SPACER]
    return [b for s in syms for b in _bits(s)] + [b for _ in range(12) for b in _bits(_tok(0))]


def _commit(responses, *, opcode=0x00, depths=None, mask=0x001):
    """A Commit whose devices answer `responses` (a token, or UNDRIVEN as a raw symbol),
    each delayed by 2 bits per hub level, then CONFIRM_COMMIT."""
    depths = depths or [0] * 12
    out = [b for s in _header(COMMIT, mask, [opcode, 0x01, 0x00]) + [SPACER] for b in _bits(s)]
    prev = 0
    for dev, r in enumerate(responses):
        out += [False] * (2 * max(0, depths[dev] - prev))
        prev = depths[dev]
        out += _bits(UNDRIVEN if r == UNDRIVEN else _tok(r))
    out += _bits(SPACER) * 2 + _bits(_tok(0))       # PM spacer, CONFIRM_COMMIT
    return out


def _idle(n=4):
    return _bits(SPACER) * n


def _bus(cols, streams):
    """Bus levels for a `cols`-column bus whose column c carries the CDS bits streams[c]
    (NRZS against the UI before it: a 1 holds the level, a 0 toggles it; a 1 past the
    stream's end) and whose other columns hold the level, after 8 rows held low and with 8
    held rows after."""
    rows = max(len(s) for s in streams.values())
    levels = [False] * (cols * 8)
    for r in range(rows):
        for c in range(cols):
            bits = streams.get(c)
            same = bits is None or r >= len(bits) or bits[r]
            levels.append(levels[-1] if same else not levels[-1])
    return levels + [levels[-1]] * (cols * 8)


def test_a_garbled_header_is_reported_and_the_next_phase_decodes():
    stream = _ping(garble=(4, SPACER))
    for _ in range(12):
        stream += _idle() + _ping()
    cmds = swi3score._parse_cds_test(stream + _idle())
    assert len(cmds) == 13
    assert not cmds[0]["header_valid"] and cmds[0]["packet_length"] == 0
    assert all(c["header_valid"] and c["crc_valid"] for c in cmds[1:])


def test_a_garbled_device_mask_selects_no_device():
    syms = _header(COMMIT, 0x001, [0x00, 0x01, 0x00], garble=(2, SPACER))
    cmds = swi3score._parse_cds_test([b for s in syms for b in _bits(s)] + _idle(30))
    assert cmds[0]["header_valid"] is False
    assert cmds[0]["device_mask"] == 0


def test_symbol_framing_reads_only_the_depths_of_the_bus_devices():
    """decode_symbols frames hub-aware only when one of Devices 0..11 has a depth above 0: a
    depth at index 12 or later, or a negative one, frames the symbols as no depth does."""
    depths = [1] * 12
    cds = (_idle(4) + _ping() + _idle(2) + _commit([0] * 12, depths=depths, mask=0xFFF)
           + _idle(2) + _ping() + _idle(4))
    levels = _bus(4, {0: cds})

    def symbols(hub_depths):
        src = swi3score.MemorySampleSource(levels, 98_304_000, 4)
        return [(s["kind"], s["value"], s["raw"])
                for s in swi3score.decode_symbols(src, 4, hub_depths=hub_depths)]

    blind = symbols([])
    assert symbols(depths) != blind, "test premise: the hub gaps reframe the symbols"
    for ignored in ([0] * 12 + [3], [0] * 12 + [5] * 4, [-1] * 12, [0] * 11 + [-2, 4]):
        assert symbols(ignored) == blind, ignored


def test_commit_responses_follow_the_hub_depths():
    depths = [1] * 12
    stream = _commit([COMMIT_NOT_READY] + [UNDRIVEN] * 11, depths=depths) + _idle()
    hubbed = swi3score._parse_cds_test(stream, depths)
    assert len(hubbed) == 1
    assert hubbed[0]["command"] == "SSCR"
    assert hubbed[0]["peripheral_response"] == COMMIT_NOT_READY
    assert hubbed[0]["manager_response"] == 0
    blind = swi3score._parse_cds_test(stream)
    assert (blind[0]["peripheral_response"], blind[0]["manager_response"]) != (
        COMMIT_NOT_READY, 0)


def test_each_commit_response_is_read_after_its_own_hub_gap():
    """Non-uniform depths, device 0 included: each device in turn answers
    COMMIT_NOT_READY among READY ones, after 2 bits per hub level more than the device
    before it, and the parser reads that token and the Manager's CONFIRM_COMMIT after."""
    depths = [1, 1, 2, 4, 4, 4, 5, 5, 5, 5, 5, 5]
    for dev in range(12):
        responses = [0] * 12
        responses[dev] = COMMIT_NOT_READY
        stream = _commit(responses, depths=depths, mask=0xFFF) + _idle()
        [cmd] = swi3score._parse_cds_test(stream, depths)
        assert (cmd["peripheral_response"], cmd["manager_response"]) == \
            (COMMIT_NOT_READY, 0), dev
    stream = _commit([0] * 12, depths=depths, mask=0xFFF) + _idle()
    assert [(c["peripheral_response"], c["manager_response"])
            for c in swi3score._parse_cds_test(stream, depths)] == [(0, 0)]
    shallow = [d - 1 if d else 0 for d in depths]
    assert [(c["peripheral_response"], c["manager_response"])
            for c in swi3score._parse_cds_test(stream, shallow)] != [(0, 0)]


def test_a_multicast_commit_shows_the_device_that_is_not_ready():
    responses = [UNDRIVEN] * 12
    responses[0], responses[5] = 0, COMMIT_NOT_READY
    cmds = swi3score._parse_cds_test(_commit(responses, mask=0x021) + _idle())
    assert cmds[0]["peripheral_response"] == COMMIT_NOT_READY
    assert cmds[0]["manager_response"] == 0


def test_a_reserved_opcode_is_named_by_its_number():
    names = [swi3score._parse_cds_test(_commit([0] + [UNDRIVEN] * 11, opcode=op) + _idle())[0]
             ["command"] for op in (0x07, 0x01)]
    assert names == ["Op0x07", "DSCR"]


def _symbol_bits(symbols):
    return [b for s in symbols for b in _bits(s)]


_WRITE_PACKET = [0x00, 0x00, 0x00, 0x10, 0x04, 0x2A]     # WriteA32 0x00001004 <- 0x2A


def _write(mask=0x001, packet=_WRITE_PACKET, *, corrupt=None):
    """A WriteA32 phase; `corrupt` = a symbol index (7.. = the Manager Packet, then its two
    CRC bytes) whose byte is replaced by 0x5A after the CRC is computed."""
    syms = _header(WRITE, mask, packet) + [SPACER, _tok(0)]
    if corrupt is not None:
        syms[corrupt] = swi3score.encode_codeword(0x5A)
    return _symbol_bits(syms)


def _delivery(data, corrupt):
    """Read data and its CRC-16 as symbols; `corrupt` flips bit 0 of the first data byte
    after the CRC is computed."""
    crc = _crc16(data)
    body = [*data, crc >> 8, crc & 0xFF]
    if corrupt:
        body[0] ^= 0x01
    return [swi3score.encode_codeword(b) for b in body]


def _read_setup(mask, count, address, response, data=(), *, corrupt=False):
    """A ReadSetup (ReadA32) phase; READ_DATA_NOW (response 0) delivers `data` in it."""
    packet = [0x00, count - 1, *address.to_bytes(4, "big")]
    syms = _header(READ_SETUP, mask, packet) + [SPACER, _tok(response)]
    if response == 0:
        syms += [SPACER, *_delivery(list(data), corrupt), SPACER, SPACER, _tok(0)]
    return _symbol_bits(syms)


def _read_data(mask, data, *, corrupt=False):
    """A ReadData phase (no Manager Packet) answered READ_DATA_NOW with `data`."""
    syms = _header(READ_DATA, mask, [])[:7]          # comma and header only
    syms += [SPACER, _tok(0), SPACER, *_delivery(list(data), corrupt), SPACER, SPACER, _tok(0)]
    return _symbol_bits(syms)


def _decode(phases):
    """Decoder.commands() other than Pings for `phases` between Pings, on a 4-column bus
    whose Column-0 CDS carries the stream (NRZS) and whose other columns hold the level."""
    cds = _idle(4)
    for _ in range(3):
        cds += _ping() + _idle(2)
    for p in phases:
        cds += p + _idle(2)
    for _ in range(2):
        cds += _ping() + _idle(2)
    levels = _bus(4, {0: cds + _idle(4)})
    st = swi3score.DecoderSettings()
    st.forced_column_count, st.decode_audio = 4, False
    d = swi3score.Decoder(swi3score.MemorySampleSource(levels, 98_304_000, 4), st)
    d.run()
    return [c for c in d.commands() if c["command"] != "Ping"]


def test_a_write_a32_header_and_packet_decode():
    cmds = swi3score._parse_cds_test(_write() + _idle())
    assert [(c["command"], c["phase"], c["crc_valid"], c["device_mask"], c["packet_length"])
            for c in cmds] == [("WriteA32", "Write", True, 0x001, 6)]
    [w] = _decode([_write()])
    assert (w["has_address"], w["address"], w["data"]) == (True, 0x1004, b"\x2a")


def test_a_corrupted_packet_or_read_data_fails_its_crc():
    from swi3s_studio.store import command_store
    good, payload, crc = _decode([_write(), _write(corrupt=8), _write(corrupt=13)])
    assert [c["crc_valid"] for c in (good, payload, crc)] == [True, False, False]
    assert [w[1] for w in command_store.register_writes([good, payload, crc])] == [0x1004]
    reads = _decode([_read_setup(0x001, 2, 0x1004, 0, [0x12, 0x34]),
                     _read_setup(0x001, 2, 0x1004, 0, [0x12, 0x34], corrupt=True)])
    assert [(c["command"], c["crc_valid"], c["has_read_data"], c["read_data_crc_valid"])
            for c in reads] == [("ReadA32", True, True, True), ("ReadA32", True, True, False)]
    [clean, bad] = _decode([_read_data(0x001, [0x09]), _read_data(0x001, [0x09], corrupt=True)])
    assert (clean["read_data"], clean["read_data_crc_valid"]) == (b"\x09", True)
    assert (bad["has_read_data"], bad["read_data_crc_valid"]) == (True, False)


def test_a_completed_read_data_clears_the_pending_read():
    deferred = _read_setup(0x004, 3, 0x2000, 1)              # READ_DATA_DEFERRED
    _setup, first, second = _decode([deferred, _read_data(0x004, [1, 2, 3]),
                                     _read_data(0x004, [9])])
    assert (first["read_data"], first["has_address"], first["address"]) == \
        (b"\x01\x02\x03", True, 0x2000)
    assert first["read_data_crc_valid"]
    assert (second["read_data"], second["has_address"], second["read_data_crc_valid"]) == \
        (b"\x09", False, True)


def test_the_first_device_mask_token_carries_devices_8_to_11():
    masks = (0x100, 0x800, 0xF01)
    stream = [b for m in masks for b in _write(m) + _idle()]
    assert [c["device_mask"] for c in swi3score._parse_cds_test(stream)] == list(masks)


def test_a_bad_header_is_named_as_one_and_flagged_in_the_decode():
    """A phase whose header did not read is "Bad header" in phase "?", whatever its PhaseID
    token held and whichever of its six tokens was garbled, and every command a decode
    reports carries header_valid."""
    for garble in ((0, _tok(7)), *((i, SPACER) for i in range(6))):
        [bad, *_rest] = swi3score._parse_cds_test(_ping(garble=garble) + _idle())
        assert (bad["command"], bad["phase"], bad["header_valid"]) == \
            ("Bad header", "?", False), garble
    phases = {"Commit": (COMMIT, [0x07, 0x01, 0x00]), "Write": (WRITE, _WRITE_PACKET)}
    for name, (phase, packet) in phases.items():
        for garble in ((i, SPACER) for i in range(6)):
            syms = _header(phase, 0x001, packet, garble=garble)
            [bad, *_rest] = swi3score._parse_cds_test(_symbol_bits(syms) + _idle(30))
            assert (bad["command"], bad["phase"], bad["header_valid"]) == \
                ("Bad header", "?", False), (name, garble)
    for garble in ((i, SPACER) for i in range(6)):
        syms = _header(WRITE, 0x001, _WRITE_PACKET, garble=garble) + [SPACER, _tok(0)]
        assert [(c["command"], c["phase"], c["header_valid"])
                for c in _decode([_symbol_bits(syms), _write()])] == \
            [("Bad header", "?", False), ("WriteA32", "Write", True)], garble
    dec = swi3score.Decoder(swi3score.MemorySampleSource(swi3score.make_demo_levels(8),
                                                         98_304_000, 4),
                            swi3score.DecoderSettings())
    dec.run()
    cmds = dec.commands()
    assert cmds and all(c["header_valid"] is True for c in cmds)


_STAGED = 0x188         # a dual-ranked (_NEXT) register on device 0


def _confirmed_commit(opcode):
    """A Commit to device 0 with Group Mask 0x01 and Row_Delay 14, answered READY by device
    0 and CONFIRM_COMMIT by the Manager."""
    syms = _header(COMMIT, 0x001, [opcode, 0x01, 14]) + [SPACER, _tok(0)]
    return _symbol_bits(syms + [UNDRIVEN] * 11 + [SPACER, SPACER, _tok(0)])


def test_only_an_sscr_or_dscr_commits_the_staged_registers():
    assert not swi3score.register_write_is_immediate(_STAGED)
    write = _write(packet=[0x00, 0x00, 0x00, _STAGED >> 8, _STAGED & 0xFF, 0x05])
    for opcode, name, commits in ((0x00, "SSCR", True), (0x01, "DSCR", True),
                                  (0x02, "Op0x02", False), (0x07, "Op0x07", False)):
        cds = _idle(4)
        for p in [_ping(), write, _confirmed_commit(opcode)] + [_ping()] * 20:
            cds += p + _idle(2)
        st = swi3score.DecoderSettings()
        st.forced_column_count, st.decode_audio = 4, False
        d = swi3score.Decoder(swi3score.MemorySampleSource(_bus(4, {0: cds + _idle(4)}),
                                                           98_304_000, 4), st)
        d.run()
        [commit] = [c for c in d.commands() if c["phase"] == "Commit"]
        assert (commit["command"], commit["commit_confirmed"], commit["is_commit"]) == \
            (name, True, commits), opcode
        assert ((0, _STAGED, 5) in d.committed_registers()) is commits, opcode
        assert (commit["effective_row"] >= 0) is commits, opcode


def test_each_ping_status_is_read_after_its_own_hub_gap():
    """Device i's Ping status token follows 2 * (depth[i] - depth[i-1]) gap bits."""
    for depths in ([0, 2, 5, 5, 5, 5, 5, 5, 5, 5, 5, 5], [3, 3, 4, 4, 5, 5, 5, 5, 5, 5, 5, 5],
                   [0] * 11 + [5]):
        assert swi3score._hub_ping_test(depths, depths) == list(range(12)), depths
        single = [d // 2 for d in depths]
        assert swi3score._hub_ping_test(depths, single) != list(range(12)), depths


def test_a_hub_depth_above_the_limit_reads_as_the_limit():
    from swi3s_studio.session import MAX_HUB_DEPTH
    assert swi3score.max_hub_depth == MAX_HUB_DEPTH
    limit = swi3score.max_hub_depth
    emitted = [0, 2] + [limit] * 10
    for over in (limit + 1, 9, 1000):
        given = [0, 2] + [over] * 10
        assert swi3score._hub_ping_test(emitted, given) == list(range(12)), over
        stream = _commit([0] * 11 + [COMMIT_NOT_READY], depths=emitted, mask=0xFFF) + _idle()
        assert [(c["peripheral_response"], c["manager_response"])
                for c in swi3score._parse_cds_test(stream, given)] == \
            [(COMMIT_NOT_READY, 0)], over
    assert swi3score._hub_ping_test([0] * 12, [-3] * 12) == list(range(12))
