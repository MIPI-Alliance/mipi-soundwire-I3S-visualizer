"""The swviz DataPort / FlowControlPort models are a published spec deliverable.

`swi3s_studio/swviz/models/dataport.py` and `flow_control_port.py` are the golden
reference model for the SWI3S transport algorithm and are published in the MIPI
specification. They therefore carry NO `#` comments: the spec text is the explanation, and
implementation rationale belongs in docs/ or in the tests, not in the deliverable.

This exists because comments are invisible to every other test in the suite, so nothing
else can catch the drift — and the drift was real. v2.1.12 shipped exactly one comment in
each file (`state: ... # created in initialize()`). By 3.0.11 dataport.py had 21 lines
carrying `#` and flow_control_port.py 4, added across three unrelated commits: a perf
hoist (43779d7), the spacing row-boundary fix (f680b14), and the partial-channel-group
fix. Each looked locally reasonable; together they put ~20 lines of implementation
commentary into a spec artefact. The v2-era comment was dropped too, so the rule is now
simply "none" — no allowlist, no judgement about which comment is blessed.

Where the removed rationale went: the spacing row-boundary story is docs/spacing_bug.md,
and the partial-channel-group story is tests/test_transport_slot_budget.py.
"""
import ast
import os
import tokenize

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_MODELS = (
    "swi3s_studio/swviz/models/dataport.py",
    "swi3s_studio/swviz/models/flow_control_port.py",
)


def _read(rel: str) -> str:
    with open(os.path.join(_ROOT, rel), encoding="utf-8") as f:
        return f.read()


@pytest.mark.parametrize("rel", _MODELS)
def test_reference_model_carries_no_comments(rel):
    """Tokenize rather than grep for '#': a '#' inside a string literal is not a comment,
    and a grep-based guard would either miss real comments or fire on data."""
    with open(os.path.join(_ROOT, rel), "rb") as f:
        found = [
            (tok.start[0], tok.string)
            for tok in tokenize.tokenize(f.readline)
            if tok.type == tokenize.COMMENT
        ]
    assert not found, (
        f"{rel} is published in the MIPI spec as the reference model and must carry no "
        f"comments; found {len(found)}:\n"
        + "\n".join(f"  line {ln}: {text}" for ln, text in found)
        + "\nPut the rationale in docs/ or in the test that covers the behaviour."
    )


@pytest.mark.parametrize("rel", _MODELS)
def test_reference_model_keeps_its_docstrings(rel):
    """The counterweight: docstrings are the documentation form these files DO use (v2
    shipped them), so the no-comment rule must not be satisfied by deleting the module or
    class docs instead."""
    tree = ast.parse(_read(rel))
    assert ast.get_docstring(tree), f"{rel} lost its module docstring"
    classes = [n for n in tree.body if isinstance(n, ast.ClassDef)]
    assert classes, f"{rel} defines no classes?"
    undocumented = [c.name for c in classes if not ast.get_docstring(c)]
    assert not undocumented, f"{rel}: classes lost their docstrings: {undocumented}"
