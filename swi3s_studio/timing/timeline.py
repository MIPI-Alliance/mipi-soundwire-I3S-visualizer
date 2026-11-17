"""Per-leg timelines: every event of an inequality on one time axis, at its corner.

`leg_timeline(inp, leg)` places each event a leg's terms connect on the ramp it belongs to:
the Manager's clock reference, a peripheral's detection of the clock, a launch's anchor, the
receiver's threshold crossing and its setup or hold deadline. Time is in ns from the leg's
first clock edge at the Manager's pin: from the start of that edge's ramp on M→P and P→M legs,
and from its reference, its 20 % point, on P→P legs. EVERY EDGE IS PLACED BY ITS 20 % POINT,
as the calculator places it: consecutive clock edges' references are one UI apart, and one
edge's references at two pins are the flight between them apart, whatever each ramp's slew.
A clock edge two peripherals both detect has one t_RF at both pins (`calculator.pp_clock_edges`).
A P→P placement whose bus term nets to zero is drawn with both peripherals at the Manager, and
so is PP_contention under a placement that puts every peripheral at one end. Every flight is
the peripheral's distance a placement gives (`calculator.peripheral_distance_cm`), 0 with every
peripheral at the Manager.
A gathered P→P crossing, the difference of two detections of one edge, is drawn as the
ramp of the device the span views; the other end is a labelled instant with no ramp of its own.
Each ramp carries what it is drawn with (its edge's own t_RF, its shape, the rail it swings
to), and each crossing carries its threshold corner (`CrossingCorner`): the receiver's level
against its own ground and the ground offset that puts it on the transmitter's.

THE SPANS ARE THE TERMS. The timeline holds one span per term of the leg's breakdown, between
the two events that term connects, as long as the term's value, and one span for the margin:
from the threshold crossing to the setup or hold deadline, from the release to the
acquirer's 20 % point, or from the keeper having followed to the release. Each term is
measured at the pin it is specified at: a launch from the launcher's clock to its data at its
own pin, a flight between two pins, a crossing or a detection at the receiver's.
`tests/test_timing_timeline.py` checks that, and that every event lies on its ramp.

THE CLOCK HAS EVERY EDGE AT EVERY PIN IT IS DRAWN AT (`_Builder.complete_clock`): from the
leg's first clock edge to its last, one UI apart and alternating in polarity, at each pin the
leg places a clock edge at, an edge's references at two pins the clock's flight between them
apart. An edge no term connects at a pin is a ramp with no event on it. `clock_edges` goes on
past the leg's first and last edges at a pin, so a window wider than the leg's edges draws the
clock as it runs there.

Each span names the ramps its term is drawn with (`Span.view`): the edges the term connects,
and for a setup or hold window the other signal at that pin too. A span's `guides` are
related intervals drawn beside it: the margin beside a setup or hold window, a release, a
turn-on or a keeper's response, and a Peripheral's data flight to the keeper. A flight
mismatch has no ramps: its guides are the clock's and the data's flights from the launch, and
it is their difference. One device has one supply in a leg: every ramp it drives swings to
the same rail.

The events are placed from the inputs (the edges' slews, the crossing corners, the launch
anchors), not from the breakdown's terms, so the spans equalling the terms is a check that the
two agree.

Every leg in `calculator.INEQUALITIES` has a timeline, except where its breakdown does not
apply (`InequalityBreakdown.not_applicable`); `leg_timeline` refuses such a leg with a
ValueError that names the reason.

THE CHAINS CLOSE ON THE MARGIN. Every leg also carries a `WholeView`: every term on one of
two chains of spans end to end in time, from one start to one end, the second chain ending in
the margin. A span on the first chain lasts its term's value and one on the second the
negative of it, so the margin is the sum of the terms; every span but the margin is one term
of the breakdown, labelled with its symbol, or one detection of a gathered term.
A FLIGHT THAT NO TERM SPANS IS NEVER DRAWN. Where a leg's two chains each cross the same
flight between two pins, so that the flights cancel (PM_contention, PP_contention,
keeper_Per), the view is drawn at one pin, the other pin's part of a chain moved there by its
flight, and `WholeView.note` says so; every chain link is a term or the margin
(`tests/test_timing_timeline.py`). Each span is measured at a pin the view draws, the pin the
leg is checked at last.
- M→P, at the Peripheral alone, the handover variants too: the launch moved there by the
  clock's flight, the flight mismatch, the data's crossing, on setup the setup time and the
  margin, against the UI and the sampling edge's detection; on hold the data's chain is
  first, against the clock's detection, the hold time and the margin. MP_hold_ho counts its
  handover UIs on the Peripheral's clock edges, the Manager's moved by the clock's flight,
  from the sampling edge's reference to the launching edge's.
- P→M, at the Peripheral and the Manager: from the Manager's clock reference the clock's
  flight, the Peripheral's detection and launch, the data's flight, the flight mismatch and
  the data's crossing at the Manager, on setup the setup time and the margin, against the UI; on hold that chain is
  first, against the hold time and the margin. PM_hold_ho counts its handover UIs at the
  Peripheral, after the clock's flight.
- P→P, at both peripherals: a gathered term's two detections are two spans, each at its own
  peripheral from that peripheral's reference of the edge it detects, labelled with its own
  crossing, together the term's value. The data's chain starts from B's reference of the edge
  B samples on hold, or of A's launching edge on setup; a placement that nets the bus to zero
  has no flight between the peripherals, A's and B's pins one instant apart. The trace
  mismatch, t_PD,mis,AB, is a span on B's data from where the clock's flight puts A's data to
  where it arrives, later on setup and earlier on hold. PP_hold_ho counts
  its handover UIs at A, from A's reference of the edge B samples.
- Contention, at the releaser's pin, on M→P at the acquirer's too, the releaser's last: the
  acquirer's chain from the edge that closes the releaser's UI, or opens it under
  EndDriveEarly, through the UI, the handover UIs, its detection and turn-on, to its drive at
  the releaser's pin; against the release (t_DZ, or the launch, the ramp to its 80 % point
  and the EDE Delay) from the same edge, and the margin. P→M is drawn at the Peripheral
  alone and P→P at A alone, where contention starts: the acquirer's chain moved there by the
  clock's flight, which the acquirer's drive's flight back cancels, its UIs on the releaser's
  clock edges and on P→P B's detection a mark on no drawn ramp (`WholeView.note` says so,
  and on P→P that the UIs are counted at B). The trace mismatch, the acquirer's chain's last
  term, runs back from there to the acquirer's 20 % point at the releaser's pin.
- Keeper, at the releaser's pin: the release, from the edge that opens the releaser's UI or
  from its data's 20 % point under EndDriveEarly, against the launch, the data's crossing of
  the keeper's threshold, the keeper's response and the margin. keeper_Per is drawn at the
  Peripheral alone: the crossing, the response and the release as the Manager sees it moved
  back there by the data's flight, which the release's flight equals, so they lie on the
  Peripheral's data and the keeper's threshold is read on it (`WholeView.note` says so, and
  that the keeper acts at the Manager, a flight later); its detections of the two edges, one
  gathered term, are two spans, each on its own edge.
A handover's UIs are counted at the launcher's pin, or on MP_hold_ho and PM_contention at the
Peripheral. Every pin draws every clock edge in the drawn window, the edges the UIs count
through too, each at its own flight-shifted time (`clock_edges`). A span's way in time is
fixed (`Chain.ways`), but a crossing's or a detection's (its threshold can lie before its
ramp's 20 % point), a SoundWire anchor correction's and the margin's; one that runs the other way at
a corner, a term of the wrong sign, is drawn back and named in `WholeView.against`.

Setup and hold legs: MP, PM and P→P, and their handover variants. Each is drawn at the
polarities of the case its breakdown reports (`InequalityBreakdown.sampling_rising`): the
sampling edge's, and the launching edge's that follows from it. A setup leg's origin is the
point one UI before its sampling reference: the launch edge's reference on M→P and P→M setup,
and on P→P setup where B's detection of the sampling edge would be one UI earlier. A turn-on
(t_ZD, on the handover variants) is drawn leaving high-Z at the launcher's pin and at the
receiver's (`Ramp.from_hiz`): the far end of the bus is undriven until the drive arrives.

DRIVER STATE. A ramp whose driver goes high-Z has `off_ns`: the wire keeps the level the
driver left, which the keeper holds, and the driver is off from there. A turn-on has
`from_hiz`: undriven before the ramp starts, and `before`, the device that drove the wire
last: on a handover leg the other device (the Peripheral on M→P, the Manager on P→M, B on
P→P), on contention the releaser. A level held through the leg with no edge in it
(a conventional release's last bit) is a ramp with `edge` False. Event kinds name what the
point is: a ramp's 80 % point (`valid`, V_OH rising and V_OL falling), the driver going
high-Z (`release`), and the keeper having followed the level (`keeper`).

Contention legs: time is in ns from the reference of the edge that closes the releaser's
last UI, at the Manager or on P→P at B; an EndDriveEarly release starts from the edge that
opens that UI, one UI earlier. Each is drawn at the closing-edge polarity its breakdown
reports (`InequalityBreakdown.closing_rising`), and the margin runs from the releaser's high-Z
to the acquirer's 20 % point at the releaser's pin, its drive flying to it. MP_contention's
two flights are its two t_PD terms. PM_contention and PP_contention, whose bus terms net to
zero, are drawn with the acquirer at the Manager's end of the bus and the releaser one t_PD
from it, its clock a t_PD after the acquirer's, and where that flight is not zero say the
flights net to zero but for the trace mismatch (`LegTimeline.note`); their whole views say how
they are drawn at the releaser alone (`PM_ONE_PIN_NOTE`, `PP_ONE_PIN_NOTE`), and PP_contention
under a placement that puts every peripheral at one end has no flight between them
(`contention_flight_ns`). The acquirer's drive rides the DATA trace, so it reaches the
releaser's pin the trace mismatch sooner than the clock's flight puts it: the mismatch is a
span on the drive's ramp there, from a mark one flight after the acquirer's 20 % point at its
own pin back to it. Contention carries no data crossing: the acquirer's first bit is the
edge its turn-on is priced on (`calculator.turn_on_rising`, rising unless a SoundWire anchor
places the falling one earlier), and a conventional releaser's last bit is the other level.
An EndDriveEarly releaser's last bit is the edge its release is priced on
(`calculator.ede_release_rising`), falling unless the rising one reaches V_OH the later.

Every launch is drawn on the data and clock edges its leg's case places it on, which a
SoundWire side's anchor correction depends on (`calculator.sw_anchor_ns`).

Keeper legs: the releaser's data edge and the keeper's threshold on it are the corner the leg
is priced on (`calculator._keeper_case`), and keeper_Man's opening edge the one
`calculator.keeper_open_rising` gives. Time is in ns from the opening edge's reference
(keeper_Man) or from its reference at the Peripheral (keeper_Per); an EndDriveEarly leg,
whose terms start at the releasing ramp, from that ramp's 20 % point. The keeper's response is
counted at the Manager from the data's crossing of the keeper's threshold
(`calculator.keeper_corners`: the Manager's V_IH,max / V_IL,min, or a SoundWire Manager's
V_TH_Keeper,max / V_TL_Keeper,min), drawn as the threshold line, so keeper_Per's timeline holds
its data at both pins, every span measured within one pin. The margin runs from the keeper
having followed the level to the release at the Manager.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from typing import Callable

from . import calculator as C
from .delta_tpd import CrossingCorner, RampShape, crossing_time_frac, ramp_level

# The legs `leg_timeline` draws, which refuses any other name: every inequality. A leg that
# does not apply at the given inputs (`InequalityBreakdown.not_applicable`) is refused too.
SUPPORTED_LEGS: tuple[C.Inequality, ...] = C.INEQUALITIES


@dataclass(frozen=True)
class Ramp:
    """One edge of one signal, as seen at one pin."""
    key: str
    pin: str            # "Manager", "Peripheral", "Peripheral A", "Peripheral B"
    signal: str         # "clock" or "data"
    driver: str         # "Man" or "Per": whose output, and whose ground the ramp is drawn on
    lane: str           # "Man CLK", "Man DATA" or "Per DATA"
    rising: bool
    role: str           # "late" or "early": which slew factor the edge takes
    start_ns: float     # where the ramp leaves its rail
    t_rf_ns: float      # this edge's t_RF: the lane's row times its slew factor
    shape: RampShape
    vdd_tx: float       # the rail it swings to, as a fraction of V_nom
    swing_frac: float   # V_OH − V_OL, as a fraction of the rail
    off_ns: float | None = None     # the driver goes high-Z here, the wire keeping its level
    from_hiz: bool = False          # undriven before start_ns, at the level it starts from
    edge: bool = True               # False: a level held through the leg, high when `rising`
    device: str = ""                # the driving device where `driver` does not say: "Per B"
    # A turn-on's: the device that last drove the wire, whose level the keeper holds before
    # it ("Man", "Per", "Per A", "Per B"); "" where the leg does not say.
    before: str = ""

    def level(self, t_ns: float) -> float:
        """The ramp's own curve at `t_ns`, as a fraction of V_nom, on the driver's ground,
        whatever the driver's state (`wire` gives the level the wire holds)."""
        if not self.edge:
            return self.vdd_tx if self.rising else 0.0
        if self.t_rf_ns <= 0:
            return self.vdd_tx if (t_ns >= self.start_ns) == self.rising else 0.0
        return ramp_level(self.shape, self.rising, (t_ns - self.start_ns) / self.t_rf_ns,
                          V_DD=self.vdd_tx, swing_frac=self.swing_frac)

    def wire(self, t_ns: float) -> float:
        """The wire's level at `t_ns`, as drawn: the ramp's while its driver drives, and past
        `off_ns` the level the driver left, which the keeper holds."""
        return self.level(t_ns if self.off_ns is None else min(t_ns, self.off_ns))


