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
    # header, value "").
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


def test_a_regions_clock_is_the_average_one_the_status_bar_shows():
    """Edges land on whole samples, so a 24.576 MHz UI at 500 MS/s is spaced 20 and 21
    samples, and the median of those spacings is the commoner integer. Each region's UI
    is therefore the mean, which is the decoder's own whole-capture figure and matches
    the status bar."""
    sess = Session.from_demo(300)
    audio = [st for st in sess.section_ui_stats() if st["column_count"] == 16]
    assert audio, "the demo has a 16-column audio region"
    ui_hz = 1e9 / audio[0]["ui_ns"]
    assert abs(ui_hz - sess.ui_rate_hz) / sess.ui_rate_hz < 1e-3, (ui_hz, sess.ui_rate_hz)
    stats = sess.section_ui_stats()
    clocks = [r[1] for r in capture_measurements(sess) if r[0] == "  clock"]
    assert len(clocks) == len(stats) > 1, clocks
    for st, v in zip(stats, clocks):
        assert v == f"{1e9 / st['ui_ns'] / 2e6:.4f} MHz", (st, v)
        assert abs(float(v.split()[0]) - sess.ui_rate_hz / 2e6) < 0.002, (st, v)


def test_bus_configs_leads_the_regions_section():
    """Bus configs comes FIRST in Regions: it says how many regions there are and which
    geometries they use, which is the frame the per-region rates are read against."""
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
    Ping commands) — independent of the per-section clock rate."""
    sess = Session.from_demo(8)
    d = dict((r[0], r[1]) for r in capture_measurements(sess))
    ping_rows = [int(c["bus_row"]) for c in sess.commands if c.get("command") == "Ping"]
    assert len(ping_rows) >= 2, "the demo must carry at least two Pings"
    periods = [b - a for a, b in zip(ping_rows, ping_rows[1:]) if b - a > 0]
    assert d["Ping period"] == f"{len(ping_rows):,} pings"
    assert d["  min"] == f"{min(periods):,} rows"
    assert d["  max"] == f"{max(periods):,} rows"
    assert d["  mean"] == f"{sum(periods) / len(periods):,.1f} rows"


def test_a_single_ping_reports_no_period():
    """One Ping has no gap to measure, so no Ping period is reported."""
    sess = Session.from_demo(8)
    first = next(c for c in sess.commands if c.get("command") == "Ping")
    sess.commands = [c for c in sess.commands if c.get("command") != "Ping"] + [first]
    assert "Ping period" not in dict((r[0], r[1]) for r in capture_measurements(sess))


def _demo_reads():
    from swi3s_studio.session import Session
    s = Session.from_demo(4000, cold_start=True)
    return s, [c for c in s.commands if "Read" in str(c.get("command", ""))]


def test_demo_carries_an_immediate_and_a_deferred_read():
    """The demo emits both read shapes, and they decode.

    Pins the frame as well as the decode: the read phase must close with the PM spacer +
    Manager Response, or the next command's comma abandons it mid-spacer and the read
    produces no record and no error. So the test counts RECORDS, not errors."""
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
    logical identity: the ReadData delivery folds into its ReadA32 setup, bytes included."""
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


def test_link_control_lists_every_bring_up():
    from types import SimpleNamespace

    from swi3s_studio.analysis.link_control import LinkControlResult
    from swi3s_studio.analysis.measurements import _link_control_rows
    timing = [{"param": "t_Reset", "measured_us": 12.0, "min_us": 10.0, "max_us": None,
               "ok": True}]
    first = LinkControlResult(sequence="cold", phy_name="PHY2", phy_kind="FBCSE-fast",
                              safe_lock_columns=2, timing=timing)
    later = LinkControlResult(sequence="warm", phy_name="PHY2", phy_kind="FBCSE-fast",
                              sections=[{"name": "Idle", "start": 500_000, "end": 600_000}],
                              timing=timing)
    s = SimpleNamespace(link_control=first, link_controls=[first, later], commands=[],
                        register_map=None, capture=SimpleNamespace(sample_rate_hz=1_000_000))
    rows = _link_control_rows(s)
    assert rows[0] == ("Sequence", "Cold Start")
    assert ("Safe-Lock", "2 columns") in rows
    assert not any(r[0] == "  Safe-Lock" for r in rows)            # the later one reports none
    i = rows.index(("Bring-up 2", "Warm Start at 500.000 ms"))
    assert rows[i + 1] == ("  Sequence", "Warm Start")
    assert ("  Selected PHY", "PHY2 (FBCSE-fast)") in rows
    assert [r for r in rows[i:] if r[0] == "  t_Reset"][0][2] == "pass"
    # One bring-up: no "Bring-up N" block.
    s.link_controls = [first]
    assert not any(r[0].startswith("Bring-up") for r in _link_control_rows(s))


