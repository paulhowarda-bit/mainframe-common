from cobol_parser.parser import parse_program
from cobol_parser.model import (
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
    from cobol_parser.model import walk_statements, Action, IfStmt
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
    from cobol_parser.model import walk_statements, Action, IfStmt
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
    from cobol_parser.model import walk_statements, IfStmt, Action
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
    from cobol_parser.model import Action, walk_statements
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
    from cobol_parser.model import HandledStmt
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
    from cobol_parser.model import HandledStmt
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
    "       PROGRAM-ID. SAMPMAIN.\n"
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
    "       END PROGRAM SAMPMAIN.\n"
)


def test_hyphenated_data_name_does_not_hide_a_nested_program():
    prog = parse_program(_NESTED_WITH_HYPHENATED_ITEM)
    assert prog.program_id == "SAMPMAIN"
    assert prog.nested_programs == ["USECICS"], (
        "a data item ending in -PROGRAM-ID was counted as a program, breaking the "
        "contained-program walk")


def test_find_program_id_ignores_a_hyphenated_data_name():
    """Even if the DATA DIVISION item came first, the leading hyphen must keep it from
    being read as the program's own id."""
    from cobol_parser.parser import _find_program_id
    from cobol_parser.normalizer import normalize
    lines = normalize(
        "       01  WS-SAVED-PROGRAM-ID PIC X(8).\n"
        "       PROGRAM-ID. REALPGM.\n")
    assert _find_program_id(lines) == ("REALPGM", 2)


# --------------------------------------------------------------------------- #
# a quoted program name, and a name on the line after `PROGRAM-ID.`
#
# The PROGRAM-ID regex required the name to follow the optional period immediately and on
# the SAME line - it is applied per CodeLine, so its `\s*` never crosses one. So
# `PROGRAM-ID. 'MYPGM'.` stopped at the apostrophe, and `PROGRAM-ID.` with the name on
# the next card could not match at all. Both are valid COBOL and both occur in the estate
# (roughly 4 quoted to 1 next-line). The file otherwise parsed perfectly - paragraphs,
# working storage, COPY references and CALLs were all extracted - so only the program's
# own name was lost, and it came back as the same `RECOVERED` sentinel a file that names
# no program gets. Downstream the two are indistinguishable: the program acquires no
# identity and every CALL to it reads as a module missing from the estate.
# --------------------------------------------------------------------------- #

def test_a_program_name_on_the_line_after_program_id_is_read():
    """`PROGRAM-ID.` alone on its card with the name on the next one.
    The reported line is the NAME's, not the `PROGRAM-ID` token's."""
    from cobol_parser.parser import _find_program_id
    from cobol_parser.normalizer import normalize
    lines = normalize(
        "       IDENTIFICATION DIVISION.\n"
        "       PROGRAM-ID.\n"
        "       MYPGM.\n")
    assert _find_program_id(lines) == ("MYPGM", 3)


def test_a_program_name_is_read_across_a_blank_line():
    from cobol_parser.parser import _find_program_id
    from cobol_parser.normalizer import normalize
    lines = normalize(
        "       IDENTIFICATION DIVISION.\n"
        "       PROGRAM-ID.\n"
        "\n"
        "       MYPGM.\n")
    assert _find_program_id(lines) == ("MYPGM", 4)


def test_a_quoted_program_name_is_read():
    """`PROGRAM-ID. 'MYPGM'.` - a program-name literal, in either quote character."""
    from cobol_parser.parser import _find_program_id
    from cobol_parser.normalizer import normalize
    single = normalize(
        "       IDENTIFICATION DIVISION.\n"
        "       PROGRAM-ID. 'MYPGM'.\n")
    double = normalize(
        "       IDENTIFICATION DIVISION.\n"
        '       PROGRAM-ID. "MYPGM".\n')
    assert _find_program_id(single) == ("MYPGM", 2)
    assert _find_program_id(double) == ("MYPGM", 2)


def test_a_quoted_program_name_is_read_on_the_continuation_line():
    """The two shapes combined: a bare `PROGRAM-ID.` whose next card carries a literal."""
    from cobol_parser.parser import _find_program_id
    from cobol_parser.normalizer import normalize
    lines = normalize(
        "       IDENTIFICATION DIVISION.\n"
        "       PROGRAM-ID.\n"
        "       'MYPGM'.\n")
    assert _find_program_id(lines) == ("MYPGM", 3)


def test_a_hyphenated_data_name_still_yields_the_sentinel():
    """The widened patterns must not undo the `(?<!-)` guard: a copybook declaring an item
    ending in -PROGRAM-ID names no program."""
    from cobol_parser.parser import _find_program_id
    from cobol_parser.normalizer import normalize
    lines = normalize(
        "       01  PNET-MQ-AREA.\n"
        "           05  PNET-MQ-PROGRAM-ID   PIC X(8).\n")
    assert _find_program_id(lines) == ("RECOVERED", 0)


def test_a_program_id_in_a_comment_banner_still_yields_the_sentinel():
    """A copybook whose only PROGRAM-ID token is prose in a banner. The normalizer drops
    column-7 comment cards before the scan ever sees them, so no pattern can reach it."""
    from cobol_parser.parser import _find_program_id
    from cobol_parser.normalizer import normalize
    lines = normalize(
        "      *    PROGRAM-ID. BANNERPG.\n"
        "       01  WS-FLAG   PIC X.\n")
    assert _find_program_id(lines) == ("RECOVERED", 0)


def test_a_bare_program_id_does_not_absorb_the_following_statement():
    """The forward scan gives up rather than taking whatever follows, so a malformed
    IDENTIFICATION DIVISION returns the sentinel and not `PROCEDURE`."""
    from cobol_parser.parser import _find_program_id
    from cobol_parser.normalizer import normalize
    lines = normalize(
        "       IDENTIFICATION DIVISION.\n"
        "       PROGRAM-ID.\n"
        "       PROCEDURE DIVISION.\n")
    assert _find_program_id(lines) == ("RECOVERED", 0)


def test_a_bare_program_id_does_not_absorb_an_id_division_paragraph():
    """The single-token case the two-word `PROCEDURE DIVISION.` test cannot reach. Every
    optional IDENTIFICATION DIVISION paragraph header is one word plus a period - the exact
    shape of a bare program name, and the card that really follows PROGRAM-ID - so each must
    leave the name unrecovered rather than become it."""
    from cobol_parser.parser import _find_program_id
    from cobol_parser.normalizer import normalize
    for para in ("AUTHOR", "INSTALLATION", "DATE-WRITTEN", "DATE-COMPILED",
                 "SECURITY", "REMARKS"):
        lines = normalize(
            "       IDENTIFICATION DIVISION.\n"
            "       PROGRAM-ID.\n"
            "       " + para + ".\n"
            "           J SMITH.\n")
        assert _find_program_id(lines) == ("RECOVERED", 0), para


