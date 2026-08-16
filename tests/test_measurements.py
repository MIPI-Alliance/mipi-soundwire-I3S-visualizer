"""Measurements analysis test.

Run: python3 -m pytest tests/test_measurements.py
"""
from swi3s_studio.analysis import capture_measurements
from swi3s_studio.session import Session


def test_measurements():
    sess = Session.from_demo(300)
    rows = capture_measurements(sess)
    d = dict((r[0], r[1]) for r in rows)     # rows may be (m, v) or (m, v, kind)
    # The demo reconfigures (safe-lock-2 -> 8 -> 16 columns), so rates are listed PER
    # REGION (the single whole-capture "Columns" row only appears for a fixed geometry).
    assert d["Region 0"] == "2col"
    assert d["Region 1"] == "8col"
    assert d["Region 2"] == "16col"
    # Command count lives under the Commands section (the "Commands" key is the section
    # header now, value "").
    assert d["Count"] == str(len(sess.commands))
    assert d["Errors"] == "0"
    # Bus-config segments summary (three geometries in order).
    assert d["Bus configs"] == str(len(sess.segments))
    assert d["  column counts"] == "2col, 8col, 16col"
    # SSP count = commands carrying a Stream Sync Point (SSCR/SSPA).
    n_ssp = sum(1 for c in sess.commands if c.get("has_sync_point"))
    assert d["SSP count"] == str(n_ssp) and n_ssp >= 1
    # Per-(device, dataport) audio metrics: dp0/dp1 are mono 16-bit PCM.
    assert d["Dev0 DP0 channels"] == "1"
    assert d["Dev0 DP1 channels"] == "1"
    assert d["Dev0 DP0 sample size"] == "16 bits"
    assert "kbit/s" in d["Dev0 DP0 bandwidth"]
    # Bandwidth = rate * channels * bits (1 channel x 16 bits).
    rate = sess.audio_store().rate(0, 0)
    bw = float(d["Dev0 DP0 bandwidth"].split()[0])
    assert abs(bw - rate * 1 * 16 / 1000) < 1.0


def test_sections_ordered_link_regions_commands_dataports():
    """The Statistics rows are grouped into collapsible sections in a fixed order:
    Link Control, Regions, Commands, Data Ports (each a 'header'-kind row)."""
    sess = Session.from_demo(300, cold_start=True)      # cold start => Link Control present
    rows = capture_measurements(sess)
    headers = [m for (m, *rest) in rows if len(rest) > 1 and rest[1] == "header"]
    assert headers == ["Link Control", "Regions", "Commands", "Data Ports"], headers
    # Link Control carries the decoded sequence + the §5.2.3 timing (with PASS/FAIL).
    d = dict((r[0], r[1]) for r in rows)
    assert "Cold Start" in d.get("Sequence", "")
    assert any(len(r) > 2 and r[2] in ("pass", "fail") for r in rows)


def test_per_config_rates_when_bus_reconfigures():
    """When the bus reconfigures mid-capture (>1 region), the headline rates/columns
    are listed PER REGION from each section's own measured UI period — not one
    misleading whole-capture figure."""
    sess = Session.from_demo(8)
    # Two sections: a 2-col slow region then a 16-col fast region.
    sess.section_ui_stats = lambda: [
        {"section": 0, "column_count": 2,  "ui_ns": 162.76, "ui_min_ns": 160.0, "ui_max_ns": 165.0},
        {"section": 1, "column_count": 16, "ui_ns": 40.69,  "ui_min_ns": 40.0,  "ui_max_ns": 41.0},
    ]
    rows = capture_measurements(sess)
    labels = [m for (m, *_rest) in rows]
    # Both regions are listed, and the single whole-capture "Columns" row is gone.
    assert labels.count("Region 0") == 1 and labels.count("Region 1") == 1
    assert "Columns" not in labels and "Clock" not in labels
    # Region 0 (2col) is the slower clock; region 1 (16col) faster — from ui_ns.
    def val_after(anchor, key):
        i = labels.index(anchor)
        return next(v for m, v in ((r[0], r[1]) for r in rows[i:]) if m == key)
    assert val_after("Region 0", "Region 0") == "2col"
    assert val_after("Region 1", "Region 1") == "16col"
    c0 = float(val_after("Region 0", "  clock").split()[0])
    c1 = float(val_after("Region 1", "  clock").split()[0])
    assert c1 > c0 and abs(c0 - 3.072) < 0.01 and abs(c1 - 12.288) < 0.01


def test_bus_configs_leads_the_regions_section():
    """Bus configs comes FIRST in Regions: it says how many regions there are and which
    geometries they use, which is the frame the per-region rates are read against. Behind
    them it read as a footnote to the last region rather than a summary of all of them."""
    sess = Session.from_demo(300)
    rows = capture_measurements(sess)
    labels = [m for (m, *_rest) in rows]
    start = labels.index("Regions")                      # the section header row
    body = labels[start + 1:]
    assert body[0] == "Bus configs", body[:4]
    assert body[1] == "  column counts", body[:4]
    # ...and it is ahead of every per-region / rate row, not merely ahead of the first.
    assert body.index("Bus configs") < min(
        i for i, m in enumerate(body) if m.startswith(("Region ", "Clock", "UI rate")))


