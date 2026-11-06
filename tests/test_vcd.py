"""VCD (Value Change Dump) capture import tests.

Synthesises a VCD from the demo capture's clock/data edges and checks it decodes
to the same commands/columns, that $timescale drives the sample rate, that only
1-bit signals are offered as clock/data, and that auto-clock picks the busier line.

Run: QT_QPA_PLATFORM=offscreen PYTHONPATH=. python3 -m pytest tests/test_vcd.py
"""
import os
import re
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pytest

from swi3s_studio import decode_capture, import_notes
from swi3s_studio.ingest import transitions, vcd
from swi3s_studio.ingest.capture import Capture


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
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def _slow_vcd(tmp_path, timescale):
    p = tmp_path / "slow.vcd"
    cap = Capture(np.array([1, 2], dtype=np.uint64), np.array([2], dtype=np.uint64),
                  False, False, 1)
    _write_vcd(str(p), cap, timescale=timescale)
    return str(p)


@pytest.mark.parametrize("timescale", ["100 s", "10 s", "3 s", "2 s"])
def test_a_timescale_that_gives_a_rate_below_1_hz_is_refused_by_file(tmp_path, timescale):
    with pytest.raises(ValueError, match=r"slow\.vcd: the \$timescale of .* below 1 Hz"):
        vcd.read_info(_slow_vcd(tmp_path, timescale))


@pytest.mark.parametrize("timescale, rate", [("1 s", 1), ("1500 ms", 1), ("1 fs", 10 ** 15)])
def test_a_timescale_gives_its_reciprocal_rounded_to_a_whole_hertz(tmp_path, timescale, rate):
    assert vcd.read_info(_slow_vcd(tmp_path, timescale)).sample_rate_hz == rate


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
        counts = vcd.transition_counts_until(p)[0]
        # The clock toggles every UI, so it has (many) more transitions than data.
        assert counts["!"] > counts.get("%", 0) > 0
        assert counts["!"] == cap.clock_edges.size


def test_vcd_workspace_source_roundtrip():
    """A VCD session persists to a workspace `source` descriptor and reloads to the
    same decode (session_from_source dispatch for type 'vcd'): the same edges and commands."""
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
        assert sess2.capture.clock_edges.tolist() == sess.capture.clock_edges.tolist()
        assert sess2.capture.data_edges.tolist() == sess.capture.data_edges.tolist()
        assert ([c["start_sample"] for c in sess2.commands]
                == [c["start_sample"] for c in sess.commands])


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
        with open(p, "w", encoding="utf-8") as f:
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
        with open(p, "w", encoding="utf-8") as f:
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
        with open(p, "w", encoding="utf-8") as f:
            f.write(vcd_text)
        with import_notes.collect() as notes:
            cap = vcd.load_capture(p, "!", "%")   # must not raise
    assert cap.clock_edges.tolist() == [10]
    assert [str(n) for n in notes] == ['trunc.vcd: 1 "#" with no time was ignored (the dump '
                                       'may be truncated)']


def test_bounded_transition_counts_stop_early_and_say_so(tmp_path):
    lines = ["$timescale 1ns $end", "$var wire 1 ! clk $end", "$var wire 1 \" dat $end",
             "$enddefinitions $end", "#0", "0!", "0\""]
    for t in range(1, 2001):
        lines.append(f"#{t}")
        lines.append(f"{t % 2}!")
    p = tmp_path / "long.vcd"
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    full, cut = vcd.transition_counts_until(str(p), None)[:2]
    assert not cut and full["!"] == 2000
    part, cut = vcd.transition_counts_until(str(p), None, 500)[:2]
    assert cut and 0 < part["!"] < full["!"]
    assert vcd.transition_counts_until(str(p))[0] == full


_VCD_HEAD = (
    "$timescale 1ns $end\n"
    "$var wire 1 ! clk $end\n"
    "$var wire 1 % dat $end\n"
    "$enddefinitions $end\n"
)


def _load_text(tmp_path, body):
    p = tmp_path / "t.vcd"
    p.write_text(_VCD_HEAD + body, encoding="utf-8")
    with import_notes.collect() as notes:
        cap = vcd.load_capture(str(p), "!", "%")
    return cap, [str(n) for n in notes]


