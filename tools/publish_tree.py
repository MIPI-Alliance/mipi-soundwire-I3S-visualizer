#!/usr/bin/env python3
"""Build and VERIFY the tree that gets published.

    python3 tools/publish_tree.py              # from HEAD: uncommitted changes are NOT included
    python3 tools/publish_tree.py <sha>        # from a specific commit
    python3 tools/publish_tree.py --keep       # leave the built tree on disk to inspect
    python3 tools/publish_tree.py --upstream=<ref>   # default public/main
    python3 tools/publish_tree.py <sha> --message-out=<f>   # write the branch commit message
    python3 tools/publish_tree.py <sha> --preview           # an unfinished cycle, for preview
    python3 tools/publish_tree.py <sha> --approve-names=<digest>   # a person read the new names
    python3 tools/publish_tree.py --record-sync      # record the upstream commit taken in
    python3 tools/publish_tree.py <sha> --check-published=<ref>   # check 7 alone: <ref> is the tree

The published tree is this repository MINUS the internal tier (docs/internal/), PLUS the
handful of files the upstream project owns rather than us (see _UPSTREAM_OWNED). Both halves
are derivable relationships, not a curated copy: re-running this reproduces the tree exactly,
so the release tag can state what it is rather than asking anyone to trust a hand-pruned
snapshot.

The upstream half exists because publishing is a tree REPLACEMENT onto a repository that has
its own history: files added there by its maintainers are not in this tree, and a naive
replacement deletes them.

The checks run AFTER assembling, each against one way a published tree can be broken:

  1. nothing from the internal tier survived                (the prune actually happened)
  2. no file of this tree's own (the grafted upstream-      (no dangling links)
     owned files excepted) REFERENCES the internal tier, by
     its path or by a relative link or symlink into it; a
     file or directory that cannot be read fails the check
  3. every upstream-owned file is present, and none of      (a tree replacement cannot
     them was shadowed by a copy of ours                     silently revert upstream)
 3b. the release tree has taken in everything the upstream  (a tree replacement cannot
     branch holds (_SYNC_FILE), and undoes no contributed    silently revert what was
     commit between two recorded points, in whole or in      merged upstream)
     part
  4. the tree carries docs/releases/v<its own version>.md,  (a description cannot claim work
     and no docs/releases/next.md; the branch message is     the tag does not contain)
     DERIVED from it. A --preview describes itself from
     next.md when the tree has one
  5. this checkout's leak scan passes on the ASSEMBLED      (what ships is what is scanned)
     tree, with the site patterns of $SWI3S_LEAK_PATTERNS
 5b. every NAME new since the published tree is approved    (the denylist's blind spot: a
     by a person (tools/name_review.py)                      name nobody listed)
  6. the test suite passes on the ASSEMBLED tree, against   (nothing load-bearing was pruned)
     a native core compiled from that tree's own native/

--check-published=<ref> runs one check on its own, against the branch as pushed: it builds
the tree (prune and graft), runs check 7 and none of the others, and exits 0 only when it
passes. It takes a commit, --upstream and --keep, and refuses the options of the other checks;
--record-sync takes --upstream only.

  7. every file of <ref>'s tree is the assembled tree's,     (what was pushed is what the
     byte for byte and mode for mode, and neither has a       tool built, not a hand edit)
     file the other lacks

A change the assembled tree needs is a commit on the release branch, published from that
commit; the assembled tree is never edited.

Check 6 is the one that cannot be replaced by reading a manifest: docs are safe to drop,
but a fixture or module is not, and only running the suite proves which is which.

Check 4 is not about a broken tree but about a description that disagrees with one. One
reviewed file in the tree, referenced by every consumer, cannot drift from itself.
"""
from __future__ import annotations

import glob
import hashlib
import importlib.util
import os
import posixpath
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import textwrap

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_WINDOWS = os.name == "nt"
_INTERNAL = "docs/internal/"
# Maintainers'-tier files that live outside docs/internal/ because a tool expects them where
# they are: CLAUDE.md is the coding agent's instructions for this checkout, and it names
# places outside the tree. Pruned with the tier; nothing published may refer to them.
_INTERNAL_FILES = ("CLAUDE.md",)

# Files the UPSTREAM repository owns. We must not carry our own copy of these (ours would
# clobber theirs on publish) and must not drop them (that reverts their maintainers' work),
# so they are taken from the upstream ref at assembly time. Keep this list SHORT: it is for
# files that are meaningless or actively broken here — a workflow calling an org-level
# reusable workflow errors on every push in a repository that cannot resolve it.
_UPSTREAM_OWNED = (
    ".mipi/project.yml",
    ".github/workflows/config-check.yml",
    # CODEOWNERS names PEOPLE, so it changes upstream without anything here
    # moving, and a stale copy would silently un-name a maintainer. Who maintains
    # that repository is decided upstream.
    "CODEOWNERS",
)


def _build(rev: str | None, dest: str, git_dir: str | None = None) -> list[str]:
    """Export the tree at `rev` (or the working tree's HEAD content) into dest, minus the
    internal tier. Only TRACKED files land — an untracked local file cannot leak into a
    published tree by accident. Each file is written from its blob, so it holds exactly the
    bytes git stores for it whatever the checkout's core.autocrlf and core.eol and whatever
    .gitattributes says (eol, text, ident, filter, export-subst). A file is executable when
    git records 100755 and a symlink is a symlink; a submodule entry is not exported."""
    cwd = git_dir or _ROOT
    listing = subprocess.run(["git", "ls-tree", "-r", "-z", "--full-tree", rev or "HEAD"],
                             cwd=cwd, capture_output=True, check=True).stdout
    entries = []
    for entry in listing.split(b"\0"):
        if not entry:
            continue
        meta, path = entry.split(b"\t", 1)
        mode, kind, blob = meta.decode().split()
        if kind == "blob":
            entries.append((mode, blob, path.decode("utf-8")))
    reader = subprocess.Popen(["git", "cat-file", "--batch"], cwd=cwd, stdin=subprocess.PIPE,
                              stdout=subprocess.PIPE)
    assert reader.stdin is not None and reader.stdout is not None
    try:
        for mode, blob, rel in entries:
            reader.stdin.write(blob.encode() + b"\n")
            reader.stdin.flush()
            header = reader.stdout.readline().split()
            if len(header) != 3:
                raise SystemExit(f"publish_tree: {rel}: git cat-file did not return blob "
                                 f"{blob} ({b' '.join(header).decode() or 'no answer'})")
            data = reader.stdout.read(int(header[2]))
            reader.stdout.read(1)                      # the newline after the content
            full = os.path.join(dest, *rel.split("/"))
            os.makedirs(os.path.dirname(full), exist_ok=True)
            if mode == "120000":
                os.symlink(data.decode("utf-8"), full)
                continue
            with open(full, "wb") as f:
                f.write(data)
            if mode == "100755":
                os.chmod(full, os.stat(full).st_mode | stat.S_IXUSR | stat.S_IXGRP
                         | stat.S_IXOTH)
    finally:
        reader.stdin.close()
        reader.stdout.close()
        reader.wait()
    if reader.returncode != 0:
        raise SystemExit(f"publish_tree: git cat-file exited {reader.returncode} exporting "
                         f"{rev or 'HEAD'}")
    pruned = []
    internal_dir = os.path.join(dest, _INTERNAL.rstrip("/"))
    if os.path.isdir(internal_dir):
        for name in sorted(os.listdir(internal_dir)):
            pruned.append(_INTERNAL + name)
        shutil.rmtree(internal_dir)
    for rel in _INTERNAL_FILES:
        full = os.path.join(dest, rel)
        if os.path.isfile(full):
            os.remove(full)
            pruned.append(rel)
    return pruned


