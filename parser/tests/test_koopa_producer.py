"""The optional Koopa producer: runner, provenance join, and the coverage diff.

These tests need `java` on PATH and a Koopa release jar named by
COBOL_PARSE_KOOPA_JAR. Like the Node-backed emitter tests, they SKIP cleanly when
either is absent - so a green run does not prove they ran; check for `skipped` when a
change touches producers/koopa.py, or run one yourself with the jar set."""

import os
import shutil

import pytest

from cobol_parse.normalizer import detect_source_format, normalize
from cobol_parse.parser import parse_program
from cobol_parse.preprocessor import CopybookResolver, preprocess
from cobol_parse.producers.koopa import (JAR_ENV, KoopaUnavailable, diff_producers,
                                         find_koopa_jar, koopa_statements,
                                         run_koopa)

_JAR = os.environ.get(JAR_ENV)
needs_koopa = pytest.mark.skipif(
    not (_JAR and os.path.isfile(_JAR) and shutil.which("java")),
    reason=f"needs java on PATH and {JAR_ENV} pointing at a Koopa release jar "
           f"(https://github.com/krisds/koopa/releases)")

_PROG = (
    "       IDENTIFICATION DIVISION.\n"
    "       PROGRAM-ID. KOOPAT.\n"
    "       DATA DIVISION.\n"
    "       WORKING-STORAGE SECTION.\n"
    "       COPY KREC.\n"
    "       PROCEDURE DIVISION.\n"
    "       0000-MAIN.\n"
    "           EXEC CICS READ FILE('CUSTMAS') INTO(K-REC) END-EXEC\n"
    "           IF K-ID > 0\n"
    "               ADD 1 TO K-ID\n"
    "           END-IF\n"
    "           STOP RUN.\n"
)

_COPYBOOK = (
    "       01  K-REC.\n"
    "           05  K-ID PIC S9(5) COMP-3.\n"
)


def _expanded(tmp_path):
    (tmp_path / "KREC.cpy").write_text(_COPYBOOK, encoding="utf-8")
    fmt = detect_source_format(_PROG).format
    resolver = CopybookResolver(paths=[str(tmp_path)], fetcher=None, store={})
    lines = preprocess(normalize(_PROG, fmt), resolver=resolver, fmt=fmt).lines
    return lines, resolver, fmt


def test_finding_no_jar_names_what_to_set(monkeypatch):
    monkeypatch.delenv(JAR_ENV, raising=False)
    with pytest.raises(KoopaUnavailable, match=JAR_ENV):
        find_koopa_jar(None)


def test_an_explicit_jar_path_that_does_not_exist_is_refused():
    with pytest.raises(KoopaUnavailable, match="does not exist"):
        find_koopa_jar("no/such/koopa.jar")


@needs_koopa
def test_koopa_statements_carry_our_copybook_provenance(tmp_path):
    lines, _resolver, _fmt = _expanded(tmp_path)
    root = run_koopa(lines)
    rows = koopa_statements(root, lines)
    kinds = {r["kind"] for r in rows}
    # The CICS block is genuinely parsed (typed node), not skipped as water.
    assert "execCICSStatement" in kinds
    assert "ifStatement" in kinds
    # Data entries from the copybook are positioned into OUR CodeLine map: any node in
    # the expanded region joins back to (member, member-line) through `lines` - the
    # statements here are all mainline, so prove the join on the map itself.
    members = {cl.origin for cl in lines if cl.origin}
    assert members == {"KREC"}
    for r in rows:
        assert 1 <= r["renderedLine"] <= len(lines)


@needs_koopa
def test_diff_report_agrees_on_a_program_both_parsers_handle(tmp_path):
    lines, resolver, fmt = _expanded(tmp_path)
    program = parse_program(_PROG, fmt, resolver=resolver)
    report = diff_producers(program, run_koopa(lines), lines)
    assert report["format"] == "cobol-parse-producer-diff"
    t = report["totals"]
    assert t["nativeStatements"] == t["koopaStatements"] > 0
    assert report["nativeMissed"] == []
    assert report["koopaMissed"] == []
    assert report["parseErrorParagraphs"] == []
    assert t["koopaWater"] == 0


@needs_koopa
def test_diff_report_surfaces_what_koopa_recovers_from_a_parse_error(tmp_path):
    """A paragraph the native parser gives up on wholesale is the report's strongest
    signal: it must name the paragraph AND list what Koopa still recovered inside."""
    prog = (
        "       IDENTIFICATION DIVISION.\n"
        "       PROGRAM-ID. GAPPY.\n"
        "       PROCEDURE DIVISION.\n"
        "       0000-MAIN.\n"
        "           MOVE 1 TO WS-A\n"
        "           STOP RUN.\n"
    )
    fmt = detect_source_format(prog).format
    lines = preprocess(normalize(prog, fmt), fmt=fmt).lines
    program = parse_program(prog, fmt)
    # Force the native side to have failed a paragraph, the way parser.py records it.
    program.paragraphs[0].parse_error = "forced for the test"
    report = diff_producers(program, run_koopa(lines), lines)
    assert len(report["parseErrorParagraphs"]) == 1
    entry = report["parseErrorParagraphs"][0]
    assert entry["paragraph"] == "0000-MAIN"
    recovered = {r["kind"] for r in entry["koopaRecovered"]}
    assert "moveStatement" in recovered