@dataclass(frozen=True)
class Threshold:
    """A receiver's threshold, read on a ramp: `corner.threshold` against the ramp's driver's
    ground, `corner.rx_threshold` against the receiver's own."""
    ramp: str
    receiver: str
    corner: CrossingCorner


@dataclass(frozen=True)
class Event:
    key: str
    t_ns: float
    kind: str           # "origin", "reference", "anchor", "detection", "crossing", "deadline",
                        # "valid" (a ramp's 80 % point), "release" (its driver goes high-Z),
                        # "keeper" (the keeper has followed the level),
                        # "mark" (a point a span starts or ends at with no ramp of its own)
    label: str
    ramp: str | None = None


@dataclass(frozen=True)
class Span:
    """A term between the two events it connects (`term` indexes the breakdown's terms), or
    the margin (`term` None), with the ramps it is drawn with (keys) and the intervals drawn
    beside it, as (start, end, label)."""
    start: str
    end: str
    term: int | None
    label: str
    view: tuple[str, ...] = ()
    guides: tuple[tuple[str, str, str], ...] = ()


@dataclass(frozen=True)
class Chain:
    """One side of a leg's balance (`WholeView`): spans end to end in time, each a term of the
    breakdown (`Span.term`), one detection a gathered term nets (labelled with that detection's
    crossing, `Span.term` the gathered term's index), or the margin. `pins` names where each
    span is measured, a pin or a flight "<pin> → <pin>"; `ways` the way each runs in time: 1
    forward, −1 back, 0 either (a crossing or a detection, a SoundWire anchor correction, the
    margin)."""
    spans: tuple[Span, ...]
    pins: tuple[str, ...]
    ways: tuple[int, ...]


@dataclass(frozen=True)
class WholeView:
    """A leg drawn whole: two chains between one start and one end, the second ending in the
    margin. On the first chain a span lasts its term's value, on the second the negative of
    it, so the margin is the sum of the terms; the detections a gathered term nets add up to
    that term. Every span but the margin is a term, and a chain's spans run end to end.
    `checked_at` is the pin whose
    timing the leg checks, and `pins` the pins drawn, in drawing order, that pin last, every
    ramp at them drawn. `against` names each span with a fixed way that runs the other way at
    this corner, a term of the wrong sign, drawn back. `note`, where given, is what the view's
    footnote says of how it is drawn, in place of `LegTimeline.note`."""
    checked_at: str
    pins: tuple[str, ...]
    chains: tuple[Chain, Chain]
    against: tuple[str, ...] = ()
    note: str | None = None     # how the view is drawn, in place of the leg's note: rich text

    @property
    def margin(self) -> Span:
        return self.chains[1].spans[-1]

    @property
    def start(self) -> str:
        """The event the chains start at: the first chain's first span's start."""
        return self.chains[0].spans[0].start

    @property
    def end(self) -> str:
        """The event the first chain ends at, its last span's end; the margin ends at the same
        instant."""
        return self.chains[0].spans[-1].end


# The legs `leg_timeline` gives a `WholeView`: every inequality.
WHOLE_LEGS: tuple[C.Inequality, ...] = C.INEQUALITIES


@dataclass(frozen=True)
class LegTimeline:
    leg: str
    ramps: tuple[Ramp, ...]
    thresholds: tuple[Threshold, ...]
    events: tuple[Event, ...]
    spans: tuple[Span, ...]
    margin_ns: float
    note: str = ""      # how the leg is drawn, where that is a convention: rich text
    whole: WholeView | None = None     # the leg drawn whole, on a `WHOLE_LEGS` leg
    ui_ns: float = 0.0  # the clock's UI, one edge to the next (`clock_edges`)
    # From a clock ramp's start to its 20 % point, in units of its t_RF: falling, rising.
    clock_ref_frac: tuple[float, float] = (0.0, 0.0)

    def event(self, key: str) -> Event:
        """The event named `key`; KeyError, naming the leg and the key, if there is none."""
        found = next((e for e in self.events if e.key == key), None)
        if found is None:
            raise KeyError(f"{self.leg}: no event {key!r}")
        return found

    def ramp(self, key: str) -> Ramp:
        """The ramp named `key`; KeyError, naming the leg and the key, if there is none."""
        found = next((r for r in self.ramps if r.key == key), None)
        if found is None:
            raise KeyError(f"{self.leg}: no ramp {key!r}")
        return found

    def duration(self, span: Span) -> float:
        return self.event(span.end).t_ns - self.event(span.start).t_ns


