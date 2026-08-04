"""Periodic SSPAs in the demo captures.

An SSPA (Stream Sync Point Announce — Announce phase, opcode 0x00) generates an SSP
without committing anything. Its job is to re-assert data-port synchronization: a port
that has drifted is pulled back into phase, and an *unexpected* SSPA raises a peripheral
interrupt. Every demo region with active data ports now emits them at a ~100 ms cadence.

The delicate part is WHERE. The decoder takes an SSPA's SSP as
(row of its last CDS bit) + 1 + Row_Delay - SyncPointOffset and re-anchors every port's
row_in_interval to 0 there. Landing on any row that isn't already a port sync point
SHIFTS the transport phase and garbles the audio — the same 1/N-phases failure a
post-commit capture has. So the demo picks the target SSP row first (aligned to the LCM
of the enabled ports' interval periods) and back-computes where the command must start.
A one-row misplacement is not subtle: it drops the decoded tone SNR from ~33 dB to ~13 dB.
"""
import numpy as np
import pytest

from swi3s_studio.session import Session

# Demo variants and the number of audio regions each has (regions with active data
# ports — the Safe-Lock bring-up region carries none and must NOT get an SSPA).
_DEMOS = [
    ("phy1", dict(phy=1, cold_start=True), 1),          # 4-col, mid-capture REPOSITION
    ("phy2", dict(phy=2, cold_start=True), 2),          # 8-col then 16-col
    ("phy3", dict(phy=3, cold_start=True), 1),          # DLV, Safe-Lock-4 -> 16-col
    ("flow", dict(phy=2, variant="flow_control"), 1),   # flow-control ports
]
_SAMPLES = 1500          # deep enough that every region can hold at least one SSPA


@pytest.fixture(scope="module")
def sessions():
    return {name: Session.from_demo(_SAMPLES, **kw) for name, kw, _n in _DEMOS}


def _sspas(session):
    return [c for c in session.commands if c.get("command") == "SSPA"]


def _tone_snr(a) -> float:
    """Crude single-tone SNR (dB) of a decoded PCM stream: dominant FFT bin vs the rest.
    A cleanly decoded sine reads well above 0; a phase-shifted one collapses."""
    a = np.asarray(a, dtype=np.float64)
    if a.size < 512:
        return -99.0
    a = a - a.mean()
    w = np.hanning(min(4096, a.size))
    A = np.abs(np.fft.rfft(a[:w.size] * w)) ** 2
    A[0] = 0.0
    k = int(np.argmax(A))
    sig = A[max(1, k - 2):k + 3].sum()
    return 10.0 * np.log10(sig / (A.sum() - sig + 1e-9))


@pytest.mark.parametrize("name,_kw,_n", _DEMOS, ids=[d[0] for d in _DEMOS])
def test_demo_emits_sspas(sessions, name, _kw, _n):
    """Every demo with active data ports carries at least one SSPA."""
    assert _sspas(sessions[name]), f"{name}: no SSPA emitted"


@pytest.mark.parametrize("name,_kw,_n", _DEMOS, ids=[d[0] for d in _DEMOS])
def test_sspas_are_well_formed(sessions, name, _kw, _n):
    """An SSPA must decode as a sync-point-carrying, NON-commit command with a valid
    CRC — the decoder's `sspaReanchor` path keys on exactly that combination."""
    for c in _sspas(sessions[name]):
        assert c.get("crc_valid"), f"{name}: SSPA with bad CRC at {c.get('start_sample')}"
        assert c.get("has_sync_point") is True
        assert not c.get("is_commit"), "an SSPA announces; it must not read as a commit"


@pytest.mark.parametrize("name,_kw,_n", _DEMOS, ids=[d[0] for d in _DEMOS])
def test_sspas_do_not_corrupt_the_capture(sessions, name, _kw, _n):
    """Adding SSPAs must not introduce a single CRC error anywhere: a mis-sized or
    mis-framed SSPA would desync the 8b10b stream and redden later commands too."""
    bad = [c for c in sessions[name].commands if not c.get("crc_valid", True)]
    assert not bad, f"{name}: {len(bad)} CRC-invalid command(s)"


@pytest.mark.parametrize("name,_kw,_n", [d for d in _DEMOS if d[0] != "flow"],
                         ids=[d[0] for d in _DEMOS if d[0] != "flow"])
def test_audio_survives_the_sspa_reanchor(sessions, name, _kw, _n):
    """THE point of aligned placement: re-anchoring at a row the ports already treat as
    their sync point re-asserts the phase instead of shifting it, so every PCM stream
    stays a clean single tone and yields exactly the expected sample count.

    Excludes the flow-control demo, whose ports gate transport on DRQ/SourceReady and so
    don't produce a fixed-length pure tone."""
    s = sessions[name]
    assert _sspas(s), "precondition: this demo must contain an SSPA"
    store = s.audio_store()
    checked = 0
    for dev, dp in store.streams():
        for ch in store.channels(dev, dp):
            a = store.samples(dev, dp, ch)
            if store.native_sample_bits(dev, dp, ch) == 1:
                continue                     # PDM: density stream, not a tone here
            assert len(a) == _SAMPLES, (
                f"{name} dev{dev} dp{dp} ch{ch}: {len(a)} samples, expected {_SAMPLES}")
            snr = _tone_snr(a)
            assert snr > 20.0, f"{name} dev{dev} dp{dp} ch{ch}: tone SNR {snr:.1f} dB"
            checked += 1
    assert checked > 0, "no PCM stream checked"


