"""The Commands free-text filter: malformed expressions, and All Links matching the text shown."""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pytest
from PySide6.QtWidgets import QApplication

from swi3s_studio.ui.command_table import (
    AllLinksCommandModel,
    CommandTableModel,
    eval_filter,
    parse_filter,
)

pytestmark = pytest.mark.gui


def _match(query, hay):
    return eval_filter(parse_filter(query), hay)


def test_an_operator_with_a_missing_operand_leaves_the_other_side_alone():
    assert not _match("write or", "read only")
    assert _match("write or", "write only")
    assert not _match("or write", "read only")
    assert not _match("write and", "read only")
    assert parse_filter("or") is None and parse_filter("()") is None


def test_an_unmatched_close_paren_does_not_drop_the_rest():
    assert _match("write) or read", "a read here")
    assert not _match("write) or read", "something else")
    assert _match("(write or read", "read")                # an unclosed '(' closes at the end


def test_the_filter_grammar_is_unchanged_for_well_formed_input():
    assert _match("(write or read) and dp0", "read dp0")
    assert not _match("(write or read) and dp0", "read dp1")
    assert _match("write or read and dp1", "write dp0")


class _Rmap:
    def field_summary(self, *_a):
        return ""

    def name_of(self, *_a):
        return ""

    def __getattr__(self, _n):
        return lambda *a, **k: ""


def test_all_links_filter_matches_the_global_time_it_displays():
    QApplication.instance() or QApplication([])
    cmd = {"start_sample": 234_560, "bus_row": 0, "command": "WRITE", "phase": "",
           "device_mask": 1}
    model = CommandTableModel([cmd], _Rmap(), 1_000_000)
    offset_ps = 1e9                                        # 1000 us
    all_links = AllLinksCommandModel([("B", model, np.array([0.0]), offset_ps)])
    local, shown = "234,560.00", "235,560.00"
    assert all_links.data(all_links.index(0, AllLinksCommandModel.TIME_COLUMN)) == shown
    hay = all_links.search_text(0)
    assert shown in hay and local not in hay
