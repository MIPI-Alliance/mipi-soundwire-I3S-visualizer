"""The timeline's tick and commit-point decimation is the SAME answer, computed faster.

It used to be one Python iteration per command per view change (76 ms per wheel step at
100k commands, linear in the count); it is now a bisect to the view plus a vectorised
per-column maximum. The reference below is the old loop, copied verbatim, and the new code
must equal it exactly — which command wins each pixel column AND the order of the list —
over random commands of every paint rank, listed out of time order, with colliding and
duplicate starts, under a filter, at many zooms, and in a negative domain (a stack of Links
puts a later Link's view below its sample 0).
"""
import os
import random

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication

from swi3s_studio.ui.timeline import TimelineRibbon

_KINDS = [
    {"command": "Ping", "has_manager_packet": True, "crc_valid": True},
    {"command": "WriteA32", "has_manager_packet": True, "crc_valid": True},
    {"command": "ReadA32", "has_manager_packet": True, "crc_valid": True},
    {"command": "SSCR", "is_commit": True, "commit_confirmed": False,
     "has_manager_packet": True, "crc_valid": True},
    {"command": "SSCR", "is_commit": True, "commit_confirmed": True,
     "has_manager_packet": True, "crc_valid": True, "has_sync_point": True},
    {"command": "SSPA", "has_sync_point": True, "has_manager_packet": True, "crc_valid": True},
    {"command": "WriteA32", "has_manager_packet": True, "crc_valid": False},
    {"command": "Mystery"},
]


def _reference_ticks(r):
    """The pre-3.0.19 _decimated_ticks body, verbatim."""
    drawn_px = set()
    ticks = []
    for cmd in reversed(r._draw_order):
        s = cmd.get("start_sample", 0)
        if r._draw_set is not None and int(s) not in r._draw_set:
            continue
        if not r._in_view(s):
            continue
        x = int(r._x_for(s))
        if x in drawn_px:
            continue
        drawn_px.add(x)
        ticks.append((x, cmd))
    return ticks


def _reference_commit_points(r):
    """The pre-3.0.19 _decimated_commit_points body, verbatim."""
    seen_px = set()
    xs = []
    for cp in r._commit_points:
        if not r._in_view(cp):
            continue
        x = int(r._x_for(cp))
        if x in seen_px:
            continue
        seen_px.add(x)
        xs.append(x)
    return xs


def _ribbon(seed, n, total, width=900):
    QApplication.instance() or QApplication([])
    rng = random.Random(seed)
    cmds = []
    for i in range(n):
        start = rng.randrange(total) if rng.random() > 0.05 else rng.choice((0, total // 2))
        cmd = dict(rng.choice(_KINDS), start_sample=start, bus_row=i)
        if rng.random() < 0.01:
            del cmd["start_sample"]                       # a missing start counts as 0
        cmds.append(cmd)                                  # NOT in time order
    r = TimelineRibbon()
    r.resize(width, TimelineRibbon.BAND_HEIGHT)
    r.set_events(cmds, total)
    r.set_commit_points(rng.sample(range(total), min(total, n // 10)))
    return r, cmds, rng


def _views(rng, lo, hi):
    yield lo, hi                                          # the whole domain
    for _ in range(25):
        a = rng.uniform(lo, hi)
        span = (hi - lo) * rng.choice((0.5, 0.1, 1e-3, 1e-5))
        yield a, a + span
    yield lo, lo + 16.0                                   # the zoom-in limit, at an edge
    yield hi - 16.0, hi


def _same(r):
    got = r._decimated_ticks()
    want = _reference_ticks(r)
    assert [(x, id(c)) for x, c in got] == [(x, id(c)) for x, c in want]
    assert r._decimated_commit_points() == _reference_commit_points(r)


@pytest.mark.parametrize("seed", range(6))
def test_the_decimation_equals_the_old_loop(seed):
    r, _cmds, rng = _ribbon(seed, n=3000, total=5_000_000)
    for lo, hi in _views(rng, 0.0, 5_000_000.0):
        r.set_view(lo, hi)
        _same(r)


@pytest.mark.parametrize("seed", range(3))
def test_the_decimation_equals_the_old_loop_under_a_filter(seed):
    r, cmds, rng = _ribbon(100 + seed, n=3000, total=5_000_000)
    shown = [c.get("start_sample", 0) for c in cmds if rng.random() < 0.3]
    r.set_draw_filter(shown)
    for lo, hi in _views(rng, 0.0, 5_000_000.0):
        r.set_view(lo, hi)
        _same(r)
    r.set_draw_filter([])                                 # a filter that shows nothing
    r.set_view(0.0, 5_000_000.0)
    assert r._decimated_ticks() == [] and _reference_ticks(r) == []
    r.set_draw_filter(None)                               # and back to everything
    _same(r)


def test_the_decimation_equals_the_old_loop_in_a_negative_domain():
    """A stack of Links gives a later Link's ribbon a domain starting below its sample 0."""
    r, _cmds, rng = _ribbon(7, n=2000, total=2_000_000)
    r.set_domain(-1_500_000, 2_000_000)
    for lo, hi in _views(rng, -1_500_000.0, 2_000_000.0):
        r.set_view(lo, hi)
        _same(r)


def test_widths_and_an_empty_ribbon():
    r, _cmds, _rng = _ribbon(8, n=500, total=100_000, width=37)
    _same(r)
    r.resize(2400, TimelineRibbon.BAND_HEIGHT)
    _same(r)
    empty = TimelineRibbon()
    empty.resize(500, TimelineRibbon.BAND_HEIGHT)
    assert empty._decimated_ticks() == [] and empty._decimated_commit_points() == []
    empty.set_events([], 1000)
    assert empty._decimated_ticks() == []
