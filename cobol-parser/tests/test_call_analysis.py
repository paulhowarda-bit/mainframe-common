"""Constant propagation for dynamic CALL targets, tested on the parse alone.

These exercise ``cobol_parser.analysis`` directly rather than through a consumer: the
whole point of the module living here is that a program which parses COBOL and models
nothing resolves the same target the statechart does. The statechart-level tests (which
assert what the resolution becomes in a machine) stay with the modelling engine.
"""

from cobol_parser.analysis import analyze_calls
from cobol_parser.parser import parse_program
from cobol_parser.textutil import split_outside_literals


def _resolve(ws: str, proc: str, name: str, operand_limit=None):
    src = (
        "       IDENTIFICATION DIVISION.\n"
        "       PROGRAM-ID. CALLRES.\n"
        "       DATA DIVISION.\n"
        "       WORKING-STORAGE SECTION.\n" + ws +
        "       PROCEDURE DIVISION.\n"
        "       0000-MAIN.\n" + proc
    )
    return analyze_calls(parse_program(src)).resolve(name, operand_limit=operand_limit)


def test_a_value_clause_is_the_literal_that_reaches():
    res = _resolve("       01 WS-PGM PIC X(8) VALUE 'PGMVALUE'.\n",
                   "           CALL WS-PGM\n"
                   "           GOBACK.\n", "WS-PGM")
    assert (res.confident, res.resolved, res.evidence) == (True, "PGMVALUE", "assigned")


def test_a_moved_literal_is_the_literal_that_reaches():
    res = _resolve("       01 WS-PGM PIC X(8).\n",
                   "           MOVE 'PGMMOVED' TO WS-PGM\n"
                   "           CALL WS-PGM\n"
                   "           GOBACK.\n", "WS-PGM")
    assert (res.confident, res.resolved) == (True, "PGMMOVED")


def test_two_literals_are_candidates_and_not_an_answer():
    res = _resolve("       01 WS-PGM PIC X(8).\n"
                   "       01 WS-FLAG PIC X.\n",
                   "           IF WS-FLAG = 'A'\n"
                   "               MOVE 'PGMONEAA' TO WS-PGM\n"
                   "           ELSE\n"
                   "               MOVE 'PGMTWOBB' TO WS-PGM\n"
                   "           END-IF\n"
                   "           CALL WS-PGM\n"
                   "           GOBACK.\n", "WS-PGM")
    assert not res.confident
    assert res.candidates == ["PGMONEAA", "PGMTWOBB"]
    assert not res.has_variable_assignment


def test_a_literal_and_a_variable_together_stay_runtime_determined():
    res = _resolve("       01 WS-PGM PIC X(8) VALUE 'PGMVALUE'.\n"
                   "       01 WS-OTHER PIC X(8).\n",
                   "           MOVE WS-OTHER TO WS-PGM\n"
                   "           CALL WS-PGM\n"
                   "           GOBACK.\n", "WS-PGM")
    assert not res.confident and res.has_variable_assignment
    assert res.candidates == ["PGMVALUE"]


def test_set_to_true_stores_the_condition_value():
    """The 88-level idiom: the target itself has no VALUE, and ``SET <88> TO TRUE``
    is what puts the module name in it."""
    res = _resolve("       01 WS-PGM PIC X(8).\n"
                   "          88 PGM-IS-A VALUE 'PGMSETAA'.\n",
                   "           SET PGM-IS-A TO TRUE\n"
                   "           CALL WS-PGM\n"
                   "           GOBACK.\n", "WS-PGM")
    assert (res.confident, res.resolved, res.evidence) == (True, "PGMSETAA", "assigned")


def test_88_values_with_no_set_are_declared_candidates_not_proof():
    """What the program was WRITTEN to allow is not what it demonstrably does, and the
    two must not be reported with the same confidence."""
    res = _resolve("       01 WS-PGM PIC X(8).\n"
                   "          88 PGM-IS-A VALUE 'PGMSETAA'.\n",
                   "           CALL WS-PGM\n"
                   "           GOBACK.\n", "WS-PGM")
    assert not res.confident
    assert (res.candidates, res.evidence) == (["PGMSETAA"], "declared-88")


