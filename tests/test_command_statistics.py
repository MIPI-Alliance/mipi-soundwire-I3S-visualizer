"""Expandable per-command statistics.

The Statistics pane's Commands section used to be one flat count per command kind.
Each kind is now a collapsible GROUP whose detail rows carry the breakdown:

  * Ping      -> per-peripheral response states (PING_ATTACHED, PING_ATTACHED_BUSY,
                 PING_ALERT, PING_ALERT_BUSY, NO_RESPONSE)
  * Read/Write -> per-device command count, bytes read/written, and error count

That needed a SECOND fold level in MeasurementsView: a detail row is visible only when
both its group is expanded and its section is not collapsed. Groups start collapsed, so
the section still reads as one line per command kind until you ask for more.
"""
import pytest

from swi3s_studio.analysis.measurements import capture_measurements
from swi3s_studio.session import Session


@pytest.fixture(scope="module")
def rows():
    return capture_measurements(Session.from_demo(300, phy=2, cold_start=True))


@pytest.fixture
def view(rows):
    from PySide6.QtWidgets import QApplication

    from swi3s_studio.ui.measurements_view import MeasurementsView

    QApplication.instance() or QApplication([])
    v = MeasurementsView()
    v.set_rows(rows)
    return v


def _kind(row):
    return row[2] if len(row) > 2 else None


def _section(rows, title):
    """The rows belonging to one section header, exclusive of the next header."""
    out, inside = [], False
    for r in rows:
        if _kind(r) == "header":
            inside = (r[0] == title)
            continue
        if inside:
            out.append(r)
    return out


def _group_block(rows, label):
    """A group row plus its following detail rows."""
    cmds = _section(rows, "Commands")
    for i, r in enumerate(cmds):
        if r[0].strip() == label and _kind(r) == "group":
            block = []
            for nxt in cmds[i + 1:]:
                if _kind(nxt) != "detail" and _kind(nxt) != "fail":
                    break
                block.append(nxt)
            return r, block
    return None, []


# --------------------------------------------------------------------------
# The rows themselves
# --------------------------------------------------------------------------

def test_ping_is_an_expandable_group(rows):
    group, detail = _group_block(rows, "Ping")
    assert group is not None, "Ping should be a collapsible group"
    assert detail, "Ping group has no detail rows"


def test_ping_detail_breaks_down_by_peripheral_and_state(rows):
    """The PHY2 demo has exactly ONE peripheral (device 0), which answers
    PING_ATTACHED. The other eleven aren't on the bus, so they leave the PingInfo field
    undriven and collapse to a single NO_RESPONSE line each rather than five states."""
    _group, detail = _group_block(rows, "Ping")
    text = [(r[0].strip(), r[1]) for r in detail]
    labels = [t[0] for t in text]
    assert "Device 0" in labels
    dev0 = labels.index("Device 0")
    assert labels[dev0 + 1] == "PING_ATTACHED", text[dev0:dev0 + 2]
    # An absent peripheral is one compact row, and it says NO_RESPONSE.
    absent = [t for t in text if t[0] == "Device 1"]
    assert absent and "NO_RESPONSE" in absent[0][1], text[:6]


def test_ping_detail_shows_an_alert_on_a_present_peripheral():
    """The flow-control demo has four peripherals and device 3 raises PING_ALERT, so the
    ALERT decode path is exercised on a device that is genuinely on the bus."""
    s = Session.from_demo(300, phy=2, variant="flow_control")
    _group, detail = _group_block(capture_measurements(s), "Ping")
    labels = [r[0].strip() for r in detail]
    assert "Device 3" in labels, labels
    assert labels[labels.index("Device 3") + 1] == "PING_ALERT", labels


def test_ping_detail_does_not_invent_attached_peripherals():
    """The regression this fixes: the demo used to drive PING_ATTACHED for all twelve
    peripherals, so the breakdown claimed twelve attached devices on a bus that
    configures one. Only devices the demo actually has may report ATTACHED."""
    for kw, present in [(dict(phy=2, cold_start=True), {0}),
                        (dict(phy=1, cold_start=True), {0}),
                        (dict(phy=3, cold_start=True), {0}),
                        (dict(phy=2, variant="flow_control"), {0, 1, 2, 3})]:
        s = Session.from_demo(200, **kw)
        _group, detail = _group_block(capture_measurements(s), "Ping")
        attached = {int(lbl.split()[1])
                    for i, (lbl, _v) in enumerate((r[0].strip(), r[1]) for r in detail)
                    if lbl.startswith("Device ")
                    and any(d[0].strip().startswith(("PING_ATTACHED", "PING_ALERT"))
                            for d in detail[i + 1:i + 2])}
        assert attached == present, f"{kw}: responding devices {attached}, expected {present}"