def _graft_upstream(ref: str, dest: str,
                    git_dir: str | None = None) -> tuple[list[str], list[str]]:
    """Copy the upstream-owned files in from `ref`, each executable when `ref` records it
    as 100755. Returns (grafted, problems)."""
    git_dir = git_dir or _ROOT
    if subprocess.run(["git", "rev-parse", "--verify", "--quiet", ref],
                      cwd=git_dir, capture_output=True).returncode != 0:
        return [], [f"upstream ref {ref!r} does not resolve — run `git fetch` for the "
                    f"publish remote. A tree built without it would DELETE "
                    f"{len(_UPSTREAM_OWNED)} file(s) the upstream project owns."]
    modes = _ls_tree(ref, git_dir, list(_UPSTREAM_OWNED)) or {}
    grafted, problems = [], []
    for rel in _UPSTREAM_OWNED:
        # Ours shadowing theirs is a defect wherever it comes from, so say so rather than
        # silently overwriting: the development tree is not supposed to carry these at all.
        if os.path.exists(os.path.join(dest, rel)):
            problems.append(f"this tree carries {rel}, which upstream owns — publishing it "
                            f"would overwrite theirs")
        blob = subprocess.run(["git", "show", f"{ref}:{rel}"],
                              cwd=git_dir, capture_output=True)
        if blob.returncode != 0:
            problems.append(f"{rel} is declared upstream-owned but absent from {ref}")
            continue
        full = os.path.join(dest, rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "wb") as f:
            f.write(blob.stdout)
        if modes.get(rel, ("",))[0] == "100755":
            os.chmod(full, os.stat(full).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        grafted.append(rel)
    return grafted, problems


# The upstream commits the release tree has taken in, oldest first, one full sha per line
# ("#" starts a comment). Published with the tree, so anyone can audit it; read from the SOURCE
# commit. The first line is a publish commit (the baseline); `--record-sync` appends the
# upstream branch's current commit once its changes are in the release tree, and refuses
# while any of them is not.
_SYNC_FILE = "tools/publish_sync.txt"
# Every publish commit's message carries this sentence (branch_message). A publish commit is
# this tree's own change, so check 3b does not hold the release tree to it.
_PUBLISH_MARK = "This branch replaces the tree wholesale."
# A line shorter than this, once its whitespace is collapsed, is too common to show whether
# a change was kept or undone.
_MIN_LINE = 20


def _git(args: list[str], git_dir: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=git_dir, capture_output=True)


def read_sync_points(rev: str | None, git_dir: str = _ROOT) -> list[str]:
    """The recorded sync points of `_SYNC_FILE` at `rev` (HEAD when None), oldest first."""
    shown = _git(["show", f"{rev or 'HEAD'}:{_SYNC_FILE}"], git_dir)
    if shown.returncode != 0:
        return []
    return [ln.split()[0] for ln in shown.stdout.decode("utf-8", "replace").splitlines()
            if ln.strip() and not ln.lstrip().startswith("#")]


def _blob(ref: str, path: str, git_dir: str) -> bytes | None:
    shown = _git(["show", f"{ref}:{path}"], git_dir)
    return _lf(shown.stdout) if shown.returncode == 0 else None


def _lf(data: bytes) -> bytes:
    """Content with CRLF line ends as LF, so a checkout's line-end conversion is not a change."""
    return data.replace(b"\r\n", b"\n")


def _flat(text: str) -> str:
    """Text with every run of whitespace as one space, so re-wrapping is not a change."""
    return " ".join(text.split())


def _is_publish(sha: str, git_dir: str) -> bool:
    body = _git(["log", "-1", "--format=%B", sha], git_dir).stdout.decode("utf-8", "replace")
    return _PUBLISH_MARK in body


def _contributions(base: str, head: str, git_dir: str) -> list[str]:
    """The non-merge commits in base..head, oldest first, that are not publish commits."""
    shas = _git(["rev-list", "--reverse", "--no-merges", f"{base}..{head}"],
                git_dir).stdout.decode().split()
    return [s for s in shas if not _is_publish(s, git_dir)]


_HUNK_HEADER = re.compile(r"^@@ -\d+(?:,(\d+))? \+\d+(?:,(\d+))? @@")
# The C escapes git writes in a quoted path, and the byte each stands for.
_C_ESCAPES = {"a": 7, "b": 8, "t": 9, "n": 10, "v": 11, "f": 12, "r": 13, '"': 34, "\\": 92}


def _header_path(field: str) -> str | None:
    """The path a `--- ` or `+++ ` diff header names, without its a/ or b/ prefix: None for
    /dev/null. Git C-quotes a path holding a double quote, a backslash or a control
    character (prefix included, octal escapes for bytes), and ends a path holding a space with
    a tab."""
    field = field.rstrip("\t")
    if field == "/dev/null":
        return None
    if not (len(field) > 1 and field[0] == '"' and field.endswith('"')):
        return field[2:]
    raw = bytearray()
    body, i = field[1:-1], 0
    while i < len(body):
        ch = body[i]
        if ch == "\\" and body[i + 1:i + 4].isdigit() and len(body[i + 1:i + 4]) == 3:
            raw.append(int(body[i + 1:i + 4], 8))
            i += 4
        elif ch == "\\" and body[i + 1:i + 2] in _C_ESCAPES:
            raw.append(_C_ESCAPES[body[i + 1]])
            i += 2
        else:
            raw += ch.encode("utf-8")
            i += 1
    return raw.decode("utf-8", "replace")[2:]


def _hunks(sha: str, git_dir: str) -> dict[str, list[tuple[list[str], list[str]]]]:
    """{path: [(removed lines, added lines), ...]} of one commit against its first parent;
    a file the commit deletes is listed under its old path with every line removed.

    The diff is requested in one fixed format, whatever the user's git configuration says
    about prefixes, colour, quoting, external diff drivers or text conversion, and a quoted
    header path is read unquoted (`_header_path`). A hunk's body is read by the line counts
    its header gives, so a removed line that starts with `-- ` or an added one that starts
    with `++ ` is content, not a file header."""
    out = _git(["-c", "color.ui=never", "-c", "color.diff=never", "-c", "diff.noprefix=false",
                "-c", "diff.mnemonicPrefix=false", "-c", "core.quotePath=false",
                "show", "--format=", "--no-color", "--no-ext-diff", "--no-textconv",
                "--no-renames", "--src-prefix=a/", "--dst-prefix=b/", "-U0", "--first-parent",
                sha], git_dir).stdout.decode("utf-8", "replace")
    hunks: dict[str, list[tuple[list[str], list[str]]]] = {}
    path = None
    current: tuple[list[str], list[str]] = ([], [])
    old_left = new_left = 0
    for line in out.split("\n"):
        if old_left or new_left:
            if line.startswith("-") and old_left:
                old_left -= 1
                current[0].append(line[1:].rstrip("\r"))
                continue
            if line.startswith("+") and new_left:
                new_left -= 1
                current[1].append(line[1:].rstrip("\r"))
                continue
            if line.startswith("\\"):                  # "\ No newline at end of file"
                continue
            old_left = new_left = 0
        if line.startswith("+++ "):
            path = _header_path(line[4:]) or path      # a deleted file keeps its `--- ` name
        elif line.startswith("--- "):
            if line[4:] != "/dev/null":
                path = _header_path(line[4:])
        elif path is not None and (m := _HUNK_HEADER.match(line)):
            current = ([], [])
            hunks.setdefault(path, []).append(current)
            old_left = int(m.group(1)) if m.group(1) is not None else 1
            new_left = int(m.group(2)) if m.group(2) is not None else 1
    return hunks


def _undone(sha: str, root: str, git_dir: str) -> list[str]:
    """What the tree at `root` undoes of commit `sha`: a file the commit added or changed that
    is absent, a file the commit deleted that is present with any of its deleted text, or a
    change whose removed text is back while its added text is not (a line it
    added that is missing, for a change that only adds; a line it removed that the tree holds
    more often than the commit's own file does, for a change that only removes). Text is
    compared with whitespace collapsed and line ends as LF; a later edit of the commit's text,
    which brings back neither side, is kept. The upstream-owned files are grafted, so they are
    not checked."""
    found = []
    subject = _git(["log", "-1", "--format=%h %s", sha], git_dir).stdout.decode().strip()
    for path, changes in _hunks(sha, git_dir).items():
        if path in _UPSTREAM_OWNED:
            continue
        full = os.path.join(root, path)
        after = _blob(sha, path, git_dir)
        if not os.path.isfile(full):
            if after is not None:
                found.append(f"{path} is absent from the release tree, undoing {subject}")
            continue
        with open(full, "rb") as f:
            have = _flat(_lf(f.read()).decode("utf-8", "replace"))
        if after is None:                              # the commit deleted the file
            gone = [_flat(x) for removed, _ in changes for x in removed
                    if len(_flat(x)) >= _MIN_LINE]
            if not gone or any(g in have for g in gone):
                found.append(f"{path} is back in the release tree, undoing {subject}")
            continue
        kept_by_commit = _flat(after.decode("utf-8", "replace"))
        for removed, added in changes:
            gone = [_flat(x) for x in removed if len(_flat(x)) >= _MIN_LINE]
            new = [_flat(x) for x in added if len(_flat(x)) >= _MIN_LINE]
            joined = _flat(" ".join(added))
            gone = [g for g in gone if g not in joined]   # text the change kept is not removed
            if new:
                back = [g for g in gone if g in have]
            else:                                      # a pure deletion
                back = [g for g in gone if have.count(g) > kept_by_commit.count(g)]
            kept = [a for a in new if a in have]
            if (back and (not new or len(kept) < len(new))) or (new and not gone and not kept):
                shown = (back or new)[0]
                found.append(f"{path} undoes {subject}: "
                             f"{'its old text is back' if back else 'its added text is gone'}"
                             f" ({shown[:70]!r})")
                break
    return found


def check_upstream_sync(dest: str, upstream: str, points: list[str],
                        git_dir: str = _ROOT) -> list[str]:
    """Check 3b: the tree replacement keeps what was merged upstream.

    Refuses when the upstream branch has commits after the last recorded sync point (the
    release tree has not taken them in), when a recorded point is not a commit on the upstream
    branch, or when the assembled tree undoes any contributed commit between two recorded
    points (see `_undone`). Publish commits are this tree's own and are not checked."""
    resolved = _git(["rev-parse", "--verify", "--quiet", f"{upstream}^{{commit}}"], git_dir)
    if resolved.returncode != 0:
        return [f"upstream ref {upstream!r} does not resolve — run `git fetch` for the publish "
                f"remote"]
    head = resolved.stdout.decode().strip()
    if not points:
        return [f"{_SYNC_FILE} records no {upstream} commit the release tree has taken in: "
                f"carry in what {upstream} holds, run `python3 tools/publish_tree.py "
                f"--record-sync` and commit the file"]
    full = []
    for pt in points:
        got = _git(["rev-parse", "--verify", "--quiet", f"{pt}^{{commit}}"], git_dir)
        if got.returncode != 0:
            return [f"{_SYNC_FILE}'s sync point {pt[:12]} is not a commit here — run "
                    f"`git fetch` for the publish remote"]
        sha = got.stdout.decode().strip()
        if _git(["merge-base", "--is-ancestor", sha, head], git_dir).returncode != 0:
            return [f"{_SYNC_FILE}'s sync point {sha[:12]} is not on {upstream}: its "
                    f"history was rewritten, so what the release tree took in cannot be told"]
        full.append(sha)
    problems = []
    ahead = _git(["log", "--reverse", "--format=%h %s", f"{full[-1]}..{head}"],
                 git_dir).stdout.decode("utf-8", "replace").splitlines()
    if ahead:
        problems.append(
            f"{upstream} has {len(ahead)} commit(s) the release tree has not taken in:\n      "
            + "\n      ".join(ahead)
            + f"\n    carry their changes into the release tree, then run `python3 "
              f"tools/publish_tree.py --record-sync` and commit {_SYNC_FILE}")
    for base, tip in zip(full, full[1:]):
        for sha in _contributions(base, tip, git_dir):
            problems += _undone(sha, dest, git_dir)
    return problems


def record_sync(upstream: str, git_dir: str = _ROOT) -> str:
    """Append the upstream branch's current commit to the working tree's `_SYNC_FILE`. With no
    point recorded it must be a publish commit (the baseline); after that, the last point must
    be a commit on the upstream branch and every contributed commit since it must be in the
    working tree (`_undone`), or nothing is recorded. Returns what was done."""
    resolved = _git(["rev-parse", "--verify", "--quiet", f"{upstream}^{{commit}}"], git_dir)
    if resolved.returncode != 0:
        raise SystemExit(f"upstream ref {upstream!r} does not resolve — run `git fetch` first")
    head = resolved.stdout.decode().strip()
    path = os.path.join(git_dir, _SYNC_FILE)
    held = ""
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as f:
            held = f.read()
    lines = held.splitlines()
    points = [ln.split()[0] for ln in lines if ln.strip() and not ln.lstrip().startswith("#")]
    if points and points[-1] == head:
        return f"{_SYNC_FILE} already records {upstream} at {head[:12]}"
    if not points and not _is_publish(head, git_dir):
        raise SystemExit(f"{_SYNC_FILE} records nothing yet, and {upstream} at {head[:12]} is "
                         f"not a publish commit: the first point must be one")
    if points:
        last = _git(["rev-parse", "--verify", "--quiet", f"{points[-1]}^{{commit}}"], git_dir)
        if last.returncode != 0:
            raise SystemExit(f"{_SYNC_FILE}'s last sync point {points[-1][:12]} is not a commit "
                             f"here — run `git fetch` for the publish remote")
        if _git(["merge-base", "--is-ancestor", last.stdout.decode().strip(), head],
                git_dir).returncode != 0:
            raise SystemExit(f"not recorded — {_SYNC_FILE}'s last sync point "
                             f"{points[-1][:12]} is not on {upstream}: its history was "
                             f"rewritten, so what the release tree took in cannot be told")
        missing = []
        for sha in _contributions(points[-1], head, git_dir):
            missing += _undone(sha, git_dir, git_dir)
        if missing:
            raise SystemExit("not recorded — the working tree does not have these yet:\n  "
                             + "\n  ".join(missing))
    subject = _git(["log", "-1", "--format=%cs %s", head], git_dir).stdout.decode().strip()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        if held and not held.endswith("\n"):          # a point starts a line of its own
            f.write("\n")
        f.write(f"{head}  {subject}\n")
    return f"recorded {upstream} at {head[:12]} in {_SYNC_FILE}; commit it with the change"


_RELEASES = "docs/releases/"
# The next release's notes, collected during the cycle and renamed to v<version>.md by the
# release commit (docs/DEVELOPMENT.md, release checklist).
_NEXT_NOTES = f"{_RELEASES}next.md"


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


def release_notes_rel(dest: str, preview: bool = False) -> str | None:
    """The notes file a build describes itself from: docs/releases/next.md for a preview of a
    tree that has one (the version there is still the last release's), else the notes of the
    tree's own version."""
    if preview and os.path.isfile(os.path.join(dest, _NEXT_NOTES)):
        return _NEXT_NOTES
    version = tree_version(dest)
    return notes_path_for(version) if version else None


def check_release_notes(dest: str, preview: bool = False) -> tuple[str | None, list[str]]:
    """(notes text, problems). The published tree must carry the notes for its OWN version.

    This is check 4: not a broken tree, but a description that disagrees with it. A file in
    the tree cannot describe work the tag does not contain, and it is reviewable in the
    release diff like any other change.

    A release tree carries no docs/releases/next.md: the release commit renames it to the
    version's notes. A preview describes itself from next.md when the tree has one.
    """
    problems: list[str] = []
    version = tree_version(dest)
    if not version:
        return None, ["cannot read __version__ from the assembled tree"]
    if release_notes_rel(dest, preview) == _NEXT_NOTES:
        with open(os.path.join(dest, _NEXT_NOTES), encoding="utf-8") as f:
            return f.read(), []
    if os.path.isfile(os.path.join(dest, _NEXT_NOTES)):
        problems.append(f"the assembled tree carries {_NEXT_NOTES}, the notes of a release not "
                        f"yet made: the release commit renames it to "
                        f"{notes_path_for(version)}. If this is a PREVIEW of an unfinished "
                        f"cycle, pass --preview")
    rel = notes_path_for(version)
    full = os.path.join(dest, rel)
    if not os.path.isfile(full):
        return None, [f"the assembled tree declares {version} but carries no {rel} — "
                      f"a release description must come from a reviewed file in the tree, "
                      f"not be written at push time (see docs/DEVELOPMENT.md)"]
    with open(full, encoding="utf-8") as f:
        text = f.read()
    title = (text.splitlines() or [""])[0]           # an empty file has an empty title
    if version not in title:
        problems.append(f"{rel} does not name {version} in its title line: {title!r}")
    if "(in development)" in text and not preview:
        problems.append(f"{rel} is still marked '(in development)' — finalise it before "
                        f"publishing. If this is "
                        f"a PREVIEW of an unfinished cycle, pass --preview, which says so in the "
                        f"branch message instead of pretending the notes are done")
    return text, problems


def branch_message(notes: str, version: str, pruned: list[str], grafted: list[str],
                   upstream: str, preview: bool = False, notes_rel: str | None = None) -> str:
    """The publish branch's commit message: a DERIVED preamble plus the notes verbatim.

    Everything the preamble states, this tool knows for a fact — what was pruned, what was
    grafted and from where. Nothing in it is retyped from the release, so the message cannot
    drift from the notes the release body also uses.
    """
    title, _, body = notes.partition("\n")
    # A PREVIEW says so first, in its own paragraph, because the one thing a reader must not
    # conclude from a branch that looks exactly like a release branch is that it is one.
    # From next.md the release has no number yet; the tree's version is the last release's.
    label = f"the release after {version}" if notes_rel == _NEXT_NOTES else version
    lead = "" if not preview else (textwrap.fill(
        f"PREVIEW of {label}, NOT A RELEASE. This cycle is unfinished: the notes below are the "
        f"work-in-progress entry, the version is not tagged, and nothing here has been through "
        f"the multi-platform release gate. Published for evaluation only — expect the final "
        f"release to differ.", width=95) + "\n\n")
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
        f"What follows is `{notes_rel or notes_path_for(version)}` from this tree, unmodified "
        f"— the same "
        f"file the release body uses, so the two cannot disagree.",
    ]
    # Wrapped to the width the notes themselves use. An unwrapped paragraph in a commit
    # message renders as one very long line in every git UI.
    preamble = "\n\n".join(textwrap.fill(p, width=95) for p in paragraphs)
    subject = title.lstrip("# ").strip()
    if preview:
        subject = f"{subject} — PREVIEW, not a release"
    return f"{subject}\n\n{lead}{preamble}\n{body.rstrip()}\n"


