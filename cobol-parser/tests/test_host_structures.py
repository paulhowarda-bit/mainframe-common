"""Db2 host structures and null indicators: what the precompiler does to a statement
before it reaches the database, and what this parser therefore has to do to it too.

A GROUP-level host variable stands for every elementary item under it, and `:DATA:IND`
is one value with its null status rather than two values. Recovered any other way, the
column count and the host-variable count disagree, the correlation is refused, and
every field of the statement reaches a consumer with no column to map it to
(docs/issues/host-structure-expansion.md, in the cobol-xstate repository).
"""

from cobol_parser.data_division import elementary_subordinates, parse_data_division
from cobol_parser.model import ExecStmt
from cobol_parser.normalizer import detect_source_format, normalize
from cobol_parser.parser import parse_program


LAYOUT = """       IDENTIFICATION DIVISION.
       PROGRAM-ID. T.
       DATA DIVISION.
       WORKING-STORAGE SECTION.
       01  OUTER-GROUP.
           05  INNER-1.
               10  F1              PIC X(5).
               10  F2              PIC X(5).
           05  FILLER              PIC X(2).
           05  INNER-2.
               10  F3              PIC X(5).
               10  F3-R  REDEFINES F3.
                   15  F3A         PIC X(2).
                   15  F3B         PIC X(3).
           05  TAB                 PIC X(4) OCCURS 10.
           05  ST                  PIC X.
               88  ST-YES          VALUE 'Y'.
       77  WS-STANDALONE           PIC X(4).
       01  ALL-FILLER.
           05  FILLER              PIC X(3).
       01  PLAIN-SCALAR            PIC X(3).
       PROCEDURE DIVISION.
       0000-MAIN.
           STOP RUN.
"""


def _items(src=LAYOUT):
    return parse_data_division(normalize(src, detect_source_format(src).format))[0]


# --------------------------------------------------------------------------- #
# the expansion walk
# --------------------------------------------------------------------------- #

def test_group_expands_to_its_elementary_items_in_declaration_order():
    # Nested groups are recursed into and never named themselves; FILLER is not a
    # host variable; a REDEFINES occupies its original's storage, so it AND its
    # subordinates are excluded; an OCCURS item is one host variable, not ten; and an
    # 88-level condition name is not storage at all, so the walk steps over it rather
    # than ending there (ST, declared after it, still arrives).
    assert elementary_subordinates(_items(), "OUTER-GROUP") == [
        "F1", "F2", "F3", "TAB", "ST"]


def test_a_nested_group_expands_on_its_own_too():
    assert elementary_subordinates(_items(), "INNER-1") == ["F1", "F2"]
    assert elementary_subordinates(_items(), "INNER-2") == ["F3"]


def test_a_level_77_after_a_group_is_not_swept_into_it():
    # 77 is always top-level, but its LEVEL NUMBER is higher than any group's - so a
    # bare `level > group.level` walk reads WS-STANDALONE as a member of OUTER-GROUP
    # and hands the statement a host variable the source never put there.
    assert "WS-STANDALONE" not in elementary_subordinates(_items(), "OUTER-GROUP")


def test_what_is_not_a_group_is_left_alone():
    # None, not [] - the caller keeps the name exactly as the source spells it. A
    # group of pure FILLER answers the same way: an empty expansion would silently
    # DELETE the reference, where keeping it lets the count gate flag it.
    assert elementary_subordinates(_items(), "PLAIN-SCALAR") is None
    assert elementary_subordinates(_items(), "ALL-FILLER") is None
    assert elementary_subordinates(_items(), "NOT-DECLARED-ANYWHERE") is None


# --------------------------------------------------------------------------- #
# the statement parser
# --------------------------------------------------------------------------- #

