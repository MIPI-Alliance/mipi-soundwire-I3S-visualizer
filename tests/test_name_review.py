"""tools/name_review.py: what a publish would introduce is listed for a person to approve.

The leak scan is a denylist and catches only what someone listed. This lists every word the
published tree would gain, splits identifiers so a name inside one is still seen, and blocks
until a person approves exactly that list. Every tree here is synthetic.
"""
import os
import subprocess
import sys

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "tools"))

import name_review  # noqa: E402


def _write(root, files):
    for rel, text in files.items():
        path = os.path.join(root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)


@pytest.fixture
def repo(tmp_path):
    """A git repository whose HEAD is the 'published' baseline."""
    base = tmp_path / "base"
    base.mkdir()
    _write(str(base), {"pkg/mod.py": "def parse_register(address):\n    return address\n",
                       "README.md": "A register map importer for a bus analyzer.\n"})
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.invalid",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.invalid")
    for cmd in (["git", "init", "-q"], ["git", "add", "-A"],
                ["git", "commit", "-q", "-m", "base"]):
        subprocess.run(cmd, cwd=base, check=True, env=env, capture_output=True)
    return base


def _review(repo, tree):
    baseline = name_review.vocabulary_of_rev("HEAD", cwd=str(repo))
    return name_review.new_names(str(tree), baseline)


def test_a_name_inside_an_identifier_is_seen(repo, tmp_path):
    """A word inside a class name is found."""
    tree = tmp_path / "tree"
    _write(str(tree), {"pkg/mod.py": "class NimbusXmlImporter:\n    pass\n"
                                     "def parse_register(address):\n    return address\n"})
    found = _review(repo, tree)
    assert "nimbus" in found and found["nimbus"][0][:2] == ("pkg/mod.py", 1)
    assert "parse" not in found and "register" not in found


def test_an_ordinary_english_word_is_still_listed(repo, tmp_path):
    """No dictionary filter: a company or codename can be an English word."""
    tree = tmp_path / "tree"
    _write(str(tree), {"README.md": "A register map importer, nimbus-style.\n"})
    assert "nimbus" in _review(repo, tree)


def test_what_was_published_and_its_inflections_are_not_new(repo, tmp_path):
    tree = tmp_path / "tree"
    _write(str(tree), {"README.md": "Register maps and importers; a register mapped.\n",
                       "pkg/mod.py": "x = 0xDEADBEEF  # 12345\n"})
    found = _review(repo, tree)
    assert not ({"registers", "maps", "importers", "mapped"} & set(found)), found
    assert not any(w.isdigit() or w == "deadbeef" for w in found)


def test_approval_is_for_exactly_the_list_that_was_read(repo, tmp_path, monkeypatch, capsys):
    tree = tmp_path / "tree"
    _write(str(tree), {"README.md": "Thanks to Zorblax for the Quuxor colours.\n"})
    found = _review(repo, tree)
    d = name_review.digest(found)
    monkeypatch.chdir(repo)
    monkeypatch.setattr(name_review, "_ROOT", str(repo))
    args = [f"--tree={tree}", "--baseline=HEAD"]
    assert name_review.main(args) == 1                       # waits for a person
    out = capsys.readouterr().out
    assert "zorblax" in out and "quuxor" in out and f"--approve-names={d}" in out
    assert "not for their occurrences" in out          # what the digest does not cover
    assert name_review.main(args + [f"--approve-names={d}"]) == 0   # the flag it printed
    assert name_review.main(args + [f"--approve={d}"]) == 0         # and its synonym
    # One more word is another list: the old digest approves nothing.
    _write(str(tree), {"NOTES.md": "Built on host frobnitz.\n"})
    capsys.readouterr()
    assert name_review.main(args + [f"--approve-names={d}"]) == 1
    assert "not this list's digest" in capsys.readouterr().out


