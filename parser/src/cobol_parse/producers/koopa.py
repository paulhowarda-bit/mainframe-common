"""Run the Koopa COBOL parser (BSD, Java) over OUR pre-expanded stream.

Koopa (https://github.com/krisds/koopa) is the external second producer chosen for the
dual-producer contract: an island-grammar parser with real sub-grammars for embedded
CICS and SQL, an XML concrete-syntax-tree dump, and per-node line/column positioning.

**Our preprocessor stays the provenance owner.** Koopa is fed the ALREADY-EXPANDED
stream (COPY resolved, comments stripped, continuation literals stitched) and is run
WITHOUT its own ``--preprocess`` - so every ``from-line`` in its CST indexes 1:1 into
the ``CodeLine`` list, whose entries carry the original line number AND the copybook
member each line came from. That sidesteps the provenance loss that disqualified every
other external parser (and Koopa's own XML serializer omitting ``resourceName``): the
external parser is judged purely on statement-grammar coverage, which is the thing it
is here to provide.

Java is an OPTIONAL, separate-process dependency: nothing in the default parse path
imports this module, and everything here degrades to a clear
:class:`KoopaUnavailable` naming what is missing. The jar is never vendored - point
``--koopa-jar`` or ``COBOL_PARSE_KOOPA_JAR`` at a release jar.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from ..errors import ParseError
from ..normalizer import CodeLine

#: Environment variable naming the Koopa release jar (e.g. koopa-20260516.jar).
JAR_ENV = "COBOL_PARSE_KOOPA_JAR"


class KoopaError(ParseError):
    """The Koopa run itself failed (bad jar, non-zero exit, unreadable XML)."""


class KoopaUnavailable(KoopaError):
    """Java or the Koopa jar is not available; the message names which and how to
    supply it."""


def find_koopa_jar(explicit: Optional[str] = None) -> str:
    """The jar to run, or a refusal that says exactly what to set."""
    for candidate, why in ((explicit, "--koopa-jar"),
                           (os.environ.get(JAR_ENV), JAR_ENV)):
        if candidate:
            p = Path(candidate)
            if not p.is_file():
                raise KoopaUnavailable(f"{why} points at {p}, which does not exist")
            return str(p)
    raise KoopaUnavailable(
        f"no Koopa jar configured: pass --koopa-jar or set {JAR_ENV} to a release "
        f"jar from https://github.com/krisds/koopa/releases (BSD licensed)")


def require_java() -> str:
    java = shutil.which("java")
    if not java:
        raise KoopaUnavailable("no `java` on PATH - Koopa is a Java parser run as a "
                               "separate process; install a JRE/JDK (11+)")
    return java


def render_free(lines: List[CodeLine]) -> str:
    """The expanded stream as free-format text Koopa can read.

    Free format on purpose, though the original was almost certainly fixed: our
    normalizer already consumed the columns (each CodeLine is one clean logical line -
    comments gone, continuation literals stitched), and a stitched literal or a deep
    statement can exceed column 72, which a fixed-format re-render would silently
    truncate mid-token. Free format has no right margin, so the whole class of
    failure is gone; the gating experiment showed both renders produce the same CST
    (same rule tags, same counts) wherever both are possible."""
    return "\n".join(cl.text for cl in lines) + "\n"


def run_koopa(lines: List[CodeLine], jar: Optional[str] = None) -> ET.Element:
    """Render, run ToXml (positioning on, NO --preprocess), return the CST root."""
    jar_path = find_koopa_jar(jar)
    java = require_java()
    with tempfile.TemporaryDirectory(prefix="cobol-parse-koopa-") as td:
        src = Path(td) / "expanded.cbl"
        dst = Path(td) / "expanded.xml"
        src.write_text(render_free(lines), encoding="utf-8")
        proc = subprocess.run(
            [java, "-Dkoopa.xml.include_positioning=true", "-cp", jar_path,
             "koopa.app.cli.ToXml", "--free-format", str(src), str(dst)],
            capture_output=True, text=True, timeout=300)
        if proc.returncode != 0 or not dst.is_file():
            detail = (proc.stderr or proc.stdout).strip().splitlines()
            raise KoopaError(f"koopa.app.cli.ToXml failed (rc={proc.returncode}): "
                             f"{detail[-1][:200] if detail else 'no output'}")
        try:
            return ET.parse(dst).getroot()
        except ET.ParseError as exc:
            raise KoopaError(f"Koopa wrote unreadable XML: {exc}") from None


def _node_text(el: ET.Element, limit: int = 80) -> str:
    return " ".join(t.text for t in el.iter("t") if t.text)[:limit]


def _statement_kind(st: ET.Element) -> str:
    """A `statement` node's one non-token child names the verb's grammar rule."""
    for child in st:
        if child.tag != "t":
            return child.tag
    return "unknown"


def koopa_statements(root: ET.Element, lines: List[CodeLine]
                     ) -> List[Dict[str, object]]:
    """Every statement Koopa typed, keyed back to (origin member, original line).

    ``from-line`` is 1-based into the rendered stream, which is index+1 into
    ``lines`` - the join that makes our provenance the CST's provenance."""
    out = []
    for st in root.iter("statement"):
        fl = st.get("from-line")
        if fl is None:
            continue
        cl = lines[int(fl) - 1]
        out.append({
            "kind": _statement_kind(st),
            "renderedLine": int(fl),
            "line": cl.line,
            "member": cl.origin,
            "text": _node_text(st),
        })
    return out