SQL = """       IDENTIFICATION DIVISION.
       PROGRAM-ID. T.
       DATA DIVISION.
       WORKING-STORAGE SECTION.
       01  REC.
           05  R-A                 PIC X(4).
           05  R-B                 PIC X(4).
       01  WS-N                    PIC 9(3).
       01  WS-IND                  PIC S9(4) COMP.
       PROCEDURE DIVISION.
       0000-MAIN.
           EXEC SQL
               SELECT A, B INTO :REC FROM T1
           END-EXEC
           EXEC SQL
               INSERT INTO T1 (A, B, N) VALUES (:REC, :WS-N)
           END-EXEC
           EXEC SQL
               INSERT INTO T1 VALUES (:REC, :WS-N)
           END-EXEC
           EXEC SQL
               SELECT A, B INTO :R-A, :R-B:WS-IND FROM T1
           END-EXEC
           EXEC SQL
               SELECT A INTO :R-A INDICATOR :WS-IND FROM T1
           END-EXEC
           EXEC SQL
               UPDATE T1 SET A = :R-A:WS-IND WHERE N = :WS-N
           END-EXEC
           EXEC SQL
               SELECT A, B INTO :NOT-DECLARED FROM T1
           END-EXEC
           STOP RUN.
"""


def _execs(src=SQL):
    prog = parse_program(src)
    return [st for p in prog.paragraphs for st in p.statements
            if isinstance(st, ExecStmt)]


def test_into_a_group_names_one_target_per_elementary_item():
    st = _execs()[0]
    assert st.into_vars == ["R-A", "R-B"]
    assert st.expanded_structures == ["REC"]
    # ...and the correlation the expansion unlocks: 2 columns, 2 targets.
    assert st.columns == [{"column": "A", "hostVar": "R-A"},
                          {"column": "B", "hostVar": "R-B"}]
    assert st.column_note is None


def test_the_statement_wide_scan_expands_too():
    # `fields` downstream is built from this list, not from into_vars. Expanding one
    # without the other leaves the group name as a phantom PARAMETER of the very
    # event whose fields it was just expanded into.
    assert _execs()[0].host_vars == [":R-A", ":R-B"]


def test_a_group_slot_fills_as_many_columns_as_it_has_items():
    st = _execs()[1]
    assert st.columns == [{"column": "A", "hostVar": "R-A"},
                          {"column": "B", "hostVar": "R-B"},
                          {"column": "N", "hostVar": "WS-N"}]
    assert st.expanded_structures == ["REC"]


def test_a_column_list_less_insert_records_the_expanded_slots():
    # The zip against the table's declared order happens at build time; what this
    # site owes it is the slot list AFTER expansion, or the arity it is weighed
    # against was never the one Db2 sees.
    st = _execs()[2]
    assert st.values_list == ["R-A", "R-B", "WS-N"]
    assert st.expanded_structures == ["REC"]


def test_an_indicator_is_not_a_target_of_its_own():
    st = _execs()[3]
    assert st.into_vars == ["R-A", "R-B"]
    assert st.indicator_vars == ["WS-IND"]
    assert st.host_vars == [":R-A", ":R-B"]
    assert st.columns == [{"column": "A", "hostVar": "R-A"},
                          {"column": "B", "hostVar": "R-B"}]


def test_the_indicator_keyword_spelling_reads_the_same_way():
    st = _execs()[4]
    assert st.into_vars == ["R-A"]
    assert st.indicator_vars == ["WS-IND"]


def test_an_indicated_set_slot_still_maps_its_column():
    # `SET A = :R-A:WS-IND` writes column A from R-A exactly as `SET A = :R-A` does;
    # the indicator says whether the value is null, not what the value is.
    st = _execs()[5]
    assert st.columns == [{"column": "A", "hostVar": "R-A"}]
    assert st.indicator_vars == ["WS-IND"]


def test_a_name_the_data_division_does_not_hold_is_kept_as_written():
    # A field whose copybook never arrived cannot be expanded, and inventing an
    # expansion for it would be a guess. It stays as the source spells it, and the
    # count gate says so rather than the recovery pretending otherwise.
    st = _execs()[6]
    assert st.into_vars == ["NOT-DECLARED"]
    assert st.expanded_structures == []
    assert st.columns == []
    assert "not correlatable" in st.column_note