def test_bring_ups_are_numbered_from_one_when_the_first_lists_nothing():
    """A capture that opens in audio mode has no first bring-up to list; its later bring-up is
    the first one shown."""
    from types import SimpleNamespace

    from swi3s_studio.analysis.link_control import LinkControlResult
    from swi3s_studio.analysis.measurements import _link_control_rows
    none = LinkControlResult()
    later = LinkControlResult(sequence="cold", phy_name="PHY2", phy_kind="FBCSE-fast",
                              sections=[{"name": "Idle", "start": 2_000, "end": 3_000}])
    s = SimpleNamespace(link_control=none, link_controls=[none, later], commands=[],
                        register_map=None, capture=SimpleNamespace(sample_rate_hz=1_000_000))
    rows = _link_control_rows(s)
    assert rows[0] == ("Bring-up 1", "Cold Start at 2.000 ms")
    assert not any(r[0] == "Bring-up 2" for r in rows)


def test_pm_actions_list_the_first_eight_and_count_them_all():
    from types import SimpleNamespace

    from swi3s_studio.analysis.link_events import PM_ACTION_ADDR
    from swi3s_studio.analysis.measurements import _link_control_rows
    from swi3s_studio.model import RegisterMap

    def write(i):
        return {"command": "WriteA32", "crc_valid": True, "has_address": True,
                "address": PM_ACTION_ADDR, "data": bytes([1]), "device_mask": 1,
                "bus_row": 100 + i, "start_sample": i}
    s = SimpleNamespace(link_control=None, link_controls=[], commands=[write(i) for i in range(9)],
                        register_map=RegisterMap.load(), capture=None)
    rows = _link_control_rows(s)
    assert ("PM actions", "9") in rows
    assert [r[0] for r in rows if r[0].startswith("  row")] == [f"  row {100 + i}" for i in range(8)]
    s.commands = s.commands[:8]
    assert len([r for r in _link_control_rows(s) if r[0].startswith("  row")]) == 8


def test_regions_list_reconfigurations_from_two_bus_configs():
    from types import SimpleNamespace

    from swi3s_studio.analysis.measurements import _region_rows

    def session(n):
        return SimpleNamespace(
            segments=[{"column_count": 2 + 6 * i} for i in range(n)], is_dlv=False,
            section_ui_stats=lambda: [], ui_rate_hz=24_576_000.0, row_rate_khz=3072.0,
            column_count=8)
    assert ("  reconfigurations", "1") in _region_rows(session(2))
    assert ("  reconfigurations", "2") in _region_rows(session(3))
    assert not any(r[0] == "  reconfigurations" for r in _region_rows(session(1)))


class _Store:
    """The slice of AudioStore the Data Ports section reads."""
    def __init__(self, streams):
        self._streams = streams            # {(dev, dp): (channels, rate, bits)}

    def streams(self):
        return sorted(self._streams)

    def channels(self, dev, dp):
        return list(range(self._streams[(dev, dp)][0]))

    def native_rate(self, dev, dp):
        return self._streams[(dev, dp)][1]

    def native_sample_bits(self, dev, dp, ch):
        return self._streams[(dev, dp)][2]


def _data_port_rows_for(commands, streams, sample_rate_hz=1_000_000):
    from types import SimpleNamespace

    from swi3s_studio.analysis.measurements import _data_port_rows
    return _data_port_rows(SimpleNamespace(commands=commands, sample_rate_hz=sample_rate_hz),
                           _Store(streams))