def koopa_water(root: ET.Element, lines: List[CodeLine]) -> List[Dict[str, object]]:
    """What the island grammar SKIPPED - Koopa's own honest coverage signal."""
    out = []
    for w in root.iter("water"):
        fl = w.get("from-line")
        cl = lines[int(fl) - 1] if fl else None
        out.append({
            "renderedLine": int(fl) if fl else None,
            "line": cl.line if cl else None,
            "member": cl.origin if cl else None,
            "text": _node_text(w),
        })
    return out


# --------------------------------------------------------------- the coverage diff

def diff_producers(program, root: ET.Element, lines: List[CodeLine]) -> dict:
    """Native parse vs Koopa CST, joined per (member, line): the report that decides
    whether a full Koopa producer is worth building - or where the native parser
    should grow next.

    Reading it honestly requires one fact: a native ``Action`` is OPAQUE BY DESIGN for
    straight-line data verbs (MOVE, ADD, DISPLAY... - their data effects are recovered
    separately by the semantics layer), so "native says Action, Koopa says
    moveStatement" is agreement, not a gap. The gap signals, strongest first:

    ``parseErrorParagraphs``  a paragraph the native parser gave up on wholesale,
                              with every statement Koopa recovered inside it
    ``nativeMissed``          lines where Koopa typed a statement and the native
                              parse has nothing at all
    ``koopaMissed``           lines the native parse typed and Koopa skipped (water)
                              or does not cover - the reverse check keeps this a
                              comparison, not a sales pitch
    ``disagreements``         both typed, kinds that do not correspond
    """
    from ..model import Action, walk_statements

    native: Dict[Tuple[Optional[str], int], List[object]] = {}
    for para in list(program.paragraphs) + list(program.declaratives):
        for st in walk_statements(para.statements):
            native.setdefault((para.origin, st.line), []).append(st)
    # walk_statements does not descend into HandledStmt.inner's own line because inner
    # shares the handler's line; the per-line join absorbs that.

    k_statements = koopa_statements(root, lines)
    koopa_by_line: Dict[Tuple[Optional[str], int], List[dict]] = {}
    for row in k_statements:
        koopa_by_line.setdefault((row["member"], row["line"]), []).append(row)

    error_paras = []
    for para in list(program.paragraphs) + list(program.declaratives):
        if para.parse_error:
            span = [row for row in k_statements
                    if row["member"] == para.origin and row["line"] >= para.line]
            error_paras.append({
                "paragraph": para.name,
                "line": para.line,
                "parseError": para.parse_error,
                "koopaRecovered": span,
            })

    native_missed = []
    for key, rows in sorted(koopa_by_line.items(),
                            key=lambda kv: (kv[0][0] or "", kv[0][1])):
        if key not in native:
            member, line = key
            native_missed.append({"member": member, "line": line,
                                  "koopa": [r["kind"] for r in rows],
                                  "text": rows[0]["text"]})

    koopa_missed = []
    for key, stmts in sorted(native.items(),
                             key=lambda kv: (kv[0][0] or "", kv[0][1])):
        if key not in koopa_by_line:
            member, line = key
            koopa_missed.append({"member": member, "line": line,
                                 "native": [type(s).__name__ for s in stmts]})

    agree_opaque = agree_typed = 0
    disagreements = []
    for key in sorted(set(native) & set(koopa_by_line),
                      key=lambda k: (k[0] or "", k[1])):
        n_kinds = {type(s).__name__ for s in native[key]}
        k_kinds = {r["kind"] for r in koopa_by_line[key]}
        if n_kinds == {"Action"}:
            agree_opaque += 1
        elif n_kinds - {"Action"}:
            agree_typed += 1
        # A finer kind-to-kind correspondence table can grow here; per-line presence
        # is the v1 signal, so structural disagreement is only recorded when one side
        # is empty - which the two *Missed lists already carry.
    _ = disagreements  # reserved in the report shape for the finer comparison

    return {
        "format": "cobol-parse-producer-diff",
        "version": 1,
        "producers": {"native": "cobol-parse-python", "external": "koopa"},
        "totals": {
            "nativeStatements": sum(len(v) for v in native.values()),
            "koopaStatements": len(k_statements),
            "linesBothTyped": agree_typed,
            "linesAgreeOpaqueByDesign": agree_opaque,
            "koopaWater": len(koopa_water(root, lines)),
        },
        "parseErrorParagraphs": error_paras,
        "nativeMissed": native_missed,
        "koopaMissed": koopa_missed,
        "koopaWater": koopa_water(root, lines),
        "note": ("a native Action is opaque BY DESIGN for straight-line data verbs "
                 "(their data effects are recovered by the semantics layer); the gap "
                 "signals are parseErrorParagraphs and nativeMissed, and koopaMissed "
                 "is the reverse check"),
    }
