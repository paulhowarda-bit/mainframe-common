from cobol_parse.parser import parse_program
from cobol_parse.model import (
    EvaluateStmt,
    GoToStmt,
    IfStmt,
    IoStmt,
    PerformStmt,
    SortStmt,
    TerminateStmt,
    AlterStmt,
    CallStmt,
)


def _wrap(proc_body: str) -> str:
    return (
        "       IDENTIFICATION DIVISION.\n"
        "       PROGRAM-ID. T.\n"
        "       PROCEDURE DIVISION.\n" + proc_body
    )


def test_program_id_and_paragraphs():
    prog = parse_program(_wrap(
        "       0000-MAIN.\n"
        "           PERFORM 1000-A\n"
        "           STOP RUN.\n"
        "       1000-A.\n"
        "           DISPLAY 'HI'.\n"
    ))
    assert prog.program_id == "T"
    assert [p.name for p in prog.paragraphs] == ["0000-MAIN", "1000-A"]


def test_sort_captures_input_and_output_procedures():
    prog = parse_program(_wrap(
        "       0000-MAIN.\n"
        "           SORT SORT-FILE\n"
        "               ON ASCENDING KEY S-KEY\n"
        "               INPUT PROCEDURE IS 1000-FILL\n"
        "               OUTPUT PROCEDURE IS 2000-EMIT\n"
        "           STOP RUN.\n"
    ))
    st = prog.paragraphs[0].statements[0]
    assert isinstance(st, SortStmt)
    assert st.verb == "SORT" and st.file == "SORT-FILE"
    assert st.input_proc == "1000-FILL"
    assert st.output_proc == "2000-EMIT"
    assert st.using == [] and st.giving == []


def test_declaratives_split_out_with_use_trigger():
    prog = parse_program(_wrap(
        "       DECLARATIVES.\n"
        "       IO-ERR SECTION.\n"
        "           USE AFTER STANDARD ERROR PROCEDURE ON CUST-FILE.\n"
        "       IO-ERR-HANDLER.\n"
        "           ADD 1 TO WS-ERR.\n"
        "       END DECLARATIVES.\n"
        "       0000-MAIN.\n"
        "           STOP RUN.\n"
    ))
    # the USE section is NOT in the main flow
    assert [p.name for p in prog.paragraphs] == ["0000-MAIN"]
    decl_names = [p.name for p in prog.declaratives]
    assert "IO-ERR" in decl_names and "IO-ERR-HANDLER" in decl_names
    io_err = next(p for p in prog.declaratives if p.name == "IO-ERR")
    assert io_err.use_trigger == "ERROR"
    assert io_err.use_files == ["CUST-FILE"]
    # the USE statement itself is not executable and is dropped
    assert io_err.statements == []


def test_cics_handle_condition_pairs_captured():
    prog = parse_program(_wrap(
        "       0000-MAIN.\n"
        "           EXEC CICS HANDLE CONDITION NOTFND(9000-NF) END-EXEC\n"
        "           STOP RUN.\n"
        "       9000-NF.\n"
        "           DISPLAY 'NF'.\n"
    ))
    assert prog.cics_handlers == [("NOTFND", "9000-NF")]


def test_sort_using_giving_files():
    prog = parse_program(_wrap(
        "       0000-MAIN.\n"
        "           SORT WORK-FILE\n"
        "               USING IN-FILE\n"
        "               GIVING OUT-FILE\n"
        "           STOP RUN.\n"
    ))
    st = prog.paragraphs[0].statements[0]
    assert isinstance(st, SortStmt)
    assert st.input_proc is None and st.output_proc is None
    assert st.using == ["IN-FILE"] and st.giving == ["OUT-FILE"]