class _Builder:
    """Collects ramps, events and spans for one leg, and finds each span's term by symbol."""

    def __init__(self, inp: C.CalcInputs, leg: C.Inequality) -> None:
        self.leg = leg
        self.breakdown = C.compute(inp).breakdowns[leg]
        # Every edge at the slew factor the leg is reported at (`CalcInputs.fast_data_edge`).
        self.inp = replace(inp, fast_data_edge=self.breakdown.fast_data_edge)
        self.terms = self.breakdown.terms
        self.used: set[int] = set()
        self.ramps: list[Ramp] = []
        self.thresholds: list[Threshold] = []
        self.events: list[Event] = []
        self.spans: list[Span] = []
        self.swing = inp.V_OH_min_frac - inp.V_OL_max_frac
        self.supplies = self.breakdown.supplies
        self.corners = inp.crossing_corners(self.supplies)
        self.note = ""
        # The leg's `WholeView`, built once its clock is complete; None where it has none.
        self.whole_of: Callable[[], WholeView] | None = None

    # --- geometry -----------------------------------------------------------------------
    def shape(self, lane: str) -> RampShape:
        return self.inp.Per_shape if lane == "Per DATA" else self.inp.Man_shape

    def edge_trf(self, lane: str, role: str) -> float:
        return C.lane_ns(self.inp, lane) * C.edge_factor(self.inp, lane, role)

    def ramp(self, key: str, *, pin: str, signal: str, lane: str, rising: bool, role: str,
             start: float, vdd_tx: float = 1.0, off_ns: float | None = None,
             from_hiz: bool = False, edge: bool = True, device: str = "",
             before: str = "") -> Ramp:
        r = Ramp(key, pin, signal, "Per" if lane == "Per DATA" else "Man", lane, rising, role,
                 start, self.edge_trf(lane, role), self.shape(lane), vdd_tx, self.swing,
                 off_ns, from_hiz, edge, device, before)
        self.ramps.append(r)
        return r

    def anchor_frac(self, lane: str, rising: bool) -> float:
        return C.anchor_frac(self.inp, "Per" if lane == "Per DATA" else "Man", rising)

    def anchor_offset(self, lane: str, role: str, rising: bool) -> float:
        """From the start of a ramp of polarity `rising` to its 20 % point."""
        return self.anchor_frac(lane, rising) * self.edge_trf(lane, role)

    def valid_offset(self, lane: str, role: str, rising: bool) -> float:
        """From the start of a ramp of polarity `rising` to its 80 % point, V_OH rising and
        V_OL falling, where the drawn ramp reaches that level."""
        level = self.inp.V_OH_min_frac if rising else self.inp.V_OL_max_frac
        at = CrossingCorner(rising, "V_OH" if rising else "V_OL", level, 1.0, 0.0, 1.0, +1)
        return (crossing_time_frac(at, self.shape(lane), swing_frac=self.swing)
                * self.edge_trf(lane, role))

    def crossing_offset(self, name: str, lane: str, role: str) -> float:
        """From a ramp's start to its crossing at corner `name`."""
        return (crossing_time_frac(self.corners[name], self.shape(lane), swing_frac=self.swing)
                * self.edge_trf(lane, role))

    def threshold(self, ramp: Ramp, receiver: str, name: str) -> None:
        self.thresholds.append(Threshold(ramp.key, receiver, self.corners[name]))

    def clock_detection(self, key: str, ramp: Ramp, receiver: str, name: str,
                        label: str) -> tuple[str, float]:
        """A receiver's detection of clock edge `ramp` at corner `name`: its threshold on the
        ramp and the event, keyed `key`. Returns the key and the time."""
        self.threshold(ramp, receiver, name)
        t = ramp.start_ns + self.crossing_offset(name, "Man CLK", ramp.role)
        return self.event(key, t, "detection", label, ramp), t

    # --- events and spans -----------------------------------------------------------------
    def event(self, key: str, t: float, kind: str, label: str, ramp: Ramp | None = None) -> str:
        self.events.append(Event(key, t, kind, label, ramp.key if ramp else None))
        return key

    def attach(self, key: str, kind: str, label: str, ramp: Ramp) -> None:
        """Put an already-placed point on the ramp built after it."""
        i = next(i for i, e in enumerate(self.events) if e.key == key)
        self.events[i] = Event(key, self.events[i].t_ns, kind, label, ramp.key)

    def term(self, pred) -> int:
        """The index of the first unused term matching `pred`, marked used. Raises
        AssertionError, naming the leg and its unused terms, where none matches."""
        i = next((i for i, t in enumerate(self.terms) if i not in self.used and pred(t.symbol)),
                 None)
        if i is None:
            unused = [t.symbol for j, t in enumerate(self.terms) if j not in self.used]
            raise AssertionError(f"{self.leg}: no unused term matches; unused: {unused}")
        self.used.add(i)
        return i

    def has(self, pred) -> bool:
        return any(i not in self.used and pred(t.symbol) for i, t in enumerate(self.terms))

    def span(self, start: str, end: str, term: int | None, *, view: tuple[str, ...],
             guides: tuple[tuple[str, str, str], ...] = ()) -> None:
        label = self.terms[term].symbol if term is not None else "margin"
        self.spans.append(Span(start, end, term, label, view, guides))

    def launch(self, start: str, t0: float, side: str, which: str, corner: str,
               end_key: str, view: tuple[str, ...],
               guides: tuple[tuple[str, str, str], ...] = (), *, rising: bool,
               clk_rising: bool = True) -> float:
        """The launch's row span from `start`, and a SoundWire side's anchor correction after
        it (`calculator.sw_anchor_ns`, for a data edge of polarity `rising` from a Manager clock
        edge of polarity `clk_rising`), both drawn with `view` and `guides`. Returns where the
        anchor is."""
        row = C.launch_row_ns(self.inp, side, which, corner)
        corr = C.sw_anchor_ns(self.inp, side, corner, rising=rising, clk_rising=clk_rising)
        sym = f"{side}_t_{which}"
        anchor = C.anchor_symbol(self.inp, side, which, corner, rising=rising,
                                 clk_rising=clk_rising)
        if self.has(lambda s: s == anchor):
            mid = self.event(f"{end_key}_row", t0 + row, "mark", f"{sym} as tabulated")
            self.span(start, mid, self.term(lambda s: s == sym), view=view, guides=guides)
            self.event(end_key, t0 + row + corr, "mark", "the launch's anchor")
            self.span(mid, end_key, self.term(lambda s: s == anchor), view=view,
                      guides=guides)
        else:
            self.event(end_key, t0 + row + corr, "mark", "the launch's anchor")
            self.span(start, end_key, self.term(lambda s: s == sym), view=view, guides=guides)
        return t0 + row + corr

    def receive(self, at: str, at_t: float, side: str, which: str, cross: str, *,
                view: tuple[str, ...]) -> str:
        """The receive row `which` ("IS" or "IH") of `side`: its span before the sampling point
        `at` (at `at_t`) on setup and after it on hold, and a SoundWire side's correction
        beyond it (`calculator.sw_receive_ns`, on the leg's sampling edge), both drawn with
        `view`, the last with the guide from the data's crossing `cross`. Returns the
        deadline's key."""
        clk_rising = bool(self.breakdown.sampling_rising)
        row = getattr(self.inp, f"{side}_t_{which}_max_ns")
        corr = C.sw_receive_ns(self.inp, side, which, clk_rising=clk_rising)
        sym = f"{side}_t_{which}"
        corr_sym = C.receive_symbol(side, which, clk_rising=clk_rising)
        setup = which == "IS"
        sign = -1.0 if setup else 1.0
        what = "the setup deadline" if setup else "the hold deadline"
        guides = ((cross, "deadline", "margin"),) if setup else (("deadline", cross, "margin"),)

        def ends(a: str, b: str) -> tuple[str, str]:
            return (b, a) if setup else (a, b)

        if self.has(lambda s: s == corr_sym):
            mid = self.event("deadline_row", at_t + sign * row, "mark", f"{sym} as tabulated")
            self.span(*ends(at, mid), self.term(lambda s: s == sym), view=view)
            dl = self.event("deadline", at_t + sign * (row + corr), "deadline", what)
            self.span(*ends(mid, dl), self.term(lambda s: s == corr_sym), view=view,
                      guides=guides)
        else:
            dl = self.event("deadline", at_t + sign * row, "deadline", what)
            self.span(*ends(at, dl), self.term(lambda s: s == sym), view=view, guides=guides)
        return dl

    def handover(self, start: str, t0: float, clock: Ramp | None = None,
                 view: tuple[str, ...] = ()) -> tuple[float, str | None]:
        """The allocated handover UIs from `start`, if any. With `clock`, the edge `start` is
        the reference of, the launching edge is added that many UIs later, of the polarity
        the count gives, with the handover's end at its reference. Returns where the UIs end
        and the launching clock edge's key (`clock`'s without UIs)."""
        own = clock.key if clock else None
        if not self.has(lambda s: s.endswith("·UI")):
            return t0, own
        n = max(self.inp.handover_UIs, 0.0)
        n_ui = n * self.inp.UI_ns()
        launch = None
        if clock is not None:
            rising = clock.rising != C.handover_flips_edge(self.inp)
            launch = replace(clock, key=clock.key.replace("clk@", "clk_l@"), rising=rising,
                             start_ns=(clock.start_ns + n_ui
                                       + self.anchor_offset(clock.lane, clock.role,
                                                            clock.rising)
                                       - self.anchor_offset(clock.lane, clock.role, rising)))
            self.ramps.append(launch)
            self.event("handover", t0 + n_ui, "reference", "the launching edge's reference",
                       launch)
            view = (clock.key, launch.key)
        else:
            self.event("handover", t0 + n_ui, "mark", "after the handover UIs")
        self.span(start, "handover", self.term(lambda s: s.endswith("·UI")), view=view)
        return t0 + n_ui, launch.key if launch else own

    def complete_clock(self) -> None:
        """Every clock edge of the leg at every pin the leg places a clock edge at: the edges
        from the leg's first to its last, one UI apart, alternating in polarity, each at a pin
        the clock's flight to that pin after its reference at the first pin. An edge the leg
        places at another pin keeps its slew factor there; one it places at no pin takes the
        slew factor of the nearest edge before it at the pin. Raises AssertionError where the
        leg's clock edges at one pin are not a whole number of UIs apart."""
        clocks = [r for r in self.ramps if r.signal == "clock"]
        if not clocks:
            return
        ui = self.inp.UI_ns()

        def ref(r: Ramp) -> float:
            return r.start_ns + self.anchor_offset(r.lane, r.role, r.rising)

        base = min(ref(r) for r in clocks)
        index = {r.key: round((ref(r) - base) / ui) for r in clocks}
        lo, hi = min(index.values()), max(index.values())
        pins: dict[str, list[Ramp]] = {}
        for r in clocks:
            pins.setdefault(r.pin, []).append(r)
        for pin, own in pins.items():
            flight = ref(own[0]) - base - index[own[0].key] * ui
            for r in own:
                if abs(ref(r) - base - index[r.key] * ui - flight) > 1e-6:
                    raise AssertionError(f"{self.leg}: the clock edges at the {pin} are not "
                                         "a whole number of UIs apart")
            have = {index[r.key]: r for r in own}
            tag = own[0].key.partition("@")[2]
            first = min(have)
            for n in range(lo, hi + 1):
                if n in have:
                    continue
                twin = next((r for r in clocks if index[r.key] == n), None)
                near = have[max((k for k in have if k < n), default=first)]
                rising = (twin.rising if twin is not None
                          else near.rising == ((n - index[near.key]) % 2 == 0))
                role = twin.role if twin is not None else near.role
                have[n] = added = self.ramp(
                    f"clk_e{n}@{tag}", pin=pin, signal="clock", lane=near.lane, rising=rising,
                    role=role, vdd_tx=near.vdd_tx, device=near.device,
                    start=base + flight + n * ui - self.anchor_offset(near.lane, role, rising))
                index[added.key] = n

    # --- the leg drawn whole --------------------------------------------------------------
    def find(self, start: str | None = None, end: str | None = None, pred=None) -> Span:
        """The span from `start`, to `end`, and whose term's symbol matches `pred`, of those
        given; AssertionError, naming the leg, where there is none."""
        found = next((sp for sp in self.spans
                      if (start is None or sp.start == start) and (end is None or sp.end == end)
                      and (pred is None or (sp.term is not None
                                            and pred(self.terms[sp.term].symbol)))), None)
        if found is None:
            raise AssertionError(f"{self.leg}: no span from {start} to {end}")
        return found

    def at(self, key: str) -> float:
        return next(e.t_ns for e in self.events if e.key == key)

    def has_handover(self) -> bool:
        """Whether a span counts handover UIs."""
        return any(sp.label.endswith("·UI") for sp in self.spans)

    def duration_of(self, sp: Span) -> float:
        return self.at(sp.end) - self.at(sp.start)


    def reference(self, key: str, pin: str, t: float, label: str) -> str:
        """A reference event keyed `key` on the clock edge at `pin` whose 20 % point is at
        `t`; AssertionError, naming the leg, where no edge there has it."""
        r = next((r for r in self.ramps if r.signal == "clock" and r.pin == pin
                  and abs(r.start_ns + self.anchor_offset(r.lane, r.role, r.rising) - t) < 1e-6),
                 None)
        if r is None:
            raise AssertionError(f"{self.leg}: no clock edge at the {pin} with its reference "
                                 f"at {t:g} ns")
        return self.event(key, t, "reference", label, r)

    def pin_of(self, sp: Span) -> str:
        """Where `sp` is measured: the pin of the ramps its ends lie on, or a flight between
        two; "" where neither end lies on a ramp."""
        ramps = {e.key: e.ramp for e in self.events}
        pins = list(dict.fromkeys(r.pin for k in (sp.start, sp.end)
                                  for r in self.ramps if r.key == ramps[k]))
        return " → ".join(pins)

    def chain(self, links: list[tuple[Span, int]], margin: Span | None = None) -> Chain:
        """A `Chain` of (span, way) links, a crossing's or a detection's span taking either
        way, as its threshold can lie before its ramp's 20 % point; AssertionError, naming the
        leg, where one link does not start at the instant the one before it ends or a link
        but `margin` has no term."""
        for (a, _), (b, _) in zip(links, links[1:]):
            if abs(self.at(a.end) - self.at(b.start)) > 1e-9:
                raise AssertionError(f"{self.leg}: {b.label} does not start where {a.label} "
                                     "ends")
        for sp, _ in links:
            if sp.term is None and sp is not margin:
                raise AssertionError(f"{self.leg}: {sp.label} is a chain link with no term")
        links = [(sp, 0 if " · " in sp.label else w) for sp, w in links]
        pins = [self.pin_of(sp) for sp, _ in links]
        # A span between two points on no ramp is measured where the chain is: at the pin
        # the span before it ends at, else the one the span after it starts at.
        for i, pin in enumerate(pins):
            if not pin:
                before = next((p for p in reversed(pins[:i]) if p), "")
                after = next((p for p in pins[i + 1:] if p), "")
                pins[i] = before.split(" → ")[-1] or after.split(" → ")[0]
        return Chain(tuple(sp for sp, _ in links), tuple(pins), tuple(w for _, w in links))

    def whole(self, checked_at: str, pins: tuple[str, ...], first: list[tuple[Span, int]],
              second: list[tuple[Span, int]], note: str | None = None) -> WholeView:
        """The `WholeView` of the two chains, `second` ending in the margin, with `note` in
        place of the leg's where given; AssertionError, naming the leg, where they do not start
        and end at the same instants."""
        margin = second[-1][0]
        chains = (self.chain(first), self.chain(second, margin))
        for k in ("start", "end"):
            ts = [self.at(getattr(links[0 if k == "start" else -1][0], k))
                  for links in (first, second)]
            if abs(ts[0] - ts[1]) > 1e-9:
                raise AssertionError(f"{self.leg}: the chains do not {k} at one instant")
        against = tuple(sp.label for c in chains for sp, w in zip(c.spans, c.ways)
                        if w and w * (self.at(sp.end) - self.at(sp.start)) < -1e-9)
        return WholeView(checked_at, pins, chains, against, note)

    def path(self, start: str, end: str, avoid: tuple[str, ...] = ()) -> list[tuple[Span, int]]:
        """The leg's term spans end to end from `start` to `end`, none whose label is in
        `avoid`, as chain links, a launch's span its way (`_anchor_way`); AssertionError,
        naming the leg, where there is no such path."""
        def walk(cur: str, seen: frozenset[str]) -> list[Span] | None:
            if cur == end:
                return []
            for sp in self.spans:
                if (sp.start == cur and sp.term is not None and sp.label not in avoid
                        and sp.end not in seen):
                    rest = walk(sp.end, seen | {sp.end})
                    if rest is not None:
                        return [sp, *rest]
            return None
        got = walk(start, frozenset({start}))
        if got is None:
            raise AssertionError(f"{self.leg}: no spans from {start} to {end}")
        return [(sp, _anchor_way(sp)) for sp in got]

    def ui_at(self, links: list[tuple[Span, int]], pin: str, key: str) -> list[tuple[Span, int]]:
        """`links` with a handover's UIs followed by a flight to `pin` swapped: the flight
        first, to the reference at `pin` (keyed `key`) of the edge the UIs count from, then the
        UIs there."""
        i = next((i for i, (sp, _) in enumerate(links) if sp.label.endswith("·UI")), None)
        if i is None:
            return links
        ui, flight = links[i][0], links[i + 1][0]
        t = self.at(ui.start) + self.at(flight.end) - self.at(ui.end)
        ref = self.reference(key, pin, t, f"the edge the handover UIs count from, at the {pin}")
        return [*links[:i], (Span(ui.start, ref, flight.term, flight.label), 1),
                (Span(ref, flight.end, ui.term, ui.label), 1), *links[i + 2:]]

    def margin_span(self) -> Span:
        return next(sp for sp in self.spans if sp.term is None)

    def done(self) -> LegTimeline:
        missing = [self.terms[i].symbol for i in range(len(self.terms)) if i not in self.used]
        if missing:
            raise AssertionError(f"{self.leg}: terms with no span: {missing}")
        self.complete_clock()
        whole = self.whole_of() if self.whole_of is not None else None
        return LegTimeline(self.leg, tuple(self.ramps), tuple(self.thresholds),
                           tuple(self.events), tuple(self.spans), self.breakdown.margin_ns,
                           self.note, whole, self.inp.UI_ns(),
                           (self.anchor_frac("Man CLK", False), self.anchor_frac("Man CLK", True)))


def _clk(s: str) -> bool:
    return s.endswith("· Man_t_RF,CLK")


def _data(s: str) -> bool:
    return "· " in s and s.endswith(("Man_t_RF,DATA", "Per_t_RF,DATA"))


# ---------------------------------------------------------------------------------------------
# M->P: the Manager launches, a peripheral samples
# ---------------------------------------------------------------------------------------------