# The files that ENFORCE the tier boundary necessarily contain its path: self-exclusion, not
# an allowlist for real violations. tools/leak_scan.py holds it as `_INTERNAL_TIER`, to skip
# published-only site patterns inside the maintainers' tier, which is not a LINK to a
# document; tests/test_publish_tiers.py asserts the scanner and the pruner agree on it.
_BOUNDARY_FILES = {"tests/test_publish_tiers.py", "tools/publish_tree.py", "tools/leak_scan.py"}


def _entries(dest: str, unreadable: list[str] | None = None):
    """(relative POSIX path, full path) of every entry under `dest` that is not a directory:
    each file, and each symlink, including one to a directory, which is not descended into.
    A directory that cannot be listed is appended to `unreadable` (as `dir/`) when given."""
    def note(err: OSError) -> None:
        if unreadable is not None:
            unreadable.append(os.path.relpath(str(err.filename), dest).replace(os.sep, "/") + "/")

    for base, dirs, names in os.walk(dest, onerror=note):
        links = [d for d in dirs if os.path.islink(os.path.join(base, d))]
        for n in sorted(names + links):
            full = os.path.join(base, n)
            yield os.path.relpath(full, dest).replace(os.sep, "/"), full


# Link targets: a Markdown inline link `](target)`, a Markdown reference definition
# `[label]: target`, and an HTML href= or src= attribute.
_LINK = re.compile(r"\]\(\s*<?([^)\s>]+)"
                   r"|^\s{0,3}\[[^\]]+\]:\s*<?([^\s>]+)"
                   r"|\b(?:href|src)\s*=\s*[\"']([^\"']+)", re.M)


