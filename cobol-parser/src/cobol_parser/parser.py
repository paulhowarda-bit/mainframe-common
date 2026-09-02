"""Stage 3 - recover PROCEDURE DIVISION structure.

Two passes:

1. Split the PROCEDURE DIVISION into sections/paragraphs using **Area A** header
   detection (a header stands alone in Area A as ``NAME.`` or ``NAME SECTION.``).
   Doing this at the line/area level, not the token level, is what makes header
   detection reliable (see references/parsing-cobol.md, Stage 1 + Stage 5).

2. Parse each paragraph body into a control-flow statement AST (model.py) with a
   small recursive-descent parser that understands the block-structured verbs
   (IF / EVALUATE / PERFORM / READ-WRITE handlers) and folds everything else into
   opaque ``Action`` nodes.

This is a heuristic control-flow recovery, not a conformant COBOL parser: it has no
copybook preprocessor, no embedded-SQL/CICS extraction, and no full grammar. It is
honest about that - constructs it cannot resolve statically are surfaced as flags by
the statechart stage, never silently smoothed.
"""

from __future__ import annotations

import logging
import re
from typing import Callable, Dict, List, Optional, Set, Tuple

from .normalizer import CodeLine, SourceFormat, detect_source_format, normalize
from .lexer import Token, tokenize
from .data_division import elementary_subordinates, parse_data_division
from .textutil import mask_literals
from .preprocessor import CopybookResolver, preprocess
from .model import (
    Action,
    AlterStmt,
    CallStmt,
    ContinueStmt,
    EvaluateStmt,
    ExecStmt,
    ExitStmt,
    GoToStmt,
    HandledStmt,
    IfStmt,
    IoStmt,
    Paragraph,
    PerformStmt,
    Program,
    SearchStmt,
    SortStmt,
    Stmt,
    TerminateStmt,
    walk_statements,
)

logger = logging.getLogger(__name__)

# Verbs that begin a statement. Used to bound opaque actions and conditions.
ACTION_VERBS: Set[str] = {
    "MOVE", "ADD", "SUBTRACT", "MULTIPLY", "DIVIDE", "COMPUTE", "OPEN", "CLOSE",
    "DISPLAY", "ACCEPT", "SET", "INITIALIZE", "RELEASE", "SORT", "MERGE", "CANCEL",
    "INVOKE", "UNLOCK", "STRING", "UNSTRING", "INSPECT", "SEARCH", "ALLOCATE", "FREE",
}
CONTROL_VERBS: Set[str] = {
    "IF", "EVALUATE", "PERFORM", "GO", "READ", "WRITE", "REWRITE", "DELETE", "START",
    "RETURN", "CALL", "ALTER", "EXIT", "CONTINUE", "NEXT", "STOP", "GOBACK", "EXEC",
}
STARTERS: Set[str] = ACTION_VERBS | CONTROL_VERBS

# Verbs consumed opaquely up to their END- terminator (STRING/UNSTRING carry an inner
# ON OVERFLOW clause that the statechart stage flags). SEARCH is parsed structurally
# (its WHEN/AT END branches are real control flow) - see parse_search.
OPAQUE_SCOPED = {"STRING": "END-STRING", "UNSTRING": "END-UNSTRING"}

IO_VERBS = {"READ", "WRITE", "REWRITE", "DELETE", "START", "RETURN"}

# Verbs whose trailing [NOT] ON SIZE ERROR / EXCEPTION phrase guards an imperative:
# the handler body is a conditional branch, captured as a HandledStmt (never hoisted).
_SIZE_VERBS = {"ADD", "SUBTRACT", "MULTIPLY", "DIVIDE", "COMPUTE"}
_EXC_VERBS = {"ACCEPT", "DISPLAY"}
_HANDLED_VERBS = _SIZE_VERBS | _EXC_VERBS

# Clause words that may follow the file/record name inside an I/O statement; NEXT
# collides with the NEXT SENTENCE starter, so it must be recognized as a clause here
# (READ f NEXT RECORD is the standard VSAM browse idiom).
_IO_CLAUSE_WORDS = {"NEXT", "PREVIOUS", "RECORD", "KEY", "WITH", "NO", "LOCK",
                    "KEPT", "WAIT", "ADVANCING", "BEFORE", "AFTER", "PAGE",
                    "LINE", "LINES", "IGNORING"}

_HEADER_RE = re.compile(r"^([A-Z0-9][A-Z0-9-]*)(\s+SECTION)?\s*\.\s*(.*)$", re.I)
_RESERVED_HEADER = STARTERS | {
    "END-IF", "END-PERFORM", "END-EVALUATE", "END-READ", "END-WRITE", "END-CALL",
    "ELSE", "WHEN", "THEN", "DECLARATIVES",
}


# --------------------------------------------------------------------------- #
# Pass 1: program structure
# --------------------------------------------------------------------------- #

def _find_program_id(lines: List[CodeLine]) -> Tuple[str, int]:
    """Return (program-id, source line it was matched on); (\"RECOVERED\", 0) if absent."""
    for cl in lines:
        m = _PROGRAM_ID_RE.search(cl.text)
        if m:
            return m.group(1).upper(), cl.line
    return "RECOVERED", 0


# `(?<!-)`: `\b` alone fires after a hyphen, so `\bPROGRAM-ID\b` matched the tail of a data
# name like `PNET-MQ-PROGRAM-ID` and counted a phantom program. Excluding a preceding hyphen
# drops those false hits and still rejects `XPROGRAM-ID`, exactly as the bare `\b` did.
_PROGRAM_ID_RE = re.compile(r"(?<!-)\bPROGRAM-ID\b\s*\.?\s*([A-Z0-9][A-Z0-9-]*)", re.I)
_END_PROGRAM_RE = re.compile(r"\bEND\s+PROGRAM\b(?:\s+([A-Z0-9][A-Z0-9-]*))?", re.I)


def _split_program_units(lines: List[CodeLine]) -> Tuple[List[CodeLine], List[str]]:
    """Separate the MAIN (first) program's own lines from any CONTAINED programs.

    IBM COBOL allows nested/contained programs: ``PROGRAM-ID. OUTER.`` may contain
    ``PROGRAM-ID. INNER. ... END PROGRAM INNER.`` before its own ``END PROGRAM OUTER.``.
    The whole file is parsed as one program today, so a contained program's DATA/PROCEDURE
    divisions fold into the outer one (corrupting its recovered logic) and a ``CALL 'INNER'``
    looks like a missing external module.

    Returns ``(main_lines, contained_names)``: the outer program's own lines with every
    contained program's body removed, and the names of the contained programs. A source
    with a single ``PROGRAM-ID`` (the overwhelmingly common case, and every existing
    fixture) has no contained programs, so the lines are returned unchanged and the name
    list is empty - this pass is then a no-op and output is byte-identical."""
    n_ids = sum(1 for cl in lines if _PROGRAM_ID_RE.search(cl.text))
    if n_ids <= 1:
        return lines, []

    main_lines: List[CodeLine] = []
    contained: List[str] = []
    depth = 0                       # 0 = outside; 1 = in the main program; >=2 = contained
    for cl in lines:
        mid = _PROGRAM_ID_RE.search(cl.text)
        if mid:
            depth += 1
            if depth == 1:
                main_lines.append(cl)           # the main program's own PROGRAM-ID
            else:
                contained.append(mid.group(1).upper())   # a contained program - excluded
                # Its (optional) IDENTIFICATION DIVISION header sits on the line just
                # before its PROGRAM-ID and was appended to the main program above -
                # drop it too, so only the outer program's own lines remain.
                if main_lines and re.search(
                        r"\bIDENTIFICATION\s+DIVISION\b", main_lines[-1].text, re.I):
                    main_lines.pop()
            continue
        if _END_PROGRAM_RE.search(cl.text):
            if depth == 1:
                main_lines.append(cl)           # the main program's own END PROGRAM
            depth = max(0, depth - 1)
            continue
        if depth <= 1:
            main_lines.append(cl)               # preamble (0) or main-program body (1)
        # depth >= 2: inside a contained program - dropped from the main program's lines
    # A contained program is ALWAYS delimited by END PROGRAM (IBM requires every unit to
    # carry one once any nesting is present). If the walk did not cleanly close (depth != 0),
    # these are NOT well-formed nested programs but concatenated separate compilation units
    # (or a nested unit missing its END PROGRAM) - and the loop has just dropped a whole
    # unit's body, taking its CALLs out of the dependency manifest. That silent loss is worse
    # than not splitting, so fall back to the pre-nesting behaviour: one program, nothing
    # removed, no contained names. Well-formed nesting (depth returns to 0) is unaffected.
    if depth != 0:
        return lines, []
    # Preserve first-seen order, drop duplicates deterministically (no set iteration).
    return main_lines, list(dict.fromkeys(contained))


def _rest_line(cl: CodeLine, rest: str) -> CodeLine:
    """A synthetic Area-B line carrying the code that followed a same-line header."""
    return CodeLine(text=rest, line=cl.line, area_a=False, origin=cl.origin)


def _split_header(cl: CodeLine) -> Optional[Tuple[str, bool, str]]:
    """If this line is an Area-A paragraph/section header, return
    ``(name, is_section, rest)`` where ``rest`` is any code following the header
    period on the same line (the legal ``PARA-1. MOVE ...`` style); otherwise None."""
    if not cl.area_a:
        return None
    m = _HEADER_RE.match(cl.text.strip())
    if not m:
        return None
    name = m.group(1).upper()
    if name in _RESERVED_HEADER:
        return None
    return name, bool(m.group(2)), (m.group(3) or "").strip()


_VALUE_RE = re.compile(
    r"^\s*\d+\s+([A-Z0-9][A-Z0-9-]*)\b.*?\bVALUE\b\s+(?:IS\s+)?(['\"])(.*?)\2", re.I)


def _scan_value_clauses(lines: List[CodeLine]) -> dict:
    """Capture `<level> NAME ... VALUE 'lit'` initial values (string literals only)
    from the DATA DIVISION, for constant propagation of CALL targets."""
    out = {}
    for cl in lines:
        m = _VALUE_RE.match(cl.text)
        if m:
            out[m.group(1).upper()] = m.group(3).rstrip()
    return out


def _procedure_lines(lines: List[CodeLine]) -> List[CodeLine]:
    """Return body lines after the PROCEDURE DIVISION header clause (skipping any
    ``USING ...`` continuation up to its terminating period)."""
    start = None
    for i, cl in enumerate(lines):
        if re.search(r"\bPROCEDURE\s+DIVISION\b", cl.text, re.I):
            start = i
            break
    if start is None:
        return []
    # Consume header lines until the one carrying the terminating period.
    j = start
    while j < len(lines) and "." not in lines[j].text:
        j += 1
    return lines[j + 1:]


def _procedure_interface(lines: List[CodeLine]) -> Tuple[List[str], Optional[str]]:
    """Extract the program's own parameter interface from the PROCEDURE DIVISION header:
    ``PROCEDURE DIVISION USING p1 p2 ... [RETURNING r].`` Returns ``(using, returning)``.

    These LINKAGE-backed items are the perimeter at the program's entry point - what the
    caller (COMMAREA / parameter list) passes in and what is returned."""
    start = None
    for i, cl in enumerate(lines):
        if re.search(r"\bPROCEDURE\s+DIVISION\b", cl.text, re.I):
            start = i
            break
    if start is None:
        return [], None
    header = []
    j = start
    while j < len(lines):
        header.append(lines[j].text)
        if "." in lines[j].text:
            break
        j += 1
    text = " ".join(header)
    text = text.split(".", 1)[0]  # header clause only, up to the terminating period
    returning = None
    mret = re.search(r"\bRETURNING\s+([A-Z0-9][A-Z0-9-]*)", text, re.I)
    if mret:
        returning = mret.group(1).upper()
    using: List[str] = []
    mus = re.search(r"\bUSING\b(.*?)(?:\bRETURNING\b|$)", text, re.I)
    if mus:
        for tok in re.split(r"[,\s]+", mus.group(1).strip()):
            u = tok.upper()
            if u and u not in ("BY", "REFERENCE", "CONTENT", "VALUE"):
                using.append(u)
    return using, returning


_SELECT_RE = re.compile(
    r"\bSELECT\s+(?:OPTIONAL\s+)?([A-Z0-9][A-Z0-9-]*)(.*?)(?=\bSELECT\b|$)",
    re.I | re.S)