def test_a_program_named_like_an_id_division_paragraph_is_still_read_on_its_own_line():
    """The guard is on the forward scan only. A program legitimately named AUTHOR on the
    same card as PROGRAM-ID is a name, not a paragraph header, and still reads."""
    from cobol_parser.parser import _find_program_id
    from cobol_parser.normalizer import normalize
    lines = normalize(
        "       IDENTIFICATION DIVISION.\n"
        "       PROGRAM-ID. AUTHOR.\n")
    assert _find_program_id(lines) == ("AUTHOR", 2)


def test_a_bare_program_id_on_the_last_line_yields_the_sentinel():
    """There is no next card to scan; the forward scan must fall through, not raise."""
    from cobol_parser.parser import _find_program_id
    from cobol_parser.normalizer import normalize
    lines = normalize(
        "       IDENTIFICATION DIVISION.\n"
        "       PROGRAM-ID.\n")
    assert _find_program_id(lines) == ("RECOVERED", 0)


def test_the_ordinary_same_line_program_id_is_unchanged():
    """GUARD (passes before and after)."""
    from cobol_parser.parser import _find_program_id
    from cobol_parser.normalizer import normalize
    lines = normalize(
        "       IDENTIFICATION DIVISION.\n"
        "       PROGRAM-ID. GOODPGM.\n")
    assert _find_program_id(lines) == ("GOODPGM", 2)
    assert parse_program(_wrap("       0000-MAIN.\n"
                               "           GOBACK.\n")).program_id == "T"


_NESTED_WITH_QUOTED_NAMES = (
    "       IDENTIFICATION DIVISION.\n"
    "       PROGRAM-ID. 'OUTERPGM'.\n"
    "       PROCEDURE DIVISION.\n"
    "       0000-MAIN.\n"
    "           CALL 'INNERPGM'\n"
    "           GOBACK.\n"
    "       IDENTIFICATION DIVISION.\n"
    "       PROGRAM-ID. 'INNERPGM'.\n"
    "       PROCEDURE DIVISION.\n"
    "       0000-INNER.\n"
    "           GOBACK.\n"
    "       END PROGRAM INNERPGM.\n"
    "       END PROGRAM OUTERPGM.\n"
)


def test_a_quoted_contained_program_is_split_out():
    """_split_program_units shares the widened pattern, so quoted names now count towards
    its nesting depth too: before the fix this file counted ZERO PROGRAM-IDs, took the
    single-program no-op path, and folded the inner program's paragraphs into the outer
    one while reporting no contained programs at all."""
    prog = parse_program(_NESTED_WITH_QUOTED_NAMES)
    assert prog.program_id == "OUTERPGM"
    assert prog.nested_programs == ["INNERPGM"]
    assert [p.name for p in prog.paragraphs] == ["0000-MAIN"], (
        "the contained program's body folded into the outer program")


def test_a_data_item_named_program_id_does_not_mask_the_real_name():
    """`01  PROGRAM-ID.` as a group item carries no name on its own card, so it matches the
    bare pattern - and the bare branch returns unconditionally. The real PROGRAM-ID earlier
    in the file is still what comes back, because the scan reaches it first."""
    from cobol_parser.parser import _find_program_id
    from cobol_parser.normalizer import normalize
    lines = normalize(
        "       IDENTIFICATION DIVISION.\n"
        "       PROGRAM-ID. REALPGM.\n"
        "       DATA DIVISION.\n"
        "       WORKING-STORAGE SECTION.\n"
        "       01  PROGRAM-ID.\n"
        "           05  WS-NAME   PIC X(8).\n")
    assert _find_program_id(lines) == ("REALPGM", 2)


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


# -- a VALUE clause belongs to its whole entry, not to one physical line ---------

_SPLIT_VALUES = (
    "       IDENTIFICATION DIVISION.\n"
    "       PROGRAM-ID. TSTSPLIT.\n"
    "       DATA DIVISION.\n"
    "       WORKING-STORAGE SECTION.\n"
    "       01  WS-ONELINE        PIC X(08) VALUE 'AAAAMOD1'.\n"
    "       01  WS-SPLIT-VALUE    PIC X(08)\n"
    "                             VALUE 'BBBBMOD2'.\n"
    "       01  WS-SPLIT-LITERAL  PIC X(08) VALUE\n"
    "                             'CCCCMOD3'.\n"
    "       01  WS-CONSTANTS.\n"
    "           05  CN-DEMOC104   PIC X(08).\n"
    "               88  DEMOC104-MODULE  VALUE 'DEMOC104'.\n"
    "       01  WS-COUNT          PIC 9(04) VALUE 1234.\n"
    "       PROCEDURE DIVISION.\n"
    "       0000-MAIN.\n"
    "           CALL WS-ONELINE.\n"
    "           CALL WS-SPLIT-VALUE.\n"
    "           CALL WS-SPLIT-LITERAL.\n"
    "           GOBACK.\n")


def test_working_values_reads_a_value_clause_carried_onto_a_later_line():
    """A data description entry runs to its terminating period, across physical lines
    with no continuation indicator. Scanned line by line, `VALUE 'BBBBMOD2'.` under
    `01 WS-SPLIT-VALUE PIC X(08)` was never seen, and a dynamic CALL through it - whose
    target the source fixes - was reported as runtime-determined."""
    values = parse_program(_SPLIT_VALUES).working_values
    assert values["WS-ONELINE"] == "AAAAMOD1"
    assert values["WS-SPLIT-VALUE"] == "BBBBMOD2"
    assert values["WS-SPLIT-LITERAL"] == "CCCCMOD3"


def test_working_values_keeps_condition_names_and_excludes_numeric_values():
    """The two things the per-line scan did that the entry-based one must keep doing: a
    level-88 string VALUE is recorded under the condition name, and a numeric VALUE is
    not a string literal, so it is not recorded at all."""
    values = parse_program(_SPLIT_VALUES).working_values
    assert values["DEMOC104-MODULE"] == "DEMOC104"
    assert "WS-COUNT" not in values


# --------------------------------------------------------------------------- #
# Db2 cursor DECLARE attributes
#
# `DECLARE cursor-name [ASENSITIVE|INSENSITIVE|SENSITIVE STATIC|SENSITIVE DYNAMIC]
#  [NO SCROLL|SCROLL] CURSOR [WITH HOLD] ... FOR select`. Binding on "CURSOR is the
# token after the name" refuses the whole statement, and refusing it is not a harmless
# miss: no cursor is registered, so every FETCH on it loses its columns, its endpoint
# becomes a phantom `<cursor NAME>` instead of the real table, and the diagnosis blames
# an absent copybook for a DECLARE sitting in the source.
# --------------------------------------------------------------------------- #