def _mp(b: _Builder, setup: bool, which: str) -> None:
    inp = b.inp
    ui, t_pd, mis = inp.UI_ns(), inp.t_PD_ns(), inp.t_PD_mis_ns()
    # The flights: the longer is the bus's t_PD, the shorter t_PD less the mismatch; setup
    # takes the data's as the longer, hold the clock's.
    clk_pd, data_pd = (t_pd - mis, t_pd) if setup else (t_pd, t_pd - mis)
    s_rising = bool(b.breakdown.sampling_rising)
    # The launch edge's reference: of the other polarity to the sampling edge on setup.
    f_ref = b.anchor_offset("Man CLK", "late", s_rising != setup)
    if setup:
        # The launch edge (late) is of the other polarity to the sampling edge, one UI later
        # (early). The data's edge is the launch edge's polarity. Every edge the Manager drives
        # swings to its supply corner.
        det_name = "sigma_pm_hold_rf_clk" if s_rising else "mp_early_fall"
        data_name = "mp_late_fall_data" if s_rising else "mp_late_rise_data"
        data_role = "late"
        c_det = b.corners[det_name]
        clk_l = b.ramp("clk@man", pin="Manager", signal="clock", lane="Man CLK",
                       rising=not s_rising, role="late", start=0.0, vdd_tx=c_det.vdd_tx)
        clk_p = b.ramp("clk@per", pin="Peripheral", signal="clock", lane="Man CLK",
                       rising=not s_rising, role="late", start=clk_pd, vdd_tx=c_det.vdd_tx)
        clk_s = b.ramp("clk_s@per", pin="Peripheral", signal="clock", lane="Man CLK",
                       rising=s_rising, role="early",
                       start=f_ref + ui + clk_pd - b.anchor_offset("Man CLK", "early", s_rising),
                       vdd_tx=c_det.vdd_tx)
        b.threshold(clk_s, "Peripheral", det_name)
    else:
        # The sampling edge (late); the data's edge is of the other polarity.
        det_name = "mp_late_rise" if s_rising else "mp_late_fall"
        data_name = "mp_early_fall_data" if s_rising else "mp_early_rise_data"
        data_role = "early"
        c_det = b.corners[det_name]
        clk_l = b.ramp("clk@man", pin="Manager", signal="clock", lane="Man CLK",
                       rising=s_rising, role="late", start=0.0, vdd_tx=c_det.vdd_tx)
        clk_s = clk_p = b.ramp("clk@per", pin="Peripheral", signal="clock", lane="Man CLK",
                               rising=s_rising, role="late", start=clk_pd,
                               vdd_tx=c_det.vdd_tx)
        b.threshold(clk_s, "Peripheral", det_name)
    b.event("ref", f_ref, "reference", "the Manager's clock reference", clk_l)
    origin = b.event("origin", clk_pd + f_ref, "reference",
                     "the Manager's clock reference, at the Peripheral", clk_p)
    # The launch, at the Manager's pin, from the launching clock edge.
    c_data = b.corners[data_name]
    rising = c_data.rising
    t, start_key, clk_launch = f_ref, "ref", clk_l.key
    if not setup:
        t, launch_key = b.handover("ref", f_ref, clk_l)
        clk_launch = launch_key or clk_l.key
        start_key = "handover" if t != f_ref else "ref"
    clk_rising = (clk_l.rising if setup
                  else clk_l.rising != (which == "ZD" and C.handover_flips_edge(inp)))
    a_man = b.launch(start_key, t, "Man", which, "max" if setup else "min", "anchor_man",
                     view=(clk_launch, "data@man"), rising=rising, clk_rising=clk_rising)
    data_m = b.ramp("data@man", pin="Manager", signal="data", lane="Man DATA", rising=rising,
                    role=data_role, start=a_man - b.anchor_offset("Man DATA", data_role, rising),
                    vdd_tx=c_data.vdd_tx,
                    from_hiz=which == "ZD", before="Per")
    b.attach("anchor_man", "anchor", "the data's 20 % point, at the Manager", data_m)
    # A turn-on's far end is undriven too until its drive arrives.
    data = b.ramp("data@per", pin="Peripheral", signal="data", lane="Man DATA", rising=rising,
                  role=data_role, start=data_m.start_ns + data_pd, vdd_tx=c_data.vdd_tx,
                  from_hiz=which == "ZD", before="Per")
    b.threshold(data, "Peripheral", data_name)
    b.event("anchor_nominal", a_man + clk_pd, "mark", "the clock's flight, from the launch")
    a_per = b.event("anchor", a_man + data_pd, "anchor", "the data's 20 % point", data)
    # The mismatch: the data's flight against the clock's, both from the launch, drawn with
    # no ramps.
    mis_guides = (("anchor_man", "anchor_nominal", "Clock propagation"),
                  ("anchor_man", a_per, "Data propagation"))
    if setup:
        b.span("anchor_nominal", a_per, b.term(lambda s: s == "t_PD,mis"), view=(),
               guides=mis_guides)
    else:
        b.span(a_per, "anchor_nominal", b.term(lambda s: s == "t_PD,mis"), view=(),
               guides=mis_guides)
    cross = b.event("crossing", data.start_ns + b.crossing_offset(data_name, "Man DATA",
                                                                  data_role),
                    "crossing", "the data crosses the Peripheral's threshold", data)
    b.span(a_per, cross, b.term(_data), view=("data@per",))
    det_t = clk_s.start_ns + b.crossing_offset(det_name, "Man CLK", clk_s.role)
    det = b.event("detection", det_t, "detection", "the Peripheral detects the clock", clk_s)
    at_per = (clk_s.key,)
    window = (clk_s.key, "data@per")
    if setup:
        b.event("ui_end", clk_pd + f_ref + ui, "reference", "the sampling edge's reference",
                clk_s)
        b.span(origin, "ui_end", b.term(lambda s: s == "UI"), view=(clk_p.key, clk_s.key))
        b.span("ui_end", det, b.term(_clk), view=at_per)
        dl = b.receive(det, det_t, "Per", "IS", cross, view=window)
        b.span(cross, dl, None, view=("data@per", clk_s.key))
    else:
        b.span(origin, det, b.term(_clk), view=at_per)
        dl = b.receive(det, det_t, "Per", "IH", cross, view=window)
        b.span(dl, cross, None, view=("data@per", clk_s.key))
    b.whole_of = lambda: _mp_whole(b, setup)


def _anchor_way(sp: Span) -> int:
    """A chain link's way for a span of a launch or a receive row: either way for a SoundWire
    anchor or receive-row correction (`,anchor`, `,ref`), forward for the rest."""
    return 0 if ",anchor" in sp.label or ",ref," in sp.label else 1


def _setup_end(b: _Builder, cross: str, side: str, sample: str) -> list[tuple[Span, int]]:
    """A setup leg's chain after the data's crossing: `side`'s setup time from the crossing,
    its row and then a SoundWire side's correction (`_Builder.receive`), either way, and the
    margin, from there to the sampling point `sample`."""
    is_sym = f"{side}_t_IS"
    t_is = getattr(b.inp, f"{side}_t_IS_max_ns")
    end = b.event("is_end", b.at(cross) + t_is, "mark", "the setup time, from the crossing")
    t = b.find(pred=lambda s: s == is_sym).term
    links = [(Span(cross, end, t, is_sym), 1)]
    corr = next((sp for sp in b.spans if sp.label.startswith(f"{is_sym},ref,")), None)
    if corr is not None:
        corr_end = b.event("is_end_corr", b.at(end) + b.duration_of(corr), "mark",
                           "the setup time at the operating clock slew, from the crossing")
        links.append((Span(end, corr_end, corr.term, corr.label), 0))
        end = corr_end
    return links + [(Span(end, sample, None, "margin"), 0)]


def _mp_whole(b: _Builder, setup: bool) -> WholeView:
    """M→P at the Peripheral: the clock gives the UI and the sampling edge's detection on
    setup, its detection and the hold time on hold; the data takes the launch, moved to the
    Peripheral by the clock's flight, then the flight mismatch, the data's crossing and on
    setup the setup time. A handover's UIs, on MP_hold_ho, are counted on the Peripheral's
    clock edges, from its reference of the sampling edge to its reference of the launching
    edge."""
    flight = b.at("origin") - b.at("ref")
    moved = {"ref": "origin", "anchor_man": "anchor_nominal"}
    launch: list[tuple[Span, int]] = []
    cur = "ref"
    while cur != "anchor_man":
        sp = b.find(start=cur)
        if sp.end not in moved:
            t = b.at(sp.end) + flight
            moved[sp.end] = (
                b.reference(f"{sp.end}@per", "Peripheral", t,
                            "the launching edge's reference, at the Peripheral")
                if sp.label.endswith("·UI") else
                b.event(f"{sp.end}@per", t, "mark", "the launch's row, at the Peripheral"))
        launch.append((Span(moved[sp.start], moved[sp.end], sp.term, sp.label), _anchor_way(sp)))
        cur = sp.end
    mis = b.find(pred=lambda s: s == "t_PD,mis")
    data = launch + [(Span("anchor_nominal", "anchor", mis.term, mis.label), 1 if setup else -1),
                     (b.find(start="anchor", end="crossing"), 1)]
    if setup:
        clock = [(b.find(start="origin", end="ui_end"), 1),
                 (b.find(start="ui_end", end="detection"), 1)]
        return b.whole("Peripheral", ("Peripheral",), clock,
                       data + _setup_end(b, "crossing", "Per", "detection"))
    clock = [(b.find(start="origin", end="detection"), 1),
             *b.path("detection", "deadline"),
             (b.find(start="deadline", end="crossing"), 0)]
    return b.whole("Peripheral", ("Peripheral",), data, clock)


# ---------------------------------------------------------------------------------------------
# P->M: a peripheral launches, the Manager samples
# ---------------------------------------------------------------------------------------------

def _pm(b: _Builder, setup: bool, which: str) -> None:
    inp = b.inp
    ui, t_pd, mis = inp.UI_ns(), inp.t_PD_ns(), inp.t_PD_mis_ns()
    # The data's flight back: the clock's flight to the Peripheral, t_PD, and the mismatch,
    # later on setup and earlier on hold.
    data_pd = t_pd + (mis if setup else -mis)
    env = inp.envelope(b.supplies)
    k, s = inp.tRF_Man_CLK_ns, inp.tRF_Per_DATA_ns
    # The Peripheral launches on the other polarity to the Manager's sampling edge on setup,
    # on its polarity on hold, and on PM_hold_ho on the one its handover count gives.
    s_rising = bool(b.breakdown.sampling_rising)
    if setup:
        launch_rising = not s_rising
    else:
        launch_rising = s_rising != (b.leg == "PM_hold_ho" and C.handover_flips_edge(inp))
    if setup:
        # The case Sigma_cross,PM,setup selects, its data on either polarity.
        det_name, data_name = env.sigma_pm_setup_fields(
            k * C.tau_adj(inp, "Man CLK", "late"), s * C.tau_adj(inp, "Per DATA", "late"),
            launch_rising=launch_rising, data_offset_ns=C.per_anchor_offsets(inp, "max"))
        role = ref_role = "late"
    else:
        pair = env.sigma_pm_hold_lanes(k * C.tau_adj(inp, "Man CLK", "early"),
                                       s * C.tau_adj(inp, "Per DATA", "early"),
                                       launch_rising=launch_rising,
                                       data_offset_ns=C.per_anchor_offsets(inp, "min"))
        if launch_rising:
            rr = pair == (env.sigma_pm_hold_rr_clk, env.sigma_pm_hold_rr_data)
            det_name = "sigma_pm_hold_rr_clk" if rr else "sigma_pm_hold_rf_clk"
            data_name = "sigma_pm_hold_rr_data" if rr else "sigma_pm_hold_rf_data"
        else:
            ff = pair == (env.sigma_pm_hold_ff_clk, env.sigma_pm_hold_ff_data)
            det_name = "sigma_pm_hold_ff_clk" if ff else "mp_early_fall"
            data_name = "sigma_pm_hold_ff_data" if ff else "sigma_pm_hold_fr_data"
        role = ref_role = "early"
    c_det, c_data = b.corners[det_name], b.corners[data_name]
    f_ref = b.anchor_offset("Man CLK", ref_role, launch_rising if setup else s_rising)
    origin_t = f_ref
    # The leg's first edge at the Manager: the launching edge on setup, the sampling edge on
    # hold.
    clk_m = b.ramp("clk@man", pin="Manager", signal="clock", lane="Man CLK",
                   rising=launch_rising if setup else s_rising,
                   role=role, start=0.0, vdd_tx=c_det.vdd_tx)
    origin = b.event("origin", origin_t, "reference", "the Manager's clock reference", clk_m)
    t, start_key, clk_launch = origin_t, origin, clk_m.key
    if not setup:
        t, launch_key = b.handover(origin, origin_t, clk_m)
        clk_launch = launch_key or clk_m.key
        start_key = "handover" if t != origin_t else origin
    # `t` is the reference of the edge the peripheral launches from.
    clk_p = b.ramp("clk@per", pin="Peripheral", signal="clock", lane="Man CLK",
                   rising=c_det.rising, role=role,
                   start=t + t_pd - b.anchor_offset("Man CLK", role, c_det.rising),
                   vdd_tx=c_det.vdd_tx)
    b.threshold(clk_p, "Peripheral", det_name)
    b.event("clk_at_per", t + t_pd, "reference", "the Manager's clock reference, at the "
            "Peripheral", clk_p)
    b.span(start_key, "clk_at_per", b.term(lambda s_: s_ == "t_PD"),
           view=(clk_launch, "clk@per"))
    det_t = clk_p.start_ns + b.crossing_offset(det_name, "Man CLK", role)
    det = b.event("detection", det_t, "detection", "the Peripheral detects the clock", clk_p)
    b.span("clk_at_per", det, b.term(_clk), view=("clk@per",))
    anchor = b.launch(det, det_t, "Per", which, "max" if setup else "min", "anchor",
                      view=("clk@per", "data@per"), rising=c_data.rising)
    data_start = anchor - b.anchor_offset("Per DATA", role, c_data.rising)
    data_p = b.ramp("data@per", pin="Peripheral", signal="data", lane="Per DATA",
                    rising=c_data.rising, role=role, start=data_start, vdd_tx=c_data.vdd_tx,
                    from_hiz=which == "ZD", before="Man")
    b.attach("anchor", "anchor", "the data's 20 % point", data_p)
    data_m = b.ramp("data@man", pin="Manager", signal="data", lane="Per DATA",
                    rising=c_data.rising, role=role, start=data_start + data_pd,
                    vdd_tx=c_data.vdd_tx, from_hiz=which == "ZD", before="Man")
    b.threshold(data_m, "Manager", data_name)
    b.event("anchor_nominal", anchor + t_pd, "mark", "the clock's flight, from the launch")
    a_man = b.event("anchor_man", anchor + data_pd, "anchor",
                    "the data's 20 % point, at the Manager", data_m)
    b.span("anchor", "anchor_nominal", b.term(lambda s_: s_ == "t_PD"),
           view=("data@per", "data@man"))
    # The mismatch: the data's flight against the clock's, both from the launch, drawn with
    # no ramps.
    mis_guides = (("anchor", "anchor_nominal", "Clock propagation"),
                  ("anchor", a_man, "Data propagation"))
    if setup:
        b.span("anchor_nominal", a_man, b.term(lambda s_: s_ == "t_PD,mis"), view=(),
               guides=mis_guides)
    else:
        b.span(a_man, "anchor_nominal", b.term(lambda s_: s_ == "t_PD,mis"), view=(),
               guides=mis_guides)
    cross = b.event("crossing", data_start + data_pd
                    + b.crossing_offset(data_name, "Per DATA", role),
                    "crossing", "the data crosses the Manager's threshold", data_m)
    sample = "clk_s@man" if setup else "clk@man"
    b.span(a_man, cross, b.term(_data), view=("data@man",))
    at_man = (sample, "data@man")
    if setup:
        clk_s = b.ramp("clk_s@man", pin="Manager", signal="clock", lane="Man CLK",
                       rising=s_rising, role="early",
                       start=f_ref + ui - b.anchor_offset("Man CLK", "early", s_rising),
                       vdd_tx=c_det.vdd_tx)
        sref = b.event("sample_ref", ui + f_ref, "reference", "the Manager's sampling reference",
                       clk_s)
        b.span(origin, sref, b.term(lambda s_: s_ == "UI"), view=("clk@man", "clk_s@man"))
        dl = b.receive(sref, ui + f_ref, "Man", "IS", cross, view=at_man)
        b.span(cross, dl, None, view=("data@man", sample))
    else:
        dl = b.receive(origin, origin_t, "Man", "IH", cross, view=at_man)
        b.span(dl, cross, None, view=("data@man", sample))
    b.whole_of = lambda: _pm_whole(b, setup)


