#!/usr/bin/env python3
"""Leak scan: does this tree carry anything that only makes sense where it was built?

    python3 tools/leak_scan.py              # working tree (tracked + untracked, not ignored)
    python3 tools/leak_scan.py <sha>        # a specific commit's tree
    python3 tools/leak_scan.py --tree=<dir> # a directory that is not a checkout: an
                                           # exported tree, walked whole (see _walk)
    python3 tools/leak_scan.py --text=<f>  # any text file: a release description, a commit
                                           # message — content that ships but is not a FILE

"Leak" is broader than secrets. A reader of a published repository cannot see the
environment the code was developed in, so anything that reveals or depends on that
environment is a leak — it is unverifiable to them at best and confusing at worst. The
categories are documented in docs/DEVELOPMENT.md ("Pre-flight before publishing a release").

WHAT SHIPS IS NOT ONLY FILES. A release description and the commit message on a publish
branch are usually the FIRST things an outside reader sees, and neither is in the tree;
`--text=` scans them. The patterns here are structural, so prose disclosures are a review
rule, not a pattern — see docs/DEVELOPMENT.md, "Write the RULE, not the story".

This scanner carries only STRUCTURAL patterns, which are safe to publish: private IP addresses,
home-directory paths, macOS volume, iCloud Drive and container paths, the absolute path of this
checkout and of the scanned tree, key file names, private-key headers. Site-specific names —
host names, repository names, a lab machine's address, and the names of third parties whose
material the project was given (a vendor whose register file a dialect was written from, its
part numbers and codenames) — must NOT live here, because a published list of the strings you
are trying not to publish is itself the leak. Put those in a file named by
$SWI3S_LEAK_PATTERNS, one regex per line, kept outside the repository; the scan uses it when
present and says so when it is absent, so a missing file can never look like a clean result.
"""
from __future__ import annotations

import fnmatch
import os
import re
import subprocess
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Built from parts so this file does not match its own patterns when it scans the tree.
_HOME = "/" + "Users/"
# A Windows home with one or more backslashes per separator: JSON and non-raw string
# literals store `C:\\Users\\name` with each backslash doubled.
_WINHOME = r"\b[A-Za-z]:\\+" + r"Users\\+"
# A Linux home. `runner` is the hosted CI account, which names no person.
_LINUXHOME = "/" + r"home/(?!runner\b)"
# A macOS volume mount, an iCloud Drive container, and an app's sandbox container: each names
# a disk, an account's synced storage or a sandboxed process, wherever the home directory is.
_VOLUME = "/" + r"(?-i:Volumes)/[^/\r\n]+/"   # a volume name may hold spaces
_ICLOUD = r"Mobile " + r"Documents[/\\]|com~apple~" + r"CloudDocs"
_CONTAINER = "/" + r"Library/(?:Daemon )?" + r"Containers/"

# A dotted spec-section citation (§10.1.4.4.2) looks exactly like a 10.0.0.0/8 address.
# Narrowed rather than allowlisted, by two structural discriminators, neither of which a
# real address can satisfy:
#   * a section citation is introduced by §, so reject that prefix;
#   * an IPv4 address has exactly FOUR octets, so reject a fifth (§10.1.4.4.2).
# A genuine 10.x address in prose is still caught: it has four octets and no §.
#
# The fifth-octet guard is `(?!\.\d)`: only a dot FOLLOWED BY A DIGIT means "this is not
# an address", so a sentence-final period after an address still matches.
_IPV4_TAIL = r"(?!\.?\d)(?!\.\d)"   # no 5th octet; a sentence-final '.' is fine