def test_data_port_bandwidth_is_rate_times_channels_times_bits():
    rows = _data_port_rows_for([], {(2, 1): (4, 48_000, 24)})
    assert rows == [("Dev2 DP1 channels", "4"), ("Dev2 DP1 sample rate", "48.00 kHz"),
                    ("Dev2 DP1 sample size", "24 bits"),
                    ("Dev2 DP1 bandwidth", "4608.0 kbit/s")]


def test_the_ssp_interval_needs_two_sync_points_and_is_averaged_per_device():
    def ssp(sample, mask):
        return {"has_sync_point": True, "start_sample": sample, "device_mask": mask}
    streams = {(0, 0): (1, 48_000, 16)}
    one = _data_port_rows_for([ssp(100, 1)], streams)
    assert not any("SSP interval" in r[0] for r in one)
    two = _data_port_rows_for([ssp(100, 1), ssp(300, 1), ssp(999, 2)], streams)
    assert ("Dev0 DP0 avg SSP interval", "200.00 µs") in two
    three = _data_port_rows_for([ssp(100, 1), ssp(300, 1), ssp(700, 1)], streams)
    assert ("Dev0 DP0 avg SSP interval", "300.00 µs") in three


def _read_cmd(address, value, row):
    return {"command": "ReadA32", "phase": "ReadSetup", "device_mask": 1 << 4,
            "has_address": True, "address": address, "data": b"", "read_data": bytes([value]),
            "read_data_crc_valid": True, "bus_row": row, "start_sample": row, "crc_valid": True,
            "has_manager_packet": True}


def test_a_counter_fault_shows_its_value_and_a_flag_fault_its_count():
    from types import SimpleNamespace

    from swi3s_studio.analysis.measurements import _peripheral_report_rows
    bad_crc = 0x1068                                       # EC_BadCRC
    int_stat = 0x1022                                      # IntStat_Link: BadCRC is bit 6
    cmds = [_read_cmd(bad_crc, 7, 10), _read_cmd(int_stat, 0x40, 20), _read_cmd(int_stat, 0x40, 30)]
    rows = {r[0].strip(): r for r in _peripheral_report_rows(SimpleNamespace(commands=cmds))}
    assert rows["Dev4 EC_BadCRC"][1:] == ("7, from row 10", "fail")
    assert rows["Dev4 IntStat_BadCRC"][1:] == ("2×, from row 20", "fail")
    assert rows["faults reported"][1:] == ("2", "fail")


def test_the_ping_period_ignores_pings_in_the_same_row():
    from types import SimpleNamespace

    from swi3s_studio.analysis.measurements import _command_rows
    pings = [{"command": "Ping", "phase": "GetStatus", "bus_row": r, "device_mask": 0xFFF}
             for r in (10, 10, 30, 70)]
    rows = dict((r[0], r[1]) for r in _command_rows(SimpleNamespace(commands=pings)))
    assert rows["Ping period"] == "4 pings"
    assert (rows["  min"], rows["  max"], rows["  mean"]) == ("20 rows", "40 rows", "30.0 rows")


def test_a_regions_row_rate_is_its_ui_rate_over_its_columns():
    from types import SimpleNamespace

    from swi3s_studio.analysis.measurements import _region_rows
    s = SimpleNamespace(
        segments=[{"column_count": 2}, {"column_count": 16}], is_dlv=False,
        section_ui_stats=lambda: [
            {"section": 0, "column_count": 2, "ui_ns": 100.0},
            {"section": 1, "column_count": 16, "ui_ns": 50.0}],
        ui_rate_hz=0.0, row_rate_khz=0.0, column_count=0)
    values = [r[1] for r in _region_rows(s) if r[0] == "  row rate"]
    assert values == ["5000.000 kHz", "1250.000 kHz"]          # 10 MHz / 2, 20 MHz / 16


def _region_session(dlv, stats, ui_rate_hz=24_576_000.0):
    from types import SimpleNamespace
    return SimpleNamespace(segments=[{"column_count": 16}], is_dlv=dlv,
                           section_ui_stats=lambda: stats, ui_rate_hz=ui_rate_hz,
                           row_rate_khz=1536.0, column_count=16)