def test_ping_states_are_the_documented_five(rows):
    """Every state row must be a real GetStatus response name — a bare token number
    would mean the mapping fell through."""
    from swi3s_studio.analysis.measurements import _PING_STATES

    _group, detail = _group_block(rows, "Ping")
    states = [r[0].strip() for r in detail if not r[0].strip().startswith("Device")]
    assert states
    for s in states:
        assert s in _PING_STATES, f"unexpected ping state row {s!r}"


def test_write_detail_reports_per_device_counts_and_bytes(rows):
    """Writes break down per device with a byte total taken from the payload actually
    on the wire."""
    group, detail = _group_block(rows, "WriteA32")
    assert group is not None
    labels = [r[0].strip() for r in detail]
    assert any(l.startswith("Device ") for l in labels), labels
    assert "bytes written" in labels, labels
    # The demo writes only device 0, and the byte total must be positive and consistent
    # with the per-device command count being the same as the group's total.
    nbytes = next(int(r[1].replace(",", "")) for r in detail
                  if r[0].strip() == "bytes written")
    assert nbytes > 0
    dev_count = next(int(r[1].replace(",", "")) for r in detail
                     if r[0].strip().startswith("Device "))
    assert dev_count == int(group[1])


def test_write_byte_total_matches_the_decoded_payloads():
    """Independent check of the arithmetic: the reported byte total must equal the sum
    of the decoded Write payload lengths."""
    s = Session.from_demo(300, phy=2, cold_start=True)
    expected = sum(len(c.get("data") or b"") for c in s.commands
                   if c.get("command") == "WriteA32")
    _group, detail = _group_block(capture_measurements(s), "WriteA32")
    got = next(int(r[1].replace(",", "")) for r in detail
               if r[0].strip() == "bytes written")
    assert got == expected


def test_kinds_without_a_breakdown_are_not_groups(rows):
    """An arrow that expands to nothing is worse than no arrow: SSCR/SSPA have no
    per-device breakdown, so they stay plain rows."""
    cmds = _section(rows, "Commands")
    for r in cmds:
        if r[0].strip() in ("SSCR", "SSPA"):
            assert _kind(r) != "group", f"{r[0].strip()} should not be collapsible"


def test_flat_totals_are_still_present(rows):
    """The breakdown is additive — the section keeps its Count / Errors / SSP count."""
    labels = [r[0] for r in _section(rows, "Commands")]
    for want in ("Count", "Errors", "SSP count"):
        assert want in labels, labels


def test_per_kind_counts_match_the_commands():
    """The group row's own value must still be the plain count for that kind."""
    from collections import Counter

    s = Session.from_demo(300, phy=2, cold_start=True)
    want = Counter(c.get("command", "") for c in s.commands)
    for r in _section(capture_measurements(s), "Commands"):
        label = r[0].strip()
        if label in want and _kind(r) in ("group", None) and r[0].startswith("  "):
            assert int(r[1]) == want[label], (label, r[1], want[label])


# --------------------------------------------------------------------------
# The two-level fold in the view
# --------------------------------------------------------------------------

def test_groups_start_collapsed(view):
    assert view._groups, "no groups registered"
    for gr, details in view._groups.items():
        assert view.item(gr, 0).text().startswith("▸"), "group should start collapsed"
        for dr in details:
            assert view.isRowHidden(dr)


def test_clicking_a_group_expands_only_that_group(view):
    grs = sorted(view._groups)
    assert len(grs) >= 2, "need two groups to prove isolation"
    first, second = grs[0], grs[1]
    view._on_cell_clicked(first, 0)
    assert all(not view.isRowHidden(d) for d in view._groups[first])
    assert all(view.isRowHidden(d) for d in view._groups[second]), \
        "expanding one group must not expand another"
    assert view.item(first, 0).text().startswith("▾")


def test_clicking_a_group_again_collapses_it(view):
    gr = sorted(view._groups)[0]
    view._on_cell_clicked(gr, 0)
    view._on_cell_clicked(gr, 0)
    assert all(view.isRowHidden(d) for d in view._groups[gr])


def test_folding_the_section_hides_expanded_details(view):
    """A detail row needs BOTH its group expanded and its section open. Folding the
    section while a group is expanded must not leave orphaned rows on screen."""
    gr = sorted(view._groups)[0]
    view._on_cell_clicked(gr, 0)                     # expand the group
    assert any(not view.isRowHidden(d) for d in view._groups[gr])
    header = view._group_owner[gr]
    view._on_cell_clicked(header, 0)                 # fold the whole section
    assert all(view.isRowHidden(d) for d in view._groups[gr])


