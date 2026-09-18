"""The CICS lexer, on the cases real decks actually contain.

Moved here from cics-dependencies with the lexer itself (``tests/test_lexer.py`` and the
lexer half of ``tests/test_tables.py``), unchanged but for the import: the property they
pin is the reader's, whoever calls it.
"""

from cics_parser.lexer import lex_csd, lex_macro


def _ops(statement):
    return [(op.keyword, op.value) for op in statement.operands]


def test_a_statement_continues_until_the_next_command():
    stmts, flags = lex_csd(
        " DEFINE FILE(ACCTDAT) GROUP(CARDDEMO)\n"
        "        DSNAME(AWS.M2.ACCTDATA) STATUS(ENABLED)\n"
        " DEFINE FILE(CUSTDAT) GROUP(CARDDEMO)\n")
    assert [s.verb for s in stmts] == ["DEFINE", "DEFINE"]
    assert stmts[0].first("DSNAME") == "AWS.M2.ACCTDATA"
    assert stmts[1].first("FILE") == "CUSTDAT"
    assert flags == []


def test_indentation_is_not_the_continuation_signal():
    """DESCRIPTION sits in column 2, the same column DEFINE sits in. A lexer that
    continued on indentation would make it a statement of its own and lose it."""
    stmts, _ = lex_csd(
        " DEFINE MAPSET(COACTUP) GROUP(CARDDEMO)\n"
        " DESCRIPTION(CREDIT CARD ACCOUNT UPDATE MAP)\n"
        "        RESIDENT(NO)\n")
    assert len(stmts) == 1
    assert stmts[0].first("DESCRIPTION") == "CREDIT CARD ACCOUNT UPDATE MAP"


def test_a_value_may_contain_blanks_and_commas():
    """Both are real: DEFINETIME(22/06/10 20:03:53) and WAITTIME(0,0,0). Splitting
    operands on whitespace truncates the first; splitting on commas shatters the second."""
    stmts, _ = lex_csd(" DEFINE TRANSACTION(CAUP) WAITTIME(0,0,0)\n"
                       "        DEFINETIME(22/06/10 20:03:53)\n")
    assert stmts[0].first("WAITTIME") == "0,0,0"
    assert stmts[0].first("DEFINETIME") == "22/06/10 20:03:53"


def test_a_command_verb_followed_by_a_paren_is_an_operand():
    """ADD, DELETE and LIST are DFHCSDUP commands AND ordinary FILE attributes. Without
    this rule a continuation line beginning DELETE(YES) silently becomes a DELETE command
    and every attribute after it lands on the wrong statement."""
    stmts, _ = lex_csd(" DEFINE FILE(ACCTDAT) GROUP(CARDDEMO)\n"
                       "        ADD(YES) DELETE(YES) READ(YES)\n"
                       "        BROWSE(YES) UPDATE(YES)\n")
    assert len(stmts) == 1
    assert stmts[0].first("ADD") == "YES"
    assert stmts[0].first("DELETE") == "YES"


def test_a_command_verb_not_followed_by_a_paren_starts_a_statement():
    stmts, _ = lex_csd(" DEFINE FILE(X) GROUP(G)\n"
                       " ADD GROUP(G) LIST(L)\n")
    assert [s.verb for s in stmts] == ["DEFINE", "ADD"]
    assert _ops(stmts[1]) == [("GROUP", "G"), ("LIST", "L")]


def test_a_bare_keyword_keeps_a_none_value():
    """DELETE GROUP(CARDDEMO) ALL - the ALL is not decoration, it changes the command."""
    stmts, _ = lex_csd(" DELETE GROUP(CARDDEMO) ALL\n")
    assert _ops(stmts[0]) == [("GROUP", "CARDDEMO"), ("ALL", None)]


def test_operands_report_their_own_line_not_the_statements():
    stmts, _ = lex_csd(" DEFINE TRANSACTION(CAUP) GROUP(CARDDEMO)\n"
                       "        PROGRAM(COACTUPC)\n"
                       "        TRANCLASS(DFHTCL00)\n")
    lines = {op.keyword: op.line for op in stmts[0].operands}
    assert lines["TRANSACTION"] == 1
    assert lines["PROGRAM"] == 2
    assert lines["TRANCLASS"] == 3


def test_comments_and_blank_lines_are_skipped():
    stmts, flags = lex_csd("*** a banner\n"
                           "\n"
                           " DEFINE FILE(X) GROUP(G)\n"
                           "*\n")
    assert len(stmts) == 1
    assert flags == []


def test_content_past_column_72_is_reported_not_dropped_silently():
    """On a genuine card image - 80 columns, no more - column 73 onwards is the sequence
    field and CICS does not read it."""
    long = " DEFINE FILE(X) GROUP(G)" + " " * 44 + "STATUS(ENA)"
    assert len(long) <= 80
    stmts, flags = lex_csd(long + "\n")
    assert stmts[0].first("STATUS") is None
    assert any("past column 72" in f for f in flags)


