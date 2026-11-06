"""A framing taken at a known column count without a CRC-valid command to confirm Column 0
is reported to the user as an import warning, once per distinct report, for the decode the
Session keeps: a decode a later one in the same step replaces is not reported."""
import numpy as np

from swi3s_studio import import_notes
from swi3s_studio.ingest.capture import Capture
from swi3s_studio.session import Session


def _noise_capture(n_ui=20000, spu=4, seed=1):
    """Clock toggling every UI, data random: no command decodes."""
    bits = np.random.default_rng(seed).integers(0, 2, n_ui)
    changed = np.nonzero(np.diff(np.concatenate([[0], bits])))[0]
    return Capture(clock_edges=np.arange(n_ui * 2) * (spu // 2),
                   data_edges=changed * spu, initial_clock=False, initial_data=False,
                   sample_rate_hz=100_000_000)


def test_a_known_count_with_no_valid_command_warns_that_the_framing_is_a_guess():
    with import_notes.collect() as notes:
        sess = Session(_noise_capture(), forced_column_count=8)
    guessed = [int(x) for x in sess.decoder.guessed_framing_samples()]
    assert guessed
    [note] = [n for n in notes if "guess" in n.message]
    assert f"sample {guessed[0]}" in note.message and "CRC-valid command" in note.message
    assert note.source == sess.source_label()


def test_a_generated_demo_does_not_warn(tmp_path):
    from swi3s_studio.model.bus_config import demo_config
    path = demo_config().to_csv_file(str(tmp_path / "cfg.csv"))
    with import_notes.collect() as notes:
        sess = Session.from_demo(8, phy=2, cold_start=True, config_csv=path)
    assert len(sess.decoder.guessed_framing_samples()) > 0      # guessed, but a demo
    assert not [n for n in notes if "guess" in n.message]


def test_a_validated_framing_does_not_warn():
    with import_notes.collect() as notes:
        sess = Session(Session.from_demo(8).capture, forced_column_count=0)
    assert len(sess.decoder.guessed_framing_samples()) == 0
    assert not [n for n in notes if "guess" in n.message]


def test_the_report_names_the_first_samples_and_counts_the_rest(monkeypatch):
    sess = Session(_noise_capture(n_ui=200))

    class Fake:
        def guessed_framing_samples(self):
            return np.arange(100, 108, dtype=np.uint64)

    monkeypatch.setattr(sess, "decoder", Fake())
    with import_notes.collect() as notes:
        sess._warn_guessed_framing()
    assert "samples 100, 101, 102, 103, 104 and 3 more" in notes[0].message


def test_only_the_decode_kept_is_warned_about(monkeypatch, tmp_path):
    """A step that decodes more than once (the bring-up checks in the constructor, a
    config CSV followed by the SSPA lock) warns once, for the decoder it keeps."""
    warned = []
    monkeypatch.setattr(Session, "_warn_guessed_framing",
                        lambda self: warned.append(self.decoder))
    real_later = Session._check_later_bringups

    def decode_twice(self, settings, decoder_ready=None):
        real_later(self, settings, decoder_ready)
        self._decode_with(settings, decoder_ready)
    monkeypatch.setattr(Session, "_check_later_bringups", decode_twice)
    sess = Session(_noise_capture(), forced_column_count=8)
    assert warned == [sess.decoder]

    from swi3s_studio.model.bus_config import demo_config
    path = demo_config().to_csv_file(str(tmp_path / "cfg.csv"))
    monkeypatch.setattr(Session, "lock_ssp_from_sspa", lambda self: self._redecode())
    warned.clear()
    sess.apply_config_csv(path)
    assert warned == [sess.decoder]
    warned.clear()
    sess.set_hub_depths({1: 2})                  # one decode: warned as it is made
    assert warned == [sess.decoder]


def test_two_decodes_with_the_same_guess_report_it_once():
    """The constructor's decode and a re-decode that guesses at the same samples give one
    note in one collector."""
    with import_notes.collect() as notes:
        sess = Session(_noise_capture(), forced_column_count=8)
        first = [int(x) for x in sess.decoder.guessed_framing_samples()]
        sess.set_hub_depths({1: 2})
        assert [int(x) for x in sess.decoder.guessed_framing_samples()] == first
    assert len([n for n in notes if "guess" in n.message]) == 1