def test_sspa_lands_on_a_port_sync_point(sessions):
    """Structural check on placement: each SSPA's SSP row must be congruent, modulo the
    ports' interval alignment, with the region's own commit SSP. This is the invariant
    the demo's back-computed start row exists to satisfy.

    The SSP row is derived from the command's END (its last CDS bit) — `bus_row` is the
    phase START, so using it directly is off by the command's length."""
    s = sessions["phy2"]
    sspas = _sspas(s)
    assert sspas
    # Region anchors: the confirmed commits' effective rows (authoritative SSP rows).
    commits = sorted(int(c["effective_row"]) for c in s.commands
                     if c.get("is_commit") and c.get("commit_confirmed")
                     and int(c.get("effective_row", -1)) >= 0)
    assert commits, "no confirmed commit SSP rows to anchor against"
    for c in sspas:
        last_row = s.bus_row_for_sample(int(c["end_sample"]))
        ssp_row = last_row + 1 + int(c.get("row_delay", 0))
        anchor = max((r for r in commits if r <= ssp_row), default=commits[0])
        delta = ssp_row - anchor
        # PCM interval is 63 (8-col) / 31 (16-col) -> 64 / 32 rows; PDM 2 / 1. The LCM is
        # 64 or 32, and both are multiples of 32, so congruence mod 32 must hold in either
        # region for the re-anchor to land on an existing port sync point.
        assert delta % 32 == 0, (
            f"SSPA SSP row {ssp_row} is {delta} rows past anchor {anchor} — "
            f"not an interval-aligned sync point")


def test_no_sspa_before_audio_starts(sessions):
    """The bring-up / Safe-Lock region has no active data ports, so there is nothing to
    re-synchronize and it must carry no SSPA."""
    s = sessions["phy2"]
    audio_start = int(s.audio_start_sample)
    early = [c for c in _sspas(s) if int(c["start_sample"]) < audio_start]
    assert not early, f"{len(early)} SSPA(s) before audio started"


def test_sspa_is_offered_in_the_command_filter():
    """The Commands filter is populated from the kinds actually present, so SSPA must
    show up there once the demos emit it — that's what makes it filterable."""
    from PySide6.QtWidgets import QApplication

    from swi3s_studio.ui.main_window import MainWindow

    app = QApplication.instance() or QApplication([])
    w = MainWindow()
    try:
        w.load_session(Session.from_demo(_SAMPLES, phy=2, cold_start=True))
        app.processEvents()
        kinds = [a.text() for a in w._kind_menu.actions() if a.isCheckable()]
        assert "SSPA" in kinds, kinds
    finally:
        w.join_worker_threads()


def test_timeline_marks_an_sspa_as_a_sync_point():
    """An SSPA re-anchors every port, so the timeline must not bury it under the generic
    'Other command' colour — it reads as an SSP (and wins a colliding Ping's pixel)."""
    from swi3s_studio.ui import timeline as T

    sspa = {"command": "SSPA", "has_sync_point": True, "is_commit": False,
            "crc_valid": True, "has_manager_packet": True}
    assert T._kind_label(sspa) == "SSP Announce"
    assert T.tick_rank(sspa) > T.tick_rank({"command": "Ping", "crc_valid": True})
    assert any(row[0] == "SSP Announce" for row in T._MARK_LEGEND_SPEC), \
        "the legend must document the mark it draws"


def test_flow_control_handshake_stays_clean_across_an_sspa():
    """A correctly-placed SSPA must not invent a DRQ<->TxPresent violation.

    CPayloadEngine::SyncToSSP used to clear the DRQ history unconditionally, so the first
    TxPresent after any re-anchor had nothing to pair with — a real DRQ, sent d intervals
    earlier, reported as a handshake failure. No demo exercised that until SSPAs existed.
    The history is now preserved when the port is already at its sync point (an aligned
    SSPA re-asserts the phase; it doesn't move it)."""
    import swi3score

    from swi3s_studio.ingest import transitions

    cap = transitions.demo_capture(200, variant="flow_control")
    dec = swi3score.Decoder(cap.sample_source(), swi3score.DecoderSettings())
    dec.run()
    fc = dec.flow_control_stats()
    assert fc["checked"] > 0 and fc["drq_bits"] > 0
    assert fc["fails"] == 0, f"{fc['fails']} handshake violations of {fc['checked']}"