def _into_internal(rel: str, target: str) -> bool:
    """True when `target`, cited from the file at `rel`, resolves into the internal tier or
    to an internal file. A URL, a mail link and an in-page anchor resolve to neither; a
    target starting with / is taken from the tree root."""
    target = target.split("#", 1)[0].split("?", 1)[0].replace("\\", "/")
    if not target or re.match(r"^[A-Za-z][\w+.-]*:", target):
        return False
    if target.startswith("/"):
        path = posixpath.normpath(target.lstrip("/"))
    else:
        path = posixpath.normpath(posixpath.join(posixpath.dirname(rel), target))
    return (path + "/").startswith(_INTERNAL) or path in _INTERNAL_FILES


def cites_internal(rel: str, text: str) -> bool:
    """True when `text`, the content of the file at `rel`, names the internal tier or an
    internal file, or links to one by a path relative to `rel`."""
    if _INTERNAL in text or any(f in text for f in _INTERNAL_FILES):
        return True
    return any(_into_internal(rel, next(g for g in m.groups() if g))
               for m in _LINK.finditer(text))


def _decoder():
    """tools/leak_scan.py's decode_bytes, loaded by path (tools/ is not a package)."""
    spec = importlib.util.spec_from_file_location(
        "_publish_tree_leak_scan", os.path.join(_ROOT, "tools", "leak_scan.py"))
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.decode_bytes


