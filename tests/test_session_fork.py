"""Session.fork / Session.adopt: a re-decode made on a fork leaves the Session it came from
whole until it is adopted, and adoption keeps what was set on that Session meanwhile."""
import pytest

from swi3s_studio.session import Session


def _demo() -> Session:
    return Session.from_demo(300, cold_start=True, phy=2)


def test_a_re_decode_on_a_fork_leaves_the_session_whole_until_adopted():
    s = _demo()
    decoder, commands, source = s.decoder, s.commands, dict(s.source)
    fork = s.fork()
    fork.set_hub_depths({1: 1})
    assert s.decoder is decoder and s.commands is commands
    assert s.hub_depths == {} and s.source == source
    assert fork.decoder is not decoder
    s.adopt(fork)
    assert s.decoder is fork.decoder and s.commands is fork.commands
    assert s.register_files is fork.register_files
    assert s.hub_depths == {1: 1}


def test_a_fork_shares_no_container_with_its_session():
    s = _demo()
    fork = s.fork()
    fork.register_overrides[(None, 1, 0x40)] = 7
    fork.source["probe"] = 1
    fork.dp_names[(1, 1)] = "x"
    assert (None, 1, 0x40) not in s.register_overrides
    assert "probe" not in s.source and (1, 1) not in s.dp_names


def test_adopt_merges_source_per_key_and_keeps_what_was_set_on_the_session():
    s = _demo()
    fork = s.fork()
    fork.set_ssp_row(5)                    # no config CSV: stored, not re-decoded
    s.set_dp_name(1, 1, "Mic L")
    s.adopt(fork)
    assert s.source["ssp_row"] == 5
    assert s.source["dp_names"] == {"1.1": "Mic L"}
    assert s.dp_names == {(1, 1): "Mic L"}


def test_adopt_removes_a_source_key_the_fork_removed(tmp_path):
    """A region CSV and a column pin the fork removes are gone from the adopted source, so a
    saved workspace does not put them back."""
    from swi3s_studio import import_notes
    from swi3s_studio.model.bus_config import demo_config
    s = _demo()
    mid = int(s.last_sample()) // 2
    s.force_column_count_at(mid, 8)
    with import_notes.collect():
        s.apply_config_csv_at(mid, demo_config().to_csv_file(str(tmp_path / "cfg.csv")))
    assert {"config_csv@1", "forced_column_sections"} <= set(s.source)
    fork = s.fork()
    with import_notes.collect():
        fork.apply_config_csv_at(mid, None)
    fork.force_column_count_at(mid, 0)
    s.set_dp_name(1, 1, "Mic L")
    s.adopt(fork)
    assert "config_csv@1" not in s.source and "forced_column_sections" not in s.source
    assert s.source["dp_names"] == {"1.1": "Mic L"}


def test_adopt_takes_an_attribute_the_fork_reset_even_to_the_value_it_had():
    s = _demo()
    s._eff_sorted = None
    fork = s.fork()
    fork.set_hub_depths({1: 1})            # resets _eff_sorted to None on the fork
    s._eff_sorted = ("built over the old decode",)
    s.adopt(fork)
    assert s._eff_sorted is None


def test_a_register_map_bound_during_the_re_decode_rebuilds_the_adopted_files():
    s = _demo()
    fork = s.fork()
    fork.set_hub_depths({1: 1})
    s.set_peripheral_map(1, object(), rebuild=False)
    s.adopt(fork)
    assert s.register_files is not fork.register_files


def test_adopt_refuses_a_session_that_is_not_a_fork():
    s = _demo()
    with pytest.raises(TypeError):
        s.adopt(_demo())
