"""The on-disk results of a large capture and the register checkpoint log.

A decode whose audio columns exceed Session.LARGE_CAPTURE_AUDIO_BYTES keeps its results in
a per-Session temp directory (store.results_store): each audio store it makes is
memmap-backed in a subdirectory of its own, and its commands are written as a Parquet
command table. The directory goes when the Session is closed or dropped. Under the
threshold everything stays in RAM. Register state at a cursor continues from the latest
register checkpoint (Session._reg_checkpoint) and equals the whole replay's.
"""
import bisect
import gc
import glob
import os

import numpy as np
import pytest
import swi3score

import swi3s_studio.session as S
from swi3s_studio.ingest import saleae_sal
from swi3s_studio.session import Session
from swi3s_studio.store import command_store
from swi3s_studio.store.audio_store import AudioStore


@pytest.fixture
def large(monkeypatch):
    """Every decode counts as a large capture."""
    monkeypatch.setattr(Session, "LARGE_CAPTURE_AUDIO_BYTES", -1)


def _channels(store):
    return {k: np.asarray(v).copy() for k, v in store._channels.items()}


def test_the_large_capture_threshold_is_the_watchdogs_minimum_floor():
    assert Session.LARGE_CAPTURE_AUDIO_BYTES == saleae_sal._DECODE_FLOOR_MIN


def test_a_capture_under_the_threshold_keeps_its_results_in_ram():
    s = Session.from_demo(32)
    assert not s.large_results and s.results_dir is None and s.commands_path is None
    store = s.audio_store()
    assert store._channels and not any(isinstance(a, np.memmap) for a in store._channels.values())
    assert s.commands_table().equals(command_store.commands_to_table(s.commands))


def test_a_large_capture_keeps_its_audio_and_commands_on_disk(large):
    s = Session.from_demo(32)
    root = s.results_dir
    assert s.large_results and root and os.path.isdir(root)
    store = s.audio_store()
    assert store._channels
    for arr in store._channels.values():
        assert isinstance(arr, np.memmap)
        assert os.path.dirname(arr.filename).startswith(root)
    want = _channels(AudioStore.from_session(s))
    got = _channels(store)
    assert want.keys() == got.keys()
    assert all(np.array_equal(want[k], got[k]) for k in want)
    assert s.commands_path.startswith(root)
    assert s.commands_table().equals(command_store.commands_to_table(s.commands))
    s.close()
    assert not os.path.exists(root) and not store._channels
    assert s.results_dir is None and s.commands_path is None
    assert s.commands_table().num_rows == len(s.commands)


def test_each_audio_store_has_its_own_files(large):
    s = Session.from_demo(32)
    a = s.audio_store()
    before = _channels(a)
    b = s.audio_store()
    dirs = {os.path.dirname(arr.filename) for st in (a, b) for arr in st._channels.values()}
    assert len(dirs) == 2
    assert all(np.array_equal(before[k], np.asarray(a._channels[k])) for k in before)
    s.close()


def test_a_dropped_session_removes_its_results_dir(large):
    s = Session.from_demo(32)
    store = s.audio_store()
    root = s.results_dir
    assert os.listdir(root)
    del s, store
    gc.collect()
    assert not os.path.exists(root)


def test_an_audio_store_that_outlives_its_session_keeps_the_results_dir_until_it_goes(large):
    s = Session.from_demo(32)
    store = s.audio_store()
    want = _channels(store)
    root = s.results_dir
    del s
    gc.collect()
    assert os.path.exists(root)
    assert all(np.array_equal(want[k], np.asarray(store._channels[k])) for k in want)
    del store
    gc.collect()
    assert not os.path.exists(root)


def test_a_redecode_keeps_one_command_table_and_a_failed_one_keeps_the_decode(large, monkeypatch):
    s = Session.from_demo(32)
    root, first = s.results_dir, s.commands_path
    s.set_hub_depths({1: 1})
    assert s.results_dir == root and s.commands_path != first
    assert glob.glob(os.path.join(root, "commands-*.parquet")) == [s.commands_path]
    kept = s.commands

    calls = []

    def fail(*a, **k):
        calls.append(1)
        raise MemoryError("register files")
    with monkeypatch.context() as m:
        m.setattr(Session, "_build_register_files", fail)
        with pytest.raises(MemoryError):
            s.set_hub_depths({1: 2})
    assert calls and s.commands is kept
    assert s.commands_table().equals(command_store.commands_to_table(kept))
    assert glob.glob(os.path.join(root, "commands-*.parquet")) == [s.commands_path]
    s.close()