def _declare(attrs: str) -> str:
    return _wrap(
        "       0000-MAIN.\n"
        "           EXEC SQL DECLARE CSR1 " + attrs + " CURSOR FOR\n"
        "             SELECT A, B FROM T_ACCT\n"
        "           END-EXEC.\n"
        "           STOP RUN.\n"
    )


def test_declare_cursor_accepts_scroll_and_sensitivity_attributes():
    """Db2 allows an attribute run between the cursor NAME and the CURSOR keyword,
    exactly as it allows a positioning run between FETCH and its cursor name. The four
    forms the estate actually uses are INSENSITIVE SCROLL, SCROLL, SENSITIVE STATIC
    SCROLL and SENSITIVE DYNAMIC SCROLL; the rest are here for grammar coverage."""
    for attrs in ("", "INSENSITIVE", "ASENSITIVE", "INSENSITIVE SCROLL",
                  "SENSITIVE STATIC", "SENSITIVE DYNAMIC SCROLL", "NO SCROLL",
                  "SCROLL"):
        prog = parse_program(_declare(attrs))
        # whole-stream scan path -> Program.sql_cursors
        assert [d["cursor"] for d in prog.sql_cursors] == ["CSR1"], attrs
        assert prog.sql_cursors[0]["selectList"] == ["A", "B"], attrs
        assert prog.sql_cursors[0]["table"] == "T_ACCT", attrs
        # statement path -> ExecStmt
        st = prog.paragraphs[0].statements[0]
        assert st.verb == "DECLARE" and st.cursor == "CSR1", attrs
        assert st.select_list == ["A", "B"], attrs


def test_holdability_still_follows_the_cursor_keyword():
    """WITH HOLD / WITH RETURN come AFTER CURSOR and so are not attributes. They are
    deliberately absent from the skip set - putting them in would only let a malformed
    statement slide - and this pins that the real form still parses."""
    prog = parse_program(_wrap(
        "       0000-MAIN.\n"
        "           EXEC SQL DECLARE CSR1 INSENSITIVE SCROLL CURSOR WITH HOLD FOR\n"
        "             SELECT A FROM T_ACCT\n"
        "           END-EXEC.\n"
        "           STOP RUN.\n"
    ))
    assert [d["cursor"] for d in prog.sql_cursors] == ["CSR1"]
    assert prog.sql_cursors[0]["table"] == "T_ACCT"


def test_declare_cursor_name_may_be_an_attribute_keyword():
    """GUARD (passes before and after). The name is positional, so it must never be
    tested against the attribute set - an implementation that starts skipping at i+1
    instead of i+2 would swallow the name and break this."""
    prog = parse_program(_wrap(
        "       0000-MAIN.\n"
        "           EXEC SQL DECLARE SCROLL CURSOR FOR SELECT A FROM T END-EXEC\n"
        "           STOP RUN.\n"
    ))
    assert [d["cursor"] for d in prog.sql_cursors] == ["SCROLL"]


def test_declare_cursor_across_fixed_format_lines_with_underscored_name():
    """The real estate shape (IDS662C): DECLARE and the attributes are on SEPARATE
    lines and the name carries underscores, which `lexer._is_word_char` admits. A
    single-line grep finds none of these, which is part of why it survived."""
    prog = parse_program(_wrap(
        "       0000-MAIN.\n"
        "           EXEC SQL DECLARE WC_TRDS_AND_CNFMS_ACCT_ASC\n"
        "                INSENSITIVE SCROLL CURSOR FOR\n"
        "                SELECT A, B FROM T_DDPN_PENDCA\n"
        "           END-EXEC\n"
        "           STOP RUN.\n"
    ))
    assert [d["cursor"] for d in prog.sql_cursors] == ["WC_TRDS_AND_CNFMS_ACCT_ASC"]
    assert prog.sql_cursors[0]["table"] == "T_DDPN_PENDCA"


def test_non_cursor_declares_still_name_no_cursor():
    """GUARD (passes before and after). DECLARE TABLE / STATEMENT / GLOBAL TEMPORARY
    TABLE must stay refused - they hold no select list to zip, and DECLARE TABLE is
    routed to declared_tables instead.

    Noted in passing and deliberately NOT asserted: for the GLOBAL TEMPORARY TABLE
    form `declared_tables` records the table as "GLOBAL". That is a separate
    pre-existing defect in `_declare_table_columns`, out of scope here, and this test
    must not be read as blessing it.
    """
    for stmt in ("DECLARE T_ACCT TABLE ( A CHAR(8), B CHAR(4) )",
                 "DECLARE S1 STATEMENT",
                 "DECLARE GLOBAL TEMPORARY TABLE TMP1 ( A CHAR(8) )"):
        prog = parse_program(_wrap(
            "       0000-MAIN.\n"
            "           EXEC SQL " + stmt + " END-EXEC\n"
            "           STOP RUN.\n"
        ))
        assert prog.sql_cursors == [], stmt
    # ...and the DECLARE TABLE case still lands in the other bucket.
    prog = parse_program(_wrap(
        "       0000-MAIN.\n"
        "           EXEC SQL DECLARE T_ACCT TABLE ( A CHAR(8) ) END-EXEC\n"
        "           STOP RUN.\n"
    ))
    assert [e["table"] for e in prog.declared_tables] == ["T_ACCT"]


def test_malformed_declare_does_not_run_off_the_token_stream():
    """GUARD (passes before and after). A truncated attribute run must return None
    rather than raise; the skip loop's bound is what makes that safe."""
    prog = parse_program(_wrap(
        "       0000-MAIN.\n"
        "           EXEC SQL DECLARE CSR1 NO SCROLL END-EXEC\n"
        "           STOP RUN.\n"
    ))
    assert prog.sql_cursors == []


# --------------------------------------------------------------------------- #
# WHICH form of `FOR` a cursor DECLARE used
#
# `DECLARE c CURSOR FOR SELECT ...` has a statically knowable select list.
# `DECLARE c CURSOR FOR DYNSTMT` names a PREPAREd statement whose select list does not
# exist until run time. Both leave `selectList` empty in the second case - and so does
# a FAILED parse, which is why the form has to be recorded as POSITIVE evidence rather
# than inferred from emptiness. Without it the two are indistinguishable, and the
# dynamic one gets reported as a recovery failure that a copybook could fix.
# --------------------------------------------------------------------------- #


