"""Lexing a window of a source, the way a reader that never holds the whole member must.

The property a windowing caller depends on: lex a source whole, then lex it as overlapping
line ranges with the SAME supplied prelude, and every statement a window holds completely
is the statement the whole produced - same line numbers, same operands, same damage. The
last statement of a window may be cut short by the window's end; that is what the overlap
is for, and why it is excluded.

The prelude is the part a window cannot work out for itself: whether the source is card
images (its longest line) and whether column 1 is carriage control (column 1 of every
line). Each test below supplies it rather than letting the window derive it, so both paths
- supplied and derived - are covered.
"""

import io

from cics_parser.lexer import (Prelude, lex_csd, lex_csd_lines, lex_csd_report,
                               lex_csd_report_lines, lex_macro, lex_macro_lines)


def _deck(count=12):
    """A deck whose statements run over several lines, with the shapes real decks have:
    DESCRIPTION in column 2, a value holding blanks and commas, an ADD(YES) attribute."""
    lines = ["*** a banner comment"]
    for n in range(count):
        lines += [
            " DEFINE FILE(FILE%02d) GROUP(APPG)" % n,
            " DESCRIPTION(FILE NUMBER %d, IN GROUP APPG)" % n,
            "        DSNAME(PROD.FILE%02d.DATA) ADD(YES) DELETE(NO)" % n,
            "        WAITTIME(0,0,%d) STATUS(ENABLED)" % n,
            "",
        ]
    return lines


def _listing(count=12, eject="1"):
    """DFHCSDUP LIST output: ASA column 1, a page eject on every third header, the print
    stamp in the right margin, indented attributes, and the message trailer."""
    lines = [" DFHCSDUP - CICS DEFINITION FILE UTILITY PROGRAM" + " " * 22 + "15.206 23:09",
             " LIST GROUP(RPTG) OBJECTS"]
    for n in range(count):
        asa = eject if n % 3 == 0 else " "
        lines += [
            asa + ("TRANSACTION(T%03d)" % n).ljust(24) + "GROUP(RPTG)" + " " * 44
            + "15.206 23:09",
            " " * 25 + "PROGRAM(PGM%03d)        TWASIZE(0)" % n,
            " " * 25 + "DEFINETIME(15/07/25 23:09:21)",
        ]
    lines.append(" DFH5109 I END OF DFHCSDUP UTILITY JOB.  HIGHEST RETURN CODE WAS:   0")
    return lines


def _macro(count=12):
    lines = ["* A PCT"]
    for n in range(count):
        lines += [
            ("         DFHPCT TYPE=ENTRY,TRANSID=T%03d," % n).ljust(71) + "X",
            "               PROGRAM=PGM%03d,TWASIZE=0" % n,
        ]
    return lines


def _window(lex, lines, start, end, **kw):
    """Lex lines[start:end] - 0-based slice, so the window's first line is start + 1."""
    return lex(lines[start:end], first_line=start + 1, **kw)


def _assert_windows_agree(lex, lines, bounds, **kw):
    whole, _ = lex(lines, **kw)
    by_line = {s.line: s for s in whole}
    covered = set()
    for start, end in bounds:
        stmts, _ = _window(lex, lines, start, end, **kw)
        complete = stmts if end >= len(lines) else stmts[:-1]
        assert complete, "window %d-%d holds no complete statement" % (start + 1, end)
        for stmt in complete:
            assert stmt == by_line[stmt.line], stmt.line
            covered.add(stmt.line)
    assert covered == set(by_line), sorted(set(by_line) - covered)


# --------------------------------------------------------------------------- #
# overlapping windows agree with the whole
# --------------------------------------------------------------------------- #

def test_deck_windows_carry_the_wholes_line_numbers_and_operands():
    lines = _deck()
    prelude = Prelude.of(lines)
    # Overlaps chosen to start and end mid-statement, never on a boundary.
    _assert_windows_agree(lex_csd_lines, lines, [(0, 27), (19, 45), (38, len(lines))],
                          prelude=prelude)


def test_listing_windows_carry_the_wholes_line_numbers_and_operands():
    lines = _listing()
    prelude = Prelude.of(lines)
    _assert_windows_agree(lex_csd_report_lines, lines, [(0, 17), (12, 30), (25, len(lines))],
                          prelude=prelude)


def test_macro_windows_carry_the_wholes_line_numbers_and_operands():
    """No prelude here: the assembler dialect decides nothing per source."""
    lines = _macro()
    # A window starting on a continuation line would read it as a statement of its own,
    # exactly as a whole deck whose first line is one does - so these start on a boundary.
    _assert_windows_agree(lex_macro_lines, lines, [(0, 10), (7, 18), (15, len(lines))])


