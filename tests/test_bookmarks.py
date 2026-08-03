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
