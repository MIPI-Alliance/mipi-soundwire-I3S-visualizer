"""What a MainWindow shows must follow from its Links. `check_window` recomputes it.

A test that checks the field it just set passes when the window shows something stale: a
reopened workspace had every Link's offset right while every band but the first was drawn
at offset 0, and an offset change left the remembered cursor instant at the old offset.
Neither is visible in the field, only in what was derived from it. So this derives each
displayed quantity again, from the Links alone, and compares it with what is on screen.
Call it after anything that changes the Links: a round-trip, an offset, a removal.
"""
from __future__ import annotations


def expected_band_bounds(win) -> list:
    """Each band's range in its own samples: the union of every Link's span in global time
    when there are two or more Links, else the capture itself."""
    links, ribbons = list(win.links), win._ribbons
    if len(links) < 2:
        return [(0.0, float(max(1, r._total))) for r in ribbons[:len(links)]]
    spans = [(link.to_ps(0), link.to_ps(r._total)) for link, r in zip(links, ribbons)]
    lo, hi = min(a for a, _ in spans), max(b for _, b in spans)
    out = []
    for link in links:
        a, b = float(link.to_sample(lo)), float(link.to_sample(hi))
        out.append((a, max(b, a + 1)))
    return out


def check_window(win) -> None:
    links = list(win.links)
    n = len(links)
    a = win.links.active_index
    assert n == 0 or 0 <= a < n, f"active Link {a} of {n}"
    if n == 0:
        return

    # One band per Link, each over the range the Links' spans and offsets give.
    assert len(win._ribbons) >= n, f"{len(win._ribbons)} bands for {n} Links"
    got = [r._bounds() for r in win._ribbons[:n]]
    assert got == expected_band_bounds(win), (
        f"band ranges {got} are not the ones the Links give {expected_band_bounds(win)}")

    # The cursor is inside the active Link's capture.
    last = max(0, int(links[a].session.last_sample()))
    assert 0 <= win.cursor.sample <= last, f"cursor {win.cursor.sample} outside 0..{last}"

    # A remembered cursor instant that still applies must be the instant the cursor shows.
    memo = win._cursor_instant
    if memo is not None and memo[:2] == (a, win.cursor.sample):
        shown = min(max(0, links[a].to_sample(memo[2])), last)
        assert shown == win.cursor.sample, (
            f"the remembered instant {memo[2]} ps is sample {shown} on {links[a].name}, but "
            f"the cursor is at {win.cursor.sample}: it was not dropped when the mapping moved")

    # Everything that names a Link by index names one that exists.
    for b in win._bookmarks:
        assert 0 <= b.link < n, f"a bookmark on Link {b.link} of {n}"
    for group, i in win._pane_link.items():
        assert 0 <= i < n, f"pane group {group!r} shows Link {i} of {n}"
    assert [act.text() for act in win._link_actions] == [link.name for link in links]
    combo = [win._link_combo.itemText(i) for i in range(win._link_combo.count())]
    assert combo[:n] == [link.name for link in links], combo
    if win._cmd_all:
        model = win._all_model
        assert {model.link_at(r) for r in range(model.rowCount())} <= set(range(n))


def snapshot(win) -> dict:
    """What a saved workspace promises to bring back, as displayed."""
    return {
        "links": [(link.name, link.offset_ps) for link in win.links],
        "bands": [r._bounds() for r in win._ribbons[:len(win.links)]],
        "bookmarks": sorted((b.sample, b.link, b.group, b.index) for b in win._bookmarks),
        "active": win.links.active_index,
        "cursor": win.cursor.sample,
    }