def test_declare_for_records_positive_evidence_for_both_forms():
    prog = parse_program(_wrap(
        "       0000-MAIN.\n"
        "           EXEC SQL DECLARE DYN-CSR CURSOR FOR DYNSTMT END-EXEC\n"
        "           EXEC SQL DECLARE STA-CSR CURSOR FOR\n"
        "               SELECT A, B FROM T_S\n"
        "           END-EXEC\n"
        "           STOP RUN.\n"
    ))
    by_cursor = {c["cursor"]: c for c in prog.sql_cursors}
    assert by_cursor["DYN-CSR"]["forKind"] == "statement"
    assert by_cursor["DYN-CSR"]["forStatement"] == "DYNSTMT"
    assert by_cursor["DYN-CSR"]["selectList"] == []      # empty either way...
    assert by_cursor["STA-CSR"]["forKind"] == "select"   # ...so the FORM is the evidence
    assert by_cursor["STA-CSR"]["forStatement"] is None
    # the statement path agrees with the whole-stream scan
    decls = [st for st in prog.paragraphs[0].statements
             if getattr(st, "verb", None) == "DECLARE"]
    assert [(d.cursor, d.cursor_for_kind, d.cursor_for_statement) for d in decls] == [
        ("DYN-CSR", "statement", "DYNSTMT"), ("STA-CSR", "select", None)]


def test_declare_for_is_unknown_rather_than_static_when_no_for_is_reached():
    """The distinction the whole field exists to make. A DECLARE with no FOR at all
    must be None - UNKNOWN - and never "select"; claiming static here would assert a
    select list that was never seen."""
    prog = parse_program(_wrap(
        "       0000-MAIN.\n"
        "           EXEC SQL DECLARE C6 CURSOR END-EXEC\n"
        "           STOP RUN.\n"
    ))
    assert prog.sql_cursors[0]["forKind"] is None
    assert prog.sql_cursors[0]["forStatement"] is None


def test_declare_for_reads_the_first_for_across_every_db2_shape():
    """The attribute keywords (INSENSITIVE, SCROLL, WITH HOLD, WITH RETURN TO CALLER,
    ROWSET POSITIONING) all PRECEDE the FOR, and a trailing `FOR FETCH ONLY` /
    `FOR 10 ROWS` FOLLOWS the select list - so the first FOR is the right one for every
    shape in this estate. A CTE (`FOR WITH R AS (...) SELECT ...`) is static too."""
    cases = [
        ("FOR DYNSTMT",                                   ("statement", "DYNSTMT")),
        ("FOR SELECT A , B FROM T",                       ("select", None)),
        ("FOR SELECT A FROM T FOR FETCH ONLY",            ("select", None)),
        ("FOR WITH R AS ( SELECT A FROM T ) SELECT A FROM R", ("select", None)),
        ("WITH HOLD FOR DYN-SELECT",                      ("statement", "DYN-SELECT")),
        ("WITH ROWSET POSITIONING FOR SELECT A FROM T",   ("select", None)),
        ("WITH RETURN TO CALLER FOR SELECT A FROM T",     ("select", None)),
        ("FOR    DYN-SELECT",                             ("statement", "DYN-SELECT")),
        ("FOR : WS-STMT",                                 (None, None)),
    ]
    for tail, expected in cases:
        prog = parse_program(_wrap(
            "       0000-MAIN.\n"
            "           EXEC SQL DECLARE CX CURSOR " + tail + " END-EXEC\n"
            "           STOP RUN.\n"
        ))
        got = (prog.sql_cursors[0]["forKind"], prog.sql_cursors[0]["forStatement"])
        assert got == expected, (tail, got)


def test_declare_for_survives_the_attribute_run_between_name_and_cursor():
    """The two cursor-DECLARE fixes compose: an attribute-qualified DYNAMIC cursor is
    the shape that was invisible twice over."""
    prog = parse_program(_wrap(
        "       0000-MAIN.\n"
        "           EXEC SQL DECLARE C4 INSENSITIVE SCROLL CURSOR FOR DYNSTMT\n"
        "           END-EXEC\n"
        "           STOP RUN.\n"
    ))
    assert [(c["cursor"], c["forKind"], c["forStatement"]) for c in prog.sql_cursors] \
        == [("C4", "statement", "DYNSTMT")]


def test_a_statement_form_is_claimed_without_a_matching_prepare():
    """Deliberate, not a gap: a cursor may name a statement PREPAREd under a different
    name, and a `DECLARE X STATEMENT` that is never PREPAREd still has no statically
    knowable select list. Requiring a matching PREPARE would re-hide those."""
    prog = parse_program(_wrap(
        "       0000-MAIN.\n"
        "           EXEC SQL DECLARE C1 CURSOR FOR NEVER-PREPARED END-EXEC\n"
        "           STOP RUN.\n"
    ))
    assert prog.sql_cursors[0]["forKind"] == "statement"
    assert prog.sql_cursors[0]["forStatement"] == "NEVER-PREPARED"


def test_non_cursor_declares_carry_no_for_form():
    """A DECLARE TABLE never reaches the FOR reader at all - `_exec_declare_cursor`
    refuses it first - so it must contribute no cursor row and no form."""
    prog = parse_program(_wrap(
        "       0000-MAIN.\n"
        "           EXEC SQL DECLARE T_ACCT TABLE ( A CHAR(8) ) END-EXEC\n"
        "           STOP RUN.\n"
    ))
    assert prog.sql_cursors == []
    assert [e["table"] for e in prog.declared_tables] == ["T_ACCT"]


# --------------------------------------------------------------------------- #
# Db2 column mapping: paren depth, refusal TOKENS, and the shapes that used to
# fall out in silence. (Upstream ledger batch 2, items 9, 10, 11 and 13.)
# --------------------------------------------------------------------------- #


def test_a_cte_declare_returns_the_outer_select_list():
    """A CTE's inner select sits at paren depth 1. Returning ITS columns hands the
    caller another statement's list, which then fails the FETCH arity gate and costs
    the whole statement its mapping."""
    prog = parse_program(_wrap(
        "       0000-MAIN.\n"
        "           EXEC SQL DECLARE CSR1 CURSOR FOR\n"
        "             WITH X (P, Q) AS (SELECT P, Q FROM T1)\n"
        "             SELECT C, D, E FROM T2\n"
        "           END-EXEC.\n"
        "           STOP RUN.\n"
    ))
    assert prog.paragraphs[0].statements[0].select_list == ["C", "D", "E"]


