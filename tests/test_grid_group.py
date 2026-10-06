"""The Bus Grid is a pane group of its own.

It used to be in the bottom group with Capture, CDS, Audio, Samples and Timing, which can
come to show every Link at once; the grid cannot (two grids do not fit across), so it got a
group, a picker and a Link of its own. The grid's code reads the ACTIVE Link's session and
cursor, so every way in to it (a cursor move, a bind, its own toolbar, the dock being raised,
its menu actions, a theme switch) has to run on the grid's Link. These pin each, by recording
which Session is in force when the grid renders.
"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from test_links_gui import _with_added_link


@pytest.fixture
def two():
    """Two Links, every group on Link 2; then the grid moved to Link 1 alone."""
    win, first, second = _with_added_link()
    win._grid_dock.show()
    win.set_group_link("grid", 0)
    win.set_group_link("bottom", 1)                   # active: Link 2, the bottom group's
    assert win._pane_link == {"commands": 1, "right": 1, "grid": 0, "bottom": 1}
    assert win.links.active_index == 1
    win.cursor.set_sample(int(second.commands[len(second.commands) // 2]["start_sample"]))
    return win, first, second


def _spy(monkeypatch, win):
    """Record the Session in force each time the grid renders or sizes its TX scrollbar:
    the Link the grid code actually read. (Spying on the Session would also catch the
    Timing pane, which asks for grid cells of its own.)"""
    asked = []
    for name in ("_apply_grid_for_sample", "_update_tx_scrollbar"):
        real = getattr(win, name)

        def spy(*a, _r=real, **k):
            asked.append(win._session)
            return _r(*a, **k)
        monkeypatch.setattr(win, name, spy)
    return asked


def test_the_grid_has_its_own_picker_and_link(two):
    win, first, second = two
    assert win._group_docks["grid"] == [win._grid_dock]
    assert win._grid_dock not in win._group_docks["bottom"]
    (picker,) = win._pane_pickers["grid"]
    assert picker.currentIndex() == 0 and not picker.isHidden()
    assert all(p.currentIndex() == 1 for p in win._pane_pickers["bottom"])
    assert win._audio_view._store is win.links[1].view.audio_store     # bottom unmoved


def test_a_cursor_move_draws_the_grid_s_link(two, monkeypatch):
    win, first, second = two
    asked = _spy(monkeypatch, win)
    win.cursor.set_sample(int(second.commands[-2]["start_sample"]))   # somewhere new
    assert asked and set(asked) == {first}


@pytest.mark.parametrize("handler", ["rows", "tx_map", "persist", "scroll", "raise",
                                     "theme", "compare"])
def test_every_grid_handler_draws_the_grid_s_link(two, monkeypatch, handler):
    win, first, second = two
    if handler in ("persist", "scroll"):
        win._toggles_btn.setChecked(True)             # both act inside the TX map
    asked = _spy(monkeypatch, win)
    if handler == "rows":
        win._grid_rows_edit.setText(str(win._grid_rows + 8))
        win._on_grid_rows_edit()
    elif handler == "tx_map":
        win._toggles_btn.setChecked(not win._toggles_btn.isChecked())
    elif handler == "persist":
        win._persist_btn.setChecked(True)
    elif handler == "scroll":
        win._on_tx_scroll(3)
    elif handler == "raise":
        win._on_grid_dock_visible(True)
    elif handler == "theme":
        monkeypatch.setattr("swi3s_studio.ui.main_window.save_preference", lambda _p: None)
        win.apply_theme("light")
    elif handler == "compare":
        win.clear_comparison()
    if handler != "persist":                          # persistence renders off-thread
        assert asked, handler
    assert set(asked) <= {first}, handler
    assert win.links.active_index == 1                # and the active Link is unchanged


def test_the_grid_s_menu_actions_act_on_the_grid_s_link(two):
    win, first, second = two
    ran = []
    win._for_group("grid", lambda: ran.append(win._session))()
    assert ran == [first]


def test_a_bottom_pick_leaves_the_grid_alone(two, monkeypatch):
    win, first, second = two
    asked = _spy(monkeypatch, win)
    win.set_group_link("bottom", 0)
    win.set_group_link("bottom", 1)
    assert set(asked) <= {first}
    assert win._pane_link["grid"] == 0


def test_a_layout_saved_before_the_split_puts_the_grid_on_the_bottom_s_link(two):
    win, first, second = two
    win.set_group_link("grid", 1)
    win._apply_link_view_prefs({"pane_links": {"commands": 1, "right": 1, "bottom": 0}}, 1)
    assert win._pane_link["grid"] == 0 == win._pane_link["bottom"]