# ---- a re-decode on a fork (Session.fork / adopt) ------------------------------------

def _listing(root):
    return sorted(os.path.relpath(os.path.join(d, f), root)
                  for d, _dirs, files in os.walk(root) for f in files)


def _live_state(s, store):
    """What a reader of `s` sees of its on-disk results, as plain values."""
    return (s.results_dir, s.commands_path, _listing(s.results_dir),
            s.commands_table(), _channels(store))


def _assert_unchanged(s, store, before):
    root, path, listing, table, channels = before
    assert (s.results_dir, s.commands_path, _listing(root)) == (root, path, listing)
    assert s.commands_table().equals(table)
    now = _channels(store)
    assert now.keys() == channels.keys()
    assert all(np.array_equal(now[k], channels[k]) for k in channels)


def test_a_forks_re_decode_writes_its_results_in_a_directory_of_its_own(large):
    s = Session.from_demo(32)
    store = s.audio_store()
    before = _live_state(s, store)
    fork = s.fork()
    assert fork.results_dir is None
    fork._redecode()
    fork_store = fork.audio_store()
    assert fork.results_dir not in (None, s.results_dir)
    _assert_unchanged(s, store, before)
    root = fork.results_dir
    s.adopt(fork)
    assert s.results_dir == root and s.commands_path == fork.commands_path
    assert os.path.exists(before[0])               # the replaced store waits for its release
    assert _channels(store).keys()                 # and the audio store over it still reads
    s.release_replaced()
    assert not os.path.exists(before[0]) and not store._channels
    assert _channels(fork_store).keys() and os.path.exists(s.commands_path)
    s.close()
    assert not os.path.exists(root)


def test_a_failed_re_decode_on_a_fork_leaves_the_session_and_its_store_as_they_were(
        large, monkeypatch):
    s = Session.from_demo(32)
    store = s.audio_store()
    before = _live_state(s, store)
    made = []
    from swi3s_studio.store import results_store
    init = results_store.ResultsStore.__init__

    def recording(self):
        init(self)
        made.append(self.path)
    monkeypatch.setattr(results_store.ResultsStore, "__init__", recording)

    def fail(*a, **k):
        raise MemoryError("register files")
    fork = s.fork()
    with monkeypatch.context() as m:
        m.setattr(Session, "_build_register_files", fail)
        with pytest.raises(MemoryError):
            fork._redecode()
    assert len(made) == 1                           # the failed decode wrote into its own
    assert not os.path.exists(made[0])              # and that directory is gone
    fork.close()
    _assert_unchanged(s, store, before)
    s.close()


def test_closing_a_fork_that_is_not_adopted_leaves_the_session_alone(large):
    s = Session.from_demo(32)
    store = s.audio_store()
    before = _live_state(s, store)
    fork = s.fork()
    fork._redecode()
    fork.audio_store()
    root = fork.results_dir
    fork.close()
    assert not os.path.exists(root)
    _assert_unchanged(s, store, before)
    s.close()


# ---- the register checkpoint log ------------------------------------------------------

def _replay_reference(s, sample):
    """register_files_at(sample) replayed whole, from the replay start, with no checkpoint."""
    reg_replay, reg_eff = s._reg_replay_sorted()
    reset = s._replay_start(sample)
    lo = bisect.bisect_left(reg_eff, reset)
    hi = bisect.bisect_right(reg_eff, int(sample))
    index = s.segment_index_for_sample(int(sample))
    stale = bool(reset and s.segments and int(s.segments[index]["start_sample"]) < reset)
    return s._build_register_files_from_replay(reg_replay[lo:hi], None if stale else index)


def _state(files):
    return {d: (dict(f._cells), dict(f._curr)) for d, f in files.items()}


@pytest.fixture(scope="module")
def relinked():
    """Two cold-start demos with the Link stopped between them (a register replay that
    restarts at the second's Bus Reset), with a what-if on the second region."""
    from test_link_restart import _GAP_S, _cold, _relinked
    a, b = _cold(), _cold()
    cap, _shift = _relinked(a, b, int(_GAP_S * a.sample_rate_hz))
    s = Session(cap, source={"type": "test"})
    s.set_register_override(len(s.segments) - 1, 0, 0x1081, 0x05)
    return s