def check_dangling(dest: str, unreadable: list[str] | None = None) -> list[str]:
    """Check 2: the entries under `dest` that cite the internal tier or an internal file
    (see cites_internal), sorted. Every file is read, whatever its extension, decoded as
    tools/leak_scan.py decodes it (a binary by its printable runs). A symlink is checked by
    its target path, resolved from the link's directory. _BOUNDARY_FILES are exempt. A file
    or directory that cannot be read has not been checked: it is appended to `unreadable`
    when given."""
    decode = _decoder()
    dangling = []
    for rel, full in _entries(dest, unreadable):
        if rel in _BOUNDARY_FILES:
            continue
        if os.path.islink(full):
            target = os.readlink(full)
            if cites_internal(rel, target) or _into_internal(rel, target):
                dangling.append(rel)
            continue
        try:
            with open(full, "rb") as f:
                text = decode(f.read())[0]
        except OSError:
            if unreadable is not None:
                unreadable.append(rel)
            continue
        if cites_internal(rel, text):
            dangling.append(rel)
    return sorted(dangling)


_SCAN_COUNTS = re.compile(r"\((\d+) structural, (\d+) site-specific\)")
_SCAN_FILES = re.compile(r"^leak scan — [^\n]*?: (\d+) files", re.M)


def _file_count(dest: str) -> int:
    """Entries the assembled tree ships: files and symlinks, as _entries lists them."""
    return sum(1 for _ in _entries(dest))


def check_leak_scan(dest: str) -> list[str]:
    """Check 5: this checkout's tools/leak_scan.py on the assembled tree (`--tree=dest`),
    WITH site-specific patterns, so a tree from an older commit is scanned with the current
    patterns, and with this checkout's absolute path among them. Returns the problems. The
    scan's header lines are always printed.

    The structural patterns alone cover addresses, home paths and keys; the host, repository
    and third-party names a publish must not carry are only in $SWI3S_LEAK_PATTERNS, so a
    scan that ran none of them is a failure here, not a pass. So is a scan that read fewer
    files than the assembled tree holds: every file in it ships."""
    scan = subprocess.run([sys.executable, os.path.join(_ROOT, "tools", "leak_scan.py"),
                           f"--tree={dest}"],
                          cwd=_ROOT, capture_output=True, encoding="utf-8", errors="replace",
                          env=dict(os.environ, PYTHONUTF8="1"))
    lines = scan.stdout.splitlines()
    header = lines[:next((i for i, ln in enumerate(lines) if not ln.strip()), len(lines))]
    print(textwrap.indent("\n".join(header), "    "))
    problems = []
    if scan.returncode != 0:
        problems.append("leak scan failed on the assembled tree")
        print(scan.stdout[-2000:])
        if scan.stderr.strip():
            print(scan.stderr.strip()[-500:])
    counts = _SCAN_COUNTS.search(scan.stdout)
    if counts is None:
        problems.append("the leak scan's header names no pattern counts, so what it covered "
                        "is unknown")
    elif int(counts.group(2)) == 0:
        problems.append("the leak scan ran no site-specific patterns: set "
                        "$SWI3S_LEAK_PATTERNS to the pattern file, or host, repository and "
                        "third-party names in the tree are not checked")
    scanned, present = _SCAN_FILES.search(scan.stdout), _file_count(dest)
    if scanned is None:
        problems.append("the leak scan's header names no file count, so what it read is "
                        "unknown")
    elif int(scanned.group(1)) != present:
        problems.append(f"the leak scan read {scanned.group(1)} of the {present} files in the "
                        f"assembled tree; every file in it ships")
    return problems


def uncommitted_changes() -> list[str]:
    """`git status --porcelain` lines for tracked files that differ from HEAD."""
    out = subprocess.run(["git", "status", "--porcelain", "--untracked-files=no"],
                         cwd=_ROOT, capture_output=True, encoding="utf-8", errors="replace",
                         env=dict(os.environ, PYTHONUTF8="1"))
    return [ln for ln in out.stdout.splitlines() if ln.strip()]


def _load_build_overrides() -> tuple[str, ...]:
    """BUILD_OVERRIDES from tools/build_overrides.py, loaded by path (tools/ is not a package)."""
    spec = importlib.util.spec_from_file_location(
        "_build_overrides", os.path.join(_ROOT, "tools", "build_overrides.py"))
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.BUILD_OVERRIDES


# Variables of native/build_local.sh that move its output or replace its optimisation flags.
# The publish build removes them, so the core lands beside its sources, built as released.
_BUILD_OVERRIDES = _load_build_overrides()