def test_perform_until_captures_target_and_control():
    prog = parse_program(_wrap(
        "       0000-MAIN.\n"
        "           PERFORM 2000-P UNTIL WS-EOF = 'Y'\n"
        "           STOP RUN.\n"
    ))
    main = prog.paragraphs[0]
    perform = main.statements[0]
    assert isinstance(perform, PerformStmt)
    assert perform.kind == "until"
    assert perform.target == "2000-P"
    assert "UNTIL" in perform.control_text.upper()
    assert isinstance(main.statements[1], TerminateStmt)


def test_if_then_else_with_goto():
    prog = parse_program(_wrap(
        "       0000-MAIN.\n"
        "           IF WS-X = 1\n"
        "               GO TO 9000-Z\n"
        "           ELSE\n"
        "               MOVE 1 TO WS-Y\n"
        "           END-IF.\n"
    ))
    stmt = prog.paragraphs[0].statements[0]
    assert isinstance(stmt, IfStmt)
    assert "WS-X" in stmt.cond_text
    assert isinstance(stmt.then_body[0], GoToStmt)
    assert stmt.then_body[0].targets == ["9000-Z"]
    assert stmt.else_body[0].__class__.__name__ == "Action"


def test_evaluate_dispatch_with_when_other():
    prog = parse_program(_wrap(
        "       0000-MAIN.\n"
        "           EVALUATE WS-T\n"
        "               WHEN 'D' PERFORM 100-D\n"
        "               WHEN 'W' PERFORM 200-W\n"
        "               WHEN OTHER PERFORM 900-E\n"
        "           END-EVALUATE.\n"
    ))
    ev = prog.paragraphs[0].statements[0]
    assert isinstance(ev, EvaluateStmt)
    assert len(ev.whens) == 2
    assert ev.other_body is not None
    assert isinstance(ev.whens[0][1][0], PerformStmt)


def test_read_at_end_handler():
    prog = parse_program(_wrap(
        "       0000-MAIN.\n"
        "           READ CUST-FILE\n"
        "               AT END MOVE 'Y' TO WS-EOF\n"
        "           END-READ.\n"
    ))
    io = prog.paragraphs[0].statements[0]
    assert isinstance(io, IoStmt)
    assert io.verb == "READ"
    assert io.file == "CUST-FILE"
    assert "AT_END" in io.handlers


def test_alter_and_dynamic_call_recovered():
    prog = parse_program(_wrap(
        "       0000-MAIN.\n"
        "           CALL WS-PGM USING WS-A\n"
        "           ALTER 100-X TO PROCEED TO 200-Y.\n"
    ))
    stmts = prog.paragraphs[0].statements
    assert isinstance(stmts[0], CallStmt)
    assert stmts[0].dynamic is True
    assert isinstance(stmts[1], AlterStmt)
    assert stmts[1].pairs == [("100-X", "200-Y")]


def test_alter_stops_at_following_goto_without_period():
    # ALTER and a following GO TO share a sentence (no period between them).
    prog = parse_program(_wrap(
        "       1000-SWITCH.\n"
        "           GO TO 1100-FIRST.\n"
        "       1100-FIRST.\n"
        "           ALTER 1000-SWITCH TO PROCEED TO 1200-NORMAL\n"
        "           GO TO 1900-DONE.\n"
    ))
    first = prog.paragraphs[1]
    alter, goto = first.statements[0], first.statements[1]
    assert isinstance(alter, AlterStmt)
    assert alter.pairs == [("1000-SWITCH", "1200-NORMAL")]
    assert isinstance(goto, GoToStmt)
    assert goto.targets == ["1900-DONE"]


def test_static_call_is_not_dynamic():
    prog = parse_program(_wrap(
        "       0000-MAIN.\n"
        "           CALL 'SUBPGM' USING WS-A.\n"
    ))
    call = prog.paragraphs[0].statements[0]
    assert isinstance(call, CallStmt)
    assert call.dynamic is False
    assert call.target == "SUBPGM"


def test_no_procedure_division():
    prog = parse_program(
        "       IDENTIFICATION DIVISION.\n"
        "       PROGRAM-ID. T.\n"
    )
    assert prog.has_procedure_division is False
    assert prog.paragraphs == []


