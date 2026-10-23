"""swi3s_studio.import_notes: the collector an importer reports its warnings into."""
import threading
import warnings

import pytest

from swi3s_studio import import_notes
from swi3s_studio.import_notes import ImportIssueWarning, ImportWarning


def test_collect_gathers_in_order_and_drops_exact_repeats():
    with import_notes.collect() as notes:
        import_notes.warn("a.csv", "first")
        import_notes.warn("a.csv", "second")
        import_notes.warn("a.csv", "first")
    assert notes == [ImportWarning("a.csv", "first"), ImportWarning("a.csv", "second")]
    assert str(notes[0]) == "a.csv: first"
    assert not import_notes.active()


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
