#!/usr/bin/env python3
"""Leak scan: does this tree carry anything that only makes sense where it was built?

    python3 tools/leak_scan.py              # working tree (tracked files)
    python3 tools/leak_scan.py <sha>        # a specific commit's tree

"Leak" is broader than secrets. A reader of a published repository cannot see the
environment the code was developed in, so anything that reveals or depends on that
environment is a leak — it is unverifiable to them at best and confusing at worst. The
categories are documented in docs/DEVELOPMENT.md ("Pre-flight before publishing a release").

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

_STRUCTURAL: list[tuple[str, str]] = [
    (r"\b10\.\d{1,3}\.\d{1,3}\.\d{1,3}\b", "private IP address (10.0.0.0/8)"),
    (r"\b192\.168\.\d{1,3}\.\d{1,3}\b", "private IP address (192.168.0.0/16)"),
    (r"\b172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}\b", "private IP address (172.16.0.0/12)"),
    (_HOME + r"[A-Za-z0-9._-]+", "absolute home path (reveals a user name)"),
    (_WINHOME + r"[A-Za-z0-9._-]+", "absolute Windows home path (reveals a user name)"),
    (r"\bid_(?:rsa|ed25519|ecdsa|dsa)\b", "SSH key file name"),
    (r"BEGIN (?:OPENSSH|RSA|DSA|EC|PGP) PRIVATE KEY", "PRIVATE KEY MATERIAL"),
    (r"ssh\s+-i\s+\S+", "ssh invocation naming a key file"),
]

# Files that legitimately contain the patterns because they DEFINE or TEST them.
_SELF = {"tools/leak_scan.py", "tests/test_leak_scan.py"}


def _files(rev: str | None) -> list[str]:
    if rev:
        out = subprocess.run(["git", "ls-tree", "-r", "--name-only", rev],
                             cwd=_ROOT, capture_output=True, text=True, check=True)
    else:
        out = subprocess.run(["git", "ls-files"], cwd=_ROOT,
                             capture_output=True, text=True, check=True)
    return [f for f in out.stdout.splitlines() if f and f not in _SELF]


def _content(path: str, rev: str | None) -> str:
    if rev:
        p = subprocess.run(["git", "show", f"{rev}:{path}"], cwd=_ROOT,
                           capture_output=True, text=True)
        return p.stdout if p.returncode == 0 else ""
    try:
        with open(os.path.join(_ROOT, path), encoding="utf-8", errors="replace") as f:
            return f.read()
    except (OSError, IsADirectoryError):
        return ""


def _site_patterns() -> tuple[list[tuple[str, str]], str | None]:
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
            if line and not line.startswith("#"):
                pats.append((line, "site-specific pattern"))
    return pats, path


def main() -> int:
    rev = next((a for a in sys.argv[1:] if not a.startswith("-")), None)
    site, site_path = _site_patterns()
    patterns = [(re.compile(p, re.I), why) for p, why in _STRUCTURAL + site]

    hits: list[tuple[str, int, str, str]] = []
    for path in _files(rev):
        text = _content(path, rev)
        if not text:
            continue
        for i, line in enumerate(text.splitlines(), 1):
            for rx, why in patterns:
                m = rx.search(line)
                if m:
                    hits.append((path, i, m.group(0), why))

    where = f"commit {rev}" if rev else "the working tree"
    print(f"leak scan — {where}, {len(patterns)} patterns "
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