def test_malformed_time_stamp_names_the_file_and_token(tmp_path):
    for bad in ("#2.5", "#-5", "#abc", "#" + "9" * 30, "#\u0663", f"#{1 << 64}"):
        p = tmp_path / "bad.vcd"
        p.write_text(_VCD_HEAD + f"#0\n0!\n0%\n{bad}\n1!\n", encoding="utf-8")
        shown = bad if len(bad) <= 40 else bad[:40] + "\u2026"
        with pytest.raises(ValueError, match=r"bad\.vcd: " + re.escape(repr(shown))
                           + " is not a time stamp"):
            vcd.load_capture(str(p), "!", "%")


def test_unterminated_comment_is_reported(tmp_path):
    cap, notes = _load_text(
        tmp_path, "#0\n0!\n0%\n$comment never ends\n#10\n1!\n#20\n0!\n")
    assert cap.clock_edges.size == 0
    assert any("$comment block has no $end" in n for n in notes)


def test_changes_in_one_time_step_collapse_to_the_final_level(tmp_path):
    cap, notes = _load_text(tmp_path, "#0\n0!\n0%\n#10\n1!\n0!\n1!\n#20\n0!\n")
    assert cap.clock_edges.tolist() == [10, 20]
    assert any("2 value changes on signal 'clk'" in n and "#10" in n for n in notes)
    cap, notes = _load_text(tmp_path, "#0\n0!\n0%\n#10\n1!\n0!\n#20\n1!\n")
    assert cap.clock_edges.tolist() == [20]          # net no change at #10


@pytest.mark.parametrize("tick", [1 << 63, 1 << 64, (1 << 64) - 1])
def test_a_time_stamp_of_2_pow_63_or_more_is_refused(tmp_path, tick):
    with pytest.raises(ValueError, match=r"t\.vcd: .* is not a time stamp"):
        _load_text(tmp_path, f"#0\n0!\n0%\n#{tick}\n1!\n")


def test_a_time_stamp_of_2_pow_63_minus_1_is_read(tmp_path):
    cap, _notes = _load_text(tmp_path, f"#0\n0!\n0%\n#{(1 << 63) - 1}\n1!\n")
    assert cap.clock_edges.tolist() == [(1 << 63) - 1]


def test_data_changes_in_one_time_step_collapse_to_the_final_level(tmp_path):
    cap, notes = _load_text(tmp_path, "#0\n0!\n0%\n#10\n1%\n0%\n1%\n#20\n0%\n")
    assert cap.data_edges.tolist() == [10, 20]
    assert any("2 value changes on signal 'dat'" in n and "#10" in n for n in notes)
    cap, notes = _load_text(tmp_path, "#0\n0!\n0%\n#10\n1%\n0%\n#20\n1%\n")
    assert cap.data_edges.tolist() == [20]           # net no change at #10
    assert any("1 value change on signal 'dat'" in n for n in notes)
    assert cap.clock_edges.size == 0


def test_an_unterminated_block_is_reported_by_its_keyword_once(tmp_path):
    cap, notes = _load_text(
        tmp_path, "#0\n0!\n0%\n$dumpoff $comment two\n#10\n1!\n")
    open_notes = [n for n in notes if "has no $end" in n]
    assert len(open_notes) == 1 and "$comment block has no $end" in open_notes[0]


def test_a_dump_on_one_line_is_read_in_steps(tmp_path, monkeypatch):
    changes = " ".join(f"#{t} {t % 2}!" for t in range(1, 40001))
    p = tmp_path / "flat.vcd"
    p.write_text(_VCD_HEAD.replace("\n", " ") + "#0 0! 0% " + changes + "\n", encoding="utf-8")
    assert len(p.read_text(encoding="utf-8").splitlines()) == 1
    step = 1024
    reads = []
    real = vcd._tokens

    class _Spy:
        def __init__(self, f):
            self.f, self.name = f, f.name

        def read(self, n):
            got = self.f.read(n)
            reads.append((n, len(got)))
            return got

    monkeypatch.setattr(vcd, "_READ_CHARS", step)
    monkeypatch.setattr(vcd, "_tokens", lambda f: real(_Spy(f)))
    cap = vcd.load_capture(str(p), "!", "%")
    assert cap.clock_edges.size == 40000
    assert cap.clock_edges.tolist() == list(range(1, 40001))
    assert len(reads) > 100                          # many steps, not one read of the file
    assert all(n == step and got <= step for n, got in reads)