def _pm_whole(b: _Builder, setup: bool) -> WholeView:
    """P→M from the Manager's clock to its sampling: the data's chain runs from the
    Manager's clock reference to the Peripheral, through its detection and launch, and back
    by the clock's flight, then the flight mismatch and the Manager's crossing, then on setup
    the setup time; the clock gives the UI on setup, and on hold takes the hold time."""
    # On PM_hold_ho the handover UIs are counted at the Peripheral, the launcher, after the
    # clock's flight to it.
    mis = b.find(pred=lambda s: s == "t_PD,mis")
    data = b.ui_at(b.path("origin", "anchor_nominal", avoid=("UI", "Man_t_IH")), "Peripheral",
                   "ref_s@per")
    data += [(Span("anchor_nominal", "anchor_man", mis.term, mis.label), 1 if setup else -1),
             (b.find(start="anchor_man", end="crossing"), 1)]
    pins = ("Peripheral", "Manager")
    if setup:
        return b.whole("Manager", pins, [(b.find(start="origin", end="sample_ref"), 1)],
                       data + _setup_end(b, "crossing", "Man", "sample_ref"))
    return b.whole("Manager", pins, data,
                   [*b.path("origin", "deadline"),
                    (b.find(start="deadline", end="crossing"), 0)])


# ---------------------------------------------------------------------------------------------
# P->P: peripheral A launches, peripheral B samples
# ---------------------------------------------------------------------------------------------

def _pp(b: _Builder, setup: bool, which: str) -> None:
    inp = b.inp
    ui, t_pd = inp.UI_ns(), inp.t_PD_ns()
    n = C.pp_net_pd(inp.pp_placement, is_setup=setup)
    # Two flights put A at the far end and B at the near one. A placement whose bus term
    # nets to zero (both near, both far, driver near) is drawn with both peripherals at the
    # Manager: the spans and the margin are the same, and the clock arrivals are the
    # Manager's.
    pd_a, pd_b, flight = (t_pd, 0.0, t_pd) if n else (0.0, 0.0, 0.0)
    # B samples on the polarity of the case the leg reports.
    b_rising = bool(b.breakdown.sampling_rising)
    if setup:
        # A launches on the edge before, of the other polarity, detected at its late corner;
        # B's detection of the sampling edge is early, and A's data takes A's edge's polarity.
        a_rising = not b_rising
        a_name = "mp_late_rise" if a_rising else "mp_late_fall"
        b_name = "mp_early_fall_b" if a_rising else "sigma_pm_hold_rf_clk_b"
        data_name, role = ("mp_late_rise_per" if a_rising else "mp_late_fall_per"), "late"
    else:
        # B's detection is at its polarity's late corner, and A launches on the edge its
        # handover count gives after it (`calculator.pp_hold_edges`), detected at that
        # polarity's early corner.
        a_rising = next(a for a, bb in C.pp_hold_edges(inp, b.leg) if bb == b_rising)
        a_name = "sigma_pm_hold_rf_clk" if a_rising else "mp_early_fall"
        b_name = "mp_late_rise_b" if b_rising else "mp_late_fall_b"
        data_name = "mp_early_fall_per" if b_rising else "mp_early_rise_per"
        role = "early"
    # The slew factor of the edge each clock ramp is: one factor for both where A and B detect
    # one edge (`calculator.pp_clock_edges`).
    a_role, b_role = C.pp_clock_edges(inp, inp.envelope(b.supplies), b.leg,
                                      sampling_rising=b_rising)
    c_a, c_b, c_data = b.corners[a_name], b.corners[b_name], b.corners[data_name]
    # Each pin's ramp starts its edge's 20 % offset before the edge's reference there.
    f_a = b.anchor_offset("Man CLK", a_role, a_rising)
    f_b = b.anchor_offset("Man CLK", b_role, b_rising)
    x_a = b.crossing_offset(a_name, "Man CLK", a_role)
    x_b = b.crossing_offset(b_name, "Man CLK", b_role)
    # The origin: where B's detection would sit (setup: of an edge with the sampling edge's
    # slew, one UI earlier; hold: A's detection, transposed to B's pin).
    origin_t = pd_b + (x_b - f_b if setup else x_a - f_a)
    origin = b.event("origin", origin_t, "origin",
                     "B's detection, one UI before its sampling edge" if setup
                     else "A's detection, at B")
    t, start_key = origin_t, origin
    if not setup:
        t, _ = b.handover(origin, origin_t, view=("clk@b", "clk@a"))
        start_key = "handover" if t != origin_t else origin
    shift = t - origin_t
    if n:
        b.event("at_a", t + t_pd, "mark", "the clock's flight to A")
        b.span(start_key, "at_a", b.term(lambda s: s == "t_PD"),
               view=("clk@a",) if setup else ("clk@b", "clk@a"))
    clk_a = b.ramp("clk@a", pin="Peripheral A", signal="clock", lane="Man CLK", rising=a_rising,
                   role=a_role, start=shift + pd_a - f_a, vdd_tx=c_a.vdd_tx)
    b.threshold(clk_a, "Peripheral A", a_name)
    det_a = b.event("det_a", clk_a.start_ns + x_a, "detection", "A detects the clock", clk_a)
    if setup:
        b.span("at_a" if n else start_key, det_a, b.term(_clk), view=("clk@a",))
    anchor = b.launch(det_a, clk_a.start_ns + x_a, "Per", which, "max" if setup else "min",
                      "anchor", view=("clk@a", "data@a"), rising=c_data.rising)
    data_start = anchor - b.anchor_offset("Per DATA", role, c_data.rising)
    data_a = b.ramp("data@a", pin="Peripheral A", signal="data", lane="Per DATA",
                    rising=c_data.rising, role=role, start=data_start, vdd_tx=c_data.vdd_tx,
                    from_hiz=which == "ZD", before="Per B")
    b.attach("anchor", "anchor", "A's data's 20 % point", data_a)
    # The trace mismatch (`calculator.pp_net_mis`): A's data reaches B that much later than the
    # clock's flight puts it on setup, and earlier on hold.
    mis = C.pp_net_mis(inp.pp_placement) * inp.t_PD_mis_ns()
    arrival = flight + (mis if setup else -mis)
    data_b = b.ramp("data@b", pin="Peripheral B", signal="data", lane="Per DATA",
                    rising=c_data.rising, role=role, start=data_start + arrival,
                    vdd_tx=c_data.vdd_tx, from_hiz=which == "ZD", before="Per B")
    b.threshold(data_b, "Peripheral B", data_name)
    b.event("anchor_nominal", anchor + flight, "mark",
            "where the clock's flight puts A's data at B")
    a_b = b.event("anchor_b", anchor + arrival, "anchor", "A's data's 20 % point, at B", data_b)
    if n:
        b.span("anchor", "anchor_nominal", b.term(lambda s: s == "t_PD"),
               view=("data@a", "data@b"))
    b.span("anchor_nominal", a_b, b.term(lambda s: s == C.PP_MIS_SYMBOL), view=("data@b",))
    cross = b.event("crossing", data_start + arrival
                    + b.crossing_offset(data_name, "Per DATA", role),
                    "crossing", "the data crosses B's threshold", data_b)
    sample = "clk_s@b" if setup else "clk@b"
    b.span(a_b, cross, b.term(_data), view=("data@b",))
    at_b = (sample,)
    window = (sample, "data@b")
    if setup:
        clk_b = b.ramp("clk_s@b", pin="Peripheral B", signal="clock", lane="Man CLK",
                       rising=b_rising, role=b_role, start=ui + pd_b - f_b, vdd_tx=c_b.vdd_tx)
        b.threshold(clk_b, "Peripheral B", b_name)
        det_b = b.event("det_b", clk_b.start_ns + x_b, "detection",
                        "B detects the sampling edge", clk_b)
        b.span(origin, det_b, b.term(lambda s: s == "UI"), view=(sample,))
        dl = b.receive(det_b, clk_b.start_ns + x_b, "Per", "IS", cross, view=window)
        b.span(cross, dl, None, view=("data@b", sample))
    else:
        clk_b = b.ramp("clk@b", pin="Peripheral B", signal="clock", lane="Man CLK",
                       rising=b_rising, role=b_role, start=pd_b - f_b, vdd_tx=c_b.vdd_tx)
        b.threshold(clk_b, "Peripheral B", b_name)
        det_b = b.event("det_b", clk_b.start_ns + x_b, "detection", "B detects the clock", clk_b)
        b.span(origin, det_b, b.term(_clk), view=at_b)
        dl = b.receive(det_b, clk_b.start_ns + x_b, "Per", "IH", cross, view=window)
        b.span(dl, cross, None, view=("data@b", sample))
    b.whole_of = lambda: _pp_whole(b, setup, bool(n))


def _ref_of(b: _Builder, key: str) -> float:
    """The 20 % point of the clock edge `key`."""
    r = next(r for r in b.ramps if r.key == key)
    return r.start_ns + b.anchor_offset(r.lane, r.role, r.rising)


def _pp_whole(b: _Builder, setup: bool, far: bool) -> WholeView:
    """P→P at both peripherals' pins, from B's reference of the edge B samples on hold, or of
    the edge A launches from on setup: the gathered term's two detections each at its own
    pin, A's from A's reference of its launching edge and B's from B's of its sampling edge.
    The data's chain runs to A, through its detection and launch, and to B's crossing, then
    on setup the setup time; the clock's gives the UI and
    B's detection on setup, and on hold takes B's detection and the hold time. With `far`
    each flight between the peripherals is a span; without, A's and B's pins are one
    instant apart."""
    gathered = b.find(pred=lambda s: s.startswith("("))
    part = _parts(b, gathered, 1)
    ref_a = b.reference("ref_a", "Peripheral A", _ref_of(b, "clk@a"),
                        "A's reference of its launching edge")
    x_a = Span(ref_a, "det_a", gathered.term, part["A"])
    clock: list[tuple[Span, int]]
    data: list[tuple[Span, int]] = []
    if setup:
        t_s = _ref_of(b, "clk_s@b")
        ref_s = b.reference("ref_s", "Peripheral B", t_s, "B's reference of its sampling edge")
        ref_b = b.reference("ref_b", "Peripheral B", t_s - b.inp.UI_ns(),
                            "B's reference of A's launching edge")
        ui = b.find(pred=lambda s: s == "UI")
        clock = [(Span(ref_b, ref_s, ui.term, ui.label), 1),
                 (Span(ref_s, "det_b", gathered.term, part["B"]), 1)]
    else:
        t_b = _ref_of(b, "clk@b")
        ref_b = b.reference("ref_b", "Peripheral B", t_b, "B's reference of its sampling edge")
        clock = [(Span(ref_b, "det_b", gathered.term, part["B"]), 1),
                 (b.find(start="det_b", end="deadline"), 1),
                 (b.find(start="deadline", end="crossing"), 0)]
    # On PP_hold_ho the handover UIs are counted at A, the launcher, from A's reference of the
    # edge B samples.
    ho = (next((sp for sp in b.spans if sp.label.endswith("·UI")), None) if not setup
          else None)
    at_a = ref_a
    if ho is not None:
        at_a = b.reference("ref_a0", "Peripheral A", b.at(ref_a) - b.duration_of(ho),
                           "A's reference of the edge B samples")
    if far:
        flight = b.find(end="at_a")
        data.append((Span(ref_b, at_a, flight.term, flight.label), 1))
    if ho is not None:
        data.append((Span(at_a, ref_a, ho.term, ho.label), 1))
    data.append((x_a, 1))
    cur = "det_a"
    while cur != "crossing":
        nxt_spans = [s for s in b.spans if s.start == cur]
        if not nxt_spans and cur == "anchor":
            cur = "anchor_nominal"          # A's data at B, the same instant: no flight
            continue
        sp = nxt_spans[0]
        # The trace mismatch takes A's data at B earlier on hold, a cost on the data's chain.
        way = -1 if sp.label == C.PP_MIS_SYMBOL and not setup else _anchor_way(sp)
        data.append((sp, way))
        cur = sp.end
    pins = ("Peripheral A", "Peripheral B")
    if setup:
        return b.whole("Peripheral B", pins, clock,
                       data + _setup_end(b, "crossing", "Per", "det_b"))
    return b.whole("Peripheral B", pins, data, clock)


