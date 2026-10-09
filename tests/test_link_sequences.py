"""Random sequences of Link operations, with the window re-derived after every step.

Each operation is tested on its own elsewhere. What those tests cannot see is an operation
leaving state that a LATER one trusts: an offset change left the remembered cursor instant
at the old offset, and only the next Link activation, putting the cursor back by the shift,
showed it. A seeded sequence of offsets, activations, cursor moves, bookmark alignments,
removals, additions and pane-scope toggles reaches orders like that without anyone having
to think of them, and `check_window` after each step says which step broke what. A failure
names its seed and step, so it replays exactly.
"""
import os
import random

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from conftest import pump_loads
from PySide6.QtWidgets import QApplication
from window_checks import check_window, snapshot

_SEEDS = (1, 2, 3)
_STEPS = 40


def _session(win, variant=""):
    from swi3s_studio.session import Session
    return Session.from_demo(300, cold_start=True, phy=2, variant=variant,
                             register_map=win._rmap)


def _window():
    QApplication.instance() or QApplication([])
    from swi3s_studio.ui.main_window import MainWindow
    win = MainWindow()
    win.load_session(_session(win))
    win.load_session(_session(win, "flow_control"), add_link=True)
    win.load_session(_session(win), add_link=True)
    return win


def _last(win, i):
    return max(0, int(win.links[i].session.last_sample()))


def _offset(win, rng):
    i = rng.randrange(len(win.links))
    win.set_link_offset(i, rng.randint(-3_000_000_000, 3_000_000_000))     # within ±3 ms
    return f"offset Link {i + 1}"


def _activate(win, rng):
    i = rng.randrange(len(win.links))
    win._activate_link(i)
    return f"activate Link {i + 1}"


def _cursor(win, rng):
    win.cursor.set_sample(rng.randint(0, _last(win, win.links.active_index)))
    return "move the cursor"


def _round_trip(win, rng):
    """Activating another Link and coming back, without moving the cursor, returns it to
    the same sample: the instant is kept across the clamp into a shorter capture."""
    a = win.links.active_index
    b = rng.choice([i for i in range(len(win.links)) if i != a])
    before = win.cursor.sample
    win._activate_link(b)
    win._activate_link(a)
    assert win.cursor.sample == before, (
        f"Link {a + 1} → Link {b + 1} → Link {a + 1} moved the cursor {before} → "
        f"{win.cursor.sample}")
    return f"round trip via Link {b + 1}"


def _align(win, rng):
    i, j = rng.sample(range(len(win.links)), 2)
    first = win._bookmarks.add(rng.randint(0, _last(win, i)), link=i)
    second = win._bookmarks.add(rng.randint(0, _last(win, j)), link=j)
    win._push_bookmarks()
    if first.group != second.group:         # the first completed a pair an earlier step left
        return "add two bookmarks"
    win.align_on_bookmark_pair(first.group)
    return f"align Link {j + 1} on Link {i + 1}"


def _remove(win, rng):
    if len(win.links) <= 2:
        win.load_session(_session(win, "flow_control"), add_link=True)
        return "add a Link"
    i = rng.randrange(len(win.links))
    win.remove_link(i, confirm=False)
    return f"remove Link {i + 1}"


def _scopes(win, rng):
    on = rng.random() < 0.5
    if rng.random() < 0.5:
        win.set_bottom_scope_all(on)
        return f"bottom panes All Links {'on' if on else 'off'}"
    win.set_commands_scope_all(on)
    return f"Commands All Links {'on' if on else 'off'}"


_OPS = (_offset, _activate, _cursor, _round_trip, _align, _remove, _scopes)


@pytest.mark.parametrize("seed", _SEEDS)
def test_a_random_sequence_of_link_operations_keeps_the_window_consistent(seed):
    rng = random.Random(seed)
    win = _window()
    check_window(win)
    done = []
    for step in range(_STEPS):
        op = rng.choice(_OPS)
        try:
            done.append(op(win, rng))
            check_window(win)
        except AssertionError as exc:
            raise AssertionError(f"seed {seed}, step {step + 1} ({op.__name__.strip('_')}), "
                                 f"the last steps {done[-5:]}: {exc}") from None


@pytest.mark.parametrize("seed", _SEEDS)
def test_a_workspace_saved_after_a_random_sequence_reopens_as_it_was(seed, tmp_path, monkeypatch):
    """Reopening must bring back what was DISPLAYED, not only the fields saved: every
    Link's name and offset, and also each band's range, which is derived from them."""
    from PySide6.QtWidgets import QFileDialog

    from swi3s_studio.ui.main_window import MainWindow
    rng = random.Random(seed)
    win = _window()
    for _ in range(_STEPS // 2):
        rng.choice((_offset, _activate, _cursor, _align))(win, rng)
    path = str(tmp_path / "ws.json")
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *a, **k: (path, ""))
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *a, **k: (path, ""))
    win.save_workspace()
    before = snapshot(win)

    again = MainWindow()
    again.open_workspace()
    pump_loads(again)
    check_window(again)
    assert snapshot(again) == before