def _parse_file_control(lines: List[CodeLine]) -> Dict[str, dict]:
    """ENVIRONMENT DIVISION FILE-CONTROL: SELECT clauses binding each logical file to
    its external dataset (ASSIGN TO ddname), organization/access, record key, and -
    crucially for the perimeter - its FILE STATUS field (the VSAM/QSAM analogue of
    SQLCODE: branching on it is reacting to the file subsystem's response)."""
    start = end = None
    for i, cl in enumerate(lines):
        up = cl.text.upper()
        if start is None and "FILE-CONTROL" in up:
            start = i
        elif start is not None and re.search(
                r"\b(?:I-O-CONTROL|DATA\s+DIVISION|CONFIGURATION\s+SECTION)\b", up):
            end = i
            break
    if start is None:
        return {}
    text = " ".join(cl.text for cl in lines[start:end if end is not None else len(lines)])
    files: Dict[str, dict] = {}
    # Clause boundaries found on the MASKED text, bodies sliced from the original: an
    # ASSIGN literal can legally contain the word SELECT (`ASSIGN TO 'PROD.SELECT.DATA'`),
    # and the lookahead used to cut this entry's body there - losing the dataset, the
    # ORGANIZATION, and the FILE STATUS binding that the JCL join and the perimeter need.
    for m in _SELECT_RE.finditer(mask_literals(text)):
        name = m.group(1).upper()
        body = text[m.start(2):m.end(2)]
        entry: Dict[str, object] = {"file": name}
        am = re.search(r"\bASSIGN\s+(?:TO\s+)?([A-Z0-9$#@.-]+|'[^']*'|\"[^\"]*\")",
                       body, re.I)
        if am:
            raw = am.group(1)
            if raw[:1] in ("'", '"'):
                assign = raw.strip("'\"")          # a literal keeps any dots it has
            else:
                # An unquoted operand ends at the sentence period; a ddname never
                # contains one. Keeping it would produce "CNTLDD." - a name that
                # matches no //DD statement, silently breaking the JCL join.
                assign = raw.rstrip(".")
            entry["assign"] = assign.upper()
        om = re.search(r"\bORGANIZATION\s+(?:IS\s+)?([A-Z-]+)", body, re.I)
        if om:
            entry["organization"] = om.group(1).upper()
        acm = re.search(r"\bACCESS\s+(?:MODE\s+)?(?:IS\s+)?([A-Z-]+)", body, re.I)
        if acm:
            entry["access"] = acm.group(1).upper()
        km = re.search(r"\bRECORD\s+KEY\s+(?:IS\s+)?([A-Z0-9-]+)", body, re.I)
        if km:
            entry["recordKey"] = km.group(1).upper()
        sm = re.search(r"\b(?:FILE\s+)?STATUS\s+(?:IS\s+)?([A-Z0-9-]+)", body, re.I)
        if sm:
            entry["statusField"] = sm.group(1).upper()
        files[name] = entry
    return files


def parse_program(source: str, fmt: Optional[SourceFormat] = None,
                  resolver: Optional[CopybookResolver] = None) -> Program:
    if fmt is None:
        fmt = detect_source_format(source).format
    lines = normalize(source, fmt)
    pre = preprocess(lines, resolver, fmt=fmt)
    lines = pre.lines
    # Separate any CONTAINED (nested) programs so their bodies do not fold into the outer
    # program, and so a CALL to one is recognised as internal rather than a missing module.
    lines, contained = _split_program_units(lines)
    program_id, program_id_line = _find_program_id(lines)
    prog = Program(program_id=program_id, program_id_line=program_id_line)
    prog.nested_programs = contained
    prog.copybooks = pre.copybooks
    if contained:
        prog.notes.append(
            "Contained (nested) program(s): " + ", ".join(contained)
            + " - parsed out of the main program; a CALL to one is an internal call")
    if pre.expanded:
        prog.notes.append("Expanded copybooks: " + ", ".join(sorted(set(pre.expanded))))
    for member in sorted(set(pre.missing)):
        prog.notes.append(
            f"COPY {member}: not found - data/logic it defines is missing from the model")
    prog.notes.extend(n for n in pre.notes if "not found" not in n and "recursive" in n)

    if any(re.search(r"\bPROCEDURE\s+DIVISION\b", cl.text, re.I) for cl in lines):
        prog.has_procedure_division = True
        prog.using, prog.returning = _procedure_interface(lines)
    if any(re.search(r"\bDECLARATIVES\b", cl.text, re.I) for cl in lines):
        prog.notes.append(
            "DECLARATIVES present: USE-procedure error handlers form an implicit "
            "orthogonal region; recovered chart does not model the implicit transfer."
        )

    prog.working_values = _scan_value_clauses(lines)
    prog.data_items, prog.data_by_name = parse_data_division(lines)
    expand = _structure_expander(prog.data_items)
    prog.files = _parse_file_control(lines)
    prog.sql_cursors, prog.declared_tables = _scan_sql_declarations(lines)

    body = _procedure_lines(lines)
    if not body:
        return prog

    # DECLARATIVES ... END DECLARATIVES is an orthogonal error-handler region, not part of
    # the main sequential flow - split it out so its USE sections don't pollute it.
    decl_lines, main_lines = _split_declaratives(body)
    prog.paragraphs = _group_paragraphs(main_lines, expand)
    if decl_lines:
        prog.declaratives = _mark_use_handlers(_group_paragraphs(decl_lines, expand))
    _qualify_duplicate_paragraphs(prog.paragraphs + prog.declaratives)

    # Collect CICS HANDLE CONDITION registrations across all statements.
    prog.cics_handlers = _collect_cics_handlers(prog.paragraphs + prog.declaratives)
    return prog


def _structure_expander(items) -> Callable[[str], Optional[List[str]]]:
    """A memoized ``name -> its elementary items, or None`` for the statement parser.

    A CALLABLE rather than the item list: the statement parser has no business knowing
    what a DataItem is, and `None` (its default) is simply "no data division was
    supplied", which keeps `StmtParser` constructible on its own. Memoized because
    :func:`elementary_subordinates` is a linear scan of the whole data division, and a
    copybook-heavy program does hundreds of SQL statements against the same few DCLGEN
    groups - repeating the scan per reference is quadratic for no answer that changes.
    """
    cache: Dict[str, Optional[List[str]]] = {}

    def expand(name: str) -> Optional[List[str]]:
        key = (name or "").upper()
        if key not in cache:
            cache[key] = elementary_subordinates(items, key)
        return cache[key]

    return expand


def _scan_sql_declarations(lines) -> Tuple[List[dict], List[dict]]:
    """Every SQL cursor DECLARE and DECLARE TABLE in the WHOLE expanded stream.

    The statement parser walks only the PROCEDURE DIVISION, but production code keeps
    both declarations in WORKING-STORAGE - a DCLGEN arrives as `EXEC SQL INCLUDE`
    carrying `DECLARE t TABLE (...)`, and cursor DECLAREs sit beside it in copybooks.
    Declared anywhere, they are program-wide facts: this scan runs over the full
    origin-tagged line stream so a FETCH can find its cursor's columns and a
    column-list-less INSERT its table's declared order, wherever the DECLARE lives.

    Returns ``(cursors, tables)``:
      cursors: [{cursor, selectList, selectDerivations, table, forKind,
                 forStatement, line, member}]
               forKind: "select" | "statement" | None (None = UNKNOWN)
      tables:  [{table, columns, line, member}]
    """
    cursors: List[dict] = []
    tables: List[dict] = []
    toks = tokenize(lines)
    i = 0
    while i < len(toks):
        t = toks[i]
        if not (t.kind == "word" and t.up == "EXEC" and i + 1 < len(toks)
                and toks[i + 1].kind == "word" and toks[i + 1].up == "SQL"):
            i += 1
            continue
        j = i + 2
        block: List[Token] = []
        while j < len(toks) and not (toks[j].kind == "word"
                                     and toks[j].up == "END-EXEC"):
            block.append(toks[j])
            j += 1
        i = j + 1
        words = [b for b in block if b.kind == "word"]
        if not words or words[0].up != "DECLARE":
            continue
        head = block[0]
        cursor = StmtParser._exec_declare_cursor(block)
        if cursor:
            select_list, derivations, _note =                 StmtParser._exec_select_columns(block)
            for_kind, for_stmt = StmtParser._exec_declare_for(block)
            cursors.append({"cursor": cursor, "selectList": select_list,
                            "selectDerivations": derivations,
                            "table": _declare_from_table(block),
                            "forKind": for_kind, "forStatement": for_stmt,
                            "line": head.line, "member": head.origin})
            continue
        entry = _declare_table_columns(block)
        if entry:
            entry.update({"line": head.line, "member": head.origin})
            tables.append(entry)
    return cursors, tables


# Words that can legally follow FROM and are NOT the table: a data-change table
# reference (`FROM FINAL TABLE (INSERT ...)`, and its OLD/NEW forms) and a table
# function (`FROM TABLE (f(...))`). Taking one of these as the table name mints a
# shared anchor identity for a KEYWORD, which is worse than admitting the name is
# unknown - a consumer can handle "unknown", but not a table called FINAL, which
# two programs would then MERGE onto as if it were the same one.
_NOT_A_TABLE = frozenset({"FINAL", "OLD", "NEW", "TABLE", "XMLTABLE", "LATERAL",
                          "UNNEST", "SELECT"})


def _declare_from_table(block: List[Token]) -> Optional[str]:
    """The FROM table of a cursor DECLARE's select, at paren depth 0 (a literal in the
    select list is a single token here, so it cannot supply a phantom FROM)."""
    depth = 0
    for i, t in enumerate(block):
        if t.kind == "punct" and t.text == "(":
            depth += 1
        elif t.kind == "punct" and t.text == ")":
            depth -= 1
        elif depth == 0 and t.kind == "word" and t.up == "FROM":
            return StmtParser._table_name(block[i + 1:])
    return None


def _declare_table_columns(block: List[Token]) -> Optional[dict]:
    """`DECLARE t TABLE (c1 type, c2 type, ...)` -> {table, columns}, else None.

    This is the DCLGEN's own statement of the table's declared column ORDER - the
    fact a column-list-less INSERT needs, stated in the source rather than fetched
    from the catalog.
    """
    i_table = next((i for i, t in enumerate(block)
                    if t.kind == "word" and t.up == "TABLE"), None)
    if i_table is None or i_table == 0:
        return None
    name = StmtParser._table_name(block[1:i_table])
    if not name:
        return None
    inner = StmtParser._paren_group(block, i_table + 1)
    if not inner:
        return None
    columns: List[str] = []
    for item in StmtParser._split_top_commas(inner):
        col = next((t.up for t in item if t.kind == "word"), None)
        if col is None:
            return None      # not a column-definition list after all
        columns.append(col)
    return {"table": name, "columns": columns}


# Fields naming a PARAGRAPH (never a data item or a program), by statement type. A
# `str` field holds one name, a `List[str]` field holds several.
_PARA_REF_FIELDS = {
    "PerformStmt": ("target", "thru"),
    "GoToStmt": ("targets",),
    "SortStmt": ("input_proc", "input_thru", "output_proc", "output_thru"),
}


def _qualify_duplicate_paragraphs(paras: List[Paragraph]) -> None:
    """Make paragraph names unique, in place, when the same one is used in two SECTIONs.

    COBOL requires a paragraph name to be unique only WITHIN its section, and the
    `COMMON-EXIT.` per-section-exit idiom leans on that. The statechart uses the
    paragraph name as the state id, so the second definition simply overwrote the first:
    one state, carrying the LAST body, and every PERFORM of that name - from either
    section - ran it. Section A's exit routine executed section B's, with no flag, and
    the fall-through chain merged there too.

    Each clashing definition becomes `NAME_OF_SECTION`, and references are re-pointed at
    the definition in the REFERRING paragraph's own section, which is how COBOL resolves
    an unqualified reference. A reference from outside any section that owns the name is
    genuinely ambiguous - COBOL rejects it - so it is left alone for the statechart's
    unknown-target check to flag rather than being silently pointed at a guess.
    """
    counts: Dict[str, int] = {}
    for p in paras:
        counts[p.name] = counts.get(p.name, 0) + 1
    dupes = {n for n, c in counts.items() if c > 1}
    if not dupes:
        return

    taken = {p.name for p in paras}
    # bare name -> {section: unique id}
    resolve: Dict[str, Dict[Optional[str], str]] = {}
    for p in paras:
        if p.name not in dupes:
            continue
        bare = p.name
        # `_OF_` rather than `__OF__`: a double underscore is how a structural state id
        # separates a paragraph from its suffix, so `_para_of` would read it back as a
        # different paragraph entirely.
        qualified = f"{bare}_OF_{p.section}" if p.section else bare
        while qualified in taken and qualified != bare:
            qualified += "_"
        taken.add(qualified)
        p.bare_name = bare
        p.name = qualified
        resolve.setdefault(bare, {})[p.section] = qualified

    for p in paras:
        for st in walk_statements(p.statements):
            for f in _PARA_REF_FIELDS.get(type(st).__name__, ()):
                v = getattr(st, f, None)
                if isinstance(v, str):
                    setattr(st, f, resolve.get(v, {}).get(p.section, v))
                elif isinstance(v, list):
                    setattr(st, f, [resolve.get(t, {}).get(p.section, t) for t in v])
            if isinstance(st, AlterStmt):
                st.pairs = [(resolve.get(a, {}).get(p.section, a),
                             resolve.get(b, {}).get(p.section, b)) for a, b in st.pairs]