def test_a_source_wider_than_a_card_keeps_its_operands():
    """A line of 121 characters is not sitting on an 80-column card, so the margin rule
    does not describe the file it is in. Cutting at 72 there threw away real attributes -
    a transaction's REMOTESYSTEM, a file's STRINGS - and the truncation then left an
    unclosed parenthesis that swallowed the rest of the statement."""
    long = (" DEFINE TRANSACTION(T) GROUP(G)" + " " * 40
            + "ROUTABLE(NO) REMOTESYSTEM(RSYB) REMOTENAME(TRNB)")
    assert len(long) > 80
    stmts, flags = lex_csd(long + "\n")
    assert stmts[0].first("REMOTESYSTEM") == "RSYB"
    assert stmts[0].first("REMOTENAME") == "TRNB"
    assert not any("past column 72" in f for f in flags)
    assert not any("never closed" in f for f in flags)
    # One flag about the FILE, not one per line: it is a single fact about the source.
    assert sum("not a deck of 80-column card images" in f for f in flags) == 1


def test_a_cut_line_marks_the_definition_it_damaged():
    """The region flag names the line. The statement has to carry it too, or a row whose
    attributes were cut reads exactly like one written without them."""
    long = " DEFINE TRANSACTION(T) GROUP(G)".ljust(72) + "SYS(RSY)"
    assert len(long) <= 80          # a card image, so the margin genuinely applies
    stmts, _ = lex_csd(long + "\n")
    assert stmts[0].damaged
    assert "cut" in stmts[0].damaged[0]


def test_an_unclosed_parenthesis_marks_the_definition_too():
    stmts, _ = lex_csd(" DEFINE FILE(ACCTDAT GROUP(CARDDEMO)\n")
    assert any("never closed" in d for d in stmts[0].damaged)


def test_a_sequence_field_past_column_72_is_not_flagged():
    line = (" DEFINE FILE(X) GROUP(G)".ljust(72) + "00000100")
    _, flags = lex_csd(line + "\n")
    assert flags == []


def test_an_unclosed_parenthesis_is_flagged_on_the_statement():
    stmts, _ = lex_csd(" DEFINE FILE(ACCTDAT GROUP(CARDDEMO)\n")
    assert any("never closed" in f for f in stmts[0].flags)


def test_text_before_the_first_command_is_reported():
    _, flags = lex_csd("garbage\n DEFINE FILE(X) GROUP(G)\n")
    assert any("before the first command" in f for f in flags)


def test_lower_case_is_accepted_and_normalised():
    """IBM's own sample deck is written in mixed case."""
    stmts, _ = lex_csd(" Define Transaction(HCAZ) Group(HCAZMOBL)\n"
                       "        Program(HCAZMENU) TaskDataLoc(Any)\n")
    assert stmts[0].verb == "DEFINE"
    assert stmts[0].first("TRANSACTION") == "HCAZ"
    assert stmts[0].first("PROGRAM") == "HCAZMENU"


# --------------------------------------------------------------------------- #
# the assembler dialect
# --------------------------------------------------------------------------- #

def test_a_continuation_resumes_at_column_16():
    first = "         DFHPCT TYPE=ENTRY,TRANSID=LGMN,".ljust(71) + "X"
    second = "               PROGRAM=LGMENU"
    stmts, flags = lex_macro(first + "\n" + second + "\n")
    assert len(stmts) == 1
    assert stmts[0].first("PROGRAM") == "LGMENU"
    assert flags == []


def test_a_continuation_mark_that_missed_column_72_is_flagged():
    """The classic hand-edited-deck failure, and it is silent: the mark is past the margin
    so nothing continues, the next line is read as a fresh statement, its operation is not
    a known macro, and every operand on it disappears. This repository's own first draft of
    legacy.pct had its X in column 74."""
    first = "         DFHPCT TYPE=ENTRY,TRANSID=LGMN,".ljust(73) + "X"
    stmts, flags = lex_macro(first + "\n               PROGRAM=LGMENU\n")
    assert any("column 72 itself is blank" in f for f in flags)
    assert stmts[0].first("PROGRAM") is None


def test_a_parenthesised_value_is_one_operand():
    """ACCMETH=(VSAM,KSDS) and SERVREQ=(GET,PUT) contain the comma that separates
    operands. Split naively, one file definition becomes four nonsense ones."""
    stmts, _ = lex_macro(
        "         DFHFCT TYPE=DATASET,DATASET=CUSTMAS,ACCMETH=(VSAM,KSDS)\n")
    assert stmts[0].first("ACCMETH") == "(VSAM,KSDS)"
    assert stmts[0].first("DATASET") == "CUSTMAS"


def test_conditional_assembly_is_flagged_not_evaluated():
    """A table deck inside an AIF is the &SYSPARM problem. Deciding it here would model a
    deck that never assembles."""
    _, flags = lex_macro("         AIF   ('&SYSPARM' EQ 'PROD').PROD\n")
    assert any("conditional assembly" in f for f in flags)


def test_a_label_in_column_one_is_the_statement_name():
    stmts, _ = lex_macro("DFHPCTL1 DFHPCT TYPE=INITIAL,SUFFIX=L1\n")
    assert stmts[0].label == "DFHPCTL1"
    assert stmts[0].operation == "DFHPCT"
