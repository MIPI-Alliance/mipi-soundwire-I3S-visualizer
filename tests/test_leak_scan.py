"""The structural leak patterns must catch real leaks and not fire on prose.

`tools/leak_scan.py` is a release gate, so both of its failure modes are
expensive and neither is visible from a green run:

* a **false negative** publishes the thing the scanner exists to stop;
* a **false positive** fails the gate on a legitimate file, and the tempting
  fix — an allowlist entry — is exactly what `docs/DEVELOPMENT.md` forbids,
  because it silences the pattern for that file permanently.

This file pins the discrimination itself, in both directions. It exists because
a spec-section citation (`§10.1.4.4.2`) is indistinguishable from a private IPv4
address to a naive pattern, and failed the gate. Narrowing the pattern to fix
that then dropped a genuine address written at the end of a sentence
("the host is 10.1.4.4.") — caught only because these cases were written down.

Only STRUCTURAL patterns are tested here. The site-specific list lives outside
the repository on purpose (see docs/DEVELOPMENT.md); a test that asserted
against its contents would republish exactly what it is meant to hide.
"""
from __future__ import annotations

import importlib.util
import os
import re
import subprocess
import sys

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _patterns():
    spec = importlib.util.spec_from_file_location(
        "leak_scan", os.path.join(_ROOT, "tools", "leak_scan.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod._STRUCTURAL


def _hits(text: str) -> bool:
    return any(re.search(rx, text) for rx, _ in _patterns())


# Real leakage, in the shapes it actually appears in: bare, in a URL, followed
# by a port, and — the case the first narrowing broke — ending a sentence.
@pytest.mark.parametrize("text", [
    "ssh user@10.0.1.42",
    "the lab host is 10.1.4.4 today",
    "10.20.30.40",
    "192.168.1.1",
    "172.16.5.9",
    "connect to 10.1.4.4:8080",
    "IP=10.1.4.4.",
    "reachable at 10.1.4.4, then",
    "(10.1.4.4)",
    "/Users/somebody/secrets",
    "id_ed25519",
    "ssh -i ~/.ssh/somekey host",
])
def test_a_real_leak_is_caught(text):
    assert _hits(text), f"structural scan MISSED a real leak: {text!r}"


# Spec citations. The repository cites SWI3S sections constantly, and §10.x is
# the family that collides with 10.0.0.0/8. A four-part §10 citation is still
# rejected by the § prefix; a five-part one by the octet count.
@pytest.mark.parametrize("text", [
    "the bus keeper holds the level (§10.1.4.4.2)",
    "§10.1.4.4",
    "spec §10.1.2.3.4",
    "Safe-Lock CDS position (§12.1.10.1)",
    "handover duration (§11.1.1.1)",
    "descrambler against spec §14.1.3.2",
    "version 10.1.4.4.2",
])
def test_a_spec_citation_is_not_a_leak(text):
    assert not _hits(text), f"structural scan FALSE-POSITIVED on: {text!r}"


def test_the_octet_guard_rejects_five_parts_not_a_trailing_period():
    """The distinction the first fix got wrong, pinned on its own.

    A dot means "not an address" only when a DIGIT follows it. A sentence-final
    period after a valid four-octet address is still a leak.
    """
    assert _hits("the host is 10.1.4.4.")          # sentence end — still a leak
    assert not _hits("§10.1.4.4.2")                # five parts — a citation


# --------------------------------------------------------------- the CLI's own contract
# Everything above tests the patterns. These test the two ways the tool can report a pass
# about something it did not actually check — which is worse than a wrong pattern, because
# a wrong pattern shows up as a finding and this shows up as silence.

def _run(*args, cwd=_ROOT):
    return subprocess.run([sys.executable, os.path.join(_ROOT, "tools", "leak_scan.py"), *args],
                          cwd=cwd, capture_output=True, text=True)


def test_an_unrecognised_option_is_refused_not_ignored():
    """`--text=<f>` mistyped is the case that matters: the option parser took the first
    argument NOT starting with "-" as the revision, so a misspelled flag left both the
    revision and the text unset and the tool scanned the working tree — printing "no leaks
    found" about input the caller never named. The release description it was pointed at
    would have shipped unscanned with a green transcript to show for it."""
    for bad in ("--tex=/etc/hosts", "--txt=/etc/hosts", "--verbose"):
        r = _run(bad)
        assert r.returncode == 2, f"{bad} exited {r.returncode}, not 2:\n{r.stdout}"
        assert "no leaks found" not in r.stdout, f"{bad} reported a PASS: {r.stdout!r}"


def test_a_missing_text_file_is_refused_not_read_as_empty():
    r = _run("--text=/nonexistent/description.md")
    assert r.returncode == 2
    assert "no leaks found" not in r.stdout


def test_the_scanner_scans_its_own_prose_for_site_patterns():
    """The self-exemption is per pattern KIND, not per file.

    tools/leak_scan.py and this file must skip the STRUCTURAL patterns — they contain them
    as literal source. They must NOT skip the site-specific ones, which cannot self-match
    because they live outside the repository. A whole-file exemption hid a real leak in this
    module's own docstring for a release: it was describing what a published description had
    disclosed, by repeating it, in the one file guaranteed to escape the scan.
    """
    spec = importlib.util.spec_from_file_location(
        "leak_scan", os.path.join(_ROOT, "tools", "leak_scan.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    assert "tools/leak_scan.py" in mod._SELF and "tests/test_leak_scan.py" in mod._SELF
    # Both are in the scanned file list; exemption happens per pattern, not by omission.
    files = mod._files(None)
    for name in mod._SELF:
        assert name in files, (
            f"{name} is excluded from the file list, so NO pattern reaches it — the "
            "exemption must be per pattern kind (see _SELF)")