def _split_declaratives(body: List[CodeLine]):
    """Return (declaratives_lines, main_lines), splitting at DECLARATIVES/END DECLARATIVES."""
    start = end = None
    for i, cl in enumerate(body):
        u = cl.text.strip().upper()
        if start is None and re.match(r"^DECLARATIVES\s*\.?$", u):
            start = i
        elif re.match(r"^END\s+DECLARATIVES\s*\.?$", u):
            end = i
            break
    if start is None or end is None:
        return [], body
    return body[start + 1:end], body[:start] + body[end + 1:]


def _group_paragraphs(body: List[CodeLine],
                      expand: Optional[Callable[[str], Optional[List[str]]]] = None
                      ) -> List[Paragraph]:
    """Group body lines into paragraphs by Area-A headers and parse each one's statements.

    ``expand`` resolves a group-level host variable to its elementary items (see
    :func:`_structure_expander`); without one, host variables are kept as written."""
    if not body:
        return []
    current = Paragraph(name="_ENTRY_", line=body[0].line)
    section: Optional[str] = None
    buckets: List[Paragraph] = [current]
    bucket_lines: List[List[CodeLine]] = [[]]
    # A paragraph/section header can only begin at a sentence boundary - after the
    # previous sentence's terminating period. Free format has no Area A, so the
    # normalizer marks EVERY line an Area-A candidate; without this gate a statement
    # continued onto a line that is just `NAME.` (a bare data-name plus period) matched
    # the header shape and became a phantom paragraph, stealing the rest of the real one.
    # (In fixed format the header is already column-gated, and a real header always sits
    # at a boundary, so the gate is a safe no-op there.)
    at_boundary = True
    for cl in body:
        split = _split_header(cl) if at_boundary else None
        if split is not None:
            name, is_section, rest = split
            section = name if is_section else section
            current = Paragraph(name=name, line=cl.line,
                                section=None if is_section else section,
                                is_section=is_section,
                                origin=cl.origin)
            buckets.append(current)
            bucket_lines.append([])
            if rest:  # PARA-1. MOVE ... - code on the header line belongs to the body
                bucket_lines[-1].append(_rest_line(cl, rest))
            at_boundary = (not rest) or rest.rstrip().endswith(".")
        else:
            bucket_lines[-1].append(cl)
            at_boundary = cl.text.rstrip().endswith(".")

    for para, plines in zip(buckets, bucket_lines):
        # Robustness at scale: an unparseable paragraph must not abort the whole program
        # (or a batch of thousands). On failure, fall back to one opaque action carrying
        # the raw text and mark the paragraph so the statechart stage can flag it.
        try:
            para.statements = StmtParser(tokenize(plines), expand).parse_paragraph()
        except Exception as exc:  # noqa: BLE001 - deliberate catch-all for corpus safety
            raw = " ".join(cl.text.strip() for cl in plines).strip()
            para.statements = [Action(line=para.line, text=raw[:200], verb="?")]
            para.parse_error = f"{type(exc).__name__}: {exc}"
            # The one-line parse_error above rides on the paragraph (and into the output);
            # the full traceback is only recoverable here, so keep it at DEBUG (-v).
            logger.debug("paragraph %r failed to parse; opaque fallback used",
                         para.name, exc_info=True)

    if buckets and buckets[0].name == "_ENTRY_" and not buckets[0].statements:
        buckets.pop(0)
    return buckets


_USE_RE = re.compile(
    r"\bUSE\b\s+(?:GLOBAL\s+)?(?:AFTER\s+)?(?:STANDARD\s+)?"
    r"(?:(ERROR|EXCEPTION)\s+PROCEDURE|FOR\s+DEBUGGING)\s*(?:ON\s+(.+))?", re.I)


def _mark_use_handlers(paras: List[Paragraph]) -> List[Paragraph]:
    """A declarative section head carries a USE statement; pull its trigger/files onto the
    section paragraph and drop the USE from the statement list (it is not executable)."""
    for p in paras:
        for st in p.statements:
            if isinstance(st, Action) and st.verb == "USE":
                m = _USE_RE.search(st.text)
                if m:
                    p.use_trigger = (m.group(1) or "DEBUGGING").upper()
                    if m.group(2):
                        p.use_files = [w.upper() for w in re.split(r"[\s,]+", m.group(2).strip())
                                       if w and w.upper() not in ("INPUT", "OUTPUT", "I-O", "EXTEND")]
                else:
                    p.use_trigger = "EXCEPTION"
                p.statements = [s for s in p.statements
                                if not (isinstance(s, Action) and s.verb == "USE")]
                break
    return paras


def _collect_cics_handlers(paras: List[Paragraph]):
    """Pull (condition, target) pairs out of every EXEC CICS HANDLE CONDITION statement."""
    out = []
    for p in paras:
        for st in walk_statements(p.statements):
            if isinstance(st, ExecStmt) and st.kind == "handle" and st.lang == "CICS" \
                    and st.verb == "HANDLE":
                names = st.conditions
                for i in range(0, len(names) - 1, 2):  # interleaved cond, target, ...
                    out.append((names[i], names[i + 1]))
    return out


# --------------------------------------------------------------------------- #
# Pass 2: statement parser (recursive descent over a paragraph's tokens)
# --------------------------------------------------------------------------- #

def _share_stacked_when_bodies(whens: List[Tuple[str, List[Stmt]]],
                               other_body: Optional[List[Stmt]]) -> None:
    """Stacked WHENs (``WHEN 1 WHEN 2 body``) fall into the next branch's body: a WHEN
    with no imperative of its own executes the body of the WHEN (or WHEN OTHER) that
    follows it. Share the body backwards so the first WHEN doesn't silently skip it."""
    for i in range(len(whens) - 1, -1, -1):
        cond, body = whens[i]
        if body:
            continue
        if i + 1 < len(whens):
            whens[i] = (cond, whens[i + 1][1])
        elif other_body:
            whens[i] = (cond, other_body)