def test_a_window_reports_its_flags_at_the_files_line_numbers():
    """Both kinds: a file-level flag (text cut at the margin) and a statement's own (a
    parenthesis that never closes)."""
    lines = _deck()
    lines[28] = lines[28].ljust(72) + "SYS(RS1)"                 # line 29, cut at 72
    lines[30] = " DEFINE FILE(BROKEN GROUP(APPG)"                # line 31, never closed
    _, whole = lex_csd_lines(lines)
    stmts, flags = _window(lex_csd_lines, lines, 25, 40, prelude=Prelude.of(lines))
    cut = [f for f in flags if "past column 72" in f]
    assert len(cut) == 1 and cut[0].startswith("line 29:") and cut[0] in whole
    broken = next(s for s in stmts if s.line == 31)
    assert any(f.startswith("line 31:") and "never closed" in f for f in broken.flags)


# --------------------------------------------------------------------------- #
# the prelude, in each direction
# --------------------------------------------------------------------------- #

def test_a_wide_source_is_read_at_full_width_in_a_window_that_looks_like_cards():
    """One line past column 80 anywhere makes the whole source not card images. A window
    that does not contain that line looks like cards on its own, and would cut at 72 -
    losing the REMOTESYSTEM that sits at columns 73-80 on the line it DOES contain."""
    lines = _deck()
    lines[3] = lines[3].ljust(72) + "SYS(RS1)"                   # columns 73-80
    lines.append(" DEFINE FILE(WIDE) GROUP(APPG)".ljust(90) + "STATUS(ENABLED)")
    prelude = Prelude.of(lines)
    assert prelude.longest > 80

    stmts, flags = _window(lex_csd_lines, lines, 0, 20, prelude=prelude)
    assert stmts[0].first("SYS") == "RS1"
    assert not stmts[0].damaged
    assert any("not a deck of 80-column card images" in f for f in flags)

    derived, _ = _window(lex_csd_lines, lines, 0, 20)             # the window decides
    assert derived[0].first("SYS") is None
    assert derived[0].damaged


def test_a_genuine_card_image_is_still_cut_at_72_in_a_window():
    lines = _deck()
    lines[3] = lines[3].ljust(72) + "SYS(RS1)"
    prelude = Prelude.of(lines)
    assert prelude.longest <= 80

    whole, _ = lex_csd_lines(lines)
    stmts, flags = _window(lex_csd_lines, lines, 0, 20, prelude=prelude)
    assert stmts[0].first("SYS") is None
    assert stmts[0].damaged == whole[0].damaged
    assert "line(s) 4 " in stmts[0].damaged[0]
    assert any(f.startswith("line 4: content past column 72") for f in flags)


def test_asa_control_is_stripped_in_a_window_because_the_prelude_says_so():
    """Column 1 of a page-eject header is `1`. Read as data it joins the object's type -
    `1TRANSACTION(...)` is no header at all - and the object disappears."""
    lines = _listing()
    prelude = Prelude.of(lines)
    assert prelude.asa

    stmts, _ = _window(lex_csd_report_lines, lines, 2, 12, prelude=prelude)
    assert [s.first("TRANSACTION") for s in stmts][:2] == ["T000", "T001"]

    as_data = Prelude(prelude.longest, frozenset("X"))           # column 1 is data
    lost, flags = _window(lex_csd_report_lines, lines, 2, 12, prelude=as_data)
    assert "T000" not in [s.first("TRANSACTION") for s in lost]
    assert any(f.startswith("line 3: text outside any object") for f in flags)


def test_a_listing_captured_without_column_one_is_not_stripped():
    """The other direction: a PC-side transfer that dropped the carriage control. Stripped
    anyway, every header loses the first letter of its type."""
    lines = [line[1:] if line[:1] in " 1" else line for line in _listing()]
    prelude = Prelude.of(lines)
    assert not prelude.asa
    stmts, _ = _window(lex_csd_report_lines, lines, 2, 12, prelude=prelude)
    assert stmts[0].first("TRANSACTION") == "T000"


# --------------------------------------------------------------------------- #
# a streaming caller
# --------------------------------------------------------------------------- #

def test_the_prelude_is_one_pass_over_a_files_own_lines():
    """An open file yields lines WITH their terminators; the prelude must not count them."""
    text = "\n".join(_listing()) + "\n"
    expected = Prelude.of(text.splitlines())
    assert Prelude.of(io.StringIO(text)) == expected
    assert Prelude.of(io.StringIO(text.replace("\n", "\r\n"), newline="")) == expected


def test_a_supplied_prelude_lets_the_lines_be_consumed_as_they_arrive():
    text = "\n".join(_deck()) + "\n"
    prelude = Prelude.of(io.StringIO(text))
    streamed = lex_csd_lines(io.StringIO(text), prelude=prelude)
    assert streamed == lex_csd(text)


def test_without_a_prelude_the_lines_given_are_the_whole_source():
    """The default keeps today's behaviour: every text entry point is this, over
    ``text.splitlines()``."""
    for lex_lines, lex, lines in ((lex_csd_lines, lex_csd, _deck()),
                                  (lex_csd_report_lines, lex_csd_report, _listing()),
                                  (lex_macro_lines, lex_macro, _macro())):
        text = "\n".join(lines) + "\n"
        assert lex_lines(iter(lines)) == lex(text)
