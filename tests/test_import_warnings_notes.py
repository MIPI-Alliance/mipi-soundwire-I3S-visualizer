"""swi3s_studio.import_notes: the collector an importer reports its warnings into."""
import threading
import warnings

import pytest

from swi3s_studio import import_notes
from swi3s_studio.import_notes import ImportIssueWarning, ImportNote


def test_collect_gathers_in_order_and_drops_exact_repeats():
    with import_notes.collect() as notes:
        import_notes.warn("a.csv", "first")
        import_notes.warn("a.csv", "second")
        import_notes.warn("a.csv", "first")
    assert notes == [ImportNote("a.csv", "first"), ImportNote("a.csv", "second")]
    assert str(notes[0]) == "a.csv: first"
    assert not import_notes.active()


def test_a_list_edited_behind_the_collector_is_still_de_duplicated():
    with import_notes.collect() as notes:
        import_notes.warn("a", "first")
        notes.append(ImportNote("a", "x"))            # appended behind the collector
        import_notes.warn("a", "x")
        assert notes == [ImportNote("a", "first"), ImportNote("a", "x")]
        notes[:] = [ImportNote("a", "x")]             # replaced by a shorter list
        import_notes.warn("a", "first")
        import_notes.warn("a", "first")
        assert notes == [ImportNote("a", "x"), ImportNote("a", "first")]
        notes[:] = [ImportNote("a", "y"), ImportNote("a", "z")]     # a different list, as long
        import_notes.warn("a", "first")               # no longer held: reported again
        import_notes.warn("a", "z")                   # held: not reported twice
        import_notes.warn("a", "first")
    assert notes == [ImportNote("a", "y"), ImportNote("a", "z"), ImportNote("a", "first")]


def test_a_note_replaced_in_place_by_another_of_the_same_length_is_re_indexed():
    with import_notes.collect() as notes:
        import_notes.warn("a", "first")
        notes[:] = [ImportNote("a", "other")]         # one note for one, a different one
        import_notes.warn("a", "first")               # no longer held: reported again
        import_notes.warn("a", "other")               # held: not reported twice
    assert notes == [ImportNote("a", "other"), ImportNote("a", "first")]


def test_an_item_replaced_or_removed_in_the_middle_is_re_indexed():
    with import_notes.collect() as notes:
        import_notes.warn("a", "one")
        import_notes.warn("a", "two")
        notes[0] = ImportNote("a", "other")           # the length and the last note are unchanged
        import_notes.warn("a", "one")                 # no longer held: reported again
        import_notes.warn("a", "other")               # held: not reported twice
        assert notes == [ImportNote("a", "other"), ImportNote("a", "two"),
                         ImportNote("a", "one")]
        del notes[1]
        import_notes.warn("a", "two")                 # removed: reported again
        notes.sort(key=lambda n: n.message)
        import_notes.warn("a", "one")                 # held: not reported twice
    assert [n.message for n in notes] == ["one", "other", "two"]


def test_names_are_listed_a_few_at_a_time_and_cut_to_the_quote_limit():
    from swi3s_studio.ingest.timebase import QUOTED_CHARS
    assert import_notes.listed(["a", "b", "c"]) == "a, b, c"
    assert import_notes.listed(["a", "b", "c", "d", "e"]) == "a, b, c and 2 more"
    long = "k" * 100
    text = import_notes.names_listed([long, "b", "c", "d"])
    assert text == f"{'k' * (QUOTED_CHARS - 1)}\u2026, b, c and 1 more"


def test_without_a_collector_warn_issues_an_import_issue_warning():
    with pytest.warns(ImportIssueWarning, match=r"b\.vcd: 3 lines were skipped"):
        import_notes.warn("b.vcd", "3 lines were skipped")


def test_nested_collectors_keep_their_own_warnings():
    with import_notes.collect() as outer:
        import_notes.warn("o", "outer")
        with import_notes.collect() as inner:
            import_notes.warn("i", "inner")
        import_notes.warn("o", "outer again")
    assert [n.message for n in inner] == ["inner"]
    assert [n.message for n in outer] == ["outer", "outer again"]


def test_a_collector_belongs_to_the_thread_that_opened_it():
    seen = []

    def other():
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            import_notes.warn("t.sal", "from another thread")
        seen.extend(w)

    with import_notes.collect() as notes:
        t = threading.Thread(target=other)
        t.start()
        t.join()
    assert notes == []
    assert len(seen) == 1 and issubclass(seen[0].category, ImportIssueWarning)


def test_source_name_is_the_base_name():
    assert import_notes.source_name("/x/y/cap.bin") == "cap.bin"
    assert import_notes.source_name("") == ""


def test_extend_re_raises_into_the_active_collector():
    with import_notes.collect() as first:
        import_notes.warn("f", "m")
    with import_notes.collect() as second:
        import_notes.extend(first)
    assert second == first
