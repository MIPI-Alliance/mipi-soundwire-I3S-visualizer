"""Per-leg timelines: every event of an inequality on one time axis, at its corner.

`leg_timeline(inp, leg)` places each event a leg's terms connect on the ramp it belongs to:
the Manager's clock reference, a peripheral's detection of the clock, a launch's anchor, the
receiver's threshold crossing and its setup or hold deadline. Time is in ns from the leg's
first clock edge at the Manager's pin: from the start of that edge's ramp on M→P and P→M legs,
and from its reference, its 20 % point, on P→P legs. EVERY EDGE IS PLACED BY ITS 20 % POINT,
as the calculator places it: consecutive clock edges' references are one UI apart, and one
edge's references at two pins are the flight between them apart, whatever each ramp's slew.
A clock edge two peripherals both detect has one t_RF at both pins (`calculator.pp_clock_edges`).
A P→P placement whose bus term nets to zero is drawn with both peripherals at the Manager.
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

Setup and hold legs: MP, PM and P→P, and their handover variants. Each is drawn at the
polarities of the case its breakdown reports (`InequalityBreakdown.sampling_rising`): the
sampling edge's, and the launching edge's that follows from it. A setup leg's origin is the
point one UI before its sampling reference: the launch edge's reference on M→P and P→M setup,
and on P→P setup where B's detection of the sampling edge would be one UI earlier. A turn-on
(t_ZD, on the handover variants) is drawn leaving high-Z at the launcher's pin
(`Ramp.from_hiz`).

DRIVER STATE. A ramp whose driver goes high-Z has `off_ns`: the wire keeps the level the
driver left, which the keeper holds, and the driver is off from there. A turn-on has
`from_hiz`: undriven before the ramp starts. A level held through the leg with no edge in it
(a conventional release's last bit) is a ramp with `edge` False. Event kinds name what the
point is: a ramp's 80 % point (`valid`, V_OH rising and V_OL falling), the driver going
high-Z (`release`), and the keeper having followed the level (`keeper`).

Contention legs: time is in ns from the reference of the edge that closes the releaser's
last UI, at the Manager; an EndDriveEarly release starts from the edge that opens that UI,
one UI earlier. Each is drawn at the closing-edge polarity its breakdown reports
(`InequalityBreakdown.closing_rising`), and the margin runs from the releaser's high-Z to the
acquirer's 20 % point at the releaser's pin. MP_contention is drawn at the Manager's pin,
with the acquirer's drive flying back to it; PM_contention and PP_contention, whose bus terms
net to zero, are drawn with both devices at the Manager (`LegTimeline.note`). Contention carries
no data crossing: the acquirer's first bit is the edge its turn-on is priced on
(`calculator.turn_on_rising`, rising unless a SoundWire anchor places the falling one earlier),
and a conventional releaser's last bit is the other level. An EndDriveEarly releaser's last
bit is the edge its release is priced on (`calculator.ede_release_rising`), falling unless the
rising one reaches V_OH the later.

Every launch is drawn on the data and clock edges its leg's case places it on, which a
SoundWire side's anchor correction depends on (`calculator.sw_anchor_ns`).

Keeper legs: the releaser's data edge is the one its leg is priced on
(`calculator._keeper_case`), and keeper_Man's opening edge the one
`calculator.keeper_open_rising` gives. Time is in ns from the opening edge's reference
(keeper_Man) or from its reference at the Peripheral (keeper_Per); an EndDriveEarly leg,
whose terms start at the releasing ramp, from that ramp's 20 % point. The keeper's response is
counted at the Manager from the data's crossing of the keeper's threshold
(`calculator.keeper_corners`: the Manager's V_IH,max / V_IL,min, or a SoundWire Manager's
V_TH_Keeper,max / V_TL_Keeper,min), drawn as the threshold line, so keeper_Per's data is drawn
at both pins, the flight between them a guide, and every span measured within one pin. The
margin runs from the keeper having followed the level to the release at the Manager.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace

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

    def level(self, t_ns: float) -> float:
        """The ramp's level at `t_ns`, as a fraction of V_nom, on the driver's ground. The
        driver's state does not change it: past `off_ns` the keeper holds the wire."""
        if not self.edge:
            return self.vdd_tx if self.rising else 0.0
        if self.t_rf_ns <= 0:
            return self.vdd_tx if (t_ns >= self.start_ns) == self.rising else 0.0
        return ramp_level(self.shape, self.rising, (t_ns - self.start_ns) / self.t_rf_ns,
                          V_DD=self.vdd_tx, swing_frac=self.swing_frac)


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
class LegTimeline:
    leg: str
    ramps: tuple[Ramp, ...]
    thresholds: tuple[Threshold, ...]
    events: tuple[Event, ...]
    spans: tuple[Span, ...]
    margin_ns: float
    note: str = ""      # how the leg is drawn, where that is a convention: rich text

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

    # --- geometry -----------------------------------------------------------------------
    def shape(self, lane: str) -> RampShape:
        return self.inp.Per_shape if lane == "Per DATA" else self.inp.Man_shape

    def edge_trf(self, lane: str, role: str) -> float:
        return C.lane_ns(self.inp, lane) * C.edge_factor(self.inp, lane, role)

    def ramp(self, key: str, *, pin: str, signal: str, lane: str, rising: bool, role: str,
             start: float, vdd_tx: float = 1.0, off_ns: float | None = None,
             from_hiz: bool = False, edge: bool = True, device: str = "") -> Ramp:
        r = Ramp(key, pin, signal, "Per" if lane == "Per DATA" else "Man", lane, rising, role,
                 start, self.edge_trf(lane, role), self.shape(lane), vdd_tx, self.swing,
                 off_ns, from_hiz, edge, device)
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

    def done(self) -> LegTimeline:
        missing = [self.terms[i].symbol for i in range(len(self.terms)) if i not in self.used]
        if missing:
            raise AssertionError(f"{self.leg}: terms with no span: {missing}")
        return LegTimeline(self.leg, tuple(self.ramps), tuple(self.thresholds),
                           tuple(self.events), tuple(self.spans), self.breakdown.margin_ns,
                           self.note)


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
                    from_hiz=which == "ZD")
    b.attach("anchor_man", "anchor", "the data's 20 % point, at the Manager", data_m)
    data = b.ramp("data@per", pin="Peripheral", signal="data", lane="Man DATA", rising=rising,
                  role=data_role, start=data_m.start_ns + data_pd, vdd_tx=c_data.vdd_tx)
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
        dl = b.event("deadline", det_t - inp.Per_t_IS_max_ns, "deadline", "the setup deadline")
        b.span(dl, det, b.term(lambda s: s == "Per_t_IS"), view=window,
               guides=((cross, dl, "margin"),))
        b.span(cross, dl, None, view=("data@per", clk_s.key))
    else:
        b.span(origin, det, b.term(_clk), view=at_per)
        dl = b.event("deadline", det_t + inp.Per_t_IH_max_ns, "deadline", "the hold deadline")
        b.span(det, dl, b.term(lambda s: s == "Per_t_IH"), view=window,
               guides=((dl, cross, "margin"),))
        b.span(dl, cross, None, view=("data@per", clk_s.key))


