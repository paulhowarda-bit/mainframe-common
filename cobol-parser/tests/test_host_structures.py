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


# --------------------------------------------------------------------------- #
# Db2 VARCHAR: a level-49 length/data pair is ONE host variable
#
# The precompiler treats the GROUP as one value filling one column and never sends
# its two subordinates. Expanding it manufactures a second host variable, after which
# the count gate refuses the whole statement - or, worse, the inflated count happens
# to match and the correlation maps a column onto the 2-byte length item and states it
# as fact.
# --------------------------------------------------------------------------- #

VARCHAR = """       IDENTIFICATION DIVISION.
       PROGRAM-ID. T.
       DATA DIVISION.
       WORKING-STORAGE SECTION.
       01  BE-REC.
           10  BE-ACC-N          PIC X(9).
           10  BE-CMT-X.
               49  BE-CMT-X-LEN      PIC S9(4) COMP.
               49  BE-CMT-X-TEXT     PIC X(254).
       01  SPIB-BUF.
           49  SPIB-LENGTH       PIC S9(4).
           49  SPIB-DATA         PIC X(400).
       01  SQLERRM.
           49  SQLERRML          PIC S9(04) COMP-4.
           49  SQLERRMC          PIC X(70).
       01  IN-PARM-5.
           49  IN-RECEIVER-LEN   PIC S9(4) COMP.
           05  IN-RECEIVER-ID    PIC X(400).
       01  PLAIN-PAIR.
           05  P-LEN             PIC S9(4) COMP.
           05  P-TXT             PIC X(254).
       01  TAIL-REC.
           10  T-CMT.
               49  T-CMT-LEN         PIC S9(4) COMP.
               49  T-CMT-TXT         PIC X(10).
           10  T-AFTER           PIC X(4).
       01  NEST-OUTER.
           10  NEST-REC.
               49  NV-LEN            PIC S9(4) COMP.
               05  NV-DATA           PIC X(10).
       01  THREE-KIDS.
           49  TK-A              PIC S9(4) COMP.
           49  TK-B              PIC X(10).
           49  TK-C              PIC X(10).
       01  WS-N              PIC 9(3).
       LINKAGE SECTION.
       01  LK-PARM-V.
           49  LK-PARM-LEN       PIC S9(4) COMP.
           49  LK-PARM-TEXT      PIC X(400).
       PROCEDURE DIVISION.
       0000-MAIN.
           STOP RUN.
"""


def test_a_varchar_pair_is_one_host_variable():
    # A group with two level-49 subordinates is what Db2 calls a VARCHAR: the
    # precompiler sends the GROUP, one value for one column. Expanding it manufactures
    # a second host variable, and the count gate then refuses the whole statement.
    assert elementary_subordinates(_items(VARCHAR), "BE-CMT-X") is None


def test_a_varchar_length_item_needs_no_usage_clause():
    # Estate members write `49 X-LENGTH PIC S9(4).` with no COMP (SAMPI009.CBL:39,
    # SAMPR119.CBL:744). The gate is the LEVEL NUMBER; a `S9(4) COMP` picture test
    # would miss them.
    assert elementary_subordinates(_items(VARCHAR), "SPIB-BUF") is None


def test_sqlca_own_varchar_reads_the_same_way():
    # SQLERRM is a hand-written copybook group, not a DCLGEN. The rule is the shape,
    # not "looks like a DCLGEN".
    assert elementary_subordinates(_items(VARCHAR), "SQLERRM") is None


def test_only_one_child_has_to_be_at_49():
    # SAMPG004:133-135 and SAMPG008:296-298 are real VARCHAR parameters declared
    # 49 + 10 and 49 + 05.
    assert elementary_subordinates(_items(VARCHAR), "IN-PARM-5") is None


def test_a_plain_two_field_group_still_expands():
    # An ordinary two-field group filling two columns. Collapsing one would INVENT
    # lineage, which is the only way this could be worse than the defect. This test
    # must pass both before and after the change.
    assert elementary_subordinates(_items(VARCHAR), "PLAIN-PAIR") == ["P-LEN", "P-TXT"]


def test_a_three_child_group_at_49_is_not_a_varchar_pair():
    # A VARCHAR is exactly two subordinates. Three is some other structure, and the
    # scan must stop counting rather than collapse it.
    assert elementary_subordinates(_items(VARCHAR), "THREE-KIDS") == [
        "TK-A", "TK-B", "TK-C"]


def test_a_varchar_member_of_a_record_contributes_its_group_name():
    # The mid-walk case: `SELECT ... INTO :BE-REC` names the record, and the VARCHAR
    # member inside it sends ITS OWN name for one column and its pair for none. The
    # walk is a flat positional scan, so this needs a skip-subtree level tracker -
    # returning early only covers a statement naming the VARCHAR group directly.
    assert elementary_subordinates(_items(VARCHAR), "BE-REC") == [
        "BE-ACC-N", "BE-CMT-X"]


def test_the_walk_resumes_after_a_varchar_members_pair():
    # The skip tracker must clear on the first item at or above the VARCHAR group's
    # level, or every field declared after it is swallowed too.
    assert elementary_subordinates(_items(VARCHAR), "TAIL-REC") == ["T-CMT", "T-AFTER"]


