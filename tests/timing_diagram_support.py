"""Helpers shared by the Timing term-diagram suites (test_timing_diagram.py and
test_timing_diagram_layout.py): one section built from a leg's timeline and terms, and the
highlight keys of its terms."""
from swi3s_studio.timing import compute
from swi3s_studio.timing.timeline import leg_timeline


def sections(inp, leg):
    """The diagram sections of `leg` at `inp`: one, headed "Specification"."""
    from swi3s_studio.ui.timing_diagram import DiagramSection
    tl = leg_timeline(inp, leg)
    return [DiagramSection("Specification", tl, list(compute(inp).breakdowns[leg].terms))]


def keys(secs):
    """The highlight key of every term of the first section."""
    from swi3s_studio.ui.timing_view import _term_key
    return {_term_key(t.symbol) for t in secs[0].terms}