def test_an_undeclared_target_names_the_copybook_that_did_not_arrive():
    src = (
        "       IDENTIFICATION DIVISION.\n"
        "       PROGRAM-ID. CALLRES.\n"
        "       DATA DIVISION.\n"
        "       WORKING-STORAGE SECTION.\n"
        "       COPY NOSUCHCB.\n"
        "       PROCEDURE DIVISION.\n"
        "       0000-MAIN.\n"
        "           CALL CN-MODULE\n"
        "           GOBACK.\n"
    )
    res = analyze_calls(parse_program(src)).resolve("CN-MODULE")
    assert not res.confident
    assert "not declared in the visible source" in res.reason
    assert "NOSUCHCB" in res.reason


def test_a_declared_target_that_nothing_assigns_says_so():
    res = _resolve("       01 WS-PGM PIC X(8).\n",
                   "           CALL WS-PGM\n"
                   "           GOBACK.\n", "WS-PGM")
    assert not res.confident
    assert "declared but never assigned a literal" in res.reason


def test_a_value_carried_onto_a_later_line_is_the_same_clause():
    """A data description entry runs to its terminating period; scanned line by line the
    VALUE was invisible and the CALL through it read as runtime-determined."""
    res = _resolve("       01 WS-PGM PIC X(8)\n"
                   "                  VALUE 'PGMSPLIT'.\n",
                   "           CALL WS-PGM\n"
                   "           GOBACK.\n", "WS-PGM")
    assert (res.confident, res.resolved) == (True, "PGMSPLIT")


def test_a_name_declared_twice_keeps_both_literals():
    """Collapsed to one declaration, ``CALL WS-PGM OF GRP-A`` resolved confidently to
    GRP-B's literal - the wrong program, with no flag."""
    res = _resolve("       01 GRP-A.\n"
                   "          05 WS-PGM PIC X(8) VALUE 'PGMINAAA'.\n"
                   "       01 GRP-B.\n"
                   "          05 WS-PGM PIC X(8) VALUE 'PGMINBBB'.\n",
                   "           CALL WS-PGM OF GRP-A\n"
                   "           GOBACK.\n", "WS-PGM")
    assert not res.confident
    assert res.candidates == ["PGMINAAA", "PGMINBBB"]


def test_keywords_inside_a_moved_literal_are_data_not_syntax():
    """The regression this whole analysis exists not to re-introduce.

    ``MOVE 'CALL TO FRCEMAIL FAILED' TO WS-ERR-MSG`` torn at the ` TO ` INSIDE the
    message manufactures the phantom assignment ``FRCEMAIL := 'CALL'``, and a dynamic
    ``CALL FRCEMAIL`` then resolves - confidently - to a program named CALL.
    """
    res = _resolve("       01 FRCEMAIL PIC X(8).\n"
                   "       01 WS-ERR-MSG PIC X(40).\n",
                   "           MOVE 'PGMEMAIL' TO FRCEMAIL\n"
                   "           MOVE 'CALL TO FRCEMAIL FAILED' TO WS-ERR-MSG\n"
                   "           CALL FRCEMAIL\n"
                   "           GOBACK.\n", "FRCEMAIL")
    assert (res.confident, res.resolved) == (True, "PGMEMAIL")
    assert "CALL" not in res.candidates


def test_the_split_that_tears_a_move_ignores_keywords_inside_literals():
    assert split_outside_literals("'CALL TO FRCEMAIL FAILED' TO WS-ERR-MSG", "TO") == (
        "'CALL TO FRCEMAIL FAILED'", "WS-ERR-MSG")
    assert split_outside_literals("WS-A FROM WS-B", "TO") is None


# --------------------------------------------------------------------------- #
# chains: MOVE WS-A TO WS-B, CALL WS-B
# --------------------------------------------------------------------------- #