def test_a_varchar_whose_data_item_outranks_its_group_is_a_known_gap():
    """A `10 GRP.` / `49 LEN` / `05 TEXT` member is NOT detected, and this pins that.

    `_varchar_pair_at` breaks on `it.level <= g.level`, so the non-49 child must sit
    strictly BELOW the group's own level - the `05` here ends the run before the pair
    is complete, leaving one child. Both estate declarations of the mixed-level shape
    are at `01`, where `05`/`10` are genuinely subordinate, so neither is missed; the
    shape is recorded as a stated gap rather than papered over, because widening the
    run past a lower level number would break the positional walk that FILLER,
    REDEFINES and 66/77 handling all depend on.
    """
    items = _items(VARCHAR)
    assert elementary_subordinates(items, "NEST-REC") == ["NV-LEN"]
    assert elementary_subordinates(items, "NEST-OUTER") == ["NV-LEN", "NV-DATA"]


def test_a_linkage_varchar_parameter_reads_the_same_way():
    # A stored-procedure VARCHAR parameter that is never used as a host variable
    # (modelled on DEMOPRC1.CBL:88-90). Nothing about it should differ from a
    # WORKING-STORAGE one: the rule is the declaration shape, not the section.
    assert elementary_subordinates(_items(VARCHAR), "LK-PARM-V") is None


VARCHAR_SQL = """       IDENTIFICATION DIVISION.
       PROGRAM-ID. TSQL.
       DATA DIVISION.
       WORKING-STORAGE SECTION.
       01  BE-ACC-N          PIC X(9).
       01  BE-CMT-X.
           49  BE-CMT-X-LEN      PIC S9(4) COMP.
           49  BE-CMT-X-TEXT     PIC X(254).
       01  BE-REC.
           10  BE-R-ACC          PIC X(9).
           10  BE-R-CMT.
               49  BE-R-CMT-LEN      PIC S9(4) COMP.
               49  BE-R-CMT-TEXT     PIC X(254).
       PROCEDURE DIVISION.
       0000-MAIN.
           EXEC SQL
               INSERT INTO T_X (ACC_N, CMT)
                    VALUES (:BE-ACC-N, :BE-CMT-X)
           END-EXEC.
           EXEC SQL
               SELECT ACC_N, CMT INTO :BE-REC FROM T_X
           END-EXEC.
           EXEC SQL
               UPDATE T_X SET CMT = :BE-CMT-X
                 WHERE ACC_N = :BE-ACC-N
           END-EXEC.
           EXEC SQL
               DECLARE C1 CURSOR FOR
                 SELECT CMT FROM T_X
           END-EXEC.
           EXEC SQL
               FETCH C1 INTO :BE-CMT-X
           END-EXEC.
           STOP RUN.
"""


def test_a_varchar_no_longer_breaks_the_insert_arity():
    # Before: "2 column(s) vs 3 VALUES item(s), host structures already expanded" - and
    # the note blamed a copybook that had in fact arrived and been expanded.
    st = _execs(VARCHAR_SQL)[0]
    assert st.columns == [{"column": "ACC_N", "hostVar": "BE-ACC-N"},
                          {"column": "CMT",   "hostVar": "BE-CMT-X"}]
    assert st.column_note is None
    assert st.expanded_structures == []      # nothing WAS expanded


def test_a_varchar_member_of_a_record_correlates():
    st = _execs(VARCHAR_SQL)[1]              # SELECT ACC_N, CMT INTO :BE-REC
    assert st.into_vars == ["BE-R-ACC", "BE-R-CMT"]
    assert st.expanded_structures == ["BE-REC"]
    assert st.column_note is None
    assert st.columns == [{"column": "ACC_N", "hostVar": "BE-R-ACC"},
                          {"column": "CMT",   "hostVar": "BE-R-CMT"}]


def test_an_update_set_slot_names_a_host_variable_the_statement_also_lists():
    """The SET pair named the GROUP while the statement-wide host-variable scan listed
    its two children - a column mapped to a host variable absent from the statement's
    own field list, with NO note anywhere. Downstream that loses the edge exactly as
    the loud refusal does, by a different mechanism. SAMPF099:997 is the estate
    instance.

    Note this closes the INCONSISTENCY, not the silent-drop class: a SET slot whose
    column or host variable does not parse is still dropped with no note, which is its
    own defect and needs its own test.
    """
    st = _execs(VARCHAR_SQL)[2]
    assert st.columns == [{"column": "CMT", "hostVar": "BE-CMT-X"}]
    assert st.host_vars == [":BE-CMT-X", ":BE-ACC-N"]
    assert st.expanded_structures == []


def test_a_fetch_of_a_varchar_does_not_zip_a_column_onto_the_length_item():
    # The worst outcome: when the inflated count HAPPENS to equal the cursor's column
    # count the correlation does not refuse - it maps the first column onto the 2-byte
    # length item and states it as fact, emitting no note, so these sites appear in no
    # census.
    st = _execs(VARCHAR_SQL)[4]
    assert st.into_vars == ["BE-CMT-X"]
    assert st.expanded_structures == []
