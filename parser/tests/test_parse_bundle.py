"""The parse-bundle contract: encode/decode fidelity and every refusal.

The strongest proof lives in tools/gate.py (every view of every example, byte-identical
through the round trip); these are the fast, pointed complements - per-node fidelity,
the two representational restorations (tuples, data_by_name identity), and the envelope
refusing what it must refuse."""

import json

import pytest

from cobol_parse.model import (Action, AlterStmt, EvaluateStmt, HandledStmt,
                               SearchStmt)
from cobol_parse.normalizer import SourceFormat
from cobol_parse.parse_bundle import (FORMAT, VERSION, ParseBundleError,
                                      open_parse_bundle, program_from_dict,
                                      program_to_dict, write_parse_bundle)
from cobol_parse.parser import parse_program


def _wrap(proc_body: str, data: str = "") -> str:
    return (
        "       IDENTIFICATION DIVISION.\n"
        "       PROGRAM-ID. RT.\n"
        + ("       DATA DIVISION.\n       WORKING-STORAGE SECTION.\n" + data
           if data else "")
        + "       PROCEDURE DIVISION.\n" + proc_body
    )


def _roundtrip(program):
    # Through real JSON text, not just dicts - the file is the contract.
    return program_from_dict(json.loads(json.dumps(program_to_dict(program))))


def test_roundtrip_is_equal_and_rebuilds_real_nodes():
    prog = parse_program(_wrap(
        "       0000-MAIN.\n"
        "           IF WS-X = 1\n"
        "               PERFORM 1000-A UNTIL WS-EOF = 'Y'\n"
        "           ELSE\n"
        "               EVALUATE WS-T\n"
        "                   WHEN 'D' PERFORM 1000-A\n"
        "                   WHEN OTHER CONTINUE\n"
        "               END-EVALUATE\n"
        "           END-IF\n"
        "           STOP RUN.\n"
        "       1000-A.\n"
        "           READ CUST-FILE AT END MOVE 'Y' TO WS-EOF END-READ\n"
        "           ADD 1 TO WS-N ON SIZE ERROR MOVE 0 TO WS-N END-ADD.\n",
        data=("       01  WS-X PIC 9.\n"
              "       01  WS-N PIC S9(5)V99 COMP-3 VALUE 0.\n"
              "       01  WS-EOF PIC X VALUE 'N'.\n"
              "           88  EOF-YES VALUE 'Y'.\n")))
    back = _roundtrip(prog)
    # Dataclass equality is deep - one comparison covers every node and field. It is
    # also exactly what a consumer needs: the rehydrated Program IS the parsed one.
    assert back == prog
    # And the nodes are REAL dataclass instances (consumers dispatch on isinstance).
    ev = back.paragraphs[0].statements[0].else_body[0]
    assert isinstance(ev, EvaluateStmt)
    handled = back.paragraphs[1].statements[1]
    assert isinstance(handled, HandledStmt) and isinstance(handled.inner, Action)


def test_tuple_fields_come_back_as_tuples():
    prog = parse_program(_wrap(
        "       0000-MAIN.\n"
        "           EVALUATE WS-T\n"
        "               WHEN 'A' CONTINUE\n"
        "               WHEN 'B' CONTINUE\n"
        "           END-EVALUATE\n"
        "           EXEC CICS HANDLE CONDITION NOTFND(9000-NF) END-EXEC\n"
        "           SEARCH WS-TBL\n"
        "               AT END CONTINUE\n"
        "               WHEN WS-KEY(WS-I) = 'X' CONTINUE\n"
        "           END-SEARCH\n"
        "           ALTER 1000-SW TO PROCEED TO 2000-B.\n"
        "       9000-NF.\n"
        "           STOP RUN.\n"))
    back = _roundtrip(prog)
    ev = back.paragraphs[0].statements[0]
    assert isinstance(ev, EvaluateStmt)
    assert all(isinstance(w, tuple) for w in ev.whens)
    search = next(s for s in back.paragraphs[0].statements if isinstance(s, SearchStmt))
    assert all(isinstance(w, tuple) for w in search.whens)
    alter = next(s for s in back.paragraphs[0].statements if isinstance(s, AlterStmt))
    assert alter.pairs == [("1000-SW", "2000-B")]
    assert all(isinstance(p, tuple) for p in alter.pairs)
    assert back.cics_handlers == [("NOTFND", "9000-NF")]
    assert all(isinstance(h, tuple) for h in back.cics_handlers)


