"""PHY1 (slow FBCSE) demo round-trip: a constant 4-column bus whose ports are
REPOSITIONED mid-capture (no column-count change), matching the reference
phy1_bus_config_1 -> _2 CSVs.

PHY1 is the slow forwarded-clock PHY: CDS (NRZS) at Column 0, a 4-column geometry at
1.536 MRows/s. Two 16-bit PCM ports (48 kHz) share one column interleaved (DP0 Offset 0
unscrambled, DP1 Offset 16 scrambled); one PDM port (2 samples/row, 3.072 MHz). Halfway
through, a commit moves the PCM pair (col 1 -> col 3) and the PDM (cols 2-3 -> cols 1-2)
without changing the column count — exercising a placement-only reconfigure.

Run: PYTHONPATH=. python3 -m pytest tests/test_phy1_demo.py
"""
import math

from conftest import demo_skipped_rate_hz, demo_transported_samples

from swi3s_studio.session import Session


def test_phy1_cold_start_selects_fbcse_slow():
    """A PHY1 cold start decodes as FBCSE-slow / Safe-Lock-2 (PhyNum 0b0001)."""
    s = Session.from_demo(64, cold_start=True, phy=1)
    lc = s.link_control
    assert lc.sequence == "cold" and lc.phy_number == 1
    assert lc.phy_name == "PHY1" and lc.phy_kind == "FBCSE-slow"
    assert lc.safe_lock_columns == 2
    assert not s.is_dlv                                  # forwarded clock, not DLV


def test_phy1_operational_geometry_and_rates():
    """PHY1 runs a constant 4-column bus at 1.536 MRows/s carrying two 48 kHz PCM ports
    and one 3.072 MHz PDM port."""
    s = Session.from_demo(400, cold_start=True, phy=1)
    assert s.column_count == 4
    assert abs(s.row_rate_khz - 1536.0) < 1.0, s.row_rate_khz
    assert s.audio_store().streams() == [(0, 0), (0, 1), (0, 2)]
    rates = s.decoder.audio_sample_rates()
    # DP0 skips 13 of every 160 intervals, so it carries 44.1 kHz on 48 kHz transport
    # opportunities; DP1 shares the column and does not skip. See conftest.
    assert abs(rates[(0, 0)] - demo_skipped_rate_hz()) < 1.0     # DP0 PCM, 44.1 kHz
    assert abs(rates[(0, 1)] - 48_000) < 1.0                     # DP1 PCM, 48 kHz
    assert abs(rates[(0, 2)] - 3_072_000) < 100.0       # DP2 PDM


def test_phy1_audio_decodes_clean():
    """DP0 (unscrambled PCM) decodes to the exact synthesized sine across the mid-capture
    reconfigure.

    DP0 SKIPS (13/160 -> 44.1 kHz) so it transports fewer samples than there were
    opportunities; DP1 does not and recovers all of them. The sine is indexed in SAMPLES, so a
    skipped interval advances nothing and the sequence continues — which is why the VALUES are
    still exact, and why they are the assertion that matters here. A mis-phased skip would read
    an idle interval as a sample and break the sequence at the first skip."""
    n = 600
    s = Session.from_demo(n, cold_start=True, phy=1)
    au = s.audio                           # the Session's copy (decoder's is released)
    dp0 = [a["value"] for a in sorted((a for a in au if a["dp"] == 0),
                                      key=lambda a: a["start_sample"])]
    dp1 = [a for a in au if a["dp"] == 1]
    lo, hi = demo_transported_samples(n)
    assert lo <= len(dp0) <= hi, f"dp0 transported {len(dp0)}, expected {lo}..{hi} of {n}"
    assert len(dp1) == n, len(dp1)
    amp = (1 << 15) * 0.45
    expect = [int(round(amp * math.sin(2 * math.pi * i / 64))) & 0xFFFF for i in range(8)]
    assert dp0[:8] == expect, (dp0[:8], expect)


def test_phy1_midcapture_reconfigure_moves_ports():
    """The bus grid / config export follow the cursor across the placement change: early
    in the audio the ports sit at config_1's columns (HS 1,1,2), later at config_2's
    (HS 3,3,1) — a repositioning at a CONSTANT 4-column geometry."""
    s = Session.from_demo(600, cold_start=True, phy=1)
    a0, a1 = int(s.audio_start_sample), int(s.audio_end_sample)
    early = s.config_dataports_at(int(a0 + 0.25 * (a1 - a0)))
    late = s.config_dataports_at(int(a0 + 0.75 * (a1 - a0)))
    assert early["num_columns"] + 1 == 4 and late["num_columns"] + 1 == 4
    assert [d["horizontal_start"] for d in early["dataports"][:3]] == [1, 1, 2]
    assert [d["horizontal_start"] for d in late["dataports"][:3]] == [3, 3, 1]
    # DP0/DP1 stay interleaved (Offset 0 / 16) in both placements.
    assert [d["offset"] for d in early["dataports"][:2]] == [0, 16]
    assert [d["offset"] for d in late["dataports"][:2]] == [0, 16]


def test_phy1_ui_load_demo():
    """The 'Open Demo Capture ▸ PHY1' menu path loads an FBCSE PHY1 session."""
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    from swi3s_studio.ui.main_window import MainWindow

    win = MainWindow()
    win.load_demo(phy=1)
    assert win._session.link_control.phy_name == "PHY1"
    assert not win._session.is_dlv
    assert win._session.source_label() == "PHY1 Demo Capture"
