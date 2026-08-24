"""Telling JCL from COBOL, for whichever front-end has to ask.

The COBOL command uses it to hand a job over rather than trying to parse it as a program;
the JCL command uses it to say something useful when handed a program. Same question,
one answer.
"""

from __future__ import annotations

import re

_JOB_OR_PROC = re.compile(r"^//\S*\s+(JOB|PROC)\b", re.I)
_JCL_SUFFIXES = ("jcl", "prc", "proc")


def looks_like_jcl(source_name: str, source: str) -> bool:
    """JCL by extension, or by a leading ``//NAME JOB/PROC`` statement.

    A COBOL source never begins that way, so this does not misfire on COBOL.
    """
    if source_name.lower().rsplit(".", 1)[-1] in _JCL_SUFFIXES:
        return True
    for line in source.splitlines():
        s = line.strip()
        if not s or s.startswith("//*"):
            continue
        return bool(_JOB_OR_PROC.match(s))
    return False