def test_a_dlv_capture_lists_its_recovered_clock_at_the_ui_rate_and_fbcse_at_half():
    """FBCSE forwards a clock at half the UI rate; DLV recovers one at the full UI rate."""
    from swi3s_studio.analysis.measurements import _region_rows
    fbcse = dict(_region_rows(_region_session(False, [])))
    assert fbcse["Clock"] == "12.2880 MHz" and fbcse["UI rate"] == "24.5760 MHz"
    assert "Recovered clock" not in fbcse
    dlv = dict(_region_rows(_region_session(True, [])))
    assert dlv["Recovered clock"] == "24.5760 MHz" and dlv["UI rate"] == "24.5760 MHz"
    assert "Clock" not in dlv
    stats = [{"section": 0, "column_count": 4, "ui_ns": 100.0},
             {"section": 1, "column_count": 16, "ui_ns": 50.0}]
    assert [r[1] for r in _region_rows(_region_session(True, stats)) if r[0] == "  clock"] == \
        ["10.0000 MHz", "20.0000 MHz"]                       # the UI rate itself
    assert [r[1] for r in _region_rows(_region_session(False, stats)) if r[0] == "  clock"] == \
        ["5.0000 MHz", "10.0000 MHz"]


def test_a_region_with_no_measured_ui_lists_zero_rates_rather_than_dividing():
    from swi3s_studio.analysis.measurements import _region_rows
    stats = [{"section": 0, "column_count": 2, "ui_ns": 0.0},
             {"section": 1, "column_count": 0, "ui_ns": 50.0}]
    rows = _region_rows(_region_session(False, stats))
    assert [r[1] for r in rows if r[0] == "  clock"] == ["0.0000 MHz", "10.0000 MHz"]
    assert [r[1] for r in rows if r[0] == "  UI rate"] == ["0.0000 MHz", "20.0000 MHz"]
    assert [r[1] for r in rows if r[0] == "  row rate"] == ["0.000 kHz", "0.000 kHz"]


def test_link_control_limits_read_low_to_high_and_a_failed_row_is_coloured():
    from swi3s_studio.analysis.link_control import LinkControlResult
    from swi3s_studio.analysis.measurements import _bringup_rows
    timing = [
        {"param": "A", "measured_us": 40.0, "min_us": 5.0, "max_us": 32.0, "ok": False},
        {"param": "B", "measured_us": 10.0, "min_us": 5.0, "max_us": 32.0, "ok": True},
        {"param": "C", "measured_us": 1600.0, "min_us": 1569.0, "max_us": None, "ok": True},
        {"param": "D", "measured_us": 3.0, "min_us": None, "max_us": 4.0, "ok": True},
        {"param": "E", "measured_us": 3.0, "min_us": None, "max_us": None, "ok": None}]
    rows = _bringup_rows(LinkControlResult(timing=timing))
    assert rows == [
        ("A", "40.0 µs   [5–32 µs]   FAIL", "fail"),
        ("B", "10.0 µs   [5–32 µs]   PASS", "pass"),
        ("C", "1,600.0 µs   [≥ 1569 µs]   PASS", "pass"),
        ("D", "3.0 µs   [≤ 4 µs]   PASS", "pass"),
        ("E", "3.0 µs   (no limit)", None)]


def _write(mask, data, crc_valid=True):
    return {"command": "WriteA32", "device_mask": mask, "phase": "Write", "crc_valid": crc_valid,
            "has_manager_packet": True, "data": data}


def test_the_per_device_write_breakdown_covers_device_11_and_counts_errors_per_device():
    """A device with only clean writes shows no error row beside a device that has one."""
    from swi3s_studio.analysis import measurements as m
    cmds = [_write(1 << 11, b"\x01\x02"), _write(1 << 11, b"\x03"),
            _write(1 << 3, b"\x04", crc_valid=False), _write(1 << 3, b"\x05\x06\x07"),
            _write(1 << 0, b"\x08")]
    assert m._devices_of(cmds[0]) == [11]
    assert m._devices_of({"device_mask": 0xFFF}) == list(range(12))
    assert list(m._rw_detail_rows(cmds, reading=False)) == [
        ("    Device 0", "1", "detail"), ("      bytes written", "1", "detail"),
        ("    Device 3", "2", "detail"), ("      bytes written", "4", "detail"),
        ("      errors", "1", "fail"),
        ("    Device 11", "2", "detail"), ("      bytes written", "3", "detail")]