class StmtParser:
    def __init__(self, tokens: List[Token],
                 expand: Optional[Callable[[str], Optional[List[str]]]] = None):
        self.toks = tokens
        self.i = 0
        # Group-level host variable -> its elementary items, the Db2 precompiler's
        # expansion (parser._structure_expander). Absent, every host variable is kept
        # exactly as the source spells it - which is also the answer for a name the
        # data division does not hold.
        self._expand = expand or (lambda name: None)

    # -- token helpers -----------------------------------------------------
    def _peek(self, k: int = 0) -> Optional[Token]:
        j = self.i + k
        return self.toks[j] if 0 <= j < len(self.toks) else None

    def _next(self) -> Optional[Token]:
        t = self._peek()
        if t is not None:
            self.i += 1
        return t

    def _at_period(self) -> bool:
        t = self._peek()
        return t is not None and t.kind == "period"

    def _eof(self) -> bool:
        return self.i >= len(self.toks)

    @staticmethod
    def _is_end_word(t: Optional[Token]) -> bool:
        return t is not None and t.kind == "word" and t.up.startswith("END-")

    def _line(self) -> int:
        t = self._peek()
        return t.line if t else (self.toks[-1].line if self.toks else 0)

    # -- top level ---------------------------------------------------------
    def parse_paragraph(self) -> List[Stmt]:
        stmts: List[Stmt] = []
        while not self._eof():
            if self._at_period():
                self._next()  # consume sentence-ending period
                continue
            stmts.extend(self.parse_block(stops=set()))
            if self._at_period():
                self._next()
            elif not self._eof():
                # Defensive: avoid an infinite loop on an unexpected token.
                self._next()
        return stmts

    def parse_block(self, stops: Set[str]) -> List[Stmt]:
        """Parse statements until a period, EOF, an outer END- terminator, or a token
        in ``stops`` (none consumed)."""
        out: List[Stmt] = []
        while not self._eof():
            t = self._peek()
            if t.kind == "period":
                break
            if t.kind == "word" and (t.up in stops or self._is_end_word(t)):
                break
            stmt = self.parse_statement(stops)
            if stmt is not None:
                out.append(stmt)
            else:
                break
        return out

    def parse_statement(self, stops: Set[str]) -> Optional[Stmt]:
        t = self._peek()
        if t is None or t.kind != "word":
            # Stray token; consume so we make progress.
            self._next()
            return None
        v = t.up
        if v == "EXEC":
            return self.parse_exec()
        if v == "IF":
            return self.parse_if(stops)
        if v == "EVALUATE":
            return self.parse_evaluate()
        if v == "PERFORM":
            return self.parse_perform()
        if v == "GO":
            return self.parse_goto()
        if v in ("SORT", "MERGE"):
            return self.parse_sort()
        if v == "SEARCH":
            return self.parse_search()
        if v in IO_VERBS:
            return self.parse_io()
        if v == "CALL":
            return self.parse_call()
        if v == "ALTER":
            return self.parse_alter()
        if v == "EXIT":
            return self.parse_exit()
        if v == "CONTINUE":
            ln = self._next().line
            return ContinueStmt(line=ln)
        if v == "NEXT":
            ln = self._next().line  # NEXT
            if self._peek() and self._peek().is_word("SENTENCE"):
                self._next()
            return ContinueStmt(line=ln, next_sentence=True)
        if v == "STOP":
            ln = self._next().line
            if self._peek() and self._peek().is_word("RUN"):
                self._next()
                return TerminateStmt(line=ln, kind="STOP_RUN")
            return Action(line=ln, text="STOP", verb="STOP")
        if v == "GOBACK":
            ln = self._next().line
            return TerminateStmt(line=ln, kind="GOBACK")
        if v in OPAQUE_SCOPED:
            return self.parse_opaque_scoped(OPAQUE_SCOPED[v], stops)
        if v in _HANDLED_VERBS:
            return self.parse_handled_action(stops)
        return self.parse_action(stops)

    # -- opaque action -----------------------------------------------------
    def parse_action(self, stops: Set[str]) -> Stmt:
        start = self._next()
        verb = start.up
        parts = [start.text]
        line = start.line
        exc_verb = verb in _EXC_VERBS
        while not self._eof():
            t = self._peek()
            if t.kind == "period":
                break
            if t.kind == "word":
                u = t.up
                if u in STARTERS or u in stops or self._is_end_word(t):
                    break
                if u in {"ELSE", "WHEN", "THEN"}:
                    break
                if u == "AT" or (u == "NOT" and self._peek(1)
                                 and self._peek(1).up in {"AT", "INVALID", "ON",
                                                          "SIZE", "EXCEPTION"}):
                    break
                if u == "INVALID":
                    break
                # An ON-condition handler phrase opens here: [NOT] [ON] SIZE ERROR,
                # or (for ACCEPT/DISPLAY) [NOT] [ON] EXCEPTION. Its imperative is a
                # conditional branch - stop so the caller captures it as a handler.
                if u == "SIZE" and self._peek(1) and self._peek(1).is_word("ERROR"):
                    break
                if u == "ON" and self._peek(1) and self._peek(1).up in {
                        "SIZE", "EXCEPTION", "OVERFLOW"}:
                    break
                if exc_verb and u in {"EXCEPTION", "OVERFLOW"}:
                    break
            parts.append(self._next().text)
        return Action(line=line, text=" ".join(parts), verb=verb)

    def _handler_phrase(self, exc_ok: bool) -> Optional[str]:
        """If the upcoming tokens open an ON-condition handler phrase, consume the
        phrase words and return its key ('ON_SIZE_ERROR', 'NOT_ON_EXCEPTION', ...);
        otherwise consume nothing and return None."""
        j = 0
        neg = False
        t = self._peek(j)
        if t is None or t.kind != "word":
            return None
        if t.up == "NOT":
            neg = True
            j += 1
            t = self._peek(j)
            if t is None or t.kind != "word":
                return None
        if t.up == "ON":
            j += 1
            t = self._peek(j)
            if t is None or t.kind != "word":
                return None
        if t.up == "SIZE":
            nxt = self._peek(j + 1)
            if nxt is not None and nxt.is_word("ERROR"):
                for _ in range(j + 2):
                    self._next()
                return ("NOT_" if neg else "") + "ON_SIZE_ERROR"
            return None
        if t.up in ("EXCEPTION", "OVERFLOW") and (exc_ok or j > 0 or neg):
            for _ in range(j + 1):
                self._next()
            return ("NOT_" if neg else "") + "ON_" + t.up
        return None

    def parse_handled_action(self, stops: Set[str]) -> Stmt:
        """An arithmetic / ACCEPT / DISPLAY statement whose [NOT] ON SIZE ERROR /
        EXCEPTION handler imperatives are captured as real conditional branches."""
        inner = self.parse_action(stops)
        endword = "END-" + inner.verb
        exc_ok = inner.verb in _EXC_VERBS
        handlers: Dict[str, List[Stmt]] = {}
        while not self._eof():
            t = self._peek()
            if t.kind == "period":
                break
            if t.kind == "word" and t.up == endword:
                self._next()
                break
            key = self._handler_phrase(exc_ok)
            if key is None:
                break
            handlers[key] = self.parse_block(stops={"NOT", "ON", "SIZE"})
        if handlers:
            return HandledStmt(line=inner.line, inner=inner, handlers=handlers)
        return inner

    def parse_opaque_scoped(self, endword: str, stops: Set[str]) -> Stmt:
        """Consume a STRING / UNSTRING statement as one opaque action.

        These carry an *optional* ``END-<verb>`` scope terminator. When it is present
        we consume up to it. When it is ABSENT, COBOL terminates the statement
        implicitly at the next statement-starting verb, so we must stop there too -
        exactly like :meth:`parse_action`. Otherwise a terminator-less STRING swallows
        the entire rest of the paragraph (every following IF / PERFORM / EVALUATE) as a
        single opaque blob, which is the common one-period-per-paragraph style and was
        collapsing real control flow.

        The one exception is an ``ON OVERFLOW`` / ``NOT ON OVERFLOW`` phrase, whose
        imperative legitimately contains verbs; those belong to the statement until its
        ``END-`` word or the sentence period, so we keep consuming while inside it.
        """
        start = self._next()
        parts = [start.text]
        line = start.line
        depth = 1
        in_overflow = False
        while not self._eof():
            t = self._peek()
            if t.kind == "period":
                break
            if t.kind == "word":
                u = t.up
                if u == endword:
                    parts.append(self._next().text)
                    depth -= 1
                    if depth == 0:
                        break
                    continue
                if u == "OVERFLOW":
                    in_overflow = True
                elif not in_overflow and (
                    u in STARTERS or u in stops or self._is_end_word(t)
                    or u in {"ELSE", "WHEN", "THEN"}
                ):
                    break  # implicit terminator: no END- word for this statement
            parts.append(self._next().text)
        return Action(line=line, text=" ".join(parts), verb=start.up)

    # -- IF ----------------------------------------------------------------
    def parse_if(self, stops: Set[str]) -> Stmt:
        """Parse IF [THEN] ... [ELSE ...] [END-IF].

        Both branches inherit the caller's ``stops``: an IF nested in a WHEN, an AT END
        handler or another IF's branch must not read past the terminator that closes the
        construct it sits in. ``ELSE`` joins them because of the dangling-else rule -
        ELSE binds to the NEAREST unmatched IF, so once this IF has taken its own ELSE, a
        further ELSE at the same depth closes this statement and belongs to an outer one.
        Without that, a period-terminated `IF / IF / ELSE / ELSE` (no END-IF) hands the
        inner IF both else-bodies and leaves the outer one with none - an inversion.
        """
        line = self._next().line  # IF
        cond = self._collect_condition()
        if self._peek() and self._peek().is_word("THEN"):
            self._next()
        inner = stops | {"ELSE"}
        then_body = self.parse_block(stops=inner)
        else_body: List[Stmt] = []
        if self._peek() and self._peek().is_word("ELSE"):
            self._next()
            else_body = self.parse_block(stops=inner)
        if self._peek() and self._peek().is_word("END-IF"):
            self._next()
        return IfStmt(line=line, cond_text=cond, then_body=then_body, else_body=else_body)

    def _collect_condition(self) -> str:
        parts: List[str] = []
        while not self._eof():
            t = self._peek()
            if t.kind == "period":
                break
            if t.kind == "word":
                u = t.up
                if u in STARTERS or u in {"THEN", "ELSE", "WHEN", "CONTINUE", "NEXT"}:
                    break
                if self._is_end_word(t):
                    break
            parts.append(self._next().text)
        return " ".join(parts).strip()

    # -- EVALUATE ----------------------------------------------------------
    def parse_evaluate(self) -> Stmt:
        line = self._next().line  # EVALUATE
        subject = self._collect_until_word({"WHEN"})
        ev = EvaluateStmt(line=line, subject=subject.strip())
        while self._peek() and self._peek().is_word("WHEN"):
            self._next()  # WHEN
            if self._peek() and self._peek().is_word("OTHER"):
                self._next()
                ev.other_body = self.parse_block(stops={"WHEN"})
            else:
                cond = self._collect_condition_when()
                body = self.parse_block(stops={"WHEN"})
                ev.whens.append((cond.strip(), body))
        if self._peek() and self._peek().is_word("END-EVALUATE"):
            self._next()
        _share_stacked_when_bodies(ev.whens, ev.other_body)
        return ev

    def _collect_condition_when(self) -> str:
        parts: List[str] = []
        while not self._eof():
            t = self._peek()
            if t.kind == "period":
                break
            if t.kind == "word":
                u = t.up
                if u in STARTERS or u in {"WHEN"} or self._is_end_word(t):
                    break
            parts.append(self._next().text)
        return " ".join(parts)

    # -- SEARCH ------------------------------------------------------------
    def parse_search(self) -> Stmt:
        line = self._next().line  # SEARCH
        is_all = False
        if self._peek() and self._peek().is_word("ALL"):
            self._next()
            is_all = True
        table = None
        if self._peek() and self._peek().kind == "word" and self._peek().up not in STARTERS:
            table = self._next().up
        varying = None
        if self._peek() and self._peek().is_word("VARYING"):
            self._next()
            if self._peek() and self._peek().kind == "word":
                varying = self._next().up
        st = SearchStmt(line=line, table=table or "?", all=is_all, varying=varying)
        # AT END handler and WHEN branches, in any order, until END-SEARCH / period.
        while not self._eof():
            t = self._peek()
            if t.kind == "period":
                break
            if t.is_word("END-SEARCH"):
                self._next()
                break
            if t.up == "AT" and self._peek(1) and self._peek(1).is_word("END"):
                self._next(); self._next()
                st.at_end_body = self.parse_block(stops={"WHEN"})
                continue
            if t.is_word("WHEN"):
                self._next()
                cond = self._collect_condition_when()
                body = self.parse_block(stops={"WHEN"})
                st.whens.append((cond.strip(), body))
                continue
            # Stray token inside SEARCH: consume to make progress.
            self._next()
        _share_stacked_when_bodies(st.whens, None)
        return st

    def _collect_until_word(self, words: Set[str]) -> str:
        parts: List[str] = []
        while not self._eof():
            t = self._peek()
            if t.kind == "period":
                break
            if t.kind == "word" and (t.up in words or self._is_end_word(t)):
                break
            parts.append(self._next().text)
        return " ".join(parts)

    # -- PERFORM -----------------------------------------------------------
    def parse_perform(self) -> Stmt:
        line = self._next().line  # PERFORM
        t = self._peek()
        control_words = {"UNTIL", "VARYING", "WITH", "TEST", "TIMES", "FOREVER"}
        # `PERFORM 5 TIMES` and `PERFORM WS-N TIMES` name no procedure: the operand in
        # front of TIMES is the repeat COUNT. Deciding that on the first token alone got
        # both spellings wrong, and silently:
        #   * a numeric count is not a `word`, so the statement looked out-of-line and
        #     the inline body was never parsed - it stayed in the token stream and became
        #     the paragraph's NEXT statements, so the body ran ONCE, after the loop, and
        #     the loop itself spun n times doing nothing. Verified under real XState:
        #     `PERFORM 5 TIMES ADD 1 TO WS-A END-PERFORM` left WS-A = 1.
        #   * an identifier count was taken as the procedure name, inventing a PERFORM of
        #     a paragraph called WS-N and leaving the control clause as a bare "TIMES"
        #     with no count in it at all.
        # One token of lookahead settles it. `PERFORM P 5 TIMES` still parses as a
        # procedure plus a count, because the token after P is the count, not TIMES.
        nxt = self._peek(1)
        count_first = nxt is not None and nxt.is_word("TIMES")
        is_inline = (
            t is None
            or t.kind == "period"
            or t.kind != "word"
            or t.up in control_words
            or t.up in STARTERS
            or count_first
        )
        target = None
        thru = None
        if not is_inline and t.kind == "word":
            target = self._next().up
            if self._peek() and self._peek().up in {"THRU", "THROUGH"}:
                self._next()
                if self._peek() and self._peek().kind == "word":
                    thru = self._next().up
        control = self._collect_control_clause()
        kind, test_after = _perform_kind(control, inline=is_inline)
        inline_body: List[Stmt] = []
        if is_inline:
            inline_body = self.parse_block(stops=set())
            if self._peek() and self._peek().is_word("END-PERFORM"):
                self._next()
        return PerformStmt(line=line, kind=kind, target=target, thru=thru,
                           control_text=control.strip(), test_after=test_after,
                           inline_body=inline_body)

    def _collect_control_clause(self) -> str:
        parts: List[str] = []
        while not self._eof():
            t = self._peek()
            if t.kind == "period":
                break
            if t.kind == "word":
                u = t.up
                if u in STARTERS or self._is_end_word(t) or u in {"ELSE", "WHEN"}:
                    break
            parts.append(self._next().text)
        return " ".join(parts)

    # -- GO TO -------------------------------------------------------------
    def parse_goto(self) -> Stmt:
        line = self._next().line  # GO
        if self._peek() and self._peek().is_word("TO"):
            self._next()
        targets: List[str] = []
        depending = False
        depending_on: Optional[str] = None
        while not self._eof():
            t = self._peek()
            if t.kind == "period":
                break
            if t.is_word("DEPENDING"):
                depending = True
                self._next()
                if self._peek() and self._peek().is_word("ON"):
                    self._next()
                if self._peek() and self._peek().kind == "word":
                    depending_on = self._next().up  # the index variable
                while not self._eof() and not self._at_period():
                    self._next()
                break
            if t.kind == "word" and t.up in {"OF", "IN"}:
                # GO TO para OF section: consume the qualification (the unqualified
                # name is the edge target; qualification does not change it here).
                self._next()
                if self._peek() and self._peek().kind == "word":
                    self._next()
                continue
            if (t.kind == "word" and t.up not in STARTERS
                    and t.up not in {"ELSE", "WHEN", "THEN"}
                    and not self._is_end_word(t)):
                targets.append(self._next().up)
            else:
                break
        return GoToStmt(line=line, targets=targets, depending=depending,
                        depending_on=depending_on)

    # -- CALL --------------------------------------------------------------
    def parse_call(self) -> Stmt:
        line = self._next().line  # CALL
        t = self._peek()
        target = "?"
        dynamic = True
        if t is not None:
            if t.kind == "string":
                target = t.text.strip("'\"")
                dynamic = False
                self._next()
            elif t.kind == "word":
                target = t.up
                dynamic = True
                self._next()
        using: List[str] = []
        by_content: List[str] = []
        returning: Optional[str] = None
        handlers: Dict[str, List[Stmt]] = {}
        mode = None  # 'using' | 'returning' while collecting arg names
        passing = "REFERENCE"  # BY REFERENCE (default) | CONTENT | VALUE, sticky per arg
        while not self._eof():
            t = self._peek()
            if t.kind == "period":
                break
            if t.kind == "word":
                if t.up == "END-CALL":
                    self._next()
                    break
                if t.up in {"NOT", "ON", "EXCEPTION", "OVERFLOW"}:
                    # [NOT] [ON] EXCEPTION/OVERFLOW imperative: a conditional branch,
                    # captured as a handler body (not hoisted into the main flow).
                    key = self._handler_phrase(exc_ok=True)
                    if key is not None:
                        handlers[key] = self.parse_block(stops={"NOT", "ON"})
                        mode = None
                        continue
                    self._next()  # stray NOT/ON: skip so we make progress
                    continue
                if t.up == "USING":
                    mode = "using"
                    passing = "REFERENCE"
                    self._next()
                    continue
                if t.up in {"RETURNING", "GIVING"}:
                    mode = "returning"
                    self._next()
                    continue
                if t.up == "BY":
                    self._next()
                    continue
                if t.up in {"REFERENCE", "CONTENT", "VALUE"}:
                    passing = t.up
                    self._next()
                    continue
                if t.up in STARTERS:
                    # Including a following CALL: two CALLs in one sentence
                    # (`CALL 'A'` newline `CALL 'B'.`) are two statements, and
                    # consuming the second as this one's trailing tokens loses an
                    # entire program dependency. CALL is a reserved word, so it can
                    # never be an argument name here.
                    break
                if mode == "using":
                    using.append(t.up)
                    if passing in ("CONTENT", "VALUE"):
                        by_content.append(t.up)
                elif mode == "returning":
                    returning = t.up
                    mode = None
            self._next()
        return CallStmt(line=line, target=target, dynamic=dynamic,
                        on_exception=bool(handlers), using=using, returning=returning,
                        by_content=by_content, handlers=handlers)

    # -- ALTER -------------------------------------------------------------
    def parse_alter(self) -> Stmt:
        line = self._next().line  # ALTER
        parts = ["ALTER"]
        words: List[str] = []
        # ALTER operands are only paragraph-names + TO/PROCEED; stop at the next
        # statement (there may be no period before it, e.g. a following GO TO).
        while not self._eof() and not self._at_period():
            t = self._peek()
            if t.kind == "word" and (t.up in STARTERS or self._is_end_word(t)
                                     or t.up in {"ELSE", "WHEN"}):
                break
            self._next()
            parts.append(t.text)
            if t.kind == "word" and t.up not in {"TO", "PROCEED"}:
                words.append(t.up)
        # words arrive as [altered, target, altered, target, ...]
        pairs = [(words[i], words[i + 1]) for i in range(0, len(words) - 1, 2)]
        return AlterStmt(line=line, text=" ".join(parts), pairs=pairs)

    # -- EXIT --------------------------------------------------------------
    def parse_exit(self) -> Stmt:
        line = self._next().line  # EXIT
        t = self._peek()
        if t and t.is_word("PROGRAM"):
            self._next()
            return TerminateStmt(line=line, kind="EXIT_PROGRAM")
        if t and t.is_word("PERFORM"):
            self._next()
            if self._peek() and self._peek().is_word("CYCLE"):
                self._next()
                return ExitStmt(line=line, kind="PERFORM_CYCLE")
            return ExitStmt(line=line, kind="PERFORM")
        if t and t.is_word("PARAGRAPH"):
            self._next()
            return ExitStmt(line=line, kind="PARAGRAPH")
        if t and t.is_word("SECTION"):
            self._next()
            return ExitStmt(line=line, kind="SECTION")
        return ExitStmt(line=line, kind="PLAIN")

    # -- EXEC SQL / CICS / DLI ---------------------------------------------
    def parse_exec(self) -> Stmt:
        line = self._next().line  # EXEC
        lang = "?"
        if self._peek() and self._peek().kind == "word":
            lang = self._next().up
        toks: List[Token] = []
        while not self._eof():
            t = self._peek()
            if t.kind == "word" and t.up == "END-EXEC":
                self._next()
                break
            toks.append(self._next())
        # optional terminating period
        if self._at_period():
            self._next()
        words = [t.up for t in toks if t.kind == "word"]
        verb = words[0] if words else "?"
        text = " ".join(t.text for t in toks)
        # host variables: ':' immediately followed by a word - carrying the Db2
        # precompiler's two rewrites, because the statement that reaches the database
        # carries them: a GROUP name stands for its elementary items, and a null
        # indicator belongs to the variable it qualifies rather than being one of its
        # own. SQL only: neither construct exists in CICS or DLI, where a ':' is not a
        # host-variable marker at all.
        host_vars: List[str] = []
        expanded_structures: List[str] = []
        indicator_vars: List[str] = []
        if lang == "SQL":
            scanned = self._scan_host_vars(toks)
            indicator_vars = [ind for _, ind in scanned if ind]
            host_vars = [":" + n for n in self._expand_structures(
                [n for n, _ in scanned], expanded_structures)]
        else:
            for idx in range(len(toks)):
                hv = self._host_var_at(toks, idx)
                if hv is not None:
                    host_vars.append(":" + hv[0])

        kind, target, conditions = "effect", None, []
        dynamic = False
        into_vars: List[str] = []
        columns: List[dict] = []
        select_list: List[Optional[str]] = []
        column_note: Optional[str] = None
        column_unresolved: Optional[str] = None
        cursor: Optional[str] = None
        cursor_for_kind: Optional[str] = None
        cursor_for_statement: Optional[str] = None
        table: Optional[str] = None
        values_list: List[Optional[str]] = []
        where_vars: List[str] = []
        select_derivations: List[Optional[dict]] = []
        if lang == "CICS":
            if verb in ("RETURN", "ABEND"):
                kind = "terminate"
            elif verb == "XCTL":
                kind = "transfer"
                target, dynamic = self._exec_program(toks)
            elif verb == "LINK":
                kind = "call"
                target, dynamic = self._exec_program(toks)
            elif verb == "HANDLE":
                kind = "handle"
                conditions = self._exec_handle_conditions(toks)
        elif lang == "SQL":
            if verb == "WHENEVER":
                kind = "handle"
            elif verb in ("SELECT", "FETCH"):
                # SELECT/FETCH ... INTO :a, :b ... FROM/WHERE: the DB populates the host
                # variables. Capture them so the action models a real (external) input.
                into_vars = self._expand_structures(self._exec_into_vars(toks),
                                                    expanded_structures)
                if into_vars:
                    kind = "input"
                # ...and WHICH COLUMN fills each one - the only thing that proves two
                # programs read the same state. A FETCH's columns live on its cursor's
                # DECLARE, so it is correlated later (see interface._cursor_columns).
                if verb == "SELECT":
                    select_list, select_derivations, column_note =                         self._exec_select_columns(toks)
                    columns, column_note, column_unresolved = self._correlate(
                        select_list, into_vars, column_note, select_derivations)
                else:
                    # The cursor named here is what links this FETCH to the DECLARE
                    # holding its columns; resolved from tokens so the positioning
                    # keywords of a rowset FETCH cannot be mistaken for the name.
                    cursor = self._exec_fetch_cursor(toks)
            elif verb == "DECLARE":
                # DECLARE c CURSOR FOR SELECT cols FROM t: the columns are here, but the
                # host variables are on the FETCH. Carry the list; the FETCH zips it.
                cursor = self._exec_declare_cursor(toks)
                if cursor:
                    select_list, select_derivations, column_note =                         self._exec_select_columns(toks)
                    cursor_for_kind, cursor_for_statement =                         self._exec_declare_for(toks)
            elif verb == "UPDATE":
                columns, column_note, column_unresolved = \
                    self._exec_update_sets(toks)
                where_vars = self._exec_where_vars(toks)
            elif verb == "INSERT":
                columns, column_note, table, values_list, column_unresolved = \
                    self._exec_insert_columns(toks, expanded_structures)
                # An `INSERT ... SELECT ... WHERE` filters the rows it copies. A plain
                # VALUES insert has no WHERE at all, and this stays empty.
                where_vars = self._exec_where_vars(toks)
            elif verb == "DELETE":
                where_vars = self._exec_where_vars(toks)
            elif verb == "CALL":
                # EXEC SQL CALL proc(:p1, :p2): a stored-procedure invocation. The
                # operands are the procedure's PARAMETERS, not table columns - carrying
                # the name here is what lets the interface classify it as a procedure
                # endpoint instead of manufacturing a phantom table.
                target = self._exec_sql_call_target(toks)
        return ExecStmt(line=line, lang=lang, verb=verb, text=text, kind=kind,
                        target=target, dynamic=dynamic,
                        host_vars=host_vars, conditions=conditions,
                        into_vars=into_vars, columns=columns, select_list=select_list,
                        column_note=column_note,
                        column_unresolved=column_unresolved, cursor=cursor,
                        table=table, values_list=values_list,
                        where_vars=where_vars,
                        select_derivations=select_derivations,
                        expanded_structures=expanded_structures,
                        indicator_vars=indicator_vars,
                        cursor_for_kind=cursor_for_kind,
                        cursor_for_statement=cursor_for_statement)

    # -- SQL column <-> host-variable correlation ---------------------------
    #
    # Which COLUMN a host variable receives is what proves two programs touch the same
    # state: A's `SELECT BAL INTO :WS-BALANCE` and B's `SELECT BAL INTO :CUST-BAL` are the
    # same balance, and nothing else in the recovery says so. Parsed HERE, on the token
    # list, because ExecStmt.text is space-joined tokens - re-parsing that string loses
    # paren depth, and `SUM(A,B)` / `SUBSTR(X,1,3)` break a naive comma split.

    @staticmethod
    def _host_var_at(toks: List[Token], idx: int) -> Optional[Tuple[str, int]]:
        """The host variable the ':' at ``idx`` introduces, as ``(name, token width)``:
        uppercased, WITHOUT the colon. None when ``idx`` is not a ':' or nothing
        nameable follows it.

        The width comes back with the name because a scanner that walks past one
        variable to find the next has to know how wide this one was - advancing two
        tokens over a four-token qualified reference reads its own qualifier as the
        next variable.

        One rule in one place: the statement-wide scan and the WHERE-clause scan have to
        agree on what a host variable IS. Were they to spell one differently, the same
        variable would be a field to one and a parameter to the other, and the split
        between them would leak.

        `:GFAC . AC-ACC-N` is COBOL's QUALIFIED reference - "field AC-ACC-N inside group
        GFAC" - and the name that means anything downstream is the ELEMENTARY one after
        the period. Reading only the word after the ':' named the GROUP instead, so
        every slot of `VALUES (:GFAC . AC-MULTI-CO-N, :GFAC . AC-ACC-N, ...)` came back
        as the single name "GFAC": no column matched it, and the data dictionary has no
        such field to trace. The qualifier is dropped rather than kept because the
        elementary name is what the data division holds and what the naming conventions
        resolve - `data_division` already collapses same-named fields first-wins, so
        carrying it would disambiguate nothing that is not already collapsed.
        """
        t = toks[idx]
        if not (t.kind == "punct" and t.text == ":"):
            return None
        if idx + 1 >= len(toks) or toks[idx + 1].kind != "word":
            return None
        if (idx + 3 < len(toks) and toks[idx + 2].kind == "period"
                and toks[idx + 3].kind == "word"):
            return toks[idx + 3].up, 4
        return toks[idx + 1].up, 2

    @staticmethod
    def _indicator_at(toks: List[Token], idx: int,
                      end: Optional[int] = None) -> Optional[Tuple[str, int]]:
        """The NULL INDICATOR hanging off the host variable that ends at ``idx``, as
        ``(name, token width)``, or None when nothing there is one.

        Db2 spells it two ways - `:WS-BAL:IND-BAL` and `:WS-BAL INDICATOR :IND-BAL` -
        and both mean ONE value: host variable WS-BAL, whose null status arrives in
        IND-BAL. The indicator fills no column and carries no column's data.

        Read as a host variable in its own right it becomes an extra slot, and a cursor
        selecting 17 columns then disagrees with a FETCH that "has 18 host variables" -
        so the count gate refuses a correlation that is in fact exact, and every one of
        those fields reaches the tracer unmapped
        (docs/issues/host-structure-expansion.md). There is no comma between a variable
        and its indicator, which is what tells the pair from the next INTO target.
        """
        stop = len(toks) if end is None else end
        j, consumed = idx, 0
        if j < stop and toks[j].kind == "word" and toks[j].up == "INDICATOR":
            j += 1
            consumed += 1
        ind = StmtParser._host_var_at(toks, j) if j < stop else None
        return (ind[0], consumed + ind[1]) if ind is not None else None

    @classmethod
    def _scan_host_vars(cls, toks: List[Token], start: int = 0,
                        stop: Optional[int] = None) -> List[Tuple[str, Optional[str]]]:
        """Every host variable in ``toks[start:stop]``, in order, as
        ``(name, indicator or None)``.

        The ONE colon-walker: stepping by each reference's own width is what keeps a
        qualified `:GRP . FLD` from being read as two variables and an indicator from
        being read as a third. Every caller that needs "the host variables here" uses
        it, so no two of them can disagree about what one IS.
        """
        out: List[Tuple[str, Optional[str]]] = []
        end = len(toks) if stop is None else stop
        i = start
        while i < end:
            hv = cls._host_var_at(toks, i)
            if hv is None:
                i += 1
                continue
            name, width = hv
            ind = cls._indicator_at(toks, i + width, end)
            out.append((name, ind[0] if ind is not None else None))
            i += width + (ind[1] if ind is not None else 0)
        return out

    def _expand_structures(self, names: List[str], expanded: List[str]) -> List[str]:
        """Rewrite each GROUP-level name into its elementary items, in place and in
        declaration order, recording in ``expanded`` which names were expanded.

        A name the data division does not hold as a group is kept exactly as written -
        a field in a copybook that did not arrive cannot be expanded, and inventing an
        expansion for it would be a guess. The count gate downstream then flags it,
        which is the honest answer.
        """
        out: List[str] = []
        for n in names:
            subs = self._expand(n)
            if not subs:
                out.append(n)
                continue
            out.extend(subs)
            if n not in expanded:
                expanded.append(n)
        return out

    @staticmethod
    def _split_top_commas(toks: List[Token]) -> List[List[Token]]:
        """Split a token run on commas at paren depth 0, so `SUM(A,B)` stays one item."""
        out: List[List[Token]] = []
        cur: List[Token] = []
        depth = 0
        for t in toks:
            if t.kind == "punct" and t.text == "(":
                depth += 1
            elif t.kind == "punct" and t.text == ")":
                depth -= 1
            elif t.kind == "punct" and t.text == "," and depth == 0:
                out.append(cur)
                cur = []
                continue
            cur.append(t)
        if cur:
            out.append(cur)
        return out

    @staticmethod
    def _column_of(item: List[Token]) -> Optional[str]:
        """The column one select-list item names, or None if it is *derived* (an
        expression such as `SUM(X)` - it occupies a slot but is not a column)."""
        for i, t in enumerate(item):            # drop a trailing `AS alias`
            if t.kind == "word" and t.up == "AS":
                item = item[:i]
                break
        item = [t for t in item if not (t.kind == "punct" and t.text in "()")] or item
        if len(item) == 1 and item[0].kind == "word":
            return item[0].up                                   # BAL
        if (len(item) == 3 and item[0].kind == "word"           # T.BAL -> BAL
                and item[1].kind == "period" and item[2].kind == "word"):
            return item[2].up
        return None

    #: Words that are SQL SYNTAX rather than a column name, inside a derived item.
    #: `CURRENT DATE` / `CURRENT TIMESTAMP` are special registers, not columns; a table
    #: with a column genuinely called DATE loses a provenance hint here, which is the
    #: cheaper of the two errors (the entry stays `derived` either way - see below).
    _SQL_NOT_A_COLUMN = frozenset({
        "AS", "DISTINCT", "ALL", "CASE", "WHEN", "THEN", "ELSE", "END", "AND", "OR",
        "NOT", "NULL", "IS", "BETWEEN", "LIKE", "IN", "CAST", "CURRENT", "DATE",
        "TIME", "TIMESTAMP", "USER", "FROM", "FOR",
    })

    @classmethod
    def _columns_in(cls, toks: List[Token]) -> List[str]:
        """The column-shaped names in a token run, qualifiers dropped (`T . BAL` -> BAL).

        A word that opens a parenthesis is a FUNCTION name, a word after ':' is a host
        variable, and a word in :attr:`_SQL_NOT_A_COLUMN` is syntax. None of the three
        is a column, and naming one as the source of a field would be an INVENTED
        dependency rather than a recovered one.
        """
        out: List[str] = []
        i = 0
        while i < len(toks):
            t = toks[i]
            if t.kind == "punct" and t.text == ":":
                hv = cls._host_var_at(toks, i)
                if hv is not None:
                    i += hv[1]
                    continue
            if t.kind != "word":
                i += 1
                continue
            nxt = toks[i + 1] if i + 1 < len(toks) else None
            if nxt is not None and nxt.kind == "punct" and nxt.text == "(":
                i += 1                                  # a function name, not a column
                continue
            if (nxt is not None and nxt.kind == "period"
                    and i + 2 < len(toks) and toks[i + 2].kind == "word"):
                name, i = toks[i + 2].up, i + 3         # T . BAL -> BAL
            else:
                name, i = t.up, i + 1
            if name not in cls._SQL_NOT_A_COLUMN and name not in out:
                out.append(name)
        return out

    @classmethod
    def _derivation_of(cls, item: List[Token]) -> Optional[dict]:
        """What a DERIVED select-list item is made of: ``{expression, derivedFrom}``.

        `SUM(SPOKE_DOL_A)` fills its host variable from an aggregate OVER MANY ROWS, so
        the variable is not that column and must never be mapped to it - the entry stays
        `derived`. But the column is right there in the tokens, and discarding it left
        the reader with a field that came from nowhere. Recorded as PROVENANCE beside
        the refusal: we know where the expression read, and we still refuse to call it
        an identity.

        `COUNT(*)` and `SELECT 'Y'` genuinely have no source column, and say so with an
        empty ``derivedFrom`` - which is what tells them apart from a SUM whose source
        was simply lost. A CASE expression refuses to guess: which branch supplied the
        value is a run-time fact, so its columns are not a dependency this can prove.
        """
        for i, t in enumerate(item):                    # drop a trailing `AS alias`
            if t.kind == "word" and t.up == "AS":
                item = item[:i]
                break
        if not item:
            return None
        if any(t.kind == "word" and t.up == "CASE" for t in item):
            return {"expression": "CASE", "derivedFrom": []}
        if (len(item) >= 3 and item[0].kind == "word"
                and item[1].kind == "punct" and item[1].text == "("):
            # `VALUE(SUM(COL), 0)` reports the OUTERMOST function and recurses through
            # its arguments, so the innermost named column is what comes back.
            return {"expression": item[0].up,
                    "derivedFrom": cls._columns_in(cls._paren_group(item, 1) or [])}
        if all(t.kind in ("number", "string") for t in item):
            return {"expression": "literal", "derivedFrom": []}
        return {"expression": "expression", "derivedFrom": cls._columns_in(item)}

    @staticmethod
    def _is_star_item(item: List[Token]) -> bool:
        """True for a select-list item that IS a star: `*` or `T.*`.

        Not every `*` in a select list is one. `COUNT(*)` and `QTY * PRICE` are ordinary
        derived items that occupy one slot each, and a flat scan for the character read
        both as `SELECT *` - reporting "the column list is not in the source" about a
        statement whose column list is right there, and discarding the sibling slots'
        real mappings with it. Deciding per ITEM, after the comma split, is what tells
        the whole-list star apart from an operator inside one item."""
        if len(item) == 1 and item[0].kind == "punct" and item[0].text == "*":
            return True
        return (len(item) == 3 and item[0].kind == "word"           # T.*
                and item[1].kind == "period"
                and item[2].kind == "punct" and item[2].text == "*")

    @classmethod
    def _exec_select_columns(
            cls, toks: List[Token]) -> Tuple[List[Optional[str]],
                                             List[Optional[dict]], Optional[str]]:
        """The select list between SELECT and INTO/FROM, as column names (None = derived).

        Returns ``(columns, derivations, note)``. ``derivations`` is index-aligned with
        ``columns`` and holds an entry only where the column is None - what the derived
        slot is made of, so a lost source column is recoverable as provenance. A note
        means the list cannot be correlated at all.
        """
        # The select list of THIS statement is the one at paren depth 0. A CTE's inner
        # select and a scalar subquery in the select list both sit deeper, and handing
        # the caller one of those returns ANOTHER statement's column list - which then
        # fails the arity gate and costs the whole statement its mapping. Same rule
        # `_declare_from_table` already applies to the FROM table.
        depth = 0
        start = None
        shallowest = None        # fallback: a fullselect wrapped entirely in parens
        for i, t in enumerate(toks):
            if t.kind == "punct" and t.text == "(":
                depth += 1
            elif t.kind == "punct" and t.text == ")":
                depth -= 1
            elif t.kind == "word" and t.up == "SELECT":
                if depth == 0:
                    start = i + 1
                    break
                if shallowest is None or depth < shallowest[0]:
                    shallowest = (depth, i + 1)
        if start is None and shallowest is not None:
            # `DECLARE c CURSOR FOR (SELECT ...)` has no depth-0 SELECT at all.
            # Returning [] here would convert a loud arity refusal into a silent
            # empty mapping with no note - strictly worse than today.
            start = shallowest[1]
        if start is None:
            return [], [], None
        # Terminate at the first INTO/FROM at the SELECT's OWN depth, so a `FROM`
        # inside a subquery in the select list cannot truncate the list. `depth` is
        # counted from `start`, so it stays relative to whichever SELECT was chosen.
        depth = 0
        end = len(toks)
        for i in range(start, len(toks)):
            t = toks[i]
            if t.kind == "punct" and t.text == "(":
                depth += 1
            elif t.kind == "punct" and t.text == ")":
                depth -= 1
            elif depth == 0 and t.kind == "word" and t.up in ("INTO", "FROM"):
                end = i
                break
        sel = toks[start:end]
        if sel and sel[0].kind == "word" and sel[0].up in ("DISTINCT", "ALL"):
            sel = sel[1:]
        items = cls._split_top_commas(sel)
        if any(cls._is_star_item(item) for item in items):
            return [], [], ("SELECT * : the column list is not in the source; resolving "
                            "it needs the Db2 catalog")
        columns = [cls._column_of(item) for item in items]
        derivations = [None if c is not None else cls._derivation_of(item)
                       for c, item in zip(columns, items)]
        return columns, derivations, None

    @classmethod
    def _exec_update_sets(cls, toks: List[Token]
                          ) -> Tuple[List[dict], Optional[str], Optional[str]]:
        """`UPDATE t SET c = :h, c2 = :h2` -> the pairs. Explicit, not positional: the
        highest-fidelity shape there is.

        Returns ``(columns, note, unresolved)``. A host variable this cannot pair is
        never dropped in SILENCE, which is the one outcome the surrounding code is
        designed to avoid: where the target column is known, the variables inside the
        expression become explicit `derived` entries - the rule `_correlate` already
        applies on the SELECT side, for the reason argued there - and where the target
        cannot be identified at all, the note and its token say so.

        `SET C = CURRENT TIMESTAMP` still contributes nothing and still says nothing:
        it names no host variable, so there is no field whose fate needs explaining.
        """
        start = None
        for i, t in enumerate(toks):
            if t.kind == "word" and t.up == "SET":
                start = i + 1
                break
        if start is None:
            return [], None, None
        # The SET list ends at the WHERE at ITS OWN depth. Under a flat scan
        # `SET c = (SELECT x FROM y WHERE z = :h1), d = :h2 WHERE k = :f` ends inside
        # the subquery, losing `d = :h2` entirely.
        depth = 0
        end = len(toks)
        for i in range(start, len(toks)):
            t = toks[i]
            if t.kind == "punct" and t.text == "(":
                depth += 1
            elif t.kind == "punct" and t.text == ")":
                depth -= 1
            elif depth == 0 and t.kind == "word" and t.up == "WHERE":
                end = i
                break
        out: List[dict] = []
        unmapped = 0
        for item in cls._split_top_commas(toks[start:end]):
            eq = next((j for j, t in enumerate(item)
                       if t.kind == "punct" and t.text == "="), None)
            if eq is None:
                continue
            col = cls._column_of(item[:eq])
            if col is None:
                # The row-value form `SET (C1, C2) = (SELECT a, b FROM t)`: a `derived`
                # entry has no single column to sit at, so the reason goes in the note.
                unmapped += 1
                continue
            # The same slot rule the VALUES list uses, so `SET C = :GRP . FLD` keeps its
            # mapping and `SET C = CURRENT TIMESTAMP` still has none.
            hv = cls._host_var_of(item[eq + 1:])
            if hv is not None:
                out.append({"column": col, "hostVar": hv})
                continue
            # A variable inside the expression's own WHERE is a row SELECTOR, not a
            # value written to `col` - `SET BAL = (SELECT TOT FROM L WHERE REF = :R)`
            # writes TOT, and :R only picks the row. `_exec_where_vars` is already the
            # authority on which those are, so subtract exactly its answer rather than
            # deciding it twice (it carries colons; `columns[].hostVar` does not).
            rhs = item[eq + 1:]
            selectors = {v.lstrip(":") for v in cls._exec_where_vars(rhs)}
            kind = cls._expression_kind(rhs)
            for hvar in cls._host_vars_in(rhs):
                if hvar in selectors:
                    continue
                out.append({"column": None, "hostVar": hvar, "derived": True,
                            "expression": kind, "derivedFrom": [col]})
        if unmapped and not out:
            # Only when the statement resolved NOTHING. `columnsUnresolved` means the
            # recovery FAILED (interface._note), and a statement that mapped some of
            # its columns has not failed - tokenising it would put the mappings beside
            # it out of reach of a consumer that skips on the token. The prose note
            # still reports the gap either way.
            return out, (f"{unmapped} SET assignment(s) name no single target column "
                         f"(a row-value `SET (C1, C2) = ...`) - the host variables in "
                         f"them fill no column this can identify"), \
                "set-expression-unmapped"
        if unmapped:
            return out, (f"{unmapped} SET assignment(s) name no single target column "
                         f"(a row-value `SET (C1, C2) = ...`) - the host variables in "
                         f"them fill no column this can identify"), None
        return out, None, None

    @classmethod
    def _host_vars_in(cls, toks: List[Token]) -> List[str]:
        """Every host variable named anywhere in a token run, at any depth, in source
        order and without repeats.

        The BARE name, matching `columns[].hostVar`. `_exec_where_vars` carries the
        leading colon because it feeds `where_vars`, which is a different field with a
        different convention - joining the two would be a silent shape change."""
        out: List[str] = []
        for idx in range(len(toks)):
            hv = cls._host_var_at(toks, idx)
            if hv is not None and hv[0] not in out:
                out.append(hv[0])
        return out

    @classmethod
    def _expression_kind(cls, item: List[Token]) -> str:
        """A COARSE label for what fills a SET target, at the granularity
        `_derivation_of` already uses on the SELECT side. Only the subselect case is
        added here: on the select-list side that shape is rare enough to fall through
        to the generic label, but it is the whole point of a row-value UPDATE."""
        if any(t.kind == "word" and t.up == "SELECT" for t in item):
            return "SUBSELECT"
        return (cls._derivation_of(item) or {}).get("expression") or "expression"

    @classmethod
    def _exec_where_vars(cls, toks: List[Token]) -> List[str]:
        """The host variables inside a WHERE clause: the row SELECTOR, at any depth.

        `UPDATE t SET c = :h WHERE k = :f` sends both variables to Db2, but only :h
        reaches a column - :f decides which ROW is written. Reporting :f among the
        statement's FIELDS is what made a filter read as a write whose column mapping
        had been lost, and every one of them arrived downstream as an unmapped field.

        A WHERE is a PREDICATE wherever it appears, so a nested one counts too - but
        only within its own parentheses. Reading instead from the first WHERE to the end
        of the statement gets `SET c = (SELECT x FROM y WHERE z = :h1), d = :h2 WHERE
        k = :h3` exactly backwards, sweeping the write :h2 in with the filters. Tracking
        the depth each open WHERE sits at, and closing it when its group closes, keeps
        `:h1` and `:h3` as the filters and leaves `:h2` a write.
        """
        out: List[str] = []
        depth = 0
        active: List[int] = []          # the depths of the WHERE clauses now open
        for idx, t in enumerate(toks):
            if t.kind == "punct" and t.text == "(":
                depth += 1
            elif t.kind == "punct" and t.text == ")":
                depth -= 1
                while active and active[-1] > depth:
                    active.pop()
            elif t.kind == "word" and t.up == "WHERE":
                if not active or active[-1] != depth:
                    active.append(depth)
            if active:
                hv = cls._host_var_at(toks, idx)
                if hv is not None and ":" + hv[0] not in out:
                    out.append(":" + hv[0])
        return out

    @staticmethod
    def _paren_group(toks: List[Token], start: int,
                     end: Optional[int] = None) -> Optional[List[Token]]:
        """The inner tokens of the first parenthesized group whose `(` lies in
        ``toks[start:end]``, or None when there is none. Depth-aware, so a nested
        `(SUBSTR(X,1,3))` closes on its own paren."""
        stop = len(toks) if end is None else end
        i = start
        while i < stop and not (toks[i].kind == "punct" and toks[i].text == "("):
            i += 1
        if i >= stop:
            return None
        depth = 0
        for j in range(i, len(toks)):
            t = toks[j]
            if t.kind == "punct" and t.text == "(":
                depth += 1
            elif t.kind == "punct" and t.text == ")":
                depth -= 1
                if depth == 0:
                    return toks[i + 1:j]
        return None

    #: The note a column-list-less INSERT carries out of the parse. Build time re-tries
    #: it against the program's DECLARE TABLE / DCLGEN declarations (and any synonym
    #: map), so the note doubles as the marker that correlation is still pending -
    #: statechart._correlate_inserts matches on it.
    INSERT_NO_COLUMN_LIST = (
        "INSERT without an explicit column list: the target columns are the table's "
        "declared order, which is not in the source; resolving it needs the table's "
        "DECLARE TABLE / DCLGEN or the Db2 catalog")

    @staticmethod
    def _table_name(item: List[Token]) -> Optional[str]:
        """A (possibly qualified) table name from the START of a token run: `T` or
        `OWNER.T`, joined as written. Stops at the first word unless a `.` qualifier
        continues it - two adjacent words (`T WHERE ...`) are a name and the next
        clause, not one long name."""
        if not item or item[0].kind != "word":
            return None
        if item[0].up in _NOT_A_TABLE:
            return None
        parts = [item[0].up]
        i = 1
        while (i + 1 < len(item)
               and (item[i].kind == "period"
                    or (item[i].kind == "punct" and item[i].text == "."))
               and item[i + 1].kind == "word"):
            parts.append(item[i + 1].up)
            i += 2
        return ".".join(parts)

    def _values_slots(self, val_toks: Optional[List[Token]],
                      expanded: List[str]) -> List[Optional[str]]:
        """The VALUES list, one entry per slot AFTER host-structure expansion: the host
        variable that fills it, or None for a literal/expression slot.

        A group-level slot fills as many columns as the group has elementary items -
        `VALUES (:DSTI-TRNF-INIT, :WS-MULTI-CO-N)` is two slots in the source and 51 to
        Db2 - so the expansion has to happen BEFORE the arity is weighed against the
        column list, or the comparison is between two counts that were never meant to
        agree.
        """
        out: List[Optional[str]] = []
        for item in self._split_top_commas(val_toks or []):
            hv = self._host_var_of(item)
            if hv is None:
                out.append(None)
                continue
            subs = self._expand(hv)
            if subs:
                out.extend(subs)
                if hv not in expanded:
                    expanded.append(hv)
            else:
                out.append(hv)
        return out

    def _exec_insert_columns(self, toks: List[Token], expanded: List[str]
                             ) -> Tuple[List[dict], Optional[str],
                                        Optional[str], List[Optional[str]],
                                        Optional[str]]:
        """`INSERT INTO t (c1, c2) VALUES (:h1, CURRENT TIMESTAMP, :h2)` -> the pairs.

        Positional, like `SELECT ... INTO`: the Nth VALUES item fills the Nth column. A
        slot holding a literal or an expression rather than a host variable writes a
        column the program does not source from any field, so it maps to nothing - and
        is noted, because a column written from somewhere unrecovered is a gap.

        INSERT is the WRITE half of the cross-program state identity. Without it a
        program that only ever inserts contributes no column evidence at all, and its
        rows look unrelated to the rows every reader of that table selects.

        Returns ``(columns, note, table, values_list, unresolved)``. ``table`` and
        ``values_list`` are set only for an INSERT with NO column list: the columns then
        live in the table's DECLARE TABLE / DCLGEN, which is a whole-program fact - so
        this site records the table and the ordered VALUES slots, and build time
        finishes the zip (statechart._correlate_inserts). ``unresolved`` is the stable
        TOKEN for the same fact ``note`` states in prose, so a consumer can branch on
        the reason without matching on wording. It stays None for the no-column-list
        case: that one is not a refusal yet, and build time either resolves it or
        tokenises it there.  Group names expanded on the way are appended to
        ``expanded``, which the statement carries.
        """
        i_into = next((i for i, t in enumerate(toks)
                       if t.kind == "word" and t.up == "INTO"), None)
        if i_into is None:
            return [], None, None, [], None
        i_values = next((i for i, t in enumerate(toks)
                         if t.kind == "word" and t.up == "VALUES"), None)
        if i_values is None:
            return [], ("INSERT ... SELECT / fullselect: the values come from a query "
                        "rather than from host variables, so no column<->field mapping "
                        "exists at this site - the source table's lineage does"), None, \
                [], "insert-from-fullselect"
        col_toks = self._paren_group(toks, i_into + 1, i_values)
        if col_toks is None:
            table = self._table_name(toks[i_into + 1:i_values])
            values = self._values_slots(self._paren_group(toks, i_values + 1), expanded)
            return [], self.INSERT_NO_COLUMN_LIST, table, values, None
        val_toks = self._paren_group(toks, i_values + 1)
        if val_toks is None:
            return [], "INSERT VALUES list is not parseable here - verify by hand", \
                None, [], "values-unparseable"
        cols = [self._column_of(item) for item in self._split_top_commas(col_toks)]
        vals = self._values_slots(val_toks, expanded)
        if len(cols) != len(vals):
            return [], (f"{len(cols)} column(s) vs {len(vals)} VALUES item(s), host "
                        f"structures already expanded: not correlatable - a group whose "
                        f"data division entry did not arrive cannot be expanded, and a "
                        f"slot that is not one host variable fills no single column "
                        f"- verify by hand"), None, [], "count-mismatch"
        out: List[dict] = []
        unsourced: List[str] = []
        for col, hv in zip(cols, vals):
            if col is None:
                continue
            if hv is not None:
                out.append({"column": col, "hostVar": hv})
            else:
                unsourced.append(col)
        if unsourced:
            # Deliberately NO token: some columns mapped, so this is a success with a
            # documented gap, not a failed recovery (see `_exec_update_sets`).
            return out, (f"{len(unsourced)} column(s) written from a literal or "
                         f"expression rather than a host variable "
                         f"({', '.join(unsourced)}) - no program field maps to them"), \
                None, [], None
        return out, None, None, [], None

    @classmethod
    def _host_var_slot(cls, item: List[Token]) -> Optional[Tuple[str, Optional[str]]]:
        """The host variable one VALUES / SET slot names, as ``(name, indicator)``, or
        None for a literal/expression slot.

        `:WS-X` -> WS-X, the qualified `:GRP . FLD` -> FLD, and `:WS-BAL:IND-BAL` ->
        WS-BAL with indicator IND-BAL. That last one used to be refused along with the
        rest: the slot was required to be JUST the variable, and the indicator made it
        wider. But an indicator is not a second value - `SET C = :H:IND` writes column C
        from H exactly as `SET C = :H` does, and it says so in the same breath - so
        refusing it threw away a mapping the source proves.

        The slot must still be nothing MORE than that. `:A + :B` fills its column from an
        expression rather than from one field, so it maps to no single field and stays
        unsourced, which is what the width check below enforces.
        """
        hv = cls._host_var_at(item, 0) if item else None
        if hv is None:
            return None
        name, width = hv
        if len(item) == width:
            return name, None
        ind = cls._indicator_at(item, width)
        if ind is not None and len(item) == width + ind[1]:
            return name, ind[0]
        return None

    @classmethod
    def _host_var_of(cls, item: List[Token]) -> Optional[str]:
        """The NAME one VALUES / SET slot fills its column from (see `_host_var_slot`)."""
        slot = cls._host_var_slot(item)
        return slot[0] if slot is not None else None

    @staticmethod
    def _exec_sql_call_target(toks: List[Token]) -> Optional[str]:
        """`CALL proc(...)` / `CALL owner.proc(...)` -> the procedure name as written.
        None when the procedure itself is a host variable (`CALL :WS-PROC` - a
        runtime-determined name, like a dynamic COBOL CALL)."""
        i_call = next((i for i, t in enumerate(toks)
                       if t.kind == "word" and t.up == "CALL"), None)
        if i_call is None:
            return None
        rest = toks[i_call + 1:]
        if rest and rest[0].kind == "punct" and rest[0].text == ":":
            return None
        return StmtParser._table_name(rest)

    # Db2 positioning keywords that may sit between FETCH and its cursor name.
    _FETCH_POS = frozenset({
        "NEXT", "PRIOR", "FIRST", "LAST", "CURRENT", "BEFORE", "AFTER", "ABSOLUTE",
        "RELATIVE", "ROWSET", "FROM", "INSENSITIVE", "SENSITIVE", "STARTING", "AT",
        "CONTINUE",
    })

    # Db2 cursor attributes that may sit between the cursor NAME and the CURSOR
    # keyword: [ASENSITIVE | INSENSITIVE | SENSITIVE STATIC | SENSITIVE DYNAMIC]
    # [NO SCROLL | SCROLL] CURSOR. Holdability and returnability (WITH HOLD,
    # WITH RETURN, WITH/WITHOUT ROWSET POSITIONING) come AFTER the CURSOR keyword and
    # so do NOT belong here - adding them would only let a malformed statement slide.
    _DECLARE_ATTRS = frozenset({
        "ASENSITIVE", "INSENSITIVE", "SENSITIVE", "STATIC", "DYNAMIC", "NO", "SCROLL",
    })

    @classmethod
    def _exec_declare_cursor(cls, toks: List[Token]) -> Optional[str]:
        """`DECLARE c CURSOR ...` -> c. None for the other DECLAREs (``... TABLE``,
        ``... STATEMENT``, ``... GLOBAL TEMPORARY TABLE``), which name no cursor and
        hold no select list to zip.

        Db2 allows an attribute run between the cursor NAME and the CURSOR keyword -
        `DECLARE C1 INSENSITIVE SCROLL CURSOR FOR SELECT ...` - exactly as it allows a
        positioning run between FETCH and its cursor name (`_FETCH_POS`). Binding on
        `toks[i+2] == CURSOR` refused the whole statement, and refusing it is not a
        harmless miss: no cursor is registered, so every FETCH on it reports
        `cursor-declare-missing` and blames an absent copybook for a DECLARE that is
        sitting in the source.
        """
        for i, t in enumerate(toks):
            if t.kind == "word" and t.up == "DECLARE":
                if not (i + 2 < len(toks) and toks[i + 1].kind == "word"):
                    return None
                name = toks[i + 1].up
                # The NAME is positional and is never consulted against the attribute
                # set, so a cursor legitimately named SCROLL or DYNAMIC still binds.
                j = i + 2
                while (j < len(toks) and toks[j].kind == "word"
                        and toks[j].up in cls._DECLARE_ATTRS):
                    j += 1
                if j < len(toks) and toks[j].kind == "word" and toks[j].up == "CURSOR":
                    return name
                # A `return`, not a `continue`: both call sites hand this the tokens of
                # exactly ONE EXEC SQL block, so there is never a second DECLARE to
                # find, and falling through would let a DECLARE TABLE match whatever
                # came after it.
                return None
        return None

    @staticmethod
    def _exec_declare_for(toks: List[Token]) -> Tuple[Optional[str], Optional[str]]:
        """``DECLARE c CURSOR ... FOR <what>`` -> ``(kind, statement)``.

        ``("select", None)`` for a select list (or a ``WITH`` common table expression),
        ``("statement", "DYNSTMT")`` for a PREPAREd statement name, ``(None, None)``
        when no FOR was reached.

        POSITIVE evidence only. An empty ``selectList`` already means "no SELECT
        found", which is also exactly what a FAILED parse looks like, so dynamic-ness
        must never be inferred from it - the whole point of recording the form is to
        tell "the select list exists only at run time" apart from "we could not read
        the select list", and an inference from emptiness collapses them again.

        The FIRST ``FOR`` is the right one for every shape in this estate: the
        attribute keywords (INSENSITIVE, SCROLL, WITH HOLD, WITH RETURN TO CALLER,
        ROWSET POSITIONING) all precede it, and a trailing ``FOR FETCH ONLY`` /
        ``FOR 10 ROWS`` follows the select list.

        "statement" is claimed for a statement name whether or not a matching PREPARE
        is found anywhere. That is deliberate: a cursor may name a statement PREPAREd
        under a different name, and a ``DECLARE X STATEMENT`` that is never PREPAREd
        still has no statically knowable select list.
        """
        for i, t in enumerate(toks):
            if t.kind == "word" and t.up == "FOR" and i + 1 < len(toks):
                nxt = toks[i + 1]
                if nxt.kind != "word":
                    return None, None
                if nxt.up in ("SELECT", "WITH"):
                    return "select", None
                return "statement", nxt.up
        return None, None

    @classmethod
    def _exec_fetch_cursor(cls, toks: List[Token]) -> Optional[str]:
        """The cursor a FETCH reads, from its TOKENS rather than from its text.

        Db2 allows a run of positioning keywords between the verb and the cursor name -
        `FETCH NEXT ROWSET FROM CSR1 FOR 10 ROWS INTO ...` - and a pattern that does not
        know them binds the first word after FETCH. Reading `ROWSET` as the cursor name
        is not a harmless miss: it matches no DECLARE, so the FETCH loses its columns AND
        its endpoint becomes a phantom `<cursor ROWSET>` instead of the real table, which
        propagates into the artifact manifest and on into the retrieval stage.
        """
        i = next((i for i, t in enumerate(toks)
                  if t.kind == "word" and t.up == "FETCH"), None)
        if i is None:
            return None
        i += 1
        while i < len(toks):
            t = toks[i]
            if t.kind == "number":                        # ABSOLUTE 5
                i += 1
            elif t.kind == "punct" and t.text == ":":     # ABSOLUTE :WS-N
                i += 2
            elif t.kind == "word" and t.up in cls._FETCH_POS:
                i += 1
            elif t.kind == "word":
                return t.up
            else:
                return None
        return None

    @staticmethod
    def _correlate(columns: List[Optional[str]], into_vars: List[str],
                   note: Optional[str],
                   derivations: Optional[List[Optional[dict]]] = None
                   ) -> Tuple[List[dict], Optional[str], Optional[str]]:
        """Zip a select list against the INTO host variables - ONLY when the counts prove
        the correspondence.

        The gate is not defensive programming, it is the whole point: a naive zip that
        slipped by one would map BAL -> IND-NAME and state it as fact, and wrong lineage
        is worse than none.

        The two constructs that used to trip it are now resolved before it rather than
        refused at it, because both are things the Db2 precompiler DOES to the statement
        and the source says exactly what it does. `INTO :WS-NAME:IND-NAME, :WS-BAL` is
        two targets, not three (`_indicator_at`), and `INTO :CUST-REC` is one target per
        elementary item of CUST-REC, not one (`_expand_structures`). What still fails
        here is a count nothing in the source explains - a group whose data division
        entry did not arrive, most of all - and that stays refused.
        """
        if note:
            return [], note, None
        if not columns or not into_vars:
            return [], None, None
        if len(columns) != len(into_vars):
            return [], (f"{len(columns)} column(s) vs {len(into_vars)} host variable(s), "
                        f"host structures already expanded and null indicators already "
                        f"attached: not correlatable - a group whose data division entry "
                        f"did not arrive cannot be expanded - verify by hand"), \
                "count-mismatch"
        # A derived slot (`SELECT 'Y'`, `COUNT(*)`, `A + B`) fills its host variable
        # from something that is not a column, so it gets an EXPLICIT `derived` entry
        # rather than silence: without one, "an aggregate fills this field by
        # construction" and "the recovery failed on this field" are indistinguishable
        # to a consumer, which cannot skip the one without hiding the other. The note
        # still says so per variable - whether a derived slot matters is a judgement
        # for the reviewer looking at the statement, not one to make here by staying
        # quiet.
        # A derived slot carries WHAT it was derived from where the tokens prove it
        # (parser._derivation_of): still not a column identity - `derived` stays, and no
        # `column` key is ever added - but the reader can now see that the value came
        # out of SUM(SPOKE_DOL_A) rather than out of nowhere.
        mapped: List[dict] = []
        for i, (c, h) in enumerate(zip(columns, into_vars)):
            if c is not None:
                mapped.append({"column": c, "hostVar": h})
                continue
            entry = {"hostVar": h, "derived": True}
            d = derivations[i] if derivations and i < len(derivations) else None
            if d:
                entry.update(d)
            mapped.append(entry)
        derived = [h for c, h in zip(columns, into_vars) if c is None]
        if derived:
            # No token: `mapped` is populated, so the correlation SUCCEEDED and the
            # derived slots are a documented gap inside it, not a failure.
            return mapped, (f"{len(derived)} select-list item(s) name no column (literal "
                            f"or expression) - the value reaching "
                            f"{', '.join(derived)} has no column identity"), None
        return mapped, None, None

    _INTO_END = ("FROM", "WHERE", "ORDER", "GROUP", "HAVING", "FOR")

    @classmethod
    def _exec_into_vars(cls, toks: List[Token]) -> List[str]:
        """The TARGETS of a SELECT/FETCH INTO clause, up to the next clause keyword.

        One target per column, which is the whole point - this list is what the count
        gate weighs against the select list. `INTO :A, :B:IND` names TWO targets, not
        three: the indicator qualifies B rather than occupying a slot of its own
        (`_indicator_at`). A qualified `:GRP . FLD` spans four tokens and resolves to
        FLD; stepping two would take `. FLD` for the next item and report a phantom
        target in the slot after it. Both rules live in `_scan_host_vars`, so the INTO
        clause cannot come to disagree with the statement-wide scan about either.
        """
        i = next((k for k, t in enumerate(toks)
                  if t.kind == "word" and t.up == "INTO"), None)
        if i is None:
            return []
        stop = next((k for k in range(i + 1, len(toks))
                     if toks[k].kind == "word" and toks[k].up in cls._INTO_END),
                    len(toks))
        return [n for n, _ in cls._scan_host_vars(toks, i + 1, stop)]

    @staticmethod
    def _exec_program(toks: List[Token]) -> Tuple[Optional[str], bool]:
        """Pull PROGRAM('NAME') or PROGRAM(name) from a CICS command.

        Returns ``(name, dynamic)``: a quoted operand IS the load-module name; a bare
        word is a data item holding it, so the target is runtime-determined."""
        for idx, t in enumerate(toks):
            if t.kind == "word" and t.up == "PROGRAM":
                for k in range(idx + 1, min(idx + 4, len(toks))):
                    if toks[k].kind == "string":
                        return toks[k].text.strip("'\"").strip(), False
                    if toks[k].kind == "word":
                        return toks[k].up, True
        return None, False

    @staticmethod
    def _exec_handle_conditions(toks: List[Token]) -> List[str]:
        names = [t.up for t in toks if t.kind == "word"
                 and t.up not in ("HANDLE", "CONDITION", "AID")]
        return names

    # -- I/O ---------------------------------------------------------------
    # -- SORT / MERGE ------------------------------------------------------
    _SORT_KEYWORDS = {
        "INPUT", "OUTPUT", "USING", "GIVING", "ON", "ASCENDING", "DESCENDING", "KEY",
        "WITH", "DUPLICATES", "IN", "ORDER", "COLLATING", "SEQUENCE", "IS", "THRU",
        "THROUGH", "PROCEDURE",
    }

    def _sort_name(self):
        """Read a single procedure/file name token (not a keyword or statement starter)."""
        t = self._peek()
        if t and t.kind == "word" and t.up not in self._SORT_KEYWORDS and t.up not in STARTERS:
            return self._next()
        return None

    def parse_sort(self) -> Stmt:
        vt = self._next()
        verb, line = vt.up, vt.line
        parts = [vt.text]
        file_name = None
        nt = self._peek()
        if nt and nt.kind == "word" and nt.up not in STARTERS:
            file_name = self._next().up
            parts.append(file_name)

        in_proc = in_thru = out_proc = out_thru = None
        using: List[str] = []
        giving: List[str] = []

        def read_proc():
            if self._peek() and self._peek().is_word("IS"):
                self._next()
            head = self._sort_name()
            thru = None
            if head and self._peek() and self._peek().up in ("THRU", "THROUGH"):
                self._next()
                tt = self._sort_name()
                thru = tt.up if tt else None
            return (head.up if head else None), thru

        while not self._eof():
            t = self._peek()
            if t.kind == "period":
                break
            if t.kind == "word":
                u = t.up
                if u in STARTERS:
                    break
                if u == "INPUT" and self._peek(1) and self._peek(1).up == "PROCEDURE":
                    self._next(); self._next()
                    in_proc, in_thru = read_proc()
                    continue
                if u == "OUTPUT" and self._peek(1) and self._peek(1).up == "PROCEDURE":
                    self._next(); self._next()
                    out_proc, out_thru = read_proc()
                    continue
                if u in ("USING", "GIVING"):
                    self._next()
                    bucket = using if u == "USING" else giving
                    while True:
                        nm = self._sort_name()
                        if nm is None:
                            break
                        bucket.append(nm.up)
                    continue
            self._next()  # skip ordering noise (ON ASCENDING KEY ..., COLLATING ...)

        return SortStmt(line=line, verb=verb, file=file_name,
                        input_proc=in_proc, input_thru=in_thru,
                        output_proc=out_proc, output_thru=out_thru,
                        using=using, giving=giving, raw=" ".join(parts))

    def parse_io(self) -> Stmt:
        verb_tok = self._next()
        verb = verb_tok.up
        line = verb_tok.line
        endword = "END-" + verb
        file_name = None
        if self._peek() and self._peek().kind == "word" and self._peek().up not in STARTERS:
            file_name = self._next().up
        handlers = {}
        into: Optional[str] = None
        from_: Optional[str] = None

        def _end_key(consume_at: bool) -> str:
            """Consume the END / END-OF-PAGE word after [NOT] AT and return the key stem."""
            if consume_at and self._peek() and self._peek().is_word("AT"):
                self._next()
            t = self._peek()
            if t is not None and t.kind == "word" and t.up in ("END-OF-PAGE", "EOP"):
                self._next()
                return "AT_EOP"
            if t is not None and t.is_word("END"):
                self._next()
            return "AT_END"

        # Skip clause noise (NEXT/KEY/ADVANCING/...) until a handler intro, end, or
        # period; capture INTO/FROM data targets on the way.
        while not self._eof():
            t = self._peek()
            if t.kind == "period":
                break
            if t.kind == "word":
                u = t.up
                if u == endword:
                    self._next()
                    break
                if u == "INTO" or u == "FROM":
                    self._next()
                    nxt = self._peek()
                    if nxt is not None and nxt.kind == "word" and nxt.up not in STARTERS:
                        if u == "INTO":
                            into = self._next().up
                        else:
                            from_ = self._next().up
                    continue
                if u == "AT" or u in ("END-OF-PAGE", "EOP"):
                    key = _end_key(consume_at=(u == "AT")) if u == "AT" else "AT_EOP"
                    if u != "AT":
                        self._next()
                    handlers[key] = self.parse_block(stops={"NOT", "AT", "END"})
                    continue
                if u == "INVALID":
                    self._next()
                    if self._peek() and self._peek().is_word("KEY"):
                        self._next()
                    handlers["INVALID_KEY"] = self.parse_block(stops={"NOT", "INVALID"})
                    continue
                if u == "NOT":
                    self._next()
                    nxt = self._peek()
                    if nxt is not None and (nxt.is_word("AT") or nxt.kind == "word"
                                            and nxt.up in ("END", "END-OF-PAGE", "EOP")):
                        key = "NOT_" + _end_key(consume_at=nxt.is_word("AT"))
                        handlers[key] = self.parse_block(stops={"AT", "INVALID", "NOT"})
                        continue
                    if nxt is not None and nxt.is_word("INVALID"):
                        self._next()
                        if self._peek() and self._peek().is_word("KEY"):
                            self._next()
                        handlers["NOT_INVALID_KEY"] = self.parse_block(
                            stops={"AT", "INVALID", "NOT"})
                        continue
                    continue
                if u in _IO_CLAUSE_WORDS:
                    self._next()  # I/O clause word (READ f NEXT RECORD, AFTER ADVANCING...)
                    continue
                if u in STARTERS:
                    break
            self._next()
        return IoStmt(line=line, verb=verb, file=file_name, handlers=handlers,
                      into=into, from_=from_)


def _perform_kind(control: str, inline: bool):
    up = control.upper()
    test_after = "TEST AFTER" in up or ("AFTER" in up and "TEST" in up and "BEFORE" not in up)
    if "VARYING" in up:
        return "varying", test_after
    if "UNTIL" in up:
        return "until", test_after
    if "TIMES" in up:
        return "times", test_after
    if inline:
        return "inline", test_after
    return "call", test_after