def build_core(dest: str, work: str) -> tuple[str | None, str | None]:
    """(directory holding swi3score compiled from `dest`'s own native/, problem or None).

    The sources are copied to `work`/src and built there, so nothing is written into the
    assembled tree: native/build_local.sh writes the module beside its native/ directory,
    and the scikit-build-core build directory is relative to native/. POSIX runs
    build_local.sh with $PYTHON set to this interpreter and `_BUILD_OVERRIDES` removed from
    its environment; Windows pip-installs the copy into `work`/lib. The module lands in
    `work`/lib either way."""
    src = os.path.join(dest, "native")
    if not os.path.isdir(src):
        return None, "the assembled tree has no native/ to build the core from"
    copy = os.path.join(work, "src", "native")
    lib = os.path.join(work, "lib")
    shutil.copytree(src, copy, symlinks=True)
    os.makedirs(lib, exist_ok=True)
    env = {k: v for k, v in os.environ.items() if k not in _BUILD_OVERRIDES}
    env.update(PYTHON=sys.executable, PYTHONUTF8="1")
    if _WINDOWS:
        cmd = [sys.executable, "-m", "pip", "install", "--no-deps", "--target", lib, copy]
    else:
        cmd = ["bash", os.path.join(copy, "build_local.sh")]
    build = subprocess.run(cmd, cwd=work, env=env, capture_output=True, encoding="utf-8",
                           errors="replace")
    if build.returncode != 0:
        tail = (build.stderr or build.stdout).strip().splitlines()[-3:]
        return None, (f"the native core did not build from the assembled tree's native/ "
                      f"(exit {build.returncode}): {' | '.join(tail) or '?'}")
    for built in (glob.glob(os.path.join(work, "src", "swi3score*.so"))
                  + glob.glob(os.path.join(work, "src", "swi3score*.pyd"))):
        shutil.move(built, lib)
    if not (glob.glob(os.path.join(lib, "swi3score*.so"))
            or glob.glob(os.path.join(lib, "swi3score*.pyd"))):
        return None, "the native core build reported success but produced no swi3score module"
    return lib, None


def _object_format(git_dir: str | None = None) -> str:
    """The hash the repository at `git_dir` names its objects by: sha1 or sha256."""
    shown = subprocess.run(["git", "rev-parse", "--show-object-format"], cwd=git_dir or _ROOT,
                           capture_output=True, encoding="utf-8",
                           errors="replace").stdout.strip()
    return shown if shown in ("sha1", "sha256") else "sha1"


def _blob_id(data: bytes, algorithm: str = "sha1") -> str:
    """The id git gives a blob holding `data` in a repository of object format `algorithm`."""
    return hashlib.new(algorithm, b"blob %d\0" % len(data) + data).hexdigest()


def _ls_tree(ref: str, git_dir: str | None = None,
             paths: list[str] | None = None) -> dict[str, tuple[str, str]] | None:
    """{path: (mode, blob id)} of every entry of `ref`'s tree, or of `paths` only when
    given, each path from the repository's root whatever the directory git runs in; None
    when `ref` does not resolve."""
    listing = subprocess.run(["git", "ls-tree", "-r", "-z", "--full-tree", ref, "--",
                              *(paths or [])], cwd=git_dir or _ROOT, capture_output=True)
    if listing.returncode != 0:
        return None
    out: dict[str, tuple[str, str]] = {}
    for entry in listing.stdout.split(b"\0"):
        if not entry:
            continue
        meta, path = entry.split(b"\t", 1)
        mode, _kind, blob = meta.decode().split()
        out[path.decode("utf-8")] = (mode, blob)
    return out


def _file_mode(full: str) -> str:
    """The mode git records for the entry at `full`: 120000 for a symlink, 100755 for a file
    its owner can execute, 100644 otherwise."""
    if os.path.islink(full):
        return "120000"
    return "100755" if os.stat(full).st_mode & stat.S_IXUSR else "100644"


def check_published(dest: str, ref: str, git_dir: str | None = None,
                    modes: dict[str, str] | None = None) -> list[str]:
    """Check 7: `ref`'s tree, the branch as pushed, is the assembled tree at `dest`. Returns
    one problem per path missing from either side, holding different bytes (a symlink is
    compared by its target, as git stores it), or carrying a different mode. `modes` gives
    the git mode of each assembled path it names; any other path takes its mode from the
    file (_file_mode). An assembled file or directory that cannot be read is a problem of
    its own, and the paths under it are not reported missing."""
    pushed = _ls_tree(ref, git_dir)
    if pushed is None:
        return [f"{ref!r} does not resolve — fetch the publish remote first"]
    algorithm = _object_format(git_dir)
    built: dict[str, tuple[str, str]] = {}
    unreadable: list[str] = []
    for rel, full in _entries(dest, unreadable):
        try:
            if os.path.islink(full):
                data = os.readlink(full).encode("utf-8")
            else:
                with open(full, "rb") as f:
                    data = f.read()
            mode = (modes or {}).get(rel) or _file_mode(full)
        except OSError:
            unreadable.append(rel)
            continue
        built[rel] = (mode, _blob_id(data, algorithm))

    def unread(rel: str) -> bool:
        return any(rel == u or (u.endswith("/") and rel.startswith(u)) for u in unreadable)

    both = sorted(pushed.keys() & built.keys())
    problems = [f"{rel}: could not be read in the assembled tree" for rel in sorted(unreadable)]
    problems += [f"{rel}: in {ref} but not in the assembled tree"
                 for rel in sorted(pushed.keys() - built.keys()) if not unread(rel)]
    problems += [f"{rel}: in the assembled tree but not in {ref}"
                 for rel in sorted(built.keys() - pushed.keys())]
    problems += [f"{rel}: {ref} holds different bytes from the assembled tree"
                 for rel in both if pushed[rel][1] != built[rel][1]]
    problems += [f"{rel}: {ref} has mode {pushed[rel][0]}, the assembled tree "
                 f"{built[rel][0]}" for rel in both if pushed[rel][0] != built[rel][0]]
    return problems


def assembled_modes(rev: str | None, upstream: str, grafted: list[str],
                    git_dir: str | None = None) -> dict[str, str]:
    """{path: git mode} of the tree _assemble builds: each path as `rev` records it, and
    each grafted path as `upstream` records it."""
    modes = {rel: mode for rel, (mode, _blob) in (_ls_tree(rev or "HEAD", git_dir)
                                                   or {}).items()}
    if grafted:
        modes.update({rel: mode for rel, (mode, _blob)
                      in (_ls_tree(upstream, git_dir, grafted) or {}).items()})
    return modes


# Options that take a value, written --name=<value>. One given bare is refused, so a value
# written after a space is not read as the commit to build.
_VALUE_OPTIONS = ("--upstream", "--message-out", "--approve-names", "--check-published")
# Options that take no value. An option in neither tuple is refused.
_FLAGS = ("--keep", "--preview", "--record-sync")
# At most this many differences of check 7 are listed; the rest are counted.
_MAX_LISTED = 20


def _usage_problems(argv: list[str]) -> list[str]:
    """What is wrong with the command line `argv` (without the program name)."""
    problems = []
    for a in argv:
        if not a.startswith("-"):
            continue
        name, eq, value = a.partition("=")
        if name in _VALUE_OPTIONS:
            if not (eq and value):
                problems.append(f"{name} takes a value: write {name}=<value>")
        elif name in _FLAGS:
            if eq:
                problems.append(f"{name} takes no value")
        else:
            problems.append(f"unknown option {name}: the options are "
                            f"{', '.join(_FLAGS + _VALUE_OPTIONS)}")
    positional = [a for a in argv if not a.startswith("-")]
    if len(positional) > 1:
        problems.append(f"one commit to build, at most; got {' '.join(positional)}")
    given = {a.partition("=")[0] for a in argv if a.startswith("-")}
    if "--check-published" in given:
        unread = sorted(given & {"--message-out", "--approve-names", "--preview", "--record-sync"})
        if unread:
            problems.append(f"--check-published runs check 7 alone, which reads no "
                            f"{', '.join(unread)}")
    elif "--record-sync" in given:
        unread = sorted(given - {"--record-sync", "--upstream"}) + positional
        if unread:
            problems.append(f"--record-sync reads --upstream only, not {' '.join(unread)}")
    return problems


