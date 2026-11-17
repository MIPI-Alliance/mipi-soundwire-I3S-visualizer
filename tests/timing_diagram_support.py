"""Helpers shared by the Timing term-diagram suites (test_timing_diagram.py and
test_timing_diagram_layout.py): one section built from a leg's timeline and terms, the
highlight keys of its terms, and the parts a sweep over every leg and term is split into for
the platform scope (docs/TESTING.md §1.1)."""
import pytest

from swi3s_studio.timing import compute
from swi3s_studio.timing.timeline import SUPPORTED_LEGS, leg_timeline

# The reason a test the platform scope leaves to the full suite is skipped with; it names the
# platform scope, which tools/diagram_cover.py reads from the JUnit report.
SUBSET_SKIP = "the platform scope sweeps a subset of this (docs/TESTING.md §1.1)"

# The parts of a sweep over (leg, term) pairs: every pair, `cover`'s pairs, the others.
PARTS = ("whole", "cover", "rest")


def sections(inp, leg):
    """The diagram sections of `leg` at `inp`: one, headed "Specification"."""
    from swi3s_studio.ui.timing_diagram import DiagramSection
    tl = leg_timeline(inp, leg)
    return [DiagramSection("Specification", tl, list(compute(inp).breakdowns[leg].terms))]


def keys(secs):
    """The highlight key of every term of the first section."""
    from swi3s_studio.ui.timing_view import _term_key
    return {_term_key(t.symbol) for t in secs[0].terms}


def pairs(inp, built=None):
    """(leg, term key) for every leg of `SUPPORTED_LEGS` and every term of it at `inp`, in leg
    order and each leg's keys sorted. `built`, a dict, is given each leg's sections."""
    built = {} if built is None else built
    for leg in SUPPORTED_LEGS:
        built[leg] = sections(inp, leg)
    return [(leg, key) for leg in SUPPORTED_LEGS for key in sorted(keys(built[leg]))]


def cover(all_pairs):
    """The pairs of `all_pairs` that hold every leg and every term key of it at least once:
    for each leg in order its first key not yet held, else its first key; then, for each key
    still not held, its first pair. In `all_pairs`' order."""
    chosen, held = set(), set()
    legs = list(dict.fromkeys(leg for leg, _ in all_pairs))
    for leg in legs:
        own = [p for p in all_pairs if p[0] == leg]
        pick = next((p for p in own if p[1] not in held), own[0])
        chosen.add(pick)
        held.add(pick[1])
    for leg, key in all_pairs:
        if key not in held:
            chosen.add((leg, key))
            held.add(key)
    return [p for p in all_pairs if p in chosen]


def part_of(all_pairs, part):
    """The pairs of `all_pairs` a `PARTS` part sweeps: all of them, `cover`'s, or the rest."""
    if part == "whole":
        return list(all_pairs)
    held = set(cover(all_pairs))
    return [p for p in all_pairs if (p in held) == (part == "cover")]


def sweep(inp, part):
    """(leg, term key, the leg's sections) for each pair `part` (a `PARTS` entry) sweeps of
    `pairs(inp)`, the sections built once per leg."""
    built: dict = {}
    for leg, key in part_of(pairs(inp, built), part):
        yield leg, key, built[leg]


def sweep_params(named_inputs):
    """pytest params (inputs, part) for a sweep over the (name, inputs) of `named_inputs`:
    the first, the default, whole, and each other one's cover and rest, each named
    "<name> <part>"."""
    (first, inp0), *others = named_inputs
    return [pytest.param(inp0, "whole", id=f"{first} whole")] + [
        pytest.param(inp, part, id=f"{name} {part}")
        for name, inp in others for part in ("cover", "rest")]


def left_to_the_full_suite(request, reason=SUBSET_SKIP):
    """Skip the test on the platform scope (`conftest.platform_scope`) with `reason`."""
    from conftest import platform_scope
    if platform_scope(request.config):
        pytest.skip(reason)
