#!/usr/bin/env python3
"""Build and VERIFY the tree that gets published.

    python3 tools/publish_tree.py              # from the working tree
    python3 tools/publish_tree.py <sha>        # from a specific commit
    python3 tools/publish_tree.py --keep       # leave the built tree on disk to inspect
    python3 tools/publish_tree.py --upstream=<ref>   # default public/main
    python3 tools/publish_tree.py <sha> --message-out=<f>   # write the branch commit message
    python3 tools/publish_tree.py <sha> --preview           # an unfinished cycle, for preview

The published tree is this repository MINUS the internal tier (docs/internal/), PLUS the
handful of files the upstream project owns rather than us (see _UPSTREAM_OWNED). Both halves
are derivable relationships, not a curated copy: re-running this reproduces the tree exactly,
so the release tag can state what it is rather than asking anyone to trust a hand-pruned
snapshot.

The upstream half exists because publishing is a tree REPLACEMENT onto a repository that has
its own history. Files added there by its maintainers are invisible to us, so a naive
replacement deletes them — which is exactly what nearly shipped: an upstream pull request had
added a project-identity file and a config-check workflow, and reverting those would have
undone a merged decision without saying so.

Pruning is the easy half. The point of this tool is the checks AFTER assembling, because every
one of them corresponds to a way a published tree has been or could be broken:

  1. nothing from the internal tier survived                (the prune actually happened)
  2. no surviving file REFERENCES the internal tier         (no dangling links — 18 such
                                                             references existed before the
                                                             split, 6 in shipping code)
  3. every upstream-owned file is present, and none of      (a tree replacement cannot
     them was shadowed by a copy of ours                     silently revert upstream)
  4. the tree carries docs/releases/v<its own version>.md,  (a description cannot claim work
     and the branch message is DERIVED from it               the tag does not contain)
  5. the leak scan passes on the ASSEMBLED tree             (what ships is what is scanned)
  6. the test suite passes on the ASSEMBLED tree            (nothing load-bearing was pruned)

Check 6 is the one that cannot be replaced by reading a manifest: docs are safe to drop,
but a fixture or module is not, and only running the suite proves which is which.

Check 4 is the newest and the odd one out — it is not about a broken tree but about a
description that disagrees with one. v3.0.17 was described three separate times by hand (a
signed tag annotation, a corp release body, this branch's commit message) with nothing tying
any of them to the tree, and one announced a memory-guard fix that landed AFTER the tag. One
reviewed file in the tree, referenced by every consumer, cannot drift from itself.
"""
from __future__ import annotations

import glob
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_INTERNAL = "docs/internal/"

# Files the UPSTREAM repository owns. We must not carry our own copy of these (ours would
# clobber theirs on publish) and must not drop them (that reverts their maintainers' work),
# so they are taken from the upstream ref at assembly time. Keep this list SHORT: it is for
# files that are meaningless or actively broken here — a workflow calling an org-level
# reusable workflow errors on every push in a repository that cannot resolve it.
_UPSTREAM_OWNED = (
    ".mipi/project.yml",
    ".github/workflows/config-check.yml",
    # CODEOWNERS names PEOPLE, so it changes upstream without anything here
    # moving, and a stale copy silently un-names a maintainer. It did: this tree
    # carried "@NielWarren" while upstream had corrected it to "@nielwarren582",
    # and the v3.0.13 preview would have reverted that — a one-token diff nobody
    # reviews closely, in the file that decides who must approve a pull request.
    #
    # It is the clearest case for the rule: who maintains that repository is
    # THEIR decision, not a value this tree should hold an opinion about.
    "CODEOWNERS",
)


def _build(rev: str | None, dest: str) -> list[str]:
    """Export the tree at `rev` (or the working tree's HEAD content) into dest, minus the
    internal tier. Uses git archive so only TRACKED files land — an untracked local file
    cannot leak into a published tree by accident."""
    src = rev or "HEAD"
    tar = subprocess.run(["git", "archive", src], cwd=_ROOT, capture_output=True, check=True)
    subprocess.run(["tar", "-x", "-C", dest], input=tar.stdout, check=True)
    pruned = []
    internal_dir = os.path.join(dest, _INTERNAL.rstrip("/"))
    if os.path.isdir(internal_dir):
        for name in sorted(os.listdir(internal_dir)):
            pruned.append(_INTERNAL + name)
        shutil.rmtree(internal_dir)
    return pruned