# ---------------------------------------------------------------------------------------------
# Contention: one device releases the bus, the next acquires it
# ---------------------------------------------------------------------------------------------

# The note of PM_contention and PP_contention where the leg is drawn with a flight between the
# two devices (`contention_flight_ns`); with none it has no note.
NET_ZERO_NOTE = ("The flights between the two devices net to zero in this inequality but for "
                 "the trace mismatch, its own term: the clock's to the releaser and the "
                 "acquirer's drive to it.")
# PM_contention's whole view, drawn at the Peripheral alone (`WholeView.note`), with a flight
# between the two devices, and with none.
PM_ONE_PIN_NOTE_NO_FLIGHT = ("Drawn at the Peripheral, where contention starts: the Manager's "
                             "drive reaching a Peripheral still driving.")
PM_ONE_PIN_NOTE = (PM_ONE_PIN_NOTE_NO_FLIGHT + " The Manager's chain is moved there by the "
                   "clock's flight, which its drive's flight back cancels but for the trace "
                   "mismatch, its own span. At the Manager's pin the release arrives margin − "
                   "2·(t<sub>PD</sub> − t<sub>PD,mis</sub>) before the Manager's turn-on.")
# PP_contention's whole view, drawn at A alone (`WholeView.note`), with a flight between the two
# peripherals, and with none; with handover UIs and a flight, `PP_UI_NOTE` after it.
PP_ONE_PIN_NOTE_NO_FLIGHT = ("Drawn at Peripheral A, where contention starts: Peripheral B's "
                             "drive reaching Peripheral A still driving.")
PP_ONE_PIN_NOTE = (PP_ONE_PIN_NOTE_NO_FLIGHT + " Peripheral B's chain is moved there by the "
                   "clock's flight, which its drive's flight back cancels but for the trace "
                   "mismatch, its own span. At Peripheral B's pin the release arrives margin − "
                   "2·(t<sub>PD</sub> − t<sub>PD,mis,AB</sub>) before Peripheral B's turn-on.")
PP_UI_NOTE = ("The handover UIs are counted at Peripheral B, on the clock edges drawn here a "
              "flight later.")
# keeper_Per's whole view, drawn at the Peripheral alone (`WholeView.note`), with a flight to the
# Manager, and with none.
KEEPER_PER_NOTE_NO_FLIGHT = ("Drawn at the Peripheral, where it releases the bus: the keeper's "
                             "threshold read on its data.")
KEEPER_PER_NOTE = (KEEPER_PER_NOTE_NO_FLIGHT + " The keeper's crossing, its response and the "
                   "release as the Manager sees it are moved back there by the data's flight, "
                   "which the release's flight to the Manager equals, so the flights cancel. "
                   "The keeper acts at the Manager, a flight later.")


def _release(b: _Builder, side: str, *, start: str, t0: float, clk: str, pin: str, key: str,
             vdd: float, device: str = "", beside: tuple[str, ...],
             guide: tuple[str, str, str], clk_rising: bool, held_high: bool) -> None:
    """The releaser's last bit at its pin `pin`, from `start` (its clock reference or its
    detection, at `t0`) to the event "release": an EndDriveEarly release as its launch from
    the opening clock edge, of polarity `clk_rising`, its ramp to the 80 % point and the EDE
    Delay after it, on the edge `ede_release_rising` gives; a conventional one as t_DZ, with
    the bit a held level, high where `held_high`.

    The release's last span is drawn beside `beside` (the acquirer's data) with the margin
    `guide`."""
    inp = b.inp
    lane = f"{side} DATA"
    if inp.Man_ede if side == "Man" else inp.Per_ede:
        rising = C.ede_release_rising(inp, side, clk_rising=clk_rising)
        anchor = b.launch(start, t0, side, "DD", "max", "rel_anchor", view=(clk, key),
                          rising=rising, clk_rising=clk_rising)
        ramp_start = anchor - b.anchor_offset(lane, "late", rising)
        valid_t = ramp_start + b.valid_offset(lane, "late", rising)
        release = valid_t + (inp.Man_t_EDE_ns if side == "Man" else inp.Per_t_EDE_ns)
        data = b.ramp(key, pin=pin, signal="data", lane=lane, rising=rising, role="late",
                      start=ramp_start, vdd_tx=vdd, off_ns=release, device=device)
        b.attach("rel_anchor", "anchor", "the releaser's data's 20 % point", data)
        valid = b.event("valid", valid_t, "valid",
                        f"the ramp's 80 % point, {'V_OH' if rising else 'V_OL'}", data)
        ramp_sym = C.to_voh_symbol(inp, lane, side, rising)
        b.span("rel_anchor", valid, b.term(lambda s: s == ramp_sym), view=(key,))
        rel = b.event("release", release, "release", "the releaser goes high-Z", data)
        b.span(valid, rel, b.term(lambda s: s == f"{side}_t_EDE"), view=(key, *beside),
               guides=(guide,))
        return
    t_dz = C.man_tdz_parts(inp)[0] if side == "Man" else inp.Per_t_DZ_max_ns
    data = b.ramp(key, pin=pin, signal="data", lane=lane, rising=held_high, role="late",
                  start=-math.inf, vdd_tx=vdd, off_ns=t0 + t_dz, edge=False, device=device)
    rel = b.event("release", t0 + t_dz, "release", "the releaser goes high-Z", data)
    b.span(start, rel, b.term(lambda s: s == f"{side}_t_DZ"), view=(clk, key, *beside),
           guides=(guide,))


def contention_flight_ns(inp: C.CalcInputs, leg: str) -> float:
    """The flight between the two devices' pins contention leg `leg` is drawn at: the clock's
    flight to a peripheral (`CalcInputs.t_PD_ns`), and on PP_contention under a placement that
    puts every peripheral at one end (`calculator.PINNED_PLACEMENTS`) none."""
    if leg == "PP_contention" and inp.pp_placement in C.PINNED_PLACEMENTS:
        return 0.0
    return inp.t_PD_ns()


def _contention_mis_ns(b: _Builder) -> float:
    """The trace mismatch the contention leg `b` charges, as the magnitude its term reads: the
    acquirer's drive reaches the releaser's pin that much sooner than the clock's flight puts
    it (`calculator._NET_PD`'s note)."""
    return -b.terms[next(i for i, t in enumerate(b.terms) if t.symbol in _MIS_SYMBOLS)].value_ns


# The trace mismatch's symbols on the contention legs.
_MIS_SYMBOLS = ("t_PD,mis", C.PP_MIS_SYMBOL)


def _contention_arrival(b: _Builder, start: str | None, nominal: float, mis: float, key: str,
                        label: str, ramp: Ramp, *, flight: int | None = None,
                        view: tuple[str, ...] = ()) -> None:
    """The acquirer's 20 % point at the releaser's pin, `key`, on `ramp`: the mark "<key>_nominal"
    where the clock's flight puts it, at `nominal` on that ramp, and the trace mismatch's span
    from there back to it, `mis` earlier. With `flight`, the drive's flight term, its span from
    `start` to the mark."""
    mark = b.event(f"{key}_nominal", nominal, "mark",
                   "a clock's flight after the acquirer's 20 % point at its own pin", ramp)
    if flight is not None:
        assert start is not None
        b.span(start, mark, flight, view=view)
    b.event(key, nominal - mis, "anchor", label, ramp)
    b.span(mark, key, b.term(lambda s: s in _MIS_SYMBOLS), view=(ramp.key,))


