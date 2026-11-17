"""The register-model checkpoint the as-of-cursor Bus Grid continues from.

- `grid_from_commands(cmds[k:], base=ReplayCheckpoint(cmds[:k]))` equals
  `grid_from_commands(cmds)` at every split point k, with a forced width, what-if overrides
  and the DLV framing.
- `Session.grid_cells_at` and `grid_cells` with a config CSV applied lay out the same cells
  as replaying the CSV's write+commit prefix ahead of the wire's commands on every call, at
  every probed cursor, on every demo (PHY1/2/3, cold start, flow control, a delayed second
  Link), with a region CSV and after a re-decode, and the checkpoint follows the CSV that is
  applied.

Every input is synthetic.
"""
import os

import pytest
import swi3score

from swi3s_studio.model.bus_config import demo_config
from swi3s_studio.session import Session

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_BIG_CSV = os.path.join(_ROOT, "visualizer_examples", "directed_tests", "intentional_errors",
                        "CDS_split_guard_1.csv")


class _Prefix:
    """Stands in for ReplayCheckpoint: keeps the commands so the reference replays them."""

    def __init__(self, commands):
        self.commands = list(commands)


@pytest.fixture
def prefix_path(monkeypatch):
    """A function that runs `fn` with the CSV prefix replayed on every grid call."""
    def run(session, fn):
        orig = swi3score.grid_from_commands

        def grid(commands, rows=32, *, base=None, **kw):
            return orig((base.commands if base else []) + list(commands), rows, **kw)
        with monkeypatch.context() as m:
            m.setattr(swi3score, "ReplayCheckpoint", _Prefix)
            m.setattr(swi3score, "grid_from_commands", grid)
            _reseed(session)
            out = fn()
        _reseed(session)
        return out
    return run


def _reseed(session):
    session._csv_seed_key = None
    session._reload_csv_seed()


def _cursors(session):
    last = int(session.last_sample())
    xs = {last * k // 63 for k in range(64)}
    for seg in session.segments:
        s0 = int(seg.get("start_sample", 0))
        xs.update({max(0, s0 - 1), s0, s0 + 1})
    return sorted(xs)


def _cells(session):
    return [session.grid_cells_at(x) for x in _cursors(session)] + [session.grid_cells()]


def _check(session, prefix_path):
    got = _cells(session)
    want = prefix_path(session, lambda: _cells(session))
    assert got == want
    assert session._csv_base is not None
    assert any(got), "no cursor laid out a cell"


@pytest.fixture(scope="module")
def small_csv(tmp_path_factory):
    return demo_config().to_csv_file(str(tmp_path_factory.mktemp("cfg") / "cfg.csv"))


def _wire_and_csv_replay():
    s = Session.from_demo(600, phy=3)
    s.apply_config_csv(_BIG_CSV)
    wire = s._grid_replay_in_effect(s.last_sample())
    assert wire, "the PHY3 demo carries config writes on top of the CSV"
    return s._csv_replay + wire


@pytest.mark.parametrize("kw", [{"force_columns": 0}, {"force_columns": 8},
                                {"register_overrides": [(0, 0x1060, 0x03), (1, 0x1020, 0x01)]},
                                {"dlv": True, "cds_horizontal_start": 2}])
def test_a_checkpoint_split_anywhere_lays_out_the_whole_replays_grid(kw):
    cmds = _wire_and_csv_replay()
    whole = swi3score.grid_from_commands(cmds, 32, **kw)
    assert whole
    for k in range(len(cmds) + 1):
        base = swi3score.ReplayCheckpoint(cmds[:k])
        assert swi3score.grid_from_commands(cmds[k:], 32, base=base, **kw) == whole, k
    # The checkpoint is read, never written: a second use lays out the same grid.
    base = swi3score.ReplayCheckpoint(cmds[:len(cmds) // 2])
    for _ in range(2):
        assert swi3score.grid_from_commands(cmds[len(cmds) // 2:], 32, base=base, **kw) == whole


@pytest.mark.parametrize("demo", [{}, {"phy": 1}, {"phy": 3}, {"cold_start": True},
                                  {"cold_start": True, "phy": 3},
                                  {"variant": "flow_control"}, {"delay_samples": 5000}])
@pytest.mark.parametrize("csv", ["small", "big"])
def test_the_grid_at_every_cursor_equals_the_csv_prefix_replay(demo, csv, small_csv,
                                                              prefix_path):
    s = Session.from_demo(600, **demo)
    s.apply_config_csv(small_csv if csv == "small" else _BIG_CSV)
    _check(s, prefix_path)


def test_the_grid_equals_the_prefix_replay_across_a_re_decode(small_csv, prefix_path):
    s = Session.from_demo(600, phy=3)
    s.apply_config_csv(_BIG_CSV)
    _check(s, prefix_path)
    s.set_register_override(0, 0, 0x1060, 0x03)          # a re-decode, CSV unchanged
    _check(s, prefix_path)
    s.apply_config_csv_at(s.last_sample(), small_csv)    # a region's CSV is its whole config
    _check(s, prefix_path)
    s.apply_config_csv(small_csv)                       # another CSV: a new checkpoint
    assert s._csv_base is not None
    _check(s, prefix_path)
    s.apply_config_csv(None)
    assert s._csv_base is None
