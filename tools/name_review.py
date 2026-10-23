#!/usr/bin/env python3
"""Every NAME a publish would introduce, for a person to read and approve.

    python3 tools/name_review.py --tree=DIR [--baseline=REV] [--approve-names=DIGEST]

Lists each name-like word in the tree at DIR that the baseline tree (default: public/main, the
last published tree) does not contain, with where it appears. Exit 0 when there is nothing new
or the list is approved; 1 when names wait for approval; 2 when the baseline cannot be read or
an option is not recognised. tools/publish_tree.py runs it on the assembled tree, as its check
5b. `--approve=DIGEST` is accepted as a synonym of `--approve-names=DIGEST`.

A LIST OF NEW NAMES, NOT ANOTHER PATTERN. The leak scan is a denylist: it catches the strings
someone thought to list, and nothing else. This lists what is NEW between two releases, which
is short enough for a person to read.

WHAT IS READ. Every file's relative path (reported as line 0) and its content, decoded the way
tools/leak_scan.py decodes it: UTF-8, UTF-16, or the printable ASCII runs of a binary file.
A symlink, to a file or a directory, is read as its target path, as git stores it. A file
over 4 MB is not read; each one the baseline does not hold byte-for-byte is listed by name
with its SHA-256, and the digest approves that content: changing the file's bytes changes the
digest.

WHAT COUNTS AS A WORD. Identifiers are split at underscores, hyphens and case changes
(`VendorXmlImporter` -> vendor, xml, importer), so a name hidden inside an identifier is still
seen. A token that mixes letters and digits is also kept whole (`QRT7731` -> qrt7731, as
well as qrt), so a part number is listed as itself. A letter is any Unicode letter, read
after NFC normalisation, so an accented or non-Latin name is one word (`Ødegård` ->
ødegård), and a run of a script without case (CJK, kana) is one word. Words are lowercased
and compared as words, at three letters or more, or two for a caseless run. Numbers, `0x`
literals and hex-shaped tokens are skipped, except a token of two or more hex letters followed
by two or more digits, ten characters at most (`FADE42` -> fade42), which is the shape of a
part number rather than of a hash. There is NO DICTIONARY FILTER: a company or a codename can
be an ordinary English word.

WHAT IS NOT NEW. A word the baseline holds, and a plural, past or -ing form of one (-s, -es,
-ed, -ing, with a dropped final e or a doubled final consonant restored), is not listed.
Derivations (-er, -ers, -al, -ly, a bare -d) are listed, because a surname or a company name
can be one: porter beside port, bard beside bar. A name that is itself a plural, past or -ing
form of a published word is not listed.

APPROVAL IS FOR A PERSON. --approve-names takes the digest printed with the list, which stands
for that exact set of words and the unread files' contents: any change to either changes it.
It stands for WORDS, NOT OCCURRENCES: a later use of a word already in the approved set does
not change the digest and is not shown again. Pass it only after reading the list; an agent
preparing a publish must ask the maintainer for it rather than copy it from the output.
"""
from __future__ import annotations

