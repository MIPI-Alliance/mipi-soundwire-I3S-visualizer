"""VCD (Value Change Dump) capture import tests.

Synthesises a VCD from the demo capture's clock/data edges and checks it decodes
to the same commands/columns, that $timescale drives the sample rate, that only
1-bit signals are offered as clock/data, and that auto-clock picks the busier line.

Run: QT_QPA_PLATFORM=offscreen PYTHONPATH=. python3 -m pytest tests/test_vcd.py
"""
import os
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np

from swi3s_studio import decode_capture
from swi3s_studio.ingest import transitions, vcd


def _write_vcd(path, cap, clk_id="!", dat_id="%", timescale="1ns", extra_vars=""):
    """Write a minimal VCD for a Capture: two 1-bit signals whose transitions are
    the clock/data edges (VCD time == sample index, so $timescale is the period)."""
    events = []
    lvl = 1 if cap.initial_clock else 0
    for t in cap.clock_edges.tolist():
        lvl ^= 1
        events.append((int(t), clk_id, lvl))
    lvl = 1 if cap.initial_data else 0
    for t in cap.data_edges.tolist():
        lvl ^= 1
        events.append((int(t), dat_id, lvl))
    events.sort(key=lambda e: e[0])
    lines = [
        f"$timescale {timescale} $end",
        "$scope module tb $end",
        f"$var wire 1 {clk_id} clk $end",
        f"$var wire 1 {dat_id} data $end",
    ]
    if extra_vars:
        lines.append(extra_vars)
    lines += [
        "$upscope $end",
        "$enddefinitions $end",
        "#0",
        "$dumpvars",
        f"{1 if cap.initial_clock else 0}{clk_id}",
        f"{1 if cap.initial_data else 0}{dat_id}",
        "$end",
    ]
    cur_t = None
    for t, ident, val in events:
        if t != cur_t:
            lines.append(f"#{t}")
            cur_t = t
        lines.append(f"{val}{ident}")
    with open(path, "w") as f:
        f.write("\n".join(lines) + "\n")


def test_parse_timescale():
    assert abs(vcd._parse_timescale("1ns") - 1e-9) < 1e-21
    assert abs(vcd._parse_timescale("10 ps") - 1e-11) < 1e-24
    assert abs(vcd._parse_timescale("100us") - 1e-4) < 1e-16
    for bad in ("", "ns", "5", "1 minute"):
        try:
            vcd._parse_timescale(bad)
            assert False, f"expected ValueError for {bad!r}"
        except ValueError:
            pass


def test_read_info_signals_and_rate():
    cap = transitions.demo_capture(16)
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "sim.vcd")
        # Include a multi-bit vector signal; it must NOT be a clock/data candidate.
        _write_vcd(p, cap, extra_vars="$var wire 8 v bus [7:0] $end")
        info = vcd.read_info(p)
        assert info.sample_rate_hz == 1_000_000_000            # 1 ns tick
        assert len(info.signals) == 3                          # clk, data, bus
        assert len(info.scalar_signals) == 2                   # only the 1-bit lines
        assert {s.name for s in info.scalar_signals} == {"clk", "data"}


def test_vcd_roundtrip_decodes():
    cap = transitions.demo_capture(16)
    ref = decode_capture(cap)
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "sim.vcd")
        _write_vcd(p, cap)
        cap2 = vcd.load_capture(p, "!", "%")
        # $timescale ticks map exactly onto samples: edges + initial levels preserved.
        np.testing.assert_array_equal(cap2.clock_edges, cap.clock_edges)
        np.testing.assert_array_equal(cap2.data_edges, cap.data_edges)
        assert cap2.initial_clock == cap.initial_clock
        assert cap2.initial_data == cap.initial_data
        res = decode_capture(cap2)
    assert res.column_count == ref.column_count == 16
    assert [c["command"] for c in res.commands] == [c["command"] for c in ref.commands]


