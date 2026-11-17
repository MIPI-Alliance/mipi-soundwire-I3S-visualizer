"""main_window.py's source and its controllers' (ui/*_controller.py), read with ast: no
getattr or hasattr reads an attribute of the window or of a controller, the attributes
behind the ribbon's, the views' and the dock title bars' accessors are read only through
those accessors, and every re-decode goes through one helper. Builds no widget."""
import ast
import pathlib

import pytest

_UI = pathlib.Path(__file__).resolve().parents[1] / "swi3s_studio" / "ui"
_SOURCE = _UI / "main_window.py"
# main_window.py and every controller module the window holds (each reaches the window
# as `self._win`).
_SOURCES = [_SOURCE, *sorted(_UI.glob("*_controller.py"))]


def _is_window(node) -> bool:
    """`self`, or a controller's `self._win`."""
    if isinstance(node, ast.Name):
        return node.id == "self"
    return (isinstance(node, ast.Attribute) and node.attr == "_win"
            and isinstance(node.value, ast.Name) and node.value.id == "self")


@pytest.mark.parametrize("source", _SOURCES, ids=lambda p: p.name)
def test_no_getattr_or_hasattr_reads_an_attribute_of_self(source):
    """No getattr or hasattr call takes `self` (or a controller's `self._win`) as its
    object, in any spelling of the call, so a window attribute is read by name and a
    misspelled one is an AttributeError, not a silent default. That the state is declared
    in __init__ is test_a_new_window_declares_its_load_and_locate_state's."""
    tree = ast.parse(source.read_text(encoding="utf-8"))
    found = [node.lineno for node in ast.walk(tree)
             if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
             and node.func.id in ("getattr", "hasattr") and node.args
             and _is_window(node.args[0])]
    assert found == [], f"getattr/hasattr on self at lines {found}"


# The private attributes the accessors of the ribbon, the views and the dock title bars
# stand for (test_window_state_declared.py checks that each accessor agrees with it).
_ACCESSOR_BACKED = {"_store", "_dock", "_table", "_cursor", "_total", "_view_lo", "_view_hi"}


@pytest.mark.parametrize("source", _SOURCES, ids=lambda p: p.name)
def test_main_window_reads_no_accessor_backed_attribute_of_another_object(source):
    """`_store`, `_dock`, `_table`, `_cursor`, `_total`, `_view_lo` and `_view_hi` are read
    only on the window (`self`, or a controller's `self._win`): another object's are read
    through its accessor."""
    tree = ast.parse(source.read_text(encoding="utf-8"))
    found = [(node.lineno, node.attr) for node in ast.walk(tree)
             if isinstance(node, ast.Attribute) and node.attr in _ACCESSOR_BACKED
             and not _is_window(node.value)]
    assert found == []


def _load_async_kinds(tree) -> list:
    """(enclosing method, kind) for every `self._load_async(...)` call that names a kind."""
    found = []
    for fn in ast.walk(tree):
        if not isinstance(fn, ast.FunctionDef):
            continue
        for node in ast.walk(fn):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                    and node.func.attr == "_load_async":
                for kw in node.keywords:
                    if kw.arg == "kind" and isinstance(kw.value, ast.Constant):
                        found.append((fn.name, kw.value.value))
    return found


def test_every_re_decode_goes_through_redecode_preserving():
    """The one `_load_async(..., kind="redecode")` call in main_window.py is
    `_redecode_preserving`'s, so every re-decode of the loaded capture keeps the cursor
    and the status message the same way."""
    tree = ast.parse(_SOURCE.read_text(encoding="utf-8"))
    redecodes = [name for name, kind in _load_async_kinds(tree) if kind == "redecode"]
    assert redecodes == ["_redecode_preserving"]
