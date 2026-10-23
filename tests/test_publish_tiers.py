"""Three tiers of content, and the rules that keep them apart.

  T0  dev_docs/       local only, gitignored — session notes, transcripts, probe scripts
  T1  docs/internal/  engineering artefacts the maintainers need, not users
  T2  everything else what a user or an outside contributor needs

Only T2 is published. The prune is a PATH ("drop docs/internal/"), not a list of files.

Two rules, both DEFAULT-DENY:

  * nothing published may REFERENCE something unpublished, so pruning leaves no dangling
    links in shipping code and docs.
  * every tracked path must be CLASSIFIED, so a new top-level file forces a decision instead
    of defaulting to published.
"""
import functools
import os
import re
import subprocess

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_INTERNAL = "docs/internal/"
# Maintainers'-tier files outside docs/internal/ (see tools/publish_tree.py): pruned too.
_INTERNAL_FILES = ("CLAUDE.md",)

# Top-level paths that are PUBLISHED. A new entry here is a conscious decision to publish.
_PUBLIC_ROOTS = (
    "README.md", "LICENSE.md", "GOVERNANCE.md", "CONTRIBUTING.md", "CODEOWNERS", ".gitignore",
    ".github/",
    ".mipi/",                                # upstream-owned; present only once published
    "pyproject.toml", "requirements.txt", "requirements-dev.txt", "run.sh", "run.ps1", "swi3s-studio.spec",
    "swi3s_studio/", "native/", "tests/", "tools/", "data/", "visualizer_examples/",
    "docs/",                                 # docs/internal/ is carved out below
)