def test_a_scalar_subquery_in_the_select_list_does_not_truncate_it():
    """Three slots, not one: the subquery's own FROM sits deeper than the select list
    it lives in, so it must not terminate the scan."""
    prog = parse_program(_wrap(
        "       0000-MAIN.\n"
        "           EXEC SQL SELECT A, (SELECT MAX(X) FROM T2), C\n"
        "             INTO :H1, :H2, :H3 FROM T1 END-EXEC.\n"
        "           STOP RUN.\n"
    ))
    st = prog.paragraphs[0].statements[0]
    assert st.select_list == ["A", None, "C"]
    assert [c.get("hostVar") for c in st.columns] == ["H1", "H2", "H3"]


def test_a_parenthesised_fullselect_still_yields_its_columns():
    """A fullselect wrapped entirely in parens has NO depth-0 SELECT. Returning []
    here would convert a loud arity refusal into a silent empty mapping."""
    prog = parse_program(_wrap(
        "       0000-MAIN.\n"
        "           EXEC SQL DECLARE CSR1 CURSOR FOR (SELECT A, B FROM T)\n"
        "           END-EXEC.\n"
        "           STOP RUN.\n"
    ))
    assert prog.paragraphs[0].statements[0].select_list == ["A", "B"]


def test_an_ordinary_select_is_unchanged():
    """Regression guard: the depth-0 path is what almost every statement takes."""
    prog = parse_program(_wrap(
        "       0000-MAIN.\n"
        "           EXEC SQL SELECT A, B INTO :H1, :H2 FROM T END-EXEC.\n"
        "           STOP RUN.\n"
    ))
    st = prog.paragraphs[0].statements[0]
    assert st.select_list == ["A", "B"]
    assert st.columns == [{"column": "A", "hostVar": "H1"},
                          {"column": "B", "hostVar": "H2"}]
    assert st.column_note is None and st.column_unresolved is None


def test_an_insert_arity_refusal_publishes_a_token_not_only_prose():
    """A consumer must be able to branch on the REASON without pattern-matching on
    prose that was never a contract."""
    prog = parse_program(_wrap(
        "       0000-MAIN.\n"
        "           EXEC SQL INSERT INTO T (A, B)\n"
        "             VALUES (:H1, :H2, :H3) END-EXEC.\n"
        "           STOP RUN.\n"
    ))
    st = prog.paragraphs[0].statements[0]
    assert st.column_unresolved == "count-mismatch"
    assert st.column_note and st.columns == []


def test_a_select_into_arity_refusal_publishes_a_token():
    """The same failure down the other parser path, carrying the same token."""
    prog = parse_program(_wrap(
        "       0000-MAIN.\n"
        "           EXEC SQL SELECT A, B INTO :H1, :H2, :H3 FROM T END-EXEC.\n"
        "           STOP RUN.\n"
    ))
    st = prog.paragraphs[0].statements[0]
    assert st.column_unresolved == "count-mismatch"


def test_an_insert_from_a_fullselect_publishes_its_own_token():
    """Not an arity failure - a different reason, so a different token."""
    prog = parse_program(_wrap(
        "       0000-MAIN.\n"
        "           EXEC SQL INSERT INTO T (A, B) SELECT P, Q FROM S END-EXEC.\n"
        "           STOP RUN.\n"
    ))
    assert prog.paragraphs[0].statements[0].column_unresolved == "insert-from-fullselect"


def test_an_unparseable_values_list_publishes_its_own_token():
    """The third parser-side refusal wording. Untokenised it was the one reason a
    consumer could only reach by matching prose, since unlike the other two it names
    no counts to key on."""
    prog = parse_program(_wrap(
        "       0000-MAIN.\n"
        "           EXEC SQL INSERT INTO T (A, B) VALUES :H END-EXEC.\n"
        "           STOP RUN.\n"
    ))
    st = prog.paragraphs[0].statements[0]
    assert st.column_unresolved == "values-unparseable"
    assert st.column_note and st.columns == []


def test_a_data_change_table_reference_is_not_a_table_named_final():
    """FINAL is a keyword here. Minting a table identity for it is worse than
    admitting the name is unknown: two programs would MERGE onto the same anchor.
    Since batch-21 item 53 the name is not unknown either: it is the inner INSERT's."""
    prog = parse_program(_wrap(
        "       0000-MAIN.\n"
        "           EXEC SQL DECLARE CSR1 CURSOR FOR\n"
        "             SELECT A FROM FINAL TABLE (INSERT INTO T (A) VALUES (1))\n"
        "           END-EXEC.\n"
        "           STOP RUN.\n"
    ))
    assert prog.sql_cursors[0]["table"] == "T"


def test_a_table_function_is_not_a_table_named_table():
    prog = parse_program(_wrap(
        "       0000-MAIN.\n"
        "           EXEC SQL DECLARE CSR1 CURSOR FOR\n"
        "             SELECT A FROM TABLE (F(:H2)) AS X\n"
        "           END-EXEC.\n"
        "           STOP RUN.\n"
    ))
    assert prog.sql_cursors[0]["table"] is None


def test_a_real_table_whose_name_starts_with_a_keyword_is_unaffected():
    """Only the EXACT word is refused. OLDER_ACCOUNTS is an ordinary table, and a
    prefix match here would delete real edges."""
    prog = parse_program(_wrap(
        "       0000-MAIN.\n"
        "           EXEC SQL DECLARE CSR1 CURSOR FOR\n"
        "             SELECT A FROM OWNER.OLDER_ACCOUNTS\n"
        "           END-EXEC.\n"
        "           STOP RUN.\n"
    ))
    assert prog.sql_cursors[0]["table"] == "OWNER.OLDER_ACCOUNTS"


def test_a_case_expression_on_the_right_hand_side_is_not_dropped_silently():
    """SET C = CASE WHEN :H ... END names :H, and :H used to reach nothing at all -
    no pair, no note, no token. Silence is the one outcome that must not survive."""
    prog = parse_program(_wrap(
        "       0000-MAIN.\n"
        "           EXEC SQL UPDATE T SET C = CASE WHEN :H > X THEN 1 ELSE 2 END\n"
        "             WHERE K = :F END-EXEC.\n"
        "           STOP RUN.\n"
    ))
    st = prog.paragraphs[0].statements[0]
    assert st.columns == [{"column": None, "hostVar": "H", "derived": True,
                           "expression": "CASE", "derivedFrom": ["C"]}]
    assert st.where_vars == [":F"]