def test_data_by_name_holds_the_same_objects_as_data_items():
    prog = parse_program(_wrap(
        "       0000-MAIN.\n           STOP RUN.\n",
        data=("       01  WS-REC.\n"
              "           05  WS-A PIC X(3).\n"
              "           05  WS-B PIC 9(4) COMP.\n")))
    back = _roundtrip(prog)
    assert set(back.data_by_name) == set(prog.data_by_name)
    for name, item in back.data_by_name.items():
        # identity, not equality: a consumer that reaches an item through either path
        # must see ONE object, or a mutation through one is invisible through the other.
        assert any(item is it for it in back.data_items), name
    # ...and the typed PicType came back as a real PicType.
    assert back.data_by_name["WS-B"].type.usage == "COMP"


def test_provenance_fields_ride_along():
    prog = parse_program(_wrap(
        "       0000-MAIN.\n"
        "           MOVE 1 TO WS-X\n"
        "           STOP RUN.\n"))
    back = _roundtrip(prog)
    assert back.paragraphs[0].line == prog.paragraphs[0].line
    assert [s.line for s in back.paragraphs[0].statements] == \
           [s.line for s in prog.paragraphs[0].statements]


def test_write_and_open_bundle(tmp_path):
    src = _wrap("       0000-MAIN.\n           STOP RUN.\n")
    prog = parse_program(src)
    out = tmp_path / "rt.parse.json"
    write_parse_bundle(out, source_name="rt.cbl", source_text=src,
                       fmt=SourceFormat.FIXED, program=prog,
                       copybook_errors=[("CUSTREC", "boom")])
    pb = open_parse_bundle(out)
    assert pb.source_name == "rt.cbl"
    assert pb.fmt is SourceFormat.FIXED
    assert pb.copybook_errors == (("CUSTREC", "boom"),)
    assert pb.program() == prog
    # check_source: the exact text passes, anything else refuses.
    pb.check_source(src)
    with pytest.raises(ParseBundleError, match="not the source"):
        pb.check_source(src + "\n")


def test_bundle_bytes_are_deterministic(tmp_path):
    src = _wrap("       0000-MAIN.\n           STOP RUN.\n")
    a, b = tmp_path / "a.json", tmp_path / "b.json"
    for out in (a, b):
        write_parse_bundle(out, source_name="rt.cbl", source_text=src,
                           fmt=SourceFormat.FIXED, program=parse_program(src))
    assert a.read_bytes() == b.read_bytes()


def _doc(tmp_path, mutate):
    src = _wrap("       0000-MAIN.\n           STOP RUN.\n")
    out = tmp_path / "m.parse.json"
    write_parse_bundle(out, source_name="rt.cbl", source_text=src,
                       fmt=SourceFormat.FIXED, program=parse_program(src))
    doc = json.loads(out.read_text(encoding="utf-8"))
    mutate(doc)
    out.write_text(json.dumps(doc), encoding="utf-8")
    return out


def test_open_refuses_wrong_format(tmp_path):
    out = _doc(tmp_path, lambda d: d.update(format="something-else"))
    with pytest.raises(ParseBundleError, match=FORMAT):
        open_parse_bundle(out)


def test_open_refuses_a_newer_version_rather_than_partially_reading(tmp_path):
    out = _doc(tmp_path, lambda d: d.update(version=VERSION + 1))
    with pytest.raises(ParseBundleError, match="upgrade the tool"):
        open_parse_bundle(out)


def test_decode_refuses_an_unknown_node_type(tmp_path):
    def mutate(d):
        d["program"]["paragraphs"][0]["statements"][0]["t"] = "FutureStmt"
    out = _doc(tmp_path, mutate)
    with pytest.raises(ParseBundleError, match="FutureStmt"):
        open_parse_bundle(out).program()


