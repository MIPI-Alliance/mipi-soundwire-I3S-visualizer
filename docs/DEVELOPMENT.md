# Development & Release Workflow

How SWI3S Studio is developed, tested, reviewed, and released. For setup/build see the
[README](../README.md); for the test strategy see [TESTING.md](TESTING.md); for the
design see [architecture.md](architecture.md).

## Branch model

- **`main`** is always the latest *released* state (it carries the most recent `vX.Y.Z`
  tag). It is protected — changes reach it only by merging a release branch.
- **`release/X.Y.Z`** — every release cuts the branch for the **next** version. All work
  toward that version (features, fixes, review follow-ups) lands on this branch via PRs,
  runs through CI, and is reviewed there. At release time it merges to `main` and is
  tagged.

**On every release, create the next version's branch.** Immediately after tagging
`vX.Y.Z`, branch `release/X.Y.(Z+1)` off `main` and bump the version
(`swi3s_studio/__init__.py::__version__`, `pyproject.toml::version`,
`swi3s_studio/swviz/version.py::APP_VERSION`) so dev builds identify as the next version.
Branch names use `release/…`, never `vX.Y.Z`, so they don't collide with the release tag.

### A corp push is a checkpoint, not a release

A release branch may be **long-lived**. Work evolves on it locally, reaches the corp remote
whenever it is convenient, and is published ONCE at the end — so three different things happen
at three different cadences, and only the last of them is a release:

| | when | what runs | what moves |
|---|---|---|---|
| **local commit** | continuously | the gate (`bash tests/gate.sh`) | nothing outside this machine |
| **corp checkpoint** | whenever convenient | the gate | `release/X.Y.Z` on the corp remote. **No tag. `main` untouched.** |
| **release** | once, at the end | the gate **and both test guests** | `main`, one tag, one published PR |

Two rules follow, and both exist because 3.0.16 was tagged the moment its branch merged rather
than when the cycle was actually finished:

- **The version does not change during a cycle, and the changelog header keeps
  `(in development)` until the release commit.** Finalising that header is part of releasing,
  not part of working. All three version sites are pinned to each other by
  `tests/test_release_gate.py`, so the marker is the only thing left to discipline.
- **Tag once, at the end, and never re-point it.** The tag is the last step after the guests are
  green, not a by-product of merging. If a checkpoint needs a name — a build handed to someone,
  a bisect anchor — use a pre-release tag (`vX.Y.Z-rc1`), which is expected to be superseded and
  so does not need re-pointing. Re-pointing a real tag is the 3.0.6 smell.

The cost of getting this wrong is not academic: a tag cut early either has to be re-pointed, or
the tree eventually published differs from what the tag claims to describe — and this document
says elsewhere that such a mismatch makes both untrustworthy.

## CI (`.github/workflows/ci.yml`)

**Where it runs:** on **this repository**, where Actions is enabled — confirmed 2026-08-01,
when the first published PR of the v3 line ran the workflow and reported 445 ruff findings.
It had been configured since 3.0.7 without ever executing, which is how those accumulated
unnoticed.

Jobs (on every push to `main`/`release/**` and every PR):

- **`test`** — builds the native core and runs the full pytest suite (goldens included)
  across ubuntu (3.11/3.12/3.13), windows (3.12 **and 3.14**) and macos (3.12). Excludes
  perf. The matrix is deliberate and its rationale is commented in the workflow.
- **`perf`** — the perf-regression gate (`-m perf`) on one consistent runner.
- **`coverage`** — full suite with a coverage report (non-gating; no threshold yet).
- **`lint`** — `ruff` + the `mypy` non-regression gate. **Blocking as of 3.0.12.**

### Neither gate subsumes the other — run both

This is the trap that cost five releases' worth of lint debt. CI and the hand-run gate cover
different things, and each was assumed to cover everything:

| | CI | `tools/gate.py` |
|---|---|---|
| pytest, perf gate | ✅ (6 configs) | ✅ (macOS, Windows) |
| ruff, mypy | ✅ | ✅ |
| Linux, Python 3.12/3.13 | ✅ | ❌ |
| process-isolated per-suite pass | ❌ | ✅ |
| stale-ABI assertion | ❌ | ✅ |