@pytest.mark.parametrize("every", [1, 3, 512])
def test_register_state_from_a_checkpoint_is_the_whole_replays(relinked, monkeypatch, every):
    s = relinked
    monkeypatch.setattr(Session, "REG_CHECKPOINT_EVERY", every)
    _replay, reg_eff = s._reg_replay_sorted()
    assert s.bus_reset_samples() and len(reg_eff) > 9
    cursors = sorted({x for e in reg_eff for x in (e - 1, e, e + 1)} | {0, 10 ** 12}
                     | set(s.bus_reset_samples()))
    for x in cursors:
        assert _state(s.register_files_at(x)) == _state(_replay_reference(s, x)), x


def test_register_state_at_a_cursor_replays_at_most_one_checkpoint_interval(relinked,
                                                                            monkeypatch):
    s = relinked
    monkeypatch.setattr(Session, "REG_CHECKPOINT_EVERY", 4)
    reg_replay, reg_eff = s._reg_replay_sorted()
    assert len(reg_replay) > 8
    s.register_files_at(10 ** 12)                     # builds the log
    lengths = []
    real = swi3score.registers_from_commands

    def counted(cmds, *a, **k):
        lengths.append(len(cmds))
        return real(cmds, *a, **k)
    monkeypatch.setattr(S.swi3score, "registers_from_commands", counted)
    for x in reg_eff:
        s.register_files_at(x)
    assert lengths and max(lengths) <= 4


def test_the_core_continues_a_replay_from_a_snapshot():
    """registers_from_commands(rest, start=registers_from_commands(prefix)) equals the whole
    replay, overrides folded after, across writes, commits, reads and a write-1-to-clear."""
    devstat, nxt = 0x1021, 0x1081
    replay = [
        {"is_write": True, "device_mask": 0b11, "address": nxt, "data": b"\x07"},
        {"is_read": True, "device_mask": 0b01, "address": devstat, "data": b"\x03"},
        {"is_commit": True, "commit_confirmed": True, "group_mask": 1, "device_mask": 0b01},
        {"is_write": True, "device_mask": 0b01, "address": devstat, "data": b"\x01"},
        {"is_write": True, "device_mask": 0b10, "address": nxt, "data": b"\x09"},
        {"is_read": True, "device_mask": 0b10, "address": nxt + 0x40, "data": b"\x04"},
        {"is_commit": True, "commit_confirmed": True, "group_mask": 1, "device_mask": 0b10},
    ]
    overrides = [(0, nxt, 0x0B)]
    whole = swi3score.registers_from_commands(replay, register_overrides=overrides)
    for k in range(len(replay) + 1):
        start = swi3score.registers_from_commands(replay[:k])
        got = swi3score.registers_from_commands(replay[k:], register_overrides=overrides,
                                                start=start)
        assert got == whole, k


def test_a_re_decode_removes_the_earlier_command_table_under_a_path_with_glob_characters(
        tmp_path, monkeypatch):
    import tempfile

    from swi3s_studio.store.results_store import ResultsStore
    base = tmp_path / "[lab]"                     # a name every platform allows
    base.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(base))
    store = ResultsStore()
    try:
        assert os.path.dirname(store.path) == str(base)
        store.save_commands([])
        newest = store.save_commands([])
        assert [os.path.join(store.path, n) for n in os.listdir(store.path)] == [newest]
    finally:
        store.close()


def test_a_results_dir_held_open_for_a_moment_is_still_removed(monkeypatch):
    from swi3s_studio.store import results_store
    store = results_store.ResultsStore()
    root = store.path
    real, tries = results_store.shutil.rmtree, []

    def held_twice(path, ignore_errors=False):
        tries.append(path)
        if len(tries) > 2:                  # a scanner lets go after the second attempt
            real(path, ignore_errors=ignore_errors)
    monkeypatch.setattr(results_store.shutil, "rmtree", held_twice)
    monkeypatch.setattr(results_store, "_REMOVE_WAITS", (0.0, 0.0, 0.0, 0.0))
    store.close()
    assert len(tries) == 3 and not os.path.exists(root) and store.closed