def _assemble(rev: str | None, upstream: str, dest: str) -> tuple[list[str], list[str],
                                                                 list[str]]:
    """Build the published tree from `rev` into `dest`: export and prune (_build), then
    graft the upstream-owned files from `upstream`. Prints each step. Returns (pruned,
    grafted, the graft's problems)."""
    print(f"building published tree from {rev or 'HEAD'} -> {dest}")
    if rev is None and uncommitted_changes():
        print("  WARNING: the working tree has uncommitted changes to tracked files; they are "
              "NOT in this tree, which is built from HEAD")
    pruned = _build(rev, dest)
    print(f"  pruned {len(pruned)} internal file(s)")
    grafted, problems = _graft_upstream(upstream, dest)
    print(f"  grafted {len(grafted)} upstream-owned file(s) from {upstream}")
    return pruned, grafted, problems


def run_check_published(rev: str | None, ref: str, upstream: str, keep: bool = False) -> int:
    """Check 7 alone: build the tree from `rev` (_assemble), compare it with `ref`, modes as
    git records them (assembled_modes), print the result, and return 0 when they are
    identical, 1 otherwise. A graft that fails is a failure, since the tree it compares is
    then incomplete. `keep` leaves the built tree on disk."""
    dest = tempfile.mkdtemp(prefix="swi3s-publish-")
    try:
        _pruned, grafted, failures = _assemble(rev, upstream, dest)
        diffs = check_published(dest, ref,
                                modes=assembled_modes(rev, upstream, grafted))
        print(f"  {ref} against the assembled tree: "
              f"{'identical' if not diffs else f'{len(diffs)} difference(s)'}")
        failures += [f"{ref} is not the assembled tree: {d}" for d in diffs[:_MAX_LISTED]]
        if len(diffs) > _MAX_LISTED:
            failures.append(f"... and {len(diffs) - _MAX_LISTED} more difference(s)")
    finally:
        if keep:
            print(f"  tree kept at {dest}")
        else:
            shutil.rmtree(dest, ignore_errors=True)
    print("\n=== check-published summary ===")
    if failures:
        print(f"  FAIL ({len(failures)}):")
        for f in failures:
            print(f"    - {f}")
        return 1
    print(f"  PASS — {ref} is the assembled tree")
    return 0


