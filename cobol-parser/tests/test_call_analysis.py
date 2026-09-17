"""Constant propagation for dynamic CALL targets, tested on the parse alone.

These exercise ``cobol_parser.analysis`` directly rather than through a consumer: the
whole point of the module living here is that a program which parses COBOL and models
nothing resolves the same target the statechart does. The statechart-level tests (which
assert what the resolution becomes in a machine) stay with the modelling engine.
"""

from cobol_parser.analysis import analyze_calls
from cobol_parser.parser import parse_program
from cobol_parser.textutil import split_outside_literals


def _resolve(ws: str, proc: str, name: str):
    src = (
        "       IDENTIFICATION DIVISION.\n"
        "       PROGRAM-ID. CALLRES.\n"
        "       DATA DIVISION.\n"
        "       WORKING-STORAGE SECTION.\n" + ws +
        "       PROCEDURE DIVISION.\n"
        "       0000-MAIN.\n" + proc
    )
    return analyze_calls(parse_program(src)).resolve(name)


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
