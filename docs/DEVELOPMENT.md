# Development & Release Workflow

How SWI3S Studio is developed, tested, reviewed, and released. For setup/build see the
[README](../README.md); for the test strategy see [TESTING.md](TESTING.md); for the
design see [architecture.md](architecture.md).

## Branch model

- **`main`** is the integration branch. Every change reaches it through a reviewed pull
  request, it is kept releasable (the gate green), and new major and minor releases are
  tagged on it.
- **Work happens on short-lived branches off `main`**: a feature branch
  (`feature/<topic>`) or a fix branch (`fix/<topic>`) for one change, or a sprint branch
  (`sprint/<name>`) for a group of changes that only make sense together. Each merges to
  `main` through its own pull request once it is reviewed and CI is green, and is deleted
  on merge. Prefer several small pull requests to one large one: a reviewer can keep up
  with them, a finding on one does not hold up the rest, and an outside contributor's
  pull request against `main` does not collide with a long-running branch.
- **Work prepared elsewhere arrives the same way**: as a branch based on the current
  `main` and a pull request, so that whatever was merged meanwhile, outside contributions
  included, is kept. A publish branch built by `tools/publish_tree.py` replaces the whole
  tree, so the tool refuses to build while `main` has commits the release tree has not taken
  in, or when the tree would undo a change `main` made (check 3b; see "Pre-flight before
  publishing a release").
- **Release branches `vX.Y`** are created when a minor release `vX.Y.0` is tagged on
  `main` ([GOVERNANCE.md](../GOVERNANCE.md), Branching and Tags). Sub-minor releases of
  that line are made on that branch: a fix lands on `main` first and is cherry-picked to
  `vX.Y`, which is then tagged `vX.Y.1`, `vX.Y.2`, and so on.

**Version numbers are applied only to releases.** No branch is named after the version it
is expected to become, and nothing during development changes the version: the three
version sites (`swi3s_studio/__init__.py::__version__`, `pyproject.toml::version`,
`swi3s_studio/swviz/version.py::APP_VERSION`) keep the last release's number until the
release commit sets the new one. The number follows from what the release contains
([GOVERNANCE.md](../GOVERNANCE.md), Version Numbering): any new user-visible
functionality makes it a minor release, and a sub-minor release carries fixes only.

### A pushed branch is a checkpoint, not a release

Pushing a branch, opening a pull request for it, and merging it to `main` are all
checkpoints. A release happens once, at the end, and is the only one of these that sets a
version or creates a tag:

| | when | what runs | what moves |
|---|---|---|---|
| **local commit** | continuously | the gate (`bash tests/gate.sh`) | nothing outside this machine |
| **branch push / pull request** | whenever convenient | CI, and the gate before review | the work branch. **No version change, no tag.** |
| **merge** | when reviewed and green | CI | `main` |
| **release** | once, at the end | the gate **and both test guests** | the version, the notes file, one signed tag |

Two rules:

- **The version changes only in the release commit.** Release notes for the next release
  collect in `docs/releases/next.md`, titled `(in development)`. The release commit sets
  the version, renames the notes file to `vX.Y.Z.md` and finalises its title.
  `tests/test_release_gate.py` pins the three version sites to each other.
- **Tag once, at the end, and never move or delete it.** The tag is the last step after the
  guests are green, not a by-product of merging. An annotation that turns out to be wrong
  stays as it is; the correction goes in the release notes or the next release. If a
  checkpoint needs a name — a build handed to someone, a bisect anchor — use a pre-release
  tag (`vX.Y.Z-rc1`), which the next one supersedes.

## CI (`.github/workflows/ci.yml`)

**Where it runs:** on **this repository**, where Actions is enabled.

Jobs (on every push to `main`, a `vX.Y` release branch or a `feature/`, `fix/` or `sprint/`
branch, and on every PR). Each installs
`requirements-dev.txt`, the one list of what testing needs; a job does not name packages of
its own (`tests/test_release_gate.py` checks this).

- **`test`** — builds the native core and runs the full pytest suite (goldens included)
  across ubuntu (3.11/3.12/3.13), windows (3.12 **and 3.14**) and macos (3.12). Excludes
  perf.
- **`perf`** — the perf-regression gate (`-m perf`) on one consistent runner.
- **`coverage`** — full suite with a coverage report; fails below 80 %.
- **`lint`** — `ruff` + the `mypy` non-regression gate, with mypy pinned in
  `requirements-dev.txt` to the version `tools/mypy_baseline.json` records so the ratchet
  compares. **Blocking**: a ruff finding, a
  mypy regression, mypy failing to run, or a NOT COMPARABLE result fails the job.

### Neither gate subsumes the other — run both

CI and the hand-run gate cover different things:

| | CI | `tools/gate.py` |
|---|---|---|
| pytest, perf gate | ✅ (6 configs) | ✅ (macOS; Windows 3.14 and Linux 3.11–3.13 guests) |
| ruff, mypy | ✅ | ✅ |
| runs on every push and PR | ✅ | ❌ (by hand, at a release) |
| coverage floor (80 %) | ✅ | ❌ |
| process-isolated per-suite pass | ❌ | ✅ |
| stale-ABI assertion | ❌ | ✅ |

The **release gate is `tools/gate.py`, run by hand at the release commit**: on the
development machine (checklist step 2), then on both test guests, a Windows guest on Python
3.14 and a Linux guest whose venv is repointed between 3.11, 3.12 and 3.13 (step 3). CI is a
second, differently-shaped net — useful, not sufficient.
Making `test`, `perf` and `lint` required checks on `main` is the natural next step once
branch protection is configured.

## Running tests locally

`bash tools/setup_dev.sh` (macOS / Linux) or `.\tools\setup_dev.ps1` (Windows) installs
`requirements-dev.txt` and builds the decode core into the venv `run.sh` / `run.ps1` use, and
prints that venv's activate command; `--test` / `-Test` then runs the functional suite.

```bash
python3 -m pytest -q                  # full functional suite (-m "not perf" is the default)
python3 -m pytest -m perf -q          # perf gate, in its own process
bash tests/run_all.sh                 # per-suite pass/fail summary (--verbose: test counts)
```

The runner and CI run the same tests. `run_all.sh` is a wrapper over the gate's per-suite
check (`tools/gate.py --only per-suite`): one process per suite, so you get per-suite results
and a teardown crash can't be masked by whichever suites shared a process.
`tests/test_collection.py` fails the build if any test file collects zero tests. Warnings
from our own code are errors, and a test that opens an unpatched modal dialog fails rather
than hangs; see `docs/TESTING.md` §8.

`tests/conftest.py` sets the offscreen Qt platform, a short demo, and a test-scoped
QSettings identity. After editing `native/`, rebuild the core or ~half the suite fails an
ABI check. There are two builds, and under `PYTHONPATH=.` the first one wins:

- `bash native/build_local.sh` writes `swi3score*.so` at the checkout's root. The gate's
  Unix build is this one, so after a gate run the root module exists, and the tests, the
  manual recipe and `./run.sh` (which all put the checkout first on the path) import it.
- `python3 -m pip install --force-reinstall --no-deps ./native` installs into the active
  environment (`./run.sh --rebuild` does this for its venv). pip rebuilds a local directory
  on every install, even at the same package version, so a plain install rebuilds it too.

Rebuild the one you import: `python3 -c 'import swi3score; print(swi3score.__file__)'` from
the checkout's root names it. If the root module exists, rebuild it with `build_local.sh`,
or delete it to use the installed one.

## Performance

See the maintainers' performance notes for the full loop — the recurring
whole-capture-scan regression class, the three layers of defense (CI cliff-detectors,
release-time interaction profiling on a large capture, the periodic deep-review
Workflow), the interaction latency budgets, and the UI shots review checklist.

- Hot paths are guarded by **wall-clock ceilings** in `tests/test_perf.py` (marked
  `perf`). They are *cliff detectors*, not micro-benchmarks — ceilings are generous
  (several x observed) so they catch order-of-magnitude regressions, not runner jitter.
- When you optimize (or touch) a hot path, add/adjust a ceiling so the win is defended.
  For a **per-interaction** op (cursor move / toggle / re-decode), drive the ceiling with
  a large synthetic capture (`_big_capture()`), not the demo — the demo is too small to
  expose an O(#UIs) scan.
- Capture memory scales with bus *activity* (edge arrays), not duration x sample rate —
  keep it that way; prefer windowed/on-demand work over full-capture materialization.

## Code review (per release cycle)

The review that gates a release runs on the release branch/PR, not post-hoc on `main`:

1. **Parallel subsystem reviews** — decode/ingest, swviz-engine/model, and UI/rendering,
   each with a correctness-first + performance lens (delegated to cheaper models to
   control cost). Reviewers read the actual code and verify claims by running it.
2. **A cross-subsystem "coupling / architecture" lens.** Per-subsystem reviews miss bugs
   that span layers. Explicitly review the contracts *between* subsystems.
3. **Feed prior findings forward** — hand the last cycle's findings (and this register)
   to the reviewers so a fixed class of bug can't quietly return.
4. **Adversarially verify** high-impact findings before acting; rank most-severe first.
5. **Report the declared gates' state, first.** Every lens states whether
   `bash tests/gate.sh` passes at the reviewed commit, before reporting findings. A red
   declared gate blocks the review regardless of what else was found.
6. **Trace every writer of the state a change touches.** For each field the change reads
   or writes, list everything that writes it, and check each writer keeps what is derived
   from it current: a range computed from it, a memo keyed on it, a model built from it.
   Reading the changed lines finds what they do wrong, not what a path that skips them
   leaves stale. `tests/window_checks.py` recomputes that derived state for any test to
   compare.
7. **Make each stage fail.** For a pipeline with callbacks (the load queue), the question
   is not whether the happy path works but whether every request still ends, once, when
   any stage raises. `tests/test_load_queue_settles.py` is the pattern.

## Mechanical sweeps (auto-fixers, formatters, bulk renames)

A sweep is not a normal change: it is large, looks cosmetic, and green tests prove less
than they appear to. Rules:

- **Land sweeps at the START of a cycle, never in a release commit.** A release tree should
  not mix feature work with mechanical churn, and a reviewer cannot see the former through
  the latter.
- **Prove behaviour preservation, don't assert it.** Compare the parsed AST before/after for
  every changed file. Where an AST legitimately differs (isort reorders statements), say so
  and account for it. For "whitespace only" claims, prove it: re-apply just that whitespace
  to the old source and show the ASTs then match.
- **Check the hazard the tests cannot see.** `conftest.py` sets `QT_QPA_PLATFORM` and
  `PYQTGRAPH_QT_LIB` itself, so import reordering that moves an `os.environ` line relative
  to a Qt import passes every test and breaks only the real launch. Grep the diff for moved
  env-var lines and import the real entry points.
- **Never bulk-apply `--unsafe-fixes`.** Ruff flags them unsafe because they can change
  behaviour. Handle each by hand or leave it.
- **Some fixes are wrong.** `app = QApplication(...)` in a test looks unused but keeps the
  object alive — rename to `_app`, don't delete. An `F401` in a package `__init__` may be a
  deliberate re-export. Decide per case; verify with a repo-wide search before removing.
- **The published reference model** (`swviz/models/dataport.py`, `flow_control_port.py`)
  needs the AST proof *and* a zero-comment check — see "The swviz reference model is a
  published spec deliverable" below.
- **Prefer ignoring a style rule to churning the tree for it.** Silence it in
  `[tool.ruff.lint] ignore` with a written rationale rather than hand-editing statements
  across many files.
- **A sweep over PROSE has no test at all — read the result as prose.** The rules above lean
  on the AST and the suite; a comment has neither. A substitution that changes the grammar
  around it therefore ships silently, through a green gate and a green leak scan: a doubled
  article, a dangling object, a pronoun with no antecedent. The scan checks whether a string
  is present, not whether the sentence still parses to a human. So diff the
  swept text and read every changed line, including the lines either side of it, since a
  replacement's fallout usually lands on its neighbour rather than on itself.

## Importers report what they did not read

Every importer of a user file (captures, config / Visualizer CSVs, register maps, workspaces,
Timing Calculator settings) reports each part of its input that it skipped, clamped, masked,
defaulted, truncated, reinterpreted or ignored, through `swi3s_studio.import_notes`:

- **Call `import_notes.warn(import_notes.source_name(path), message)`.** The message says what
  was not read as written and what was used instead. One warning per condition per file,
  summarised: "37 rows with an unparseable timestamp were skipped (first at line 120)".
- **The caller that owns the import collects.** `_LoadWorker` opens `import_notes.collect()`
  on its own thread around each load and re-decode and hands the list back with the session
  (`extras["import_warnings"]`, `session.import_warnings`); a GUI-thread import runs inside
  `MainWindow.reporting_import()`. The window shows each import's warnings once, in a
  non-modal **Import warnings** window, and counts them in the status bar. With no
  collector open, `warn` issues an `ImportIssueWarning`.
- **Warn on the calling thread.** The collector is a `contextvars.ContextVar` and does not
  reach a pool thread; run such work through `contextvars.copy_context().run`, or report
  from the thread that started it.
- **Warn in the load path, not the probe.** The Open Capture dialog's probe estimates; the
  load reports, so a condition is reported once.
- **Native code returns its warnings as strings**: `SwI3sConfig::warnings`, filled by
  `LoadCsv` and reached through `swi3score.csv_warnings(path)` (additive; Python reaches it
  with `getattr`), re-raised through `import_notes` by
  `visualizer_csv.report_csv_warnings`.
- **A hard failure is still an exception.** A condition the format documents as normal (an
  optional column absent, an earlier workspace without a later key) is not a warning.
- **A new importer, or a new silent case in an existing one, comes with a test** in
  `tests/test_import_warnings_*.py`: an input that triggers it gives the expected warning,
  and a well-formed input gives none.

## Golden tests

`test_visualizer_engine.py` compares `viz_engine.model_json(csv)` to
`tests/visualizer/golden_json/**`. **Never regenerate goldens without a diff review** —
when output changes, explain *why* first.

Two test files are executed directly, each to regenerate its golden through a `--regen`
flag in its `__main__`:

```bash
PYTHONPATH=. python3 tests/test_authoring_render_golden.py --regen   # the authoring-render golden
PYTHONPATH=. python3 tests/test_filters.py --regen                   # tests/fixtures/highpass_reference.json (needs SciPy)
```

The same rule applies to both: show the regenerated fixture's diff (which fields, old → new)
and wait for review before committing it. Every other suite runs through pytest only (see
[TESTING.md](TESTING.md) §6).

## The swviz reference model is a published spec deliverable

`swi3s_studio/swviz/models/dataport.py` and `flow_control_port.py` are the golden reference
model for the SWI3S transport algorithm, **published in the MIPI specification**. Treat both
as **VENDORED**: they are authored externally, arrive as whole-file drops, and this repository
owns the code path they sit on rather than their contents.

**Do not edit either file without its owners' explicit approval** — not a comment, not a
docstring, not a rename, not a hoist. No local change to them is only local: the next drop
either reverts it silently or has to be reconciled by hand. If a defect really is in the
model, the fix belongs upstream in the incoming drop; raise it, and open a `tech-debt` issue for
it meanwhile. If something needs to change on this side of the boundary, change the
code path around them instead.

Two further rules are enforced by `tests/test_reference_model_clean.py`:

- **No `#` comments, at all.** The spec text is the explanation. Implementation rationale
  goes in `docs/` or in the test that covers the behaviour — never in the deliverable.
- **Keep the module and class docstrings.** They are the documentation form these files do
  use, so the no-comment rule must not be met by deleting docs instead.

If you are about to explain something in one of those two files, that explanation belongs
somewhere else: the spacing row-boundary rationale lives in the maintainers' spacing
row-boundary write-up, and the partial-channel-group behaviour is covered by
`tests/test_transport_slot_budget.py`.

**What is NOT enforced: the docstrings' content.** This repository owns the code path these
files sit on and not their prose. A docstring that names this codebase (module and test
paths, a comparison to the C++ core, "the engine") is the authors' to change: raise it with
them on the incoming drop, and do not "fix" such a docstring here. Speed is not a goal of
this model either: it carries no hoists that exist only for speed. The partial-channel-group
clamp is not one of them.

## One release, one description

**A release is described ONCE, in `docs/releases/vX.Y.Z.md`, and every consumer reads that
file.** It is the release's changelog entry: write it during the cycle as
`docs/releases/next.md`; renaming it and finalising its title, which drops the
`(in development)` marker, is part of the release commit.

| consumer | how |
|---|---|
| GitHub release | `gh release create vX.Y.Z --notes-file docs/releases/vX.Y.Z.md` |
| publish branch commit message | `tools/publish_tree.py <sha> --message-out=<f>` (derived preamble + the file verbatim) |
| pull request body | the same file |
| annotated tag | one paragraph, then point at the file — nothing in it to drift |

Because the file is in the tree:

- it is in the **tagged tree**, so a claim about post-tag work cannot be added without amending
  something reviewable;
- it appears in the release **diff**, and gets read like any other change;
- the **leak scan covers it automatically** as a tracked file — no `--text=` to remember;
- `tests/test_release_gate.py` asserts it exists for the current `__version__`, that its title
  names that version, and that it **references no later version** except under an explicit
  `## Not in this release` heading, which is how a scope correction is recorded;
  `tests/test_publish_sync.py` asserts that an open `next.md` is marked `(in development)`.

`tools/publish_tree.py` refuses to build a tree whose version has no notes file, whose notes
are still marked `(in development)`, or that still carries `next.md`. A `--preview` of an
unfinished cycle describes itself from `next.md`.

**A claim about every case names the test that shows it.** "Each step allows less",
"never", "always", "every": a sentence of that shape is a property, and a property either
has a test or is a guess. When writing one, find the test; if there is none, write it or
narrow the sentence.

**What does NOT go in it:** anything about the publish mechanism. Which files were pruned, what
was grafted and from where, and how the tree relates to the tag are all things the tool knows
for a fact, so it generates that preamble. The notes file is about the software.

## Release checklist

1. Land all planned work and review follow-ups on `main` through their pull requests,
   and keep `docs/releases/next.md` current as you go (see "One release, one description"
   above). For a sub-minor release, cherry-pick the fixes from `main` to `vX.Y`.
2. **The gate — one command:**

   ```bash
   bash tests/gate.sh          # macOS / Linux
   .\tests\gate.ps1            # Windows (PowerShell)
   ```

   Both are thin wrappers around **`tools/gate.py`** — one implementation, so the two
   platforms cannot diverge. It rebuilds the native core and **asserts** `score_abi` against
   `session.py::_REQUIRED_SCORE_ABI`, then runs the suite, the perf gate, a per-suite pass
   (one process per file), ruff, the mypy ratchet, the leak scan and the screenshots check —
   aggregating failures so you see all of them, not just the first. Exit 0 is the gate.

   **The screenshots check** (`tools/screenshots.py`) renders the Bus Visualizer, Timing
   Calculator and Bus Analyzer in light and dark, each started in a theme and each switched
   live, on the platform's own windowing system and style. It fails on a window drawn in the
   other theme's background, text under 3:1 contrast (or not drawn at all), and a button,
   label or entry narrower than its text. Its contact sheet, `build/screenshots/
   contact_sheet.png`, is the gate's visual review: look at it on every platform before
   tagging, since the checks cannot see a layout that is merely wrong. With no display (a
   Linux shell) it checks the offscreen platform and lists the native style as NOT RUN HERE.

   If you extend the gate, extend `tools/gate.py`, never a wrapper: the wrappers are
   per-platform, and `native/build_local.sh` is a Unix compile path (Windows builds through
   pip/MSVC).

   **`NOT RUN HERE` in the summary is not a pass.** The mypy ratchet runs mypy with
   `--no-site-packages`, so its count depends only on the source and the mypy version, which
   the baseline records (`env` in `tools/mypy_baseline.json`). With any other mypy version
   the step reports NOT COMPARABLE and the gate lists it here. Install the recorded version,
   or re-baseline deliberately (`python3 tools/mypy_gate.py --update`) and pin the new
   version in `requirements-dev.txt`. The leak scan's site-specific half is NOT RUN when
   `$SWI3S_LEAK_PATTERNS` is unset or names a file holding no patterns. A mypy run that
   stopped on a blocking error (a syntax error; mypy says "errors prevented further
   checking") is reported as "mypy could not run", never as a pass.

   That does not leave the guests' code paths unchecked. The ratchet runs mypy once per
   supported platform (`--platform darwin`, `linux`, `win32`) wherever it runs, and compares
   each platform's per-file counts with that platform's counts in the baseline
   (`per_platform` in tools/mypy_baseline.json), so a Windows-only branch is type-checked on
   the Mac and a rise on one platform fails even where another platform counts more. A guest
   compares against the baseline when it has the recorded mypy version installed.

   The gate lives in that script, not in this list. `tests/test_release_gate.py` fails the
   build if `ci.yml` declares a check `tools/gate.py` does not run.

   **A green CI is not a green gate.** CI runs on the public repo only, without the
   per-suite pass or the stale-ABI assertion; the gate runs only where it is run by hand
   (here and on the two guests of step 3), not on every push. Run both.
3. **Run the gate on the test VMs, BEFORE tagging.** Not after, and not "if there is
   time". Dialog layout follows each platform's font metrics, so a defect on one guest can
   be invisible on macOS.

   **It is one command, and it must not need supervising.** The maintainers' tier holds a
   runner that gates a commit on every configured guest at once and prints a single verdict,
   nonzero if any guest fails. Both guests run concurrently. A guest with no host
   configured is reported `UNTESTED`, which is not a pass — same principle as `NOT RUN HERE`.
   There is also a queue watcher, so a run can be requested and its verdict collected later
   rather than watched; it can be installed as a login agent so it is running after a reboot.
   Prefer the direct run when you want the answer now; prefer the queue when the alternative
   is skipping the step.

   The guests are separate machines reached over SSH, with addresses and keys recorded in the
   maintainer's own configuration rather than here — neither is a git checkout, so the runner
   ships the exact tree (`git archive <sha>`) and extracts it beside the working copy rather
   than over it. Windows runs a 3.14 venv; Linux runs a venv the maintainer repoints between
   3.11/3.12/3.13.

   **When a re-run is required, and when it is not.** Gate the commit you will tag. If
   something changes after that run, re-run it when the change touches anything the guest
   would **execute or import** — product code, tests, tooling the gate calls, CI config,
   packaging. PROSE does not earn a cycle, wherever it lives: what a documentation change can
   break is a leak or a tier violation, and both are checked on the machine you are sitting
   at, not by a second platform. Note the delta in the release notes instead.

   **The release commit does not need a new guest run** when the guests gated its parent and
   it changes only the version sites, the bundled examples' `AppVersion`, the notes file and
   the README's Releases section. None of these behaves differently on another platform, and
   the gate on the development machine runs every check that reads them. Anything else in the
   release commit needs the guests again.

   **A simulated font is not this step.** Scaling the application font locally can reproduce
   a layout defect, but a point size is not a width.

   **Review the three contact sheets.** The runner's `wait` fetches each guest's screenshots
   into `build/screenshots/<platform>-<sha>/`; with the Mac's own `build/screenshots/`, that
   is every mode in both themes on all three platforms. Look at all three before tagging.
4. Make the release commit, through a pull request to `main` (or to `vX.Y` for a
   sub-minor release): choose the number (see Branch model), set it in the three version
   sites, rename `docs/releases/next.md` to `vX.Y.Z.md`, and finalise its title. For a new
   minor version, also update the README's Releases section, which names the minor version
   that receives fixes. **Only when
   the cycle is finished** — every push and merge before it is a checkpoint, not a release
   (see "A pushed branch is a checkpoint, not a release" under Branch model). Steps 4-6 are
   one event at the end, not a habit.
5. Tag **once**, at the end: annotated `vX.Y.Z` on the release commit (GPG-signed, as
   [GOVERNANCE.md](../GOVERNANCE.md) requires). For a new minor release, create the `vX.Y`
   branch from the tag at the same time.
   If signing fails with `gpg: signing failed: No agent running`, start the agent with
   `gpgconf --launch gpg-agent` and retry — do NOT fall back to an unsigned tag.

   **Keep the annotation to one paragraph and point at `docs/releases/vX.Y.Z.md`.** It is the
   one description no tool can regenerate, and the tag is never moved or replaced, so the
   annotation cannot be corrected at all. A pointer has nothing in it to drift.
6. Publish the release: `gh release create vX.Y.Z --latest --notes-file docs/releases/vX.Y.Z.md`.
   Never a hand-written body — see "One release, one description".
7. **Open a fresh `docs/releases/next.md`** with the `(in development)` marker. Nothing
   else changes: the version stays at the release just made until the next release commit.

### Pre-flight before publishing a release

Two things must hold: the checks are green, and the tree carries nothing that only makes
sense inside the project's development environment.

1. **The gate must be green**, lint included.
2. **Scan the exact tree you will publish for leakage:**

   ```bash
   python3 tools/leak_scan.py            # the working tree: tracked files, and untracked ones .gitignore does not exclude
   python3 tools/leak_scan.py <sha>      # a specific commit's tree
   python3 tools/leak_scan.py --tree=<dir>   # a directory that is not a checkout (an exported tree), walked whole
   ```

   "Leak" is **broader than secrets.** Anything that reveals or depends on the development
   environment is one, because a reader here cannot verify it and should not have to:

   - **Machine and account identifiers** — user names, absolute home paths, host names, lab
     or VM addresses, key file names. The home-path patterns cover `/Users/<name>`,
     `C:\Users\<name>` with any number of backslashes (as JSON stores it), and
     `/home/<name>` other than CI's `/home/runner`.
   - **Infrastructure names** — git hosts, repository names, CI hosts, internal tooling.
   - **Process detail that assumes a second repository** — wording that refers to a release,
     tag or review that happened somewhere the reader cannot see, or branch and tag names
     that do not exist here. Describe what a reader can check in *this* tree instead.
     (Phrase the rule, don't quote the phrases: a document that lists the exact wording it
     forbids will trip the scanner that enforces it.)
   - **Third-party material** — customer or partner names, and capture files that are not
     cleared for release.
   - **Credentials**, of any kind.

   **Write the RULE, not the story.** Tracked files carry WHAT and HOW only: what a module,
   function, field, test or section does, how to use, run, extend or configure it, and the
   rules it must keep. The leak scan cannot see this property of prose; review does.

   - **State constraints as rules.** "Bind both key spellings; the OS delivers Ctrl+Shift+.
     for Ctrl+>." One clause naming the mechanism is enough to stop someone breaking it.
   - **Keep the checkable facts:** spec references, formulas, units, the meaning of a magic
     number, compatibility notes such as "absent from workspaces saved before 3.0.20".
   - **Leave out the why:** history ("used to", "originally", "since 3.0.x"), how a defect
     was found, before/after measurements, rejected alternatives, credits and who asked for
     what. That context goes to the maintainer's local notes, outside both remotes.
   - **Generalise the artifact.** A specific capture filename, a particular machine, a named
     individual's configuration: none of it is verifiable from here, and a capture name may
     be third-party material. (This rule states the categories rather than quoting the
     words — a document that spells out the vocabulary it forbids trips the scanner
     enforcing it.) Say "a 291 MB capture", "a real cold-start capture".

   The same applies to a **release description and a commit message on a published branch**,
   which are usually the FIRST things an outside reader sees. The structural answer is "One
   release, one description" above: the description lives in `docs/releases/vX.Y.Z.md`, so it
   is a tracked file and this scan covers it with no extra step. For anything genuinely
   outside the tree — a pull request comment, a mail — `python3 tools/leak_scan.py --text=<f>`
   scans it.

   The scanner's built-in patterns are structural (private IP addresses, home-directory
   paths, macOS volume, iCloud Drive and container paths, the absolute path of the scanned tree
   and, with --tree, of the scanner's checkout, key file names, private-key headers) so they are safe to publish. Site-specific
   names go in a file named by `$SWI3S_LEAK_PATTERNS`, kept **outside** the repository — a
   published list of the strings you are trying not to publish is itself the leak. An
   exported tree with no `.git` is walked whole, except what its own `.gitignore` excludes;
   the scan prints how many paths it left out. A `.gitignore` using `!` or `**` excludes
   nothing.
3. **Check what this repository will run** — its Actions state and required checks may differ
   from where the work was staged, and a check that is advisory in one place may gate in
   another.
4. **Build the tree with the tool, never by hand:**

   ```bash
   git fetch <publish remote>                 # the graft in step 3 of the tool needs it
   python3 tools/publish_tree.py <sha>        # prune, graft, scan, and run the suite on it
   ```

   **Take in what the publish branch merged first.** Check 3b reads the upstream commits the
   release tree has taken in from `tools/publish_sync.txt`, which is published with the tree.
   It refuses to build while the upstream branch has commits after the last one, when a
   recorded commit is not on the upstream branch, or when the assembled tree undoes a commit
   contributed between two recorded points: a file it added or changed is absent, or a change
   whose removed text is back while its added text is not (whitespace and line ends do not
   count, so re-wrapping is not a change, and a later edit of the contributed text passes).
   Publish commits are this tree's own and are not checked. Carry the changes into the release
   tree and run `python3 tools/publish_tree.py --record-sync`: it appends the upstream commit
   only when every contributed commit since the last point is in the working tree, and the
   first point must be a publish commit. Commit the file with the change.

   With no <sha> the tree is built from HEAD: uncommitted changes are not in it, and the tool
   prints a warning when tracked files have any. Check 5 runs this checkout's
   tools/leak_scan.py on the assembled tree (`--tree=`), so a tree from an older commit is
   scanned with the current patterns. Check 2 also resolves relative links and symlinks into
   the internal tier.

   Set `$SWI3S_LEAK_PATTERNS` first: the tool's leak check fails when the scan ran no
   site-specific patterns, because host, repository and third-party names are only in that
   file. The leak check also fails when the scan read fewer files than the assembled tree
   holds. The suite runs against a native core the tool compiles from the assembled tree's
   own `native/`, in a temporary directory outside the tree (`native/build_local.sh` on macOS
   and Linux; `pip install --target` on Windows, which needs the build requirements from the
   package index). A core that does not build fails the check.

   Publishing is a tree **replacement** onto a repository that has its own history, so a file
   added there by its maintainers is invisible from here and a naive replacement deletes it.

   The rule is **one-directional ownership.** A short declared set of paths belongs upstream;
   this tree does not carry them at all, and the tool grafts them in from the upstream ref.
   Carrying our own copy would be worse than dropping them: dropping shows up in a diff,
   overwriting does not. The tool refuses to build a tree if the upstream ref is unavailable,
   if a declared file is missing there, or if this tree carries a copy of one.

   **It also lists every name the tree would publish for the first time** — each word the
   published tree does not already contain, split out of identifiers, with where it appears —
   and fails until a person has read the list and re-run with the `--approve-names=<digest>`
   it prints. The digest stands for that exact list, so an approval cannot cover a word added
   after it was read. It stands for words, not occurrences: a new use of a word already in the
   approved list does not change it. The review reads file paths, UTF-16 files and the
   printable text of binary files, and keeps a letter+digit token whole, so a part number is
   listed as itself, including one made only of hex letters and digits (two or more letters,
   then two or more digits, ten characters at most). A plural, past or -ing form of a
   published word is not listed; -er, -ers, -al and -ly derivations are. A file over 4 MB is
   not read; it is listed by name and SHA-256 to be opened by hand, and the digest approves
   those exact bytes. Read it for companies, people, products and part numbers, codenames and
   hosts. The leak scan cannot do this: it refuses only what someone thought to list. An agent
   preparing a publish must hand the list to the maintainer and wait for the digest.
5. Push the release branch's **content tip** — the last real commit of the release, not a
   merge commit — and never force-push `main`.

The published tree is the release tree **minus** the maintainers' tier and **plus** the
upstream-owned layer. Both halves are derivable, so the tag can state the relationship; the
published tree is not byte-identical to what the tag names, and the tag annotation must say
so. Fix anything you find in the NEXT cycle rather than patching the published copy: a tree
that differs from its tag in ways the tag does not state makes both untrustworthy.

## Deferred work

Findings consciously deferred from a review are tracked as issues with the `tech-debt`
label, so they aren't lost between releases and anyone can see what is known and left for
later. A finding from a pull request review that is not fixed in that pull request gets an
issue before the pull request is merged or closed. Pick one strategic item per cycle.