import hashlib
import importlib.util
import io
import os
import re
import subprocess
import sys
import tarfile
import unicodedata
from collections import defaultdict
from typing import Dict, Iterable, List, Set, Tuple

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_decoder():
    """tools/leak_scan.py's decode_bytes, loaded by path (tools/ is not a package)."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "leak_scan.py")
    spec = importlib.util.spec_from_file_location("_name_review_leak_scan", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.decode_bytes


decode_bytes = _load_decoder()

# Words are found in a CLASS STRING the length of the text, one code per character:
# A an upper- or titlecase letter, a a lowercase letter, x a letter of a script without case
# (CJK, kana), 0 a digit or other number, m a combining mark, and a space for anything else.
_TOKEN = re.compile(r"[Aax][Aax0]*")
# CamelCase parts: an acronym before a capitalised word, a word, an acronym, a run of
# caseless letters, a run of digits.
_PART = re.compile(r"A+(?=Aa)|A?a+|A+|x+|0+")
_MIN_CASED, _MIN_CASELESS = 3, 2       # shortest word kept, in characters


class _Classes(dict):
    """str.translate table: code point -> its class code, computed on first use."""

    def __missing__(self, cp: int) -> str:
        cat = unicodedata.category(chr(cp))
        code = ("A" if cat in ("Lu", "Lt") else "a" if cat == "Ll" else
                "x" if cat[0] == "L" else "m" if cat[0] == "M" else
                "0" if cat[0] == "N" else " ")
        self[cp] = code
        return code


_CLASSES = _Classes()


def _classes(text: str) -> str:
    """The class string of `text`. A combining mark takes the class of the letter it follows
    (a lowercase run's, after a cased letter), and is a separator anywhere else."""
    codes = text.translate(_CLASSES)
    if "m" not in codes:
        return codes
    out = []
    for c in codes:
        if c == "m":
            prev = out[-1] if out else " "
            c = "x" if prev == "x" else "a" if prev in "Aa" else " "
        out.append(c)
    return "".join(out)
_HEXISH = re.compile(r"^(?=.*\d)[0-9a-f]+$")
_PART_NUMBER = re.compile(r"^[a-f]{2,}[0-9]{2,}$")    # bead42: hex letters, then digits
_HEX_LITERAL = re.compile(r"\b0[xX][0-9A-Fa-f_]+\b")   # 0xDEADBEEF is a number, not a word
_COLOUR = re.compile(r"#(?:[0-9A-Fa-f]{8}|[0-9A-Fa-f]{6}|[0-9A-Fa-f]{3,4})\b")   # #aa0000
_MAX_BYTES = 4 << 20                         # larger files are data, not prose or code
_SHOWN = 3                                   # locations listed per word


def words_in(text: str) -> Iterable[str]:
    """The comparable words of `text`, lowercased: identifier parts split out, and a token
    mixing letters and digits also kept whole. Letters are Unicode letters, read after NFC
    normalisation; a run of caseless letters is one word."""
    text = _COLOUR.sub(" ", _HEX_LITERAL.sub(" ", text))
    if not text.isascii():
        text = unicodedata.normalize("NFC", text)
    codes = _classes(text)
    for tok_m in _TOKEN.finditer(codes):
        start, tok_codes = tok_m.start(), tok_m.group()
        tok = text[start:tok_m.end()]
        parts = [(tok[m.start():m.end()], m.group()[0] == "x")
                 for m in _PART.finditer(tok_codes)]
        if "0" in tok_codes and len(parts) > 1:
            parts.append((tok, False))
        for part, caseless in parts:
            w = part.lower()
            if len(w) < (_MIN_CASELESS if caseless else _MIN_CASED) or w.isdigit():
                continue
            if _HEXISH.match(w) and not (len(w) <= 10 and _PART_NUMBER.match(w)):
                continue
            yield w


def _text_of(data: bytes) -> str | None:
    """The file's text as tools/leak_scan.py reads it, or None for a file over _MAX_BYTES."""
    if len(data) > _MAX_BYTES:
        return None
    return decode_bytes(data)[0]


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read_dir(root: str) -> Tuple[Dict[str, List[Tuple[str, int, str]]], Dict[str, str]]:
    """(word -> [(path, line, text)], unread path -> sha256) for every file under `root`,
    .git excluded. A path's own words are at line 0. A symlink, including one to a directory
    (which is not descended into), is read as its target path, the text git stores for it."""
    seen: Dict[str, List[Tuple[str, int, str]]] = defaultdict(list)
    unread: Dict[str, str] = {}
    for base, dirs, files in os.walk(root):
        links = [d for d in dirs if os.path.islink(os.path.join(base, d))]
        dirs[:] = sorted(d for d in dirs if d != ".git" and d not in links)
        for name in sorted(files + links):
            path = os.path.join(base, name)
            rel = os.path.relpath(path, root).replace(os.sep, "/")
            for w in set(words_in(rel)):
                seen[w].append((rel, 0, rel))
            try:
                if os.path.islink(path):
                    data = os.fsencode(os.readlink(path))
                else:
                    with open(path, "rb") as f:
                        data = f.read()
            except OSError:
                continue
            text = _text_of(data)
            if text is None:
                unread[rel] = _sha(data)
                continue
            for n, line in enumerate(text.splitlines(), 1):
                for w in set(words_in(line)):
                    seen[w].append((rel, n, line.strip()))
    return seen, unread


def vocabulary_of_dir(root: str) -> Dict[str, List[Tuple[str, int, str]]]:
    """word -> [(path, line, text)] for every file under `root`, .git excluded."""
    return _read_dir(root)[0]


def _read_rev(rev: str, cwd: str | None = None) -> Tuple[Set[str], Dict[str, str]]:
    """(every word, unread path -> sha256) of the tree at git revision `rev`, read from
    `git archive`, paths included. A symlink is read as its target path."""
    out = subprocess.run(["git", "archive", "--format=tar", rev], cwd=cwd or _ROOT,
                         capture_output=True)
    if out.returncode != 0:
        raise RuntimeError(out.stderr.decode(errors="replace").strip() or f"no tree at {rev}")
    words: Set[str] = set()
    unread: Dict[str, str] = {}
    with tarfile.open(fileobj=io.BytesIO(out.stdout)) as tar:
        for member in tar:
            if member.issym():
                words.update(words_in(member.name))
                words.update(words_in(member.linkname))
                continue
            if not member.isfile():
                continue
            words.update(words_in(member.name))
            f = tar.extractfile(member)
            data = f.read() if f is not None else b""
            text = _text_of(data)
            if text is None:
                unread[member.name] = _sha(data)
            else:
                words.update(words_in(text))
    return words, unread


def vocabulary_of_rev(rev: str, cwd: str | None = None) -> Set[str]:
    """Every word of the tree at git revision `rev`, paths included."""
    return _read_rev(rev, cwd)[0]


# Inflections only: a plural, past or -ing form of a published word ("provenances" beside
# "provenance"). -er, -ers, -al and -ly make new words, and surnames and company names are
# among them, so those are listed. See "WHAT IS NOT NEW" in the module docstring.
_SUFFIXES = ("s", "es", "ed", "ing")


def _inflects(word: str, known: Set[str]) -> bool:
    """True when `word` is `known` word plus one of _SUFFIXES: the stem as is, with a final
    e restored (used, saving), or with a doubled final consonant undone (mapped)."""
    for suf in _SUFFIXES:
        if not word.endswith(suf):
            continue
        stem = word[: -len(suf)]
        stems = [stem]
        if suf != "s":
            stems.append(stem + "e")
            if len(stem) > 3 and stem[-1] == stem[-2]:
                stems.append(stem[:-1])
        if any(len(st) >= 3 and st in known for st in stems):
            return True
    return False


def _new(seen: Dict[str, List[Tuple[str, int, str]]],
         baseline: Set[str]) -> Dict[str, List[Tuple[str, int, str]]]:
    return {w: locs for w, locs in seen.items()
            if w not in baseline and not _inflects(w, baseline)}


def _changed(unread: Dict[str, str], baseline_unread: Dict[str, str]) -> List[str]:
    return sorted(p for p, sha in unread.items() if baseline_unread.get(p) != sha)


def new_names(tree: str, baseline: Set[str]) -> Dict[str, List[Tuple[str, int, str]]]:
    return _new(vocabulary_of_dir(tree), baseline)


def unread_files(tree: str, baseline_unread: Dict[str, str]) -> List[str]:
    """Files under `tree` too large to read that the baseline does not hold byte-for-byte."""
    return _changed(_read_dir(tree)[1], baseline_unread)


def digest(words: Iterable[str], unread: Dict[str, str] | None = None) -> str:
    """Stands for exactly this set of words and these unread files' contents (path ->
    sha256): approving it approves nothing else. Words, not occurrences: a new use of a word
    in the set leaves it unchanged."""
    items = sorted(words) + sorted(f"file:{p}:{sha}" for p, sha in (unread or {}).items())
    return hashlib.sha256("\n".join(items).encode()).hexdigest()[:12]


def report(found: Dict[str, List[Tuple[str, int, str]]],
           unread: Dict[str, str] | None = None) -> str:
    lines = []
    for w in sorted(found, key=lambda k: (-len(found[k]), k)):
        locs = found[w]
        lines.append(f"  {w}  ({len(locs)}×)")
        for path, n, text in locs[:_SHOWN]:
            lines.append(f"      {path}:{n}: {text[:110]}")
        if len(locs) > _SHOWN:
            lines.append(f"      … and {len(locs) - _SHOWN} more")
    if unread:
        lines.append(f"  NOT READ (over {_MAX_BYTES >> 20} MB) — open each and check it by hand;"
                     f" the digest approves these exact bytes:")
        lines.extend(f"      {p}  sha256 {sha[:16]}" for p, sha in sorted(unread.items()))
    return "\n".join(lines)


_OPTIONS = ("tree", "baseline", "approve", "approve-names")


def main(argv: List[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    bad = [a for a in argv
           if not (a.startswith("--") and "=" in a and a[2:].split("=", 1)[0] in _OPTIONS)]
    if bad:
        print(f"name review: unknown option(s): {' '.join(bad)}\n{__doc__}", file=sys.stderr)
        return 2
    opts = dict(a[2:].split("=", 1) for a in argv)
    approve = opts.get("approve-names", opts.get("approve"))
    tree = opts.get("tree", _ROOT)
    rev = opts.get("baseline", "public/main")
    try:
        baseline, baseline_unread = _read_rev(rev)
    except RuntimeError as exc:
        print(f"name review: cannot read the baseline tree {rev!r}: {exc}", file=sys.stderr)
        return 2
    seen, tree_unread = _read_dir(tree)
    found = _new(seen, baseline)
    unread = {p: tree_unread[p] for p in _changed(tree_unread, baseline_unread)}
    if not found and not unread:
        print(f"name review: no names new since {rev}")
        return 0
    d = digest(found, unread)
    print(f"name review: {len(found)} name(s) new since {rev}"
          + (f", {len(unread)} file(s) not read" if unread else "")
          + " — read each one: a company, a person, a product or part, a codename or a host "
            "does not ship")
    print(report(found, unread))
    print(f"\n  the digest stands for these words{' and files' if unread else ''}, not for "
          f"their occurrences: a later use of an approved word is not listed again")
    if approve == d:
        print(f"\n  approved: {len(found)} name(s), {len(unread)} unread file(s), digest {d}")
        return 0
    if approve is not None:
        print(f"\n  --approve-names={approve} is not this list's digest ({d}): the list "
              f"changed since it was read, or the digest is from another list")
    print(f"\n  to approve exactly this list, re-run with --approve-names={d}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