def test_an_unknown_option_is_refused(repo, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(name_review, "_ROOT", str(repo))
    assert name_review.main([f"--tree={tmp_path}", "--baseline=HEAD", "--aprove=abc"]) == 2
    assert name_review.main([f"--tree={tmp_path}", "--baseline=HEAD", "--approve"]) == 2
    assert "unknown option" in capsys.readouterr().err


def test_a_name_in_a_file_path_is_seen(repo, tmp_path):
    tree = tmp_path / "tree"
    _write(str(tree), {"tests/fixtures/zorblaxcorp_capture.csv": "x = 1\n"})
    found = _review(repo, tree)
    assert found["zorblaxcorp"][0][:2] == ("tests/fixtures/zorblaxcorp_capture.csv", 0)


def test_utf16_and_binary_files_are_read(repo, tmp_path):
    """Read as tools/leak_scan.py reads them: UTF-16 as text, a binary by its printable runs."""
    tree = tmp_path / "tree"
    tree.mkdir()
    (tree / "notes_utf16.txt").write_bytes("Built for Quuxor\n".encode("utf-16"))
    (tree / "capture.bin").write_bytes(b"\x00\x01\x02Frobnitz board\x00\xff\xfe")
    found = _review(repo, tree)
    assert found["quuxor"][0][:2] == ("notes_utf16.txt", 1)
    assert "frobnitz" in found


@pytest.mark.parametrize("text, word", [
    ("QX12L34", "qx12l34"),
    ("the ZB2468 amplifier", "zb2468"),
    ("ZB1357-VQ", "zb1357"),
    ("QQX86420", "qqx86420"),
])
def test_a_part_number_is_kept_whole(text, word):
    assert word in set(name_review.words_in(text))


def test_hex_shapes_are_still_not_words():
    assert not set(name_review.words_in("0x1F2E a1b2c3 12345"))


def test_a_file_too_large_to_read_is_listed_by_name(repo, tmp_path, monkeypatch, capsys):
    """Not read, so its words cannot be listed: the file itself is, and the digest covers it.
    A file the baseline holds byte-for-byte is not listed."""
    monkeypatch.setattr(name_review, "_MAX_BYTES", 64)
    monkeypatch.setattr(name_review, "_ROOT", str(repo))
    tree = tmp_path / "tree"
    _write(str(tree), {"README.md": "A register map importer for a bus analyzer.\n",
                       "pkg/mod.py": "def parse_register(address):\n    return address\n",
                       "data/big.raw": "register " * 20})
    args = [f"--tree={tree}", "--baseline=HEAD"]
    assert name_review.main(args) == 1
    out = capsys.readouterr().out
    assert "NOT READ" in out and "data/big.raw" in out
    found = _review(repo, tree)
    sha = name_review._sha(("register " * 20).encode())
    assert f"data/big.raw  sha256 {sha[:16]}" in out
    d = name_review.digest(found, {"data/big.raw": sha})
    assert f"--approve-names={d}" in out
    assert name_review.digest(found) != d, "the digest does not cover the unread file"
    # The same bytes in the baseline: nothing unread is new.
    _, base_unread = name_review._read_rev("HEAD", cwd=str(repo))
    assert name_review.unread_files(str(tree), {**base_unread,
                                                "data/big.raw": name_review._sha(
                                                    ("register " * 20).encode())}) == []


def test_an_unreadable_baseline_is_an_error_not_a_pass(repo, tmp_path, monkeypatch):
    monkeypatch.setattr(name_review, "_ROOT", str(repo))
    assert name_review.main([f"--tree={tmp_path}", "--baseline=no-such-ref"]) == 2


def test_the_digest_covers_an_unread_files_content(repo, tmp_path, monkeypatch, capsys):
    """Approving a list that names an unread file approves those bytes: the same path with
    other content is another list."""
    monkeypatch.setattr(name_review, "_MAX_BYTES", 64)
    monkeypatch.setattr(name_review, "_ROOT", str(repo))
    tree = tmp_path / "tree"
    _write(str(tree), {"README.md": "A register map importer for a bus analyzer.\n",
                       "data/big.raw": "register " * 20})
    args = [f"--tree={tree}", "--baseline=HEAD"]
    name_review.main(args)
    d = capsys.readouterr().out.rsplit("--approve-names=", 1)[1].split()[0]
    assert name_review.main(args + [f"--approve-names={d}"]) == 0
    _write(str(tree), {"data/big.raw": "zorblax " * 20})          # same path, same size
    capsys.readouterr()
    assert name_review.main(args + [f"--approve-names={d}"]) == 1
    assert "not this list's digest" in capsys.readouterr().out


def test_a_derivation_of_a_published_word_is_listed():
    """-er, -ers, -al, -ly and a bare -d make new words, and a surname or a company name can
    be one, so they are listed; plurals, past and -ing forms of a published word are not."""
    baseline = {"port", "pack", "glob", "hunt", "week", "bar", "use", "map", "save", "turn"}
    derived = {"porter", "packers", "global", "hunter", "weekly", "bard", "turner"}
    inflected = {"ports", "packed", "globbing", "used", "mapped", "saving", "turns"}
    found = {w for w in derived | inflected if not name_review._inflects(w, baseline)}
    assert found == derived, found


def test_a_hex_shaped_part_number_is_a_word():
    """Two or more hex letters then two or more digits is a part number's shape; a hash, an
    interleaved hex string and a colour are not words."""
    assert "fade42" in set(name_review.words_in("the FADE42 codec"))
    assert "cafe1234" in set(name_review.words_in("CAFE1234-QFN"))
    words = set(name_review.words_in("a1b2c3 abcdef0123456789 #aabb00 #ACE 5df517b"))
    assert not words & {"a1b2c3", "abcdef0123456789", "aabb00", "ace", "df517b"}, words


@pytest.mark.parametrize("text, words", [
    ("Thanks to Bjørn Ødegård and Jiří Šťastný",
     {"thanks", "and", "bjørn", "ødegård", "jiří", "šťastný"}),
    ("Søren Ågård", {"søren", "ågård"}),
    ("Müller GmbH", {"müller", "gmb"}),
    ("株式会社ゾルバックス", {"株式会社ゾルバックス"}),
    ("田中 さん", {"田中", "さん"}),          # a caseless run of two is a word
    ("ZoëXmlImporter", {"zoë", "xml", "importer"}),
])
def test_an_accented_or_non_latin_name_is_one_word(text, words):
    assert set(name_review.words_in(text)) == words


def test_a_decomposed_accent_reads_as_the_composed_word():
    """NFD text (a base letter then a combining mark) gives the same words as NFC."""
    import unicodedata
    text = "Jiří Šťastný and Émile"
    assert (set(name_review.words_in(unicodedata.normalize("NFD", text)))
            == set(name_review.words_in(text)) == {"jiří", "šťastný", "and", "émile"})


def test_a_non_latin_name_new_since_the_baseline_is_listed(repo, tmp_path):
    tree = tmp_path / "tree"
    _write(str(tree), {"README.md": "A register map importer for Bjørn Ødegård.\n"})
    found = _review(repo, tree)
    assert {"bjørn", "ødegård"} <= set(found), sorted(found)


def test_a_symlink_is_read_as_its_target_path(repo, tmp_path):
    """A link to a directory is an entry, not descended into, and its target path is read,
    as git stores it; a baseline symlink's target is part of the baseline's words."""
    outside = tmp_path / "quuxcorp_share"
    (outside / "inner").mkdir(parents=True)
    (outside / "inner" / "plughwords.txt").write_text("plughword\n", encoding="utf-8")
    tree = tmp_path / "tree"
    _write(str(tree), {"README.md": "A register map importer for a bus analyzer.\n"})
    os.symlink(str(outside), tree / "captures")
    found = _review(repo, tree)
    assert found["quuxcorp"][0][:2] == ("captures", 1)
    assert "captures" in found and "plughword" not in found and "plughwords" not in found


def test_a_baseline_symlinks_target_is_published(tmp_path):
    base = tmp_path / "base"
    base.mkdir()
    os.symlink("zorblaxdir/thing", base / "link")
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.invalid",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.invalid")
    for cmd in (["git", "init", "-q"], ["git", "add", "-A"],
                ["git", "commit", "-q", "-m", "base"]):
        subprocess.run(cmd, cwd=base, check=True, env=env, capture_output=True)
    assert "zorblaxdir" in name_review.vocabulary_of_rev("HEAD", cwd=str(base))
