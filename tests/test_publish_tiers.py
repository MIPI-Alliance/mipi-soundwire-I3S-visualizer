"""Three tiers of content, and the rules that keep them apart.

The repository serves three audiences, and conflating them is how it bloats:

  T0  dev_docs/       local only, gitignored — session notes, transcripts, probe scripts
  T1  docs/internal/  engineering artefacts the maintainers need, not users
  T2  everything else what a user or an outside contributor needs

Only T2 is published. The prune is therefore a PATH ("drop docs/internal/"), not a list that
can drift out of step with the tree — the lesson from the release gate, which spent five
releases as a prose list that no longer matched `ci.yml`.

Two rules make it enforceable, both DEFAULT-DENY:

  * nothing published may REFERENCE something unpublished — otherwise pruning leaves dangling
    links in shipping code and docs. This is not hypothetical: before the split, 18 references
    to the now-internal docs lived in files that stay public, six of them in shipping code.
  * every tracked path must be CLASSIFIED, so a new top-level file forces a decision instead
    of defaulting to published.
"""
import os
import re
import subprocess

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_INTERNAL = "docs/internal/"

# Top-level paths that are PUBLISHED. A new entry here is a conscious decision to publish.
_PUBLIC_ROOTS = (
    "README.md", "LICENSE.md", "GOVERNANCE.md", "CODEOWNERS", ".gitignore", ".github/",
    ".mipi/",                                # upstream-owned; present only once published
    "CLAUDE.md",                             # agent guidance: published deliberately, since
                                             # an outside contributor using an agent needs
                                             # the same gate and reference-model rules a
                                             # maintainer does. Keep it free of anything
                                             # site-specific — it ships.
    "pyproject.toml", "requirements.txt", "run.sh", "run.ps1", "swi3s-studio.spec",
    "swi3s_studio/", "native/", "tests/", "tools/", "data/", "visualizer_examples/",
    "docs/",                                 # docs/internal/ is carved out below
)

