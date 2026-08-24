#!/usr/bin/env python3
"""Byte-stability ratchet over what THIS repo owns: the parse bundle.

The parse bundle (``cobol-parse prog.cbl -o prog.parse.json``) is the one artifact these
two distributions produce - the serialized ``Program`` any consumer models from. It is
deliberately timestamp-free, so parsing the same source twice must produce identical
bytes, and a refactor of the front-end (normalizer, preprocessor, lexer, parser, data
division) that should not change the parse must not change one byte of the bundle. The
downstream views are ratcheted where they are produced: cobol-xstate-json's
``tools/gate.py`` hashes every view of every example there, including through the
parse-bundle round trip, against ITS goldens.

What is hashed is the EXACT TEXT a ``cobol-parse <example> --no-fetch`` run writes -
this driver calls the real CLI, not a reconstruction of it. ``--no-fetch`` keeps the
ratchet estate-free: every example resolves its copybooks from examples/ alone (the
directory beside the source is on the search path by default), so the hashes cannot
depend on what an estate happened to answer.

ONE NORMALIZATION, stated plainly: a copybook resolved from disk carries the ``source``
path it was read from, so a bundle for a program that COPYs a member is
machine-dependent before this tool touches it. The examples directory and the repo root
are replaced with stable tokens before hashing; nothing else is normalized.

Note on line endings: core.autocrlf converts on checkout, so a fresh clone may carry
CRLF example files where the recording working tree had LF - which changes the source
text the bundle records, and with it every hash. The goldens are recorded from the
working tree.

Usage:
    python tools/byteproof.py --record goldens/parse.sha256
    python tools/byteproof.py --check  goldens/parse.sha256
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Dict, List

REPO = Path(__file__).resolve().parents[1]
EXAMPLES = REPO / "examples"

for _tree in ("core/src", "parser/src"):
    sys.path.insert(0, str(REPO / _tree))

from cobol_parse.cli import run as cobol_parse_run                    # noqa: E402


def normalize(text: str) -> str:
    """Replace this checkout's paths with stable tokens before hashing.

    Most specific root first: the examples directory sits under the repo root, so
    replacing the repo root first would leave its tail unnormalized.
    """
    for root, token in ((EXAMPLES, "<EXAMPLES>"), (REPO, "<REPO>")):
        for form in (str(root), str(root).replace("\\", "/"),
                     str(root).replace("\\", "\\\\")):
            text = text.replace(form, token)
    return text


def digest(text: str) -> str:
    return hashlib.sha256(normalize(text).encode("utf-8")).hexdigest()


def build_manifest() -> Dict[str, str]:
    """key -> sha256, over every example. Sorted so the file is diff-friendly and the
    ordering cannot depend on the filesystem."""
    out: Dict[str, str] = {}
    tmp = Path(tempfile.mkdtemp(prefix="byteproof-parse-"))
    try:
        for src in sorted(EXAMPLES.glob("*.cbl")):
            bundle = tmp / (src.stem + ".parse.json")
            rc = cobol_parse_run([str(src), "-o", str(bundle), "--no-fetch", "-q"])
            if rc != 0:
                # A CLI failure is a result, not an absence: record it so --check fails
                # loudly rather than the bundle silently vanishing from the manifest.
                out[f"{src.name}::parse"] = digest(f"CLI-FAILED: rc {rc}\n")
                continue
            out[f"{src.name}::parse"] = digest(bundle.read_text(encoding="utf-8"))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return dict(sorted(out.items()))


def dump(path: Path, manifest: Dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(f"{sha}  {key}" for key, sha in manifest.items()) + "\n",
                    encoding="utf-8")


def load(path: Path) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            sha, _, key = line.partition("  ")
            out[key] = sha
    return out


def compare(before: Dict[str, str], after: Dict[str, str]) -> List[str]:
    problems = []
    for key in sorted(set(before) - set(after)):
        problems.append(f"MISSING  {key} (was in the goldens, not produced now)")
    for key in sorted(set(after) - set(before)):
        problems.append(f"ADDED    {key} (produced now, not in the goldens)")
    for key in sorted(set(before) & set(after)):
        if before[key] != after[key]:
            problems.append(f"CHANGED  {key}\n           golden {before[key]}\n"
                            f"           now    {after[key]}")
    return problems


def main() -> int:
    ap = argparse.ArgumentParser(description="byte-stability ratchet over the parse "
                                             "bundle of every example")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--record", metavar="FILE", help="write the goldens")
    g.add_argument("--check", metavar="FILE", help="compare against the goldens")
    args = ap.parse_args()

    manifest = build_manifest()

    if args.record:
        dump(Path(args.record), manifest)
        print(f"recorded {len(manifest)} parse-bundle digests -> {args.record}")
        return 0

    path = Path(args.check)
    if not path.exists():
        print(f"error: no goldens at {path} - run --record first", file=sys.stderr)
        return 2
    problems = compare(load(path), manifest)
    if problems:
        print(f"BYTE-STABILITY FAILURE: {len(problems)} difference(s)\n",
              file=sys.stderr)
        for p in problems:
            print(f"  {p}", file=sys.stderr)
        return 1
    print(f"byte-stable: {len(manifest)} parse-bundle digests match {args.check}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