def test_decode_refuses_a_field_this_model_does_not_have(tmp_path):
    def mutate(d):
        d["program"]["paragraphs"][0]["statements"][0]["novel_field"] = 1
    out = _doc(tmp_path, mutate)
    with pytest.raises(ParseBundleError, match="different version"):
        open_parse_bundle(out).program()


def test_open_refuses_a_missing_file(tmp_path):
    with pytest.raises(ParseBundleError, match="no such parse bundle"):
        open_parse_bundle(tmp_path / "absent.json")


def test_producer_cli_writes_a_bundle_the_reader_accepts(tmp_path):
    from cobol_parse.cli import run
    src = tmp_path / "hello.cbl"
    src.write_text(_wrap(
        "       0000-MAIN.\n"
        "           DISPLAY 'HI'\n"
        "           STOP RUN.\n"), encoding="utf-8")
    out = tmp_path / "hello.parse.json"
    rc = run([str(src), "-o", str(out), "--no-fetch", "-q"])
    assert rc == 0
    pb = open_parse_bundle(out)
    assert pb.program().program_id == "RT"
    # The CLI reads bytes and decodes them itself (decode_member) - compare through the
    # same door, or Windows newline translation manufactures a false mismatch.
    from cobol_xstate_core.artifact_service import decode_member
    pb.check_source(decode_member(src.read_bytes()))


def test_producer_cli_refuses_gather_only(tmp_path):
    from cobol_parse.cli import run
    src = tmp_path / "hello.cbl"
    src.write_text(_wrap("       0000-MAIN.\n           STOP RUN.\n"),
                   encoding="utf-8")
    rc = run([str(src), "--gather-only", str(tmp_path / "b"), "-q"])
    assert rc == 2


def test_v2_fields_round_trip_and_v1_bundles_still_open(tmp_path):
    """VERSION 2 added the whole-stream SQL declarations and the column-list-less
    INSERT's table/VALUES slots; a v1 bundle (without them) still opens, the missing
    fields taking their dataclass defaults."""
    src = (
        "       IDENTIFICATION DIVISION.\n"
        "       PROGRAM-ID. V2RT.\n"
        "       DATA DIVISION.\n"
        "       WORKING-STORAGE SECTION.\n"
        "       01  WS-E PIC X(8).\n"
        "           EXEC SQL DECLARE C1 CURSOR FOR\n"
        "               SELECT E FROM T_E\n"
        "           END-EXEC.\n"
        "           EXEC SQL DECLARE T_E TABLE ( E CHAR(8) ) END-EXEC.\n"
        "       PROCEDURE DIVISION.\n"
        "       0000-MAIN.\n"
        "           EXEC SQL INSERT INTO T_E VALUES (:WS-E) END-EXEC\n"
        "           STOP RUN.\n"
    )
    prog = parse_program(src)
    assert prog.sql_cursors and prog.declared_tables
    back = _roundtrip(prog)
    assert back == prog
    assert back.sql_cursors == prog.sql_cursors
    assert back.declared_tables == prog.declared_tables
    ins = back.paragraphs[0].statements[0]
    assert ins.table == "T_E" and ins.values_list == ["WS-E"]
    # A v1 bundle: strip the v2 fields and mark it version 1 - it must still open.
    out = tmp_path / "v1.parse.json"
    write_parse_bundle(out, source_name="v.cbl", source_text=src,
                       fmt=SourceFormat.FIXED, program=prog)
    doc = json.loads(out.read_text(encoding="utf-8"))
    doc["version"] = 1
    for key in ("sql_cursors", "declared_tables"):
        del doc["program"][key]
    for st in doc["program"]["paragraphs"][0]["statements"]:
        st.pop("table", None)
        st.pop("values_list", None)
    out.write_text(json.dumps(doc), encoding="utf-8")
    old = open_parse_bundle(out).program()
    assert old.sql_cursors == [] and old.declared_tables == []
    assert old.paragraphs[0].statements[0].table is None