The **release gate is `tools/gate.py`, run by hand on macOS and Windows** at the release
commit (checklist step 2). CI is a second, differently-shaped net — useful, not sufficient.
Making `test`, `perf` and `lint` required checks on `main` is the natural next step once
branch protection is configured.

## Running tests locally

```bash
bash tests/run_all.sh                 # per-suite pass/fail summary + test counts
QT_QPA_PLATFORM=offscreen PYTHONPATH=. python3 -m pytest -m "not perf" -q   # full suite
QT_QPA_PLATFORM=offscreen PYTHONPATH=. python3 -m pytest -m perf -q         # perf gate
```

The runner and CI run the same tests — `run_all.sh` is a pytest wrapper (one process per
suite, so you get per-suite results and a teardown crash can't be masked by whichever
suites shared a process). `tests/test_collection.py` fails the build if any test file
collects zero tests.

`tests/conftest.py` sets the offscreen Qt platform, a short demo, and a test-scoped
QSettings identity. After editing `native/`, rebuild the core (`pip install ./native` or
`bash native/build_local.sh`) or ~half the suite fails an ABI check.

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
   that span layers — e.g. the 3.0.6 DP-label bug (an engine label doubling as a
   regex-parsed serialization key *and* a display string) slipped because no single
   reviewer owned the seam. Explicitly review the contracts *between* subsystems.
3. **Feed prior findings forward** — hand the last cycle's findings (and this register)
   to the reviewers so a fixed class of bug can't quietly return.
4. **Adversarially verify** high-impact findings before acting; rank most-severe first.
5. **Report the declared gates' state, first.** Every lens states whether
   `bash tests/gate.sh` passes at the reviewed commit, before reporting findings. A red
   declared gate blocks the review regardless of what else was found. The 3.0.11 review ran
   four lenses and found three real defects while the tree carried 445 ruff findings and 448
   mypy errors — because the brief said "verify by running tests" and lint/type was in no
   lens's scope. One of those findings (an f-string with no placeholder) was introduced by
   that very cycle and passed four reviewers.

## Mechanical sweeps (auto-fixers, formatters, bulk renames)

A sweep is not a normal change: it is large, looks cosmetic, and green tests prove less
than they appear to. Rules, learned from the 3.0.12 ruff sweep (226 auto-fixes, 126 files):

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
  needs the AST proof *and* a zero-comment check — see the reference-model section above.
- **Prefer ignoring a style rule to churning the tree for it.** 194 layout findings were
  silenced in `[tool.ruff.lint] ignore` with a written rationale rather than hand-splitting
  ~190 statements across 30+ files. A clean tree is what makes the gate blockable; uniform
  statement-per-line style is not worth a 30-file diff.
- **A sweep over PROSE has no test at all — read the result as prose.** The rules above lean
  on the AST and the suite; a comment has neither. A substitution that changes the grammar
  around it therefore ships silently: the changelog de-narration pass made 33 of them and left
  a doubled article, two dangling objects, a pronoun with no antecedent and a 147-character
  line, all through a green gate. The leak scan was green as well, and correctly — it checks
  whether a string is present, not whether the sentence still parses to a human. So diff the
  swept text and read every changed line, including the lines either side of it, since a
  replacement's fallout usually lands on its neighbour rather than on itself.

## Golden tests

`test_visualizer_engine.py` compares `viz_engine.model_json(csv)` to
`tests/visualizer/golden_json/**`. **Never regenerate goldens without a diff review** —
when output changes, explain *why* first. A golden diff has already caught a fix landing
in the wrong layer; that's the point.

Regenerating the authoring-render golden is the one place a test file is still executed
directly, because the regenerator lives behind a flag in its `__main__`:

```bash
PYTHONPATH=. python3 tests/test_authoring_render_golden.py --regen
```

Every other suite runs through pytest only (see [TESTING.md](TESTING.md) §6).

## The swviz reference model is a published spec deliverable

`swi3s_studio/swviz/models/dataport.py` and `flow_control_port.py` are the golden reference
model for the SWI3S transport algorithm, **published in the MIPI specification**. Treat both
as **VENDORED**: they are authored externally, arrive as whole-file drops, and this repository
owns the code path they sit on rather than their contents.

