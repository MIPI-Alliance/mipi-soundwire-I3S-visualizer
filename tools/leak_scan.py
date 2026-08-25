#!/usr/bin/env python3
"""Leak scan: does this tree carry anything that only makes sense where it was built?

    python3 tools/leak_scan.py              # working tree (tracked files)
    python3 tools/leak_scan.py <sha>        # a specific commit's tree
    python3 tools/leak_scan.py --text=<f>  # any text file: a release description, a commit
                                           # message — content that ships but is not a FILE

"Leak" is broader than secrets. A reader of a published repository cannot see the
environment the code was developed in, so anything that reveals or depends on that
environment is a leak — it is unverifiable to them at best and confusing at worst. The
categories are documented in docs/DEVELOPMENT.md ("Pre-flight before publishing a release").

WHAT SHIPS IS NOT ONLY FILES. A release description and the commit message on a publish
branch are usually the FIRST things an outside reader sees, and neither is in the tree — so a
tree-only scan reports clean while the most-read text goes unchecked. That is not
hypothetical: a published branch description carried three of the categories this exists to
stop — an attribution, two capture filenames, and the platforms the release was gated on — and
the scan passed on that same content both as a tree and, when tested, as a tracked file. It
passed because the patterns here are structural and all three of those are prose. `--text=`
closes the artifact half of the gap. The prose half is a review rule, not a pattern — see
docs/DEVELOPMENT.md, "Write the RULE, not the story".

This scanner carries only STRUCTURAL patterns, which are safe to publish: private IP
addresses, home-directory paths, key file names, private-key headers. Site-specific names —
host names, repository names, a lab machine's address — must NOT live here, because a
published list of the strings you are trying not to publish is itself the leak (this
project's own release docs made exactly that mistake). Put those in a file named by
$SWI3S_LEAK_PATTERNS, one regex per line, kept outside the repository; the scan uses it when
present and says so when it is absent, so a missing file can never look like a clean result.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Built from parts so this file does not match its own patterns when it scans the tree.
_HOME = "/" + "Users/"
_WINHOME = r"C:\\" + r"Users\\"

# A dotted spec-section citation looks exactly like a 10.0.0.0/8 address:
# "§10.1.4.4.2" matched the private-IP pattern and failed the gate. Narrowed
# rather than allowlisted — an allowlist entry would have to name the file, and
# the next §10.x citation anywhere else would fail again.
#
# Two structural discriminators, neither of which a real address can satisfy:
#   * a section citation is introduced by §, so reject that prefix;
#   * an IPv4 address has exactly FOUR octets, so reject a fifth (§10.1.4.4.2).
# A genuine 10.x address in prose is still caught: it has four octets and no §.
#
# The fifth-octet guard is `(?!\.\d)`, NOT `(?![.\d])`. The stricter form also
# rejected a trailing sentence period — "the host is 10.1.4.4." stopped being a
# finding, which is a real address in ordinary prose. Only a dot FOLLOWED BY A
# DIGIT means "this is not an address".
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
    (r"\bid_(?:rsa|ed25519|ecdsa|dsa)\b", "SSH key file name"),
    (r"BEGIN (?:OPENSSH|RSA|DSA|EC|PGP) PRIVATE KEY", "PRIVATE KEY MATERIAL"),
    # A VARIABLE is not a key name. The point of this pattern is a literal path escaping
    # into the tree (`ssh -i ~/.ssh/<name>`); `ssh -i "$KEY"` names nothing and is how a
    # script SHOULD be written, so matching it taught the opposite lesson — and the project's
    # own rule is to narrow a pattern rather than allowlist the string it fires on.
    (r"ssh\s+-i\s+(?![\"']?\$)\S+", "ssh invocation naming a key file"),
]

# Files that legitimately contain the STRUCTURAL patterns because they DEFINE or TEST them.
# The exemption is per-pattern-KIND, not per-file: these two are skipped for structural
# patterns only, and are scanned normally for site-specific ones.
#
# A whole-file exemption is what it used to be, and it hid a real leak in a shipping file for a
# release. This module's own docstring is published prose, and it was describing what v3.0.17's
# branch description had disclosed by REPEATING the vocabulary — so the one file guaranteed to
# escape the scan was the one arguing for it. Site patterns cannot self-match by construction:
# they live outside the repository (see _site_patterns), so nothing here defines them.
_SELF = {"tools/leak_scan.py", "tests/test_leak_scan.py"}


def _files(rev: str | None) -> list[str]:
    """Paths to scan.

    Falls back to walking the filesystem when this is not a git repository, because the most
    important thing to scan is an EXPORTED tree — `tools/publish_tree.py` builds one with
    `git archive` and it has no .git, so a git-only implementation could not scan the exact
    artefact that gets published. (It couldn't: this returned a CalledProcessError there.)
    """
    if rev:
        out = subprocess.run(["git", "ls-tree", "-r", "--name-only", rev],
                             cwd=_ROOT, capture_output=True, text=True, check=True)
        return [f for f in out.stdout.splitlines() if f]

    out = subprocess.run(["git", "ls-files"], cwd=_ROOT, capture_output=True, text=True)
    if out.returncode == 0 and out.stdout.strip():
        return [f for f in out.stdout.splitlines() if f]

    skip = {".git", "__pycache__", ".venv", "venv", "env", "build", "dist", ".mypy_cache",
            ".ruff_cache", ".pytest_cache", "scratch", "node_modules", "dev_docs", ".eggs"}
    found: list[str] = []
    for base, dirs, names in os.walk(_ROOT):
        dirs[:] = [d for d in dirs if d not in skip]
        # macOS AppleDouble sidecars are filesystem metadata git never tracks;
        # one undeletable leftover on the Windows VM failed this otherwise.
        names = [n for n in names if not n.startswith('._')]
        for n in names:
            # POSIX separators ALWAYS: _SELF and every caller compare against "a/b.py", so on
            # Windows a backslash path silently matches nothing — which made this file scan
            # ITSELF there and report its own pattern descriptions ("10.0.0.0/8") as leaks.
            rel = os.path.relpath(os.path.join(base, n), _ROOT).replace(os.sep, "/")
            found.append(rel)
    return sorted(found)


def _content(path: str, rev: str | None) -> tuple[str, bool]:
    """(text to scan, is_binary).

    Three cases the 3.0.12 release review proved were being missed:

    * SYMLINKS. The gate invokes this with no revision, and `open(..., "rb")` FOLLOWS a
      symlink — so the link's own target path, which is where a leak lives (a fixture
      pointing at /Users/<name>/… or an internal share), was never read. `git show` returns
      the target string for a symlink blob, so the commit-mode path caught what the mode the
      gate actually uses did not. Read the link text explicitly.
    * UTF-16. `b"\\0" in raw` classified it as binary, and UTF-16 puts a NUL between every
      ASCII character, so no printable run of 4+ bytes exists and the "printable runs inside
      binaries" net recovered NOTHING — a leak in a UTF-16 file scanned as zero text while
      being counted as covered. Decode it as text instead.
    * Binary files still get their printable ASCII runs, for names in capture headers.
    """
    if rev:
        p = subprocess.run(["git", "show", f"{rev}:{path}"], cwd=_ROOT, capture_output=True)
        raw = p.stdout if p.returncode == 0 else b""
    else:
        full = os.path.join(_ROOT, path)
        if os.path.islink(full):
            return os.readlink(full), False      # the TARGET PATH is the thing to scan
        try:
            with open(full, "rb") as f:
                raw = f.read()
        except (OSError, IsADirectoryError):
            return "", False

    if b"\0" not in raw[:8192]:
        return raw.decode("utf-8", "replace"), False

    # NULs present: UTF-16 text, or genuinely binary?
    for enc in ("utf-16", "utf-16-le", "utf-16-be"):
        try:
            text = raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
        printable = sum(ch.isprintable() or ch in "\r\n\t" for ch in text)
        if text and printable / len(text) > 0.9:
            return text, False                   # UTF-16 TEXT, not binary
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
# Without this distinction, adding the capture names that leaked into v3.0.17 would have
# failed the gate on the internal format notes that are pruned before publishing — so the
# names would have been left out of the scan entirely, which is how they leaked.
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
    # scan. `--text=<file>` mistyped as `--tex=` used to scan the WORKING TREE instead and print
    # "no leaks found" — a pass, about input the caller never named. That is the NOT-RUN-HERE
    # failure in the one tool whose whole job is to be believed: the release description it was
    # pointed at would have gone out unscanned, and the transcript would show a green run.
    known = [a for a in sys.argv[1:] if a.startswith("-")]
    if any(not a.startswith("--text=") for a in known):
        bad = [a for a in known if not a.startswith("--text=")]
        print(f"unknown option(s): {' '.join(bad)}\n{__doc__}", file=sys.stderr)
        return 2
    text_arg = next((a.split("=", 1)[1] for a in sys.argv[1:]
                     if a.startswith("--text=")), None)
    rev = next((a for a in sys.argv[1:] if not a.startswith("-")), None)
    site, site_path = _site_patterns()
    patterns = [(re.compile(p, re.I), why, False, True) for p, why in _STRUCTURAL]
    patterns += [(re.compile(p, re.I), why, only, False) for p, why, only in site]
    if text_arg is not None:
        if not os.path.isfile(text_arg):
            print(f"--text={text_arg}: no such file", file=sys.stderr)
            return 2
        if not site_path:
            print("  no $SWI3S_LEAK_PATTERNS set — structural checks only.")
        return _scan_text(text_arg, patterns)

    hits: list[tuple[str, int, str, str]] = []
    files = _files(rev)
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
        text, is_binary = _content(path, rev)
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

    where = f"commit {rev}" if rev else "the working tree"
    print(f"leak scan — {where}: {len(files)} files "
          f"({len(files) - n_binary} text, {n_binary} binary), {len(patterns)} patterns "
          f"({len(_STRUCTURAL)} structural, {len(site)} site-specific)")
    if site_path:
        print(f"  site patterns from {site_path}")
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
