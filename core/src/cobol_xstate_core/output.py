"""Where a run writes, and exactly how - in one place, for both CLIs.

Two front-ends writing JSON "the same way" is a promise nothing keeps on its own: one
gains a trailing newline, or an indent default, or forgets ``encoding=`` and starts
emitting cp1252 on Windows, and their outputs quietly stop being comparable. The writing
contract therefore has one implementation.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional


def run_dir(outdir) -> Path:
    """The one directory a run writes into: exactly ``--outdir``, as given.

    Everything lands here - the bundle, every companion view, both retrieval reports, the
    retrieved artifacts (under ``deps/``), and the JS runtime. ``--outdir`` is taken
    literally: the path you give is the path files appear in, with nothing appended.
    There is deliberately no second mechanism that can place a file somewhere else.
    """
    return Path(outdir)


def make_run_dir(path: Path) -> Optional[str]:
    """Create the run directory, or return the message explaining why we cannot.

    ``exist_ok=True`` only forgives an existing DIRECTORY, so pointing ``--outdir`` at an
    existing regular file raised FileExistsError out of main() as a raw traceback - while
    the neighbouring bad-path cases all report cleanly and exit 2.
    """
    try:
        path.mkdir(parents=True, exist_ok=True)
    except (FileExistsError, NotADirectoryError):
        return f"--outdir {path} exists and is not a directory"
    except OSError as exc:
        return f"cannot create --outdir {path}: {exc}"
    return None


def write_text(path: Path, text: str) -> None:
    """Always UTF-8, explicitly: the platform default (cp1252 on Windows) cannot encode
    the runtime's non-ASCII text, and JSON/JS artifacts must be portable."""
    path.write_text(text, encoding="utf-8")


def write_json(path: Path, obj, indent: int = 2) -> None:
    """One JSON document, with the trailing newline every artifact of this tool carries."""
    write_text(path, json.dumps(obj, indent=indent) + "\n")
