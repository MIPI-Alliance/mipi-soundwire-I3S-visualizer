"""LinkSet: ordering, the active Link across add/remove, default names, rename, replace."""
import pytest

from swi3s_studio.links import LinkSet


def test_empty_set_has_no_active_link():
    s = LinkSet()
    assert len(s) == 0 and s.active is None and s.active_index == -1


def test_first_add_becomes_active_later_adds_do_not():
    s = LinkSet()
    a, b = object(), object()
    s.add(a)
    s.add(b)
    assert s.active.session is a        # adding a Link never changes what the panels show
    assert [e.name for e in s] == ["Link 1", "Link 2"]


def test_default_name_reuses_the_smallest_free_number():
    s = LinkSet()
    for _ in range(3):
        s.add(object())
    s.remove(1)                         # "Link 2" gone
    assert s.add(object()).name == "Link 2"
    assert s.add(object()).name == "Link 4"


def test_a_renamed_link_frees_its_default_number():
    s = LinkSet()
    s.add(object())
    s.rename(0, "  Speaker bus ")
    assert s[0].name == "Speaker bus"   # trimmed
    assert s.add(object()).name == "Link 1"


def test_rename_rejects_an_empty_name():
    s = LinkSet()
    s.add(object())
    with pytest.raises(ValueError):
        s.rename(0, "   ")
    assert s[0].name == "Link 1"


@pytest.mark.parametrize("active, removed, expect", [
    (2, 0, 1),     # removing an earlier Link: the same Link stays active, one place down
    (0, 2, 0),     # removing a later Link: unaffected
    (1, 1, 1),     # removing the active Link: its successor becomes active
    (2, 2, 1),     # removing the active LAST Link: the new last becomes active
])
def test_remove_keeps_a_sensible_active_link(active, removed, expect):
    s = LinkSet()
    sessions = [object() for _ in range(3)]
    for x in sessions:
        s.add(x)
    s.set_active(active)
    survivor = s.active.session if active != removed else None
    s.remove(removed)
    assert s.active_index == expect
    if survivor is not None:
        assert s.active.session is survivor


def test_removing_the_only_link_empties_the_set():
    s = LinkSet()
    s.add(object())
    s.remove(0)
    assert s.active is None and s.active_index == -1


def test_replace_leaves_one_active_link_with_the_first_default_name():
    s = LinkSet()
    s.add(object())
    s.add(object())
    s.rename(0, "old")
    new = object()
    entry = s.replace(new, view="state")
    assert len(s) == 1 and s.active is entry
    assert entry.session is new and entry.name == "Link 1" and entry.view == "state"


def test_set_active_out_of_range_raises():
    s = LinkSet()
    s.add(object())
    with pytest.raises(IndexError):
        s.set_active(1)


def test_index_of_is_by_identity():
    s = LinkSet()
    a, b = [], []                       # equal values, distinct objects
    s.add(a)
    s.add(b)
    assert s.index_of(b) == 1 and s.index_of(object()) == -1


class _Rate:
    def __init__(self, hz):
        self.sample_rate_hz = hz


@pytest.mark.parametrize("hz", [1, 6_250_000, 500_000_000, 781_250_000.0, 1e9, 3.3333e8])
def test_sample_round_trips_through_global_time(hz):
    """The single-Link invariant rests on this: with offset 0, sample -> ps -> sample is
    the identity, so nothing a single Link shows can move."""
    s = LinkSet()
    link = s.add(_Rate(hz))
    for n in (0, 1, 2, 3, 999_999, 2**31 + 7, 123_456_789_012):
        assert link.to_sample(link.to_ps(n)) == n


def test_offset_shifts_global_time_and_may_be_negative():
    s = LinkSet()
    link = s.add(_Rate(1_000_000))            # 1 µs per sample
    link.offset_ps = -2_500_000               # -2.5 µs
    assert link.to_ps(0) == -2_500_000
    assert link.to_ps(10) == 7_500_000
    assert link.to_sample(0) == 2              # 2.5 samples: round half to even
    assert link.to_sample(link.to_ps(10)) == 10


def test_two_links_at_different_rates_meet_at_the_same_instant():
    s = LinkSet()
    a = s.add(_Rate(500_000_000))              # 2 ns per sample
    b = s.add(_Rate(125_000_000))              # 8 ns per sample
    b.offset_ps = 1_000                        # b started 1 ns later
    t = a.to_ps(4001)                          # 8.002 µs
    assert b.to_sample(t) == 1000             # (8002 ns - 1 ns) / 8 ns = 1000.125
    assert a.to_ps(4001) - b.to_ps(1000) == 1_000
