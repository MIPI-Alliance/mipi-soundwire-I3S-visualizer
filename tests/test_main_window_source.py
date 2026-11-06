"""main_window.py's source, read with ast: no getattr or hasattr reads an attribute of the
window, and the attributes behind the ribbon's, the views' and the dock title bars'
accessors are read only through those accessors. Builds no widget."""
import ast
import pathlib

_SOURCE = pathlib.Path(__file__).resolve().parents[1] / "swi3s_studio" / "ui" / "main_window.py"


def test_no_getattr_or_hasattr_reads_an_attribute_of_self():
    """No getattr or hasattr call in main_window.py takes `self` as its object, in any
    spelling of the call, so a window attribute is read by name and a misspelled one is an
    AttributeError, not a silent default. That the state is declared in __init__ is
    test_a_new_window_declares_its_load_and_locate_state's."""
    tree = ast.parse(_SOURCE.read_text(encoding="utf-8"))
    found = [node.lineno for node in ast.walk(tree)
             if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
             and node.func.id in ("getattr", "hasattr") and node.args
             and isinstance(node.args[0], ast.Name) and node.args[0].id == "self"]
    assert found == [], f"getattr/hasattr on self at lines {found}"


# The private attributes the accessors of the ribbon, the views and the dock title bars
# stand for (test_window_state_declared.py checks that each accessor agrees with it).
_ACCESSOR_BACKED = {"_store", "_dock", "_table", "_cursor", "_total", "_view_lo", "_view_hi"}


def test_main_window_reads_no_accessor_backed_attribute_of_another_object():
    """main_window.py reads `_store`, `_dock`, `_table`, `_cursor`, `_total`, `_view_lo` and
    `_view_hi` only on `self`: another object's are read through its accessor."""
    tree = ast.parse(_SOURCE.read_text(encoding="utf-8"))
    found = [(node.lineno, node.attr) for node in ast.walk(tree)
             if isinstance(node, ast.Attribute) and node.attr in _ACCESSOR_BACKED
             and not (isinstance(node.value, ast.Name) and node.value.id == "self")]
    assert found == []