**Do not edit either file without the maintainer's explicit approval** — not a comment, not a
docstring, not a rename, not a hoist. There is no local change to them that is only local: the
next drop either reverts it silently or has to be reconciled by hand, and a divergence here is
a divergence from a published specification, which is not a thing this repository is entitled
to introduce on its own. If a defect really is in the model, the fix belongs upstream in the
incoming drop; raise it, and record it in the tech-debt register meanwhile. If something needs
to change on this side of the boundary, change the code path around them instead.

Two further rules are enforced by `tests/test_reference_model_clean.py`:

- **No `#` comments, at all.** The spec text is the explanation. Implementation rationale
  goes in `docs/` or in the test that covers the behaviour — never in the deliverable.
- **Keep the module and class docstrings.** They are the documentation form these files do
  use, so the no-comment rule must not be met by deleting docs instead.

This is easy to violate by accident, because a comment looks free and no other test can
see one. v2.1.12 shipped a single comment in each file; by 3.0.11 `dataport.py` had 21
lines carrying `#` and `flow_control_port.py` had 4, arrived via three unrelated commits —
a perf hoist (`43779d7`), the spacing row-boundary fix (`f680b14`), and the
partial-channel-group fix. Each was locally defensible; the sum was ~20 lines of
implementation commentary inside a spec artefact.

If you are about to explain something in one of those two files, that explanation belongs
somewhere else: the spacing row-boundary rationale lives in
the maintainers' spacing row-boundary write-up, and the partial-channel-group rationale in
`tests/test_transport_slot_budget.py`.

**What is NOT enforced: the docstrings' content.** This repository owns the code path these
files sit on and not their prose. A third rule briefly rejected docstrings that named this
codebase (module and test paths, a comparison to the C++ core, "the engine"); it is withdrawn,
because holding it means rewriting the author's words on every drop or carrying a divergent
copy, and the next drop undoes either one. Raise it in review of the incoming drop instead —
and do not "fix" such a docstring here, because that is the change that silently diverges the
file. Speed is likewise not a goal of this model: three hoists that existed only for it were
removed in 3.0.13, at a cost of 7% on an engine build three orders of magnitude inside its
ceiling. The partial-channel-group clamp is not in that category.

## One release, one description

**A release is described ONCE, in `docs/releases/vX.Y.Z.md`, and every consumer reads that
file.** Write it during the cycle alongside the changelog entry; finalising it is part of
releasing, like the `(in development)` header.

| consumer | how |
|---|---|
| corp GitHub release | `gh release create vX.Y.Z --notes-file docs/releases/vX.Y.Z.md` |
| publish branch commit message | `tools/publish_tree.py <sha> --message-out=<f>` (derived preamble + the file verbatim) |
| pull request body | the same file |
| annotated tag | one paragraph, then point at the file — nothing in it to drift |

The alternative is what 3.0.17 did, and it is worth stating plainly because every one of those
descriptions looked fine on its own. It was written **three times by hand** — a signed tag
annotation, a corp release body, and the publish branch's commit message — from a changelog
that was already the real record. They disagreed with each other, and one of them **announced
a memory-guard fix that landed after the tag**, because it was written at push time, by which
point the fix existed and the sentence read as true.

Four things follow from the file being in the tree, and they are the reason this is a file
rather than a discipline:

- it is in the **tagged tree**, so a claim about post-tag work cannot be added without amending
  something reviewable;
- it appears in the release **diff**, and gets read like any other change;
- the **leak scan covers it automatically** as a tracked file — no `--text=` to remember;
- `tests/test_release_gate.py` asserts it exists for the current `__version__`, that its title
  names that version, and that it **references no later version** except under an explicit
  `## Not in this release` heading, which is the honest way to record a scope correction.

`tools/publish_tree.py` refuses to build a tree whose version has no notes file, or whose notes
are still marked `(in development)`.

**What does NOT go in it:** anything about the publish mechanism. Which files were pruned, what
was grafted and from where, and how the tree relates to the tag are all things the tool knows
for a fact, so it generates that preamble. The notes file is about the software.

## Release checklist