_STRUCTURAL: list[tuple[str, str]] = [
    (r"(?<!§)\b10\.\d{1,3}\.\d{1,3}\.\d{1,3}\b" + _IPV4_TAIL,
     "private IP address (10.0.0.0/8)"),
    (r"\b192\.168\.\d{1,3}\.\d{1,3}\b" + _IPV4_TAIL,
     "private IP address (192.168.0.0/16)"),
    (r"\b172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}\b" + _IPV4_TAIL,
     "private IP address (172.16.0.0/12)"),
    (_HOME + r"[A-Za-z0-9._-]+", "absolute home path (reveals a user name)"),
    (_WINHOME + r"[A-Za-z0-9._-]+", "absolute Windows home path (reveals a user name)"),
    (_LINUXHOME + r"[A-Za-z0-9._-]+", "absolute Linux home path (reveals a user name)"),
    (_VOLUME, "absolute macOS volume path (names a disk)"),
    (_ICLOUD, "iCloud Drive path (names synced storage)"),
    (_CONTAINER, "macOS container path (names a sandboxed process's storage)"),
    (r"\bid_(?:rsa|ed25519|ecdsa|dsa)\b", "SSH key file name"),
    (r"BEGIN (?:OPENSSH|RSA|DSA|EC|PGP) PRIVATE KEY", "PRIVATE KEY MATERIAL"),
    # A VARIABLE is not a key name. The pattern targets a literal path escaping into the
    # tree (`ssh -i ~/.ssh/<name>`); `ssh -i "$KEY"` names nothing and is not matched.
    (r"ssh\s+-i\s+(?![\"']?\$)\S+", "ssh invocation naming a key file"),
]

# Files that legitimately contain the STRUCTURAL patterns because they DEFINE or TEST them.
# The exemption is per-pattern-KIND, not per-file: these two are skipped for structural
# patterns only, and are scanned normally for site-specific ones.
#
# Site patterns cannot self-match by construction: they live outside the repository (see
# _site_patterns), so nothing here defines them.
_SELF = {"tools/leak_scan.py", "tests/test_leak_scan.py"}


def _own_path_patterns(root: str, why: str = "absolute path of the tree being scanned"
                       ) -> list[tuple[str, str]]:
    """Patterns for the absolute path of the tree being scanned, as `root` spells it and, on
    Windows, with forward or doubled separators. Matched only as a whole path (not preceded
    by a path character, ending at a separator or the end of a component). No pattern for a
    path of fewer than two components, which would match ordinary relative paths."""
    root = os.path.abspath(root)
    parts = [p for p in re.split(r"[\\/]", os.path.splitdrive(root)[1]) if p]
    if len(parts) < 2:
        return []
    spellings = {root}
    if os.sep == "\\":
        spellings |= {root.replace("\\", "/"), root.replace("\\", "\\\\")}
    return [(r"(?<![\w./\\-])" + re.escape(sp) + r"(?![\w.-])", why)
            for sp in sorted(spellings)]


def _files(rev: str | None, tree: str | None = None) -> list[str]:
    """Paths to scan, relative to `tree` when one is given and to _ROOT otherwise.

    A `tree` is walked (see _walk for what the walk skips). So is _ROOT when it is not a git
    repository: the most important thing to scan is an EXPORTED tree, and
    `tools/publish_tree.py` builds one with `git archive`, which has no .git.
    """
    if tree is not None:
        found, _SKIPPED[:] = _walk(tree)
        return found
    if rev:
        out = subprocess.run(["git", "ls-tree", "-r", "--name-only", rev],
                             cwd=_ROOT, capture_output=True, text=True, check=True)
        return [f for f in out.stdout.splitlines() if f]

    # Tracked files plus untracked ones that are not ignored: what the next commit can carry.
    out = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard"],
                         cwd=_ROOT, capture_output=True, text=True)
    if out.returncode == 0 and out.stdout.strip():
        return sorted({f for f in out.stdout.splitlines() if f})

    found, _SKIPPED[:] = _walk(_ROOT)
    return found


# Paths the last filesystem walk left out because the tree's .gitignore excludes them.
_SKIPPED: list[str] = []
# The native core a gate builds beside the tree (gitignored as /swi3score*.so etc.) is a
# build artefact, never part of a published tree, and its name carries the platform, which
# a published-tier pattern can rightly forbid in the files that are published.
_CORE = re.compile(r"swi3score[^/]*\.(so|pyd|dylib)$")


def _gitignore_rules(root: str) -> list[tuple[list[str], bool, bool]]:
    """(pattern components, directory only, anchored) for each rule of `root`/.gitignore.

    Git's rules for one top-level file: `#` starts a comment only in column 0, trailing
    spaces are dropped, a trailing `/` matches directories only, and a pattern holding a `/`
    elsewhere is anchored to the root and matched component by component. A file using
    `!` (re-include) or `**` yields no rules, so nothing is skipped rather than too much."""
    try:
        with open(os.path.join(root, ".gitignore"), encoding="utf-8") as f:
            lines = f.read().splitlines()
    except OSError:
        return []
    rules = []
    for line in lines:
        line = line.rstrip(" ")
        if not line or line.startswith("#"):
            continue
        if line.startswith("!") or "**" in line:
            return []
        dir_only = line.endswith("/")
        pat = line.rstrip("/")
        anchored = "/" in pat
        rules.append(([c for c in pat.split("/") if c], dir_only, anchored))
    return rules