def test_a_token_that_straddles_a_read_step_is_not_split(tmp_path):
    step = vcd._READ_CHARS
    pad = " " * (step - len(_VCD_HEAD) - len("#0\n0!\n0%\n#"))
    p = tmp_path / "edge.vcd"
    p.write_text(_VCD_HEAD + "#0\n0!\n0%\n" + pad + "#123456\n1!\n", encoding="utf-8")
    assert vcd.load_capture(str(p), "!", "%").clock_edges.tolist() == [123456]
    with open(p, encoding="utf-8") as f:
        assert "".join(vcd._tokens(f)) == "".join(p.read_text(encoding="utf-8").split())


def test_a_token_longer_than_the_limit_is_refused_naming_the_file(tmp_path):
    p = tmp_path / "junk.vcd"
    p.write_text("$timescale 1ns $end " + "a" * (vcd._MAX_TOKEN_CHARS + 1), encoding="utf-8")
    with pytest.raises(ValueError, match=r"junk\.vcd: not a VCD"):
        vcd.read_info(str(p))
    p.write_text("$timescale 1ns $end " + "a" * vcd._MAX_TOKEN_CHARS + " $end", encoding="utf-8")
    assert vcd.read_info(str(p)).sample_rate_hz == 1_000_000_000


def test_a_time_stamp_of_thousands_of_digits_is_refused_by_name(tmp_path):
    p = tmp_path / "long.vcd"
    p.write_text(_VCD_HEAD + "#0\n0!\n0%\n#" + "9" * 6000 + "\n1!\n", encoding="utf-8")
    with pytest.raises(ValueError, match=r"long\.vcd: '#9+…' is not a time stamp"):
        vcd.load_capture(str(p), "!", "%")
    q = tmp_path / "zeros.vcd"
    q.write_text(_VCD_HEAD + "#0\n0!\n0%\n#" + "0" * 6000 + "5\n1!\n", encoding="utf-8")
    assert vcd.load_capture(str(q), "!", "%").clock_edges.tolist() == [5]


def test_a_header_with_digits_that_are_not_ascii_or_far_too_many_is_refused_or_ignored(tmp_path):
    p = tmp_path / "odd.vcd"
    p.write_text("$timescale 1ns $end $var wire \u00b2 ! a $end $var wire " + "9" * 5000
                 + " % b $end $enddefinitions $end", encoding="utf-8")
    assert [s.width for s in vcd.read_info(str(p)).signals] == [0, 0]
    p.write_text("$timescale 1\u0663ns $end $enddefinitions $end", encoding="utf-8")
    with pytest.raises(ValueError, match=r"odd\.vcd: unrecognised \$timescale"):
        vcd.read_info(str(p))


def test_three_changes_in_one_time_step_keep_the_edge_before_it(tmp_path):
    cap, notes = _load_text(tmp_path, "#0\n0!\n0%\n#5\n1!\n#10\n0!\n1!\n0!\n")
    assert cap.clock_edges.tolist() == [5, 10]
    assert any("2 value changes on signal 'clk'" in n and "#10" in n for n in notes)
    cap, _notes = _load_text(tmp_path, "#0\n0!\n0%\n#5\n1%\n#10\n0%\n1%\n0%\n")
    assert cap.data_edges.tolist() == [5, 10]


def test_the_busier_signal_takes_its_initial_level_with_it_as_the_clock(tmp_path):
    body = "#0\n0!\n1%\n#10\n0%\n#20\n1%\n1!\n#30\n0%\n"
    cap, _notes = _load_text(tmp_path, body)
    assert cap.clock_edges.size == 1 and cap.data_edges.size == 3
    assert (cap.initial_clock, cap.initial_data) == (False, True)
    p = tmp_path / "t.vcd"
    cap = vcd.load_capture(str(p), "!", "%", auto_clock=True)
    assert cap.clock_edges.tolist() == [10, 20, 30] and cap.data_edges.tolist() == [20]
    assert (cap.initial_clock, cap.initial_data) == (True, False)


