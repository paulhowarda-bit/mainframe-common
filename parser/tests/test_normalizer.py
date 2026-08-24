from cobol_parse.normalizer import (
    normalize,
    SourceFormat,
    detect_source_format,
)


def test_fixed_strips_comment_and_sequence_area():
    src = (
        "000100 IDENTIFICATION DIVISION.\n"
        "000200* this is a comment line\n"
        "000300 PROGRAM-ID. T.\n"
    )
    lines = normalize(src, SourceFormat.FIXED)
    texts = [cl.text.strip() for cl in lines]
    assert "IDENTIFICATION DIVISION." in texts
    assert "PROGRAM-ID. T." in texts
    assert all("comment" not in t for t in texts)


def test_fixed_discards_identification_area_73_80():
    # Cols 73-80 carry an identifier that must be dropped in fixed format.
    line = "      " + " " + "       MOVE A TO B.".ljust(65) + "IDENT123"
    lines = normalize(line, SourceFormat.FIXED)
    assert lines[0].text.strip() == "MOVE A TO B."
    assert "IDENT" not in lines[0].text


def test_area_a_flag_distinguishes_header_from_statement():
    src = (
        "       0000-MAIN.\n"
        "           MOVE A TO B.\n"
    )
    lines = normalize(src, SourceFormat.FIXED)
    assert lines[0].area_a is True   # paragraph header in Area A
    assert lines[1].area_a is False  # statement in Area B


def test_inline_comment_removed_but_not_inside_literal():
    src = "       DISPLAY 'a *> b'  *> trailing comment\n"
    lines = normalize(src, SourceFormat.FIXED)
    assert lines[0].text.strip() == "DISPLAY 'a *> b'"


def test_fixed_continuation_stitches_split_literal():
    src = (
        "       MOVE 'HELLO-\n"
        "      -    WORLD' TO WS.\n"
    )
    lines = normalize(src, SourceFormat.FIXED)
    joined = " ".join(cl.text for cl in lines)
    assert "'HELLO-WORLD'" in joined.replace(" ", "") or "HELLO-WORLD" in joined


def test_free_format_detected_by_directive():
    src = ">>SOURCE FORMAT FREE\nMOVE A TO B.\n"
    lines = normalize(src)
    assert lines[-1].text.strip() == "MOVE A TO B."


def test_spaced_source_format_directive_wins_over_column_heuristic():
    # `>>SOURCE FORMAT FREE` (the standard IBM spaced form) must be honored even when
    # the body is indented enough that the column heuristic would vote FIXED. If the
    # directive were missed, fixed-format normalization would chop cols 1-7 off every
    # line and corrupt the whole program downstream.
    src = (
        ">>SOURCE FORMAT FREE\n"
        "        MOVE WS-A TO WS-B.\n"
        "        ADD 1 TO WS-A.\n"
    )
    lines = normalize(src)
    texts = [cl.text.strip() for cl in lines]
    assert "MOVE WS-A TO WS-B." in texts
    assert "ADD 1 TO WS-A." in texts


def test_fixed_format_directive_spaced_form():
    src = ">>SOURCE FORMAT IS FIXED\n000100 PROGRAM-ID. T.\n"
    lines = normalize(src)
    assert any(cl.text.strip() == "PROGRAM-ID. T." for cl in lines)


def test_free_format_indented_header_is_area_a_candidate():
    # Free format has no Area A column rule; an indented paragraph header must still
    # be flagged as a header candidate so the parser can find it.
    src = ">>SOURCE FORMAT FREE\n    1000-MAIN.\n        MOVE A TO B.\n"
    lines = normalize(src)
    header = next(cl for cl in lines if cl.text.strip() == "1000-MAIN.")
    assert header.area_a is True


# --- source-format detection: layered, with confidence ---------------------- #

def test_detect_directive_is_authoritative_and_certain():
    det = detect_source_format(">>SOURCE FORMAT FREE\nMOVE A TO B.\n")
    assert det.format is SourceFormat.FREE
    assert det.confidence == 1.0


def test_detect_numbered_fixed_source_is_confident():
    src = (
        "000100 IDENTIFICATION DIVISION.\n"
        "000200 PROGRAM-ID. T.\n"
        "000300 PROCEDURE DIVISION.\n"
    )
    det = detect_source_format(src)
    assert det.format is SourceFormat.FIXED
    assert det.is_confident


def test_detect_left_margin_code_is_free_and_confident():
    src = (
        "IDENTIFICATION DIVISION.\n"
        "PROGRAM-ID. T.\n"
        "PROCEDURE DIVISION.\n"
        "MOVE A TO B.\n"
    )
    det = detect_source_format(src)
    assert det.format is SourceFormat.FREE
    assert det.is_confident


def test_fixed_long_lines_and_ident_area_not_misread_as_free():
    # Regression: real fixed source fills cols 73-80 (identification area) and can run
    # past column 80. Line length is NOT a free-format signal - the compiler ignores
    # everything past col 72 - so such a program must still detect as FIXED.
    def fixed(code: str) -> str:
        # blank seq (1-6), blank indicator (7), code at col 8, ident field past col 80
        return ("      " + " " + code).ljust(72) + "IDENT0001"
    src = "\n".join([
        fixed("IDENTIFICATION DIVISION."),
        fixed("PROGRAM-ID. BIG."),
        fixed("PROCEDURE DIVISION."),
        fixed("0000-MAIN."),
        fixed("    MOVE WS-A TO WS-B"),
        fixed("    STOP RUN."),
    ]) + "\n"
    assert max(len(line) for line in src.splitlines()) > 80
    det = detect_source_format(src)
    assert det.format is SourceFormat.FIXED