# Files the UPSTREAM project owns, declared in tools/publish_tree.py and grafted in at
# publish time. They must be ABSENT here and PRESENT once published — see the test below.
def _upstream_owned() -> tuple[str, ...]:
    """Read the list from the publish tool, imported by path, so there is one definition."""
    import importlib.util
    path = os.path.join(_ROOT, "tools", "publish_tree.py")
    spec = importlib.util.spec_from_file_location("publish_tree", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod._UPSTREAM_OWNED


_UPSTREAM_OWNED = _upstream_owned()


# Files that must NAME the tier boundary to do their job, excluded from the reference check:
# this test, the tool that prunes the tier, and the leak scanner (which exempts the internal
# tier from published-only patterns).
_SELF = {"tests/test_publish_tiers.py", "tools/publish_tree.py", "tools/leak_scan.py"}

# The three patterns .gitignore uses for the compiled core, so the walk in _files() agrees
# with the git listing about what is a build product rather than a tracked path.
_BUILD_PRODUCT = re.compile(r"^swi3score.*\.(so|pyd|dylib)$")


def _files() -> list[str]:
    """Tracked paths in a source tree; a filesystem walk in a tree without .git (a published
    tree)."""
    out = subprocess.run(["git", "ls-files"], cwd=_ROOT, capture_output=True, text=True)
    if out.returncode == 0 and out.stdout.strip():
        return [p for p in out.stdout.splitlines() if p]
    skip = {".git", "__pycache__", ".venv", "build", "dist", ".mypy_cache", ".ruff_cache",
            ".pytest_cache", "scratch", "node_modules", "dev_docs"}
    found = []
    for base, dirs, names in os.walk(_ROOT):
        dirs[:] = [d for d in dirs if d not in skip]
        # Skip what git never lists: macOS AppleDouble sidecars, and the built native core
        # (matched by the .gitignore patterns, not the bare suffix, so a committed binary
        # still has to be classified).
        names = [n for n in names
                 if not n.startswith('._') and not _BUILD_PRODUCT.match(n)]
        # POSIX separators: every rule below matches a "docs/…" style prefix.
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


@functools.lru_cache(maxsize=None)
def _load(name: str, rel: str):
    import importlib.util
    spec = importlib.util.spec_from_file_location(name, os.path.join(_ROOT, rel))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _decoded(rel: str) -> str:
    """A file's text as tools/leak_scan.py reads it: UTF-8, UTF-16, or a binary's printable
    runs."""
    with open(os.path.join(_ROOT, rel), "rb") as f:
        return _load("_tiers_leak_scan", "tools/leak_scan.py").decode_bytes(f.read())[0]


def test_nothing_published_references_something_internal():
    """No public file cites docs/internal/... or a maintainers'-tier file, by path or by a
    link relative to the citing file (publish_tree.cites_internal): the link would be dead
    once the tree is published. Every file is read, whatever its extension; a symlink is
    checked by its target."""
    pt = _load("_tiers_publish_tree", "tools/publish_tree.py")
    offenders = []
    for rel in _files():
        if rel.startswith(_INTERNAL) or rel in _SELF or rel in _INTERNAL_FILES:
            continue
        full = os.path.join(_ROOT, rel)
        if os.path.islink(full):
            target = os.readlink(full)
            if pt.cites_internal(rel, target) or pt._into_internal(rel, target):
                offenders.append(f"{rel} -> {target}")
            continue
        if not os.path.isfile(full):
            continue
        for i, line in enumerate(_decoded(rel).splitlines(), 1):
            if pt.cites_internal(rel, line) or "internal/" + "TECH_DEBT" in line:
                offenders.append(f"{rel}:{i}: {line.strip()[:90]}")
    assert not offenders, (
        "published files reference docs/internal/, which will not exist in the published "
        "tree:\n  " + "\n  ".join(offenders)
        + "\n\nEither describe the content inline, or move the doc back out of "
          "docs/internal/ if a public reader genuinely needs it.")


def test_every_tracked_path_is_classified():
    """Default-deny: an unclassified path fails rather than silently shipping."""
    unclassified = [p for p in _files() if p not in _INTERNAL_FILES
                    and not any(p == r or p.startswith(r) for r in _PUBLIC_ROOTS)]
    assert not unclassified, (
        "tracked path(s) belong to no declared tier:\n  " + "\n  ".join(unclassified)
        + "\n\nAdd to _PUBLIC_ROOTS to publish it, or put it under docs/internal/ to keep it "
          "in-house. Do not leave it unclassified — that is how a tree bloats by default.")


def test_the_split_is_intact():
    """Asserts in BOTH contexts rather than skipping in one.

    Source tree: the internal tier is populated and the user-facing docs are public.
    Published tree: the internal tier is GONE and the user-facing docs are still there."""
    files = _files()
    internal = [p for p in files if p.startswith(_INTERNAL)]
    public_docs = [p for p in files
                   if p.startswith("docs/") and not p.startswith(_INTERNAL)]
    if _is_published_tree():
        assert not internal, f"published tree still carries the internal tier: {internal}"
        assert not [f for f in _INTERNAL_FILES if f in files], \
            f"published tree still carries {_INTERNAL_FILES}"
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
    """The governance paths whose content the upstream project decides are declared in
    `_UPSTREAM_OWNED`, named independently here so a path deleted from the tool still fails.

    GOVERNANCE.md, CONTRIBUTING.md, LICENSE.md and the CLA files are not in the list: they
    are identical in both trees.
    """
    for path in ("CODEOWNERS", ".mipi/project.yml",
                 ".github/workflows/config-check.yml"):
        assert path in _UPSTREAM_OWNED, (
            f"{path} is owned by the upstream project but is not declared in "
            f"tools/publish_tree.py::_UPSTREAM_OWNED. Publishing would either "
            f"overwrite their copy with ours or drop it entirely.")


def test_upstream_owned_files_are_absent_here_and_present_once_published():
    """Upstream-owned files are absent from this tree and present in a published one.

    Publishing REPLACES the tree on a repository with its own history; the publish tool
    grafts these files in from upstream, and a copy carried here would overwrite theirs.
    Asserted in both contexts."""
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
    """Every upstream-owned path appears as a quoted string in tools/publish_tree.py."""
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
    """No tracked path is a file-sync conflict copy. The tier rules above cannot catch one:
    each duplicate sits INSIDE an already-classified prefix."""
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


def test_the_tier_boundary_has_one_definition():
    """`publish_tree` prunes the internal tier and `leak_scan` exempts it — from the same path.

    Both name it (they are in _SELF for that reason); this test keeps the two strings, and
    this file's, equal.
    """
    import importlib.util

    def load(name, rel):
        spec = importlib.util.spec_from_file_location(name, os.path.join(_ROOT, rel))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    pruner = load("_pt", "tools/publish_tree.py")
    scanner = load("_ls", "tools/leak_scan.py")
    assert scanner._INTERNAL_TIER == pruner._INTERNAL == _INTERNAL, (
        f"tier boundary disagrees: leak_scan={scanner._INTERNAL_TIER!r} "
        f"publish_tree={pruner._INTERNAL!r} test={_INTERNAL!r}")


def test_the_publish_tool_prunes_the_same_internal_files():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "publish_tree", os.path.join(_ROOT, "tools", "publish_tree.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert tuple(mod._INTERNAL_FILES) == _INTERNAL_FILES


def test_the_publish_reference_check_reads_every_file_type(tmp_path):
    """publish_tree's check 2 reads a build spec, a requirements or CMake text file, JSON,
    CSV, SVG and .gitignore as well as source and docs; the files that define the boundary
    are exempt."""
    pt = _load("_tiers_publish_tree", "tools/publish_tree.py")
    assert pt._BOUNDARY_FILES == _SELF
    citing = {"swi3s-studio.spec": "datas=[('docs/internal/x.md', '.')]\n",
              "native/CMakeLists.txt": "# see CLAUDE.md\n",
              "data/fixture.json": '{"doc": "docs/internal/notes.md"}\n',
              "data/table.csv": "a,docs/internal/t.md\n",
              "docs/figure.svg": "<svg><!-- docs/internal/fig.md --></svg>\n",
              ".gitignore": "# CLAUDE.md is local\n",
              "data/utf16.txt": "docs/internal/u.md\n"}
    clean = {"README.md": "A bus analyzer.\n",
             "tools/publish_tree.py": "_INTERNAL = 'docs/internal/'\n"}
    for rel, text in {**citing, **clean}.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if rel == "data/utf16.txt":
            path.write_bytes(text.encode("utf-16"))
        else:
            path.write_text(text, encoding="utf-8")
    assert pt.check_dangling(str(tmp_path)) == sorted(citing)


def test_a_relative_link_into_the_internal_tier_is_dangling(tmp_path):
    """A link resolved from the citing file's directory, in Markdown, a Markdown reference
    definition, HTML, or a symlink target, is dangling when it lands in docs/internal/; a
    link elsewhere, a URL and an anchor are not."""
    pt = _load("_tiers_publish_tree", "tools/publish_tree.py")
    citing = {"docs/DEVELOPMENT.md": "see [the perf notes](internal/PERFORMANCE.md)\n",
              "docs/USER_GUIDE.md": "see [x](./internal/x.md#part)\n",
              "docs/sub/page.md": "[ref]: ../internal/y.md\n",
              "docs/page.html": '<a href="internal/z.html">z</a>\n',
              "tools/notes.md": "[d](../docs/internal/)\n"}
    clean = {"README.md": "[guide](docs/USER_GUIDE.md) "
                          "[web](https://example.invalid/internal/x)\n",
             "docs/TESTING.md": "[a](#internal) [b](../tools/internal/x.md) internal/ prose\n"}
    for rel, text in {**citing, **clean}.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    os.symlink("internal/TECH_DEBT.md", tmp_path / "docs" / "debt.md")
    os.symlink("../docs/USER_GUIDE.md", tmp_path / "tools" / "guide.md")
    assert pt.check_dangling(str(tmp_path)) == sorted([*citing, "docs/debt.md"])
