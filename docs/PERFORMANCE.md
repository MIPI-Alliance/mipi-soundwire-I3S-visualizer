# Finding & preventing performance / UI regressions

How we systematically catch the speed and UI issues that were surfacing reactively in
user testing. The pattern behind almost all of them is the same, and so is the defense.

## The recurring class

A **per-interaction operation that scans the whole capture** — O(#UIs) or O(#samples) —
instead of O(active / #edges / windowed). It hides because:

- The unit tests and the demo are **small** (the demo's largest region is ~16 M UIs, ~0.2 s);
  the pathology only bites on a **large real capture** (100s of millions of UIs → tens of
  seconds), which CI never opens.
- It runs on a **routine action** (cursor move, Show Toggles, Persistence, re-decode), so
  the user hits it constantly.

Examples fixed in 3.0.8: `.sal` v3 varint decode (Python loop → native), Link Control
bring-up (whole-capture sort/scan → 8192-edge prefix), `is_complementary` (gigabyte copy
before a size check), TX-map persistence (`logical_or.at` over every UI → `bincount` over
data edges), audio grouping (double sort → one lexsort).

## Three layers of defense

### 1. CI cliff-detectors — `tests/test_perf.py` (`-m perf`)

Wall-clock **ceilings** on hot paths, run in their own CI job. They are *cliff detectors*,
not micro-benchmarks: ceilings are several× the observed time, so they catch
order-of-magnitude regressions, not runner jitter.

**The rule:** when you touch or add an op that runs **per cursor-move / per-toggle / per-
frame / per-re-decode**, add a ceiling — and drive it with a capture **big enough that the
O(#UIs) path would be seconds**. The whole-capture-scan tests use `_big_capture()` (30 M
synthetic clock edges + sparse data edges, no decode) so a regression to O(#UIs) is
unmistakable while the correct O(#edges) path stays sub-second. Copy that pattern; don't
rely on the demo (too small to expose scaling).

### 2. Release-time interaction profiling — on a LARGE real capture

CI can't hold a multi-GB capture, so once per release cycle profile the interactions by
hand on a representative large capture (a multi-hundred-MB PDM `.sal`). The recipe (what found
every issue above):

```bash
# Split a full open into its stages, then cProfile the heavy ones by tottime.
python3 -c "
import cProfile, pstats
from swi3s_studio.ingest import saleae_sal
from swi3s_studio.session import Session
cap = saleae_sal.load_capture('BIG.sal', 0, 1, None, auto_clock=True)
pr = cProfile.Profile(); pr.enable()
s = Session(cap, source={'type':'sal','path':'BIG.sal'}); s.audio_store()
pr.disable(); pstats.Stats(pr).sort_stats('tottime').print_stats(20)
"
```

For the GUI interactions, build a `MainWindow` offscreen, `load_session`, then time each
action (`_apply_grid_for_sample` at idle/config/audio samples; `_toggle_tx_map`;
`_toggle_tx_persist`; an SSP step / override re-decode) — a big `tottime` entry in Python
code that scales with capture length is the tell. Anything a user waits on with **no
progress indicator** is a bug; fix it or log it to
[PERF_REVIEW.md](PERF_REVIEW.md) / [TECH_DEBT.md](TECH_DEBT.md).

**Interaction latency budgets** (guidance, not hard SLAs):

| Interaction | Budget | If it can't meet it |
|---|---|---|
| Cursor move / settle → visible panes | < 100 ms | window/lazy the pane; defer hidden tabs |
| Show Toggles / pane switch | < 150 ms | windowed render, cache per capture |
| Persistence / region summary | < 100 ms cached | O(#edges) scan; worker thread on a miss |
| Re-decode (SSP / override / CSV / scrambler) | progress + off-thread | never block the GUI thread |
| Open capture | progress + off-thread | already the async load worker |

Rules of thumb: **windowed/on-demand over full-capture materialization**; **O(active) not
O(duration)**; memory scales with bus *activity* (edges), not duration × rate; heavy work
off the GUI thread with a placeholder + token-guarded result (see the TX-persistence
worker in `main_window.py`).

### 3. Periodic deep review — the find→verify→dedup Workflow

Once per major cycle, run the multi-agent performance review (5 rounds: find →
adversarially-verify → dedup; see the 3.0.8 run recorded in [PERF_REVIEW.md](PERF_REVIEW.md)).
It sweeps for the whole class at once rather than waiting for user reports. Feed the prior
findings forward so a fixed class can't quietly return.

## UI review pass

Render every view offscreen and **look** before a release (and before pushing UI changes):

```bash
python tools/shots.py            # all views → ./shots/*.png  (also --list, --size, --out)
```

Review `analysis`, `visualizer`, `timing` in **both** themes. Checklist:

- **Native-style chrome the QSS can't reach.** On macOS `QMacStyle` paints dock chrome
  (title-bar close/float buttons, the tab-bar *base* strip) with system colours a QSS
  `background`/`::close-button` rule can't override — offscreen (non-native style) renders
  it flat, so it looks fine in shots but wrong on a Mac. Fixes so far: custom
  `_DockTitleBar` widget for the close X; `QTabBar.setDrawBase(False)` for the tab base.
  When a control looks wrong only on real macOS, suspect this and verify on the Mac.
- **Clipping / alignment / overflow** — pinned widths, two-row bars where a one-row bar
  clips, equivalent elements aligned across sibling panes.
- **Dark + light** — deselected tab text readable, no dark-on-dark, contrast holds.
- **Empty / bring-up states** — e.g. the Bus Grid shows "No PHY Selected" in the pre-audio
  region in *every* mode (config layout AND TX map), not a bogus grid.

Shots use a fallback font, so judge structure/alignment/clipping — not final typography.

## Release gate

On each release branch, before merging to `main`:

1. Full suite green on macOS **and** the Windows VM (native rebuilt) — see
   [windows setup](TESTING.md) / DEVELOPMENT.md.
2. `pytest -m perf` green (the cliff-detectors above).
3. The release-time interaction profiling pass (§2) on a large capture — fix or log.
4. The UI shots review (both themes) with the checklist above.
5. Deep Workflow review (§3) at least once per major cycle.
