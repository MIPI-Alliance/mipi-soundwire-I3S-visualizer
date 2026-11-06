"""transitions.demo_capture: giving a sample rate or a samples-per-UI pins the capture's
sampling; giving neither keeps the analyzer-rate demo.

Run: python3 -m pytest tests/test_demo_capture_pinning.py
"""
from swi3s_studio.ingest import transitions


def _ui_spacing(cap):
    """Samples between the capture's first two clock edges (one edge per UI)."""
    return int(cap.clock_edges[1]) - int(cap.clock_edges[0])


def test_no_argument_is_the_analyzer_rate_demo():
    assert transitions.demo_capture(8).sample_rate_hz == transitions.ANALYZER_SAMPLE_RATE_HZ


def test_samples_per_ui_alone_is_honoured():
    cap = transitions.demo_capture(8, samples_per_ui=40)
    assert cap.sample_rate_hz == transitions.DEFAULT_SAMPLE_RATE_HZ
    assert _ui_spacing(cap) == 40


def test_the_default_rate_given_explicitly_is_not_replaced():
    cap = transitions.demo_capture(8, sample_rate_hz=transitions.DEFAULT_SAMPLE_RATE_HZ)
    assert cap.sample_rate_hz == transitions.DEFAULT_SAMPLE_RATE_HZ
    assert _ui_spacing(cap) == transitions.DEFAULT_SAMPLES_PER_UI