def _para(prog, name):
    return next(p for p in prog.paragraphs if p.name == name)


def test_string_without_end_string_does_not_swallow_following_statements():
    # A STRING with no END-STRING terminator must end at the next statement verb,
    # not consume the rest of the paragraph (one-period-per-paragraph style).
    from cobol_parse.model import walk_statements, Action, IfStmt
    prog = parse_program(_wrap(
        "       5000-PROCESS.\n"
        "           STRING WS-A DELIMITED BY SIZE\n"
        "                  WS-B DELIMITED BY SIZE\n"
        "               INTO WS-OUT\n"
        "           MOVE 1 TO WS-FLAG\n"
        "           IF WS-FLAG = 1\n"
        "               PERFORM 6000-NEXT\n"
        "           END-IF\n"
        "           IF WS-A > 0\n"
        "               MOVE 2 TO WS-FLAG\n"
        "           END-IF.\n"
    ))
    stmts = _para(prog, "5000-PROCESS").statements
    ifs = [s for s in walk_statements(stmts) if isinstance(s, IfStmt)]
    assert len(ifs) == 2, "the two IFs after the STRING must survive as real control flow"
    # The STRING itself is still captured as an opaque action, but only its own text.
    string_actions = [s for s in walk_statements(stmts)
                      if isinstance(s, Action) and s.verb == "STRING"]
    assert len(string_actions) == 1
    assert "PERFORM" not in string_actions[0].text  # did not swallow the paragraph


def test_string_with_end_string_and_overflow_keeps_its_imperative():
    # With an explicit END-STRING, the ON OVERFLOW imperative (which contains verbs)
    # belongs to the STRING and must not prematurely terminate the opaque scope.
    from cobol_parse.model import walk_statements, Action, IfStmt
    prog = parse_program(_wrap(
        "       5000-PROCESS.\n"
        "           STRING WS-A DELIMITED BY SIZE INTO WS-OUT\n"
        "               ON OVERFLOW\n"
        "                   MOVE 1 TO WS-ERR\n"
        "                   PERFORM 9000-ERR\n"
        "           END-STRING\n"
        "           MOVE 5 TO WS-DONE\n"
        "           IF WS-DONE = 5\n"
        "               PERFORM 6000-NEXT\n"
        "           END-IF.\n"
    ))
    stmts = _para(prog, "5000-PROCESS").statements
    string_actions = [s for s in walk_statements(stmts)
                      if isinstance(s, Action) and s.verb == "STRING"]
    assert len(string_actions) == 1
    # The overflow imperative stayed inside the STRING opaque text.
    assert "OVERFLOW" in string_actions[0].text
    assert "END-STRING" in string_actions[0].text
    # And the statement AFTER END-STRING is still its own control flow.
    ifs = [s for s in walk_statements(stmts) if isinstance(s, IfStmt)]
    assert len(ifs) == 1


def test_string_without_end_string_inside_if_does_not_eat_end_if():
    from cobol_parse.model import walk_statements, IfStmt, Action
    prog = parse_program(_wrap(
        "       5000-PROCESS.\n"
        "           IF WS-A > 0\n"
        "               STRING WS-A DELIMITED BY SIZE INTO WS-OUT\n"
        "               MOVE 1 TO WS-FLAG\n"
        "           END-IF\n"
        "           PERFORM 6000-NEXT.\n"
    ))
    stmts = _para(prog, "5000-PROCESS").statements
    ifs = [s for s in walk_statements(stmts) if isinstance(s, IfStmt)]
    assert len(ifs) == 1
    # The MOVE lives inside the IF then-body, and the STRING did not eat END-IF.
    string_actions = [s for s in walk_statements(stmts)
                      if isinstance(s, Action) and s.verb == "STRING"]
    assert len(string_actions) == 1
    assert "MOVE" not in string_actions[0].text


# --------------------------------------------------------------------------- #
# ON-condition handlers are real conditional branches (never hoisted)
# --------------------------------------------------------------------------- #