1. Land all planned work + review follow-ups on `release/X.Y.Z`, and write
   `docs/releases/vX.Y.Z.md` as you go (see "One release, one description" above).2. **The gate — one command:**

   ```bash
   bash tests/gate.sh          # macOS / Linux
   .\tests\gate.ps1            # Windows (PowerShell)
   ```

   Both are thin wrappers around **`tools/gate.py`** — one implementation, so the two
   platforms cannot diverge. It rebuilds the native core and **asserts** `score_abi` against
   `session.py::_REQUIRED_SCORE_ABI` (a stale `.pyd`/`.so` has faked a green Windows result
   before), then runs the suite, the perf gate, a per-suite pass (one process per file),
   ruff, and the mypy ratchet — aggregating failures so you see all of them, not just the
   first. Exit 0 is the gate.

   It was briefly a bash script, which made it Unix-only and so re-created the very gap it
   exists to close: no `bash` on PATH on the Windows VM, and `native/build_local.sh` is a
   Unix compile path (Windows builds through pip/MSVC). If you extend the gate, extend
   `tools/gate.py`, never a wrapper.

   **`NOT RUN HERE` in the summary is not a pass.** The mypy ratchet compares against a
   baseline recorded in a specific environment; mypy's findings depend on the stub versions
   it reads, so numpy 2.4.4 (macOS) and 2.5.1 (the VM) give 205 and 222 errors for the
   *identical* tree. Rather than fail on an environment difference or pretend it checked,
   the step reports NOT COMPARABLE and the gate lists it. Run the ratchet where the baseline
   was taken, or re-baseline there (`python3 tools/mypy_gate.py --update`).

   The gate lives in that script, not in this list. It used to be prose here while
   `ci.yml` declared its own set; they drifted, the lint/type half was in CI but not here,
   and 445 ruff findings plus 448 mypy errors accumulated over five releases before the
   first public PR reported them. `tests/test_release_gate.py` now fails the build if
   `ci.yml` declares a check `tools/gate.py` does not run.

   **A green CI is not a green gate.** CI runs on the public repo only, and covers neither
   the perf gate nor Windows; the gate covers neither Linux nor Python 3.12/3.13. Run both.
3. **Run the gate on the test VMs, BEFORE tagging.** Not after, and not "if there is
   time": this step is the one the checklist keeps losing and it has cost two consecutive
   releases. 3.0.13 and 3.0.14 were both tagged, published and opened as pull requests on a
   macOS-only gate, and both were red on Windows within minutes — first a dialog button
   clipped by 4 px, then, after a fix verified against a *simulated* wide font, the same
   dialog's label column clipped by 53 px. Neither defect is visible on this platform's
   metrics, and neither was a subtle one; they were simply never run.

   **It is one command, and it must not need supervising.** The maintainers' tier holds a
   runner that gates a commit on every configured guest at once and prints a single verdict,
   nonzero if any guest fails. Both guests run concurrently, because in series this is a
   16-minute step and a 16-minute step is one that gets skipped. A guest with no host
   configured is reported `UNTESTED`, which is not a pass — same principle as `NOT RUN HERE`.
   There is also a queue watcher, so a run can be requested and its verdict collected later
   rather than watched; it can be installed as a login agent so it is running after a reboot.
   Prefer the direct run when you want the answer now; prefer the queue when the alternative
   is skipping the step.

   The guests are separate machines reached over SSH, with addresses and keys recorded in the
   maintainer's own configuration rather than here — neither is a git checkout, so the runner
   ships the exact tree (`git archive <sha>`) and extracts it beside the working copy rather
   than over it. Windows runs the established 3.14 venv, deliberately a version no CI job and
   no macOS run covers; Linux runs a venv the maintainer repoints between 3.11/3.12/3.13,
   because "which interpreter" is the question that guest exists to answer.

   **What the guests have actually caught**, beyond the two dialog defects: the offline core
   build (`native/build_local.sh`) had never worked on Linux at all — it hardcoded `clang++`
   and passed a Mach-O-only linker flag — and it compiled against whatever `python3` PATH
   offered rather than the interpreter running the gate. Both were invisible here, and CI
   cannot see them because CI builds the core through pip.

   **When a re-run is required, and when it is not.** Gate the commit you will tag. If
   something changes after that run, re-run it when the change touches anything the guest
   would **execute or import** — product code, tests, tooling the gate calls, CI config,
   packaging. PROSE does not earn a cycle, wherever it lives: what a documentation change can
   break is a leak or a tier violation, and both are checked on the machine you are sitting
   at, not by a second platform. Note the delta in the release notes instead.

   (The first wording of this rule said "touches the published tree", which would have
   demanded a Windows run for editing this paragraph. Stated as a rule at all because the
   alternative is either a regress of 15-minute runs or a shrug, and the shrug is how this
   step went missing in the first place.)

   **A simulated font is not this step.** Scaling the application font locally is worth doing
   — it is how both defects were finally reproduced — but a point size is not a width, and a
   fix verified that way shipped broken once already.