def test_equal_counts_keep_the_clock_as_given(tmp_path):
    p = tmp_path / "t.vcd"
    p.write_text(_VCD_HEAD + "#0\n0!\n1%\n#10\n1!\n0%\n", encoding="utf-8")
    cap = vcd.load_capture(str(p), "!", "%", auto_clock=True)
    assert cap.clock_edges.tolist() == [10] and cap.data_edges.tolist() == [10]
    assert (cap.initial_clock, cap.initial_data) == (False, True)


def test_a_real_change_whose_ident_starts_with_a_level_digit_is_not_a_scalar_change(tmp_path):
    p = tmp_path / "real.vcd"
    p.write_text("$timescale 1ns $end\n$var wire 1 ! clk $end\n$var wire 1 % dat $end\n"
                 "$var real 64 1! volts $end\n$enddefinitions $end\n"
                 "#0\n0!\n0%\n#10\nr2.5 1!\nR3.5 0!\nb101 1!\nB11 0!\n#20\n1!\n",
                 encoding="utf-8")
    assert vcd.load_capture(str(p), "!", "%").clock_edges.tolist() == [20]
    assert vcd.transition_counts_until(str(p))[0] == {"!": 1}


def test_an_ident_declared_twice_keeps_its_first_name(tmp_path):
    p = tmp_path / "alias.vcd"
    p.write_text("$timescale 1ns $end\n$var wire 1 ! clk $end\n$var wire 1 ! clk_again $end\n"
                 "$enddefinitions $end\n", encoding="utf-8")
    assert [(s.ident, s.name) for s in vcd.read_info(str(p)).signals] == [("!", "clk")]


def test_a_signal_has_the_name_and_bit_select_without_its_scope(tmp_path):
    p = tmp_path / "scoped.vcd"
    p.write_text("$timescale 1ns $end\n$scope module top $end\n$scope module u1 $end\n"
                 "$var wire 1 ! clk $end\n$upscope $end\n$var wire 1 % bit [3] $end\n"
                 "$upscope $end\n$enddefinitions $end\n", encoding="utf-8")
    assert [s.name for s in vcd.read_info(str(p)).signals] == ["clk", "bit[3]"]


def test_x_values_after_a_dumpoff_block_are_reported_again(tmp_path):
    body = "#0\n0!\n0%\n#10\n$dumpoff\nx!\nx%\n$end\n#20\nx!\n"
    _cap, notes = _load_text(tmp_path, body)
    assert any("1 x/z value on signal 'clk' (first at #20) was read" in n for n in notes)
    assert not any("signal 'dat'" in n and "x/z" in n for n in notes)


def test_a_bounded_count_stops_after_the_given_number_of_changes(tmp_path):
    lines = ["$timescale 1ns $end", "$var wire 1 ! clk $end", "$enddefinitions $end", "#0", "0!"]
    for t in range(1, 6):
        lines += [f"#{t}", f"{t % 2}!"]
    p = tmp_path / "five.vcd"
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    counts, cut = vcd.transition_counts_until(str(p), None, 3)[:2]
    assert cut and counts == {"!": 2}             # the initial value and two changes
    counts, cut = vcd.transition_counts_until(str(p), None, 6)[:2]
    assert not cut and counts == {"!": 5}
    counts, cut = vcd.transition_counts_until(str(p), None, 5)[:2]
    assert cut and counts == {"!": 4}


