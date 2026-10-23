"""The timeline's tick and commit-point decimation equals a per-command reference loop.

The decimation is a bisect to the view plus a vectorised per-column maximum. The reference
below is one Python iteration per command, and the decimation must equal it exactly — which
command wins each pixel column AND the order of the list — over random commands of every
paint rank, listed out of time order, with colliding and duplicate starts, under a filter,
at many zooms, and in a negative domain (a stack of Links puts a later Link's view below its
sample 0).
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
    """Reference for _decimated_ticks: one iteration per command."""
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
    """Reference for _decimated_commit_points: one iteration per commit point."""
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


def test_every_filter_change_between_two_paints_is_drawn():
    """set_draw_filter replaces its set on every call, so several calls between two paints
    each have to reach the ticks: Clear All Filters pushes the filter more than once."""
    r, cmds, rng = _ribbon(200, n=3000, total=5_000_000)
    starts = [c.get("start_sample", 0) for c in cmds]
    _same(r)
    for _ in range(10):
        r.set_draw_filter([s for s in starts if rng.random() < 0.3])
        _same(r)
        r.set_draw_filter(None)
        r.set_draw_filter([s for s in starts if rng.random() < 0.5])
        _same(r)
        r.set_draw_filter([s for s in starts if rng.random() < 0.1])
        r.set_draw_filter(None)
        _same(r)


def test_every_events_and_commit_points_update_between_two_paints_is_drawn():
    """set_events and set_commit_points replace their lists too: from one to eight updates
    between two paints, the last one is what is drawn."""
    r, _cmds, rng = _ribbon(201, n=2000, total=1_000_000)
    events = [list(_ribbon(300 + i, n=2000, total=1_000_000)[1]) for i in range(4)]
    points = [rng.sample(range(1_000_000), 150 - 30 * i) for i in range(4)]
    for k in range(1, 9):
        _same(r)
        for j in range(k):
            r.set_events(events[j % 4], 1_000_000)
            r.set_commit_points(points[j % 4])
        _same(r)