_CHAIN_WS = ("       01 WS-A PIC X(8) VALUE 'PGMCHAIN'.\n"
             "       01 WS-B PIC X(8).\n"
             "       01 WS-C PIC X(8).\n"
             "       01 WS-D PIC X(8).\n")


def test_a_move_from_an_item_carrying_a_literal_resolves_the_call():
    res = _resolve(_CHAIN_WS,
                   "           MOVE WS-A TO WS-B\n"
                   "           CALL WS-B\n"
                   "           GOBACK.\n", "WS-B")
    assert (res.confident, res.resolved) == (True, "PGMCHAIN")
    assert "through WS-A" in res.reason


def test_the_chain_is_followed_as_far_as_it_goes():
    res = _resolve(_CHAIN_WS,
                   "           MOVE WS-A TO WS-B\n"
                   "           MOVE WS-B TO WS-C\n"
                   "           CALL WS-C\n"
                   "           GOBACK.\n", "WS-C")
    assert (res.confident, res.resolved) == (True, "PGMCHAIN")
    assert "through WS-B, WS-A" in res.reason


def test_a_non_literal_reaching_the_chain_keeps_it_flagged():
    """WS-D is declared and nothing in the program puts a value in it, so what it
    carries at run time is not something this analysis can name. The literal is still
    a candidate; it is not an answer."""
    res = _resolve(_CHAIN_WS,
                   "           MOVE WS-A TO WS-B\n"
                   "           MOVE WS-D TO WS-B\n"
                   "           CALL WS-B\n"
                   "           GOBACK.\n", "WS-B")
    assert not res.confident and res.has_variable_assignment
    assert res.candidates == ["PGMCHAIN"]


def test_a_cycle_terminates():
    res = _resolve(_CHAIN_WS,
                   "           MOVE WS-B TO WS-C\n"
                   "           MOVE WS-C TO WS-B\n"
                   "           CALL WS-B\n"
                   "           GOBACK.\n", "WS-B")
    assert not res.confident and res.candidates == []
    assert "set only from variables" in res.reason


def test_a_cycle_with_a_literal_in_it_still_resolves():
    res = _resolve(_CHAIN_WS,
                   "           MOVE WS-A TO WS-B\n"
                   "           MOVE WS-B TO WS-C\n"
                   "           MOVE WS-C TO WS-B\n"
                   "           CALL WS-C\n"
                   "           GOBACK.\n", "WS-C")
    assert (res.confident, res.resolved) == (True, "PGMCHAIN")


def test_a_figurative_constant_is_a_value_this_analysis_cannot_name():
    res = _resolve(_CHAIN_WS,
                   "           MOVE SPACES TO WS-B\n"
                   "           CALL WS-B\n"
                   "           GOBACK.\n", "WS-B")
    assert not res.confident and res.has_variable_assignment
    assert "set only from variables" in res.reason


def test_a_subscripted_source_names_no_single_item():
    """Which occurrence reaches the CALL is decided at run time, so the chain stops
    here rather than resolving to whatever the table happens to declare."""
    res = _resolve("       01 WS-TAB.\n"
                   "          05 WS-E PIC X(8) OCCURS 3 VALUE 'PGMTABLE'.\n"
                   "       01 WS-B PIC X(8).\n"
                   "       01 IDX PIC 9(2).\n",
                   "           MOVE WS-E (IDX) TO WS-B\n"
                   "           CALL WS-B\n"
                   "           GOBACK.\n", "WS-B")
    assert not res.confident and res.has_variable_assignment
    assert "PGMTABLE" not in res.candidates


def test_a_qualified_source_names_its_item():
    res = _resolve("       01 GRP-A.\n"
                   "          05 WS-Q PIC X(8) VALUE 'PGMQUAL'.\n"
                   "       01 WS-B PIC X(8).\n",
                   "           MOVE WS-Q OF GRP-A TO WS-B\n"
                   "           CALL WS-B\n"
                   "           GOBACK.\n", "WS-B")
    assert (res.confident, res.resolved) == (True, "PGMQUAL")


# --------------------------------------------------------------------------- #
# A literal that cannot be the name the call invokes is never offered as one
# --------------------------------------------------------------------------- #