def test_an_arithmetic_set_yields_a_derived_entry():
    """`SET Q = (Q + :H)` reads Q and writes Q, so :H is neither a plain pair nor a row
    selector. It used to contribute nothing at all; now it is derived, naming the column
    it feeds. The label is coarse on purpose - `_derivation_of`'s granularity - because
    which operand supplied the value is not a fact the tokens prove."""
    prog = parse_program(_wrap(
        "       0000-MAIN.\n"
        "           EXEC SQL UPDATE T SET Q = (Q + :H) WHERE K = :F END-EXEC.\n"
        "           STOP RUN.\n"
    ))
    st = prog.paragraphs[0].statements[0]
    assert st.columns == [{"column": None, "hostVar": "H", "derived": True,
                           "expression": "expression", "derivedFrom": ["Q"]}]
    assert st.where_vars == [":F"]
    assert st.column_note is None and st.column_unresolved is None


def test_a_row_value_set_reports_a_reason():
    """No single target column for a `derived` entry to sit at, so the reason goes in
    the note and its token instead."""
    prog = parse_program(_wrap(
        "       0000-MAIN.\n"
        "           EXEC SQL UPDATE T SET (C1, C2) = (SELECT A, B FROM S)\n"
        "             WHERE K = :F END-EXEC.\n"
        "           STOP RUN.\n"
    ))
    st = prog.paragraphs[0].statements[0]
    assert st.column_unresolved == "set-expression-unmapped"
    assert st.column_note


def test_a_plain_set_is_unchanged():
    """Regression guard for the shape almost every UPDATE takes."""
    prog = parse_program(_wrap(
        "       0000-MAIN.\n"
        "           EXEC SQL UPDATE T SET C = :H, C2 = :H2 WHERE K = :F END-EXEC.\n"
        "           STOP RUN.\n"
    ))
    st = prog.paragraphs[0].statements[0]
    assert st.columns == [{"column": "C", "hostVar": "H"},
                          {"column": "C2", "hostVar": "H2"}]
    assert st.column_note is None and st.column_unresolved is None


def test_a_set_containing_a_subselect_with_its_own_where_keeps_later_assignments():
    """The SET list ends at the WHERE at ITS OWN depth. A flat scan ends inside the
    subquery and loses `D = :H2` entirely."""
    prog = parse_program(_wrap(
        "       0000-MAIN.\n"
        "           EXEC SQL UPDATE T SET C = (SELECT X FROM Y WHERE Z = :H1),\n"
        "             D = :H2 WHERE K = :F END-EXEC.\n"
        "           STOP RUN.\n"
    ))
    st = prog.paragraphs[0].statements[0]
    assert {"column": "D", "hostVar": "H2"} in st.columns


def test_a_set_from_current_timestamp_still_says_nothing():
    """It names no host variable, so there is no field whose fate needs explaining -
    and inventing a note here would be noise, not recovery."""
    prog = parse_program(_wrap(
        "       0000-MAIN.\n"
        "           EXEC SQL UPDATE T SET C = CURRENT TIMESTAMP WHERE K = :F END-EXEC.\n"
        "           STOP RUN.\n"
    ))
    st = prog.paragraphs[0].statements[0]
    assert st.columns == [] and st.column_note is None
    assert st.column_unresolved is None


# --------------------------------------------------------------------------- #
# The FD/SD entry: its own clauses, and the records it says are its
# --------------------------------------------------------------------------- #

_FD_SRC = (
    "       IDENTIFICATION DIVISION.\n"
    "       PROGRAM-ID. FDREC.\n"
    "       ENVIRONMENT DIVISION.\n"
    "       INPUT-OUTPUT SECTION.\n"
    "       FILE-CONTROL.\n"
    "           SELECT A-FILE ASSIGN TO DDA.\n"
    "           SELECT B-FILE ASSIGN TO DDB.\n"
    "           SELECT C-FILE ASSIGN TO DDC.\n"
    "       DATA DIVISION.\n"
    "       FILE SECTION.\n"
    "       FD  A-FILE.\n"
    "       01  A-REC.\n"
    "           05 A-KEY  PIC X(8).\n"
    "           05 A-REST PIC X(72).\n"
    "       FD  B-FILE\n"
    "           RECORDING MODE IS F\n"
    "           VALUE OF FILE-ID IS 'B.DAT'\n"
    "           DATA RECORDS ARE B-REC-1, B-REC-2.\n"
    "       01  B-REC-1 PIC X(80).\n"
    "       01  B-REC-2 PIC X(80).\n"
    "       FD  C-FILE DATA RECORD C-REC LABEL RECORDS ARE STANDARD.\n"
    "       01  C-REC PIC X(80).\n"
    "       WORKING-STORAGE SECTION.\n"
    "       01  WS-X PIC X.\n"
    "       PROCEDURE DIVISION.\n"
    "           GOBACK.\n"
)


def test_the_data_record_clause_names_the_fds_records():
    """`DATA RECORD IS` / `DATA RECORDS ARE` (IS/ARE optional, commas allowed), on the
    FD line or a later one; the list ends where the FD's next clause begins."""
    assert parse_program(_FD_SRC).fd_data_records == {
        "B-FILE": ["B-REC-1", "B-REC-2"], "C-FILE": ["C-REC"]}


def test_an_fds_clause_lines_do_not_join_the_item_above_it():
    """The FD's continuation lines used to be appended to the last entry of the
    previous record, so `VALUE OF FILE-ID IS 'B.DAT'` gave A-REST the VALUE `OF`."""
    items = {i.name: i for i in parse_program(_FD_SRC).data_items}
    assert items["A-REST"].value is None and items["A-REST"].pic == "X(72)"
    assert [i.file for i in items.values() if i.name != "WS-X"] == \
        ["A-FILE"] * 3 + ["B-FILE"] * 2 + ["C-FILE"]


# --- batch-21 item 52: an EXEC SQL INCLUDE split across lines ---------------------

def _members(body: str):
    from cobol_parser.preprocessor import scan_copy_members
    return scan_copy_members(
        "       IDENTIFICATION DIVISION.\n       PROGRAM-ID. T.\n"
        "       DATA DIVISION.\n       WORKING-STORAGE SECTION.\n" + body)


def _expanded(body: str):
    from cobol_parser.normalizer import normalize
    from cobol_parser.preprocessor import preprocess
    res = preprocess(normalize(
        "       IDENTIFICATION DIVISION.\n       PROGRAM-ID. T.\n"
        "       DATA DIVISION.\n       WORKING-STORAGE SECTION.\n" + body))
    return res


def test_an_sql_include_is_found_on_one_line_and_split_across_three():
    """All three tokens used to be required on ONE physical line, so the estate's
    usual layout - EXEC SQL / INCLUDE m / END-EXEC. - was never fetched or expanded."""
    assert _members("           EXEC SQL INCLUDE ONE END-EXEC.\n") == ["ONE"]
    split = ("           EXEC SQL\n"
             "               INCLUDE DCLACCT\n"
             "           END-EXEC.\n")
    assert _members(split) == ["DCLACCT"]
    rows = _expanded(split).copybooks
    assert [(r["member"], r["via"]) for r in rows] == [("DCLACCT", "EXEC SQL INCLUDE")]