def test_call_on_exception_handler_captured_as_branch():
    from cobol_parse.model import Action, walk_statements
    prog = parse_program(_wrap(
        "       0000-MAIN.\n"
        "           CALL 'SUBPGM' USING BY REFERENCE WS-A BY CONTENT WS-B\n"
        "               ON EXCEPTION MOVE 8 TO WS-RC\n"
        "           END-CALL\n"
        "           MOVE 1 TO WS-OK.\n"
    ))
    stmts = prog.paragraphs[0].statements
    call = next(s for s in walk_statements(stmts) if isinstance(s, CallStmt))
    assert call.using == ["WS-A", "WS-B"]
    assert call.by_content == ["WS-B"]           # BY CONTENT tracked per argument
    assert "ON" not in call.using               # the old 'ON' leak
    assert "ON_EXCEPTION" in call.handlers
    handler_moves = [s for s in call.handlers["ON_EXCEPTION"]
                     if isinstance(s, Action) and "8" in s.text]
    assert handler_moves, "handler imperative must live inside the handler body"
    # the MOVE after END-CALL is unconditional top-level flow, not the handler
    top_moves = [s for s in stmts if isinstance(s, Action) and "WS-OK" in s.text]
    assert top_moves


def test_arith_on_size_error_captured_as_branches():
    from cobol_parse.model import HandledStmt
    prog = parse_program(_wrap(
        "       0000-MAIN.\n"
        "           ADD 1 TO WS-A\n"
        "               ON SIZE ERROR MOVE 9 TO WS-RC\n"
        "               NOT ON SIZE ERROR MOVE 1 TO WS-RC\n"
        "           END-ADD\n"
        "           STOP RUN.\n"
    ))
    st = prog.paragraphs[0].statements[0]
    assert isinstance(st, HandledStmt)
    assert st.inner.verb == "ADD"
    assert "SIZE" not in st.inner.text          # the clause is out of the action text
    assert set(st.handlers) == {"ON_SIZE_ERROR", "NOT_ON_SIZE_ERROR"}


def test_read_next_record_keeps_at_end_handlers():
    prog = parse_program(_wrap(
        "       0000-MAIN.\n"
        "           READ IN-FILE NEXT RECORD\n"
        "               AT END MOVE 'Y' TO WS-EOF\n"
        "               NOT AT END ADD 1 TO WS-CNT\n"
        "           END-READ\n"
        "           STOP RUN.\n"
    ))
    st = prog.paragraphs[0].statements[0]
    assert isinstance(st, IoStmt)
    assert st.file == "IN-FILE"
    assert set(st.handlers) == {"AT_END", "NOT_AT_END"}
    assert st.handlers["AT_END"], "AT END imperative must be inside the handler"


def test_write_at_end_of_page_is_its_own_handler_key():
    prog = parse_program(_wrap(
        "       0000-MAIN.\n"
        "           WRITE OUT-REC FROM WS-LINE\n"
        "               AT END-OF-PAGE MOVE 1 TO WS-EOP\n"
        "           END-WRITE\n"
        "           STOP RUN.\n"
    ))
    st = prog.paragraphs[0].statements[0]
    assert isinstance(st, IoStmt)
    assert st.from_ == "WS-LINE"
    assert set(st.handlers) == {"AT_EOP"}


def test_read_into_and_accept_exception_captured():
    from cobol_parse.model import HandledStmt
    prog = parse_program(_wrap(
        "       0000-MAIN.\n"
        "           READ IN-FILE INTO WS-REC\n"
        "               AT END MOVE 'Y' TO WS-EOF\n"
        "           END-READ\n"
        "           ACCEPT WS-MSG\n"
        "               ON EXCEPTION MOVE 7 TO WS-RC\n"
        "           END-ACCEPT\n"
        "           STOP RUN.\n"
    ))
    rd = prog.paragraphs[0].statements[0]
    assert isinstance(rd, IoStmt) and rd.into == "WS-REC"
    acc = prog.paragraphs[0].statements[1]
    assert isinstance(acc, HandledStmt)
    assert acc.inner.verb == "ACCEPT"
    assert set(acc.handlers) == {"ON_EXCEPTION"}