def _contention(b: _Builder) -> None:
    inp, leg = b.inp, b.leg
    b.whole_of = lambda: _contention_whole(b)
    ui, t_pd = inp.UI_ns(), contention_flight_ns(inp, leg)
    closing = bool(b.breakdown.closing_rising)
    ede = inp.Man_ede if leg == "MP_contention" else inp.Per_ede
    # The releaser's timing counts from the closing edge, or under EDE from the opening one,
    # of the other polarity; the acquirer's from the edge its handover count gives.
    rel_rising = closing != ede
    acq_rising = closing != C.handover_flips_edge(inp)
    t_open = -ui if ede else 0.0
    has_ho = b.has(lambda s: s.endswith("·UI"))
    man_v = b.supplies[0]
    early = "sigma_pm_hold_rf_clk" if acq_rising else "mp_early_fall"
    late = "mp_late_rise" if rel_rising else "mp_late_fall"
    guide = ("release", "acq_anchor_man" if leg == "MP_contention" else "acq_at_rel", "margin")
    # The acquirer's first bit is on the edge its turn-on is priced on, and a conventional
    # releaser's last bit is the other level.
    acq_data = C.turn_on_rising(inp, "Man" if leg == "PM_contention" else "Per",
                                clk_rising=acq_rising)

    if leg == "MP_contention":
        # At the Manager's pin: the Manager releases, the Peripheral's drive flies back.
        clk_c = b.ramp("clk@man", pin="Manager", signal="clock", lane="Man CLK",
                       rising=closing, role="early",
                       start=-b.anchor_offset("Man CLK", "early", closing), vdd_tx=man_v)
        b.event("ref", 0.0, "reference", "the closing edge's reference", clk_c)
        start, clk = "ref", "clk@man"
        if ede:
            clk_o = b.ramp("clk_o@man", pin="Manager", signal="clock", lane="Man CLK",
                           rising=not closing, role="late",
                           start=-ui - b.anchor_offset("Man CLK", "late", not closing),
                           vdd_tx=man_v)
            start = b.event("open_ref", -ui, "reference", "the opening edge's reference", clk_o)
            clk = "clk_o@man"
            b.span(start, "ref", b.term(lambda s: s == "UI"), view=(clk, "clk@man"))
        _release(b, "Man", start=start, t0=t_open, clk=clk, pin="Manager", key="data@man",
                 vdd=man_v, beside=("data_acq@man",), guide=guide, clk_rising=not closing,
                 held_high=not acq_data)
        t_acq, acq_clk = b.handover("ref", 0.0, clk_c)
        clk_p = b.ramp("clk@per", pin="Peripheral", signal="clock", lane="Man CLK",
                       rising=acq_rising, role="early",
                       start=t_acq + t_pd - b.anchor_offset("Man CLK", "early", acq_rising),
                       vdd_tx=b.corners[early].vdd_tx)
        b.event("clk_at_per", t_acq + t_pd, "reference",
                "the acquiring edge's reference, at the Peripheral", clk_p)
        b.span("handover" if has_ho else "ref", "clk_at_per", b.term(lambda s: s == "t_PD"),
               view=(acq_clk or "clk@man", "clk@per"))
        det, det_t = b.clock_detection("det_acq", clk_p, "Peripheral", early,
                                       "the Peripheral detects the clock")
        b.span("clk_at_per", det, b.term(_clk), view=("clk@per",))
        anchor = b.launch(det, det_t, "Per", "ZD", "min", "acq_anchor",
                          view=("clk@per", "data_acq@per", "data@man", "data_acq@man"),
                          guides=(guide,), rising=acq_data, clk_rising=acq_rising)
        start_acq = anchor - b.anchor_offset("Per DATA", "early", acq_data)
        acq = b.ramp("data_acq@per", pin="Peripheral", signal="data", lane="Per DATA",
                     rising=acq_data, role="early", start=start_acq, vdd_tx=b.supplies[1],
                     from_hiz=True, before="Man")
        b.attach("acq_anchor", "anchor", "the acquirer's data's 20 % point", acq)
        # The drive back rides the DATA trace, short of the clock's flight by the mismatch.
        mis = _contention_mis_ns(b)
        acq_m = b.ramp("data_acq@man", pin="Manager", signal="data", lane="Per DATA",
                       rising=acq_data, role="early", start=start_acq + t_pd - mis,
                       vdd_tx=b.supplies[1])
        _contention_arrival(b, "acq_anchor", anchor + t_pd, mis, "acq_anchor_man",
                            "the acquirer's 20 % point, at the Manager", acq_m,
                            flight=b.term(lambda s: s == "t_PD"),
                            view=("data_acq@per", "data_acq@man"))
        b.span("release", "acq_anchor_man", None, view=("data@man", "data_acq@man"))
        return

    b.note = NET_ZERO_NOTE if t_pd > 0 else ""
    if leg == "PM_contention":
        # The Peripheral a flight from the Manager: its clock arrives a flight after the
        # Manager's, and the Manager's drive reaches its pin a flight after the Manager's.
        clk_c = b.ramp("clk@man", pin="Manager", signal="clock", lane="Man CLK",
                       rising=closing, role="late",
                       start=-b.anchor_offset("Man CLK", "late", closing), vdd_tx=man_v)
        b.event("ref", 0.0, "reference", "the closing edge's reference", clk_c)
        clk_r = b.ramp("clk@per", pin="Peripheral", signal="clock", lane="Man CLK",
                       rising=rel_rising, role="late",
                       start=t_open + t_pd - b.anchor_offset("Man CLK", "late", rel_rising),
                       vdd_tx=b.corners[late].vdd_tx)
        b.event("ref_rel", t_open + t_pd, "reference",
                "the clock's reference, at the Peripheral", clk_r)
        det, det_t = b.clock_detection("det_rel", clk_r, "Peripheral", late,
                                       "the Peripheral detects the clock")
        b.span("ref_rel", det, b.term(_clk), view=("clk@per",))
        if ede:
            clk_cp = b.ramp("clk_c@per", pin="Peripheral", signal="clock", lane="Man CLK",
                            rising=closing, role="late",
                            start=t_pd - b.anchor_offset("Man CLK", "late", closing),
                            vdd_tx=b.corners[late].vdd_tx)
            close = b.event("close@per", t_pd, "reference",
                            "the closing edge's reference, at the Peripheral", clk_cp)
            b.span("ref_rel", close, b.term(lambda s: s == "UI"), view=("clk@per", "clk_c@per"))
        _release(b, "Per", start=det, t0=det_t, clk="clk@per", pin="Peripheral",
                 key="data@per", vdd=b.supplies[1], beside=("data_acq@per",), guide=guide,
                 clk_rising=not closing, held_high=not acq_data)
        t_acq, acq_clk = b.handover("ref", 0.0, clk_c)
        anchor = b.launch("handover" if has_ho else "ref", t_acq, "Man", "ZD", "min",
                          "acq_anchor", view=(acq_clk or "clk@man", "data_acq@man", "data@per",
                                              "data_acq@per"),
                          guides=(guide,), rising=acq_data, clk_rising=acq_rising)
        start_acq = anchor - b.anchor_offset("Man DATA", "early", acq_data)
        acq = b.ramp("data_acq@man", pin="Manager", signal="data", lane="Man DATA",
                     rising=acq_data, role="early", start=start_acq, vdd_tx=man_v,
                     from_hiz=True, before="Per")
        b.attach("acq_anchor", "anchor", "the acquirer's data's 20 % point", acq)
        mis = _contention_mis_ns(b)
        acq_p = b.ramp("data_acq@per", pin="Peripheral", signal="data", lane="Man DATA",
                       rising=acq_data, role="early", start=start_acq + t_pd - mis,
                       vdd_tx=man_v)
        _contention_arrival(b, None, anchor + t_pd, mis, "acq_at_rel",
                            "the acquirer's 20 % point, at the Peripheral", acq_p)
        b.span("release", "acq_at_rel", None, view=("data@per", "data_acq@per"))
        return

    # P->P: A releases, B acquires, B at the Manager's end of the bus and A a flight from it.
    # A pinned placement puts both at one end, drawn at the Manager with no flight between
    # them (`contention_flight_ns`). The two detections are one gathered term, drawn on A's
    # edge from A's detection to B's, transposed to it.
    rel_role, acq_role = C.pp_clock_edges(inp, inp.envelope(b.supplies), leg,
                                          closing_rising=closing)
    close_role = acq_role if ede else rel_role
    b_name = "sigma_pm_hold_rf_clk_b" if acq_rising else "mp_early_fall_b"
    f_a = b.anchor_offset("Man CLK", rel_role, rel_rising)
    f_b = b.anchor_offset("Man CLK", acq_role, acq_rising)
    clk_a = b.ramp("clk@a", pin="Peripheral A", signal="clock", lane="Man CLK", rising=rel_rising,
                   role=rel_role, start=t_open + t_pd - f_a, vdd_tx=b.corners[late].vdd_tx)
    b.event("ref_rel", t_open + t_pd, "reference", "the clock's reference, at A", clk_a)
    det, det_t = b.clock_detection("det_a", clk_a, "Peripheral A", late, "A detects the clock")
    mark = b.event("det_b_at_a",
                   t_open + t_pd + b.crossing_offset(b_name, "Man CLK", acq_role) - f_b,
                   "mark", "B's detection, transposed to A's edge")
    b.span(det, mark, b.term(_clk), view=("clk@a",))
    if ede:
        # The closing edge takes the acquirer's factor, as on M→P and P→M: with no handover
        # UIs it is B's acquiring edge, priced by B's detection.
        clk_c = b.ramp("clk_c@a", pin="Peripheral A", signal="clock", lane="Man CLK",
                       rising=closing, role=close_role,
                       start=t_pd - b.anchor_offset("Man CLK", close_role, closing),
                       vdd_tx=man_v)
        close = b.event("close@a", t_pd, "reference", "the closing edge's reference, at A",
                        clk_c)
        b.span("ref_rel", close, b.term(lambda s: s == "UI"), view=("clk@a", "clk_c@a"))
    acq_view = ("data@b", "data@a", "data_acq@a")
    _release(b, "Per", start=det, t0=det_t, clk="clk@a", pin="Peripheral A", key="data@a",
             vdd=b.supplies[1], device="Per A", beside=("data_acq@a",), guide=guide,
             clk_rising=not closing, held_high=not acq_data)
    t_acq = max(inp.handover_UIs, 0.0) * ui if has_ho else 0.0
    clk_b = b.ramp("clk@b", pin="Peripheral B", signal="clock", lane="Man CLK",
                   rising=acq_rising, role=acq_role, start=t_acq - f_b,
                   vdd_tx=b.corners[b_name].vdd_tx)
    if has_ho:
        clk_cb = b.ramp("clk_c@b", pin="Peripheral B", signal="clock", lane="Man CLK",
                        rising=closing, role=close_role,
                        start=-b.anchor_offset("Man CLK", close_role, closing),
                        vdd_tx=b.corners[b_name].vdd_tx)
        b.event("ref_b", 0.0, "reference", "the closing edge's reference, at B", clk_cb)
        b.handover("ref_b", 0.0, view=("clk_c@b", "clk@b"))
        b.attach("handover", "reference", "the acquiring edge's reference, at B", clk_b)
    else:
        b.event("ref_b", 0.0, "reference", "the closing edge's reference, at B", clk_b)
    det_b, det_b_t = b.clock_detection("det_b", clk_b, "Peripheral B", b_name,
                                       "B detects the clock")
    anchor = b.launch(det_b, det_b_t, "Per", "ZD", "min", "acq_anchor",
                      view=("clk@b", *acq_view), guides=(guide,), rising=acq_data,
                      clk_rising=acq_rising)
    start_acq = anchor - b.anchor_offset("Per DATA", "early", acq_data)
    acq = b.ramp("data@b", pin="Peripheral B", signal="data", lane="Per DATA",
                 rising=acq_data, role="early", start=start_acq, vdd_tx=b.supplies[2],
                 from_hiz=True, device="Per B", before="Per A")
    b.attach("acq_anchor", "anchor", "B's data's 20 % point", acq)
    mis = _contention_mis_ns(b)
    acq_a = b.ramp("data_acq@a", pin="Peripheral A", signal="data", lane="Per DATA",
                   rising=acq_data, role="early", start=start_acq + t_pd - mis,
                   vdd_tx=b.supplies[2], device="Per B")
    _contention_arrival(b, None, anchor + t_pd, mis, "acq_at_rel", "B's data's 20 % point, at A",
                        acq_a)
    b.span("release", "acq_at_rel", None, view=("data@a", "data_acq@a"))


def _parts(b: _Builder, gathered: Span, field: int) -> dict[str, str]:
    """A gathered term's detections, each as its own crossing's symbol with the lane, keyed
    by field `field` of its crossing: the peripheral (1) or the corner (−1)."""
    assert gathered.term is not None
    head, _, lane = b.terms[gathered.term].symbol.partition(" · ")
    return {p.split(",")[field]: f"{p} · {lane}" for p in head.strip("()").split(" − ")}


def _move(b: _Builder, links: list[tuple[Span, int]], dt: float, pin: str,
          keep: dict[str, str]) -> list[tuple[Span, int]]:
    """`links` moved `dt` later to `pin`: each end in `keep` to the event it names, a clock
    reference to the reference of the clock edge at `pin` then, any other end to a mark."""
    moved = dict(keep)
    out = []
    for sp, way in links:
        for k in (sp.start, sp.end):
            if k in moved:
                continue
            e = next(e for e in b.events if e.key == k)
            label = f"{e.label}, moved to the {pin}"
            moved[k] = (b.reference(f"{k}@mv", pin, e.t_ns + dt, label)
                        if e.kind == "reference" else
                        b.event(f"{k}@mv", e.t_ns + dt, "mark", label))
        out.append((Span(moved[sp.start], moved[sp.end], sp.term, sp.label), way))
    return out


def _mis_back(links: list[tuple[Span, int]]) -> list[tuple[Span, int]]:
    """`links` with the trace mismatch's span running back: a cost on the acquirer's chain."""
    return [(sp, -1 if sp.label in _MIS_SYMBOLS else w) for sp, w in links]


def _contention_whole(b: _Builder) -> WholeView:
    """Contention at the releaser's pin, the margin last: the acquirer's chain from the edge
    that opens the releaser's UI (an EndDriveEarly release) or closes it, through the
    handover UIs, counted at the acquirer's pin, its detection and turn-on, to its drive
    reaching the releaser's pin; against the release, from the same edge, and the margin.
    On M→P the flights are the leg's two t_PD terms. P→M is drawn at the Peripheral alone and
    P→P at A alone, the acquirer's chain moved there by the clock's flight (`_move`), which
    the acquirer's drive's flight back cancels. A P→P gathered detection is two spans, A's on
    A's edge and B's from the moved edge to a mark."""
    inp, leg = b.inp, b.leg
    ede = inp.Man_ede if leg == "MP_contention" else inp.Per_ede
    margin = b.margin_span()
    if leg == "MP_contention":
        start = "open_ref" if ede else "ref"
        acq = _mis_back(b.ui_at(b.path(start, "acq_anchor_man"), "Peripheral", "ref_c@per"))
        return b.whole("Manager", ("Peripheral", "Manager"), acq,
                       [*b.path(start, "release", avoid=("UI",)), (margin, 0)])
    acq_pin, rel_pin, ref = (("Manager", "Peripheral", "ref") if leg == "PM_contention"
                             else ("Peripheral B", "Peripheral A", "ref_b"))
    head: list[tuple[Span, int]] = []
    start = ref
    if ede:
        start = b.reference("open_acq", acq_pin, -inp.UI_ns(), "the opening edge's reference")
        ui = b.find(pred=lambda s: s == "UI")
        head = [(Span(start, ref, ui.term, ui.label), 1)]
    if leg == "PM_contention":
        # At the Peripheral alone: the Manager's chain moved there by the clock's flight,
        # which its drive's flight back to the Peripheral equals.
        acq = _move(b, [*head, *b.path(ref, "acq_anchor")], inp.t_PD_ns(), rel_pin,
                    {start: "ref_rel", "acq_anchor": "acq_at_rel_nominal"})
        acq += _mis_back([(b.find(start="acq_at_rel_nominal"), 1)])
        rel = b.path("ref_rel", "release", avoid=("UI",))
        return b.whole(rel_pin, (rel_pin,), acq, [*rel, (margin, 0)],
                       note=PM_ONE_PIN_NOTE if contention_flight_ns(inp, leg) > 0
                       else PM_ONE_PIN_NOTE_NO_FLIGHT)
    # At A alone: B's chain moved there by the clock's flight, which B's drive's flight back
    # to A equals, B's detection a mark on no drawn ramp.
    gathered = b.find(pred=lambda s: s.startswith("("))
    part = _parts(b, gathered, 1)
    has_ho = b.has_handover()
    flight = contention_flight_ns(inp, leg)
    acq = _move(b, [*head, *(b.path(ref, "handover") if has_ho else []),
                    (Span("handover" if has_ho else ref, "det_b", gathered.term, part["B"]), 0),
                    *b.path("det_b", "acq_anchor")], flight, rel_pin,
                {start: "ref_rel", "acq_anchor": "acq_at_rel_nominal"})
    acq += _mis_back([(b.find(start="acq_at_rel_nominal"), 1)])
    rel = [(Span("ref_rel", "det_a", gathered.term, part["A"]), 0),
           *b.path("det_a", "release", avoid=(gathered.label,))]
    note = PP_ONE_PIN_NOTE if flight > 0 else PP_ONE_PIN_NOTE_NO_FLIGHT
    if has_ho and flight > 0:
        note += " " + PP_UI_NOTE
    return b.whole(rel_pin, (rel_pin,), acq, [*rel, (margin, 0)], note=note)