def test_detect_fixed_with_alphanumeric_change_markers_in_sequence_area():
    # THE change-marker case (e.g. FBMMAAIO): real fixed source carries alphanumeric
    # change/revision markers in the sequence area (cols 1-6). The compiler ignores
    # cols 1-6, so these must NOT be read as free-format code. Column 7 stays a valid
    # indicator on every line, so the column-7 invariant classifies it fixed.
    src = (
        "CHG001 IDENTIFICATION DIVISION.\n"
        "CHG001 PROGRAM-ID. FBMMAAIO.\n"
        "PR1234 PROCEDURE DIVISION.\n"
        "PR1234 5000-PROCESS.\n"
        "MOD07A     MOVE WS-A TO WS-B.\n"
    )
    det = detect_source_format(src)
    assert det.format is SourceFormat.FIXED
    assert det.is_confident
    # And the change markers are stripped, leaving clean code.
    texts = [cl.text.strip() for cl in normalize(src)]
    assert "IDENTIFICATION DIVISION." in texts
    assert "PROGRAM-ID. FBMMAAIO." in texts
    assert not any("CHG001" in t or "PR1234" in t for t in texts)


def test_detect_indented_fixed_layout_is_fixed_and_lossless():
    # Code indented to col 8 with a clean column 7 (all blank): the column-7 invariant
    # holds, so this is fixed - and reading it as fixed is lossless.
    src = (
        "        IDENTIFICATION DIVISION.\n"
        "        PROGRAM-ID. T.\n"
        "        PROCEDURE DIVISION.\n"
        "        MOVE A TO B.\n"
    )
    det = detect_source_format(src)
    assert det.format is SourceFormat.FIXED
    texts = [cl.text.strip() for cl in normalize(src)]
    assert "IDENTIFICATION DIVISION." in texts
    assert "PROCEDURE DIVISION." in texts


def test_detect_margin_code_is_free_via_division_header_and_column7():
    # A free program whose code sits at the left margin: column 7 holds code (invariant
    # broken) and the DIVISION header is at column 1. Detection must recover FREE, and
    # normalization must keep the program intact.
    src = (
        "IDENTIFICATION DIVISION.\n"
        "PROGRAM-ID. FOO.\n"
        "PROCEDURE DIVISION.\n"
        "    DISPLAY 'HI'.\n"
        "    STOP RUN.\n"
    )
    det = detect_source_format(src)
    assert det.format is SourceFormat.FREE
    texts = [cl.text.strip() for cl in normalize(src)]
    assert "PROGRAM-ID. FOO." in texts


# -- continuation of a split literal (parsing-cobol.md Stage 1) --------------

def test_continuation_stitches_a_split_literal_without_the_resume_quote():
    """A literal continued across lines: the continuation's leading quote is the
    RESUME marker, not data. Keeping it produced a truncated literal followed by a
    junk word - silent corruption of every value built this way."""
    src = (
        "       IDENTIFICATION DIVISION.\n"
        "       PROGRAM-ID. T.\n"
        "       PROCEDURE DIVISION.\n"
        "       0000-MAIN.\n"
        "           MOVE 'ABCDEFGHIJ\n"
        "      -    'KLMNO' TO WS-LONG.\n"
    )
    text = normalize(src, SourceFormat.FIXED)[-1].text
    assert "'ABCDEFGHIJKLMNO'" in text
    assert "'ABCDEFGHIJ'" not in text      # not closed early
    assert text.count("'") == 2            # exactly one literal, properly terminated


def test_continuation_of_a_plain_word_still_joins_with_a_space():
    src = (
        "       PROCEDURE DIVISION.\n"
        "       0000-MAIN.\n"
        "           MOVE WS-A\n"
        "      -    TO WS-B.\n"
    )
    text = normalize(src, SourceFormat.FIXED)[-1].text
    assert "MOVE WS-A TO WS-B." in text


def test_continued_literal_keeps_blanks_that_belong_to_it():
    """Blanks through column 72 are part of a continued literal, so they must not be
    right-stripped away before the stitch."""
    line1 = "           MOVE 'AB" + " " * 10          # literal open, trailing blanks
    src = (
        "       PROCEDURE DIVISION.\n"
        "       0000-MAIN.\n"
        + line1 + "\n"
        "      -    'CD' TO WS-X.\n"
    )
    text = normalize(src, SourceFormat.FIXED)[-1].text
    assert "'AB          CD'" in text


# -- compiler-directing statements -------------------------------------------

def test_cbl_directive_line_is_consumed_not_mangled():
    """CBL/PROCESS may start in column 1 - inside the sequence area the fixed reader
    slices off - which used to leave a fragment like 'RCE,NOSSRANGE' in the stream."""
    src = (
        "CBL SOURCE,NOSSRANGE\n"
        "       IDENTIFICATION DIVISION.\n"
        "       PROGRAM-ID. PAYROLL.\n"
    )
    texts = [cl.text.strip() for cl in normalize(src, SourceFormat.FIXED)]
    assert texts[0] == "IDENTIFICATION DIVISION."
    assert not any("NOSSRANGE" in t for t in texts)


def test_a_paragraph_named_process_is_not_eaten_as_a_directive():
    src = (
        "       IDENTIFICATION DIVISION.\n"
        "       PROGRAM-ID. T.\n"
        "       PROCEDURE DIVISION.\n"
        "       PROCESS-RECORD.\n"
        "           MOVE 1 TO WS-A.\n"
    )
    texts = [cl.text.strip() for cl in normalize(src, SourceFormat.FIXED)]
    assert "PROCESS-RECORD." in texts