def _graft_upstream(ref: str, dest: str) -> tuple[list[str], list[str]]:
    """Copy the upstream-owned files in from `ref`. Returns (grafted, problems)."""
    if subprocess.run(["git", "rev-parse", "--verify", "--quiet", ref],
                      cwd=_ROOT, capture_output=True).returncode != 0:
        return [], [f"upstream ref {ref!r} does not resolve — run `git fetch` for the "
                    f"publish remote. A tree built without it would DELETE "
                    f"{len(_UPSTREAM_OWNED)} file(s) the upstream project owns."]
    grafted, problems = [], []
    for rel in _UPSTREAM_OWNED:
        # Ours shadowing theirs is a defect wherever it comes from, so say so rather than
        # silently overwriting: the corp tree is not supposed to carry these at all.
        if os.path.exists(os.path.join(dest, rel)):
            problems.append(f"this tree carries {rel}, which upstream owns — publishing it "
                            f"would overwrite theirs")
        blob = subprocess.run(["git", "show", f"{ref}:{rel}"],
                              cwd=_ROOT, capture_output=True)
        if blob.returncode != 0:
            problems.append(f"{rel} is declared upstream-owned but absent from {ref}")
            continue
        full = os.path.join(dest, rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "wb") as f:
            f.write(blob.stdout)
        grafted.append(rel)
    return grafted, problems


_RELEASES = "docs/releases/"


def tree_version(dest: str) -> str | None:
    """`__version__` as the ASSEMBLED tree declares it — read from the file, not from this
    checkout, because the tree being published is usually not the one you are standing in."""
    try:
        with open(os.path.join(dest, "swi3s_studio", "__init__.py"), encoding="utf-8") as f:
            for line in f:
                if line.startswith("__version__"):
                    return line.split("=", 1)[1].strip().strip('"\'')
    except OSError:
        return None
    return None


def notes_path_for(version: str) -> str:
    return f"{_RELEASES}v{version}.md"


def check_release_notes(dest: str, preview: bool = False) -> tuple[str | None, list[str]]:
    """(notes text, problems). The published tree must carry the notes for its OWN version.

    This is check 4 and it is about a different failure than the other five: not a broken tree,
    but a description that disagrees with it. v3.0.17 was described three times by hand — a tag
    annotation, a corp release body and this branch's commit message — and one of them announced
    a fix that landed AFTER the tag, because nothing tied any of them to the tree. A file in the
    tree cannot make that mistake without the tag containing it, and it is reviewable in the
    release diff like any other change.
    """
    problems: list[str] = []
    version = tree_version(dest)
    if not version:
        return None, ["cannot read __version__ from the assembled tree"]
    rel = notes_path_for(version)
    full = os.path.join(dest, rel)
    if not os.path.isfile(full):
        return None, [f"the assembled tree declares {version} but carries no {rel} — "
                      f"a release description must come from a reviewed file in the tree, "
                      f"not be written at push time (see docs/DEVELOPMENT.md)"]
    with open(full, encoding="utf-8") as f:
        text = f.read()
    if version not in text.splitlines()[0]:
        problems.append(f"{rel} does not name {version} in its title line: "
                        f"{text.splitlines()[0]!r}")
    if "(in development)" in text and not preview:
        problems.append(f"{rel} is still marked '(in development)' — finalise it before "
                        f"publishing, the same way the changelog header is finalised. If this is "
                        f"a PREVIEW of an unfinished cycle, pass --preview, which says so in the "
                        f"branch message instead of pretending the notes are done")
    return text, problems