def _ignored(rel: str, is_dir: bool, rules: list[tuple[list[str], bool, bool]]) -> bool:
    parts = rel.split("/")
    for pat, dir_only, anchored in rules:
        if dir_only and not is_dir:
            continue
        if anchored:
            if len(pat) == len(parts) and all(
                    fnmatch.fnmatchcase(p, q) for p, q in zip(parts, pat)):
                return True
        elif pat and fnmatch.fnmatchcase(parts[-1], pat[0]):
            return True
    return False


def _walk(root: str) -> tuple[list[str], list[str]]:
    """(files to scan, paths skipped) for a tree that is not a git checkout.

    Skipped: `.git`, whatever the tree's own .gitignore excludes (what a git listing would
    leave out), the top-level native core, and an AppleDouble `._name` beside its `name`.
    A symlink is a file, as git stores it, whatever it points at: a link to a directory is
    listed and not descended into, and _content reads its target path.
    POSIX separators always: _SELF and every caller compare against "a/b.py"."""
    rules = _gitignore_rules(root)
    found: list[str] = []
    skipped: list[str] = []
    for base, dirs, names in os.walk(root):
        rel_base = os.path.relpath(base, root).replace(os.sep, "/")
        rel_base = "" if rel_base == "." else rel_base + "/"
        keep = []
        for d in sorted(dirs):
            if os.path.islink(os.path.join(base, d)):
                (skipped if _ignored(rel_base + d, False, rules) else found).append(
                    rel_base + d)
            elif d == ".git" or _ignored(rel_base + d, True, rules):
                skipped.append(rel_base + d + "/")
            else:
                keep.append(d)
        dirs[:] = keep
        for n in names:
            rel = rel_base + n
            if (_CORE.fullmatch(rel)                    # top level only, as .gitignore has it
                    or (n.startswith("._") and n[2:] in names)
                    or _ignored(rel, False, rules)):
                skipped.append(rel)
            else:
                found.append(rel)
    return sorted(found), sorted(skipped)


def _content(path: str, rev: str | None, tree: str | None = None) -> tuple[str, bool]:
    """(text to scan, is_binary).

    * SYMLINKS. `open(..., "rb")` FOLLOWS a symlink, but the link's own target path is
      where a leak lives, so the link text is read explicitly (`git show` already returns
      the target string for a symlink blob).
    * UTF-16. UTF-16 puts a NUL between every ASCII character, so it is decoded as text
      rather than classified as binary (it has no printable run of 4+ bytes).
    * Binary files still get their printable ASCII runs, for names in capture headers.
    """
    if rev:
        p = subprocess.run(["git", "show", f"{rev}:{path}"], cwd=_ROOT, capture_output=True)
        raw = p.stdout if p.returncode == 0 else b""
    else:
        full = os.path.join(_ROOT if tree is None else tree, path)
        if os.path.islink(full):
            # The TARGET PATH is the thing to scan. Windows reports an absolute target with
            # the `\\?\` long-path prefix, which is not part of the path a reader sees.
            target = os.readlink(full)
            return (target[4:] if target.startswith("\\\\?\\") else target), False
        try:
            with open(full, "rb") as f:
                raw = f.read()
        except (OSError, IsADirectoryError):
            return "", False

    return decode_bytes(raw)


def _utf16_codec(sample: bytes) -> str | None:
    """The UTF-16 codec `sample` is written in, or None when it is not UTF-16.

    A byte-order mark decides it. Without one, UTF-16 is accepted only when its NULs fall the
    way ASCII-range UTF-16 puts them: in at least 40% of the pairs, all on the same side of
    the pair. A stray NUL in an 8-bit text file does not make it UTF-16."""
    if sample.startswith((b"\xff\xfe", b"\xfe\xff")):
        return "utf-16"
    pairs = len(sample) // 2
    if not pairs:
        return None
    even, odd = sample[0:2 * pairs:2].count(0), sample[1:2 * pairs:2].count(0)
    if odd >= 0.4 * pairs and even <= 0.05 * pairs:
        return "utf-16-le"
    if even >= 0.4 * pairs and odd <= 0.05 * pairs:
        return "utf-16-be"
    return None


