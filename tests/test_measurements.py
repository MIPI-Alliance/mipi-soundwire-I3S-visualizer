"""Measurements analysis test.

Run: python3 tests/test_measurements.py
"""
from swi3s_studio.session import Session
from swi3s_studio.analysis import capture_measurements


def test_measurements():
    sess = Session.from_demo(8)
    rows = capture_measurements(sess)
    d = dict(rows)
    assert d["Columns"] == "16"               # two devices, 16-column grid
    assert d["Commands"] == str(len(sess.commands))
    assert d["Errors"] == "0"
    # Bus-config segments summary (the demo is a single 16-col config).
    assert d["Bus configs"] == str(len(sess.segments))
    assert "16col" in d["  column counts"]
    # SSP count = commands carrying a Stream Sync Point (SSCR/SSPA).
    n_ssp = sum(1 for c in sess.commands if c.get("has_sync_point"))
    assert d["SSP count"] == str(n_ssp) and n_ssp >= 1
    # Per-(device, dataport) audio metrics present (Dev0 DP0 and Dev1 DP1).
    assert d["Dev0 DP0 channels"] == "2"
    assert d["Dev1 DP1 channels"] == "2"
    assert d["Dev0 DP0 sample size"] == "8 bits"
    assert "kbit/s" in d["Dev0 DP0 bandwidth"]
    # Bandwidth = rate * channels * bits.
    rate = sess.audio_store().rate(0, 0)
    bw = float(d["Dev0 DP0 bandwidth"].split()[0])
    assert abs(bw - rate * 2 * 8 / 1000) < 1.0


def test_per_config_rates_when_bus_reconfigures():
    """When the bus reconfigures mid-capture (>1 config section), the headline
    rates/columns are listed PER CONFIG from each section's own measured UI period —
    not one misleading whole-capture figure."""
    sess = Session.from_demo(8)
    # Two sections: a 2-col slow region then a 16-col fast region.
    sess.section_ui_stats = lambda: [
        {"section": 0, "column_count": 2,  "ui_ns": 162.76, "ui_min_ns": 160.0, "ui_max_ns": 165.0},
        {"section": 1, "column_count": 16, "ui_ns": 40.69,  "ui_min_ns": 40.0,  "ui_max_ns": 41.0},
    ]
    rows = capture_measurements(sess)
    labels = [m for m, _ in rows]
    # Both configs are listed, and the single whole-capture "Columns" row is gone.
    assert labels.count("Config 0") == 1 and labels.count("Config 1") == 1
    assert "Columns" not in labels and "Clock" not in labels
    # Config 0 (2col) is the slower clock; config 1 (16col) faster — from ui_ns.
    def val_after(anchor, key):
        i = labels.index(anchor)
        return next(v for m, v in rows[i:] if m == key)
    assert val_after("Config 0", "Config 0") == "2col"
    assert val_after("Config 1", "Config 1") == "16col"
    c0 = float(val_after("Config 0", "  clock").split()[0])
    c1 = float(val_after("Config 1", "  clock").split()[0])
    assert c1 > c0 and abs(c0 - 3.072) < 0.01 and abs(c1 - 12.288) < 0.01


def test_ping_period_in_rows():
    """Ping cadence is reported in bus rows (min/max/mean of the gap between consecutive
    Ping commands) — independent of the per-section clock rate. The demo pings on a fixed
    schedule, so min == max == mean."""
    sess = Session.from_demo(8)
    d = dict(capture_measurements(sess))
    ping_rows = [int(c["bus_row"]) for c in sess.commands if c.get("command") == "Ping"]
    if len(ping_rows) >= 2:
        periods = [b - a for a, b in zip(ping_rows, ping_rows[1:]) if b - a > 0]
        assert d["Ping period"] == f"{len(ping_rows):,} pings"
        assert d["  min"] == f"{min(periods):,} rows"
        assert d["  max"] == f"{max(periods):,} rows"
        assert "rows" in d["  mean"]
    else:                                    # no cadence to report on a 1-ping capture
        assert "Ping period" not in d


if __name__ == "__main__":
    test_measurements()
    print("ok: capture measurements (columns, SSP, per-stream bandwidth)")
    test_per_config_rates_when_bus_reconfigures()
    print("ok: per-config rates listed when the bus reconfigures")
    test_ping_period_in_rows()
    print("ok: ping period reported in bus rows (min/max/mean)")
    print("ALL MEASUREMENTS TESTS PASSED")