# Files the UPSTREAM project owns, declared in tools/publish_tree.py and grafted in at
# publish time. They must be ABSENT here and PRESENT once published — see the test below.
def _upstream_owned() -> tuple[str, ...]:
    """Read the list from the publish tool rather than restating it.

    Two hand-maintained copies drift, and the drift is invisible: a path added to
    the tool but not here would be grafted with nothing asserting it arrived, and
    one added here but not to the tool would fail a published tree for a file
    nothing grafts. Importing by path keeps one definition.
    """
    import importlib.util
    path = os.path.join(_ROOT, "tools", "publish_tree.py")
    spec = importlib.util.spec_from_file_location("publish_tree", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod._UPSTREAM_OWNED


_UPSTREAM_OWNED = _upstream_owned()


# The enforcing files necessarily CONTAIN the path they forbid, exactly as the leak scanner
# and the reference-model guard do. Self-exclusion, not an allowlist for real violations.
_SELF = {"tests/test_publish_tiers.py", "tools/publish_tree.py"}

# The three patterns .gitignore uses for the compiled core, so the walk in _files() agrees
# with the git listing about what is a build product rather than a tracked path.
_BUILD_PRODUCT = re.compile(r"^swi3score.*\.(so|pyd|dylib)$")


def _files() -> list[str]:
    """Tracked paths in a source tree; a filesystem walk in a PUBLISHED tree.

    These tests must run in both, because the published tree is precisely where the tier
    rules matter — and it has no .git, so a git-only listing raised CalledProcessError and
    took the whole file down with it (4 failures, found by tools/publish_tree.py running the
    suite on a pruned tree)."""
    out = subprocess.run(["git", "ls-files"], cwd=_ROOT, capture_output=True, text=True)
    if out.returncode == 0 and out.stdout.strip():
        return [p for p in out.stdout.splitlines() if p]
    skip = {".git", "__pycache__", ".venv", "build", "dist", ".mypy_cache", ".ruff_cache",
            ".pytest_cache", "scratch", "node_modules", "dev_docs"}
    found = []
    for base, dirs, names in os.walk(_ROOT):
        dirs[:] = [d for d in dirs if d not in skip]
        # macOS AppleDouble sidecars are filesystem metadata git never tracks;
        # one undeletable leftover on the Windows VM failed this otherwise.
        #
        # The built native core is the same kind of thing: .gitignore keeps
        # /swi3score*.{so,pyd,dylib} out of the listing above, but a walk has no such rule,
        # and the gate REBUILDS that file into the tree root before running the suite. So on
        # any tree without .git — both VM guests, and a published tree — the gate produced
        # the very artefact that then failed this test as unclassified. Matching the
        # gitignore patterns rather than the bare suffix, so a binary someone genuinely
        # commits still has to be classified.
        names = [n for n in names
                 if not n.startswith('._') and not _BUILD_PRODUCT.match(n)]
        # POSIX separators: every rule below is a "docs/…" style prefix, so a
        # backslash path matches nothing and the whole file misfires on Windows.
        found += [os.path.relpath(os.path.join(base, n), _ROOT).replace(os.sep, "/")
                  for n in names]
    return sorted(found)


def _is_published_tree() -> bool:
    """A published tree is one the internal tier has been pruned from. Keyed on that rather
    than on the presence of .git, because the public repository IS a git repository."""
    return not os.path.isdir(os.path.join(_ROOT, "docs", "internal"))


def _read(rel: str) -> str:
    with open(os.path.join(_ROOT, rel), encoding="utf-8", errors="replace") as f:
        return f.read()


def test_nothing_published_references_something_internal():
    """The rule that makes the prune safe. A public file citing docs/internal/... becomes a
    dead link the moment the tree is published, and a reader cannot tell whether the target
    was pruned or never existed."""
    offenders = []
    for rel in _files():
        if rel.startswith(_INTERNAL) or rel in _SELF:
            continue
        if not rel.endswith((".md", ".py", ".cpp", ".h", ".toml", ".sh", ".ps1", ".yml")):
            continue
        for i, line in enumerate(_read(rel).splitlines(), 1):
            if _INTERNAL in line or "internal/" + "TECH_DEBT" in line:
                offenders.append(f"{rel}:{i}: {line.strip()[:90]}")
    assert not offenders, (
        "published files reference docs/internal/, which will not exist in the published "
        "tree:\n  " + "\n  ".join(offenders)
        + "\n\nEither describe the content inline, or move the doc back out of "
          "docs/internal/ if a public reader genuinely needs it.")


def test_every_tracked_path_is_classified():
    """Default-deny: an unclassified path fails rather than silently shipping."""
    unclassified = [p for p in _files()
                    if not any(p == r or p.startswith(r) for r in _PUBLIC_ROOTS)]
    assert not unclassified, (
        "tracked path(s) belong to no declared tier:\n  " + "\n  ".join(unclassified)
        + "\n\nAdd to _PUBLIC_ROOTS to publish it, or put it under docs/internal/ to keep it "
          "in-house. Do not leave it unclassified — that is how a tree bloats by default.")


def test_the_split_is_intact():
    """Asserts something in BOTH contexts rather than skipping in one — a test that quietly
    does nothing is the failure mode this whole area keeps producing.

    Source tree: the internal tier is populated and the user-facing docs are public.
    Published tree: the internal tier is GONE and the user-facing docs are still there."""
    files = _files()
    internal = [p for p in files if p.startswith(_INTERNAL)]
    public_docs = [p for p in files
                   if p.startswith("docs/") and not p.startswith(_INTERNAL)]
    if _is_published_tree():
        assert not internal, f"published tree still carries the internal tier: {internal}"
    else:
        assert internal, "docs/internal/ is empty — the tier exists for a reason"
    for needed in ("docs/USER_GUIDE.md", "docs/architecture.md", "docs/DEVELOPMENT.md"):
        assert needed in public_docs, f"{needed} must be present — users and contributors read it"


def test_dev_docs_never_reaches_a_tree():
    """T0 is local-only in both contexts: gitignored in the source tree, absent from a
    published one."""
    assert "dev_docs/" in _read(".gitignore"), "dev_docs/ must be gitignored"
    assert not [p for p in _files() if p.startswith("dev_docs/")], \
        "dev_docs/ is present in the tree — it is local-only by definition"


def test_the_governance_files_upstream_owns_are_actually_declared():
    """Reading the list from the tool removed the drift, and with it the guard.

    `_UPSTREAM_OWNED` is now imported rather than restated, so the two copies
    cannot disagree — but a path DELETED from the tool would also vanish from the
    assertion, and the file would be dropped on the next publish with nothing
    complaining. So name the governance paths independently here.

    These three are the ones whose content is a decision of the upstream project
    rather than of this codebase. CODEOWNERS is the sharpest case and the reason
    this test exists: it names PEOPLE, so it changes upstream with nothing here
    moving. A stale copy in this tree carried "@NielWarren" after upstream had
    corrected it to "@nielwarren582", and the v3.0.13 preview would have reverted
    that — a one-token diff, in the file deciding who must approve a PR.

    Deliberately NOT the whole governance layer: GOVERNANCE.md, LICENSE.md and
    the CLA files are identical in both trees and shared by agreement, so
    carrying them is not a divergence risk.
    """
    for path in ("CODEOWNERS", ".mipi/project.yml",
                 ".github/workflows/config-check.yml"):
        assert path in _UPSTREAM_OWNED, (
            f"{path} is owned by the upstream project but is not declared in "
            f"tools/publish_tree.py::_UPSTREAM_OWNED. Publishing would either "
            f"overwrite their copy with ours or drop it entirely.")


def test_upstream_owned_files_are_absent_here_and_present_once_published():
    """Publishing is a tree REPLACEMENT onto a repository with its own history, so a file its
    maintainers added is invisible to us and a naive replacement DELETES it. That nearly
    shipped: an upstream pull request had added a project-identity file and a config-check
    workflow, and the tree built from here would have reverted both without saying so.

    The rule that resolves it is one-directional ownership — we do not carry these at all, and
    the publish tool grafts them in from upstream. Carrying our own copy would be worse than
    dropping them, because it would silently OVERWRITE theirs. Asserted in both contexts, so
    neither half can quietly become a no-op."""
    files = set(_files())
    if _is_published_tree():
        missing = [p for p in _UPSTREAM_OWNED if p not in files]
        assert not missing, (
            f"published tree is missing upstream-owned file(s): {missing}\n"
            "Build it with tools/publish_tree.py, which grafts them in — do not hand-assemble.")
    else:
        ours = [p for p in _UPSTREAM_OWNED if p in files]
        assert not ours, (
            f"this tree carries upstream-owned file(s): {ours}\n"
            "Ours would overwrite theirs on publish. Delete them here; tools/publish_tree.py "
            "takes them from the upstream ref.")


def test_the_publish_tool_declares_the_same_upstream_set():
    """Two lists of the same thing drift — the release gate spent five releases proving it. A
    test comparing them is cheaper than merging them, because the tool must run in a published
    tree where this file is only a test."""
    src = _read("tools/publish_tree.py")
    for path in _UPSTREAM_OWNED:
        assert f'"{path}"' in src, (
            f"{path} is upstream-owned here but not declared in tools/publish_tree.py's "
            "_UPSTREAM_OWNED, so the tool would not graft it in")


# "name 2.ext" / "name (2).ext" beside "name.ext" — how a file-syncing service records a
# conflict. Matched only when the un-suffixed sibling also exists, so a file legitimately
# named with a trailing number is not a false positive.
_CONFLICT_DUP = re.compile(r"^(?P<stem>.+?) (?:\(\d+\)|\d+)(?P<ext>\.[^.]*)?$")


def test_no_sync_conflict_duplicates_are_tracked():
    """A `git add -A` in a synced directory commits the sync service's conflict copies, and
    the tier rules above cannot catch it: every duplicate sits INSIDE an already-classified
    prefix, so prefix-based default-deny waves it through. Three reached a release branch this
    way, one of them a byte-identical copy of a release-gate wrapper — the worst kind, because
    a stale second copy of a gate is something a person runs by mistake and believes."""
    files = set(_files())
    dups = []
    for p in sorted(files):
        base = p.rsplit("/", 1)[-1]
        m = _CONFLICT_DUP.match(base)
        if m and p[:len(p) - len(base)] + m.group("stem") + (m.group("ext") or "") in files:
            dups.append(p)
    assert not dups, (
        "tracked path(s) look like file-sync conflict copies:\n  " + "\n  ".join(dups)
        + "\n\nDelete them and stage explicitly rather than with `git add -A`. If a name like "
          "this is deliberate, rename it — the pattern is indistinguishable from a conflict.")
