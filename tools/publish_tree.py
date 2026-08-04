#!/usr/bin/env python3
"""Build and VERIFY the tree that gets published.

    python3 tools/publish_tree.py              # from the working tree
    python3 tools/publish_tree.py <sha>        # from a specific commit
    python3 tools/publish_tree.py --keep       # leave the built tree on disk to inspect
    python3 tools/publish_tree.py --upstream=<ref>   # default public/main

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
  4. the leak scan passes on the ASSEMBLED tree             (what ships is what is scanned)
  5. the test suite passes on the ASSEMBLED tree            (nothing load-bearing was pruned)

Check 5 is the one that cannot be replaced by reading a manifest: docs are safe to drop,
but a fixture or module is not, and only running the suite proves which is which.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile

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


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    rev = args[0] if args else None
    keep = "--keep" in sys.argv
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

    # 2. no dangling references to the pruned tier. The two files that ENFORCE the rule
    # necessarily contain the path, exactly as the leak scanner and the reference-model guard
    # do — self-exclusion, not an allowlist for real violations.
    _SELF = {"tests/test_publish_tiers.py", "tools/publish_tree.py"}
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

    # 4. leak scan on what actually ships
    print("  leak scan on the assembled tree ...")
    scan = subprocess.run([sys.executable, os.path.join("tools", "leak_scan.py")],
                          cwd=dest, capture_output=True, text=True,
                          env=dict(os.environ))
    if scan.returncode != 0:
        failures.append("leak scan failed on the assembled tree")
        print(scan.stdout[-2000:])

    # 5. the suite passes on what actually ships — the check a manifest cannot give you
    print("  test suite on the assembled tree (this is the slow one) ...")
    env = dict(os.environ, PYTHONPATH=".", QT_QPA_PLATFORM="offscreen",
               PYQTGRAPH_QT_LIB="PySide6", PYTHONUTF8="1")
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
