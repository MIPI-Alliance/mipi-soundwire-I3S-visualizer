"""File ▸ Open Demo Capture: each entry opens its demo with its bundled workspace
(data/demos/*.swi3s, workspace.DEMO_WORKSPACES), applied as Open Workspace applies one.

The shipped files hold only built-in demo sources and the view: no window geometry, no
Commands sort, no Visualizer or Timing settings, and no paths.
"""
import json
import os
import re

import pytest
from PySide6.QtWidgets import QApplication

from swi3s_studio.ingest.transitions import DEMO_LINK2_DELAY_SAMPLES
from swi3s_studio.workspace import DEMO_WORKSPACES, Workspace, demo_dir, source_files

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_MENU = [("PHY1", "phy1"), ("PHY2", "phy2"), ("PHY2 (Flow Control)", "phy2_flow_control"),
         ("PHY3 (DLV)", "phy3")]


def _doc(key):
    with open(os.path.join(demo_dir(), DEMO_WORKSPACES[key]), encoding="utf-8") as f:
        return json.load(f)


def _names(d):
    """{(device, dp): name} of a saved Link's source."""
    return {tuple(int(x) for x in k.split(".")): v
            for k, v in (d["source"].get("dp_names") or {}).items()}


def _menu(win):
    from PySide6.QtWidgets import QMenu
    menu = next(m for m in win.findChildren(QMenu) if m.title() == "Open &Demo Capture")
    return {a.text(): a for a in menu.actions() if a.text()}


@pytest.fixture
def win():
    from swi3s_studio.ui.main_window import MainWindow
    QApplication.instance() or QApplication([])
    w = MainWindow()
    w.resize(1400, 900)
    w.show()
    return w


def _open(win, label):
    _menu(win)[label].trigger()
    QApplication.processEvents()


# ---------------------------------------------------------------- the menu
def test_the_menu_has_one_entry_per_bundled_workspace(win):
    assert list(_menu(win)) == [label for label, _k in _MENU]
    assert sorted(k for _l, k in _MENU) == sorted(DEMO_WORKSPACES)


@pytest.mark.parametrize("label", ["PHY1", "PHY2", "PHY3 (DLV)"])
def test_a_demo_opens_with_its_data_port_names(win, label):
    _open(win, label)
    assert win.links[0].session.dp_names[(0, 0)] == "AMP1"
    key = dict(_MENU)[label]
    saved = _doc(key)["links"][0]
    assert win.links[0].session.dp_names == _names(saved)
    assert win.links[0].session.region_names == {
        int(k): v for k, v in saved["source"]["region_names"].items()}


def test_phy2_opens_two_links_named_as_their_demos(win):
    _open(win, "PHY2")
    assert [link.name for link in win.links] == ["Link 1", "Link 2"]
    fc = _doc("phy2_flow_control")["links"][0]
    phy2 = _doc("phy2")["links"]
    assert win.links[1].session.dp_names == _names(fc)
    assert win.links[1].session.dp_names[(0, 0)] == "No Flow Control"
    assert win.links[0].session.dp_names == _names(_doc("phy2")["links"][0])
    src = win.links[1].session.source
    assert src["variant"] == "flow_control" and src["delay_samples"] == DEMO_LINK2_DELAY_SAMPLES
    assert phy2[1]["view"] == fc["view"]                  # Link 2 carries the flow-control view
    assert win.links.active_index == 0
    assert all(i == 0 for i in win._pane_link.values())


def test_the_flow_control_demo_opens_with_its_names(win):
    _open(win, "PHY2 (Flow Control)")
    assert len(win.links) == 1
    assert win.links[0].session.dp_names == _names(_doc("phy2_flow_control")["links"][0])


def test_a_demo_applies_its_views(win):
    """The Commands filter, the Audio channels, the Registers filter and the hidden
    Commands columns are the workspace's (those the short test demo has); the rows are in
    capture order."""
    from swi3s_studio.model import Provenance
    _open(win, "PHY1")
    d = _doc("phy1")
    view = d["links"][0]["view"]
    kinds = set(win.links[0].view.kinds)                # what the (short) test demo has
    assert win._filter_snapshot()["kinds"] == set(view["commands"]["filters"]["kinds"]) & kinds
    assert win._filter_snapshot()["kinds"] < kinds
    want = {tuple(c) for c in view["audio"]["channels"]}
    assert win._audio_view.channel_selection() == want & set(win._audio_view._checks)
    assert win._reg_view.hidden_provenances() == {
        Provenance(v) for v in d["view"]["registers"]["hidden"]}
    hidden = {h.replace("\n", " ") for h in d["view"]["commands_header"]["hidden"]}
    assert win._cmd_view.hidden_names() == hidden
    assert win._cmd_proxy.sortColumn() == -1


def test_a_demo_loads_through_the_async_queue(win, monkeypatch):
    from conftest import pump_loads

    from swi3s_studio.ui.main_window import MainWindow
    monkeypatch.setattr(MainWindow, "_event_loop_live", True)
    _open(win, "PHY2")
    pump_loads(win)
    QApplication.processEvents()
    assert [link.session.dp_names.get((0, 0)) for link in win.links] == [
        "AMP1", "No Flow Control"]


# ---------------------------------------------------------------- the shipped files
@pytest.mark.parametrize("key", sorted(DEMO_WORKSPACES))
def test_a_shipped_workspace_holds_only_the_demo_and_its_view(key):
    from swi3s_studio import import_notes
    d = _doc(key)
    assert "geometry" not in d["view"]["window"] and d["view"]["window"]["docks"]
    assert d["view"]["commands_header"]["sort"] is None
    assert "authoring" not in d and "timing" not in d
    for link in d["links"]:
        assert link["source"]["type"] == "demo" and not source_files(link["source"])
        assert "channel_list_width" not in link["view"]["audio"]
    text = json.dumps(d)
    assert not re.search(r"(/Users/|/home/|[A-Za-z]:\\\\|\.sal|\.csv)", text)
    with import_notes.collect() as notes:
        Workspace.from_json(json.dumps(d), key)
    assert not list(notes)


def test_the_shipped_workspaces_are_packaged():
    """pip data-files and the PyInstaller spec both carry every file demo_dir reads."""
    with open(os.path.join(_ROOT, "pyproject.toml"), encoding="utf-8") as f:
        pyproject = f.read()
    with open(os.path.join(_ROOT, "swi3s-studio.spec"), encoding="utf-8") as f:
        spec = f.read()
    assert os.path.normpath(demo_dir()) == os.path.join(_ROOT, "data", "demos")
    assert sorted(os.listdir(demo_dir())) == sorted(DEMO_WORKSPACES.values())
    for name in DEMO_WORKSPACES.values():
        assert f'"data/demos/{name}"' in pyproject, name
    assert '"share/swi3s-studio/demos"' in pyproject
    assert 'os.path.join(HERE, "data", "demos")' in spec