def test_auto_clock_picks_busier_signal():
    cap = transitions.demo_capture(16)
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "sim.vcd")
        _write_vcd(p, cap)
        # Deliberately pass the data line as "clock": auto_clock must swap them so the
        # forwarded clock (the busier signal, "!") ends up as clock_edges.
        cap2 = vcd.load_capture(p, "%", "!", auto_clock=True)
        np.testing.assert_array_equal(cap2.clock_edges, cap.clock_edges)
        np.testing.assert_array_equal(cap2.data_edges, cap.data_edges)


def test_transition_counts():
    cap = transitions.demo_capture(16)
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "sim.vcd")
        _write_vcd(p, cap)
        counts = vcd.transition_counts(p)
        # The clock toggles every UI, so it has (many) more transitions than data.
        assert counts["!"] > counts.get("%", 0) > 0
        assert counts["!"] == cap.clock_edges.size


def test_vcd_workspace_source_roundtrip():
    """A VCD session persists to a workspace `source` descriptor and reloads to the
    same decode (session_from_source dispatch for type 'vcd')."""
    from swi3s_studio.session import Session
    from swi3s_studio.workspace import session_from_source
    cap = transitions.demo_capture(16)
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "sim.vcd")
        _write_vcd(p, cap)
        sess = Session.from_vcd(p, "!", "%")
        assert sess.source["type"] == "vcd"
        sess2 = session_from_source(sess.source)
        assert sess2.column_count == sess.column_count == 16


def test_dumpoff_no_phantom_edges():
    # A $dumpoff/$dumpon block emits x for every signal; those x-fills must NOT be
    # read as 0 (which would inject phantom 1->0 / 0->1 edges on a driven line).
    vcd_text = (
        "$timescale 1ns $end\n"
        "$var wire 1 ! clk $end\n"
        "$var wire 1 % dat $end\n"
        "$enddefinitions $end\n"
        "#0\n$dumpvars\n1!\n1%\n$end\n"    # clk=1, dat=1
        "#10\n0!\n"                          # real clk edge
        "#20\n$dumpoff\nx!\nx%\n$end\n"      # undump: x-fills (must be ignored)
        "#30\n$dumpon\n0!\n1%\n$end\n"       # redump: no real change (clk still 0, dat still 1)
        "#40\n1!\n"                          # real clk edge
    )
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "dumpoff.vcd")
        with open(p, "w") as f:
            f.write(vcd_text)
        cap = vcd.load_capture(p, "!", "%")
    assert cap.clock_edges.tolist() == [10, 40], cap.clock_edges.tolist()
    assert cap.data_edges.tolist() == [], cap.data_edges.tolist()   # no phantom from the x-fill
    assert cap.initial_clock is True and cap.initial_data is True


def test_header_comment_not_misparsed():
    # A $comment block whose free text contains $var / $timescale / $enddefinitions
    # must be skipped, not parsed (else a fake signal or early stop leaks in).
    vcd_text = (
        "$timescale 1ns $end\n"
        "$comment note $var wire 8 fakeid fakebus $end\n"   # decoy $var inside a comment
        "$var wire 1 ! clk $end\n"
        "$var wire 1 % dat $end\n"
        "$enddefinitions $end\n"
        "#0\n$dumpvars\n0!\n0%\n$end\n"
    )
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "comment.vcd")
        with open(p, "w") as f:
            f.write(vcd_text)
        info = vcd.read_info(p)
    assert {s.name for s in info.signals} == {"clk", "dat"}       # no phantom "fakebus"
    assert len(info.scalar_signals) == 2


def test_truncated_time_token_survives():
    # A stray bare '#' (truncated dump) must not abort the load with int('') -> error.
    vcd_text = (
        "$timescale 1ns $end\n"
        "$var wire 1 ! clk $end\n"
        "$var wire 1 % dat $end\n"
        "$enddefinitions $end\n"
        "#0\n0!\n0%\n"
        "#10\n1!\n"
        "#\n"                                # truncated time marker at end of dump
    )
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "trunc.vcd")
        with open(p, "w") as f:
            f.write(vcd_text)
        cap = vcd.load_capture(p, "!", "%")   # must not raise
    assert cap.clock_edges.tolist() == [10]