# ---------------------------------------------------------------------------------------------
# P->M: a peripheral launches, the Manager samples
# ---------------------------------------------------------------------------------------------

def _pm(b: _Builder, setup: bool, which: str) -> None:
    inp = b.inp
    ui, t_pd = inp.UI_ns(), inp.t_PD_ns()
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
                    from_hiz=which == "ZD")
    b.attach("anchor", "anchor", "the data's 20 % point", data_p)
    data_m = b.ramp("data@man", pin="Manager", signal="data", lane="Per DATA",
                    rising=c_data.rising, role=role, start=data_start + t_pd,
                    vdd_tx=c_data.vdd_tx)
    b.threshold(data_m, "Manager", data_name)
    a_man = b.event("anchor_man", anchor + t_pd, "anchor",
                    "the data's 20 % point, at the Manager", data_m)
    b.span("anchor", a_man, b.term(lambda s_: s_ == "t_PD"), view=("data@per", "data@man"))
    cross = b.event("crossing", data_start + t_pd
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
        dl = b.event("deadline", ui + f_ref - inp.Man_t_IS_max_ns, "deadline",
                     "the setup deadline")
        b.span(dl, sref, b.term(lambda s_: s_ == "Man_t_IS"), view=at_man,
               guides=((cross, dl, "margin"),))
        b.span(cross, dl, None, view=("data@man", sample))
    else:
        dl = b.event("deadline", origin_t + inp.Man_t_IH_max_ns, "deadline", "the hold deadline")
        b.span(origin, dl, b.term(lambda s_: s_ == "Man_t_IH"), view=at_man,
               guides=((dl, cross, "margin"),))
        b.span(dl, cross, None, view=("data@man", sample))


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
                    from_hiz=which == "ZD")
    b.attach("anchor", "anchor", "A's data's 20 % point", data_a)
    data_b = b.ramp("data@b", pin="Peripheral B", signal="data", lane="Per DATA",
                    rising=c_data.rising, role=role, start=data_start + flight,
                    vdd_tx=c_data.vdd_tx)
    b.threshold(data_b, "Peripheral B", data_name)
    a_b = b.event("anchor_b", anchor + flight, "anchor", "A's data's 20 % point, at B", data_b)
    if n:
        b.span("anchor", a_b, b.term(lambda s: s == "t_PD"), view=("data@a", "data@b"))
    cross = b.event("crossing", data_start + flight
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
        dl = b.event("deadline", clk_b.start_ns + x_b - inp.Per_t_IS_max_ns, "deadline",
                     "the setup deadline")
        b.span(dl, det_b, b.term(lambda s: s == "Per_t_IS"), view=window,
               guides=((cross, dl, "margin"),))
        b.span(cross, dl, None, view=("data@b", sample))
    else:
        clk_b = b.ramp("clk@b", pin="Peripheral B", signal="clock", lane="Man CLK",
                       rising=b_rising, role=b_role, start=pd_b - f_b, vdd_tx=c_b.vdd_tx)
        b.threshold(clk_b, "Peripheral B", b_name)
        det_b = b.event("det_b", clk_b.start_ns + x_b, "detection", "B detects the clock", clk_b)
        b.span(origin, det_b, b.term(_clk), view=at_b)
        dl = b.event("deadline", clk_b.start_ns + x_b + inp.Per_t_IH_max_ns, "deadline",
                     "the hold deadline")
        b.span(det_b, dl, b.term(lambda s: s == "Per_t_IH"), view=window,
               guides=((dl, cross, "margin"),))
        b.span(dl, cross, None, view=("data@b", sample))


# ---------------------------------------------------------------------------------------------
# Contention: one device releases the bus, the next acquires it
# ---------------------------------------------------------------------------------------------

CO_LOCATED_NOTE = ("Drawn with both devices at the Manager: the bus nets to zero in this "
                   "inequality.")


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


def _contention(b: _Builder) -> None:
    inp, leg = b.inp, b.leg
    ui, t_pd = inp.UI_ns(), inp.t_PD_ns()
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
    guide = ("release", "acq_anchor_man" if leg == "MP_contention" else "acq_anchor", "margin")
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
                     from_hiz=True)
        b.attach("acq_anchor", "anchor", "the acquirer's data's 20 % point", acq)
        acq_m = b.ramp("data_acq@man", pin="Manager", signal="data", lane="Per DATA",
                       rising=acq_data, role="early", start=start_acq + t_pd,
                       vdd_tx=b.supplies[1])
        b.event("acq_anchor_man", anchor + t_pd, "anchor",
                "the acquirer's 20 % point, at the Manager", acq_m)
        b.span("acq_anchor", "acq_anchor_man", b.term(lambda s: s == "t_PD"),
               view=("data_acq@per", "data_acq@man"))
        b.span("release", "acq_anchor_man", None, view=("data@man", "data_acq@man"))
        return

    b.note = CO_LOCATED_NOTE
    if leg == "PM_contention":
        clk_c = b.ramp("clk@man", pin="Manager", signal="clock", lane="Man CLK",
                       rising=closing, role="late",
                       start=-b.anchor_offset("Man CLK", "late", closing), vdd_tx=man_v)
        b.event("ref", 0.0, "reference", "the closing edge's reference", clk_c)
        clk_r = b.ramp("clk@per", pin="Peripheral", signal="clock", lane="Man CLK",
                       rising=rel_rising, role="late",
                       start=t_open - b.anchor_offset("Man CLK", "late", rel_rising),
                       vdd_tx=b.corners[late].vdd_tx)
        b.event("ref_rel", t_open, "reference", "the clock's reference, at the Peripheral",
                clk_r)
        det, det_t = b.clock_detection("det_rel", clk_r, "Peripheral", late,
                                       "the Peripheral detects the clock")
        b.span("ref_rel", det, b.term(_clk), view=("clk@per",))
        if ede:
            b.span("ref_rel", "ref", b.term(lambda s: s == "UI"), view=("clk@per", "clk@man"))
        _release(b, "Per", start=det, t0=det_t, clk="clk@per", pin="Peripheral",
                 key="data@per", vdd=b.supplies[1], beside=("data_acq@man",), guide=guide,
                 clk_rising=not closing, held_high=not acq_data)
        t_acq, acq_clk = b.handover("ref", 0.0, clk_c)
        anchor = b.launch("handover" if has_ho else "ref", t_acq, "Man", "ZD", "min",
                          "acq_anchor", view=(acq_clk or "clk@man", "data_acq@man", "data@per"),
                          guides=(guide,), rising=acq_data, clk_rising=acq_rising)
        acq = b.ramp("data_acq@man", pin="Manager", signal="data", lane="Man DATA",
                     rising=acq_data, role="early",
                     start=anchor - b.anchor_offset("Man DATA", "early", acq_data),
                     vdd_tx=man_v, from_hiz=True)
        b.attach("acq_anchor", "anchor", "the acquirer's data's 20 % point", acq)
        b.span("release", "acq_anchor", None, view=("data@per", "data_acq@man"))
        return

    # P->P: A releases, B acquires. The two detections are one gathered term, drawn on A's
    # edge from A's detection to B's, transposed to it.
    rel_role, acq_role = C.pp_clock_edges(inp, inp.envelope(b.supplies), leg,
                                          closing_rising=closing)
    b_name = "sigma_pm_hold_rf_clk_b" if acq_rising else "mp_early_fall_b"
    f_a = b.anchor_offset("Man CLK", rel_role, rel_rising)
    f_b = b.anchor_offset("Man CLK", acq_role, acq_rising)
    clk_a = b.ramp("clk@a", pin="Peripheral A", signal="clock", lane="Man CLK",
                   rising=rel_rising, role=rel_role, start=t_open - f_a,
                   vdd_tx=b.corners[late].vdd_tx)
    b.event("ref_rel", t_open, "reference", "the clock's reference, at A", clk_a)
    det, det_t = b.clock_detection("det_a", clk_a, "Peripheral A", late, "A detects the clock")
    mark = b.event("det_b_at_a", t_open + b.crossing_offset(b_name, "Man CLK", acq_role) - f_b,
                   "mark", "B's detection, transposed to A's edge")
    b.span(det, mark, b.term(_clk), view=("clk@a",))
    close, close_clk = "ref_rel", "clk@a"
    if ede:
        clk_c = b.ramp("clk_c@a", pin="Peripheral A", signal="clock", lane="Man CLK",
                       rising=closing, role=rel_role,
                       start=-b.anchor_offset("Man CLK", rel_role, closing), vdd_tx=man_v)
        close = b.event("ref", 0.0, "reference", "the closing edge's reference, at A", clk_c)
        close_clk = "clk_c@a"
        b.span("ref_rel", close, b.term(lambda s: s == "UI"), view=("clk@a", close_clk))
    _release(b, "Per", start=det, t0=det_t, clk="clk@a", pin="Peripheral A", key="data@a",
             vdd=b.supplies[1], device="Per A", beside=("data@b",), guide=guide,
             clk_rising=not closing, held_high=not acq_data)
    t_acq, _ = b.handover(close, 0.0, view=(close_clk, "clk@b"))
    clk_b = b.ramp("clk@b", pin="Peripheral B", signal="clock", lane="Man CLK",
                   rising=acq_rising, role=acq_role, start=t_acq - f_b,
                   vdd_tx=b.corners[b_name].vdd_tx)
    if has_ho:
        b.attach("handover", "reference", "the acquiring edge's reference, at B", clk_b)
    det_b, det_b_t = b.clock_detection("det_b", clk_b, "Peripheral B", b_name,
                                       "B detects the clock")
    anchor = b.launch(det_b, det_b_t, "Per", "ZD", "min", "acq_anchor",
                      view=("clk@b", "data@b", "data@a"), guides=(guide,), rising=acq_data,
                      clk_rising=acq_rising)
    acq = b.ramp("data@b", pin="Peripheral B", signal="data", lane="Per DATA",
                 rising=acq_data, role="early",
                 start=anchor - b.anchor_offset("Per DATA", "early", acq_data),
                 vdd_tx=b.supplies[2], from_hiz=True, device="Per B")
    b.attach("acq_anchor", "anchor", "B's data's 20 % point", acq)
    b.span("release", "acq_anchor", None, view=("data@a", "data@b"))


# ---------------------------------------------------------------------------------------------
# Keeper: the releasing driver holds its level until the keeper has followed it
# ---------------------------------------------------------------------------------------------

def _keeper_corner(b: _Builder, side: str, clk_rising: bool) -> CrossingCorner:
    """The keeper's threshold on releasing side `side`'s data (`calculator.keeper_corners`),
    on the edge `calculator._keeper_case` prices for a launch from a clock edge of polarity
    `clk_rising`."""
    lane = f"{side} DATA"
    ede = b.inp.Man_ede if side == "Man" else b.inp.Per_ede
    rising = C._keeper_case(b.inp, lane, b.supplies, ede=ede, clk_rising=clk_rising)[0]
    return C.keeper_corners(b.inp, lane, b.supplies)[rising]


def _keeper(b: _Builder) -> None:
    inp = b.inp
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