def decode_bytes(raw: bytes) -> tuple[str, bool]:
    """(text, is_binary) for a file's bytes: UTF-8 text, UTF-16 text, or the printable ASCII
    runs (4+ bytes) of a binary file. tools/name_review.py reads files the same way.

    * No NUL in the first 8 KB: UTF-8 (undecodable bytes replaced).
    * UTF-16 by _utf16_codec, kept when over 90% of the decoded characters are printable.
    * NULs in under 1% of the first 8 KB, and the whole file strict UTF-8: text, NULs kept.
    * Otherwise binary: the printable ASCII runs.
    """
    sample = raw[:8192]
    if b"\0" not in sample:
        return raw.decode("utf-8", "replace"), False

    codec = _utf16_codec(sample)
    if codec:
        text = raw.decode(codec, "replace")
        printable = sum(ch.isprintable() or ch in "\r\n\t" for ch in text)
        if text and printable / len(text) > 0.9:
            return text, False                   # UTF-16 TEXT, not binary
    if sample.count(0) < 0.01 * len(sample):
        try:
            return raw.decode("utf-8"), False    # text with a stray NUL
        except UnicodeDecodeError:
            pass
    runs = re.findall(rb"[\x20-\x7e]{4,}", raw)
    return b"\n".join(runs).decode("ascii", "replace"), True


# Prefix marking a site pattern as PUBLISHED-TIER ONLY. Two different rules hide behind
# "leak", and conflating them makes one of them unusable:
#
#   * a private key or a lab address must not appear ANYWHERE, internal notes included;
#   * a third-party capture filename or a partner's name must not appear in what SHIPS —
#     but naming it in docs/internal/ is the whole point of an internal record, and the
#     format notes legitimately cite the captures a decode was verified against.
#
# With this prefix, a capture name can be scanned for without failing on the internal
# format notes that are pruned before publishing.
_PUBLISHED_ONLY_PREFIX = "published-only:"
# The tier tools/publish_tree.py prunes. Kept in sync with its _INTERNAL by the release-gate
# test, so the two cannot drift into disagreeing about what "published" means.
_INTERNAL_TIER = "docs/internal/"


def _site_patterns() -> tuple[list[tuple[str, str, bool]], str | None]:
    """(regex, why, published_only) for each site pattern, and the file they came from."""
    path = os.environ.get("SWI3S_LEAK_PATTERNS")
    if not path:
        return [], None
    if not os.path.isfile(path):
        print(f"SWI3S_LEAK_PATTERNS points at {path!r}, which does not exist", file=sys.stderr)
        raise SystemExit(2)
    pats = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith(_PUBLISHED_ONLY_PREFIX):
                pats.append((line[len(_PUBLISHED_ONLY_PREFIX):].strip(),
                             "site-specific pattern (published tier)", True))
            else:
                pats.append((line, "site-specific pattern", False))
    return pats, path


def _scan_text(path: str, patterns) -> int:
    """Scan ONE text file that is not part of the tree — a release description or a commit
    message. Same patterns, same exit code, so it can gate a publish step."""
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError as exc:
        print(f"cannot read {path!r}: {exc}", file=sys.stderr)
        return 2
    hits = []
    for i, line in enumerate(text.splitlines(), 1):
        for rx, why, _published_only, _structural in patterns:   # a description IS published
            m = rx.search(line)
            if m:
                hits.append((i, m.group(0), why))
    print(f"leak scan — text {path}: {len(text.splitlines())} lines, {len(patterns)} patterns")
    if not hits:
        print("\nno leaks found")
        return 0
    print(f"\n{len(hits)} POSSIBLE LEAK(S):")
    for line, text_, why in hits:
        print(f"  {path}:{line}: {why}\n    {text_}")
    return 1