def _keeper_whole(b: _Builder) -> WholeView:
    """A keeper leg at the releaser's pin: the release, from the edge that opens the
    releaser's UI (a conventional release) or from its data's 20 % point (EndDriveEarly),
    against the launch, the data's crossing of the keeper's threshold, the keeper's response
    and the margin. keeper_Per is drawn at the Peripheral alone: the crossing, the response
    and the release as the Manager sees it moved back there by the data's flight, onto the
    Peripheral's data (`_keeper_at_per`); its gathered detection of the two edges is two
    spans, each on its own edge."""
    margin = b.margin_span()
    held = margin.start
    if b.leg == "keeper_Man":
        start = "rel_anchor" if b.inp.Man_ede else "open_ref"
        return b.whole("Manager", ("Manager",), b.path(start, "release"),
                       [*b.path(start, held), (margin, 0)])
    kept, margin = _keeper_at_per(b, b.path("anchor_man", held), margin)
    note = KEEPER_PER_NOTE if b.inp.t_PD_ns() > 0 else KEEPER_PER_NOTE_NO_FLIGHT
    pins = ("Peripheral",)
    if b.inp.Per_ede:
        return b.whole("Peripheral", pins, b.path("rel_anchor", "release"),
                       [*kept, (margin, 0)], note=note)
    gathered = b.find(pred=lambda s: s.startswith("("))
    part = _parts(b, gathered, -1)
    rel = [*b.path("open_ref", "close_ref"),
           (Span("close_ref", "det_close", gathered.term, part["early"]), 0),
           *b.path("det_close", "release")]
    kept = [(Span("open_ref", "det_open", gathered.term, part["late"]), 0),
            *b.path("det_open", "rel_anchor"), *kept, (margin, 0)]
    return b.whole("Peripheral", pins, rel, kept, note=note)


def _keeper_at_per(b: _Builder, links: list[tuple[Span, int]],
                   margin: Span) -> tuple[list[tuple[Span, int]], Span]:
    """keeper_Per's `links` at the Manager (the data's crossing of the keeper's threshold and
    the keeper's response) and its `margin`, moved back to the Peripheral by the data's
    flight: the data's 20 % point and the release there, and the crossing and the keeper
    having followed as points on the Peripheral's data, the keeper's threshold read on it."""
    t_pd = b.inp.t_PD_ns()
    data = next(r for r in b.ramps if r.key == "data@per")
    th = next(t for t in b.thresholds if t.ramp == "data@man")
    b.thresholds.append(replace(th, ramp=data.key))
    at = {e.key: e for e in b.events}
    moved = {"anchor_man": "rel_anchor", "release_man": "release"}
    for k in ("kcross", "keeper"):
        if k in at:
            moved[k] = b.event(f"{k}@per", at[k].t_ns - t_pd, at[k].kind,
                               f"{at[k].label}, a flight earlier at the Peripheral", data)

    def back(sp: Span) -> Span:
        return Span(moved.get(sp.start, sp.start), moved.get(sp.end, sp.end), sp.term,
                    sp.label, sp.view, sp.guides)
    return [(back(sp), w) for sp, w in links], back(margin)


# ---------------------------------------------------------------------------------------------
# Keeper: the releasing driver holds its level until the keeper has followed it
# ---------------------------------------------------------------------------------------------

def _keeper_corner(b: _Builder, side: str, clk_rising: bool) -> CrossingCorner:
    """The keeper's threshold on releasing side `side`'s data: the corner
    `calculator._keeper_case` prices for a launch from a clock edge of polarity
    `clk_rising`."""
    lane = f"{side} DATA"
    ede = b.inp.Man_ede if side == "Man" else b.inp.Per_ede
    return C._keeper_case(b.inp, lane, b.supplies, ede=ede, clk_rising=clk_rising)[0]


def _keeper(b: _Builder) -> None:
    inp = b.inp
    b.whole_of = lambda: _keeper_whole(b)
    side = "Man" if b.leg == "keeper_Man" else "Per"
    lane, key = f"{side} DATA", ("data@man" if side == "Man" else "data@per")
    pin = "Manager" if side == "Man" else "Peripheral"
    vdd = b.supplies[0] if side == "Man" else b.supplies[1]
    ede = inp.Man_ede if side == "Man" else inp.Per_ede
    # The opening edge: keeper_Man's is the one `keeper_open_rising` gives, keeper_Per's of
    # the other polarity to its closing edge. Only a Manager's launch reads its polarity.
    if side == "Man":
        opening = C.keeper_open_rising(inp)
    else:
        opening = not bool(b.breakdown.closing_rising)
    corner = _keeper_corner(b, side, opening)
    rising = corner.rising
    # The keeper reads a Peripheral's data at the Manager, one flight later.
    two_pins = side == "Per"
    t_pd = inp.t_PD_ns() if two_pins else 0.0
    # From the data's 20 % point to its 80 % point, on the drawn ramp.
    to_valid = (b.valid_offset(lane, "late", rising)
                - b.anchor_offset(lane, "late", rising))
    dz_view: tuple[str, ...] = ()
    if ede:
        anchor = 0.0
        release = anchor + to_valid + (inp.Man_t_EDE_ns if side == "Man" else inp.Per_t_EDE_ns)
    else:
        ui = inp.UI_ns()
        if side == "Man":
            clk_o = b.ramp("clk_o@man", pin=pin, signal="clock", lane="Man CLK",
                           rising=opening, role="late",
                           start=-b.anchor_offset("Man CLK", "late", opening),
                           vdd_tx=b.supplies[0])
            clk_c = b.ramp("clk_c@man", pin=pin, signal="clock", lane="Man CLK",
                           rising=not opening, role="late",
                           start=ui - b.anchor_offset("Man CLK", "late", not opening),
                           vdd_tx=b.supplies[0])
            b.event("open_ref", 0.0, "reference", "the opening edge's reference", clk_o)
            launch_from, t0 = "open_ref", 0.0
        else:
            # The Peripheral's two timings count from its own detections of the two edges.
            closing = bool(b.breakdown.closing_rising)
            late = "mp_late_fall" if closing else "mp_late_rise"
            early = "sigma_pm_hold_rf_clk" if closing else "mp_early_fall"
            clk_o = b.ramp("clk_o@per", pin=pin, signal="clock", lane="Man CLK",
                           rising=not closing, role="late",
                           start=-b.anchor_offset("Man CLK", "late", not closing),
                           vdd_tx=b.corners[late].vdd_tx)
            clk_c = b.ramp("clk_c@per", pin=pin, signal="clock", lane="Man CLK",
                           rising=closing, role="early",
                           start=ui - b.anchor_offset("Man CLK", "early", closing),
                           vdd_tx=b.corners[early].vdd_tx)
            b.event("open_ref", 0.0, "reference", "the opening edge's reference", clk_o)
            launch_from, t0 = b.clock_detection("det_open", clk_o, pin, late,
                                                "the Peripheral detects the opening edge")
        b.event("close_ref", ui, "reference", "the closing edge's reference", clk_c)
        b.span("open_ref", "close_ref", b.term(lambda s: s == "1·UI"),
               view=(clk_o.key, clk_c.key))
        if side == "Man":
            dz_from, dz_t = "close_ref", ui
            t_dz = C.man_tdz_parts(inp)[0]
        else:
            dz_from, dz_t = b.clock_detection("det_close", clk_c, pin, early,
                                              "the Peripheral detects the closing edge")
            mark = b.event("det_open_late", t0 + ui, "mark",
                           "the opening edge's detection, one UI later")
            b.span(mark, dz_from, b.term(_clk), view=(clk_c.key,))
            t_dz = inp.Per_t_DZ_max_ns
        release = dz_t + t_dz
        anchor = b.launch(launch_from, t0, side, "DD", "max", "rel_anchor",
                          view=(clk_o.key, key), rising=rising, clk_rising=opening)
        dz_view = (clk_c.key,)
    data = b.ramp(key, pin=pin, signal="data", lane=lane, rising=rising, role="late",
                  start=anchor - b.anchor_offset(lane, "late", rising), vdd_tx=vdd,
                  off_ns=release)
    if ede:
        b.event("rel_anchor", anchor, "anchor", "the data's 20 % point", data)
    else:
        b.attach("rel_anchor", "anchor", "the data's 20 % point", data)
    b.event("release", release, "release", "the driver goes high-Z", data)
    # Where the keeper reads the data: the Manager's pin, a flight from a Peripheral's.
    k_data, k_anchor, k_release = data, "rel_anchor", "release"
    if two_pins:
        k_data = b.ramp("data@man", pin="Manager", signal="data", lane=lane, rising=rising,
                        role="late", start=data.start_ns + t_pd, vdd_tx=vdd,
                        off_ns=release + t_pd)
        k_anchor = b.event("anchor_man", anchor + t_pd, "anchor",
                           "the data's 20 % point, at the Manager", k_data)
        k_release = b.event("release_man", release + t_pd, "release",
                            "the driver's high-Z, at the Manager", k_data)
    at_k = (k_data.key,) if not two_pins else (key, k_data.key)
    resp = b.has(lambda s: s == "Man_t_Keeper_Response")
    valid_label = f"the ramp's 80 % point, {'V_OH' if rising else 'V_OL'}"
    ramp_sym = C.to_voh_symbol(inp, lane, side, rising)
    b.thresholds.append(Threshold(k_data.key, "Manager", corner))
    start_t = k_data.start_ns + crossing_time_frac(corner, b.shape(lane),
                                                   swing_frac=b.swing) * k_data.t_rf_ns
    start = b.event("kcross", start_t, "crossing", "the data crosses the keeper's threshold",
                    k_data)
    flight = (("rel_anchor", k_anchor, "Flight to the keeper"),) if two_pins else ()
    b.span(k_anchor, start, b.term(lambda s: s.startswith("X_K")), view=at_k,
           guides=flight)
    if ede:
        valid = b.event("valid", anchor + to_valid, "valid", valid_label, data)
        b.span("rel_anchor", valid, b.term(lambda s: s == ramp_sym), view=(key,))
    held = start
    if resp:
        held = b.event("keeper", start_t + inp.t_keeper_ns, "keeper", "the keeper has followed",
                       k_data)
    guide = (held, k_release, "margin")
    if resp:
        b.span(start, held, b.term(lambda s: s == "Man_t_Keeper_Response"),
               view=(k_data.key,), guides=(guide,))
    if ede:
        b.span("valid", "release", b.term(lambda s: s == f"{side}_t_EDE"),
               view=tuple(dict.fromkeys((key, k_data.key))), guides=(guide,))
    else:
        b.span(dz_from, "release", b.term(lambda s: s == f"{side}_t_DZ"),
               view=tuple(dict.fromkeys((*dz_view, key, k_data.key))), guides=(guide,))
    b.span(held, k_release, None, view=(k_data.key,))


def clock_edges(tl: LegTimeline, pin: str, lo: float, hi: float) -> tuple[Ramp, ...]:
    """The clock edges at `pin` that a window from `lo` to `hi` ns can show, in time order:
    the timeline's own, and past the first and the last of them the edges the clock goes on
    making there, one UI apart and alternating in polarity, each its edge's reference the
    clock's flight to `pin` after its reference at the leg's first pin, at the slew factor,
    rail and driver of the timeline's nearest edge at `pin`. Every edge whose 20 % point lies
    within a UI and three t_RF of the window is given; the caller keeps those its window
    draws."""
    own = sorted((r for r in tl.ramps if r.signal == "clock" and r.pin == pin),
                 key=lambda r: r.start_ns)
    clocks = [r for r in tl.ramps if r.signal == "clock"]
    if not own or tl.ui_ns <= 0:
        return tuple(own)

    def ref(r: Ramp) -> float:
        return r.start_ns + tl.clock_ref_frac[r.rising] * r.t_rf_ns

    ui = tl.ui_ns
    base = min(ref(r) for r in clocks)
    index = {r.key: round((ref(r) - base) / ui) for r in own}
    flight = ref(own[0]) - base - index[own[0].key] * ui
    reach = ui + 3.0 * max(r.t_rf_ns for r in clocks)
    first, last = own[0], own[-1]
    tag = first.key.partition("@")[2]
    out = list(own)
    for near, step in ((first, -1), (last, 1)):
        n = index[near.key] + step
        while True:
            rising = near.rising == ((n - index[near.key]) % 2 == 0)
            t = base + flight + n * ui
            if (step < 0 and t < lo - reach) or (step > 0 and t > hi + reach):
                break
            out.append(replace(near, key=f"clk_e{n}@{tag}", rising=rising,
                               start_ns=t - tl.clock_ref_frac[rising] * near.t_rf_ns))
            n += step
    return tuple(sorted(out, key=lambda r: r.start_ns))


def leg_timeline(inp: C.CalcInputs, leg: C.Inequality) -> LegTimeline:
    """The timeline of `leg` at the corner `inp` describes. Raises ValueError for a name that is
    not an inequality, and for a leg that does not apply at `inp`, naming the reason."""
    if leg not in SUPPORTED_LEGS:
        raise ValueError(f"{leg}: not an inequality")
    b = _Builder(inp, leg)
    if b.breakdown.not_applicable:
        raise ValueError(f"{leg}: does not apply, so it has no timeline: "
                         f"{b.breakdown.not_applicable}")
    if leg.endswith("_contention"):
        _contention(b)
    elif leg.startswith("keeper_"):
        _keeper(b)
    else:
        which = "ZD" if leg.endswith("_ho") else "DD"
        {"MP": _mp, "PM": _pm, "PP": _pp}[leg[:2]](b, "setup" in leg, which)
    return b.done()