def test_ping_period_in_rows():
    """Ping cadence is reported in bus rows (min/max/mean of the gap between consecutive
    Ping commands) — independent of the per-section clock rate. The demo pings on a fixed
    schedule, so min == max == mean."""
    sess = Session.from_demo(8)
    d = dict((r[0], r[1]) for r in capture_measurements(sess))
    ping_rows = [int(c["bus_row"]) for c in sess.commands if c.get("command") == "Ping"]
    if len(ping_rows) >= 2:
        periods = [b - a for a, b in zip(ping_rows, ping_rows[1:]) if b - a > 0]
        assert d["Ping period"] == f"{len(ping_rows):,} pings"
        assert d["  min"] == f"{min(periods):,} rows"
        assert d["  max"] == f"{max(periods):,} rows"
        assert "rows" in d["  mean"]
    else:                                    # no cadence to report on a 1-ping capture
        assert "Ping period" not in d


def _demo_reads():
    from swi3s_studio.session import Session
    s = Session.from_demo(4000, cold_start=True)
    return s, [c for c in s.commands if "Read" in str(c.get("command", ""))]


def test_demo_carries_an_immediate_and_a_deferred_read():
    """The demo emits both read shapes, and they decode.

    Until 3.0.13 no demo emitted a Read of any kind, so nothing exercised the ReadSetup /
    ReadData transport at all — which is why the statistics split below went unnoticed and
    why a packetless ReadData read as CRC-invalid to one test's weaker predicate.

    Pins the frame as well as the decode. An earlier version of the demo's payload helper
    stopped at the data CRC and omitted the PM spacer + Manager Response that CLOSE the
    phase; the immediate read then produced no record at all and no error — the next
    command's comma abandoned the phase mid-spacer. Counting errors would not have caught
    that. Counting RECORDS does."""
    _s, reads = _demo_reads()
    kinds = [(c.get("command"), c.get("peripheral_response")) for c in reads]
    assert ("ReadA32", 0) in kinds, f"no immediate read (READ_DATA_NOW): {kinds}"
    assert ("ReadA32", 5) in kinds, f"no deferred setup (REMOTE_READ_DEFERRED): {kinds}"
    assert ("ReadData", 0) in kinds, f"no deferred delivery: {kinds}"

    imm = next(c for c in reads if c.get("command") == "ReadA32"
               and c.get("peripheral_response") == 0)
    dat = next(c for c in reads if c.get("command") == "ReadData")
    assert imm.get("read_data") == b"\x5a\xa5", imm.get("read_data")
    assert dat.get("read_data") == b"\xc3\x3c", dat.get("read_data")
    assert imm.get("read_data_crc_valid") and dat.get("read_data_crc_valid")
    # The delivery inherits the SETUP's address, which is how the two are paired.
    setup = next(c for c in reads if c.get("command") == "ReadA32"
                 and c.get("peripheral_response") == 5)
    assert dat.get("address") == setup.get("address"), (dat.get("address"),
                                                        setup.get("address"))
    # A ReadData has no Manager Packet, so it carries no CRC to be valid or invalid.
    assert dat.get("has_manager_packet") is False


def test_deferred_read_is_one_statistics_group_not_two():
    """A deferred read is two wire records but ONE logical read, and the statistics group by
    logical identity. Grouped by the raw wire name it showed as "ReadA32 / 0 bytes read"
    beside an unconnected "ReadData" group holding the real bytes — two rows a reader cannot
    join up, for one read they asked for."""
    from swi3s_studio.analysis import measurements as M
    s, _reads = _demo_reads()
    rows = M._command_rows(s)
    labels = [str(r[0]).strip() for r in rows]
    assert "ReadA32" in labels, labels
    assert "ReadData" not in labels, "the delivery is still its own group: " + str(labels)

    i = labels.index("ReadA32")
    assert rows[i][1] == "2", f"expected 2 logical reads, got {rows[i][1]}"
    # 2 bytes from the immediate read + 2 delivered for the deferred one, on the read that
    # requested them — the whole point of folding.
    byte_rows = [r for r in rows[i:i + 4] if "bytes read" in str(r[0])]
    assert byte_rows and byte_rows[0][1] == "4", byte_rows


def test_folding_keeps_an_orphan_delivery():
    """A capture that starts after the ReadSetup has a delivery with nothing to fold into.
    It must still count: dropping it would under-report a read that genuinely happened."""
    from swi3s_studio.analysis.measurements import _fold_deferred_reads
    orphan = [{"command": "ReadData", "device_mask": 1, "read_data": b"\x01\x02"}]
    assert len(_fold_deferred_reads(orphan)) == 1
    # And a FAILED setup (READ_FAILED = 4, not DEFERRED = 5) must not swallow a later
    # unrelated delivery.
    seq = [{"command": "ReadA32", "device_mask": 1, "peripheral_response": 4},
           {"command": "ReadData", "device_mask": 1, "read_data": b"\x03"}]
    assert len(_fold_deferred_reads(seq)) == 2