def test_a_literal_longer_than_the_item_is_not_its_value():
    """A 25-character message reaching an 8-byte item through a MOVE chain cannot be
    what the item holds, so it is no candidate - but it is kept, with its reason."""
    res = _resolve("       01 WS-PGM PIC X(08).\n"
                   "       01 WS-MSG PIC X(25) VALUE 'RUN COMPLETED NORMALLY OK'.\n",
                   "           MOVE WS-MSG TO WS-PGM\n"
                   "           MOVE 'PGMREALA' TO WS-PGM\n"
                   "           CALL WS-PGM\n"
                   "           GOBACK.\n", "WS-PGM")
    assert res.candidates == ["PGMREALA"]
    assert res.rejected == [{"literal": "RUN COMPLETED NORMALLY OK", "reason": "length"}]


def test_a_rejected_literal_never_promotes_the_one_that_remains():
    """One admissible name left is not one name reaching: the rejected literal also
    reaches the call, so it stays flagged rather than resolving by elimination."""
    res = _resolve("       01 WS-PGM PIC X(08) VALUE 'ZZZZZZZZ'.\n",
                   "           MOVE 'PGMREALA' TO WS-PGM\n"
                   "           CALL WS-PGM\n"
                   "           GOBACK.\n", "WS-PGM")
    assert not res.confident and res.resolved is None
    assert res.candidates == ["PGMREALA"] and res.evidence == "assigned"
    assert "not a name: 'ZZZZZZZZ' (filler)" in res.reason


def test_a_literal_holding_a_blank_is_not_a_name():
    res = _resolve("       01 WS-PGM PIC X(08).\n",
                   "           MOVE 'SEE LOG' TO WS-PGM\n"
                   "           CALL WS-PGM\n"
                   "           GOBACK.\n", "WS-PGM")
    assert (res.confident, res.candidates) == (False, [])
    assert res.rejected == [{"literal": "SEE LOG", "reason": "space"}]
    assert res.reason.startswith("no literal that can be a name reaches WS-PGM")


def test_cics_passes_eight_characters_of_a_longer_item():
    """A PIC X(9) holding `ABC40001C` LINKs to ABC40001: CICS reads 8 characters of a
    PROGRAM() operand whatever the item's length. A batch CALL has no such bound."""
    ws = "       01 WS-LNK-TARGET PIC X(09) VALUE 'ABC40001C'.\n"
    proc = ("           EXEC CICS LINK PROGRAM(WS-LNK-TARGET) END-EXEC\n"
            "           GOBACK.\n")
    res = _resolve(ws, proc, "WS-LNK-TARGET", operand_limit=8)
    assert (res.confident, res.resolved, res.candidates) == (True, "ABC40001", ["ABC40001"])
    assert "'ABC40001C' -> 'ABC40001'" in res.reason and not res.rejected
    assert _resolve(ws, proc, "WS-LNK-TARGET").resolved == "ABC40001C"


def test_two_literals_that_pass_the_same_eight_characters_are_one_program():
    res = _resolve("       01 WS-LNK-TARGET PIC X(09).\n"
                   "       01 WS-FLAG PIC X.\n",
                   "           IF WS-FLAG = 'A'\n"
                   "               MOVE 'ABC40001C' TO WS-LNK-TARGET\n"
                   "           ELSE\n"
                   "               MOVE 'ABC40001D' TO WS-LNK-TARGET\n"
                   "           END-IF\n"
                   "           EXEC CICS LINK PROGRAM(WS-LNK-TARGET) END-EXEC\n"
                   "           GOBACK.\n", "WS-LNK-TARGET", operand_limit=8)
    assert (res.confident, res.resolved) == (True, "ABC40001")
    assert res.reason.startswith("only name reaching WS-LNK-TARGET is 'ABC40001'")


