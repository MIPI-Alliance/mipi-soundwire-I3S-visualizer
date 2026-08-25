"""Shared pytest fixtures / setup.

Keep the GUI tests fast: MainWindow preloads the demo capture, which is 1 s of audio
(~24.6M UIs, ~2.5 s to decode) by default. Force a short demo for the test process so
constructing a MainWindow stays near-instant. Tests that need specific audio lengths
call Session.from_demo(N) with an explicit N, which this does not affect.
"""
import os

os.environ.setdefault("SWI3S_DEMO_SAMPLES", "300")   # short preload demo for MainWindow()
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# QSettings needs a non-empty organisation/application name to persist reliably on
# Windows (its registry scope is keyed on them). The real app sets these in app.py, but
# tests build a bare QApplication — so per-mode dir-memory (capture/last_dir_*) tests
# didn't round-trip on Windows and fell back to the default examples dir. Set a
# TEST-scoped identity here (distinct from the app's, so it never touches a developer's
# real saved settings), before any test constructs a QApplication.
from PySide6.QtCore import QCoreApplication  # noqa: E402

QCoreApplication.setOrganizationName("MIPI SWI3S Test")
QCoreApplication.setApplicationName("SWI3S Studio Test")


def pytest_sessionfinish(session, exitstatus):  # noqa: ARG001
    """Join every MainWindow's worker threads before the interpreter tears down.

    GUI tests build MainWindows and rarely close them, so their load / locate /
    TX-persistence QThreads can still be alive at exit. Qt calls abort() when a running
    QThread is destroyed, which turns a fully passing run into a SIGABRT (exit 134) —
    invisible under pytest, which reports its result before teardown, but fatal to
    tests/run_all.sh, which judges each suite by its exit code.

    Fixing it here rather than per-test covers every suite, including ones added later.
    """
    _join_all_main_windows()


def _join_all_main_windows() -> None:
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance()
    if app is None:
        return
    for w in app.topLevelWidgets():
        join = getattr(w, "join_worker_threads", None)
        if callable(join):
            try:
                join()
            except Exception:  # noqa: BLE001 - teardown must never raise
                pass


# ---------------------------------------------------------------- payload skipping in the demos
# The PHY1/PHY2/PHY3 demos carry DP0 at 44.1 kHz on 48 kHz transport opportunities: 13 of every
# 160 intervals are skipped (Section 14.1.10). 44100/48000 == 147/160 exactly, and 160 is the
# smallest denominator that can express it. Named here rather than repeated as 44_100 in each
# test, so a test states the RULE — a rate derived from the skipping fraction — instead of a
# constant that says nothing about where it comes from.
DEMO_SKIP_NUMERATOR = 13
DEMO_SKIP_DENOMINATOR = 160
DEMO_PCM_OPPORTUNITY_HZ = 48_000.0


def demo_skipped_rate_hz(opportunity_hz: float = DEMO_PCM_OPPORTUNITY_HZ) -> float:
    """The effective rate of a demo port that skips: 48000 * 147/160 == 44100.0."""
    kept = DEMO_SKIP_DENOMINATOR - DEMO_SKIP_NUMERATOR
    return opportunity_hz * kept / DEMO_SKIP_DENOMINATOR


def demo_transported_samples(opportunities: int) -> tuple[int, int]:
    """(low, high) bound on how many of `opportunities` intervals actually transport.

    NOT an exact number, deliberately. The skip decision is a Bresenham accumulator
    (CDataPort::advanceSkippingAccumulator) that RESTARTS at every SSP — Section 9.1.6.2.1 — so
    the exact count depends on how many SSPs the capture contains and which interval each lands
    on. The three demos differ there: at 1500 samples/channel PHY1 transports 1380 and PHY2/PHY3
    transport 1379, because PHY1's mid-capture reconfigure and their SSPA cadences reset the
    accumulator at different points.

    A first version of this returned the single-run count and would have been wrong for PHY1 by
    one sample — a bound that is honest about what the phase depends on beats a constant that
    happens to match two demos out of three. The strong assertion is not the count anyway: it is
    that every transported VALUE equals the generated sine (see test_demo_skipping), which a
    mis-phased skip breaks immediately.
    """
    exact = opportunities * (DEMO_SKIP_DENOMINATOR - DEMO_SKIP_NUMERATOR) / DEMO_SKIP_DENOMINATOR
    # One extra sample per SSP at most; no demo has more than a handful.
    return (int(exact) - 4, int(exact) + 4)