def _tolerant_output() -> None:
    """Make stdout and stderr write a character their encoding cannot hold as a '?' rather
    than raise: the tool echoes test and tool output, and a Windows console or redirected log
    is often cp1252."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(errors="replace")


def _terminate_as_interrupt() -> None:
    """SIGTERM and SIGHUP end the run as a KeyboardInterrupt does, so the process groups it
    started are killed and its working directories removed: each child is in a session of its
    own and would otherwise outlive a killed or hung-up parent."""
    def _raise(signum, frame):
        raise KeyboardInterrupt
    for name in ("SIGTERM", "SIGHUP"):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), _raise)


def main() -> int:
    usage = _usage_problems(sys.argv[1:])
    if usage:
        for line in usage:
            print(f"publish_tree: {line}", file=sys.stderr)
        return 2
    _terminate_as_interrupt()
    _tolerant_output()
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    rev = args[0] if args else None
    if rev is not None and subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}"],
            cwd=_ROOT, capture_output=True).returncode != 0:
        print(f"publish_tree: {rev!r} is not a commit in this repository", file=sys.stderr)
        return 2
    keep = "--keep" in sys.argv
    message_out = next((a.split("=", 1)[1] for a in sys.argv[1:]
                        if a.startswith("--message-out=")), None)
    preview = "--preview" in sys.argv
    upstream = next((a.split("=", 1)[1] for a in sys.argv[1:]
                     if a.startswith("--upstream=")), "public/main")
    if "--record-sync" in sys.argv:
        print(record_sync(upstream))
        return 0
    published = next((a.split("=", 1)[1] for a in sys.argv[1:]
                      if a.startswith("--check-published=")), None)
    if published:
        return run_check_published(rev, published, upstream, keep=keep)
    # Both working directories are removed whatever ends the checks (`dest` is kept with
    # --keep), an exception or a SystemExit from a helper included.
    dest = tempfile.mkdtemp(prefix="swi3s-publish-")
    work = tempfile.mkdtemp(prefix="swi3s-native-core-")
    try:
        failures = _check_assembled(rev, upstream, dest, work, preview, message_out)
        print("\n=== publish-tree summary ===")
        if failures:
            print(f"  FAIL ({len(failures)}):")
            for failure in failures:
                print(f"    - {failure}")
        else:
            print("  PASS — the assembled tree is complete, self-contained, and clean")
    finally:
        shutil.rmtree(work, ignore_errors=True)
        if keep:
            print(f"\n  tree kept at {dest}")
        else:
            shutil.rmtree(dest, ignore_errors=True)
    return 1 if failures else 0


# Seconds the suite on the assembled tree may take before it is a named failure.
_SUITE_TIMEOUT = 7200


# Variables that make a child colour its output; the suite's FAILED lines are matched as text.
_COLOUR_FORCING = ("FORCE_COLOR", "MYPY_FORCE_COLOR", "CLICOLOR_FORCE")


# Seconds the output of a killed suite is waited for before it is given up.
_KILL_GRACE = 30


def _kill_tree(p: subprocess.Popen) -> None:
    """Kill `p` and every process it started: its process group on POSIX, its tree on
    Windows."""
    if _WINDOWS:
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(p.pid)], capture_output=True)
    else:
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    try:
        p.kill()
    except OSError:
        pass


def run_assembled_suite(dest: str, env: dict[str, str],
                        timeout: float = _SUITE_TIMEOUT) -> list[str]:
    """Run the test suite in `dest` with `env`; the failures, empty when it passes. The summary
    line is printed. A failing run prints each FAILED and ERROR line pytest reports (or the
    end of its output when it reports none), so the tests and the file they need are named;
    a run past `timeout` seconds is a failure."""
    cmd = [sys.executable, "-m", "pytest", "-q", "--no-header", "--color=no", "-rfE",
           "-m", "not perf"]
    suite = subprocess.Popen(cmd, cwd=dest, env=env, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, encoding="utf-8", errors="replace",
                             start_new_session=not _WINDOWS,
                             creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                             if _WINDOWS else 0)
    try:
        out, err = suite.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_tree(suite)
        try:
            suite.communicate(timeout=_KILL_GRACE)
        except subprocess.TimeoutExpired:
            pass                                # a process outside the group holds the pipes
        return [f"test suite did not finish within {timeout:g} s on the assembled tree"]
    except BaseException:
        _kill_tree(suite)
        raise
    suite = subprocess.CompletedProcess(cmd, suite.returncode, out, err)
    lines = [ln for ln in suite.stdout.strip().splitlines() if ln]
    print(f"    {lines[-1] if lines else '(no output)'}")
    if suite.returncode == 0:
        return []
    named = [ln for ln in lines if ln.startswith(("FAILED ", "ERROR "))]
    shown = named or (lines + suite.stderr.strip().splitlines())[-40:]
    print(textwrap.indent("\n".join(shown), "    "))
    return ["test suite FAILED on the assembled tree — something load-bearing was pruned, "
            "or a test reads a pruned file"]


def _check_assembled(rev: str | None, upstream: str, dest: str, work: str, preview: bool,
                     message_out: str | None) -> list[str]:
    """Assemble the tree from `rev` into `dest` and run checks 1 to 6 on it, compiling the
    native core in `work`; the failures, in order."""
    failures: list[str] = []

    # The tree that ships: grafted BEFORE the scan and the suite so both see it.
    pruned, grafted, problems = _assemble(rev, upstream, dest)

    # 1. the prune happened
    if os.path.isdir(os.path.join(dest, _INTERNAL.rstrip("/"))):
        failures.append("internal tier still present after pruning")
    for rel in _INTERNAL_FILES:
        if os.path.exists(os.path.join(dest, rel)):
            failures.append(f"{rel} still present after pruning")
    if not pruned:
        failures.append("nothing was pruned — is docs/internal/ populated?")

    # 2. no dangling references to the pruned tier, in this tree's own files
    unreadable: list[str] = []
    dangling = [rel for rel in check_dangling(dest, unreadable) if rel not in grafted]
    if unreadable:
        failures.append(f"could not read, so not checked for references to {_INTERNAL}: "
                        f"{', '.join(sorted(unreadable))}")
    if dangling:
        failures.append(f"dangling references to {_INTERNAL} or {', '.join(_INTERNAL_FILES)} in: "
                        f"{', '.join(dangling)}")

    # 3. the upstream-owned layer is complete and unshadowed
    failures += problems

    # 3b. everything merged upstream is in the release tree, and nothing it changed reverted
    points = read_sync_points(rev)
    sync_problems = check_upstream_sync(dest, upstream, points)
    print(f"  upstream sync: {len(points)} recorded point(s); "
          f"{'OK' if not sync_problems else f'{len(sync_problems)} problem(s)'}")
    failures += sync_problems

    # 4. the tree carries the release notes for its OWN version, and the branch message is
    # DERIVED from them rather than written at push time.
    notes, note_problems = check_release_notes(dest, preview=preview)
    failures += note_problems
    version = tree_version(dest)
    notes_rel = release_notes_rel(dest, preview)
    if notes and version:
        kind = "PREVIEW" if preview else "release"
        print(f"  {kind} notes: {notes_rel} ({len(notes.splitlines())} lines)")
        if preview and "(in development)" in notes:
            print("  note: the notes are still marked '(in development)' — allowed for a "
                  "preview, and the branch message says so")
        if message_out:
            with open(message_out, "w", encoding="utf-8") as f:
                f.write(branch_message(notes, version, pruned, grafted, upstream,
                                       preview=preview, notes_rel=notes_rel))
            print(f"  branch message written to {message_out}")
    elif message_out:
        failures.append(f"--message-out={message_out} was asked for but the notes are missing")

    # 5. leak scan on what actually ships
    print("  leak scan on the assembled tree ...")
    failures += check_leak_scan(dest)

    # 5b. names new since the published tree, read and approved by a person. The leak scan
    # can only refuse what someone listed; this lists what is new, and the digest it prints
    # approves exactly that list. An agent preparing a publish asks the maintainer for it.
    approve = next((a.split("=", 1)[1] for a in sys.argv[1:]
                    if a.startswith("--approve-names=")), None)
    print(f"  name review against {upstream} ...")
    review = subprocess.run(
        [sys.executable, os.path.join(_ROOT, "tools", "name_review.py"), f"--tree={dest}",
         f"--baseline={upstream}"] + ([f"--approve-names={approve}"] if approve else []),
        cwd=_ROOT, capture_output=True, encoding="utf-8", errors="replace",
        env=dict(os.environ, PYTHONUTF8="1"))
    print(textwrap.indent(review.stdout.rstrip(), "    "))
    if review.returncode == 1:
        failures.append(f"names new since {upstream} await a person's approval: read the "
                        f"list above, then re-run with the --approve-names it prints")
    elif review.returncode != 0:
        failures.append(f"name review could not run: {review.stderr.strip()[-300:]}")

    # 6. the suite passes on what actually ships — the check a manifest cannot give you
    env = {k: v for k, v in os.environ.items() if k not in _COLOUR_FORCING}
    env.update(PYTHONPATH=".", QT_QPA_PLATFORM="offscreen", PYQTGRAPH_QT_LIB="PySide6",
               PYTHONUTF8="1")

    # The assembled tree has NO built native core: the module is gitignored, so the export
    # cannot carry it. It is compiled here from the assembled tree's own native/, outside
    # `dest` (any path inside it is walked by test_every_tracked_path_is_classified), put on
    # PYTHONPATH, and ASSERTED to be the module the suite imports.
    print("  native core from the assembled tree's native/ ...")
    corelib, problem = build_core(dest, work)
    if problem:
        failures.append(f"{problem} — the suite below does not test this tree's native "
                        f"code")
    else:
        assert corelib is not None
        env["PYTHONPATH"] = os.pathsep.join([".", corelib])
        probe = subprocess.run(
            [sys.executable, "-c",
             "import swi3score, swi3s_studio.session as s;"
             "print(swi3score.score_abi, s._REQUIRED_SCORE_ABI, swi3score.__file__, "
             "sep='\\n')"],
            cwd=dest, env=env, capture_output=True, encoding="utf-8", errors="replace")
        if probe.returncode != 0:
            failures.append(f"native core will not import on the assembled tree: "
                            f"{(probe.stderr.strip().splitlines() or ['?'])[-1]}")
        else:
            abi, want, path = probe.stdout.strip().splitlines()[-3:]
            # Must be the module just built, NOT a stray site-packages build.
            if not os.path.realpath(path).startswith(os.path.realpath(corelib) + os.sep):
                failures.append(f"the suite would import swi3score from {path}, not the "
                                f"core built from the assembled tree")
            elif abi != want:
                failures.append(f"score_abi {abi} != session.py's required {want} in the "
                                f"assembled tree")
            else:
                print(f"    native core score_abi={abi}, compiled from the assembled tree's "
                      f"native/")

    print("  test suite on the assembled tree (this is the slow one) ...")
    failures += run_assembled_suite(dest, env)
    return failures


if __name__ == "__main__":
    raise SystemExit(main())