4. Merge `release/X.Y.Z` → `main`. **Only when the cycle is finished** — a release branch may be
   long-lived, and pushing it to the corp remote along the way is a checkpoint, not a release
   (see "A corp push is a checkpoint, not a release" under Branch model). Steps 4-6 are one
   event at the end, not a habit.
5. Tag **once**, at the end: annotated `vX.Y.Z` on the merge commit (public MIPI releases
   use a GPG-signed tag + the governed PR flow — see [GOVERNANCE.md](../GOVERNANCE.md)).
   If signing fails with `gpg: signing failed: No agent running`, start the agent with
   `gpgconf --launch gpg-agent` and retry — do NOT quietly fall back to an unsigned tag, which
   is how v3.0.15's tag ended up unsigned.

   **Keep the annotation to one paragraph and point at `docs/releases/vX.Y.Z.md`.** It is the
   one description no tool can regenerate, and it is signed, so it is also the one that cannot
   be corrected without rewriting a signed object. A pointer has nothing in it to drift.

   **Rewriting an annotation is NOT the 3.0.6 smell, and the difference is the target.** Moving
   a tag to a different commit says the release was cut too early; rewriting the message at a
   fixed commit says nothing about when it was cut. If you do it: force-UPDATE the tag rather
   than delete-and-recreate, because a delete can orphan the attached release; keep the old tag
   object on a local `refs/original/…` ref so it stays recoverable; and remember that
   `git fetch` does **not** update a tag that already exists locally — anyone holding it needs
   `git fetch --tags --force`. That last point is the real cost, and it is why a pointer
   annotation is worth having in the first place.
6. Publish the release: `gh release create vX.Y.Z --latest --notes-file docs/releases/vX.Y.Z.md`.
   Never a hand-written body — see "One release, one description".
7. **Cut `release/X.Y.(Z+1)` and bump the version** (see Branch model), and open
   `docs/releases/vX.Y.(Z+1).md` with the `(in development)` marker.

Cut the tag *once, after the cycle settles* — re-pointing a published tag (as happened
repeatedly during 3.0.6) is a smell that the release was tagged too early.

### Pre-flight before publishing a release

Publishing is the first time this code meets an audience that cannot see how it was made, so
two things matter: the checks must be green, and the tree must carry nothing that only makes
sense inside the project's development environment.

1. **The gate must be green**, lint included. That step was once missing, and the first
   published PR of the v3 line greeted reviewers with 445 `ruff` findings.