def test_a_split_sql_include_with_a_change_tag_in_columns_1_to_6_is_found():
    """A fixed-format test without the tag would pass while real source failed."""
    assert _members("CH0042     EXEC SQL\n"
                    "CH0042         INCLUDE DCLACCT\n"
                    "CH0042     END-EXEC.\n") == ["DCLACCT"]


def test_an_ordinary_sql_statement_names_no_member_and_is_not_consumed():
    body = ("           EXEC SQL\n"
            "               SELECT A INTO :B FROM T\n"
            "           END-EXEC.\n")
    assert _members(body) == []
    res = _expanded(body)
    assert res.copybooks == []
    assert [cl.text.strip() for cl in res.lines][-3:] == [
        "EXEC SQL", "SELECT A INTO :B FROM T", "END-EXEC."]


def test_a_later_include_does_not_fold_an_open_sql_statement_into_one_line():
    """An embedded statement with no period gathers up to the next one. The INCLUDE
    found at the end of that run belongs to ITS own line, so the lines before it must
    come through untouched rather than being joined as a prefix."""
    body = ("           EXEC SQL\n"
            "               DECLARE C1 CURSOR FOR SELECT A FROM T\n"
            "           END-EXEC\n"
            "           EXEC SQL INCLUDE ONE END-EXEC.\n")
    assert _members(body) == ["ONE"]
    res = _expanded(body)
    assert [r["member"] for r in res.copybooks] == ["ONE"]
    assert [cl.text.strip() for cl in res.lines][-3:] == [
        "EXEC SQL", "DECLARE C1 CURSOR FOR SELECT A FROM T", "END-EXEC"]


# --- batch-21 item 53: a data-change table reference names the inner table ---------

def _cursor_table(select: str):
    prog = parse_program(
        "       IDENTIFICATION DIVISION.\n       PROGRAM-ID. T.\n"
        "       DATA DIVISION.\n       WORKING-STORAGE SECTION.\n"
        "       01  WS-ID PIC 9(5).\n"
        "           EXEC SQL DECLARE C1 CURSOR FOR\n"
        f"               {select}\n"
        "           END-EXEC.\n"
        "       PROCEDURE DIVISION.\n           GOBACK.\n")
    return prog.sql_cursors[0]["table"]


def test_a_data_change_reference_names_its_inner_table_for_every_keyword():
    assert _cursor_table("SELECT ID FROM FINAL TABLE (UPDATE S.T1 SET A = 1)") == "S.T1"
    assert _cursor_table("SELECT ID FROM OLD TABLE (DELETE FROM T2 WHERE ID = 1)") == "T2"
    assert _cursor_table(
        "SELECT ID FROM NEW TABLE (INSERT INTO T3 (ID) VALUES (:WS-ID))") == "T3"


def test_a_table_genuinely_called_final_is_still_its_name():
    """Not a keyword blocklist: without `TABLE (` behind it, FINAL is the table."""
    assert _cursor_table("SELECT ID FROM FINAL WHERE ID = 1") == "FINAL"
    assert _cursor_table("SELECT ID FROM NEW") == "NEW"


def _cursor_row(select: str) -> dict:
    return parse_program(
        "       IDENTIFICATION DIVISION.\n       PROGRAM-ID. T.\n"
        "       DATA DIVISION.\n       WORKING-STORAGE SECTION.\n"
        "       01  WS-ID PIC 9(5).\n"
        "           EXEC SQL DECLARE C1 CURSOR FOR\n"
        f"           {select}\n"
        "           END-EXEC.\n"
        "       PROCEDURE DIVISION.\n           GOBACK.\n").sql_cursors[0]


def test_a_cursor_over_a_data_change_reference_records_the_write_its_open_runs():
    """Db2 runs the inner statement at OPEN, so the cursor record carries which write
    and the host variables that feed it - the table is already `table`."""
    # Kept short of column 72: fixed format drops whatever lies past it.
    row = _cursor_row("SELECT ID FROM FINAL TABLE (INSERT INTO T3 VALUES (:WS-ID))")
    assert row["table"] == "T3"
    assert row["dataChange"] == {"verb": "INSERT", "hostVars": ["WS-ID"]}
    assert _cursor_row("SELECT ID FROM OLD TABLE (DELETE FROM T2 WHERE A=:WS-ID)"
                       )["dataChange"] == {"verb": "DELETE", "hostVars": ["WS-ID"]}


def test_an_ordinary_cursor_carries_no_data_change_key():
    """Only the data-change form gets the key, so no existing parse output moves."""
    assert "dataChange" not in _cursor_row("SELECT ID FROM T WHERE ID = :WS-ID")
    assert "dataChange" not in _cursor_row("SELECT ID FROM FINAL WHERE ID = :WS-ID")


# --------------------------------------------------------------------------- #
# a contained program's body is parsed, not discarded (upstream ledger item 55)
#
# _split_program_units took each contained unit's lines out of the main program - right,
# because folding them in corrupts the outer program's logic - and then nothing parsed
# them. A contained program is part of the same compilation unit, so its CALLs are
# dependencies of the member; they vanished with its body, and `nested_programs` (names
# only) gave a consumer no way to get them back.
# --------------------------------------------------------------------------- #

from cobol_parser.analysis import analyze_calls                          # noqa: E402
from cobol_parser.model import walk_statements                           # noqa: E402
from cobol_parser.normalizer import SourceFormat, normalize              # noqa: E402
from cobol_parser.parser import _split_program_units                     # noqa: E402

_OUTER_WITH_TWO_UNITS = (
    "       IDENTIFICATION DIVISION.\n"
    "       PROGRAM-ID. OUTERPGM.\n"
    "       DATA DIVISION.\n"
    "       WORKING-STORAGE SECTION.\n"
    "       01  WS-OUT       PIC X(8).\n"
    "       PROCEDURE DIVISION.\n"
    "       0000-MAIN.\n"
    "           CALL 'INNERONE'\n"
    "           CALL 'INNERTWO'\n"
    "           GOBACK.\n"
    "       IDENTIFICATION DIVISION.\n"
    "       PROGRAM-ID. INNERONE.\n"
    "       PROCEDURE DIVISION.\n"
    "       1000-ONE.\n"
    "           CALL 'EXTMOD'\n"
    "           GOBACK.\n"
    "       END PROGRAM INNERONE.\n"
    "       IDENTIFICATION DIVISION.\n"
    "       PROGRAM-ID. INNERTWO.\n"
    "       DATA DIVISION.\n"
    "       WORKING-STORAGE SECTION.\n"
    "       05  WS-TARGET    PIC X(8) VALUE 'OTHERMOD'.\n"
    "       PROCEDURE DIVISION.\n"
    "       2000-TWO.\n"
    "           CALL WS-TARGET\n"
    "           GOBACK.\n"
    "       END PROGRAM INNERTWO.\n"
    "       END PROGRAM OUTERPGM.\n"
)