def test_same_line_paragraph_header_keeps_code():
    prog = parse_program(_wrap(
        "       0000-MAIN. PERFORM 1000-SUB\n"
        "           STOP RUN.\n"
        "       1000-SUB. ADD 1 TO WS-A.\n"
    ))
    assert [p.name for p in prog.paragraphs] == ["0000-MAIN", "1000-SUB"]
    sub = prog.paragraphs[1]
    assert sub.statements, "code on the header line must land in the paragraph body"
    assert "ADD" in sub.statements[0].text.upper()


def test_goto_qualified_target_drops_qualification_only():
    prog = parse_program(_wrap(
        "       0000-MAIN.\n"
        "           GO TO 1000-SUB OF 2000-SEC.\n"
        "       1000-SUB.\n"
        "           STOP RUN.\n"
    ))
    st = prog.paragraphs[0].statements[0]
    assert isinstance(st, GoToStmt)
    assert st.targets == ["1000-SUB"]          # not [1000-SUB, OF, 2000-SEC]


def test_perform_literal_times_keeps_its_inline_body():
    """`PERFORM 5 TIMES ... END-PERFORM`: the count is not a `word`, so the statement
    looked out-of-line and the inline body was never taken - it stayed in the stream and
    became the paragraph's next statements, so the body ran once, AFTER the empty loop."""
    prog = parse_program(_wrap(
        "       0000-MAIN.\n"
        "           PERFORM 5 TIMES\n"
        "               ADD 1 TO WS-A\n"
        "           END-PERFORM\n"
        "           STOP RUN.\n"
    ))
    main = prog.paragraphs[0]
    perform = main.statements[0]
    assert isinstance(perform, PerformStmt)
    assert perform.kind == "times"
    assert perform.target is None
    assert len(perform.inline_body) == 1               # the ADD is INSIDE the loop
    assert perform.inline_body[0].__class__.__name__ == "Action"
    assert isinstance(main.statements[1], TerminateStmt)  # STOP RUN, not the ADD


def test_perform_variable_times_is_not_a_procedure_call():
    """`PERFORM WS-N TIMES`: the identifier before TIMES is the COUNT, not a paragraph.
    Taking it as a target invented a PERFORM of a paragraph WS-N and dropped the count."""
    prog = parse_program(_wrap(
        "       0000-MAIN.\n"
        "           PERFORM WS-N TIMES\n"
        "               ADD 1 TO WS-A\n"
        "           END-PERFORM\n"
        "           STOP RUN.\n"
    ))
    perform = prog.paragraphs[0].statements[0]
    assert perform.target is None
    assert "WS-N" in perform.control_text.upper()      # count survives in the clause
    assert len(perform.inline_body) == 1


def test_perform_procedure_then_times_still_parses_both():
    """`PERFORM P n TIMES` (out-of-line with a count) must keep BOTH: the token after P
    is the count, not TIMES, so the one-token lookahead does not misfire."""
    prog = parse_program(_wrap(
        "       0000-MAIN.\n"
        "           PERFORM 1000-BUMP 3 TIMES\n"
        "           STOP RUN.\n"
    ))
    perform = prog.paragraphs[0].statements[0]
    assert perform.target == "1000-BUMP"
    assert perform.kind == "times"
    assert "3 TIMES" in perform.control_text.upper()
    assert not perform.inline_body