def main() -> int:
    # REFUSE AN ARGUMENT THIS DOES NOT UNDERSTAND, rather than falling through to the default
    # scan: a mistyped `--text=` must not report a pass about input the caller never named.
    known = [a for a in sys.argv[1:] if a.startswith("-")]
    bad = [a for a in known if not a.startswith(("--text=", "--tree="))]
    if bad:
        print(f"unknown option(s): {' '.join(bad)}\n{__doc__}", file=sys.stderr)
        return 2
    text_arg = next((a.split("=", 1)[1] for a in sys.argv[1:]
                     if a.startswith("--text=")), None)
    tree = next((a.split("=", 1)[1] for a in sys.argv[1:] if a.startswith("--tree=")), None)
    rev = next((a for a in sys.argv[1:] if not a.startswith("-")), None)
    if tree is not None:
        if rev or text_arg is not None:
            print("--tree= scans a directory; it takes no <sha> and no --text=",
                  file=sys.stderr)
            return 2
        if not os.path.isdir(tree):
            print(f"--tree={tree}: no such directory", file=sys.stderr)
            return 2
    site, site_path = _site_patterns()
    # This checkout's path, and the scanned tree's when that is another directory.
    if tree is None:
        own = _own_path_patterns(_ROOT)
    else:
        own = _own_path_patterns(tree)
        own += [p for p in _own_path_patterns(_ROOT, "absolute path of the scanner's checkout")
                if p[0] not in {rx for rx, _ in own}]
    struct_pats = _STRUCTURAL + own
    patterns = [(re.compile(p, re.I), why, False, True) for p, why in struct_pats]
    patterns += [(re.compile(p, re.I), why, only, False) for p, why, only in site]
    if text_arg is not None:
        if not os.path.isfile(text_arg):
            print(f"--text={text_arg}: no such file", file=sys.stderr)
            return 2
        if not site_path:
            print("  no $SWI3S_LEAK_PATTERNS set — structural checks only.")
        return _scan_text(text_arg, patterns)

    hits: list[tuple[str, int, str, str]] = []
    files = _files(rev, tree)
    n_binary = 0
    for path in files:
        # The PATH itself can leak — a fixture named after a device discloses it without the
        # name appearing in any file's contents. Scanned as line 0.
        internal = path.startswith(_INTERNAL_TIER)
        defines_them = path in _SELF
        for rx, why, published_only, structural in patterns:
            if published_only and internal:
                continue        # this tier is pruned before publishing — see the prefix note
            if structural and defines_them:
                continue        # this file DEFINES that pattern — see _SELF
            m = rx.search(path)
            if m:
                hits.append((path, 0, m.group(0), why + " — IN THE FILE NAME"))
        text, is_binary = _content(path, rev, tree)
        n_binary += is_binary
        if not text:
            continue
        for i, line in enumerate(text.splitlines(), 1):
            for rx, why, published_only, structural in patterns:
                if published_only and internal:
                    continue
                if structural and defines_them:
                    continue
                m = rx.search(line)
                if m:
                    hits.append((path, i, m.group(0),
                                 why + (" (printable run in a binary)" if is_binary else "")))

    where = (f"commit {rev}" if rev else f"the tree at {tree}" if tree is not None
             else "the working tree (tracked + untracked, not ignored)")
    print(f"leak scan — {where}: {len(files)} files "
          f"({len(files) - n_binary} text, {n_binary} binary), {len(patterns)} patterns "
          f"({len(struct_pats)} structural, {len(site)} site-specific)")
    if _SKIPPED and not rev:
        print(f"  {len(_SKIPPED)} path(s) not scanned: .git, or excluded by the tree's "
              f".gitignore")
    if site_path:
        print(f"  site patterns from {site_path}")
        if not site:
            print(f"  {site_path} holds NO patterns — host names, repository names and lab"
                  "\n  addresses are NOT covered by this run.")
    else:
        # Loud, because a missing site list means the scan covered LESS than it looks like.
        print("  no $SWI3S_LEAK_PATTERNS set — structural checks only. Host names, repository"
              "\n  names and lab addresses are NOT covered by this run.")

    if not hits:
        print("\nno leaks found")
        return 0
    print(f"\n{len(hits)} POSSIBLE LEAK(S):")
    for path, line, text, why in hits:
        print(f"  {path}:{line}: {why}\n    {text}")
    print("\nSee docs/DEVELOPMENT.md 'Pre-flight before publishing a release'. If a hit is a"
          "\nfalse positive, narrow the pattern — do not add the real string to an allowlist"
          "\nin a published file.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