2. **Scan the exact tree you will publish for leakage:**

   ```bash
   python3 tools/leak_scan.py            # the working tree
   python3 tools/leak_scan.py <sha>      # a specific commit's tree
   ```

   "Leak" is **broader than secrets.** Anything that reveals or depends on the development
   environment is one, because a reader here cannot verify it and should not have to:

   - **Machine and account identifiers** — user names, absolute home paths, host names, lab
     or VM addresses, key file names.
   - **Infrastructure names** — git hosts, repository names, CI hosts, internal tooling.
   - **Process detail that assumes a second repository** — wording that refers to a release,
     tag or review that happened somewhere the reader cannot see, or branch and tag names
     that do not exist here. Describe what a reader can check in *this* tree instead.
     (Phrase the rule, don't quote the phrases: a document that lists the exact wording it
     forbids will trip the scanner that enforces it.)
   - **Third-party material** — customer or partner names, and capture files that are not
     cleared for release.
   - **Credentials**, of any kind.

   **Write the RULE, not the story.** Every leak above is a fact about the world; this one is
   a fact about the prose, and it is the one the scanner cannot see. A published file — code
   comment, changelog entry, test docstring, release description — should state what the
   software does and what a reader can check in this tree. It should not narrate how the
   change came about. Three habits, each of which shipped in v3.0.17 and had to be reworded:

   - **No discovery narrative.** "A report came in that X was slow, and investigation found
     four defects" tells the reader about the project's week. "X was slow because of A, B, C
     and D" tells them about the software. Cut who reported it, who measured it, what was
     tried first and what was ruled out. The reasoning that survives is the reasoning a
     reader needs to use or change the code — not the sequence in which it was obtained.
   - **Generalise the artifact.** A specific capture filename, a particular machine's memory
     size, a named individual's configuration: none of it is verifiable from here, and a
     capture name may be third-party material besides. (This rule states the categories
     rather than quoting the words — a document that spells out the vocabulary it forbids
     trips the scanner enforcing it, which is the same trap the scanner itself avoids by
     building its patterns from parts.) Say "a 291 MB capture", "a machine
     with far more RAM", "a real cold-start capture". Keep the MEASUREMENT — it is what makes
     the claim checkable — and drop the label identifying whose it was.
   - **Generalise the finding.** State the defect and its rule so it reads as a property of
     the code ("the guard's budget scaled with free RAM, so a larger machine loaded more
     without asking"), not as an incident report ("on the 103 GB box it reached 60 GB").

   The same applies to a **release description and a commit message on a published branch**,
   which are usually the FIRST things an outside reader sees. The structural answer is "One
   release, one description" above: the description lives in `docs/releases/vX.Y.Z.md`, so it
   is a tracked file and this scan covers it with no extra step. For anything genuinely
   outside the tree — a pull request comment, a mail — `python3 tools/leak_scan.py --text=<f>`
   scans it. (An earlier version of this paragraph named a `publish_tree.py --message=` flag
   that was never implemented; the scan lives in the scanner.)

   The scanner's built-in patterns are structural (private IP addresses, home-directory
   paths, key file names, private-key headers) so they are safe to publish. Site-specific
   names go in a file named by `$SWI3S_LEAK_PATTERNS`, kept **outside** the repository — a
   published list of the strings you are trying not to publish is itself the leak. This
   document used to contain exactly that list.
3. **Check what this repository will run** — its Actions state and required checks may differ
   from where the work was staged, and a check that is advisory in one place may gate in
   another.
4. **Build the tree with the tool, never by hand:**

   ```bash
   git fetch <publish remote>                 # the graft in step 3 of the tool needs it
   python3 tools/publish_tree.py <sha>        # prune, graft, scan, and run the suite on it
   ```

   Publishing is a tree **replacement** onto a repository that has its own history, so a file
   added there by its maintainers is invisible from here and a naive replacement deletes it.
   That is not hypothetical: an upstream pull request had aligned that repository with its
   organisation's project template, and the tree built from here would have reverted the whole
   thing — silently, because a revert and an absence look identical in a tree replacement.

   The rule is **one-directional ownership.** A short declared set of paths belongs upstream;
   this tree does not carry them at all, and the tool grafts them in from the upstream ref.
   Carrying our own copy would be worse than dropping them: dropping shows up in a diff,
   overwriting does not. The tool refuses to build a tree if the upstream ref is unavailable,
   if a declared file is missing there, or if this tree carries a copy of one.
5. Push the release branch's **content tip** — the last real commit of the release, not a
   merge commit — and never force-push `main`.

The published tree is the release tree **minus** the maintainers' tier and **plus** the
upstream-owned layer. Both halves are derivable, so the tag can state the relationship rather
than asking a reader to trust a hand-assembled snapshot — but it does mean the published tree
is no longer byte-identical to what the tag names, and the tag annotation must say so. Fix
anything you find in the NEXT cycle rather than patching the published copy: a tree that
differs from the tag claiming to describe it in ways the tag does not state makes both
untrustworthy.

## Deferred work

Findings consciously deferred from a review cycle live in the maintainers' tech-debt register so
they aren't lost between releases. Pick one strategic item per cycle.