def test_the_edge_budget_is_checked_every_so_many_edges_and_refuses_only_when_exceeded(
        tmp_path, monkeypatch):
    monkeypatch.setattr(vcd, "_CHECK_EVERY", 4)
    lines = ["$timescale 1ns $end", "$var wire 1 ! clk $end", "$enddefinitions $end", "#0", "0!"]
    for t in range(1, 5):
        lines += [f"#{t}", f"{t % 2}!"]
    p = tmp_path / "four.vcd"
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    held = 4 * vcd.BYTES_PER_EDGE
    with import_notes.collect():                      # the data signal is not declared
        assert vcd.load_capture(str(p), "!", "%", max_bytes=held).clock_edges.size == 4
        with pytest.raises(vcd.VcdTooLargeError):
            vcd.load_capture(str(p), "!", "%", max_bytes=held - 1)
        monkeypatch.setattr(vcd, "_CHECK_EVERY", 5)   # the fifth edge is never reached
        assert vcd.load_capture(str(p), "!", "%", max_bytes=1).clock_edges.size == 4


def test_a_clock_signal_that_is_also_the_data_signal_is_refused_by_file(tmp_path):
    p = tmp_path / "same.vcd"
    p.write_text(_VCD_HEAD + "#0\n0!\n0%\n#10\n1!\n", encoding="utf-8")
    with pytest.raises(ValueError, match=r"same\.vcd: the clock and the data are the same "
                                         r"signal"):
        vcd.load_capture(str(p), "!", "!")


def _vector_vcd(tmp_path, decls, body, clock="!", data="%"):
    p = tmp_path / "v.vcd"
    p.write_text("$timescale 1ns $end\n" + decls + "$enddefinitions $end\n" + body,
                 encoding="utf-8")
    with import_notes.collect() as notes:
        cap = vcd.load_capture(str(p), clock, data)
    return cap, [str(n) for n in notes], p


_ONE_BIT = "$var reg 1 ! clk [0:0] $end\n$var reg 1 % dat [0:0] $end\n"


@pytest.mark.parametrize("zero,one", [("b0", "b1"), ("B0", "B1")])
def test_a_one_bit_vector_value_change_is_read_as_that_level(tmp_path, zero, one):
    scalar, _n, _p = _vector_vcd(tmp_path, _ONE_BIT,
                                 "#0\n0!\n0%\n#2\n1!\n#4\n1%\n#6\n0!\n#8\n0%\n")
    body = f"#0\n{zero} !\n{zero} %\n#2\n{one} !\n#4\n{one} %\n#6\n{zero} !\n#8\n{zero} %\n"
    cap, notes, p = _vector_vcd(tmp_path, _ONE_BIT, body)
    assert cap.clock_edges.tolist() == scalar.clock_edges.tolist() == [2, 6]
    assert cap.data_edges.tolist() == scalar.data_edges.tolist() == [4, 8]
    assert notes == []
    assert vcd.transition_counts_until(str(p))[0] == {"!": 2, "%": 2}


def test_a_one_bit_vector_unknown_value_is_reported_as_x_z(tmp_path):
    _cap, notes, _p = _vector_vcd(tmp_path, _ONE_BIT, "#0\nb0 !\nb0 %\n#5\nbx !\n#9\nb1 !\n")
    assert any("1 x/z value on signal 'clk[0:0]'" in n for n in notes)


def test_a_vector_or_real_change_of_a_watched_signal_is_reported_not_dropped(tmp_path):
    decls = "$var wire 8 ! bus [7:0] $end\n$var wire 1 % dat $end\n"
    cap, notes, p = _vector_vcd(tmp_path, decls, "#0\nb0 !\n0%\n#5\nb101 !\nb11 !\n#9\n1%\n")
    assert cap.clock_edges.size == 0 and cap.data_edges.tolist() == [9]
    assert any("3 vector or real value changes on signal 'bus[7:0]' (first at #0) were "
               "not read" in n for n in notes)
    assert vcd.transition_counts_until(str(p))[0] == {"%": 1}   # a bus is not a scalar change
    decls = "$var real 64 ! volts $end\n$var wire 1 % dat $end\n"
    _cap, notes, _p = _vector_vcd(tmp_path, decls, "#0\n0%\n#5\nr2.5 !\n")
    assert any("1 vector or real value change on signal 'volts' (first at #5) was not read"
               in n for n in notes)
    _cap, notes, _p = _vector_vcd(tmp_path, decls, "#0\n0%\n#5\nbxxxx !\n")
    assert not any("not read" in n for n in notes)           # all-x fill is not a change