# --------------------------------------------------------------------------- #
# a hyphenated data name must not be mistaken for a PROGRAM-ID
#
# The PROGRAM-ID regex led with `\bPROGRAM-ID\b`, and a word boundary fires between a
# hyphen and a letter - so it matched the tail of a data name like PNET-MQ-PROGRAM-ID
# (an item a copybook can declare) and counted a phantom PROGRAM-ID with no END PROGRAM.
# _split_program_units then ended with depth != 0, tripped its "not well-formed nesting"
# fallback, and returned NO contained programs. The real nested programs vanished, were
# classified `unresolved` rather than `internal-nested`, and got requested from the estate.
# --------------------------------------------------------------------------- #

_NESTED_WITH_HYPHENATED_ITEM = (
    "       IDENTIFICATION DIVISION.\n"
    "       PROGRAM-ID. FBMMAAIO.\n"
    "       DATA DIVISION.\n"
    "       WORKING-STORAGE SECTION.\n"
    "       01  PNET-MQ-AREA.\n"
    "           05  PNET-MQ-PROGRAM-ID   PIC X(8).\n"   # the false PROGRAM-ID match
    "       PROCEDURE DIVISION.\n"
    "       0000-MAIN.\n"
    "           CALL 'USECICS'\n"
    "           GOBACK.\n"
    "       IDENTIFICATION DIVISION.\n"
    "       PROGRAM-ID. USECICS.\n"
    "       PROCEDURE DIVISION.\n"
    "       0000-USECICS.\n"
    "           GOBACK.\n"
    "       END PROGRAM USECICS.\n"
    "       END PROGRAM FBMMAAIO.\n"
)


def test_hyphenated_data_name_does_not_hide_a_nested_program():
    prog = parse_program(_NESTED_WITH_HYPHENATED_ITEM)
    assert prog.program_id == "FBMMAAIO"
    assert prog.nested_programs == ["USECICS"], (
        "a data item ending in -PROGRAM-ID was counted as a program, breaking the "
        "contained-program walk")


def test_find_program_id_ignores_a_hyphenated_data_name():
    """Even if the DATA DIVISION item came first, the leading hyphen must keep it from
    being read as the program's own id."""
    from cobol_parse.parser import _find_program_id
    from cobol_parse.normalizer import normalize
    lines = normalize(
        "       01  WS-SAVED-PROGRAM-ID PIC X(8).\n"
        "       PROGRAM-ID. REALPGM.\n")
    assert _find_program_id(lines) == "REALPGM"


# -- a literal is data, not clause boundaries (audit finding #12) -----------

def test_select_clause_survives_a_dataset_literal_containing_select():
    """`ASSIGN TO 'PROD.SELECT.DATA'` cut THIS entry's body at the SELECT inside its own
    literal - losing the dataset, the ORGANIZATION, and the FILE STATUS binding that
    the JCL join and the perimeter both need."""
    src = (
        "       IDENTIFICATION DIVISION.\n"
        "       PROGRAM-ID. T.\n"
        "       ENVIRONMENT DIVISION.\n"
        "       INPUT-OUTPUT SECTION.\n"
        "       FILE-CONTROL.\n"
        "           SELECT F1 ASSIGN TO 'PROD.SELECT.DATA'\n"
        "               ORGANIZATION IS SEQUENTIAL\n"
        "               FILE STATUS IS WS-FS1.\n"
        "           SELECT F2 ASSIGN TO OUTDD.\n"
        "       DATA DIVISION.\n"
        "       FILE SECTION.\n"
        "       FD  F1.\n"
        "       01  R1  PIC X(10).\n"
        "       FD  F2.\n"
        "       01  R2  PIC X(10).\n"
        "       WORKING-STORAGE SECTION.\n"
        "       01  WS-FS1  PIC XX.\n"
        "       PROCEDURE DIVISION.\n"
        "       0000-MAIN.\n"
        "           STOP RUN.\n")
    files = parse_program(src).files
    assert files["F1"]["assign"] == "PROD.SELECT.DATA"
    assert files["F1"]["organization"] == "SEQUENTIAL"
    assert files["F1"]["statusField"] == "WS-FS1"
    # ...and the literal did not swallow the NEXT entry either.
    assert files["F2"]["assign"] == "OUTDD"
