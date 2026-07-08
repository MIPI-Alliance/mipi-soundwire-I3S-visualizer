"""Manual Stream Sync Point (SSP): Session.set_ssp_row re-anchors the payload phase
and persists; the GUI exposes set/step/clear. For a post-commit capture the SSPA/
SSCR was never on the wire, so interval>1 ports need the phase picked by ear.

Run: QT_QPA_PLATFORM=offscreen PYTHONPATH=. python3 tests/test_ssp.py
"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import tempfile

from swi3s_studio.session import Session
from swi3s_studio.workspace import session_from_source


def test_manual_ssp_survives_negative_interval_csv():
    """A manual SSP with a malformed config CSV (negative Interval_REG on an enabled
    port) must not crash: Interval+1 == 0 would make the interval-period LCM 0 and the
    SSP-row modulo divide by zero (SIGFPE). The decoder skips negative intervals and
    floors the period at 1."""
    from swi3s_studio.model.bus_config import demo_config
    with tempfile.TemporaryDirectory() as d:
        csv = demo_config().to_csv_file(os.path.join(d, "c.csv"))
        txt = open(csv).read().replace("Interval_REG,7,", "Interval_REG,-1,")
        bad = os.path.join(d, "bad.csv")
        open(bad, "w").write(txt)
        s = Session.from_demo(64, config_csv=bad, ssp_row=3)   # must not SIGFPE
        assert s.ssp_row == 3


def test_ssp_row_set_persist_and_clear():
    sess = Session.from_demo(64)
    assert sess.ssp_row == -1                          # auto by default
    n = sess.audio_count

    sess.set_ssp_row(5)
    assert sess.ssp_row == 5
    assert sess.source.get("ssp_row") == 5             # persisted in the descriptor
    assert sess.audio_count == n                        # still decodes (re-groups same bits)

    sess.set_ssp_row(-1)                                # back to auto
    assert sess.ssp_row == -1
    assert "ssp_row" not in sess.source


def test_ssp_row_survives_workspace_reload():
    sess = Session.from_demo(64)
    sess.set_ssp_row(9)
    reloaded = session_from_source(sess.source)
    assert reloaded.ssp_row == 9


def test_gui_exposes_ssp_controls():
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    from swi3s_studio.ui.main_window import MainWindow
    win = MainWindow()
    for name in ("set_ssp_at_cursor", "step_ssp_row", "set_ssp_row_async"):
        assert hasattr(win, name), name
    # With a capture, set/step/clear run without error (re-decode on the worker path).
    win.load_demo()
    win.set_ssp_at_cursor()
    win.step_ssp_row(+1)
    win.set_ssp_row_async(-1)


def test_ssp_redecode_keeps_audio_view():
    """Stepping the SSP must NOT re-show hidden tracks or reset the zoom — the user
    is watching one spot on one channel to judge sync."""
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    from swi3s_studio.ui.main_window import MainWindow

    def wait_for_load(win, limit=2000):
        """Pump the event loop until the async re-decode thread joins (done runs on
        the GUI thread via a queued signal, so it needs a spinning loop here)."""
        for _ in range(limit):
            if getattr(win, "_load_thread", None) is None:
                return
            app.processEvents()
        raise AssertionError("re-decode did not finish")

    win = MainWindow()
    win.load_demo()
    av = win._audio_view
    lanes = list(av.channel_selection())
    if len(lanes) < 2:
        return                                          # need >=2 tracks to hide one
    keep = {lanes[0]}
    av.set_channel_selection(keep)                      # hide all but one
    assert av.channel_selection() == keep
    av.set_visible_index_range((100, 200))              # zoom to a window
    win.step_ssp_row(+1)                                # re-decode (async)
    wait_for_load(win)
    assert win._session.ssp_row >= 0                    # the re-decode actually ran
    assert av.channel_selection() == keep               # selection preserved
    x0, x1 = av.visible_index_range()
    assert (x0, x1) != (0, 0) and x1 - x0 < 10_000      # still zoomed, not full-scale


if __name__ == "__main__":
    test_ssp_row_set_persist_and_clear(); print("ok: set / persist / clear")
    test_ssp_row_survives_workspace_reload(); print("ok: survives workspace reload")
    test_gui_exposes_ssp_controls(); print("ok: GUI exposes SSP controls")
    test_ssp_redecode_keeps_audio_view(); print("ok: re-decode keeps zoom + selection")
    test_manual_ssp_survives_negative_interval_csv(); print("ok: negative-interval CSV no crash")
    print("ALL SSP TESTS PASSED")