def test_a_timescale_with_no_end_reads_a_few_words_and_names_the_missing_end(tmp_path):
    p = tmp_path / "open.vcd"
    p.write_text("$timescale 1 ns", encoding="utf-8")
    with pytest.raises(ValueError, match=r"open\.vcd: the \$timescale block has no \$end"):
        vcd.read_info(str(p))


def test_a_long_unterminated_timescale_is_refused_with_a_short_message(tmp_path, monkeypatch):
    p = tmp_path / "long.vcd"
    p.write_text("$timescale 1 ns " + "a " * 200_000, encoding="utf-8")
    taken = []
    real = vcd._declaration

    def spy(toks):
        words, state = real(toks)
        taken.append((len(words), state))
        return words, state

    monkeypatch.setattr(vcd, "_declaration", spy)
    with pytest.raises(ValueError, match=r"long\.vcd: unrecognised \$timescale") as caught:
        vcd.read_info(str(p))
    assert len(str(caught.value)) < len(str(p)) + 120
    assert taken == [(vcd._MAX_DECL_TOKENS, "long")]


def test_a_long_timescale_text_is_cut_in_the_error(tmp_path):
    p = tmp_path / "wide.vcd"
    p.write_text("$timescale " + "x" * 5000 + " $end", encoding="utf-8")
    with pytest.raises(ValueError, match=r"wide\.vcd: unrecognised \$timescale 'x+…'") as caught:
        vcd.read_info(str(p))
    assert len(str(caught.value)) < len(str(p)) + 120


def test_a_var_with_no_end_holds_a_bounded_declaration(tmp_path, monkeypatch):
    p = tmp_path / "var.vcd"
    p.write_text("$timescale 1ns $end\n$var wire 1 ! clk $end\n$var wire 1 % " +
                 "a " * 100_000, encoding="utf-8")
    sizes = []
    real = vcd._declaration

    def spy(toks):
        words, state = real(toks)
        sizes.append(len(words))
        return words, state

    monkeypatch.setattr(vcd, "_declaration", spy)
    info = vcd.read_info(str(p))
    assert [s.name for s in info.signals] == ["clk"]            # the long one is no declaration
    assert max(sizes) <= vcd._MAX_DECL_TOKENS
    sizes.clear()
    with import_notes.collect():
        vcd.load_capture(str(p), "!", "%")
    assert sizes and max(sizes) <= vcd._MAX_DECL_TOKENS


def test_a_bit_select_var_is_still_a_declaration(tmp_path):
    p = tmp_path / "sel.vcd"
    p.write_text("$timescale 1ns $end\n$var wire 1 ! bus [3] $end\n$enddefinitions $end\n",
                 encoding="utf-8")
    assert [s.name for s in vcd.read_info(str(p)).signals] == ["bus[3]"]


@pytest.mark.parametrize("head", [
    "$timescale 1ns $end\n$var wire 1 ! clk $end\n$var wire 1 % dat $end\n",
    "$timescale 1ns $end\n$var wire 1 ! clk $end\n$var wire 1 % dat\n",
])
def test_a_dump_with_no_enddefinitions_says_the_header_is_unterminated(tmp_path, head):
    """The value changes of a dump whose header never ends are not read; the note names
    the missing $enddefinitions, not a signal with no value change."""
    p = tmp_path / "cut.vcd"
    p.write_text(head + "#0\n0!\n0%\n#10\n1!\n", encoding="utf-8")
    with import_notes.collect() as notes:
        cap = vcd.load_capture(str(p), "!", "%")
    assert cap.clock_edges.size == 0 and cap.data_edges.size == 0
    msgs = [str(n) for n in notes]
    assert len(msgs) == 1, msgs
    assert msgs[0].startswith("cut.vcd: the header has no $enddefinitions")
    assert "no 0/1 value change" not in msgs[0]


def test_a_dump_with_its_enddefinitions_makes_no_header_note(tmp_path):
    cap, notes = _load_text(tmp_path, "#0\n0!\n0%\n#10\n1!\n")
    assert cap.clock_edges.tolist() == [10] and notes == []
