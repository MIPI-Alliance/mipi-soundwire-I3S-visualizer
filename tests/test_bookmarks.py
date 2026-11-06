"""BookmarkSet: stable A1/A2/B1/B2 pairing, drag stability, legacy load, pair deltas."""
from swi3s_studio.model.bookmarks import BookmarkSet, _group_index, _group_name


def test_add_sequences_into_pairs():
    s = BookmarkSet()
    labels = [s.add(x * 10).display() for x in range(6)]
    assert labels == ["A1", "A2", "B1", "B2", "C1", "C2"]
    assert len(s.pairs()) == 3


def test_pair_membership_is_stable_under_reorder():
    """Dragging A2 past B1 (by sample) must NOT re-pair them: group identity is fixed
    at creation, and pairs() returns members in sample order for a positive delta."""
    s = BookmarkSet()
    s.add(100); a2 = s.add(200); s.add(300); s.add(400)
    s.move(a2, 350)                       # A2 now sits between B1 and B2 by sample
    groups = {g: (lo.display(), hi.display()) for g, lo, hi in s.pairs()}
    assert groups["A"] == ("A1", "A2")    # still paired with A1
    assert groups["B"] == ("B1", "B2")
    # left/right ordered by sample for a positive delta
    a = next(p for p in s.pairs() if p[0] == "A")
    assert a[1].sample == 100 and a[2].sample == 350


def test_a_pair_lists_its_members_left_to_right_whichever_was_placed_first():
    s = BookmarkSet()
    a1, a2 = s.add(100), s.add(50)                 # the second is placed left of the first
    (g, lo, hi), = s.pairs()
    assert (g, lo.display(), hi.display()) == ("A", "A2", "A1")
    assert hi.sample - lo.sample == 50             # a positive delta
    s.move(a2, 400)                                # dragged past the first: the order follows
    (g, lo, hi), = s.pairs()
    assert (lo.display(), hi.display()) == ("A1", "A2") and hi.sample - lo.sample == 300
    s.move(a1, 400)                                # equal samples keep the placement order
    (g, lo, hi), = s.pairs()
    assert (g, lo.display(), hi.display()) == ("A", "A1", "A2")
    assert hi.sample - lo.sample == 0


def test_delete_reopens_group_for_next_add():
    s = BookmarkSet()
    s.add(10); a2 = s.add(20)             # pair A complete
    s.remove(a2)                          # A now incomplete (one member)
    bm = s.add(30)                        # fills A, not a new group B
    assert bm.group == "A" and bm.index == 2
    assert [g for g, _, _ in s.pairs()] == ["A"]


def test_incomplete_pair_excluded_from_measurement():
    s = BookmarkSet()
    s.add(10)                             # A1 only
    assert s.pairs() == []


def test_legacy_int_list_pairs_in_order():
    s = BookmarkSet.from_json([5, 15, 25, 35, 45])
    assert [b.display() for b in s] == ["A1", "A2", "B1", "B2", "C1"]
    assert len(s.pairs()) == 2            # last single is unpaired


def test_json_roundtrip_preserves_groups_and_labels():
    s = BookmarkSet()
    s.add(10); s.add(20)
    s.items[0].label = "start"
    d = s.to_json()
    s2 = BookmarkSet.from_json(d)
    assert s2.items[0].display() == "start"
    assert (s2.items[1].group, s2.items[1].index) == ("A", 2)


def test_group_name_and_index_are_inverse():
    for n in (0, 1, 25, 26, 27, 51, 52, 700):
        assert _group_index(_group_name(n)) == n


def test_remove_at_tolerance():
    s = BookmarkSet()
    s.add(1000)
    assert not s.remove_at(1200, tol=50)   # too far
    assert s.remove_at(1010, tol=50)       # within tolerance
    assert len(s) == 0


def test_a_bookmark_keeps_its_link_through_json_and_copy():
    s = BookmarkSet()
    s.add(100, link=0)
    s.add(200, link=1)
    back = BookmarkSet.from_json(s.to_json())
    assert [(b.sample, b.link) for b in back] == [(100, 0), (200, 1)]
    assert [(b.sample, b.link) for b in back.copy()] == [(100, 0), (200, 1)]


def test_a_bookmark_saved_before_links_loads_on_link_0():
    s = BookmarkSet.from_json([{"sample": 5, "group": "A", "index": 1, "label": ""}])
    assert s.items[0].link == 0


def test_remove_at_one_link_leaves_the_other_links_bookmarks():
    """Samples of different Links are not comparable, so a toggle on one Link must never
    remove a bookmark that merely has a nearby sample NUMBER on another."""
    s = BookmarkSet()
    s.add(100, link=1)
    assert not s.remove_at(100, tol=5, link=0)
    assert len(s) == 1
    assert s.remove_at(101, tol=5, link=1) and len(s) == 0


def test_the_next_add_completes_the_earliest_incomplete_group():
    s = BookmarkSet()
    for sample in (10, 20, 30, 40):                  # A1 A2 B1 B2
        s.add(sample)
    assert s.remove_at(20) and s.remove_at(40)       # A and B each lose their second member
    bm = s.add(50)
    assert (bm.group, bm.index) == ("A", 2)
    bm = s.add(60)
    assert (bm.group, bm.index) == ("B", 2)
    assert s.add(70).group == "C"


def test_remove_at_a_distance_of_exactly_the_tolerance_removes_and_one_more_does_not():
    s = BookmarkSet()
    s.add(100)
    assert not s.remove_at(106, tol=5)
    assert len(s) == 1
    assert s.remove_at(105, tol=5)
    assert len(s) == 0


def test_a_json_boolean_is_not_a_legacy_sample():
    s = BookmarkSet.from_json([True, 5, False])
    assert s.samples() == [5]


def test_bookmarks_read_each_item_by_its_own_form():
    """A mapping is a bookmark, a bare finite number a legacy sample; any other item, and a
    mapping whose fields int() rejects, is skipped; fields int() accepts are truncated."""
    s = BookmarkSet.from_json([5, {"sample": 7, "group": "B", "index": 1}, float("inf"),
                               "x", {"group": "C"}, {"sample": "q"}, {"sample": 12.7}])
    assert [(b.sample, b.group, b.index) for b in s.items] == [
        (5, "A", 1), (7, "B", 1), (12, "A", 1)]
