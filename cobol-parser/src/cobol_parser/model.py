"""Control-flow AST for the PROCEDURE DIVISION.

Only constructs that can alter the order of execution get their own node; a run of
straight-line data manipulation (MOVE/COMPUTE/ADD ...) collapses into ``Action``
nodes that later fold into a state's action list (the reduction principle from
references/cobol-to-statecharts.md). Every node carries the source line for
provenance.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple


@dataclass
class Stmt:
    line: int


@dataclass
class Action(Stmt):
    """Opaque straight-line statement (MOVE, ADD, OPEN, DISPLAY, SET, ...).

    ``text`` is the original spelling; ``verb`` is the leading word. The body is not
    interpreted - it becomes a *named* action reference in the statechart, never an
    inferred implementation.
    """

    text: str
    verb: str


@dataclass
class IfStmt(Stmt):
    cond_text: str
    then_body: List[Stmt] = field(default_factory=list)
    else_body: List[Stmt] = field(default_factory=list)


@dataclass
class EvaluateStmt(Stmt):
    subject: str
    whens: List[Tuple[str, List[Stmt]]] = field(default_factory=list)  # (cond, body)
    other_body: Optional[List[Stmt]] = None


@dataclass
class PerformStmt(Stmt):
    """PERFORM in all its forms.

    kind:
      'call'    - PERFORM p [THRU q]                (call-return into a range)
      'until'   - PERFORM p UNTIL c                 (out-of-line loop)
      'times'   - PERFORM p N TIMES
      'varying' - PERFORM p VARYING ... UNTIL c
      'inline'  - PERFORM ... END-PERFORM           (body is inline_body)
    """

    kind: str
    target: Optional[str] = None
    thru: Optional[str] = None
    control_text: str = ""      # raw UNTIL/VARYING/TIMES clause, for provenance
    test_after: bool = False
    inline_body: List[Stmt] = field(default_factory=list)


@dataclass
class GoToStmt(Stmt):
    targets: List[str]
    depending: bool = False     # GO TO ... DEPENDING ON  -> computed multi-target
    depending_on: Optional[str] = None  # the index variable (target i taken when var = i)


@dataclass
class AlterStmt(Stmt):
    text: str                   # original spelling
    # (altered-paragraph, new-proceed-to-target) pairs. ALTER rewrites the GO TO at
    # the *head* of `altered` so it proceeds to `target` - i.e. a switchable exit.
    pairs: List[Tuple[str, str]] = field(default_factory=list)


@dataclass
class CallStmt(Stmt):
    target: str
    dynamic: bool               # CALL identifier (not a literal) -> target unknown
    on_exception: bool = False
    using: List[str] = field(default_factory=list)      # CALL ... USING args (data passed)
    returning: Optional[str] = None                     # RETURNING receiver
    # BY CONTENT / BY VALUE args (a subset of `using`): passed one-way, the callee's
    # writes are NOT visible to this program - the caller-output claim excludes them.
    by_content: List[str] = field(default_factory=list)
    # ON EXCEPTION / ON OVERFLOW handler bodies, keyed 'ON_EXCEPTION' /
    # 'NOT_ON_EXCEPTION' / 'ON_OVERFLOW': real conditional branches, compiled as
    # guarded edges (the trigger is a runtime condition -> external guard).
    handlers: Dict[str, List[Stmt]] = field(default_factory=dict)


@dataclass
class IoStmt(Stmt):
    """READ / WRITE / REWRITE / DELETE / START with their implicit handlers.

    handlers maps a handler key to the statements guarded by it:
      'AT_END', 'NOT_AT_END', 'INVALID_KEY', 'NOT_INVALID_KEY', 'AT_EOP', 'NOT_AT_EOP'
    The handler edges are control flow that is invisible at the I/O site.
    """

    verb: str
    file: Optional[str]
    handlers: Dict[str, List[Stmt]] = field(default_factory=dict)
    into: Optional[str] = None   # READ/RETURN ... INTO target (data lands here too)
    from_: Optional[str] = None  # WRITE/REWRITE ... FROM source (data comes from here)


@dataclass
class HandledStmt(Stmt):
    """An imperative statement carrying an ON-condition handler phrase: arithmetic with
    [NOT] ON SIZE ERROR, or ACCEPT/DISPLAY with [NOT] ON EXCEPTION. The inner statement
    is the action itself; handlers maps 'ON_SIZE_ERROR' / 'NOT_ON_SIZE_ERROR' /
    'ON_EXCEPTION' / ... to the guarded bodies - real conditional branches whose trigger
    is a runtime condition (compiled as flagged external guards, never hoisted)."""

    inner: Stmt
    handlers: Dict[str, List[Stmt]] = field(default_factory=dict)


@dataclass
class ExecStmt(Stmt):
    """An embedded ``EXEC SQL|CICS|DLI ... END-EXEC`` block, extracted opaquely.

    kind classifies its COBOL control effect:
      'effect'    - no COBOL control transfer (most SQL/CICS/DLI commands)
      'call'      - CICS LINK (call-return into another program)
      'transfer'  - CICS XCTL (transfers out, no return)
      'terminate' - CICS RETURN / ABEND (control leaves this program)
      'handle'    - CICS HANDLE CONDITION/AID (registers implicit later transfer)
    """

    lang: str                   # 'SQL' | 'CICS' | 'DLI'
    verb: str
    text: str                   # raw inner command text
    kind: str = "effect"
    target: Optional[str] = None            # program name for LINK/XCTL
    # PROGRAM(data-name) rather than PROGRAM('literal'): the operand is a data item
    # holding the module name, so the target is runtime-determined - same situation
    # as `CALL identifier`, resolved by the same constant propagation where provable.
    dynamic: bool = False
    host_vars: List[str] = field(default_factory=list)   # :WS-FOO references
    conditions: List[str] = field(default_factory=list)  # HANDLE condition names
    into_vars: List[str] = field(default_factory=list)   # SELECT/FETCH ... INTO targets
    # WHICH COLUMN fills which host variable: [{"column": "BAL", "hostVar": "WS-BAL"}].
    # A field name is program-local, so this is the only thing that proves two programs
    # read the same state. Emitted ONLY when the source proves it (see _correlate).
    columns: List[dict] = field(default_factory=list)
    # The raw select list (None entries = derived expressions). Set for SELECT and for a
    # cursor DECLARE, whose columns must be zipped against a later FETCH's host vars.
    select_list: List[Optional[str]] = field(default_factory=list)
    column_note: Optional[str] = None     # why the columns could NOT be correlated
    # The cursor a FETCH reads, resolved from tokens (a rowset FETCH buries the name
    # behind positioning keywords). It is the join back to the DECLARE that holds the
    # columns, and it names the endpoint the FETCH crosses.
    cursor: Optional[str] = None
    # The DML target table, recorded when a later pass still has work to do on this
    # statement: an INSERT with no explicit column list keeps its table here (with the
    # VALUES slots below) so build time can zip them against the table's DECLARE
    # TABLE / DCLGEN column order. An SQL CALL keeps the procedure name in `target`.
    table: Optional[str] = None
    # One entry per VALUES slot of a column-list-less INSERT: the host variable that
    # fills the slot, or None for a literal/expression slot (which maps to no field).
    values_list: List[Optional[str]] = field(default_factory=list)


@dataclass
class SearchStmt(Stmt):
    """SEARCH / SEARCH ALL with its WHEN branches and AT END handler.

    A serial SEARCH increments an index over ``table`` testing each WHEN until one
    matches (-> its body) or the table is exhausted (-> AT END). The conditional
    branches are real control flow; the index iteration itself is a runtime effect.
    """

    table: str
    all: bool = False                # SEARCH ALL (binary) vs serial SEARCH
    varying: Optional[str] = None
    at_end_body: List[Stmt] = field(default_factory=list)
    whens: List[Tuple[str, List[Stmt]]] = field(default_factory=list)  # (cond, body)


@dataclass
class SortStmt(Stmt):
    """SORT / MERGE with its compiler-inserted control flow.

    A SORT runs (1) its INPUT PROCEDURE (which RELEASEs records) or reads USING files,
    (2) the sort itself, then (3) its OUTPUT PROCEDURE (which RETURNs records, RETURN ...
    AT END signalling exhaustion) or writes GIVING files. The procedures are PERFORMed -
    call-return - so they map exactly like a simple PERFORM.
    """

    verb: str                    # 'SORT' | 'MERGE'
    file: Optional[str] = None   # the sort/merge work file
    input_proc: Optional[str] = None
    input_thru: Optional[str] = None
    output_proc: Optional[str] = None
    output_thru: Optional[str] = None
    using: List[str] = field(default_factory=list)
    giving: List[str] = field(default_factory=list)
    raw: str = ""


@dataclass
class TerminateStmt(Stmt):
    kind: str                   # 'STOP_RUN' | 'GOBACK' | 'EXIT_PROGRAM'


@dataclass
class ExitStmt(Stmt):
    kind: str                   # 'PARAGRAPH'|'SECTION'|'PERFORM'|'PERFORM_CYCLE'|'PLAIN'


@dataclass
class ContinueStmt(Stmt):
    next_sentence: bool = False  # NEXT SENTENCE differs from CONTINUE - flagged


@dataclass
class Paragraph:
    name: str
    line: int
    section: Optional[str] = None
    # The source spelling, when `name` had to be qualified to stay unique. COBOL allows
    # one paragraph name in two SECTIONs; `name` is the machine's state id, this is what
    # the program calls it. None when they are the same.
    bare_name: Optional[str] = None
    origin: Optional[str] = None  # copybook member name if the header came from a COPY
    statements: List[Stmt] = field(default_factory=list)
    parse_error: Optional[str] = None  # set if the body failed to parse (corpus safety)
    # DECLARATIVES handler metadata (set only on the section head of a USE procedure):
    use_trigger: Optional[str] = None   # 'ERROR' | 'EXCEPTION' | 'DEBUGGING' | ...
    use_files: List[str] = field(default_factory=list)  # files it applies to ([]=global)


@dataclass
class Program:
    program_id: str
    paragraphs: List[Paragraph] = field(default_factory=list)
    # DECLARATIVES USE-procedure sections (kept OUT of the main flow; entered on error).
    declaratives: List[Paragraph] = field(default_factory=list)
    # CICS HANDLE CONDITION registrations: (condition-name, target-paragraph).
    cics_handlers: List[tuple] = field(default_factory=list)
    has_procedure_division: bool = False
    # The program's own parameter interface (its perimeter at the entry point):
    using: List[str] = field(default_factory=list)   # PROCEDURE DIVISION USING params
    returning: Optional[str] = None                  # PROCEDURE DIVISION RETURNING
    notes: List[str] = field(default_factory=list)  # parser-level remarks
    # data-name -> initial literal from a WORKING-STORAGE `VALUE 'lit'` clause, used
    # by constant propagation to resolve dynamic CALL targets.
    working_values: Dict[str, str] = field(default_factory=dict)
    # DATA DIVISION recovery (data_division.DataItem); duck-typed to avoid coupling.
    data_items: List = field(default_factory=list)
    data_by_name: Dict[str, object] = field(default_factory=dict)
    # FILE-CONTROL SELECT entries: file -> {assign, organization, access, recordKey,
    # statusField} (the file's external binding and its status-response field).
    files: Dict[str, dict] = field(default_factory=dict)
    # COPY / EXEC SQL INCLUDE dependencies (from the preprocessor): each
    # {member, status, via, replacing}. A compile-time source dependency, not a runtime
    # endpoint - carried so the related-artifact manifest can list copybooks.
    copybooks: List[dict] = field(default_factory=list)
    # Names of programs CONTAINED in this source (nested `PROGRAM-ID ... END PROGRAM`,
    # or a sibling compilation unit in the same member). A CALL to one of these is an
    # INTERNAL call, not an external dependency - so the classifier can tell a contained
    # program (e.g. USEMQ inside FBBMQPNT) apart from a missing external module.
    nested_programs: List[str] = field(default_factory=list)
    # SQL declarations recovered from the WHOLE expanded stream - data division and
    # copybooks included, where production code actually keeps them. The statement
    # parser only walks the PROCEDURE DIVISION, so a cursor DECLAREd in
    # WORKING-STORAGE (the common case: a DCLGEN-adjacent copybook) was invisible to
    # FETCH correlation, and every FETCH on it lost its column identity.
    #   sql_cursors:     [{cursor, selectList, table, line, member}]
    #   declared_tables: [{table, columns, line, member}]  (DECLARE TABLE / DCLGEN)
    sql_cursors: List[dict] = field(default_factory=list)
    declared_tables: List[dict] = field(default_factory=list)


def walk_statements(stmts: List[Stmt]):
    """Yield every statement, descending into IF / EVALUATE / PERFORM-inline / I-O
    handler bodies (needed by whole-program analyses like constant propagation)."""
    for st in stmts:
        yield st
        if isinstance(st, IfStmt):
            yield from walk_statements(st.then_body)
            yield from walk_statements(st.else_body)
        elif isinstance(st, EvaluateStmt):
            for _cond, body in st.whens:
                yield from walk_statements(body)
            if st.other_body:
                yield from walk_statements(st.other_body)
        elif isinstance(st, PerformStmt):
            yield from walk_statements(st.inline_body)
        elif isinstance(st, IoStmt):
            for body in st.handlers.values():
                yield from walk_statements(body)
        elif isinstance(st, CallStmt):
            for body in st.handlers.values():
                yield from walk_statements(body)
        elif isinstance(st, HandledStmt):
            yield from walk_statements([st.inner])
            for body in st.handlers.values():
                yield from walk_statements(body)
        elif isinstance(st, SearchStmt):
            yield from walk_statements(st.at_end_body)
            for _cond, body in st.whens:
                yield from walk_statements(body)
