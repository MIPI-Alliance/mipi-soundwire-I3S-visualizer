"""Timeline ribbon tick precedence.

A long capture is ~98% Pings; with millions of samples per timeline pixel, many
commands collide on one x. The ribbon paints ticks in ASCENDING importance so a
significant mark (Write/Read, commit, CRC error) WINS the pixel over a colliding
Ping — fixing the bug where a Write tick was overpainted by a later Ping and
vanished. This checks the paint-order rank (tick_rank) that drives that sort,
without relying on fragile offscreen pixel rendering.

Run: python3 tests/test_timeline.py
"""
from swi3s_studio.ui import timeline as tl
from swi3s_studio.ui.timeline import tick_rank, _kind_color

_PING = {"command": "Ping", "has_manager_packet": True, "crc_valid": True}
_WRITE = {"command": "WriteA32", "has_manager_packet": True, "crc_valid": True}
_READ = {"command": "ReadA32", "has_manager_packet": True, "crc_valid": True}
_COMMIT = {"command": "SSCR", "is_commit": True, "commit_confirmed": True,
           "has_manager_packet": True, "crc_valid": True, "has_sync_point": True}
_ERROR = {"command": "WriteA32", "has_manager_packet": True, "crc_valid": False}


def test_rank_order():
    """Ping ranks below every significant mark; error/commit rank highest."""
    assert tick_rank(_PING) < tick_rank(_WRITE)
    assert tick_rank(_PING) < tick_rank(_READ)
    assert tick_rank(_WRITE) < tick_rank(_COMMIT)
    assert tick_rank(_COMMIT) <= tick_rank(_ERROR)
    assert tick_rank(_ERROR) == max(tick_rank(c) for c in
                                    (_PING, _WRITE, _READ, _COMMIT, _ERROR))


def test_significant_marks_sort_after_pings():
    """Sorting a Ping-heavy command list by tick_rank puts the Write LAST among a
    pile of pings sharing its slot — so it is painted last and wins the pixel."""
    cmds = [_PING] * 20 + [_WRITE] + [_PING] * 20
    ordered = sorted(cmds, key=tick_rank)
    assert ordered[-1] is _WRITE, "Write must paint after all colliding Pings"
    # And an error beats even a write when both collide.
    cmds2 = [_PING, _WRITE, _ERROR, _PING]
    assert sorted(cmds2, key=tick_rank)[-1] is _ERROR


def test_colors_distinct():
    """The colours the ranks map to are the expected, distinct tick colours. Read the
    constants off the module (not import-bound copies): retheme() rebinds these globals
    to new QColor objects, so a prior test that applied a theme would otherwise leave
    the imported names stale and the identity check spuriously failing."""
    assert _kind_color(_PING) is tl._C_PING
    assert _kind_color(_WRITE) is tl._C_OTHER
    assert _kind_color(_ERROR) is tl._C_ERROR


if __name__ == "__main__":
    test_rank_order(); print("ok: Ping ranks below write/read/commit/error")
    test_significant_marks_sort_after_pings(); print("ok: significant marks paint after colliding Pings")
    test_colors_distinct(); print("ok: tick colours distinct")
    print("ALL TIMELINE TESTS PASSED")