def _call_targets(unit):
    """What one unit calls: literal targets, plus dynamic ones its own analysis names."""
    analysis = analyze_calls(unit)
    out = []
    for para in unit.paragraphs:
        for st in walk_statements(para.statements):
            if isinstance(st, CallStmt):
                res = analysis.resolve(st.target) if st.dynamic else None
                out.append(res.resolved if res else st.target)
    return out


def _member_manifest(prog):
    """The consumer the ledger describes: every unit's calls, minus the internal ones."""
    internal = set(prog.nested_programs)
    return [t for unit in [prog] + prog.contained for t in _call_targets(unit)
            if t not in internal]


def test_a_contained_programs_external_calls_reach_the_members_manifest():
    prog = parse_program(_OUTER_WITH_TWO_UNITS)
    manifest = _member_manifest(prog)
    assert manifest == ["EXTMOD", "OTHERMOD"], (
        "a contained unit's CALLs were dropped with its body")
    # ...and a call to a contained unit is internal, never a dependency.
    assert "INNERONE" not in manifest and "INNERTWO" not in manifest


def test_contained_holds_one_program_per_unit_and_nested_programs_is_unchanged():
    prog = parse_program(_OUTER_WITH_TWO_UNITS)
    assert prog.nested_programs == ["INNERONE", "INNERTWO"]
    assert [u.program_id for u in prog.contained] == ["INNERONE", "INNERTWO"]
    one, two = prog.contained
    assert [p.name for p in one.paragraphs] == ["1000-ONE"]
    assert [p.name for p in two.paragraphs] == ["2000-TWO"]
    # Each unit's data is its own: the dynamic target resolves from INNERTWO's storage,
    # and none of it leaked into the outer program.
    assert two.working_values == {"WS-TARGET": "OTHERMOD"}
    assert "WS-TARGET" not in prog.data_by_name
    assert [p.name for p in prog.paragraphs] == ["0000-MAIN"]
    # The copybooks and their notes belong to the member, which is the main program.
    assert one.copybooks == [] and one.notes == []


def test_a_contained_unit_keeps_its_own_identification_division_line():
    prog = parse_program(_OUTER_WITH_TWO_UNITS)
    one = prog.contained[0]
    assert one.program_id_line == 12
    _, units = _split_program_units(normalize(_OUTER_WITH_TWO_UNITS, SourceFormat.FIXED))
    first_line = units[0][1][0]
    assert "IDENTIFICATION DIVISION" in first_line.text and first_line.line == 11


def test_a_single_program_source_is_left_alone():
    src = (
        "       IDENTIFICATION DIVISION.\n"
        "       PROGRAM-ID. ONLYPGM.\n"
        "       PROCEDURE DIVISION.\n"
        "       0000-MAIN.\n"
        "           CALL 'ONLYEXT'\n"
        "           GOBACK.\n"
        "       END PROGRAM ONLYPGM.\n"
    )
    lines = normalize(src, SourceFormat.FIXED)
    main, units = _split_program_units(lines)
    assert main is lines and units == []
    prog = parse_program(src)
    assert prog.nested_programs == [] and prog.contained == []


def test_malformed_nesting_still_falls_back_to_one_program():
    """Two PROGRAM-IDs and no END PROGRAM: concatenated units, not nesting. Splitting
    on a guess would drop a whole body, so nothing is split - and the second unit's CALL
    stays visible in the one program rather than vanishing into an unparsed unit."""
    src = (
        "       IDENTIFICATION DIVISION.\n"
        "       PROGRAM-ID. FIRSTPGM.\n"
        "       PROCEDURE DIVISION.\n"
        "       0000-MAIN.\n"
        "           GOBACK.\n"
        "       IDENTIFICATION DIVISION.\n"
        "       PROGRAM-ID. SECNDPGM.\n"
        "       PROCEDURE DIVISION.\n"
        "       1000-SECOND.\n"
        "           CALL 'SECNDEXT'\n"
        "           GOBACK.\n"
    )
    prog = parse_program(src)
    assert prog.nested_programs == [] and prog.contained == []
    assert "SECNDEXT" in _call_targets(prog)


_THREE_DEEP = (
    "       IDENTIFICATION DIVISION.\n"
    "       PROGRAM-ID. TOPPGM.\n"
    "       PROCEDURE DIVISION.\n"
    "       0000-TOP.\n"
    "           CALL 'MIDPGM'\n"
    "           GOBACK.\n"
    "       IDENTIFICATION DIVISION.\n"
    "       PROGRAM-ID. MIDPGM.\n"
    "       PROCEDURE DIVISION.\n"
    "       1000-MID.\n"
    "           CALL 'DEEPPGM'\n"
    "           CALL 'MIDEXT'\n"
    "           GOBACK.\n"
    "       IDENTIFICATION DIVISION.\n"
    "       PROGRAM-ID. DEEPPGM.\n"
    "       PROCEDURE DIVISION.\n"
    "       2000-DEEP.\n"
    "           CALL 'DEEPEXT'\n"
    "           GOBACK.\n"
    "       END PROGRAM DEEPPGM.\n"
    "       END PROGRAM MIDPGM.\n"
    "       END PROGRAM TOPPGM.\n"
)


def test_a_unit_inside_a_contained_unit_is_parsed_too_at_any_depth():
    """The depth supported is any: `contained` is FLAT - one Program per unit at every
    depth, each holding only its own lines - so one pass over it sees every unit exactly
    once, and a consumer that does not recurse still loses nothing."""
    prog = parse_program(_THREE_DEEP)
    assert prog.nested_programs == ["MIDPGM", "DEEPPGM"]          # as before this change
    assert [u.program_id for u in prog.contained] == ["MIDPGM", "DEEPPGM"]
    mid, deep = prog.contained
    assert mid.nested_programs == ["DEEPPGM"] and deep.nested_programs == []
    assert mid.contained == [] and deep.contained == []
    assert [p.name for p in mid.paragraphs] == ["1000-MID"]       # DEEP's body not folded in
    assert _call_targets(mid) == ["DEEPPGM", "MIDEXT"]
    assert _call_targets(deep) == ["DEEPEXT"]
    assert _member_manifest(prog) == ["MIDEXT", "DEEPEXT"]