def branch_message(notes: str, version: str, pruned: list[str], grafted: list[str],
                   upstream: str, preview: bool = False) -> str:
    """The publish branch's commit message: a DERIVED preamble plus the notes verbatim.

    Everything the preamble states, this tool knows for a fact — what was pruned, what was
    grafted and from where. Nothing in it is retyped from the release, so the message cannot
    drift from the notes the release body also uses.
    """
    title, _, body = notes.partition("\n")
    # A PREVIEW says so first, in its own paragraph, because the one thing a reader must not
    # conclude from a branch that looks exactly like a release branch is that it is one.
    lead = "" if not preview else (textwrap.fill(
        f"PREVIEW of {version}, NOT A RELEASE. This cycle is unfinished: the notes below are the "
        f"work-in-progress entry, the version is not tagged, and nothing here has been through "
        f"the multi-platform release gate. Published for evaluation only — expect the final "
        f"{version} to differ.", width=95) + "\n\n")
    n_pruned = f"{len(pruned)} file" + ("" if len(pruned) == 1 else "s")
    n_grafted = f"{len(grafted)} file" + ("" if len(grafted) == 1 else "s")
    # A preview has no tag, so naming one as the thing this tree is not identical to would be
    # nonsense — the honest comparison is the source commit it was assembled from.
    reference = "source commit it was built from" if preview else "release tag"
    paragraphs = [
        f"This branch replaces the tree wholesale. It is the release tree minus the "
        f"maintainers' internal documentation tier ({n_pruned}) and plus the {n_grafted} this "
        f"repository owns, taken from `{upstream}` at assembly time so that a tree replacement "
        f"cannot revert work done here. Both relationships are derivable: the same tool "
        f"rebuilds this tree from one recorded source commit and re-runs the leak scan and the "
        f"full suite on the result. It is therefore not byte-identical to the {reference}.",
        f"What follows is `{notes_path_for(version)}` from this tree, unmodified — the same "
        f"file the release body uses, so the two cannot disagree.",
    ]
    # Wrapped to the width the notes themselves use. An unwrapped paragraph in a commit
    # message renders as one very long line in every git UI.
    preamble = "\n\n".join(textwrap.fill(p, width=95) for p in paragraphs)
    subject = title.lstrip("# ").strip()
    if preview:
        subject = f"{subject} — PREVIEW, not a release"
    return f"{subject}\n\n{lead}{preamble}\n{body.rstrip()}\n"


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    rev = args[0] if args else None
    keep = "--keep" in sys.argv
    message_out = next((a.split("=", 1)[1] for a in sys.argv[1:]
                        if a.startswith("--message-out=")), None)
    preview = "--preview" in sys.argv
    upstream = next((a.split("=", 1)[1] for a in sys.argv[1:]
                     if a.startswith("--upstream=")), "public/main")
    dest = tempfile.mkdtemp(prefix="swi3s-publish-")
    failures: list[str] = []

    print(f"building published tree from {rev or 'HEAD'} -> {dest}")
    pruned = _build(rev, dest)
    print(f"  pruned {len(pruned)} internal file(s)")

    # 1. the prune happened
    if os.path.isdir(os.path.join(dest, _INTERNAL.rstrip("/"))):
        failures.append("internal tier still present after pruning")
    if not pruned:
        failures.append("nothing was pruned — is docs/internal/ populated?")

    # 2. no dangling references to the pruned tier. The files that ENFORCE the boundary
    # necessarily contain the path, exactly as the leak scanner and the reference-model guard
    # do — self-exclusion, not an allowlist for real violations.
    #
    # tools/leak_scan.py joined this set in 3.0.18, when it gained a `_INTERNAL_TIER` prefix so
    # that published-only site patterns can be skipped inside the maintainers' tier. That is not
    # a LINK to a document — the thing this check exists to catch — it is the tier boundary
    # itself, held as a string because that is the only way to compare a path against it.
    # tests/test_publish_tiers.py asserts the scanner and the pruner agree on that boundary, so
    # the two definitions cannot drift apart while both are exempt here.
    _SELF = {"tests/test_publish_tiers.py", "tools/publish_tree.py", "tools/leak_scan.py"}
    dangling = []
    for base, _dirs, names in os.walk(dest):
        for n in names:
            full = os.path.join(base, n)
            rel = os.path.relpath(full, dest).replace(os.sep, "/")
            if rel in _SELF:
                continue
            if not n.endswith((".md", ".py", ".cpp", ".h", ".toml", ".sh", ".ps1", ".yml")):
                continue
            try:
                with open(full, encoding="utf-8", errors="replace") as f:
                    text = f.read()
            except OSError:
                continue
            if _INTERNAL in text:
                dangling.append(rel)
    if dangling:
        failures.append(f"dangling references to {_INTERNAL} in: {', '.join(sorted(dangling))}")

    # 3. the upstream-owned layer, grafted BEFORE the scan and the suite so both see the
    # tree that actually ships.
    grafted, problems = _graft_upstream(upstream, dest)
    print(f"  grafted {len(grafted)} upstream-owned file(s) from {upstream}")
    failures += problems

    # 4. the tree carries the release notes for its OWN version, and the branch message is
    # DERIVED from them rather than written at push time.
    notes, note_problems = check_release_notes(dest, preview=preview)
    failures += note_problems
    version = tree_version(dest)
    if notes and version:
        kind = "PREVIEW" if preview else "release"
        print(f"  {kind} notes: {notes_path_for(version)} ({len(notes.splitlines())} lines)")
        if preview and "(in development)" in notes:
            print("  note: the notes are still marked '(in development)' — allowed for a "
                  "preview, and the branch message says so")
        if message_out:
            with open(message_out, "w", encoding="utf-8") as f:
                f.write(branch_message(notes, version, pruned, grafted, upstream,
                                       preview=preview))
            print(f"  branch message written to {message_out}")
    elif message_out:
        failures.append(f"--message-out={message_out} was asked for but the notes are missing")

    # 5. leak scan on what actually ships
    print("  leak scan on the assembled tree ...")
    scan = subprocess.run([sys.executable, os.path.join("tools", "leak_scan.py")],
                          cwd=dest, capture_output=True, text=True,
                          env=dict(os.environ))
    if scan.returncode != 0:
        failures.append("leak scan failed on the assembled tree")
        print(scan.stdout[-2000:])

    # 6. the suite passes on what actually ships — the check a manifest cannot give you
    print("  test suite on the assembled tree (this is the slow one) ...")
    env = dict(os.environ, PYTHONPATH=".", QT_QPA_PLATFORM="offscreen",
               PYQTGRAPH_QT_LIB="PySide6", PYTHONUTF8="1")

    # The assembled tree has NO built native core — the .so is gitignored, so
    # `git archive` cannot carry it. Python then resolves `import swi3score`
    # from wherever else it can, which in practice is a months-old copy in
    # site-packages: the suite runs green or red against a binary that has
    # nothing to do with this tree. That is the "a stale binary has faked a
    # green result" failure mode gate.py exists to prevent, reappearing in the
    # one check gate.py does not cover.
    #
    # Copy the core the caller just built and ASSERT it is the right one, the
    # same way gate.py does, rather than letting the import silently wander.
    #
    # Copied OUTSIDE the assembled tree and put on PYTHONPATH. It is a build
    # artefact of this check, not a file being published, and ANYWHERE inside
    # `dest` — even a dot-directory — is walked by
    # test_every_tracked_path_is_classified, which correctly fails an
    # unclassified path. Keeping it out of the tree is what that test wants and
    # what the published tree should contain.
    corelib = tempfile.mkdtemp(prefix="swi3s-native-core-")
    core = sorted(glob.glob(os.path.join(_ROOT, "swi3score*.so"))
                  + glob.glob(os.path.join(_ROOT, "swi3score*.pyd")))
    if not core:
        failures.append("no built native core beside this checkout — run "
                        "`bash native/build_local.sh` first, or the suite below "
                        "silently tests whatever swi3score site-packages holds")
    else:
        for c in core:
            shutil.copy2(c, corelib)
        env["PYTHONPATH"] = os.pathsep.join([".", corelib])
        probe = subprocess.run(
            [sys.executable, "-c",
             "import swi3score, swi3s_studio.session as s;"
             "print(swi3score.__file__, swi3score.score_abi, s._REQUIRED_SCORE_ABI)"],
            cwd=dest, env=env, capture_output=True, text=True)
        if probe.returncode != 0:
            failures.append(f"native core will not import on the assembled tree: "
                            f"{probe.stderr.strip().splitlines()[-1:] or ['?']}")
        else:
            path, abi, want = probe.stdout.split()
            # Must be the copy just staged — NOT a stray site-packages build.
            if not os.path.realpath(path).startswith(os.path.realpath(corelib)):
                failures.append(f"the suite would import swi3score from {path}, not the "
                                f"core built beside this checkout — it would not be "
                                f"testing this tree")
            elif abi != want:
                failures.append(f"score_abi {abi} != session.py's required {want} — "
                                f"the native core is stale, rebuild it")
            else:
                print(f"    native core score_abi={abi} (asserted, freshly built)")

    suite = subprocess.run([sys.executable, "-m", "pytest", "-q", "--no-header",
                            "-m", "not perf"], cwd=dest, env=env,
                           capture_output=True, text=True)
    tail = [ln for ln in suite.stdout.strip().splitlines() if ln][-1:] or ["(no output)"]
    print(f"    {tail[0]}")
    if suite.returncode != 0:
        failures.append("test suite FAILED on the assembled tree — something load-bearing "
                        "was pruned, or a test reads a pruned file")

    print("\n=== publish-tree summary ===")
    if failures:
        print(f"  FAIL ({len(failures)}):")
        for f in failures:
            print(f"    - {f}")
    else:
        print("  PASS — the assembled tree is complete, self-contained, and clean")
    if keep:
        print(f"\n  tree kept at {dest}")
    else:
        shutil.rmtree(dest, ignore_errors=True)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