def test_a_wildcard_template_gives_way_to_the_88_values():
    """The VALUE is a template (`ABCDE***`), the 88-level names the real programs.
    With the template gone the declared values are the answer - at their weaker grade."""
    res = _resolve("       01 WS-LNK-TARGET PIC X(08) VALUE 'ABCDE***'.\n"
                   "           88 WS-LNK-TARGET-GOOD VALUE 'ABCDE200' 'ABCDE300'.\n",
                   "           CALL WS-LNK-TARGET\n"
                   "           GOBACK.\n", "WS-LNK-TARGET")
    assert not res.confident
    assert res.candidates == ["ABCDE200", "ABCDE300"] and res.evidence == "declared-88"
    assert res.rejected == [{"literal": "ABCDE***", "reason": "wildcard"}]


def test_a_placeholder_is_rejected_on_its_question_marks():
    res = _resolve("       01 WS-PGM PIC X(08).\n",
                   "           MOVE 'ABCD??X' TO WS-PGM\n"
                   "           CALL WS-PGM\n"
                   "           GOBACK.\n", "WS-PGM")
    assert (res.confident, res.candidates) == (False, [])
    assert res.rejected == [{"literal": "ABCD??X", "reason": "wildcard"}]


def test_an_undeclared_receiver_is_never_rejected_for_length():
    """With no declaration in sight (its copybook did not arrive) nothing fixes the
    item's length, so only the name tests apply: the filter must not start discarding
    candidates in exactly the situation where the parse cannot see the PIC."""
    res = _resolve("",
                   "           MOVE 'ABCDEFGHIJKL' TO WS-GONE\n"
                   "           MOVE 'PGMREALA' TO WS-GONE\n"
                   "           CALL WS-GONE\n"
                   "           GOBACK.\n", "WS-GONE")
    assert res.candidates == ["ABCDEFGHIJKL", "PGMREALA"] and not res.rejected


def test_a_filler_goes_and_both_real_names_stay():
    """The regression that matters is not that the filler goes: it is that the filter
    must not narrow the list to one, which would silently drop a real dispatch target."""
    res = _resolve("       01 WS-DSP-PGM PIC X(8) VALUE 'ZZZZZZZZ'.\n"
                   "       01 WS-DSP-PGM-ONE PIC X(8) VALUE 'PGMDSP0A'.\n"
                   "       01 WS-DSP-PGM-TWO PIC X(8) VALUE 'PGMDSP0B'.\n"
                   "       01 WS-FLAG PIC X.\n",
                   "           IF WS-FLAG = 'A'\n"
                   "               MOVE WS-DSP-PGM-ONE TO WS-DSP-PGM\n"
                   "           ELSE\n"
                   "               MOVE WS-DSP-PGM-TWO TO WS-DSP-PGM\n"
                   "           END-IF\n"
                   "           CALL WS-DSP-PGM\n"
                   "           GOBACK.\n", "WS-DSP-PGM")
    assert res.candidates == ["PGMDSP0A", "PGMDSP0B"]
    assert res.evidence == "assigned" and not res.has_variable_assignment
    assert res.rejected == [{"literal": "ZZZZZZZZ", "reason": "filler"}]


def test_a_short_repeated_run_is_still_a_name():
    """`ZZ` and `XXX` are plausible prefixes of a real member name; only four or more of
    one character is a fill pattern."""
    res = _resolve("       01 WS-PGM PIC X(08) VALUE 'XXX'.\n",
                   "           CALL WS-PGM\n"
                   "           GOBACK.\n", "WS-PGM")
    assert (res.confident, res.resolved) == (True, "XXX")


def test_a_group_receiver_is_never_rejected_for_length():
    """A group's length is its children's; nothing here sizes it, so no literal is
    discarded for length - the same rule as an undeclared item."""
    res = _resolve("       01 WS-PGM.\n"
                   "          05 WS-PGM-PFX PIC X(4).\n"
                   "          05 WS-PGM-SFX PIC X(4).\n",
                   "           MOVE 'ABCDEFGHIJ' TO WS-PGM\n"
                   "           CALL WS-PGM\n"
                   "           GOBACK.\n", "WS-PGM")
    assert (res.confident, res.resolved) == (True, "ABCDEFGHIJ")