def test_reopening_the_section_restores_the_group_state(view):
    """Fold state survives a section fold/unfold round trip."""
    gr = sorted(view._groups)[0]
    header = view._group_owner[gr]
    view._on_cell_clicked(gr, 0)                     # expand
    view._on_cell_clicked(header, 0)                 # fold section
    view._on_cell_clicked(header, 0)                 # unfold section
    assert all(not view.isRowHidden(d) for d in view._groups[gr])


def test_group_state_survives_a_repopulate(view, rows):
    """A cursor move / re-decode re-populates the table; the user's expansion must not
    be thrown away (the same guarantee sections already had)."""
    gr = sorted(view._groups)[0]
    title = view.item(gr, 0).data(256) or view.item(gr, 0).text()
    view._on_cell_clicked(gr, 0)
    view.set_rows(rows)
    same = [r for r in view._groups if (view.item(r, 0).data(256) or "") == title]
    assert same, "group vanished after repopulate"
    assert all(not view.isRowHidden(d) for d in view._groups[same[0]])


def test_section_headers_still_fold(view):
    """The original single-level behaviour is intact."""
    header = sorted(view._sections)[0]
    children = view._sections[header]
    assert children
    view._on_cell_clicked(header, 0)
    assert all(view.isRowHidden(c) for c in children)
    view._on_cell_clicked(header, 0)
    # Detail rows of a collapsed group stay hidden; everything else comes back.
    detail_rows = {d for ds in view._groups.values() for d in ds}
    assert all(not view.isRowHidden(c) for c in children if c not in detail_rows)


# --- a group's own error row must fold WITH the group -----------------------------------
#
# Found by the 3.0.11 release review. `_rw_detail_rows` reports a device's error count as
# kind "fail" (red), but MeasurementsView only adopted kind "detail" into the open group.
# The errors row therefore stayed visible under a COLLAPSED group and, worse, ended the
# group — so every following device's rows leaked out too. No fixture reaches it: none of
# the demo captures contains a CRC-errored Read/Write, so the whole suite passed. These
# tests use the row shape directly, and pin the kind-vocabulary contract that was violated.

def _fold_rows():
    """The exact shape _rw_detail_rows emits when device 0 has errors and device 1 follows."""
    return [("Commands", "", "header"),
            ("  Write", "12", "group"),
            ("    Device 0", "8", "detail"),
            ("      bytes written", "32", "detail"),
            ("      errors", "3", "fail"),
            ("    Device 1", "4", "detail"),
            ("      bytes written", "16", "detail")]


def test_a_groups_error_row_folds_with_the_group(view):
    """A collapsed group hides its error row, and the device after it, like any child."""
    rows = _fold_rows()
    view.set_rows(rows)                      # groups start collapsed
    hidden = [view.isRowHidden(r) for r in range(len(rows))]
    assert hidden[2:] == [True] * 5, (
        "a collapsed group must hide every child, including the 'fail' errors row and the "
        f"device after it; got hidden={hidden}")


def test_an_error_row_does_not_orphan_the_devices_after_it(view):
    """The errors row must not end the group — rows 5-6 belong to it as much as 2-3 do."""
    rows = _fold_rows()
    view.set_rows(rows)
    members = view._groups[1]
    assert members == [2, 3, 4, 5, 6], (
        f"group should own every child row; got {members} (missing rows leak out of the fold)")


def test_every_detail_kind_is_adopted_by_its_group():
    """The contract the leak broke: whatever kinds the detail builders emit must all be
    kinds MeasurementsView folds. Guards the seam rather than one row shape, so adding a
    new styled child kind can't silently reintroduce this."""
    from swi3s_studio.analysis import measurements as m
    from swi3s_studio.ui.measurements_view import _GROUPED_KINDS

    cmds = [{"command": "WriteA32", "device": 0, "phase": "Write", "crc_valid": False,
             "payload": [1, 2, 3], "is_error": True},
            {"command": "WriteA32", "device": 1, "phase": "Write", "crc_valid": True,
             "payload": [4, 5], "is_error": False}]
    emitted = {_kind(r) for r in m._rw_detail_rows(cmds, reading=False)}
    emitted |= {_kind(r) for r in m._ping_detail_rows([])}
    stray = {k for k in emitted if k is not None} - set(_GROUPED_KINDS)
    assert not stray, (
        f"detail builders emit kind(s) {stray} that MeasurementsView will not fold into the "
        f"group (it folds {_GROUPED_KINDS}) — those rows would stay visible when collapsed")
